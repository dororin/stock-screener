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
TIMEFRAMES = ["1d", "60m", "5m", "1m"]

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

def repair_single_ticker_all_timeframes(
    ticker: str,
    is_jp: bool = True,
    forced_split_ratio: float = None
) -> dict:
    """
    指定銘柄の全時間足を修復する。

    1d:
        Yahooの全履歴(period=max)を取得し、
        対象銘柄の日足DBを完全置換する。

    60m/5m/1m:
        従来通り安全マージ修復を実施する。
    """

    pure_ticker = sanitize_ticker(ticker, is_jp)
    symbol = get_download_symbol(pure_ticker, is_jp)

    now = datetime.now()
    results = {}

    for interval in ["1d", "60m", "5m", "1m"]:

        try:

            # ==================================================
            # 日足（完全再構築モード）
            # ==================================================
            if interval == "1d":

                print(f"📥 [{pure_ticker}] 1d 全履歴取得中(period=max)")

                df_raw = yf.download(
                    symbol,
                    period="max",
                    interval="1d",
                    auto_adjust=False,  # 分割調整のみ・配当調整なし（TVとの整合性を優先）
                    actions=True,
                    progress=False
                )

                if df_raw.empty:
                    results["1d"] = "データ取得失敗"
                    continue

                new_df = parse_yfinance_batch(
                    df_raw,
                    [pure_ticker],
                    is_jp=is_jp
                )

                if new_df.empty:
                    results["1d"] = "パース結果空"
                    continue

                try:
                    db_df = load_price_db(
                        "1d",
                        is_jp=is_jp
                    )
                except FileNotFoundError:
                    db_df = pd.DataFrame()

# 既存データを完全に消去せず、分割調整エンジンを適用するために退避
                old_df = (
                    db_df[db_df["ticker"] == pure_ticker].copy()
                    if not db_df.empty
                    else pd.DataFrame()
                )

                new_df["is_finalized"] = True

                # 日足に対しても、手動比率や自動検知を反映してマージ・調整を実行
                merged_df = merge_price_data(
                    old_df,
                    new_df,
                    "1d",
                    is_jp=is_jp,
                    forced_split_ratio=forced_split_ratio
                )

                # データベースから対象銘柄の古いデータを削除して、調整済みのデータを結合
                if not db_df.empty:
                    db_df = db_df[db_df["ticker"] != pure_ticker]

                db_df = pd.concat(
                    [db_df, merged_df],
                    ignore_index=True
                )

                db_df = db_df.sort_values(
                    ["ticker", "date"]
                ).reset_index(drop=True)

                save_price_db(
                    db_df,
                    "1d",
                    is_jp=is_jp
                )

                results["1d"] = (
                    f"完全再構築成功 "
                    f"({len(new_df):,}件)"
                )

                continue

            # ==================================================
            # 短期足（従来の安全修復）
            # ==================================================
            try:
                db_df = load_price_db(
                    interval,
                    is_jp=is_jp
                )
            except FileNotFoundError:
                db_df = pd.DataFrame()

            old_df = (
                db_df[db_df["ticker"] == pure_ticker].copy()
                if not db_df.empty
                else pd.DataFrame()
            )

            if interval == "1m":
                start_date_dt = now - timedelta(days=6)

            elif interval == "5m":
                start_date_dt = now - timedelta(days=58)

            elif interval == "60m":
                start_date_dt = now - timedelta(days=718)

            print(
                f"📥 [{pure_ticker}] "
                f"{interval} 修復データ取得中"
            )

            df_raw = yf.download(
                symbol,
                start=start_date_dt.strftime("%Y-%m-%d"),
                interval=interval,
                auto_adjust=False,
                actions=True,
                progress=False
            )

            if df_raw.empty:
                results[interval] = "新規データ空"
                continue

            new_df = parse_yfinance_batch(
                df_raw,
                [pure_ticker],
                is_jp=is_jp
            )

            if new_df.empty:
                results[interval] = "パース結果空"
                continue

            merged_df = merge_price_data(
                old_df,
                new_df,
                interval,
                forced_split_ratio=forced_split_ratio
            )

            if not db_df.empty:
                db_df = db_df[
                    db_df["ticker"] != pure_ticker
                ]

            db_df = pd.concat(
                [db_df, merged_df],
                ignore_index=True
            )

            db_df = db_df.sort_values(
                ["ticker", "date"]
            ).reset_index(drop=True)

            save_price_db(
                db_df,
                interval,
                is_jp=is_jp
            )

            results[interval] = (
                f"修復成功 ({len(merged_df):,}件)"
            )

        except Exception as e:

            results[interval] = (
                f"エラー: {str(e)}"
            )

            print(
                f"❌ [{pure_ticker}] "
                f"{interval} 修復失敗: {e}"
            )

    return results

def merge_price_data(old_df: pd.DataFrame, new_df: pd.DataFrame, interval: str, is_jp: bool = True, forced_split_ratio: float = None) -> pd.DataFrame:
    if new_df is None or new_df.empty: return old_df
    # 💡 old_df が空の場合でも、新規データ自体に分割調整をかけるために早期リターンを廃止（直後の個別ループ内で空対応します）

    new_tickers = new_df["ticker"].unique()
    # old_df が空の場合のハンドリング
    if not old_df.empty:
        old_untouched = old_df[~old_df["ticker"].isin(new_tickers)].copy()
    else:
        old_untouched = pd.DataFrame()
    
    processed_parts = []
    for t in new_tickers:
        t_new = new_df[new_df["ticker"] == t].sort_values("date").reset_index(drop=True)
        t_old = (
            old_df[old_df["ticker"] == t].sort_values("date").reset_index(drop=True)
            if not old_df.empty
            else pd.DataFrame()
        )
        
        # 💡 old_dfが空でもt_new単体に対する調整を実行するため、ここでは早期追加をせず下部の調整処理へ進めます

        has_split = False
        split_ratio = 1.0
        
        # --- 1. 手動分割指定がある場合、最優先で適用 ---
        if forced_split_ratio is not None and forced_split_ratio > 0:
            split_ratio = 1.0 / forced_split_ratio
            has_split = True
            print(f"⚠️ [手動分割適用] Ticker {t}: 指定された比率 {forced_split_ratio:.1f} に基づき、新規および過去データを調整します。")
            
            # 新規データ(t_new)の内部に断絶がある場合、その断絶日より前の部分を手動比率で調整
            if len(t_new) > 1:
                pct_changes = t_new["close"].pct_change()
                anomaly_mask = pct_changes <= -0.40
                if anomaly_mask.any():
                    anomaly_idx = anomaly_mask.idxmax()
                    split_date = t_new.loc[anomaly_idx, "date"]
                    pre_mask = t_new["date"] < split_date
                    price_cols = ["open", "high", "low", "close"]
                    for col in price_cols:
                        if col in t_new.columns:
                            t_new.loc[pre_mask, col] = t_new.loc[pre_mask, col] / forced_split_ratio
                    if "volume" in t_new.columns:
                        t_new.loc[pre_mask, "volume"] = t_new.loc[pre_mask, "volume"] * forced_split_ratio

        # --- 2. 手動指定がない場合の自動判定 ---
        else:
            # 💡 [防衛1] 新規データ（t_new）の「内部」に隠れた未調整の急落（40%以上）がないか走査
            if len(t_new) > 1:
                pct_changes = t_new["close"].pct_change()
                anomaly_mask = pct_changes <= -0.40  # 40%以上の急落
                
                if anomaly_mask.any():
                    anomaly_idx = anomaly_mask.idxmax()
                    anomaly_row = t_new.loc[anomaly_idx]
                    split_date = anomaly_row["date"]
                    
                    has_official_split = False
                    if "stock splits" in t_new.columns:
                        has_official_split = (t_new["stock splits"] > 0).any()
                    
                    if not has_official_split:
                        pre_close = t_new.loc[anomaly_idx - 1, "close"]
                        post_close = anomaly_row["close"]
                        raw_ratio = pre_close / post_close
                        
                        common_ratios = [1.5, 2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0]
                        est_ratio = min(common_ratios, key=lambda x: abs(x - raw_ratio))
                        if abs(est_ratio - raw_ratio) / raw_ratio > 0.15:
                            est_ratio = float(round(raw_ratio))
                            
                        if est_ratio >= 1.5:
                            print(f"🚨 [データ内部の隠れ分割検知] Ticker {t}: 新規データ内部（{split_date}）に40%以上の断絶を検知。比率 {est_ratio:.1f} で調整します。")
                            
                            # 新規データ内の該当する過去期間の価格・出来高を調整
                            pre_mask = t_new["date"] < split_date
                            price_cols = ["open", "high", "low", "close"]
                            for col in price_cols:
                                if col in t_new.columns:
                                    t_new.loc[pre_mask, col] = t_new.loc[pre_mask, col] / est_ratio
                            if "volume" in t_new.columns:
                                t_new.loc[pre_mask, "volume"] = t_new.loc[pre_mask, "volume"] * est_ratio
                            
                            # 💡 【連動修正】t_old に対する遡及調整用のフラグと比率をセット
                            split_ratio = 1.0 / est_ratio
                            has_split = True

            # 公式分割データの検証
            official_split_val = 1.0
            if "stock splits" in t_new.columns:
                splits_active = t_new["stock splits"].dropna()
                splits_active = splits_active[(splits_active > 0) & (splits_active != 1.0)]
                if not splits_active.empty:
                    official_split_val = splits_active.iloc[-1]
            
            if official_split_val != 1.0:
                split_ratio = 1.0 / official_split_val
                has_split = True
                print(f"⚠️ [公式分割検知] Ticker {t}: 株式分割（{official_split_val}）を検知しました。 (R = {split_ratio:.4f})")
            elif not has_split and not t_old.empty:  # 防衛1で検知されておらず、かつ既存データがある場合
                # 💡 [防衛2] 結合の境界（既存データ末尾と新データ先頭）を比較
                old_last_close = t_old.iloc[-1]["close"]
                new_first_close = t_new.iloc[0]["close"]
                
                if old_last_close > 0 and new_first_close > 0:
                    if new_first_close <= (old_last_close * 0.60):
                        raw_ratio = old_last_close / new_first_close
                        common_ratios = [1.5, 2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0]
                        est_ratio = min(common_ratios, key=lambda x: abs(x - raw_ratio))
                        if abs(est_ratio - raw_ratio) / raw_ratio > 0.15:
                            est_ratio = float(round(raw_ratio))
                        
                        if est_ratio >= 1.5:
                            split_ratio = 1.0 / est_ratio
                            has_split = True
                            print(f"🚨 [データ境界の隠れ分割検知] Ticker {t}: 異常価格ギャップを検知。比率 {est_ratio:.1f} で過去データを調整します。")

        # 既存データベース側のさらに古い過去データ（t_old）への調整適用
        if has_split and not t_old.empty:
            # 二重調整防止用の安全ガード
            split_date = pd.to_datetime(t_new.iloc[0]["date"])
            t_old_pre = t_old[pd.to_datetime(t_old["date"]) < split_date]
            t_new_pre = t_new[pd.to_datetime(t_new["date"]) < split_date]
            
            apply_split = True
            if not t_old_pre.empty:
                common_dates = t_old_pre["date"].isin(t_new_pre["date"])
                if common_dates.any():
                    last_common_date = t_old_pre[common_dates]["date"].max()
                    price_db = t_old_pre[t_old_pre["date"] == last_common_date]["close"].iloc[-1]
                    price_new = t_new_pre[t_new_pre["date"] == last_common_date]["close"].iloc[-1]
                    if price_db <= (price_new * 1.1):
                        apply_split = False
                        print(f"ℹ️ [{t}] {interval} データベースはすでに調整済みであることを確認したため二重処理を回避します。")
            
            if apply_split:
                price_cols = ["open", "high", "low", "close"]
                for col in price_cols:
                    if col in t_old.columns:
                        t_old[col] = t_old[col] * split_ratio
                if "volume" in t_old.columns:
                    t_old["volume"] = t_old["volume"] / split_ratio
                print(f"  -> [{t}] 既存データへの遡及調整を適用しました。")
                
        if not t_old.empty:
            new_dates = t_new["date"]
            t_old_filtered = t_old[~t_old["date"].isin(new_dates)]
            t_combined = pd.concat([t_old_filtered, t_new], ignore_index=True)
        else:
            t_combined = t_new
            
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

def get_benchmark_latest_date(interval: str, is_jp: bool = True) -> pd.Timestamp:
    """
    yfinanceから代表ベンチマーク（日本株なら1306.T、米国株ならSPY）の最新の取引日時を1件取得する。
    """
    bm_symbol = "1306.T" if is_jp else "SPY"
    try:
        # 過去5日分を取得（休日や深夜でも確実に直近データを得るため）
        df_bm = yf.download(bm_symbol, period="5d", interval=interval, progress=False, auto_adjust=True)
        if df_bm.empty:
            return None
        
        # yfinanceのインデックス（DatetimeIndex）から最新の行のタイムスタンプを取得
        latest_dt = df_bm.index[-1]
        
        # データベース側のtz-naive形式にタイムゾーンを合わせる
        if latest_dt.tzinfo is not None:
            if is_jp:
                latest_dt = latest_dt.tz_convert("Asia/Tokyo").tz_localize(None)
            else:
                latest_dt = latest_dt.tz_localize(None)
        else:
            latest_dt = pd.to_datetime(latest_dt)
            
        return latest_dt
    except Exception:
        return None

def get_benchmark_latest_date(interval: str, is_jp: bool = True) -> pd.Timestamp:
    """
    yfinanceから超高流動性の代表銘柄の最新取引日時を取得する。
    大引け後の時間外ノイズデータ（15:00以降）をカットするため、取引終了時刻で時間をクリップ（丸め）する。
    """
    symbols = ["7203.T", "^N225"] if is_jp else ["AAPL", "^GSPC"]
    
    for bm_symbol in symbols:
        try:
            df_bm = yf.download(bm_symbol, period="5d", interval=interval, progress=False, auto_adjust=True)
            if not df_bm.empty:
                latest_dt = df_bm.index[-1]
                
                # データベース側のtz-naive形式にタイムゾーンを合わせる
                if latest_dt.tzinfo is not None:
                    if is_jp:
                        latest_dt = latest_dt.tz_convert("Asia/Tokyo").tz_localize(None)
                    else:
                        latest_dt = latest_dt.tz_localize(None)
                else:
                    latest_dt = pd.to_datetime(latest_dt)
                
                # 💡 【追加】大引け後の時間外ノイズ時刻（日本株15:00、米国株16:00以降）をクリップ
                if interval != "1d":
                    limit_hour = 15 if is_jp else 16
                    limit_time = datetime.strptime(f"{limit_hour}:00:00", "%H:%M:%S").time()
                    if latest_dt.time() > limit_time:
                        latest_dt = latest_dt.replace(hour=limit_hour, minute=0, second=0, microsecond=0)
                
                return latest_dt
        except Exception:
            continue
            
    return None

def update_price_database(is_jp: bool = True, target_tickers: list = None, force_refetch: bool = False, status_callback=None):
    market_name = "JP" if is_jp else "US"
    tickers = target_tickers if target_tickers else []
    
    # コールバック関数と標準出力を統一して処理する内部ヘルパー
    def log(msg):
        print(msg)
        if status_callback:
            status_callback(msg)
            
    if is_jp and not tickers:
        tickers = get_all_collection_tickers()
        
    if not tickers:
        log(f"[{market_name}] 更新対象銘柄リストが空です。処理をスキップします。")
        return

    now = datetime.now()
    suffix = ".T" if is_jp else ""
    
    tickers = [sanitize_ticker(t, is_jp) for t in tickers]

    for interval in TIMEFRAMES:
        log(f"⏱️ 【{market_name}】{interval} データベースの同期処理を開始します...")
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
        except FileNotFoundError as e:
            log(f"⚠️ スキップ: {e}")
            continue

        # 💡 【スマートロジック：ベンチマーク先行比較（デバッグ情報付き）】
        db_max_date = db_df["date"].max() if not db_df.empty else None
        
        if db_max_date is not None:
            # 💡 【追加】DB最新時刻に対しても大引け後の時間外ノイズ（日本株15:00、米国株16:00以降）をクリップ
            if interval != "1d":
                limit_hour = 15 if is_jp else 16
                limit_time = datetime.strptime(f"{limit_hour}:00:00", "%H:%M:%S").time()
                if db_max_date.time() > limit_time:
                    db_max_date = db_max_date.replace(hour=limit_hour, minute=0, second=0, microsecond=0)
            
            bm_last_date = get_benchmark_latest_date(interval, is_jp=is_jp)
            
            # 現在の比較状況をログに出力
            log(f"  🔍 [判定情報] ベンチマーク最新時刻: {bm_last_date} | DBの物理最新時刻: {db_max_date}")
            
            if bm_last_date is not None:
                # 比較：ベンチマークの最新時刻が、DBの物理的な最新時刻以下ならこの時間足は丸ごとスキップ
                if bm_last_date <= db_max_date:
                    log(f"  ✨ 【同期スキップ】すでに最新状態（または休場・深夜）のため、同期処理をスキップします。")
                    continue
                else:
                    log(f"  📥 【同期実行】新データを確認したため、差分を同期します。")
            else:
                log(f"  ⚠️ ベンチマーク時刻の取得に失敗したため、安全のため通常の同期処理に移行します。")

        # 各銘柄の物理的な最新日付を取得（未確定分も含む）
        last_updates_map = {}
        if not db_df.empty:
            last_updates_map = db_df.groupby("ticker")["date"].max().to_dict()

        # =====================================================================
        # 💡 3レイヤー・バケットグループ化ロジック（未来ズレ対応）
        # =====================================================================
        
        # 1. 基準最新時刻（最頻値）の算出
        active_timestamps = [pd.to_datetime(last_updates_map[t]) for t in tickers if t in last_updates_map]
        if active_timestamps and not force_refetch:
            base_time = pd.Series(active_timestamps).mode()[0]
        else:
            base_time = None

        # 2. 時間足に応じた「軽微なズレ」の閾値（許容遅延幅）の決定
        if interval == "1m":
            max_delay = timedelta(hours=4)
        elif interval == "5m":
            max_delay = timedelta(hours=12)
        elif interval == "60m":
            max_delay = timedelta(days=2)
        else: # "1d" (日足)
            max_delay = timedelta(days=10)

        # 未来へのズレを許容する誤差幅（例: 5分）
        future_tolerance = timedelta(minutes=5)

        group_A_tickers = []  # 最新グループ（重複ゼロ）
        group_B_tickers = []  # 軽微な遅れグループ（最古一括化）
        group_C_tickers = []  # 例外・新規・大遅延グループ（個別隔離）
        
        group_B_timestamps = []

        # 3. 3つのグループへの自動振り分け
        for t in tickers:
            t_last = last_updates_map.get(t)
            if t_last is None or force_refetch:
                group_C_tickers.append(t)
                continue
                
            t_last_dt = pd.to_datetime(t_last)
            
            if base_time is None:
                group_C_tickers.append(t)
            else:
                delay = base_time - t_last_dt
                
                # 基準より「5分以内」の未来に進んでいるズレは、最新（グループA）として吸収
                if -future_tolerance <= delay <= timedelta(0):
                    group_A_tickers.append(t)
                elif timedelta(0) < delay <= max_delay:
                    # 軽微な遅れ：グループB
                    group_B_tickers.append(t)
                    group_B_timestamps.append(t_last_dt)
                else:
                    # 5分を超える未来（データ異常）または 4時間を超える大遅延：グループC
                    group_C_tickers.append(t)

        # 4. 一括化・丸め処理を適用した最終グループ辞書の組み立て
        groups = {}

        # 【グループA（最新）】
        if group_A_tickers:
            groups[base_time] = group_A_tickers

        # 【グループB（軽微な遅れ）】
        if group_B_tickers and group_B_timestamps:
            oldest_b_time = min(group_B_timestamps)
            
            # 時間足に応じたバケッティング
            if interval in ["1m", "5m"]:
                rounded_time = oldest_b_time.floor("30min")
            elif interval == "60m":
                rounded_time = oldest_b_time.floor("h")
            else:
                rounded_time = oldest_b_time.floor("D")
                
            groups[rounded_time] = group_B_tickers
            log(f"  📦 [一括化適用] 遅延{len(group_B_tickers)}銘柄を最古の丸め時刻 [{rounded_time}] に引き下げて1つのグループに統合しました。")

        # 【グループC（例外・大遅延・新規上場）】
        for t in group_C_tickers:
            t_last = last_updates_map.get(t)
            t_key = pd.to_datetime(t_last) if t_last is not None else None
            groups.setdefault(t_key, []).append(t)
            
        if group_C_tickers:
            log(f"  ⚠️ [個別隔離適用] 新規上場または極端な大遅延を持つ {len(group_C_tickers)} 銘柄を例外枠として個別同期リストに隔離しました。")

        # =====================================================================
        # ダウンロード処理 (yfinanceの安定化に向け、startは常に日付単位に統一)
        # =====================================================================
        all_downloaded = []

        # グループごとに適切な開始日からダウンロード
        for t_last, chunk_tickers in groups.items():
            if t_last is None:
                # 新規銘柄：過去制限最大から取得
                if interval == "1m": start_date_dt = now - timedelta(days=6)
                elif interval == "5m": start_date_dt = now - timedelta(days=58)
                elif interval == "60m": start_date_dt = now - timedelta(days=718)
                else: start_date_dt = datetime(2020, 1, 1)
                log(f"  📥 新規または再取得銘柄 ({len(chunk_tickers)}件) をフル同期... (開始: {start_date_dt.strftime('%Y-%m-%d')})")
            else:
                # 既存銘柄の同期
                start_date_dt = t_last
                
                # 直近2分以内に同期された既存銘柄は除外（本来の t_last で判定）
                if interval != "1d" and (now - t_last).total_seconds() < 120:
                    continue
                
                # 💡 【改善】開始時刻は「日付のみ（%Y-%m-%d）」にして、yfinanceのタイムゾーンバグを完全に回避
                start_date_str = start_date_dt.strftime("%Y-%m-%d")
                log(f"  📥 既存銘柄 ({len(chunk_tickers)}件) を差分同期... (開始: {start_date_str} *yfinance安定化適用済み*)")

            # 100銘柄ごとに一括ダウンロード
            BATCH_SIZE = 100
            for i in range(0, len(chunk_tickers), BATCH_SIZE):
                chunk = chunk_tickers[i:i+BATCH_SIZE]
                log(f"    -> {interval}: {i}/{len(chunk_tickers)} 銘柄ダウンロード中...")
                symbols = [f"{t}{suffix}" for t in chunk]
                try:
                    # エラー判別の準備。過去のエラーログをこのバッチ分だけ一旦リセット
                    batch_errors = {}
                    try:
                        import yfinance.shared as yf_shared
                        for sym in symbols:
                            if hasattr(yf_shared, "_ERRORS") and sym in yf_shared._ERRORS:
                                del yf_shared._ERRORS[sym]
                    except Exception:
                        pass

                    df_raw = yf.download(
                        symbols, 
                        start=start_date_str if t_last is not None else start_date_dt.strftime("%Y-%m-%d"),
                        interval=interval, 
                        auto_adjust=False, 
                        actions=True, 
                        progress=False, 
                        threads=True, 
                        timeout=30
                    )
                    
                    # ダウンロード直後のエラー情報の回収
                    try:
                        import yfinance.shared as yf_shared
                        if hasattr(yf_shared, "_ERRORS"):
                            batch_errors = {sym: yf_shared._ERRORS[sym] for sym in symbols if sym in yf_shared._ERRORS}
                    except Exception:
                        pass

                    chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
                    
                    if not chunk_processed.empty:
                        all_downloaded.append(chunk_processed)
                    else:
                        # 取得できなかった原因の判別ログ出力
                        if batch_errors:
                            err_sample = list(batch_errors.values())[0]
                            log(f"      ❌ APIエラーによりデータ取得失敗: {err_sample} (他 {len(batch_errors)-1}件の不具合)")
                        else:
                            log(f"      🧊 APIは正常応答しましたが、指定日（{start_date_str}）以降に新しい取引データがありませんでした（出来高0、または未生成）。")
                            
                except Exception as e:
                    log(f"     Batch Error: {e}")
                time.sleep(1)

        if all_downloaded:
            new_combined = pd.concat(all_downloaded, ignore_index=True)
            if "date" in new_combined.columns:
                # 💡 【分足確定判定の修正案適用】
                # 日足・短期足に関わらず、データの「日付部分（dt.date）」が現在の日付（now.date()）より過去であれば確定(True)、
                # 今日（当日分）であれば一律で未確定(False)と判定します。
                # これにより、当日取得した不安定なデータは、翌日以降に必ず公式の綺麗な確定データでリフレッシュされます。
                new_combined["is_finalized"] = new_combined["date"].dt.date < now.date()
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
                    has_split = False
                    split_ratio = 1.0
                    
                    # 1dデータより、有効な分割マークがあるか確認
                    if "stock splits" in t_new.columns:
                        splits_active = t_new["stock splits"].dropna()
                        splits_active = splits_active[(splits_active > 0) & (splits_active != 1.0)]
                        if not splits_active.empty:
                            S = splits_active.iloc[-1]
                            split_ratio = 1.0 / S
                            has_action = True
                            has_split = True
                            
                    # 配当金マークの確認
                    if "dividends" in t_new.columns:
                        divs_active = t_new["dividends"].dropna()
                        if not divs_active[divs_active > 0].empty:
                            has_action = True
                            
                    if has_action:
                        # 💡 株式分割の場合のみ、他の短期足に先に価格調整を波及させる
                        if has_split:
                            log(f"🚨 [分割波及トリガー] {ticker} に株式分割（比: {1.0/split_ratio:.1f}）を検知。短期足DBを事前調整します。")
                            propagate_split_to_other_timeframes(ticker, split_ratio, is_jp=is_jp, log_func=log)
                            
                        # 日足（1d）データベース自体のフル再構築を実行
                        log(f"🚨 [権利落ち自動トリガー発動] {ticker} に分割または配当を検知。日足(1d)をフル再構築します。")
                        rebuild_success = rebuild_single_ticker_db(ticker, is_jp=is_jp, interval="1d")
                        if rebuild_success:
                            reset_tickers.append(ticker)
                            
                if reset_tickers:
                    new_combined = new_combined[~new_combined["ticker"].isin(reset_tickers)]
                    db_df = load_price_db(interval, is_jp=is_jp)
            
            if not new_combined.empty:
                db_df = merge_price_data(db_df, new_combined, interval, is_jp=is_jp)
                save_price_db(db_df, interval, is_jp=is_jp)
                log(f"  ✅ {interval} データベースの更新を完了し、保存しました。")
            else:
                log(f"  🧊 トリガー再構築により、追加マージデータはありません。")
        else:
            log(f"  🧊 新規追加データはありませんでした。")

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
                auto_adjust=False,  # 分割調整のみ・配当調整なし（TVとの整合性を優先）
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

def propagate_split_to_other_timeframes(ticker: str, split_ratio: float, is_jp: bool = True, log_func=None):
    """
    日足で検知した株式分割の比率を、他の短期足（60m, 5m, 1m）の
    データベースに事前に反映（価格調整・出来高調整）させる。
    """
    def _log(msg):
        if log_func:
            log_func(msg)
        else:
            print(msg)

    ticker_symbol = f"{ticker}.T" if is_jp and ticker.isdigit() else ticker
    
    # 💡 [二重処理ガード用] yfinanceから最新の正しい日足（調整後）を5日分だけ取得
    try:
        df_check = yf.download(ticker_symbol, period="5d", interval="1d", auto_adjust=False, progress=False)
        if df_check.empty:
            _log(f"  ⚠️ [{ticker}] 調整確認用の基準日足データを取得できませんでした。波及処理を中止します。")
            return
        check_dates = df_check.index
    except Exception as e:
        _log(f"  ⚠️ [{ticker}] 基準日足データ取得エラー: {e}")
        return

    short_intervals = ["60m", "5m", "1m"]
    for interval in short_intervals:
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
            if db_df.empty:
                continue
            
            mask = db_df["ticker"] == ticker
            ticker_db = db_df[mask].copy()
            if ticker_db.empty:
                continue
            
            ticker_db["date"] = pd.to_datetime(ticker_db["date"])
            
            # yfinanceの日足データと、短期足DBに共通して存在する「過去の日付」を特定
            common_dates = ticker_db["date"].dt.date.isin(check_dates.date)
            
            apply_split = True
            if common_dates.any():
                last_common_dt = ticker_db[common_dates]["date"].max()
                check_date_only = last_common_dt.date()
                
                # 短期足DB(db_df)の終値を取得
                price_db = ticker_db[ticker_db["date"] == last_common_dt]["close"].iloc[-1]
                
                # 正確な日足(df_check)の調整後の価格を取得
                matching_check_row = df_check[df_check.index.date == check_date_only]
                if not matching_check_row.empty:
                    price_real = matching_check_row["Close"].iloc[-1]
                    
                    # 💡 【ガード判定】DBの価格が実勢(調整後)とほぼ同じなら、波及調整をスキップ
                    if price_db <= (price_real * 1.1):
                        apply_split = False
                        _log(f"  ℹ️ [{ticker}] {interval} データベースはすでに波及調整済みであることを確認しました。二重処理をスキップします。")
            
            if apply_split:
                _log(f"  🔄 [{ticker}] {interval} データベースに事前分割調整を波及中 (ratio: {split_ratio:.4f})...")
                # 4本値（価格）の調整
                price_cols = ["open", "high", "low", "close"]
                for col in price_cols:
                    if col in db_df.columns:
                        db_df.loc[mask, col] = db_df.loc[mask, col] * split_ratio
                # 出来高（volume）の調整
                if "volume" in db_df.columns:
                    db_df.loc[mask, "volume"] = db_df.loc[mask, "volume"] / split_ratio
                
                save_price_db(db_df, interval, is_jp=is_jp)
                
        except FileNotFoundError:
            pass
        except Exception as e:
            _log(f"  ⚠️ [{ticker}] {interval} への分割波及処理中にエラーが発生しました: {e}")


# ==============================================================================
# [修正2] backward_scale_repair() - yfinanceバグデータの崖修正
# ==============================================================================

def backward_scale_repair(df: pd.DataFrame, threshold: float = 0.35) -> tuple:
    """
    日足データの崖（急落/急騰バグ）を検出し、崖より古い全データを倍率修正する。

    検出ロジック：
    1. 負の株価を最優先で直接検出
       崖位置 = 「負の株価の最終行の次の行」（正常値に戻った最初の行）
    2. pct_changeによる急変検出
       valid_maskによる除外はしない（出来高ゼロ行除外だと崖位置がずれる）
       絶対値で閾値判定（負値同士の符号反転対策）

    Parameters
    ----------
    df : pd.DataFrame
        単一銘柄の日足データ（date, open, high, low, close, volume列を含む）
    threshold : float
        崖とみなす変化率の絶対値（デフォルト0.35 = 35%）

    Returns
    -------
    df_fixed : pd.DataFrame
        修正済みデータ
    repairs : list[dict]
        適用した修正のログ（崖日付・multiplier・修正前後終値）。分足への流用に使用する。
    """
    if df.empty:
        return df, []

    df = df.sort_values("date").reset_index(drop=True)
    price_cols = [c for c in ["open", "high", "low", "close"] if c in df.columns]
    repairs = []
    processed_idxs = set()

    ticker_label = df["ticker"].iloc[0] if "ticker" in df.columns else ""

    # ------------------------------------------------------------------
    # 検出①：負の株価を直接検出
    # 崖位置 = 負の株価ブロックの「直後の正常値行」
    # 例: ..., 負, 負, 負, [正] ← ここが崖（idxはこの行）
    # ------------------------------------------------------------------
    negative_mask = df["close"] < 0
    if negative_mask.any():
        # 負→正に切り替わる行（崖の「後」側 = 正常値の先頭）を特定
        neg_to_pos = (~negative_mask) & (negative_mask.shift(1, fill_value=False))
        cliff_idxs_neg = df[neg_to_pos].index.tolist()

        for idx in sorted(cliff_idxs_neg, reverse=True):
            if idx == 0:
                continue

            # 崖の「後」側（正常値）と「前」側（負値ブロックの最後）
            after_price = df.loc[idx, "close"]
            # 負値ブロック全体を対象にするため idx-1 から遡って負値の開始点を探す
            neg_start = idx - 1
            while neg_start > 0 and df.loc[neg_start, "close"] < 0:
                neg_start -= 1

            before_price = after_price  # 負値ブロック全体を正常値に合わせる
            cliff_date = df.loc[idx, "date"]

            # 負値ブロック全体（neg_start〜idx-1）を正常値の絶対値で置き換える
            # 負値の場合は単純な乗算では直らないため絶対値変換で対応
            neg_block_mask = (df.index >= neg_start) & (df.index < idx) & (df["close"] < 0)
            if neg_block_mask.any():
                # 負値の絶対値をそのまま正常値として使う（符号を反転）
                for col in price_cols:
                    df.loc[neg_block_mask, col] = df.loc[neg_block_mask, col].abs()

                # neg_startより古いデータへの倍率は「負値ブロック直前の正常値」で計算
                if neg_start > 0:
                    pre_normal_price = df.loc[neg_start, "close"]
                    # neg_startの価格がafter_priceと大きく乖離していれば遡及調整
                    if pre_normal_price > 0 and abs(pre_normal_price / after_price - 1) > threshold:
                        multiplier = after_price / pre_normal_price
                        for col in price_cols:
                            df.loc[:neg_start - 1, col] = df.loc[:neg_start - 1, col] * multiplier
                        if "volume" in df.columns:
                            df.loc[:neg_start - 1, "volume"] = df.loc[:neg_start - 1, "volume"] / multiplier
                    else:
                        multiplier = 1.0
                else:
                    multiplier = 1.0

                processed_idxs.add(idx)
                repairs.append({
                    "cliff_date": cliff_date,
                    "multiplier": multiplier,
                    "before_close": round(float(df.loc[idx - 1, "close"]), 3),
                    "after_close": round(float(after_price), 3),
                })
                print(f"  🔧 [{ticker_label}] 負の株価修正: {cliff_date} | "
                      f"負値ブロック({neg_block_mask.sum()}行)を絶対値変換 | multiplier={multiplier:.6f}")

    # ------------------------------------------------------------------
    # 検出②：pct_changeによる急変検出
    # valid_maskによる除外なし・絶対値で判定
    # ------------------------------------------------------------------
    pct_changes = df["close"].pct_change()
    cliff_mask = pct_changes.abs() >= threshold
    cliffs = pct_changes[cliff_mask].index.tolist()
    # 検出①で処理済みの行は除外
    cliffs = [i for i in cliffs if i not in processed_idxs]

    cliffs.sort(reverse=True)

    for idx in cliffs:
        if idx == 0:
            continue

        after_price = df.loc[idx, "close"]
        before_price = df.loc[idx - 1, "close"]

        if before_price == 0 or after_price == 0:
            continue
        # 負の株価が残っている場合はスキップ（検出①で対処済みのはず）
        if before_price < 0 or after_price < 0:
            continue

        multiplier = after_price / before_price
        cliff_date = df.loc[idx, "date"]

        for col in price_cols:
            df.loc[:idx - 1, col] = df.loc[:idx - 1, col] * multiplier

        if "volume" in df.columns:
            df.loc[:idx - 1, "volume"] = df.loc[:idx - 1, "volume"] / multiplier

        repairs.append({
            "cliff_date": cliff_date,
            "multiplier": multiplier,
            "before_close": round(float(before_price), 3),
            "after_close": round(float(after_price), 3),
        })
        print(f"  🔧 [{ticker_label}] 崖修正: {cliff_date} | "
              f"before={before_price:.3f} → after={after_price:.3f} | multiplier={multiplier:.6f}")

    return df, repairs


# ==============================================================================
# [修正3] scan_all_anomalies() - 全銘柄ベクトル走査による異常検出
# ==============================================================================

def scan_all_anomalies(
    is_jp: bool = True,
    interval: str = "1d",
    threshold: float = 0.35
) -> pd.DataFrame:
    """
    指定時間足のDBを全銘柄ベクトル走査して異常箇所を検出する。
    ダウンロード時ではなく後工程での一括チェックに使用する。

    検出ロジック：
    1. 負の株価を最優先で直接検出（pct_change不要・確実）
    2. pct_changeによる急変検出（valid_maskによる除外はしない）
       ※負値同士のpct_changeは符号が反転するため絶対値で判定

    Returns
    -------
    pd.DataFrame: 異常が検出された銘柄・日付の一覧
        列: ticker, cliff_date, before_close, after_close, pct_change, anomaly_type
    """
    try:
        db_df = load_price_db(interval, is_jp=is_jp)
    except FileNotFoundError as e:
        print(f"❌ DBファイルが見つかりません: {e}")
        return pd.DataFrame()

    if db_df.empty:
        print("⚠️ DBが空です。")
        return pd.DataFrame()

    db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)

    result_rows = []

    # ------------------------------------------------------------------
    # 検出①：負の株価を直接検出（最優先・最確実）
    # 負値は変化率計算に依存しないため出来高ゼロでも確実に検出できる
    # ------------------------------------------------------------------
    negative_mask = db_df["close"] < 0
    if negative_mask.any():
        neg_rows = db_df[negative_mask].copy()
        neg_rows["before_close"] = db_df["close"].shift(1)[negative_mask].values
        neg_rows["after_close"] = neg_rows["close"]
        neg_rows["pct_change"] = float("nan")  # 負値は変化率が意味をなさない
        neg_rows["anomaly_type"] = "負の株価"
        result_rows.append(neg_rows[["ticker", "date", "before_close", "after_close", "pct_change", "anomaly_type"]])

    # ------------------------------------------------------------------
    # 検出②：pct_changeによる急変検出
    # valid_maskによる除外はしない（出来高ゼロ行を除外すると崖位置がずれる）
    # 負値同士のpct_changeは符号が反転するためabs()で絶対値判定
    # ------------------------------------------------------------------
    pct = (
        db_df["close"]
        .groupby(db_df["ticker"])
        .pct_change()
    )

    # 絶対値で閾値判定（負値時の符号反転対策）
    cliff_mask = pct.abs() >= threshold
    # すでに検出済みの負の株価行は重複除けで除外
    cliff_mask = cliff_mask & ~negative_mask

    if cliff_mask.any():
        cliff_rows = db_df[cliff_mask].copy()
        cliff_rows["before_close"] = db_df["close"].shift(1)[cliff_mask].values
        cliff_rows["after_close"] = cliff_rows["close"]
        cliff_rows["pct_change"] = pct[cliff_mask].values
        cliff_rows["anomaly_type"] = "急変（" + (pct[cliff_mask] * 100).round(1).astype(str) + "%）"
        result_rows.append(cliff_rows[["ticker", "date", "before_close", "after_close", "pct_change", "anomaly_type"]])

    if not result_rows:
        print(f"✅ 異常箇所は検出されませんでした（閾値: {threshold*100:.0f}%）")
        return pd.DataFrame()

    result = pd.concat(result_rows, ignore_index=True)
    result = result.rename(columns={"date": "cliff_date"})
    # 負の株価を先頭に、次いでpct_changeの絶対値が大きい順に表示
    result = result.sort_values(
        ["anomaly_type", "pct_change"],
        ascending=[True, True]
    ).reset_index(drop=True)

    print(f"⚠️ {len(result)}件の異常箇所を検出しました（{result['ticker'].nunique()}銘柄）")
    return result


# ==============================================================================
# [修正4] apply_scale_repair_with_intraday_propagation()
#         日足修正 → 分足DBへの自動波及
# ==============================================================================

def apply_scale_repair_with_intraday_propagation(
    ticker: str,
    is_jp: bool = True,
    threshold: float = 0.35,
    dry_run: bool = False
) -> dict:
    """
    日足の異常データを修正し、同じ倍率を分足DBにも波及させる。

    Parameters
    ----------
    ticker : str
        修正対象の銘柄コード（例: "1629"）
    is_jp : bool
    threshold : float
        崖判定の閾値（デフォルト0.35 = 35%）
    dry_run : bool
        Trueのとき修正内容を表示するだけでDBへの書き込みを行わない

    Returns
    -------
    dict
        各時間足の処理結果サマリー。
        "repair_details" キーに崖ごとの詳細情報（ログ保存用）を含む。
    """
    pure_ticker = sanitize_ticker(ticker, is_jp)
    results = {}

    # ------------------------------------------------------------------
    # Step 1: 日足DBから対象銘柄を取得してbackward_scale_repairを適用
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"[{pure_ticker}] 日足 異常修正スキャン開始")
    print(f"{'='*60}")

    try:
        db_1d = load_price_db("1d", is_jp=is_jp)
    except FileNotFoundError as e:
        return {"error": str(e)}

    ticker_1d = db_1d[db_1d["ticker"] == pure_ticker].copy()

    if ticker_1d.empty:
        print(f"⚠️ [{pure_ticker}] 日足DBにデータが見つかりません。")
        return {"1d": "データなし", "repair_details": []}

    fixed_1d, repairs = backward_scale_repair(ticker_1d, threshold=threshold)

    if not repairs:
        print(f"✅ [{pure_ticker}] 日足に異常箇所は検出されませんでした。")
        return {"1d": "異常なし", "repair_details": []}

    results["1d"] = f"{len(repairs)}箇所修正"

    if not dry_run:
        db_1d = db_1d[db_1d["ticker"] != pure_ticker]
        db_1d = pd.concat([db_1d, fixed_1d], ignore_index=True)
        db_1d = db_1d.sort_values(["ticker", "date"]).reset_index(drop=True)
        save_price_db(db_1d, "1d", is_jp=is_jp)
        print(f"✅ [{pure_ticker}] 日足DB更新完了")
    else:
        print(f"[DRY RUN] [{pure_ticker}] 日足: {len(repairs)}箇所の修正が必要（書き込みスキップ）")

    # ------------------------------------------------------------------
    # Step 2: 日足の崖情報（cliff_date, multiplier）を分足DBに波及
    # ------------------------------------------------------------------
    short_intervals = ["60m", "5m", "1m"]

    for interval in short_intervals:
        print(f"\n[{pure_ticker}] {interval} への波及処理...")

        try:
            db_intra = load_price_db(interval, is_jp=is_jp)
        except FileNotFoundError:
            results[interval] = "DBなし（スキップ）"
            print(f"  ⚠️ [{pure_ticker}] {interval} DBが見つかりません。スキップします。")
            continue

        ticker_intra = db_intra[db_intra["ticker"] == pure_ticker].copy()

        if ticker_intra.empty:
            results[interval] = "データなし（スキップ）"
            continue

        ticker_intra = ticker_intra.sort_values("date").reset_index(drop=True)
        price_cols = [c for c in ["open", "high", "low", "close"] if c in ticker_intra.columns]

        # 新しい崖から古い崖へ降順で適用
        repairs_sorted = sorted(repairs, key=lambda x: x["cliff_date"], reverse=True)

        applied_count = 0
        for repair in repairs_sorted:
            cliff_date = pd.to_datetime(repair["cliff_date"])
            multiplier = repair["multiplier"]

            pre_mask = pd.to_datetime(ticker_intra["date"]) < cliff_date

            if not pre_mask.any():
                continue

            if not dry_run:
                for col in price_cols:
                    ticker_intra.loc[pre_mask, col] = ticker_intra.loc[pre_mask, col] * multiplier
                if "volume" in ticker_intra.columns:
                    ticker_intra.loc[pre_mask, "volume"] = ticker_intra.loc[pre_mask, "volume"] / multiplier

            applied_count += 1
            print(f"  🔄 [{pure_ticker}] {interval}: {cliff_date.date()} 以前を "
                  f"multiplier={multiplier:.6f} で{'[DRY RUN] ' if dry_run else ''}修正")

        if applied_count == 0:
            results[interval] = "該当期間のデータなし"
            continue

        if not dry_run:
            db_intra = db_intra[db_intra["ticker"] != pure_ticker]
            db_intra = pd.concat([db_intra, ticker_intra], ignore_index=True)
            db_intra = db_intra.sort_values(["ticker", "date"]).reset_index(drop=True)
            save_price_db(db_intra, interval, is_jp=is_jp)
            print(f"  ✅ [{pure_ticker}] {interval} DB更新完了")
            results[interval] = f"{applied_count}崖分を波及適用"
        else:
            results[interval] = f"[DRY RUN] {applied_count}崖分の波及が必要"

    # ログ保存用の詳細情報を結果に含める
    results["repair_details"] = repairs
    results["ticker"] = pure_ticker

    print(f"\n{'='*60}")
    print(f"[{pure_ticker}] 修正完了: {results}")
    print(f"{'='*60}")
    return results
