# core/collector.py
import os
import json
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from config import settings
from data_access.sheets_api import load_extra_tickers_from_sheets
import io

def fetch_etf_constituents(etf_code: str) -> dict:
    """
    SolactiveのPCF CSVから指定されたETFの構成銘柄（4桁コード: 銘柄名）を取得します。
    """
    base_url = getattr(settings, "SOLACTIVE_PCF_BASE_URL", "https://www.solactive.com/downloads/etfservices/tse-pcf/single/")
    url = f"{base_url}{etf_code}.csv"
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"❌ [fetch_etf_constituents] ダウンロード失敗 (コード: {etf_code}): {e}")
        return {}
    
    try:
        # 先頭2行（メタデータ）をスキップし、3行目をヘッダーとしてPandasで読み込む
        df = pd.read_csv(io.StringIO(response.text), skiprows=2)
        df.columns = [str(c).strip() for c in df.columns]
        
        # 'Code' と 'Name' に相当するカラムを抽出
        code_col = None
        name_col = None
        for col in df.columns:
            if col.lower() == 'code':
                code_col = col
            elif col.lower() == 'name':
                name_col = col
                
        if not code_col or not name_col:
            print(f"❌ [fetch_etf_constituents] 必要なカラム（Code / Name）が見つかりません。")
            return {}
            
        result = {}
        for _, row in df.iterrows():
            code_raw = str(row[code_col]).strip()
            # 小数点が入っている場合の除去 (例: "6723.0" -> "6723")
            code = code_raw.split(".")[0]
            name = str(row[name_col]).strip()
            
            # 英数字4桁を判定（防衛テック513A等の混在を許容するため、len=4で確認）
            if code and len(code) == 4:
                result[code] = name
                
        return result
    except Exception as e:
        print(f"❌ [fetch_etf_constituents] CSVパースエラー (コード: {etf_code}): {e}")
        return {}

def sanitize_ticker(ticker: str, is_jp: bool = True) -> str:
    """ティッカーシンボルを整形（サニタイズ）します。"""
    t = str(ticker).strip().upper()
    if is_jp and t.endswith(".T"):
        t = t[:-2]
    return t

def get_download_symbol(ticker: str, is_jp: bool = True) -> str:
    """yfinance等に渡す正規のダウンロード用シンボル（例: 7203.T）を返します。"""
    pure_ticker = sanitize_ticker(ticker, is_jp)
    if is_jp and not pure_ticker.endswith(".T") and pure_ticker.isdigit():
        return f"{pure_ticker}.T"
    return pure_ticker

def get_topix500_tickers() -> list:
    """JPX公式ExcelからTOPIX500（Core/Large/Mid）の株式コードを取得します（当日キャッシュあり）。"""
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

def get_extra_tickers() -> list:
    """ローカルにキャッシュされた追加ETF等（extra_tickers.json）のコードを読み込みます。"""
    cache_path = os.path.join(settings.WORK_DIR, "extra_tickers.json")
    if not os.path.exists(cache_path):
        return []
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("codes", [])
    except Exception as e:
        print(f"❌ [get_extra_tickers] JSONデコードエラー: {e}")
        return []

def get_all_collection_tickers() -> list:
    """TOPIX500、追加ETF、およびセクター定義シート（sector_JP）の個別株・ETFをマージしたリストを取得します（重複排除）。"""
    import pandas as pd
    # 循環参照を防ぐために関数内でインポート
    from data_access.sheets_api import get_sector_spreadsheet
    
    topix = get_topix500_tickers()
    extra = get_extra_tickers()
    
    sector_tickers = []
    try:
        # スプレッドシートから直接データを取得し、個別株(B列)とETF(D列)の両方を回収する
        sh = get_sector_spreadsheet()
        if sh:
            ws = sh.worksheet("sector_JP")
            records = ws.get_all_records()
            if records:
                df = pd.DataFrame(records)
                
                # B列相当（銘柄コード）の抽出
                code_col = next((c for c in df.columns if c in ["銘柄コード", "code", "ticker", "コード"]), None)
                if code_col:
                    codes = df[code_col].dropna().astype(str).str.strip().str.split(".").str[0].tolist()
                    sector_tickers.extend([c for c in codes if c])
                    
                # D列相当（ETFコード）の抽出
                etf_col = next((c for c in df.columns if c in ["ETFコード", "etf", "etf_code"]), None)
                if etf_col:
                    etfs = df[etf_col].dropna().astype(str).str.strip().str.split(".").str[0].tolist()
                    sector_tickers.extend([e for e in etfs if e])
                    
    except Exception as e:
        print(f"❌ [get_all_collection_tickers] セクター定義シート読み込みエラー: {e}")

    # 3つのソースを全て結合
    merged = topix + extra + sector_tickers
    
    # 順序を保持したまま重複を完全に排除（セットを使って高速化）
    cleaned = []
    seen = set()
    for t in merged:
        t_clean = str(t).strip()
        # 空文字でなく、かつ未登録のものだけ追加
        if t_clean and t_clean not in seen:
            seen.add(t_clean)
            cleaned.append(t_clean)
            
    return cleaned

def sync_extra_tickers_to_local() -> tuple:
    """Google Sheetsから追加ティッカーを取得し、ローカルのJSONキャッシュと同期します。"""
    try:
        df = load_extra_tickers_from_sheets()
        if df.empty:
            raise ValueError("スプレッドシートから追加ティッカーが取得できません。")
        codes = df["code"].tolist()
        cache_path = os.path.join(settings.WORK_DIR, "extra_tickers.json")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"codes": codes, "updated": datetime.now().strftime("%Y-%m-%d")}, f)
        return codes, None
    except Exception as e:
        return [], str(e)

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

# core/collector.py より修正

# core/collector.py より修正

def parse_yfinance_batch(df_raw: pd.DataFrame, chunk_tickers: list, is_jp: bool = True) -> pd.DataFrame:
    """yfinanceの生バッチ出力（MultiIndex対応含む）を統一されたDataFrame形式にパースします。"""
    if df_raw.empty:
        return pd.DataFrame()
    all_rows = []
    is_multi = isinstance(df_raw.columns, pd.MultiIndex)
    suffix = ".T" if is_jp else ""
    
    # 強制的に数値キャストおよび無限大（inf）をNaNに変換する対象カラム
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
            
            # --- 数値キャストおよび異常値のクレンジング処理 ---
            for col in numeric_cols:
                if col in t_df.columns:
                    # errors='coerce' で非数値や不適切な文字列をすべて NaN にキャスト
                    t_df[col] = pd.to_numeric(t_df[col], errors='coerce')
                    # 実数値の inf, -inf も nan に置換して保存時の PyArrow エラーを防止
                    t_df[col] = t_df[col].replace([float('inf'), float('-inf')], float('nan'))
            
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
            
            # --- 数値キャストおよび異常値のクレンジング処理 ---
            for col in numeric_cols:
                if col in t_df.columns:
                    # errors='coerce' で非数値や不適切な文字列をすべて NaN にキャスト
                    t_df[col] = pd.to_numeric(t_df[col], errors='coerce')
                    # 実数値の inf, -inf も nan に置換して保存時の PyArrow エラーを防止
                    t_df[col] = t_df[col].replace([float('inf'), float('-inf')], float('nan'))
            
            target_cols = ["date", "ticker", "open", "high", "low", "close", "adj close", "volume", "stock splits", "dividends"]
            valid_cols = [c for c in target_cols if c in t_df.columns]
            all_rows.append(t_df[valid_cols])
        except Exception:
            continue
            
    return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    
def get_benchmark_latest_date(interval: str, is_jp: bool = True) -> pd.Timestamp:
    """高流動性のベンチマークを用いて取引所の最新の日時を判定し、時間外ノイズを丸めて返します。"""
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