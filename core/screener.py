# core/screener.py

import io
import requests
import pandas as pd
import numpy as np
import streamlit as st
from config import settings

@st.cache_data(ttl=86400)
def get_jpx_list() -> pd.DataFrame:
    """
    規模区分に基づくスクリーニング基礎銘柄マスタ（TOPIX500対象）を取得します。
    自律型統合マスター（stock_master）から即時取得します。
    """
    try:
        from core.stock_master import get_stock_master
        df_master = get_stock_master()
        if not df_master.empty and "is_topix500" in df_master.columns:
            active_mask = (df_master["is_active"] == True) if "is_active" in df_master.columns else True
            topix500_df = df_master[(df_master["is_topix500"] == True) & active_mask][["symbol", "name"]].copy()
            if not topix500_df.empty:
                topix500_df["symbol"] = topix500_df["symbol"].astype(str).str.strip().str.upper()
                return topix500_df.reset_index(drop=True)
    except Exception as ex:
        print(f"⚠️ [screener] stock_masterからのTOPIX500リスト取得失敗、ファイルへフォールバック: {ex}")

    # フォールバック（従来処理）
    url = getattr(settings, "JPX_URL", "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200 or len(resp.content) < 10000:
        return pd.DataFrame(columns=['symbol', 'name'])

    df = pd.read_excel(io.BytesIO(resp.content))
    if df.shape[1] < 10:
        return pd.DataFrame(columns=['symbol', 'name'])

    scale_col_name = df.columns[9]
    target = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
    
    df_filtered = df[df[scale_col_name].isin(target)].iloc[:, [1, 2]].copy()
    df_filtered.columns = ['symbol', 'name']
    df_filtered['symbol'] = df_filtered['symbol'].astype(str).str.strip().str.split('.').str[0].str.upper()
    df_filtered = df_filtered.dropna(subset=['symbol'])
    
    return df_filtered.reset_index(drop=True)


@st.cache_data(ttl=86400)
def get_jpx_full_list() -> pd.DataFrame:
    """
    ETFや全上場銘柄のティッカー情報が内包された完全マスタを取得します（検索サジェスト用）。
    自律型統合マスター（stock_master）から即時取得します。
    """
    try:
        from core.stock_master import get_stock_master
        df_master = get_stock_master()
        if not df_master.empty and "symbol" in df_master.columns and "name" in df_master.columns:
            active_mask = (df_master["is_active"] == True) if "is_active" in df_master.columns else True
            full_df = df_master[active_mask][["symbol", "name"]].copy()
            if not full_df.empty:
                full_df["symbol"] = full_df["symbol"].astype(str).str.strip().str.upper()
                return full_df.drop_duplicates(subset=["symbol"]).reset_index(drop=True)
    except Exception as ex:
        print(f"⚠️ [screener] stock_masterからの全銘柄リスト取得失敗、ファイルへフォールバック: {ex}")

    # フォールバック（従来処理）
    url = getattr(settings, "JPX_URL", "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200 or len(resp.content) < 10000:
        return pd.DataFrame(columns=['symbol', 'name'])

    df_full = pd.read_excel(io.BytesIO(resp.content))
    if df_full.shape[1] < 10:
        return pd.DataFrame(columns=['symbol', 'name'])

    df_scale = df_full.iloc[:, [1, 2, 9]].copy()
    df_scale.columns = ['symbol', 'name', 'scale_type']
    target_scales = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
    topix = df_scale[df_scale['scale_type'].isin(target_scales)][['symbol', 'name']]
    
    df_market = df_full.iloc[:, [1, 2, 3]].copy()
    df_market.columns = ['symbol', 'name', 'market']
    etf = df_market[df_market['market'] == 'ETF・ETN'][['symbol', 'name']]
    
    combined = pd.concat([topix, etf]).drop_duplicates(subset=['symbol'])
    combined['symbol'] = combined['symbol'].astype(str).str.strip().str.split('.').str[0].str.upper()
    combined = combined.dropna(subset=['symbol'])
    
    return combined.reset_index(drop=True)


def run_fast_screening(db_df: pd.DataFrame, log_accumulator: list = None) -> pd.DataFrame:
    """WVF点灯＆200SMA上向き／上乗せ条件でスキャンを実行します（失敗・合致サマリーのみ出力）。"""
    def log(msg):
        if log_accumulator is not None:
            log_accumulator.append(msg)
        print(f"[SCREENER] {msg}")

    if db_df.empty:
        log("❌ データベースが空のため、判定処理を中止しました。")
        return pd.DataFrame()
    
    jpx_list = get_jpx_list()
    name_map = dict(zip(jpx_list['symbol'].astype(str), jpx_list['name']))
    
    results = []
    error_count = 0
    tickers = db_df['ticker'].unique()
    db_df = db_df.sort_values(["ticker", "date"])
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    total = len(tickers)
    
    for idx, ticker in enumerate(tickers):
        if idx % 25 == 0:
            progress_bar.progress((idx + 1) / total)
            status_text.text(f"判定中: {ticker} ({idx+1}/{total})")
            
        try:
            df = db_df[db_df['ticker'] == ticker].copy()
            if len(df) < 220:
                continue

            from core.calculator import compute_wvf_signals
            df = compute_wvf_signals(df)
            
            df['sma50'] = df['close'].rolling(window=50).mean()
            df['sma200'] = df['close'].rolling(window=200).mean()
            
            latest = df.iloc[-1]
            sma200_win = df['sma200'].tail(20).values
            slope, _ = np.polyfit(np.arange(len(sma200_win)), sma200_win, 1)
            slope_rate = slope / latest['close']
            
            is_uptrend = (latest['close'] > latest['sma200']) or (slope_rate >= -0.0001)
            is_wvf_lit = bool(latest.get('is_lime', False))
            
            if is_uptrend and is_wvf_lit:
                ext_price = latest.get('ext_price', 0.0)
                lime_streak = int((df["is_lime"].iloc[::-1].cumprod()).sum())

                results.append({
                    'チャート': df.tail(60)[['date','open','high','low','close','sma50','sma200','volume','is_lime']].to_json(orient='records', date_format='iso'),
                    'シグナル日': latest["date"].strftime('%Y-%m-%d'),
                    'コード': ticker,
                    '銘柄': name_map.get(ticker, "-"),
                    '現在値': round(latest['close'], 1),
                    '消灯目安': round(ext_price, 1) if pd.notna(ext_price) else 0.0,
                    '点灯日数': lime_streak,
                    'is_lime': is_wvf_lit,
                    'is_fuchsia': bool(latest.get('is_fuchsia', False)),
                    'is_normal_off': bool(latest.get('is_normal_off', False)),
                    'お気に入り': False
                })
                
        except Exception as e:
            error_count += 1
            log(f"❌ [{ticker}] {name_map.get(ticker, '')} 判定エラー: {e}")
            continue
            
    progress_bar.empty()
    status_text.empty()

    if error_count == 0:
        log(f"🎉 判定処理が完了しました。合致数: {len(results)} 件（エラーなし）")
    else:
        log(f"🎉 判定処理が完了しました。合致数: {len(results)} 件（エラー発生: {error_count} 件）")

    return pd.DataFrame(results)