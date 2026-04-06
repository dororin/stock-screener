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
    save_df = df.copy()
    save_df['screening_id'] = screening_id
    try:
        existing_data = conn.read()
        updated_data = pd.concat([existing_data, save_df], ignore_index=True)
    except Exception:
        updated_data = save_df
    conn.update(data=updated_data)
    return screening_id

def get_history_list():
    if conn is None: return []
    try:
        df = conn.read(ttl=0)
        if df is None or df.empty or 'screening_id' not in df.columns:
            return []
        ids = df['screening_id'].unique().tolist()
        return sorted(ids, reverse=True)
    except Exception: return []

def load_history(screening_id):
    if conn is None: return pd.DataFrame()
    try:
        df = conn.read()
        target_df = df[df['screening_id'] == screening_id].copy()
        if not target_df.empty and 'コード' in target_df.columns:
            target_df['コード'] = target_df['コード'].astype(str).str.replace(r'\.0$', '', regex=True)
        if not target_df.empty and 'お気に入り' not in target_df.columns:
            target_df['お気に入り'] = False
        return target_df
    except Exception: return pd.DataFrame()

def delete_history(screening_id):
    if conn is None: return False
    try:
        df = conn.read()
        new_df = df[df['screening_id'] != screening_id].copy()
        conn.update(data=new_df)
        return True
    except Exception: return False

# --- マーケット情報用定数 ---
MARKET_DATA_URL = "https://docs.google.com/spreadsheets/d/1vaX2dKcHO_fo_KMffNiC98pY1fzfMkHCRkHE1IFE0PI/edit"

def fetch_sinyou_data():
    url = "https://nikkei225jp.com/_data/_nfsWEB/DAY/dailyweek2.json"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://nikkei225jp.com/data/sinyou.php"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        res.encoding = 'utf-8'
        if res.status_code != 200: return pd.DataFrame()
        json_text = res.text.strip().replace("var DAILY =", "").strip().rstrip(";")
        raw_rows = json.loads(json_text)
        data = []
        for r in raw_rows:
            if len(r) >= 7 and r[4] != "" and r[6] != "":
                data.append({
                    'Date': pd.to_datetime(r[0], unit='ms'),
                    'Nikkei225': float(r[1]) if r[1] != "" else np.nan,
                    'Sell(M-yen)': int(str(r[4]).replace(',', '')),
                    'Buy(M-yen)': int(str(r[6]).replace(',', ''))
                })
        df = pd.DataFrame(data)
        if not df.empty:
            df['Date'] = df['Date'].dt.tz_localize(None)
            df = df.sort_values('Date').reset_index(drop=True)
        return df
    except Exception: return pd.DataFrame()

def update_and_load_sinyou_data():
    if conn is None: return pd.DataFrame()
    try:
        existing_df = conn.read(spreadsheet=MARKET_DATA_URL, worksheet="sinyou_data", ttl=0)
        if existing_df is not None and not existing_df.empty:
            existing_df['Date'] = pd.to_datetime(existing_df['Date'], errors='coerce')
            existing_df = existing_df.dropna(subset=['Sell(M-yen)', 'Buy(M-yen)'], how='any').copy()
        else: existing_df = pd.DataFrame()
    except: existing_df = pd.DataFrame()
    web_df = fetch_sinyou_data()
    
    # 統合処理
    if web_df.empty:
        merged_df = existing_df
    elif existing_df.empty:
        merged_df = web_df
    else:
        merged_df = pd.concat([existing_df, web_df])
        
    if not merged_df.empty:
        # 日付の正規化と重複排除を徹底
        merged_df['Date'] = pd.to_datetime(merged_df['Date']).dt.normalize()
        merged_df = merged_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
        
    # 保存の必要性チェック（最新日付が既存データより新しい場合のみ保存）
    try:
        should_update = True
        if not existing_df.empty and not merged_df.empty:
            last_existing = pd.to_datetime(existing_df['Date']).max()
            last_merged = merged_df['Date'].max()
            if last_merged <= last_existing:
                should_update = False
        
        if should_update and not merged_df.empty:
            save_df = merged_df.copy()
            save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
            conn.update(spreadsheet=MARKET_DATA_URL, worksheet="sinyou_data", data=save_df)
    except:
        pass
        
    return merged_df

def plot_market_dashboard(saitei_df, sinyou_df):
    if saitei_df.empty and sinyou_df.empty: return None
    s_df = saitei_df.copy()
    if not s_df.empty:
        s_df['Date'] = pd.to_datetime(s_df['Date'])
        s_df.columns = [str(c).lower().strip() for c in s_df.columns]
        nik_col = 'nikkei225' if 'nikkei225' in s_df.columns else 'nikkei'
        buy_col = 'buy(oku-yen)' if 'buy(oku-yen)' in s_df.columns else 'buy'
        s_df['ratio'] = s_df[buy_col] / s_df[nik_col]
    m_df = sinyou_df.copy()
    if not m_df.empty:
        m_df['Date'] = pd.to_datetime(m_df['Date'])
        m_df['ratio'] = m_df['Buy(M-yen)'] / m_df['Nikkei225']
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.02,
                        row_heights=[0.3, 0.2, 0.2, 0.3], specs=[[{"secondary_y": True}], [{}], [{}], [{}]],
                        subplot_titles=('日経平均 & 裁定倍率 (右軸)', '裁定買残 (億円)', '信用買残 / 日経比率', '信用売り残・買い残 (百万円)'))
    if not s_df.empty:
        fig.add_trace(go.Scatter(x=s_df['date'], y=s_df[nik_col], mode='lines+markers', name='日経平均', line=dict(color='orange', width=2), marker=dict(size=4)), row=1, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(x=s_df['date'], y=s_df['ratio'], mode='lines+markers', name='裁定倍率', line=dict(color='red', width=2), marker=dict(size=4)), row=1, col=1, secondary_y=True)
        fig.add_trace(go.Bar(x=s_df['date'], y=s_df[buy_col], name='裁定買残', marker_color='#1f77b4'), row=2, col=1)
    if not m_df.empty:
        fig.add_trace(go.Scatter(x=m_df['Date'], y=m_df['ratio'], mode='lines+markers', name='信用比率', line=dict(color='green', width=2), fill='tozeroy', fillcolor='rgba(0, 255, 0, 0.1)'), row=3, col=1)
        fig.add_trace(go.Scatter(x=m_df['Date'], y=m_df['Buy(M-yen)'], mode='lines+markers', name='信用買い残', line=dict(color='rgba(255, 0, 0, 0.8)', width=2)), row=4, col=1)
        fig.add_trace(go.Scatter(x=m_df['Date'], y=m_df['Sell(M-yen)'], mode='lines+markers', name='信用売り残', line=dict(color='rgba(0, 0, 255, 0.8)', width=2)), row=4, col=1)
    # 全体レイアウト設定 (エラーの原因となっていたspikedash等の不正な引数を削除)
    fig.update_layout(
        height=1000,
        margin=dict(l=20, r=60, t=50, b=20),
        showlegend=False,
        hovermode='x',
        dragmode='pan'
    )
    
    # 全てのx軸に対して縦線（クロスヘア）の設定を適用
    fig.update_xaxes(
        showspikes=True,
        spikemode='across',
        spikesnap='cursor',
        spikethickness=1,
        spikedash='solid',
        spikecolor='#999',
        showline=True
    )
    
    # 2段目以降のx軸のスパイクも確実に出すための調整
    fig.update_yaxes(showspikes=False)
    fig.update_yaxes(title_text="株価", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="倍率", row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text="億円", row=2, col=1)
    fig.update_yaxes(title_text="比率", row=3, col=1)
    fig.update_yaxes(title_text="百万円", row=4, col=1)
    
    return fig

def parse_saitei_amount(val):
    try:
        if not val or val == "": return np.nan
        return int(str(val).replace(',', '').strip()) // 100
    except: return np.nan

def fetch_saitei_data():
    url = "https://nikkei225jp.com/_data/_nfsWEB/HS_DATA_DAY/daily_saitei.json"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://nikkei225jp.com/data/saitei.php"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        res.encoding = 'utf-8'
        if res.status_code != 200: return pd.DataFrame()
        text = res.text.strip().replace("var DAILY =", "").strip().rstrip(";")
        raw = json.loads(text)
        data = []
        for r in raw:
            if len(r) >= 9 and r[7] != "" and r[8] != "":
                data.append({'Date': pd.to_datetime(r[0], unit='ms'), 'Nikkei225': float(r[1]) if r[1] != "" else np.nan, 'Sell(Oku-yen)': parse_saitei_amount(r[7]), 'Buy(Oku-yen)': parse_saitei_amount(r[8])})
        df = pd.DataFrame(data)
        if not df.empty:
            df['Date'] = df['Date'].dt.tz_localize(None).dropna()
            df = df.sort_values('Date').reset_index(drop=True)
        return df
    except: return pd.DataFrame()

def update_and_load_saitei_data():
    if conn is None: return pd.DataFrame()
    try:
        existing_df = conn.read(spreadsheet=MARKET_DATA_URL, worksheet="saitei_data", ttl=0)
        if existing_df is not None and not existing_df.empty:
            existing_df['Date'] = pd.to_datetime(existing_df['Date'], errors='coerce')
            existing_df = existing_df.dropna(subset=['Sell(Oku-yen)', 'Buy(Oku-yen)'], how='any').copy()
        else: existing_df = pd.DataFrame(columns=['Date', 'Nikkei225', 'Sell(Oku-yen)', 'Buy(Oku-yen)'])
    except: existing_df = pd.DataFrame(columns=['Date', 'Nikkei225', 'Sell(Oku-yen)', 'Buy(Oku-yen)'])
    web_df = fetch_saitei_data()
    
    # 統合処理
    if web_df.empty:
        merged_df = existing_df
    elif existing_df.empty:
        merged_df = web_df
    else:
        merged_df = pd.concat([existing_df, web_df])
        
    if not merged_df.empty:
        # 日付の正規化と重複排除を徹底
        merged_df['Date'] = pd.to_datetime(merged_df['Date']).dt.normalize()
        merged_df = merged_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
        
    # 保存の必要性チェック（最新日付が既存データより新しい場合のみ保存）
    try:
        should_update = True
        if not existing_df.empty and not merged_df.empty:
            last_existing = pd.to_datetime(existing_df['Date']).max()
            last_merged = merged_df['Date'].max()
            if last_merged <= last_existing:
                should_update = False
                
        if should_update and not merged_df.empty:
            save_df = merged_df.copy()
            save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
            conn.update(spreadsheet=MARKET_DATA_URL, worksheet="saitei_data", data=save_df)
    except:
        pass
        
    return merged_df

def generate_mini_chart_base64(df):
    try:
        plot_df = df.tail(60).copy()
        buf = io.BytesIO()
        mc = mpf.make_marketcolors(up='green', down='red', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)
        add_plots = []
        if 'sma50' in plot_df.columns: add_plots.append(mpf.make_addplot(plot_df['sma50'], color='orange', width=0.7))
        if 'sma200' in plot_df.columns: add_plots.append(mpf.make_addplot(plot_df['sma200'], color='red', width=1.0))
        fig, axlist = mpf.plot(plot_df, type='candle', style=s, addplot=add_plots, figsize=(4, 2.5), tight_layout=True, returnfig=True, axisoff=True)
        fig.set_facecolor('#f0f2f6')
        for ax in axlist: ax.set_facecolor('#f0f2f6')
        fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0)
        plt.close(fig)
        return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"
    except: return None

@st.cache_data(ttl=86400)
def get_jpx_list():
    url = 'https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls'
    try:
        df = pd.read_excel(url)
        df = df.iloc[:, [1, 2, 3, 9]]
        target = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
        df = df.loc[df["規模区分"].isin(target)].iloc[:, [0, 1]]
        df.columns = ['symbol', 'name']
        return df
    except: return pd.DataFrame()

def analyze_market_streamlit(df_targets):
    if df_targets.empty: return pd.DataFrame()
    tickers = [f"{code}.T" for code in df_targets['symbol'].tolist()]
    total = len(tickers)
    progress_bar = st.progress(0)
    status_text = st.empty()
    try:
        batch_size = 50
        all_dfs = []
        for i in range(0, total, batch_size):
            batch = tickers[i : i + batch_size]
            status_text.text(f"ダウンロード中... ({i}〜{min(i+batch_size, total)} / {total})")
            batch_data = yf.download(batch, period="1y", interval="1d", group_by='ticker', auto_adjust=False, actions=True, threads=True, progress=False)
            if not batch_data.empty: all_dfs.append(batch_data)
        data = pd.concat(all_dfs, axis=1)
    except: return pd.DataFrame()
    results = []
    for i, ticker_symbol in enumerate(tickers):
        try:
            if i % 10 == 0:
                progress_bar.progress((i + 1) / total)
                status_text.text(f"分析中: {ticker_symbol} ({i+1}/{total})")
            df = data[ticker_symbol].copy() if total > 1 else data.copy()
            df.dropna(how='all', inplace=True)
            if len(df) < 220: continue
            new_cols = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]
            df.columns = new_cols
            if 'stock splits' in df.columns:
                splits = df['stock splits'].replace(0, 1)
                df['split_factor'] = (1 / splits).iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
                for col in ['open', 'high', 'low', 'close']: df[col] = df[col] * df['split_factor']
            df['sma50'] = df['close'].rolling(window=50).mean()
            df['sma200'] = df['close'].rolling(window=200).mean()
            df['highest_close'] = df['close'].rolling(window=11).max()
            df['wvf'] = (df['highest_close'] - df['low']) / df['highest_close'] * 100
            df['wvf_std'] = df['wvf'].rolling(window=20).std(ddof=0)
            df['wvf_mid'] = df['wvf'].rolling(window=20).mean()
            df['wvf_upper'] = df['wvf_mid'] + (2.0 * df['wvf_std'])
            df['range_high'] = df['wvf'].rolling(window=100).max() * 0.85
            latest, prev = df.iloc[-1], df.iloc[-2]
            sma200_win = df['sma200'].tail(20).values
            slope, _ = np.polyfit(np.arange(len(sma200_win)), sma200_win, 1)
            slope_rate = slope / latest['close']
            is_uptrend = (latest['close'] > latest['sma200']) or (slope_rate >= -0.0001)
            is_wvf_lit = latest['wvf'] >= latest['wvf_upper'] or latest['wvf'] >= latest['range_high']
            if is_uptrend and is_wvf_lit and latest['wvf'] >= 5.0:
                code = ticker_symbol.replace('.T', '')
                matching = df_targets[df_targets['symbol'] == int(code)]
                name = matching['name'].values[0] if not matching.empty else "-"
                ext_price = min(max(latest['highest_close'] * (1 - latest['wvf_upper'] / 100), latest['highest_close'] * (1 - latest['range_high'] / 100)), latest['highest_close'] * (1 - 5.0 / 100))
                results.append({'チャート': generate_mini_chart_base64(df), 'シグナル日': latest.name.strftime('%Y-%m-%d'), 'コード': f"{code}", '銘柄': name, '現在値': round(latest['close'], 1), '消灯目安(安値)': round(ext_price, 1), 'SMA200': round(latest['sma200'], 1), '乖離率(%)': round((latest['close'] - latest['sma200']) / latest['sma200'] * 100, 2), '200MA傾き率': round(slope_rate, 6), 'WVF': round(latest['wvf'], 2), 'WVF Upper': round(latest['wvf_upper'], 2), 'お気に入り': False})
        except: continue
    progress_bar.empty(); status_text.empty()
    return pd.DataFrame(results)

if 'result_df' not in st.session_state: st.session_state.result_df = pd.DataFrame()
if 'saitei_df' not in st.session_state: st.session_state.saitei_df = pd.DataFrame()
if 'sinyou_df' not in st.session_state: st.session_state.sinyou_df = pd.DataFrame()

with st.sidebar:
    selected_page = st.radio("画面選択", ["スクリーニング", "マーケット情報"])
    st.divider()

if selected_page == "マーケット情報":
    st.title("📈 マーケット情報")
    if st.button("データ取得・更新", type="primary"):
        with st.spinner("更新中..."):
            df_s = update_and_load_saitei_data()
            if not df_s.empty: st.session_state.saitei_df = df_s
            df_m = update_and_load_sinyou_data()
            if not df_m.empty: st.session_state.sinyou_df = df_m
            st.success("更新完了")
    st.write("---")
    col1, col2 = st.columns([2, 3])
    with col1: st.subheader("📊 分析ダッシュボード")
    with col2: period = st.radio("期間:", ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "3年", "全"], index=3, horizontal=True, label_visibility="collapsed")
    end_dt = st.session_state.saitei_df['Date'].max() if not st.session_state.saitei_df.empty else pd.Timestamp.now()
    if period == "1ヶ月": start_dt = end_dt - pd.DateOffset(months=1)
    elif period == "3ヶ月": start_dt = end_dt - pd.DateOffset(months=3)
    elif period == "6ヶ月": start_dt = end_dt - pd.DateOffset(months=6)
    elif period == "1年": start_dt = end_dt - pd.DateOffset(years=1)
    elif period == "3年": start_dt = end_dt - pd.DateOffset(years=3)
    else: start_dt = st.session_state.saitei_df['Date'].min() if not st.session_state.saitei_df.empty else end_dt - pd.DateOffset(years=10)
    if not st.session_state.saitei_df.empty or not st.session_state.sinyou_df.empty:
        fig = plot_market_dashboard(st.session_state.saitei_df, st.session_state.sinyou_df)
        if fig:
            fig.update_xaxes(range=[start_dt, end_dt + pd.Timedelta(days=7)])
            if not st.session_state.saitei_df.empty:
                v = st.session_state.saitei_df[(st.session_state.saitei_df['Date'] >= start_dt) & (st.session_state.saitei_df['Date'] <= end_dt)]
                if not v.empty: fig.update_yaxes(range=[v['Nikkei225'].min()*0.98, v['Nikkei225'].max()*1.02], row=1, col=1, secondary_y=False)
            fig.update_yaxes(fixedrange=True)
            st.plotly_chart(fig, use_container_width=True, config={'scrollZoom': True})
    else: st.info("「データ取得・更新」ボタンを押してください。")
    st.stop()

with st.sidebar:
    st.subheader("スクリーニング操作")
    with st.expander("📂 履歴", expanded=True):
        ids = get_history_list()
        if ids:
            sid = st.selectbox("過去の結果", ["-- 選択 --"] + ids, key="h_sel")
            if sid != "-- 選択 --" and st.session_state.get('last_id') != sid:
                st.session_state.result_df = load_history(sid); st.session_state.last_id = sid
            if sid != "-- 選択 --":
                if st.button("削除", type="primary"):
                    if delete_history(sid): st.session_state.result_df = pd.DataFrame(); st.session_state.last_id = None; st.rerun()
    list_src = st.radio("取得元", ["JPX (TOPIX)", "CSV"], label_visibility="collapsed")
    csv = st.file_uploader("CSV", type=["csv"]) if list_src == "CSV" else None
    if st.button("開始", use_container_width=True):
        df_t = pd.DataFrame()
        if list_src == "JPX (TOPIX)": df_t = get_jpx_list()
        elif csv:
            try:
                try: df_c = pd.read_csv(csv)
                except: csv.seek(0); df_c = pd.read_csv(csv, encoding='shift_jis')
                df_t['symbol'] = pd.to_numeric(df_c[next(c for c in ["コード", "銘柄コード", "symbol"] if c in df_c.columns)], errors='coerce').dropna().astype(int)
                df_t['name'] = df_c[next(c for c in ["銘柄", "name"] if c in df_c.columns)] if any(c in df_c.columns for c in ["銘柄", "name"]) else "-"
            except: st.error("CSVエラー")
        if not df_t.empty: st.session_state.result_df = analyze_market_streamlit(df_t); st.session_state.last_id = None
    if not st.session_state.result_df.empty:
        if st.button("結果を保存", use_container_width=True):
            if save_history(st.session_state.result_df): st.rerun()

st.title("WVF + Trend Screener :blue[Pro]")
if not st.session_state.result_df.empty:
    rdf = st.session_state.result_df
    for i in range(0, len(rdf), 2):
        cols = st.columns(2)
        for j in range(2):
            if i + j < len(rdf):
                r = rdf.iloc[i + j]
                with cols[j]:
                    with st.container(border=True):
                        c1, c2 = st.columns([0.85, 0.15])
                        c1.subheader(f"[{r['コード']}](https://jp.tradingview.com/chart/?symbol=TSE%3A{r['コード']}) {r['銘柄']}")
                        if c2.toggle("⭐", value=r['お気に入り'], key=f"f_{r['コード']}_{i+j}", label_visibility="collapsed") != r['お気に入り']:
                            st.session_state.result_df.at[i + j, 'お気に入り'] = not r['お気に入り']
                        i1, i2 = st.columns([1, 2])
                        if r['チャート']: i1.image(r['チャート'], use_container_width=True)
                        m1 = i2.columns(3)
                        m1[0].metric("現在値", f"¥{r['現在値']:,.1f}"); m1[1].metric("消灯目安", f"¥{r['消灯目安(安値)']:,.1f}"); m1[2].metric("200日乖離", f"{r['乖離率(%)']}%")
                        m2 = i2.columns(4)
                        m2[0].metric("WVF", r['WVF']); m2[1].metric("Upper", r['WVF Upper']); m2[2].metric("傾き", f"{r['200MA傾き率']:.5f}"); m2[3].metric("日", r['シグナル日'])
else: st.info("開始ボタンを押してください。")
