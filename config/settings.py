# config/settings.py
import os

try:
    import streamlit as st
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# --- 環境判定とディレクトリ設定（ロードの都合上、先に定義） ---
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def setup_directories():
    is_colab = False
    try:
        from google.colab import drive
        is_colab = True
    except ImportError:
        pass
        
    is_kaggle = os.environ.get('KAGGLE_KERNEL_RUN_TYPE') is not None
    
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 🚀 secrets.toml からの共通環境設定ロード（Streamlit/ローカル双方対応）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
cfg_secrets = {}

# 1. Streamlit Secrets のロード試行
if HAS_STREAMLIT:
    try:
        if hasattr(st, "secrets") and st.secrets:
            cfg_secrets = dict(st.secrets)
    except Exception:
        pass

# 2. 非GUI環境（ローカル実行時等）のための直接 TOML ロード試行
if not cfg_secrets:
    secrets_path = os.path.join(PROJECT_ROOT, ".streamlit", "secrets.toml")
    if os.path.exists(secrets_path):
        try:
            import toml
            cfg_secrets = toml.load(secrets_path)
        except Exception as e:
            print(f"⚠️ config/settings: secrets.toml の直接ロードに失敗しました: {e}")

if not cfg_secrets:
    raise RuntimeError(
        "❌ 認証・設定ファイル (secrets.toml) が正常にロードされていません。プログラムの実行を中断します。"
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 🚀 新しい secrets.toml のキーから環境設定を安全に取得
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
try:
    FOLDER_ID = cfg_secrets["FOLDER_ID"]
    LOGS_FOLDER_ID = cfg_secrets["LOGS_FOLDER_ID"]
    MARKET_DATA_URL = cfg_secrets["spreadsheet"]
    SPREADSHEET_VWF_URL = cfg_secrets["spreadsheet_VWF_url"]
except KeyError as e:
    raise KeyError(
        f"❌ secrets.toml に必要な設定 {e} が不足しています。プログラムの実行を安全に中断します。"
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# --- 各種外部URL・基本設定 ---
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
TIMEFRAMES = ["1d", "60m", "5m", "1m"]

# --- Google Sheets 内のシート名設定 ---
WATCHLIST_SHEET_NAME = "watchlist"
REPAIR_LOG_SHEET_NAME = "repair_log"
EXTRA_TICKERS_SHEET = "extra_tickers"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# --- セクター定義（スプレッドシート接続不可時のデフォルト） ---
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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

# --- Solactive PCF CSV 設定 ---
SOLACTIVE_PCF_BASE_URL = "https://www.solactive.com/downloads/etfservices/tse-pcf/single/"