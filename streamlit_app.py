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
import requests
from bs4 import BeautifulSoup
import plotly.graph_objects as go
from plotly.subplots import make_subplots

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
threshold = 5.0 # WVFの閾値

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
        # キャッシュを無効にして最新データを取得
        df = conn.read(ttl=0)
        if df is None or df.empty or 'screening_id' not in df.columns:
            return []
        ids = df['screening_id'].unique().tolist()
        return sorted(ids, reverse=True)
    except Exception as e:
        # エラー時はデバッグ用に表示（必要に応じて）
        # st.sidebar.error(f"履歴取得エラー: {e}")
        return []

def load_history(screening_id):
    """Google Sheetsから指定IDのデータを読み込み"""
    if conn is None:
        return pd.DataFrame()
    try:
        df = conn.read()
        target_df = df[df['screening_id'] == screening_id].copy()
        # 銘柄コードの整形 (.0 が付くのを防止)
        if not target_df.empty and 'コード' in target_df.columns:
            target_df['コード'] = target_df['コード'].astype(str).str.replace(r'\.0$', '', regex=True)
        # お気に入りカラムの存在確認と初期化
        if not target_df.empty and 'お気に入り' not in target_df.columns:
            target_df['お気に入り'] = False
        return target_df
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

# --- マーケット情報用定数 ---
MARKET_DATA_URL = "https://docs.google.com/spreadsheets/d/1vaX2dKcHO_fo_KMffNiC98pY1fzfMkHCRkHE1IFE0PI/edit"

def parse_jp_amount(text):
    text = str(text).replace('億円', '').replace(',', '').strip()
    if '兆' in text:
        parts = text.split('兆')
        trillion = int(parts[0]) if parts[0] else 0
        hundred_mil = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        return trillion * 10000 + hundred_mil
    else:
        try:
            return int(text)
        except:
            return 0

def fetch_saitei_data():
    url = "https://karauri.net/saitei/"
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers)
    res.encoding = 'utf-8'
    soup = BeautifulSoup(res.text, "html.parser")
    tables = soup.find_all("table")
    data = []
    if tables:
        rows = tables[0].find_all("tr")
        for row in rows[1:]:
            cols = [td.text.strip() for td in row.find_all(["td", "th"])]
            if len(cols) >= 3:
                # 日付形式の統一
                date_str = cols[0].replace('年', '-').replace('月', '-').replace('日', '')
                sell_amt = cols[1]
                buy_amt = cols[2]
                data.append({
                    'Date': date_str, 
                    'Sell(Oku-yen)': parse_jp_amount(sell_amt), 
                    'Buy(Oku-yen)': parse_jp_amount(buy_amt)
                })
    df = pd.DataFrame(data)
    if not df.empty:
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.sort_values('Date').reset_index(drop=True)
    return df

def update_and_load_saitei_data():
    if conn is None:
        st.error("Google Sheets への接続設定が見つかりません。")
        return pd.DataFrame()
        
    try:
        # マーケットデータ専用の別ファイルを読み込み
        existing_df = conn.read(spreadsheet=MARKET_DATA_URL, worksheet="saitei_data", ttl=0)
    except Exception:
        existing_df = pd.DataFrame(columns=['Date', 'Sell(Oku-yen)', 'Buy(Oku-yen)'])
    
    web_df = fetch_saitei_data()
    if web_df.empty:
        return existing_df
    
    if not existing_df.empty and 'Date' in existing_df.columns:
        existing_df['Date'] = pd.to_datetime(existing_df['Date'])
        merged_df = pd.concat([existing_df, web_df]).drop_duplicates(subset=['Date'], keep='last')
        merged_df = merged_df.sort_values('Date').reset_index(drop=True)
    else:
        merged_df = web_df.copy()
        
    try:
        save_df = merged_df.copy()
        save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
        # マーケットデータ専用の別ファイルへ保存
        conn.update(spreadsheet=MARKET_DATA_URL, worksheet="saitei_data", data=save_df)
    except Exception as e:
        if "saitei_data" in str(e):
             st.error(f"新しいスプレッドシート（marketdata）の中に 'saitei_data' という名前のタブが見つかりません。タブ名を正確に設定しているか確認してください。")
        else:
             st.error(f"裁定残データの保存に失敗しました: {e}")
        
    return merged_df

def plot_saitei_and_nikkei(saitei_df):
    if saitei_df.empty:
        return None
    
    # 日付の型変換 (GSheetsから読み込まれた際に文字列になっている可能性があるため)
    saitei_df = saitei_df.copy()
    saitei_df['Date'] = pd.to_datetime(saitei_df['Date'])
    
    # 期間の取得
    start_date = saitei_df['Date'].min()
    end_date = saitei_df['Date'].max() + pd.Timedelta(days=7)
    
    # 日経平均データの取得 (1d: 日足に変更)
    try:
        n225_ticker = yf.Ticker('^N225')
        n225 = n225_ticker.history(start=start_date, end=end_date, interval='1d')
        if n225.empty:
            n225 = yf.download('^N225', start=start_date, end=end_date, interval='1d', progress=False)
            
        if not n225.empty:
            n225.reset_index(inplace=True)
            new_cols = []
            for c in n225.columns:
                if isinstance(c, tuple):
                    new_cols.append(c[0].lower())
                else:
                    new_cols.append(str(c).lower())
            n225.columns = new_cols
            
            if 'date' in n225.columns:
                n225['date'] = pd.to_datetime(n225['date']).dt.tz_localize(None)
    except Exception as e:
        st.warning(f"日経平均データの取得中にエラーが発生しました: {e}")
        n225 = pd.DataFrame()

    # 比率（裁定買残 / 日経平均）の計算
    ratio_df = pd.DataFrame()
    if not n225.empty and not saitei_df.empty:
        try:
            temp_saitei = saitei_df.copy()
            temp_saitei.columns = [c.lower() for c in temp_saitei.columns]
            # 直近の日足終値でマージして比率を算出
            ratio_df = pd.merge_asof(
                temp_saitei.sort_values('date'),
                n225[['date', 'close']].sort_values('date'),
                on='date',
                direction='nearest'
            )
            ratio_df['ratio'] = ratio_df['buy(oku-yen)'] / ratio_df['close']
        except Exception:
            ratio_df = pd.DataFrame()

    # サブプロットの作成 (3段構成)
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, 
                        vertical_spacing=0.05, row_heights=[0.5, 0.25, 0.25],
                        subplot_titles=('日経平均 (日足)', '裁定買残 (億円)', '裁定買残 / 日経平均 (倍率)'))
                        
    # 1段目: 日経平均ローソク足
    if not n225.empty and 'date' in n225.columns:
        fig.add_trace(go.Candlestick(x=n225['date'],
                                    open=n225['open'], high=n225['high'],
                                    low=n225['low'], close=n225['close'],
                                    name='日経平均'), row=1, col=1)
                                    
    # 2段目: 裁定買残棒グラフ
    fig.add_trace(go.Bar(x=saitei_df['Date'], y=saitei_df['Buy(Oku-yen)'],
                         name='裁定買残', marker_color='#1f77b4'), row=2, col=1)
    
    # 3段目: 比率折れ線グラフ
    if not ratio_df.empty and 'ratio' in ratio_df.columns:
        fig.add_trace(go.Scatter(x=ratio_df['date'], y=ratio_df['ratio'],
                                 mode='lines+markers', name='倍率',
                                 line=dict(color='red', width=2),
                                 fill='tozeroy', fillcolor='rgba(255, 0, 0, 0.1)'), row=3, col=1)
                         
    fig.update_layout(height=800, margin=dict(l=20, r=20, t=40, b=20),
                      xaxis_rangeslider_visible=False,
                      showlegend=False,
                      dragmode='zoom')
                      
    fig.update_xaxes(spikemode='across', spikethickness=1, spikedash='solid', spikecolor='grey')
    fig.update_yaxes(title_text="株価", row=1, col=1)
    fig.update_yaxes(title_text="億円", row=2, col=1)
    fig.update_yaxes(title_text="倍率", row=3, col=1)
    
    return fig

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
def analyze_market_streamlit(df_targets):
    if df_targets.empty:
        return pd.DataFrame()

    tickers = [f"{code}.T" for code in df_targets['symbol'].tolist()]
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
            batch_data = yf.download(batch, period="1y", interval="1d", group_by='ticker', auto_adjust=False, actions=True, threads=True, progress=False)
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
            # 200日線 + 傾き計算用(20日) のデータを確保
            if len(df) < SMA_LONG_PERIOD + 20:
                continue

            # カラム名の正規化
            new_cols = []
            for c in df.columns:
                if isinstance(c, tuple):
                    new_cols.append(c[0].lower())
                else:
                    new_cols.append(str(c).lower())
            df.columns = new_cols

            if 'stock splits' in df.columns:
                # 0以外の分割情報を抽出し、累積比率を逆算（現在を1.0として過去を修正）
                splits = df['stock splits'].copy()
                splits.replace(0, 1, inplace=True)
                # 過去に遡って累積の分割係数を計算
                df['split_factor'] = (1 / splits).iloc[::-1].cumprod().iloc[::-1]
                df['split_factor'] = df['split_factor'].shift(-1).fillna(1.0)
                
                # 4値を分割修正（配当落ちは含まない）
                for col in ['open', 'high', 'low', 'close']:
                    df[col] = df[col] * df['split_factor']
            
            # ロジック計算
            df['sma50'] = df['close'].rolling(window=SMA_MID_PERIOD).mean()
            df['sma200'] = df['close'].rolling(window=SMA_LONG_PERIOD).mean()
            df['highest_close'] = df['close'].rolling(window=pdh).max()
            df['wvf'] = (df['highest_close'] - df['low']) / df['highest_close'] * 100
            df['wvf_std'] = df['wvf'].rolling(window=bbl).std(ddof=0) # ddof=0 で計算精度を同期
            df['wvf_mid'] = df['wvf'].rolling(window=bbl).mean()
            df['wvf_upper'] = df['wvf_mid'] + (mult * df['wvf_std'])
            # PINEスクリプトの rangeHigh ロジックを追加
            df['range_high'] = df['wvf'].rolling(window=lb).max() * ph

            # 直近と1日前を取得
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            
            # --- 200MAの傾き計算 (直近20日間) ---
            sma200_window = df['sma200'].tail(20).values
            x = np.arange(len(sma200_window))
            slope, _ = np.polyfit(x, sma200_window, 1)
            # 傾きを価格で割って正規化 (率にする)
            slope_rate = slope / latest['close']

            # 条件判定
            # 上昇トレンド判定の変更 (以下のどちらかを満たせばOK)
            # 1. 200日移動平均線より当日の価格が上か
            # 2. 200MAの傾き率が -0.0001 以上 (上昇または横ばい)
            is_uptrend = (latest['close'] > latest['sma200']) or (slope_rate >= -0.0001)
            
            # 当日点灯（本日バンドを上回っている、またはrangeHighを上回っている銘柄を抽出）
            is_wvf_lit = latest['wvf'] >= latest['wvf_upper'] or latest['wvf'] >= latest['range_high']
            
            # 閾値チェック
            is_above_threshold = latest['wvf'] >= threshold

            if is_uptrend and is_wvf_lit and is_above_threshold:
                code_num = int(ticker_symbol.replace('.T', ''))
                matching_rows = df_targets[df_targets['symbol'] == code_num]
                stock_name = matching_rows['name'].values[0] if not matching_rows.empty else "-"
                
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
                    '200MA傾き率': round(slope_rate, 6),
                    'WVF': round(latest['wvf'], 2),
                    'WVF Upper': round(latest['wvf_upper'], 2),
                    'お気に入り': False
                })

        except Exception as e:
            continue

    progress_bar.empty()
    status_text.empty()
    return pd.DataFrame(results)

# --- セッション状態の初期化 ---
if 'result_df' not in st.session_state:
    st.session_state.result_df = pd.DataFrame()
if 'saitei_df' not in st.session_state:
    st.session_state.saitei_df = pd.DataFrame()

# --- サイドバー ナビゲーション ---
with st.sidebar:
    selected_page = st.radio("画面選択", ["スクリーニング", "マーケット情報"])
    st.divider()

if selected_page == "マーケット情報":
    st.title("📈 マーケット情報")
    if st.button("データ取得・更新", type="primary"):
        with st.spinner("データを取得・更新中... (初回は時間がかかる場合があります)"):
            df = update_and_load_saitei_data()
            if not df.empty:
                st.session_state.saitei_df = df
                st.success("最新データを読み込みました。")
    
    if not st.session_state.saitei_df.empty:
        fig = plot_saitei_and_nikkei(st.session_state.saitei_df)
        if fig:
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("上の「データ取得・更新」ボタンを押して最新データを取得してください。")
    
    st.stop() # ここで実行を停止し、スクリーニング側のUIを描画しない

# --- サイドバー UI ---
with st.sidebar:
    st.subheader("スクリーニング操作")
    
    # --- 履歴管理セクション ---
    with st.expander("📂 履歴から読み込み", expanded=True):
        history_ids = get_history_list()
        if history_ids:
            selected_id = st.selectbox("過去の結果を選択", ["-- 選択してください --"] + history_ids, key="history_select")
            if selected_id != "-- 選択してください --":
                if st.session_state.get('last_loaded_id') != selected_id:
                    with st.spinner("読み込み中..."):
                        st.session_state.result_df = load_history(selected_id)
                        st.session_state.last_loaded_id = selected_id
                    st.success(f"読み込み完了: {selected_id}")
            
            # 削除機能 (読み込み時のみ表示)
            if selected_id != "-- 選択してください --":
                st.divider()
                st.caption("履歴の削除")
                confirm_delete = st.checkbox("この履歴を削除する", key="confirm_delete")
                if st.button("完全に削除", type="primary", disabled=not confirm_delete, use_container_width=True):
                    if delete_history(selected_id):
                        st.success("削除しました")
                        st.session_state.result_df = pd.DataFrame()
                        st.session_state.last_loaded_id = None
                        st.rerun()
        else:
            st.info("保存された履歴はありません。")
            if st.button("履歴を再読込"):
                st.rerun()

    st.divider()
    
    st.subheader("対象銘柄リスト")
    list_source = st.radio("取得元を選択", ["JPX (TOPIX)", "CSVファイルアップロード"], label_visibility="collapsed")
    
    csv_file = None
    if list_source == "CSVファイルアップロード":
        csv_file = st.file_uploader("CSVファイルを選択", type=["csv"], help="「コード」や「銘柄コード」といった列が含まれている必要があります。")
    
    # スクリーニング開始ボタン
    if st.button("スクリーニング開始", use_container_width=True):
        df_targets = pd.DataFrame()
        if list_source == "JPX (TOPIX)":
            df_targets = get_jpx_list()
        else:
            if csv_file is not None:
                try:
                    # CSVの読み込み。エンコーディングはShift-JISも考慮
                    try:
                        df_csv = pd.read_csv(csv_file)
                    except UnicodeDecodeError:
                        csv_file.seek(0)
                        df_csv = pd.read_csv(csv_file, encoding='shift_jis')
                        
                    code_col = None
                    # コード列を探す
                    for col in ["コード", "銘柄コード", "symbol", "Code", "Ticker"]:
                        if col in df_csv.columns:
                            code_col = col
                            break
                    if code_col is None:
                        code_col = df_csv.columns[0] # 見つからない場合は1列目を強制的にコードとする
                    
                    df_targets['symbol'] = pd.to_numeric(df_csv[code_col], errors='coerce').dropna().astype(int)
                    
                    name_col = None
                    for col in ["銘柄", "銘柄名", "名称", "name", "Name"]:
                        if col in df_csv.columns:
                            name_col = col
                            break
                    if name_col:
                        df_targets['name'] = df_csv[name_col]
                    else:
                        df_targets['name'] = "-"
                        
                except Exception as e:
                    st.error(f"CSVファイルの読み込みエラー: {e}")
            else:
                st.warning("CSVファイルがアップロードされていません。")

        if not df_targets.empty:
            st.session_state.result_df = analyze_market_streamlit(df_targets)
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
                        # 銘柄コードをTradingViewへのリンクにする
                        tv_url = f"https://jp.tradingview.com/chart/?symbol=TSE%3A{row['コード']}"
                        
                        # お気に入り(星)と銘柄名を表示
                        title_col, fav_col = st.columns([0.85, 0.15])
                        with title_col:
                            st.subheader(f"[{row['コード']}]({tv_url}) {row['銘柄']}")
                        with fav_col:
                            # インデックスを使ってユニークなキーを作成
                            fav_key = f"fav_{row['コード']}_{i+j}"
                            # toggleを使って星マークを表現
                            is_fav = st.toggle("⭐", value=row['お気に入り'], key=fav_key, label_visibility="collapsed")
                            # セッション状態を更新
                            if is_fav != row['お気に入り']:
                                st.session_state.result_df.at[i + j, 'お気に入り'] = is_fav
                        
                        img_col, info_col = st.columns([1, 2])
                        with img_col:
                            if row['チャート']:
                                st.image(row['チャート'], use_container_width=True) # 幅いっぱいに
                        with info_col:
                            m_cols = st.columns(3)
                            m_cols[0].metric("現在値", f"¥{row['現在値']:,.1f}" if isinstance(row['現在値'], (int, float)) else row['現在値'])
                            m_cols[1].metric("消灯目安", f"¥{row['消灯目安(安値)']:,.1f}" if isinstance(row['消灯目安(安値)'], (int, float)) else row['消灯目安(安値)'])
                            m_cols[2].metric("200日乖離", f"{row['乖離率(%)']}%")
                            
                            m_cols2 = st.columns(4)
                            m_cols2[0].metric("WVF", row['WVF'])
                            m_cols2[1].metric("WVF Upper", row['WVF Upper'])
                            m_cols2[2].metric("200MA傾き率", f"{row['200MA傾き率']:.5f}")
                            m_cols2[3].metric("シグナル日", row['シグナル日'])
else:
    st.info("左の「スクリーニング開始」ボタンを押すか、履歴から読み込んでください。")
