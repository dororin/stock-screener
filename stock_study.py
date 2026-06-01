import os
import pandas as pd
import yfinance as yf
import json
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
FOLDER_ID = "1Lx-Xdsm8h20Q-ZRI91Ty7smdYVhkuoFD"
if HAS_STREAMLIT:
    try:
        if "connections" in st.secrets and "gsheets" in st.secrets["connections"]:
            FOLDER_ID = st.secrets["connections"]["gsheets"].get("folder_id", FOLDER_ID)
    except Exception:
        pass

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
    if not service or not FOLDER_ID:
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
    
    # 【重要修正】辞書エンコーディングをオフにして、複数辞書ページの重複書き込みバグを強制回避
    df.to_parquet(work_file, index=False, use_dictionary=False)
    
    api_success = upload_to_drive_api(filename, work_file)
    if not api_success:
        try:
            shutil.copy2(work_file, drive_file)
        except Exception as e:
            print(f"Failed copy to drive: {e}")

# --- TOPIXユニバースのダウンロード ---

def get_topix500_tickers() -> list:
    """JPX公式エクセルからTOPIX500（Core30+Large70+Mid400）の銘柄コードのみを取得（当日キャッシュあり、ETF/ETNは除外）"""
    cache_path = os.path.join(WORK_DIR, "jpx_ticker_cache.json")
    today_str = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                cache = json.load(f)
            if cache.get("date") == today_str and cache.get("tickers"):
                print(f"✅ JPXリスト: 当日キャッシュ使用 ({len(cache['tickers'])}銘柄)")
                return cache["tickers"]
        except Exception:
            pass
    try:
        resp = requests.get(JPX_URL, timeout=10)
        jpx_save_path = os.path.join(DRIVE_DIR, "jpx_stock_list_raw.xls")
        with open(jpx_save_path, "wb") as f:
            f.write(resp.content)
        df_full = pd.read_excel(jpx_save_path)

        # TOPIX500（株式）のみ: 規模区分列(index 9)で絞り込み（ETF・ETNの結合を廃止）
        df_scale = df_full.iloc[:, [1, 2, 3, 9]].copy()
        df_scale.columns = ['symbol', 'name', 'market', 'scale_type']
        target_scales = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
        topix500 = df_scale[df_scale["scale_type"].isin(target_scales)]['symbol'].dropna()

        all_symbols = topix500.drop_duplicates()
        codes = [str(s).strip().split('.')[0] for s in all_symbols if str(s).strip()]
        print(f"✅ 収集対象: TOPIX500={len(codes)}銘柄")
        try:
            with open(cache_path, "w") as f:
                json.dump({"date": today_str, "tickers": codes}, f)
        except Exception:
            pass
        return codes
    except Exception as e:
        print(f"JPX銘柄リスト取得失敗: {e}")
        return []

def get_extra_tickers() -> list:
    """extra_tickers.jsonから追加収集ティッカーを読み込む"""
    cache_path = os.path.join(WORK_DIR, "extra_tickers.json")
    if not os.path.exists(cache_path):
        return []
    try:
        with open(cache_path, "r") as f:
            data = json.load(f)
        codes = data.get("codes", [])
        print(f"✅ 追加ティッカー: {len(codes)}件 ({cache_path})")
        return codes
    except Exception:
        return []

def get_all_collection_tickers() -> list:
    """TOPIX500 + extra_tickersの全収集対象ティッカーを返す"""
    topix = get_topix500_tickers()
    extra = get_extra_tickers()
    combined = list(dict.fromkeys(topix + extra))  # 重複除去・順序保持
    print(f"✅ 全収集対象: TOPIX500={len(topix)} + 追加={len(extra)} = 計{len(combined)}銘柄")
    return combined

def load_tickers_from_file(file_path: str) -> list:
    """CSVまたはExcel(XLS/XLSX)ファイルから、1行目を検索して『コード』列を特定し、ティッカーリストを読み込む"""
    possible_paths = [
        file_path,
        os.path.join("/content/drive/MyDrive", file_path),
        os.path.join("/content/drive/MyDrive/stock_data_hub", file_path),
        os.path.join(os.getcwd(), file_path)
    ]
    
    actual_path = None
    for p in possible_paths:
        if os.path.exists(p):
            actual_path = p
            break
            
    if not actual_path:
        print(f"⚠️ 【警告】指定されたファイル '{file_path}' が見つかりませんでした。")
        return []
        
    ext = os.path.splitext(actual_path)[1].lower()
    try:
        if ext == '.csv':
            df = pd.read_csv(actual_path)
        elif ext in ['.xls', '.xlsx']:
            df = pd.read_excel(actual_path)
        else:
            print(f"❌ 【エラー】サポートされていないファイル形式です: {ext}")
            return []
            
        if df.empty:
            print("⚠️ 【警告】ファイルが空です。")
            return []
            
        raw_tickers = []
        ticker_col = None
        
        target_keywords = ['コード', 'ticker', 'symbol', 'code', '銘柄コード']
        for col in df.columns:
            col_str = str(col).strip().lower()
            if any(k in col_str for k in target_keywords):
                ticker_col = col
                break
                
        if ticker_col is not None:
            raw_tickers = df[ticker_col].dropna().astype(str).tolist()
            print(f"🔍 1行目から『{ticker_col}』列を自動検出しました。この列からコードを抽出します。")
        else:
            raw_tickers = df.iloc[:, 0].dropna().astype(str).tolist()
            print("⚠️ 1行目に『コード』に該当する見出しが見つかりませんでした。代わりに1列目のデータを読み込みます。")
            
            first_col_name = str(df.columns[0]).strip().split('.')[0]
            if first_col_name and not any(h in first_col_name.lower() for h in ['name', 'date', '日付', '市場', '価格', 'close']):
                raw_tickers.insert(0, str(df.columns[0]))
        
        cleaned = []
        for t in raw_tickers:
            t_clean = t.strip().split('.')[0]
            if t_clean and t_clean.isalnum():
                cleaned.append(t_clean)
                
        seen = set()
        unique_cleaned = [x for x in cleaned if not (x in seen or seen.add(x))]
        
        print(f"✅ ファイル '{actual_path}' から {len(unique_cleaned)} 個 of 固有銘柄を読み込みました。")
        return unique_cleaned
        
    except Exception as e:
        print(f"❌ 【エラー】ファイルの読み込み中に問題が発生しました: {e}")
        return []

# --- ティッカーシンボルのサニタイズ処理 ---

def sanitize_ticker(ticker: str, is_jp: bool = True) -> str:
    t = str(ticker).strip().upper()
    if is_jp and t.endswith(".T"):
        t = t[:-2]
    return t

def get_download_symbol(ticker: str, is_jp: bool = True) -> str:
    pure_ticker = sanitize_ticker(ticker, is_jp)
    if is_jp and not pure_ticker.endswith(".T") and pure_ticker.isdigit():
        return f"{pure_ticker}.T"
    return pure_ticker

# --- データベース統合更新エンジン・個別修復・再構築 ---

def rebuild_single_ticker_db(ticker: str, is_jp: bool = True, interval: str = "1d") -> bool:
    if interval != "1d":
        msg = f"❌ 【ガード発動】短期足（{interval}）に対するフル再構築は、データ永久消失リスクを回避するため実行できません。"
        print(msg)
        if HAS_STREAMLIT:
            st.error(msg)
        return False
        
    pure_ticker = sanitize_ticker(ticker, is_jp)
    symbol = get_download_symbol(pure_ticker, is_jp)
    
    print(f"🔄 [{pure_ticker}] 1d データベースをフル再構築します...")
    try:
        db_df = load_price_db("1d", is_jp=is_jp)
    except FileNotFoundError:
        db_df = pd.DataFrame()
        
    if not db_df.empty:
        db_df = db_df[db_df["ticker"] != pure_ticker]
        
    try:
        df_raw = yf.download(symbol, period="max", interval="1d", auto_adjust=True, actions=True, progress=False)
        if df_raw.empty:
            print(f"⚠️ {symbol} のデータが取得できませんでした。")
            return False
            
        df_clean = parse_yfinance_batch(df_raw, [pure_ticker], is_jp=is_jp)
        if not df_clean.empty:
            df_clean["is_finalized"] = True
            db_df = pd.concat([db_df, df_clean], ignore_index=True)
            db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
            save_price_db(db_df, "1d", is_jp=is_jp)
            print(f"✅ [{pure_ticker}] 1d フル再構築が完了しました。 (行数: {len(df_clean)})")
            return True
        else:
            print(f"⚠️ [{pure_ticker}] パース結果が空です。")
            return False
    except Exception as e:
        print(f"❌ [{pure_ticker}] フル再構築中にエラーが発生しました: {e}")
        return False

def repair_single_ticker_short_term_db(ticker: str, interval: str, is_jp: bool = True) -> bool:
    pure_ticker = sanitize_ticker(ticker, is_jp)
    symbol = get_download_symbol(pure_ticker, is_jp)
    now = datetime.now()
    
    try:
        db_df = load_price_db(interval, is_jp=is_jp)
    except FileNotFoundError:
        db_df = pd.DataFrame()
        
    old_df = db_df[db_df["ticker"] == pure_ticker].copy() if not db_df.empty else pd.DataFrame()
    
    if interval == "1m":
        start_date_dt = now - timedelta(days=6)
    elif interval == "5m":
        start_date_dt = now - timedelta(days=58)
    elif interval == "60m":
        start_date_dt = now - timedelta(days=718)
    elif interval == "1d":
        start_date_dt = now - timedelta(days=365)
    else:
        start_date_dt = now - timedelta(days=7)
        
    try:
        print(f"📥 [{pure_ticker}] {interval} 修復用の新規データを取得中 ({start_date_dt.strftime('%Y-%m-%d')} ~)...")
        df_raw = yf.download(
            symbol,
            start=start_date_dt.strftime("%Y-%m-%d"),
            interval=interval,
            auto_adjust=False,
            actions=True,
            progress=False
        )
        if df_raw.empty:
            print(f"⚠️ {symbol} の新規取得データが空です。修復を中断します。")
            return False
            
        new_df = parse_yfinance_batch(df_raw, [pure_ticker], is_jp=is_jp)
        if new_df.empty:
            print(f"⚠️ パース処理の結果、有効なデータが確認できませんでした。")
            return False
            
        has_split = False
        split_ratio = 1.0
        if "stock splits" in new_df.columns:
            splits_active = new_df["stock splits"].dropna()
            splits_active = splits_active[(splits_active > 0) & (splits_active != 1.0)]
            if not splits_active.empty:
                S = splits_active.iloc[-1]
                split_ratio = 1.0 / S
                has_split = True
                print(f"⚠️ [分割検知] 修復期間中に株式分割（分割比: {S}）を検知。既存の全蓄積データに遡及適用します (R = {split_ratio:.4f})")
                
        if has_split and not old_df.empty:
            price_cols = ["open", "high", "low", "close"]
            for col in price_cols:
                if col in old_df.columns:
                    old_df[col] = old_df[col] * split_ratio
            if "volume" in old_df.columns:
                old_df["volume"] = old_df["volume"] / split_ratio
                
        if not old_df.empty:
            new_dates = new_df["date"]
            old_df_filtered = old_df[~old_df["date"].isin(new_dates)]
            combined_ticker = pd.concat([old_df_filtered, new_df], ignore_index=True)
        else:
            combined_ticker = new_df
            
        if not db_df.empty:
            other_tickers_df = db_df[db_df["ticker"] != pure_ticker]
            final_df = pd.concat([other_tickers_df, combined_ticker], ignore_index=True)
        else:
            final_df = combined_ticker
            
        if "is_finalized" not in final_df.columns:
            final_df["is_finalized"] = True
        else:
            final_df.loc[final_df["ticker"] == pure_ticker, "is_finalized"] = True
            
        final_df = final_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        save_price_db(final_df, interval, is_jp=is_jp)
        print(f"✅ [{pure_ticker}] {interval} 重複排除マージによる安全修復が完了しました。 (行数: {len(combined_ticker)})")
        return True
    except Exception as e:
        print(f"❌ [{pure_ticker}] 修復処理中にエラーが発生しました: {e}")
        return False

def merge_price_data(old_df: pd.DataFrame, new_df: pd.DataFrame, interval: str, is_jp: bool = True) -> pd.DataFrame:
    if new_df is None or new_df.empty: return old_df
    if old_df.empty: return new_df

    new_tickers = new_df["ticker"].unique()
    old_untouched = old_df[~old_df["ticker"].isin(new_tickers)].copy()
    
    processed_parts = []
    for t in new_tickers:
        t_new = new_df[new_df["ticker"] == t].sort_values("date")
        t_old = old_df[old_df["ticker"] == t].sort_values("date")
        
        if t_old.empty:
            processed_parts.append(t_new)
            continue
            
        has_split = False
        split_ratio = 1.0
        if "stock splits" in t_new.columns:
            splits_active = t_new["stock splits"].dropna()
            splits_active = splits_active[(splits_active > 0) & (splits_active != 1.0)]
            if not splits_active.empty:
                S = splits_active.iloc[-1]
                split_ratio = 1.0 / S
                has_split = True
                print(f"⚠️ [分割検知] Ticker {t}: 株式分割（{S}）が確認されました。過去データ全体に数学的自己調整を適用します (R = {split_ratio:.4f})")
                
        if has_split:
            price_cols = ["open", "high", "low", "close"]
            for col in price_cols:
                if col in t_old.columns:
                    t_old[col] = t_old[col] * split_ratio
            if "volume" in t_old.columns:
                t_old["volume"] = t_old["volume"] / split_ratio
                
        new_dates = t_new["date"]
        t_old_filtered = t_old[~t_old["date"].isin(new_dates)]
        
        t_combined = pd.concat([t_old_filtered, t_new], ignore_index=True)
        processed_parts.append(t_combined)
        
    combined = pd.concat([old_untouched] + processed_parts, ignore_index=True)
    combined = combined.drop_duplicates(subset=["date", "ticker"], keep="last")
    return combined.sort_values(["ticker", "date"]).reset_index(drop=True)

def parse_yfinance_batch(df_raw, chunk_tickers, is_jp: bool = True):
    if df_raw.empty: return pd.DataFrame()
    all_rows = []
    
    is_multi = isinstance(df_raw.columns, pd.MultiIndex)
    suffix = ".T" if is_jp else ""
    
    if not is_multi:
        if len(chunk_tickers) == 1:
            t_df = df_raw.copy()
            t_df = t_df.dropna(how="all").reset_index()
            t_df.columns = [str(c).lower() for c in t_df.columns]
            t_df = t_df.rename(columns={"datetime": "date", "index": "date"})
            
            dt_col = pd.to_datetime(t_df["date"])
            t_df["date"] = dt_col.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None) if dt_col.dt.tz is not None else dt_col
            t_df["ticker"] = str(chunk_tickers[0])
            
            target_cols = ["date", "ticker", "open", "high", "low", "close", "volume", "stock splits", "dividends"]
            valid_cols = [c for c in target_cols if c in t_df.columns]
            return t_df[valid_cols]
        else:
            return pd.DataFrame()
            
    for ticker in chunk_tickers:
        symbol = f"{ticker}{suffix}"
        try:
            if symbol in df_raw.columns.get_level_values(1):
                t_df = df_raw.xs(symbol, axis=1, level=1).copy()
            elif symbol in df_raw.columns.get_level_values(0):
                t_df = df_raw[symbol].copy()
            else:
                alt_symbol = ticker
                if alt_symbol in df_raw.columns.get_level_values(1):
                    t_df = df_raw.xs(alt_symbol, axis=1, level=1).copy()
                elif alt_symbol in df_raw.columns.get_level_values(0):
                    t_df = df_raw[alt_symbol].copy()
                else:
                    continue

            t_df = t_df.dropna(how="all").reset_index()
            t_df.columns = [str(c).lower() for c in t_df.columns]
            t_df = t_df.rename(columns={"datetime": "date", "index": "date"}) 
            
            dt_col = pd.to_datetime(t_df["date"])
            t_df["date"] = dt_col.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None) if dt_col.dt.tz is not None else dt_col
            t_df["ticker"] = str(ticker)
            
            target_cols = ["date", "ticker", "open", "high", "low", "close", "volume", "stock splits", "dividends"]
            valid_cols = [c for c in target_cols if c in t_df.columns]
            all_rows.append(t_df[valid_cols])
        except Exception:
            continue
            
    return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()

def update_price_database(is_jp: bool = True, target_tickers: list = None, force_refetch: bool = False):
    market_name = "JP" if is_jp else "US"
    tickers = target_tickers if target_tickers else []
    
    if is_jp and not tickers:
        tickers = get_all_collection_tickers()
        
    if not tickers:
        print(f"[{market_name}] 更新対象銘柄リストが空です。処理をスキップします。")
        return

    now = datetime.now()
    suffix = ".T" if is_jp else ""
    
    tickers = [sanitize_ticker(t, is_jp) for t in tickers]

    for interval in TIMEFRAMES:
        print(f"\n--- Database Sync: {market_name} ({interval}) ---")
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
        except FileNotFoundError as e:
            print(f"Skipped: {e}")
            continue

        last_updates_map = {}
        if not db_df.empty:
            # is_finalized == True (確定済み) のデータから最新日を取得
            if "is_finalized" in db_df.columns:
                finalized_df = db_df[db_df["is_finalized"] == True]
                if not finalized_df.empty:
                    last_updates_map = finalized_df.groupby("ticker")["date"].max().to_dict()
            
            # 確定データが1つもない、またはカラムがない場合は通常通り全体の max 日付を fallback として取得
            if not last_updates_map:
                last_updates_map = db_df.groupby("ticker")["date"].max().to_dict()

        global_max_date = max(last_updates_map.values()) if last_updates_map else None
        group_up_to_date, group_catchup = [], []

        for t in tickers:
            t_last = last_updates_map.get(t)
            if force_refetch:
                group_catchup.append(t)
            elif t_last and global_max_date and t_last >= global_max_date:
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
                        df_raw = yf.download(symbols, start=start_date_dt.strftime("%Y-%m-%d"), interval=interval, auto_adjust=False, actions=True, progress=False, threads=True, timeout=30)
                        chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
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
                
                if force_refetch and global_max_date:
                    start_date_dt = global_max_date
                else:
                    start_date_dt = datetime(2023, 1, 1)
                limit = None
                if interval == "1m": limit = now - timedelta(days=6)
                elif interval == "5m": limit = now - timedelta(days=58)
                elif interval == "60m": limit = now - timedelta(days=720)
                
                if limit and start_date_dt < limit:
                    start_date_dt = limit

                symbols = [f"{t}{suffix}" for t in chunk]
                try:
                    df_raw = yf.download(symbols, start=start_date_dt.strftime("%Y-%m-%d"), interval=interval, auto_adjust=False, actions=True, progress=False, threads=True, timeout=30)
                    chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
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
            
            # --- 権利落ち自動トリガー（日足 1d のみ適用） ---
            reset_tickers = []
            if interval == "1d":
                for ticker in new_combined["ticker"].unique():
                    t_new = new_combined[new_combined["ticker"] == ticker]
                    has_action = False
                    
                    if "stock splits" in t_new.columns:
                        splits_active = t_new["stock splits"].dropna()
                        if not splits_active[(splits_active > 0) & (splits_active != 1.0)].empty:
                            has_action = True
                            
                    if "dividends" in t_new.columns:
                        divs_active = t_new["dividends"].dropna()
                        if not divs_active[divs_active > 0].empty:
                            has_action = True
                            
                    if has_action:
                        print(f"🚨 [権利落ち自動トリガー発動] {ticker} に分割または配当を検知しました。過去全期間をフル再構築します。")
                        rebuild_success = rebuild_single_ticker_db(ticker, is_jp=is_jp, interval="1d")
                        if rebuild_success:
                            reset_tickers.append(ticker)
                            
                if reset_tickers:
                    new_combined = new_combined[~new_combined["ticker"].isin(reset_tickers)]
                    db_df = load_price_db(interval, is_jp=is_jp)
            
            if not new_combined.empty:
                db_df = merge_price_data(db_df, new_combined, interval, is_jp=is_jp)
                save_price_db(db_df, interval, is_jp=is_jp)
                print(f"  ✅ Saved updated price_{market_name.lower()}_{interval}.parquet. Rows: {len(db_df)}")
            else:
                print(f"  🧊 No extra new data left to merge after triggers.")
        else:
            print(f"  🧊 No new data added.")

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", type=str, default="jp", choices=["jp", "us"])
    parser.add_argument("--file", "--csv", dest="file_path", type=str, default=None, 
                        help="Path to custom CSV/XLS/XLSX ticker list")
    args = parser.parse_args()

    start_time = datetime.now()
    is_jp = (args.market == "jp")
    
    target_tickers = None
    
    if args.file_path:
        target_tickers = load_tickers_from_file(args.file_path)
        if not target_tickers:
            print("❌ 有効な銘柄コードが見つからなかったため、処理を中断します。")
            return
    elif not is_jp:
        target_tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
    
    update_price_database(is_jp=is_jp, target_tickers=target_tickers)
    print(f"\nPipeline finished. Duration: {datetime.now() - start_time}")

if __name__ == "__main__":
    main()

def full_rebuild_all_database(is_jp: bool = True, interval: str = "1d") -> bool:
    """
    指定した市場と時間足のデータを完全にゼロから新規取得し、Parquetデータベースとして新規保存します。
    """
    market_name = "JP" if is_jp else "US"
    tickers = get_all_collection_tickers() if is_jp else ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
    
    if not tickers:
        print("❌ 収集対象の銘柄コードが見つかりません。")
        return False
        
    tickers = [sanitize_ticker(t, is_jp) for t in tickers]
    suffix = ".T" if is_jp else ""
    now = datetime.now()
    
    # 時間足に応じた取得開始日の算出（yfinanceの取得限界制限を考慮）
    if interval == "1m":
        start_date_dt = now - timedelta(days=6)
    elif interval == "5m":
        start_date_dt = now - timedelta(days=58)
    elif interval == "60m":
        start_date_dt = now - timedelta(days=718)
    else:  # "1d" (日足)
        # スクリーニングに必要な移動平均期間（200日以上）を十分に満たし、
        # ダウンロード負荷を現実的に抑えるため「2020年以降(約6年間)」をデフォルトに設定
        start_date_dt = datetime(2020, 1, 1)
        
    print(f"🚨 [全体一括再構築] {market_name} ({interval}) をゼロから新規作成します。 (開始日: {start_date_dt.strftime('%Y-%m-%d')})")
    
    all_downloaded = []
    # 安全にダウンロードするため、30銘柄ずつの小バッチに分けてループ実行
    BATCH_SIZE = 30
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i+BATCH_SIZE]
        symbols = [f"{t}{suffix}" for t in chunk]
        try:
            df_raw = yf.download(
                symbols,
                start=start_date_dt.strftime("%Y-%m-%d"),
                interval=interval,
                auto_adjust=(interval == "1d"), # 日足は自動調整、短期足は未調整で配当なども残す
                actions=True,
                progress=False,
                threads=True,
                timeout=30
            )
            chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
            if not chunk_processed.empty:
                all_downloaded.append(chunk_processed)
        except Exception as e:
            print(f"⚠️ バッチ取得エラー ({i}-{i+BATCH_SIZE}): {e}")
        time.sleep(1.5) # レートリミット回避のディレイ
        
    if all_downloaded:
        final_df = pd.concat(all_downloaded, ignore_index=True)
        if "date" in final_df.columns:
            if interval == "1d":
                final_df["is_finalized"] = final_df["date"].dt.date < now.date()
            else:
                final_df["is_finalized"] = final_df["date"] < (now - timedelta(hours=1))
        
        final_df = final_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        # use_dictionary=False を適用して保存する
        save_price_db(final_df, interval, is_jp=is_jp)
        return True
    else:
        return False