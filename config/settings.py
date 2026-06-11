# config/settings.py
import os

try:
    import streamlit as st
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

# --- Google Drive 共有フォルダ設定 ---
FOLDER_ID = "1Lx-Xdsm8h20Q-ZRI91Ty7smdYVhkuoFD"
if HAS_STREAMLIT:
    try:
        if "connections" in st.secrets and "gsheets" in st.secrets["connections"]:
            FOLDER_ID = st.secrets["connections"]["gsheets"].get("folder_id", FOLDER_ID)
    except Exception:
        pass

# --- 各種外部URL・基本設定 ---
JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
TIMEFRAMES = ["1d", "60m", "5m", "1m"]
MARKET_DATA_URL = "https://docs.google.com/spreadsheets/d/1vaX2dKcHO_fo_KMffNiC98pY1fzfMkHCRkHE1IFE0PI/edit"

# --- Google Sheets 内のシート名設定 ---
WATCHLIST_SHEET_NAME = "watchlist"
REPAIR_LOG_SHEET_NAME = "repair_log"
EXTRA_TICKERS_SHEET = "extra_tickers"

# --- セクター定義（スプレッドシート接続不可時のデフォルト） ---
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

JP_BENCHMARKS = {"なし（絶対値）": None, "TOPIX (1306.T)": "1306.T", "日経平均": "^N225"}
US_BENCHMARKS = {"なし（絶対値）": None, "S&P500": "^GSPC", "NASDAQ100": "^NDX"}

# --- TOPIX-17 ETF 定義 ---
TOPIX17_ETF_MAPPING = {
    "① 金融・金利敏感": ["1631", "1632", "1633"],
    "② ディフェンシブ": ["1617", "1621", "1627", "1628", "1630"],
    "③ 景気敏感バリュー": ["1618", "1619", "1620", "1623", "1629"],
    "④ グローバル製造業": ["1622", "1624"],
    "⑤ 情報通信・グロース": ["1625", "1626"]
}

TOPIX17_NAMES = {
    "1617": "食品", "1618": "エネルギー資源", "1619": "建設・資材", "1620": "素材・化学",
    "1621": "医薬品", "1622": "自動車・輸送機", "1623": "鉄鋼・非鉄", "1624": "機械",
    "1625": "電機・精密", "1626": "情報通信・サービス他", "1627": "電気・ガス",
    "1628": "運輸・物流", "1629": "商社・卸売", "1630": "小売", "1631": "銀行",
    "1632": "金融（除く銀行）", "1633": "不動産"
}

# --- 環境判定とディレクトリ設定 ---
def setup_directories():
    is_colab = False
    try:
        from google.colab import drive
        is_colab = True
    except ImportError:
        pass
        
    is_kaggle = os.environ.get('KAGGLE_KERNEL_RUN_TYPE') is not None
    
    # settings.pyは project_root/config に配置されるため、親階層を project_root と判定する
    current_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else os.getcwd()
    project_root = os.path.dirname(current_dir)
    
    if is_colab:
        drive_path = "/content/drive/MyDrive/stock_data_hub"
        if not os.path.exists("/content/drive/MyDrive") and os.path.exists("/content/drive/My Drive"):
            drive_path = "/content/drive/My Drive/stock_data_hub"
        work_path = "/content/stock_data_work"
    elif is_kaggle:
        drive_path = "/kaggle/working/drive/MyDrive/stock_data_hub" 
        work_path = "/kaggle/working/stock_data_work"
    else:
        drive_path = os.path.join(project_root, "data_drive")
        work_path = os.path.join(project_root, "data_work")

    os.makedirs(drive_path, exist_ok=True)
    os.makedirs(work_path, exist_ok=True)
    return project_root, drive_path, work_path

PROJECT_ROOT, DRIVE_DIR, WORK_DIR = setup_directories()