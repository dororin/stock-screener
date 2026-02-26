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
import os
import json
from datetime import datetime
from streamlit_gsheets import GSheetsConnection

# --- ページ設定 ---
st.set_page_config(
    page_title="WVF Stock Screener",
    page_icon="📈",
    layout="wide"
)

# --- カスタムCSS (デザインのコンパクト化) ---
st.markdown("""
    <style>
    /* 全体のフォントサイズ縮小 */
    html, body, [class*="st-"] {
        font-size: 0.95rem !important;
    }
    /* メトリクスの余白とサイズ調整 */
    [data-testid="stMetricValue"] {
        font-size: 1.1rem !important;
        font-weight: 600;
    }
    [data-testid="stMetricLabel"] {
        font-size: 0.8rem !important;
    }
    /* カードの余白削減 */
    .stMainContainer {
        padding-top: 2rem !important;
    }
    .stVerticalBlock {
        gap: 0.5rem !important;
    }
    /* セクション間の区切り線を細く */
    hr {
        margin: 0.8rem 0 !important;
    }
    /* サブヘッダーのサイズ調整 */
    h3 {
        font-size: 1.1rem !important;
        margin-bottom: 0.3rem !important;
    }
    </style>
    """, unsafe_allow_html=True)

# --- パラメータ設定 (TradingViewの設定に合わせる) ---
pdh = 11       # WVFの期間 (LookBack Period)
bbl = 20       # ボリンジャーバンド期間
mult = 2.0     # ボリンジャーバンド標準偏差倍率
lb = 100       # rangeHighのルックバック期間 (Look Back Period Percentile High)
ph = 0.85      # 最高値の係数 (Highest Percentile - e.g. 0.85 = 85%)
SMA_LONG_PERIOD = 200 # トレンドフィルター用移動平均線
SMA_MID_PERIOD = 50   # チャート表示用移動平均線
threshold = 2.0 # WVFの閾値

# --- 保存・読み込み設定 (Google Sheets) ---
# ※ .streamlit/secrets.toml または Streamlit Cloud の Secrets 設定が必要
try:
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception:
    conn = None

def save_history(df):
    """結果をGoogle Sheetsに保存 (1銘柄1行)"""
    if conn is None:
        st.error("Google Sheets への接続設定が見つかりません。")
        return None
        
    screening_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 保存用データフレームの作成
    save_df = df.copy()
    save_df['screening_id'] = screening_id
    
    # 既存データの取得
    try:
        existing_data = conn.read()
        updated_data = pd.concat([existing_data, save_df], ignore_index=True)
    except Exception:
        # シートが空、または初回の場合
        updated_data = save_df
        
    conn.update(data=updated_data)
    return screening_id

def get_history_list():
    """保存されたIDのリストを取得（新しい順）"""
    if conn is None:
        return []
    try:
        df = conn.read()
        if df.empty or 'screening_id' not in df.columns:
            return []
        ids = df['screening_id'].unique().tolist()
        return sorted(ids, reverse=True)
    except Exception:
        return []

def load_history(screening_id):
    """Google Sheetsから指定IDのデータを読み込み"""
    if conn is None:
        return pd.DataFrame()
    try:
        df = conn.read()
        return df[df['screening_id'] == screening_id].copy()
    except Exception:
        return pd.DataFrame()

def delete_history(screening_id):
    """Google Sheetsから指定IDのデータを削除"""
    if conn is None:
        return False
    try:
        df = conn.read()
        new_df = df[df['screening_id'] != screening_id].copy()
        conn.update(data=new_df)
        return True
    except Exception:
        return False

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
    
    # 過去データ取得（直近1年分あれば十分：200日線 + WVF期間をカバー）
    # start/end 指定だと end が「その日の前日まで」になる仕様があるため、period="1y" を使用して最新データを含めます
    progress_bar = st.progress(0)
    status_text = st.empty()

    try:
        status_text.text("株価データをダウンロード中 (分割取得)...")
        # 銘柄をバッチに分けてダウンロード (Windowsの Errno 22 / CPU100% 回避)
        batch_size = 50
        all_dfs = []
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            status_text.text(f"ダウンロード中... ({i}〜{min(i+batch_size, len(tickers))} / {len(tickers)})")
            # threads=True を維持しつつバッチ化
            batch_data = yf.download(batch, period="1y", interval="1d", group_by='ticker', auto_adjust=False, threads=True, progress=False)
            if not batch_data.empty:
                all_dfs.append(batch_data)
        
        if not all_dfs:
            st.error("データの取得に失敗しました。")
            return pd.DataFrame()
            
        # ダウンロードしたデータを結合
        data = pd.concat(all_dfs, axis=1)
    except Exception as e:
        st.error(f"データダウンロードエラー: {e}")
        return pd.DataFrame()

    results = []
    
    for i, ticker_symbol in enumerate(tickers):
        try:
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

            # カラム名の正規化
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
            # PINEスクリプトの rangeHigh ロジックを追加
            df['range_high'] = df['wvf'].rolling(window=lb).max() * ph

            # 直近と1日前を取得
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            
            # 条件判定
            # 上昇トレンド
            is_uptrend = latest['close'] > latest['sma200'] and latest['sma50'] > latest['sma200']
            
            # 当日点灯（本日バンドを上回っている、またはrangeHighを上回っている銘柄を抽出）
            is_wvf_lit = latest['wvf'] >= latest['wvf_upper'] or latest['wvf'] >= latest['range_high']
            
            # 閾値チェック
            is_above_threshold = latest['wvf'] >= threshold

            if is_uptrend and is_wvf_lit and is_above_threshold:
                code_num = int(ticker_symbol.replace('.T', ''))
                stock_name = df_jpx[df_jpx['symbol'] == code_num]['name'].values[0]
                
                # シグナル日（最新データの日付）
                signal_date = latest.name.strftime('%Y-%m-%d')
                
                # チャート画像生成
                img_base64 = generate_mini_chart_base64(df)

                # 消灯価格の計算 (安値がこの価格を上回るとWVFがシグナル圏内を下回る)
                # wvf = (highest_close - low) / highest_close * 100
                # wvf < range_high などを満たす low の条件を算出
                ext_price_upper = latest['highest_close'] * (1 - latest['wvf_upper'] / 100)
                ext_price_range = latest['highest_close'] * (1 - latest['range_high'] / 100)
                ext_price_thresh = latest['highest_close'] * (1 - threshold / 100)
                
                # シグナルは「upperBand または rangeHigh」のいずれかを越えていれば点灯するため、
                # 消灯するには「その高い方の価格を下回る」必要があります。
                # ただし、そもそも WVF が閾値 (threshold) を下回れば消灯するため、
                # 「(UpperかRangeHighを割る) か (Thresholdを割る)」のいずれか早い方（低い価格）が目安となります。
                extinction_price = min(max(ext_price_upper, ext_price_range), ext_price_thresh)

                results.append({
                    'チャート': img_base64,
                    'シグナル日': signal_date,
                    'コード': f"{code_num}",
                    '銘柄': stock_name,
                    '現在値': round(latest['close'], 1),
                    '消灯目安(安値)': round(extinction_price, 1),
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

# --- セッション状態の初期化 ---
if 'result_df' not in st.session_state:
    st.session_state.result_df = pd.DataFrame()

# --- サイドバー UI ---
with st.sidebar:
    st.subheader("スクリーニング操作")
    
    # 履歴読み込みと削除
    history_ids = get_history_list()
    if history_ids:
        selected_id = st.selectbox("履歴から読み込み", ["-- 選択してください --"] + history_ids)
        if selected_id != "-- 選択してください --":
            if st.session_state.get('last_loaded_id') != selected_id:
                st.session_state.result_df = load_history(selected_id)
                st.session_state.last_loaded_id = selected_id
            
            # 削除機能
            with st.expander("履歴の削除"):
                st.warning("選択中の履歴を削除します。")
                confirm_delete = st.checkbox("削除を確認")
                if st.button("この履歴を完全に削除", type="primary", disabled=not confirm_delete, use_container_width=True):
                    if delete_history(selected_id):
                        st.success(f"削除しました: {selected_id}")
                        st.session_state.result_df = pd.DataFrame()
                        st.session_state.last_loaded_id = None
                        st.rerun()
    
    # スクリーニング開始ボタン
    if st.button("スクリーニング開始", use_container_width=True):
        df_jpx = get_jpx_list()
        if not df_jpx.empty:
            st.session_state.result_df = analyze_market_streamlit(df_jpx)
            st.session_state.last_loaded_id = None # 新規実行時はIDなし
            if st.session_state.result_df.empty:
                st.warning("該当する銘柄はありませんでした。")
    
    # 保存ボタン (結果がある場合のみ表示)
    if not st.session_state.result_df.empty:
        if st.button("スクリーニング結果を保存", use_container_width=True):
            saved_id = save_history(st.session_state.result_df)
            if saved_id:
                st.success(f"保存しました: {saved_id}")
                st.rerun()

# --- メインコンテンツ UI ---
st.title("WVF + Trend Screener :blue[Pro]")
st.markdown("""
Google Colabで実行していたスクリーニングをWebアプリ化しました。
- **当日点灯**: 本日時点でシグナルが点灯している（バンドを上回っている）銘柄をすべて抽出します。
- **最新データ反映**: yfinanceの仕様に合わせて最新の営業日データを取得するように調整済みです。
""")

if not st.session_state.result_df.empty:
    result_df = st.session_state.result_df
    st.success(f"該当銘柄が {len(result_df)} 銘柄見つかりました。")
    
    # 銘柄を縦2列（PC時）に並べるためのロジック
    for i in range(0, len(result_df), 2):
        cols = st.columns(2)
        for j in range(2):
            if i + j < len(result_df):
                row = result_df.iloc[i + j]
                with cols[j]:
                    card_container = st.container(border=True) # 枠線を追加して見やすく
                    with card_container:
                        title_col, link_col = st.columns([4, 1])
                        title_col.subheader(f"{row['コード']} {row['銘柄']}")
                        link_col.markdown(f"[TVで表示](https://jp.tradingview.com/chart/?symbol=TSE%3A{row['コード']})")
                        
                        img_col, info_col = st.columns([1, 2])
                        with img_col:
                            if row['チャート']:
                                st.image(row['チャート'], use_container_width=True) # 幅いっぱいに
                        with info_col:
                            m_cols = st.columns(3)
                            m_cols[0].metric("現在値", f"¥{row['現在値']:,.1f}" if isinstance(row['現在値'], (int, float)) else row['現在値'])
                            m_cols[1].metric("消灯目安", f"¥{row['消灯目安(安値)']:,.1f}" if isinstance(row['消灯目安(安値)'], (int, float)) else row['消灯目安(安値)'])
                            m_cols[2].metric("200日乖離", f"{row['乖離率(%)']}%")
                            
                            m_cols2 = st.columns(3)
                            m_cols2[0].metric("WVF", row['WVF'])
                            m_cols2[1].metric("WVF Upper", row['WVF Upper'])
                            m_cols2[2].metric("シグナル日", row['シグナル日'])
else:
    st.info("左の「スクリーニング開始」ボタンを押すか、履歴から読み込んでください。")
