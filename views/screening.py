# views/screening.py
import io
import pandas as pd
import streamlit as st

from config import settings
from data_access.local_db import load_price_db
from data_access.sheets_api import (
    save_history,
    get_history_list,
    load_history
)
from core.screener import run_fast_screening
from utils.plotting import render_lwc_candle_mini

@st.cache_data(ttl=300)
def load_unified_db(interval: str, is_jp: bool = True) -> pd.DataFrame:
    """Parquetデータベースから1dデータをキャッシュ付きでロードします。"""
    try:
        return load_price_db(interval, is_jp=is_jp)
    except FileNotFoundError as e:
        st.warning(str(e))
        return pd.DataFrame()

# =====================================================================
# 🔍 画面描画制御部
# =====================================================================

# セッション状態の初期化
if 'result_df' not in st.session_state:
    st.session_state.result_df = pd.DataFrame()
if 'performed_scan' not in st.session_state:
    st.session_state.performed_scan = False

st.title("WVF + Trend Screener :blue[Pro]")

with st.sidebar:
    st.subheader("スクリーニング操作")
    
    # スプレッドシートから過去の実行履歴を検索・ロード
    with st.expander("📂 履歴表示", expanded=True):
        ids = get_history_list()
        if ids:
            sid = st.selectbox("過去の結果", ["-- 選択 --"] + ids, key="h_sel")
            if sid != "-- 選択 --" and st.session_state.get('last_id') != sid:
                st.session_state.result_df = load_history(sid)
                st.session_state.last_id = sid
        else:
            st.caption("過去の履歴はありません")
        
    st.markdown("**対象: TOPIX中大型500銘柄**")
    st.caption("※日本取引所グループ（JPX）公認のCore30、Large70、Mid400銘柄を一括スクリーニングします。")
    
    # 高速WVFスクリーニング開始トリガー
    if st.button("🚀 スクリーニング開始", use_container_width=True):
        with st.spinner("データベースからTOPIX500データを抽出中..."):
            db_df = load_unified_db("1d", is_jp=True)
            
            if not db_df.empty:
                st.session_state.result_df = run_fast_screening(db_df)
                st.session_state.performed_scan = True
                st.session_state.last_id = None
            else:
                st.error("スクリーニング対象となるデータベース（price_jp_1d.parquet）が検出されませんでした。")
                
    # 判定結果のSheetsへの永続化トリガー
    if not st.session_state.result_df.empty:
        if st.button("💾 結果をGoogle Sheetsに保存", use_container_width=True):
            if save_history(st.session_state.result_df):
                st.success("結果を保存しました！")
                st.rerun()

# スクリーニング銘柄カード群の描画
if not st.session_state.result_df.empty:
    rdf = st.session_state.result_df
    # 2列グリッド形式でループ描画します
    for i in range(0, len(rdf), 2):
        cols = st.columns(2)
        for j in range(2):
            if i + j < len(rdf):
                r = rdf.iloc[i + j]
                with cols[j]:
                    with st.container(border=True):
                        c1, c2 = st.columns([0.85, 0.15])
                        c1.subheader(f"[{r['コード']}](https://jp.tradingview.com/chart/?symbol=TSE%3A{r['コード']}) {r['銘柄']}")
                        
                        # お気に入りトグルスイッチ
                        if c2.toggle("⭐", value=r['お気に入り'], key=f"f_{r['コード']}_{i+j}", label_visibility="collapsed") != r['お気に入り']:
                            st.session_state.result_df.at[i + j, 'お気に入り'] = not r['お気に入り']
                            
                        i1, i2 = st.columns([1, 2])
                        
                        # 銘柄に添付された簡易ローソク足情報をLWCで復元・描画
                        if r['チャート']:
                            try:
                                _raw = r['チャート']
                                if isinstance(_raw, str) and len(_raw) > 10:
                                    chart_df = pd.read_json(io.StringIO(_raw))
                                    chart_df['date'] = pd.to_datetime(chart_df['date'])
                                    chart_df = chart_df.sort_values('date').reset_index(drop=True)
                                    _sma_fast = chart_df.set_index('date')['sma50'] if 'sma50' in chart_df.columns else None
                                    _sma_slow = chart_df.set_index('date')['sma200'] if 'sma200' in chart_df.columns else None
                                    _lwc_key = f"sc_{r['コード']}_{i}_{j}"
                                    with i1:
                                        render_lwc_candle_mini(
                                            chart_df,
                                            sma_fast=_sma_fast,
                                            sma_slow=_sma_slow,
                                            key=_lwc_key,
                                            height=180,
                                        )
                            except Exception as _e:
                                i1.caption(f"⚠️ {_e}")
                                
                        # 指標メトリクスの展開表示
                        m1 = i2.columns(3)
                        m1[0].metric("現在値", f"¥{r['現在値']:,.1f}")
                        m1[1].metric("消灯目安", f"¥{r['消灯目安(安値)']:,.1f}")
                        m1[2].metric("200日乖離", f"{r['乖離率(%)']}%")
                        
                        m2 = i2.columns(4)
                        m2[0].metric("WVF", r['WVF'])
                        m2[1].metric("Upper", r['WVF Upper'])
                        m2[2].metric("傾き", f"{r['200MA傾き率']:.5f}")
                        m2[3].metric("日", r['シグナル日'])
else:
    if st.session_state.performed_scan:
        st.warning("条件に一致する銘柄は見つかりませんでした。")
    else:
        st.info("左サイドバーの「🚀 スクリーニング開始」ボタンを押してください。データベースから超高速判定を行います。")