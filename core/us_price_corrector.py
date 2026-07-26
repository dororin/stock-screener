# core/us_price_corrector.py

import os
import json
import time
from datetime import datetime, timedelta, time as dt_time
import pandas as pd
import numpy as np
import yfinance as yf
from config import settings
from data_access.local_db import load_price_db, save_price_db

try:
    import streamlit as st
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

# --- TradingView Client 取得用ユーティリティ（米国株専用） ---
_TV_CLIENT_FALLBACK = None

def _create_tv_client_instance():
    try:
        from tvDatafeed import TvDatafeed
        return TvDatafeed()
    except Exception:
        return None

if HAS_STREAMLIT:
    @st.cache_resource(show_spinner=False)
    def _get_tv_client_cached():
        return _create_tv_client_instance()

def _get_tv_client():
    if HAS_STREAMLIT:
        return _get_tv_client_cached()
    global _TV_CLIENT_FALLBACK
    if _TV_CLIENT_FALLBACK is None:
        _TV_CLIENT_FALLBACK = _create_tv_client_instance()
    return _TV_CLIENT_FALLBACK

# 米国株インデックスのマッピング定義
US_INDEX_TICKER_TV_MAP = {
    "^GSPC": {"symbol": "SPX", "exchange": "TVC"},
    "^NDX": {"symbol": "NDX", "exchange": "NASDAQ"},
    "^DJI": {"symbol": "DJI", "exchange": "TVC"},
}

def map_ticker_to_tv_symbol(ticker: str) -> dict:
    raw_ticker = str(ticker).strip()
    if raw_ticker in US_INDEX_TICKER_TV_MAP:
        return US_INDEX_TICKER_TV_MAP[raw_ticker]

    # 個別株のサニタイズ（USはドットに変換してNASDAQ/NYSEを参照）
    pure_ticker = raw_ticker.replace("-", ".") if "-" in raw_ticker else raw_ticker
    return {"symbol": pure_ticker, "exchange": None}


# --- 1. yfinance米国株バッチデータパース（US専用） ---
def parse_yfinance_batch(df_raw: pd.DataFrame, chunk_tickers: list) -> pd.DataFrame:
    """yfinanceの生バッチ出力を米国株（US）前提でパースします。"""
    if df_raw.empty:
        return pd.DataFrame()
    all_rows = []
    is_multi = isinstance(df_raw.columns, pd.MultiIndex)
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
                start_time = dt_time(9, 30)  # 米国市場開場
                end_time = dt_time(16, 0)    # 米国市場閉場
                
                is_intraday = not (times == dt_time(0, 0)).all()
                if is_intraday:
                    t_df = t_df[(times >= start_time) & (times <= end_time)]
            
            target_cols = ["date", "ticker", "open", "high", "low", "close", "adj close", "volume", "stock splits", "dividends"]
            valid_cols = [c for c in target_cols if c in t_df.columns]
            return t_df[valid_cols]
        else:
            return pd.DataFrame()
            
    for ticker in chunk_tickers:
        symbol = ticker
        try:
            if symbol in df_raw.columns.get_level_values(1):
                t_df = df_raw.xs(symbol, axis=1, level=1).copy()
            elif symbol in df_raw.columns.get_level_values(0):
                t_df = df_raw[symbol].copy()
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
                start_time = dt_time(9, 30)
                end_time = dt_time(16, 0)
                
                is_intraday = not (times == dt_time(0, 0)).all()
                if is_intraday:
                    t_df = t_df[(times >= start_time) & (times <= end_time)]
            
            target_cols = ["date", "ticker", "open", "high", "low", "close", "adj close", "volume", "stock splits", "dividends"]
            valid_cols = [c for c in target_cols if c in t_df.columns]
            all_rows.append(t_df[valid_cols])
        except Exception:
            continue
            
    return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()


# --- 2. 株式分割補正ロジック（US専用） ---
def adjust_ticker_splits_backward_in_memory(df_ticker: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """メモリ上で配信された株式分割情報に基づき過去データを修正します。"""
    applied_splits = []
    
    if df_ticker.empty or len(df_ticker) < 2:
        return df_ticker, applied_splits
        
    df = df_ticker.sort_values("date").reset_index(drop=True)
    price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df.columns]
    
    if "split_multiplier" not in df.columns:
        df["split_multiplier"] = 1.0
    if "patched_multiplier" not in df.columns:
        df["patched_multiplier"] = 1.0
    
    if "stock splits" in df.columns:
        split_events = df[(df["stock splits"] > 0) & (df["stock splits"] != 1.0)]
        if not split_events.empty:
            for idx in sorted(split_events.index, reverse=True):
                if idx == 0:
                    continue
                
                split_val = df.loc[idx, "stock splits"]
                if split_val <= 0:
                    continue
                
                pre_close = df.loc[idx - 1, "close"]
                post_close = df.loc[idx, "close"]
                
                if pd.isna(pre_close) or pd.isna(post_close) or pre_close <= 0 or post_close <= 0:
                    continue
                
                actual_ratio = pre_close / post_close
                unadjusted_mask = (df.index < idx) & (df["split_multiplier"] == 1.0)
                
                # 比率の自動判別と遡及補正
                dist_to_split = abs(actual_ratio - split_val)
                dist_to_flat = abs(actual_ratio - 1.0)
                
                if unadjusted_mask.any() and (dist_to_split < dist_to_flat) and (dist_to_split / split_val <= 0.15):
                    ratio = 1.0 / split_val
                    for col in price_cols:
                        df.loc[unadjusted_mask, col] = df.loc[unadjusted_mask, col] * ratio
                    if "volume" in df.columns:
                        df.loc[unadjusted_mask, "volume"] = df.loc[unadjusted_mask, "volume"] / ratio
                        
                    df.loc[unadjusted_mask, "split_multiplier"] = ratio
                    
                    applied_splits.append({
                        "date": df.loc[idx, "date"],
                        "ratio": split_val
                    })

    return df, applied_splits


# --- 3. 保存パッチ適用ロジック（US専用） ---
def apply_saved_patches_to_df(df: pd.DataFrame, repair_log_df: pd.DataFrame = None) -> pd.DataFrame:
    """米国株用の保存されたパッチ定義（repair_log）を適用します。"""
    log_df = repair_log_df
    if log_df is None:
        try:
            from data_access.sheets_api import load_repair_log_from_sheets
            log_df = load_repair_log_from_sheets()
        except Exception:
            return df

    if log_df is None or log_df.empty:
        return df

    df_result = df.copy()
    
    if "patched_multiplier" not in df_result.columns:
        df_result["patched_multiplier"] = 1.0
    if "split_multiplier" not in df_result.columns:
        df_result["split_multiplier"] = 1.0
    
    log_df["parsed_date"] = pd.to_datetime(log_df["cliff_date"], errors="coerce")
    log_df = log_df.dropna(subset=["parsed_date"]).sort_values("parsed_date", ascending=False)

    for _, row in log_df.iterrows():
        # 米国株パッチのみに限定適用
        if str(row.get("market", "")).strip().upper() != "US":
            continue
            
        ticker = str(row.get("ticker", "")).strip()
        cliff_date = row["parsed_date"]
        try:
            multiplier = float(row.get("multiplier", 1.0))
            if multiplier <= 0 or multiplier == 1.0:
                continue
        except ValueError:
            continue

        ticker_mask = df_result["ticker"] == ticker
        if not ticker_mask.any():
            continue

        df_result["date_dt"] = pd.to_datetime(df_result["date"])
        pre_mask = ticker_mask & (df_result["date_dt"] <= cliff_date) & (df_result["patched_multiplier"] == 1.0)
        
        if pre_mask.any():
            price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df_result.columns]
            for col in price_cols:
                df_result.loc[pre_mask, col] = df_result.loc[pre_mask, col] * multiplier
            if "volume" in df_result.columns:
                df_result.loc[pre_mask, "volume"] = df_result.loc[pre_mask, "volume"] / multiplier
            
            df_result.loc[pre_mask, "patched_multiplier"] = multiplier
        
        df_result = df_result.drop(columns=["date_dt"])

    return df_result


# --- 4. 手動ピンポイントパッチ適用・検証（US専用） ---
def test_forced_scale_patch_in_memory(ticker: str, patch_date_str: str, multiplier: float) -> tuple[dict, dict]:
    """特定米国株銘柄に対するパッチ適用テストをメモリ上で実行します（保存なし）。"""
    if multiplier <= 0:
        return {"error": "倍率に0以下の数値は指定できません。"}, {}

    pure_ticker = str(ticker).strip().upper()
    try:
        target_dt = pd.to_datetime(patch_date_str)
    except Exception as e:
        return {"error": f"要補正Close日時のパースに失敗しました: {e}"}, {}

    test_results = {}
    temp_repaired_dfs = {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            db_df = load_price_db(interval, is_jp=False, is_raw=False)
        except FileNotFoundError:
            continue
            
        if db_df.empty:
            continue

        mask = db_df["ticker"] == pure_ticker
        ticker_data = db_df[mask].copy()
        if ticker_data.empty:
            continue

        if "patched_multiplier" not in ticker_data.columns:
            ticker_data["patched_multiplier"] = 1.0

        ticker_data["date_dt"] = pd.to_datetime(ticker_data["date"])
        pre_mask = (ticker_data["date_dt"] <= target_dt) & (ticker_data["patched_multiplier"] == 1.0)
        
        if not pre_mask.any():
            continue

        applied_count = pre_mask.sum()
        before_ticker_data = ticker_data.copy()
        
        price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in ticker_data.columns]
        for col in price_cols:
            ticker_data.loc[pre_mask, col] = ticker_data.loc[pre_mask, col] * multiplier
        if "volume" in ticker_data.columns:
            ticker_data.loc[pre_mask, "volume"] = ticker_data.loc[pre_mask, "volume"] / multiplier

        ticker_data.loc[pre_mask, "patched_multiplier"] = multiplier

        adjusted_idx = ticker_data[pre_mask].index
        unadjusted_idx = ticker_data[~pre_mask].index
        sample_indices = list(adjusted_idx[-5:]) + list(unadjusted_idx[:5])
        sample_indices = [idx for idx in sample_indices if idx in ticker_data.index]
        
        before_sample = before_ticker_data.loc[sample_indices].drop(columns=["date_dt"], errors="ignore")
        after_sample = ticker_data.loc[sample_indices].drop(columns=["date_dt"], errors="ignore")

        if "date" in before_sample.columns:
            before_sample = before_sample.sort_values("date")
            after_sample = after_sample.sort_values("date")

        test_results[interval] = {
            "applied_count": applied_count,
            "before_sample": before_sample,
            "after_sample": after_sample
        }

        repaired_ticker_data = ticker_data.drop(columns=["date_dt"], errors="ignore")
        full_repaired_df = db_df[~mask].copy()
        full_repaired_df = pd.concat([full_repaired_df, repaired_ticker_data], ignore_index=True)
        full_repaired_df = full_repaired_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        
        temp_repaired_dfs[interval] = full_repaired_df

    if not test_results:
        return {"error": "対象銘柄または適用可能な未調整データが見つかりませんでした。"}, {}

    return test_results, temp_repaired_dfs


def apply_forced_scale_patch_to_all_timeframes(ticker: str, patch_date: str, multiplier: float) -> dict:
    """特定の米国株銘柄について、指定日以前の全時間足にパッチを適用し保存します。"""
    if multiplier <= 0:
        return {"error": f"処理を中断しました。倍率に 0 以下の数値は指定できません。"}

    pure_ticker = str(ticker).strip().upper()
    results = {}
    try:
        target_dt = pd.to_datetime(patch_date)
    except Exception as e:
        return {"error": f"要補正Close日時のパース失敗: {e}"}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            db_df = load_price_db(interval, is_jp=False, is_raw=False)
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

        if "patched_multiplier" not in ticker_data.columns:
            ticker_data["patched_multiplier"] = 1.0

        ticker_data["date_dt"] = pd.to_datetime(ticker_data["date"])
        pre_mask = (ticker_data["date_dt"] <= target_dt) & (ticker_data["patched_multiplier"] == 1.0)
        
        if not pre_mask.any():
            results[interval] = "スキップ（既に調整済み、または対象期間のデータなし）"
            continue

        price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in db_df.columns]
        for col in price_cols:
            ticker_data.loc[pre_mask, col] = ticker_data.loc[pre_mask, col] * multiplier
        if "volume" in db_df.columns:
            ticker_data.loc[pre_mask, "volume"] = ticker_data.loc[pre_mask, "volume"] / multiplier

        ticker_data.loc[pre_mask, "patched_multiplier"] = multiplier
        ticker_data = ticker_data.drop(columns=["date_dt"])

        db_df = db_df[~mask]
        db_df = pd.concat([db_df, ticker_data], ignore_index=True)
        db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        save_price_db(db_df, interval, is_jp=False, is_raw=False)
        results[interval] = f"{pre_mask.sum()}件補正適用完了"
    return results


# --- 5. TradingView 照合付き自動異常検出（US専用） ---
def scan_all_anomalies(interval: str = "1d", threshold: float = 0.35) -> pd.DataFrame:
    """米国株データベースの価格急変点（崖・不具合）を自動スキャンします。"""
    try:
        db_df = load_price_db(interval, is_jp=False, is_raw=False) 
    except FileNotFoundError:
        return pd.DataFrame()
    if db_df.empty:
        return pd.DataFrame()

    db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
    has_adj = "adj close" in db_df.columns
    result_rows = []

    # マイナス転換の検出
    negative_mask = db_df["close"] < 0
    shifted_neg_mask_for_pos = db_df.groupby("ticker")["close"].apply(lambda x: (x < 0).shift(1, fill_value=True)).reset_index(level=0, drop=True)
    pos_to_neg = negative_mask & (~shifted_neg_mask_for_pos)
    shifted_neg_mask_for_neg = db_df.groupby("ticker")["close"].apply(lambda x: (x < 0).shift(1, fill_value=False)).reset_index(level=0, drop=True)
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

    # 段差価格急変（崖）の検出
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
    result["patch_date"] = pd.to_datetime(result["before_date"])
    return result.sort_values(["ticker", "cliff_date"]).reset_index(drop=True)


def fetch_tv_close_pair(ticker: str, patch_date) -> dict:
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

    mapped = map_ticker_to_tv_symbol(ticker)
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


def scan_and_diagnose_cliffs_with_tv(intervals: list = None) -> pd.DataFrame:
    """米国株データベース全体を自動スキャンし、TradingViewの正しい終値データと自動照合を行います。"""
    target_intervals = intervals if intervals else list(settings.TIMEFRAMES)
    per_interval_dfs = {}
    for iv in target_intervals:
        df_iv = scan_all_anomalies(interval=iv)
        if not df_iv.empty:
            df_iv = df_iv.copy()
            df_iv["interval"] = iv
        per_interval_dfs[iv] = df_iv

    if "1d" in per_interval_dfs and not per_interval_dfs["1d"].empty:
        df_1d = per_interval_dfs["1d"]
        daily_flagged_pairs = set(
            zip(df_1d["ticker"], pd.to_datetime(df_1d["cliff_date"]).dt.normalize())
        )
        
        for iv in target_intervals:
            if iv == "1d" or per_interval_dfs.get(iv) is None or per_interval_dfs[iv].empty:
                continue
            df_iv = per_interval_dfs[iv]
            iv_dates = pd.to_datetime(df_iv["cliff_date"]).dt.normalize()
            iv_pairs = list(zip(df_iv["ticker"], iv_dates))
            keep_mask = [pair not in daily_flagged_pairs for pair in iv_pairs]
            per_interval_dfs[iv] = df_iv[keep_mask].reset_index(drop=True)

    non_empty = [df for df in per_interval_dfs.values() if df is not None and not df.empty]
    if not non_empty:
        return pd.DataFrame()

    result = pd.concat(non_empty, ignore_index=True)
    result["tv_close"] = float("nan")       
    result["tv_after_close"] = float("nan")  
    result["true_multiplier"] = float("nan")

    for idx, row in result.iterrows():
        interval = row["interval"]
        ticker = row["ticker"]
        patch_date = row.get("patch_date")
        before_close = row.get("before_close", float("nan"))
        after_close = row.get("after_close", float("nan"))

        if interval != "1d" or pd.isna(patch_date):
            result.at[idx, "true_multiplier"] = row.get("est_multiplier", float("nan"))
            continue

        tv_pair = fetch_tv_close_pair(ticker, patch_date)
        tv_before_close = tv_pair.get("tv_before_close")
        tv_after_close = tv_pair.get("tv_after_close")

        if tv_before_close is not None:
            result.at[idx, "tv_close"] = tv_before_close
        if tv_after_close is not None:
            result.at[idx, "tv_after_close"] = tv_after_close

        if tv_before_close is not None and pd.notna(before_close) and before_close != 0:
            result.at[idx, "true_multiplier"] = tv_before_close / before_close
        elif (
            tv_before_close is not None and tv_after_close is not None and tv_before_close != 0
            and pd.notna(before_close) and pd.notna(after_close) and before_close != 0
        ):
            tv_ratio = tv_after_close / tv_before_close
            self_ratio = after_close / before_close
            if self_ratio != 0:
                result.at[idx, "true_multiplier"] = tv_ratio / self_ratio
        else:
            result.at[idx, "true_multiplier"] = row.get("est_multiplier", float("nan"))

    drop_cols = [c for c in ["open", "high", "low", "pct_change"] if c in result.columns]
    result = result.drop(columns=drop_cols)
    return result.sort_values(["ticker", "cliff_date", "interval"]).reset_index(drop=True)


def apply_bulk_selected_patches(patches: list, status_callback=None) -> dict:
    """チェックボックスで選択されたパッチを、対象米国株に一括本番適用します。"""
    def log(msg):
        if status_callback:
            status_callback(msg)

    repaired_count = 0
    skipped_count = 0
    log_rows = []

    for patch in patches:
        ticker = patch.get("ticker")
        patch_date = patch.get("patch_date")
        multiplier = patch.get("multiplier")

        if not ticker or not patch_date or multiplier is None or pd.isna(multiplier) or multiplier <= 0:
            log(f"⚠️ [{ticker}] 真の倍率が取得できていないため、スキップしました。")
            skipped_count += 1
            continue

        pure_t = str(ticker).strip().upper()
        try:
            patch_dt_str = pd.to_datetime(patch_date).strftime("%Y-%m-%d")
        except Exception:
            log(f"⚠️ [{ticker}] 要補正Close日時が不正なためスキップしました。")
            skipped_count += 1
            continue

        log(f"🔧 [{pure_t}] {patch_dt_str}（要補正Close日時）以前の一括修復パッチを判定・適用中（倍率: {multiplier:.6f}）...")
        results = apply_forced_scale_patch_to_all_timeframes(pure_t, patch_dt_str, multiplier)
        applied_intervals = [iv for iv, msg in results.items() if "補正適用完了" in str(msg)]

        if applied_intervals:
            repaired_count += 1
            log(f"   ✅ 適用完了 ({', '.join(applied_intervals)})")
            log_rows.append({
                "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ticker": pure_t,
                "market": "US",
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


# --- 6. TradingView 終値自動照合・最新値確定（US専用） ---
def finalize_latest_with_tradingview_in_df(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """最新日の未確定バーについて、TradingViewから正しいデータをオンライン取得して上書き確定させます。"""
    if df.empty:
        return df
        
    try:
        from tradingview_screener import Query
        tickers = df["ticker"].unique().tolist()
        if not tickers:
            return df
            
        tv_symbols = []
        symbol_to_ticker = {}
        for t in tickers:
            clean_t = t.replace("-", ".")
            us_exchanges = {
                "AAPL": "NASDAQ", "MSFT": "NASDAQ", "NVDA": "NASDAQ", "GOOGL": "NASDAQ", "META": "NASDAQ",
                "AMZN": "NASDAQ", "AMD": "NASDAQ", "AVGO": "NASDAQ", "QCOM": "NASDAQ", "MU": "NASDAQ",
                "INTC": "NASDAQ", "TSLA": "NASDAQ", "NFLX": "NASDAQ",
                "JPM": "NYSE", "BAC": "NYSE", "GS": "NYSE", "MS": "NYSE", "WFC": "NYSE",
                "BRK.B": "NYSE", "BRK-B": "NYSE", "JNJ": "NYSE", "UNH": "NYSE", "LLY": "NYSE",
                "ABBV": "NYSE", "MRK": "NYSE", "XOM": "NYSE", "CVX": "NYSE", "COP": "NYSE",
                "SLB": "NYSE", "EOG": "NYSE", "HD": "NYSE", "MCD": "NYSE", "NKE": "NYSE",
                "T": "NYSE", "VZ": "NYSE", "NEE": "NYSE", "DUK": "NYSE", "SO": "NYSE",
                "AEP": "NYSE", "D": "NYSE", "LIN": "NYSE", "APD": "NYSE", "FCX": "NYSE",
                "NEM": "NYSE", "DOW": "NYSE"
            }
            exc = us_exchanges.get(clean_t, "NASDAQ")
            tv_sym = f"{exc}:{clean_t}"
            tv_symbols.append(tv_sym)
            symbol_to_ticker[tv_sym] = t
            symbol_to_ticker[t.replace("-", ".")] = t

        total_rows, tv_df = Query().set_tickers(*tv_symbols).select('open', 'high', 'low', 'close', 'volume').get_scanner_data()
        if tv_df.empty:
            return df
        
        tv_data = {}
        for idx, r in tv_df.iterrows():
            tv_ticker_symbol = r.get("ticker", idx)
            if not isinstance(tv_ticker_symbol, str):
                continue
                
            ticker_key = symbol_to_ticker.get(tv_ticker_symbol)
            if not ticker_key:
                clean_idx = tv_ticker_symbol.split(":")[-1] if ":" in tv_ticker_symbol else tv_ticker_symbol
                ticker_key = symbol_to_ticker.get(clean_idx)
                
            if ticker_key:
                tv_data[ticker_key] = {
                    "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                    "close": float(r["close"]), "volume": float(r["volume"])
                }

        df = df.copy()
        df["date_dt"] = pd.to_datetime(df["date"])
        
        latest_day = df["date_dt"].dt.normalize().max()
        latest_day_mask = df["date_dt"].dt.normalize() == latest_day
        
        for ticker, data in tv_data.items():
            ticker_today_mask = latest_day_mask & (df["ticker"] == ticker)
            if not ticker_today_mask.any():
                continue
                
            if interval == "1d":
                df.loc[ticker_today_mask, "open"] = data["open"]
                df.loc[ticker_today_mask, "high"] = data["high"]
                df.loc[ticker_today_mask, "low"] = data["low"]
                df.loc[ticker_today_mask, "close"] = data["close"]
                df.loc[ticker_today_mask, "volume"] = data["volume"]
                if "adj close" in df.columns:
                    df.loc[ticker_today_mask, "adj close"] = data["close"]
                    
            else:
                ticker_today_rows = df[ticker_today_mask]
                max_timestamp = ticker_today_rows["date_dt"].max()
                
                last_bar_mask = ticker_today_mask & (df["date_dt"] == max_timestamp)
                previous_bars_mask = ticker_today_mask & (df["date_dt"] < max_timestamp)
                
                previous_vol_sum = df.loc[previous_bars_mask, "volume"].sum()
                calculated_vol = max(0.0, data["volume"] - previous_vol_sum)
                
                existing_high = df.loc[last_bar_mask, "high"].values[0] if not df.loc[last_bar_mask, "high"].empty else data["high"]
                existing_low = df.loc[last_bar_mask, "low"].values[0] if not df.loc[last_bar_mask, "low"].empty else data["low"]
                
                df.loc[last_bar_mask, "close"] = data["close"]
                df.loc[last_bar_mask, "high"] = max(existing_high, data["close"])
                df.loc[last_bar_mask, "low"] = min(existing_low, data["close"])
                df.loc[last_bar_mask, "volume"] = calculated_vol
                if "adj close" in df.columns:
                    df.loc[last_bar_mask, "adj close"] = data["close"]

        df = df.drop(columns=["date_dt"])
        return df
    except Exception:
        return df