# core/collector.py
import os
import json
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, time as dt_time
from config import settings
import io

def fetch_etf_constituents(etf_code: str, fund_provider: str = None) -> dict:
    """ETFの構成銘柄（PCF）を自動取得します。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://www.nomura-am.co.jp/",
        "Connection": "keep-alive"
    }
    df = None
    provider = str(fund_provider).strip().lower() if fund_provider else ""

    etf_code = str(etf_code).strip().upper()
    if "." in etf_code:
        etf_code = etf_code.split(".")[0]

    print(f"🔎 [{etf_code}] 構成銘柄データの取得を開始します (ファンド: {fund_provider or '自動判定'})")

    # Global X (Solactive)
    if not provider or "global" in provider or "solactive" in provider:
        base_url = "https://legacy2.solactive.com/downloads/etfservices/tse-pcf/single/"
        solactive_url = f"{base_url}{etf_code}.csv"
        
        try:
            response = requests.get(solactive_url, headers=headers, timeout=10)
            if response.status_code == 200:
                print("  -> ✅ Solactiveサーバーからデータを検出しました。")
                lines = response.text.splitlines()
                header_idx = -1
                import csv
                
                for i, line in enumerate(lines[:15]):
                    try:
                        row_cells = next(csv.reader([line]))
                        row_cells_clean = [str(c).strip().lower() for c in row_cells]
                        if "code" in row_cells_clean and "name" in row_cells_clean:
                            header_idx = i
                            break
                    except Exception:
                        continue
                
                if header_idx != -1:
                    df = pd.read_csv(io.StringIO(response.text), skiprows=header_idx)
                    df.columns = [str(c).strip().lower() for c in df.columns]
                    code_col = next((col for col in df.columns if "code" in col or "ticker" in col), None)
                    name_col = next((col for col in df.columns if "name" in col), None)
        except Exception as e:
            print(f"  -> ❌ Solactive取得失敗: {e}")

    # NEXT FUNDS (野村アセット)
    if (df is None or df.empty) and (not provider or "next" in provider or "nomura" in provider):
        try:
            nf_url = f"https://www.nomura-am.co.jp/fund/monthly_holdings/{etf_code}_brd_data.xlsx"
            file_resp = requests.get(nf_url, headers=headers, timeout=15)
            
            if file_resp.status_code == 200:
                is_real_excel = file_resp.content.startswith(b'PK\x03\x04')
                if not is_real_excel:
                    print("  -> ⚠️ [警告] ダウンロードされたデータは有効なExcelファイルではありません！")
                    df = None
                else:
                    print(f"  -> 📥 NEXT FUNDSファイルを発見: {nf_url}")
                    xl = pd.ExcelFile(io.BytesIO(file_resp.content))
                    target_sheet = None
                    
                    if "保有明細" in xl.sheet_names:
                        target_sheet = "保有明細"
                    else:
                        valid_sheets = [s for s in xl.sheet_names if s != "$MetaData" and "実行" not in s]
                        target_sheet = valid_sheets[0] if valid_sheets else xl.sheet_names[0]
                            
                    df = xl.parse(sheet_name=target_sheet, header=None)
            else:
                nf_url_csv = f"https://www.nomura-am.co.jp/fund/monthly_holdings/{etf_code}_brd_data.csv"
                file_resp_csv = requests.get(nf_url_csv, headers=headers, timeout=15)
                if file_resp_csv.status_code == 200:
                    print(f"  -> 📥 NEXT FUNDSファイル(CSV)を発見: {nf_url_csv}")
                    content = file_resp_csv.content.decode('shift_jis', errors='replace')
                    df = pd.read_csv(io.StringIO(content), header=None)
            
            if df is not None and not df.empty:
                target_row_idx = -1
                for i, row in df.head(20).iterrows():
                    row_strs = [str(v).strip().replace('\n', '').replace('\r', '').lower() for v in row.values]
                    has_code_cell = any("銘柄コード" in s or "code" in s for s in row_strs)
                    has_name_cell = any(("銘柄" in s or "name" in s) and "コード" not in s and "code" not in s for s in row_strs)
                    if has_code_cell and has_name_cell:
                        target_row_idx = i
                        break
                
                if target_row_idx != -1:
                    df.columns = [str(c).strip().replace('\n', '').replace('\r', '').lower() for c in df.iloc[target_row_idx]]
                    df = df.iloc[target_row_idx+1:].reset_index(drop=True)
                    code_col = next((col for col in df.columns if "銘柄コード" in col), None)
                    name_col = next((col for col in df.columns if "name" in col and "コード" not in col and "code" not in col), None)
                else:
                    df = None
        except Exception as e:
            print(f"  -> ❌ NEXT FUNDS取得・解析失敗: {e}")
            df = None

    if df is None or df.empty:
        print(f"❌ [{etf_code}] 構成銘柄データの取得またはパースに失敗しました。")
        return {}

    try:
        if not code_col or not name_col:
            return {}
        result = {}
        for _, row in df.iterrows():
            code_raw = str(row[code_col]).strip()
            code = code_raw.split(".")[0]
            name = str(row[name_col]).strip()
            if code and len(code) == 4 and code.isalnum():
                result[code] = name
        return result
    except Exception as e:
        print(f"❌ [{etf_code}] 最終パース中にエラーが発生しました: {e}")
        return {}

def sanitize_ticker(ticker: str, is_jp: bool = True) -> str:
    """ティッカーシンボルを整形（サニタイズ）します。"""
    t = str(ticker).strip().upper()
    if is_jp and t.endswith(".T"):
        t = t[:-2]
    return t

def get_download_symbol(ticker: str, is_jp: bool = True) -> str:
    """ 正規のダウンロード用シンボル（例: 7203.T）を返します。"""
    pure_ticker = sanitize_ticker(ticker, is_jp)
    if is_jp and not pure_ticker.endswith(".T") and pure_ticker.isdigit():
        return f"{pure_ticker}.T"
    return pure_ticker

def get_topix500_tickers() -> list:
    """JPX公式ExcelからTOPIX500（Core/Large/Mid）の株式コードを取得します。"""
    cache_path = os.path.join(settings.WORK_DIR, "jpx_ticker_cache.json")
    today_str = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                cache = json.load(f)
            if cache.get("date") == today_str and cache.get("tickers"):
                return cache["tickers"]
        except Exception:
            pass
    try:
        resp = requests.get(settings.JPX_URL, timeout=10)
        jpx_save_path = os.path.join(settings.DRIVE_DIR, "jpx_stock_list_raw.xls")
        with open(jpx_save_path, "wb") as f:
            f.write(resp.content)
        df_full = pd.read_excel(jpx_save_path)

        df_scale = df_full.iloc[:, [1, 2, 3, 9]].copy()
        df_scale.columns = ['symbol', 'name', 'market', 'scale_type']
        target_scales = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
        topix500 = df_scale[df_scale["scale_type"].isin(target_scales)]['symbol'].dropna()

        all_symbols = topix500.drop_duplicates()
        codes = [str(s).strip().split('.')[0] for s in all_symbols if str(s).strip()]
        try:
            with open(cache_path, "w") as f:
                json.dump({"date": today_str, "tickers": codes}, f)
        except Exception:
            pass
        return codes
    except Exception as e:
        print(f"JPX銘柄リスト取得失敗: {e}")
        return []

# --- 🚀 JPX規模区分マッピング関数の追加 ---
def get_jpx_scale_map() -> dict:
    """JPXのキャッシュファイルから {銘柄コード: 規模区分} の辞書を構築します。"""
    jpx_save_path = os.path.join(settings.DRIVE_DIR, "jpx_stock_list_raw.xls")
    if not os.path.exists(jpx_save_path):
        jpx_save_path = os.path.join(settings.WORK_DIR, "jpx_stock_list_raw.xls")
        
    if not os.path.exists(jpx_save_path):
        return {}
        
    try:
        df_full = pd.read_excel(jpx_save_path)
        if df_full.shape[1] >= 10:
            df_scale = df_full.iloc[:, [1, 9]].copy()
            df_scale.columns = ['symbol', 'scale_type']
            df_scale['symbol'] = df_scale['symbol'].astype(str).str.strip().str.split('.').str[0]
            df_scale['scale_type'] = df_scale['scale_type'].astype(str).str.strip()
            return dict(zip(df_scale['symbol'], df_scale['scale_type']))
    except Exception as e:
        print(f"⚠️ [get_jpx_scale_map] 読み込みエラー: {e}")
        
    return {}

# 互換性維持のための空関数
def get_extra_tickers() -> list:
    """【廃止】後方互換性のため空リストを返します。"""
    return []

def sync_extra_tickers_to_local() -> tuple:
    """【廃止】後方互換性のため空の処理を返します。"""
    return [], None

def get_all_collection_tickers() -> list:
    """TOPIX500、およびセクター定義シート（sector_JP）の個別株・ETFをマージしたリストを取得します（重複排除）。"""
    from data_access.sheets_api import get_sector_spreadsheet
    
    topix = get_topix500_tickers()
    
    sector_tickers = []
    try:
        sh = get_sector_spreadsheet()
        if sh:
            ws = sh.worksheet("sector_JP")
            all_values = ws.get_all_values()
            if all_values and len(all_values) > 1:
                headers = [str(h).strip().lower() for h in all_values[0]]
                code_idx = -1
                for i, h in enumerate(headers):
                    if h in ["銘柄コード", "code", "ticker", "コード"]:
                        code_idx = i
                        break
                
                if code_idx != -1:
                    for row in all_values[1:]:
                        if len(row) > code_idx:
                            code_raw = str(row[code_idx]).strip()
                            code = code_raw.split(".")[0]
                            if code and len(code) > 0:
                                sector_tickers.append(code)
                                
    except Exception as e:
        print(f"❌ [get_all_collection_tickers] sector_JP シートからの銘柄コード抽出に失敗しました: {e}")

    merged = topix + sector_tickers
    
    cleaned = []
    seen = set()
    for t in merged:
        t_clean = str(t).strip()
        if t_clean and t_clean not in seen:
            seen.add(t_clean)
            cleaned.append(t_clean)
            
    return cleaned

def load_tickers_from_file(file_path: str) -> list:
    """ユーザーがアップロードしたCSV/Excelファイルをパースして、銘柄コードを抽出します。"""
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
        return []
        
    ext = os.path.splitext(actual_path)[1].lower()
    try:
        if ext == '.csv':
            df = pd.read_csv(actual_path)
        elif ext in ['.xls', '.xlsx']:
            df = pd.read_excel(actual_path)
        else:
            return []
            
        if df.empty:
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
        else:
            raw_tickers = df.iloc[:, 0].dropna().astype(str).tolist()
            first_col_name = str(df.columns[0]).strip().split('.')[0]
            if first_col_name and not any(h in first_col_name.lower() for h in ['name', 'date', '日付', '市場', '価格', 'close']):
                raw_tickers.insert(0, str(df.columns[0]))
        
        cleaned = []
        for t in raw_tickers:
            t_clean = t.strip().split('.')[0]
            if t_clean and t_clean.isalnum():
                cleaned.append(t_clean)
        seen = set()
        return [x for x in cleaned if not (x in seen or seen.add(x))]
    except Exception as e:
        print(f"❌ [load_tickers_from_file] 読み込み失敗: {e}")
        return []

def parse_yfinance_batch(df_raw: pd.DataFrame, chunk_tickers: list, is_jp: bool = True) -> pd.DataFrame:
    """yfinanceの生バッチ出力をパースします。"""
    if df_raw.empty:
        return pd.DataFrame()
    all_rows = []
    is_multi = isinstance(df_raw.columns, pd.MultiIndex)
    suffix = ".T" if is_jp else ""
    numeric_cols = ["open", "high", "low", "close", "adj close", "volume", "stock splits", "dividends"]
    
    if not is_multi:
        if len(chunk_tickers) == 1:
            t_df = df_raw.copy()
            t_df = t_df.dropna(how="all").reset_index()
            t_df.columns = [str(c).lower() for c in t_df.columns]
            t_df = t_df.rename(columns={"datetime": "date", "index": "date"})
            dt_col = pd.to_datetime(t_df["date"])
            t_df["date"] = dt_col.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None) if dt_col.dt.tz is not None else dt_col
            t_df["ticker"] = str(chunk_tickers[0])
            
            for col in numeric_cols:
                if col in t_df.columns:
                    t_df[col] = pd.to_numeric(t_df[col], errors='coerce')
                    t_df[col] = t_df[col].replace([float('inf'), float('-inf')], float('nan'))
            
            if "date" in t_df.columns:
                times = t_df["date"].dt.time
                start_time = dt_time(9, 0)
                end_time = dt_time(15, 30) if is_jp else dt_time(16, 0)
                if not is_jp:
                    start_time = dt_time(9, 30)
                
                is_intraday = not (times == dt_time(0, 0)).all()
                if is_intraday:
                    t_df = t_df[(times >= start_time) & (times <= end_time)]
            
            target_cols = ["date", "ticker", "open", "high", "low", "close", "adj close", "volume", "stock splits", "dividends"]
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
            
            for col in numeric_cols:
                if col in t_df.columns:
                    t_df[col] = pd.to_numeric(t_df[col], errors='coerce')
                    t_df[col] = t_df[col].replace([float('inf'), float('-inf')], float('nan'))
            
            if "date" in t_df.columns:
                times = t_df["date"].dt.time
                start_time = dt_time(9, 0)
                end_time = dt_time(15, 30) if is_jp else dt_time(16, 0)
                if not is_jp:
                    start_time = dt_time(9, 30)
                
                is_intraday = not (times == dt_time(0, 0)).all()
                if is_intraday:
                    t_df = t_df[(times >= start_time) & (times <= end_time)]
            
            target_cols = ["date", "ticker", "open", "high", "low", "close", "adj close", "volume", "stock splits", "dividends"]
            valid_cols = [c for c in target_cols if c in t_df.columns]
            all_rows.append(t_df[valid_cols])
        except Exception:
            continue
            
    return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    
def get_benchmark_latest_date(interval: str, is_jp: bool = True) -> pd.Timestamp:
    """ベンチマークを用いて取引所の最新の日時を判定します。"""
    symbols = ["7203.T", "^N225"] if is_jp else ["AAPL", "^GSPC"]
    for bm_symbol in symbols:
        try:
            df_bm = yf.download(bm_symbol, period="5d", interval=interval, progress=False, auto_adjust=True)
            if not df_bm.empty:
                latest_dt = df_bm.index[-1]
                if latest_dt.tzinfo is not None:
                    if is_jp:
                        latest_dt = latest_dt.tz_convert("Asia/Tokyo").tz_localize(None)
                    else:
                        latest_dt = latest_dt.tz_localize(None)
                else:
                    latest_dt = pd.to_datetime(latest_dt)
                
                if interval != "1d":
                    limit_hour = 15 if is_jp else 16
                    limit_time = datetime.strptime(f"{limit_hour}:00:00", "%H:%M:%S").time()
                    if latest_dt.time() > limit_time:
                        latest_dt = latest_dt.replace(hour=limit_hour, minute=0, second=0, microsecond=0)
                return latest_dt
        except Exception:
            continue
    return None