# data_access/sheets_api.py
import pandas as pd
import pytz
from datetime import datetime
from google.oauth2.service_account import Credentials as SACredentials
import gspread
from config import settings

try:
    import streamlit as st
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

# --- Streamlit標準接続(GSheetsConnection)の初期化 ---
conn = None
if HAS_STREAMLIT:
    try:
        from streamlit_gsheets import GSheetsConnection
        conn = st.connection("gsheets", type=GSheetsConnection)
    except Exception:
        pass

def get_gspread_client():
    """gspreadを使用したシート書き込み用クライアントを作成して返します。"""
    if not HAS_STREAMLIT:
        return None
    try:
        cfg = dict(st.secrets["connections"]["gsheets"])
        sa_info = {k: cfg[k] for k in ["type", "project_id", "private_key_id", "private_key", "client_email", "client_id", "auth_uri", "token_uri"] if k in cfg}
        if "private_key" in sa_info:
            sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
        creds = SACredentials.from_service_account_info(
            sa_info, 
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        )
        return gspread.authorize(creds)
    except Exception:
        return None

def get_drive_service():
    """Google Drive API（ファイルアップロード等）操作用のサービスクライアントを作成して返します。"""
    if not HAS_STREAMLIT:
        return None
    try:
        from googleapiclient.discovery import build
        cfg = dict(st.secrets["connections"]["gsheets"])
        sa_info = {k: cfg[k] for k in ["type", "project_id", "private_key_id", "private_key", "client_email", "client_id", "auth_uri", "token_uri"] if k in cfg}
        if "private_key" in sa_info:
            sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
        creds = SACredentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"❌ [sheets_api] Google Drive サービスの認証に失敗しました: {e}")
        return None

def upload_sync_log_to_drive(log_lines: list, is_jp: bool = True, prefix: str = "sync") -> str:
    """同期処理中にメモリへ蓄積された詳細ログ（文字列のリスト）をバッチアップロードします。"""
    if not log_lines:
        return None

    service = get_drive_service()
    if service is None:
        print("⚠️ [upload_sync_log_to_drive] Google Drive サービスが利用できないため、ログ保存をスキップしました。")
        return None

    folder_id = getattr(settings, "LOGS_FOLDER_ID", None)
    if not folder_id:
        print("⚠️ [upload_sync_log_to_drive] settings.LOGS_FOLDER_ID が未設定のため、ログ保存をスキップしました。")
        return None

    try:
        from googleapiclient.http import MediaInMemoryUpload

        tz = pytz.timezone("Asia/Tokyo") if is_jp else pytz.timezone("America/New_York")
        now_tz = datetime.now(pytz.utc).astimezone(tz)
        filename = f"{prefix}_{now_tz.strftime('%Y-%m-%d_%H%M%S')}.log"

        content = "\n".join(str(line) for line in log_lines)
        file_metadata = {"name": filename, "parents": [folder_id]}
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
        service.files().create(body=file_metadata, media_body=media, fields="id").execute()
        return filename
    except Exception as e:
        print(f"⚠️ [upload_sync_log_to_drive] ログのGoogleドライブへのアップロードに失敗しました: {e}")
        return None

def get_sector_spreadsheet():
    """構成定義等のスプレッドシート（ブック単位）を開いて返します。"""
    gc = get_gspread_client()
    if gc is None:
        print("❌ [sheets_api] gspreadクライアントの認証に失敗しました。")
        return None
    try:
        cfg = st.secrets["connections"]["gsheets"]
        url = cfg.get("sector_spreadsheet", cfg.get("spreadsheet"))
        return gc.open_by_url(url)
    except Exception as e:
        print(f"❌ [sheets_api] スプレッドシートのオープンに失敗しました: {e}")
        return None

# --- スクリーニング履歴管理 ---
def save_history(df: pd.DataFrame) -> str:
    """スクリーニング判定結果を履歴シートに追加保存します。"""
    if conn is None:
        return None
    screening_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_df = df.copy()
    save_df['screening_id'] = screening_id
    try:
        existing_data = conn.read()
        updated_data = pd.concat([existing_data, save_df], ignore_index=True)
    except Exception:
        updated_data = save_df
    conn.update(data=updated_data)
    return screening_id

def get_history_list() -> list:
    """履歴シートから、過去に実行されたユニークなスクリーニング実行日時リストを返します。"""
    if conn is None:
        return []
    try:
        df = conn.read(ttl=0)
        if df is None or df.empty or 'screening_id' not in df.columns:
            return []
        return sorted(df['screening_id'].unique().tolist(), reverse=True)
    except Exception:
        return []

def load_history(screening_id: str) -> pd.DataFrame:
    """指定されたスクリーニング実行IDに紐づく過去の判定データをロードして返します。"""
    if conn is None:
        return pd.DataFrame()
    try:
        df = conn.read()
        target_df = df[df['screening_id'] == screening_id].copy()
        if not target_df.empty and 'コード' in target_df.columns:
            target_df['コード'] = target_df['コード'].astype(str).str.replace(r'\.0$', '', regex=True)
        if not target_df.empty and 'お気に入り' not in target_df.columns:
            target_df['お気に入り'] = False
        return target_df
    except Exception:
        return pd.DataFrame()

# --- セクター定義シート連携（非表示ONセクターを自動除外） ---
def load_sector_master_from_sheets(is_jp: bool) -> dict:
    """Google Sheetsのセクター定義シートから、セクターと構成ティッカーの対応マップを構築して返します。
    extra_tickers（手動台帳）で「非表示: ON」に設定されているセクターは自動的に除外します。"""
    sh = get_sector_spreadsheet()
    default_sectors = settings.JP_SECTORS if is_jp else settings.US_SECTORS
    if sh is None:
        return default_sectors
    
    sheet_name = "sector_JP" if is_jp else "sector_US"
    try:
        # --- 1. 手動定義シート（extra_tickers）から非表示セクター名を特定 ---
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

        # --- 2. 本番シート（sector_JP）から構成銘柄を読み込む ---
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
            
            # 非表示に指定されているセクターはUI用の戻り値辞書から除外
            if sec in hidden_sectors:
                continue
                
            if sec and code:
                result.setdefault(sec, []).append(code)
        return result if result else default_sectors
    except Exception:
        return default_sectors

# --- ウォッチリスト連携 ---
def load_watchlist_from_sheets() -> dict:
    """ウォッチリストシートから、登録銘柄コードと名称の組み合わせ辞書を返します。"""
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
    """現在のウォッチリスト辞書をスプレッドシートに永続化保存します。"""
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
    """データ修復の実行ログをスプレッドシートに安全に追記します。"""
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
    except Exception as e:
        print(f"⚠️ 修復ログ保存エラー: {e}")
        return False

def load_repair_log_from_sheets() -> pd.DataFrame:
    """過去の修復ログ履歴をスプレッドシートからロードしDataFrameとして返します。"""
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
    except Exception as e:
        print(f"❌ [load_repair_log_from_sheets] データのロードに失敗しました: {e}")
        return pd.DataFrame(columns=REPAIR_LOG_COLUMNS)

# --- 🚀 手動登録台帳（extra_tickers）の連携 ---
EXTRA_TICKERS_COLUMNS = ["セクター名", "銘柄コード", "備考", "ETFコード", "ファンド", "非表示"]

def load_extra_tickers_from_sheets() -> pd.DataFrame:
    """手動追加用シート（extra_tickers）からマスタ登録リストをロードします。"""
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
    except Exception as e:
        print(f"❌ [load_extra_tickers_from_sheets] ロード失敗: {e}")
        return pd.DataFrame(columns=EXTRA_TICKERS_COLUMNS)

def save_extra_tickers_to_sheets(df: pd.DataFrame):
    """手動追加の変更定義（extra_tickers）をスプレッドシートに保存します。"""
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
    except Exception as e:
        print(f"❌ [save_extra_tickers_to_sheets] 保存失敗: {e}")

# --- 🚀 フィルタポリシー＆マージ対応型 統合セクター同期システム ---
ETF_MASTER_COLUMNS = ["ETFコード", "セクター名", "フィルターポリシー", "ファンド"]
SECTOR_JP_COLUMNS = ["セクター名", "銘柄コード", "備考", "ETFコード"]

def sync_etf_sectors_consolidated(is_jp: bool = True) -> dict:
    """
    etf_master シートから自動同期対象（ポリシー等）をロードし、
    各ETFの構成銘柄を取得した上でフィルターポリシー（TOPIX100等）を適用。
    さらに、手動台帳シート（extra_tickers）から手動登録の個別銘柄をロードして、
    メモリ上で結合・重複排除（マージ）した完成データを sector_JP (または sector_US) へ一括上書き出力します。
    """
    sh = get_sector_spreadsheet()
    if sh is None:
        return {"error": "スプレッドシートを開けませんでした。"}
        
    sheet_name = "sector_JP" if is_jp else "sector_US"
    etf_master_sheet_name = "etf_master"
    
    # 必要シートの存在チェックと新規自動作成
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

    # etf_master から自動同期設定をロード
    master_values = ws_master.get_all_values()
    if not master_values or len(master_values) < 2:
        return {"info": "etf_master シートが空のため、自動同期の対象はありません。"}
        
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
                
    # Webからの構成ダウンロード＆フィルタリング
    from core.collector import fetch_etf_constituents, get_jpx_scale_map
    scale_map = get_jpx_scale_map()
    
    sync_results = {}
    auto_rows = []
    
    downloaded_cache = {}
    for target in etf_targets:
        etf_code = target["etf_code"]
        fund_val = target["fund_val"]
        if etf_code not in downloaded_cache:
            constituents = fetch_etf_constituents(etf_code, fund_provider=fund_val)
            downloaded_cache[etf_code] = constituents
            
    for target in etf_targets:
        etf_code = target["etf_code"]
        sec_name = target["sector_name"]
        policy = target["policy"]
        constituents = downloaded_cache.get(etf_code)
        
        if not constituents:
            sync_results[sec_name] = "⚠️ 通信エラー等により既存データを維持できないためスキップ"
            continue
            
        filtered_count = 0
        
        # TOP N フィルタの抽出
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
                else: # デフォルト: TOPIX500
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
        
    # 手動構成台帳（extra_tickers）をロード
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
                
    # 自動同期分と手動分をマージ（手動分を最優先で重複排除）
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
            
    # sector_JP / sector_US へ一括出力
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