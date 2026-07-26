# core/calculator.py
import pandas as pd
import numpy as np
import streamlit as st  # キャッシュ処理 st.cache_data のためにインポート
from datetime import datetime, timedelta
from config import settings
from data_access.local_db import load_price_db

@st.cache_data(ttl=3600)
def fetch_proxy_market_value(proxy_ticker: str, start_date: datetime, end_date: datetime, db_df: pd.DataFrame = None) -> pd.Series:
    """
    市場全体の総売買代金の代理（プロキシ）として、1306 や SPY の
    時系列データを取得します。db_dfが渡された場合はそのロード済みメモリデータを再利用し、
    渡されない場合はローカルDBから取得します（外部通信なし）。
    """
    try:
        pure_ticker = str(proxy_ticker).strip().upper()
        is_jp = True
        if pure_ticker in ["SPY", "SPX", "^GSPC"]:
            is_jp = False
        
        clean_ticker = pure_ticker
        if is_jp and clean_ticker.endswith(".T"):
            clean_ticker = clean_ticker[:-2]
        
        # [改修：親一括ロードデータの再利用]
        if db_df is not None and not db_df.empty:
            df_db = db_df
        else:
            # 渡されていない場合のみ、フォールバックとして1d Active DBからロード
            df_db = load_price_db("1d", is_jp=is_jp, is_raw=False)

        if df_db.empty:
            return pd.Series(dtype=float)
        
        df_ticker = df_db[df_db["ticker"] == clean_ticker].copy()
        if df_ticker.empty:
            return pd.Series(dtype=float)
        
        df_ticker["date"] = pd.to_datetime(df_ticker["date"]).dt.tz_localize(None)
        df_ticker = df_ticker.set_index("date").sort_index()
        
        # 期間スライス
        df_sliced = df_ticker.loc[start_date:end_date]
        if df_sliced.empty:
            return pd.Series(dtype=float)
            
        # 売買代金（価格 * 出来高）の算出
        return df_sliced["close"] * df_sliced["volume"]
    except Exception:
        return pd.Series(dtype=float)

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
    基準となるベンチマーク指数の累積リターン推移をローカルDBから取得します（外部通信なし）。
    """
    try:
        is_jp = False
        pure_ticker = str(ticker).strip().upper()
        if pure_ticker.endswith(".T"):
            pure_ticker = pure_ticker[:-2]
            is_jp = True
        elif pure_ticker.isdigit():
            is_jp = True
        elif pure_ticker in ["^N225", "1306"]:
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
                    
                    # 急落異常値のクリップ処理
                    anomaly_mask = ret <= -0.40
                    if anomaly_mask.any():
                        for idx_loc in ret[anomaly_mask].index:
                            ret.loc[idx_loc] = 0.0
                            
                    idx = (1 + ret).cumprod() * 100
                    if len(idx) > 0: 
                        idx.iloc[0] = 100.0
                    return idx

        return pd.Series(dtype=float)
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

def compute_sector_absolute_data(db_df: pd.DataFrame, tickers: list, period_days: int, resample_weekly: bool, interval: str = "1d", is_jp: bool = True) -> tuple:
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
    """TOPIX-17業種データから、5大コアセクターの累積騰落指標を数学的に合成算出します（ローカルDBのみ）。"""
    all_etfs = [t for etfs in settings.TOPIX17_ETF_MAPPING.values() for t in etfs]
    etf_df = pd.DataFrame()
    if not db_df.empty:
        db_df = db_df.copy()
        db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
        end_date = db_df["date"].max()
        start_date = end_date - timedelta(days=period_days + 30)
        etf_df = db_df[(db_df["date"] >= start_date) & (db_df["ticker"].isin(all_etfs))].copy()
        
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

def compute_theme_equal_weighted_return_rate(
    db_df: pd.DataFrame, 
    tickers: list, 
    period_days: int, 
    resample_weekly: bool,
    is_jp: bool = True
) -> tuple:
    """
    指定された構成銘柄（等金額投資）の、基準日からのリターン率（％）を計算します。
    """
    if db_df.empty or not tickers:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), []
        
    db_df = db_df.copy()
    db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
    
    end_date = db_df["date"].max()
    fetch_start = end_date - timedelta(days=period_days + 365)
    
    target_df = db_df[(db_df["date"] >= fetch_start) & (db_df["ticker"].isin(tickers))].copy()
    if target_df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), []

    if "volume" in target_df.columns:
        target_df["val"] = target_df["close"] * target_df["volume"]
    else:
        target_df["val"] = 0.0

    close_pivot = target_df.pivot_table(index="date", columns="ticker", values="close").sort_index()
    val_pivot = target_df.pivot_table(index="date", columns="ticker", values="val").sort_index()

    if resample_weekly:
        close_pivot = close_pivot.resample("W-FRI").last().ffill()
        val_pivot = val_pivot.resample("W-FRI").sum().fillna(0)

    if close_pivot.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), []

    display_start = end_date - timedelta(days=period_days)
    display_close = close_pivot[close_pivot.index >= display_start]
    display_val = val_pivot[val_pivot.index >= display_start]
    
    if display_close.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), []

    base_prices = display_close.bfill().iloc[0]
    base_prices = base_prices.apply(lambda x: x if (pd.notna(x) and x > 0) else np.nan)

    all_normalized = close_pivot.div(base_prices) * 100.0 - 100.0
    all_index_series = all_normalized.mean(axis=1)
    return_rate_series = all_index_series[all_index_series.index >= display_start]
    
    sma75 = all_index_series.rolling(window=75, min_periods=1).mean()
    sma200 = all_index_series.rolling(window=200, min_periods=1).mean()

    sma75 = sma75[sma75.index >= display_start]
    sma200 = sma200[sma200.index >= display_start]

    # 4ステージ出来高マトリクスの算出
    proxy_ticker = "1306.T" if is_jp else "SPY"
    
    # [改修：親側からロード済みの db_df を引き渡し、多重Parquetロードを完全に回避]
    proxy_m_val = fetch_proxy_market_value(proxy_ticker, fetch_start, end_date, db_df=db_df)
    
    if resample_weekly and not proxy_m_val.empty:
        proxy_m_val = proxy_m_val.resample("W-FRI").sum()

    vdr_df = pd.DataFrame(index=val_pivot.index)
    for col in val_pivot.columns:
        med = val_pivot[col].shift(1).rolling(window=25, min_periods=5).median()
        vdr_df[col] = val_pivot[col] / med.replace(0, np.nan)
    
    theme_vdr = vdr_df.mean(axis=1).fillna(1.0)

    theme_total_val = val_pivot.sum(axis=1)
    if not proxy_m_val.empty:
        proxy_m_val_aligned = proxy_m_val.reindex(theme_total_val.index, method="ffill").bfill()
        theme_vs = (theme_total_val / proxy_m_val_aligned.replace(0, np.nan)) * 100.0
    else:
        theme_vs = theme_total_val / 1e6

    theme_vs_ma = theme_vs.rolling(window=20, min_periods=1).mean()

    display_vdr = theme_vdr[theme_vdr.index >= display_start]
    display_vs = theme_vs[theme_vs.index >= display_start]
    display_vs_ma = theme_vs_ma[theme_vs_ma.index >= display_start]

    lwc_volume_data = []
    for dt, vdr, vs, vs_ma in zip(display_vs.index, display_vdr, display_vs, display_vs_ma):
        if pd.isna(vs):
            continue
        
        if vdr >= 1.5 and vs >= vs_ma:
            color = "rgba(239, 83, 80, 0.85)"      # ステージA: 赤
        elif vdr < 1.5 and vs >= vs_ma:
            color = "rgba(38, 166, 154, 0.60)"     # ステージB: 緑
        elif vdr < 1.5 and vs < vs_ma:
            color = "rgba(128, 128, 128, 0.25)"    # ステージC: 灰
        else:
            color = "rgba(255, 167, 38, 0.50)"     # ステージD: 黄

        lwc_volume_data.append({
            "time": str(dt)[:10],
            "value": float(round(vs, 4)),
            "color": color
        })

    return return_rate_series, sma75, sma200, lwc_volume_data