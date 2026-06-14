# views/market_info.py
import io
import re
import json
import requests
import numpy as np
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

from config import settings
from data_access.sheets_api import conn
from utils.plotting import plot_market_dashboard, plot_individual_margin

# =====================================================================
# 📡 NAAIM Exposure Index 収集・同期ロジック
# =====================================================================

def fetch_naaim_data() -> pd.DataFrame:
    """NAAIM公式サイトから週次のExposure Indexのエクセルリンクを特定し、取得します。"""
    base_url = "https://naaim.org/programs/naaim-exposure-index/"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(base_url, headers=headers, timeout=15)
        if res.status_code != 200:
            return pd.DataFrame()
        soup = BeautifulSoup(res.text, "html.parser")
        links = soup.find_all("a", href=re.compile(r"\.xlsx$"))
        excel_url = None
        for link in links:
            if "HERE" in link.get_text().upper():
                excel_url = link.get('href')
                break
        if not excel_url and links:
            excel_url = links[0].get('href')
        if not excel_url:
            return pd.DataFrame()
        
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

def update_and_load_naaim_data() -> pd.DataFrame:
    """既存のSheetsデータを取得し、外部新データと重複排除したうえで差分マージ同期します。"""
    existing_df = pd.DataFrame(columns=['Date', 'NAAIM'])
    if conn is not None:
        try:
            existing_df = conn.read(spreadsheet=settings.MARKET_DATA_URL, worksheet="naaim_data", ttl=0)
            if existing_df is not None and not existing_df.empty:
                existing_df['Date'] = pd.to_datetime(existing_df['Date'], errors='coerce')
                existing_df = existing_df.dropna(subset=['NAAIM']).copy()
        except Exception:
            pass
    
    web_df = fetch_naaim_data()
    merged_df = web_df if existing_df.empty else pd.concat([existing_df, web_df]) if not web_df.empty else existing_df
        
    if not merged_df.empty:
        merged_df['Date'] = pd.to_datetime(merged_df['Date']).dt.normalize()
        merged_df = merged_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
        
    if conn is not None and not merged_df.empty:
        try:
            save_df = merged_df.copy()
            save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
            conn.update(spreadsheet=settings.MARKET_DATA_URL, worksheet="naaim_data", data=save_df)
        except Exception:
            pass
    return merged_df

# =====================================================================
# 📡 信用残高（IRBank / 日経225JP）収集・同期ロジック
# =====================================================================

def fetch_irbank_margin(code: str) -> pd.DataFrame:
    """IRBankの margin ページから個別日本株の買い残、売り残の週次推移をスクレイピングします。"""
    url = f"https://irbank.net/{code}/margin"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return pd.DataFrame()
        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            return pd.DataFrame()
        rows = table.find_all("tr")
        data = []
        current_year = str(pd.Timestamp.now().year)
        for row in rows:
            if "occ" in row.get('class', []):
                year_td = row.find("td", class_="ct")
                if year_td:
                    year_val = year_td.get_text(strip=True)
                    if re.match(r"^\d{4}$", year_val):
                        current_year = year_val
                continue
            if any(cls in row.get('class', []) for cls in ["obb", "odd"]):
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue
                date_text = cells[0].get_text(strip=True)
                if not re.match(r"^\d{1,2}/\d{1,2}$", date_text):
                    continue
                try:
                    buy_text = cells[1].get_text(separator="|", strip=True).split("|")[0].replace(",", "")
                    sell_text = cells[3].get_text(separator="|", strip=True).split("|")[0].replace(",", "")
                    data.append({
                        'Date': pd.to_datetime(f"{current_year}/{date_text}"),
                        'Buy(Shares)': int(buy_text),
                        'Sell(Shares)': int(sell_text)
                    })
                except Exception:
                    continue
        df = pd.DataFrame(data)
        if not df.empty:
            df = df.drop_duplicates(subset=['Date']).sort_values('Date').reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()

def fetch_sinyou_data() -> pd.DataFrame:
    """日経225JPのデイリー週次JSONから全体の信用買い、売り合計推移をスクレイピングします。"""
    url = "https://nikkei225jp.com/_data/_nfsWEB/DAY/dailyweek2.json"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://nikkei225jp.com/data/sinyou.php"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        res.encoding = 'utf-8'
        if res.status_code != 200:
            return pd.DataFrame()
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
    except Exception:
        return pd.DataFrame()

def update_and_load_sinyou_data() -> pd.DataFrame:
    """Sheetsから既存信用合計推移をロードし、最新差分を結合・マージして再保存します。"""
    if conn is None:
        return pd.DataFrame()
    try:
        existing_df = conn.read(spreadsheet=settings.MARKET_DATA_URL, worksheet="sinyou_data", ttl=0)
        if existing_df is not None and not existing_df.empty:
            existing_df['Date'] = pd.to_datetime(existing_df['Date'], errors='coerce')
    except Exception:
        existing_df = pd.DataFrame()
    
    web_df = fetch_sinyou_data()
    merged_df = web_df if existing_df.empty else pd.concat([existing_df, web_df]) if not web_df.empty else existing_df
        
    if not merged_df.empty:
        merged_df['Date'] = pd.to_datetime(merged_df['Date']).dt.normalize()
        merged_df = merged_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
        
    try:
        if not merged_df.empty:
            save_df = merged_df.copy()
            save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
            conn.update(spreadsheet=settings.MARKET_DATA_URL, worksheet="sinyou_data", data=save_df)
    except Exception:
        pass
    return merged_df

# =====================================================================
# 📡 裁定取引残高 収集・同期ロジック
# =====================================================================

def parse_saitei_amount(val) -> float:
    try:
        if not val or val == "":
            return np.nan
        return int(str(val).replace(',', '').strip()) // 100
    except Exception:
        return np.nan

def fetch_saitei_data() -> pd.DataFrame:
    """日経225JPの裁定推移用生JSONから裁定売り・買い（億円単位）を取得します。"""
    url = "https://nikkei225jp.com/_data/_nfsWEB/HS_DATA_DAY/daily_saitei.json"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://nikkei225jp.com/data/saitei.php"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        res.encoding = 'utf-8'
        if res.status_code != 200:
            return pd.DataFrame()
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
    except Exception:
        return pd.DataFrame()

def update_and_load_saitei_data() -> pd.DataFrame:
    """裁定取引残高データをGoogle Sheets経由で最新にマージ同期します。"""
    if conn is None:
        return pd.DataFrame()
    try:
        existing_df = conn.read(spreadsheet=settings.MARKET_DATA_URL, worksheet="saitei_data", ttl=0)
        if existing_df is not None and not existing_df.empty:
            existing_df['Date'] = pd.to_datetime(existing_df['Date'], errors='coerce')
    except Exception:
        existing_df = pd.DataFrame()
    
    web_df = fetch_saitei_data()
    merged_df = web_df if existing_df.empty else pd.concat([existing_df, web_df]) if not web_df.empty else existing_df
        
    if not merged_df.empty:
        merged_df['Date'] = pd.to_datetime(merged_df['Date']).dt.normalize()
        merged_df = merged_df.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
        
    try:
        if not merged_df.empty:
            save_df = merged_df.copy()
            save_df['Date'] = save_df['Date'].dt.strftime('%Y-%m-%d')
            conn.update(spreadsheet=settings.MARKET_DATA_URL, worksheet="saitei_data", data=save_df)
    except Exception:
        pass
    return merged_df


# =====================================================================
# 📈 画面描画制御部 (フラグメント設計)
# =====================================================================

# セッション状態の初期化
if 'saitei_df' not in st.session_state:
    st.session_state.saitei_df = pd.DataFrame()
if 'sinyou_df' not in st.session_state:
    st.session_state.sinyou_df = pd.DataFrame()
if 'naaim_df' not in st.session_state:
    st.session_state.naaim_df = pd.DataFrame()

# タイトルヘッダー
st.title("📈 マーケット情報")

# 指数最新化ボタン（これはセッションデータを全体更新するため非フラグメントとします）
if st.button("マーケット指数データを最新化", type="primary"):
    with st.spinner("外部サイトから指数情報を収集しています..."):
        df_s = update_and_load_saitei_data()
        if not df_s.empty:
            st.session_state.saitei_df = df_s
        df_m = update_and_load_sinyou_data()
        if not df_m.empty:
            st.session_state.sinyou_df = df_m
        df_n = update_and_load_naaim_data()
        if not df_n.empty:
            st.session_state.naaim_df = df_n
        st.success("指数データの取得が完了しました。")
        st.rerun()  # データ更新後にアプリ全体を再ロードして各フラグメントに反映させる

st.write("---")


# ── 【フラグメント1】全体指数分析ダッシュボード ──
@st.fragment
def render_market_dashboard_fragment():
    """
    全体指数のダッシュボードを描画するフラグメント。
    表示期間（period）を変更しても、下部にある個別銘柄の検索・描画処理は巻き込まれません。
    """
    col1, col2 = st.columns([2, 3])
    with col1:
        st.subheader("📊 分析ダッシュボード")
    with col2:
        period = st.radio(
            "表示期間の変更:", 
            ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "3年", "全"], 
            index=3, 
            horizontal=True, 
            label_visibility="collapsed",
            key="market_dashboard_period"  # キーの重複を回避
        )
        
    # 選択した期間に基づく日付フィルターの計算
    end_dt = st.session_state.saitei_df['Date'].max() if not st.session_state.saitei_df.empty else pd.Timestamp.now()
    if period == "1ヶ月":
        start_dt = end_dt - pd.DateOffset(months=1)
    elif period == "3ヶ月":
        start_dt = end_dt - pd.DateOffset(months=3)
    elif period == "6ヶ月":
        start_dt = end_dt - pd.DateOffset(months=6)
    elif period == "1年":
        start_dt = end_dt - pd.DateOffset(years=1)
    elif period == "3年":
        start_dt = end_dt - pd.DateOffset(years=3)
    else:
        start_dt = st.session_state.saitei_df['Date'].min() if not st.session_state.saitei_df.empty else end_dt - pd.DateOffset(years=10)

    st.write("---")

    m_col1, _ = st.columns([1, 1])
    with m_col1:
        if not st.session_state.naaim_df.empty:
            latest_naaim = st.session_state.naaim_df.iloc[-1]
            prev_naaim = st.session_state.naaim_df.iloc[-2] if len(st.session_state.naaim_df) > 1 else latest_naaim
            delta = round(latest_naaim['NAAIM'] - prev_naaim['NAAIM'], 2)
            st.metric("最新 NAAIM Exposure Index", f"{latest_naaim['NAAIM']}", delta=f"{delta}")
            st.caption(f"更新日: {latest_naaim['Date'].strftime('%Y-%m-%d')}")
            
    # 総合マーケットサブプロットチャートの展開
    if not st.session_state.saitei_df.empty or not st.session_state.sinyou_df.empty or not st.session_state.naaim_df.empty:
        fig = plot_market_dashboard(st.session_state.saitei_df, st.session_state.sinyou_df, st.session_state.naaim_df)
        if fig:
            fig.update_xaxes(range=[start_dt, end_dt + pd.Timedelta(days=7)])
            if not st.session_state.saitei_df.empty:
                v = st.session_state.saitei_df[(st.session_state.saitei_df['Date'] >= start_dt) & (st.session_state.saitei_df['Date'] <= end_dt)]
                if not v.empty:
                    fig.update_yaxes(range=[v['Nikkei225'].min()*0.98, v['Nikkei225'].max()*1.02], row=1, col=1, secondary_y=False)
            fig.update_yaxes(fixedrange=True)
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("「マーケット指数データを最新化」ボタンを押して、スプレッドシートおよび最新データをロードしてください。")


# ── 【フラグメント2】個別銘柄の信用残検索 ──
@st.fragment
def render_individual_margin_fragment():
    """
    個別銘柄の検索窓とチャートを描画するフラグメント。
    銘柄コードを入力したり、期間を切り替えたりしても、上部にある非常に重い全体ダッシュボードは再描画されません。
    """
    st.subheader("🔍 個別銘柄 信用残検索 (IRBank)")
    c1, c2 = st.columns([1, 4])
    search_code = c1.text_input("銘柄コード", value="1321", placeholder="例: 1321", key="margin_search_code")
    
    if search_code:
        with st.spinner(f"{search_code} の信用残データを取得中..."):
            idf = fetch_irbank_margin(search_code)
            if not idf.empty:
                # フラグメント内のラジオボタンとしてkeyを設定
                p = st.radio("表示期間の変更:", ["6ヶ月", "1年", "3年", "全"], key="ir_p", horizontal=True)
                i_end = idf['Date'].max()
                if p == "6ヶ月":
                    i_start = i_end - pd.DateOffset(months=6)
                elif p == "1年":
                    i_start = i_end - pd.DateOffset(years=1)
                elif p == "3年":
                    i_start = i_end - pd.DateOffset(years=3)
                else:
                    i_start = idf['Date'].min()
                    
                vdf = idf[idf['Date'] >= i_start]
                if not vdf.empty:
                    ifig = plot_individual_margin(vdf, search_code)
                    st.plotly_chart(ifig, use_container_width=True)
            else:
                st.warning("IRBankからデータが見つかりませんでした。日本株のコードを再確認してください。")


# =====================================================================
# 呼び出し実行部
# =====================================================================

# 1. 全体指数分析ダッシュボードのフラグメントを実行
render_market_dashboard_fragment()

st.write("---")

# 2. 個別信用残検索のフラグメントを実行
render_individual_margin_fragment()