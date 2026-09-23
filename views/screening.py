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
    st.session_state.screening_logs = []  # 永続詳細ログ格納用


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
                        st.rerun()
            else:
                st.caption("過去の履歴はありません")

        with col_ctrl2:
            # 操作ヘッダーと最新データ強制リフレッシュボタン
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
                    with st.spinner("データベースから対象データを抽出・判定中..."):
                        db_df = load_unified_db("1d", is_jp=True)
                        if not db_df.empty:
                            temp_logs = []
                            st.session_state.result_df = run_fast_screening(db_df, log_accumulator=temp_logs)
                            st.session_state.screening_logs = temp_logs
                            st.session_state.performed_scan = True
                            st.session_state.last_id = None
                            st.success("スキャンが正常に完了しました。")
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
        
        # お気に入りトグル
        is_fav = bool(rdf.at[index_num, 'お気に入り'])
        new_fav = c2.toggle("⭐", value=is_fav, key=f"f_toggle_{r['コード']}_{unique_key}", label_visibility="collapsed")
        
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

                    # 💡 3本の移動平均線（25SMA赤, 75SMAオレンジ, 200SMA紫）
                    chart_df['sma25'] = chart_df['close'].rolling(window=25, min_periods=1).mean()
                    chart_df['sma75'] = chart_df['close'].rolling(window=75, min_periods=1).mean()
                    if 'sma200' not in chart_df.columns:
                        chart_df['sma200'] = chart_df['close'].rolling(window=200, min_periods=1).mean()

                    # 💡 ボリンジャーバンド計算 (20 SMA, ±2σ, ±3σ) をchart_dfに確実に付与
                    bb_mid = chart_df['close'].rolling(window=20, min_periods=1).mean()
                    bb_std = chart_df['close'].rolling(window=20, min_periods=1).std(ddof=0)
                    chart_df['bb_p2'] = bb_mid + (2.0 * bb_std)
                    chart_df['bb_m2'] = bb_mid - (2.0 * bb_std)
                    chart_df['bb_p3'] = bb_mid + (3.0 * bb_std)
                    chart_df['bb_m3'] = bb_mid - (3.0 * bb_std)

                    # 日付インデックスで抽出
                    disp_indexed = chart_df.set_index('date')
                    bb_dict = {
                        "p2": disp_indexed['bb_p2'],
                        "m2": disp_indexed['bb_m2'],
                        "p3": disp_indexed['bb_p3'],
                        "m3": disp_indexed['bb_m3']
                    }

                    _sma25 = disp_indexed['sma25']
                    _sma75 = disp_indexed['sma75']
                    _sma200 = disp_indexed['sma200']

                    _lwc_key = f"sc_mini_cand_{r['コード']}_{unique_key}"
                    with i1:
                        render_lwc_candle_mini(
                            chart_df,
                            sma25=_sma25,
                            sma75=_sma75,
                            sma200=_sma200,
                            key=_lwc_key,
                            height=180,
                            bb_dict=bb_dict
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

# ─── 🚀 実行後に勝手に消えない永続的な詳細ログコンソール ───
if st.session_state.screening_logs:
    st.write(" ")
    with st.expander("📋 WVF+Trend スクリーニング詳細実行ログ・コンソール", expanded=True):
        st.markdown(
            "システム内部で計算された全500銘柄のWVF値、アッパーバンド、トレンド判定の途中結果と、"
            "合致・スキップされた詳細な理由がすべて記録されています。特定のコード（例: `4631`）でブラウザ検索（Ctrl + F）してデバッグできます。"
        )
        st.code("\n".join(st.session_state.screening_logs), language="text")
        
        if st.button("🗑️ ログ表示履歴をクリア", key="btn_clear_screening_logs_history", use_container_width=True):
            st.session_state.screening_logs = []
            st.rerun()

st.write("---")

# 2. スクリーニング結果表示
if not st.session_state.result_df.empty:
    rdf = st.session_state.result_df
    st.info(f"🔍 判定結果: {len(rdf)} 件検出されました。")
    
    for i in range(0, len(rdf), 2):
        cols = st.columns(2)
        for j in range(2):
            if i + j < len(rdf):
                with cols[j]:
                    render_screened_stock_card(index_num=i+j, unique_key=f"grid_{i}_{j}")
else:
    if st.session_state.performed_scan:
        st.warning("⚠️ スキャンの結果、条件に一致する銘柄は見つかりませんでした。上記の詳細実行ログを確認してください。")
    else:
        st.info("💡 上記パネルの「🚀 判定開始 (TOPIX500)」ボタンを押してください。データベースから超高速判定を行います。")