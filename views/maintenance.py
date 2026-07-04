# views/maintenance.py
import os
import time
from datetime import datetime
import pandas as pd
import streamlit as st
import re

from config import settings
from data_access.local_db import load_price_db
from data_access.sheets_api import (
    load_extra_tickers_from_sheets,
    save_extra_tickers_to_sheets,
    load_repair_log_from_sheets,
    save_repair_log_to_sheets,
    upload_sync_log_to_drive
)
from core.collector import (
    sync_extra_tickers_to_local,
    get_all_collection_tickers,
    sanitize_ticker
)
from core.screener import get_jpx_full_list
from core.database_service import (
    analyze_db_update_needs,
    update_price_database,
    repair_single_ticker_all_timeframes,
    scan_all_anomalies,
    full_rebuild_all_database,
    run_database_health_scan,
    apply_forced_scale_patch_to_all_timeframes,
    apply_all_saved_patches,
    repair_stop_allocation_bars_full,
    rebuild_active_from_raw
)

# ── 日足の最新更新日の簡易取得 ──
def get_db_last_update(interval: str, is_jp: bool = True) -> str:
    try:
        df = load_price_db(interval, is_jp=is_jp, is_raw=False)
        if df.empty:
            return "不明"
        last = pd.to_datetime(df["date"]).max()
        return last.strftime("%Y-%m-%d")
    except Exception:
        return "不明"

# ── 追加ETF（extra_tickers）管理コンポーネント ──
def render_etf_manager():
    df = load_extra_tickers_from_sheets()
    count = len(df) if not df.empty else 0

    with st.expander(f"📁 収集対象ETF設定（{count}件）", expanded=False):
        st.caption("データベースに収集する追加ティッカーを管理します")

        q = st.text_input(
            "銘柄コード・名前で検索",
            placeholder="例: 1306 / TOPIX / 半導体",
            key="etf_manager_search_input"
        ).strip()

        if len(q) >= 2:
            jpx_df = get_jpx_full_list()
            if not jpx_df.empty:
                mask = (
                    jpx_df["name"].str.contains(q, na=False, case=False) |
                    jpx_df["symbol"].str.contains(q, na=False)
                )
                found = jpx_df[mask].head(8)
                if not found.empty:
                    st.markdown("**検索結果**")
                    for _, row in found.iterrows():
                        code_str = str(row["symbol"])
                        name_str = str(row["name"])
                        already = code_str in df["code"].values if not df.empty else False
                        
                        col_add_left, col_add_right = st.columns([4, 1])
                        col_add_left.write(f"{code_str}　{name_str}")
                        if already:
                            col_add_right.button("✅ 登録済", key=f"etf_add_btn_{code_str}", disabled=True, width='stretch')
                        else:
                            if col_add_right.button("追加", key=f"etf_add_btn_{code_str}", width='stretch'):
                                new_row = pd.DataFrame([{"code": code_str, "name": name_str, "memo": ""}])
                                df = pd.concat([df, new_row], ignore_index=True)
                                save_extra_tickers_to_sheets(df)
                                sync_extra_tickers_to_local()
                                st.success(f"{code_str} を追加しました。")
                                time.sleep(0.5)
                                st.rerun()
                else:
                    st.caption(f"「{q}」の候補なし")
        elif len(q) == 1:
            st.caption("もう1文字以上入力すると候補が表示されます")

        st.divider()

        if not df.empty:
            col_h1, col_h2, col_h3 = st.columns([2, 5, 1])
            col_h1.markdown("**コード**")
            col_h2.markdown("**名称**")
            col_h3.markdown("**削除**")
            st.markdown("<hr style='margin: 0.2rem 0 !important;'>", unsafe_allow_html=True)
            
            to_delete = []
            for _, row in df.iterrows():
                col_c1, col_c2, col_c3 = st.columns([2, 5, 1])
                col_c1.write(row['code'])
                col_c2.write(row['name'])
                if col_c3.button("🗑️", key=f"etf_del_{row['code']}", help=f"{row['code']}を削除"):
                    to_delete.append(row["code"])
                    
            if to_delete:
                df = df[~df["code"].isin(to_delete)].reset_index(drop=True)
                save_extra_tickers_to_sheets(df)
                sync_extra_tickers_to_local()
                st.success("削除しました。")
                time.sleep(0.5)
                st.rerun()
        else:
            st.caption("登録されている追加ETFはありません。")

# ── ストップ高安（寄り付かず比例配分）バー一括修復コンポーネント ──
def render_stop_allocation_repair_ui(is_jp: bool):
    st.divider()
    st.subheader("🩹 ストップ高安（寄り付かず比例配分）バー修復")
    st.write(
        "日足が「寄り付かずS高/S安（比例配分）」で確定している日について、"
        "短期足(60m/5m/1m)から消失している大引けバーをRawデータから加工リビルドして再構成します。"
    )
    if st.button("🩹 ストップ高安バーを一括修復", key="btn_repair_stop_allocation", type="secondary"):
        status_box = st.status("📡 ストップ高安バー修復に伴うActiveリビルド中...", expanded=True)
        with status_box:
            def update_status(msg):
                st.write(msg)
            try:
                results = repair_stop_allocation_bars_full(is_jp=is_jp, status_callback=update_status)
                status_box.update(label="✅ Activeの修復リビルドが正常に完了しました。", state="complete")
            except Exception as e:
                status_box.update(label="❌ エラーが発生しました", state="error")
                st.error(f"修復中にエラーが発生しました: {e}")

# ── データベース健康診断コンポーネント ──
def render_database_diagnostics_ui(is_jp: bool):
    st.divider()
    st.subheader("📊 データベース健康診断 (段差・不具合検出)")
    st.write("各時間足(1d, 60m, 5m, 1m)を巡回し、価格データの断絶や一時的な配信バグを高速スキャンします。")
    
    if st.button("🔍 データベース健康診断を実行", key="btn_run_health_check", type="primary"):
        with st.spinner("データベースの整合性をフルスキャン中..."):
            st.session_state.detected_anomalies = run_database_health_scan(is_jp)
            st.success("健康診断が完了しました。")

    if "detected_anomalies" in st.session_state:
        anom_list = st.session_state.detected_anomalies
        
        if not anom_list:
            st.success("✅ データベース内に未調整の不整合（崖・バグ）は1件も検出されませんでした。")
            return
            
        st.warning(f"⚠️ データベースの整合性に不審な点がある箇所が {len(anom_list)} 件検出されました。")
        
        options = [
            f"【{a['不具合種類']}】{a['コード']} ({a['時間足']}) - 発生: {a['発生日/時刻']}" 
            for a in anom_list
        ]
        selected_idx = st.selectbox(
            "詳細を確認・治療する不具合を選択してください", 
            range(len(options)), 
            format_func=lambda x: options[x],
            key="sel_anomaly_view"
        )
        
        target_anom = anom_list[selected_idx]
        ticker = target_anom["コード"]
        interval = target_anom["時間足"]
        anomaly_type = target_anom["不具合種類"]
        
        detected_dates = re.findall(r"\d{4}-\d{2}-\d{2}", target_anom["発生日/時刻"])
        if not detected_dates:
            st.error("発生日の取得に失敗しました。")
            return
            
        base_date = pd.to_datetime(detected_dates[0])
        
        try:
            df_full = load_price_db(interval, is_jp=is_jp, is_raw=False)
            df_ticker = df_full[df_full["ticker"] == ticker].copy()
            df_ticker["date"] = pd.to_datetime(df_ticker["date"])
            df_ticker = df_ticker.sort_values("date").reset_index(drop=True)
        except Exception as e:
            st.error(f"データのロード中にエラーが発生しました: {e}")
            return
            
        st.write("---")
        st.markdown(f"### 🔍 **{ticker} ({interval})** 崖の周辺データ確認")
        
        cols_to_disp = ["date", "open", "high", "low", "close"]
        if "adj close" in df_ticker.columns:
            cols_to_disp.append("adj close")
        cols_to_disp.append("volume")
        
        if len(detected_dates) == 2:
            start_date = pd.to_datetime(detected_dates[0])
            end_date = pd.to_datetime(detected_dates[1])
            duration = (end_date - start_date).days
            
            if duration > 30:
                col_in, col_out = st.columns(2)
                with col_in:
                    st.markdown(f"📉 **崖の入り口**")
                    df_in = df_ticker[
                        (df_ticker["date"] >= start_date - pd.Timedelta(days=10)) & 
                        (df_ticker["date"] <= start_date + pd.Timedelta(days=10))
                    ]
                    st.dataframe(df_in[cols_to_disp], use_container_width=True, hide_index=True)
                with col_out:
                    st.markdown(f"📈 **崖の出口**")
                    df_out = df_ticker[
                        (df_ticker["date"] >= end_date - pd.Timedelta(days=10)) & 
                        (df_ticker["date"] <= end_date + pd.Timedelta(days=10))
                    ]
                    st.dataframe(df_out[cols_to_disp], use_container_width=True, hide_index=True)
            else:
                st.markdown(f"📊 **不具合の全貌**")
                df_view = df_ticker[
                    (df_ticker["date"] >= start_date - pd.Timedelta(days=10)) & 
                    (df_ticker["date"] <= end_date + pd.Timedelta(days=10))
                ]
                st.dataframe(df_view[cols_to_disp], use_container_width=True, hide_index=True)
                
        elif len(detected_dates) == 1:
            start_date = pd.to_datetime(detected_dates[0])
            df_view = df_ticker[
                (df_ticker["date"] >= start_date - pd.Timedelta(days=15)) & 
                (df_ticker["date"] <= start_date + pd.Timedelta(days=15))
            ]
            st.dataframe(df_view[cols_to_disp], use_container_width=True, hide_index=True)

# =====================================================================
# 🛠️ メイン画面描画処理
# =====================================================================

st.title("🗄️ データベース管理・保守センター")
st.caption("Raw / Activeの2層分離設計を搭載した安全な管理システムです。")

m_col1, m_col2 = st.columns([1, 1])
with m_col1:
    market_mode = st.radio("対象市場の選択", ["日本株 🇯🇵", "米国株 🇺🇸"], horizontal=True)
    is_jp = (market_mode == "日本株 🇯🇵")
with m_col2:
    last_date = get_db_last_update("1d", is_jp=is_jp)
    st.metric(label="現在のActive日足(1d)最終更新日", value=last_date)
    
st.divider()

# 🔬 安全アサーション & 開発検証制御用ウィジェット
st.subheader("🧪 データベースビルド検証コントロール")
use_dry_run = st.checkbox(
    "**Dry Run（検証のみ：価格加工後に健康チェックを行い、Parquet保存をスキップ）**", 
    value=True, 
    help="チェックをONにしておくと、バグ等で壊れた成果物Activeデータがディスクに保存されるのを未然に防止できます。"
)

st.divider()

# 🔄 ETF構成銘柄の同期（1枚完結・統合版）
st.subheader("🔄 ETFセクター構成の同期（スプレッドシート連動）")
if st.button("🚀 ETF構成銘柄を同期する", key="btn_sync_etf_master", use_container_width=True, type="primary"):
    with st.spinner("スプレッドシートを更新中..."):
        try:
            from data_access.sheets_api import sync_etf_sectors_consolidated
            results = sync_etf_sectors_consolidated(is_jp=is_jp)
            
            if "error" in results:
                st.error(f"❌ 同期に失敗しました: {results['error']}")
            else:
                sync_results = [f"• {k}: {v}" for k, v in results.items()]
                st.success("✅ 同期が完了しました！\n\n" + "\n".join(sync_results))
                time.sleep(1.0)
                st.rerun()
        except Exception as e:
            st.error(f"❌ 同期中にエラーが発生しました: {e}")

st.divider()

# ETFマスタ管理
render_etf_manager()

st.divider()

# 【セクション1】 全体差分ダウンロード（自動権利落ち防衛）
@st.fragment
def render_full_sync_section(is_jp: bool, dry_run_flag: bool):
    st.subheader("1️⃣ 全体差分ダウンロード＆自動Active再構築")
    st.write(
        "最新データまでRawデータベースを安全に差分収集した上で、"
        "メモリ上で一括変換・検証を行い、Activeデータベースを再構築します。"
    )

    if st.button("🔄 全体差分ダウンロードを実行", key="btn_all_diff_update", type="primary"):
        sync_log_lines = []
        status_box = st.status("📡 データベース全体差分同期中...", expanded=True)
        with status_box:
            st.write("追加収集ティッカーのローカル同期を実行中...")
            try:
                sync_extra_tickers_to_local()
                msg = "✅ ティッカーリストの同期に成功しました。"
                st.write(msg); sync_log_lines.append(msg)
            except Exception as e:
                msg = f"⚠️ ティッカー同期スキップ（キャッシュを使用）: {e}"
                st.write(msg); sync_log_lines.append(msg)

            st.write("差分情報のスキャンと必要更新箇所の算出中...")
            needs = analyze_db_update_needs(is_jp=is_jp)

            if needs.get("needs_period_update"):
                msg = f"⚠️ 3日以上のデータ未同期を検出（最新日: {needs['global_max_date']}）。"
                st.write(msg); sync_log_lines.append(msg)

            def update_status_on_screen(msg):
                st.write(f"  * {msg}")
                sync_log_lines.append(str(msg))

            with st.spinner("同期・構築中..."):
                try:
                    all_tickers = get_all_collection_tickers() if is_jp else ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
                    update_price_database(
                        is_jp=is_jp,
                        target_tickers=all_tickers,
                        status_callback=update_status_on_screen,
                        dry_run=dry_run_flag
                    )

                    status_box.update(label="🎉 データベース同期・ビルドタスク完了！", state="complete")
                except Exception as e:
                    status_box.update(label="❌ エラーが発生しました", state="error")
                    st.error(f"データベースの更新処理中に例外エラーが発生しました: {e}")
                    sync_log_lines.append(f"❌ エラーが発生しました: {e}")
                finally:
                    try:
                        uploaded_name = upload_sync_log_to_drive(sync_log_lines, is_jp=is_jp, prefix="sync")
                        if uploaded_name:
                            st.caption(f"📝 同期ログを Google ドライブに保存しました: {uploaded_name}")
                    except Exception as log_e:
                        st.caption(f"⚠️ ログのドライブ保存に失敗しました: {log_e}")

render_full_sync_section(is_jp, use_dry_run)

st.divider()

# 【セクション3】手動ピンポイント一括安全修復
@st.fragment
def render_manual_repair_section(is_jp: bool, dry_run_flag: bool):
    st.subheader("3️⃣ 手動ピンポイント一括安全修復")
    st.write(
        "特定の銘柄においてデータの欠損が発生した場合、Rawデータベースからクリーンダウンロード復元し、"
        "最新のTransform加工を通じてActiveを一元的に再構成します。"
    )

    with st.expander("🔍 異常データスキャン（Active DB対象）", expanded=False):
        st.caption("全銘柄の日足ActiveDBをスキャンして35%以上の急変箇所を検出します。")
        if st.button("🔍 異常スキャン実行", key="btn_anomaly_scan"):
            with st.spinner("全Active銘柄をスキャン中..."):
                anomalies = scan_all_anomalies(is_jp=is_jp, interval="1d")
            if anomalies.empty:
                st.success("✅ 異常箇所は検出されませんでした。")
            else:
                st.warning(f"⚠️ {len(anomalies)}件の異常箇所を検出（{anomalies['ticker'].nunique()}銘柄）")
                display_df = anomalies.copy()
                if "cliff_date" in display_df.columns:
                    display_df["cliff_date"] = pd.to_datetime(display_df["cliff_date"]).dt.strftime("%Y-%m-%d")
                if "pct_change" in display_df.columns:
                    display_df["pct_change"] = display_df["pct_change"].apply(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "－")
                if "est_multiplier" in display_df.columns:
                    display_df["est_multiplier"] = display_df["est_multiplier"].apply(lambda x: f"{x:.8f}".rstrip('0').rstrip('.') if pd.notna(x) else "－")

                rename_map = {
                    "ticker": "銘柄", "cliff_date": "崖日付", "est_multiplier": "推測修正比率 (当日÷1日前)", "pct_change": "変化率",
                    "before_close": "1日前 Close", "after_close": "Close", "before_adj_close": "1日前 Adj Close", "after_adj_close": "Adj Close"
                }
                display_df = display_df.rename(columns=rename_map)
                st.dataframe(display_df[[c for c in rename_map.values() if c in display_df.columns]], use_container_width=True, hide_index=True)

    st.write(" ")

    with st.form(key="safe_repair_form"):
        col_t1, col_t2, col_t3 = st.columns(3)
        with col_t1:
            rep_ticker = st.text_input("銘柄コード", placeholder="例:1629", key="rep_ticker_box")
        with col_t2:
            rep_date_str = st.text_input("修正開始日", placeholder="空白で個別ダウンロード復元", key="rep_date_box")
        with col_t3:
            rep_ratio_str = st.text_input("修正比率", placeholder="空白で個別ダウンロード復元", key="rep_ratio_box")
        
        repair_confirm = st.checkbox("⚠️ 入力内容が適正であることを確認しました", value=False, key="chk_repair_confirm")
        rep_col3_btn = st.form_submit_button("🔧 安全一括修復を実行", type="primary")

    if rep_col3_btn:
        if not repair_confirm:
            st.error("❌ 安全ロックがかかっています。チェックボックスをONにしてください。")
            st.stop()

        if not rep_ticker:
            st.error("銘柄コードが入力されていません。")
        else:
            if rep_ratio_str.strip():
                try:
                    temp_ratio = float(rep_ratio_str.strip())
                    if temp_ratio <= 0:
                        st.error(f"❌ 危険防止のため、修正比率に 0 以下の数値は設定できません。")
                        st.stop()
                except ValueError:
                    st.error("修正比率には有効な数値を入力してください。")
                    st.stop()

            pure_t = sanitize_ticker(rep_ticker, is_jp=is_jp)
            market_str = "JP" if is_jp else "US"
            
            if rep_date_str.strip() and rep_ratio_str.strip():
                try:
                    multiplier = float(rep_ratio_str.strip())
                    cliff_dt = pd.to_datetime(rep_date_str.strip())
                    cliff_dt_str = cliff_dt.strftime("%Y-%m-%d")
                except Exception as e:
                    st.error(f"形式が不正です: {e}")
                    st.stop()
                    
                with st.spinner(f"🔧 [{pure_t}] のパッチ定義を保存中..."):
                    results = apply_forced_scale_patch_to_all_timeframes(pure_t, cliff_dt_str, multiplier, is_jp=is_jp)
                    
                    executed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    log_row = {
                        "executed_at": executed_at, "ticker": pure_t, "market": market_str, "cliff_date": cliff_dt_str,
                        "interval": "all", "before_close": "", "after_close": "", "multiplier": multiplier, "memo": "手動ピンポイント崖一律修復（パッチ定義）",
                    }
                    save_repair_log_to_sheets([log_row])
                    
                    st.write("🔄 加工検証（Activeのバックビルド）を走らせています...")
                    for interval in settings.TIMEFRAMES:
                        rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=dry_run_flag)
                        
                    st.success("✅ パッチ定義が正常に保存され、加工検証が終了しました。")
            
            elif not rep_date_str.strip() and not rep_ratio_str.strip():
                with st.spinner(f"🔧 [{pure_t}] のRawデータ部分ダウンロード及びActive再生成中..."):
                    results = repair_single_ticker_all_timeframes(pure_t, is_jp=is_jp)
                    for interval, msg in results.items():
                        st.write(f" **{interval}**: {msg}")
                    st.success("✅ 個別復元が正常に完了しました。")

render_manual_repair_section(is_jp, use_dry_run)

# ── 指定日以前データ部分削除パッチUI ──
st.markdown("#### 🗑️ 指定日以前データ一括物理削除パッチ")
del_col1, del_col2, del_col3 = st.columns([3, 2, 1])
with del_col1:
    del_ticker = st.text_input("データ削除を実行する銘柄コード", placeholder="例: 8303", key="del_ticker_box")
with del_col2:
    del_date_str = st.text_input("削除の境界となる日付", placeholder="例: 2025-12-16", key="del_date_box")
with del_col3:
    st.write(" ")
    st.write(" ")
    btn_delete_before = st.button("🗑️ 指定日以前を物理削除", use_container_width=True, type="primary")

if btn_delete_before:
    if not del_ticker:
        st.error("銘柄コードが入力されていません。")
    elif not del_date_str:
        st.error("基準日付が入力されていません。")
    else:
        try:
            pd.to_datetime(del_date_str)
        except ValueError:
            st.error("日付は有効な形式（YYYY-MM-DD）で入力してください。")
            st.stop()
            
        pure_t = sanitize_ticker(del_ticker, is_jp=is_jp)
        with st.spinner(f"🗑️ [{pure_t}] 物理削除・ビルド中..."):
            from core.database_service import delete_data_before_date
            del_results = delete_data_before_date(pure_t, del_date_str, is_jp=is_jp)
            for interval, msg in del_results.items():
                st.write(f" **{interval}**: {msg}")
            st.success("✅ 物理削除とActive再構築が完了しました。")

# ── 修復ログ一覧 ──
with st.expander("📋 修復ログ一覧", expanded=False):
    log_col1, log_col2 = st.columns([1, 1])
    with log_col1:
        log_ticker_filter = st.text_input("銘柄コードで絞り込み", placeholder="例: 1629", key="log_filter_ticker")
    with log_col2:
        st.write(" ")
        btn_load_log = st.button("🔄 ログを読み込む", key="btn_load_log", use_container_width=True)

    if "repair_log_df" not in st.session_state:
        st.session_state.repair_log_df = None

    if btn_load_log:
        with st.spinner("スプレッドシートから修復ログを読み込み中..."):
            try:
                log_df = load_repair_log_from_sheets()
                st.session_state.repair_log_df = log_df if log_df is not None else pd.DataFrame()
            except Exception as e:
                st.error(f"❌ エラーが発生しました: {e}")

    if st.session_state.repair_log_df is not None:
        log_df = st.session_state.repair_log_df.copy()
        if log_df.empty:
            st.info("ℹ️ 保存されている修復ログはありません。")
        else:
            if log_ticker_filter.strip():
                log_df = log_df[log_df["ticker"].astype(str).str.contains(log_ticker_filter.strip(), case=False, na=False)]
            
            if log_df.empty:
                st.info(f"🔍 「{log_ticker_filter}」に一致するログは見つかりませんでした。")
            else:
                st.dataframe(log_df, use_container_width=True, hide_index=True)

# ── 手動パッチ全適用 ──
st.write(" ")
st.markdown("#### 🔄 **保存済みパッチのActiveクリーンリビルド適用**")
if st.button("🔄 保存されているすべてのパッチをActiveへ一括適用（リビルド）", key="btn_apply_all_patches_manual", type="secondary"):
    status_box = st.status("📡 パッチマスタ適用に伴うActive再構築中...", expanded=True)
    with status_box:
        def update_patch_status(msg):
            st.write(msg)
        try:
            count = apply_all_saved_patches(is_jp=is_jp, status_callback=update_patch_status)
            status_box.update(label="✅ Activeの再構築・検証・パッチ復元が全て完了しました！", state="complete")
        except Exception as e:
            status_box.update(label="❌ エラーが発生しました", state="error")
            st.error(f"パッチの一括適用中にエラーが発生しました: {e}")

st.divider()

# 【セクション4】 全件一括フルダウンロード・再構築
@st.fragment
def render_full_rebuild_section(is_jp: bool, market_mode: str, dry_run_flag: bool):
    st.subheader("4️⃣ 全件一括フルダウンロード・再構築（Rawもクリア）")
    st.write(
        "既存のRawおよびActiveデータベースを削除し、yfinanceの提供限界から一発でクリーンビルドし直します。"
    )

    fb_col1, fb_col2 = st.columns([2, 1])
    with fb_col1:
        rebuild_interval = st.selectbox(
            "一括再構築する時間足（タイムフレーム）を選択", ["1m", "5m", "60m", "1d"], index=3, key="rebuild_interval_select"
        )
    with fb_col2:
        st.write(" ")
        st.write(" ")
        btn_full_rebuild = st.button("💥 一括フルダウンロードを実行", use_container_width=True, type="primary")
        
    if btn_full_rebuild:
        rebuild_log_lines = []
        status_box = st.status(f"📡 {market_mode} {rebuild_interval} データベースを一括クリーンビルド中...", expanded=True)
        with status_box:
            st.write("既存のParquetファイルをクリア中...")
            for is_raw_target in [True, False]:
                from data_access.local_db import get_db_filename
                filename = get_db_filename(rebuild_interval, is_jp, is_raw=is_raw_target)
                work_file = os.path.join(settings.WORK_DIR, filename)
                if os.path.exists(work_file):
                    os.remove(work_file)
            
            def update_rebuild_status(msg):
                st.write(msg)
                rebuild_log_lines.append(str(msg))
                
            try:
                success = full_rebuild_all_database(
                    is_jp=is_jp, interval=rebuild_interval, status_callback=update_rebuild_status, dry_run=dry_run_flag
                )
                if success:
                    status_box.update(label="✅ 一括クリーンビルド検証完了！", state="complete")
                else:
                    status_box.update(label="❌ ビルド中に検証に失敗しました。Activeは上書きされませんでした。", state="error")
            except Exception as e:
                status_box.update(label="❌ エラー発生", state="error")
                st.error(f"再構築中に予期せぬエラーが発生しました: {e}")

render_full_rebuild_section(is_jp, market_mode, use_dry_run)

# UI拡張パーツ
render_stop_allocation_repair_ui(is_jp=is_jp)
render_database_diagnostics_ui(is_jp=is_jp)