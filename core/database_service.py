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

def check_anomaly_need_patch(df_ticker: pd.DataFrame, cliff_date_str: str, multiplier: float, threshold: float = 0.10) -> bool:
    if df_ticker.empty or len(df_ticker) < 2:
        return False
        
    df_t = df_ticker.sort_values("date").reset_index(drop=True)
    df_t["date_dt"] = pd.to_datetime(df_t["date"])
    
    try:
        target_dt = pd.to_datetime(cliff_date_str)
    except Exception:
        return False
    
    before_rows = df_t[df_t["date_dt"] < target_dt]
    after_rows = df_t[df_t["date_dt"] >= target_dt]
    
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

        pre_mask = (df_result["ticker"] == ticker) & (pd.to_datetime(df_result["date"]) < cliff_date)
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
    def log(msg):
        print(msg)
        if status_callback: status_callback(msg)

    log(f"🏗️ [{interval}] RawデータからActiveデータの加工ビルドを開始します...")
    
    df_raw = load_price_db(interval, is_jp=is_jp, is_raw=True)
    if df_raw.empty:
        log("❌ Rawデータベースファイルが空、または検出されません。")
        return False

    df_raw["is_finalized"] = compute_is_finalized(df_raw["date"], interval, is_jp=is_jp)

    processed_parts = []
    for ticker, group in df_raw.groupby("ticker"):
        adjusted_group = adjust_ticker_splits_backward_in_memory(group)
        processed_parts.append(adjusted_group)
    df_processed = pd.concat(processed_parts, ignore_index=True)

    if not skip_assertion:
        df_processed = apply_saved_patches_to_df(df_processed, is_jp=is_jp)

    if interval != "1d":
        try:
            df_1d_active = load_price_db("1d", is_jp=is_jp, is_raw=False)
            df_processed = propagate_stop_allocation_bars_in_memory(df_1d_active, df_processed, is_jp=is_jp)
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
                log("🛑 深刻なデータ不整合（ジャンプなど）を検出したため、破損防止のため同期を強制中断しました。")
                return False
    else:
        log("✨ [白紙構築] 新旧データの整合性比較、および過去パッチの干渉をスキップしてクリーン処理します。")

    if dry_run:
        log(f"🧪 [DRY RUN] {interval} 加工・アサーション検証を正常に通過。")
        if settings.HAS_STREAMLIT:
            import streamlit as st
            st.session_state[f"temp_verified_active_df_{interval}"] = df_processed
            log(f"   💾 検証済みデータを一時メモリに格納しました。画面から「本番適用」できます。")
        return True
    else:
        df_processed = df_processed.sort_values(["ticker", "date"]).reset_index(drop=True)
        cloud_success, cloud_msg = save_price_db(df_processed, interval, is_jp=is_jp, is_raw=False)
        
        if cloud_success:
            log(f"✅ [{interval}] ActiveデータベースをGoogleドライブへ正常に保存しました。")
        else:
            log(f"⚠️ 【重要警告】[{interval}] Googleドライブへの同期に失敗しました（一時的にローカルフォルダに保存）。")
            log(f"   ❌ エラー詳細: {cloud_msg}")
            if "storageQuotaExceeded" in cloud_msg or "storage quota" in cloud_msg.lower():
                log("   💡 【解決方法】サービスアカウントのストレージ容量制限（0GB）に衝突しています。")
                log("      事前に同名ファイルをあなたのGoogleアカウントからアップロードして、所有者をご自身に変更してください。")
        return True

# =====================================================================
# 🧪 開発検証: 手動パッチのメモリ上適用シミュレーション
# =====================================================================

def test_forced_scale_patch_in_memory(ticker: str, cliff_date_str: str, multiplier: float, is_jp: bool = True) -> tuple:
    """
    手動パッチ（cliff_date / multiplier）を、実際のParquetを書き換えることなく
    すべてメモリ上でシミュレーション実行し、検証結果と仮の加工後DataFrameを返します。
    """
    pure_ticker = sanitize_ticker(ticker, is_jp)
    results = {}
    st_temp_dfs = {}
    
    try:
        target_dt = pd.to_datetime(cliff_date_str)
    except Exception as e:
        return {"error": f"崖日付のパース失敗: {e}"}, {}

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

        need_apply = check_anomaly_need_patch(ticker_data, cliff_date_str, multiplier)
        if not need_apply:
            results[interval] = "適用不要（既に調整済み、または落差がありません）"
            continue

        ticker_data["date"] = pd.to_datetime(ticker_data["date"])
        pre_mask = ticker_data["date"] < target_dt
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

    for interval in settings.TIMEFRAMES:
        log(f"⏱️ 【{market_name}】{interval} Rawデータ差分収集開始...")
        try:
            df_raw_db = load_price_db(interval, is_jp=is_jp, is_raw=True)
        except FileNotFoundError:
            df_raw_db = pd.DataFrame()

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

        active_timestamps = [pd.to_datetime(last_updates_map[t]) for t in tickers if t in last_updates_map]
        base_time = pd.Series(active_timestamps).mode()[0] if active_timestamps and not force_refetch else None

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
            for i in range(0, len(chunk_tickers), BATCH_SIZE):
                chunk = chunk_tickers[i:i+BATCH_SIZE]
                symbols = [f"{t}{suffix}" for t in chunk]
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
                    chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
                    if not chunk_processed.empty:
                        all_downloaded.append(chunk_processed)
                except Exception as e:
                    log(f"     Batch Error: {e}")
                time.sleep(1)

        if all_downloaded:
            new_combined = pd.concat(all_downloaded, ignore_index=True)
            if not df_raw_db.empty:
                df_raw_db = pd.concat([df_raw_db, new_combined], ignore_index=True)
                df_raw_db = df_raw_db.drop_duplicates(subset=["date", "ticker"], keep="last")
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

def apply_forced_scale_patch_to_all_timeframes(ticker: str, cliff_date: str, multiplier: float, is_jp: bool = True) -> dict:
    if multiplier <= 0:
        return {"error": f"処理を中断しました。倍率に 0 以下の数値（{multiplier}）は指定できません。"}

    pure_ticker = sanitize_ticker(ticker, is_jp)
    results = {}
    try:
        target_dt = pd.to_datetime(cliff_date)
    except Exception as e:
        return {"error": f"崖日付のパース失敗: {e}"}

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

        need_apply = check_anomaly_need_patch(ticker_data, cliff_date, multiplier)
        if not need_apply:
            results[interval] = "スキップ（既に調整済み、または適用不要な落差です）"
            continue

        ticker_data["date"] = pd.to_datetime(ticker_data["date"])
        pre_mask = ticker_data["date"] < target_dt
        
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
        result_rows.append(boundary_rows[["ticker", "date", "before_close", "after_close", "before_adj_close", "after_adj_close", "open", "high", "low", "volume", "pct_change"]])

    abs_close = db_df["close"].abs()
    pct = abs_close.groupby(db_df["ticker"]).pct_change()
    cliff_mask = pct.abs() >= threshold

    if cliff_mask.any():
        cliff_rows = db_df[cliff_mask].copy()
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
        result_rows.append(cliff_rows[["ticker", "date", "before_close", "after_close", "before_adj_close", "after_adj_close", "open", "high", "low", "volume", "pct_change"]])

    if not result_rows:
        return pd.DataFrame()
        
    result = pd.concat(result_rows, ignore_index=True).rename(columns={"date": "cliff_date"})
    
    def aggregate_anomalies(group):
        pct_vals = group["pct_change"].dropna()
        pct_val = pct_vals.iloc[0] if not pct_vals.empty else float("nan")
        
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
    return result.sort_values(["ticker", "cliff_date"]).reset_index(drop=True)

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