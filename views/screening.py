# views/screening.py

import io
import pandas as pd
import streamlit as st

from config import settings
from data_access.local_db import get_price_data_cached
from data_access.sheets_api import (
    save_history,
    get_history_list,
    load_history
)
from core.screener import run_fast_screening
from utils.plotting import render_lwc_candle_mini

def load_unified_db(interval: str, is_jp: bool = True) -> pd.DataFrame:
    """レイヤー1共有キャッシュ経由で1dデータをロードします。"""
    try:
        return get_price_data_cached(interval, is_jp=is_jp)
    except FileNotFoundError as e:
        st.warning(str(e))
        return pd.DataFrame()


# =====================================================================
# 📈 セッション状態の初期化
# =====================================================================
if 'result_df' not in st.session_state:
    st.session_state.result_df = pd.DataFrame()
if 'performed_scan' not in st.session_state:
    st.session_state.performed_scan = False
if 'screening_logs' not in st.session_state:
    st.session_state.screening_logs = []


st.title("WVF + Trend Screener :blue[Pro]")
st.caption("TOPIX中大型500銘柄（Core30、Large70、Mid400）を一括判定します。")


# =====================================================================
# 🛠️ 【フラグメント1】操作コントロールパネル
# =====================================================================
@st.fragment
def render_screener_controls_panel():
    with st.container(border=True):
        col_ctrl1, col_ctrl2 = st.columns([1.5, 2.5])
        
        with col_ctrl1:
            st.markdown("**📂 過去履歴の表示**")
            ids = get_history_list()
            if ids:
                sid = st.selectbox("過去の結果履歴", ["── 選択してください ──"] + ids, key="h_sel_main", label_visibility="collapsed")
                if sid != "── 選択してください ──" and st.session_state.get('last_id') != sid:
                    with st.spinner("履歴をロード中..."):
                        st.session_state.result_df = load_history(sid)
                        st.session_state.performed_scan = True
                        st.session_state.last_id = sid
                        st.rerun()
            else:
                st.caption("過去の履歴はありません")

        with col_ctrl2:
            header_col, refresh_col = st.columns([2, 1])
            with header_col:
                st.markdown("**🚀 スクリーニング操作**")
            with refresh_col:
                if st.button("🔄 キャッシュ更新", help="メモリ上のキャッシュを消去して最新データを強制再取得します", use_container_width=True):
                    from data_access.local_db import clear_local_parquet_cache
                    clear_local_parquet_cache(interval="1d", is_jp=True)
                    st.cache_data.clear()
                    st.success("キャッシュをクリアしました。")
                    st.rerun()

            col_b1, col_b2 = st.columns(2)
            with col_b1:
                if st.button("🚀 判定開始 (TOPIX500)", use_container_width=True, type="primary"):
                    with st.spinner("データベースから抽出・判定中..."):
                        db_df = load_unified_db("1d", is_jp=True)
                        if not db_df.empty:
                            temp_logs = []
                            st.session_state.result_df = run_fast_screening(db_df, log_accumulator=temp_logs)
                            st.session_state.screening_logs = temp_logs
                            st.session_state.performed_scan = True
                            st.session_state.last_id = None
                            st.rerun()
                        else:
                            st.error("データベース（price_jp_1d.parquet）が検出されませんでした。")
            
            with col_b2:
                if not st.session_state.result_df.empty:
                    if st.button("💾 Google Sheetsに保存", use_container_width=True):
                        with st.spinner("シートに保存中..."):
                            if save_history(st.session_state.result_df):
                                st.success("結果を正常に保存しました！")
                                st.rerun()
                else:
                    st.button("💾 Google Sheetsに保存", use_container_width=True, disabled=True, help="判定結果が空のため保存できません。")


# =====================================================================
# 📌 【フラグメント2】個別銘柄ミニチャートカード（セクターローテーション準拠）
# =====================================================================
@st.fragment
def render_screened_stock_card(index_num: int, unique_key: str):
    rdf = st.session_state.result_df
    if index_num >= len(rdf):
        return

    r = rdf.iloc[index_num]
    code = str(r['コード'])
    name = str(r['銘柄'])
    tv_url = f"https://jp.tradingview.com/chart/?symbol=TSE%3A{code}"
    display_label = f"{code}　{name}"

    chart_df = pd.DataFrame()
    s_mom = 0.0
    bb_dict = None

    if r.get('チャート'):
        try:
            _raw = r['チャート']
            if isinstance(_raw, str) and len(_raw) > 10:
                chart_df = pd.read_json(io.StringIO(_raw))
                chart_df['date'] = pd.to_datetime(chart_df['date'])
                chart_df = chart_df.sort_values('date').reset_index(drop=True)

                chart_df['sma25'] = chart_df['close'].rolling(window=25, min_periods=1).mean()
                chart_df['sma75'] = chart_df['close'].rolling(window=75, min_periods=1).mean()
                if 'sma200' not in chart_df.columns:
                    chart_df['sma200'] = chart_df['close'].rolling(window=200, min_periods=1).mean()

                bb_mid = chart_df['close'].rolling(window=20, min_periods=1).mean()
                bb_std = chart_df['close'].rolling(window=20, min_periods=1).std(ddof=0)
                chart_df['bb_p2'] = bb_mid + (2.0 * bb_std)
                chart_df['bb_m2'] = bb_mid - (2.0 * bb_std)
                chart_df['bb_p3'] = bb_mid + (3.0 * bb_std)
                chart_df['bb_m3'] = bb_mid - (3.0 * bb_std)

                disp_indexed = chart_df.set_index('date')
                bb_dict = {
                    "p2": disp_indexed['bb_p2'],
                    "m2": disp_indexed['bb_m2'],
                    "p3": disp_indexed['bb_p3'],
                    "m3": disp_indexed['bb_m3']
                }

                recent_closes = chart_df["close"].tail(min(5, len(chart_df))).values
                if len(recent_closes) >= 2 and recent_closes[0] > 0:
                    s_mom = float((recent_closes[-1] / recent_closes[0] - 1) * 100)
        except Exception:
            pass

    # WVFバッジの構成
    ext_val = r.get('消灯目安') if '消灯目安' in r else r.get('消灯目安(安値)', 0.0)
    try:
        ext_val = float(ext_val)
    except (ValueError, TypeError):
        ext_val = 0.0
    ext_str = f"¥{ext_val:,.1f}" if ext_val > 0 else "-"
    streak = r.get('点灯日数', 1)

    wvf_badge_html = (
        f"<span style='font-size:0.75rem; background:#00e676; color:#000; padding:2px 6px; border-radius:3px; font-weight:bold;'>"
        f"🟢 点灯中({streak}日目)</span> "
        f"<span style='font-size:0.75rem; color:#b0bec5;'>翌日消灯目安: {ext_str}</span>"
    )

    s_badge = "🟢" if s_mom >= 3.0 else "🔴" if s_mom <= -3.0 else "⚪"
    s_color = "#26a69a" if s_mom >= 3.0 else "#ef5350" if s_mom <= -3.0 else "#9e9e9e"
    is_fav = bool(rdf.at[index_num, 'お気に入り'])

    with st.container(border=True):
        # 1行目: コード+銘柄名(TradingViewリンク) ｜ 5日騰落率 ｜ ⭐トグル
        hc1, hc2, hc3 = st.columns([3.3, 1.1, 0.6])
        hc1.markdown(
            f"<div style='font-size:0.86rem; font-weight:600; color:{s_color}; line-height:1.8; "
            f"white-space:nowrap; overflow:hidden; text-overflow:ellipsis;' title='TradingViewで開く: {display_label}'>"
            f"{s_badge} <a href='{tv_url}' target='_blank' rel='noopener noreferrer' "
            f"style='color:{s_color}; text-decoration:none; border-bottom:1px dotted {s_color};'>"
            f"{display_label}</a></div>",
            unsafe_allow_html=True
        )
        hc2.markdown(
            f"<div style='font-size:0.83rem; text-align:right; color:{s_color}; font-weight:bold; line-height:1.8;'>"
            f"{s_mom:+.2f}%</div>",
            unsafe_allow_html=True
        )
        new_fav = hc3.toggle("⭐", value=is_fav, key=f"f_toggle_{code}_{unique_key}", label_visibility="collapsed")
        if new_fav != is_fav:
            st.session_state.result_df.at[index_num, 'お気に入り'] = new_fav

        # 2行目: WVF点灯日数 & 翌日消灯目安
        st.markdown(
            f"<div style='margin-top:2px; margin-bottom:4px; height:20px; line-height:20px; overflow:hidden;'>"
            f"{wvf_badge_html}</div>",
            unsafe_allow_html=True
        )

        # 3行目: ローソク足ミニチャート (高さ170px)
        if not chart_df.empty and len(chart_df) >= 2:
            disp_indexed = chart_df.set_index("date")
            render_lwc_candle_mini(
                chart_df,
                sma25=disp_indexed["sma25"],
                sma75=disp_indexed["sma75"],
                sma200=disp_indexed["sma200"],
                key=f"sc_cand_{code}_{unique_key}",
                height=170,
                is_jp=True,
                wvf_df=chart_df,
                bb_dict=bb_dict
            )
        else:
            st.caption("データなし")


# =====================================================================
# 🚀 画面描画制御部
# =====================================================================

render_screener_controls_panel()

# ─── 実行ログ（エラーや例外が存在する場合のみ表示） ───
if st.session_state.screening_logs:
    has_errors = any("❌" in log_line for log_line in st.session_state.screening_logs)
    if has_errors:
        with st.expander("⚠️ 判定エラーログ", expanded=True):
            st.code("\n".join(st.session_state.screening_logs), language="text")
            if st.button("🗑️ ログをクリア", key="btn_clear_logs", use_container_width=True):
                st.session_state.screening_logs = []
                st.rerun()

st.write("---")

# ─── 判定結果一覧（3列グリッド表示） ───
if not st.session_state.result_df.empty:
    rdf = st.session_state.result_df
    st.info(f"🔍 判定結果: **{len(rdf)} 件** 検出されました。")
    
    N_COLS = 3
    for i in range(0, len(rdf), N_COLS):
        cols = st.columns(N_COLS)
        for j in range(N_COLS):
            if i + j < len(rdf):
                with cols[j]:
                    render_screened_stock_card(index_num=i + j, unique_key=f"g_{i}_{j}")
else:
    if st.session_state.performed_scan:
        st.warning("⚠️ スキャンの結果、条件に一致する銘柄は見つかりませんでした。")
    else:
        st.info("💡 上記パネルの「🚀 判定開始 (TOPIX500)」ボタンを押してください。")