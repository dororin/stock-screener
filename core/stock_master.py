# core/stock_master.py

import os
import io
import time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import requests
from tradingview_screener import Query

from config import settings

_STOCK_MASTER_MEMORY_CACHE = None
_STOCK_MASTER_CACHE_TIME = 0
CACHE_TTL_SECONDS = 3600  # 1時間

STOCK_MASTER_COLUMNS = [
    "symbol",
    "name",
    "scale_type",
    "is_topix500",
    "market_cap",
    "market_cap_rank",
    "is_active",
    "updated_at"
]

PARQUET_FILE_NAME = "stock_master.parquet"


def fetch_live_tse_universe_via_tv() -> pd.DataFrame:
    """TradingView Screener API から東証全銘柄をリアルタイム取得"""
    try:
        query = (
            Query()
            .set_markets("japan")
            .select("name", "description", "market_cap_basic", "close", "type", "subtype", "exchange")
            .limit(10000)
        )
        total_count, df_res = query.get_scanner_data()
        if df_res is None or df_res.empty:
            return pd.DataFrame()

        tse_mask = pd.Series(True, index=df_res.index)
        if "exchange" in df_res.columns:
            tse_mask = df_res["exchange"].astype(str).str.upper().isin(["TSE", "TOKYO"])
        elif "ticker" in df_res.columns:
            tse_mask = df_res["ticker"].astype(str).str.startswith("TSE:")

        df_tse = df_res[tse_mask].copy()
        if "name" in df_tse.columns:
            df_tse["symbol"] = df_tse["name"].astype(str).str.strip().str.upper()
        else:
            df_tse["symbol"] = df_tse["ticker"].astype(str).str.split(":").str[-1].str.strip().str.upper()

        df_tse = df_tse[df_tse["symbol"].str.match(r"^[0-9A-Z]{3,6}$")].copy()
        df_tse["tv_name"] = df_tse["description"].fillna("").astype(str).str.strip()
        df_tse["tv_name"] = df_tse["tv_name"].replace("", np.nan).fillna(df_tse["symbol"])
        df_tse["market_cap"] = pd.to_numeric(df_tse.get("market_cap_basic"), errors="coerce").fillna(0.0)

        df_valid_mcap = df_tse[df_tse["market_cap"] > 0].copy()
        df_valid_mcap["market_cap_rank"] = df_valid_mcap["market_cap"].rank(ascending=False, method="min").astype(int)
        df_tse = df_tse.merge(df_valid_mcap[["symbol", "market_cap_rank"]], on="symbol", how="left")
        df_tse["market_cap_rank"] = df_tse["market_cap_rank"].fillna(99999).astype(int)
        df_tse["is_active"] = True

        out_cols = ["symbol", "tv_name", "market_cap", "market_cap_rank", "is_active"]
        return df_tse[out_cols].drop_duplicates(subset=["symbol"]).reset_index(drop=True)
    except Exception as ex:
        print(f"❌ [stock_master] TradingView API取得エラー: {ex}")
        return pd.DataFrame()


def _load_jpx_raw_excel(force_download: bool = False) -> pd.DataFrame:
    """JPX公式Excelをダウンロード（💡 30日以上経過で自動再ダウンロード）"""
    jpx_path = os.path.join(settings.DRIVE_DIR, "jpx_stock_list_raw.xlsx")
    
    # 💡 改善：ファイルが30日以上古ければ強制再ダウンロード
    file_too_old = False
    if os.path.exists(jpx_path):
        file_age_days = (time.time() - os.path.getmtime(jpx_path)) / 86400.0
        if file_age_days > 30:
            file_too_old = True

    need_download = force_download or not os.path.exists(jpx_path) or os.path.getsize(jpx_path) < 10000 or file_too_old

    if need_download:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            resp = requests.get(settings.JPX_URL, headers=headers, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 10000:
                os.makedirs(os.path.dirname(jpx_path), exist_ok=True)
                with open(jpx_path, "wb") as f:
                    f.write(resp.content)
        except Exception as e:
            print(f"⚠️ [stock_master] JPX銘柄リストのダウンロード失敗: {e}")

    if not os.path.exists(jpx_path):
        return pd.DataFrame()

    for eng in ["openpyxl", None, "xlrd"]:
        try:
            df = pd.read_excel(jpx_path, engine=eng)
            if df is not None and not df.empty and df.shape[1] >= 10:
                return df
        except Exception:
            continue
    return pd.DataFrame()


def build_unified_stock_master(force_reload_jpx: bool = False) -> pd.DataFrame:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    df_tv = fetch_live_tse_universe_via_tv()
    df_jpx_raw = _load_jpx_raw_excel(force_download=force_reload_jpx)
    
    jpx_map = {}
    if not df_jpx_raw.empty and df_jpx_raw.shape[1] >= 10:
        for _, row in df_jpx_raw.iterrows():
            code_raw = str(row.iloc[1]).strip()
            if code_raw.endswith(".0"):
                code_raw = code_raw[:-2]
            code = code_raw.upper()
            name = str(row.iloc[2]).strip()
            scale = str(row.iloc[9]).strip() if pd.notna(row.iloc[9]) else "Others"
            if code and code not in ["CODE", "コード", "SYMBOL", "証券コード"]:
                jpx_map[code] = {"jpx_name": name, "scale_type": scale}

    records = []
    seen_symbols = set()

    # A. TradingView側
    if not df_tv.empty:
        for _, r in df_tv.iterrows():
            sym = str(r["symbol"]).strip().upper()
            seen_symbols.add(sym)
            mcap = float(r["market_cap"])
            mcap_rank = int(r["market_cap_rank"])
            tv_name = str(r["tv_name"]).strip()

            j_info = jpx_map.get(sym, {})
            jpx_name = j_info.get("jpx_name", "")
            jpx_scale = j_info.get("scale_type", "")
            display_name = jpx_name if jpx_name and jpx_name != "-" else tv_name

            is_topix500 = False
            scale_type = "Others"

            if jpx_scale in ["TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"]:
                scale_type = jpx_scale
                is_topix500 = True
            elif jpx_scale and jpx_scale not in ["-", "nan", "Others"]:
                scale_type = jpx_scale
                is_topix500 = False
            else:
                if mcap_rank <= 500 and mcap > 0:
                    scale_type = "TOPIX Mid400"
                    is_topix500 = True
                else:
                    scale_type = "Others"
                    is_topix500 = False

            records.append({
                "symbol": sym, "name": display_name, "scale_type": scale_type,
                "is_topix500": is_topix500, "market_cap": mcap, "market_cap_rank": mcap_rank,
                "is_active": True, "updated_at": now_str
            })

    # B. JPX公式にあってTradingView未ヒットの銘柄（ETFなど東証公式現役上場銘柄）
    for sym, j_info in jpx_map.items():
        if sym not in seen_symbols:
            scale_val = j_info.get("scale_type", "Others")
            records.append({
                "symbol": sym, "name": j_info.get("jpx_name", sym), "scale_type": scale_val,
                "is_topix500": (scale_val in ["TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"]),
                "market_cap": 0.0, "market_cap_rank": 99999,
                "is_active": True,  # 💡 JPX公式掲載銘柄（全ETF含む）はアクティブ
                "updated_at": now_str
            })

    df_master = pd.DataFrame(records)
    if df_master.empty:
        return pd.DataFrame(columns=STOCK_MASTER_COLUMNS)

    return df_master.drop_duplicates(subset=["symbol"]).sort_values(
        by=["is_active", "is_topix500", "market_cap_rank"], ascending=[False, False, True]
    ).reset_index(drop=True)[STOCK_MASTER_COLUMNS]


def sync_stock_master_to_storage(force_reload_jpx: bool = False) -> tuple[bool, str]:
    """統合マスターを構築し、ローカルParquetおよびGoogle Sheetsへ保存"""
    global _STOCK_MASTER_MEMORY_CACHE, _STOCK_MASTER_CACHE_TIME

    df_master = build_unified_stock_master(force_reload_jpx=force_reload_jpx)
    if df_master.empty:
        return False, "統合マスターの構築結果が空でした。"

    local_path = os.path.join(settings.WORK_DIR, PARQUET_FILE_NAME)
    try:
        os.makedirs(settings.WORK_DIR, exist_ok=True)
        df_master.to_parquet(local_path, index=False, compression="SNAPPY")
    except Exception as ex:
        print(f"⚠️ [stock_master] ローカルParquet保存失敗: {ex}")

    _STOCK_MASTER_MEMORY_CACHE = df_master.copy()
    _STOCK_MASTER_CACHE_TIME = time.time()

    # Google Sheetsへの書き込み
    try:
        from data_access.sheets_api import get_sector_spreadsheet
        sh = get_sector_spreadsheet()
        if sh:
            sheet_title = "stock_master"
            try:
                ws = sh.worksheet(sheet_title)
            except Exception:
                ws = sh.add_worksheet(title=sheet_title, rows=len(df_master) + 50, cols=len(STOCK_MASTER_COLUMNS))

            save_df = df_master.copy()
            save_df["is_topix500"] = save_df["is_topix500"].astype(str)
            save_df["is_active"] = save_df["is_active"].astype(str)
            save_df = save_df.fillna("")

            rows = [STOCK_MASTER_COLUMNS] + save_df.values.tolist()
            ws.clear()
            ws.update(values=rows, range_name="A1")
            return True, f"Google Sheetsおよびローカル保存完了 ({len(df_master)}件)"
    except Exception as e_sheet:
        return True, f"ローカル保存完了 (Sheets警告: {e_sheet})"

    return True, f"ローカルParquet保存完了 ({len(df_master)}件)"


def load_stock_master_from_sheets() -> pd.DataFrame:
    """💡【新設】Google Sheetsの stock_master シートから最新マスターをフェッチ"""
    try:
        from data_access.sheets_api import get_sector_spreadsheet
        sh = get_sector_spreadsheet()
        if not sh:
            return pd.DataFrame()
        ws = sh.worksheet("stock_master")
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
        if "is_active" in df.columns:
            df["is_active"] = df["is_active"].astype(str).str.upper().isin(["TRUE", "1", "YES"])
        if "is_topix500" in df.columns:
            df["is_topix500"] = df["is_topix500"].astype(str).str.upper().isin(["TRUE", "1", "YES"])
        return df
    except Exception:
        return pd.DataFrame()


def get_stock_master(force_reload: bool = False) -> pd.DataFrame:
    """
    💡【改善】メモリキャッシュ ➔ ローカルParquet ➔ Google Sheets ➔ 再構築 の4段階フォールバック
    クラウド（Streamlit）で更新されたマスターをローカルPCが確実に同期します。
    """
    global _STOCK_MASTER_MEMORY_CACHE, _STOCK_MASTER_CACHE_TIME

    now = time.time()
    if not force_reload and _STOCK_MASTER_MEMORY_CACHE is not None and (now - _STOCK_MASTER_CACHE_TIME < CACHE_TTL_SECONDS):
        return _STOCK_MASTER_MEMORY_CACHE.copy()

    local_path = os.path.join(settings.WORK_DIR, PARQUET_FILE_NAME)

    # 1. ローカルParquetの有効期限チェック
    if not force_reload and os.path.exists(local_path):
        try:
            file_age = now - os.path.getmtime(local_path)
            if file_age < CACHE_TTL_SECONDS:
                df = pd.read_parquet(local_path)
                if not df.empty:
                    _STOCK_MASTER_MEMORY_CACHE = df.copy()
                    _STOCK_MASTER_CACHE_TIME = now
                    return df
        except Exception:
            pass

    # 2. 💡 クラウド（Google Sheets）からのフェッチを最優先試行
    if not force_reload:
        df_sheets = load_stock_master_from_sheets()
        if not df_sheets.empty and len(df_sheets) >= 3000:
            try:
                os.makedirs(settings.WORK_DIR, exist_ok=True)
                df_sheets.to_parquet(local_path, index=False, compression="SNAPPY")
            except Exception:
                pass
            _STOCK_MASTER_MEMORY_CACHE = df_sheets.copy()
            _STOCK_MASTER_CACHE_TIME = now
            print(f"☁️ [stock_master] Google Sheetsから最新の統合マスターを同期ロードしました ({len(df_sheets)}件)")
            return df_sheets

    # 3. どちらも無ければフル再構築
    success, msg = sync_stock_master_to_storage(force_reload_jpx=force_reload)
    if _STOCK_MASTER_MEMORY_CACHE is not None:
        return _STOCK_MASTER_MEMORY_CACHE.copy()

    return pd.DataFrame(columns=STOCK_MASTER_COLUMNS)