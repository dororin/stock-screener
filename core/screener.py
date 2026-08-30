# core/screener.py
import pandas as pd
import numpy as np
import streamlit as st

@st.cache_data(ttl=86400)
def get_jpx_list() -> pd.DataFrame:
    """規模区分に基づくスクリーニング基礎銘柄マスタをJPXからDL取得します。"""
    url = 'https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls'
    try:
        df = pd.read_excel(url)
        df = df.iloc[:, [1, 2, 3, 9]]
        target = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
        df = df.loc[df["規模区分"].isin(target)].iloc[:, [0, 1]]
        df.columns = ['symbol', 'name']
        df['symbol'] = pd.to_numeric(df['symbol'], errors='coerce')
        df = df.dropna(subset=['symbol'])
        df['symbol'] = df['symbol'].astype(int)
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=86400)
def get_jpx_full_list() -> pd.DataFrame:
    """ETFやCore/Large/Midすべてのティッカー情報が内包された完全マスタを取得します。"""
    try:
        url = 'https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls'
        df_full = pd.read_excel(url)
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
    except Exception:
        df = get_jpx_list()
        if df.empty:
            return pd.DataFrame(columns=['symbol', 'name'])
        df = df.copy()
        df['symbol'] = df['symbol'].astype(str)
        return df

def run_fast_screening(db_df: pd.DataFrame, log_accumulator: list = None) -> pd.DataFrame:
    """WVF（Williams Variable Accumulation）点灯＆200SMA上向き／上乗せ条件で高速スキャンを実行します。"""
    def log(msg):
        if log_accumulator is not None:
            log_accumulator.append(msg)
        print(f"[SCREENER] {msg}")

    if db_df.empty:
        log("❌ データベースが空のため、判定処理を中止します。")
        return pd.DataFrame()
    
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
            
            df['sma50'] = df['close'].rolling(window=50).mean()
            df['sma200'] = df['close'].rolling(window=200).mean()
            df['highest_close'] = df['close'].rolling(window=11).max()
            df['wvf'] = (df['highest_close'] - df['low']) / df['highest_close'] * 100
            df['wvf_std'] = df['wvf'].rolling(window=20).std(ddof=0)
            df['wvf_mid'] = df['wvf'].rolling(window=20).mean()
            df['wvf_upper'] = df['wvf_mid'] + (2.0 * df['wvf_std'])
            df['range_high'] = df['wvf'].rolling(window=100).max() * 0.85
            
            latest = df.iloc[-1]
            sma200_win = df['sma200'].tail(20).values
            slope, _ = np.polyfit(np.arange(len(sma200_win)), sma200_win, 1)
            slope_rate = slope / latest['close']
            
            is_uptrend = (latest['close'] > latest['sma200']) or (slope_rate >= -0.0001)
            is_wvf_lit = latest['wvf'] >= latest['wvf_upper'] or latest['wvf'] >= latest['range_high']
            
            # 各銘柄の直近判定パラメータをテキスト化
            param_details = (
                f"Close={latest['close']:.1f}, SMA200={latest['sma200']:.1f}, "
                f"WVF={latest['wvf']:.2f}%, Upper={latest['wvf_upper']:.2f}%, RangeHigh={latest['range_high']:.2f}%, "
                f"SlopeRate={slope_rate:.6f}"
            )
            
            if is_uptrend and is_wvf_lit and latest['wvf'] >= 5.0:
                ext_price = min(max(latest['highest_close'] * (1 - latest['wvf_upper'] / 100), latest['highest_close'] * (1 - latest['range_high'] / 100)), latest['highest_close'] * (1 - 5.0 / 100))
                results.append({
                    'チャート': df.tail(60)[['date','open','high','low','close','sma50','sma200','volume']].to_json(orient='records', date_format='iso'),
                    'シグナル日': latest["date"].strftime('%Y-%m-%d'),
                    'コード': ticker,
                    '銘柄': name_map.get(ticker, "-"),
                    '現在値': round(latest['close'], 1),
                    '消灯目安(安値)': round(ext_price, 1),
                    'SMA200': round(latest['sma200'], 1),
                    '乖離率(%)': round((latest['close'] - latest['sma200']) / latest['sma200'] * 100, 2),
                    '200MA傾き率': round(slope_rate, 6),
                    'WVF': round(latest['wvf'], 2),
                    'WVF Upper': round(latest['wvf_upper'], 2),
                    'お気に入り': False
                })
                log(f"✅ [{ticker}] {name_map.get(ticker, '-')} ➔ 点灯！条件クリア ({param_details})")
            else:
                # なぜ引っかからなかったのか不一致の理由をログに出力
                reasons = []
                if not is_uptrend:
                    reasons.append(f"トレンド条件未達 (Close {latest['close']:.1f} <= SMA200 {latest['sma200']:.1f} かつ 200MA傾き率 {slope_rate:.6f} < -0.0001)")
                if not is_wvf_lit:
                    reasons.append(f"WVF未点灯 (WVF {latest['wvf']:.2f}% が Upper {latest['wvf_upper']:.2f}% および RangeHigh {latest['range_high']:.2f}% をともに下回る)")
                if latest['wvf'] < 5.0:
                    reasons.append(f"WVF値が5.0%未満 (WVF: {latest['wvf']:.2f}%)")
                
                log(f"⏭️ [{ticker}] {name_map.get(ticker, '-')} ➔ スキップ ({param_details}) 理由: {' / '.join(reasons)}")
                
        except Exception as e:
            # 判定中に発生したエラーを完全に可視化
            log(f"❌ [{ticker}] 判定処理中に例外エラーが発生しました: {e}")
            continue
            
    progress_bar.empty()
    status_text.empty()
    log(f"🎉 判定処理が完了しました。合致数: {len(results)} 件")
    return pd.DataFrame(results)