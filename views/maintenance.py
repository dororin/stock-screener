# views/maintenance.py
import os
import time
from datetime import datetime
import pandas as pd
import streamlit as st
import re

from config import settings
from data_access.local_db import load_price_db, save_price_db, execute_jp_merge
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
    full_rebuild_all_database,
    run_database_health_scan,
    apply_all_saved_patches,
    rebuild_active_from_raw,
    execute_apply_verified_temp_dbs_to_active,
    delete_data_before_date
)
from core.us_price_corrector import (
    apply_forced_scale_patch_to_all_timeframes,
    test_forced_scale_patch_in_memory,
    scan_and_diagnose_cliffs_with_tv,
    apply_bulk_selected_patches
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
@st.fragment
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
                        already = code_str in df["銘柄コード"].values if not df.empty else False
                        
                        col_add_left, col_add_right = st.columns([4, 1])
                        col_add_left.write(f"{code_str}　{name_str}")
                        if already:
                            col_add_right.button("✅ 登録済", key=f"etf_add_btn_{code_str}", disabled=True, use_container_width=True)
                        else:
                            if col_add_right.button("追加", key=f"etf_add_btn_{code_str}", use_container_width=True):
                                new_row = pd.DataFrame([{
                                    "セクター名": "追加ETF",
                                    "銘柄コード": code_str,
                                    "備考": name_str,
                                    "ETFコード": "",
                                    "ファンド": "",
                                    "非表示": "OFF"
                                }])
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
                col_c1.write(row['銘柄コード'])
                col_c2.write(row['備考'])
                if col_c3.button("🗑️", key=f"etf_del_{row['銘柄コード']}", help=f"{row['銘柄コード']}を削除"):
                    to_delete.append(row["銘柄コード"])
                    
            if to_delete:
                df = df[~df["銘柄コード"].isin(to_delete)].reset_index(drop=True)
                save_extra_tickers_to_sheets(df)
                sync_extra_tickers_to_local()
                st.success("削除しました。")
                time.sleep(0.5)
                st.rerun()
        else:
            st.caption("登録されている追加ETFはありません。")

# ── 🔍 US専用：統合データスキャン ──
@st.fragment
def render_unified_scan_and_repair_ui(is_jp: bool):
    if is_jp:
        return  

    st.divider()
    st.subheader("🔍 統合データスキャン（自動検出 → TradingView照合 → 一括自動修復）")
    st.write(
        "「異常データスキャン」と「データベース健康診断」を統合。日足の段差・マイナス転換を自動検出し、"
        "TradingViewの正しい終値と自動照合して「真の倍率」を算出します。"
        "チェックボックスで選択した行だけを、ワンクリックで安全に一括本番修復できます。"
    )

    if st.button("🔍 統合スキャンを実行", key="btn_unified_scan", type="primary", use_container_width=True):
        with st.spinner("全時間足（1d/60m/5m/1m）の段差検出とTradingViewデータ照合を実行中..."):
            st.session_state.unified_scan_result = scan_and_diagnose_cliffs_with_tv()
        st.success("統合スキャンが完了しました。")

    result_df = st.session_state.get("unified_scan_result")
    if result_df is None:
        return

    if result_df.empty:
        st.success("✅ データベース内に未調整の不整合（崖・バグ）は1件も検出されませんでした。")
        return

    st.warning(f"⚠️ {len(result_df)}件の異常箇所を検出（{result_df['ticker'].nunique()}銘柄）")

    display_df = result_df.copy()
    display_df["選択"] = False
    if "cliff_date" in display_df.columns:
        display_df["cliff_date"] = pd.to_datetime(display_df["cliff_date"]).dt.strftime("%Y-%m-%d")
    if "patch_date" in display_df.columns:
        display_df["patch_date"] = pd.to_datetime(display_df["patch_date"]).dt.strftime("%Y-%m-%d")

    unresolved_mask = display_df["true_multiplier"].isna()

    rename_map = {
        "選択": "選択",
        "ticker": "銘柄",
        "interval": "時間足",
        "cliff_date": "変化点",
        "patch_date": "要補正Close日時（適用基準）",
        "before_close": "要補正 Close",
        "after_close": "基準 Close",
        "before_adj_close": "要補正 Adj Close",
        "after_adj_close": "基準 Adj Close",
        "volume": "出来高",
        "tv_close": "TV Close（要補正側）",
        "est_multiplier": "推測倍率(est_multiplier)",
        "true_multiplier": "真の倍率(true_multiplier)",
    }
    display_df = display_df.rename(columns=rename_map)
    ordered_cols = [c for c in rename_map.values() if c in display_df.columns]
    display_df = display_df[ordered_cols]

    edited_df = st.data_editor(
        display_df,
        use_container_width=True,
        hide_index=True,
        disabled=[c for c in ordered_cols if c != "選択"],
        column_config={
            "選択": st.column_config.CheckboxColumn("選択", help="真の倍率が取得できていない行は選択できません"),
        },
        key="unified_scan_editor",
    )

    edited_df.loc[unresolved_mask.values, "選択"] = False

    selected_rows = edited_df[edited_df["選択"] == True]
    st.caption(f"現在 {len(selected_rows)} 件が選択されています。")

    if st.button("🚀 選択したパッチをすべて本番適用（一括実行）", key="btn_bulk_apply_selected", type="primary", use_container_width=True, disabled=selected_rows.empty):
        patches = [
            {"ticker": r["銘柄"], "patch_date": r["要補正Close日時（適用基準）"], "multiplier": r["真の倍率(true_multiplier)"]}
            for _, r in selected_rows.iterrows()
        ]
        status_box = st.status("📡 選択パッチの一括本番適用を実行中...", expanded=True)
        with status_box:
            def update_bulk_status(msg):
                st.write(msg)
            summary = apply_bulk_selected_patches(patches, status_callback=update_bulk_status)
            status_box.update(
                label=f"🎉 完了：{summary['repaired']}件修復、{summary['skipped']}件スキップ",
                state="complete"
            )
        st.success(f"✅ {summary['repaired']}件修復、{summary['skipped']}件スキップしました。")
        del st.session_state["unified_scan_result"]
        time.sleep(1.0)
        st.rerun()

# ── US専用：開発者機能 ──
def render_commit_verified_data_ui(is_jp: bool):
    if is_jp:
        return  

    has_temp_verified = any(
        st.session_state.get(f"temp_verified_active_exists_{tf}", False) 
        for tf in settings.TIMEFRAMES
    )
    has_temp_manual_repair = "temp_manual_repair_dfs" in st.session_state and st.session_state.temp_manual_repair_dfs

    if not has_temp_verified and not has_temp_manual_repair:
        return

    st.markdown("### ☁️ **検証済みの一時データがディスクに保管されています**")
    st.info(
        "現在、Dry Runテスト検証を通過した安全なデータが一時ファイル（Parquet）としてローカルディスクに退避されています。\n\n"
        "以下の「本番適用」ボタンを押すと、再計算や再ダウンロードを行うことなく、数秒でGoogleドライブへ一括確定保存されます。"
    )

    for tf in settings.TIMEFRAMES:
        preview_key = f"temp_verified_active_preview_{tf}"
        if st.session_state.get(preview_key) is not None:
            with st.expander(f"📊 【{tf}】加工データ構造プレビュー（先頭100行）"):
                st.dataframe(st.session_state[preview_key], use_container_width=True, hide_index=True)

    col_btn_apply, col_btn_clear = st.columns([2, 1])
    
    if col_btn_apply.button("💻 一時ファイル（またはメモリパッチ）をGoogleドライブへ本番適用する", key="btn_apply_verified_data_commit", type="primary", use_container_width=True):
        status_box = st.status("📡 一時ファイルの本番確定・Googleドライブ同期中...", expanded=True)
        with status_box:
            success_count = 0
            if has_temp_verified:
                results = execute_apply_verified_temp_dbs_to_active(is_jp=False, status_callback=None)
                for interval, res in results.items():
                    if res["success"]:
                        st.success(f"✅ [{interval}] のGoogleドライブ本番同期が正常に完了しました。")
                        success_count += 1
                    else:
                        st.error(f"❌ [{interval}] の同期に失敗しました: {res['message']}")
                    
            if has_temp_manual_repair:
                temp_repair_dfs = st.session_state.temp_manual_repair_dfs
                payload = st.session_state.temp_manual_repair_payload
                
                for interval, df_repaired in temp_repair_dfs.items():
                    cloud_success, cloud_msg = save_price_db(df_repaired, interval, is_jp=False, is_raw=False)
                    if cloud_success:
                        st.success(f"✅ [{interval}] の手動パッチ本番同期完了。")
                        success_count += 1
                    else:
                        st.error(f"❌ [{interval}] の同期失敗: {cloud_msg}")
                
                log_row = {
                    "executed_at": payload["executed_at"],
                    "ticker": payload["ticker"],
                    "market": payload["market"],
                    "cliff_date": payload["cliff_date"],
                    "interval": "all",
                    "before_close": "",
                    "after_close": "",
                    "multiplier": payload["multiplier"],
                    "memo": payload["memo"],
                }
                save_repair_log_to_sheets([log_row])
                st.success("📝 手動修復パッチ定義をスプレッドシートに保存しました。")
                
                del st.session_state["temp_manual_repair_dfs"]
                del st.session_state["temp_manual_repair_payload"]
            
            if success_count > 0:
                status_box.update(label=f"🎉 計 {success_count} 個の時間足データの本番同期が完了しました！", state="complete")
                time.sleep(1.0)
                st.rerun()

    if col_btn_clear.button("🗑️ 検証一時データを破棄する", key="btn_clear_verified_data_cache", type="secondary", use_container_width=True):
        for tf in settings.TIMEFRAMES:
            if f"temp_verified_active_exists_{tf}" in st.session_state:
                st.session_state[f"temp_verified_active_exists_{tf}"] = False
            if f"temp_verified_active_preview_{tf}" in st.session_state:
                del st.session_state[f"temp_verified_active_preview_{tf}"]
                
            from data_access.local_db import get_db_filename
            temp_filename = get_db_filename(tf, is_jp=False, is_raw=False, is_temp=True)
            temp_path = os.path.join(settings.WORK_DIR, temp_filename)
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                    
        if has_temp_manual_repair:
            del st.session_state["temp_manual_repair_dfs"]
            del st.session_state["temp_manual_repair_payload"]
            
        st.warning("メモリ上およびディスク上の一時データを消去しました。")
        time.sleep(0.5)
        st.rerun()


# ── US専用：手動ピンポイント一括安全修復 ──
@st.fragment
def render_manual_repair_section(is_jp: bool):
    if is_jp:
        return  

    st.write(
        "TradingView APIが停止している等の非常時に備え、手動ピンポイント一括安全修復フォームを"
        "バックアップとして残しています。"
    )

    with st.form(key="safe_repair_form"):
        col_t1, col_t2, col_t3 = st.columns(3)
        with col_t1:
            rep_ticker = st.text_input("銘柄コード", placeholder="例:AAPL", key="rep_ticker_box")
        with col_t2:
            rep_date_str = st.text_input("要補正Close日時（崖前日）", placeholder="空白で個別ダウンロード復元", key="rep_date_box")
        with col_t3:
            rep_ratio_str = st.text_input("修正比率", placeholder="空白で個別ダウンロード復元", key="rep_ratio_box")
        
        repair_confirm = st.checkbox("⚠️ 入力内容が適正であることを確認しました", value=False, key="chk_repair_confirm")
        
        col_form_test, col_form_real = st.columns(2)
        rep_col3_test_btn = col_form_test.form_submit_button("🧪 まずテスト検証を実行（保存なし）", type="secondary")
        rep_col3_btn = col_form_real.form_submit_button("🚀 直接本番修復を実行（即時保存）", type="primary")

    if rep_col3_test_btn:
        if not repair_confirm:
            st.error("❌ 安全ロックがかかっています。チェックボックスをONにしてください。")
        elif not rep_ticker:
            st.error("銘柄コードが入力されていません。")
        else:
            pure_t = sanitize_ticker(rep_ticker, is_jp=False)
            
            if rep_date_str.strip() and rep_ratio_str.strip():
                try:
                    multiplier = float(rep_ratio_str.strip())
                    if multiplier <= 0:
                        st.error("修正比率に 0 以下の数値は設定できません。")
                        st.stop()
                    cliff_dt = pd.to_datetime(rep_date_str.strip())
                    cliff_dt_str = cliff_dt.strftime("%Y-%m-%d")
                except Exception as e:
                    st.error(f"形式が不正です: {e}")
                    st.stop()
                    
                with st.spinner(f"🔧 [{pure_t}] のパッチ適用テストをメモリ上で実行中..."):
                    test_results, temp_repaired_dfs = test_forced_scale_patch_in_memory(
                        pure_t, cliff_dt_str, multiplier
                    )
                    
                    if "error" in test_results:
                        st.error(f"❌ 検証失敗: {test_results['error']}")
                    else:
                        st.success("✅ メモリ上での崖調整テストが完了しました。")
                        
                        for interval, info in test_results.items():
                            if isinstance(info, dict):
                                st.markdown(f"📊 **【{interval}】 調整対比プレビュー (調整件数: {info['applied_count']}件)**")
                                disp_cols = [c for c in ["date", "open", "high", "low", "close"] if c in info["before_sample"].columns]
                                col_b, col_a = st.columns(2)
                                with col_b:
                                    st.caption("調整前 (Before)")
                                    st.dataframe(info["before_sample"][disp_cols], use_container_width=True, hide_index=True)
                                with col_a:
                                    st.caption("調整後 (After)")
                                    st.dataframe(info["after_sample"][disp_cols], use_container_width=True, hide_index=True)
                        
                        st.session_state.temp_manual_repair_dfs = temp_repaired_dfs
                        st.session_state.temp_manual_repair_payload = {
                            "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "ticker": pure_t, "market": "US", "cliff_date": cliff_dt_str, "multiplier": multiplier, "memo": "手動修復テスト"
                        }
            
            elif not rep_date_str.strip() and not rep_ratio_str.strip():
                with st.spinner(f"🔧 [{pure_t}] のRawデータダウンロード中..."):
                    results = repair_single_ticker_all_timeframes(pure_t, is_jp=False)
                    for interval, msg in results.items():
                        st.write(f" **{interval}**: {msg}")


# ── US専用：指定日以前データ部分削除パッチUI ──
@st.fragment
def render_delete_before_date_ui(is_jp: bool):
    if is_jp:
        return  

    st.markdown("#### 🗑️ 指定日以前データ一括物理削除パッチ")
    del_col1, del_col2, del_col3 = st.columns([3, 2, 1])
    with del_col1:
        del_ticker = st.text_input("データ削除を実行する銘柄コード", placeholder="例:AAPL", key="del_ticker_box")
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
                
            pure_t = sanitize_ticker(del_ticker, is_jp=False)
            with st.spinner(f"🗑️ [{pure_t}] 物理削除中..."):
                del_results = delete_data_before_date(pure_t, del_date_str, is_jp=False)
                for interval, msg in del_results.items():
                    st.write(f" **{interval}**: {msg}")


# ── US専用：全体差分ダウンロード ──
@st.fragment
def render_full_sync_section(is_jp: bool):
    st.subheader("1️⃣ 全体差分ダウンロード＆自動Active構築")
    
    if is_jp:
        st.warning("⚠️ **日本株（JP）のデータ更新は、ローカル環境で `rss_collector_jp.py` を実行してください。**")
        return

    st.write(
        "最新データまで米国株Rawデータベースを安全に差分ダウンロードします。 "
    )

    col_btn_test, col_btn_real = st.columns(2)
    
    if col_btn_test.button("🧪 まずテスト検証を実行", key="btn_test_diff_update", type="secondary", use_container_width=True):
        st.session_state["sync_logs_history"] = []
        status_box = st.status("📡 米国株全体差分 テスト検証中...", expanded=True)
        with status_box:
            def update_status_on_screen(msg):
                st.write(f"  * {msg}")
            try:
                all_tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
                update_price_database(is_jp=False, target_tickers=all_tickers, status_callback=update_status_on_screen, dry_run=True)
                status_box.update(label="🎉 テスト検証が完了しました！", state="complete")
                time.sleep(1.0)
                st.rerun()
            except Exception as e:
                st.error(f"エラー: {e}")


# ── US専用：一括フルダウンロード再構築ダイアログ ──
@st.dialog("🚨 データベース完全再構築の最終確認", width="medium")
def run_full_rebuild_dialog(interval: str, is_jp: bool, market_mode: str):
    if "rebuild_status" not in st.session_state:
        st.session_state.rebuild_status = "confirm"
    if "rebuild_logs" not in st.session_state:
        st.session_state.rebuild_logs = []

    if st.session_state.rebuild_status == "confirm":
        st.warning("⚠️ **警告：この操作は取り消せません**")
        st.write("1分足や5分足の場合、取得可能上限を過ぎて消失した古い履歴データは完全に失われます。本当に実行してよろしいですか？")
        st.write(" ")

        col_yes, col_no = st.columns(2)
        if col_no.button("いいえ（キャンセル）", use_container_width=True):
            st.session_state.show_rebuild_dialog = False
            st.rerun()
        if col_yes.button("はい（本当に実行する）", type="primary", use_container_width=True):
            st.session_state.rebuild_status = "processing"
            st.rerun()

    elif st.session_state.rebuild_status == "processing":
        status_box = st.status(f"📡 {market_mode} {interval} を一括クリーンビルド中...", expanded=True)
        rebuild_log_lines = []
        with status_box:
            for is_raw_target in [True, False]:
                from data_access.local_db import get_db_filename
                filename = get_db_filename(interval, is_jp=False, is_raw=is_raw_target)
                work_file = os.path.join(settings.WORK_DIR, filename)
                if os.path.exists(work_file):
                    try: os.remove(work_file)
                    except Exception: pass
            
            def update_rebuild_status(msg):
                st.write(msg)
                rebuild_log_lines.append(str(msg))
                
            try:
                success = full_rebuild_all_database(is_jp=False, interval=interval, status_callback=update_rebuild_status, dry_run=False)
                st.session_state.rebuild_logs = rebuild_log_lines
                st.session_state.rebuild_status = "success" if success else "failed"
                st.rerun()
            except Exception as e:
                st.session_state.rebuild_logs = rebuild_log_lines + [f"エラー: {e}"]
                st.session_state.rebuild_status = "failed"
                st.rerun()

    elif st.session_state.rebuild_status == "success":
        st.success("🎉 一括フルダウンロード・再構築に成功しました！")
        if st.button("確認して閉じる", type="primary", use_container_width=True):
            st.session_state.show_rebuild_dialog = False
            st.session_state.rebuild_status = "confirm"
            st.rerun()


# ── US専用：全件一括フルダウンロード ──
@st.fragment
def render_full_rebuild_section(is_jp: bool, market_mode: str):
    if is_jp:
        return  

    st.subheader("4️⃣ 全件一括フルダウンロード・再構築（Rawもクリア）")
    col1, col2 = st.columns([2, 1])
    with col1:
        rebuild_interval = st.selectbox("一括再構築する時間足を選択", ["1m", "5m", "60m", "1d"], index=3, key="rebuild_interval_select")
    with col2:
        st.write(" ") 
        st.write(" ")
        if st.button("💥 一括フルダウンロードを実行", key="btn_real_full_rebuild_trigger", type="primary", use_container_width=True):
            st.session_state.show_rebuild_dialog = True
            st.session_state.rebuild_status = "confirm"
            st.rerun()


# ─── 🚀 新設：日本株専用手動上書きマージセンター ───
@st.fragment
def render_jp_manual_merge_center(is_jp: bool):
    if not is_jp:
        return

    st.divider()
    st.subheader("⚙️ 日本株データ統合マージセンター (手動コンパクション)")
    st.write(
        "楽天RSSからダウンロードされ、Google Driveの時間足フォルダ（例：`1m/`）直下に隔離保管されている「未処理の差分ファイル（`_diff_`）」をロードし、"
        "古い順に累積ソートした上で、対応する月別本番結合ファイル（例：`price_jp_1m_2026_07.parquet`）へ `keep='last'` で安全上書きマージします。\n\n"
        "**統合保存が100%成功した場合、Drive上の対象差分ファイルは自動的に完全に物理消去され、二重マージや先祖返りを完全に防止します。**"
    )

    from data_access.drive_api import get_drive_service, list_drive_diff_files, get_or_create_drive_folder
    service = get_drive_service()
    
    if service:
        with st.status("📡 Google Drive上の未処理差分データを簡易検索中...", expanded=False) as scan_status:
            total_diff_count = 0
            scanned_details = []
            
            for tf in settings.TIMEFRAMES:
                try:
                    tf_folder_id = get_or_create_drive_folder(tf, settings.FOLDER_ID)
                    diffs = list_drive_diff_files(tf_folder_id)
                    tf_diff_count = len(diffs)
                    total_diff_count += tf_diff_count
                    if tf_diff_count > 0:
                        scanned_details.append(f"• 【{tf}】: {tf_diff_count} 件の未処理差分ファイル")
                except Exception:
                    pass
            
            if total_diff_count > 0:
                scan_status.update(label=f"📂 未マージの日本株差分ファイルを計 {total_diff_count} 件検出しました。", state="warning")
                for line in scanned_details:
                    st.write(line)
            else:
                scan_status.update(label="✅ 未マージの差分ファイルはありません（本番データベースは最新です）。", state="complete")
    
    merge_tf = st.selectbox("マージを強制実行する時間足を選択", ["1d", "60m", "5m", "1m"], index=0, key="jp_merge_tf_select")

    col_m1, col_m2 = st.columns([2, 1])
    with col_m1:
        st.caption(f"※実行ボタンを押すと、Google Drive上の【{merge_tf}】時間足フォルダ直下の差分を一括統合マージします。")
    with col_m2:
        if st.button("🚀 マージを実行する", key="btn_execute_jp_manual_merge", type="primary", use_container_width=True):
            status_box = st.status(f"🔄 【{merge_tf}】の上書き累積マージ処理を実行中...", expanded=True)
            with status_box:
                def on_status(msg):
                    st.write(msg)
                    
                result = execute_jp_merge(merge_tf, status_callback=on_status)
                
                if result.get("success"):
                    status_box.update(label="🎉 統合マージおよび不要差分ファイルの自動消去が正常に完了しました！", state="complete")
                    st.success(result.get("message"))
                    time.sleep(2.0)
                    st.rerun()
                else:
                    status_box.update(label="❌ マージ処理中にエラーが発生しました", state="error")
                    st.error(result.get("message"))


# =====================================================================
# 🛠️ メイン画面描画
# =====================================================================

st.title("🗄️ データベース管理・保守センター")
st.caption("Raw / Activeの2層分離設計を搭載した、安全で再描画のないデータ管理システムです。")

m_col1, m_col2 = st.columns([1, 1])
with m_col1:
    market_mode = st.radio("対象市場の選択", ["日本株 🇯🇵", "米国株 🇺🇸"], horizontal=True)
    is_jp = (market_mode == "日本株 🇯🇵")
with m_col2:
    last_date = get_db_last_update("1d", is_jp=is_jp)
    st.metric(label="現在のActive日足(1d)最終更新日", value=last_date)
    
st.divider()

if "sync_logs_history" not in st.session_state:
    st.session_state["sync_logs_history"] = []

if st.session_state["sync_logs_history"]:
    with st.expander("📝 詳細ログ履歴コンソール", expanded=True):
        st.code("\n".join(st.session_state["sync_logs_history"]), language="text")
        if st.button("🗑️ ログ表示履歴をクリア", key="btn_clear_st_logs_history_on_screen", use_container_width=True):
            st.session_state["sync_logs_history"] = []
            st.rerun()
    st.divider()

# 一時ファイルのコミットUI（US株選択時のみ有効化）
render_commit_verified_data_ui(is_jp)

# 🚀 日本株専用：手動上書きマージセンター（日本株選択時のみ増設表示）
render_jp_manual_merge_center(is_jp)

# 🔄 ETF構成銘柄の同期（共通機能）
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
            st.error(f"❌ エラーが発生しました: {e}")

st.divider()

# ETFマスタ管理
render_etf_manager()
st.divider()

# 1️⃣ 全体差分ダウンロード（日本株：説明のみ / 米国株：ダウンロード可能）
render_full_sync_section(is_jp)
st.divider()

# 🔍 米国株専用：統合スキャン・自動修復エリア
render_unified_scan_and_repair_ui(is_jp=is_jp)
st.divider()

if not is_jp:
    # 物理削除パッチUI
    render_delete_before_date_ui(is_jp=False)

    # 修復ログ一覧
    with st.expander("📋 修復ログ一覧", expanded=False):
        log_col1, log_col2 = st.columns([1, 1])
        with log_col1:
            log_ticker_filter = st.text_input("銘柄コードで絞り込み", placeholder="例: AAPL", key="log_filter_ticker")
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

    # 手動パッチマスタ一括適用（リビルド）
    st.write(" ")
    st.markdown("#### 🔄 **保存済みパッチのActiveクリーンリビルド適用**")
    if st.button("🔄 保存されているすべてのパッチをActiveへ一括適用（リビルド）", key="btn_apply_all_patches_manual", type="secondary", use_container_width=True):
        status_box = st.status("📡 パッチマスタ適用に伴うActive再構築中...", expanded=True)
        with status_box:
            def update_patch_status(msg):
                st.write(msg)
            try:
                count = apply_all_saved_patches(is_jp=False, status_callback=update_patch_status)
                status_box.update(label="✅ Activeの再構築・検証・パッチ復元が全て完了しました！", state="complete")
            except Exception as e:
                st.error(f"パッチの一括適用中にエラーが発生しました: {e}")

    st.divider()

    # 4️⃣ 全件一括フルダウンロード・再構築
    render_full_rebuild_section(is_jp=False, market_mode=market_mode)

    # 非常用オーバーライドエリア
    with st.expander("⚙️ 手動修復（非常用・TradingView API停止時）", expanded=False):
        render_manual_repair_section(is_jp=False)

if st.session_state.get("show_rebuild_dialog"):
    run_full_rebuild_dialog(rebuild_interval, is_jp=False, market_mode=market_mode)