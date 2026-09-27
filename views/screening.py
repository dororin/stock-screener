# views/screening.py

import io
import pandas as pd
import streamlit as st

from config import settings
from data_access.local_db import get_price_data_cached
from data_access.sheets_api import (
    save_history,
    get_history_list,
    load_history,
    load_events_from_sheets
)
from core.calculator import prepare_candle_indicators, extract_bb_dict
from core.event_collector import (
    get_earnings_countdown_badge,
    build_event_markers
)
from core.screener import run_fast_screening
from utils.ui_components import card_container, render_card_header, build_wvf_badge_html
from utils.plotting import render_lwc_candle_mini

def load_unified_db(interval: str, is_jp: bool = True) -> pd.DataFrame:
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
    with card_container(border=True):
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
# 📌 【フラグメント2】個別銘柄ミニチャートカード
# =====================================================================
@st.fragment
def render_screened_stock_card(index_num: int, unique_key: str, events_dict: dict = None):
    rdf = st.session_state.result_df
    if index_num >= len(rdf):
        return

    r = rdf.iloc[index_num]
    code = str(r['コード']).strip().upper()
    name = str(r['銘柄'])
    display_label = f"{code}　{name}"

    chart_df = pd.DataFrame()
    s_mom = 0.0
    bb_dict = None
    wvf_summary = {}

    if r.get('チャート'):
        try:
            _raw = r['チャート']
            if isinstance(_raw, str) and len(_raw) > 10:
                raw_df = pd.read_json(io.StringIO(_raw))
                chart_df, s_mom, wvf_summary = prepare_candle_indicators(raw_df)
                # 💡 スライス済み（60本分）のchart_dfからbb_dictを抽出して期間を完全に一致させる
                bb_dict = extract_bb_dict(chart_df)
        except Exception:
            pass

    ext_val = r.get('消灯目安') if '消灯目安' in r else r.get('消灯目安(安値)', 0.0)
    streak = r.get('点灯日数', 1)
    wvf_summary.update({
        "is_lime": True,
        "lime_streak": streak,
        "ext_price": ext_val
    })
    wvf_badge_html = build_wvf_badge_html(wvf_summary)

    ev_info = (events_dict or {}).get(code, {})
    earnings_badge_html = get_earnings_countdown_badge(ev_info.get("next_earnings", ""))
    event_markers = build_event_markers(ev_info.get("prev_earnings", ""), ev_info.get("prev_dividend", ""))

    badge_items = [b for b in [wvf_badge_html, earnings_badge_html] if b]
    badges_combined = "&nbsp;&nbsp;".join(badge_items)

    is_fav = bool(rdf.at[index_num, 'お気に入り'])

    def toggle_fav():
        new_fav = st.toggle("⭐", value=is_fav, key=f"f_toggle_{code}_{unique_key}", label_visibility="collapsed")
        if new_fav != is_fav:
            st.session_state.result_df.at[index_num, 'お気に入り'] = new_fav

    with card_container(border=True):
        render_card_header(
            title=display_label,
            code=code,
            is_jp=True,
            mom_value=s_mom,
            badge_html=badges_combined,
            right_action_fn=toggle_fav,
            col_ratio=[3.3, 1.1, 0.6]
        )

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
                bb_dict=bb_dict,
                event_markers=event_markers
            )
        else:
            st.caption("データなし")

# =====================================================================
# 🚀 画面描画制御部
# =====================================================================
render_screener_controls_panel()

events_calendar_map = load_events_from_sheets()

if st.session_state.screening_logs:
    has_errors = any("❌" in log_line for log_line in st.session_state.screening_logs)
    if has_errors:
        with st.expander("⚠️ 判定エラーログ", expanded=True):
            st.code("\n".join(st.session_state.screening_logs), language="text")
            if st.button("🗑️ ログをクリア", key="btn_clear_logs", use_container_width=True):
                st.session_state.screening_logs = []
                st.rerun()

st.write("---")

if not st.session_state.result_df.empty:
    rdf = st.session_state.result_df
    st.info(f"🔍 判定結果: **{len(rdf)} 件** 検出されました。")
    
    N_COLS = 3
    for i in range(0, len(rdf), N_COLS):
        cols = st.columns(N_COLS)
        for j in range(N_COLS):
            if i + j < len(rdf):
                with cols[j]:
                    render_screened_stock_card(
                        index_num=i + j,
                        unique_key=f"g_{i}_{j}",
                        events_dict=events_calendar_map
                    )
else:
    if st.session_state.performed_scan:
        st.warning("⚠️ スキャンの結果、条件に一致する銘柄は見つかりませんでした。")
    else:
        st.info("💡 上記パネルの「🚀 判定開始 (TOPIX500)」ボタンを押してください。")