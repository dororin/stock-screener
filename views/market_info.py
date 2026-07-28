import io
import re
import json
import requests
import numpy as np
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
import yfinance as yf

from config import settings
from data_access.sheets_api import conn
from streamlit_lightweight_charts import renderLightweightCharts

# =====================================================================
# 📊 LWC 共通ヘルパー関数
# =====================================================================
def _to_lwc_time(dt_series) -> list:
    return [str(d)[:10] for d in dt_series]

def _lwc_base_options(height: int = 160) -> dict:
    return {
        "height": height,
        "layout": {
            "background": {"type": "solid", "color": "transparent"},
            "textColor": "#9e9e9e",
            "fontSize": 10,
        },
        "grid": {
            "vertLines": {"color": "rgba(128,128,128,0.12)"},
            "horzLines": {"color": "rgba(128,128,128,0.12)"},
        },
        "crosshair": {"mode": 1},
        "timeScale": {
            "borderColor": "rgba(128,128,128,0.3)",
            "timeVisible": True,
            "secondsVisible": False
        },
        "handleScroll": True,
        "handleScale": True,
    }


# =====================================================================
# 📡 NAAIM Exposure Index 収集・同期ロジック
# =====================================================================
def fetch_naaim_data() -> pd.DataFrame:
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
# 📈 セッション状態の初期化
# =====================================================================
if 'saitei_df' not in st.session_state:
    try:
        st.session_state.saitei_df = conn.read(spreadsheet=settings.MARKET_DATA_URL, worksheet="saitei_data", ttl=3600)
        st.session_state.saitei_df['Date'] = pd.to_datetime(st.session_state.saitei_df['Date'])
    except Exception:
        st.session_state.saitei_df = pd.DataFrame()

if 'sinyou_df' not in st.session_state:
    try:
        st.session_state.sinyou_df = conn.read(spreadsheet=settings.MARKET_DATA_URL, worksheet="sinyou_data", ttl=3600)
        st.session_state.sinyou_df['Date'] = pd.to_datetime(st.session_state.sinyou_df['Date'])
    except Exception:
        st.session_state.sinyou_df = pd.DataFrame()

if 'naaim_df' not in st.session_state:
    try:
        st.session_state.naaim_df = conn.read(spreadsheet=settings.MARKET_DATA_URL, worksheet="naaim_data", ttl=3600)
        st.session_state.naaim_df['Date'] = pd.to_datetime(st.session_state.naaim_df['Date'])
    except Exception:
        st.session_state.naaim_df = pd.DataFrame()


st.title("📈 マーケット情報")
st.write("---")


# =====================================================================
# 📊 【フラグメント1】全体指数分析ダッシュボード（LWC化 ＆ 閉域化）
# =====================================================================
@st.fragment
def render_market_dashboard_fragment():
    """指数最新化・期間選択・各種LWCダッシュボードを内包。ボタンやスライダー操作が外部に影響しません。"""
    
    # フラグメント内での指数最新化処理
    if st.button("🔄 マーケット指数データを最新化 (Google Sheets同期)", type="primary", use_container_width=True):
        with st.spinner("外部サイトから指数情報を最新化しています..."):
            df_s = update_and_load_saitei_data()
            if not df_s.empty:
                st.session_state.saitei_df = df_s
            df_m = update_and_load_sinyou_data()
            if not df_m.empty:
                st.session_state.sinyou_df = df_m
            df_n = update_and_load_naaim_data()
            if not df_n.empty:
                st.session_state.naaim_df = df_n
            st.success("指数データの同期が完了しました。")
            st.rerun(scope="fragment")

    st.markdown("### 📊 指数複合ダッシュボード")
    
    # 期間コントロール
    period = st.radio(
        "表示期間の変更:", 
        ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "3年", "全"], 
        index=3, 
        horizontal=True,
        key="dashboard_period_selector"
    )

    saitei_df = st.session_state.saitei_df
    sinyou_df = st.session_state.sinyou_df
    naaim_df = st.session_state.naaim_df

    if saitei_df.empty and sinyou_df.empty and naaim_df.empty:
        st.warning("⚠️ 指数データがありません。上記ボタンを押して初期データを同期・取得してください。")
        return

    # 期間フィルター計算
    end_dt = saitei_df['Date'].max() if not saitei_df.empty else pd.Timestamp.now()
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
        start_dt = saitei_df['Date'].min() if not saitei_df.empty else end_dt - pd.DateOffset(years=10)

    # 1. 総合メトリクス表示
    m_col1, m_col2 = st.columns(2)
    with m_col1:
        if not naaim_df.empty:
            latest_naaim = naaim_df.iloc[-1]
            prev_naaim = naaim_df.iloc[-2] if len(naaim_df) > 1 else latest_naaim
            delta = round(latest_naaim['NAAIM'] - prev_naaim['NAAIM'], 2)
            st.metric("最新 NAAIM Exposure Index", f"{latest_naaim['NAAIM']}", delta=f"{delta}")
            st.caption(f"更新日: {latest_naaim['Date'].strftime('%Y-%m-%d')}")

    # 日本株指数データの加工マージ
    df_jp = pd.DataFrame()
    if not saitei_df.empty and not sinyou_df.empty:
        d1 = saitei_df.copy()
        d2 = sinyou_df.copy()
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

        # 期間でスライス
        df_jp = df_jp[(df_jp['date'] >= start_dt) & (df_jp['date'] <= end_dt)]

    st.write("---")

    # 2. チャート1: 日経平均 (左) & 裁定倍率 (右) の LWC 重ね書き
    if not df_jp.empty:
        st.markdown("**📈 日経平均 ＆ 裁定倍率推移**")
        times = _to_lwc_time(df_jp['date'])
        
        nk_data = [{"time": t, "value": float(v)} for t, v in zip(times, df_jp[nik_col]) if not pd.isna(v)]
        ratio_data = [{"time": t, "value": float(v)} for t, v in zip(times, df_jp['ratio_sai']) if not pd.isna(v)]

        chart_options = _lwc_base_options(height=260)
        chart_options["leftPriceScale"] = {"visible": True, "borderColor": "rgba(128,128,128,0.3)"}
        chart_options["rightPriceScale"] = {"visible": True, "borderColor": "rgba(128,128,128,0.3)"}

        chart_def = {
            "chart": chart_options,
            "series": [
                {
                    "type": "Line",
                    "data": nk_data,
                    "options": {
                        "color": "#ffa726",
                        "lineWidth": 2,
                        "priceScaleId": "left",
                        "title": "日経平均 (左軸)",
                        "lastValueVisible": True,
                    }
                },
                {
                    "type": "Line",
                    "data": ratio_data,
                    "options": {
                        "color": "#ef5350",
                        "lineWidth": 2,
                        "priceScaleId": "right",
                        "title": "裁定倍率 (右軸)",
                        "lastValueVisible": True,
                    }
                }
            ]
        }
        renderLightweightCharts([chart_def], key="lwc_jp_index")

        # 3. チャート2: 裁定買残 (Histogram) ＆ 信用比率 (Area)
        st.markdown("**📊 裁定買残 (億円) ＆ 信用比率 (買残/日経平均)**")
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            st.caption("裁定買残推移")
            sai_vol_data = [{"time": t, "value": float(v), "color": "rgba(31, 119, 180, 0.75)"} for t, v in zip(times, df_jp[buy_sai_col]) if not pd.isna(v)]
            chart_def_sai = {
                "chart": _lwc_base_options(height=180),
                "series": [{"type": "Histogram", "data": sai_vol_data, "options": {"color": "#1f77b4"}}]
            }
            renderLightweightCharts([chart_def_sai], key="lwc_sai_vol")

        with col_c2:
            st.caption("信用比率推移")
            sin_ratio_data = [{"time": t, "value": float(v)} for t, v in zip(times, df_jp['ratio_sin']) if not pd.isna(v)]
            chart_def_sin = {
                "chart": _lwc_base_options(height=180),
                "series": [{
                    "type": "Area", 
                    "data": sin_ratio_data, 
                    "options": {
                        "topColor": "rgba(38, 166, 154, 0.4)", 
                        "bottomColor": "rgba(38, 166, 154, 0.05)", 
                        "lineColor": "#26a69a", 
                        "lineWidth": 1.5
                    }
                }]
            }
            renderLightweightCharts([chart_def_sin], key="lwc_sin_ratio")

    # 4. チャート3: NAAIM Index (US)
    if not naaim_df.empty:
        st.write("---")
        st.markdown("**🇺🇸 NAAIM Exposure Index 推移**")
        df_us = naaim_df[(naaim_df['Date'] >= start_dt) & (naaim_df['Date'] <= end_dt)].sort_values('Date')
        us_times = _to_lwc_time(df_us['Date'])
        naaim_data = [{"time": t, "value": float(v)} for t, v in zip(us_times, df_us['NAAIM']) if not pd.isna(v)]

        chart_def_naaim = {
            "chart": _lwc_base_options(height=200),
            "series": [{
                "type": "Line", 
                "data": naaim_data, 
                "options": {
                    "color": "#2e5bff", 
                    "lineWidth": 2, 
                    "title": "NAAIM Index", 
                    "lastValueVisible": True
                }
            }]
        }
        renderLightweightCharts([chart_def_naaim], key="lwc_naaim_index")


# =====================================================================
# 📈 【フラグメント2】個別銘柄の信用残検索（LWC化 ＆ 閉域化）
# =====================================================================
@st.fragment
def render_individual_margin_fragment():
    """個別株の信用残検索。上部ダッシュボードを一切巻き込まず、個別銘柄の描画が1ミリ秒で更新されます。"""
    st.subheader("🔍 個別銘柄 信用残検索 (IRBank)")
    
    col_s1, col_s2 = st.columns([1, 4])
    with col_s1:
        search_code = st.text_input("銘柄コード", value="1321", placeholder="例: 1321", key="margin_search_code")
    with col_s2:
        period_ir = st.radio("表示期間の変更:", ["6ヶ月", "1年", "3年", "全"], index=1, horizontal=True, key="ir_p_selector")

    if search_code:
        with st.spinner(f"{search_code} の信用残データを取得中..."):
            idf = fetch_irbank_margin(search_code)
            if not idf.empty:
                i_end = idf['Date'].max()
                if period_ir == "6ヶ月":
                    i_start = i_end - pd.DateOffset(months=6)
                elif period_ir == "1年":
                    i_start = i_end - pd.DateOffset(years=1)
                elif period_ir == "3年":
                    i_start = i_end - pd.DateOffset(years=3)
                else:
                    i_start = idf['Date'].min()
                    
                vdf = idf[idf['Date'] >= i_start].sort_values('Date')
                
                if not vdf.empty:
                    times = _to_lwc_time(vdf['Date'])
                    buy_shares = [{"time": t, "value": float(v)} for t, v in zip(times, vdf['Buy(Shares)']) if not pd.isna(v)]
                    sell_shares = [{"time": t, "value": float(v)} for t, v in zip(times, vdf['Sell(Shares)']) if not pd.isna(v)]

                    chart_options = _lwc_base_options(height=300)
                    chart_options["rightPriceScale"] = {"visible": True, "borderColor": "rgba(128,128,128,0.3)"}

                    chart_def = {
                        "chart": chart_options,
                        "series": [
                            {
                                "type": "Area",
                                "data": buy_shares,
                                "options": {
                                    "topColor": "rgba(239, 83, 80, 0.35)",
                                    "bottomColor": "rgba(239, 83, 80, 0.05)",
                                    "lineColor": "#ef5350",
                                    "lineWidth": 2,
                                    "title": "信用買い残 (株)",
                                    "lastValueVisible": True,
                                }
                            },
                            {
                                "type": "Area",
                                "data": sell_shares,
                                "options": {
                                    "topColor": "rgba(66, 165, 245, 0.35)",
                                    "bottomColor": "rgba(66, 165, 245, 0.05)",
                                    "lineColor": "#42a5f5",
                                    "lineWidth": 2,
                                    "title": "信用売り残 (株)",
                                    "lastValueVisible": True,
                                }
                            }
                        ]
                    }
                    renderLightweightCharts([chart_def], key=f"lwc_margin_ind_{search_code}")
                else:
                    st.caption("指定期間のデータがありません。")
            else:
                st.warning("IRBankからデータが見つかりませんでした。日本株のコードを再確認してください。")


# =====================================================================
# 呼び出し実行部 (全体再描画を挟まない並列配置)
# =====================================================================

# 1. 全体指数分析ダッシュボードフラグメントの実行
render_market_dashboard_fragment()

st.write("---")

# 2. 個別信用残検索フラグメントの実行
render_individual_margin_fragment()