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
        # Excelインスタンスが渡されていない場合は自前で接続
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

        # 展開待機（最大15秒）
        start_time = time.time()
        has_data = False
        while time.time() - start_time < 15.0:
            time.sleep(1.0)
            try:
                val_header = ws.Cells(1, 1).Value
                val_code = ws.Cells(2, 1).Value
                if val_header is not None and val_code is not None:
                    code_str = str(val_code).strip()
                    if code_str != "" and not code_str.startswith("-214") and not code_str.startswith("#"):
                        has_data = True
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

        headers = [str(c).strip() for c in matrix[0] if c is not None]
        data_rows = matrix[1:]

        df_raw = pd.DataFrame(list(data_rows), columns=headers[:len(data_rows[0])])
        clean_rows = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for _, r in df_raw.iterrows():
            code_raw = str(r.iloc[0]).strip().split(".")[0].upper()
            if not code_raw or not code_raw.isalnum() or len(code_raw) > 5:
                continue

            name_val = str(r.iloc[1]).strip() if len(r) > 1 else ""
            account_val = str(r.iloc[2]).strip() if len(r) > 2 else "特定"
            
            qty_val = pd.to_numeric(str(r.iloc[3]).replace(",", "").strip(), errors="coerce") if len(r) > 3 else 0
            buy_price_val = pd.to_numeric(str(r.iloc[5]).replace(",", "").strip(), errors="coerce") if len(r) > 5 else 0.0
            cur_price_val = pd.to_numeric(str(r.iloc[6]).replace(",", "").strip(), errors="coerce") if len(r) > 6 else 0.0
            eval_val = pd.to_numeric(str(r.iloc[9]).replace(",", "").strip(), errors="coerce") if len(r) > 9 else 0.0
            profit_val = pd.to_numeric(str(r.iloc[10]).replace(",", "").strip(), errors="coerce") if len(r) > 10 else 0.0
            profit_pct = pd.to_numeric(str(r.iloc[11]).replace(",", "").replace("%", "").strip(), errors="coerce") if len(r) > 11 else 0.0

            if pd.isna(qty_val) or qty_val <= 0:
                continue

            clean_rows.append({
                "銘柄コード": code_raw,
                "銘柄名": name_val,
                "口座区分": account_val,
                "保有数量": int(qty_val),
                "取得単価": float(buy_price_val) if pd.notna(buy_price_val) else 0.0,
                "現在値": float(cur_price_val) if pd.notna(cur_price_val) else 0.0,
                "時価評価額": float(eval_val) if pd.notna(eval_val) else 0.0,
                "評価損益額": float(profit_val) if pd.notna(profit_val) else 0.0,
                "評価損益率": float(profit_pct) if pd.notna(profit_pct) else 0.0,
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