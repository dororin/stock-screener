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
import sys
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
# パスずれによるインポートエラーを強制防止
current_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else os.getcwd()
if current_dir not in sys.path:
    sys.path.append(current_dir)

import stock_study

# =====================================================================
# セクターローテーション用 定数・設定
# =====================================================================

# --- デフォルトセクター定義 ---
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

# --- ページ設定 ---
st.set_page_config(
    page_title="WVF Stock Screener Pro",
    page_icon="📈",
    layout="wide"
)

# --- カスタムCSS ---
st.markdown("""
    <style>
    html, body, [class*="st-"] { font-size: 0.95rem !important; }
    [data-testid="stMetricValue"] { font-size: 1.1rem !important; font-weight: 600; }
    [data-testid="stMetricLabel"] { font-size: 0.8rem !important; }
    .stMainContainer { padding-top: 2rem !important; }
    .stVerticalBlock { gap: 0.5rem !important; }
    hr { margin: 0.8rem 0 !important; }
    h3 { font-size: 1.1rem !important; margin-bottom: 0.3rem !important; }
    </style>
    """, unsafe_allow_html=True)

# =====================================================================
# Google Sheets 共通接続 & 履歴管理 (履歴保存シート)
# =====================================================================
try:
    conn = st.connection("gsheets", type=GSheetsConnection)
except Exception:
    conn = None

def save_history(df):
    if conn is None: return None
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
        if df is None or df.empty or 'screening_id' not in df.columns: return []
        return sorted(df['screening_id'].unique().tolist(), reverse=True)
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

# =====================================================================
# Google Sheets セクター定義シート (シートB)
# =====================================================================
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
        cfg = st.secrets["connections"]["gsheets"]
        # sector_spreadsheetがあればそれを使い、無ければ従来のspreadsheetを使います
        url = cfg.get("sector_spreadsheet", cfg.get("spreadsheet"))
        return gc.open_by_url(url)
    except Exception:
        return None

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

# =====================================================================
# 📂 データベースアクセス関数
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
# マーケット情報ダッシュボード関係の関数
# =====================================================================
def fetch_naaim_data():
    base_url = "https://naaim.org/programs/naaim-exposure-index/"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(base_url, headers=headers, timeout=15)
        if res.status_code != 200: return pd.DataFrame()
        soup = BeautifulSoup(res.text, "html.parser")
        links = soup.find_all("a", href=re.compile(r"\.xlsx$"))
        excel_url = None
        for link in links:
            if "HERE" in link.get_text().upper():
                excel_url = link.get('href')
                break
        if not excel_url and links: excel_url = links[0].get('href')
        if not excel_url: return pd.DataFrame()
        
        content = requests.get(excel_url, headers=headers).content
        df = pd.read_excel(io.BytesIO(content))
        df.columns = [str(c).strip() for c in df.columns]
        
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
            df = df.dropna(subset=['Date'])
            val_col = next((c for c in df.columns if 'NAAIM Number' in c or 'Mean' in c or 'Average' in c), None)
            if val_col:
                df = df[['Date', val_col]].rename(columns={val_col: 'NAAIM'})
                return df.sort_values('Date').reset_index(drop=True)
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

def update_and_load_naaim_data():
    existing_df = pd.DataFrame(columns=['Date', 'NAAIM'])
    if conn is not None:
        try:
            existing_df = conn.read(spreadsheet=MARKET_DATA_URL, worksheet="naaim_data", ttl=0)
            if existing_df is not None and not existing_df.empty:
                existing_df['Date'] = pd.to_datetime(existing_df['Date'], errors='coerce')
                existing_df = existing_df.dropna(subset=['NAAIM']).copy()
        except Exception: pass
    
    web_df = fetch_naaim_data()
    merged_df = web_df if existing_df.empty else pd.concat([existing_df, web_df]) if not web_df.empty else existing_df
        
    if not merged_df.empty:
        merged_df['Date'] = pd.to_datetime(merged_df['Date']).dt.normalize()
        merged_df = merged_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
        
    if conn is not None and not merged_df.empty:
        try:
            save_df = merged_df.copy()
            save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
            conn.update(spreadsheet=MARKET_DATA_URL, worksheet="naaim_data", data=save_df)
        except Exception: pass
    return merged_df

def fetch_irbank_margin(code):
    url = f"https://irbank.net/{code}/margin"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200: return pd.DataFrame()
        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table: return pd.DataFrame()
        rows = table.find_all("tr")
        data = []
        current_year = str(pd.Timestamp.now().year)
        for row in rows:
            if "occ" in row.get('class', []):
                year_td = row.find("td", class_="ct")
                if year_td:
                    year_val = year_td.get_text(strip=True)
                    if re.match(r"^\d{4}$", year_val): current_year = year_val
                continue
            if any(cls in row.get('class', []) for cls in ["obb", "odd"]):
                cells = row.find_all("td")
                if len(cells) < 4: continue
                date_text = cells[0].get_text(strip=True)
                if not re.match(r"^\d{1,2}/\d{1,2}$", date_text): continue
                try:
                    buy_text = cells[1].get_text(separator="|", strip=True).split("|")[0].replace(",", "")
                    sell_text = cells[3].get_text(separator="|", strip=True).split("|")[0].replace(",", "")
                    data.append({
                        'Date': pd.to_datetime(f"{current_year}/{date_text}"),
                        'Buy(Shares)': int(buy_text),
                        'Sell(Shares)': int(sell_text)
                    })
                except Exception: continue
        df = pd.DataFrame(data)
        if not df.empty:
            df = df.drop_duplicates(subset=['Date']).sort_values('Date').reset_index(drop=True)
        return df
    except Exception: return pd.DataFrame()

def plot_individual_margin(df, code):
    if df.empty: return None
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df['Date'], y=df['Buy(Shares)'], mode='lines+markers', name='信用買い残', line=dict(color='red', width=2)))
    fig.add_trace(go.Scatter(x=df['Date'], y=df['Sell(Shares)'], mode='lines+markers', name='信用売り残', line=dict(color='blue', width=2)))
    fig.update_layout(
        title=f"銘柄コード {code} : 信用残高推移 (株)",
        height=400, margin=dict(l=20, r=20, t=50, b=20),
        hovermode='x', template='plotly_white',
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        spikedistance=-1, hoverdistance=-1
    )
    fig.update_xaxes(showspikes=True, spikemode='across', spikesnap='cursor', spikedash='solid', spikethickness=1, spikecolor='#ff4b4b')
    return fig

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
    except Exception: existing_df = pd.DataFrame()
    
    web_df = fetch_sinyou_data()
    merged_df = web_df if existing_df.empty else pd.concat([existing_df, web_df]) if not web_df.empty else existing_df
        
    if not merged_df.empty:
        merged_df['Date'] = pd.to_datetime(merged_df['Date']).dt.normalize()
        merged_df = merged_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
        
    try:
        if not merged_df.empty:
            save_df = merged_df.copy()
            save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
            conn.update(spreadsheet=MARKET_DATA_URL, worksheet="sinyou_data", data=save_df)
    except Exception: pass
    return merged_df

def parse_saitei_amount(val):
    try:
        if not val or val == "": return np.nan
        return int(str(val).replace(',', '').strip()) // 100
    except Exception: return np.nan

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
                data.append({
                    'Date': pd.to_datetime(r[0], unit='ms'),
                    'Nikkei225': float(r[1]) if r[1] != "" else np.nan,
                    'Sell(Oku-yen)': parse_saitei_amount(r[7]),
                    'Buy(Oku-yen)': parse_saitei_amount(r[8])
                })
        df = pd.DataFrame(data)
        if not df.empty:
            df['Date'] = df['Date'].dt.tz_localize(None).dropna()
            df = df.sort_values('Date').reset_index(drop=True)
        return df
    except Exception: return pd.DataFrame()

def update_and_load_saitei_data():
    if conn is None: return pd.DataFrame()
    try:
        existing_df = conn.read(spreadsheet=MARKET_DATA_URL, worksheet="saitei_data", ttl=0)
        if existing_df is not None and not existing_df.empty:
            existing_df['Date'] = pd.to_datetime(existing_df['Date'], errors='coerce')
    except Exception: existing_df = pd.DataFrame()
    
    web_df = fetch_saitei_data()
    merged_df = web_df if existing_df.empty else pd.concat([existing_df, web_df]) if not web_df.empty else existing_df
        
    if not merged_df.empty:
        merged_df['Date'] = pd.to_datetime(merged_df['Date']).dt.normalize()
        merged_df = merged_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
        
    try:
        if not merged_df.empty:
            save_df = merged_df.copy()
            save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
            conn.update(spreadsheet=MARKET_DATA_URL, worksheet="saitei_data", data=save_df)
    except Exception: pass
    return merged_df

def plot_market_dashboard(saitei_df, sinyou_df, naaim_df):
    if saitei_df.empty and sinyou_df.empty and naaim_df.empty: return None
    
    has_naaim = not naaim_df.empty
    rows = 4 if has_naaim else 3
    row_heights = [0.55, 0.15, 0.15, 0.15] if has_naaim else [0.6, 0.2, 0.2]
    specs = [[{"secondary_y": True}], [{}], [{}]]
    titles = ['日経平均 & 裁定倍率 (右軸)', '裁定買残 (億円)', '信用比率 (買残 / 日経平均)']
    if has_naaim:
        specs.append([{"secondary_y": True}])
        titles.append('NAAIM Exposure Index')
    
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.05, row_heights=row_heights, specs=specs, subplot_titles=titles)
    
    d1 = saitei_df.copy() if not saitei_df.empty else pd.DataFrame()
    d2 = sinyou_df.copy() if not sinyou_df.empty else pd.DataFrame()
    if not d1.empty and not d2.empty:
        d1['Date'] = pd.to_datetime(d1['Date']).dt.normalize()
        d2['Date'] = pd.to_datetime(d2['Date']).dt.normalize()
        df_jp = pd.merge(d1, d2, on='Date', how='inner', suffixes=('_sai', '_sin')).sort_values('Date')
        df_jp = df_jp[~df_jp['Date'].duplicated(keep='last')]
        df_jp.columns = [str(c).lower().strip() for c in df_jp.columns]
        
        nik_col = 'nikkei225_sai' if 'nikkei225_sai' in df_jp.columns else 'nikkei225'
        buy_sai_col = 'buy(oku-yen)'
        buy_sin_col = 'buy(m-yen)'
        df_jp['ratio_sai'] = df_jp[buy_sai_col] / df_jp[nik_col]
        df_jp['ratio_sin'] = df_jp[buy_sin_col] / df_jp[nik_col]
        
        fig.add_hline(y=0.6, row=1, col=1, secondary_y=True, line_color='lightblue', line_dash='dash', line_width=1)
        fig.add_trace(go.Scatter(x=df_jp['date'], y=df_jp[nik_col], mode='lines', name='日経平均', line=dict(color='orange', width=2)), row=1, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(x=df_jp['date'], y=df_jp['ratio_sai'], mode='lines', name='裁定倍率', line=dict(color='red', width=2)), row=1, col=1, secondary_y=True)
        fig.add_trace(go.Bar(x=df_jp['date'], y=df_jp[buy_sai_col], name='裁定買残', marker_color='#1f77b4'), row=2, col=1)
        fig.add_trace(go.Scatter(x=df_jp['date'], y=df_jp['ratio_sin'], mode='lines', name='信用比率', line=dict(color='green', width=1.5), fill='tozeroy', fillcolor='rgba(0, 255, 0, 0.1)'), row=3, col=1)

    if has_naaim:
        n_df = naaim_df.copy()
        n_df['Date'] = pd.to_datetime(n_df['Date']).dt.normalize()
        try:
            sp500 = yf.download("^GSPC", start=n_df['Date'].min(), progress=False)
            if not sp500.empty:
                sp500 = sp500.reset_index()
                close_col = 'Close' if 'Close' in sp500.columns else sp500.columns[sp500.columns.get_level_values(0) == 'Close'][0]
                fig.add_trace(go.Scatter(x=sp500['Date'], y=sp500[close_col], mode='lines', name='S&P 500', line=dict(color='rgba(128, 128, 128, 0.4)', width=1, dash='dot')), row=4, col=1, secondary_y=True)
        except Exception: pass

        fig.add_trace(go.Scatter(x=n_df['Date'], y=n_df['NAAIM'], mode='lines', name='NAAIM', line=dict(color='#2E5BFF', width=2.5)), row=4, col=1, secondary_y=False)
        fig.add_hline(y=100, row=4, col=1, line_color='rgba(255, 0, 0, 0.3)', line_dash='dash', line_width=1)
        fig.add_hline(y=0, row=4, col=1, line_color='black', line_width=1)

    fig.update_layout(height=1000 if has_naaim else 800, margin=dict(l=20, r=60, t=50, b=20), showlegend=False, hovermode='x', dragmode='pan', hoverdistance=-1, spikedistance=-1)
    fig.update_xaxes(showticklabels=True, nticks=16, matches='x', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikecolor='#ff4b4b', spikedash='solid', showline=True)
    fig.update_yaxes(showspikes=False)
    fig.update_yaxes(title_text="株価", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="倍率", row=1, col=1, secondary_y=True, range=[0.2, 1.6])
    fig.update_yaxes(title_text="億円", row=2, col=1)
    if not d1.empty and not d2.empty:
        fig.update_yaxes(title_text="比率", row=3, col=1, range=[60, df_jp['ratio_sin'].max() * 1.05])
    if has_naaim:
        fig.update_yaxes(title_text="指数", row=4, col=1, secondary_y=False, range=[0, 120])
        fig.update_yaxes(title_text="S&P500", row=4, col=1, secondary_y=True)
        
    return fig


# =====================================================================
# 🔄 セクターローテーション: ページ描画
# =====================================================================
def get_jpx_full_list():
    """get_jpx_list() を再利用してsymbolを文字列化して返す（検索用ラッパー）"""
    df = get_jpx_list()
    if df.empty:
        return pd.DataFrame(columns=['symbol', 'name'])
    df = df.copy()
    df['symbol'] = df['symbol'].astype(str)
    return df


CUSTOM_SECTOR_KEY = "custom_sector_tickers"

def render_sector_rotation_page():
    st.title("🔄 セクターローテーション分析（統合版）")

    # カスタム銘柄セクターのセッションステート初期化
    if CUSTOM_SECTOR_KEY not in st.session_state:
        st.session_state[CUSTOM_SECTOR_KEY] = {}  # {ticker: name}

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

        # ─────────────────────────────────────────
        # 📌 ウォッチリスト（カスタム銘柄）管理
        # ─────────────────────────────────────────
        st.divider()
        st.subheader("📌 ウォッチリスト")

        search_query = st.text_input(
            "銘柄コード・名前で検索",
            placeholder="例: 7203 / トヨタ / 三菱",
            key="watch_search_input"
        )

        q = search_query.strip() if search_query else ""

        # 2文字以上でリアルタイム候補表示
        # 2文字以上でリアルタイム候補表示
        if len(q) >= 2:
            jpx_df = get_jpx_full_list()
            if jpx_df.empty:
                # JPXリスト取得失敗時 → コード直接入力にフォールバック
                st.caption("⚠️ JPXリスト取得失敗。コードを直接入力して追加できます。")
                if q.isdigit():
                    if st.button(f"➕ {q} を追加", key="btn_add_direct", use_container_width=True):
                        st.session_state[CUSTOM_SECTOR_KEY][q] = q
                        st.rerun()
            else:
                mask = (
                    jpx_df['name'].str.contains(q, na=False, case=False) |
                    jpx_df['symbol'].str.contains(q, na=False)
                )
                found = jpx_df[mask].head(10)
                if not found.empty:
                    options = {
                        f"{row['symbol']}  {row['name']}": (row['symbol'], row['name'])
                        for _, row in found.iterrows()
                    }
                    selected_label = st.selectbox(
                        "候補",
                        list(options.keys()),
                        key="watch_search_select",
                        label_visibility="collapsed"
                    )
                    if st.button("➕ 追加", key="btn_add_watch", use_container_width=True):
                        sel_code, sel_name = options[selected_label]
                        st.session_state[CUSTOM_SECTOR_KEY][sel_code] = sel_name
                        st.rerun()
                else:
                    st.caption(f"「{q}」の候補なし（TOPIX500内で検索中）")
                    # 数字ならコード直接追加も提示
                    if q.isdigit():
                        if st.button(f"➕ {q} をコードとして追加", key="btn_add_direct", use_container_width=True):
                            st.session_state[CUSTOM_SECTOR_KEY][q] = q
                            st.rerun()
        elif len(q) == 1:
            st.caption("もう1文字以上入力すると候補が表示されます")

        # 現在のウォッチリスト表示（削除ボタン付き）
        custom_tickers = st.session_state[CUSTOM_SECTOR_KEY]
        if custom_tickers:
            st.caption(f"登録済み: {len(custom_tickers)}銘柄")
            to_delete = []
            for code, name in list(custom_tickers.items()):
                col_a, col_b = st.columns([4, 1])
                col_a.markdown(f"**{code}** {name}")
                if col_b.button("🗑️", key=f"del_{code}", help=f"{code}を削除"):
                    to_delete.append(code)
            for code in to_delete:
                del st.session_state[CUSTOM_SECTOR_KEY][code]
            if to_delete:
                st.rerun()
        else:
            st.caption("まだ銘柄が登録されていません")

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

    # ─────────────────────────────────────────
    # 📌 ウォッチリスト（カスタム銘柄）ミニチャート
    # ─────────────────────────────────────────
    custom_tickers = st.session_state.get(CUSTOM_SECTOR_KEY, {})
    if custom_tickers:
        st.divider()
        st.markdown("### 📌 ウォッチリスト（個別銘柄）")

        custom_codes = list(custom_tickers.keys())
        custom_rows = (len(custom_codes) + n_cols - 1) // n_cols

        for row_i in range(custom_rows):
            cols = st.columns(n_cols)
            for col_i in range(n_cols):
                idx = row_i * n_cols + col_i
                if idx >= len(custom_codes): break
                code = custom_codes[idx]
                name = custom_tickers[code]

                # 個別銘柄のインデックス系列を計算
                single_series = compute_sector_index_from_df(db_df, [code], period_days, resample_weekly)
                mom_single = get_sector_momentum(single_series, days=min(5, period_days)) if not single_series.empty else 0.0
                badge = "🟢" if mom_single >= 3.0 else "🔴" if mom_single <= -3.0 else "⚪"
                color_theme = "#26a69a" if mom_single >= 3.0 else "#ef5350" if mom_single <= -3.0 else "#9e9e9e"

                with cols[col_i]:
                    with st.container(border=True):
                        hc1, hc2, hc3 = st.columns([3, 1, 1])
                        hc1.markdown(f"<span style='font-weight:600;color:{color_theme}'>{badge} {code} {name}</span>", unsafe_allow_html=True)
                        hc2.metric("", f"{mom_single:+.2f}%", label_visibility="collapsed")
                        if hc3.button("🗑️", key=f"watchlist_del_{code}", help=f"{code}を削除"):
                            del st.session_state[CUSTOM_SECTOR_KEY][code]
                            st.rerun()

                        if not single_series.empty:
                            st.plotly_chart(
                                plot_sector_mini_chart(single_series, f"{code} {name}", mom_single),
                                use_container_width=True,
                                key=f"watch_mini_{code}"
                            )
                        else:
                            st.caption("データなし（DBにティッカーが存在しない可能性があります）")

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

# --- マーケット情報ページ ---
if selected_page == "マーケット情報":
    st.title("📈 マーケット情報")
    if st.button("データ取得・更新", type="primary"):
        with st.spinner("更新中..."):
            df_s = update_and_load_saitei_data()
            if not df_s.empty: st.session_state.saitei_df = df_s
            df_m = update_and_load_sinyou_data()
            if not df_m.empty: st.session_state.sinyou_df = df_m
            df_n = update_and_load_naaim_data()
            if not df_n.empty: st.session_state.naaim_df = df_n
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
    st.write("---")
    
    m_col1, _ = st.columns([1, 1])
    with m_col1:
        if not st.session_state.naaim_df.empty:
            latest_naaim = st.session_state.naaim_df.iloc[-1]
            prev_naaim = st.session_state.naaim_df.iloc[-2] if len(st.session_state.naaim_df) > 1 else latest_naaim
            delta = round(latest_naaim['NAAIM'] - prev_naaim['NAAIM'], 2)
            st.metric("最新 NAAIM Exposure Index", f"{latest_naaim['NAAIM']}", delta=f"{delta}")
            st.caption(f"更新日: {latest_naaim['Date'].strftime('%Y-%m-%d')}")
            
    if not st.session_state.saitei_df.empty or not st.session_state.sinyou_df.empty or not st.session_state.naaim_df.empty:
        fig = plot_market_dashboard(st.session_state.saitei_df, st.session_state.sinyou_df, st.session_state.naaim_df)
        if fig:
            fig.update_xaxes(range=[start_dt, end_dt + pd.Timedelta(days=7)])
            if not st.session_state.saitei_df.empty:
                v = st.session_state.saitei_df[(st.session_state.saitei_df['Date'] >= start_dt) & (st.session_state.saitei_df['Date'] <= end_dt)]
                if not v.empty: fig.update_yaxes(range=[v['Nikkei225'].min()*0.98, v['Nikkei225'].max()*1.02], row=1, col=1, secondary_y=False)
            fig.update_yaxes(fixedrange=True)
            st.plotly_chart(fig, use_container_width=True)
    else: st.info("「データ取得・更新」ボタンを押してください。")
    
    st.write("---")
    st.subheader("🔍 個別銘柄 信用残検索 (IRBank)")
    c1, c2 = st.columns([1, 4])
    search_code = c1.text_input("銘柄コード", value="1321", placeholder="例: 1321")
    if search_code:
        with st.spinner(f"{search_code} のデータを取得中..."):
            idf = fetch_irbank_margin(search_code)
            if not idf.empty:
                p = st.radio("表示期間:", ["6ヶ月", "1年", "3年", "全"], key="ir_p", horizontal=True)
                i_end = idf['Date'].max()
                if p == "6ヶ月": i_start = i_end - pd.DateOffset(months=6)
                elif p == "1年": i_start = i_end - pd.DateOffset(years=1)
                elif p == "3年": i_start = i_end - pd.DateOffset(years=3)
                else: i_start = idf['Date'].min()
                
                vdf = idf[idf['Date'] >= i_start]
                if not vdf.empty:
                    ifig = plot_individual_margin(vdf, search_code)
                    st.plotly_chart(ifig, use_container_width=True)
            else:
                st.warning("データが見つかりませんでした。コードを確認してください。")
    st.stop()

# --- スクリーニングページ ---
if selected_page == "スクリーニング":
    st.title("WVF + Trend Screener :blue[Pro]")
    
    with st.sidebar:
        st.subheader("スクリーニング操作")
        
        # 履歴ロード
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

    # 結果の表示
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
# --- マーケット情報ページ (従来ロジックそのまま維持) ---
if selected_page == "マーケット情報":
    st.title("📈 マーケット情報")
    # (既存のマーケット情報画面描画ロジックが安全に動作)
    st.info("インクリメンタルなマーケット情報を表示します。")