# views/maintenance.py
import os
import time
from datetime import datetime, timedelta
import pandas as pd
import streamlit as st
import re

from config import settings
from data_access.local_db import load_price_db
from data_access.sheets_api import (
    load_extra_tickers_from_sheets,
    save_extra_tickers_to_sheets,
    load_repair_log_from_sheets,
    save_repair_log_to_sheets
)
from core.collector import (
    sync_extra_tickers_to_local,
    get_all_collection_tickers,
    get_topix500_tickers,
    get_extra_tickers,
    sanitize_ticker
)
from core.screener import get_jpx_full_list
from core.database_service import (
    analyze_db_update_needs,
    update_price_database,
    repair_single_ticker_all_timeframes,
    apply_scale_repair_with_intraday_propagation,
    scan_all_anomalies,
    full_rebuild_all_database,
    run_database_health_scan,
    apply_forced_scale_patch_to_all_timeframes,
    apply_all_saved_patches
)

# ── 日足の最新更新日の簡易取得 ──
def get_db_last_update(interval: str, is_jp: bool = True) -> str:
    try:
        df = load_price_db(interval, is_jp=is_jp)
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
                            col_add_right.button("✅ 登録済", key=f"etf_add_btn_{code_str}", disabled=True, use_container_width=True)
                        else:
                            if col_add_right.button("追加", key=f"etf_add_btn_{code_str}", use_container_width=True):
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
            df_full = load_price_db(interval, is_jp=is_jp)
            df_ticker = df_full[df_full["ticker"] == ticker].copy()
            df_ticker["date"] = pd.to_datetime(df_ticker["date"])
            df_ticker = df_ticker.sort_values("date").reset_index(drop=True)
        except Exception as e:
            st.error(f"データのロード中にエラーが発生しました: {e}")
            return
            
        st.write("---")
        st.markdown(f"### 🔍 **{ticker} ({interval})** 崖の周辺データ確認")
        
        # 表示対象カラムを動的に決定 (adj close があれば追加)
        cols_to_disp = ["date", "open", "high", "low", "close"]
        if "adj close" in df_ticker.columns:
            cols_to_disp.append("adj close")
        cols_to_disp.append("volume")
        
        if len(detected_dates) == 2:
            start_date = pd.to_datetime(detected_dates[0])
            end_date = pd.to_datetime(detected_dates[1])
            duration = (end_date - start_date).days
            
            if duration > 30:
                st.info(f"📊 クレーター期間が長期（{duration}日間）にわたるため、変化の瞬間（下落時・復帰時）を切り出して左右に並べて表示します。")
                col_in, col_out = st.columns(2)
                
                with col_in:
                    st.markdown(f"📉 **崖の入り口（下落が始まったポイント）**")
                    df_in = df_ticker[
                        (df_ticker["date"] >= start_date - pd.Timedelta(days=10)) & 
                        (df_ticker["date"] <= start_date + pd.Timedelta(days=10))
                    ]
                    st.dataframe(df_in[cols_to_disp], use_container_width=True, hide_index=True)
                    
                with col_out:
                    st.markdown(f"📈 **崖の出口（正常に戻ったポイント）**")
                    df_out = df_ticker[
                        (df_ticker["date"] >= end_date - pd.Timedelta(days=10)) & 
                        (df_ticker["date"] <= end_date + pd.Timedelta(days=10))
                    ]
                    st.dataframe(df_out[cols_to_disp], use_container_width=True, hide_index=True)
            else:
                st.markdown(f"📊 **不具合の全貌（前後10日間のマージン付き）**")
                df_view = df_ticker[
                    (df_ticker["date"] >= start_date - pd.Timedelta(days=10)) & 
                    (df_ticker["date"] <= end_date + pd.Timedelta(days=10))
                ]
                st.dataframe(df_view[cols_to_disp], use_container_width=True, hide_index=True)
                
        elif len(detected_dates) == 1:
            start_date = pd.to_datetime(detected_dates[0])
            st.info("📉 最新データまで元の水域に戻っていない「永続的な段差」です。崖の前後15日間のデータを表示します。")
            df_view = df_ticker[
                (df_ticker["date"] >= start_date - pd.Timedelta(days=15)) & 
                (df_ticker["date"] <= start_date + pd.Timedelta(days=15))
            ]
            st.dataframe(df_view[cols_to_disp], use_container_width=True, hide_index=True)
            
        st.divider()
        st.markdown("#### 🛠️ **推奨される手動治療パッチの設定**")
        st.write("上記のデータを確認し、治療ツール（セクション3）に以下のように数値を入力して治療を適用してください。")
        
        if "階段段差" in anomaly_type:
            suggested_end = (base_date - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            st.code(
                f"① 銘柄コード         : {ticker}\n"
                f"② 開始日（省略可）   : (空欄にする)\n"
                f"③ 終了日（省略可）   : {suggested_end}\n"
                f"④ 修復/分割比率     : (上の崖の落差に応じて、1306.Tなら 0.1 を入力)", 
                language="text"
            )
        else:
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            st.code(
                f"① 銘柄コード         : {ticker}\n"
                f"② 開始日（省略可）   : {start_str}\n"
                f"③ 終了日（省略可）   : {end_str}\n"
                f"④ 修復/分割比率     : (元の適正価格/潰れた価格の比率。1629.Tなら 500.0 を入力)", 
                language="text"
            )

# =====================================================================
# 🛠️ メイン画面描画処理
# =====================================================================

st.title("🗄️ データベース管理・保守センター")
st.caption("自動防衛ロジックを搭載した、安全な一元データ管理システムです。")

m_col1, m_col2 = st.columns([1, 1])
with m_col1:
    market_mode = st.radio("対象市場の選択", ["日本株 🇯🇵", "米国株 🇺🇸"], horizontal=True)
    is_jp = (market_mode == "日本株 🇯🇵")
with m_col2:
    last_date = get_db_last_update("1d", is_jp=is_jp)
    st.metric(label="現在の 日足(1d) 最終更新日", value=last_date)
    
st.divider()

# ETFマスタ管理
render_etf_manager()

st.divider()

# 【セクション1】 全体差分ダウンロード（自動権利落ち防衛）
st.subheader("1️⃣ 全体差分ダウンロード（自動権利落ち防衛）")
st.write(
    "最新日までの株価データを各時間足(1m, 5m, 60m, 1d)ごとに差分収集します。"
    "配当金や株式分割などの権利落ちを自動検知すると、日足なら自動で過去全期間を再構築、短期足なら自動で価格比調整を実行します。"
)

if st.button("🔄 全体差分ダウンロードを実行", key="btn_all_diff_update", type="primary"):
    status_box = st.status("📡 データベース全体差分同期中...", expanded=True)
    with status_box:
        st.write("追加収集ティッカーのローカル同期を実行中...")
        try:
            sync_extra_tickers_to_local()
            st.write("✅ ティッカーリストの同期に成功しました。")
        except Exception as e:
            st.write(f"⚠️ ティッカー同期スキップ（キャッシュを使用）: {e}")
            
        st.write("差分情報のスキャンと必要更新箇所の算出中...")
        needs = analyze_db_update_needs(is_jp=is_jp)
        
        if needs.get("needs_period_update"):
            st.write(f"⚠️ 3日以上のデータ未同期を検出（最新日: {needs['global_max_date']}）。更新を開始します...")
        if needs.get("refetch_tickers"):
            st.write(f"🔄 未確定データの再取得対象: {len(needs['refetch_tickers'])} 銘柄")
        if needs.get("missing_tickers"):
            st.write(f"➕ 新規取得対象: {len(needs['missing_tickers'])} 銘柄")
        
        st.write("各タイムフレームの差分取得タスクを開始します...")
        
        def update_status_on_screen(msg):
            st.write(f"  * {msg}")
            
        with st.spinner("ダウンロード中..."):
            try:
                all_tickers = get_all_collection_tickers() if is_jp else ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
                update_price_database(
                    is_jp=is_jp, 
                    target_tickers=all_tickers, 
                    status_callback=update_status_on_screen
                )
                
                # --- [自動パッチ適用] 全体更新に伴い、保存済みの手動修復パッチを再適用 ---
                st.write("🔄 **【整合性自動復元】全体更新完了に伴い、保存済みの手動修復パッチを自動再適用中...**")
                apply_all_saved_patches(is_jp=is_jp, status_callback=update_status_on_screen)
                # ----------------------------------------------------------------------

                status_box.update(label="✅ 全体差分ダウンロード ＆ 補正適用完了！", state="complete")
                st.success("すべてのデータベースが正常に同期され、手動修復も自動復元されました。")
                time.sleep(0.5)
                st.rerun()
            except Exception as e:
                status_box.update(label="❌ エラーが発生しました", state="error")
                st.error(f"データベースの更新処理中に例外エラーが発生しました: {e}")

st.divider()

# 【セクション3】手動ピンポイント一括安全修復
st.subheader("3️⃣ 手動ピンポイント一括安全修復")
st.write(
    "特定の銘柄においてデータの欠損や分割による不整合が発生した場合、既存データを破壊することなく、"
    "すべての時間足（1d, 60m, 5m, 1m）に対して一括で重複排除マージまたは一律調整の治療を実行します。"
)

with st.expander("🔍 異常データスキャン（修復対象の特定）", expanded=False):
    st.caption("全銘柄の日足DBをスキャンして35%以上の急変箇所を検出します。")
    if st.button("🔍 異常スキャン実行", key="btn_anomaly_scan"):
        with st.spinner("全銘柄をスキャン中..."):
            anomalies = scan_all_anomalies(is_jp=is_jp, interval="1d")
        if anomalies.empty:
            st.success("✅ 異常箇所は検出されませんでした。")
        else:
            st.warning(f"⚠️ {len(anomalies)}件の異常箇所を検出（{anomalies['ticker'].nunique()}銘柄）")
            display_df = anomalies.copy()
            if "cliff_date" in display_df.columns:
                display_df["cliff_date"] = pd.to_datetime(display_df["cliff_date"]).dt.strftime("%Y-%m-%d")
            
            # パーセンテージ表示に変換
            if "pct_change" in display_df.columns:
                display_df["pct_change"] = display_df["pct_change"].apply(
                    lambda x: f"{x*100:.1f}%" if pd.notna(x) else "－"
                )
            
            # 推測比率のフォーマット
            if "est_multiplier" in display_df.columns:
                display_df["est_multiplier"] = display_df["est_multiplier"].apply(
                    lambda x: f"{x:.8f}".rstrip('0').rstrip('.') if pd.notna(x) else "－"
                )

            # 新カラム名マッピングにリネームして表示
            rename_map = {
                "ticker": "銘柄",
                "cliff_date": "崖日付",
                "anomaly_type": "不具合種類",
                "est_multiplier": "推測修正比率 (当日÷1日前)",
                "pct_change": "変化率",
                "before_close": "1日前 Close",
                "after_close": "Close",
                "before_adj_close": "1日前 Adj Close",
                "after_adj_close": "Adj Close",
                "open": "Open",
                "high": "High",
                "low": "Low",
                "volume": "Volume"
            }
            display_df = display_df.rename(columns=rename_map)
            
            # 整然としたカラム順を定義
            col_order = [
                "銘柄", "崖日付", "不具合種類", "推測修正比率 (当日÷1日前)", "変化率", 
                "1日前 Close", "Close", "1日前 Adj Close", "Adj Close", 
                "Open", "High", "Low", "Volume"
            ]
            valid_cols = [c for c in col_order if c in display_df.columns]
            st.dataframe(display_df[valid_cols], use_container_width=True, hide_index=True)

st.write(" ")

# UI入力欄
rep_ticker = st.text_input("安全一括修復を実行する銘柄コードを入力してください", placeholder="例: 1306 や AAPL", key="rep_ticker_box")
rep_date_str = st.text_input("修正開始日/崖日付 (空欄なら自動検知)", placeholder="例: 2026-03-30", key="rep_date_box")
rep_ratio_str = st.text_input("手動修正比率 multiplier (空欄なら自動検知)", placeholder="例: 0.1 や 3.25e-8", key="rep_ratio_box")

rep_col3_btn = st.button("🔧 安全一括修復を実行")

if rep_col3_btn:
    if not rep_ticker:
        st.error("銘柄コードが入力されていません。")
    # ─── 修正箇所: 「日付」と「倍率」の両方が空白の場合は動作させない安全ガード ───
    elif not rep_date_str.strip() and not rep_ratio_str.strip():
        st.error("❌ 誤動作防止のため、「修正開始日」と「手動修正比率」が【どちらも空白】の場合は修復処理を実行できません。どちらか片方、または両方を入力してください。")
    # ────────────────────────────────────────────────────────────────────────
    else:
        pure_t = sanitize_ticker(rep_ticker, is_jp=is_jp)
        market_str = "JP" if is_jp else "US"
        
        # ── パターンA: 崖日付と比率が明示的に入力された場合（手動ピンポイント一律調整） ──
        if rep_date_str.strip() and rep_ratio_str.strip():
            try:
                multiplier = float(rep_ratio_str.strip())
                cliff_dt = pd.to_datetime(rep_date_str.strip())
                cliff_dt_str = cliff_dt.strftime("%Y-%m-%d")
            except Exception as e:
                st.error(f"崖日付、または補正倍率の形式が不正です: {e}")
                st.stop()
                
            with st.spinner(f"🔧 [{pure_t}] の {cliff_dt_str} 以前を一律 {multiplier} 倍に補正調整中..."):
                # 1d〜1mすべての時間足にパッチを強制適用して更新
                results = apply_forced_scale_patch_to_all_timeframes(
                    pure_t, 
                    cliff_dt_str, 
                    multiplier, 
                    is_jp=is_jp
                )
                
                # 適用レポート
                st.write("### 📋 手動修復適用レポート:")
                for interval, msg in results.items():
                    icon = "✅" if "補正適用完了" in msg else "⚠️"
                    st.write(f"{icon} **{interval}**: {msg}")
                    
                # スプレッドシートにパッチ定義をマスタとして保存
                executed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                log_row = {
                    "executed_at": executed_at,
                    "ticker": pure_t,
                    "market": market_str,
                    "cliff_date": cliff_dt_str,
                    "interval": "all",
                    "before_close": "", 
                    "after_close": "",
                    "multiplier": multiplier,
                    "memo": "手動ピンポイント崖一律修復（パッチ定義）",
                }
                
                saved = save_repair_log_to_sheets([log_row])
                if saved:
                    st.success("✅ パッチ定義をスプレッドシートに保存しました。次回初期化・フル再構築時にも自動的に適用（復元）されます。")
                else:
                    st.warning("⚠️ 修復はParquetに適用されましたが、スプレッドシートへのパッチ永続化に失敗しました。")
        
        # ── パターンB: 日付または比率のいずれかが入力されている場合（自動判定と手動指定マージ修復モード） ──
        else:
            forced_ratio = None
            if rep_ratio_str.strip():
                try:
                    forced_ratio = float(rep_ratio_str.strip())
                except ValueError:
                    st.error("比率は有効な数字で入力してください。")
                    st.stop()

            with st.spinner(f"🔧 [{pure_t}] の一括修復を実行中..."):
                results_legacy = repair_single_ticker_all_timeframes(
                    pure_t,
                    is_jp=is_jp,
                    forced_split_ratio=forced_ratio
                )
                results_scale = apply_scale_repair_with_intraday_propagation(
                    pure_t,
                    is_jp=is_jp,
                    threshold=0.35,
                    dry_run=False
                )

                # 結果レポート
                st.write("### 📋 自動修復適用レポート:")
                for interval, msg in results_legacy.items():
                    icon = "✅" if "修復成功" in msg or "置換" in msg or "再構築" in msg else "⚠️"
                    st.write(f"{icon} **{interval}** (マージ修復): {msg}")

                repair_details = results_scale.pop("repair_details", [])
                results_scale.pop("ticker", None)
                for interval, msg in results_scale.items():
                    icon = "✅" if "修正" in msg or "波及" in msg else "ℹ️"
                    st.write(f"{icon} **{interval}** (崖修復): {msg}")

                # スプレッドシートへ自動修復内容を保存
                executed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if repair_details:
                    log_rows = [
                        {
                            "executed_at": executed_at,
                            "ticker": pure_t,
                            "market": market_str,
                            "cliff_date": r["cliff_date"].strftime("%Y-%m-%d") if hasattr(r["cliff_date"], "strftime") else str(r["cliff_date"]),
                            "interval": "1d→all",
                            "before_close": r.get("before_close", ""),
                            "after_close": r.get("after_close", ""),
                            "multiplier": r.get("multiplier", ""),
                            "memo": "自動崖検知補正（閾値35%）",
                        }
                        for r in repair_details
                    ]
                    save_repair_log_to_sheets(log_rows)
                else:
                    save_repair_log_to_sheets([{
                        "executed_at": executed_at,
                        "ticker": pure_t,
                        "market": market_str,
                        "cliff_date": rep_date_str.strip() if rep_date_str.strip() else "",
                        "interval": "all",
                        "before_close": "",
                        "after_close": "",
                        "multiplier": forced_ratio if forced_ratio else "",
                        "memo": "通常マージ自動修復のみ（自動調整）",
                    }])
                st.success("✅ 自動判定による修復処理およびログ保存が完了しました。")

st.write(" ")
st.divider()

# ── 【新規追加】指定日以前データ部分削除パッチUI ──
st.markdown("#### 🗑️ 指定日以前データ一括物理削除パッチ")
st.write(
    "SBI新生銀行（8303）などの再上場銘柄において、過去の上場廃止前の不要な歴史データや"
    "取引のない数年間の空白期間を、1d〜1mすべての時間足のDBから完全に物理削除します。"
)

del_col1, del_col2, del_col3 = st.columns([3, 2, 1])
with del_col1:
    del_ticker = st.text_input("データ削除を実行する銘柄コード", placeholder="例: 8303 や 1306", key="del_ticker_box")
with del_col2:
    del_date_str = st.text_input("削除の境界となる日付 (この日以前をすべて消去)", placeholder="例: 2025-12-16", key="del_date_box")
with del_col3:
    st.write(" ")
    st.write(" ")
    btn_delete_before = st.button("🗑️ 指定日以前を物理削除", use_container_width=True, type="primary")

if btn_delete_before:
    if not del_ticker:
        st.error("銘柄コードが入力されていません。")
    elif not del_date_str:
        st.error("削除の境界となる基準日付が入力されていません。")
    else:
        try:
            # 入力日付のフォーマット簡易バリデーション
            pd.to_datetime(del_date_str)
        except ValueError:
            st.error("日付は有効な形式（例: YYYY-MM-DD）で入力してください。")
            st.stop()
            
        pure_t = sanitize_ticker(del_ticker, is_jp=is_jp)
        with st.spinner(f"🗑️ [{pure_t}] の {del_date_str} 以前のデータを全時間足から物理削除中..."):
            from core.database_service import delete_data_before_date
            del_results = delete_data_before_date(pure_t, del_date_str, is_jp=is_jp)
            
            st.write("### 📋 削除完了レポート:")
            for interval, msg in del_results.items():
                icon = "✅" if "正常に" in msg else "ℹ️" if "なし" in msg else "⚠️"
                st.write(f"{icon} **{interval}**: {msg}")
            
            # 修復ログへ記録を保存
            from datetime import datetime
            from data_access.sheets_api import save_repair_log_to_sheets
            executed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            market_str = "JP" if is_jp else "US"
            save_repair_log_to_sheets([{
                "executed_at": executed_at,
                "ticker": pure_t,
                "market": market_str,
                "cliff_date": del_date_str,
                "interval": "all_timeframes",
                "before_close": "",
                "after_close": "",
                "multiplier": "",
                "memo": f"手動削除パッチ実行（指定日以前の全消去）",
            }])
            st.success("✅ 削除処理とログの保存が正常に完了しました。")

with st.expander("📋 修復ログ一覧", expanded=False):
    st.caption("スプレッドシートに保存された過去の修復履歴を表示します。")
    log_col1, log_col2 = st.columns([1, 1])
    with log_col1:
        log_ticker_filter = st.text_input("銘柄コードで絞り込み", placeholder="例: 1629", key="log_filter_ticker")
    with log_col2:
        st.write(" ")
        btn_load_log = st.button("🔄 ログを読み込む", key="btn_load_log", use_container_width=True)

    if btn_load_log:
        with st.spinner("ログを読み込み中..."):
            log_df = load_repair_log_from_sheets()

        if log_df.empty:
            st.info("修復ログがありません。")
        else:
            if log_ticker_filter.strip():
                log_df = log_df[log_df["ticker"].astype(str).str.contains(log_ticker_filter.strip(), case=False, na=False)]

            if log_df.empty:
                st.info(f"「{log_ticker_filter}」のログはありません。")
            else:
                disp = log_df.copy()
                if "executed_at" in disp.columns:
                    disp["executed_at"] = pd.to_datetime(disp["executed_at"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
                if "cliff_date" in disp.columns:
                    disp["cliff_date"] = pd.to_datetime(disp["cliff_date"], errors="coerce").dt.strftime("%Y-%m-%d")
                if "multiplier" in disp.columns:
                    disp["multiplier"] = pd.to_numeric(disp["multiplier"], errors="coerce").round(6)
                disp.columns = ["実行日時", "銘柄", "市場", "崖日付", "適用時間足", "修正前終値", "修正後終値", "倍率", "備考"]
                st.dataframe(disp, use_container_width=True, hide_index=True)

# ── 手動パッチ全適用UIコンポーネント ──
st.write(" ")
st.markdown("#### 🔄 **保存済みパッチの冪等（べきとう）復元システム**")
st.caption(
    "データベースを白紙初期化したり、yfinanceからフル再構築した場合、手動修正した履歴がすべて消えてしまいます。"
    "以下のボタンを押すことで、スプレッドシートに記録された過去のすべての手動パッチ（崖・倍率）を"
    "「新しい日付順（降順）」に自動ソートし、1dから1mのすべての時間足に対して一括で自動適用（復元）します。"
)

if st.button("🔄 保存されているすべてのパッチを一括再適用して復元", key="btn_apply_all_patches_manual", type="secondary"):
    status_box = st.status("📡 パッチマスタの一括再適用中...", expanded=True)
    with status_box:
        def update_patch_status(msg):
            st.write(msg)
        try:
            count = apply_all_saved_patches(is_jp=is_jp, status_callback=update_patch_status)
            if count > 0:
                status_box.update(label=f"✅ {count}件のパッチ再適用が完了しました！", state="complete")
                st.success(f"データベースの整合性が過去のパッチ定義に基づいて元通り復元されました。")
            else:
                status_box.update(label="🧊 適用対象の有効なパッチはありませんでした。", state="complete")
        except Exception as e:
            status_box.update(label="❌ エラーが発生しました", state="error")
            st.error(f"パッチの一括適用中にエラーが発生しました: {e}")

st.divider()

# 【セクション4】 全件一括フルダウンロード・再構築
st.subheader("4️⃣ 全件一括フルダウンロード・再構築（初期化・デバッグ用）")
st.write(
    "既存データベースを一度完全に削除し、yfinanceの提供限界から一発でフル構築し直す「白紙初期化ボタン」です。"
)

fb_col1, fb_col2 = st.columns([2, 1])
with fb_col1:
    rebuild_interval = st.selectbox(
        "一括再構築する時間足（タイムフレーム）を選択してください", 
        ["1m", "5m", "60m", "1d"], 
        index=3, 
        key="rebuild_interval_select"
    )
with fb_col2:
    st.write(" ")
    st.write(" ")
    btn_full_rebuild = st.button(
        "💥 一括フルダウンロードを実行", 
        use_container_width=True, 
        type="primary"
    )
    
if btn_full_rebuild:
    status_box = st.status(f"📡 {market_mode} {rebuild_interval} データベースを一括クリーンビルド中...", expanded=True)
    with status_box:
        st.write("🔄 **スプレッドシートから最新ティッカーの同期を試行します**")
        codes_in_json, sync_error = sync_extra_tickers_to_local()
        if sync_error:
            st.error(f"  * ❌ 同期に失敗しました: {sync_error}")
        else:
            st.success(f"  * ✅ 同期に成功しました。追加ティッカー数: {len(codes_in_json)} 件")
        
        st.write("既存のParquetファイルをクリア中...")
        filename = f"price_{'jp' if is_jp else 'us'}_{rebuild_interval}.parquet"
        work_file = os.path.join(settings.WORK_DIR, filename)
        if os.path.exists(work_file):
            try:
                os.remove(work_file)
                st.write("🗑️ 既存ファイルを正常に削除しました。")
            except Exception as e:
                st.write(f"⚠️ 既存ファイルの削除に失敗: {e}")
        
        st.write("yfinanceからバッチダウンロードを開始します（レート制限防止のために時間がかかります）...")
        
        def update_rebuild_status(msg):
            st.write(msg)
            
        try:
            success = full_rebuild_all_database(
                is_jp=is_jp, 
                interval=rebuild_interval, 
                status_callback=update_rebuild_status
            )
            if success:
                # --- [自動パッチ適用] フル再構築後に自動的にパッチを一括適用して整合性を復元 ---
                st.write("🔄 **【整合性自動復元】フル再構築完了に伴い、保存済みの手動修復パッチを再適用中...**")
                apply_all_saved_patches(is_jp=is_jp, status_callback=update_rebuild_status)
                # ----------------------------------------------------------------------
                
                status_box.update(label="✅ 一括フルダウンロード完了！", state="complete")
                st.success(f"{rebuild_interval} データベースの再構築とパッチ復元が完了しました！")
                time.sleep(0.5)
                st.rerun()
            else:
                status_box.update(label="❌ ダウンロード失敗", state="error")
                st.error("データを取得できませんでした。")
        except Exception as e:
            status_box.update(label="❌ エラー発生", state="error")
            st.error(f"再構築中に予期せぬエラーが発生しました: {e}")

# 健康診断UIの表示
render_database_diagnostics_ui(is_jp=is_jp)