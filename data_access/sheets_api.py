# data_access/sheets_api.py
import pandas as pd
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

# --- セクター定義シート連携 ---
def load_sector_master_from_sheets(is_jp: bool) -> dict:
    """Google Sheetsのセクター定義シートから、セクターと構成ティッカーの対応マップを構築して返します。"""
    sh = get_sector_spreadsheet()
    default_sectors = settings.JP_SECTORS if is_jp else settings.US_SECTORS
    if sh is None:
        return default_sectors
    
    sheet_name = "sector_JP" if is_jp else "sector_US"
    try:
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
    """データ修復の実行ログをスプレッドシートに追記します。"""
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
            ws.update([REPAIR_LOG_COLUMNS], "A1")

        existing = ws.get_all_values()
        next_row = len(existing) + 1
        rows_to_append = [
            [str(row.get(col, "")) for col in REPAIR_LOG_COLUMNS]
            for row in log_rows
        ]
        ws.update(rows_to_append, f"A{next_row}")
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
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(columns=REPAIR_LOG_COLUMNS)
        df = pd.DataFrame(records)
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

# --- 追加ETF収集対象(extra_tickers)連携 ---
def load_extra_tickers_from_sheets() -> pd.DataFrame:
    """追加ETF定義シートから収集コードリストをロードします。"""
    sh = get_sector_spreadsheet()
    if sh is None:
        return pd.DataFrame(columns=["code", "name", "memo"])
    try:
        ws = sh.worksheet(settings.EXTRA_TICKERS_SHEET)
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(columns=["code", "name", "memo"])
        df = pd.DataFrame(records)
        df.columns = [str(c).strip().lower() for c in df.columns]
        df["code"] = df["code"].astype(str).str.strip().str.split(".").str[0]
        return df[df["code"].str.len() > 0].reset_index(drop=True)
    except Exception:
        return pd.DataFrame(columns=["code", "name", "memo"])

def save_extra_tickers_to_sheets(df: pd.DataFrame):
    """追加ETFの変更定義をスプレッドシートに保存します。"""
    sh = get_sector_spreadsheet()
    if sh is None:
        return
    try:
        try:
            ws = sh.worksheet(settings.EXTRA_TICKERS_SHEET)
        except Exception:
            ws = sh.add_worksheet(title=settings.EXTRA_TICKERS_SHEET, rows=200, cols=3)
        rows = [["code", "name", "memo"]] + df[["code", "name", "memo"]].values.tolist()
        ws.clear()
        ws.update(rows, "A1")
    except Exception:
        pass