# core/calculator.py
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from config import settings
from data_access.local_db import load_price_db

def compute_sector_index_from_df(db_df: pd.DataFrame, tickers: list, period_days: int, resample_weekly: bool) -> pd.Series:
    """指定された複数のティッカーの等金額分散投資指数を計算します。"""
    if db_df.empty:
        return pd.Series(dtype=float)
    db_df = db_df.copy()
    db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
    end_date = db_df["date"].max()
    start_date = end_date - timedelta(days=period_days)
    target_df = db_df[(db_df["date"] >= start_date) & (db_df["ticker"].isin(tickers))].copy()
    if target_df.empty:
        return pd.Series(dtype=float)
    
    if resample_weekly:
        target_df = target_df.set_index("date")
        target_df = target_df.groupby("ticker").resample("W-FRI").agg({"close": "last"}).reset_index()
        
    close_pivot = target_df.pivot_table(index="date", columns="ticker", values="close")
    close_pivot = close_pivot.sort_index()
    daily_returns = close_pivot.pct_change()
    sector_return = daily_returns.mean(axis=1)
    index_series = (1 + sector_return).cumprod() * 100
    if len(index_series) > 0:
        index_series.iloc[0] = 100.0
    return index_series

def get_sector_momentum(index_series: pd.Series, days: int = 5) -> float:
    """直近指定日数における合成インデックスの騰落率(%)を計算します。"""
    if len(index_series) < 2:
        return 0.0
    recent = index_series.iloc[-min(days, len(index_series)):]
    if recent.iloc[0] == 0:
        return 0.0
    return float((recent.iloc[-1] / recent.iloc[0] - 1) * 100)

def get_benchmark_data(ticker: str, period_days: int, interval: str) -> pd.Series:
    """
    基準となるベンチマーク指数の累積リターン推移をDBまたはyfinanceから取得します。
    分割等のノイズによる突発的な急落（40%以上）を自動で排除して計算します。
    """
    try:
        is_jp = False
        pure_ticker = str(ticker).strip()
        if pure_ticker.upper().endswith(".T"):
            pure_ticker = pure_ticker[:-2]
            is_jp = True
        elif pure_ticker.isdigit():
            is_jp = True
            
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
        except Exception:
            db_df = pd.DataFrame()

        if not db_df.empty and "ticker" in db_df.columns:
            ticker_db = db_df[db_df["ticker"] == pure_ticker].copy()
            if not ticker_db.empty and len(ticker_db) >= 10:
                ticker_db["date"] = pd.to_datetime(ticker_db["date"]).dt.tz_localize(None)
                end_date = ticker_db["date"].max()
                start_date = end_date - timedelta(days=period_days + 365)
                ticker_db = ticker_db[ticker_db["date"] >= start_date].sort_values("date")
                
                if not ticker_db.empty:
                    close = ticker_db.set_index("date")["close"]
                    ret = close.pct_change()
                    idx = (1 + ret).cumprod() * 100
                    if len(idx) > 0: 
                        idx.iloc[0] = 100.0
                    return idx

        end = datetime.now()
        start = end - timedelta(days=period_days + 365)
        df_raw = yf.download(ticker, start=start.strftime("%Y-%m-%d"), interval=interval, auto_adjust=True, progress=False)
        if df_raw.empty: 
            return pd.Series(dtype=float)
        
        df_raw = df_raw.reset_index()
        df_raw.columns = [str(c).lower() if not isinstance(c, tuple) else str(c[0]).lower() for c in df_raw.columns]
        date_col = "date" if "date" in df_raw.columns else "datetime"
        df_raw = df_raw.rename(columns={date_col: "date"})
        df_raw["date"] = pd.to_datetime(df_raw["date"]).dt.tz_localize(None)
        close = df_raw.set_index("date")["close"].copy()
        
        if len(close) > 1:
            raw_pct = close.pct_change().copy()
            anomaly_mask = raw_pct <= -0.40
            if anomaly_mask.any():
                for idx_loc in raw_pct[anomaly_mask].index:
                    raw_pct.loc[idx_loc] = 0.0
            ret = raw_pct
        else:
            ret = close.pct_change()
            
        idx = (1 + ret).cumprod() * 100
        if len(idx) > 0: 
            idx.iloc[0] = 100.0
        return idx
    except Exception:
        return pd.Series(dtype=float)

def relativize_series(idx_series: pd.Series, bm_series: pd.Series) -> pd.Series:
    """基準指数に対する相対強度(RS)のインデックス推移を計算します。"""
    if bm_series is None or bm_series.empty:
        return idx_series
    bm_aligned = bm_series.reindex(idx_series.index, method='ffill')
    bm_aligned = bm_aligned.bfill()
    if bm_aligned.isna().all() or (bm_aligned == 0).all():
        return idx_series
    rel = idx_series / bm_aligned
    rel = rel / rel.iloc[0] * 100
    return rel

def compute_sector_absolute_data(db_df: pd.DataFrame, tickers: list, period_days: int, resample_weekly: bool) -> tuple:
    """指定された構成群から絶対価格平均、移動平均、WVF、合算売買代金などを一挙算出します。"""
    if db_df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=bool), pd.Series(dtype=float)
    db_df = db_df.copy()
    db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
    end_date = db_df["date"].max()
    fetch_start = end_date - timedelta(days=period_days + 365)
    target_df = db_df[(db_df["date"] >= fetch_start) & (db_df["ticker"].isin(tickers))].copy()
    if target_df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=bool), pd.Series(dtype=float)

    close_pivot = target_df.pivot_table(index="date", columns="ticker", values="close").sort_index()
    volume_pivot = target_df.pivot_table(index="date", columns="ticker", values="volume").sort_index()

    if resample_weekly:
        close_pivot = close_pivot.resample("W-FRI").last()
        volume_pivot = volume_pivot.resample("W-FRI").sum()

    sector_abs = close_pivot.mean(axis=1)
    trading_val = (close_pivot * volume_pivot).sum(axis=1)

    sma75  = sector_abs.rolling(window=75).mean()
    sma200 = sector_abs.rolling(window=200).mean()

    highest_close = sector_abs.rolling(window=11).max()
    wvf = (highest_close - sector_abs) / highest_close * 100
    wvf_std   = wvf.rolling(window=20).std(ddof=0)
    wvf_mid   = wvf.rolling(window=20).mean()
    wvf_upper = wvf_mid + (2.0 * wvf_std)
    range_high = wvf.rolling(window=100).max() * 0.85
    is_wvf_lit = (wvf >= wvf_upper) | (wvf >= range_high)

    display_start = end_date - timedelta(days=period_days)
    sector_abs = sector_abs[sector_abs.index >= display_start]
    sma75      = sma75[sma75.index           >= display_start]
    sma200     = sma200[sma200.index         >= display_start]
    is_wvf_lit = is_wvf_lit[is_wvf_lit.index >= display_start]
    trading_val = trading_val[trading_val.index >= display_start]

    return sector_abs, sma75, sma200, is_wvf_lit, trading_val

def compute_macro_cores_from_db(db_df: pd.DataFrame, period_days: int, resample_weekly: bool = False) -> dict:
    """TOPIX-17業種データから、5大コアセクターの累積騰落指標を数学的に合成算出します。"""
    all_etfs = [t for etfs in settings.TOPIX17_ETF_MAPPING.values() for t in etfs]
    etf_df = pd.DataFrame()
    if not db_df.empty:
        db_df = db_df.copy()
        db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
        end_date = db_df["date"].max()
        start_date = end_date - timedelta(days=period_days + 30)
        etf_df = db_df[(db_df["date"] >= start_date) & (db_df["ticker"].isin(all_etfs))].copy()
        
    if etf_df.empty or len(etf_df["ticker"].unique()) < 10:
        symbols = [f"{t}.T" for t in all_etfs]
        end = datetime.now()
        start = end - timedelta(days=period_days + 30)
        try:
            df_raw = yf.download(symbols, start=start.strftime("%Y-%m-%d"), progress=False)
            if not df_raw.empty:
                df_raw = df_raw.xs("Close", axis=1, level=0) if isinstance(df_raw.columns, pd.MultiIndex) else df_raw
                df_raw = df_raw.reset_index()
                rows = []
                for _, row in df_raw.iterrows():
                    dt = row["Date"]
                    for sym in symbols:
                        val = row[sym]
                        if not pd.isna(val):
                            rows.append({"date": dt, "ticker": sym.replace(".T", ""), "close": val})
                etf_df = pd.DataFrame(rows)
        except Exception:
            pass

    if etf_df.empty:
        return {}
        
    close_pivot = etf_df.pivot_table(index="date", columns="ticker", values="close").sort_index()
    if resample_weekly:
        close_pivot = close_pivot.resample("W-FRI").last()
        
    display_start = close_pivot.index.max() - timedelta(days=period_days)
    close_pivot = close_pivot[close_pivot.index >= display_start]
    
    returns_df = close_pivot.pct_change()
    macro_cores = {}
    for core_name, tickers in settings.TOPIX17_ETF_MAPPING.items():
        existing_tickers = [t for t in tickers if t in returns_df.columns]
        if not existing_tickers:
            continue
        core_return = returns_df[existing_tickers].mean(axis=1)
        core_index = (1 + core_return).cumprod() * 100
        if len(core_index) > 0:
            core_index.iloc[0] = 100.0
        macro_cores[core_name] = core_index
        
    return macro_cores