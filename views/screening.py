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


st.title("WVF + Trend Screener :blue[Pro]")
st.caption("TOPIX中大型500銘柄（Core30、Large70、Mid400）を一括判定します。")


# =====================================================================
# 🛠️ 【フラグメント1】操作コントロールパネル（完全独立）
# =====================================================================
@st.fragment
def render_screener_controls_panel():
    """履歴のロード、スキャンの開始、スプレッドシートへの保存を司る独立コントロールエリア。"""
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
                        # 履歴をロードしたら、表示エリアを全体更新するため1回だけリラン
                        st.rerun()
            else:
                st.caption("過去の履歴はありません")

        with col_ctrl2:
            st.markdown("**🚀 スクリーニング操作**")
            col_b1, col_b2 = st.columns(2)
            
            with col_b1:
                if st.button("🚀 判定開始 (TOPIX500)", use_container_width=True, type="primary"):
                    with st.spinner("データベースから対象データを抽出・判定中..."):
                        db_df = load_unified_db("1d", is_jp=True)
                        if not db_df.empty:
                            st.session_state.result_df = run_fast_screening(db_df)
                            st.session_state.performed_scan = True
                            st.session_state.last_id = None
                            st.success("スキャンが正常に完了しました。")
                            # 新規判定結果を描画領域に即時反映させるため1回リラン
                            st.rerun()
                        else:
                            st.error("データベース（price_jp_1d.parquet）が検出されませんでした。")
            
            with col_b2:
                if not st.session_state.result_df.empty:
                    if st.button("💾 Google Sheetsに保存", use_container_width=True):
                        with st.spinner("シートに永続化保存中..."):
                            if save_history(st.session_state.result_df):
                                st.success("結果を正常に保存しました！")
                                st.rerun()
                else:
                    st.button("💾 Google Sheetsに保存", use_container_width=True, disabled=True, help="判定結果が空のため保存できません。")


# =====================================================================
# 📌 【フラグメント2】個別銘柄カード（お気に入り⭐局所完結型）
# =====================================================================
@st.fragment
def render_screened_stock_card(index_num: int, unique_key: str):
    """
    スクリーニングされた1銘柄のカード。
    お気に入り⭐トグルを切り替えても、他のすべてのカードやコントロールエリアは完全に無視され、
    このカード領域内部だけが局所的に実行・同期されます。
    """
    rdf = st.session_state.result_df
    if index_num >= len(rdf):
        return

    r = rdf.iloc[index_num]

    with st.container(border=True):
        c1, c2 = st.columns([0.85, 0.15])
        c1.subheader(f"[{r['コード']}](https://jp.tradingview.com/chart/?symbol=TSE%3A{r['コード']}) {r['銘柄']}")
        
        # お気に入りトグル (セッションステート内のデータと安全に直結)
        is_fav = bool(rdf.at[index_num, 'お気に入り'])
        new_fav = c2.toggle("⭐", value=is_fav, key=f"f_toggle_{r['コード']}_{unique_key}", label_visibility="collapsed")
        
        # 局所的に状態を書き換え (再起動を一切伴わずオンメモリで確定)
        if new_fav != is_fav:
            st.session_state.result_df.at[index_num, 'お気に入り'] = new_fav

        i1, i2 = st.columns([1, 2])
        
        if r['チャート']:
            try:
                _raw = r['チャート']
                if isinstance(_raw, str) and len(_raw) > 10:
                    chart_df = pd.read_json(io.StringIO(_raw))
                    chart_df['date'] = pd.to_datetime(chart_df['date'])
                    chart_df = chart_df.sort_values('date').reset_index(drop=True)
                    _sma_fast = chart_df.set_index('date')['sma50'] if 'sma50' in chart_df.columns else None
                    _sma_slow = chart_df.set_index('date')['sma200'] if 'sma200' in chart_df.columns else None
                    _lwc_key = f"sc_mini_cand_{r['コード']}_{unique_key}"
                    with i1:
                        render_lwc_candle_mini(
                            chart_df,
                            sma_fast=_sma_fast,
                            sma_slow=_sma_slow,
                            key=_lwc_key,
                            height=180,
                        )
            except Exception as _e:
                i1.caption(f"⚠️ チャート描画エラー: {_e}")
                
        # メトリクスの表示
        m1 = i2.columns(3)
        m1[0].metric("現在値", f"¥{r['現在値']:,.1f}")
        m1[1].metric("消灯目安", f"¥{r['消灯目安(安値)']:,.1f}")
        m1[2].metric("200日乖離", f"{r['乖離率(%)']}%")
        
        m2 = i2.columns(4)
        m2[0].metric("WVF", r['WVF'])
        m2[1].metric("Upper", r['WVF Upper'])
        m2[2].metric("傾き", f"{r['200MA傾き率']:.5f}")
        m2[3].metric("点灯日数", f"{r['シグナル日']}")


# =====================================================================
# 呼び出し実行部 (全体再描画を挟まない並列配置)
# =====================================================================

# 1. 操作コントロールパネルフラグメントを実行
render_screener_controls_panel()

st.write("---")

# 2. スクリーニング結果表示
if not st.session_state.result_df.empty:
    rdf = st.session_state.result_df
    st.info(f"🔍 判定結果: {len(rdf)} 件検出されました。")
    
    # 2列グリッド形式でループ描画します
    for i in range(0, len(rdf), 2):
        cols = st.columns(2)
        for j in range(2):
            if i + j < len(rdf):
                with cols[j]:
                    # フラグメント関数の呼び出し
                    render_screened_stock_card(index_num=i+j, unique_key=f"grid_{i}_{j}")
else:
    if st.session_state.performed_scan:
        st.warning("⚠️ スキャンの結果、条件に一致する銘柄は見つかりませんでした。")
    else:
        st.info("💡 上記パネルの「🚀 判定開始 (TOPIX500)」ボタンを押してください。データベースから超高速判定を行います。")