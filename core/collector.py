# core/collector.py
import os
import json
import requests
import pandas as pd
from datetime import datetime
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

    # =========================================================================
    # 1. Global X (Solactive) 処理ブロック
    #    ※コード順で出力されるため、「株数 × 株価」で評価額を求めて降順ソートします。
    # =========================================================================
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

                    # 💡 グローバルX専用：株数 × 株価で評価額を計算し、組入比率降順に並び替え
                    shares_col = next((col for col in df.columns if "shares" in col or "amount" in col), None)
                    price_col = next((col for col in df.columns if "price" in col), None)

                    if shares_col and price_col:
                        shares_series = pd.to_numeric(
                            df[shares_col].astype(str).str.replace(',', '').str.strip(),
                            errors='coerce'
                        ).fillna(0)
                        price_series = pd.to_numeric(
                            df[price_col].astype(str).str.replace(',', '').str.strip(),
                            errors='coerce'
                        ).fillna(0)

                        df['_valuation'] = shares_series * price_series
                        df = df.sort_values(by='_valuation', ascending=False).reset_index(drop=True)
                        print(f"  -> 📊 [Global X] 組入金額（株数 × 株価）の大きい順に並び替え完了 (上位20銘柄に最適化)")

        except Exception as e:
            print(f"  -> ❌ Solactive取得失敗: {e}")

    # =========================================================================
    # 2. NEXT FUNDS (野村アセット) 処理ブロック
    #    ※最初から純資産比率順（No.1〜）で並んでいるため、無駄なソート処理は挟みません。
    # =========================================================================
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

def _download_jpx_file(save_path: str) -> bool:
    """JPX公式Excelをブラウザ偽装ヘッダー付きでダウンロードします（詳細デバッグログ付き）。"""
    import traceback

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
    }
    url = settings.JPX_URL
    print(f"[DEBUG] [JPX_DL] ダウンロード要求送信: {url}")
    print(f"[DEBUG] [JPX_DL] 保存先パス: {save_path}")

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        status = resp.status_code
        content_len = len(resp.content) if resp.content else 0
        content_type = resp.headers.get("Content-Type", "unknown")
        
        print(f"[DEBUG] [JPX_DL] HTTPステータス: {status}, Content-Type: {content_type}, サイズ: {content_len:,} bytes")

        if status != 200:
            print(f"❌ [JPX_DL] HTTPエラー返却: status={status}")
            if content_len > 0:
                print(f"   ↳ レスポンス抜粋: {resp.text[:300]}")
            return False

        if content_len < 10000:
            print(f"⚠️ [JPX_DL] ファイルサイズが小さすぎます ({content_len:,} bytes < 10KB)。")
            print(f"   ↳ 返却内容（HTMLエラーページの可能性）: {resp.text[:300]}")
            return False

        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(resp.content)
        print(f"✅ [JPX_DL] ダウンロードおよび保存成功 ({content_len:,} bytes): {save_path}")
        return True

    except requests.exceptions.Timeout:
        print("❌ [JPX_DL] 接続タイムアウト (15秒経過)")
        return False
    except requests.exceptions.RequestException as e_req:
        print(f"❌ [JPX_DL] 通信エラー: {e_req}")
        return False
    except Exception as e:
        print(f"❌ [JPX_DL] 予期しない例外が発生しました: {e}")
        print(traceback.format_exc())
        return False

def _read_excel_safely(file_path_or_bytes) -> pd.DataFrame:
    """複数のエンジン（openpyxl、xlrd、自動）をフォールバック試行してExcelを確実に読み込みます。"""
    for eng in ["openpyxl", None, "xlrd"]:
        try:
            df = pd.read_excel(file_path_or_bytes, engine=eng)
            if df is not None and not df.empty:
                return df
        except Exception:
            continue
    return pd.DataFrame()

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

    jpx_save_path = os.path.join(settings.DRIVE_DIR, "jpx_stock_list_raw.xlsx")
    if not os.path.exists(jpx_save_path) or os.path.getsize(jpx_save_path) < 10000:
        _download_jpx_file(jpx_save_path)

    df_full = _read_excel_safely(jpx_save_path)
    if not df_full.empty and df_full.shape[1] >= 10:
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
    return []

def get_jpx_scale_map() -> dict:
    """JPXのキャッシュファイルから {銘柄コード: 規模区分} の辞書を構築します。"""
    jpx_save_path = os.path.join(settings.DRIVE_DIR, "jpx_stock_list_raw.xlsx")
    
    file_exists = os.path.exists(jpx_save_path)
    file_size = os.path.getsize(jpx_save_path) if file_exists else 0
    print(f"[DEBUG] [JPX_SCALE] キャッシュ確認: 存在={file_exists}, サイズ={file_size:,} bytes, パス={jpx_save_path}")

    if not file_exists or file_size < 10000:
        print("📥 JPX銘柄リストが存在しないか破損しているため、新規ダウンロードします...")
        success = _download_jpx_file(jpx_save_path)
        if not success:
            print("⚠️ [get_jpx_scale_map] JPXリストのダウンロードに失敗しました。")
            return {}

    df_full = _read_excel_safely(jpx_save_path)
    if df_full.empty:
        print("❌ [get_jpx_scale_map] Excelファイルの読み込み結果が空(Empty)でした。")
        return {}

    print(f"[DEBUG] [JPX_SCALE] Excel読み込み成功: 形状={df_full.shape}")
    if df_full.shape[1] >= 10:
        df_scale = df_full.iloc[:, [1, 9]].copy()
        df_scale.columns = ['symbol', 'scale_type']
        df_scale['symbol'] = df_scale['symbol'].astype(str).str.strip().str.split('.').str[0]
        df_scale['scale_type'] = df_scale['scale_type'].astype(str).str.strip()
        result_map = dict(zip(df_scale['symbol'], df_scale['scale_type']))
        print(f"✅ [get_jpx_scale_map] 規模区分マッピング生成完了: {len(result_map):,} 件")
        return result_map
        
    print(f"⚠️ [get_jpx_scale_map] 列数が不足しています (列数: {df_full.shape[1]} < 10)")
    return {}

def get_extra_tickers() -> list:
    return []

def sync_extra_tickers_to_local() -> tuple:
    return [], None

def get_all_collection_tickers() -> list:
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
    possible_paths = [
        file_path,
        os.path.join("/content/drive/MyDrive", file_path),
        os.path.join("/content/drive/MyDrive/stock_data_hub", file_path),
        os.path.join(os.getcwd(), file_path)
    ]