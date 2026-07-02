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
    """
    同期処理中にメモリへ蓄積された詳細ログ（文字列のリスト）を、
    実行した市場モード（is_jp）のローカルタイムゾーン（JST/EST）の日時をファイル名に含め、
    settings.LOGS_FOLDER_ID で指定されたGoogleドライブのフォルダへ1回だけバッチアップロードします。

    ファイル名形式: {prefix}_YYYY-MM-DD_HHMMSS.log （例: sync_2026-07-02_203045.log）

    戻り値: アップロードに成功した場合はファイル名の文字列、失敗またはログが空の場合は None。
    """
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

        # 同期を実行した市場モードのローカルタイムゾーンを基準に日時を算出
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
            # シートが存在しない場合に新規作成し、ヘッダーを書き込み（名前付き引数でバージョン不整合を防止）
            ws = sh.add_worksheet(title=settings.REPAIR_LOG_SHEET_NAME, rows=1000, cols=len(REPAIR_LOG_COLUMNS))
            ws.update(values=[REPAIR_LOG_COLUMNS], range_name="A1")

        # 🚨 自前で行数を数えるのをやめ、空白列のズレやライブラリの引数順序バグを100%回避するため、
        # 🚨 Google Sheets API 本来の「末尾自動追記メソッド（append_rows）」を使用します。
        rows_to_append = [
            [str(row.get(col, "")) for col in REPAIR_LOG_COLUMNS]
            for row in log_rows
        ]
        
        # USER_ENTERED を指定することで、数値や日付が文字列ではなく正しいデータ型としてスプレッドシートに追記されます
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
        
        # 🚨 get_all_records() はエラーが起きやすく空白セルでロードに失敗するため、
        # 🚨 最も安全で100%生データをロードできる get_all_values() に変更します。
        raw_values = ws.get_all_values()
        if not raw_values or len(raw_values) < 2:
            return pd.DataFrame(columns=REPAIR_LOG_COLUMNS)
        
        # 1行目をカラム名（小文字に統一）、2行目以降をデータとしてロード
        headers = [str(h).strip().lower() for h in raw_values[0]]
        
        # 🛡️ 表記ブレ対策：手動入力時に「全角のアンダーバー」になっていても、半角に自動補正します
        headers = [h.replace("executed＿at", "executed_at") for h in headers]
        headers = [h.replace("executed_at", "executed_at") for h in headers]
        
        data_rows = raw_values[1:]
        
        # DataFrameの作成
        df = pd.DataFrame(data_rows, columns=headers)
        
        # 想定外の余分な列がシートにある場合は、規定の9列のみを抽出
        valid_cols = [c for c in REPAIR_LOG_COLUMNS if c in df.columns]
        df = df[valid_cols]
        
        # Datetime変換
        if "executed_at" in df.columns:
            df["executed_at"] = pd.to_datetime(df["executed_at"], errors="coerce")
        if "cliff_date" in df.columns:
            df["cliff_date"] = pd.to_datetime(df["cliff_date"], errors="coerce")
            
        # 数値変換（空白セルは自動的にNaN/欠損値に変換されます）
        for col in ["before_close", "after_close", "multiplier"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
                
        # 実行日時の降順に並べ替えて返却
        return df.sort_values("executed_at", ascending=False).reset_index(drop=True)
    except Exception as e:
        # 何らかのエラーが発生した場合は、静かに消さずコンソールに原因を出力します
        print(f"❌ [load_repair_log_from_sheets] データのロードに失敗しました: {e}")
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

def sync_etf_sectors_consolidated(is_jp: bool = True) -> dict:
    """
    sector_JP (または sector_US) シートから「ETFコード」が書かれたセクターを自動検知し、
    最新の構成銘柄をSolactiveから取得して、同一シートを部分上書き更新します。
    手動管理されている他のセクター（自動車や電気機器など）は一切書き換えずに保護します。
    
    戻り値: {セクター名: 状態メッセージ} の辞書形式
    """
    sh = get_sector_spreadsheet()
    if sh is None:
        return {"error": "スプレッドシートを開けませんでした。"}
        
    sheet_name = "sector_JP" if is_jp else "sector_US"
    try:
        ws = sh.worksheet(sheet_name)
    except Exception:
        return {"error": f"'{sheet_name}' シートが見つかりません。"}
        
    # 1. 全レコードの読み込み
    all_values = ws.get_all_values()
    if not all_values or len(all_values) < 1:
        return {"error": f"'{sheet_name}' シートが空です。ヘッダーを作成してください。"}
        
    headers = [str(h).strip() for h in all_values[0]]
    
    # 2. カラム位置の自動特定
    col_sector = -1
    col_code = -1
    col_memo = -1
    col_etf = -1
    
    for i, h in enumerate(headers):
        if h in ["セクター名", "sector", "sector_name"]:
            col_sector = i
        elif h in ["銘柄コード", "code", "ticker", "コード"]:
            col_code = i
        elif h in ["備考", "memo"]:
            col_memo = i
        elif h in ["ETFコード", "etf", "etf_code"]:
            col_etf = i
            
    if col_sector == -1 or col_code == -1:
        return {"error": "必須カラム（セクター名、銘柄コード）がシート内に見つかりません。"}
        
    # ETFコード列がシートにない場合は、右端に自動作成します
    if col_etf == -1:
        headers.append("ETFコード")
        col_etf = len(headers) - 1
        all_values[0] = headers
        ws.update([headers], "A1")
        
    # 3. データの解析（自動化したいセクターとETFのペアを抽出）
    etf_mapping = {}  # {セクター名: ETFコード}
    rows_data = []    # 読み込んだ元のデータを退避
    
    for row in all_values[1:]:
        # 行の長さがヘッダーと合わない場合の補正
        while len(row) < len(headers):
            row.append("")
            
        sec_val = str(row[col_sector]).strip()
        code_val = str(row[col_code]).strip()
        memo_val = str(row[col_memo]).strip() if col_memo != -1 else ""
        etf_val = str(row[col_etf]).strip()
        
        # 4桁のETFコードや、英数字(513A等)が記載されている場合のみマッピングに登録
        if sec_val and etf_val:
            etf_mapping[sec_val] = etf_val
            
        rows_data.append({
            "sector": sec_val,
            "code": code_val,
            "memo": memo_val,
            "etf": etf_val
        })
        
    if not etf_mapping:
        return {"info": "自動同期対象（ETFコードが記入されたセクター）が検出されませんでした。"}
        
    # 4. Solactiveからの最新データ取得とマージ処理
    from core.collector import fetch_etf_constituents
    
    sync_results = {}
    final_rows = []
    
    # 【ステップA】自動同期対象外（手動セクター）の行を無傷で残す
    for r in rows_data:
        if r["sector"] not in etf_mapping:
            final_rows.append(r)
            
    # 【ステップB】自動同期対象のセクターについて、1件ずつ最新データをマージ
    for sector_name, etf_code in etf_mapping.items():
        constituents = fetch_etf_constituents(etf_code)
        
        # ダウンロードに失敗した場合の安全ガード（古いデータをそのまま復元して維持）
        if not constituents:
            sync_results[sector_name] = "通信エラー等のため既存データをそのまま維持"
            for r in rows_data:
                if r["sector"] == sector_name:
                    final_rows.append(r)
            continue
            
        # ダウンロードに成功した場合、最新の構成銘柄で展開
        for code, name in constituents.items():
            final_rows.append({
                "sector": sector_name,
                "code": code,
                "memo": name,  # 備考欄に銘柄名を自動マッピング
                "etf": etf_code
            })
        sync_results[sector_name] = f"同期成功 ({len(constituents)}銘柄)"
        
    # 5. スプレッドシートへの一括書き出し
    output_values = [headers]
    for r in final_rows:
        row_out = [""] * len(headers)
        row_out[col_sector] = r["sector"]
        row_out[col_code] = r["code"]
        if col_memo != -1:
            row_out[col_memo] = r["memo"]
        row_out[col_etf] = r["etf"]
        output_values.append(row_out)
        
    # 衝突を避けるため、一度シートをクリアして全書き直し
    ws.clear()
    ws.update(output_values, "A1")
    
    return sync_results