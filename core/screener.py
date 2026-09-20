# core/screener.py

import io
import requests
import pandas as pd
import numpy as np
import streamlit as st
from config import settings

@st.cache_data(ttl=86400)
def get_jpx_list() -> pd.DataFrame:
    """規模区分に基づくスクリーニング基礎銘柄マスタをJPXからDL取得します。失敗時は即座に例外を送出します。"""
    url = getattr(settings, "JPX_URL", "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"JPX銘柄リストのダウンロードに失敗しました (HTTP {resp.status_code}): {url}")
    
    if len(resp.content) < 10000:
        raise RuntimeError(f"JPXから取得したファイルサイズが不正です ({len(resp.content)} bytes): {url}")

    df = pd.read_excel(io.BytesIO(resp.content))
    if df.shape[1] < 10:
        raise ValueError(f"JPX Excelデータの列数が不足しています (列数: {df.shape[1]} < 10)")

    # 1: コード, 2: 銘柄名, 3: 市場・商品区分, 9: 規模区分
    scale_col_name = df.columns[9]
    target = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
    
    df_filtered = df[df[scale_col_name].isin(target)].iloc[:, [1, 2]].copy()
    df_filtered.columns = ['symbol', 'name']
    df_filtered['symbol'] = pd.to_numeric(df_filtered['symbol'], errors='coerce')
    df_filtered = df_filtered.dropna(subset=['symbol'])
    df_filtered['symbol'] = df_filtered['symbol'].astype(int).astype(str)
    
    return df_filtered.reset_index(drop=True)


@st.cache_data(ttl=86400)
def get_jpx_full_list() -> pd.DataFrame:
    """ETFやCore/Large/Midすべてのティッカー情報が内包された完全マスタを取得します。失敗時は即座に例外を送出します。"""
    url = getattr(settings, "JPX_URL", "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"JPX銘柄リストのダウンロードに失敗しました (HTTP {resp.status_code}): {url}")
    
    if len(resp.content) < 10000:
        raise RuntimeError(f"JPXから取得したファイルサイズが不正です ({len(resp.content)} bytes): {url}")

    df_full = pd.read_excel(io.BytesIO(resp.content))
    if df_full.shape[1] < 10:
        raise ValueError(f"JPX Excelデータの列数が不足しています (列数: {df_full.shape[1]} < 10)")

    df_scale = df_full.iloc[:, [1, 2, 9]].copy()
    df_scale.columns = ['symbol', 'name', 'scale_type']
    target_scales = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
    topix = df_scale[df_scale['scale_type'].isin(target_scales)][['symbol', 'name']]
    
    df_market = df_full.iloc[:, [1, 2, 3]].copy()
    df_market.columns = ['symbol', 'name', 'market']
    etf = df_market[df_market['market'] == 'ETF・ETN'][['symbol', 'name']]
    
    combined = pd.concat([topix, etf]).drop_duplicates(subset=['symbol'])
    combined['symbol'] = pd.to_numeric(combined['symbol'], errors='coerce')
    combined = combined.dropna(subset=['symbol'])
    combined['symbol'] = combined['symbol'].astype(int).astype(str)
    
    return combined.reset_index(drop=True)


def run_fast_screening(db_df: pd.DataFrame, log_accumulator: list = None) -> pd.DataFrame:
    """WVF（Williams Variable Accumulation）点灯＆200SMA上向き／上乗せ条件で高速スキャンを実行します。"""
    def log(msg):
        if log_accumulator is not None:
            log_accumulator.append(msg)
        print(f"[SCREENER] {msg}")

    if db_df.empty:
        log("❌ データベースが空のため、判定処理を中止します。")
        return pd.DataFrame()
    
    # 読み込み失敗時は例外が発生し即座に停止します
    jpx_list = get_jpx_list()
    name_map = dict(zip(jpx_list['symbol'].astype(str), jpx_list['name']))
    
    results = []
    tickers = db_df['ticker'].unique()
    db_df = db_df.sort_values(["ticker", "date"])
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    total = len(tickers)
    
    log(f"🔎 判定プロセスを開始します。総判定対象: {total} 銘柄")
    
    for idx, ticker in enumerate(tickers):
        if idx % 20 == 0:
            progress_bar.progress((idx + 1) / total)
            status_text.text(f"判定中: {ticker} ({idx+1}/{total})")
            
        try:
            df = db_df[db_df['ticker'] == ticker].copy()
            
            # 【検証】データ件数のチェック
            if len(df) < 220:
                log(f"⏭️ [{ticker}] スキップ：時系列データが不足しています（実績: {len(df)} 件 / 最小必要数: 220 件）")
                continue

            # 💡 共通WVF計算関数（core.calculator）を利用
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
            
            # 各銘柄の直近判定パラメータをテキスト化
            param_details = (
                f"Close={latest['close']:.1f}, SMA200={latest['sma200']:.1f}, "
                f"WVF={latest['wvf']:.2f}%, Upper={latest['wvf_upper']:.2f}%, RangeHigh={latest['range_high']:.2f}%, "
                f"SlopeRate={slope_rate:.6f}"
            )
            
            if is_uptrend and is_wvf_lit:
                ext_price = latest.get('ext_price', 0.0)
                results.append({
                    'チャート': df.tail(60)[['date','open','high','low','close','sma50','sma200','volume']].to_json(orient='records', date_format='iso'),
                    'シグナル日': latest["date"].strftime('%Y-%m-%d'),
                    'コード': ticker,
                    '銘柄': name_map.get(ticker, "-"),
                    '現在値': round(latest['close'], 1),
                    '消灯目安(安値)': round(ext_price, 1) if pd.notna(ext_price) else 0.0,
                    'SMA200': round(latest['sma200'], 1),
                    '乖離率(%)': round((latest['close'] - latest['sma200']) / latest['sma200'] * 100, 2),
                    '200MA傾き率': round(slope_rate, 6),
                    'WVF': round(latest['wvf'], 2),
                    'WVF Upper': round(latest['wvf_upper'], 2),
                    'お気に入り': False
                })
                log(f"✅ [{ticker}] {name_map.get(ticker, '-')} ➔ 点灯！条件クリア ({param_details})")
            else:
                reasons = []
                if not is_uptrend:
                    reasons.append(f"トレンド条件未達 (Close {latest['close']:.1f} <= SMA200 {latest['sma200']:.1f} かつ 200MA傾き率 {slope_rate:.6f} < -0.0001)")
                if not is_wvf_lit:
                    reasons.append(f"WVF未点灯 (WVF {latest['wvf']:.2f}% が Upper {latest['wvf_upper']:.2f}% または 5.0% を下回る)")
                
                log(f"⏭️ [{ticker}] {name_map.get(ticker, '-')} ➔ スキップ ({param_details}) 理由: {' / '.join(reasons)}")
                
        except Exception as e:
            log(f"❌ [{ticker}] 判定処理中に例外エラーが発生しました: {e}")
            continue
            
    progress_bar.empty()
    status_text.empty()
    log(f"🎉 判定処理が完了しました。合致数: {len(results)} 件")
    return pd.DataFrame(results)