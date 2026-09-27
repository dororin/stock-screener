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
from utils.ui_components import card_container, render_card_header, build_status_badge
from utils.plotting import (
    _to_lwc_time,
    render_lwc_dual_line_chart,
    render_lwc_area_chart,
    render_lwc_histogram_chart
)

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
st.caption("日経平均・裁定取引・信用取引残高および米NAAIM指数の動向をモニタリングします。")

# =====================================================================
# 📊 【フラグメント1】全体指数分析ダッシュボード
# =====================================================================
@st.fragment
def render_market_dashboard_fragment():
    with card_container(border=True):
        col_ctrl1, col_ctrl2 = st.columns([3, 1])
        with col_ctrl1:
            period = st.radio(
                "表示期間", 
                ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "3年", "全期間"], 
                index=3, 
                horizontal=True,
                key="dashboard_period_selector"
            )
        with col_ctrl2:
            if st.button("🔄 指数データを最新化", type="primary", use_container_width=True, help="外部サイトから指数情報を同期します"):
                with st.spinner("外部サイトから指数情報を最新化中..."):
                    df_s = update_and_load_saitei_data()
                    if not df_s.empty:
                        st.session_state.saitei_df = df_s
                    df_m = update_and_load_sinyou_data()
                    if not df_m.empty:
                        st.session_state.sinyou_df = df_m
                    df_n = update_and_load_naaim_data()
                    if not df_n.empty:
                        st.session_state.naaim_df = df_n
                    st.toast("✅ 指数データの同期が完了しました。")
                    st.rerun(scope="fragment")

    saitei_df = st.session_state.saitei_df
    sinyou_df = st.session_state.sinyou_df
    naaim_df = st.session_state.naaim_df

    if saitei_df.empty and sinyou_df.empty and naaim_df.empty:
        st.warning("⚠️ 指数データがありません。上記ボタンを押して初期データを同期・取得してください。")
        return

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
        df_jp = df_jp[(df_jp['date'] >= start_dt) & (df_jp['date'] <= end_dt)]

    st.markdown("### 📊 日本市場 指数＆需給複合分析")

    # 1. 日経平均 & 裁定倍率
    if not df_jp.empty:
        times = _to_lwc_time(df_jp['date'])
        latest_row = df_jp.iloc[-1]
        prev_row = df_jp.iloc[-2] if len(df_jp) > 1 else latest_row
        
        nik_latest = latest_row[nik_col]
        nik_diff = nik_latest - prev_row[nik_col]
        nik_pct = (nik_diff / prev_row[nik_col] * 100.0) if prev_row[nik_col] > 0 else 0.0

        sai_ratio_latest = latest_row['ratio_sai']
        sai_ratio_diff = sai_ratio_latest - prev_row['ratio_sai']

        with card_container(border=True):
            badge_html = (
                f"<span style='color:#ffa726; margin-right:12px;'>日経平均(左): <b>¥{nik_latest:,.0f}</b></span>"
                f"<span style='color:#ef5350;'>裁定倍率(右): <b>{sai_ratio_latest:.4f}</b></span> "
                f"<span style='color:#9e9e9e; margin-left:8px;'>(更新: {latest_row['date'].strftime('%Y-%m-%d')})</span>"
            )
            render_card_header(
                title="日経平均 ＆ 裁定倍率推移",
                mom_value=nik_pct,
                mom_sub_text=f"倍率差: {sai_ratio_diff:+.4f}",
                badge_html=badge_html
            )

            nk_data = [{"time": t, "value": float(v)} for t, v in zip(times, df_jp[nik_col]) if not pd.isna(v)]
            ratio_data = [{"time": t, "value": float(v)} for t, v in zip(times, df_jp['ratio_sai']) if not pd.isna(v)]

            render_lwc_dual_line_chart(
                series1_data=nk_data,
                series2_data=ratio_data,
                title1="日経平均",
                title2="裁定倍率",
                height=230,
                key="lwc_jp_index"
            )

        col_g1, col_g2 = st.columns(2)

        with col_g1:
            with card_container(border=True):
                sai_val_latest = latest_row[buy_sai_col]
                sai_val_prev = prev_row[buy_sai_col]
                sai_val_diff = sai_val_latest - sai_val_prev
                sign_sai = "+" if sai_val_diff >= 0 else ""

                render_card_header(
                    title="裁定買残推移 (億円)",
                    mom_sub_text=f"前日比: {sign_sai}{sai_val_diff:,} 億円",
                    badge_html=f"<span style='color:#42a5f5;'>最新: <b>{sai_val_latest:,} 億円</b></span>"
                )

                sai_vol_data = [
                    {"time": t, "value": float(v), "color": "rgba(66, 165, 245, 0.65)"} 
                    for t, v in zip(times, df_jp[buy_sai_col]) if not pd.isna(v)
                ]
                render_lwc_histogram_chart(sai_vol_data, color="#42a5f5", height=160, key="lwc_sai_vol")

        with col_g2:
            with card_container(border=True):
                sin_ratio_latest = latest_row['ratio_sin']
                sin_ratio_prev = prev_row['ratio_sin']
                sin_ratio_diff = sin_ratio_latest - sin_ratio_prev
                sign_sin = "+" if sin_ratio_diff >= 0 else ""

                render_card_header(
                    title="信用比率 (買残 ÷ 日経平均)",
                    mom_sub_text=f"前週比: {sign_sin}{sin_ratio_diff:.2f}",
                    badge_html=f"<span style='color:#26a69a;'>最新: <b>{sin_ratio_latest:.2f}</b></span>"
                )

                sin_ratio_data = [
                    {"time": t, "value": float(v)} 
                    for t, v in zip(times, df_jp['ratio_sin']) if not pd.isna(v)
                ]
                render_lwc_area_chart(
                    series_definitions=[{
                        "type": "Area", 
                        "data": sin_ratio_data, 
                        "options": {
                            "topColor": "rgba(38, 166, 154, 0.35)", 
                            "bottomColor": "rgba(38, 166, 154, 0.03)", 
                            "lineColor": "#26a69a", 
                            "lineWidth": 2,
                            "lastValueVisible": True
                        }
                    }],
                    height=160,
                    key="lwc_sin_ratio"
                )

    # 3. NAAIM Exposure Index
    if not naaim_df.empty:
        st.write("---")
        st.markdown("### 🇺🇸 米国市場 NAAIM Exposure Index")

        df_us = naaim_df[(naaim_df['Date'] >= start_dt) & (naaim_df['Date'] <= end_dt)].sort_values('Date')
        if not df_us.empty:
            latest_naaim = df_us.iloc[-1]
            prev_naaim = df_us.iloc[-2] if len(df_us) > 1 else latest_naaim
            delta = round(latest_naaim['NAAIM'] - prev_naaim['NAAIM'], 2)
            sign_n = "+" if delta >= 0 else ""
            badge_text = "🟢 強気傾向" if latest_naaim['NAAIM'] >= 80 else "🔴 弱気傾向" if latest_naaim['NAAIM'] <= 40 else "⚪ 中立"
            badge_tag = build_status_badge(badge_text, bg_color="#37474f", text_color="#ffffff", border_color="#78909c")

            with card_container(border=True):
                badge_html = (
                    f"{badge_tag} "
                    f"<span style='color:#9e9e9e; margin-left:6px;'>更新日: {latest_naaim['Date'].strftime('%Y-%m-%d')}</span>"
                )
                render_card_header(
                    title="NAAIM Exposure Index",
                    mom_sub_text=f"前週差: {sign_n}{delta}",
                    badge_html=badge_html
                )

                us_times = _to_lwc_time(df_us['Date'])
                naaim_data = [{"time": t, "value": float(v)} for t, v in zip(us_times, df_us['NAAIM']) if not pd.isna(v)]

                render_lwc_area_chart(
                    series_definitions=[{
                        "type": "Line", 
                        "data": naaim_data, 
                        "options": {
                            "color": "#42a5f5", 
                            "lineWidth": 2, 
                            "title": "", 
                            "lastValueVisible": True,
                            "crosshairMarkerVisible": True,
                        }
                    }],
                    height=180,
                    key="lwc_naaim_index"
                )

# =====================================================================
# 📈 【フラグメント2】個別銘柄の信用残検索
# =====================================================================
@st.fragment
def render_individual_margin_fragment():
    st.subheader("🔍 個別銘柄 信用残検索 (IRBank)")

    with card_container(border=True):
        col_s1, col_s2 = st.columns([1, 4])
        with col_s1:
            search_code = st.text_input("銘柄コード", value="1321", placeholder="例: 1321", key="margin_search_code")
        with col_s2:
            period_ir = st.radio("表示期間", ["6ヶ月", "1年", "3年", "全期間"], index=1, horizontal=True, key="ir_p_selector")

    if search_code:
        clean_code = str(search_code).strip().upper()

        with st.spinner(f"{clean_code} の信用残データを取得中..."):
            idf = fetch_irbank_margin(clean_code)
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
                    
                vdf = idf[idf['Date'] >= i_start].sort_values('Date').reset_index(drop=True)
                
                if not vdf.empty:
                    latest_ir = vdf.iloc[-1]
                    prev_ir = vdf.iloc[-2] if len(vdf) > 1 else latest_ir

                    buy_now = latest_ir['Buy(Shares)']
                    sell_now = latest_ir['Sell(Shares)']
                    buy_diff = buy_now - prev_ir['Buy(Shares)']
                    sell_diff = sell_now - prev_ir['Sell(Shares)']
                    margin_ratio = (buy_now / sell_now) if sell_now > 0 else 0.0

                    times = _to_lwc_time(vdf['Date'])
                    buy_shares = [{"time": t, "value": float(v)} for t, v in zip(times, vdf['Buy(Shares)']) if not pd.isna(v)]
                    sell_shares = [{"time": t, "value": float(v)} for t, v in zip(times, vdf['Sell(Shares)']) if not pd.isna(v)]

                    with card_container(border=True):
                        badge_html = (
                            f"<span style='color:#ef5350; margin-right:12px;'>信用買残: <b>{buy_now:,} 株</b> ({buy_diff:+,.0f})</span>"
                            f"<span style='color:#42a5f5; margin-right:12px;'>信用売残: <b>{sell_now:,} 株</b> ({sell_diff:+,.0f})</span>"
                            f"<span style='color:#b0bec5;'>倍率: <b>{margin_ratio:.2f} 倍</b></span>"
                        )
                        render_card_header(
                            title=f"{clean_code} 信用残高推移",
                            code=clean_code,
                            is_jp=True,
                            mom_sub_text="IRBank提供",
                            badge_html=badge_html
                        )

                        render_lwc_area_chart(
                            series_definitions=[
                                {
                                    "type": "Area",
                                    "data": buy_shares,
                                    "options": {
                                        "topColor": "rgba(239, 83, 80, 0.35)",
                                        "bottomColor": "rgba(239, 83, 80, 0.03)",
                                        "lineColor": "#ef5350",
                                        "lineWidth": 2,
                                        "title": "信用買残 (株)",
                                        "lastValueVisible": True,
                                    }
                                },
                                {
                                    "type": "Area",
                                    "data": sell_shares,
                                    "options": {
                                        "topColor": "rgba(66, 165, 245, 0.35)",
                                        "bottomColor": "rgba(66, 165, 245, 0.03)",
                                        "lineColor": "#42a5f5",
                                        "lineWidth": 2,
                                        "title": "信用売残 (株)",
                                        "lastValueVisible": True,
                                    }
                                }
                            ],
                            height=240,
                            key=f"lwc_margin_ind_{clean_code}"
                        )
                else:
                    st.caption("指定期間のデータがありません。")
            else:
                st.warning("IRBankからデータが見つかりませんでした。日本株のコードを再確認してください。")

# =====================================================================
# 🚀 画面描画制御部
# =====================================================================
render_market_dashboard_fragment()
st.write("---")
render_individual_margin_fragment()