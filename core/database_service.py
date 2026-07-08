# core/database_service.py
import os
import time
import pandas as pd
import pytz
import yfinance as yf
from datetime import datetime, timedelta, time as dt_time
from config import settings
from data_access.local_db import load_price_db, save_price_db
from core.collector import (
    sanitize_ticker, get_download_symbol, get_all_collection_tickers,
    get_benchmark_latest_date, parse_yfinance_batch
)

# --- yfinanceが取得可能な期間の上限（日数） ---
YFINANCE_GAP_LIMITS = {"1m": 7, "5m": 60, "60m": 730}

# --- 東証: 取引時間延伸（2024年11月5日）の境界日 ---
TSE_EXTENDED_HOURS_DATE = pd.Timestamp("2024-11-05")

def get_jp_session_close_time(date) -> dt_time:
    d = pd.Timestamp(date).normalize()
    if d >= TSE_EXTENDED_HOURS_DATE:
        return dt_time(15, 30)
    return dt_time(15, 0)

def get_market_localized_now(is_jp: bool = True):
    tz = pytz.timezone("Asia/Tokyo") if is_jp else pytz.timezone("America/New_York")
    now_tz = datetime.now(pytz.utc).astimezone(tz)
    local_today = now_tz.date()
    return now_tz, local_today

def compute_is_finalized(date_series: pd.Series, interval: str, is_jp: bool = True) -> pd.Series:
    now_tz, local_today = get_market_localized_now(is_jp)
    dt_series = pd.to_datetime(date_series)

    if interval == "1d":
        close_buffer_time = dt_time(16, 30) if is_jp else dt_time(17, 30)
        today_is_finalized = now_tz.time() >= close_buffer_time

        data_dates = dt_series.dt.date
        is_finalized = data_dates < local_today
        if today_is_finalized:
            is_finalized = is_finalized | (data_dates == local_today)
        return is_finalized
    else:
        now_naive = now_tz.replace(tzinfo=None)
        return dt_series < (now_naive - timedelta(hours=1))

def detect_allocation_stop_days(df_1d: pd.DataFrame) -> pd.DataFrame:
    empty_result = pd.DataFrame(columns=["ticker", "date", "close", "volume"])
    if df_1d is None or df_1d.empty:
        return empty_result

    df = df_1d.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

    if "is_finalized" in df.columns:
        df = df[df["is_finalized"] == True].reset_index(drop=True)
    if df.empty:
        return empty_result

    df["prev_close"] = df.groupby("ticker")["close"].shift(1)

    is_allocation_stop = (
        (df["open"] == df["high"]) &
        (df["high"] == df["low"]) &
        (df["low"] == df["close"]) &
        (df["volume"] > 0) &
        df["prev_close"].notna() &
        (df["close"] != df["prev_close"])
    )

    result = df.loc[is_allocation_stop, ["ticker", "date", "close", "volume"]].reset_index(drop=True)
    return result

def _build_synthetic_15h_bar_row(schema_cols: list, ticker: str, day_date, close_price: float, volume: float, is_jp: bool = True) -> dict:
    if is_jp:
        close_t = get_jp_session_close_time(day_date)
        bar_datetime = pd.Timestamp(day_date).normalize() + pd.Timedelta(hours=close_t.hour, minutes=close_t.minute)
    else:
        bar_datetime = pd.Timestamp(day_date).normalize() + pd.Timedelta(hours=16)
    row = {}
    for col in schema_cols:
        c = str(col).lower()
        if c == "date":
            row[col] = bar_datetime
        elif c == "ticker":
            row[col] = ticker
        elif c in ("open", "high", "low", "close", "adj close"):
            row[col] = close_price
        elif c == "volume":
            row[col] = volume
        elif c == "is_finalized":
            row[col] = True
        elif c in ("dividends", "stock splits"):
            row[col] = 0.0
        else:
            row[col] = None
    return row

def _replace_stop_allocation_bar(df_interval: pd.DataFrame, ticker: str, day_date, close_price: float, volume: float, is_jp: bool = True) -> pd.DataFrame:
    if df_interval.empty:
        schema_cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
        new_row = _build_synthetic_15h_bar_row(schema_cols, ticker, day_date, close_price, volume, is_jp=is_jp)
        return pd.DataFrame([new_row])

    day_only = pd.Timestamp(day_date).normalize()
    dt_series = pd.to_datetime(df_interval["date"])

    target_mask = (df_interval["ticker"] == ticker) & (dt_series.dt.normalize() == day_only)
    df_cleaned = df_interval[~target_mask].copy()

    new_row = _build_synthetic_15h_bar_row(df_interval.columns.tolist(), ticker, day_date, close_price, volume, is_jp=is_jp)
    df_result = pd.concat([df_cleaned, pd.DataFrame([new_row])], ignore_index=True)
    return df_result

def check_anomaly_need_patch(df_ticker: pd.DataFrame, patch_date_str: str, multiplier: float, threshold: float = 0.10) -> bool:
    """
    二重適用防止判定（t-1基準）。
    patch_date_str は「要補正Closeの日時（崖前日）」を指定します。
    before側は patch_date 以下（<=）、after側は patch_date より後（>）を基準に判定します。
    """
    if df_ticker.empty or len(df_ticker) < 2:
        return False
        
    df_t = df_ticker.sort_values("date").reset_index(drop=True)
    df_t["date_dt"] = pd.to_datetime(df_t["date"])
    
    try:
        target_dt = pd.to_datetime(patch_date_str)
    except Exception:
        return False
    
    before_rows = df_t[df_t["date_dt"] <= target_dt]
    after_rows = df_t[df_t["date_dt"] > target_dt]
    
    if before_rows.empty or after_rows.empty:
        return False
        
    p_before = before_rows.iloc[-1]["close"]
    p_after = after_rows.iloc[0]["close"]
    
    if pd.isna(p_before) or pd.isna(p_after) or p_before == 0:
        return False
        
    r_raw = p_after / p_before
    r_adjusted = p_after / (p_before * multiplier)
    
    if abs(r_adjusted - 1.0) <= abs(r_raw - 1.0) - threshold:
        return True
    return False

# =====================================================================
# 🛠️ Raw & Active 2層同期およびアサーション検証エンジン
# =====================================================================

def check_processed_data_health(old_df: pd.DataFrame, new_df: pd.DataFrame) -> list:
    alerts = []
    if old_df is None or old_df.empty or new_df is None or new_df.empty:
        return alerts

    old_cnt = len(old_df)
    new_cnt = len(new_df)
    if new_cnt < old_cnt * 0.95:
        alerts.append(f"⚠️ [行数激減] 行数が減少しています: {old_cnt:,} ➔ {new_cnt:,} (減少率: {(1 - new_cnt/old_cnt)*100:.1f}%)")

    old_tickers = set(old_df["ticker"].unique())
    new_tickers = set(new_df["ticker"].unique())
    missing_tickers = old_tickers - new_tickers
    if missing_tickers:
        alerts.append(f"⚠️ [銘柄喪失] 以下の銘柄がデータから消失しています: {list(missing_tickers)[:10]}")

    common_tickers = old_tickers & new_tickers
    if common_tickers:
        sample_tickers = list(common_tickers)[:30]
        o_sample = old_df[old_df["ticker"].isin(sample_tickers)].sort_values(["ticker", "date"])
        n_sample = new_df[new_df["ticker"].isin(sample_tickers)].sort_values(["ticker", "date"])
        
        merged = pd.merge(
            o_sample[["date", "ticker", "close"]], 
            n_sample[["date", "ticker", "close"]], 
            on=["date", "ticker"], 
            suffixes=("_old", "_new")
        )
        if not merged.empty:
            merged["ratio"] = merged["close_new"] / merged["close_old"]
            crazy_changes = merged[(merged["ratio"] >= 10.0) | (merged["ratio"] <= 0.1)]
            if not crazy_changes.empty:
                sample_crazy = crazy_changes.iloc[0]
                alerts.append(
                    f"🚨 [異常価格変化] 加工前後の価格比率が異常です。 "
                    f"銘柄: {sample_crazy['ticker']}, 日付: {sample_crazy['date']}, "
                    f"旧Close: {sample_crazy['close_old']:.2f} ➔ 新Close: {sample_crazy['close_new']:.2f}"
                )
                
    return alerts

def adjust_ticker_splits_backward_in_memory(df_ticker: pd.DataFrame) -> pd.DataFrame:
    """
    メモリ上で配信された株式分割情報（stock splits）に基づき、
    実際の価格が未調整である場合（崖が残っている場合）のみ過去データを後方修正します。
    マーカー（stock splits値）が存在しない急落に対する自動比率推測補正は行いません。
    """
    if df_ticker.empty or len(df_ticker) < 2:
        return df_ticker
        
    df = df_ticker.sort_values("date").reset_index(drop=True)
    price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df.columns]
    
    if "stock splits" in df.columns:
        split_events = df[(df["stock splits"] > 0) & (df["stock splits"] != 1.0)]
        if not split_events.empty:
            for idx in sorted(split_events.index, reverse=True):
                if idx == 0:
                    continue  # 分割日の前日データ（idx-1）がない場合は比率判定ができないためスキップ
                
                split_val = df.loc[idx, "stock splits"]
                if split_val <= 0:
                    continue
                
                pre_close = df.loc[idx - 1, "close"]
                post_close = df.loc[idx, "close"]
                
                if pd.isna(pre_close) or pd.isna(post_close) or pre_close <= 0 or post_close <= 0:
                    continue
                
                # 分割前日と当日の実際の価格比率を算出
                actual_ratio = pre_close / post_close
                
                # 実際の価格落差が、配信された分割比率（split_val）に一定の許容範囲（15%以内）で近いかどうかを検証
                # 比率が近ければ「崖が残っている（未調整）」とみなし、明示されたsplit_valを用いて後方補正を適用
                if abs(actual_ratio - split_val) / split_val <= 0.15:
                    ratio = 1.0 / split_val
                    for col in price_cols:
                        df.loc[:idx-1, col] = df.loc[:idx-1, col] * ratio
                    if "volume" in df.columns:
                        df.loc[:idx-1, "volume"] = df.loc[:idx-1, "volume"] / ratio
                else:
                    # すでに調整済み、または比率が合致しない場合は二重調整防止のため補正は行わない
                    pass

    return df

def apply_saved_patches_to_df(df: pd.DataFrame, is_jp: bool = True) -> pd.DataFrame:
    try:
        from data_access.sheets_api import load_repair_log_from_sheets
        log_df = load_repair_log_from_sheets()
    except Exception:
        return df

    if log_df.empty:
        return df

    market_str = "JP" if is_jp else "US"
    df_result = df.copy()
    
    log_df["parsed_date"] = pd.to_datetime(log_df["cliff_date"], errors="coerce")
    log_df = log_df.dropna(subset=["parsed_date"]).sort_values("parsed_date", ascending=False)

    for _, row in log_df.iterrows():
        if str(row.get("market", "")).strip().upper() != market_str:
            continue
        ticker = str(row.get("ticker", "")).strip()
        cliff_date = row["parsed_date"]
        try:
            multiplier = float(row.get("multiplier", 1.0))
            if multiplier <= 0 or multiplier == 1.0:
                continue
        except ValueError:
            continue

        mask = df_result["ticker"] == ticker
        if not mask.any():
            continue

        ticker_data = df_result[mask].copy()
        if not check_anomaly_need_patch(ticker_data, cliff_date, multiplier):
            continue

        # cliff_date（ログ上の記録日付）は「要補正Closeの日時（t-1）」のため、この日を含めて（<=）適用する
        pre_mask = (df_result["ticker"] == ticker) & (pd.to_datetime(df_result["date"]) <= cliff_date)
        if pre_mask.any():
            price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df_result.columns]
            for col in price_cols:
                df_result.loc[pre_mask, col] = df_result.loc[pre_mask, col] * multiplier
            if "volume" in df_result.columns:
                df_result.loc[pre_mask, "volume"] = df_result.loc[pre_mask, "volume"] / multiplier

    return df_result

def propagate_stop_allocation_bars_in_memory(df_1d_active: pd.DataFrame, df_intra_active: pd.DataFrame, is_jp: bool = True) -> pd.DataFrame:
    if df_intra_active.empty:
        return df_intra_active

    stop_days_df = detect_allocation_stop_days(df_1d_active)
    if stop_days_df.empty:
        return df_intra_active

    df_intra = df_intra_active.copy()
    df_intra["date"] = pd.to_datetime(df_intra["date"])
    
    df_intra["date_only"] = df_intra["date"].dt.date
    limits_map = df_intra.groupby("ticker")["date_only"].agg(["min", "max"]).to_dict(orient="index")

    for _, row in stop_days_df.iterrows():
        ticker = row["ticker"]
        day_date = pd.Timestamp(row["date"]).date()
        
        if ticker in limits_map:
            t_min = limits_map[ticker]["min"]
            t_max = limits_map[ticker]["max"]
            if t_min <= day_date <= t_max:
                df_intra = _replace_stop_allocation_bar(
                    df_intra, ticker, row["date"], row["close"], row["volume"], is_jp=is_jp
                )

    df_intra = df_intra.drop(columns=["date_only"], errors="ignore")
    return df_intra

def rebuild_active_from_raw(interval: str, is_jp: bool = True, dry_run: bool = False, skip_assertion: bool = False, status_callback=None) -> bool:
    import gc  # ♻️ 内部で確実にインポート
    import pandas as pd
    
    def log(msg):
        print(msg)
        if status_callback: status_callback(msg)

    log(f"🏗️ [{interval}] RawデータからActiveデータの加工ビルドを開始します...")
    
    df_raw = load_price_db(interval, is_jp=is_jp, is_raw=True)
    if df_raw.empty:
        log("❌ Rawデータベースファイルが空、または検出されません。")
        return False

    # 1. 権利確定フラグの計算
    df_raw["is_finalized"] = compute_is_finalized(df_raw["date"], interval, is_jp=is_jp)

    # =========================================================================
    # 🔬 [株式分割データの詳細スキャン ＆ デバッグログ出力]
    # =========================================================================
    split_tickers = []
    has_splits_col = "stock splits" in df_raw.columns
    
    if has_splits_col:
        # nanを排除し、0や1.0以外の有意な分割値（例: 2.0や3.0）があるレコードをスキャン
        split_mask = (df_raw["stock splits"] > 0) & (df_raw["stock splits"] != 1.0) & (df_raw["stock splits"].notna())
        if split_mask.any():
            split_tickers = df_raw.loc[split_mask, "ticker"].unique().tolist()

    # スキャン結果の詳細をコンソールとStreamlit上に明確に出力
    log(f"  🔬 ----------------------------------------------------")
    log(f"  🔬 [株式分割・大容量検証デバッグログ] 時間足: {interval}")
    log(f"    * Rawデータ全体の行数: {len(df_raw):,} 行")
    log(f"    * 'stock splits' カラムがDBに存在するか: {has_splits_col}")
    
    if has_splits_col:
        valid_splits_cnt = df_raw["stock splits"].notna().sum()
        active_splits_mask = (df_raw["stock splits"] > 0) & (df_raw["stock splits"] != 1.0) & (df_raw["stock splits"].notna())
        active_splits_cnt = active_splits_mask.sum()
        log(f"    * 'stock splits' カラムに値（NaN以外）が入っている行数: {valid_splits_cnt:,} 行")
        log(f"    * 有意な分割イベント（0や1.0以外）が検出された行数: {active_splits_cnt:,} 行")
    
    log(f"    * 🔍 過去に実際に株式分割（崖調整）の対象となったユニーク銘柄数: {len(split_tickers)} 件 / 1,702銘柄中")
    
    if split_tickers:
        log(f"    * 調整対象となった銘柄（先頭15件のみ）: {split_tickers[:15]}")
        log(f"    * コピーを完全にスキップして素通しする銘柄数: {1702 - len(split_tickers)} 件")
    else:
        log(f"    * 📢 調整が必要な分割履歴はありません。全1,702銘柄を一切コピーせず素通しして保存処理へ流します。")
    log(f"  🔬 ----------------------------------------------------")

    # =========================================================================
    # ⚡ [メモリ超軽量化] 分割が発生した銘柄だけを切り出して処理
    # =========================================================================
    if split_tickers:
        # 分割が発生した銘柄と、不要な銘柄を分離
        df_splits = df_raw[df_raw["ticker"].isin(split_tickers)].copy()
        df_no_splits = df_raw[~df_raw["ticker"].isin(split_tickers)].copy()
        
        processed_parts = []
        for ticker, group in df_splits.groupby("ticker"):
            adjusted_group = adjust_ticker_splits_backward_in_memory(group)
            processed_parts.append(adjusted_group)
            
        df_processed_splits = pd.concat(processed_parts, ignore_index=True)
        
        # 結合
        df_processed = pd.concat([df_no_splits, df_processed_splits], ignore_index=True)
        
        # 不要になった中間データを即時破棄してガベージコレクションを実行
        del df_splits, df_no_splits, df_processed_splits, processed_parts
        gc.collect()
    else:
        df_processed = df_raw.copy()

    # 原本(df_raw)をメモリから削除
    del df_raw
    gc.collect()

    # =========================================================================
    # 以下、パッチ適用や保存などの通常処理
    # =========================================================================
    if not skip_assertion:
        df_processed = apply_saved_patches_to_df(df_processed, is_jp=is_jp)

    if interval != "1d":
        try:
            df_1d_active = load_price_db("1d", is_jp=is_jp, is_raw=False)
            df_processed = propagate_stop_allocation_bars_in_memory(df_1d_active, df_processed, is_jp=is_jp)
            del df_1d_active
            gc.collect()
        except Exception as e:
            log(f"⚠️ ストップ高安バーの自動移植はスキップされました: {e}")

    if not skip_assertion:
        try:
            df_old_active = load_price_db(interval, is_jp=is_jp, is_raw=False)
        except FileNotFoundError:
            df_old_active = pd.DataFrame()

        alerts = check_processed_data_health(df_old_active, df_processed)
        if alerts:
            log("💥 【警告】ビルド後の健康診断チェックで異常を検出しました:")
            for alert in alerts:
                log(f"   {alert}")
            if any("🚨" in a for a in alerts):
                log("🛑 深刻なデータ不整合を検出したため、破損防止のため同期を強制中断しました。")
                return False
            
        del df_old_active
        gc.collect()

    if dry_run:
        log(f"🧪 [DRY RUN] {interval} 加工・アサーション検証を正常に通過。")
        if settings.HAS_STREAMLIT:
            import streamlit as st
            st.session_state[f"temp_verified_active_df_{interval}"] = df_processed
            log(f"   💾 検証済みデータを一時メモリに格納しました。")
        return True
    else:
        df_processed = df_processed.sort_values(["ticker", "date"]).reset_index(drop=True)
        cloud_success, cloud_msg = save_price_db(df_processed, interval, is_jp=is_jp, is_raw=False)
        
        del df_processed
        gc.collect()
        
        if cloud_success:
            log(f"✅ [{interval}] ActiveデータベースをGoogleドライブへ正常に保存しました。")
        else:
            log(f"⚠️ 【重要警告】[{interval}] Googleドライブへの同期に失敗しました。")
        return True


def propagate_stop_allocation_bars_in_memory(df_1d_active: pd.DataFrame, df_intra_active: pd.DataFrame, is_jp: bool = True) -> pd.DataFrame:
    import gc  # ♻️ 内部で確実にインポート
    import pandas as pd
    
    if df_intra_active.empty:
        return df_intra_active

    stop_days_df = detect_allocation_stop_days(df_1d_active)
    if stop_days_df.empty:
        return df_intra_active

    # ストップ高安が発生した特定のティッカー（極少数）を抽出
    stop_tickers = stop_days_df["ticker"].unique().tolist()
    
    # 巨大データの中から、関係する銘柄のデータだけを切り出して置換処理（残りの99%は完全スルー）
    df_intra_target = df_intra_active[df_intra_active["ticker"].isin(stop_tickers)].copy()
    df_intra_safe = df_intra_active[~df_intra_active["ticker"].isin(stop_tickers)]

    if df_intra_target.empty:
        return df_intra_active

    df_intra_target["date"] = pd.to_datetime(df_intra_target["date"])
    
    # 対象データに対してのみループを実行
    for _, row in stop_days_df.iterrows():
        ticker = row["ticker"]
        day_date = pd.Timestamp(row["date"]).date()
        
        ticker_data = df_intra_target[df_intra_target["ticker"] == ticker]
        if ticker_data.empty:
            continue
            
        t_min = ticker_data["date"].min().date()
        t_max = ticker_data["date"].max().date()
        
        if t_min <= day_date <= t_max:
            df_intra_target = _replace_stop_allocation_bar(
                df_intra_target, ticker, row["date"], row["close"], row["volume"], is_jp=is_jp
            )

    df_result = pd.concat([df_intra_safe, df_intra_target], ignore_index=True)
    
    # テンポラリメモリの解放
    del df_intra_target, df_intra_safe
    gc.collect()
    
    return df_result

# =====================================================================
# 🧪 開発検証: 手動パッチのメモリ上適用シミュレーション
# =====================================================================

def test_forced_scale_patch_in_memory(ticker: str, patch_date_str: str, multiplier: float, is_jp: bool = True) -> tuple:
    """
    手動パッチ（patch_date / multiplier）を、実際のParquetを書き換えることなく
    すべてメモリ上でシミュレーション実行し、検証結果と仮の加工後DataFrameを返します。
    patch_date_str は「要補正Closeの日時（崖前日, t-1）」を指定し、この日を含めて（<=）過去へ一括適用します。
    """
    pure_ticker = sanitize_ticker(ticker, is_jp)
    results = {}
    st_temp_dfs = {}
    
    try:
        target_dt = pd.to_datetime(patch_date_str)
    except Exception as e:
        return {"error": f"要補正Close日時のパース失敗: {e}"}, {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            db_df = load_price_db(interval, is_jp=is_jp, is_raw=False)
        except FileNotFoundError:
            results[interval] = "DBなし"
            continue
        if db_df.empty:
            results[interval] = "データ空"
            continue

        mask = db_df["ticker"] == pure_ticker
        ticker_data = db_df[mask].copy()
        if ticker_data.empty:
            results[interval] = "対象データなし"
            continue

        need_apply = check_anomaly_need_patch(ticker_data, patch_date_str, multiplier)
        if not need_apply:
            results[interval] = "適用不要（既に調整済み、または落差がありません）"
            continue

        ticker_data["date"] = pd.to_datetime(ticker_data["date"])
        pre_mask = ticker_data["date"] <= target_dt
        if not pre_mask.any():
            results[interval] = "対象期間（崖日より過去）のデータなし"
            continue

        price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in db_df.columns]
        
        # プレビュー対比用の前後の3行サンプルを切り出し
        sample_before = ticker_data[pre_mask].tail(3).copy()
        
        # メモリ上で安全に適用
        for col in price_cols:
            ticker_data.loc[pre_mask, col] = ticker_data.loc[pre_mask, col] * multiplier
        if "volume" in db_df.columns:
            ticker_data.loc[pre_mask, "volume"] = ticker_data.loc[pre_mask, "volume"] / multiplier

        sample_after = ticker_data[pre_mask].tail(3).copy()
        
        # 既存DBから該当銘柄を一旦抜いて、メモリ上で再結合
        db_df_new = db_df[~mask].copy()
        
        if not db_df_new.empty:
            if pd.api.types.is_datetime64_any_dtype(db_df_new["date"]):
                ticker_data["date"] = pd.to_datetime(ticker_data["date"])
            else:
                ticker_data["date"] = ticker_data["date"].dt.strftime("%Y-%m-%d %H:%M:%S" if interval != "1d" else "%Y-%m-%d")
        
        db_df_new = pd.concat([db_df_new, ticker_data], ignore_index=True)
        db_df_new = db_df_new.sort_values(["ticker", "date"]).reset_index(drop=True)
        
        # 一時保持用辞書にセット
        st_temp_dfs[interval] = db_df_new
        
        results[interval] = {
            "applied_count": pre_mask.sum(),
            "before_sample": sample_before,
            "after_sample": sample_after
        }
        
    return results, st_temp_dfs

# =====================================================================
# 📥 Rawデータ更新 ＆ 統合同期システム
# =====================================================================

# core/database_service.py 内の update_raw_database 関数を差し替え

def update_raw_database(is_jp: bool = True, target_tickers: list = None, force_refetch: bool = False, status_callback=None):
    market_name = "JP" if is_jp else "US"
    tickers = target_tickers if target_tickers else []
    
    def log(msg):
        print(msg)
        if status_callback: status_callback(msg)
            
    if is_jp and not tickers:
        tickers = get_all_collection_tickers()
    if not tickers:
        log(f"[{market_name}] 更新対象銘柄がありません。")
        return

    now_tz, local_today = get_market_localized_now(is_jp)
    now = now_tz.replace(tzinfo=None)
    suffix = ".T" if is_jp else ""
    tickers = [sanitize_ticker(t, is_jp) for t in tickers]
    
    log(f"⚙️ [デバッグ] 同期対象銘柄数: {len(tickers)} 件 (例: {tickers[:10]}...)")

    for interval in settings.TIMEFRAMES:
        log(f"⏱️ 【{market_name}】{interval} Rawデータ差分収集開始...")
        try:
            df_raw_db = load_price_db(interval, is_jp=is_jp, is_raw=True)
            log(f"  🔍 [デバッグ] ローカルRaw DB読み込み成功: {len(df_raw_db):,} 行")
        except FileNotFoundError:
            df_raw_db = pd.DataFrame()
            log(f"  🔍 [デバッグ] ローカルRaw DBが見つかりません。新規作成します。")

        db_max_date = df_raw_db["date"].max() if not df_raw_db.empty else None
        if db_max_date is not None:
            if interval != "1d":
                limit_hour = 15 if is_jp else 16
                limit_time = datetime.strptime(f"{limit_hour}:00:00", "%H:%M:%S").time()
                if db_max_date.time() > limit_time:
                    db_max_date = db_max_date.replace(hour=limit_hour, minute=0, second=0, microsecond=0)
            
            bm_last_date = get_benchmark_latest_date(interval, is_jp=is_jp)
            log(f"  🔍 ベンチマーク最新: {bm_last_date} | Raw DB最新: {db_max_date}")
            if bm_last_date is not None:
                if bm_last_date <= db_max_date:
                    log(f"  ✨ 最新状態のため、差分ダウンロードはスキップします。")
                    continue

        last_updates_map = {}
        if not df_raw_db.empty:
            last_updates_map = df_raw_db.groupby("ticker")["date"].max().to_dict()
            
            # 🛡️ tickerカラムの型チェック情報をログ出力
            if last_updates_map:
                sample_key = list(last_updates_map.keys())[0]
                log(f"  🔍 [デバッグ型チェック] DB内のticker型: {type(sample_key)} (例: '{sample_key}') | 対象リストのticker型: {type(tickers[0])} (例: '{tickers[0]}')")

        # 照合件数を可視化
        matched_tickers = [t for t in tickers if t in last_updates_map]
        log(f"  🔍 [デバッグ] 同期対象 {len(tickers)} 件中、ローカルDBに合致した件数: {len(matched_tickers)} 件")

        active_timestamps = [pd.to_datetime(last_updates_map[t]) for t in tickers if t in last_updates_map]
        base_time = pd.Series(active_timestamps).mode()[0] if active_timestamps and not force_refetch else None
        log(f"  🔍 [デバッグ] 算出されたベース基準日 (base_time): {base_time}")

        if interval == "1m": max_delay = timedelta(hours=4)
        elif interval == "5m": max_delay = timedelta(hours=12)
        elif interval == "60m": max_delay = timedelta(days=2)
        else: max_delay = timedelta(days=10)
        future_tolerance = timedelta(minutes=5)

        group_A_tickers, group_B_tickers, group_C_tickers = [], [], []
        group_B_timestamps = []

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
                if -future_tolerance <= delay <= timedelta(0):
                    group_A_tickers.append(t)
                elif timedelta(0) < delay <= max_delay:
                    group_B_tickers.append(t)
                    group_B_timestamps.append(t_last_dt)
                else:
                    group_C_tickers.append(t)

        log(f"  📊 [デバッグ] グループ判定内訳: A (即時差分) = {len(group_A_tickers)} 件 | B (許容内遅延) = {len(group_B_tickers)} 件 | C (大幅遅延/新規) = {len(group_C_tickers)} 件")

        groups = {}
        if group_A_tickers:
            groups[base_time] = group_A_tickers
        if group_B_tickers and group_B_timestamps:
            oldest_b_time = min(group_B_timestamps)
            rounded_time = oldest_b_time.floor("30min") if interval in ["1m", "5m"] else oldest_b_time.floor("h") if interval == "60m" else oldest_b_time.floor("D")
            groups[rounded_time] = group_B_tickers

        for t in group_C_tickers:
            t_last = last_updates_map.get(t)
            t_key = pd.to_datetime(t_last) if t_last is not None else None
            groups.setdefault(t_key, []).append(t)

        # 取得計画の内訳を出力
        for key_dt, chunk_list in groups.items():
            key_str = key_dt.strftime("%Y-%m-%d %H:%M:%S") if key_dt is not None else "None (全期間新規取得 / 2016〜)"
            log(f"  📥 [デバッグ] 取得スケジュール: 開始日={key_str} ➔ 対象={len(chunk_list)} 銘柄 (例: {chunk_list[:5]}...)")

        all_downloaded = []
        for t_last, chunk_tickers in groups.items():
            if t_last is None:
                if interval == "1m": start_date_dt = now - timedelta(days=6)
                elif interval == "5m": start_date_dt = now - timedelta(days=58)
                elif interval == "60m": start_date_dt = now - timedelta(days=718)
                else: start_date_dt = datetime(2016, 1, 1)
                start_date_str = start_date_dt.strftime("%Y-%m-%d")
            else:
                start_date_dt = t_last
                if interval != "1d" and (now - t_last).total_seconds() < 120:
                    log(f"    ⏭️ [デバッグ] {interval} 最終取得から120秒未満のためダウンロードをスキップします。")
                    continue
                start_date_str = start_date_dt.strftime("%Y-%m-%d")

            if interval in YFINANCE_GAP_LIMITS:
                limit_days = YFINANCE_GAP_LIMITS[interval]
                try:
                    gap_start_date = start_date_dt.date() if hasattr(start_date_dt, "date") else None
                except Exception:
                    gap_start_date = None

                if gap_start_date is not None:
                    gap_days = (local_today - gap_start_date).days
                    if gap_days > limit_days:
                        sample_tickers = ", ".join(chunk_tickers[:5]) + ("..." if len(chunk_tickers) > 5 else "")
                        log(
                            f"⚠️【警告】[{interval}] {sample_tickers} の空白期間が {gap_days} 日となり、"
                            f"yfinanceの上限（{limit_days}日）を超えたため差分同期できません。手動リビルドを行ってください。"
                        )
                        continue

            BATCH_SIZE = 100
            total_batches = (len(chunk_tickers) + BATCH_SIZE - 1) // BATCH_SIZE
            log(f"  🚀 [デバッグ] 開始日 '{start_date_str}' からのダウンロードを開始します (全 {len(chunk_tickers)} 件, {total_batches} バッチ)...")

            for i in range(0, len(chunk_tickers), BATCH_SIZE):
                chunk = chunk_tickers[i:i+BATCH_SIZE]
                symbols = [f"{t}{suffix}" for t in chunk]
                batch_num = (i // BATCH_SIZE) + 1
                log(f"    📡 バッチ {batch_num}/{total_batches}: {len(chunk)} 銘柄ダウンロード中 (例: {chunk[:3]}...)")
                
                try:
                    df_raw = yf.download(
                        symbols, 
                        start=start_date_str,
                        interval=interval, 
                        auto_adjust=False, 
                        actions=True, 
                        progress=False, 
                        threads=True, 
                        timeout=30
                    )
                    log(f"      📥 生データダウンロード完了: {df_raw.shape}")
                    
                    chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
                    log(f"      ✨ パース後レコード数: {len(chunk_processed):,} 行")
                    if not chunk_processed.empty:
                        all_downloaded.append(chunk_processed)
                except Exception as e:
                    log(f"     Batch Error: {e}")
                time.sleep(1)

        if all_downloaded:
            new_combined = pd.concat(all_downloaded, ignore_index=True)
            log(f"  📊 [デバッグ] 今回新規追加されたデータの合計: {len(new_combined):,} 行")
            if not df_raw_db.empty:
                df_raw_db = pd.concat([df_raw_db, new_combined], ignore_index=True)
                before_drop = len(df_raw_db)
                df_raw_db = df_raw_db.drop_duplicates(subset=["date", "ticker"], keep="last")
                after_drop = len(df_raw_db)
                log(f"  🧹 [デバッグ] 重複排除前: {before_drop:,} 行 ➔ 重複排除後: {after_drop:,} 行 (重複による削除: {before_drop - after_drop:,} 行)")
            else:
                df_raw_db = new_combined
            
            df_raw_db = df_raw_db.sort_values(["ticker", "date"]).reset_index(drop=True)
            cloud_success, cloud_msg = save_price_db(df_raw_db, interval, is_jp=is_jp, is_raw=True)
            if cloud_success:
                log(f"  📥 Rawデータ差分保存完了。({len(new_combined):,}件追加)")
            else:
                log(f"  ⚠️ [Raw保存警告] Googleドライブへの同期に失敗しました（ローカルのみ）。エラー: {cloud_msg}")
        else:
            log(f"  🧊 yfinanceからの差分データはありません。")

def update_price_database(is_jp: bool = True, target_tickers: list = None, force_refetch: bool = False, status_callback=None, dry_run: bool = False):
    def log(msg):
        print(msg)
        if status_callback: status_callback(msg)

    log("📡 1. yfinanceからのRawデータ差分取得を開始します...")
    update_raw_database(is_jp=is_jp, target_tickers=target_tickers, force_refetch=force_refetch, status_callback=status_callback)

    log("🛠️ 2. RawデータからActiveデータベース一括加工・検証ビルドを開始します...")
    for interval in settings.TIMEFRAMES:
        rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=dry_run, skip_assertion=False, status_callback=status_callback)

# =====================================================================
# 💥 クリーンビルド（RawもActiveも完全にダウンロードし直す）
# =====================================================================

def full_rebuild_all_database(is_jp: bool = True, interval: str = "1d", status_callback=None, dry_run: bool = False) -> bool:
    """
    既存のRawおよびActiveデータベースを完全に物理削除し、yfinanceの提供限界から一発でクリーンビルドし直します。
    (日本株のフル再構築時には、スプレッドシートの追加ETF定義を事前に強制同期して最新化します)
    """
    def log(msg):
        print(msg)
        if status_callback: status_callback(msg)

    market_name = "JP" if is_jp else "US"
    if is_jp:
        # ★【追加】 一括再ダウンロードの直前にスプレッドシートから最新の追加ETFマスタを同期・キャッシュ化
        try:
            from core.collector import sync_extra_tickers_to_local
            sync_extra_tickers_to_local()
            log("🔄 Google Sheetsから最新の追加収集ETFマスタを取得し、同期しました。")
        except Exception as e:
            log(f"⚠️ 追加収集ETFマスタの同期に失敗したため、既存のローカルキャッシュを使用します: {e}")
            
        tickers = get_all_collection_tickers()
    else:
        tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
    
    if not tickers:
        log("❌ [フル再構築] 銘柄が検出されません。")
        return False
        
    tickers = [sanitize_ticker(t, is_jp) for t in tickers]
    suffix = ".T" if is_jp else ""
    now = datetime.now()
    
    if interval == "1m": start_date_dt = now - timedelta(days=6)
    elif interval == "5m": start_date_dt = now - timedelta(days=58)
    elif interval == "60m": start_date_dt = now - timedelta(days=718)
    else: start_date_dt = datetime(2016, 1, 1)
        
    log(f"🚨 [フル再構築] {market_name} ({interval}) Rawデータダウンロード開始。総数: {len(tickers)}")
    
    all_downloaded = []
    BATCH_SIZE = 30
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i+BATCH_SIZE]
        symbols = [f"{t}{suffix}" for t in chunk]
        log(f"  📥 ダウンロード中 ({i + 1}〜{min(i + BATCH_SIZE, len(tickers))}): {', '.join(chunk[:5])}...")
        
        try:
            df_raw = yf.download(
                symbols,
                start=start_date_dt.strftime("%Y-%m-%d"),
                interval=interval,
                auto_adjust=False,
                actions=True,
                progress=False,
                threads=True,
                timeout=30
            )
            chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
            if not chunk_processed.empty:
                all_downloaded.append(chunk_processed)
        except Exception as e:
            log(f"    -> ⚠️ エラー: {e}")
        time.sleep(1.5)
        
    if all_downloaded:
        final_df = pd.concat(all_downloaded, ignore_index=True)
        final_df = final_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        
        cloud_success, cloud_msg = save_price_db(final_df, interval, is_jp=is_jp, is_raw=True)
        if cloud_success:
            log("📥 Rawデータベースのフル構築に成功しました。続いてActiveデータのビルドと検証に入ります。")
        else:
            log(f"⚠️ [Raw保存警告] RawデータのGoogleドライブ同期に失敗しました。エラー: {cloud_msg}")
        
        return rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=dry_run, skip_assertion=True, status_callback=status_callback)
    
    return False

# =====================================================================
# 🩹 手動修復と一括パッチ適用
# =====================================================================

def apply_forced_scale_patch_to_all_timeframes(ticker: str, patch_date: str, multiplier: float, is_jp: bool = True) -> dict:
    """
    patch_date は「要補正Closeの日時（崖前日, t-1）」を指定します。
    この日を含めて（<=）過去のデータすべてに multiplier を一括適用します。
    """
    if multiplier <= 0:
        return {"error": f"処理を中断しました。倍率に 0 以下の数値（{multiplier}）は指定できません。"}

    pure_ticker = sanitize_ticker(ticker, is_jp)
    results = {}
    try:
        target_dt = pd.to_datetime(patch_date)
    except Exception as e:
        return {"error": f"要補正Close日時のパース失敗: {e}"}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            db_df = load_price_db(interval, is_jp=is_jp, is_raw=False)
        except FileNotFoundError:
            results[interval] = "DBなし"
            continue
        if db_df.empty:
            results[interval] = "データ空"
            continue

        mask = db_df["ticker"] == pure_ticker
        ticker_data = db_df[mask].copy()
        if ticker_data.empty:
            results[interval] = "対象データなし"
            continue

        need_apply = check_anomaly_need_patch(ticker_data, patch_date, multiplier)
        if not need_apply:
            results[interval] = "スキップ（既に調整済み、または適用不要な落差です）"
            continue

        ticker_data["date"] = pd.to_datetime(ticker_data["date"])
        pre_mask = ticker_data["date"] <= target_dt
        
        if not pre_mask.any():
            results[interval] = "対象期間（崖日より過去）のデータなし"
            continue

        price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in db_df.columns]
        for col in price_cols:
            ticker_data.loc[pre_mask, col] = ticker_data.loc[pre_mask, col] * multiplier
        if "volume" in db_df.columns:
            ticker_data.loc[pre_mask, "volume"] = ticker_data.loc[pre_mask, "volume"] / multiplier

        db_df = db_df[~mask]
        if not db_df.empty:
            if pd.api.types.is_datetime64_any_dtype(db_df["date"]):
                ticker_data["date"] = pd.to_datetime(ticker_data["date"])
            else:
                ticker_data["date"] = ticker_data["date"].dt.strftime("%Y-%m-%d %H:%M:%S" if interval != "1d" else "%Y-%m-%d")
        
        db_df = pd.concat([db_df, ticker_data], ignore_index=True)
        db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        save_price_db(db_df, interval, is_jp=is_jp, is_raw=False)
        results[interval] = f"{pre_mask.sum()}件補正適用完了"
    return results

def apply_all_saved_patches(is_jp: bool = True, status_callback=None) -> int:
    def log(msg):
        print(msg)
        if status_callback: status_callback(msg)

    log("🛠️ [パッチ復元] 保存されたパッチ定義に基づいてActiveデータベースを安全に再構築します...")
    
    success_count = 0
    for interval in settings.TIMEFRAMES:
        success = rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=False, skip_assertion=False, status_callback=status_callback)
        if success:
            success_count += 1
            log(f"  👉 [{interval}] のパッチ適用およびActiveの再生成が正常に完了しました。")
            
    return success_count

def repair_single_ticker_all_timeframes(ticker: str, is_jp: bool = True, forced_split_ratio: float = None) -> dict:
    pure_ticker = sanitize_ticker(ticker, is_jp)
    symbol = get_download_symbol(pure_ticker, is_jp)
    now = datetime.now()
    results = {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            try:
                raw_db = load_price_db(interval, is_jp=is_jp, is_raw=True)
            except FileNotFoundError:
                raw_db = pd.DataFrame()

            old_raw = raw_db[raw_db["ticker"] == pure_ticker].copy() if not raw_db.empty else pd.DataFrame()
            if interval == "1m": start_date_dt = now - timedelta(days=6)
            elif interval == "5m": start_date_dt = now - timedelta(days=58)
            elif interval == "60m": start_date_dt = now - timedelta(days=718)
            else: start_date_dt = datetime(2016, 1, 1)

            df_raw = yf.download(symbol, start=start_date_dt.strftime("%Y-%m-%d"), interval=interval, auto_adjust=False, actions=True, progress=False)
            if df_raw.empty:
                results[interval] = "新規データ空（置換なし）"
                continue
            new_df = parse_yfinance_batch(df_raw, [pure_ticker], is_jp=is_jp)
            if new_df.empty:
                results[interval] = "パース結果空（置換なし）"
                continue

            if not old_raw.empty:
                new_dates = pd.to_datetime(new_df["date"])
                old_raw["date_dt"] = pd.to_datetime(old_raw["date"])
                old_filtered = old_raw[~old_raw["date_dt"].isin(new_dates)].copy()
                if "date_dt" in old_filtered.columns:
                    old_filtered = old_filtered.drop(columns=["date_dt"])
                merged_raw = pd.concat([old_filtered, new_df], ignore_index=True)
            else:
                merged_raw = new_df

            if not raw_db.empty:
                raw_db = raw_db[raw_db["ticker"] != pure_ticker]
            raw_db = pd.concat([raw_db, merged_raw], ignore_index=True)
            raw_db = raw_db.sort_values(["ticker", "date"]).reset_index(drop=True)
            
            save_price_db(raw_db, interval, is_jp=is_jp, is_raw=True)
            
            rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=False)
            results[interval] = f"個別ダウンロード・Active再生成成功 ({len(merged_raw):,}件)"
        except Exception as e:
            results[interval] = f"エラー: {str(e)}"

    return results

def delete_data_before_date(ticker: str, limit_date_str: str, is_jp: bool = True) -> dict:
    pure_ticker = sanitize_ticker(ticker, is_jp)
    limit_dt = pd.to_datetime(limit_date_str)
    results = {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            df_raw = load_price_db(interval, is_jp=is_jp, is_raw=True)
            if not df_raw.empty:
                df_raw["temp_date"] = pd.to_datetime(df_raw["date"])
                mask_to_delete = (df_raw["ticker"] == pure_ticker) & (df_raw["temp_date"] <= limit_dt)
                deleted_count = mask_to_delete.sum()
                if deleted_count > 0:
                    df_raw = df_raw[~mask_to_delete].copy()
                    df_raw = df_raw.drop(columns=["temp_date"])
                    df_raw = df_raw.sort_values(["ticker", "date"]).reset_index(drop=True)
                    save_price_db(df_raw, interval, is_jp=is_jp, is_raw=True)
                    
                    rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=False)
                    results[interval] = f"Raw/Activeから {deleted_count:,} 件を正常物理削除"
                else:
                    results[interval] = "該当データなし"
            else:
                results[interval] = "RawDB空"
        except FileNotFoundError:
            results[interval] = "DBファイルなし"
        except Exception as e:
            results[interval] = f"エラー: {str(e)}"
            
    return results

# =====================================================================
# 🔍 診断関連関数群
# =====================================================================

def repair_stop_allocation_bars_full(is_jp: bool = True, status_callback=None) -> dict:
    def log(msg):
        print(msg)
        if status_callback: status_callback(msg)

    log("📡 ストップ高安バーの修復処理として、Activeデータベースのリビルドを開始します...")
    results = {}
    for interval in ["60m", "5m", "1m"]:
        success = rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=False, skip_assertion=False, status_callback=status_callback)
        if success:
            results[interval] = 1 
    return results

def run_database_health_scan(is_jp: bool) -> list:
    anomalies = []
    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            df = load_price_db(interval, is_jp=is_jp, is_raw=False) 
            if df.empty:
                continue
            df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
            
            cols_to_check = ["close"]
            has_adj = "adj close" in df.columns
            if has_adj:
                cols_to_check.append("adj close")
                df["pct_adj_close"] = df.groupby("ticker")["adj close"].pct_change()

            df["pct_close"] = df.groupby("ticker")["close"].pct_change()

            for p_col in cols_to_check:
                col_label = " (Adj Close)" if p_col == "adj close" else ""
                pct_col = "pct_adj_close" if p_col == "adj close" else "pct_close"
                
                anomaly_indices = df[(df[pct_col] <= -0.40) | (df[pct_col] >= 0.50)].index.tolist()
                for i in anomaly_indices:
                    row = df.loc[i]
                    ticker = row["ticker"]
                    curr_p = row[p_col]
                    pct_val = row[pct_col]
                    
                    ticker_df = df[(df["ticker"] == ticker) & (df.index >= i)].copy()
                    dates = ticker_df["date"].tolist()
                    close_vals = ticker_df[p_col].tolist()
                    n = len(ticker_df)
                    if n < 2:
                        continue
                        
                    curr_close = row["close"]
                    curr_adj = row["adj close"] if has_adj else float("nan")
                    
                    pre_close = curr_close / (1.0 + row["pct_close"])
                    pre_adj = curr_adj / (1.0 + row["pct_adj_close"]) if has_adj else float("nan")
                    
                    if pct_val <= -0.40:
                        found_recovery = False
                        recovery_idx = -1
                        for j in range(1, n):
                            post_p = close_vals[j]
                            if (pre_p := curr_p / (1.0 + pct_val)) * 0.85 <= post_p <= pre_p * 1.15:
                                found_recovery = True
                                recovery_idx = j
                                break
                        
                        post_close = ticker_df["close"].iloc[recovery_idx] if found_recovery else float("nan")
                        post_adj = ticker_df["adj close"].iloc[recovery_idx] if (found_recovery and has_adj) else float("nan")

                        if found_recovery:
                            bug_end_date = dates[recovery_idx - 1]
                            price_msg = f"Close: {pre_close:.1f} ➔ {curr_close:.1f} ➔ {post_close:.1f}"
                            if has_adj:
                                price_msg += f" | Adj: {pre_adj:.1f} ➔ {curr_adj:.1f} ➔ {post_adj:.1f}"
                                
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"🚨 クレーターバグ{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 {str(bug_end_date)[:16]}",
                                "異常値": f"Close: {curr_close:.1f}" + (f" / Adj: {curr_adj:.1f}" if has_adj else ""),
                                "前後価格": price_msg
                            })
                        else:
                            price_msg = f"Close: {pre_close:.1f} ➔ {curr_close:.1f}"
                            if has_adj:
                                price_msg += f" | Adj: {pre_adj:.1f} ➔ {curr_adj:.1f}"
                                
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"📉 階段段差（未調整分割）{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 最新",
                                "異常値": f"Close: {curr_close:.1f}" + (f" / Adj: {curr_adj:.1f}" if has_adj else ""),
                                "前後価格": price_msg
                            })
                    elif pct_val >= 0.50:
                        found_recovery = False
                        recovery_idx = -1
                        for j in range(1, n):
                            post_p = close_vals[j]
                            if (pre_p := curr_p / (1.0 + pct_val)) * 0.85 <= post_p <= pre_p * 1.15:
                                found_recovery = True
                                recovery_idx = j
                                break
                        
                        post_close = ticker_df["close"].iloc[recovery_idx] if found_recovery else float("nan")
                        post_adj = ticker_df["adj close"].iloc[recovery_idx] if (found_recovery and has_adj) else float("nan")

                        if found_recovery:
                            bug_end_date = dates[recovery_idx - 1]
                            price_msg = f"Close: {pre_close:.1f} ➔ {curr_close:.1f} ➔ {post_close:.1f}"
                            if has_adj:
                                price_msg += f" | Adj: {pre_adj:.1f} ➔ {curr_adj:.1f} ➔ {post_adj:.1f}"
                                
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"📈 タワーバグ{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 {str(bug_end_date)[:16]}",
                                "異常値": f"Close: {curr_close:.1f}" + (f" / Adj: {curr_adj:.1f}" if has_adj else ""),
                                "前後価格": price_msg
                            })
                        else:
                            price_msg = f"Close: {pre_close:.1f} ➔ {curr_close:.1f}"
                            if has_adj:
                                price_msg += f" | Adj: {pre_adj:.1f} ➔ {curr_adj:.1f}"
                                
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"📈 階段段差（未調整併合）{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 最新",
                                "異常値": f"Close: {curr_close:.1f}" + (f" / Adj: {curr_adj:.1f}" if has_adj else ""),
                                "前後価格": price_msg
                            })
        except Exception:
            pass
    return anomalies

def scan_all_anomalies(is_jp: bool = True, interval: str = "1d", threshold: float = 0.35) -> pd.DataFrame:
    try:
        db_df = load_price_db(interval, is_jp=is_jp, is_raw=False) 
    except FileNotFoundError:
        return pd.DataFrame()
    if db_df.empty:
        return pd.DataFrame()

    db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
    has_adj = "adj close" in db_df.columns
    result_rows = []

    negative_mask = db_df["close"] < 0
    shifted_neg_mask_for_pos = db_df.groupby("ticker")["close"].apply(lambda x: (x < 0).shift(1, fill_value=True)).reset_index(level=0, drop=True)
    pos_to_neg = negative_mask & (~shifted_neg_mask_for_pos)
    shifted_neg_mask_for_neg = df_neg_shift = db_df.groupby("ticker")["close"].apply(lambda x: (x < 0).shift(1, fill_value=False)).reset_index(level=0, drop=True)
    neg_to_pos = (~negative_mask) & shifted_neg_mask_for_neg
    boundary_mask = pos_to_neg | neg_to_pos
    
    if boundary_mask.any():
        boundary_rows = db_df[boundary_mask].copy()
        boundary_rows["before_date"] = db_df.groupby("ticker")["date"].shift(1)[boundary_mask].values
        boundary_rows["before_close"] = db_df.groupby("ticker")["close"].shift(1)[boundary_mask].values
        boundary_rows["after_close"] = boundary_rows["close"]
        if has_adj:
            boundary_rows["before_adj_close"] = db_df.groupby("ticker")["adj close"].shift(1)[boundary_mask].values
            boundary_rows["after_adj_close"] = boundary_rows["adj close"]
        else:
            boundary_rows["before_adj_close"] = float("nan")
            boundary_rows["after_adj_close"] = float("nan")

        boundary_rows["pct_change"] = float("nan")
        for col in ["open", "high", "low", "volume"]:
            if col not in boundary_rows.columns:
                boundary_rows[col] = float("nan")
        result_rows.append(boundary_rows[["ticker", "date", "before_date", "before_close", "after_close", "before_adj_close", "after_adj_close", "open", "high", "low", "volume", "pct_change"]])

    abs_close = db_df["close"].abs()
    pct = abs_close.groupby(db_df["ticker"]).pct_change()
    cliff_mask = pct.abs() >= threshold

    if cliff_mask.any():
        cliff_rows = db_df[cliff_mask].copy()
        cliff_rows["before_date"] = db_df.groupby("ticker")["date"].shift(1)[cliff_mask].values
        cliff_rows["before_close"] = db_df.groupby("ticker")["close"].shift(1)[cliff_mask].values
        cliff_rows["after_close"] = cliff_rows["close"]
        if has_adj:
            cliff_rows["before_adj_close"] = db_df.groupby("ticker")["adj close"].shift(1)[cliff_mask].values
            cliff_rows["after_adj_close"] = cliff_rows["adj close"]
        else:
            cliff_rows["before_adj_close"] = float("nan")
            cliff_rows["after_adj_close"] = float("nan")

        cliff_rows["pct_change"] = pct[cliff_mask].values
        for col in ["open", "high", "low", "volume"]:
            if col not in cliff_rows.columns:
                cliff_rows[col] = float("nan")
        result_rows.append(cliff_rows[["ticker", "date", "before_date", "before_close", "after_close", "before_adj_close", "after_adj_close", "open", "high", "low", "volume", "pct_change"]])

    if not result_rows:
        return pd.DataFrame()
        
    result = pd.concat(result_rows, ignore_index=True).rename(columns={"date": "cliff_date"})
    
    def aggregate_anomalies(group):
        pct_vals = group["pct_change"].dropna()
        pct_val = pct_vals.iloc[0] if not pct_vals.empty else float("nan")
        
        before_date_val = group["before_date"].dropna().iloc[0] if not group["before_date"].dropna().empty else pd.NaT
        before_close_val = group["before_close"].dropna().iloc[0] if not group["before_close"].dropna().empty else float("nan")
        after_close_val = group["after_close"].dropna().iloc[0] if not group["after_close"].dropna().empty else float("nan")
        before_adj_val = group["before_adj_close"].dropna().iloc[0] if not group["before_adj_close"].dropna().empty else float("nan")
        after_adj_val = group["after_adj_close"].dropna().iloc[0] if not group["after_adj_close"].dropna().empty else float("nan")
        
        open_val = group["open"].dropna().iloc[0] if not group["open"].dropna().empty else float("nan")
        high_val = group["high"].dropna().iloc[0] if not group["high"].dropna().empty else float("nan")
        low_val = group["low"].dropna().iloc[0] if not group["low"].dropna().empty else float("nan")
        vol_val = group["volume"].dropna().iloc[0] if not group["volume"].dropna().empty else float("nan")
        
        est_multiplier = float("nan")
        if before_close_val != 0 and pd.notna(before_close_val) and pd.notna(after_close_val):
            est_multiplier = after_close_val / before_close_val
            
        return pd.Series({
            "before_date": before_date_val,
            "before_close": before_close_val, 
            "after_close": after_close_val, 
            "before_adj_close": before_adj_val, 
            "after_adj_close": after_adj_val, 
            "open": open_val,
            "high": high_val,
            "low": low_val,
            "volume": vol_val,
            "est_multiplier": est_multiplier,
            "pct_change": pct_val
        })
        
    result = result.groupby(["ticker", "cliff_date"], as_index=False).apply(aggregate_anomalies)
    # patch_date：実際にパッチ適用時の基準日として使用する「要補正Closeの日時（崖前日, t-1）」
    result["patch_date"] = pd.to_datetime(result["before_date"])
    return result.sort_values(["ticker", "cliff_date"]).reset_index(drop=True)

# =====================================================================
# 🌐 TradingView 照合付き統合スキャン ＆ 一括自動修復エンジン
# =====================================================================

_TV_CLIENT_FALLBACK = None  # Streamlit実行環境外（バッチ処理等）での簡易シングルトン用フォールバック

def _create_tv_client_instance():
    """tvDatafeedクライアントの実インスタンス生成処理（ログイン試行はここでのみ発生）。"""
    try:
        from tvDatafeed import TvDatafeed
        return TvDatafeed()
    except Exception:
        return False

if settings.HAS_STREAMLIT:
    import streamlit as st

    @st.cache_resource(show_spinner=False)
    def _get_tv_client_cached():
        """
        Streamlitの st.cache_resource でTradingView接続オブジェクトをキャッシュ化（シングルトン化）します。
        画面操作のたびにスクリプトが再実行（リラン）されても、ここで一度確立した接続は
        プロセス内で使い回されるため、ログイン試行過多によるアカウント一時ロックや
        読み込み遅延を防止します。
        """
        return _create_tv_client_instance()

def _get_tv_client():
    """
    tvdatafeed クライアントを取得します（匿名・遅延データ取得）。
    Streamlit環境では @st.cache_resource によりキャッシュされたインスタンスを再利用し、
    それ以外（バッチスクリプト等）ではモジュールレベルの簡易シングルトンで代用します。
    """
    if settings.HAS_STREAMLIT:
        return _get_tv_client_cached()

    global _TV_CLIENT_FALLBACK
    if _TV_CLIENT_FALLBACK is None:
        _TV_CLIENT_FALLBACK = _create_tv_client_instance()
    return _TV_CLIENT_FALLBACK

# --- yfinance ⇔ TradingView シンボル表現の対応マッピング ---
# yfinance側で使われる特殊表記（ハイフン区切りの種類株、指数の^プレフィックスなど）を
# TradingView側の表記・取引所へ変換するためのルール。
JP_INDEX_TICKER_TV_MAP = {
    "^N225": {"symbol": "NI225", "exchange": "TVC"},
    "1306.T": {"symbol": "1306", "exchange": "TSE"},
}
US_INDEX_TICKER_TV_MAP = {
    "^GSPC": {"symbol": "SPX", "exchange": "TVC"},
    "^NDX": {"symbol": "NDX", "exchange": "NASDAQ"},
    "^DJI": {"symbol": "DJI", "exchange": "TVC"},
}
# yfinanceのハイフン区切り種類株（例: BRK-B, BF-B）はTradingViewではドット区切り（BRK.B）
US_SHARE_CLASS_HYPHEN_PATTERN = True  # 下の関数内でハイフン→ドット変換として一律処理

def map_ticker_to_tv_symbol(ticker: str, is_jp: bool = True) -> dict:
    """
    yfinance表記のティッカーを、TradingView(tvdatafeed)が要求する
    {"symbol":..., "exchange":...} 形式へ変換します。
    個別の対応関係が判明していない銘柄は、フォールバックの推測ルールを適用します。
    """
    raw_ticker = str(ticker).strip()

    # 1. 指数・ベンチマーク（^プレフィックス）の個別マッピング
    index_map = JP_INDEX_TICKER_TV_MAP if is_jp else US_INDEX_TICKER_TV_MAP
    if raw_ticker in index_map:
        return index_map[raw_ticker]

    pure_ticker = sanitize_ticker(raw_ticker, is_jp)

    if is_jp:
        # 日本株・日本ETF: 数字コードはyfinance/TradingViewでほぼ共通表記のため、TSE固定でそのまま利用
        return {"symbol": pure_ticker, "exchange": "TSE"}

    # 2. 米国株の種類株表記変換: yfinanceの "BRK-B" 形式 → TradingViewの "BRK.B" 形式
    tv_symbol = pure_ticker.replace("-", ".") if "-" in pure_ticker else pure_ticker

    return {"symbol": tv_symbol, "exchange": None}  # exchangeはNone＝候補を順に試す

def fetch_tv_close_price(ticker: str, cliff_date, is_jp: bool = True):
    """
    TradingViewの非公式API（tvdatafeed）を用いて、指定銘柄・指定日の
    正しい終値（Close）をピンポイントで取得します。取得できない場合は None を返します。
    """
    tv = _get_tv_client()
    if not tv:
        return None

    try:
        from tvDatafeed import Interval as TvInterval
    except Exception:
        return None

    try:
        target_date = pd.Timestamp(cliff_date).normalize()
    except Exception:
        return None

    mapped = map_ticker_to_tv_symbol(ticker, is_jp)
    symbol = mapped["symbol"]
    fixed_exchange = mapped.get("exchange")
    exchange_candidates = [fixed_exchange] if fixed_exchange else ["NASDAQ", "NYSE", "AMEX"]
    days_back = int(max((pd.Timestamp.now().normalize() - target_date).days + 30, 60))

    for exchange in exchange_candidates:
        try:
            hist = tv.get_hist(symbol=symbol, exchange=exchange, interval=TvInterval.in_daily, n_bars=days_back)
        except Exception:
            continue
        if hist is None or hist.empty:
            continue
        hist = hist.copy()
        hist.index = pd.to_datetime(hist.index).normalize()
        if target_date in hist.index:
            try:
                return float(hist.loc[target_date, "close"])
            except Exception:
                continue
    return None

def fetch_tv_close_pair(ticker: str, patch_date, is_jp: bool = True) -> dict:
    """
    TradingViewの非公式API（tvdatafeed）を用いて、
    「要補正Closeの日時（patch_date, t-1）」の終値と、その1本後（変化当日, t）の終値を
    自動オフセット処理でまとめて取得します。
    patch_dateの指定だけで、システム内部が自動的にt本後の終値もピンポイント取得しに行くため、
    呼び出し側は崖前後の2点を個別に意識する必要がありません。
    戻り値: {"tv_before_close": float|None, "tv_after_close": float|None}
    """
    empty = {"tv_before_close": None, "tv_after_close": None}
    tv = _get_tv_client()
    if not tv:
        return empty

    try:
        from tvDatafeed import Interval as TvInterval
    except Exception:
        return empty

    try:
        before_date = pd.Timestamp(patch_date).normalize()
    except Exception:
        return empty

    mapped = map_ticker_to_tv_symbol(ticker, is_jp)
    symbol = mapped["symbol"]
    fixed_exchange = mapped.get("exchange")
    exchange_candidates = [fixed_exchange] if fixed_exchange else ["NASDAQ", "NYSE", "AMEX"]
    days_back = int(max((pd.Timestamp.now().normalize() - before_date).days + 30, 60))

    for exchange in exchange_candidates:
        try:
            hist = tv.get_hist(symbol=symbol, exchange=exchange, interval=TvInterval.in_daily, n_bars=days_back)
        except Exception:
            continue
        if hist is None or hist.empty:
            continue
        hist = hist.copy()
        hist.index = pd.to_datetime(hist.index).normalize()
        hist = hist.sort_index()

        tv_before_close = None
        if before_date in hist.index:
            try:
                tv_before_close = float(hist.loc[before_date, "close"])
            except Exception:
                tv_before_close = None

        # 自動オフセット処理：要補正日（t-1）の「1本後」＝変化当日（t）以降で最初のバーを取得
        tv_after_close = None
        after_candidates = hist[hist.index > before_date]
        if not after_candidates.empty:
            try:
                tv_after_close = float(after_candidates.iloc[0]["close"])
            except Exception:
                tv_after_close = None

        if tv_before_close is not None or tv_after_close is not None:
            return {"tv_before_close": tv_before_close, "tv_after_close": tv_after_close}

    return empty

def scan_and_diagnose_cliffs_with_tv(is_jp: bool = True, intervals: list = None) -> pd.DataFrame:
    """
    「段差（Cliff）検出」と「TradingViewを用いた終値照合」を一本化した統合スキャン関数。
    全時間足（1d/60m/5m/1m）を対象に拡張し、日足で検出済みの「同一銘柄・同一崖日」については
    分足側の重複検出結果を自動的に除外します（分足固有の局所バグのみ単独表示）。

    「真の倍率（true_multiplier）」は、崖前後どちらも同じ側（要補正Close, t-1）で
    TVと自社データを比較する方式に修正済みです：
        true_multiplier = TV of t-1 (要補正Close) / 自社 of t-1 (要補正Close)
    TV側のt-1が取得できない場合は、崖前後比率同士の比（フォールバック）を用います：
        true_multiplier = (TVのt÷TVのt-1) ÷ (自社のt÷自社のt-1)
    それでも取得できない分足（TV照合が不安定）は tv_close を None のままとし、
    データ推測倍率（est_multiplier）を「真の倍率」として安全に本番適用する仕様とします。
    """
    target_intervals = intervals if intervals else list(settings.TIMEFRAMES)
    per_interval_dfs = {}
    for iv in target_intervals:
        df_iv = scan_all_anomalies(is_jp=is_jp, interval=iv)
        if not df_iv.empty:
            df_iv = df_iv.copy()
            df_iv["interval"] = iv
        per_interval_dfs[iv] = df_iv

    # ── 【修正箇所】日足優先の重複排除：同一銘柄かつ同一日付のみを除外対象にする ──
    if "1d" in per_interval_dfs and not per_interval_dfs["1d"].empty:
        df_1d = per_interval_dfs["1d"]
        # (ticker, normalized_date) のペアをセットとして作成
        daily_flagged_pairs = set(
            zip(df_1d["ticker"], pd.to_datetime(df_1d["cliff_date"]).dt.normalize())
        )
        
        for iv in target_intervals:
            if iv == "1d" or per_interval_dfs.get(iv) is None or per_interval_dfs[iv].empty:
                continue
            df_iv = per_interval_dfs[iv]
            
            # 分足側の各行について (ticker, normalized_date) のリストを作成して比較判定
            iv_dates = pd.to_datetime(df_iv["cliff_date"]).dt.normalize()
            iv_pairs = list(zip(df_iv["ticker"], iv_dates))
            
            # 1dで同じ銘柄かつ同日に検出されていないデータのみを保持
            keep_mask = [pair not in daily_flagged_pairs for pair in iv_pairs]
            per_interval_dfs[iv] = df_iv[keep_mask].reset_index(drop=True)

    non_empty = [df for df in per_interval_dfs.values() if df is not None and not df.empty]
    if not non_empty:
        return pd.DataFrame()

    result = pd.concat(non_empty, ignore_index=True)
    result["tv_close"] = float("nan")       # TVの要補正Close（t-1）：表示・算出の主基準
    result["tv_after_close"] = float("nan")  # TVの変化当日Close（t）：参考値
    result["true_multiplier"] = float("nan")

    for idx, row in result.iterrows():
        interval = row["interval"]
        ticker = row["ticker"]
        patch_date = row.get("patch_date")
        before_close = row.get("before_close", float("nan"))
        after_close = row.get("after_close", float("nan"))

        if interval != "1d" or pd.isna(patch_date):
            # 分足（60m/5m/1m）はTV側からのピンポイント取得が不安定なため、
            # TV Close は None のまま、データ推測倍率(est_multiplier)を真の倍率として採用
            result.at[idx, "true_multiplier"] = row.get("est_multiplier", float("nan"))
            continue

        tv_pair = fetch_tv_close_pair(ticker, patch_date, is_jp=is_jp)
        tv_before_close = tv_pair.get("tv_before_close")
        tv_after_close = tv_pair.get("tv_after_close")

        if tv_before_close is not None:
            result.at[idx, "tv_close"] = tv_before_close
        if tv_after_close is not None:
            result.at[idx, "tv_after_close"] = tv_after_close

        if tv_before_close is not None and pd.notna(before_close) and before_close != 0:
            # 主方式：崖前（要補正）同士の比較
            result.at[idx, "true_multiplier"] = tv_before_close / before_close
        elif (
            tv_before_close is not None and tv_after_close is not None and tv_before_close != 0
            and pd.notna(before_close) and pd.notna(after_close) and before_close != 0
        ):
            # フォールバック：崖前後の比率同士の比較（正常変動 ÷ 異常変動）
            tv_ratio = tv_after_close / tv_before_close
            self_ratio = after_close / before_close
            if self_ratio != 0:
                result.at[idx, "true_multiplier"] = tv_ratio / self_ratio
        else:
            # TV照合が完全に失敗した場合は、分足と同様に推測倍率へフォールバック
            result.at[idx, "true_multiplier"] = row.get("est_multiplier", float("nan"))

    drop_cols = [c for c in ["open", "high", "low", "pct_change"] if c in result.columns]
    result = result.drop(columns=drop_cols)
    return result.sort_values(["ticker", "cliff_date", "interval"]).reset_index(drop=True)

def apply_bulk_selected_patches(patches: list, is_jp: bool = True, status_callback=None) -> dict:
    """
    統合スキャンのテーブルでチェックされた複数パッチ（[{"ticker","patch_date","multiplier"}, ...]）を
    ループで一括本番適用します。
    patch_date は「要補正Closeの日時（崖前日, t-1）」を指定し、この日を含めて（<=）過去へ一括適用します。

    各銘柄・時間足への適用直前に既存の check_anomaly_need_patch() による
    二重適用防止判定が自動的に働くため（apply_forced_scale_patch_to_all_timeframes内部）、
    すでに修復済みと判定されたものは自動でスキップされ、修復ログにも記録されません。
    """
    def log(msg):
        print(msg)
        if status_callback: status_callback(msg)

    market_str = "JP" if is_jp else "US"
    repaired_count = 0
    skipped_count = 0
    log_rows = []

    for patch in patches:
        ticker = patch.get("ticker")
        patch_date = patch.get("patch_date")
        multiplier = patch.get("multiplier")

        if not ticker or not patch_date or multiplier is None or pd.isna(multiplier) or multiplier <= 0:
            log(f"⚠️ [{ticker}] 真の倍率が取得できていないため、この行はスキップしました。")
            skipped_count += 1
            continue

        pure_t = sanitize_ticker(ticker, is_jp)
        try:
            patch_dt_str = pd.to_datetime(patch_date).strftime("%Y-%m-%d")
        except Exception:
            log(f"⚠️ [{ticker}] 要補正Close日時が不正なためスキップしました。")
            skipped_count += 1
            continue

        log(f"🔧 [{pure_t}] {patch_dt_str}（要補正Close日時）以前の一括修復パッチを判定・適用中（倍率: {multiplier:.6f}）...")
        results = apply_forced_scale_patch_to_all_timeframes(pure_t, patch_dt_str, multiplier, is_jp=is_jp)

        applied_intervals = [iv for iv, msg in results.items() if "補正適用完了" in str(msg)]

        if applied_intervals:
            repaired_count += 1
            log(f"   ✅ 適用完了 ({', '.join(applied_intervals)})")
            log_rows.append({
                "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ticker": pure_t,
                "market": market_str,
                "cliff_date": patch_dt_str,
                "interval": ",".join(applied_intervals),
                "before_close": "",
                "after_close": "",
                "multiplier": multiplier,
                "memo": "統合スキャン・一括自動修復（TradingView照合）",
            })
        else:
            skipped_count += 1
            log(f"   ⏭️ スキップ（すでに修復済み、または対象データなし）")

    if log_rows:
        try:
            from data_access.sheets_api import save_repair_log_to_sheets
            save_repair_log_to_sheets(log_rows)
            log(f"📝 実際に修復が実行された {len(log_rows)} 件のみをrepair_logへ記録しました。")
        except Exception as e:
            log(f"⚠️ 修復ログの保存に失敗しました: {e}")

    return {"repaired": repaired_count, "skipped": skipped_count}

def analyze_db_update_needs(is_jp: bool = True) -> dict:
    try:
        db_df = load_price_db("1d", is_jp=is_jp, is_raw=True) 
        all_tickers = get_all_collection_tickers() if is_jp else ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
        today = datetime.now().date()

        if db_df.empty:
            return {
                "global_max_date": None,
                "needs_period_update": True,
                "refetch_tickers": [],
                "missing_tickers": all_tickers,
            }

        db_df["date"] = pd.to_datetime(db_df["date"])
        global_max_date = db_df["date"].max().date()
        needs_period_update = (today - global_max_date).days > 3

        if "is_finalized" in db_df.columns:
            unfinalized = db_df[db_df["is_finalized"] == False]["ticker"].unique().tolist()
        else:
            unfinalized = []

        db_tickers = set(db_df["ticker"].unique())
        missing = [t for t in all_tickers if t not in db_tickers]

        return {
            "global_max_date": global_max_date,
            "needs_period_update": needs_period_update,
            "refetch_tickers": unfinalized,
            "missing_tickers": missing,
        }
    except Exception as e:
        return {
            "global_max_date": None,
            "needs_period_update": True,
            "refetch_tickers": [],
            "missing_tickers": [],
            "error": str(e),
        }