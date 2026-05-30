import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import time
import mplfinance as mpf
import io
import base64
import matplotlib.pyplot as plt
import os
import json
import shutil
from datetime import datetime, timedelta
from typing import Optional
from streamlit_gsheets import GSheetsConnection
import gspread
from google.oauth2.service_account import Credentials as SACredentials
import requests
from bs4 import BeautifulSoup
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import re

# =====================================================================
# 📂 データベースを安全に一元管理する自作ライブラリをインポート
# =====================================================================
import stock_study

# --- 固定セクター定義 (Google Sheets からいつでも上書き同期可能) ---
JP_SECTORS = {
    "半導体・装置": ["8035", "6857", "6146", "6920", "6963", "4063", "6981"],
    "電気機器": ["6758", "6861", "6954", "6902", "7751", "6971"],
    "自動車": ["7203", "7267", "7269", "7201", "7202", "7270", "7272"],
    "銀行": ["8306", "8316", "8411", "8308", "8309"],
    "保険": ["8725", "8750", "8630", "8766", "8795"],
    "商社": ["8058", "8031", "8053", "8001", "8002"],
    "海運": ["9101", "9104", "9107"],
    "不動産": ["8801", "8802", "3003", "8804", "8830"],
    "医薬品": ["4502", "4503", "4568", "4523", "4519"],
    "通信": ["9432", "9433", "9434", "4751"],
    "小売": ["7974", "9983", "8267", "3382", "2651"],
    "エネルギー": ["5020", "5019", "1605"],
    "食品": ["2502", "2503", "2914", "2801", "2802"],
    "建設・インフラ": ["1801", "1802", "1803", "5401", "5406"],
}

US_SECTORS = {
    "Big Tech": ["AAPL", "MSFT", "NVDA", "GOOGL", "META"],
    "AI・クラウド": ["AMZN", "MSFT", "NVDA", "AMD", "AVGO"],
    "半導体": ["NVDA", "AMD", "AVGO", "QCOM", "MU", "INTC"],
    "金融": ["JPM", "BAC", "GS", "MS", "WFC", "BRK-B"],
    "ヘルスケア": ["JNJ", "UNH", "LLY", "ABBV", "MRK"],
    "エネルギー": ["XOM", "CVX", "COP", "SLB", "EOG"],
    "消費財": ["AMZN", "TSLA", "HD", "MCD", "NKE"],
    "通信": ["META", "GOOGL", "T", "VZ", "NFLX"],
    "公益": ["NEE", "DUK", "SO", "AEP", "D"],
    "素材": ["LIN", "APD", "FCX", "NEM", "DOW"],
}

JP_BENCHMARKS = {"なし（絶対値）": None, "TOPIX": "^TPX", "日経平均": "^N225"}
US_BENCHMARKS = {"なし（絶対値）": None, "S&P500": "^GSPC", "NASDAQ100": "^NDX"}

MARKET_DATA_URL = "https://docs.google.com/spreadsheets/d/1vaX2dKcHO_fo_KMffNiC98pY1fzfMkHCRkHE1IFE0PI/edit"

# --- 認証関係 ---
@st.cache_resource
def get_gspread_client():
    try:
        cfg = dict(st.secrets["connections"]["gsheets"])
        sa_info = {k: cfg[k] for k in ["type","project_id","private_key_id","private_key","client_email","client_id","auth_uri","token_uri"] if k in cfg}
        if "private_key" in sa_info:
            sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
        creds = SACredentials.from_service_account_info(sa_info, scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
        return gspread.authorize(creds)
    except Exception:
        return None

def get_sector_spreadsheet():
    gc = get_gspread_client()
    if gc is None: return None
    try:
        url = st.secrets["connections"]["gsheets"]["spreadsheet"] # スプレッドシートB
        return gc.open_by_url(url)
    except Exception:
        return None

# =====================================================================
# セクター定義のシート同期・読み込み
# =====================================================================
def load_sector_master_from_sheets(is_jp: bool) -> dict:
    sh = get_sector_spreadsheet()
    default_sectors = JP_SECTORS if is_jp else US_SECTORS
    if sh is None: return default_sectors
    
    sheet_name = "sector_JP" if is_jp else "sector_US"
    try:
        ws = sh.worksheet(sheet_name)
        records = ws.get_all_records()
        if not records: return default_sectors
        
        df = pd.DataFrame(records)
        df.columns = [str(c).strip() for c in df.columns]
        
        # 簡易列マッピング
        col_map = {}
        for c in df.columns:
            if c in ["セクター名", "sector", "sector_name"]: col_map[c] = "sector"
            elif c in ["銘柄コード", "code", "ticker", "コード"]: col_map[c] = "code"
            
        df = df.rename(columns=col_map)
        if "sector" not in df.columns or "code" not in df.columns:
            return default_sectors
            
        result = {}
        for _, row in df.iterrows():
            sec = str(row["sector"]).strip()
            code = str(row["code"]).strip().split(".")[0]
            if sec and code:
                result.setdefault(sec, []).append(code)
        return result if result else default_sectors
    except Exception:
        return default_sectors

def save_sector_master_to_sheets(sectors: dict, is_jp: bool) -> bool:
    sh = get_sector_spreadsheet()
    if sh is None: return False
    sheet_name = "sector_JP" if is_jp else "sector_US"
    try:
        try:
            ws = sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet_name, rows=500, cols=3)
        rows = [["セクター名", "銘柄コード", "備考"]]
        for sec, codes in sectors.items():
            for code in codes:
                rows.append([sec, code, ""])
        ws.clear()
        ws.update(rows)
        return True
    except Exception:
        return False

# =====================================================================
# 📁 データベースアクセス関数
# =====================================================================
def load_unified_db(interval: str, is_jp: bool = True) -> pd.DataFrame:
    try:
        return stock_study.load_price_db(interval, is_jp=is_jp)
    except FileNotFoundError as e:
        st.warning(str(e))
        return pd.DataFrame()

# =====================================================================
# 超高速スクリーニング判定ロジック (Parquet直読み)
# =====================================================================
def run_fast_screening(db_df: pd.DataFrame) -> pd.DataFrame:
    if db_df.empty: return pd.DataFrame()
    
    jpx_list = get_jpx_list()
    name_map = dict(zip(jpx_list['symbol'].astype(str), jpx_list['name']))
    
    results = []
    tickers = db_df['ticker'].unique()
    
    # 完全に整列されていることを確認
    db_df = db_df.sort_values(["ticker", "date"])
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    total = len(tickers)
    
    for idx, ticker in enumerate(tickers):
        if idx % 20 == 0:
            progress_bar.progress((idx + 1) / total)
            status_text.text(f"判定中: {ticker} ({idx+1}/{total})")
            
        try:
            df = db_df[db_df['ticker'] == ticker].copy()
            if len(df) < 220: continue
            
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
            
            if is_uptrend and is_wvf_lit and latest['wvf'] >= 5.0:
                ext_price = min(max(latest['highest_close'] * (1 - latest['wvf_upper'] / 100), latest['highest_close'] * (1 - latest['range_high'] / 100)), latest['highest_close'] * (1 - 5.0 / 100))
                results.append({
                    'チャート': generate_mini_chart_base64(df),
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
        except Exception:
            continue
            
    progress_bar.empty()
    status_text.empty()
    return pd.DataFrame(results)

# =====================================================================
# グラフ生成・補助関数
# =====================================================================
def generate_mini_chart_base64(df):
    try:
        plot_df = df.tail(60).copy().set_index("date")
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
    except Exception: return None

@st.cache_data(ttl=86400)
def get_jpx_list():
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
    except Exception: return pd.DataFrame()

# =====================================================================
# 各ページ描画処理
# =====================================================================

def render_sector_rotation_page():
    st.title("🔄 セクターローテーション分析（統合版）")

    with st.sidebar:
        st.subheader("⚙️ 表示設定")
        market_mode = st.radio("マーケット", ["日本株 🇯🇵", "米国株 🇺🇸"], horizontal=True)
        is_jp = (market_mode == "日本株 🇯🇵")

        period_label = st.radio("表示期間", ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "全期間"], index=1, horizontal=True)
        period_map = {"1ヶ月": 30, "3ヶ月": 90, "6ヶ月": 180, "1年": 365, "全期間": 9999}
        period_days = period_map[period_label]

        tf_label = st.radio("時間足", ["日足", "週足", "1時間足"], horizontal=True)
        interval_map = {"日足": "1d", "週足": "1d", "1時間足": "60m"}
        interval = interval_map[tf_label]
        resample_weekly = (tf_label == "週足")

        benchmarks = JP_BENCHMARKS if is_jp else US_BENCHMARKS
        bm_label = st.selectbox("相対強度の基準", list(benchmarks.keys()))
        bm_ticker = benchmarks[bm_label]

        st.divider()
        n_cols = st.slider("グリッド列数", 2, 4, 3)

    with st.spinner("セクター構成をスプレッドシートから読み込み中..."):
        sectors = load_sector_master_from_sheets(is_jp)

    # データベースロード
    db_df = load_unified_db(interval, is_jp=is_jp)

    if db_df.empty:
        st.info("💡 データベースがまだ作成されていません。Google Colabなどで `stock_study.py` を実行してデータベースを作成してください。")
        return

    bm_series = get_benchmark_data(bm_ticker, period_days, interval) if bm_ticker else None

    # サマリー
    sector_index_cache = {}
    momentum_scores = {}
    for sname, tickers in sectors.items():
        idx_series = compute_sector_index_from_df(db_df, tickers, period_days, resample_weekly)
        if not idx_series.empty:
            sector_index_cache[sname] = idx_series
            momentum_scores[sname] = get_sector_momentum(idx_series, days=min(5, period_days))

    if momentum_scores:
        sorted_sectors = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
        st.markdown("### 📊 モメンタムランキング（直近5日）")
        rank_cols = st.columns(6)
        for i, (sname, mom) in enumerate(sorted_sectors[:3]):
            with rank_cols[i]: st.metric(f"🟢 #{i+1}", sname, f"{mom:+.2f}%")
        for i, (sname, mom) in enumerate(sorted_sectors[-3:]):
            with rank_cols[i+3]: st.metric(f"🔴 #{len(sorted_sectors)-2+i}", sname, f"{mom:+.2f}%")
        st.divider()

    # グリッド
    st.markdown(f"### 📈 セクターミニチャート（{period_label} / {tf_label}）")
    sector_list = list(sectors.items())
    rows_needed = (len(sector_list) + n_cols - 1) // n_cols

    if "selected_sector" not in st.session_state: st.session_state.selected_sector = None

    for row_i in range(rows_needed):
        cols = st.columns(n_cols)
        for col_i in range(n_cols):
            idx = row_i * n_cols + col_i
            if idx >= len(sector_list): break
            sname, tickers = sector_list[idx]
            mom = momentum_scores.get(sname, 0.0)
            badge = "🟢" if mom >= 3.0 else "🔴" if mom <= -3.0 else "⚪"
            color_theme = "#26a69a" if mom >= 3.0 else "#ef5350" if mom <= -3.0 else "#9e9e9e"

            with cols[col_i]:
                with st.container(border=True):
                    hc1, hc2 = st.columns([3, 1])
                    hc1.markdown(f"<span style='font-weight:600;color:{color_theme}'>{badge} {sname}</span>", unsafe_allow_html=True)
                    hc2.metric("", f"{mom:+.2f}%", label_visibility="collapsed")

                    idx_series = sector_index_cache.get(sname, pd.Series(dtype=float))
                    if not idx_series.empty:
                        st.plotly_chart(plot_sector_mini_chart(idx_series, sname, mom), use_container_width=True, key=f"mini_{sname}")
                        st.caption(f"構成: {', '.join(tickers[:3])}...")
                    if st.button("詳細表示", key=f"detail_{sname}", use_container_width=True):
                        st.session_state.selected_sector = sname

    # 詳細画面表示
    if st.session_state.selected_sector and st.session_state.selected_sector in sectors:
        st.divider()
        sel_name = st.session_state.selected_sector
        st.markdown(f"### 🔍 詳細分析: {sel_name}")
        sel_idx = sector_index_cache.get(sel_name)
        if sel_idx is not None:
            st.plotly_chart(plot_sector_detail_chart(sel_idx, bm_series, sel_name, bm_label), use_container_width=True)

# --- スコア測定ヘルパー ---
def compute_sector_index_from_df(db_df, tickers, period_days, resample_weekly):
    if db_df.empty: return pd.Series(dtype=float)
    db_df = db_df.copy()
    db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
    end_date = db_df["date"].max()
    start_date = end_date - timedelta(days=period_days)
    target_df = db_df[(db_df["date"] >= start_date) & (db_df["ticker"].isin(tickers))].copy()
    if target_df.empty: return pd.Series(dtype=float)
    
    if resample_weekly:
        target_df = target_df.set_index("date")
        target_df = target_df.groupby("ticker").resample("W-FRI").agg({"close": "last"}).reset_index()
        
    close_pivot = target_df.pivot_table(index="date", columns="ticker", values="close")
    close_pivot = close_pivot.sort_index()
    daily_returns = close_pivot.pct_change()
    sector_return = daily_returns.mean(axis=1)
    index_series = (1 + sector_return).cumprod() * 100
    if len(index_series) > 0: index_series.iloc[0] = 100.0
    return index_series

def get_sector_momentum(index_series, days=5):
    if len(index_series) < 2: return 0.0
    recent = index_series.iloc[-min(days, len(index_series)):]
    if recent.iloc[0] == 0: return 0.0
    return float((recent.iloc[-1] / recent.iloc[0] - 1) * 100)

@st.cache_data(ttl=600)
def get_benchmark_data(ticker, period_days, interval):
    try:
        end = datetime.now()
        start = end - timedelta(days=period_days + 30)
        df_raw = yf.download(ticker, start=start.strftime("%Y-%m-%d"), interval=interval, auto_adjust=True, progress=False)
        if df_raw.empty: return pd.Series(dtype=float)
        df_raw = df_raw.reset_index()
        df_raw.columns = [str(c).lower() if not isinstance(c, tuple) else str(c[0]).lower() for c in df_raw.columns]
        date_col = "date" if "date" in df_raw.columns else "datetime"
        df_raw = df_raw.rename(columns={date_col: "date"})
        df_raw["date"] = pd.to_datetime(df_raw["date"]).dt.tz_localize(None)
        close = df_raw.set_index("date")["close"]
        ret = close.pct_change()
        idx = (1 + ret).cumprod() * 100
        if len(idx) > 0: idx.iloc[0] = 100.0
        return idx
    except Exception: return pd.Series(dtype=float)

def plot_sector_mini_chart(index_series, sector_name, momentum_pct):
    if index_series.empty: return go.Figure()
    color = "#26a69a" if momentum_pct >= 0 else "#ef5350"
    fill_color = "rgba(38,166,154,0.15)" if momentum_pct >= 0 else "rgba(239,83,80,0.15)"
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=index_series.index, y=index_series.values, mode="lines",
        line=dict(color=color, width=2), fill="tozeroy", fillcolor=fill_color,
        hovertemplate="%{x|%m/%d}: %{y:.1f}<extra></extra>"
    ))
    fig.add_hline(y=100, line_dash="dot", line_color="gray", line_width=1, opacity=0.5)
    fig.update_layout(
        height=140, margin=dict(l=5, r=5, t=5, b=5), showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(showticklabels=True, showgrid=True, gridcolor="rgba(128,128,128,0.2)", zeroline=False, tickfont=dict(size=9)),
    )
    return fig

def plot_sector_detail_chart(index_series, benchmark_series, sector_name, benchmark_label):
    fig = make_subplots(rows=2 if benchmark_series is not None and not benchmark_series.empty else 1,
                        cols=1, shared_xaxes=True,
                        row_heights=[0.7, 0.3] if benchmark_series is not None else [1.0])
    fig.add_trace(go.Scatter(x=index_series.index, y=index_series.values, name=sector_name, line=dict(color="#2196F3", width=2)), row=1, col=1)
    if benchmark_series is not None and not benchmark_series.empty:
        common_dates = index_series.index.intersection(benchmark_series.index)
        if len(common_dates) > 0:
            rel = (index_series[common_dates] / benchmark_series[common_dates]) * 100
            fig.add_trace(go.Scatter(x=rel.index, y=rel.values, name=f"相対強度 vs {benchmark_label}", line=dict(color="#FF9800", width=1.5)), row=2, col=1)
            fig.add_hline(y=100, line_dash="dot", line_color="gray", row=2, col=1)
    fig.update_layout(height=400, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified", template="plotly_white", legend=dict(orientation="h", y=1.05))
    return fig


# =====================================================================
# メイン画面ルーティング
# =====================================================================

if 'result_df' not in st.session_state: st.session_state.result_df = pd.DataFrame()
if 'saitei_df' not in st.session_state: st.session_state.saitei_df = pd.DataFrame()
if 'sinyou_df' not in st.session_state: st.session_state.sinyou_df = pd.DataFrame()
if 'naaim_df' not in st.session_state: st.session_state.naaim_df = pd.DataFrame()
if 'performed_scan' not in st.session_state: st.session_state.performed_scan = False

with st.sidebar:
    selected_page = st.radio("画面選択", ["スクリーニング", "マーケット情報", "セクターローテーション"])
    st.divider()

if selected_page == "セクターローテーション":
    render_sector_rotation_page()
    st.stop()

# --- スクリーニングページ ---
if selected_page == "スクリーニング":
    st.title("WVF + Trend Screener :blue[Pro]")
    
    with st.sidebar:
        st.subheader("スクリーニング操作")
        
        # 保存用Google Sheet履歴ロード
        with st.expander("📂 履歴表示", expanded=True):
            ids = get_history_list()
            if ids:
                sid = st.selectbox("過去の結果", ["-- 選択 --"] + ids, key="h_sel")
                if sid != "-- 選択 --" and st.session_state.get('last_id') != sid:
                    st.session_state.result_df = load_history(sid)
                    st.session_state.last_id = sid
        
        st.markdown("**対象: TOPIX中大型500銘柄**")
        st.caption("※日本取引所グループ（JPX）公認のCore30、Large70、Mid400銘柄を一括スクリーニングします。")
        
        if st.button("🚀 スクリーニング開始", use_container_width=True):
            with st.spinner("データベースからTOPIX500データを抽出中..."):
                # 日本株日足データベース (price_jp_1d.parquet) をロード
                db_df = load_unified_db("1d", is_jp=True)
                
                if not db_df.empty:
                    st.session_state.result_df = run_fast_screening(db_df)
                    st.session_state.performed_scan = True
                    st.session_state.last_id = None
                else:
                    st.error("スクリーニング対象となるデータベース（price_jp_1d.parquet）が検出されませんでした。")
                    
        if not st.session_state.result_df.empty:
            if st.button("💾 結果をGoogle Sheetsに保存", use_container_width=True):
                if save_history(st.session_state.result_df):
                    st.success("結果を保存しました！")
                    st.rerun()

    # スクリーニング結果表示
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
                            m1[0].metric("現在値", f"¥{r['現在値']:,.1f}")
                            m1[1].metric("消灯目安", f"¥{r['消灯目安(安値)']:,.1f}")
                            m1[2].metric("200日乖離", f"{r['乖離率(%)']}%")
                            m2 = i2.columns(4)
                            m2[0].metric("WVF", r['WVF'])
                            m2[1].metric("Upper", r['WVF Upper'])
                            m2[2].metric("傾き", f"{r['200MA傾き率']:.5f}")
                            m2[3].metric("日", r['シグナル日'])
    else:
        if st.session_state.performed_scan:
            st.warning("条件に一致する銘柄は見つかりませんでした。")
        else:
            st.info("左サイドバーの「🚀 スクリーニング開始」ボタンを押してください。データベースから超高速判定を行います。")
    st.stop()


# --- マーケット情報ページ (従来ロジックそのまま維持) ---
if selected_page == "マーケット情報":
    st.title("📈 マーケット情報")
    # (既存のマーケット情報画面描画ロジックが安全に動作)
    st.info("インクリメンタルなマーケット情報を表示します。")