# core/stock_master.py

import os
import io
import time
from datetime import datetime
import pandas as pd
import numpy as np
import requests
from tradingview_screener import Query

from config import settings

# ─── キャッシュ用グローバル変数（非Streamlitバッチ環境用） ───
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
    """
    TradingView Screener API を用いて、東証全上場銘柄の
    [symbol, name, market_cap, is_active] をバルク取得します (所要時間: 約1〜2秒)。
    """
    try:
        # 東証全銘柄を上限10,000件で一括取得
        query = (
            Query()
            .set_markets("japan")
            .select("name", "description", "market_cap_basic", "close", "type", "subtype", "exchange")
            .limit(10000)
        )
        total_count, df_res = query.get_scanner_data()

        if df_res is None or df_res.empty:
            print("⚠️ [stock_master] TradingViewから銘柄データを取得できませんでした。")
            return pd.DataFrame()

        # 東証 (TSE) 銘柄のみ抽出
        tse_mask = pd.Series(True, index=df_res.index)
        if "exchange" in df_res.columns:
            tse_mask = df_res["exchange"].astype(str).str.upper().isin(["TSE", "TOKYO"])
        elif "ticker" in df_res.columns:
            tse_mask = df_res["ticker"].astype(str).str.startswith("TSE:")

        df_tse = df_res[tse_mask].copy()

        # symbol (銘柄コード) の正規化 (例: 'TSE:7203' -> '7203', '642A' -> '642A')
        if "name" in df_tse.columns:
            df_tse["symbol"] = df_tse["name"].astype(str).str.strip().str.upper()
        else:
            df_tse["symbol"] = df_tse["ticker"].astype(str).str.split(":").str[-1].str.strip().str.upper()

        # 銘柄コード形式のバリデーション（英数字3〜6文字）
        df_tse = df_tse[df_tse["symbol"].str.match(r"^[0-9A-Z]{3,6}$")].copy()

        # 社名 (TradingViewの日本語description優先)
        df_tse["tv_name"] = df_tse["description"].fillna("").astype(str).str.strip()
        df_tse["tv_name"] = df_tse["tv_name"].replace("", np.nan).fillna(df_tse["symbol"])

        # 時価総額 (market_cap_basic: 円単位)
        df_tse["market_cap"] = pd.to_numeric(df_tse.get("market_cap_basic"), errors="coerce").fillna(0.0)

        # 東証内での時価総額順位を算出
        # 時価総額が有効な銘柄を降順ソートしてランク付け
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
    """JPX公式の data_j.xlsx をダウンロード・読み込みます。"""
    jpx_path = os.path.join(settings.DRIVE_DIR, "jpx_stock_list_raw.xlsx")
    need_download = force_download or not os.path.exists(jpx_path) or os.path.getsize(jpx_path) < 10000

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
    """
    1. TradingView APIから東証全銘柄をリアルタイム取得。
    2. JPXの data_j.xlsx から規模区分・社名をロード。
    3. TradingViewの銘柄をベースに、JPX規模区分をマージ。
    4. 【時価総額セーフティネット判定】:
       - JPX月次統計に未掲載の新銘柄（例: 642A）であっても、
         TradingViewの時価総額が東証上位500位以内であれば
         scale_type = "TOPIX Mid400", is_topix500 = True として救済補完。
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. TradingView から東証アクティブ銘柄を取得
    df_tv = fetch_live_tse_universe_via_tv()

    # 2. JPX 公式Excelをパース
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
                jpx_map[code] = {
                    "jpx_name": name,
                    "scale_type": scale
                }

    # 3. マスターDataFrameの統合構築
    records = []
    seen_symbols = set()

    # A. TradingView に存在する銘柄群の処理（現在取引可能な全銘柄）
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

            # 社名の決定: JPXに正式社名があれば採用、無ければTradingViewの名称
            display_name = jpx_name if jpx_name and jpx_name != "-" else tv_name

            # 規模区分およびTOPIX500判定
            is_topix500 = False
            scale_type = "Others"

            if jpx_scale in ["TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"]:
                scale_type = jpx_scale
                is_topix500 = True
            elif jpx_scale and jpx_scale not in ["-", "nan", "Others"]:
                scale_type = jpx_scale
                is_topix500 = False
            else:
                # ── 💡【時価総額セーフティネット】新上場・持株会社化銘柄の自動判定 ──
                # JPX未掲載（月次ズレ）でも時価総額上位500位以内なら自動的にTOPIX500（暫定Mid400）
                if mcap_rank <= 500 and mcap > 0:
                    scale_type = "TOPIX Mid400"
                    is_topix500 = True
                    print(f"🛡️ [stock_master] セーフティネット救済: [{sym}] {display_name} (時価総額順位: #{mcap_rank}) ➔ TOPIX Mid400")
                else:
                    scale_type = "Others"
                    is_topix500 = False

            records.append({
                "symbol": sym,
                "name": display_name,
                "scale_type": scale_type,
                "is_topix500": is_topix500,
                "market_cap": mcap,
                "market_cap_rank": mcap_rank,
                "is_active": True,
                "updated_at": now_str
            })

    # B. JPX公式にあってTradingView未ヒットの銘柄（上場廃止・整理銘柄などのフォールバック）
    for sym, j_info in jpx_map.items():
        if sym not in seen_symbols:
            scale_val = j_info.get("scale_type", "Others")
            records.append({
                "symbol": sym,
                "name": j_info.get("jpx_name", sym),
                "scale_type": scale_val,
                "is_topix500": scale_val in ["TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"],
                "market_cap": 0.0,
                "market_cap_rank": 99999,
                "is_active": true,  # TradingViewで現在取引不可 = 非アクティブ
                "updated_at": now_str
            })

    df_master = pd.DataFrame(records)
    if df_master.empty:
        return pd.DataFrame(columns=STOCK_MASTER_COLUMNS)

    # 重複排除とソート（TOPIX500優先、次いで時価総額順位昇順）
    df_master = df_master.drop_duplicates(subset=["symbol"]).sort_values(
        by=["is_active", "is_topix500", "market_cap_rank"],
        ascending=[False, False, True]
    ).reset_index(drop=True)

    return df_master[STOCK_MASTER_COLUMNS]


def sync_stock_master_to_storage(force_reload_jpx: bool = False) -> tuple[bool, str]:
    """
    統合マスターを構築し、
    1. ローカルParquetキャッシュ (data_work/stock_master.parquet)
    2. Google Sheets の 'stock_master' タブ
    へ保存・同期します。
    """
    global _STOCK_MASTER_MEMORY_CACHE, _STOCK_MASTER_CACHE_TIME

    df_master = build_unified_stock_master(force_reload_jpx=force_reload_jpx)
    if df_master.empty:
        return False, "統合マスターの構築結果が空でした。"

    # 1. ローカルParquetキャッシュへ保存
    local_path = os.path.join(settings.WORK_DIR, PARQUET_FILE_NAME)
    try:
        os.makedirs(settings.WORK_DIR, exist_ok=True)
        df_master.to_parquet(local_path, index=False, compression="SNAPPY")
    except Exception as ex:
        print(f"⚠️ [stock_master] ローカルParquet保存失敗: {ex}")

    # インメモリキャッシュの更新
    _STOCK_MASTER_MEMORY_CACHE = df_master.copy()
    _STOCK_MASTER_CACHE_TIME = time.time()

    # 2. Google Sheets の 'stock_master' シートへ保存
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
            # bool値を文字列に変換
            save_df["is_topix500"] = save_df["is_topix500"].astype(str)
            save_df["is_active"] = save_df["is_active"].astype(str)
            save_df = save_df.fillna("")

            rows = [STOCK_MASTER_COLUMNS] + save_df.values.tolist()
            ws.clear()
            ws.update(values=rows, range_name="A1")
            msg = f"統合マスターの同期に成功しました (総銘柄数: {len(df_master):,} 件, TOPIX500対象: {df_master['is_topix500'].sum():,} 件)"
            print(f"✅ [stock_master] {msg}")
            return True, msg
    except Exception as e_sheet:
        msg = f"ローカル保存完了、ただしGoogle Sheets書き込みに失敗: {e_sheet}"
        print(f"⚠️ [stock_master] {msg}")
        return True, msg

    return True, f"ローカルParquet保存完了 ({len(df_master)}件)"


def get_stock_master(force_reload: bool = False) -> pd.DataFrame:
    """
    メモリキャッシュ付きで最新の stock_master を高速返却します (TTL: 1時間)。
    ローカルParquetが存在すればミリ秒で返し、無ければ新規同期を実行します。
    """
    global _STOCK_MASTER_MEMORY_CACHE, _STOCK_MASTER_CACHE_TIME

    now = time.time()
    if not force_reload and _STOCK_MASTER_MEMORY_CACHE is not None and (now - _STOCK_MASTER_CACHE_TIME < CACHE_TTL_SECONDS):
        return _STOCK_MASTER_MEMORY_CACHE.copy()

    local_path = os.path.join(settings.WORK_DIR, PARQUET_FILE_NAME)

    # ローカルParquetが存在し、有効期限内であれば即座に読み込み
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

    # キャッシュ切れまたはforce_reload時は再同期
    success, msg = sync_stock_master_to_storage(force_reload_jpx=force_reload)
    if _STOCK_MASTER_MEMORY_CACHE is not None:
        return _STOCK_MASTER_MEMORY_CACHE.copy()

    # 万一失敗した場合のフォールバック
    if os.path.exists(local_path):
        try:
            return pd.read_parquet(local_path)
        except Exception:
            pass

    return pd.DataFrame(columns=STOCK_MASTER_COLUMNS)