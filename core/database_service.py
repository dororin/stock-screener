# core/database_service.py
import os
import sys
import time
import pandas as pd
import pytz
import yfinance as yf
from datetime import datetime, timedelta, time as dt_time
from config import settings
from data_access.local_db import load_price_db, save_price_db
# 新設した米国株専用補正モジュールからインポート
from core.us_price_corrector import (
    parse_yfinance_batch,
    adjust_ticker_splits_backward_in_memory,
    apply_saved_patches_to_df,
    finalize_latest_with_tradingview_in_df
)

# --- yfinanceが取得可能な期間の上限（US専用日数制限） ---
YFINANCE_GAP_LIMITS = {"1m": 7, "5m": 60, "60m": 730}

def get_market_localized_now(is_jp: bool = False):
    # 米国株を前提とするため基本はNY時間
    tz = pytz.timezone("Asia/Tokyo") if is_jp else pytz.timezone("America/New_York")
    now_tz = datetime.now(pytz.utc).astimezone(tz)
    local_today = now_tz.date()
    return now_tz, local_today

def compute_is_finalized(date_series: pd.Series, interval: str, is_jp: bool = False) -> pd.Series:
    now_tz, local_today = get_market_localized_now(is_jp)
    dt_series = pd.to_datetime(date_series)

    if interval == "1d":
        close_buffer_time = dt_time(17, 30) # US市場クローズバッファ
        today_is_finalized = now_tz.time() >= close_buffer_time

        data_dates = dt_series.dt.date
        is_finalized = data_dates < local_today
        if today_is_finalized:
            is_finalized = is_finalized | (data_dates == local_today)
        return is_finalized
    else:
        now_naive = now_tz.replace(tzinfo=None)
        return dt_series < (now_naive - timedelta(hours=1))

def check_processed_data_health(old_df: pd.DataFrame, new_df: pd.DataFrame) -> list:
    alerts = []
    if old_df is None or old_df.empty or new_df is None or new_df.empty:
        return alerts

    old_cnt = len(old_df)
    new_cnt = len(new_df)
    if new_cnt < old_cnt * 0.95:
        alerts.append(f"⚠️ [行数激減] 行数が減少しています: {old_cnt:,} ➔ {new_cnt:,} (減少率: {(1 - new_cnt/old_cnt)*100:.1f}%)")

    old_tickers = set(old_df["ticker"].unique())
    new_tickers = set(new_df["ticker"].unique())
    missing_tickers = old_tickers - new_tickers
    if missing_tickers:
        alerts.append(f"⚠️ [銘柄消失] 以下の銘柄がデータから消失しています: {list(missing_tickers)[:10]}")

    common_tickers = old_tickers & new_tickers
    if common_tickers:
        sample_tickers = list(common_tickers)[:30]
        o_sample = old_df[old_df["ticker"].isin(sample_tickers)].sort_values(["ticker", "date"])
        n_sample = new_df[new_df["ticker"].isin(sample_tickers)].sort_values(["ticker", "date"])
        
        merged = pd.merge(
            o_sample[["date", "ticker", "close"]], 
            n_sample[["date", "ticker", "close"]], 
            on=["date", "ticker"], 
            suffixes=("_old", "_new")
        )
        if not merged.empty:
            merged["ratio"] = merged["close_new"] / merged["close_old"]
            crazy_changes = merged[(merged["ratio"] >= 10.0) | (merged["ratio"] <= 0.1)]
            if not crazy_changes.empty:
                sample_crazy = crazy_changes.iloc[0]
                alerts.append(
                    f"🚨 [異常価格変化] 加工前後の価格比率が異常です。 "
                    f"銘柄: {sample_crazy['ticker']}, 日付: {sample_crazy['date']}, "
                    f"旧Close: {sample_crazy['close_old']:.2f} ➔ 新Close: {sample_crazy['close_new']:.2f}"
                )
    return alerts


# =====================================================================
# 🛠️ 米国株専用 Raw & Active 2層同期およびアサーション検証エンジン
# =====================================================================

def rebuild_active_from_raw(interval: str, is_jp: bool = False, dry_run: bool = False, skip_assertion: bool = False, status_callback=None, log_accumulator: list = None, repair_log_df: pd.DataFrame = None, raw_df: pd.DataFrame = None) -> bool:
    """Rawデータから米国株Activeデータの加工ビルドとパッチ適用・検証を実行します。"""
    import gc
    
    def log(msg):
        print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [REBUILD_ACTIVE] {msg}")
        sys.stdout.flush()
        if log_accumulator is not None:
            log_accumulator.append(f"[{datetime.now().strftime('%H:%M:%S')}] [REBUILD_{interval}] {msg}")
        if settings.HAS_STREAMLIT:
            import streamlit as st
            if "sync_logs_history" not in st.session_state:
                st.session_state["sync_logs_history"] = []
            st.session_state["sync_logs_history"].append(f"[{datetime.now().strftime('%H:%M:%S')}] [REBUILD_{interval}] {msg}")
        if status_callback: 
            try:
                status_callback(msg)
            except Exception:
                pass

    if is_jp:
        log("⚠️ 日本株(JP)は楽天RSSが直接Active DBを構築するため、再構築は不要です（スキップ完了）。")
        return True

    log(f"🏗️ [{interval}] 米国株RawデータからActiveデータの加工ビルドを開始します...")
    
    if raw_df is not None:
        df_raw = raw_df.copy()
        log("💡 メモリ上に展開済みのRawデータをインメモリ再利用します。")
    else:
        df_raw = load_price_db(interval, is_jp=False, is_raw=True)
        
    if df_raw.empty:
        log("❌ 米国株Rawデータベースファイルが空、または検出されません。")
        return False

    log(f"Rawデータのロード完了。サイズ: {df_raw.shape}, ユニーク銘柄数: {df_raw['ticker'].nunique()}")

    # 最終確定フラグ計算
    df_raw["is_finalized"] = compute_is_finalized(df_raw["date"], interval, is_jp=False)

    if "split_multiplier" not in df_raw.columns:
        df_raw["split_multiplier"] = 1.0
    if "patched_multiplier" not in df_raw.columns:
        df_raw["patched_multiplier"] = 1.0

    # 株式分割の検知と補正
    split_tickers = []
    if "stock splits" in df_raw.columns:
        split_tickers = df_raw[(df_raw["stock splits"] > 0) & (df_raw["stock splits"] != 1.0)]["ticker"].unique().tolist()

    if split_tickers:
        log(f"米国株株式分割を検知しました。対象銘柄数: {len(split_tickers)} / {df_raw['ticker'].nunique()}")
        price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df_raw.columns]
        cols_to_write = price_cols + ["volume", "split_multiplier", "patched_multiplier"]
        
        total_split_tickers = len(split_tickers)
        for idx, ticker in enumerate(split_tickers):
            ticker_mask = df_raw["ticker"] == ticker
            if not ticker_mask.any():
                continue
                
            group = df_raw[ticker_mask].copy().sort_values("date")
            adjusted_group, applied_splits = adjust_ticker_splits_backward_in_memory(group)
            
            if not adjusted_group.empty:
                if applied_splits:
                    for s_info in applied_splits:
                        split_date_str = pd.to_datetime(s_info["date"]).strftime("%Y-%m-%d %H:%M")
                        log(f"  👉 【株式分割補正適用】銘柄: {ticker} | 実施日: {split_date_str} | 比率: {s_info['ratio']}")
                
                adjusted_group.index = group.index
                valid_cols = [c for c in cols_to_write if c in df_raw.columns and c in adjusted_group.columns]
                df_raw.loc[adjusted_group.index, valid_cols] = adjusted_group[valid_cols]
                
        df_processed = df_raw
    else:
        log("米国株全体に株式分割イベントは検出されませんでした。")
        df_processed = df_raw
        
    gc.collect()

    # 崖パッチの適用
    if not skip_assertion:
        log("パッチ定義を反映中...")
        df_processed = apply_saved_patches_to_df(df_processed, repair_log_df=repair_log_df)

    # 補足：米国株においてストップ高安時間足補完(比例配分)は発生しないため処理は削除

    # TradingView確定値の反映
    if not dry_run:
        log("保存前にTradingView確定値をマージ適用中...")
        df_processed = finalize_latest_with_tradingview_in_df(df_processed, interval)

    # 健康診断チェック
    if not skip_assertion:
        try:
            df_old_active = load_price_db(interval, is_jp=False, is_raw=False)
        except FileNotFoundError:
            df_old_active = pd.DataFrame()

        alerts = check_processed_data_health(df_old_active, df_processed)
        if alerts:
            log("💥 【警告】ビルド後の健康診断チェックで異常を検出しました:")
            for alert in alerts:
                log(f"   {alert}")
            if any("🚨" in a for a in alerts):
                log("🛑 深刻なデータ不整合を検出したため、同期を安全に強制中断しました。")
                return False
        del df_old_active
        gc.collect()

    if dry_run:
        log(f"🧪 [DRY RUN] {interval} 加工・検証を正常に通過。ディスク（_temp）に一時保存します。")
        df_processed = df_processed.sort_values(["ticker", "date"]).reset_index(drop=True)
        local_success, local_msg = save_price_db(df_processed, interval, is_jp=False, is_raw=False, is_temp=True)
        
        if settings.HAS_STREAMLIT:
            import streamlit as st
            st.session_state[f"temp_verified_active_preview_{interval}"] = df_processed.head(100).copy(deep=True)
            st.session_state[f"temp_verified_active_exists_{interval}"] = True
            
        del df_processed
        gc.collect()
        return True
    else:
        log("加工済データの保存およびGoogleドライブ同期を実行中...")
        df_processed = df_processed.sort_values(["ticker", "date"]).reset_index(drop=True)
        cloud_success, cloud_msg = save_price_db(df_processed, interval, is_jp=False, is_raw=False)
        
        del df_processed
        gc.collect()
        if cloud_success:
            log(f"✅ [{interval}] 米国株ActiveデータベースをGoogleドライブへ正常に保存・同期しました。")
        else:
            log(f"⚠️ [{interval}] Googleドライブ同期に失敗しました。ローカル保存のみ完了。エラー: {cloud_msg}")
        return True


# =====================================================================
# 📥 米国株専用 Rawデータ更新 ＆ 統合同期システム
# =====================================================================

def update_raw_database(is_jp: bool = False, target_tickers: list = None, force_refetch: bool = False, status_callback=None, target_interval: str = None, log_accumulator: list = None):
    """yfinanceから米国株のRawデータ差分を取得・保存します。"""
    if is_jp:
        return

    tickers = target_tickers if target_tickers else []
    def log(msg):
        print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [UPDATE_RAW] {msg}")
        sys.stdout.flush()
        if log_accumulator is not None:
            log_accumulator.append(f"[{datetime.now().strftime('%H:%M:%S')}] [UPDATE_RAW_{interval}] {msg}")
        if settings.HAS_STREAMLIT:
            import streamlit as st
            if "sync_logs_history" not in st.session_state:
                st.session_state["sync_logs_history"] = []
            st.session_state["sync_logs_history"].append(f"[{datetime.now().strftime('%H:%M:%S')}] [UPDATE_RAW_{interval}] {msg}")
        if status_callback: 
            try:
                status_callback(msg)
            except Exception:
                pass
            
    if not tickers:
        log("❌ 更新対象の米国株銘柄が指定されていません。")
        return

    now_tz, local_today = get_market_localized_now(is_jp=False)
    now = now_tz.replace(tzinfo=None)
    tickers = [str(t).strip().upper() for t in tickers]

    timeframes_to_run = [target_interval] if target_interval else settings.TIMEFRAMES

    for interval in timeframes_to_run:
        log(f"⏱️ 【米国株】{interval} Rawデータ差分取得判定を開始...")
        
        from data_access.local_db import load_price_db_ledger
        ledger = load_price_db_ledger(interval, is_jp=False, is_raw=True)
        db_max_date_str = ledger.get("db_max_date")
        db_max_date = pd.to_datetime(db_max_date_str) if db_max_date_str else None
        
        last_updates_map = ledger.get("last_updates_map", {}) if ledger else {}

        # 差分取得開始日の特定ロジック
        active_timestamps = [pd.to_datetime(last_updates_map[t]) for t in tickers if t in last_updates_map]
        base_time = pd.Series(active_timestamps).mode()[0] if active_timestamps and not force_refetch else None

        if interval == "1m": max_delay = timedelta(hours=4)
        elif interval == "5m": max_delay = timedelta(hours=12)
        elif interval == "60m": max_delay = timedelta(days=2)
        else: max_delay = timedelta(days=10)
        future_tolerance = timedelta(minutes=5)

        group_A_tickers, group_B_tickers, group_C_tickers = [], [], []
        group_B_timestamps = []

        for t in tickers:
            t_last = last_updates_map.get(t)
            if t_last is None or force_refetch:
                group_C_tickers.append(t)
                continue
            t_last_dt = pd.to_datetime(t_last)
            if base_time is None:
                group_C_tickers.append(t)
            else:
                delay = base_time - t_last_dt
                if -future_tolerance <= delay <= timedelta(0):
                    group_A_tickers.append(t)
                elif timedelta(0) < delay <= max_delay:
                    group_B_tickers.append(t)
                    group_B_timestamps.append(t_last_dt)
                else:
                    group_C_tickers.append(t)

        groups = {}
        if group_A_tickers:
            groups[base_time] = group_A_tickers
        if group_B_tickers and group_B_timestamps:
            oldest_b_time = min(group_B_timestamps)
            rounded_time = oldest_b_time.floor("30min") if interval in ["1m", "5m"] else oldest_b_time.floor("h") if interval == "60m" else oldest_b_time.floor("D")
            groups[rounded_time] = group_B_tickers

        for t in group_C_tickers:
            t_last = last_updates_map.get(t)
            t_key = pd.to_datetime(t_last) if t_last is not None else None
            groups.setdefault(t_key, []).append(t)

        all_downloaded = []
        for group_idx, (t_last, chunk_tickers) in enumerate(groups.items()):
            if t_last is None:
                if interval == "1m": start_date_dt = now - timedelta(days=6)
                elif interval == "5m": start_date_dt = now - timedelta(days=58)
                elif interval == "60m": start_date_dt = now - timedelta(days=718)
                else: start_date_dt = datetime(2016, 1, 1)
                start_date_str = start_date_dt.strftime("%Y-%m-%d")
            else:
                start_date_dt = t_last
                start_date_str = start_date_dt.strftime("%Y-%m-%d")

            if interval in YFINANCE_GAP_LIMITS:
                limit_days = YFINANCE_GAP_LIMITS[interval]
                try:
                    gap_start_date = start_date_dt.date() if hasattr(start_date_dt, "date") else None
                except Exception:
                    gap_start_date = None

                if gap_start_date is not None:
                    gap_days = (local_today - gap_start_date).days
                    if gap_days > limit_days:
                        sample_tickers = ", ".join(chunk_tickers[:5]) + ("..." if len(chunk_tickers) > 5 else "")
                        log(
                            f"⚠️【警告】[{interval}] {sample_tickers} の空白期間が {gap_days} 日となり、"
                            f"yfinance上限を超えたため差分同期できません。フル再ダウンロードを実行してください。"
                        )
                        continue

            BATCH_SIZE = 30
            for i in range(0, len(chunk_tickers), BATCH_SIZE):
                chunk = chunk_tickers[i:i+BATCH_SIZE]
                
                try:
                    df_raw = yf.download(
                        chunk, 
                        start=start_date_str,
                        interval=interval, 
                        auto_adjust=False, 
                        actions=True, 
                        progress=False, 
                        threads=False, 
                        timeout=30
                    )
                    
                    if not df_raw.empty:
                        chunk_processed = parse_yfinance_batch(df_raw, chunk)
                        if not chunk_processed.empty:
                            all_downloaded.append(chunk_processed)
                except Exception as e:
                    log(f"     Batch Error: {e}")
                time.sleep(1.5)

        if all_downloaded:
            new_combined = pd.concat(all_downloaded, ignore_index=True)
            filtered_parts = []
            for ticker, group in new_combined.groupby("ticker"):
                t_last = last_updates_map.get(ticker)
                if t_last is not None:
                    group = group[pd.to_datetime(group["date"]) >= pd.to_datetime(t_last)]
                filtered_parts.append(group)
            
            if filtered_parts:
                new_combined = pd.concat(filtered_parts, ignore_index=True)
            else:
                new_combined = pd.DataFrame()
            
            # 米国株で株式分割を検知した場合は1dをフル再構成
            detected_split_tickers = []
            if interval == "1d" and not new_combined.empty:
                if "stock splits" in new_combined.columns:
                    split_rows = new_combined[(new_combined["stock splits"] > 0) & (new_combined["stock splits"] != 1.0)]
                    if not split_rows.empty:
                        detected_split_tickers = split_rows["ticker"].unique().tolist()
                        for st_ticker in detected_split_tickers:
                            log(f"🔔 [US分割検知] 米国株 {st_ticker} に分割を検知。1d Rawをフル再取得します。")
                            rebuild_single_ticker_1d_raw(st_ticker)
            
            if not new_combined.empty:
                try:
                    df_raw_db = load_price_db(interval, is_jp=False, is_raw=True)
                except FileNotFoundError:
                    df_raw_db = pd.DataFrame()
                
                if not df_raw_db.empty:
                    df_raw_db = pd.concat([df_raw_db, new_combined], ignore_index=True)
                    df_raw_db = df_raw_db.drop_duplicates(subset=["date", "ticker"], keep="last")
                else:
                    df_raw_db = new_combined
                
                df_raw_db = df_raw_db.sort_values(["ticker", "date"]).reset_index(drop=True)
                cloud_success, cloud_msg = save_price_db(df_raw_db, interval, is_jp=False, is_raw=True)
                
                if cloud_success:
                    log(f"  📥 米国株Rawデータ差分保存完了。({len(new_combined):,}件追加)")
                    if not detected_split_tickers:
                        log(f"  🛠️ 差分データをActiveにインクリメンタル適用中...")
                        incremental_update_active(interval, new_raw_diff=new_combined)
                    else:
                        log(f"  🛠️ 分割発生に伴い、遡及ビルドを実行中...")
                        partial_rebuild_active_for_tickers(interval, detected_split_tickers)
                else:
                    log(f"  ⚠️ Rawデータの保存同期に失敗しました。エラー: {cloud_msg}")
            else:
                log(f"  📥 yfinanceからの新規米国株データはありません。")
        else:
            log(f"  📥 yfinanceからの米国株データはありません。")


def update_price_database(is_jp: bool = False, target_tickers: list = None, force_refetch: bool = False, status_callback=None, dry_run: bool = False):
    """米国株データベース専用の更新・再構築統合パイプラインです。"""
    import gc
    from data_access.sheets_api import upload_sync_log_to_drive
    
    if is_jp:
        return

    if settings.HAS_STREAMLIT:
        import streamlit as st
        st.session_state["sync_logs_history"] = []
        
    accumulated_logs = []
    
    def log(msg):
        print(f"[CONSOLE_DEBUG] [UPDATE_PROCESS] {msg}")
        sys.stdout.flush()
        accumulated_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] [PROCESS] {msg}")
        if settings.HAS_STREAMLIT:
            if "sync_logs_history" not in st.session_state:
                st.session_state["sync_logs_history"] = []
            st.session_state["sync_logs_history"].append(f"[{datetime.now().strftime('%H:%M:%S')}] [PROCESS] {msg}")
        if status_callback: 
            status_callback(msg)

    log("📡 1. 【米国株】修復パッチ定義の事前キャッシュロード中...")
    try:
        from data_access.sheets_api import load_repair_log_from_sheets
        repair_log_df = load_repair_log_from_sheets()
        log("   ✅ スプレッドシートからのロード完了。インメモリで共有します。")
    except Exception as e:
        repair_log_df = None
        log(f"   ⚠️ 事前キャッシュ取得失敗、個別ロードを行います: {e}")

    # 1d足
    log("📡 2. 【米国株 日足 (1d)】Rawデータ差分取得を開始...")
    update_raw_database(is_jp=False, target_tickers=target_tickers, force_refetch=force_refetch, status_callback=status_callback, target_interval="1d", log_accumulator=accumulated_logs)

    log("🛠️ 3. 【米国株 日足 (1d)】Activeデータ加工・パッチ適用ビルド中...")
    rebuild_active_from_raw("1d", is_jp=False, dry_run=dry_run, skip_assertion=False, status_callback=status_callback, log_accumulator=accumulated_logs, repair_log_df=repair_log_df)
    gc.collect()

    # 分足
    intraday_timeframes = [tf for tf in settings.TIMEFRAMES if tf != "1d"]
    for interval in intraday_timeframes:
        log(f"⏱️ 【米国株 {interval}】の同期・ビルド処理中...")
        update_raw_database(is_jp=False, target_tickers=target_tickers, force_refetch=force_refetch, status_callback=status_callback, target_interval=interval, log_accumulator=accumulated_logs)
        rebuild_active_from_raw(interval, is_jp=False, dry_run=dry_run, skip_assertion=False, status_callback=status_callback, log_accumulator=accumulated_logs, repair_log_df=repair_log_df)
        gc.collect()

    log("✨ 米国株データベースの更新・再構築がすべて正常終了しました。")
    
    try:
        upload_sync_log_to_drive(accumulated_logs, is_jp=False, prefix="sync_dryrun" if dry_run else "sync")
    except Exception:
        pass


def execute_apply_verified_temp_dbs_to_active(is_jp: bool = False, status_callback=None) -> dict:
    """Dry Runで検証完了した一時Parquet（_temp）を米国株Activeとして本番確定させます。"""
    import gc
    from data_access.local_db import promote_temp_db_to_active
    from data_access.sheets_api import upload_sync_log_to_drive
    
    if is_jp:
        return {}

    accumulated_logs = []
    def log(msg):
        print(f"[CONSOLE_DEBUG] [APPLY_ACTIVE] {msg}")
        sys.stdout.flush()
        accumulated_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] [APPLY] {msg}")
        if settings.HAS_STREAMLIT:
            import streamlit as st
            if "sync_logs_history" not in st.session_state:
                st.session_state["sync_logs_history"] = []
            st.session_state["sync_logs_history"].append(f"[{datetime.now().strftime('%H:%M:%S')}] [APPLY] {msg}")
        if status_callback:
            try:
                status_callback(msg)
            except Exception:
                pass
                
    log("🚀 [US本番適用] 検証済み一時ファイルをGoogleドライブへ一括アップロードして確定します。")
    
    results = {}
    success_count = 0
    
    for interval in settings.TIMEFRAMES:
        success, msg = promote_temp_db_to_active(interval, is_jp=False)
        results[interval] = {"success": success, "message": msg}
        
        if success:
            success_count += 1
            if settings.HAS_STREAMLIT:
                import streamlit as st
                if f"temp_verified_active_exists_{interval}" in st.session_state:
                    st.session_state[f"temp_verified_active_exists_{interval}"] = False
                if f"temp_verified_active_preview_{interval}" in st.session_state:
                    del st.session_state[f"temp_verified_active_preview_{interval}"]
            log(f"   ✅ [{interval}] 米国株本番適用が正常に完了しました。")
        else:
            log(f"   ❌ [{interval}] 米国株本番適用に失敗しました: {msg}")
            
    gc.collect()
    try:
        upload_sync_log_to_drive(accumulated_logs, is_jp=False, prefix="apply_active")
    except Exception:
        pass
        
    return results


# =====================================================================
# 💥 クリーンビルド（米国株専用の完全再ダウンロード再構築）
# =====================================================================

def full_rebuild_all_database(is_jp: bool = False, interval: str = "1d", status_callback=None, dry_run: bool = False) -> bool:
    """米国株専用：ディスク情報を物理クリアし、yfinanceの限界までクリーンビルドします。"""
    def log(msg):
        print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [FULL_REBUILD] {msg}")
        sys.stdout.flush()
        if status_callback: status_callback(msg)

    if is_jp:
        log("⚠️ 日本株(JP)は楽天RSSが管轄するため、一括リビルドは不要です。")
        return False

    tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
    
    tickers = [str(t).strip().upper() for t in tickers]
    now = datetime.now()
    
    if interval == "1m": start_date_dt = now - timedelta(days=6)
    elif interval == "5m": start_date_dt = now - timedelta(days=58)
    elif interval == "60m": start_date_dt = now - timedelta(days=718)
    else: start_date_dt = datetime(2016, 1, 1)
        
    log(f"🚨 [USフル再構築] ({interval}) Rawデータダウンロード開始。対象: {len(tickers)} 銘柄")
    
    all_downloaded = []
    failed_tickers = []
    
    BATCH_SIZE = 30
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i+BATCH_SIZE]
        log(f"  📥 ダウンロード中 ({i + 1}〜{min(i + BATCH_SIZE, len(tickers))}): {', '.join(chunk[:5])}...")
        
        try:
            df_raw = yf.download(
                chunk,
                start=start_date_dt.strftime("%Y-%m-%d"),
                interval=interval,
                auto_adjust=False,
                actions=True,
                progress=False,
                threads=False,
                timeout=30
            )
            
            if not df_raw.empty:
                chunk_processed = parse_yfinance_batch(df_raw, chunk)
                if not chunk_processed.empty:
                    all_downloaded.append(chunk_processed)
                    downloaded_tickers = chunk_processed["ticker"].unique().tolist()
                    missing = [t for t in chunk if t not in downloaded_tickers]
                    if missing:
                        failed_tickers.extend(missing)
                else:
                    failed_tickers.extend(chunk)
            else:
                failed_tickers.extend(chunk)
        except Exception as e:
            log(f"    -> ⚠️ エラー: {e}")
            failed_tickers.extend(chunk)
        time.sleep(1.5)
        
    if failed_tickers:
        failed_tickers = list(set(failed_tickers))
        log(f"🔄 【自動リトライ】ダウンロード失敗した {len(failed_tickers)} 銘柄のリカバリ中...")
        
        retry_batch_size = 5
        for r_i in range(0, len(failed_tickers), retry_batch_size):
            r_chunk = failed_tickers[r_i:r_i+retry_batch_size]
            time.sleep(3.0)
            log(f"  📥 リトライ中: {', '.join(r_chunk[:5])}...")
            
            try:
                df_raw_retry = yf.download(
                    r_chunk,
                    start=start_date_dt.strftime("%Y-%m-%d"),
                    interval=interval,
                    auto_adjust=False,
                    actions=True,
                    progress=False,
                    threads=False,
                    timeout=30
                )
                if not df_raw_retry.empty:
                    chunk_processed = parse_yfinance_batch(df_raw_retry, r_chunk)
                    if not chunk_processed.empty:
                        all_downloaded.append(chunk_processed)
            except Exception as e:
                log(f"    ❌ リトライ失敗: {e}")

    if all_downloaded:
        final_df = pd.concat(all_downloaded, ignore_index=True)
        final_df = final_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        
        cloud_success, cloud_msg = save_price_db(final_df, interval, is_jp=False, is_raw=True)
        if cloud_success:
            log("📥 米国株Rawデータベースのフルダウンロード完了。続いてActiveビルドを実行します。")
        else:
            log(f"⚠️ Rawデータの保存同期失敗: {cloud_msg}")
        
        return rebuild_active_from_raw(interval, is_jp=False, dry_run=dry_run, skip_assertion=True, status_callback=status_callback, raw_df=final_df)
    return False


# =====================================================================
# 🩹 非常用ピンポイント修復と物理データ削除
# =====================================================================

def apply_all_saved_patches(is_jp: bool = False, status_callback=None) -> int:
    """米国株専用：保存されているすべての崖修正パッチをインメモリ経由でActiveに安全一括適用します。"""
    def log(msg):
        print(f"[CONSOLE_DEBUG] [APPLY_PATCH] {msg}")
        sys.stdout.flush()
        if status_callback: status_callback(msg)

    if is_jp:
        log("⚠️ 日本株(JP)は楽天RSSデータを直接使用するため、パッチリビルドは不要です。")
        return 0

    log("🛠️ [USパッチ一括適用] 保存されたパッチ定義に基づいてActiveDBをすべて安全再構築します...")
    success_count = 0
    for interval in settings.TIMEFRAMES:
        success = rebuild_active_from_raw(interval, is_jp=False, dry_run=False, skip_assertion=False, status_callback=status_callback)
        if success:
            success_count += 1
            log(f"  👉 [{interval}] 米国株パッチの安全リビルドが正常完了しました。")
    return success_count


def repair_single_ticker_all_timeframes(ticker: str, is_jp: bool = False) -> dict:
    """特定の米国株銘柄に限定して、yfinanceの提供限界からデータをフル再取得してActiveをビルドし直します。"""
    if is_jp:
        return {}

    pure_ticker = str(ticker).strip().upper()
    now = datetime.now()
    results = {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            try:
                raw_db = load_price_db(interval, is_jp=False, is_raw=True)
            except FileNotFoundError:
                raw_db = pd.DataFrame()

            old_raw = raw_db[raw_db["ticker"] == pure_ticker].copy() if not raw_db.empty else pd.DataFrame()
            if interval == "1m": start_date_dt = now - timedelta(days=6)
            elif interval == "5m": start_date_dt = now - timedelta(days=58)
            elif interval == "60m": start_date_dt = now - timedelta(days=718)
            else: start_date_dt = datetime(2016, 1, 1)

            df_raw = yf.download(pure_ticker, start=start_date_dt.strftime("%Y-%m-%d"), interval=interval, auto_adjust=False, actions=True, progress=False)
            if df_raw.empty:
                results[interval] = "新規取得空"
                continue
            new_df = parse_yfinance_batch(df_raw, [pure_ticker])
            if new_df.empty:
                results[interval] = "パース結果空"
                continue

            if not old_raw.empty:
                new_dates = pd.to_datetime(new_df["date"])
                old_raw["date_dt"] = pd.to_datetime(old_raw["date"])
                old_filtered = old_raw[~old_raw["date_dt"].isin(new_dates)].copy()
                if "date_dt" in old_filtered.columns:
                    old_filtered = old_filtered.drop(columns=["date_dt"])
                merged_raw = pd.concat([old_filtered, new_df], ignore_index=True)
            else:
                merged_raw = new_df

            if not raw_db.empty:
                raw_db = raw_db[raw_db["ticker"] != pure_ticker]
            raw_db = pd.concat([raw_db, merged_raw], ignore_index=True)
            raw_db = raw_db.sort_values(["ticker", "date"]).reset_index(drop=True)
            
            save_price_db(raw_db, interval, is_jp=False, is_raw=True)
            rebuild_active_from_raw(interval, is_jp=False, dry_run=False)
            results[interval] = f"個別ダウンロード＆Active再ビルド完了 ({len(merged_raw):,}件)"
        except Exception as e:
            results[interval] = f"エラー: {str(e)}"

    return results


def delete_data_before_date(ticker: str, limit_date_str: str, is_jp: bool = False) -> dict:
    """特定の米国株について、指定した日付以前の時系列データをRaw/Activeから完全削除します。"""
    if is_jp:
        return {}

    pure_ticker = str(ticker).strip().upper()
    limit_dt = pd.to_datetime(limit_date_str)
    results = {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            df_raw = load_price_db(interval, is_jp=False, is_raw=True)
            if not df_raw.empty:
                df_raw["temp_date"] = pd.to_datetime(df_raw["date"])
                mask_to_delete = (df_raw["ticker"] == pure_ticker) & (df_raw["temp_date"] <= limit_dt)
                deleted_count = mask_to_delete.sum()
                if deleted_count > 0:
                    df_raw = df_raw[~mask_to_delete].copy()
                    df_raw = df_raw.drop(columns=["temp_date"])
                    df_raw = df_raw.sort_values(["ticker", "date"]).reset_index(drop=True)
                    save_price_db(df_raw, interval, is_jp=False, is_raw=True)
                    
                    rebuild_active_from_raw(interval, is_jp=False, dry_run=False)
                    results[interval] = f"Raw/Activeから {deleted_count:,} 件を正常物理削除"
                else:
                    results[interval] = "該当データなし"
            else:
                results[interval] = "RawDB空"
        except FileNotFoundError:
            results[interval] = "DBファイルなし"
        except Exception as e:
            results[interval] = f"エラー: {str(e)}"
            
    return results


# =====================================================================
# 🔍 米国株 異常値健康診断スキャン
# =====================================================================

def run_database_health_scan(is_jp: bool = False) -> list:
    """米国株Activeデータベースを巡回し、整合性チェックと健康診断を行います。"""
    if is_jp:
        return []

    anomalies = []
    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            df = load_price_db(interval, is_jp=False, is_raw=False) 
            if df.empty:
                continue
            df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
            
            cols_to_check = ["close"]
            has_adj = "adj close" in df.columns
            if has_adj:
                cols_to_check.append("adj close")
                df["pct_adj_close"] = df.groupby("ticker")["adj close"].pct_change()

            df["pct_close"] = df.groupby("ticker")["close"].pct_change()

            for p_col in cols_to_check:
                col_label = " (Adj Close)" if p_col == "adj close" else ""
                pct_col = "pct_adj_close" if p_col == "adj close" else "pct_close"
                
                anomaly_indices = df[(df[pct_col] <= -0.40) | (df[pct_col] >= 0.50)].index.tolist()
                for i in anomaly_indices:
                    row = df.loc[i]
                    ticker = row["ticker"]
                    curr_p = row[p_col]
                    pct_val = row[pct_col]
                    
                    ticker_df = df[(df["ticker"] == ticker) & (df.index >= i)].copy()
                    dates = ticker_df["date"].tolist()
                    close_vals = ticker_df[p_col].tolist()
                    n = len(ticker_df)
                    if n < 2:
                        continue
                        
                    curr_close = row["close"]
                    curr_adj = row["adj close"] if has_adj else float("nan")
                    
                    pre_close = curr_close / (1.0 + row["pct_close"])
                    pre_adj = curr_adj / (1.0 + row["pct_adj_close"]) if has_adj else float("nan")
                    
                    if pct_val <= -0.40:
                        found_recovery = False
                        recovery_idx = -1
                        for j in range(1, n):
                            post_p = close_vals[j]
                            if (pre_p := curr_p / (1.0 + pct_val)) * 0.85 <= post_p <= pre_p * 1.15:
                                found_recovery = True
                                recovery_idx = j
                                break
                        
                        post_close = ticker_df["close"].iloc[recovery_idx] if found_recovery else float("nan")
                        post_adj = ticker_df["adj close"].iloc[recovery_idx] if (found_recovery and has_adj) else float("nan")

                        if found_recovery:
                            bug_end_date = dates[recovery_idx - 1]
                            price_msg = f"Close: {pre_close:.1f} ➔ {curr_close:.1f} ➔ {post_close:.1f}"
                            if has_adj:
                                price_msg += f" | Adj: {pre_adj:.1f} ➔ {curr_adj:.1f} ➔ {post_adj:.1f}"
                                
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"🚨 クレーターバグ{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 {str(bug_end_date)[:16]}",
                                "異常値": f"Close: {curr_close:.1f}" + (f" / Adj: {curr_adj:.1f}" if has_adj else ""),
                                "前後価格": price_msg
                            })
                        else:
                            price_msg = f"Close: {pre_close:.1f} ➔ {curr_close:.1f}"
                            if has_adj:
                                price_msg += f" | Adj: {pre_adj:.1f} ➔ {curr_adj:.1f}"
                                
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"📉 階段段差（未調整分割）{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 最新",
                                "異常値": f"Close: {curr_close:.1f}" + (f" / Adj: {curr_adj:.1f}" if has_adj else ""),
                                "前後価格": price_msg
                            })
                    elif pct_val >= 0.50:
                        found_recovery = False
                        recovery_idx = -1
                        for j in range(1, n):
                            post_p = close_vals[j]
                            if (pre_p := curr_p / (1.0 + pct_val)) * 0.85 <= post_p <= pre_p * 1.15:
                                found_recovery = True
                                recovery_idx = j
                                break
                        
                        post_close = ticker_df["close"].iloc[recovery_idx] if found_recovery else float("nan")
                        post_adj = ticker_df["adj close"].iloc[recovery_idx] if (found_recovery and has_adj) else float("nan")

                        if found_recovery:
                            bug_end_date = dates[recovery_idx - 1]
                            price_msg = f"Close: {pre_close:.1f} ➔ {curr_close:.1f} ➔ {post_close:.1f}"
                            if has_adj:
                                price_msg += f" | Adj: {pre_adj:.1f} ➔ {curr_adj:.1f} ➔ {post_adj:.1f}"
                                
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"📈 タワーバグ{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 {str(bug_end_date)[:16]}",
                                "異常値": f"Close: {curr_close:.1f}" + (f" / Adj: {curr_adj:.1f}" if has_adj else ""),
                                "前後価格": price_msg
                            })
                        else:
                            price_msg = f"Close: {pre_close:.1f} ➔ {curr_close:.1f}"
                            if has_adj:
                                price_msg += f" | Adj: {pre_adj:.1f} ➔ {curr_adj:.1f}"
                                
                            anomalies.append({
                                "時間足": interval, "コード": ticker, "不具合種類": f"📈 階段段差（未調整併合）{col_label}",
                                "発生日/時刻": f"{str(dates[0])[:16]} 〜 最新",
                                "異常値": f"Close: {curr_close:.1f}" + (f" / Adj: {curr_adj:.1f}" if has_adj else ""),
                                "前後価格": price_msg
                            })
        except Exception:
            pass
    return anomalies

def analyze_db_update_needs(is_jp: bool = False) -> dict:
    """米国株データベースの最大日時と更新が必要な銘柄を検知します。"""
    if is_jp:
        return {}

    try:
        db_df = load_price_db("1d", is_jp=False, is_raw=True) 
        all_tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
        today = datetime.now().date()

        if db_df.empty:
            return {
                "global_max_date": None,
                "needs_period_update": True,
                "refetch_tickers": [],
                "missing_tickers": all_tickers,
            }

        db_df["date"] = pd.to_datetime(db_df["date"])
        global_max_date = db_df["date"].max().date()
        needs_period_update = (today - global_max_date).days > 3

        if "is_finalized" in db_df.columns:
            unfinalized = db_df[db_df["is_finalized"] == False]["ticker"].unique().tolist()
        else:
            unfinalized = []

        db_tickers = set(db_df["ticker"].unique())
        missing = [t for t in all_tickers if t not in db_tickers]

        return {
            "global_max_date": global_max_date,
            "needs_period_update": needs_period_update,
            "refetch_tickers": unfinalized,
            "missing_tickers": missing,
        }
    except Exception as e:
        return {
            "global_max_date": None,
            "needs_period_update": True,
            "refetch_tickers": [],
            "missing_tickers": [],
            "error": str(e),
        }

def rebuild_single_ticker_1d_raw(ticker: str):
    """米国株分割検知時、個別銘柄の1d Rawデータベースをフル再構築します。"""
    pure_ticker = str(ticker).strip().upper()
    start_date = "2016-01-01"
    try:
        df_new = yf.download(pure_ticker, start=start_date, interval="1d", auto_adjust=False, actions=True, progress=False)
        if not df_new.empty:
            parsed = parse_yfinance_batch(df_new, [pure_ticker])
            if not parsed.empty:
                try:
                    raw_db = load_price_db("1d", is_jp=False, is_raw=True)
                except FileNotFoundError:
                    raw_db = pd.DataFrame()
                    
                if not raw_db.empty:
                    raw_db = raw_db[raw_db["ticker"] != pure_ticker]
                    
                raw_db = pd.concat([raw_db, parsed], ignore_index=True)
                raw_db = raw_db.sort_values(["ticker", "date"]).reset_index(drop=True)
                save_price_db(raw_db, "1d", is_jp=False, is_raw=True)
                print(f"✅ [rebuild_single_ticker_1d_raw] 米国株 {pure_ticker} の 1d Raw をフル再構築しました。")
    except Exception as e:
        print(f"❌ [rebuild_single_ticker_1d_raw] エラー: {e}")

def get_current_memory_usage() -> str:
    """現在のプロセスの物理メモリ使用量をMB単位で取得します。"""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return f"{int(parts[1]) / 1024:.1f} MB"
    except Exception:
        pass
    try:
        import os
        import psutil
        process = psutil.Process(os.getpid())
        return f"{process.memory_info().rss / (1024 * 1024):.1f} MB"
    except Exception:
        pass
    return "取得不可"


# --- インクリメンタル更新アペンド（US専用） ---
def incremental_update_active(interval: str, new_raw_diff: pd.DataFrame = None, repair_log_df: pd.DataFrame = None) -> bool:
    """新規の差分データをActive加工し、既存Active DBに末尾結合します。"""
    if new_raw_diff is None or new_raw_diff.empty:
        return True
        
    import gc
    df_diff = new_raw_diff.copy()
    df_diff["is_finalized"] = compute_is_finalized(df_diff["date"], interval, is_jp=False)
    
    if "split_multiplier" not in df_diff.columns:
        df_diff["split_multiplier"] = 1.0
    if "patched_multiplier" not in df_diff.columns:
        df_diff["patched_multiplier"] = 1.0
        
    # パッチ適用
    df_diff = apply_saved_patches_to_df(df_diff, repair_log_df=repair_log_df)
    # TradingView確定
    df_diff = finalize_latest_with_tradingview_in_df(df_diff, interval)
    
    try:
        df_active = load_price_db(interval, is_jp=False, is_raw=False)
    except FileNotFoundError:
        df_active = pd.DataFrame()
        
    if not df_active.empty:
        df_active = pd.concat([df_active, df_diff], ignore_index=True)
        df_active = df_active.drop_duplicates(subset=["date", "ticker"], keep="last")
    else:
        df_active = df_diff
        
    df_active = df_active.sort_values(["ticker", "date"]).reset_index(drop=True)
    success, msg = save_price_db(df_active, interval, is_jp=False, is_raw=False)
    
    del df_active, df_diff
    gc.collect()
    return success


# --- 株式分割やパッチ修正時の部分上書き遡及加工フロー（US専用） ---
def partial_rebuild_active_for_tickers(interval: str, tickers: list, repair_log_df: pd.DataFrame = None) -> bool:
    """特定米国株銘柄に限定して遡及加工し、Active DBを安全に部分上書きします。"""
    import gc
    from data_access.local_db import load_price_db_for_tickers, load_price_db_excluding_tickers
    
    if not tickers:
        return True
        
    df_raw_target = load_price_db_for_tickers(interval, tickers, is_jp=False, is_raw=True)
    if df_raw_target.empty:
        return True
        
    df_raw_target["is_finalized"] = compute_is_finalized(df_raw_target["date"], interval, is_jp=False)
    
    if "split_multiplier" not in df_raw_target.columns:
        df_raw_target["split_multiplier"] = 1.0
    if "patched_multiplier" not in df_raw_target.columns:
        df_raw_target["patched_multiplier"] = 1.0
        
    if "stock splits" in df_raw_target.columns:
        split_events = df_raw_target[(df_raw_target["stock splits"] > 0) & (df_raw_target["stock splits"] != 1.0)]
        if not split_events.empty:
            price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df_raw_target.columns]
            for ticker in tickers:
                t_mask = df_raw_target["ticker"] == ticker
                group = df_raw_target[t_mask].copy().sort_values("date")
                adjusted, applied_splits = adjust_ticker_splits_backward_in_memory(group)
                if not adjusted.empty:
                    df_raw_target.loc[df_raw_target["ticker"] == ticker, price_cols + ["volume", "split_multiplier"]] = adjusted[price_cols + ["volume", "split_multiplier"]]

    df_raw_target = apply_saved_patches_to_df(df_raw_target, repair_log_df=repair_log_df)
    
    # 対象外の他銘柄Activeデータをロード
    df_active_other = load_price_db_excluding_tickers(interval, tickers, is_jp=False)
    df_active_new = pd.concat([df_active_other, df_raw_target], ignore_index=True)
    df_active_new = df_active_new.sort_values(["ticker", "date"]).reset_index(drop=True)
    
    success, msg = save_price_db(df_active_new, interval, is_jp=False, is_raw=False)
    
    del df_raw_target, df_active_other, df_active_new
    gc.collect()
    return success