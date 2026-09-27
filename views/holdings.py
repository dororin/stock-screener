# views/holdings.py

import streamlit as st
import pandas as pd
import numpy as np

from config import settings
from data_access.sheets_api import load_holdings_from_sheets, load_events_from_sheets
from data_access.local_db import get_price_data_cached
from core.calculator import prepare_candle_indicators
from core.event_collector import (
    get_earnings_countdown_badge,
    build_event_markers
)
from utils.ui_components import (
    card_container, 
    render_card_header, 
    build_status_badge, 
    build_wvf_badge_html
)
from utils.plotting import render_lwc_candle_mini

st.title("💼 所持中株（ポートフォリオ）")
st.caption("楽天証券で保有中の現物ポジションを集約し、日足ミニチャート・口座別内訳・WVF状態・決算カウントダウンを表示します。")

# =====================================================================
# 🛠️ 【フラグメント1】コントロール＆サマリーパネル
# =====================================================================
@st.fragment
def render_holdings_controls(df_holdings: pd.DataFrame):
    with card_container(border=True):
        col_c1, col_c2 = st.columns([3, 1])
        with col_c1:
            if not df_holdings.empty:
                total_eval = df_holdings["時価評価額"].sum()
                total_profit = df_holdings["評価損益額"].sum()
                total_cost = total_eval - total_profit
                total_rate = (total_profit / total_cost * 100.0) if total_cost > 0 else 0.0

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("総保有銘柄数", f"{df_holdings['銘柄コード'].nunique()} 銘柄")
                m2.metric("総時価評価額", f"¥{total_eval:,.0f}")
                sign = "+" if total_profit >= 0 else ""
                m3.metric("トータル評価損益", f"¥{total_profit:+,.0f}", f"{sign}{total_rate:.2f}%")
                
                updated_at = df_holdings["更新日時"].dropna().iloc[0] if "更新日時" in df_holdings.columns and not df_holdings["更新日時"].empty else "-"
                m4.metric("最終同期日時", str(updated_at)[:16])
            else:
                st.info("保有証券データが登録されていません。ローカル環境で `python sync_holdings_jp.py` を実行して同期してください。")

        with col_c2:
            st.markdown("**🔄 画面更新**")
            if st.button("🔄 キャッシュ再読込", help="スプレッドシートおよびローカルDBを再読み込みします", use_container_width=True):
                st.cache_data.clear()
                st.rerun(scope="app")

# =====================================================================
# 📌 【フラグメント2】個別保有銘柄カード
# =====================================================================
@st.fragment
def render_holding_stock_card(ticker: str, stock_name: str, group_df: pd.DataFrame, db_df: pd.DataFrame, events_dict: dict = None):
    ticker_clean = str(ticker).strip().upper()
    total_qty = int(group_df["保有数量"].sum())
    total_eval = float(group_df["時価評価額"].sum())
    total_profit = float(group_df["評価損益額"].sum())
    total_cost = total_eval - total_profit
    avg_buy_price = (total_cost / total_qty) if total_qty > 0 else 0.0
    profit_pct = (total_profit / total_cost * 100.0) if total_cost > 0 else 0.0

    # 口座区分バッジ群の生成
    badges_html_list = []
    for _, sub in group_df.iterrows():
        acc = str(sub.get("口座区分", "特定")).strip()
        q = int(sub.get("保有数量", 0))
        b = float(sub.get("取得単価", 0.0))

        if "NISA" in acc.upper():
            badge_item = build_status_badge(f"<b>{acc}</b>: {q:,}株 (買¥{b:,.1f})", bg_color="#1b5e20", text_color="#a5d6a7", border_color="#2e7d32")
        else:
            badge_item = build_status_badge(f"<b>{acc}</b>: {q:,}株 (買¥{b:,.1f})", bg_color="#0d47a1", text_color="#90caf9", border_color="#1565c0")
        badges_html_list.append(badge_item)

    badges_html = "&nbsp;".join(badges_html_list)

    # チャートデータの抽出と計算
    df_stock = pd.DataFrame()
    if not db_df.empty and "ticker" in db_df.columns:
        mask = db_df["ticker"] == ticker_clean
        if mask.any():
            df_stock = db_df[mask].copy().sort_values("date").reset_index(drop=True)

    df_calc, bb_dict, s_mom, wvf_summary = prepare_candle_indicators(df_stock)
    latest_close = group_df["現在値"].iloc[-1] if not group_df.empty else 0.0

    if not df_calc.empty:
        latest_close = df_calc.iloc[-1]["close"]
        chart_display = df_calc.tail(180).copy().reset_index(drop=True)
    else:
        chart_display = pd.DataFrame()

    wvf_badge_html = build_wvf_badge_html(wvf_summary)

    ev_info = (events_dict or {}).get(ticker_clean, {})
    earnings_badge_html = get_earnings_countdown_badge(ev_info.get("next_earnings", ""))
    event_markers = build_event_markers(ev_info.get("prev_earnings", ""), ev_info.get("prev_dividend", ""))

    badge_items = [b for b in [wvf_badge_html, earnings_badge_html] if b]
    combined_badges = "&nbsp;&nbsp;".join(badge_items)

    display_label = f"{ticker_clean} {stock_name}"

    with card_container(border=True):
        render_card_header(
            title=display_label,
            code=ticker_clean,
            is_jp=True,
            mom_value=profit_pct,
            mom_sub_text=f"損益: ¥{total_profit:+,.0f}",
            badge_html=combined_badges,
            col_ratio=[3.5, 1.5]
        )

        c_left, c_right = st.columns([1, 1])

        # ミニチャート（左側）
        with c_left:
            if not chart_display.empty and len(chart_display) >= 15:
                sma25_s = chart_display.set_index("date")["sma25"]
                sma75_s = chart_display.set_index("date")["sma75"]
                sma200_s = chart_display.set_index("date")["sma200"]
                render_lwc_candle_mini(
                    chart_display,
                    sma25=sma25_s,
                    sma75=sma75_s,
                    sma200=sma200_s,
                    key=f"holding_candle_{ticker_clean}",
                    height=200,
                    is_jp=True,
                    wvf_df=chart_display,
                    bb_dict=bb_dict,
                    event_markers=event_markers
                )
            else:
                st.caption("チャートデータ準備中（ローカルDBから取得できませんでした）")

        # 集約メトリクス＆口座別内訳（右側）
        with c_right:
            m1, m2 = st.columns(2)
            m1.metric("現在値", f"¥{latest_close:,.1f}")
            sign_str = "+" if total_profit >= 0 else ""
            m2.metric("評価損益", f"¥{total_profit:+,.0f}", f"{sign_str}{profit_pct:.2f}%")

            m3, m4 = st.columns(2)
            m3.metric("トータル保有数", f"{total_qty:,} 株")
            m4.metric("加重平均買値", f"¥{avg_buy_price:,.1f}")

            st.markdown("<div style='margin-top:6px;'><b>📌 口座別内訳:</b></div>", unsafe_allow_html=True)
            st.markdown(f"<div style='margin-top:4px;'>{badges_html}</div>", unsafe_allow_html=True)

# =====================================================================
# 🚀 画面描画制御部
# =====================================================================
holdings_raw_df = load_holdings_from_sheets()

render_holdings_controls(holdings_raw_df)

if not holdings_raw_df.empty:
    db_df = get_price_data_cached("1d", limit_days=365, is_jp=True)
    events_calendar_map = load_events_from_sheets()

    grouped = holdings_raw_df.groupby("銘柄コード")
    unique_tickers = list(grouped.groups.keys())

    st.write("---")
    st.markdown(f"### 📋 保有銘柄一覧（{len(unique_tickers)} 銘柄）")

    for i in range(0, len(unique_tickers), 2):
        cols = st.columns(2)
        for j in range(2):
            if i + j < len(unique_tickers):
                t = unique_tickers[i + j]
                g_df = grouped.get_group(t)
                s_name = g_df["銘柄名"].dropna().iloc[0] if not g_df["銘柄名"].empty else ""
                with cols[j]:
                    render_holding_stock_card(
                        ticker=t,
                        stock_name=s_name,
                        group_df=g_df,
                        db_df=db_df,
                        events_dict=events_calendar_map
                    )