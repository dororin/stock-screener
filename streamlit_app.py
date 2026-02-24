import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import time
import mplfinance as mpf
import io
import base64
import matplotlib.pyplot as plt
import traceback

# --- ページ設定 ---
st.set_page_config(
    page_title="WVF Stock Screener",
    page_icon="📈",
    layout="wide"
)

# --- パラメータ設定 (Colabから継承) ---
pdh = 22       # WVFの期間
bbl = 20       # ボリンジャーバンド期間
mult = 2.0     # ボリンジャーバンド標準偏差倍率
SMA_LONG_PERIOD = 200 # トレンドフィルター用移動平均線
SMA_MID_PERIOD = 50   # チャート表示用移動平均線
threshold = 2.0 # WVFの閾値

# --- チャート画像生成関数 ---
def generate_mini_chart_base64(df):
    """mplfinanceを使用してチャート画像をbase64形式で生成"""
    try:
        plot_df = df.tail(60).copy()
        buf = io.BytesIO()
        
        mc = mpf.make_marketcolors(up='green', down='red', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)

        add_plots = []
        if 'sma50' in plot_df.columns:
            add_plots.append(mpf.make_addplot(plot_df['sma50'], color='orange', width=0.7))
        if 'sma200' in plot_df.columns:
            add_plots.append(mpf.make_addplot(plot_df['sma200'], color='red', width=1.0))

        fig, axlist = mpf.plot(plot_df, type='candle', style=s, addplot=add_plots,
                               figsize=(4, 2.5), tight_layout=True, returnfig=True,
                               axisoff=True)

        fig.set_facecolor('#f0f2f6') # Streamlitのデフォルト背景に合わせる
        for ax in axlist:
            ax.set_facecolor('#f0f2f6')

        fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0)
        plt.close(fig)

        data = base64.b64encode(buf.getvalue()).decode('utf-8')
        return f"data:image/png;base64,{data}"
    except Exception:
        return None

# --- 対象銘柄リストの取得 ---
@st.cache_data(ttl=86400) # 1日キャッシュ
def get_jpx_list():
    url = 'https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls'
    try:
        df_jpx = pd.read_excel(url)
        df_jpx = df_jpx.iloc[:, [1, 2, 3, 9]]
        target_indices = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
        df_jpx = df_jpx.loc[df_jpx["規模区分"].isin(target_indices)]
        df_jpx = df_jpx.iloc[:, [0, 1]]
        df_jpx.columns = ['symbol', 'name']
        return df_jpx
    except Exception as e:
        st.error(f"銘柄リスト取得エラー: {e}")
        return pd.DataFrame()

# --- 分析実行関数 ---
def analyze_market_streamlit(df_jpx):
    if df_jpx.empty:
        return pd.DataFrame()

    tickers = [f"{code}.T" for code in df_jpx['symbol'].tolist()]
    total_count = len(tickers)
    
    st.info(f"対象銘柄数: {total_count} の分析を開始します...")
    
    # 過去データ取得
    start_date = (pd.Timestamp.now() - pd.DateOffset(months=24)).strftime('%Y-%m-%d')
    end_date = pd.Timestamp.now().strftime('%Y-%m-%d')

    progress_bar = st.progress(0)
    status_text = st.empty()

    try:
        # yfinanceの一括取得 (auto_adjust=False はユーザーの強い要望)
        status_text.text("株価データをダウンロード中...")
        data = yf.download(tickers, start=start_date, end=end_date, interval="1d", group_by='ticker', auto_adjust=False, threads=True)
    except Exception as e:
        st.error(f"データダウンロードエラー: {e}")
        return pd.DataFrame()

    results = []
    
    for i, ticker_symbol in enumerate(tickers):
        try:
            # 進捗表示
            if i % 10 == 0:
                progress_bar.progress((i + 1) / total_count)
                status_text.text(f"分析中: {ticker_symbol} ({i+1}/{total_count})")

            if len(tickers) > 1:
                df = data[ticker_symbol].copy()
            else:
                df = data.copy()

            df.dropna(how='all', inplace=True)
            if len(df) < SMA_LONG_PERIOD + pdh:
                continue

            # Normalize columns: handle if columns are strings or tuples
            new_cols = []
            for c in df.columns:
                if isinstance(c, tuple):
                    new_cols.append(c[0].lower())
                else:
                    new_cols.append(str(c).lower())
            df.columns = new_cols

            # ロジック計算
            df['sma50'] = df['close'].rolling(window=SMA_MID_PERIOD).mean()
            df['sma200'] = df['close'].rolling(window=SMA_LONG_PERIOD).mean()
            df['highest_close'] = df['close'].rolling(window=pdh).max()
            df['wvf'] = (df['highest_close'] - df['low']) / df['highest_close'] * 100
            df['wvf_std'] = df['wvf'].rolling(window=bbl).std()
            df['wvf_mid'] = df['wvf'].rolling(window=bbl).mean()
            df['wvf_upper'] = df['wvf_mid'] + (mult * df['wvf_std'])

            latest = df.iloc[-1]
            is_uptrend = latest['close'] > latest['sma200'] and latest['sma50'] > latest['sma200']
            is_wvf_spike = latest['wvf'] >= latest['wvf_upper']
            is_above_threshold = latest['wvf'] >= threshold

            if is_uptrend and is_wvf_spike and is_above_threshold:
                code_num = int(ticker_symbol.replace('.T', ''))
                stock_name = df_jpx[df_jpx['symbol'] == code_num]['name'].values[0]
                
                # チャート画像生成
                img_base64 = generate_mini_chart_base64(df)

                results.append({
                    'チャート': img_base64,
                    'コード': f"{code_num}",
                    '銘柄': stock_name,
                    '現在値': round(latest['close'], 1),
                    'SMA200': round(latest['sma200'], 1),
                    '乖離率(%)': round((latest['close'] - latest['sma200']) / latest['sma200'] * 100, 2),
                    'WVF': round(latest['wvf'], 2),
                    'WVF Upper': round(latest['wvf_upper'], 2)
                })

        except Exception as e:
            continue

    progress_bar.empty()
    status_text.empty()
    return pd.DataFrame(results)

# --- UI実装 ---
st.title("WVF + Trend Screener :blue[Pro]")
st.markdown("""
Google Colabで実行していたスクリーニングをWebアプリ化しました。
1000銘柄規模の分析も、並列処理とバックエンドでの一括計算により高速に行われます。
""")

if st.sidebar.button("スクリーニング開始"):
    df_jpx = get_jpx_list()
    if not df_jpx.empty:
        result_df = analyze_market_streamlit(df_jpx)
        
        if not result_df.empty:
            st.success(f"該当銘柄が {len(result_df)} 銘柄見つかりました。")
            
            # 各銘柄をカード形式または個別の書き出しで表示（画像が含まれるため）
            for idx, row in result_df.iterrows():
                with st.container():
                    col1, col2 = st.columns([1, 3])
                    with col1:
                        if row['チャート']:
                            st.image(row['チャート'], caption=f"{row['コード']} {row['銘柄']}")
                    with col2:
                        st.subheader(f"{row['コード']} {row['銘柄']}")
                        metrics_cols = st.columns(4)
                        metrics_cols[0].metric("現在値", f"¥{row['現在値']:,.1f}")
                        metrics_cols[1].metric("200日線乖離", f"{row['乖離率(%)']}%")
                        metrics_cols[2].metric("WVF", row['WVF'])
                        metrics_cols[3].metric("WVF Upper", row['WVF Upper'])
                        
                        st.markdown(f"[TradingViewで表示](https://jp.tradingview.com/chart/?symbol=TSE%3A{row['コード']})")
                st.divider()
        else:
            st.warning("該当する銘柄はありませんでした。")
else:
    st.info("左の「スクリーニング開始」ボタンを押してください。")
