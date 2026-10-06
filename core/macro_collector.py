# core/macro_collector.py

import pandas as pd
import numpy as np
import yfinance as yf
import streamlit as st
from config import settings

PERIOD_MAPPING = {
    "1ヶ月": "1mo",
    "3ヶ月": "3mo",
    "6ヶ月": "6mo",
    "1年": "1y",
    "3年": "5y",
    "全期間": "max"
}

@st.cache_data(ttl=600, show_spinner=False)
def fetch_macro_market_data_ondemand(period_label: str = "1年") -> dict:
    """
    マクロ監視指標（為替、金利、信用スプレッド、指数、コモディティ）および日経平均を
    yfinanceからオンデマンド一括取得し、メモリキャッシュ（TTL: 10分）します。
    """
    period_yf = PERIOD_MAPPING.get(period_label, "1y")
    
    # ダウンロード対象シンボルのリスト化
    symbols_to_fetch = set()
    for item in settings.MACRO_WATCHLIST.values():
        sym = item["symbol"]
        if sym != "HYG_LQD":
            symbols_to_fetch.add(sym)
            
    # 合成指標および日経平均の追加
    symbols_to_fetch.add("HYG")
    symbols_to_fetch.add("LQD")
    symbols_to_fetch.add("^N225")
    
    tickers_list = sorted(list(symbols_to_fetch))
    
    try:
        df_download = yf.download(
            tickers=tickers_list,
            period=period_yf,
            interval="1d",
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=True,
            timeout=20
        )
    except Exception as e:
        print(f"❌ [macro_collector] yfinanceダウンロード失敗: {e}")
        return {}

    if df_download.empty:
        return {}

    result_dict = {}

    def extract_single_ticker_df(symbol: str) -> pd.DataFrame:
        try:
            if isinstance(df_download.columns, pd.MultiIndex):
                if symbol in df_download.columns.levels[1] or symbol in df_download.columns.get_level_values(1):
                    sub = df_download.xs(symbol, axis=1, level=1).copy()
                elif symbol in df_download.columns.levels[0] or symbol in df_download.columns.get_level_values(0):
                    sub = df_download[symbol].copy()
                else:
                    return pd.DataFrame()
            else:
                sub = df_download.copy()
            
            sub = sub.dropna(how="all").reset_index()
            sub.columns = [str(c).lower().strip() for c in sub.columns]
            date_col = next((c for c in sub.columns if "date" in c or "time" in c or "index" in c), None)
            if not date_col:
                return pd.DataFrame()
            
            sub = sub.rename(columns={date_col: "date"})
            sub["date"] = pd.to_datetime(sub["date"]).dt.tz_localize(None)
            
            for num_c in ["open", "high", "low", "close", "volume"]:
                if num_c in sub.columns:
                    sub[num_c] = pd.to_numeric(sub[num_c], errors="coerce")
            
            sub = sub.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
            return sub
        except Exception:
            return pd.DataFrame()

    raw_dfs = {}
    for sym in tickers_list:
        df_sym = extract_single_ticker_df(sym)
        if not df_sym.empty:
            raw_dfs[sym] = df_sym

    # 1. 各マクロウォッチリスト指標の紐付け
    for label, meta in settings.MACRO_WATCHLIST.items():
        sym = meta["symbol"]
        if sym == "HYG_LQD":
            continue
        if sym in raw_dfs:
            result_dict[label] = raw_dfs[sym]

    # 2. 合成指標 HYG/LQD レシオの算出
    if "HYG" in raw_dfs and "LQD" in raw_dfs:
        df_hyg = raw_dfs["HYG"][["date", "close"]].rename(columns={"close": "hyg_close"})
        df_lqd = raw_dfs["LQD"][["date", "close"]].rename(columns={"close": "lqd_close"})
        merged_ratio = pd.merge(df_hyg, df_lqd, on="date", how="inner").sort_values("date")
        if not merged_ratio.empty:
            merged_ratio["close"] = merged_ratio["hyg_close"] / merged_ratio["lqd_close"].replace(0, np.nan)
            merged_ratio["open"] = merged_ratio["close"]
            merged_ratio["high"] = merged_ratio["close"]
            merged_ratio["low"] = merged_ratio["close"]
            result_dict["信用スプレッド"] = merged_ratio[["date", "open", "high", "low", "close"]].dropna().reset_index(drop=True)

    # 3. 日経平均（^N225）の移動平均線（SMA 25, 75, 200）付与
    if "^N225" in raw_dfs:
        df_nk = raw_dfs["^N225"].copy()
        df_nk["sma25"] = df_nk["close"].rolling(window=25, min_periods=1).mean()
        df_nk["sma75"] = df_nk["close"].rolling(window=75, min_periods=1).mean()
        df_nk["sma200"] = df_nk["close"].rolling(window=200, min_periods=1).mean()
        result_dict["^N225"] = df_nk

    return result_dict