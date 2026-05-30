import os
import pandas as pd
import yfinance as yf
import requests
from datetime import datetime, timedelta
import shutil
import time

# --- Google Drive API 用のインポート ---
from google.oauth2.service_account import Credentials as SACredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

try:
    import streamlit as st
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

# --- 設定：Google Driveの共有フォルダID ---
if HAS_STREAMLIT:
    FOLDER_ID = st.secrets["connections"]["gsheets"].get("folder_id", "1Lx-Xdsm8h20Q-ZRI91Ty7smdYVhkuoFD")
else:
    FOLDER_ID = "1Lx-Xdsm8h20Q-ZRI91Ty7smdYVhkuoFD"

JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
TIMEFRAMES = ["1m", "5m", "60m", "1d"]

# --- 環境判定とディレクトリ設定 ---
def setup_directories():
    is_colab = False
    try:
        from google.colab import drive
        is_colab = True
    except ImportError:
        pass
        
    is_kaggle = os.environ.get('KAGGLE_KERNEL_RUN_TYPE') is not None
    current_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else os.getcwd()
    project_root = os.path.dirname(current_dir)
    
    if is_colab:
        drive_path = "/content/drive/MyDrive/stock_data_hub"
        if not os.path.exists("/content/drive/MyDrive") and os.path.exists("/content/drive/My Drive"):
            drive_path = "/content/drive/My Drive/stock_data_hub"
        work_path = "/content/stock_data_work"
    elif is_kaggle:
        drive_path = "/kaggle/working/drive/MyDrive/stock_data_hub" 
        work_path = "/kaggle/working/stock_data_work"
    else:
        drive_path = os.path.join(project_root, "data_drive")
        work_path = os.path.join(project_root, "data_work")

    os.makedirs(drive_path, exist_ok=True)
    os.makedirs(work_path, exist_ok=True)
    return drive_path, work_path

DRIVE_DIR, WORK_DIR = setup_directories()

def get_drive_service():
    if not HAS_STREAMLIT:
        # ローカル実行時は secrets.toml を手動ロード試行
        try:
            import toml
            secrets_path = os.path.join(".streamlit", "secrets.toml")
            if os.path.exists(secrets_path):
                cfg = toml.load(secrets_path)["connections"]["gsheets"]
                sa_info = {k: cfg[k] for k in ["type","project_id","private_key_id","private_key","client_email","client_id","auth_uri","token_uri"] if k in cfg}
                sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
                creds = SACredentials.from_service_account_info(sa_info, scopes=["https://www.googleapis.com/auth/drive"])
                return build('drive', 'v3', credentials=creds)
        except Exception:
            pass
        return None
    try:
        if "connections" in st.secrets and "gsheets" in st.secrets["connections"]:
            cfg = dict(st.secrets["connections"]["gsheets"])
            sa_keys = ["type","project_id","private_key_id","private_key","client_email","client_id","auth_uri","token_uri"]
            sa_info = {k: cfg[k] for k in sa_keys if k in cfg}
            if "private_key" in sa_info:
                sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
            creds = SACredentials.from_service_account_info(sa_info, scopes=["https://www.googleapis.com/auth/drive"])
            return build('drive', 'v3', credentials=creds)
    except Exception:
        pass
    return None

def download_from_drive_api(filename, local_path):
    service = get_drive_service()
    if not service or not FOLDER_ID or FOLDER_ID.startswith("1Lx-Xdsm8h20Q"): # デフォルト値チェック回避
        return False
    try:
        query = f"name='{filename}' and '{FOLDER_ID}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        items = results.get('files', [])
        if not items: return False
        
        file_id = items[0]['id']
        request = service.files().get_media(fileId=file_id)
        with open(local_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True
    except Exception:
        return False

def upload_to_drive_api(filename, local_path):
    service = get_drive_service()
    if not service or not FOLDER_ID: return False
    try:
        query = f"name='{filename}' and '{FOLDER_ID}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        items = results.get('files', [])
        
        media = MediaFileUpload(local_path, mimetype='application/octet-stream', resumable=True)
        if items:
            file_id = items[0]['id']
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            file_metadata = {'name': filename, 'parents': [FOLDER_ID]}
            service.files().create(body=file_metadata, media_body=media).execute()
        return True
    except Exception:
        return False

# --- データベース操作 ---

def get_db_filename(interval: str, is_jp: bool = True) -> str:
    market = "jp" if is_jp else "us"
    return f"price_{market}_{interval}.parquet"

def load_price_db(interval: str, is_jp: bool = True) -> pd.DataFrame:
    filename = get_db_filename(interval, is_jp)
    work_file = os.path.join(WORK_DIR, filename)
    drive_file = os.path.join(DRIVE_DIR, filename)

    api_success = download_from_drive_api(filename, work_file)
    if not api_success and os.path.exists(drive_file):
        shutil.copy2(drive_file, work_file)
    
    if os.path.exists(work_file):
        df = pd.read_parquet(work_file)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        return df
    
    raise FileNotFoundError(
        f"【データベースファイル未検出】'{filename}' が見つかりませんでした。意図しない全件ダウンロードを避けるため中断します。"
    )

def save_price_db(df: pd.DataFrame, interval: str, is_jp: bool = True):
    if df.empty: return
    filename = get_db_filename(interval, is_jp)
    work_file = os.path.join(WORK_DIR, filename)
    drive_file = os.path.join(DRIVE_DIR, filename)
    
    df.to_parquet(work_file, index=False)
    api_success = upload_to_drive_api(filename, work_file)
    if not api_success:
        try:
            shutil.copy2(work_file, drive_file)
        except Exception as e:
            print(f"Failed copy to drive: {e}")

# --- TOPIXユニバースのダウンロード ---

def get_topix500_tickers() -> list:
    """JPX公式エクセルからTOPIX500（Core30+Large70+Mid400）の銘柄コードを取得"""
    try:
        resp = requests.get(JPX_URL, timeout=10)
        jpx_save_path = os.path.join(DRIVE_DIR, "jpx_stock_list_raw.xls")
        with open(jpx_save_path, "wb") as f:
            f.write(resp.content)
        df = pd.read_excel(jpx_save_path)
        df = df.iloc[:, [1, 2, 3, 9]]
        df.columns = ['symbol', 'name', 'market', 'scale_type']
        target_scales = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
        df = df[df["scale_type"].isin(target_scales)]
        codes = df['symbol'].dropna().astype(int).astype(str).tolist()
        return codes
    except Exception as e:
        print(f"JPX銘柄リスト取得失敗: {e}")
        return []

# お手持ちのCSVからA列の銘柄コードを読み込む例
def get_custom_tickers_from_csv(csv_path) -> list:
    try:
        df = pd.read_csv(csv_path)
        # A列（0番目の列）のデータを文字列のリストとして取得
        codes = df.iloc[:, 0].dropna().astype(str).tolist()
        return codes
    except Exception as e:
        print(f"CSV読み込み失敗: {e}")
        return []

# --- データベース統合更新エンジン ---

def update_price_database(is_jp: bool = True, target_tickers: list = None):
    market_name = "JP" if is_jp else "US"
    tickers = target_tickers if target_tickers else []
    
    if is_jp and not tickers:
        tickers = get_topix500_tickers()
        
    if not tickers:
        print(f"[{market_name}] 更新対象銘柄リストが空です。処理をスキップします。")
        return

    now = datetime.now()
    suffix = ".T" if is_jp else ""

    for interval in TIMEFRAMES:
        print(f"\n--- Database Sync: {market_name} ({interval}) ---")
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
        except FileNotFoundError as e:
            print(f"Skipped: {e}")
            continue

        last_updates_map = {}
        if not db_df.empty:
            last_updates_map = db_df.groupby("ticker")["date"].max().to_dict()

        global_max_date = max(last_updates_map.values()) if last_updates_map else None
        group_up_to_date, group_catchup = [], []

        for t in tickers:
            t_last = last_updates_map.get(t)
            if t_last and global_max_date and t_last >= global_max_date:
                group_up_to_date.append(t)
            else:
                group_catchup.append(t)

        all_downloaded = []

        # --- 最新組 (Group A) 同期 ---
        if group_up_to_date:
            start_date_dt = global_max_date + timedelta(days=1) if interval == "1d" else global_max_date + timedelta(hours=1)
            if start_date_dt > now:
                print(f"  ✨ All Group-A tickers are already up to date.")
            else:
                BATCH_SIZE = 50
                for i in range(0, len(group_up_to_date), BATCH_SIZE):
                    chunk = group_up_to_date[i:i+BATCH_SIZE]
                    symbols = [f"{t}{suffix}" for t in chunk]
                    try:
                        df_raw = yf.download(symbols, start=start_date_dt.strftime("%Y-%m-%d"), interval=interval, auto_adjust=False, progress=False, threads=True, timeout=30)
                        chunk_processed = parse_yfinance_batch(df_raw, chunk)
                        if not chunk_processed.empty:
                            all_downloaded.append(chunk_processed)
                    except Exception as e:
                        print(f"     Batch Error: {e}")
                    time.sleep(1)

        # --- 新規/遅れ組 (Group B) 同期 ---
        if group_catchup:
            BATCH_SIZE = 20
            for i in range(0, len(group_catchup), BATCH_SIZE):
                chunk = group_catchup[i:i+BATCH_SIZE]
                
                # 取得開始可能日の制限設定
                start_date_dt = datetime(2023, 1, 1)
                limit = None
                if interval == "1m": limit = now - timedelta(days=6)
                elif interval == "5m": limit = now - timedelta(days=58)
                elif interval == "60m": limit = now - timedelta(days=720)
                
                if limit and start_date_dt < limit:
                    start_date_dt = limit

                symbols = [f"{t}{suffix}" for t in chunk]
                try:
                    df_raw = yf.download(symbols, start=start_date_dt.strftime("%Y-%m-%d"), interval=interval, auto_adjust=False, progress=False, threads=True, timeout=30)
                    chunk_processed = parse_yfinance_batch(df_raw, chunk)
                    if not chunk_processed.empty:
                        all_downloaded.append(chunk_processed)
                except Exception as e:
                    print(f"     Batch Error: {e}")
                time.sleep(1.5)

        if all_downloaded:
            new_combined = pd.concat(all_downloaded, ignore_index=True)
            if "date" in new_combined.columns:
                if interval == "1d":
                    new_combined["is_finalized"] = new_combined["date"].dt.date < now.date()
                else:
                    new_combined["is_finalized"] = new_combined["date"] < (now - timedelta(hours=1))
            
            db_df = merge_price_data(db_df, new_combined)
            save_price_db(db_df, interval, is_jp=is_jp)
            print(f"  ✅ Saved updated price_{market_name.lower()}_{interval}.parquet. Rows: {len(db_df)}")
        else:
            print(f"  🧊 No new data added.")

def merge_price_data(old_df, new_df):
    if new_df is None or new_df.empty: return old_df
    if old_df.empty: return new_df
    new_min_date = new_df["date"].min()
    old_part = old_df[old_df["date"] < new_min_date].copy()
    combined = pd.concat([old_part, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["date", "ticker"], keep="last")
    return combined.sort_values(["ticker", "date"]).reset_index(drop=True)

def parse_yfinance_batch(df_raw, chunk_tickers):
    if df_raw.empty: return pd.DataFrame()
    all_rows = []
    for ticker in chunk_tickers:
        symbol = f"{ticker}.T" if ticker.isdigit() else ticker
        try:
            if symbol in df_raw.columns.get_level_values(1):
                t_df = df_raw.xs(symbol, axis=1, level=1).copy()
            elif symbol in df_raw.columns.get_level_values(0):
                t_df = df_raw[symbol].copy()
            else:
                continue

            t_df = t_df.dropna(how="all").reset_index()
            t_df.columns = [str(c).lower() for c in t_df.columns]
            t_df = t_df.rename(columns={"datetime": "date", "index": "date"}) 
            
            dt_col = pd.to_datetime(t_df["date"])
            t_df["date"] = dt_col.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None) if dt_col.dt.tz is not None else dt_col
            t_df["ticker"] = str(ticker)
            
            valid_cols = [c for c in ["date", "ticker", "open", "high", "low", "close", "volume"] if c in t_df.columns]
            all_rows.append(t_df[valid_cols])
        except Exception:
            continue
    return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", type=str, default="jp", choices=["jp", "us"])
    parser.add_argument("--csv", type=str, default=None, help="カスタム銘柄コードを含むCSVファイルのパス")
    args = parser.parse_args()

    start_time = datetime.now()
    is_jp = (args.market == "jp")
    
    # 米国株のデフォルトセクター用シンボルの設定
    us_tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
    
    target_tickers = None
    if args.csv:
        target_tickers = get_custom_tickers_from_csv(args.csv)
        if not target_tickers:
            print("CSVから銘柄を読み込めなかったため、処理を中断します。")
            return
    elif not is_jp:
        target_tickers = us_tickers

    update_price_database(is_jp=is_jp, target_tickers=target_tickers)
    print(f"\nPipeline finished. Duration: {datetime.now() - start_time}")

if __name__ == "__main__":
    main()