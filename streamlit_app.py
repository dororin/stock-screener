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
# セクターローテーション用 定数・設定
# =====================================================================

# セクターローテーション用データ保存ディレクトリ
def _get_sector_data_dir():
    """セクターローテーション用データ保存先を決定する"""
    try:
        from google.colab import drive
        base = "/content/drive/MyDrive/stock_data_hub"
    except ImportError:
        pass
    if os.environ.get('KAGGLE_KERNEL_RUN_TYPE'):
        base = "/kaggle/working/stock_data_hub"
    else:
        # ローカル / Streamlit Cloud: スクリプトと同階層の data_drive/
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_drive")
    os.makedirs(base, exist_ok=True)
    return base

SECTOR_DATA_DIR = _get_sector_data_dir()

# --- 日本株セクター定義 ---
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
    "小売": ["7974", "9983", "8267", "3382"],
    "エネルギー": ["5020", "5019", "1605"],
    "食品": ["2502", "2503", "2914", "2801", "2802"],
    "建設・インフラ": ["1801", "1802", "1803", "5401", "5406"],
}

# 小売の誤記を修正
JP_SECTORS["小売"] = ["7974", "9983", "8267", "3382", "2651"]

# --- 米国株セクター定義 ---
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

# ベンチマーク定義
JP_BENCHMARKS = {"なし（絶対値）": None, "TOPIX": "^TPX", "日経平均": "^N225"}
US_BENCHMARKS = {"なし（絶対値）": None, "S&P500": "^GSPC", "NASDAQ100": "^NDX"}

# =====================================================================
# Google Sheets 共通クライアント（secrets.tomlの既存認証情報を使い回す）
# =====================================================================

@st.cache_resource
def get_gspread_client():
    """
    secrets.toml の [connections.gsheets] からサービスアカウント認証情報を取り出し
    gspread クライアントを生成する。secrets への追記は不要。
    """
    try:
        cfg = dict(st.secrets["connections"]["gsheets"])
        # spreadsheet キーは gspread には不要なので除外
        sa_keys = ["type","project_id","private_key_id","private_key",
                   "client_email","client_id","auth_uri","token_uri",
                   "auth_provider_x509_cert_url","client_x509_cert_url"]
        sa_info = {k: cfg[k] for k in sa_keys if k in cfg}
        if "type" not in sa_info:
            sa_info["type"] = "service_account"
        # \n エスケープを実際の改行に変換（Streamlit Secrets の仕様）
        if "private_key" in sa_info:
            sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = SACredentials.from_service_account_info(sa_info, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        return None

def get_management_spreadsheet():
    """
    secrets.toml の [connections.gsheets].management_spreadsheet を優先して管理スプレッドシートとして開く。
    設定されていない場合は、従来の [connections.gsheets].spreadsheet を開く。
    戻り値: gspread.Spreadsheet | None
    """
    gc = get_gspread_client()
    if gc is None:
        return None
    try:
        cfg = st.secrets["connections"]["gsheets"]
        # management_spreadsheet の設定があれば優先し、なければ従来の spreadsheet を使う
        url = cfg.get("management_spreadsheet", cfg.get("spreadsheet"))
        return gc.open_by_url(url)
    except Exception:
        return None

# --- ファイル管理シート操作 ---
FILE_MANAGER_SHEET = "file_manager"   # 管理スプレッドシート内のシート名

def load_file_registry() -> pd.DataFrame:
    """
    管理シートの file_manager シートを読み込む。
    列: file_name | url | sheet_name | file_type | memo
    """
    sh = get_management_spreadsheet()
    if sh is None:
        return pd.DataFrame()
    try:
        ws = sh.worksheet(FILE_MANAGER_SHEET)
        records = ws.get_all_records()
        df = pd.DataFrame(records)
        return df if not df.empty else pd.DataFrame()
    except gspread.exceptions.WorksheetNotFound:
        # シートがない場合は自動作成してヘッダーだけ書き込む
        try:
            ws = sh.add_worksheet(title=FILE_MANAGER_SHEET, rows=100, cols=5)
            headers = ["file_name", "url", "sheet_name", "file_type", "memo"]
            ws.append_row(headers)
        except Exception:
            pass
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

def save_file_registry(df: pd.DataFrame):
    """file_manager シートを上書き保存する"""
    sh = get_management_spreadsheet()
    if sh is None:
        return False
    try:
        try:
            ws = sh.worksheet(FILE_MANAGER_SHEET)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=FILE_MANAGER_SHEET, rows=100, cols=5)
        ws.clear()
        ws.update([df.columns.tolist()] + df.values.tolist())
        return True
    except Exception:
        return False

# --- 任意スプレッドシートの読み書き ---
def read_sheet_as_df(url: str, sheet_name: str = None) -> pd.DataFrame:
    """指定URLのスプレッドシートを DataFrame として読み込む"""
    gc = get_gspread_client()
    if gc is None:
        return pd.DataFrame()
    try:
        sh = gc.open_by_url(url)
        ws = sh.worksheet(sheet_name) if sheet_name else sh.get_worksheet(0)
        records = ws.get_all_records()
        return pd.DataFrame(records)
    except Exception as e:
        st.warning(f"シート読み込みエラー: {e}")
        return pd.DataFrame()

def write_df_to_sheet(df: pd.DataFrame, url: str, sheet_name: str = None) -> bool:
    """DataFrame を指定URLのスプレッドシートに上書き書き込む"""
    gc = get_gspread_client()
    if gc is None:
        return False
    try:
        sh = gc.open_by_url(url)
        try:
            ws = sh.worksheet(sheet_name) if sheet_name else sh.get_worksheet(0)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet_name or "Sheet1", rows=1000, cols=20)
        ws.clear()
        # NaN を空文字に変換してから書き込む
        safe_df = df.fillna("").astype(str)
        ws.update([safe_df.columns.tolist()] + safe_df.values.tolist())
        return True
    except Exception as e:
        st.warning(f"シート書き込みエラー: {e}")
        return False

# =====================================================================
# セクターマスタ の Sheets 読み書き
# =====================================================================
SECTOR_SHEET_NAME_JP = "sector_JP"
SECTOR_SHEET_NAME_US = "sector_US"

# sector_master シートの列構成:
#   セクター名 | 銘柄コード | 備考
# 1セクター複数銘柄 → 1銘柄1行（long形式）

def load_sector_master_from_sheets(is_jp: bool, override_url: str = None) -> dict:
    """
    指定された url または管理スプレッドシートの sector_JP または sector_US シートから
    セクター辞書 {セクター名: [コード, ...]} を読み込む。
    """
    gc = get_gspread_client()
    if gc is None:
        return JP_SECTORS if is_jp else US_SECTORS
    
    # override_url（選択された別シート）があればそれを使い、なければ従来の management_spreadsheet を使う
    if override_url:
        url = override_url
    else:
        sh_m = get_management_spreadsheet()
        if sh_m is None:
            return JP_SECTORS if is_jp else US_SECTORS
        try:
            url = st.secrets["connections"]["gsheets"]["spreadsheet"]
        except Exception:
            return JP_SECTORS if is_jp else US_SECTORS

    sheet_name = SECTOR_SHEET_NAME_JP if is_jp else SECTOR_SHEET_NAME_US
    try:
        sh = gc.open_by_url(url)
        ws = sh.worksheet(sheet_name)
        records = ws.get_all_records()
        if not records:
            return JP_SECTORS if is_jp else US_SECTORS
        df = pd.DataFrame(records)
        col_map = {}
        for c in df.columns:
            lc = str(c).strip().lower().replace(" ", "_")
            if lc in ["セクター名", "sector", "sector_name"]:
                col_map[c] = "sector"
            elif lc in ["銘柄コード", "code", "ticker", "コード"]:
                col_map[c] = "code"
            elif lc in ["備考", "memo", "note"]:
                col_map[c] = "memo"
        df = df.rename(columns=col_map)
        if "sector" not in df.columns or "code" not in df.columns:
            return JP_SECTORS if is_jp else US_SECTORS
        result = {}
        for _, row in df.iterrows():
            sec = str(row["sector"]).strip()
            code = str(row["code"]).strip().split(".")[0]
            if sec and code:
                result.setdefault(sec, [])
                if code not in result[sec]:
                    result[sec].append(code)
        return result if result else (JP_SECTORS if is_jp else US_SECTORS)
    except gspread.exceptions.WorksheetNotFound:
        # シートがない場合は自動作成（デフォルト書き込み）
        try:
            sh = gc.open_by_url(url)
            _init_sector_sheet(sh, is_jp)
        except Exception:
            pass
        return JP_SECTORS if is_jp else US_SECTORS
    except Exception:
        return JP_SECTORS if is_jp else US_SECTORS

def _init_sector_sheet(sh, is_jp: bool):
    """デフォルトセクター定義をシートに初期書き込みする"""
    sheet_name = SECTOR_SHEET_NAME_JP if is_jp else SECTOR_SHEET_NAME_US
    default = JP_SECTORS if is_jp else US_SECTORS
    try:
        ws = sh.add_worksheet(title=sheet_name, rows=500, cols=3)
        rows = [["セクター名", "銘柄コード", "備考"]]
        for sec, codes in default.items():
            for code in codes:
                rows.append([sec, code, ""])
        ws.update(rows)
    except Exception:
        pass

def save_sector_master_to_sheets(sectors: dict, is_jp: bool, override_url: str = None) -> bool:
    """セクター辞書を 指定されたURL または管理 Sheets に保存する"""
    gc = get_gspread_client()
    if gc is None:
        return False
    
    if override_url:
        url = override_url
    else:
        sh_m = get_management_spreadsheet()
        if sh_m is None:
            return False
        try:
            url = st.secrets["connections"]["gsheets"]["spreadsheet"]
        except Exception:
            return False

    sheet_name = SECTOR_SHEET_NAME_JP if is_jp else SECTOR_SHEET_NAME_US
    try:
        sh = gc.open_by_url(url)
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
    except Exception as e:
        st.warning(f"セクターマスタ保存エラー: {e}")
        return False

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

def fetch_naaim_data():
    """NAAIM Exposure IndexのExcelデータを取得 (BytesIO方式で堅牢化)"""
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
        if not excel_url:
            if links: excel_url = links[0].get('href')
            else: return pd.DataFrame()
        
        # Excelファイルのダウンロード
        content = requests.get(excel_url, headers=headers).content
        import io
        df = pd.read_excel(io.BytesIO(content))
        df.columns = [str(c).strip() for c in df.columns]
        
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
            df = df.dropna(subset=['Date'])
            # 'Mean/Average' または 'NAAIM Number' を探す
            val_col = next((c for c in df.columns if 'NAAIM Number' in c or 'Mean' in c or 'Average' in c), None)
            if val_col:
                df = df[['Date', val_col]].rename(columns={val_col: 'NAAIM'})
                df = df.sort_values('Date').reset_index(drop=True)
                return df
        return pd.DataFrame()
    except:
        return pd.DataFrame()

def update_and_load_naaim_data():
    """NAAIMデータをGSheetsと同期・読込"""
    existing_df = pd.DataFrame(columns=['Date', 'NAAIM'])
    if conn is not None:
        try:
            existing_df = conn.read(spreadsheet=MARKET_DATA_URL, worksheet="naaim_data", ttl=0)
            if existing_df is not None and not existing_df.empty:
                existing_df['Date'] = pd.to_datetime(existing_df['Date'], errors='coerce')
                existing_df = existing_df.dropna(subset=['NAAIM']).copy()
            else: existing_df = pd.DataFrame(columns=['Date', 'NAAIM'])
        except: pass
    
    web_df = fetch_naaim_data()
    
    # 統合 (Web優先)
    if web_df.empty:
        merged_df = existing_df
    elif existing_df.empty:
        merged_df = web_df
    else:
        merged_df = pd.concat([existing_df, web_df])
        
    if not merged_df.empty:
        merged_df['Date'] = pd.to_datetime(merged_df['Date']).dt.normalize()
        merged_df = merged_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
        
    # 保存 (ワークシートがない場合などは失敗するが、merged_dfは返す)
    if conn is not None and not merged_df.empty:
        try:
            save_df = merged_df.copy()
            save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
            conn.update(spreadsheet=MARKET_DATA_URL, worksheet="naaim_data", data=save_df)
        except: 
            # 失敗してもセッション用データとして merged_df を活かす
            pass
    
    return merged_df

def fetch_irbank_margin(code):
    """IRBankから個別銘柄の信用残データを取得"""
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
                if year_year := (year_td.get_text(strip=True) if year_td else None):
                    if re.match(r"^\d{4}$", year_year): current_year = year_year
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
                except: continue
        df = pd.DataFrame(data)
        if not df.empty:
            df = df.drop_duplicates(subset=['Date']).sort_values('Date').reset_index(drop=True)
        return df
    except: return pd.DataFrame()

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
    fig.update_xaxes(
        showspikes=True, 
        spikemode='across', 
        spikesnap='cursor', 
        spikedash='solid', 
        spikethickness=1, 
        spikecolor='#ff4b4b'
    )
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
        # 信用残シートに裁定データが混入するのを防ぐため、必要な列のみに絞る
        valid_cols = ['Date', 'Nikkei225', 'Sell(M-yen)', 'Buy(M-yen)']
        existing_df = existing_df[[c for c in valid_cols if c in existing_df.columns]].dropna(subset=['Sell(M-yen)', 'Buy(M-yen)'], how='any').copy()
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
        
    # 保存の必要性チェック
    try:
        if not merged_df.empty:
            # 保存前に最終的な重複チェックを日付ベースで実行 (強固なガード)
            final_df = merged_df.copy()
            final_df['Date'] = pd.to_datetime(final_df['Date']).dt.normalize()
            final_df = final_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date')
            
            should_update = True
            if not existing_df.empty:
                last_existing = pd.to_datetime(existing_df['Date']).max()
                last_merged = final_df['Date'].max()
                if last_merged <= last_existing and len(final_df) <= len(existing_df):
                    should_update = False
            
            if should_update:
                save_df = final_df.copy()
                save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
                conn.update(spreadsheet=MARKET_DATA_URL, worksheet="sinyou_data", data=save_df)
    except Exception as e:
        st.error(f"保存エラー: {e}")
        
    return merged_df

def plot_market_dashboard(saitei_df, sinyou_df, naaim_df):
    if saitei_df.empty and sinyou_df.empty and naaim_df.empty: return None
    
    # 段数の動的決定 (NAAIMがある場合のみ4段、ない場合は3段)
    has_naaim = not naaim_df.empty
    rows = 4 if has_naaim else 3
    row_heights = [0.55, 0.15, 0.15, 0.15] if has_naaim else [0.6, 0.2, 0.2]
    specs = [[{"secondary_y": True}], [{}], [{}]]
    titles = ['日経平均 & 裁定倍率 (右軸)', '裁定買残 (億円)', '信用比率 (買残 / 日経平均)']
    if has_naaim:
        specs.append([{"secondary_y": True}])
        titles.append('NAAIM Exposure Index (米個人投資家意識)')
    
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.05,
                        row_heights=row_heights, specs=specs, subplot_titles=titles)
    
    # 日経データ(裁定・信用)の統合
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

    # 4段目: NAAIM (ユーザー様の画像イメージ: 青いラインチャート)
    if has_naaim:
        n_df = naaim_df.copy()
        n_df['Date'] = pd.to_datetime(n_df['Date']).dt.normalize()
        
        # S&P500 (背景の参考程度に)
        try:
            sp500 = yf.download("^GSPC", start=n_df['Date'].min(), progress=False)
            if not sp500.empty:
                sp500 = sp500.reset_index()
                close_col = 'Close' if 'Close' in sp500.columns else sp500.columns[sp500.columns.get_level_values(0) == 'Close'][0]
                fig.add_trace(go.Scatter(x=sp500['Date'], y=sp500[close_col], mode='lines', name='S&P 500', line=dict(color='rgba(128, 128, 128, 0.4)', width=1, dash='dot')), row=4, col=1, secondary_y=True)
        except: pass

        # NAAIM Number (鮮やかな青色、画像に近い太めの線)
        fig.add_trace(go.Scatter(x=n_df['Date'], y=n_df['NAAIM'], mode='lines', name='NAAIM', line=dict(color='#2E5BFF', width=2.5)), row=4, col=1, secondary_y=False)
        # 閾値ライン
        fig.add_hline(y=100, row=4, col=1, line_color='rgba(255, 0, 0, 0.3)', line_dash='dash', line_width=1)
        fig.add_hline(y=0, row=4, col=1, line_color='black', line_width=1)

    # レイアウト設定
    fig.update_layout(
        height=1000 if has_naaim else 800, margin=dict(l=20, r=60, t=50, b=20), showlegend=False,
        hovermode='x', dragmode='pan', hoverdistance=-1, spikedistance=-1
    )
    
    fig.update_xaxes(
        showticklabels=True, nticks=16, matches='x', showspikes=True,
        spikemode='across', spikesnap='cursor', spikethickness=1,
        spikecolor='#ff4b4b', spikedash='solid', showline=True,
        tickformatstops=[
            dict(dtickrange=[None, 1000*60*60*24*7], value="%m/%d"),
            dict(dtickrange=[1000*60*60*24*7, None], value="%y/%m/%d")
        ]
    )
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
    
    # 各軸の個別設定
    fig.update_xaxes(
        showticklabels=True,
        nticks=16,
        matches='x', 
        showspikes=True,
        spikemode='across',
        spikesnap='cursor',
        spikethickness=1,
        spikecolor='#ff4b4b',
        spikedash='solid',
        showline=True,
        tickformatstops=[
            dict(dtickrange=[None, 1000*60*60*24*7], value="%m/%d"),
            dict(dtickrange=[1000*60*60*24*7, None], value="%y/%m/%d")
        ]
    )

    # Y軸の設定
    fig.update_yaxes(showspikes=False, nticks=15)
    fig.update_yaxes(title_text="株価", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="倍率", row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text="億円", row=2, col=1)
    fig.update_yaxes(title_text="比率", row=3, col=1)

    # 裁定倍率 (右軸) を 0.2〜1.6 に固定
    fig.update_yaxes(range=[0.2, 1.6], row=1, col=1, secondary_y=True)
        
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
        
    # 保存の必要性チェック
    try:
        if not merged_df.empty:
            # 保存前に最終的な重複チェックを日付ベースで実行
            final_df = merged_df.copy()
            final_df['Date'] = pd.to_datetime(final_df['Date']).dt.normalize()
            final_df = final_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date')
            
            should_update = True
            if not existing_df.empty:
                last_existing = pd.to_datetime(existing_df['Date']).max()
                last_merged = final_df['Date'].max()
                if last_merged <= last_existing and len(final_df) <= len(existing_df):
                    should_update = False
                    
            if should_update:
                save_df = final_df.copy()
                save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
                conn.update(spreadsheet=MARKET_DATA_URL, worksheet="saitei_data", data=save_df)
    except Exception as e:
        st.error(f"保存エラー: {e}")
        
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
        df['symbol'] = pd.to_numeric(df['symbol'], errors='coerce')
        df = df.dropna(subset=['symbol'])
        df['symbol'] = df['symbol'].astype(int)
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
            # yfinanceのCloseはデフォルトで分割調整済みのため、手動調整は不要（二重調整を避ける）
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

# =====================================================================
# セクターローテーション: データ管理関数
# =====================================================================

def _sector_parquet_path(interval: str) -> str:
    return os.path.join(SECTOR_DATA_DIR, f"price_{interval}.parquet")

def load_sector_db(interval: str) -> pd.DataFrame:
    """Parquetファイルからセクター価格DBをロード"""
    path = _sector_parquet_path(interval)
    if os.path.exists(path):
        try:
            df = pd.read_parquet(path)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            return df
        except Exception as e:
            return pd.DataFrame(columns=["date", "ticker", "open", "high", "low", "close", "volume", "is_finalized"])
    return pd.DataFrame(columns=["date", "ticker", "open", "high", "low", "close", "volume", "is_finalized"])

def save_sector_db(df: pd.DataFrame, interval: str):
    """セクター価格DBをParquetファイルに保存"""
    if df.empty:
        return
    path = _sector_parquet_path(interval)
    df.to_parquet(path, index=False)

def _parse_yf_batch(df_raw, tickers_with_suffix, suffix=".T") -> pd.DataFrame:
    """yf.downloadの結果をlong形式にパース"""
    if df_raw is None or df_raw.empty:
        return pd.DataFrame()
    all_rows = []
    for sym in tickers_with_suffix:
        try:
            if hasattr(df_raw.columns, 'get_level_values') and len(df_raw.columns.levels) > 1:
                if sym in df_raw.columns.get_level_values(1):
                    t_df = df_raw.xs(sym, axis=1, level=1).copy()
                elif sym in df_raw.columns.get_level_values(0):
                    t_df = df_raw[sym].copy()
                else:
                    continue
            else:
                t_df = df_raw.copy()

            t_df = t_df.dropna(how="all")
            if t_df.empty:
                continue
            t_df = t_df.reset_index()
            t_df.columns = [str(c).lower() if not isinstance(c, tuple) else str(c[0]).lower() for c in t_df.columns]
            t_df = t_df.rename(columns={"datetime": "date", "index": "date", "timestamp": "date"})
            if "date" not in t_df.columns:
                continue

            dt_col = pd.to_datetime(t_df["date"])
            if dt_col.dt.tz is not None:
                t_df["date"] = dt_col.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
            else:
                t_df["date"] = dt_col

            # ticker名（サフィックスなし）
            base_ticker = sym.replace(suffix, "")
            t_df["ticker"] = base_ticker

            valid_cols = [c for c in ["date", "ticker", "open", "high", "low", "close", "volume"] if c in t_df.columns]
            all_rows.append(t_df[valid_cols])
        except Exception:
            continue
    if not all_rows:
        return pd.DataFrame()
    return pd.concat(all_rows, ignore_index=True)

def _attach_finalized_flag(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """確定フラグを付与: 本日以前→True、本日→False"""
    now = datetime.now()
    if interval == "1d":
        today_date = now.date()
        df["is_finalized"] = df["date"].dt.date < today_date
    else:
        # 時間足: 現在時刻より過去なら確定
        df["is_finalized"] = df["date"] < (now - timedelta(hours=1))
    return df

def sync_sector_prices(all_tickers: list, interval: str, is_jp: bool = True) -> pd.DataFrame:
    """
    確定フラグ方式によるインクリメンタル同期。
    Returns: 最新のDBデータ（マージ済み）
    """
    suffix = ".T" if is_jp else ""
    db_df = load_sector_db(interval)
    now = datetime.now()

    # --- 遡り開始日を特定 ---
    if db_df.empty:
        start_date = datetime(2023, 1, 1)
        tickers_to_update = all_tickers
    else:
        # 未確定レコードがあればその最古日から再取得
        if "is_finalized" in db_df.columns:
            unfinalized = db_df[db_df["is_finalized"] == False]
        else:
            unfinalized = pd.DataFrame()

        if not unfinalized.empty:
            start_date = unfinalized["date"].min()
        else:
            max_date = db_df["date"].max()
            start_date = max_date + timedelta(days=1) if interval == "1d" else max_date + timedelta(hours=1)

        # 新規銘柄（DBにない銘柄）は2023-01-01から
        existing_tickers = set(db_df["ticker"].unique())
        new_tickers = [t for t in all_tickers if t not in existing_tickers]
        tickers_to_update = all_tickers  # 全銘柄まとめて取得（差分日付で）

    # 未来日の場合はスキップ
    if start_date > now:
        return db_df

    # データ期間の上限制限（yfinance API制約）
    if interval == "60m":
        limit = now - timedelta(days=720)
        if start_date < limit:
            start_date = limit
    elif interval == "5m":
        limit = now - timedelta(days=58)
        if start_date < limit:
            start_date = limit
    elif interval == "1m":
        limit = now - timedelta(days=5)
        if start_date < limit:
            start_date = limit

    start_str = start_date.strftime("%Y-%m-%d")
    symbols = [f"{t}{suffix}" for t in tickers_to_update]

    # バッチダウンロード（100銘柄ずつ）
    new_rows = []
    batch_size = 100
    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i:i+batch_size]
        try:
            df_raw = yf.download(
                chunk, start=start_str, interval=interval,
                auto_adjust=False, actions=False,
                progress=False, threads=True, timeout=30
            )
            chunk_df = _parse_yf_batch(df_raw, chunk, suffix=suffix)
            if not chunk_df.empty:
                new_rows.append(chunk_df)
        except Exception:
            pass
        time.sleep(0.5)

    if not new_rows:
        return db_df

    new_df = pd.concat(new_rows, ignore_index=True)
    new_df = _attach_finalized_flag(new_df, interval)

    # --- マージ: start_date以降の旧データを新データで上書き ---
    if db_df.empty:
        merged = new_df
    else:
        old_part = db_df[db_df["date"] < pd.Timestamp(start_date)].copy()
        merged = pd.concat([old_part, new_df], ignore_index=True)

    merged = merged.drop_duplicates(subset=["date", "ticker"], keep="last")
    merged = merged.sort_values(["ticker", "date"]).reset_index(drop=True)

    save_sector_db(merged, interval)
    return merged

# =====================================================================
# セクターローテーション: 指数計算関数
# =====================================================================

@st.cache_data(ttl=300)
def compute_sector_index(
    db_parquet_path: str,
    tickers: list,
    interval: str,
    period_days: int,
    benchmark_ticker: Optional[str],
    is_jp: bool = True,
    resample_weekly: bool = False
) -> pd.DataFrame:
    """
    DRAMによるセクターインデックスを計算して返す。
    Returns: DataFrame with columns [date, sector_index, (relative_index)]
    """
    if not os.path.exists(db_parquet_path):
        return pd.DataFrame()

    try:
        db_df = pd.read_parquet(db_parquet_path)
    except Exception:
        return pd.DataFrame()

    if db_df.empty or "date" not in db_df.columns:
        return pd.DataFrame()

    db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
    end_date = db_df["date"].max()
    start_date = end_date - timedelta(days=period_days)
    db_df = db_df[db_df["date"] >= start_date]

    # 対象銘柄のみフィルタ
    target_df = db_df[db_df["ticker"].isin(tickers)].copy()
    if target_df.empty:
        return pd.DataFrame()

    # 週足リサンプル
    if resample_weekly and interval == "1d":
        target_df = target_df.set_index("date")
        target_df = target_df.groupby("ticker").resample("W-FRI").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).reset_index()

    # ピボットしてclose行列を作成
    close_pivot = target_df.pivot_table(index="date", columns="ticker", values="close")
    close_pivot = close_pivot.sort_index()

    # DRAM計算
    daily_returns = close_pivot.pct_change()
    sector_return = daily_returns.mean(axis=1)
    index_series = (1 + sector_return).cumprod() * 100
    if len(index_series) > 0:
        index_series.iloc[0] = 100.0

    result = pd.DataFrame({"date": index_series.index, "sector_index": index_series.values})

    # 相対強度計算
    if benchmark_ticker:
        bm_df = db_df[db_df["ticker"] == benchmark_ticker.replace("^", "BM_")].copy()
        # ベンチマークはDBにないことが多いので別途取得ロジックで対応
        # ここでは sector_index のみ返す
        pass

    return result

@st.cache_data(ttl=600)
def get_benchmark_data(ticker: str, period_days: int, interval: str) -> pd.Series:
    """ベンチマーク指数のインデックス系列を取得（キャッシュ付き）"""
    try:
        end = datetime.now()
        start = end - timedelta(days=period_days + 30)
        df_raw = yf.download(ticker, start=start.strftime("%Y-%m-%d"), interval=interval,
                             auto_adjust=True, progress=False)
        if df_raw.empty:
            return pd.Series(dtype=float)
        df_raw = df_raw.reset_index()
        df_raw.columns = [str(c).lower() if not isinstance(c, tuple) else str(c[0]).lower() for c in df_raw.columns]
        date_col = "date" if "date" in df_raw.columns else "datetime"
        df_raw = df_raw.rename(columns={date_col: "date"})
        df_raw["date"] = pd.to_datetime(df_raw["date"]).dt.tz_localize(None)
        close = df_raw.set_index("date")["close"]
        ret = close.pct_change()
        idx = (1 + ret).cumprod() * 100
        if len(idx) > 0:
            idx.iloc[0] = 100.0
        return idx
    except Exception:
        return pd.Series(dtype=float)

def compute_sector_index_from_df(
    db_df: pd.DataFrame,
    tickers: list,
    period_days: int,
    resample_weekly: bool = False
) -> pd.Series:
    """DBのDataFrameから直接セクターインデックスを計算（キャッシュ不使用）"""
    if db_df.empty:
        return pd.Series(dtype=float)

    db_df = db_df.copy()
    db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
    end_date = db_df["date"].max()
    start_date = end_date - timedelta(days=period_days)
    target_df = db_df[(db_df["date"] >= start_date) & (db_df["ticker"].isin(tickers))].copy()

    if target_df.empty:
        return pd.Series(dtype=float)

    if resample_weekly:
        target_df = target_df.set_index("date")
        target_df = target_df.groupby("ticker").resample("W-FRI").agg({"close": "last"}).reset_index()

    close_pivot = target_df.pivot_table(index="date", columns="ticker", values="close")
    close_pivot = close_pivot.sort_index()

    daily_returns = close_pivot.pct_change()
    sector_return = daily_returns.mean(axis=1)
    index_series = (1 + sector_return).cumprod() * 100
    if len(index_series) > 0:
        index_series.iloc[0] = 100.0

    return index_series

def get_sector_momentum(index_series: pd.Series, days: int = 5) -> float:
    """直近N日のモメンタム（騰落率%）を計算"""
    if len(index_series) < 2:
        return 0.0
    recent = index_series.iloc[-min(days, len(index_series)):]
    if recent.iloc[0] == 0:
        return 0.0
    return float((recent.iloc[-1] / recent.iloc[0] - 1) * 100)

# =====================================================================
# セクターローテーション: チャート描画関数
# =====================================================================

def plot_sector_mini_chart(index_series: pd.Series, sector_name: str, momentum_pct: float) -> go.Figure:
    """セクターミニチャートを生成"""
    if index_series.empty:
        fig = go.Figure()
        fig.update_layout(height=150, margin=dict(l=5, r=5, t=25, b=5),
                          title=dict(text=sector_name, font=dict(size=11)))
        return fig

    color = "#26a69a" if momentum_pct >= 0 else "#ef5350"
    fill_color = "rgba(38,166,154,0.15)" if momentum_pct >= 0 else "rgba(239,83,80,0.15)"

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=index_series.index,
        y=index_series.values,
        mode="lines",
        line=dict(color=color, width=2),
        fill="tozeroy",
        fillcolor=fill_color,
        hovertemplate="%{x|%m/%d}: %{y:.1f}<extra></extra>"
    ))
    fig.add_hline(y=100, line_dash="dot", line_color="gray", line_width=1, opacity=0.5)
    fig.update_layout(
        height=140,
        margin=dict(l=5, r=5, t=5, b=5),
        showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(showticklabels=True, showgrid=True, gridcolor="rgba(128,128,128,0.2)",
                   zeroline=False, tickfont=dict(size=9)),
    )
    return fig

def plot_sector_detail_chart(
    index_series: pd.Series,
    benchmark_series: Optional[pd.Series],
    sector_name: str,
    benchmark_label: str
) -> go.Figure:
    """セクター詳細チャート（相対強度含む）"""
    fig = make_subplots(rows=2 if benchmark_series is not None and not benchmark_series.empty else 1,
                        cols=1, shared_xaxes=True,
                        row_heights=[0.7, 0.3] if benchmark_series is not None else [1.0])

    # セクターインデックス
    fig.add_trace(go.Scatter(
        x=index_series.index, y=index_series.values,
        name=sector_name, line=dict(color="#2196F3", width=2)
    ), row=1, col=1)

    # 相対強度
    if benchmark_series is not None and not benchmark_series.empty:
        # 日付をアライン
        common_dates = index_series.index.intersection(benchmark_series.index)
        if len(common_dates) > 0:
            rel = (index_series[common_dates] / benchmark_series[common_dates]) * 100
            fig.add_trace(go.Scatter(
                x=rel.index, y=rel.values,
                name=f"相対強度 vs {benchmark_label}",
                line=dict(color="#FF9800", width=1.5)
            ), row=2, col=1)
            fig.add_hline(y=100, line_dash="dot", line_color="gray", row=2, col=1)

    fig.update_layout(
        height=400, margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified", template="plotly_white",
        legend=dict(orientation="h", y=1.05)
    )
    return fig

# =====================================================================
# セクターローテーション: ページ描画
# =====================================================================

def render_sector_rotation_page():
    """セクターローテーション分析ダッシュボードのメインレンダリング"""
    st.title("🔄 セクターローテーション分析")

    # --- サイドバー設定 ---
    with st.sidebar:
        st.subheader("⚙️ 表示設定")

        market_mode = st.radio("マーケット", ["日本株 🇯🇵", "米国株 🇺🇸"], horizontal=True)
        is_jp = (market_mode == "日本株 🇯🇵")

        # --- 📂 追加：セクターマスタ切り替えプルダウンの追加 ---
        reg = load_file_registry()
        target_type = "sector_JP" if is_jp else "sector_US"
        sector_files = reg[reg["file_type"] == target_type] if not reg.empty and "file_type" in reg.columns else pd.DataFrame()
        
        selected_sector_url = None
        if not sector_files.empty:
            options = ["デフォルト（管理用シート）"] + sector_files["file_name"].tolist()
            sel_file_name = st.selectbox("セクターマスタの選択", options)
            if sel_file_name != "デフォルト（管理用シート）":
                selected_sector_url = sector_files[sector_files["file_name"] == sel_file_name]["url"].iloc[0]
        # -----------------------------------------------------

        period_label = st.radio(
            "表示期間",
            ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "全期間"],
            index=1, horizontal=True
        )
        period_map = {"1ヶ月": 30, "3ヶ月": 90, "6ヶ月": 180, "1年": 365, "全期間": 9999}
        period_days = period_map[period_label]

        tf_label = st.radio("時間足", ["日足", "週足", "1時間足"], horizontal=True)
        interval_map = {"日足": "1d", "週足": "1d", "1時間足": "60m"}
        interval = interval_map[tf_label]
        resample_weekly = (tf_label == "週足")

        # ベンチマーク選択
        benchmarks = JP_BENCHMARKS if is_jp else US_BENCHMARKS
        bm_label = st.selectbox("相対強度の基準", list(benchmarks.keys()))
        bm_ticker = benchmarks[bm_label]

        # 個別銘柄追加
        st.markdown("**📌 銘柄追加**")
        extra_code = st.text_input(
            "銘柄コード (例: 1615, TSLA)",
            placeholder="カンマ区切りで複数入力可"
        )
        # どのセクターに追加するか選択
        # --- 変更：選択中のセクターマスタからセクター一覧を読み込むように修正 ---
        _current_sectors = load_sector_master_from_sheets(is_jp, override_url=selected_sector_url)
        sector_options = list(_current_sectors.keys()) + ["＋ 新規セクター作成"]
        extra_sector_sel = st.selectbox("追加先セクター", sector_options, key="extra_sector_sel")
        if extra_sector_sel == "＋ 新規セクター作成":
            extra_sector = st.text_input("新規セクター名", placeholder="例: マイウォッチリスト")
        else:
            extra_sector = extra_sector_sel

        col_add1, col_add2 = st.columns(2)
        if col_add1.button("追加して保存", use_container_width=True, key="btn_add_save"):
            st.session_state["_save_extra_trigger"] = True
        if col_add2.button("表示のみ", use_container_width=True, key="btn_add_view"):
            st.session_state["_save_extra_trigger"] = False

        st.divider()

        # ファイル登録管理
        with st.expander("📁 ファイル登録・管理", expanded=False):
            st.caption("スクリーニング銘柄リストやセクターマスタとして使うスプレッドシートを登録します")
            reg_df = load_file_registry()
            if not reg_df.empty:
                st.dataframe(reg_df[["file_name","file_type","sheet_name","memo"]].fillna(""),
                             use_container_width=True, hide_index=True)
            with st.form("file_reg_form"):
                r_name  = st.text_input("ファイル名（表示用）", placeholder="例: 学習銘柄リスト2024")
                r_url   = st.text_input("スプレッドシートURL")
                r_sheet = st.text_input("シート名（空白=先頭シート）", placeholder="例: 学習")
                r_type  = st.selectbox("用途", ["screening", "sector_JP", "sector_US", "その他"])
                r_memo  = st.text_input("備考（任意）")
                if st.form_submit_button("登録", use_container_width=True):
                    if r_name and r_url:
                        new_row = pd.DataFrame([{
                            "file_name": r_name, "url": r_url,
                            "sheet_name": r_sheet, "file_type": r_type, "memo": r_memo
                        }])
                        updated = pd.concat([reg_df, new_row], ignore_index=True) if not reg_df.empty else new_row
                        if save_file_registry(updated):
                            st.success("登録しました！")
                            st.rerun()
                        else:
                            st.error("保存に失敗しました")
                    else:
                        st.warning("ファイル名とURLは必須です")
            # 削除
            if not reg_df.empty and "file_name" in reg_df.columns:
                del_name = st.selectbox("削除するファイル", ["-- 選択 --"] + reg_df["file_name"].tolist(), key="del_file_sel")
                if del_name != "-- 選択 --":
                    if st.button("削除", key="del_file_btn", type="secondary"):
                        updated = reg_df[reg_df["file_name"] != del_name].reset_index(drop=True)
                        if save_file_registry(updated):
                            st.success(f"「{del_name}」を削除しました")
                            st.rerun()

        # グリッド列数
        n_cols = st.slider("グリッド列数", 2, 4, 3)

        # 手動更新ボタン
        do_update = st.button("🔄 データ更新", type="primary", use_container_width=True)

    # --- 変更：選択されたセクターマスタをロード ---
    with st.spinner("セクターマスタを読み込み中..."):
        sectors = load_sector_master_from_sheets(is_jp, override_url=selected_sector_url)

    # --- 銘柄追加UI ---
    if extra_code.strip():
        codes = [c.strip().upper() for c in extra_code.replace("、", ",").replace("，", ",").split(",") if c.strip()]
        if codes and extra_sector.strip():
            # セクターへ追加してSheetsに保存
            sectors.setdefault(extra_sector.strip(), [])
            added = []
            for c in codes:
                if c not in sectors[extra_sector.strip()]:
                    sectors[extra_sector.strip()].append(c)
                    added.append(c)
            if added and st.session_state.get("_save_extra_trigger"):
                # --- 変更：選択中のセクターマスタに保存する ---
                ok = save_sector_master_to_sheets(sectors, is_jp, override_url=selected_sector_url)
                if ok:
                    st.success(f"✅ {added} を「{extra_sector}」に追加して保存しました")
                    st.cache_data.clear()
                st.session_state["_save_extra_trigger"] = False
        elif codes:
            # セクター未選択の場合は「個別追加」として表示のみ
            label = "個別: " + "/".join(codes)
            sectors[label] = codes

    # --- データ更新処理 ---
    all_tickers = list({t for tickers in sectors.values() for t in tickers})
    if bm_ticker:
        all_tickers_str = ", ".join(all_tickers[:5]) + "..."
    
    db_key = f"sector_db_{interval}_{int(is_jp)}"
    if db_key not in st.session_state:
        st.session_state[db_key] = None

    if do_update:
        with st.spinner(f"📡 データ更新中 ({interval})... しばらくお待ちください"):
            progress = st.progress(0, text="Parquetデータベースを同期中...")
            db_df = sync_sector_prices(all_tickers, interval, is_jp=is_jp)
            st.session_state[db_key] = db_df
            progress.progress(100, text="完了！")
            st.success(f"✅ 更新完了: {len(db_df)} レコード")

    # --- DBロード（更新済みかファイルから） ---
    if st.session_state[db_key] is not None:
        db_df = st.session_state[db_key]
    else:
        db_df = load_sector_db(interval)
        if not db_df.empty:
            st.session_state[db_key] = db_df

    if db_df.empty:
        st.info("⬅️ 左メニューの「データ更新」ボタンを押してデータを取得してください。")
        st.markdown("""
        **初回起動時の手順:**
        1. 左サイドバーで「マーケット」「時間足」などの設定を行う
        2. 「🔄 データ更新」ボタンを押す（初回は数分かかる場合があります）
        3. 更新完了後、ミニチャートグリッドが表示されます
        """)
        return

    # --- ベンチマーク系列の取得 ---
    bm_series = None
    if bm_ticker:
        try:
            bm_series = get_benchmark_data(bm_ticker, period_days, interval)
        except Exception:
            bm_series = None

    # --- サマリーバー（上位/下位セクター） ---
    # 全セクターのインデックス系列を一括計算（重複計算を防止）
    sector_index_cache = {}
    momentum_scores = {}
    for sname, tickers in sectors.items():
        idx_series = compute_sector_index_from_df(db_df, tickers, period_days, resample_weekly)
        if not idx_series.empty:
            sector_index_cache[sname] = idx_series
            momentum_scores[sname] = get_sector_momentum(idx_series, days=min(5, period_days))

    if momentum_scores:
        sorted_sectors = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
        top3 = sorted_sectors[:3]
        bot3 = sorted_sectors[-3:]

        st.markdown("### 📊 モメンタムランキング（直近5日）")
        rank_cols = st.columns(6)
        for i, (sname, mom) in enumerate(top3):
            with rank_cols[i]:
                st.metric(f"🟢 #{i+1}", sname, f"{mom:+.2f}%")
        for i, (sname, mom) in enumerate(bot3):
            with rank_cols[i+3]:
                st.metric(f"🔴 #{len(sorted_sectors)-2+i}", sname, f"{mom:+.2f}%")
        st.divider()

    # --- ミニチャートグリッド ---
    st.markdown(f"### 📈 セクターミニチャート（{period_label} / {tf_label}）")

    sector_list = list(sectors.items())
    rows_needed = (len(sector_list) + n_cols - 1) // n_cols

    # 選択セクター詳細表示用のsession_state
    if "selected_sector" not in st.session_state:
        st.session_state.selected_sector = None

    for row_i in range(rows_needed):
        cols = st.columns(n_cols)
        for col_i in range(n_cols):
            idx = row_i * n_cols + col_i
            if idx >= len(sector_list):
                break
            sname, tickers = sector_list[idx]
            mom = momentum_scores.get(sname, 0.0)

            # 色分けロジック
            if mom >= 3.0:
                border_color = "#26a69a"
                badge = "🟢"
            elif mom <= -3.0:
                border_color = "#ef5350"
                badge = "🔴"
            else:
                border_color = "#9e9e9e"
                badge = "⚪"

            with cols[col_i]:
                # カード風コンテナ
                with st.container(border=True):
                    # ヘッダー行
                    h_col1, h_col2 = st.columns([3, 1])
                    with h_col1:
                        st.markdown(
                            f"<span style='font-size:0.9rem;font-weight:600;color:{border_color}'>"
                            f"{badge} {sname}</span>",
                            unsafe_allow_html=True
                        )
                    with h_col2:
                        delta_color = "normal" if mom >= 0 else "inverse"
                        st.metric("", f"{mom:+.2f}%", label_visibility="collapsed")

                    # ミニチャート（事前計算キャッシュを使用）
                    idx_series = sector_index_cache.get(sname, pd.Series(dtype=float))
                    if not idx_series.empty:
                        mini_fig = plot_sector_mini_chart(idx_series, sname, mom)
                        st.plotly_chart(
                            mini_fig,
                            use_container_width=True,
                            config={"displayModeBar": False},
                            key=f"mini_{sname}_{interval}"
                        )
                        # 最新値表示
                        latest_val = idx_series.iloc[-1]
                        st.caption(f"指数: {latest_val:.1f} | 構成: {', '.join(tickers[:3])}{'...' if len(tickers) > 3 else ''}")
                    else:
                        st.caption("データなし（「データ更新」を実行してください）")

                    # 詳細ボタン
                    if st.button("詳細", key=f"detail_{sname}_{interval}", use_container_width=True):
                        st.session_state.selected_sector = sname

    # --- 詳細チャートパネル ---
    if st.session_state.selected_sector and st.session_state.selected_sector in sectors:
        st.divider()
        sel_name = st.session_state.selected_sector
        sel_tickers = sectors[sel_name]

        st.markdown(f"### 🔍 詳細分析: {sel_name}")
        st.caption(f"構成銘柄: {', '.join(sel_tickers)}")

        sel_idx = sector_index_cache.get(sel_name, compute_sector_index_from_df(db_df, sel_tickers, period_days, resample_weekly))
        detail_fig = plot_sector_detail_chart(sel_idx, bm_series, sel_name, bm_label)
        st.plotly_chart(detail_fig, use_container_width=True)

        # 構成銘柄パフォーマンステーブル
        if not db_df.empty:
            st.markdown("**構成銘柄パフォーマンス**")
            perf_rows = []
            for t in sel_tickers:
                t_df = db_df[db_df["ticker"] == t].copy()
                t_df = t_df.sort_values("date")
                end_dt = t_df["date"].max()
                start_dt = end_dt - timedelta(days=period_days)
                t_df_filtered = t_df[t_df["date"] >= start_dt]
                if len(t_df_filtered) >= 2:
                    pct = (t_df_filtered["close"].iloc[-1] / t_df_filtered["close"].iloc[0] - 1) * 100
                    latest_close = t_df_filtered["close"].iloc[-1]
                    suffix_label = ".T" if is_jp else ""
                    tv_url = f"https://jp.tradingview.com/chart/?symbol=TSE%3A{t}" if is_jp else f"https://www.tradingview.com/chart/?symbol={t}"
                    perf_rows.append({
                        "銘柄": f"[{t}]({tv_url})",
                        "最新値": f"{latest_close:,.1f}",
                        f"騰落率({period_label})": f"{pct:+.2f}%",
                    })
            if perf_rows:
                perf_df = pd.DataFrame(perf_rows)
                st.dataframe(perf_df, use_container_width=True, hide_index=True)

        if st.button("閉じる", key="close_detail"):
            st.session_state.selected_sector = None
            st.rerun()

    # =====================================================================
    # セクターマスタ編集パネル
    # =====================================================================
    st.divider()
    with st.expander("⚙️ セクターマスタ編集（Google Sheets と同期）", expanded=False):
        # --- 変更：選択中のセクターマスタを編集するように修正 ---
        edit_sectors = load_sector_master_from_sheets(is_jp, override_url=selected_sector_url)

        # セクター選択
        edit_sec_name = st.selectbox(
            "編集するセクター",
            list(edit_sectors.keys()),
            key="edit_sec_sel"
        )

        if edit_sec_name:
            current_codes = edit_sectors[edit_sec_name]
            st.markdown(f"**{edit_sec_name}** の構成銘柄: `{'`, `'.join(current_codes)}`")

            ec1, ec2 = st.columns(2)

            # 銘柄追加
            with ec1:
                st.markdown("**銘柄を追加**")
                add_code_input = st.text_input("銘柄コード", key="edit_add_code", placeholder="例: 6146")
                if st.button("追加して保存", key="edit_add_btn", use_container_width=True):
                    codes_to_add = [c.strip() for c in add_code_input.replace("、",",").split(",") if c.strip()]
                    changed = False
                    for c in codes_to_add:
                        if c and c not in edit_sectors[edit_sec_name]:
                            edit_sectors[edit_sec_name].append(c)
                            changed = True
                    if changed:
                        # --- 変更：選択中のURLに保存 ---
                        if save_sector_master_to_sheets(edit_sectors, is_jp, override_url=selected_sector_url):
                            st.success(f"追加しました: {codes_to_add}")
                            st.rerun()
                    else:
                        st.info("追加する銘柄がないか、すでに登録済みです")

            # 銘柄削除
            with ec2:
                st.markdown("**銘柄を削除**")
                del_code = st.selectbox("削除する銘柄", ["-- 選択 --"] + current_codes, key="edit_del_code")
                if st.button("削除して保存", key="edit_del_btn", use_container_width=True):
                    if del_code != "-- 選択 --":
                        edit_sectors[edit_sec_name] = [c for c in current_codes if c != del_code]
                        # --- 変更：選択中のURLに保存 ---
                        if save_sector_master_to_sheets(edit_sectors, is_jp, override_url=selected_sector_url):
                            st.success(f"{del_code} を削除しました")
                            st.rerun()

        # セクター追加・削除
        st.markdown("---")
        ns1, ns2 = st.columns(2)
        with ns1:
            st.markdown("**新規セクター追加**")
            new_sec_name = st.text_input("セクター名", key="new_sec_name", placeholder="例: マイセクター")
            new_sec_codes = st.text_input("初期銘柄（カンマ区切り）", key="new_sec_codes", placeholder="例: 7203,7267")
            if st.button("セクター作成", key="new_sec_btn", use_container_width=True):
                if new_sec_name and new_sec_name not in edit_sectors:
                    codes_list = [c.strip() for c in new_sec_codes.replace("、",",").split(",") if c.strip()]
                    edit_sectors[new_sec_name] = codes_list
                    # --- 変更：選択中のURLに保存 ---
                    if save_sector_master_to_sheets(edit_sectors, is_jp, override_url=selected_sector_url):
                        st.success(f"セクター「{new_sec_name}」を作成しました")
                        st.rerun()
                elif new_sec_name in edit_sectors:
                    st.warning("同名のセクターが既に存在します")

        with ns2:
            st.markdown("**セクター削除**")
            del_sec = st.selectbox("削除するセクター", ["-- 選択 --"] + list(edit_sectors.keys()), key="del_sec_sel")
            if st.button("セクター削除", key="del_sec_btn", use_container_width=True, type="secondary"):
                if del_sec != "-- 選択 --":
                    del edit_sectors[del_sec]
                    # --- 変更：選択中のURLに保存 ---
                    if save_sector_master_to_sheets(edit_sectors, is_jp, override_url=selected_sector_url):
                        st.success(f"「{del_sec}」を削除しました")
                        st.rerun()

        # Sheetsで直接編集した場合の再読み込みボタン
        if st.button("🔄 Sheetsから再読み込み", key="reload_sector_master", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

if 'result_df' not in st.session_state: st.session_state.result_df = pd.DataFrame()
if 'saitei_df' not in st.session_state: st.session_state.saitei_df = pd.DataFrame()
if 'sinyou_df' not in st.session_state: st.session_state.sinyou_df = pd.DataFrame()
if 'naaim_df' not in st.session_state: st.session_state.naaim_df = pd.DataFrame()
if 'performed_scan' not in st.session_state: st.session_state.performed_scan = False

with st.sidebar:
    selected_page = st.radio("画面選択", ["スクリーニング", "マーケット情報", "セクターローテーション"])
    st.divider()

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
    
    # メトリクス表示
    m_col1, m_col2 = st.columns(2)
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
            st.plotly_chart(fig, use_container_width=True, config={'scrollZoom': True})
    else: st.info("「データ取得・更新」ボタンを押してください。")
    
    st.write("---")
    st.subheader("🔍 個別銘柄 信用残検索 (IRBank)")
    c1, c2 = st.columns([1, 4])
    search_code = c1.text_input("銘柄コード", value="1321", placeholder="例: 1321")
    if search_code:
        with st.spinner(f"{search_code} のデータを取得中..."):
            idf = fetch_irbank_margin(search_code)
            if not idf.empty:
                if 'ir_period' not in st.session_state: st.session_state.ir_period = "1年"
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

if selected_page == "セクターローテーション":
    render_sector_rotation_page()
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
    list_src = st.radio("取得元", ["JPX (TOPIX)", "Google Sheets", "CSV（ローカル）"], label_visibility="collapsed")

    # Google Sheets 取得元の場合: ファイル登録から選択
    sheets_df_target = pd.DataFrame()
    if list_src == "Google Sheets":
        reg = load_file_registry()
        screening_files = reg[reg["file_type"] == "screening"] if not reg.empty and "file_type" in reg.columns else pd.DataFrame()
        if screening_files.empty:
            st.info("file_manager シートに file_type=screening のファイルを登録してください。")
            st.markdown("👉 下の「ファイル登録」から追加できます")
        else:
            sel_name = st.selectbox(
                "銘柄リスト選択",
                screening_files["file_name"].tolist(),
                key="screening_file_sel"
            )
            sel_row = screening_files[screening_files["file_name"] == sel_name].iloc[0]
            sel_url = sel_row["url"]
            sel_sheet = sel_row.get("sheet_name", "") or None
            if st.button("シートをプレビュー", key="preview_screening"):
                preview_df = read_sheet_as_df(sel_url, sel_sheet)
                if not preview_df.empty:
                    st.dataframe(preview_df.head(10), use_container_width=True)
                else:
                    st.warning("読み込めませんでした")

    csv = st.file_uploader("CSVファイルをアップロード", type=["csv"]) if list_src == "CSV（ローカル）" else None

    if st.button("開始", use_container_width=True):
        df_t = pd.DataFrame()
        if list_src == "JPX (TOPIX)":
            df_t = get_jpx_list()
        elif list_src == "Google Sheets":
            reg = load_file_registry()
            screening_files = reg[reg["file_type"] == "screening"] if not reg.empty and "file_type" in reg.columns else pd.DataFrame()
            if not screening_files.empty:
                sel_name = st.session_state.get("screening_file_sel", screening_files["file_name"].iloc[0])
                sel_row = screening_files[screening_files["file_name"] == sel_name].iloc[0]
                raw_df = read_sheet_as_df(sel_row["url"], sel_row.get("sheet_name") or None)
                if not raw_df.empty:
                    try:
                        code_col = next(c for c in ["コード", "銘柄コード", "symbol", "code"] if c in raw_df.columns)
                        df_t = pd.DataFrame()
                        df_t["symbol"] = pd.to_numeric(raw_df[code_col].astype(str).str.split(".").str[0], errors="coerce").dropna().astype(int)
                        name_cols = [c for c in ["銘柄", "銘柄名", "name"] if c in raw_df.columns]
                        df_t["name"] = raw_df[name_cols[0]] if name_cols else "-"
                        df_t = df_t.dropna(subset=["symbol"]).reset_index(drop=True)
                    except Exception as e:
                        st.error(f"銘柄コード列の読み込みエラー: {e}")
        elif csv:
            try:
                try: df_c = pd.read_csv(csv)
                except: csv.seek(0); df_c = pd.read_csv(csv, encoding='shift_jis')
                df_t = pd.DataFrame()
                df_t['symbol'] = pd.to_numeric(df_c[next(c for c in ["コード", "銘柄コード", "symbol"] if c in df_c.columns)], errors='coerce').dropna().astype(int)
                df_t['name'] = df_c[next(c for c in ["銘柄", "name"] if c in df_c.columns)] if any(c in df_c.columns for c in ["銘柄", "name"]) else "-"
            except: st.error("CSVエラー")
        if not df_t.empty:
            st.session_state.result_df = analyze_market_streamlit(df_t)
            st.session_state.performed_scan = True
            st.session_state.last_id = None
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
else:
    if st.session_state.performed_scan:
        st.warning("条件に一致する銘柄は見つかりませんでした。")
    else:
        st.info("左メニューの「開始」ボタンを押してスクリーニングを開始してください。")
