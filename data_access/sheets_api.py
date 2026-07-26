# data_access/sheets_api.py

import os
import pandas as pd
import pytz
from datetime import datetime
from google.oauth2.credentials import Credentials as OAuth2Credentials
import gspread
from config import settings

try:
    import streamlit as st
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

def get_oauth2_config() -> dict:
    """認証情報(google_oauth)をst.secretsまたはローカルのsecrets.tomlからロードします。"""
    cfg = None
    if HAS_STREAMLIT:
        try:
            if hasattr(st, "secrets") and "google_oauth" in st.secrets:
                cfg = dict(st.secrets["google_oauth"])
        except Exception:
            pass
    if not cfg:
        try:
            import toml
            secrets_path = os.path.join(settings.PROJECT_ROOT, ".streamlit", "secrets.toml")
            if os.path.exists(secrets_path):
                cfg = toml.load(secrets_path).get("google_oauth")
        except Exception:
            pass
    return cfg

def get_gspread_client():
    """gspreadを使用したシート書き込み用クライアントを作成して返します。"""
    cfg = get_oauth2_config()
    if not cfg or "refresh_token" not in cfg:
        print("❌ [sheets_api] OAuth2の設定(google_oauth)が見つかりません。")
        return None
    try:
        # OAuth2個人アカウントの認証インスタンスを作成してgspreadに適用
        creds = OAuth2Credentials(
            token=None,
            refresh_token=cfg["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"]
        )
        return gspread.authorize(creds)
    except Exception as e:
        print(f"❌ [sheets_api] gspread OAuth2 認証オブジェクトの生成に失敗しました: {e}")
        return None

def get_drive_service():
    """Google Drive API操作用のサービスクライアントを作成して返します。"""
    cfg = get_oauth2_config()
    if not cfg or "refresh_token" not in cfg:
        return None
    try:
        from googleapiclient.discovery import build
        creds = OAuth2Credentials(
            token=None,
            refresh_token=cfg["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"]
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"❌ [sheets_api] Google Drive OAuth2 認証に失敗しました: {e}")
        return None

def upload_sync_log_to_drive(log_lines: list, is_jp: bool = True, prefix: str = "sync") -> str:
    """同期処理中にメモリへ蓄積された詳細ログをバッチアップロードします。"""
    if not log_lines:
        return None
    service = get_drive_service()
    if service is None or not getattr(settings, "LOGS_FOLDER_ID", None):
        return None
    try:
        from googleapiclient.http import MediaInMemoryUpload
        tz = pytz.timezone("Asia/Tokyo") if is_jp else pytz.timezone("America/New_York")
        now_tz = datetime.now(pytz.utc).astimezone(tz)
        filename = f"{prefix}_{now_tz.strftime('%Y-%m-%d_%H%M%S')}.log"
        content = "\n".join(str(line) for line in log_lines)
        file_metadata = {"name": filename, "parents": [settings.LOGS_FOLDER_ID]}
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
        service.files().create(body=file_metadata, media_body=media, fields="id").execute()
        return filename
    except Exception as e:
        print(f"⚠️ ログのアップロードに失敗しました: {e}")
        return None

def get_sector_spreadsheet():
    """各種マスタースプレッドシートを取得してオープンします。"""
    gc = get_gspread_client()
    if gc is None:
        print("❌ [sheets_api] gspreadクライアントの認証に失敗しました。")
        return None
    try:
        # settings.py で解決済みのマスターURLを使用
        return gc.open_by_url(settings.MARKET_DATA_URL)
    except Exception as e:
        print(f"❌ [sheets_api] マスタースプレッドシートのオープンに失敗しました: {e}")
        return None

# --- 🚀 スクリーニング履歴管理 (gspread個人OAuth対応版) ---
def save_history(df: pd.DataFrame) -> str:
    """WVFスクリーナーの結果（履歴）を VWF用スプレッドシートの最初のシートに末尾アペンド上書き保存します。"""
    gc = get_gspread_client()
    if gc is None:
        print("❌ [sheets_api] gspreadクライアントの取得に失敗したため、履歴を保存できません。")
        return None

    screening_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_df = df.copy()
    save_df['screening_id'] = screening_id

    try:
        # 指定されたVWF結果保存シートをオープン
        sh = gc.open_by_url(settings.SPREADSHEET_VWF_URL)
        ws = sh.get_worksheet(0)  # 最初のワークシートをオープン

        # 既存データのロード試行
        try:
            raw_records = ws.get_all_records()
            if raw_records:
                existing_df = pd.DataFrame(raw_records)
                # 古い履歴データに今回の新しいスクリーニング履歴を追加
                updated_data = pd.concat([existing_df, save_df], ignore_index=True)
            else:
                updated_data = save_df
        except Exception:
            updated_data = save_df

        # pandasの特殊オブジェクトやTimestampのシリアライズエラー防止のためのサニタイズ処理
        for col in updated_data.columns:
            if pd.api.types.is_datetime64_any_dtype(updated_data[col]):
                updated_data[col] = updated_data[col].dt.strftime("%Y-%m-%d %H:%M:%S")
        updated_data = updated_data.fillna("")

        # ヘッダー行と値をネストされた配列にパース
        headers = updated_data.columns.tolist()
        rows = [headers] + updated_data.values.tolist()

        # スプレッドシートの一括クリアと最上部からの物理再書き込み（上書き）
        ws.clear()
        ws.update(values=rows, range_name="A1")
        return screening_id
    except Exception as e:
        print(f"❌ [sheets_api] WVF履歴スプレッドシートへの保存書き込みに失敗しました: {e}")
        return None

def get_history_list() -> list:
    """保存されたスクリーニング履歴の screening_id の降順リストを返します。"""
    gc = get_gspread_client()
    if gc is None:
        return []
    try:
        sh = gc.open_by_url(settings.SPREADSHEET_VWF_URL)
        ws = sh.get_worksheet(0)
        raw_records = ws.get_all_records()
        if not raw_records:
            return []
        
        df = pd.DataFrame(raw_records)
        if df.empty or 'screening_id' not in df.columns:
            return []
        return sorted(df['screening_id'].unique().tolist(), reverse=True)
    except Exception as e:
        print(f"❌ [sheets_api] 履歴リストの取得に失敗しました: {e}")
        return []

def load_history(screening_id: str) -> pd.DataFrame:
    """指定された screening_id の時系列スクリーニング履歴を復元・ロードします。"""
    gc = get_gspread_client()
    if gc is None:
        return pd.DataFrame()
    try:
        sh = gc.open_by_url(settings.SPREADSHEET_VWF_URL)
        ws = sh.get_worksheet(0)
        raw_records = ws.get_all_records()
        if not raw_records:
            return pd.DataFrame()
        
        df = pd.DataFrame(raw_records)
        target_df = df[df['screening_id'] == screening_id].copy()
        
        if not target_df.empty and 'コード' in target_df.columns:
            target_df['コード'] = target_df['コード'].astype(str).str.replace(r'\.0$', '', regex=True)
        if not target_df.empty and 'お気に入り' not in target_df.columns:
            target_df['お気に入り'] = False
        return target_df
    except Exception as e:
        print(f"❌ [sheets_api] 履歴({screening_id})のデータ復元に失敗しました: {e}")
        return pd.DataFrame()

# --- セクター定義シート連携 ---
def load_sector_master_from_sheets(is_jp: bool) -> dict:
    """セクターと構成ティッカーの対応マップをSheetsからロードします。"""
    sh = get_sector_spreadsheet()
    default_sectors = settings.JP_SECTORS if is_jp else settings.US_SECTORS
    if sh is None:
        return default_sectors
    
    sheet_name = "sector_JP" if is_jp else "sector_US"
    try:
        hidden_sectors = set()
        try:
            ws_manual = sh.worksheet(settings.EXTRA_TICKERS_SHEET)
            manual_records = ws_manual.get_all_values()
            if manual_records and len(manual_records) > 1:
                manual_headers = [str(h).strip() for h in manual_records[0]]
                sec_col_idx = next((i for i, h in enumerate(manual_headers) if h in ["セクター名", "sector", "sector_name"]), -1)
                hide_col_idx = next((i for i, h in enumerate(manual_headers) if h in ["非表示", "hidden", "is_hidden"]), -1)
                
                if sec_col_idx != -1 and hide_col_idx != -1:
                    for row in manual_records[1:]:
                        if len(row) > max(sec_col_idx, hide_col_idx):
                            sec_val = str(row[sec_col_idx]).strip()
                            hide_val = str(row[hide_col_idx]).strip().upper()
                            if sec_val and hide_val in ["ON", "TRUE", "YES"]:
                                hidden_sectors.add(sec_val)
        except Exception:
            pass

        ws = sh.worksheet(sheet_name)
        records = ws.get_all_records()
        if not records:
            return default_sectors
        
        df = pd.DataFrame(records)
        df.columns = [str(c).strip() for c in df.columns]
        
        col_map = {}
        for c in df.columns:
            if c in ["セクター名", "sector", "sector_name"]:
                col_map[c] = "sector"
            elif c in ["銘柄コード", "code", "ticker", "コード"]:
                col_map[c] = "code"
            
        df = df.rename(columns=col_map)
        if "sector" not in df.columns or "code" not in df.columns:
            return default_sectors
            
        result = {}
        for _, row in df.iterrows():
            sec = str(row["sector"]).strip()
            code = str(row["code"]).strip().split(".")[0]
            
            if sec in hidden_sectors:
                continue
                
            if sec and code:
                result.setdefault(sec, []).append(code)
        return result if result else default_sectors
    except Exception:
        return default_sectors

# --- ウォッチリスト連携 ---
def load_watchlist_from_sheets() -> dict:
    sh = get_sector_spreadsheet()
    if sh is None:
        return {}
    try:
        ws = sh.worksheet(settings.WATCHLIST_SHEET_NAME)
        records = ws.get_all_records()
        result = {}
        for row in records:
            code = str(row.get("code", "")).strip()
            name = str(row.get("name", "")).strip()
            if code:
                result[code] = name
        return result
    except Exception:
        return {}

def save_watchlist_to_sheets(watchlist: dict):
    sh = get_sector_spreadsheet()
    if sh is None:
        return
    try:
        try:
            ws = sh.worksheet(settings.WATCHLIST_SHEET_NAME)
        except Exception:
            ws = sh.add_worksheet(title=settings.WATCHLIST_SHEET_NAME, rows=200, cols=2)
        rows = [["code", "name"]] + [[code, name] for code, name in watchlist.items()]
        ws.clear()
        ws.update(rows, "A1")
    except Exception:
        pass

# --- 修復ログ連携 ---
REPAIR_LOG_COLUMNS = ["executed_at", "ticker", "market", "cliff_date", "interval", "before_close", "after_close", "multiplier", "memo"]

def save_repair_log_to_sheets(log_rows: list) -> bool:
    if not log_rows:
        return False
    sh = get_sector_spreadsheet()
    if sh is None:
        return False
    try:
        try:
            ws = sh.worksheet(settings.REPAIR_LOG_SHEET_NAME)
        except Exception:
            ws = sh.add_worksheet(title=settings.REPAIR_LOG_SHEET_NAME, rows=1000, cols=len(REPAIR_LOG_COLUMNS))
            ws.update(values=[REPAIR_LOG_COLUMNS], range_name="A1")

        rows_to_append = [
            [str(row.get(col, "")) for col in REPAIR_LOG_COLUMNS]
            for row in log_rows
        ]
        ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        return True
    except Exception:
        return False

def load_repair_log_from_sheets() -> pd.DataFrame:
    sh = get_sector_spreadsheet()
    if sh is None:
        return pd.DataFrame(columns=REPAIR_LOG_COLUMNS)
    try:
        ws = sh.worksheet(settings.REPAIR_LOG_SHEET_NAME)
        raw_values = ws.get_all_values()
        if not raw_values or len(raw_values) < 2:
            return pd.DataFrame(columns=REPAIR_LOG_COLUMNS)
        
        headers = [str(h).strip().lower() for h in raw_values[0]]
        headers = [h.replace("executed＿at", "executed_at") for h in headers]
        data_rows = raw_values[1:]
        df = pd.DataFrame(data_rows, columns=headers)
        
        valid_cols = [c for c in REPAIR_LOG_COLUMNS if c in df.columns]
        df = df[valid_cols]
        
        if "executed_at" in df.columns:
            df["executed_at"] = pd.to_datetime(df["executed_at"], errors="coerce")
        if "cliff_date" in df.columns:
            df["cliff_date"] = pd.to_datetime(df["cliff_date"], errors="coerce")
            
        for col in ["before_close", "after_close", "multiplier"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
                
        return df.sort_values("executed_at", ascending=False).reset_index(drop=True)
    except Exception:
        return pd.DataFrame(columns=REPAIR_LOG_COLUMNS)

# --- 手動登録台帳（extra_tickers）の連携 ---
EXTRA_TICKERS_COLUMNS = ["セクター名", "銘柄コード", "備考", "ETFコード", "ファンド", "非表示"]

def load_extra_tickers_from_sheets() -> pd.DataFrame:
    sh = get_sector_spreadsheet()
    if sh is None:
        return pd.DataFrame(columns=EXTRA_TICKERS_COLUMNS)
    try:
        try:
            ws = sh.worksheet(settings.EXTRA_TICKERS_SHEET)
        except Exception:
            ws = sh.add_worksheet(title=settings.EXTRA_TICKERS_SHEET, rows=500, cols=len(EXTRA_TICKERS_COLUMNS))
            ws.update(values=[EXTRA_TICKERS_COLUMNS], range_name="A1")
            return pd.DataFrame(columns=EXTRA_TICKERS_COLUMNS)
            
        raw_values = ws.get_all_values()
        if not raw_values or len(raw_values) < 2:
            return pd.DataFrame(columns=EXTRA_TICKERS_COLUMNS)
            
        headers = [str(h).strip() for h in raw_values[0]]
        data_rows = raw_values[1:]
        df = pd.DataFrame(data_rows, columns=headers)
        
        for col in EXTRA_TICKERS_COLUMNS:
            if col not in df.columns:
                df[col] = ""
                
        if "銘柄コード" in df.columns:
            df["銘柄コード"] = df["銘柄コード"].astype(str).str.strip().str.split(".").str[0]
            
        return df[EXTRA_TICKERS_COLUMNS].reset_index(drop=True)
    except Exception:
        return pd.DataFrame(columns=EXTRA_TICKERS_COLUMNS)

def save_extra_tickers_to_sheets(df: pd.DataFrame):
    sh = get_sector_spreadsheet()
    if sh is None:
        return
    try:
        try:
            ws = sh.worksheet(settings.EXTRA_TICKERS_SHEET)
        except Exception:
            ws = sh.add_worksheet(title=settings.EXTRA_TICKERS_SHEET, rows=500, cols=len(EXTRA_TICKERS_COLUMNS))
        
        valid_df = df.copy()
        for col in EXTRA_TICKERS_COLUMNS:
            if col not in valid_df.columns:
                valid_df[col] = ""
        valid_df = valid_df[EXTRA_TICKERS_COLUMNS]
        
        rows = [EXTRA_TICKERS_COLUMNS] + valid_df.values.tolist()
        ws.clear()
        ws.update(rows, "A1")
    except Exception:
        pass


# --- 🚀 フィルタポリシー＆マージ対応型 統合マスタ同期システム（TOPIX500クラウド分離対応版） ---
ETF_MASTER_COLUMNS = ["ETFコード", "セクター名", "フィルターポリシー", "ファンド"]
SECTOR_JP_COLUMNS = ["セクター名", "銘柄コード", "備考", "ETFコード"]
TOPIX500_OUT_COLUMNS = ["銘柄コード", "銘柄名", "規模区分"]

def sync_etf_sectors_consolidated(is_jp: bool = True) -> dict:
    """
    【ステップ1：クラウドマスタ完全同期】
    1. etf_master から自動同期ポリシーをロードしてETF構成をWebスクレイピング取得。
    2. 【日本株限定】JPX公式サイトからTOPIX500リストを自動ダウンロードし、新規「topix500」シートを生成・一括保存。
    3. extra_tickers(手動台帳)の個別設定、およびETF構成銘柄を重複排除マージして sector_JP / sector_US へ保存。
    """
    sh = get_sector_spreadsheet()
    if sh is None:
        return {"error": "スプレッドシートを開けませんでした。"}
        
    sheet_name = "sector_JP" if is_jp else "sector_US"
    etf_master_sheet_name = "etf_master"
    topix500_sheet_name = "topix500"
    
    # 必要シートの自動チェックと生成
    try:
        ws_master = sh.worksheet(etf_master_sheet_name)
    except Exception:
        ws_master = sh.add_worksheet(title=etf_master_sheet_name, rows=200, cols=len(ETF_MASTER_COLUMNS))
        ws_master.update(values=[ETF_MASTER_COLUMNS], range_name="A1")
        default_samples = [
            ["2646", "メタルビジネス", "TOPIX500", "Global X"],
            ["2644", "半導体", "TOPIX_SMALL1", "Global X"]
        ]
        ws_master.update(values=default_samples, range_name="A2")
        
    try:
        ws_manual = sh.worksheet(settings.EXTRA_TICKERS_SHEET)
    except Exception:
        ws_manual = sh.add_worksheet(title=settings.EXTRA_TICKERS_SHEET, rows=500, cols=len(EXTRA_TICKERS_COLUMNS))
        ws_manual.update(values=[EXTRA_TICKERS_COLUMNS], range_name="A1")
        
    try:
        ws_out = sh.worksheet(sheet_name)
    except Exception:
        ws_out = sh.add_worksheet(title=sheet_name, rows=2000, cols=len(SECTOR_JP_COLUMNS))
        ws_out.update(values=[SECTOR_JP_COLUMNS], range_name="A1")

    # TOPIX500用シートオブジェクトの生成・確認は、日本株(is_jp=True)の時のみ制限して実行
    if is_jp:
        try:
            ws_topix500 = sh.worksheet(topix500_sheet_name)
        except Exception:
            ws_topix500 = sh.add_worksheet(title=topix500_sheet_name, rows=1000, cols=len(TOPIX500_OUT_COLUMNS))
            ws_topix500.update(values=[TOPIX500_OUT_COLUMNS], range_name="A1")

    sync_results = {}

    # ────── 1. 【クラウド側自動取得】JPX公式のTOPIX500 Excelをロード ──────
    # 日本株（is_jp=True）の場合のみに完全限定化
    if is_jp:
        import requests
        try:
            print("[CONSOLE_DEBUG] [SHEETS_SYNC] JPX公式サイトからTOPIX500リストを自動ダウンロード中...")
            resp = requests.get(settings.JPX_URL, timeout=15)
            if resp.status_code == 200:
                df_jpx = pd.read_excel(resp.content)
                df_scale = df_jpx.iloc[:, [1, 2, 9]].copy()
                df_scale.columns = ['symbol', 'name', 'scale_type']
                target_scales = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
                
                topix500_df = df_scale[df_scale['scale_type'].isin(target_scales)].copy()
                
                # symbol列を文字列型に変換
                def clean_symbol(val):
                    if pd.isna(val):
                        return ""
                    val_str = str(val).strip()
                    if val_str.endswith(".0"):
                        val_str = val_str[:-2]
                    return val_str

                topix500_df['symbol'] = topix500_df['symbol'].apply(clean_symbol)
                
                # 正規表現で「4桁の半角英数字（例: 7203, 285A）」のみを完全に抽出
                topix500_df = topix500_df[topix500_df['symbol'].str.match(r'^[0-9A-Za-z]{4}$')].copy()
                topix500_df['symbol'] = topix500_df['symbol'].str.upper()
                topix500_df = topix500_df.fillna("")
                
                # topix500 シートへ一括更新
                topix500_values = [TOPIX500_OUT_COLUMNS]
                for _, r in topix500_df.iterrows():
                    topix500_values.append([r['symbol'], r['name'], r['scale_type']])
                
                ws_topix500.clear()
                ws_topix500.update(topix500_values, "A1")
                sync_results["TOPIX500 (JPX)"] = f"同期成功 ({len(topix500_df)}銘柄を 'topix500' シートへ保存完了)"
            else:
                sync_results["TOPIX500 (JPX)"] = "⚠️ JPXダウンロード失敗（ステータスコード異常）"
        except Exception as e:
            sync_results["TOPIX500 (JPX)"] = f"❌ JPX自動取得中にエラー: {e}"

    # ────── 2. ETF構成銘柄および手動セクターのマージ処理 ──────
    master_values = ws_master.get_all_values()
    if not master_values or len(master_values) < 2:
        return {"info": "etf_master シートが空のため、セクター自動同期はスキップされました。"}
        
    master_headers = [str(h).strip() for h in master_values[0]]
    col_etf_idx = next((i for i, h in enumerate(master_headers) if h in ["ETFコード", "etf", "etf_code"]), -1)
    col_sec_idx = next((i for i, h in enumerate(master_headers) if h in ["セクター名", "sector", "sector_name"]), -1)
    col_policy_idx = next((i for i, h in enumerate(master_headers) if h in ["フィルターポリシー", "policy", "filter_policy"]), -1)
    col_fund_idx = next((i for i, h in enumerate(master_headers) if h in ["ファンド", "fund", "fund_name"]), -1)
    
    if col_etf_idx == -1 or col_sec_idx == -1:
        return {"error": "etf_master シートに必要なカラム（ETFコード、セクター名）が存在しません。"}
        
    etf_targets = []
    for row in master_values[1:]:
        if len(row) > max(col_etf_idx, col_sec_idx):
            etf_code = str(row[col_etf_idx]).strip().split(".")[0]
            sec_name = str(row[col_sec_idx]).strip()
            policy = str(row[col_policy_idx]).strip().upper() if col_policy_idx != -1 and len(row) > col_policy_idx else "TOPIX500"
            fund_val = str(row[col_fund_idx]).strip() if col_fund_idx != -1 and len(row) > col_fund_idx else ""
            if etf_code and sec_name:
                etf_targets.append({
                    "etf_code": etf_code,
                    "sector_name": sec_name,
                    "policy": policy,
                    "fund_val": fund_val
                })
                
    from core.collector import fetch_etf_constituents, get_jpx_scale_map
    scale_map = get_jpx_scale_map()
    
    auto_rows = []
    downloaded_cache = {}
    
    # ETF構成のダウンロード
    for target in etf_targets:
        etf_code = target["etf_code"]
        fund_val = target["fund_val"]
        sec_name = target["sector_name"]
        
        if etf_code not in downloaded_cache:
            try:
                constituents = fetch_etf_constituents(etf_code, fund_provider=fund_val)
            except Exception as ex:
                return {"error": f"ETFコード [{etf_code}] ({sec_name}) の構成銘柄取得処理で例外エラーが発生したため同期を安全に中断しました。詳細: {ex}"}
                
            if not constituents or len(constituents) == 0:
                return {"error": f"ETFコード [{etf_code}] ({sec_name}) の構成銘柄データを取得できませんでした。既存データを保護するため、処理を強制中断しました。"}
                
            downloaded_cache[etf_code] = constituents
            
    for target in etf_targets:
        etf_code = target["etf_code"]
        sec_name = target["sector_name"]
        policy = target["policy"]
        constituents = downloaded_cache.get(etf_code)
        
        filtered_count = 0
        if policy.startswith("TOP") and policy[3:].isdigit():
            top_n = int(policy[3:])
            sub_consts = {k: v for i, (k, v) in enumerate(constituents.items()) if i < top_n}
        else:
            sub_consts = {}
            for code, name in constituents.items():
                m_scale = scale_map.get(code, "Others")
                
                if policy == "TOPIX100":
                    if m_scale in ["TOPIX Core30", "TOPIX Large70"]:
                        sub_consts[code] = name
                elif policy == "TOPIX_SMALL1":
                    if m_scale in ["TOPIX Small1"]:
                        sub_consts[code] = name
                elif policy in ["ALL", "NONE"]:
                    sub_consts[code] = name
                else: # デフォルト TOPIX500
                    if m_scale in ["TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"]:
                        sub_consts[code] = name
                        
        for code, name in sub_consts.items():
            auto_rows.append({
                "sector": sec_name,
                "code": code,
                "memo": name,
                "etf": etf_code
            })
            filtered_count += 1
            
        sync_results[sec_name] = f"同期成功 ({filtered_count} / {len(constituents)}銘柄) [ポリシー: {policy}]"
        
    manual_df = load_extra_tickers_from_sheets()
    manual_rows = []
    if not manual_df.empty:
        for _, row in manual_df.iterrows():
            sec_val = str(row.get("セクター名", "")).strip()
            code_val = str(row.get("銘柄コード", "")).strip()
            memo_val = str(row.get("備考", "")).strip()
            etf_val = str(row.get("ETFコード", "")).strip()
            
            if sec_val and code_val:
                manual_rows.append({
                    "sector": sec_val,
                    "code": code_val,
                    "memo": memo_val,
                    "etf": etf_val
                })
                
    final_rows = []
    seen_pairs = set()
    
    for r in manual_rows:
        pair_key = (r["sector"], r["code"])
        if pair_key not in seen_pairs:
            seen_pairs.add(pair_key)
            final_rows.append(r)
            
    for r in auto_rows:
        pair_key = (r["sector"], r["code"])
        if pair_key not in seen_pairs:
            seen_pairs.add(pair_key)
            final_rows.append(r)
            
    # sector_JP / sector_US へ一括上書き出力
    output_values = [SECTOR_JP_COLUMNS]
    for r in final_rows:
        output_values.append([
            r["sector"],
            r["code"],
            r["memo"],
            r["etf"]
        ])
        
    ws_out.clear()
    ws_out.update(output_values, "A1")
    
    return sync_results