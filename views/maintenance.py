# maintenance.py

import os
import time
from datetime import datetime, timedelta
import pandas as pd
import streamlit as st
import re
import calendar

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

# ── 日足の最新更新日の簡易取得 (投影ロード ＆ キャッシュによる超高速化) ──
@st.cache_data(ttl=600)
def get_db_last_update_cached(interval: str, is_jp: bool = True) -> str:
    """columns=['date'] を指定して日付列だけをロードするため、全列ロードに比べ約100倍高速に動作します。"""
    try:
        df = load_price_db(interval, is_jp=is_jp, is_raw=False, columns=["date"])
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
                                time.sleep(0.3)
                                # フラグメントだけを再読込
                                st.rerun(scope="fragment")
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
                time.sleep(0.3)
                st.rerun(scope="fragment")
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
    )

    if st.button("🔍 統合スキャンを実行", key="btn_unified_scan", type="primary", use_container_width=True):
        with st.spinner("全時間足（1d/60m/5m/1m）の段差検出とTradingViewデータ照合を実行中..."):
            st.session_state.unified_scan_result = scan_and_diagnose_cliffs_with_tv()
        st.success("統合スキャンが完了しました。")
        st.rerun(scope="fragment")

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
        st.cache_data.clear() # キャッシュクリア
        time.sleep(1.0)
        st.rerun(scope="fragment")


# ── US専用：開発者機能（コミットUIのフラグメント化） ──
@st.fragment
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
        "以下の「本番適用」ボタンを押すと、Googleドライブへ一括確定保存されます。"
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
                st.cache_data.clear() # 更新完了に付き最終更新日キャッシュをクリア
                status_box.update(label=f"🎉 計 {success_count} 個の時間足データの本番同期が完了しました！", state="complete")
                time.sleep(1.0)
                st.rerun(scope="fragment")

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
        st.rerun(scope="fragment")


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
        
        is_confirm = st.checkbox("⚠️ 入力内容が適正であることを確認しました", value=False, key="chk_repair_confirm")
        
        col_form_test, col_form_real = st.columns(2)
        rep_col3_test_btn = col_form_test.form_submit_button("🧪 まずテスト検証を実行（保存なし）", type="secondary")
        rep_col3_btn = col_form_real.form_submit_button("🚀 直接本番修復を実行（即時保存）", type="primary")

    if rep_col3_test_btn:
        if not is_confirm:
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
            st.cache_data.clear() # 物理削除が成功したらキャッシュクリア
            st.rerun(scope="fragment")


# ── US専用：全体差分ダウンロード ──
@st.fragment
def render_full_sync_section(is_jp: bool):
    st.subheader("1️⃣ 全体差分ダウンロード＆自動Active構築")
    
    if is_jp:
        st.warning("⚠️ **日本株（JP）のデータ更新は、ローカル環境で `rss_collector_jp.py` を実行してください。**")
        return

    st.write("最新データまで米国株Rawデータベースを安全に差分ダウンロードします。")

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
                st.rerun(scope="fragment")
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
        st.cache_data.clear() # フルリビルド後はキャッシュクリア
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


# ─── 🚀 日本株専用手動上書きマージセンター（状態維持＆ログの永続表示化） ───
@st.fragment
def render_jp_manual_merge_center(is_jp: bool):
    if not is_jp:
        return

    st.divider()
    st.subheader("⚙️ 日本株データ統合マージセンター (手動コンパクション)")
    st.write(
        "楽天RSSからダウンロードされ、Google Driveの時間足フォルダ（例：`1m/`）直下に隔離保管されている「未処理の差分ファイル（`_diff_`）」をロードし、"
        "古い順に累積ソートした上で、対応する月別本番結合ファイル（例：`price_jp_1m_2026_07.parquet`）へ `keep='last'` で安全上書きマージします。"
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
                scan_status.update(label=f"📂 未マージの日本株差分ファイルを計 {total_diff_count} 件検出しました。", state="complete")
                for line in scanned_details:
                    st.write(line)
            else:
                scan_status.update(label="✅ 未マージの差分ファイルはありません（本番データベースは最新です）。", state="complete")
    
    merge_tf = st.selectbox("マージを強制実行する時間足を選択", ["1d", "60m", "5m", "1m"], index=0, key="jp_merge_tf_select")

    # ログ状態管理用のセッションキー初期化
    if "jp_merge_logs_list" not in st.session_state:
        st.session_state["jp_merge_logs_list"] = []
    if "jp_merge_running" not in st.session_state:
        st.session_state["jp_merge_running"] = False
    if "jp_merge_finished" not in st.session_state:
        st.session_state["jp_merge_finished"] = False
    if "jp_merge_result_msg" not in st.session_state:
        st.session_state["jp_merge_result_msg"] = None
    if "jp_merge_success" not in st.session_state:
        st.session_state["jp_merge_success"] = False

    col_m1, col_m2 = st.columns([2, 1])
    with col_m1:
        st.caption(f"※実行ボタンを押すと、Google Drive上の【{merge_tf}】時間足フォルダ直下の差分を一括統合マージします。")
    with col_m2:
        # 実行中は重複起動できないようロック
        btn_disabled = st.session_state["jp_merge_running"]
        if st.button("🚀 マージを実行する", key="btn_execute_jp_manual_merge", type="primary", use_container_width=True, disabled=btn_disabled):
            st.session_state["jp_merge_logs_list"] = []
            st.session_state["jp_merge_running"] = True
            st.session_state["jp_merge_finished"] = False
            st.session_state["jp_merge_result_msg"] = None
            st.session_state["jp_merge_success"] = False
            st.rerun(scope="fragment")

    # マージ実処理中のスピナー & コールバック
    if st.session_state["jp_merge_running"] and not st.session_state["jp_merge_finished"]:
        status_box = st.status(f"🔄 【{merge_tf}】の上書き累積マージ処理を実行中...", expanded=True)
        with status_box:
            def on_status(msg):
                st.session_state["jp_merge_logs_list"].append(msg)
                st.write(msg)
                
            result = execute_jp_merge(merge_tf, status_callback=on_status)
            
            st.session_state["jp_merge_finished"] = True
            st.session_state["jp_merge_running"] = False
            
            if result.get("success"):
                st.cache_data.clear() # マージ成功に付き、キャッシュデータを全面フラッシュ
                status_box.update(label="🎉 統合マージおよび不要差分ファイルの自動消去が正常に完了しました！", state="complete")
                st.session_state["jp_merge_success"] = True
                st.session_state["jp_merge_result_msg"] = result.get("message")
            else:
                status_box.update(label="❌ マージ処理中にエラーが発生しました", state="error")
                st.session_state["jp_merge_success"] = False
                st.session_state["jp_merge_result_msg"] = result.get("message")
            
            st.rerun(scope="fragment")

    # 完了後の表示（ログが自動で閉じられるのを防ぐ）
    if st.session_state["jp_merge_finished"]:
        if st.session_state["jp_merge_success"]:
            st.success(f"🎉 成功: {st.session_state['jp_merge_result_msg']}")
        else:
            st.error(f"❌ 失敗: {st.session_state['jp_merge_result_msg']}")

        # 溜まったログ一覧を閉じるまで常時表示
        with st.expander("📝 実行ログ詳細", expanded=True):
            st.code("\n".join(st.session_state["jp_merge_logs_list"]), language="text")
            
            if st.button("🗑️ ログを閉じてクリアする", key="btn_clear_jp_merge_logs", use_container_width=True):
                st.session_state["jp_merge_logs_list"] = []
                st.session_state["jp_merge_finished"] = False
                st.session_state["jp_merge_result_msg"] = None
                st.session_state["jp_merge_success"] = False
                st.rerun(scope="fragment")


# ─── 🚀 【新設】日本株専用：統合段差スキャン・一括自動修復 ───
@st.fragment
def render_jp_split_scan_and_repair_ui(is_jp: bool):
    if not is_jp:
        return

    st.divider()
    st.subheader("🔍 日本株 統合段差スキャン・修復テーブル（自動スキャン → 一括パッチ適用）")
    st.write(
        "本番日足データ(`price_jp_1d.parquet`)とyfinance公式の分割情報を自動照合し、不整合のある銘柄を自動検出します。\n"
        "予定日の過去45日間におよぶ「面」のルックバック走査により、収集ズレや先回り調整された不整合（崖）も確実に見つけ出します。"
    )

    if st.button("🔍 日本株 統合段差スキャンを実行", key="btn_jp_split_scan", type="primary", use_container_width=True):
        with st.spinner("日本株の株式分割履歴をyfinanceから取得し、1d本番データとルックバック走査中..."):
            from core.jp_price_corrector import scan_jp_anomalies_with_yfinance
            st.session_state.jp_split_scan_result = scan_jp_anomalies_with_yfinance()
        st.success("日本株統合スキャンが完了しました。")
        st.rerun(scope="fragment")

    result_df = st.session_state.get("jp_split_scan_result")
    if result_df is None:
        return

    if result_df.empty:
        st.success("✅ 日本株本番データベース内に、未調整の株式分割不整合（崖）は検出されませんでした。")
        return

    st.warning(f"⚠️ {len(result_df)}件の不整合を検出しました（{result_df['ticker'].nunique()}銘柄）")

    display_df = result_df.copy()
    display_df["選択"] = False

    # 修正仕様書3.1に基づき、詳細情報をわかりやすく表示するための名称マッピング
    rename_map = {
        "選択": "選択",
        "ticker": "銘柄",
        "interval": "時間足",
        "ex_date": "公式予定日(ex_date)",
        "actual_date": "実質段差日(actual_date)",
        "cliff_date": "真の境界日(cliff_date)",
        "splits": "分割比率(splits)",
        "mode": "調整タイプ(mode)",
        "multiplier": "調整倍率(multiplier)",
        "before_close": "前日終値",
        "after_close": "当日終値",
        "status": "警告状態(status)"
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
            "選択": st.column_config.CheckboxColumn("選択", help="微小分割（ボラティリティ疑い）は手動目視で選択してください"),
        },
        key="jp_split_scan_editor",
    )

    selected_rows = edited_df[edited_df["選択"] == True]
    st.caption(f"現在 {len(selected_rows)} 件が選択されています。")

    if st.button("🚀 選択した日本株パッチを一括本番適用", key="btn_bulk_apply_jp_selected", type="primary", use_container_width=True, disabled=selected_rows.empty):
        status_box = st.status("📡 日本株一括修復パッチを実行中...", expanded=True)
        with status_box:
            from core.jp_price_corrector import apply_jp_patch_to_all_timeframes
            from data_access.sheets_api import save_repair_log_to_sheets
            import time

            repaired_count = 0
            log_rows = []

            for _, r in selected_rows.iterrows():
                ticker = r["銘柄"]
                # パッチ処理エンジンの不等号 "<" 処理（境界日未満を調整する）との辻褄を完璧に合わせるため、
                # 境界引数には「実質段差日(actual_date)」をそのまま渡します。これにより段差前日（真の境界日）以前が綺麗に調整されます。
                actual_date = r["実質段差日(actual_date)"]
                cliff_date = r["真の境界日(cliff_date)"]
                multiplier = r["調整倍率(multiplier)"]
                mode = r["調整タイプ(mode)"]
                status_label = r["警告状態(status)"]

                st.write(f"🔧 [{ticker}] {actual_date} より前（{cliff_date} 以前）のパッチを適用中 ({mode} / 倍率: {multiplier:.6f})...")
                results = apply_jp_patch_to_all_timeframes(ticker, actual_date, multiplier, mode, status_callback=None)
                
                applied_intervals = [iv for iv, msg in results.items() if "正常に修復" in str(msg)]
                if applied_intervals:
                    repaired_count += 1
                    st.success(f"   ✅ [{ticker}] パッチ適用完了 ({', '.join(applied_intervals)})")
                    log_rows.append({
                        "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "ticker": ticker,
                        "market": "JP",
                        "cliff_date": cliff_date,  # スプレッドシート履歴には真の境界日を書き込みます
                        "interval": ",".join(applied_intervals),
                        "before_close": r["前日終値"],
                        "after_close": r["当日終値"],
                        "multiplier": multiplier,
                        "memo": f"日本株自動分割修復パッチ ({mode} / {status_label})",
                    })
                else:
                    st.warning(f"   ⏭️ [{ticker}] スキップまたはエラーが発生しました。")

            if log_rows:
                save_repair_log_to_sheets(log_rows)
                st.write(f"📝 実際に修復された {len(log_rows)} 件のログをスプレッドシートへ記録しました。")

            status_box.update(label=f"🎉 完了：{repaired_count}件の銘柄を一括修復しました。", state="complete")
            if "jp_split_scan_result" in st.session_state:
                del st.session_state["jp_split_scan_result"]
            st.cache_data.clear() # キャッシュクリア
            time.sleep(1.0)
            st.rerun(scope="fragment")

@st.fragment
def render_parquet_data_inspector(is_jp: bool):
    """
    指定された条件（時間足・開始日・特定年月・特定銘柄）に基づいて、
    Parquetデータベースからピンポイントにデータをロードして軽量表示します。
    """
    st.divider()
    st.subheader("📊 データベース生データ確認（ピンポイント表示）")
    st.write(
        "Parquetファイルを直接ロードして、特定銘柄・特定期間の格納データをそのままグリッド表示します。\n"
        "銘柄、時間足、および「表示開始日」を絞り込むことで、古いデータから順番に追跡できます。"
    )

    # 1. 時間足の選択
    inspect_interval = st.selectbox(
        "時間足を選択", 
        ["1d", "60m", "5m", "1m"], 
        index=0, 
        key="inspect_interval_select"
    )

    # 2. 表示開始日の指定 (新設)
    inspect_start_date = st.date_input(
        "表示開始日を指定（この日付以降のデータを表示）",
        value=datetime.now().date() - timedelta(days=90),
        key="inspect_start_date_input"
    )

    # 3. 対象年・月の選択（5m / 1m の場合のみ年月プルダウンを表示）
    inspect_ym = None
    if inspect_interval in ["5m", "1m"]:
        # 現在日時から過去3年分までの年月リスト（YYYY_MM）を動的に生成
        now_dt = datetime.now()
        ym_options = []
        for year in range(now_dt.year, now_dt.year - 3, -1):
            # 現在年なら現在月まで、過去年なら12月まで
            max_month = now_dt.month if year == now_dt.year else 12
            for month in range(max_month, 0, -1):
                ym_options.append(f"{year:04d}_{month:02d}")
        
        inspect_ym = st.selectbox(
            "対象年・月を選択（Parquetファイル特定用）", 
            ym_options, 
            index=0, 
            key="inspect_ym_select"
        )

    # 4. 銘柄コードの入力
    inspect_ticker_raw = st.text_input(
        "銘柄コードを入力（1件指定）", 
        placeholder="例: 7203 や AAPL", 
        key="inspect_ticker_input"
    ).strip()

    # 5. 「データを表示」ボタン
    is_ready = bool(inspect_ticker_raw)
    btn_label = "🔍 データをロードして表示" if is_ready else "⚠️ 銘柄コードを入力してください"
    
    if st.button(btn_label, key="btn_execute_parquet_inspect", type="primary", disabled=not is_ready):
        # 銘柄コードの整形（大文字化・日本株用の末尾削除など）
        target_ticker = sanitize_ticker(inspect_ticker_raw, is_jp=is_jp)
        
        # 開始日付の文字列化 (例: "2026-07-15 00:00:00")
        start_date_filter_str = inspect_start_date.strftime("%Y-%m-%d 00:00:00")
        
        # フィルタポリシーの初期構築
        filters = [("ticker", "==", target_ticker)]
        
        # 分足（5m, 1m）の場合は、対象年月ファイル範囲と開始日の整合性を取る
        if inspect_interval in ["5m", "1m"] and inspect_ym:
            try:
                y_str, m_str = inspect_ym.split("_")
                year_val = int(y_str)
                month_val = int(m_str)
                _, last_day = calendar.monthrange(year_val, month_val)
                
                # 対象年月の初日と最終日を定義
                month_start_str = f"{year_val:04d}-{month_val:02d}-01 00:00:00"
                month_end_str = f"{year_val:04d}-{month_val:02d}-{last_day:02d} 23:59:59"
                
                # 指定開始日が選択年月の初日より前の場合は初日から、
                # 選択年月の中にある場合は指定開始日を優先させてロード範囲を決定
                actual_start_dt = max(pd.to_datetime(month_start_str), pd.to_datetime(start_date_filter_str))
                actual_start_str = actual_start_dt.strftime("%Y-%m-%d %H:%M:%S")
                
                filters.append(("date", ">=", actual_start_str))
                filters.append(("date", "<=", month_end_str))
            except Exception as e:
                st.error(f"年月範囲の解釈に失敗しました: {e}")
                return
        else:
            # 1d / 60m の場合は純粋に「指定開始日以降」をフィルターとして適用
            filters.append(("date", ">=", start_date_filter_str))

        with st.spinner(f"📥 Parquetから [{target_ticker}] のデータを検索中..."):
            try:
                # 必要最小限の列のみを投影ロード
                target_cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
                if not is_jp:
                    target_cols.extend(["adj close", "stock splits"])

                df_result = load_price_db(
                    interval=inspect_interval,
                    is_jp=is_jp,
                    is_raw=False, # Activeデータベースを参照
                    columns=target_cols,
                    filters=filters
                )

                if df_result.empty:
                    st.warning("⚠️ 指定された条件に合致するデータはデータベース内に見つかりませんでした。")
                else:
                    # 時系列順（昇順）にソートして整理
                    if "date" in df_result.columns:
                        df_result = df_result.sort_values("date").reset_index(drop=True)
                        # 表示フォーマットの整形
                        df_result["date"] = pd.to_datetime(df_result["date"]).dt.strftime("%Y-%m-%d %H:%M:%S")

                    st.success(f"✅ ロード完了（フィルタ該当件数: {len(df_result):,} 件）")
                    
                    # 💡 送信データ量抑制およびブラウザクラッシュ回避のセーフガード
                    MAX_DISPLAY_ROWS = 2000
                    if len(df_result) > MAX_DISPLAY_ROWS:
                        st.warning(
                            f"⚠️ 条件に該当するデータが多いため（{len(df_result):,}件）、"
                            f"指定開始日を起点とした先頭 {MAX_DISPLAY_ROWS:,} 件のみを表示しています。\n\n"
                            f"これより後ろの（より新しい）データを確認したい場合は、表示開始日を後ろにずらしてください。"
                        )
                        # 指定開始日を起点とした「古い順から2,000件」を表示するためにheadを適用
                        df_display = df_result.head(MAX_DISPLAY_ROWS)
                    else:
                        df_display = df_result

                    # グリッド描画
                    st.dataframe(
                        df_display, 
                        use_container_width=True, 
                        hide_index=True
                    )
            except Exception as ex:
                st.error(f"❌ データのロード中に例外が発生しました: {ex}")

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
    # 投影ロード & キャッシュ化により、1d最終更新日をミリ秒レベルで解決。市場選択切替時も完全ノーウェイト化
    last_date = get_db_last_update_cached("1d", is_jp=is_jp)
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

# 一時ファイルのコミットUI（US株選択時のみ有効化・独立フラグメントで閉域実行）
render_commit_verified_data_ui(is_jp)

# 🚀 日本株専用：手動上書きマージセンター（日本株選択時のみ増設表示・独立フラグメント）
render_jp_manual_merge_center(is_jp)

# 🚀 日本株専用：統合段差スキャン・一括自動修復（日本株選択時のみ増設表示・独立フラグメント）
render_jp_split_scan_and_repair_ui(is_jp)

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
                st.cache_data.clear() # データ同期によりキャッシュを破棄
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
                st.cache_data.clear() # アクティブ再構築に伴い更新日付などのキャッシュをクリア
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

# 5. データベース生データ確認（新規実装コンポーネント）
render_parquet_data_inspector(is_jp)