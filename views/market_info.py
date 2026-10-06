# views/market_info.py

import io
import re
import json
import requests
import numpy as np
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from datetime import datetime

from config import settings
from data_access.sheets_api import (
    conn,
    load_investor_type_data,
    save_investor_type_data
)
from core.macro_collector import fetch_macro_market_data_ondemand
from core.investor_type_collector import sync_jpx_investor_type_data
from utils.ui_components import card_container, render_card_header, build_status_badge
from utils.plotting import (
    _to_lwc_time,
    _lwc_base_options, 
    render_lwc_dual_line_chart,
    render_lwc_area_chart,
    render_lwc_histogram_chart,
    render_lwc_saitei_with_thresholds,
    render_lwc_investor_type_chart,
    render_lwc_macro_mini
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
# 📡 信用取引データ（新URL・評価損益率対応）
# =====================================================================
def fetch_sinyou_data() -> pd.DataFrame:
    url = settings.NIKKEI225JP_SINYOU_URL
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
            if len(r) >= 9 and str(r[4]).strip() != "" and str(r[6]).strip() != "":
                loss_rate = float(r[7]) if r[7] != "" and not pd.isna(r[7]) else np.nan
                margin_ratio = float(r[8]) if r[8] != "" and not pd.isna(r[8]) else np.nan
                data.append({
                    'Date': pd.to_datetime(r[0], unit='ms'),
                    'Nikkei225': float(r[1]) if r[1] != "" else np.nan,
                    'Sell(M-yen)': int(str(r[4]).replace(',', '')),
                    'Buy(M-yen)': int(str(r[6]).replace(',', '')),
                    'MarginLossRate': loss_rate,
                    'MarginRatio': margin_ratio
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
# 📡 裁定取引残高（新URL対応）
# =====================================================================
def parse_saitei_amount(val) -> float:
    try:
        if not val or val == "":
            return np.nan
        return int(str(val).replace(',', '').strip()) // 100
    except Exception:
        return np.nan

def fetch_saitei_data() -> pd.DataFrame:
    url = settings.NIKKEI225JP_SAITEI_URL
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
# 📡 個別銘柄 信用残高 (IRBank)
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

if 'investor_type_df' not in st.session_state:
    st.session_state.investor_type_df = load_investor_type_data()

st.title("📈 マーケット情報（Market Info Cockpit）")
st.caption("マクロ流動性・信用リスク・市場需給の歪みをリアルタイムに監視する総合コックピットです。")

# =====================================================================
# 🎛️ 【最上部】コントロールパネル
# =====================================================================
with card_container(border=True):
    col_ctrl1, col_ctrl2 = st.columns([3, 1])
    with col_ctrl1:
        period_label = st.radio(
            "表示期間", 
            ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "3年", "全期間"], 
            index=3, 
            horizontal=True,
            key="dashboard_period_selector"
        )
    with col_ctrl2:
        if st.button("🔄 指数データを最新化", type="primary", use_container_width=True, help="外部サイトおよびJPXから最新データを同期します"):
            with st.spinner("需給データおよびJPX投資部門別動向を最新化中..."):
                df_s = update_and_load_saitei_data()
                if not df_s.empty:
                    st.session_state.saitei_df = df_s
                df_m = update_and_load_sinyou_data()
                if not df_m.empty:
                    st.session_state.sinyou_df = df_m
                df_n = update_and_load_naaim_data()
                if not df_n.empty:
                    st.session_state.naaim_df = df_n
                df_inv = sync_jpx_investor_type_data()
                if not df_inv.empty:
                    st.session_state.investor_type_df = df_inv
                st.cache_data.clear()
                st.toast("✅ 指数・需給データの同期が完了しました。")
                st.rerun()

# 期間フィルタの計算
end_dt = pd.Timestamp.now()
if period_label == "1ヶ月":
    start_dt = end_dt - pd.DateOffset(months=1)
elif period_label == "3ヶ月":
    start_dt = end_dt - pd.DateOffset(months=3)
elif period_label == "6ヶ月":
    start_dt = end_dt - pd.DateOffset(months=6)
elif period_label == "1年":
    start_dt = end_dt - pd.DateOffset(years=1)
elif period_label == "3年":
    start_dt = end_dt - pd.DateOffset(years=3)
else:
    start_dt = end_dt - pd.DateOffset(years=10)

# オンデマンドマクロデータの取得（10分キャッシュ）
macro_data = fetch_macro_market_data_ondemand(period_label)

# =====================================================================
# 🇯🇵 【第1層：日本市場 需給＆日経同期コックピット】
# =====================================================================
st.markdown("### 🇯🇵 【第1層：日本市場 需給＆日経同期コックピット】")

df_nk = macro_data.get("^N225", pd.DataFrame())
saitei_df = st.session_state.saitei_df
inv_df = st.session_state.investor_type_df

with card_container(border=True):
    # ■ 上段：日経平均株価 (LWCライン / SMA 25, 75, 200)
    if not df_nk.empty:
        df_nk_filtered = df_nk[df_nk["date"] >= start_dt].copy()
        nk_latest = df_nk_filtered.iloc[-1]["close"]
        nk_prev = df_nk_filtered.iloc[-2]["close"] if len(df_nk_filtered) > 1 else nk_latest
        nk_mom = ((nk_latest - nk_prev) / nk_prev) * 100.0

        render_card_header(
            title="日経平均株価（終値 ＆ 移動平均線 25 / 75 / 200）",
            mom_value=nk_mom,
            mom_sub_text=f"前日比: {nk_latest - nk_prev:+,.1f} 円",
            badge_html=f"<span style='color:#ffa726;'>現在値: <b>¥{nk_latest:,.1f}</b></span>"
        )
        
        times_nk = _to_lwc_time(df_nk_filtered["date"])
        data_nk = [{"time": t, "value": round(float(v), 2)} for t, v in zip(times_nk, df_nk_filtered["close"])]
        data_sma25 = [{"time": t, "value": round(float(v), 2)} for t, v in zip(times_nk, df_nk_filtered["sma25"]) if not pd.isna(v)]
        data_sma75 = [{"time": t, "value": round(float(v), 2)} for t, v in zip(times_nk, df_nk_filtered["sma75"]) if not pd.isna(v)]
        data_sma200 = [{"time": t, "value": round(float(v), 2)} for t, v in zip(times_nk, df_nk_filtered["sma200"]) if not pd.isna(v)]

        chart_nk = {
            "chart": _lwc_base_options(height=200, right_offset=5),
            "series": [
                {"type": "Line", "data": data_nk, "options": {"color": "#ffffff", "lineWidth": 2, "title": "日経平均"}},
                {"type": "Line", "data": data_sma25, "options": {"color": "#ef5350", "lineWidth": 1, "title": "25SMA"}},
                {"type": "Line", "data": data_sma75, "options": {"color": "#ffa726", "lineWidth": 1, "title": "75SMA"}},
                {"type": "Line", "data": data_sma200, "options": {"color": "#ab47bc", "lineWidth": 1, "title": "200SMA"}},
            ]
        }
        try:
            from streamlit_lightweight_charts import renderLightweightCharts
            renderLightweightCharts([chart_nk], key="lwc_nk_cockpit")
        except Exception:
            pass

    st.markdown("<hr style='margin:0.4rem 0;'>", unsafe_allow_html=True)

    # ■ 中段：裁定買残高推移 (ヒストグラム + 赤2.5兆/青0.5兆線)
    if not saitei_df.empty:
        df_sai_filtered = saitei_df[(saitei_df["Date"] >= start_dt) & (saitei_df["Date"] <= end_dt)].copy()
        df_sai_filtered.columns = [str(c).lower().strip() for c in df_sai_filtered.columns]
        
        latest_sai = df_sai_filtered.iloc[-1]
        sai_val = latest_sai["buy(oku-yen)"]
        prev_sai = df_sai_filtered.iloc[-2]["buy(oku-yen)"] if len(df_sai_filtered) > 1 else sai_val
        sai_diff = sai_val - prev_sai

        alert_badge = ""
        if sai_val >= settings.SAITEI_ALERT_THRESHOLD_HIGH:
            alert_badge = build_status_badge("🚨 裁定買残2.5兆円超 (過熱・急落警戒)", bg_color="#d50000", text_color="#ffffff")
        elif sai_val <= settings.SAITEI_ALERT_THRESHOLD_LOW:
            alert_badge = build_status_badge("🟢 裁定買残0.5兆円割れ (大底圏・反発期)", bg_color="#1b5e20", text_color="#ffffff")

        render_card_header(
            title="裁定買残高推移（過熱警戒 2.5兆円ライン ＆ 底打ち 0.5兆円ライン）",
            mom_sub_text=f"前日比: {sai_diff:+,.0f} 億円",
            badge_html=f"<span style='color:#42a5f5; margin-right:8px;'>最新: <b>{sai_val:,.0f} 億円</b></span> {alert_badge}"
        )
        render_lwc_saitei_with_thresholds(
            df_sai_filtered, 
            high_threshold=settings.SAITEI_ALERT_THRESHOLD_HIGH,
            low_threshold=settings.SAITEI_ALERT_THRESHOLD_LOW,
            height=160, 
            key="saitei_cockpit"
        )

    st.markdown("<hr style='margin:0.4rem 0;'>", unsafe_allow_html=True)

    # ■ 下段：投資部門別売買動向 (海外/事業法人/個人の週間棒)
    if not inv_df.empty:
        df_inv_filtered = inv_df[pd.to_datetime(inv_df["date"]) >= start_dt].copy()
        if not df_inv_filtered.empty:
            latest_inv = df_inv_filtered.iloc[-1]
            f_net = latest_inv.get("海外投資家_差引(億円)", 0.0)
            c_net = latest_inv.get("事業法人_差引(億円)", 0.0)
            m_net = latest_inv.get("個人信用_差引(億円)", 0.0)

            badge_inv = (
                f"<span style='color:{'#26a69a' if f_net >= 0 else '#ef5350'}; margin-right:12px;'>海外勢: <b>{f_net:+,.0f} 億円</b></span>"
                f"<span style='color:#ab47bc; margin-right:12px;'>事業法人(自社株買い): <b>{c_net:+,.0f} 億円</b></span>"
                f"<span style='color:#ffa726;'>個人信用: <b>{m_net:+,.0f} 億円</b></span>"
            )
            render_card_header(
                title=f"投資部門別 株式売買状況 (週間純売買額 | 週: {latest_inv.get('week', '-')})",
                badge_html=badge_inv
            )
            render_lwc_investor_type_chart(df_inv_filtered, height=160, key="inv_cockpit")
        else:
            st.caption("指定期間内の投資部門別データはありません。")
    else:
        st.info("ℹ️ 投資部門別データはまだ取得されていません。「🔄 指数データを最新化」ボタンを押してJPXから取得してください。")

# =====================================================================
# ⚖️ 【第2層：国内・海外 レバレッジ＆過熱感モニター】
# =====================================================================
st.markdown("### ⚖️ 【第2層：国内・海外 レバレッジ＆過熱感モニター】")

col_l2_left, col_l2_right = st.columns(2)

# ■ 左列：信用比率 ＆ 信用評価損益率 (最新値 & 推移)
with col_l2_left:
    with card_container(border=True):
        sinyou_df = st.session_state.sinyou_df
        if not sinyou_df.empty:
            df_sin = sinyou_df[(sinyou_df["Date"] >= start_dt) & (sinyou_df["Date"] <= end_dt)].sort_values("Date").copy()
            latest_sin = df_sin.iloc[-1]
            prev_sin = df_sin.iloc[-2] if len(df_sin) > 1 else latest_sin

            loss_rate = latest_sin.get("MarginLossRate", np.nan)
            ratio_sin = latest_sin.get("MarginRatio", np.nan)
            if pd.isna(ratio_sin) and "Buy(M-yen)" in latest_sin and "Sell(M-yen)" in latest_sin:
                ratio_sin = latest_sin["Buy(M-yen)"] / latest_sin["Sell(M-yen)"] if latest_sin["Sell(M-yen)"] > 0 else 0.0

            loss_badge = f"<span style='color:{'#26a69a' if loss_rate >= -5 else '#ef5350' if loss_rate <= -10 else '#ffa726'}; margin-right:10px;'>評価損益率: <b>{loss_rate:+.2f}%</b></span>" if pd.notna(loss_rate) else ""
            ratio_badge = f"<span style='color:#42a5f5;'>信用倍率: <b>{ratio_sin:.2f}倍</b></span>" if pd.notna(ratio_sin) else ""

            render_card_header(
                title="個人信用評価損益率 ＆ 信用倍率",
                badge_html=f"{loss_badge} {ratio_badge}"
            )

            times_sin = _to_lwc_time(df_sin["Date"])
            loss_data = [{"time": t, "value": float(v)} for t, v in zip(times_sin, df_sin["MarginLossRate"]) if not pd.isna(v)]

            render_lwc_area_chart(
                series_definitions=[{
                    "type": "Line",
                    "data": loss_data,
                    "options": {
                        "color": "#ef5350",
                        "lineWidth": 2,
                        "title": "信用評価損益率 (%)",
                        "lastValueVisible": True,
                    }
                }],
                height=160,
                key="lwc_margin_loss_chart"
            )
        else:
            st.caption("信用データなし")

# ■ 右列：米 NAAIM Exposure Index
with col_l2_right:
    with card_container(border=True):
        naaim_df = st.session_state.naaim_df
        if not naaim_df.empty:
            df_naaim = naaim_df[(naaim_df["Date"] >= start_dt) & (naaim_df["Date"] <= end_dt)].sort_values("Date").copy()
            if not df_naaim.empty:
                latest_n = df_naaim.iloc[-1]
                prev_n = df_naaim.iloc[-2] if len(df_naaim) > 1 else latest_n
                val_n = latest_n["NAAIM"]
                diff_n = val_n - prev_n["NAAIM"]

                badge_tag = "🟢 強気・過熱(80超)" if val_n >= 80 else "🔴 総悲観・大底(40以下)" if val_n <= 40 else "⚪ 中立"
                b_color = "#d50000" if val_n >= 80 else "#1b5e20" if val_n <= 40 else "#37474f"
                status_html = build_status_badge(badge_tag, bg_color=b_color, text_color="#ffffff")

                render_card_header(
                    title="米 NAAIM Exposure Index（機関投資家エクスポージャー）",
                    mom_sub_text=f"前週差: {diff_n:+.2f}",
                    badge_html=f"<span style='color:#42a5f5; margin-right:8px;'>最新: <b>{val_n:.1f}</b></span> {status_html}"
                )

                times_n = _to_lwc_time(df_naaim["Date"])
                naaim_series = [{"time": t, "value": float(v)} for t, v in zip(times_n, df_naaim["NAAIM"]) if not pd.isna(v)]

                render_lwc_area_chart(
                    series_definitions=[{
                        "type": "Line",
                        "data": naaim_series,
                        "options": {
                            "color": "#42a5f5",
                            "lineWidth": 2,
                            "title": "NAAIM Index",
                            "lastValueVisible": True,
                        }
                    }],
                    height=160,
                    key="lwc_naaim_monitor"
                )
        else:
            st.caption("NAAIMデータなし")

# =====================================================================
# 🌐 【第3層：グローバル・マクロ監視グリッド (自動TTL10分)】
# =====================================================================
st.markdown("### 🌐 【第3層：グローバル・マクロ監視グリッド (10分自動更新)】")

if macro_data:
    macro_items = list(settings.MACRO_WATCHLIST.items())
    N_COLS = 3
    for i in range(0, len(macro_items), N_COLS):
        row_items = macro_items[i:i + N_COLS]
        cols = st.columns(N_COLS)
        for j, (label, meta) in enumerate(row_items):
            with cols[j]:
                with card_container(border=True):
                    df_m = macro_data.get(label, pd.DataFrame())
                    if not df_m.empty:
                        m_latest = df_m.iloc[-1]["close"]
                        m_prev = df_m.iloc[-2]["close"] if len(df_m) > 1 else m_latest
                        m_diff = m_latest - m_prev
                        m_pct = (m_diff / m_prev * 100.0) if m_prev != 0 else 0.0

                        is_area = (label == "信用スプレッド")
                        m_color = "#ab47bc" if is_area else "#ffa726" if meta["category"] == "コモディティ" else "#42a5f5"

                        badge_category = build_status_badge(meta["category"], bg_color="#263238", text_color="#90a4ae")
                        render_card_header(
                            title=f"{label} ({meta['name']})",
                            mom_value=m_pct,
                            mom_sub_text=f"{m_diff:+,.2f}",
                            badge_html=f"<span style='color:{m_color}; margin-right:6px;'><b>{m_latest:,.2f}</b></span> {badge_category}"
                        )
                        render_lwc_macro_mini(df_m, is_area=is_area, color=m_color, height=130, key=f"macro_chart_{label}")
                    else:
                        st.caption(f"{label}: データ取得待機中...")
else:
    st.info("マクロデータをロード中...")

# =====================================================================
# 🔍 【最下部：個別銘柄 信用残高検索 (IRBank)】
# =====================================================================
st.write("---")
with st.expander("🔍 個別銘柄 信用残高推移（IRBank検索・バックアップ）", expanded=False):
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
                            height=200,
                            key=f"lwc_margin_ind_{clean_code}"
                        )