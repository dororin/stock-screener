# core/database_service.py
import os
import time
import pandas as pd
import pytz
import yfinance as yf
import gc  # メモリ解放用に追加
import sys # ログのフラッシュ用に追加
from datetime import datetime, timedelta, time as dt_time
from config import settings
from data_access.local_db import load_price_db, save_price_db
from core.collector import (
    sanitize_ticker, get_download_symbol, get_all_collection_tickers,
    get_benchmark_latest_date, parse_yfinance_batch
)

# --- psutilによるメモリ計測（利用できない場合は代替ロジックへフォールバック） ---
try:
    import psutil
except ImportError:
    psutil = None

def get_memory_usage_str() -> str:
    """現在のプロセスの物理メモリ使用量(RSS)を人間が読める文字列で返します。"""
    try:
        if psutil is not None:
            process = psutil.Process(os.getpid())
            mem_bytes = process.memory_info().rss
            return f"[RAM: {mem_bytes / (1024 * 1024):.2f} MB]"
        else:
            # Linuxコンテナ環境用の簡易フォールバック
            if os.path.exists('/proc/self/status'):
                with open('/proc/self/status', 'r') as f:
                    for line in f:
                        if 'VmRSS' in line:
                            parts = line.split()
                            val = float(parts[1])
                            unit = parts[2]
                            if unit.lower() == 'kB':
                                return f"[RAM: {val / 1024:.2f} MB]"
                            return f"[RAM: {val} {unit}]"
    except Exception:
        pass
    return "[RAM: 計測不可]"


# --- yfinanceが取得可能な期間の上限（日数） ---
YFINANCE_GAP_LIMITS = {"1m": 7, "5m": 60, "60m": 730}

# --- 東証: 取引時間延伸（2024年11月5日、arrowhead4.0稼働）の境界日 ---
TSE_EXTENDED_HOURS_DATE = pd.Timestamp("2024-11-05")

def get_jp_session_close_time(date) -> dt_time:
    """指定日における東証の大引け時刻を返します。"""
    d = pd.Timestamp(date).normalize()
    if d >= TSE_EXTENDED_HOURS_DATE:
        return dt_time(15, 30)
    return dt_time(15, 0)

def get_market_localized_now(is_jp: bool = True):
    """市場モード（is_jp）に基づき、ローカライズされた現在時刻と本日日付を返します。"""
    tz = pytz.timezone("Asia/Tokyo") if is_jp else pytz.timezone("America/New_York")
    now_tz = datetime.now(pytz.utc).astimezone(tz)
    local_today = now_tz.date()
    return now_tz, local_today

def compute_is_finalized(date_series: pd.Series, interval: str, is_jp: bool = True) -> pd.Series:
    """時間ベースの確定（finalize）判定ロジック。"""
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
    """日足(1d)データから「寄り付かずストップ高/安（比例配分）」の日付を検出します。"""
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
    """短期足DBの既存スキーマに合わせ、大引け固定の1本分のバー行を組み立てます。"""
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
    """指定日の既存レコードを一度全削除してから、大引け固定の1本のバーに置換します。"""
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

def propagate_stop_allocation_bars_to_intraday(stop_days_df: pd.DataFrame, is_jp: bool = True, log_func=None) -> dict:
    def _log(msg):
        if log_func: log_func(msg)
        else: print(msg, flush=True)

    results = {"60m": 0, "5m": 0, "1m": 0}
    if stop_days_df is None or stop_days_df.empty:
        return results

    for interval in ["60m", "5m", "1m"]:
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
        except FileNotFoundError:
            continue
        if db_df.empty:
            continue

        db_df["date"] = pd.to_datetime(db_df["date"])
        db_df["date_only"] = db_df["date"].dt.date
        limits_map = db_df.groupby("ticker")["date_only"].agg(["min", "max"]).to_dict(orient="index")
        
        applied = 0
        for _, row in stop_days_df.iterrows():
            ticker = row["ticker"]
            day_date = pd.Timestamp(row["date"]).date()
            
            if ticker in limits_map:
                t_min = limits_map[ticker]["min"]
                t_max = limits_map[ticker]["max"]
                
                if t_min <= day_date <= t_max:
                    db_df = _replace_stop_allocation_bar(
                        db_df, ticker, row["date"], row["close"], row["volume"], is_jp=is_jp
                    )
                    applied += 1

        db_df = db_df.drop(columns=["date_only"])

        if applied > 0:
            db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
            save_price_db(db_df, interval, is_jp=is_jp)
            results[interval] = applied
            _log(f"  🩹 [{interval}] ストップ高安（比例配分）バーを {applied}件 反映しました。")
        else:
            _log(f"  🧊 [{interval}] 保持期間内の対象がないため修復はスキップされました。")

    return results

def repair_stop_allocation_bars_full(is_jp: bool = True, status_callback=None) -> dict:
    """比例配分日の大引けバー一括修復。"""
    def log(msg):
        print(msg, flush=True)
        if status_callback: status_callback(msg)

    try:
        db_1d = load_price_db("1d", is_jp=is_jp)
    except FileNotFoundError:
        log("❌ 1d データベースが見つかりません。")
        return {}

    stop_days_df = detect_allocation_stop_days(db_1d)
    if stop_days_df.empty:
        log("🧊 確定済みの比例配分日は検出されませんでした。")
        return {}

    log(f"🔍 確定済みの比例配分日を {len(stop_days_df)}件 検出しました。短期足の一括修復を開始します...")
    results = propagate_stop_allocation_bars_to_intraday(stop_days_df, is_jp=is_jp, log_func=log)
    total = sum(results.values())
    log(f"🎉 一括修復完了（合計 {total}件）。60m: {results.get('60m', 0)}件 / 5m: {results.get('5m', 0)}件 / 1m: {results.get('1m', 0)}件")
    return results

def check_anomaly_need_patch(df_ticker: pd.DataFrame, cliff_date_str: str, multiplier: float, threshold: float = 0.10) -> bool:
    """パッチ適用が必要か判定します。"""
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

def analyze_db_update_needs(is_jp: bool = True) -> dict:
    """現在の1dデータ最終更新日を判定し、期間更新や再取得、不足対象などを算出します。"""
    try:
        db_df = load_price_db("1d", is_jp=is_jp)
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

def merge_price_data(old_df: pd.DataFrame, new_df: pd.DataFrame, interval: str, is_jp: bool = True, forced_split_ratio: float = None) -> pd.DataFrame:
    """新旧DataFrameをマージし、権利落ちや手動強制分割比率などを数学的に後ろ向き調整します。"""
    if new_df is None or new_df.empty:
        return old_df

    new_tickers = new_df["ticker"].unique()
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
        
        has_split = False
        split_ratio = 1.0
        
        if forced_split_ratio is not None and forced_split_ratio > 0:
            split_ratio = forced_split_ratio
            has_split = True
            
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
                            t_new.loc[pre_mask, col] = t_new.loc[pre_mask, col] * forced_split_ratio
                    if "volume" in t_new.columns:
                        t_new.loc[pre_mask, "volume"] = t_new.loc[pre_mask, "volume"] / forced_split_ratio
        else:
            if len(t_new) > 1:
                pct_changes = t_new["close"].pct_change()
                anomaly_mask = pct_changes <= -0.40
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
                            pre_mask = t_new["date"] < split_date
                            price_cols = ["open", "high", "low", "close", "adj close"]
                            for col in price_cols:
                                if col in t_new.columns:
                                    t_new.loc[pre_mask, col] = t_new.loc[pre_mask, col] / est_ratio
                            if "volume" in t_new.columns:
                                t_new.loc[pre_mask, "volume"] = t_new.loc[pre_mask, "volume"] * est_ratio
                            split_ratio = 1.0 / est_ratio
                            has_split = True

            official_split_val = 1.0
            if "stock splits" in t_new.columns:
                splits_active = t_new["stock splits"].dropna()
                splits_active = splits_active[(splits_active > 0) & (splits_active != 1.0)]
                if not splits_active.empty:
                    official_split_val = splits_active.iloc[-1]
            
            if official_split_val != 1.0:
                split_ratio = 1.0 / official_split_val
                has_split = True
            elif not has_split and not t_old.empty:
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

        if has_split and not t_old.empty:
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
            
            if apply_split:
                price_cols = ["open", "high", "low", "close", "adj close"]
                for col in price_cols:
                    if col in t_old.columns:
                        t_old[col] = t_old[col] * split_ratio
                if "volume" in t_old.columns:
                    t_old["volume"] = t_old["volume"] / split_ratio
                
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

def propagate_split_to_other_timeframes(ticker: str, split_ratio: float, is_jp: bool = True, log_func=None):
    """株式分割を短期足DB(60m, 5m, 1m)へ調整適用。"""
    def _log(msg):
        if log_func: log_func(msg)
        else: print(msg, flush=True)

    ticker_symbol = f"{ticker}.T" if is_jp and ticker.isdigit() else ticker
    try:
        df_check = yf.download(ticker_symbol, period="5d", interval="1d", auto_adjust=False, progress=False)
        if df_check.empty: return

        if isinstance(df_check.columns, pd.MultiIndex):
            try:
                df_check = df_check.xs(ticker_symbol, axis=1, level=1)
            except Exception:
                df_check.columns = df_check.columns.get_level_values(0)

        if df_check.index.tz is not None:
            df_check.index = df_check.index.tz_localize(None)
        check_dates = df_check.index
    except Exception:
        return

    for interval in ["60m", "5m", "1m"]:
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
            if db_df.empty: continue
            mask = db_df["ticker"] == ticker
            ticker_db = db_df[mask].copy()
            if ticker_db.empty: continue
            
            ticker_db["date"] = pd.to_datetime(ticker_db["date"])
            if ticker_db["date"].dt.tz is not None:
                ticker_db["date"] = ticker_db["date"].dt.tz_localize(None)
            common_dates = ticker_db["date"].dt.date.isin(check_dates.date)
            
            apply_split = True
            if common_dates.any():
                last_common_dt = ticker_db[common_dates]["date"].max()
                check_date_only = last_common_dt.date()
                price_db = ticker_db[ticker_db["date"] == last_common_dt]["close"].iloc[-1]
                matching_check_row = df_check[df_check.index.date == check_date_only]
                if not matching_check_row.empty:
                    close_val = matching_check_row["Close"]
                    if isinstance(close_val, pd.DataFrame):
                        close_val = close_val.iloc[:, 0]
                    price_real = close_val.iloc[-1]
                    if isinstance(price_real, pd.Series):
                        price_real = price_real.iloc[-1]
                    if price_db <= (price_real * 1.1):
                        apply_split = False
            
            if apply_split:
                _log(f"  🔄 [{ticker}] {interval} に分割調整を適用 (ratio: {split_ratio:.4f})...")
                price_cols = ["open", "high", "low", "close", "adj close"]
                for col in price_cols:
                    if col in db_df.columns:
                        db_df.loc[mask, col] = db_df.loc[mask, col] * split_ratio
                if "volume" in db_df.columns:
                    db_df.loc[mask, "volume"] = db_df.loc[mask, "volume"] / split_ratio
                save_price_db(db_df, interval, is_jp=is_jp)
        except Exception:
            pass

def update_price_database(is_jp: bool = True, target_tickers: list = None, force_refetch: bool = False, status_callback=None):
    """【差分同期用】時間足(1d, 60m, 5m, 1m)データベースの同期を実行します。"""
    market_name = "JP" if is_jp else "US"
    tickers = target_tickers if target_tickers else []
    
    def log(msg):
        # 画面およびコンソールへリアルタイム出力 (セグフォ直前ログの追跡性を上げるため flush=True)
        print(f"[SYNC-DB] {msg}", flush=True)
        sys.stdout.flush()
        if status_callback: 
            status_callback(msg)
            
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
        log(f"⏱️ 【{market_name}】{interval} データベース同期開始... {get_memory_usage_str()}")
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
        except FileNotFoundError as e:
            log(f"⚠️ スキップ: {e}")
            continue

        db_max_date = db_df["date"].max() if not db_df.empty else None
        if db_max_date is not None:
            if interval != "1d":
                limit_hour = 15 if is_jp else 16
                limit_time = datetime.strptime(f"{limit_hour}:00:00", "%H:%M:%S").time()
                if db_max_date.time() > limit_time:
                    db_max_date = db_max_date.replace(hour=limit_hour, minute=0, second=0, microsecond=0)
            
            bm_last_date = get_benchmark_latest_date(interval, is_jp=is_jp)
            log(f"  🔍 ベンチマーク最新: {bm_last_date} | DB最新: {db_max_date}")
            if bm_last_date is not None:
                if bm_last_date <= db_max_date:
                    log(f"  ✨ 【同期スキップ】最新状態のためスキップします。")
                    continue
                else:
                    log(f"  📥 【同期実行】新データ同期に移行します。")

        last_updates_map = {}
        if not db_df.empty:
            last_updates_map = db_df.groupby("ticker")["date"].max().to_dict()

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
                            f"yfinanceの上限（{limit_days}日）を超えたため同期できません。"
                        )
                        continue

            # BATCH処理の逐次ダウンロード監視
            BATCH_SIZE = 100
            for i in range(0, len(chunk_tickers), BATCH_SIZE):
                chunk = chunk_tickers[i:i+BATCH_SIZE]
                symbols = [f"{t}{suffix}" for t in chunk]
                
                mem_before = get_memory_usage_str()
                log(f"📥 [DL開始] {interval} バッチ {i+1}〜{min(i+BATCH_SIZE, len(chunk_tickers))} | 銘柄数: {len(chunk)} | {mem_before}")
                log(f"   👉 銘柄サンプル: {', '.join(chunk[:8])}")
                
                try:
                    log(f"   ⚡ yf.download() 呼び出し開始 (threads=True)...")
                    # yfinanceの呼び出し直前で落ちる場合は、マルチスレッド(threads=True)によるCライブラリ競合です
                    df_raw = yf.download(
                        symbols, 
                        start=start_date_str,
                        interval=interval, 
                        auto_adjust=False, 
                        actions=True, 
                        progress=False, 
                        threads=False, 
                        timeout=30
                    )
                    
                    mem_after_dl = get_memory_usage_str()
                    log(f"   ✅ yf.download() 完了 | 生データ形状: {df_raw.shape if not df_raw.empty else '空'} | {mem_after_dl}")
                    
                    if not df_raw.empty:
                        log(f"   ⚙️ parse_yfinance_batch() 処理開始...")
                        chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
                        mem_after_parse = get_memory_usage_str()
                        log(f"   ✅ parse 完了 | 成形データ件数: {len(chunk_processed)}件 | {mem_after_parse}")
                        
                        if not chunk_processed.empty:
                            all_downloaded.append(chunk_processed)
                    else:
                        log("   ⚠️ 取得したDataFrameは空でした。")
                        
                except Exception as e:
                    log(f"   ❌ Batch Error: {e}")
                
                # ループごとの不要メモリの強制解放
                del df_raw
                gc.collect()
                time.sleep(1)

        if all_downloaded:
            log(f"🔄 全バッチ取得終了。マージ処理に移ります... {get_memory_usage_str()}")
            new_combined = pd.concat(all_downloaded, ignore_index=True)
            if "date" in new_combined.columns:
                new_combined["is_finalized"] = compute_is_finalized(new_combined["date"], interval, is_jp=is_jp)
            
            reset_tickers = []
            if interval == "1d":
                if "is_finalized" in new_combined.columns:
                    finalized_for_detection = new_combined[new_combined["is_finalized"] == True]
                else:
                    finalized_for_detection = new_combined

                for ticker in finalized_for_detection["ticker"].unique():
                    t_new = finalized_for_detection[finalized_for_detection["ticker"] == ticker]
                    has_action, has_split, split_ratio = False, False, 1.0
                    
                    if "stock splits" in t_new.columns:
                        splits_active = t_new["stock splits"].dropna()
                        splits_active = splits_active[(splits_active > 0) & (splits_active != 1.0)]
                        if not splits_active.empty:
                            split_ratio = 1.0 / splits_active.iloc[-1]
                            has_action = has_split = True
                            
                    if "dividends" in t_new.columns:
                        divs_active = t_new["dividends"].dropna()
                        if not divs_active[divs_active > 0].empty:
                            has_action = True
                            
                    if has_action:
                        if has_split:
                            log(f"🚨 [分割自動トリガー] {ticker} 短期足事前調整適用 (1/S={1.0/split_ratio:.1f})")
                            propagate_split_to_other_timeframes(ticker, split_ratio, is_jp=is_jp, log_func=log)
                        
                        log(f"🚨 [再構築自動トリガー] {ticker} (1d) を完全再構築します。")
                        rebuild_single_ticker_db(ticker, is_jp=is_jp, interval="1d")
                        reset_tickers.append(ticker)
                            
                if reset_tickers:
                    new_combined = new_combined[~new_combined["ticker"].isin(reset_tickers)]
                    db_df = load_price_db(interval, is_jp=is_jp)
            
            if not new_combined.empty:
                log(f"💾 Parquetマージ及び書き込み開始... {get_memory_usage_str()}")
                db_df = merge_price_data(db_df, new_combined, interval, is_jp=is_jp)
                save_price_db(db_df, interval, is_jp=is_jp)
                log(f"  ✅ {interval} データベース更新完了。 {get_memory_usage_str()}")

                if interval == "1d":
                    try:
                        updated_tickers = set(new_combined["ticker"].unique())
                        db_1d_scope = db_df[db_df["ticker"].isin(updated_tickers)]
                        stop_days_df = detect_allocation_stop_days(db_1d_scope)
                        if not stop_days_df.empty:
                            log(f"🚨 [S高/S安自動検知] 確定済み比例配分日を {len(stop_days_df)}件 検出。短期足へバーを自動移植します。")
                            propagate_stop_allocation_bars_to_intraday(stop_days_df, is_jp=is_jp, log_func=log)
                    except Exception as e:
                        log(f"⚠️ ストップ高安バーの自動移植処理中にエラーが発生しました: {e}")
        else:
            log(f"  🧊 追加データはありません。")

    log("🔄 【整合性自動復元】全体更新完了に伴い、保存済みの手動修復パッチを自動適用します...")
    try:
        apply_all_saved_patches(is_jp=is_jp, status_callback=status_callback)
    except Exception as e:
        log(f"⚠️ パッチの自動復元プロセス中にエラーが発生しました: {e}")

def full_rebuild_all_database(is_jp: bool = True, interval: str = "1d", status_callback=None) -> bool:
    """【完全クリーンビルド用】指定市場の該当時間足データベースを完全新規再構築します。"""
    def log(msg):
        print(f"[REBUILD-DB] {msg}", flush=True)
        sys.stdout.flush()
        if status_callback: 
            status_callback(msg)

    market_name = "JP" if is_jp else "US"
    if is_jp:
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
        
    log(f"🚨 [フル再構築] {market_name} ({interval}) 開始。総数: {len(tickers)} | {get_memory_usage_str()}")
    
    all_downloaded = []
    BATCH_SIZE = 30
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i+BATCH_SIZE]
        symbols = [f"{t}{suffix}" for t in chunk]
        
        mem_before = get_memory_usage_str()
        log(f"📥 一括DL ({i + 1}〜{min(i + BATCH_SIZE, len(tickers))}): {', '.join(chunk[:5])}... | {mem_before}")
        
        try:
            log(f"   ⚡ yf.download() 開始 (threads=True)...")
            df_raw = yf.download(
                symbols,
                start=start_date_dt.strftime("%Y-%m-%d"),
                interval=interval,
                auto_adjust=False,
                actions=True,
                progress=False,
                threads=False,
                timeout=30
            )
            
            mem_after_dl = get_memory_usage_str()
            log(f"   ✅ yf.download() 完了 | 生データ形状: {df_raw.shape if not df_raw.empty else '空'} | {mem_after_dl}")
            
            if not df_raw.empty:
                log(f"   ⚙️ parse_yfinance_batch() 処理開始...")
                chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
                mem_after_parse = get_memory_usage_str()
                log(f"   ✅ parse 完了 | 成形データ件数: {len(chunk_processed)}件 | {mem_after_parse}")
                
                if not chunk_processed.empty:
                    all_downloaded.append(chunk_processed)
            else:
                log("   ⚠️ 取得したDataFrameは空でした。")
        except Exception as e:
            log(f"    -> ⚠️ エラー: {e}")
            
        del df_raw
        gc.collect()
        time.sleep(1.5)
        
    if all_downloaded:
        log(f"💾 Parquet一括マージ・保存処理中... {get_memory_usage_str()}")
        final_df = pd.concat(all_downloaded, ignore_index=True)
        if "date" in final_df.columns:
            final_df["is_finalized"] = compute_is_finalized(final_df["date"], interval, is_jp=is_jp)
        
        final_df = final_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        save_price_db(final_df, interval, is_jp=is_jp)
        log(f"🎉 再構築データ保存完了！ {get_memory_usage_str()}")

        log("🔄 【整合性自動復元】フル再構築完了に伴い、保存済みの手動修復パッチを自動適用します...")
        try:
            apply_all_saved_patches(is_jp=is_jp, status_callback=status_callback)
        except Exception as e:
            log(f"⚠️ パッチの自動復元プロセス中にエラーが発生しました: {e}")

        return True
    return False

def rebuild_single_ticker_db(ticker: str, is_jp: bool = True, interval: str = "1d") -> bool:
    """特定1銘柄に対して完全な新規全履歴再取得を行いDBに置換マージします。"""
    pure_ticker = sanitize_ticker(ticker, is_jp)
    symbol = get_download_symbol(pure_ticker, is_jp)
    now = datetime.now()
    
    if interval == "1m": start_date_dt = now - timedelta(days=6)
    elif interval == "5m": start_date_dt = now - timedelta(days=58)
    elif interval == "60m": start_date_dt = now - timedelta(days=718)
    else: start_date_dt = datetime(2016, 1, 1)
        
    try:
        df_raw = yf.download(
            symbol,
            start=start_date_dt.strftime("%Y-%m-%d"),
            interval=interval,
            auto_adjust=False,
            actions=True,
            progress=False
        )
        new_df = parse_yfinance_batch(df_raw, [pure_ticker], is_jp=is_jp)
        if new_df.empty: return False
        
        new_df["is_finalized"] = compute_is_finalized(new_df["date"], interval, is_jp=is_jp)
            
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
        except FileNotFoundError:
            db_df = pd.DataFrame()
            
        if not db_df.empty:
            db_df = db_df[db_df["ticker"] != pure_ticker]
            
        db_df = pd.concat([db_df, new_df], ignore_index=True)
        db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        save_price_db(db_df, interval, is_jp=is_jp)
        return True
    except Exception:
        return False

def repair_single_ticker_all_timeframes(ticker: str, is_jp: bool = True, forced_split_ratio: float = None) -> dict:
    """特定銘柄の安全修復。"""
    pure_ticker = sanitize_ticker(ticker, is_jp)
    symbol = get_download_symbol(pure_ticker, is_jp)
    now = datetime.now()
    results = {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            if interval == "1d":
                df_raw = yf.download(symbol, period="max", interval="1d", auto_adjust=False, actions=True, progress=False)
                if df_raw.empty:
                    results["1d"] = "データ取得失敗"
                    continue
                new_df = parse_yfinance_batch(df_raw, [pure_ticker], is_jp=is_jp)
                if new_df.empty:
                    results["1d"] = "パース結果空"
                    continue
                try:
                    db_df = load_price_db("1d", is_jp=is_jp)
                except FileNotFoundError:
                    db_df = pd.DataFrame()

                new_df["is_finalized"] = True
                
                if not db_df.empty:
                    db_df = db_df[db_df["ticker"] != pure_ticker]
                db_df = pd.concat([db_df, new_df], ignore_index=True)
                db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
                save_price_db(db_df, "1d", is_jp=is_jp)
                results["1d"] = f"完全再構築成功 ({len(new_df):,}件)"
                continue

            try:
                db_df = load_price_db(interval, is_jp=is_jp)
            except FileNotFoundError:
                db_df = pd.DataFrame()

            old_df = db_df[db_df["ticker"] == pure_ticker].copy() if not db_df.empty else pd.DataFrame()
            if interval == "1m": start_date_dt = now - timedelta(days=6)
            elif interval == "5m": start_date_dt = now - timedelta(days=58)
            elif interval == "60m": start_date_dt = now - timedelta(days=718)

            df_raw = yf.download(symbol, start=start_date_dt.strftime("%Y-%m-%d"), interval=interval, auto_adjust=False, actions=True, progress=False)
            if df_raw.empty:
                results[interval] = "新規データ空（置換なし）"
                continue
            new_df = parse_yfinance_batch(df_raw, [pure_ticker], is_jp=is_jp)
            if new_df.empty:
                results[interval] = "パース結果空（置換なし）"
                continue

            new_df["is_finalized"] = True

            if not old_df.empty:
                new_dates = pd.to_datetime(new_df["date"])
                old_df["date_dt"] = pd.to_datetime(old_df["date"])
                old_filtered = old_df[~old_df["date_dt"].isin(new_dates)].copy()
                if "date_dt" in old_filtered.columns:
                    old_filtered = old_filtered.drop(columns=["date_dt"])
                merged_df = pd.concat([old_filtered, new_df], ignore_index=True)
            else:
                merged_df = new_df

            if not db_df.empty:
                db_df = db_df[db_df["ticker"] != pure_ticker]
            db_df = pd.concat([db_df, merged_df], ignore_index=True)
            db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
            save_price_db(db_df, interval, is_jp=is_jp)
            results[interval] = f"部分置換マージ修復成功 ({len(merged_df):,}件)"
        except Exception as e:
            results[interval] = f"エラー: {str(e)}"

    try:
        apply_all_saved_patches(is_jp=is_jp)
    except Exception as e:
        print(f"個別ダウンロード完了後の自動パッチ適用でエラー: {e}")

    return results

def backward_scale_repair(df: pd.DataFrame, threshold: float = 0.35) -> tuple:
    """異常値の後ろ向きスケール調整。"""
    if df.empty:
        return df, []
    df = df.sort_values("date").reset_index(drop=True)
    price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df.columns]
    repairs = []

    negative_mask = df["close"] < 0
    if negative_mask.any():
        for col in price_cols:
            df[col] = df[col].abs()
        repairs.append({
            "cliff_date": df.loc[negative_mask, "date"].iloc[0],
            "multiplier": 1.0,
            "before_close": "負の数",
            "after_close": "絶対値変換",
        })

    pct_changes = df["close"].pct_change()
    cliff_mask = pct_changes.abs() >= threshold
    cliffs = pct_changes[cliff_mask].index.tolist()
    cliffs.sort(reverse=True)

    for idx in cliffs:
        if idx == 0:
            continue
        after_price = df.loc[idx, "close"]
        before_price = df.loc[idx - 1, "close"]
        if before_price == 0 or after_price == 0:
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
    return df, repairs

def scan_all_anomalies(is_jp: bool = True, interval: str = "1d", threshold: float = 0.35) -> pd.DataFrame:
    """異常データをスキャン。"""
    try:
        db_df = load_price_db(interval, is_jp=is_jp)
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
    shifted_neg_mask_for_neg = db_df.groupby("ticker")["close"].apply(lambda x: (x < 0).shift(1, fill_value=False)).reset_index(level=0, drop=True)
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

def apply_scale_repair_with_intraday_propagation(ticker: str, is_jp: bool = True, threshold: float = 0.35, dry_run: bool = False) -> dict:
    """指定銘柄の日足異常を修復したうえで、倍率を分足へ波及。"""
    pure_ticker = sanitize_ticker(ticker, is_jp)
    results = {}
    try:
        db_1d = load_price_db("1d", is_jp=is_jp)
    except FileNotFoundError as e:
        return {"error": str(e)}

    ticker_1d = db_1d[db_1d["ticker"] == pure_ticker].copy()
    if ticker_1d.empty:
        return {"1d": "データなし", "repair_details": []}

    fixed_1d, repairs = backward_scale_repair(ticker_1d, threshold=threshold)
    if not repairs:
        return {"1d": "異常なし", "repair_details": []}

    results["1d"] = f"{len(repairs)}箇所修正"
    if not dry_run:
        db_1d = db_1d[db_1d["ticker"] != pure_ticker]
        db_1d = pd.concat([db_1d, fixed_1d], ignore_index=True)
        db_1d = db_1d.sort_values(["ticker", "date"]).reset_index(drop=True)
        save_price_db(db_1d, "1d", is_jp=is_jp)

    for interval in ["60m", "5m", "1m"]:
        try:
            db_intra = load_price_db(interval, is_jp=is_jp)
        except FileNotFoundError:
            results[interval] = "DBなし"
            continue
        ticker_intra = db_intra[db_intra["ticker"] == pure_ticker].copy()
        if ticker_intra.empty:
            results[interval] = "データなし"
            continue

        ticker_intra = ticker_intra.sort_values("date").reset_index(drop=True)
        price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in ticker_intra.columns]
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

        if applied_count > 0 and not dry_run:
            db_intra = db_intra[db_intra["ticker"] != pure_ticker]
            db_intra = pd.concat([db_intra, ticker_intra], ignore_index=True)
            db_intra = db_intra.sort_values(["ticker", "date"]).reset_index(drop=True)
            save_price_db(db_intra, interval, is_jp=is_jp)
            results[interval] = f"{applied_count}崖分を波及適用"
        else:
            results[interval] = f"適用崖なし / DRY"

    results["repair_details"] = repairs
    results["ticker"] = pure_ticker
    return results

def run_database_health_scan(is_jp: bool) -> list:
    """全タイムフレームのParquetデータベース健康診断。"""
    anomalies = []
    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            df = load_price_db(interval, is_jp=is_jp)
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

def apply_forced_scale_patch_to_all_timeframes(ticker: str, cliff_date: str, multiplier: float, is_jp: bool = True) -> dict:
    """補正（後ろ向き調整）を適用します。"""
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
            db_df = load_price_db(interval, is_jp=is_jp)
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
        save_price_db(db_df, interval, is_jp=is_jp)
        results[interval] = f"{pre_mask.sum()}件補正適用完了"
    return results

def apply_all_saved_patches(is_jp: bool = True, status_callback=None) -> int:
    """パッチの自動復元。"""
    def log(msg):
        print(msg, flush=True)
        if status_callback: status_callback(msg)

    try:
        from data_access.sheets_api import load_repair_log_from_sheets
        log_df = load_repair_log_from_sheets()
    except Exception as e:
        log(f"❌ [パッチ一括再適用] スプレッドシートからのログ取得に失敗: {e}")
        return 0

    if log_df.empty:
        log("🧊 保存されているパッチ情報はありません。")
        return 0

    market_str = "JP" if is_jp else "US"
    valid_patches = []
    
    for _, row in log_df.iterrows():
        if str(row.get("market", "")).strip().upper() != market_str:
            continue
        
        ticker = str(row.get("ticker", "")).strip()
        cliff_date_str = str(row.get("cliff_date", "")).strip()
        multiplier_str = str(row.get("multiplier", "")).strip()
        
        if not ticker or not cliff_date_str or not multiplier_str:
            continue
            
        try:
            multiplier_val = float(multiplier_str)
            if multiplier_val <= 0 or multiplier_val == 1.0:
                continue
            cliff_dt = pd.to_datetime(cliff_date_str)
        except Exception:
            continue

        valid_patches.append({
            "ticker": ticker,
            "cliff_date": cliff_dt,
            "multiplier": multiplier_val,
            "memo": str(row.get("memo", ""))
        })

    if not valid_patches:
        log("🧊 有効な再適用対象パッチはありませんでした。")
        return 0

    valid_patches = sorted(valid_patches, key=lambda x: x["cliff_date"], reverse=True)
    log(f"🛠️ [パッチ一括再適用] {len(valid_patches)}件の定義を適用します...")
    
    success_count = 0
    for patch in valid_patches:
        t = patch["ticker"]
        dt_str = patch["cliff_date"].strftime("%Y-%m-%d")
        mul = patch["multiplier"]
        memo = patch["memo"]
        
        res = apply_forced_scale_patch_to_all_timeframes(t, dt_str, mul, is_jp=is_jp)
        
        applied = any("補正適用完了" in str(v) for v in res.values())
        if applied:
            success_count += 1
            log(f"  👉 適用完了: [{t}] 崖日: {dt_str} | 比率: {mul:.6f} ({memo})")
            log(f"     ➔ 1d: {res.get('1d', 'なし')}, 60m: {res.get('60m', 'なし')}")
            
    log(f"🎉 [パッチ一括再適用] 処理完了。新規適用: {success_count} / {len(valid_patches)} 件")
    return success_count

def delete_data_before_date(ticker: str, limit_date_str: str, is_jp: bool = True) -> dict:
    """指定日以前のデータを物理削除します。"""
    pure_ticker = sanitize_ticker(ticker, is_jp)
    limit_dt = pd.to_datetime(limit_date_str)
    results = {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
            if db_df.empty:
                results[interval] = "DB空"
                continue
            
            db_df["temp_date"] = pd.to_datetime(db_df["date"])
            mask_to_delete = (db_df["ticker"] == pure_ticker) & (db_df["temp_date"] <= limit_dt)
            deleted_count = mask_to_delete.sum()
            
            if deleted_count > 0:
                db_df = db_df[~mask_to_delete].copy()
                db_df = db_df.drop(columns=["temp_date"])
                db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
                save_price_db(db_df, interval, is_jp=is_jp)
                results[interval] = f"正常に {deleted_count:,} 件削除"
            else:
                results[interval] = "該当データなし"
        except FileNotFoundError:
            results[interval] = "DBファイルなし"
        except Exception as e:
            results[interval] = f"エラー: {str(e)}"
            
    return results