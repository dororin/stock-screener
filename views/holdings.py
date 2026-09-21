# views/holdings.py

import streamlit as st
import pandas as pd
import numpy as np

from config import settings
from data_access.sheets_api import load_holdings_from_sheets
from data_access.local_db import get_price_data_cached
from core.calculator import compute_wvf_signals
from utils.plotting import render_lwc_candle_mini

st.title("💼 所持中株（ポートフォリオ）")
st.caption("楽天証券で保有中の現物ポジションを集約し、日足ミニチャート・口座別内訳・WVF状態を表示します。")


# =====================================================================
# 🛠️ 【フラグメント1】コントロール＆サマリーパネル
# =====================================================================
@st.fragment
def render_holdings_controls(df_holdings: pd.DataFrame):
    with st.container(border=True):
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
# 📌 【フラグメント2】個別保有銘柄カード（銘柄単位集約 ＆ 口座別バッジ）
# =====================================================================
@st.fragment
def render_holding_stock_card(ticker: str, stock_name: str, group_df: pd.DataFrame, db_df: pd.DataFrame):
    """
    1銘柄分の集約カード。
    - チャートは1つ（買値水平線はなし）
    - トータル保有数・加重平均取得単価
    - 口座別（NISA、特定など）の小分けバッジを整然と表示
    """
    total_qty = int(group_df["保有数量"].sum())
    total_eval = float(group_df["時価評価額"].sum())
    total_profit = float(group_df["評価損益額"].sum())
    total_cost = total_eval - total_profit
    avg_buy_price = (total_cost / total_qty) if total_qty > 0 else 0.0
    profit_pct = (total_profit / total_cost * 100.0) if total_cost > 0 else 0.0

    # 💡 口座別の小分けバッジ文字列を作成
    # 例: 「NISA: 100株 (買¥2,000)」「特定: 200株 (買¥2,600)」
    badges_html_list = []
    for _, sub in group_df.iterrows():
        acc = str(sub.get("口座区分", "特定")).strip()
        q = int(sub.get("保有数量", 0))
        b = float(sub.get("取得単価", 0.0))

        # 口座区分に応じたバッジ色分け（NISA=深緑、特定=深青）
        if "NISA" in acc.upper():
            b_bg = "#1b5e20"
            b_border = "#2e7d32"
            b_text = "#a5d6a7"
        else:
            b_bg = "#0d47a1"
            b_border = "#1565c0"
            b_text = "#90caf9"

        badge_item = (
            f"<span style='display:inline-block; background:{b_bg}; border:1px solid {b_border}; "
            f"border-radius:4px; padding:2px 8px; margin-right:6px; margin-bottom:4px; font-size:0.8rem; color:{b_text};'>"
            f"<b>{acc}</b>: {q:,}株 (買¥{b:,.1f})</span>"
        )
        badges_html_list.append(badge_item)

    badges_html = "".join(badges_html_list)

    # チャートデータの抽出と計算
    df_stock = pd.DataFrame()
    if not db_df.empty and "ticker" in db_df.columns:
        mask = db_df["ticker"] == ticker
        if mask.any():
            df_stock = db_df[mask].copy().sort_values("date").reset_index(drop=True)

    bb_dict = None
    wvf_badge_html = ""
    latest_close = group_df["現在値"].iloc[-1] if not group_df.empty else 0.0

    if not df_stock.empty and len(df_stock) >= 15:
        df_stock = compute_wvf_signals(df_stock)
        latest_row = df_stock.iloc[-1]
        latest_close = latest_row["close"]

        # 移動平均線（25SMA, 75SMA, 200SMA）
        df_stock["sma25"] = df_stock["close"].rolling(window=25, min_periods=1).mean()
        df_stock["sma75"] = df_stock["close"].rolling(window=75, min_periods=1).mean()
        df_stock["sma200"] = df_stock["close"].rolling(window=200, min_periods=1).mean()

        # ボリンジャーバンド
        bb_mid = df_stock["close"].rolling(window=20, min_periods=1).mean()
        bb_std = df_stock["close"].rolling(window=20, min_periods=1).std(ddof=0)
        df_stock["bb_p2"] = bb_mid + (2.0 * bb_std)
        df_stock["bb_m2"] = bb_mid - (2.0 * bb_std)
        df_stock["bb_p3"] = bb_mid + (3.0 * bb_std)
        df_stock["bb_m3"] = bb_mid - (3.0 * bb_std)

        # 直近180日にトリミング
        chart_display = df_stock.tail(180).copy().reset_index(drop=True)
        disp_indexed = chart_display.set_index("date")
        bb_dict = {
            "p2": disp_indexed["bb_p2"],
            "m2": disp_indexed["bb_m2"],
            "p3": disp_indexed["bb_p3"],
            "m3": disp_indexed["bb_m3"]
        }

        # WVF状態判定
        ext_price_val = latest_row.get("ext_price", np.nan)
        ext_str = f"¥{ext_price_val:,.1f}" if pd.notna(ext_price_val) else "-"
        tail_3 = df_stock.tail(3)
        has_recent_fuchsia = tail_3["is_fuchsia"].any() if "is_fuchsia" in tail_3.columns else False

        if latest_row.get("is_lime", False):
            lime_streak = int((df_stock["is_lime"].iloc[::-1].cumprod()).sum())
            wvf_badge_html = f"<span style='font-size:0.75rem; background:#00e676; color:#000; padding:1px 5px; border-radius:3px; font-weight:bold;'>🟢 パニック点灯中 ({lime_streak}日目)</span> <span style='font-size:0.75rem; color:#b0bec5;'>目安: {ext_str}</span>"
        elif has_recent_fuchsia:
            wvf_badge_html = f"<span style='font-size:0.75rem; background:#e91e63; color:#fff; padding:1px 5px; border-radius:3px; font-weight:bold;'>🌸 反発買いシグナル</span>"
        else:
            wvf_badge_html = f"<span style='font-size:0.75rem; color:#78909c;'>消灯目安: {ext_str}</span>"

    tv_url = f"https://jp.tradingview.com/chart/?symbol=TSE%3A{ticker}"

    with st.container(border=True):
        # 銘柄名ヘッダーと外部リンク
        head_col1, head_col2 = st.columns([3, 2])
        head_col1.markdown(f"#### [{ticker}]({tv_url}) {stock_name}")
        head_col2.markdown(
            f"<div style='text-align:right; margin-top:6px;'>{wvf_badge_html}</div>", 
            unsafe_allow_html=True
        )

        c_left, c_right = st.columns([1, 1])

        # ミニチャート（左側）
        with c_left:
            if not df_stock.empty and len(df_stock) >= 15:
                sma25_s = chart_display.set_index("date")["sma25"]
                sma75_s = chart_display.set_index("date")["sma75"]
                sma200_s = chart_display.set_index("date")["sma200"]
                render_lwc_candle_mini(
                    chart_display,
                    sma25=sma25_s,
                    sma75=sma75_s,
                    sma200=sma200_s,
                    key=f"holding_candle_{ticker}",
                    height=200,
                    is_jp=True,
                    wvf_df=chart_display,
                    bb_dict=bb_dict
                )
            else:
                st.caption("チャートデータ準備中（ローカルDBから取得できませんでした）")

        # 集約メトリクス＆口座別小分けバッジ（右側）
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

# 1. サマリーバーの描画
render_holdings_controls(holdings_raw_df)

if not holdings_raw_df.empty:
    # 2. ローカルParquet DBから全保有銘柄の日足データを一度だけロード
    holding_tickers = holdings_raw_df["銘柄コード"].unique().tolist()
    db_df = get_price_data_cached("1d", limit_days=365, is_jp=True)

    # 3. 銘柄コード単位にグループ化して集約
    grouped = holdings_raw_df.groupby("銘柄コード")
    unique_tickers = list(grouped.groups.keys())

    st.write("---")
    st.markdown(f"### 📋 保有銘柄一覧（{len(unique_tickers)} 銘柄）")

    # 2列グリッドで並べて表示
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
                        db_df=db_df
                    )