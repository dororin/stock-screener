# sync_holdings_jp.py

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
from data_access.sheets_api import save_holdings_to_sheets, upload_sync_log_to_drive


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
            if hresult in [-2147418111, -2147352567]:
                print(f"    ⚠️ [COMビジー検出] リトライ #{attempt}/{max_retries}. {delay}秒待機後に再試行...")
                time.sleep(delay)
            else:
                raise e
        except Exception as e:
            raise e
    if last_ex:
        raise last_ex


# =====================================================================
# 💼 保有銘柄（=RssPositionList()）取得・Google Sheets保存コア関数
# =====================================================================
def collect_and_save_holdings(excel=None, log_func=None) -> bool:
    """
    楽天RSSの公式保有銘柄展開関数 =RssPositionList() を利用して
    Excel上にポジション一覧を展開させ、Google Sheets (my_holdings) に保存します。
    """
    def _log(msg):
        if log_func:
            log_func(msg)
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [HOLDINGS] {msg}")

    _log("💼 楽天RSSから保有現物証券（PositionList）の取得を開始します...")

    need_close_excel = False
    wb = None
    ws = None

    try:
        if excel is None:
            need_close_excel = True
            try:
                excel = win32com.client.GetActiveObject("Excel.Application")
                excel.DisplayAlerts = False
            except Exception:
                try:
                    excel = win32com.client.GetObject(Class="Excel.Application")
                    excel.DisplayAlerts = False
                except Exception as e:
                    raise RuntimeError("ExcelのCOM接続に失敗しました。MarketSpeed II および Excel を起動して接続をONにしてください。") from e

        def create_wb():
            return excel.Workbooks.Add()
        wb = execute_com_safely(create_wb)

        def add_sheet():
            return wb.Sheets.Add()
        ws = execute_com_safely(add_sheet)

        _log("  📡 セル A1 に =RssPositionList() を書き込み中...")
        def write_formula():
            ws.Cells(1, 1).Value = "=RssPositionList()"
        execute_com_safely(write_formula)

        def force_calculate():
            excel.Calculate()
        execute_com_safely(force_calculate)

        # 展開待機（最大20秒）
        start_time = time.time()
        has_data = False
        while time.time() - start_time < 20.0:
            time.sleep(1.0)
            try:
                val_header = ws.Cells(1, 1).Value
                val_c2 = ws.Cells(2, 1).Value
                val_c3 = ws.Cells(3, 1).Value
                for test_v in [val_c2, val_c3]:
                    if test_v is not None:
                        s = str(test_v).strip()
                        # プレースホルダー（-------等）やエラーコード以外の有効値を検出
                        if s != "" and not s.startswith("-214") and not s.startswith("#") and not set(s) <= {"-"}:
                            has_data = True
                            break
                if has_data:
                    time.sleep(0.5)  # 展開安定化のためわずかに待機
                    break
            except Exception:
                pass

        used_range = ws.UsedRange
        max_row = used_range.Rows.Count
        max_col = used_range.Columns.Count

        if max_row < 2 or not has_data:
            _log("  ℹ️ 現在、楽天証券口座内に保有している現物証券データはありません。")
            return True

        _log(f"  📊 展開を検知しました（行数: {max_row}, 列数: {max_col}）。メモリ吸い上げ中...")
        def read_matrix():
            return ws.Range(ws.Cells(1, 1), ws.Cells(max_row, max_col)).Value
        matrix = execute_com_safely(read_matrix)

        if not matrix or not isinstance(matrix, tuple):
            _log("  ⚠️ 吸い上げデータが空でした。")
            return False

        # 2次元タプルを行リストへ変換
        all_rows = [list(r) for r in matrix if r is not None]
        if not all_rows:
            _log("  ⚠️ 行データが取得できませんでした。")
            return False

        # 💡 ヘッダー行の自動判定とカラム位置のマッピング
        header_row_idx = -1
        col_indices = {}
        for r_idx, r in enumerate(all_rows[:5]):
            r_strs = [str(c).strip() for c in r if c is not None]
            if any("コード" in s for s in r_strs) and any("数量" in s or "口座" in s or "名称" in s for s in r_strs):
                header_row_idx = r_idx
                for c_idx, c_val in enumerate(r):
                    c_str = str(c_val).strip()
                    if "コード" in c_str: col_indices["code"] = c_idx
                    elif "名称" in c_str or "銘柄名" in c_str: col_indices["name"] = c_idx
                    elif "口座" in c_str: col_indices["account"] = c_idx
                    elif "保有数量" in c_str or "数量" in c_str: col_indices["qty"] = c_idx
                    elif "取得" in c_str: col_indices["buy_price"] = c_idx
                    elif "時価" in c_str and "評価" not in c_str: col_indices["cur_price"] = c_idx
                    elif "時価評価額" in c_str or "評価額" in c_str: col_indices["eval_val"] = c_idx
                    elif "評価損益額" in c_str: col_indices["profit_val"] = c_idx
                    elif "評価損益率" in c_str: col_indices["profit_pct"] = c_idx
                break

        # ヘッダー行が見つからなかった場合のフォールバック（公式18列の既定順序）
        c_code = col_indices.get("code", 0)
        c_name = col_indices.get("name", 1)
        c_account = col_indices.get("account", 2)
        c_qty = col_indices.get("qty", 3)
        c_buy_price = col_indices.get("buy_price", 5)
        c_cur_price = col_indices.get("cur_price", 6)
        c_eval_val = col_indices.get("eval_val", 9)
        c_profit_val = col_indices.get("profit_val", 10)
        c_profit_pct = col_indices.get("profit_pct", 11)

        start_row = (header_row_idx + 1) if header_row_idx != -1 else 1

        clean_rows = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def to_num(val, default=0.0):
            if val is None:
                return default
            s = str(val).replace(",", "").replace("%", "").replace("¥", "").strip()
            try:
                return float(s)
            except ValueError:
                return default

        for r in all_rows[start_row:]:
            if len(r) <= max(c_code, c_qty):
                continue

            raw_code = r[c_code]
            if raw_code is None:
                continue
            code_str = str(raw_code).strip().split(".")[0].replace(" ", "").replace("　", "").upper()

            # プレースホルダー（-----等）やヘッダー行の除外
            if not code_str or code_str.startswith("-") or code_str.startswith("#") or "コード" in code_str:
                continue

            # 証券コード判定（3〜6文字の英数字）
            if not (code_str.isalnum() and 3 <= len(code_str) <= 6):
                continue

            name_val = str(r[c_name]).strip() if (len(r) > c_name and r[c_name] is not None) else ""
            account_val = str(r[c_account]).strip() if (len(r) > c_account and r[c_account] is not None) else "特定"
            
            qty_val = int(to_num(r[c_qty] if len(r) > c_qty else None, 0))
            buy_price_val = to_num(r[c_buy_price] if len(r) > c_buy_price else None, 0.0)
            cur_price_val = to_num(r[c_cur_price] if len(r) > c_cur_price else None, 0.0)
            eval_val = to_num(r[c_eval_val] if len(r) > c_eval_val else None, 0.0)
            profit_val = to_num(r[c_profit_val] if len(r) > c_profit_val else None, 0.0)
            profit_pct = to_num(r[c_profit_pct] if len(r) > c_profit_pct else None, 0.0)

            if qty_val <= 0:
                continue

            clean_rows.append({
                "銘柄コード": code_str,
                "銘柄名": name_val,
                "口座区分": account_val,
                "保有数量": qty_val,
                "取得単価": buy_price_val,
                "現在値": cur_price_val,
                "時価評価額": eval_val,
                "評価損益額": profit_val,
                "評価損益率": profit_pct,
                "更新日時": now_str
            })

        df_holdings = pd.DataFrame(clean_rows)
        if df_holdings.empty:
            _log("  ℹ️ 有効な保有株データは0件でした。")
            return True

        _log(f"  ✅ 抽出成功: 計 {len(df_holdings)} ポジション。Google Sheetsへ保存中...")
        saved = save_holdings_to_sheets(df_holdings)
        if saved:
            _log("  🎉 保有株データのスプレッドシート保存が完了しました！")
            return True
        else:
            _log("  ❌ スプレッドシート保存に失敗しました。")
            return False

    except Exception as e:
        _log(f"  ❌ 保有株同期中にエラーが発生しました: {e}")
        return False

    finally:
        if ws is not None:
            try:
                def clear_contents():
                    ws.Cells.ClearContents()
                execute_com_safely(clear_contents)
            except Exception:
                pass
        if wb is not None:
            try:
                def close_wb():
                    wb.Close(SaveChanges=False)
                execute_com_safely(close_wb)
            except Exception:
                pass
        wb = None
        if need_close_excel:
            excel = None
        gc.collect()


# =====================================================================
# 🚀 単独実行エントリーポイント (python sync_holdings_jp.py)
# =====================================================================
def main():
    print("=====================================================================")
    print("💼 楽天RSS 保有株専用同期スクリプト 起動")
    print(f"🕒 実行開始日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=====================================================================")

    logs_accumulator = []
    def log(msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line)
        logs_accumulator.append(line)

    try:
        success = collect_and_save_holdings(excel=None, log_func=log)
        if success:
            log("🎉 保有株の単独同期が正常に完了しました！")
            upload_sync_log_to_drive(logs_accumulator, is_jp=True, prefix="holdings_sync_SUCCESS")
        else:
            log("❌ 保有株の同期に失敗しました。")
            upload_sync_log_to_drive(logs_accumulator, is_jp=True, prefix="holdings_sync_FAILED")
            sys.exit(1)
    except Exception as ex:
        log(f"🚨 致命的エラー: {ex}")
        log(traceback.format_exc())
        upload_sync_log_to_drive(logs_accumulator, is_jp=True, prefix="holdings_sync_ERROR")
        sys.exit(1)


if __name__ == "__main__":
    main()