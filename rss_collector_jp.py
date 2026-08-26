import os
import sys
import time
import gc
import traceback
from datetime import datetime
import pandas as pd
import win32com.client
import pywintypes

# プロジェクトルートをインポートパスに追加
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

# Streamlitのsecretsを非GUIバッチ環境用にフォールバック
import toml
secrets_path = os.path.join(current_dir, ".streamlit", "secrets.toml")
if os.path.exists(secrets_path):
    try:
        import streamlit as st
        st.secrets = toml.load(secrets_path)
    except Exception:
        pass

from config import settings
from data_access.local_db import save_price_db, load_price_db_ledger
from data_access.sheets_api import (
    get_sector_spreadsheet,
    load_extra_tickers_from_sheets,
    load_sector_master_from_sheets,
    upload_sync_log_to_drive
)

# ─── データ収集の設定パラメータ ───
TIMEFRAMES = ["1d", "60m", "5m", "1m"]

# 楽天RSSの取得制限本数（初回ダウンロード時のフォールバック用物理最大値）
DEFAULT_BARS_LIMIT = {
    "1d": 2500,       # 最大10年
    "60m": 2900,      # 最大2年
    "5m": 1500,       # 最大31日間
    "1m": 2300        # 最大9日間
}

# 楽天RSSのタイムフレーム指定コードへのマッピング
RSS_INTERVAL_MAP = {
    "1d": "D",
    "60m": "60M",
    "5m": "5M",
    "1m": "1M"
}


# ─── 🛠️ COM通信の防護用自動リトライラッパー ───
def execute_com_safely(func, *args, max_retries=5, delay=1.0):
    """COM呼び出し時にビジーや拒否のエラーが発生した場合に安全に待機・再試行するラッパー"""
    last_ex = None
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args)
        except pywintypes.com_error as e:
            last_ex = e
            hresult = e.hresult
            # -2147418111: RPC_E_CALL_REJECTED (呼び出し先が拒否)
            # -2147352567: DISP_E_EXCEPTION (内部例外 / ビジー)
            if hresult in [-2147418111, -2147352567]:
                print(f"    ⚠️ [COMビジー検出] リトライ #{attempt}/{max_retries}. {delay}秒待機後に再試行します。")
                time.sleep(delay)
            else:
                raise e
        except Exception as e:
            raise e
    if last_ex:
        raise last_ex


# ─── 🧹 Excel状態監視ヘルパー ───
def wait_for_excel_ready(excel, timeout=5.0):
    """ExcelのReadyプロパティがTrueを返すまで待機するヘルパー"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            if excel.Ready:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def get_column_letter(col_idx: int) -> str:
    """列インデックス（1, 11, 21...）をExcelの列文字（A, K, U...）に変換します。"""
    temp = col_idx
    letter = ""
    while temp > 0:
        modulo = (temp - 1) % 26
        letter = chr(65 + modulo) + letter
        temp = (temp - modulo) // 26
    return letter


def find_last_row_by_reading(ws, col_idx: int, max_search_row: int) -> int:
    """指定列のデータをメモリに一括で読み込み、Python側で有効な最下部行を判定します。
    ExcelのEnd属性に起因するCOMバインディングエラーを安全に回避します。
    """
    try:
        def read_col():
            return ws.Range(ws.Cells(1, col_idx), ws.Cells(max_search_row, col_idx)).Value
        vals = execute_com_safely(read_col)
        if not vals or not isinstance(vals, tuple):
            return 1
        
        # 下から遡って有効な値（Noneや空文字、Excelのエラー値等以外）を探す
        for r_idx in range(len(vals) - 1, -1, -1):
            val = vals[r_idx][0]
            if val is not None:
                val_str = str(val).strip()
                # 空文字やExcelセルエラーコード（-214... または #）を除外
                if val_str != "" and not val_str.startswith("-214") and not val_str.startswith("#"):
                    return r_idx + 1
    except Exception:
        pass
    return 1


def load_all_collection_tickers_from_sheets() -> list:
    print("📡 [1/5] Google Sheetsから日本株収集対象の監視銘柄リストを読み込み中...")
    tickers = set()

    try:
        sector_master = load_sector_master_from_sheets(is_jp=True)
        for sname, t_list in sector_master.items():
            for t in t_list: tickers.add(str(t).strip())
    except Exception as e:
        print(f"  ⚠️ sector_JPシートの読み込みスキップ: {e}")

    try:
        extra_df = load_extra_tickers_from_sheets()
        if not extra_df.empty:
            for t in extra_df["銘柄コード"].dropna(): tickers.add(str(t).strip())
    except Exception as e:
        print(f"  ⚠️ extra_tickersシートの読み込みスキップ: {e}")

    try:
        sh = get_sector_spreadsheet()
        if sh:
            ws_topix = sh.worksheet("topix500")
            records = ws_topix.get_all_records()
            for r in records:
                code = str(r.get("銘柄コード", "")).strip()
                if code: tickers.add(code)
    except Exception as e:
        print(f"  ⚠️ topix500シートの読み込みスキップ: {e}")

    cleaned_list = sorted(list(tickers))
    print(f"  ✅ 監視ティッカーの統合マージ完了。総計: {len(cleaned_list)} 銘柄")
    return cleaned_list


# ─── 🚀 基準銘柄(1306)を用いた実営業日ベースの実測型バー数算出 ───
def measure_actual_needed_bars_with_benchmark(interval: str, last_updates_map: dict, tickers: list, excel, wb, log_func) -> int:
    default_limit = DEFAULT_BARS_LIMIT[interval]
    
    # 台帳に過去の同期履歴が全くない（初回ダウンロード時）場合はフォールバック
    if not last_updates_map:
        log_func(f"  💡 過去の同期履歴がないため、デフォルト最大値（{default_limit}本）を要求します。")
        return default_limit

    try:
        valid_dates = []
        active_tickers = set(tickers)  # 現在のアクティブな銘柄リストをセット化
        
        for t, d_str in last_updates_map.items():
            # リストから削除されたゴースト銘柄は無視する
            if t not in active_tickers:
                continue
                
            try:
                valid_dates.append(pd.to_datetime(d_str))
            except Exception:
                pass
        
        if not valid_dates:
            log_func(f"  💡 有効な更新日時が存在しないため、デフォルト最大値（{default_limit}本）を要求します。")
            return default_limit
            
        # 安全側に倒すため、全銘柄の中で最も古い最終更新日時を基準点とする
        last_dt = min(valid_dates)
        log_func(f"  🔎 基準とする前回同期日時: {last_dt.strftime('%Y-%m-%d %H:%M:%S')}")

        # 実測テスト用の一時シートを作成
        def add_sheet():
            return wb.Sheets.Add()
        ws = execute_com_safely(add_sheet)
        
        rss_code = RSS_INTERVAL_MAP[interval]
        formula = f'=RssChart(,"1306","{rss_code}",{default_limit})'
        
        # セルへの数式書き込みと強制計算
        def write_test_formula():
            ws.Cells(1, 1).Value = formula
        execute_com_safely(write_test_formula)
        
        def force_calculate():
            excel.Calculate()
        execute_com_safely(force_calculate)
        
        # 展開監視ループ（最大30秒）
        start_time = time.time()
        all_loaded = False
        last_row_limit = 1
        
        while time.time() - start_time < 30.0:
            last_row_limit = find_last_row_by_reading(ws, 4, default_limit + 50)
            
            if last_row_limit >= 3:
                try:
                    # 最初の確定データ行に正しい数値が展開されたか確認
                    val_date = ws.Cells(3, 4).Value
                    val_close = ws.Cells(3, 9).Value # I列終値
                    if val_date is not None and val_close is not None:
                        all_loaded = True
                        break
                except Exception:
                    pass
            time.sleep(1.0)
            
        if not all_loaded:
            log_func("  ⚠️ 基準銘柄(1306)の展開がタイムアウトしました。デフォルト最大値を使用します。")
            try:
                def delete_sheet():
                    ws.Delete()
                execute_com_safely(delete_sheet)
            except Exception:
                pass
            return default_limit
            
        # 展開された日付データを一括抽出
        try:
            def read_dates():
                return ws.Range(ws.Cells(1, 4), ws.Cells(last_row_limit, 4)).Value
            data_range = execute_com_safely(read_dates)
            dates_list = [row[0] for row in data_range if row and row[0] is not None]
        except Exception as e_read:
            log_func(f"  ⚠️ 基準データの読み込みに失敗しました: {e_read}。デフォルト最大値を使用します。")
            try:
                def delete_sheet():
                    ws.Delete()
                execute_com_safely(delete_sheet)
            except Exception:
                pass
            return default_limit
            
        # 一時シートの安全削除
        try:
            def delete_sheet():
                ws.Delete()
            execute_com_safely(delete_sheet)
            wait_for_excel_ready(excel)
            time.sleep(1.0)
        except Exception:
            pass

        # 基準日時（last_dt）より新しい実際の確定バー数をカウント
        actual_needed_bars = 0
        for dt_val in dates_list:
            try:
                dt = pd.to_datetime(dt_val)
                if dt >= last_dt:
                    actual_needed_bars += 1
            except Exception:
                pass
                
        log_func(f"  📈 前回同期以降に発生した実際のバー数（実測）: {actual_needed_bars} 本")
        
        # 1営業日分の最大確定本数（上書きマージン）の決定
        overlap_margins = {
            "1d": 1,
            "60m": 5,
            "5m": 60,
            "1m": 300
        }
        margin = overlap_margins.get(interval, 5)
        
        # 安全バッファ補正（1.2倍）
        limit_bars = int((actual_needed_bars + margin) * 1.2)
        limit_bars = min(max(limit_bars, 10), 3000)
        
        log_func(f"  🎯 決定された安全要求バー数（実測 {actual_needed_bars} + 重複 {margin}）× 1.2バッファ ➔ {limit_bars} 本")
        return limit_bars

    except Exception as ex:
        log_func(f"  ⚠️ 実測算出処理中に致命的例外が発生しました: {ex}。デフォルト最大値を使用します。")
        return default_limit


def collect_data_via_excel_rss(tickers: list, interval: str, limit_bars: int, log_func, excel, wb) -> pd.DataFrame:
    """
    1バッチ内の数式を一括で書き込み、Ready監視を行いながら、
    安全にデータ吸い出しを行います。エラーが発生した場合は一括ロールバックのため例外を投げます。
    """
    rss_code = RSS_INTERVAL_MAP[interval]
    batch_size = 50
    timeout = 120.0  # 通信ラグ対策としてタイムアウトを120秒（2分）に延長
    
    log_func(f"📡 [RSS] 【{interval}】のデータ取得を開始します... (要求バー数: {limit_bars}本 / バッチサイズ: {batch_size})")

    # 数値に変換できるか検証するヘルパー
    def is_valid_numeric(val):
        if val is None:
            return False
        if isinstance(val, (int, float)):
            return True
        try:
            float(str(val).strip().replace(',', ''))
            return True
        except ValueError:
            return False

    all_downloaded_rows = []
    total_tickers = len(tickers)
    col_step = 10 

    for b_idx in range(0, total_tickers, batch_size):
        chunk = tickers[b_idx : b_idx + batch_size]
        batch_num = b_idx//batch_size + 1
        total_batches = (total_tickers-1)//batch_size + 1
        log_func(f"  -> バッチ {batch_num} / {total_batches} ({len(chunk)} 銘柄) を処理中...")

        ws = None
        try:
            # バッチ専用のテンポラリワークシートを安全に追加
            def add_sheet():
                return wb.Sheets.Add()
            ws = execute_com_safely(add_sheet)
            
            # ① Python上で数式行の2次元配列をビルド（1パス化）
            N = len(chunk)
            formulas_row = [["" for _ in range(N * col_step)]]
            for i, ticker in enumerate(chunk):
                col_idx = i * col_step
                formulas_row[0][col_idx] = f'=RssChart(,"{ticker}","{rss_code}",{limit_bars})'
                
            # ② COM通信 1パスで数式を一斉書き込み
            def write_formulas():
                ws.Range(ws.Cells(1, 1), ws.Cells(1, N * col_step)).Value = formulas_row
            execute_com_safely(write_formulas)

            # Excelの強制再計算を実行
            def force_calculate():
                excel.Calculate()
            execute_com_safely(force_calculate)

            # ③ 配列数式の展開状況を監視（COM防護・5.0秒スリープ）
            start_time = time.time()
            loop_cnt = 0
            while True:
                loop_cnt += 1
                all_loaded = True
                elapsed = time.time() - start_time

                # 代表日付列（D列：4列目）の最終行判定
                last_row_limit = find_last_row_by_reading(ws, 4, limit_bars + 50)

                unloaded_tickers = []

                if last_row_limit < 3:
                    all_loaded = False
                    unloaded_tickers = list(chunk)
                else:
                    # バッチ内の全領域を1回で一括取得
                    def get_range_values():
                        return ws.Range(ws.Cells(1, 1), ws.Cells(last_row_limit, N * col_step)).Value
                    all_matrix = execute_com_safely(get_range_values)

                    if not all_matrix or not isinstance(all_matrix, tuple):
                        all_loaded = False
                        unloaded_tickers = list(chunk)
                    else:
                        # メモリ上の2次元タプルを走査して数値検証
                        for i, ticker in enumerate(chunk):
                            col_idx = i * col_step
                            open_idx = col_idx + 5   # F列 (始値)
                            close_idx = col_idx + 8  # I列 (終値)
                            
                            val_open = None
                            val_close = None
                            
                            # 下から上へ逆順ループ
                            for r_idx in range(len(all_matrix) - 1, 1, -1):
                                row_data = all_matrix[r_idx]
                                temp_open = row_data[open_idx] if open_idx < len(row_data) else None
                                temp_close = row_data[close_idx] if close_idx < len(row_data) else None
                                
                                if is_valid_numeric(temp_open) and is_valid_numeric(temp_close):
                                    val_open = temp_open
                                    val_close = temp_close
                                    break

                            if val_open is None or val_close is None:
                                all_loaded = False
                                unloaded_tickers.append(ticker)
                        
                if all_loaded:
                    log_func(f"    🎉 [完了] バッチ {batch_num} 内の全銘柄のロード完了が確認されました。")
                    break
                
                # 診断用：ロードが完了していない特定の銘柄リストを出力
                if unloaded_tickers:
                    log_func(f"      ⏳ ロード未完了（待機中）: 残り {len(unloaded_tickers)} / {len(chunk)} 銘柄 {unloaded_tickers[:10]}...")

                if elapsed > timeout:
                    # タイムアウトした場合は一括ロールバックのため例外をスロー
                    raise TimeoutError(
                        f"バッチ {batch_num} のデータ展開が制限時間（{timeout}秒）内に完了しませんでした。\n"
                        f"未展開またはエラーの可能性がある銘柄リスト: {unloaded_tickers}"
                    )
                    
                time.sleep(5.0)

            # ④ データのメモリ一括吸い上げ
            log_func("    📥 メモリ吸い上げ処理を開始します...")
            for i, ticker in enumerate(chunk):
                col_idx = i * col_step + 1
                
                # 各ティッカーの日付列（col_idx + 3）の最終行判定
                last_row = find_last_row_by_reading(ws, col_idx + 3, limit_bars + 50)
                
                if last_row < 2:
                    continue

                try:
                    def get_ticker_data():
                        return ws.Range(ws.Cells(1, col_idx), ws.Cells(last_row, col_idx + 9)).Value
                    data_range = execute_com_safely(get_ticker_data)
                    
                    if not data_range or not isinstance(data_range, tuple):
                        continue
                    
                    df_item = pd.DataFrame(list(data_range))
                    if len(df_item.columns) >= 10:
                        df_item = df_item.iloc[1:, :]
                        df_extracted = pd.DataFrame()
                        
                        if interval == "1d":
                            df_extracted["date"] = df_item.iloc[:, 3] 
                        else:
                            df_extracted["date"] = df_item.iloc[:, 3].astype(str) + " " + df_item.iloc[:, 4].astype(str)
                        
                        df_extracted["open"] = df_item.iloc[:, 5]   
                        df_extracted["high"] = df_item.iloc[:, 6]   
                        df_extracted["low"] = df_item.iloc[:, 7]    
                        df_extracted["close"] = df_item.iloc[:, 8]  
                        df_extracted["volume"] = df_item.iloc[:, 9] 
                        df_extracted["ticker"] = ticker
                        
                        df_extracted["date"] = pd.to_datetime(df_extracted["date"], errors="coerce", format="mixed")
                        for col in ["open", "high", "low", "close", "volume"]:
                            df_extracted[col] = pd.to_numeric(df_extracted[col], errors="coerce")
                            
                        before_cnt = len(df_extracted)
                        df_extracted = df_extracted.dropna(subset=["date", "close"])
                        after_cnt = len(df_extracted)
                        
                        if not df_extracted.empty:
                            all_downloaded_rows.append(df_extracted)
                            log_func(f"      ✅ {ticker}: {after_cnt:,} 行パース完了")
                    
                    del data_range, df_item, df_extracted
                except Exception as ex:
                    log_func(f"      🚨 銘柄 [{ticker}] の吸い上げ中に例外エラー: {ex}")
                    raise ex

        except Exception as e_batch:
            log_func(f"  ❌ 【システムエラー】バッチ {batch_num} 実行中に致命的なエラーを検出しました: {e_batch}")
            raise e_batch
            
        finally:
            # ⑤ 安全なトピック解除 ＆ ワークシートの即時物理削除（Ready同期待き付き）
            if ws is not None:
                try:
                    log_func("    🧹 楽天RSSのバックグラウンド通信を安全に登録解除中...")
                    clear_row = [["" for _ in range(len(chunk) * col_step)]]
                    
                    def clear_formulas():
                        ws.Range(ws.Cells(1, 1), ws.Cells(1, len(chunk) * col_step)).Value = clear_row
                    execute_com_safely(clear_formulas)
                    
                    execute_com_safely(force_calculate)
                    time.sleep(0.1)
                    
                    def delete_sheet():
                        ws.Delete()
                    execute_com_safely(delete_sheet)
                    log_func("    🧹 バッチ用のテンポラリワークシートを物理削除しました。")
                    
                    # Excelの非同期メモリ整理完了を待機
                    wait_for_excel_ready(excel)
                    time.sleep(1.0)
                except Exception as e_close:
                    log_func(f"    ⚠️ シート削除時にエラー検知（無視して継続します）: {e_close}")
            
            ws = None
            gc.collect()

    if not all_downloaded_rows:
        return pd.DataFrame()

    combined_df = pd.concat(all_downloaded_rows, ignore_index=True)
    return combined_df.sort_values(["ticker", "date"]).reset_index(drop=True)


def main():
    print("=====================================================================")
    print("🚀 楽天RSS・日本株 差分専用データ同期エンジン 起動")
    print(f"🕒 実行開始日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=====================================================================")
    
    logs_accumulator = []
    def log(msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line)
        logs_accumulator.append(line)

    try:
        tickers = load_all_collection_tickers_from_sheets()
        if not tickers:
            log("❌ 収集対象の銘柄が1件も存在しないため、終了します。")
            return

        for interval in TIMEFRAMES:
            log(f"⏱️ 【{interval}】のデータ収集を開始します...")
            
            # 1. 独立台帳（Ledger）をロード
            ledger = load_price_db_ledger(interval, is_jp=True, is_raw=False)
            last_updates_map = ledger.get("last_updates_map", {}) if ledger else {}

            # 2. 起動済みExcelへの接続
            excel = None
            wb = None
            try:
                excel = win32com.client.GetActiveObject("Excel.Application")
                excel.DisplayAlerts = False
            except Exception:
                try:
                    excel = win32com.client.GetObject(Class="Excel.Application")
                    excel.DisplayAlerts = False
                except Exception as e:
                    raise RuntimeError("ExcelのCOM接続に失敗しました。MarketSpeed2およびExcelを手動で立ち上げて接続をONにしてください。") from e

            # Excel自動計算設定
            try:
                excel.Calculation = -4105  # xlCalculationAutomatic
            except Exception:
                pass

            # 一時ブックの作成
            def create_wb():
                return excel.Workbooks.Add()
            wb = execute_com_safely(create_wb)

            # 3. 基準銘柄(1306)による実測型の動的バー数算出 (tickersを渡してフィルタリングさせる)
            log("📡 基準銘柄(1306)を用いた動的な必要バー数の算出を開始します...")
            limit_bars = measure_actual_needed_bars_with_benchmark(interval, last_updates_map, tickers, excel, wb, log)

            # 4. 楽天RSSから一括ダウンロード（途中でエラーが出た場合は上位に例外を投げる）
            df_new = collect_data_via_excel_rss(tickers, interval, limit_bars, log, excel, wb)
            
            # セッション終了後に一時ブックをクローズ
            if wb is not None:
                try:
                    def close_wb():
                        wb.Close(SaveChanges=False)
                    execute_com_safely(close_wb)
                except Exception:
                    pass

            wb = None
            excel = None
            gc.collect()

            if df_new.empty:
                log(f"  📥 【{interval}】 新規取得・差分データはありませんでした。")
                continue
                
            log(f"  📥 【{interval}】 ダウンロード成功。新規差分データ: {len(df_new):,} 行")

            # 5. 全件が完全に成功した段階でのみ一括書き込み（途中経過の保存はしない）
            log(f"  🛠️ 【{interval}】 差分ParquetファイルをGoogleドライブへ保存中...")
            success, msg = save_price_db(df_new, interval, is_jp=True, is_raw=False)
            if success:
                log(f"  ✅ 【{interval}】 データの保存同期および台帳更新に成功しました。")
            else:
                raise IOError(f"Googleドライブへの一括保存に失敗しました: {msg}")

        # 全て正常終了時、SUCCESSログを転送
        log("📤 すべての時間足の処理が正常終了しました。同期完了ログをGoogleドライブへ同期アップロード中...")
        log_filename = upload_sync_log_to_drive(logs_accumulator, is_jp=True, prefix="jp_rss_diff_sync_SUCCESS")
        if log_filename:
            print(f"  ✅ 正常完了ログファイル '{log_filename}' をGoogleドライブに正常転送しました。")

    except Exception as ex:
        # 例外トラップ、詳細なエラー情報とスタックトレースの転送
        log("\n🚨 【致命的エラー】同期処理中に回復不能なエラーを検知したため、処理を強制終了しました。")
        log(f"  💥 エラー内容: {ex}")
        log("  📋 発生時の詳細なスタックトレース（Traceback）を記録します:")
        
        tb_str = traceback.format_exc()
        for line in tb_str.splitlines():
            log(f"    {line}")
            
        log("\n📤 異常終了に伴い、エラー詳細ログファイルをGoogleドライブへ強制アップロード中...")
        try:
            log_filename = upload_sync_log_to_drive(logs_accumulator, is_jp=True, prefix="jp_rss_diff_sync_ERROR")
            if log_filename:
                print(f"  ✅ エラー詳細ログファイル '{log_filename}' をGoogleドライブに強制転送完了しました。")
        except Exception as e_log:
            print(f"  ⚠️ クラウドへのエラーログ強制アップロード中に例外を検知しました: {e_log}")
            
        print("\n=====================================================================")
        print("❌ 同期処理が異常終了しました。Googleドライブのエラーログを確認してください。")
        print("=====================================================================")
        sys.exit(1)


if __name__ == "__main__":
    main()