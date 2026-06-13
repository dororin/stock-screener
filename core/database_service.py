# core/database_service.py
import os
import time
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from config import settings
from data_access.local_db import load_price_db, save_price_db
from core.collector import (
    sanitize_ticker, get_download_symbol, get_all_collection_tickers,
    get_benchmark_latest_date, parse_yfinance_batch
)

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

# core/database_service.py より修正 (1/4)

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
            split_ratio = 1.0 / forced_split_ratio
            has_split = True
            
            if len(t_new) > 1:
                pct_changes = t_new["close"].pct_change()
                anomaly_mask = pct_changes <= -0.40
                if anomaly_mask.any():
                    anomaly_idx = anomaly_mask.idxmax()
                    split_date = t_new.loc[anomaly_idx, "date"]
                    pre_mask = t_new["date"] < split_date
                    # price_cols に "adj close" を追加
                    price_cols = ["open", "high", "low", "close", "adj close"]
                    for col in price_cols:
                        if col in t_new.columns:
                            t_new.loc[pre_mask, col] = t_new.loc[pre_mask, col] / forced_split_ratio
                    if "volume" in t_new.columns:
                        t_new.loc[pre_mask, "volume"] = t_new.loc[pre_mask, "volume"] * forced_split_ratio
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
                            # price_cols に "adj close" を追加
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
                # price_cols に "adj close" を追加
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

# core/database_service.py より修正 (2/4)

def propagate_split_to_other_timeframes(ticker: str, split_ratio: float, is_jp: bool = True, log_func=None):
    """日足等で検知した株式分割を、短期足DB(60m, 5m, 1m)へ数学的に先行適用します。"""
    def _log(msg):
        if log_func: log_func(msg)
        else: print(msg)

    ticker_symbol = f"{ticker}.T" if is_jp and ticker.isdigit() else ticker
    try:
        df_check = yf.download(ticker_symbol, period="5d", interval="1d", auto_adjust=False, progress=False)
        if df_check.empty: return
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
            common_dates = ticker_db["date"].dt.date.isin(check_dates.date)
            
            apply_split = True
            if common_dates.any():
                last_common_dt = ticker_db[common_dates]["date"].max()
                check_date_only = last_common_dt.date()
                price_db = ticker_db[ticker_db["date"] == last_common_dt]["close"].iloc[-1]
                matching_check_row = df_check[df_check.index.date == check_date_only]
                if not matching_check_row.empty:
                    price_real = matching_check_row["Close"].iloc[-1]
                    if price_db <= (price_real * 1.1):
                        apply_split = False
            
            if apply_split:
                _log(f"  🔄 [{ticker}] {interval} に分割調整を適用中 (ratio: {split_ratio:.4f})...")
                # price_cols に "adj close" を追加
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
    """時間足(1d, 60m, 5m, 1m)データベースの同期を実行します。"""
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

    now = datetime.now()
    suffix = ".T" if is_jp else ""
    tickers = [sanitize_ticker(t, is_jp) for t in tickers]

    for interval in settings.TIMEFRAMES:
        log(f"⏱️ 【{market_name}】{interval} データベース同期開始...")
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

        # 3レイヤー・バケットグループ化
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
            if "date" in new_combined.columns:
                new_combined["is_finalized"] = new_combined["date"].dt.date < now.date()
                if interval != "1d":
                    new_combined["is_finalized"] = new_combined["date"] < (now - timedelta(hours=1))
            
            reset_tickers = []
            if interval == "1d":
                for ticker in new_combined["ticker"].unique():
                    t_new = new_combined[new_combined["ticker"] == ticker]
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
                db_df = merge_price_data(db_df, new_combined, interval, is_jp=is_jp)
                save_price_db(db_df, interval, is_jp=is_jp)
                log(f"  ✅ {interval} データベース更新完了。")
        else:
            log(f"  🧊 追加データはありません。")

def full_rebuild_all_database(is_jp: bool = True, interval: str = "1d", status_callback=None) -> bool:
    """指定市場の該当時間足データベースを完全新規再構築（クリーンビルド）します。"""
    def log(msg):
        print(msg)
        if status_callback: status_callback(msg)

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
        
    log(f"🚨 [フル再構築] {market_name} ({interval}) 開始。総数: {len(tickers)}")
    
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
        if "date" in final_df.columns:
            final_df["is_finalized"] = final_df["date"].dt.date < now.date()
            if interval != "1d":
                final_df["is_finalized"] = final_df["date"] < (now - timedelta(hours=1))
        
        final_df = final_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        save_price_db(final_df, interval, is_jp=is_jp)
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
        
        new_df["is_finalized"] = new_df["date"].dt.date < now.date()
        if interval != "1d":
            new_df["is_finalized"] = new_df["date"] < (now - timedelta(hours=1))
            
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
    """指定された特定銘柄の1d〜1mすべての時間足を重複排除マージで安全修復します。"""
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

                old_df = db_df[db_df["ticker"] == pure_ticker].copy() if not db_df.empty else pd.DataFrame()
                new_df["is_finalized"] = True
                merged_df = merge_price_data(old_df, new_df, "1d", is_jp=is_jp, forced_split_ratio=forced_split_ratio)
                if not db_df.empty:
                    db_df = db_df[db_df["ticker"] != pure_ticker]
                db_df = pd.concat([db_df, merged_df], ignore_index=True)
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
                results[interval] = "新規データ空"
                continue
            new_df = parse_yfinance_batch(df_raw, [pure_ticker], is_jp=is_jp)
            if new_df.empty:
                results[interval] = "パース結果空"
                continue
            merged_df = merge_price_data(old_df, new_df, interval, forced_split_ratio=forced_split_ratio)
            if not db_df.empty:
                db_df = db_df[db_df["ticker"] != pure_ticker]
            db_df = pd.concat([db_df, merged_df], ignore_index=True)
            db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
            save_price_db(db_df, interval, is_jp=is_jp)
            results[interval] = f"修復成功 ({len(merged_df):,}件)"
        except Exception as e:
            results[interval] = f"エラー: {str(e)}"
    return results

# core/database_service.py より修正 (3/4)

def backward_scale_repair(df: pd.DataFrame, threshold: float = 0.35) -> tuple:
    """配信異常などによる価格の急変（崖・負の数値など）を検出し後ろ向きスケール調整します。"""
    if df.empty:
        return df, []
    df = df.sort_values("date").reset_index(drop=True)
    # price_cols の探索リストに "adj close" を追加
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

# core/database_service.py より修正 (1/2)

def scan_all_anomalies(is_jp: bool = True, interval: str = "1d", threshold: float = 0.35) -> pd.DataFrame:
    """全銘柄を対象に、ベクトル演算により瞬間的に異常価格（崖・負の値）を探索しリスト化します。"""
    try:
        db_df = load_price_db(interval, is_jp=is_jp)
    except FileNotFoundError:
        return pd.DataFrame()
    if db_df.empty:
        return pd.DataFrame()

    db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
    result_rows = []

    # チェック対象の列を決定（adj closeが存在すれば追加して両方検証）
    cols_to_check = ["close"]
    if "adj close" in db_df.columns:
        cols_to_check.append("adj close")

    for p_col in cols_to_check:
        col_label = " (Adj Close)" if p_col == "adj close" else ""
        
        # 1. 負の数チェック
        negative_mask = db_df[p_col] < 0
        shifted_neg_mask_for_pos = db_df.groupby("ticker")[p_col].apply(lambda x: (x < 0).shift(1, fill_value=True)).reset_index(level=0, drop=True)
        pos_to_neg = negative_mask & (~shifted_neg_mask_for_pos)
        shifted_neg_mask_for_neg = db_df.groupby("ticker")[p_col].apply(lambda x: (x < 0).shift(1, fill_value=False)).reset_index(level=0, drop=True)
        neg_to_pos = (~negative_mask) & shifted_neg_mask_for_neg
        boundary_mask = pos_to_neg | neg_to_pos
        
        if boundary_mask.any():
            boundary_rows = db_df[boundary_mask].copy()
            boundary_rows["before_close"] = db_df.groupby("ticker")[p_col].shift(1)[boundary_mask].values
            boundary_rows["after_close"] = boundary_rows[p_col]
            boundary_rows["pct_change"] = float("nan")
            boundary_rows["anomaly_type"] = f"負の株価（切り替え境界）{col_label}"
            result_rows.append(boundary_rows[["ticker", "date", "before_close", "after_close", "pct_change", "anomaly_type"]])

        # 2. 崖（急変）チェック
        abs_close = db_df[p_col].abs()
        pct = abs_close.groupby(db_df["ticker"]).pct_change()
        cliff_mask = pct.abs() >= threshold

        if cliff_mask.any():
            cliff_rows = db_df[cliff_mask].copy()
            cliff_rows["before_close"] = db_df.groupby("ticker")[p_col].shift(1)[cliff_mask].values
            cliff_rows["after_close"] = cliff_rows[p_col]
            cliff_rows["pct_change"] = pct[cliff_mask].values
            cliff_rows["anomaly_type"] = f"急変{col_label}（" + (pct[cliff_mask] * 100).round(1).astype(str) + "%）"
            result_rows.append(cliff_rows[["ticker", "date", "before_close", "after_close", "pct_change", "anomaly_type"]])

    if not result_rows:
        return pd.DataFrame()
    result = pd.concat(result_rows, ignore_index=True).rename(columns={"date": "cliff_date"})
    
    def aggregate_anomalies(group):
        types = " ＆ ".join(group["anomaly_type"].unique())
        pct_vals = group["pct_change"].dropna()
        pct_val = pct_vals.iloc[0] if not pct_vals.empty else float("nan")
        before_val = group["before_close"].dropna().iloc[0] if not group["before_close"].dropna().empty else float("nan")
        after_val = group["after_close"].dropna().iloc[0] if not group["after_close"].dropna().empty else float("nan")
        return pd.Series({"before_close": before_val, "after_close": after_val, "pct_change": pct_val, "anomaly_type": types})
        
    result = result.groupby(["ticker", "cliff_date"], as_index=False).apply(aggregate_anomalies)
    return result.sort_values(["ticker", "cliff_date"]).reset_index(drop=True)

# core/database_service.py より修正 (4/4)

def apply_scale_repair_with_intraday_propagation(ticker: str, is_jp: bool = True, threshold: float = 0.35, dry_run: bool = False) -> dict:
    """指定銘柄の日足異常を修復したうえで、その倍率を分足等の短期足データベースへ一挙遡及波及させます。"""
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
        # price_cols の探索リストに "adj close" を追加
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

# core/database_service.py より修正 (2/2)

def run_database_health_scan(is_jp: bool) -> list:
    """全タイムフレームのParquetデータベースを自動スキャンし、異常陥没・高騰・段差などを診断します。"""
    anomalies = []
    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            df = load_price_db(interval, is_jp=is_jp)
            if df.empty:
                continue
            df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
            
            # チェック対象カラムの動的決定
            cols_to_check = ["close"]
            if "adj close" in df.columns:
                cols_to_check.append("adj close")

            for p_col in cols_to_check:
                col_label = " (Adj Close)" if p_col == "adj close" else ""
                pct_col = f"pct_{p_col.replace(' ', '_')}"
                
                df[pct_col] = df.groupby("ticker")[p_col].pct_change()
                
                # 急激な陥没（-40%以下）または急騰（+50%以上）を検知
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
                        
                    pre_p = curr_p / (1.0 + pct_val)
                    if pct_val <= -0.40:
                        found_recovery = False
                        recovery_idx = -1
                        for j in range(1, n):
                            post_p = close_vals[j]
                            if (pre_p * 0.85) <= post_p <= (pre_p * 1.15):
                                found_recovery = True
                                recovery_idx = j
                                break
                        if found_recovery:
                            bug_end_date = dates[recovery_idx - 1]
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"🚨 クレーターバグ{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 {str(bug_end_date)[:16]}",
                                "異常値": f"{curr_p:.2f}", "前後価格": f"{pre_p:.2f} ➔ {close_vals[recovery_idx]:.2f}"
                            })
                        else:
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"📉 階段段差（未調整分割）{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 最新",
                                "異常値": f"前日: {pre_p:.1f} ➔ 当日: {curr_p:.1f}", "前後価格": f"{pre_p:.2f} ➔ {curr_p:.2f}"
                            })
                    elif pct_val >= 0.50:
                        found_recovery = False
                        recovery_idx = -1
                        for j in range(1, n):
                            post_p = close_vals[j]
                            if (pre_p * 0.85) <= post_p <= (pre_p * 1.15):
                                found_recovery = True
                                recovery_idx = j
                                break
                        if found_recovery:
                            bug_end_date = dates[recovery_idx - 1]
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"📈 タワーバグ{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 {str(bug_end_date)[:16]}",
                                "異常値": f"{curr_p:.2f}", "前後価格": f"{pre_p:.2f} ➔ {close_vals[recovery_idx]:.2f}"
                            })
                        else:
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"📈 階段段差（未調整併合）{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 最新",
                                "異常値": f"前日: {pre_p:.1f} ➔ 当日: {curr_p:.1f}", "前後価格": f"{pre_p:.2f} ➔ {curr_p:.2f}"
                            })
        except Exception:
            pass
    return anomalies