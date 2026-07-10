# core/database_service.py
import os
import sys  # 🚀 Console出力を即時反映（flush）させるために追加
import time
import pandas as pd
import pytz
import yfinance as yf
from datetime import datetime, timedelta, time as dt_time
from config import settings
from data_access.local_db import load_price_db, save_price_db
from core.collector import (
    sanitize_ticker, get_download_symbol, get_all_collection_tickers,
    get_benchmark_latest_date, parse_yfinance_batch
)

# --- yfinanceが取得可能な期間の上限（日数） ---
YFINANCE_GAP_LIMITS = {"1m": 7, "5m": 60, "60m": 730}

# --- 東証: 取引時間延伸（2024年11月5日）の境界日 ---
TSE_EXTENDED_HOURS_DATE = pd.Timestamp("2024-11-05")

def get_jp_session_close_time(date) -> dt_time:
    d = pd.Timestamp(date).normalize()
    if d >= TSE_EXTENDED_HOURS_DATE:
        return dt_time(15, 30)
    return dt_time(15, 0)

def get_market_localized_now(is_jp: bool = True):
    tz = pytz.timezone("Asia/Tokyo") if is_jp else pytz.timezone("America/New_York")
    now_tz = datetime.now(pytz.utc).astimezone(tz)
    local_today = now_tz.date()
    return now_tz, local_today

def compute_is_finalized(date_series: pd.Series, interval: str, is_jp: bool = True) -> pd.Series:
    now_tz, local_today = get_market_localized_now(is_jp)
    dt_series = pd.to_datetime(date_series)

    if interval == "1d":
        close_buffer_time = dt_time(16, 30) if is_jp else dt_time(17, 30)
        today_is_finalized = now_tz.time() >= close_buffer_time

        data_dates = dt_series.dt.date
        is_finalized = data_dates < local_today
        if today_is_finalized:
            is_finalized = is_finalized | (data_dates == local_today)
        return is_finalized
    else:
        now_naive = now_tz.replace(tzinfo=None)
        return dt_series < (now_naive - timedelta(hours=1))

def detect_allocation_stop_days(df_1d: pd.DataFrame) -> pd.DataFrame:
    empty_result = pd.DataFrame(columns=["ticker", "date", "close", "volume"])
    if df_1d is None or df_1d.empty:
        return empty_result

    df = df_1d.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)

    if "is_finalized" in df.columns:
        df = df[df["is_finalized"] == True].reset_index(drop=True)
    if df.empty:
        return empty_result

    df["prev_close"] = df.groupby("ticker")["close"].shift(1)

    is_allocation_stop = (
        (df["open"] == df["high"]) &
        (df["high"] == df["low"]) &
        (df["low"] == df["close"]) &
        (df["volume"] > 0) &
        df["prev_close"].notna() &
        (df["close"] != df["prev_close"])
    )

    result = df.loc[is_allocation_stop, ["ticker", "date", "close", "volume"]].reset_index(drop=True)
    return result

def _build_synthetic_15h_bar_row(schema_cols: list, ticker: str, day_date, close_price: float, volume: float, is_jp: bool = True) -> dict:
    if is_jp:
        close_t = get_jp_session_close_time(day_date)
        bar_datetime = pd.Timestamp(day_date).normalize() + pd.Timedelta(hours=close_t.hour, minutes=close_t.minute)
    else:
        bar_datetime = pd.Timestamp(day_date).normalize() + pd.Timedelta(hours=16)
    row = {}
    for col in schema_cols:
        c = str(col).lower()
        if c == "date":
            row[col] = bar_datetime
        elif c == "ticker":
            row[col] = ticker
        elif c in ("open", "high", "low", "close", "adj close"):
            row[col] = close_price
        elif c == "volume":
            row[col] = volume
        elif c == "is_finalized":
            row[col] = True
        elif c in ("dividends", "stock splits"):
            row[col] = 0.0
        else:
            row[col] = None
    return row

def check_anomaly_need_patch(df_ticker: pd.DataFrame, patch_date_str: str, multiplier: float, threshold: float = 0.10) -> bool:
    """二重適用防止判定（t-1基準）。"""
    if df_ticker.empty or len(df_ticker) < 2:
        return False
        
    df_t = df_ticker.sort_values("date").reset_index(drop=True)
    df_t["date_dt"] = pd.to_datetime(df_t["date"])
    
    try:
        target_dt = pd.to_datetime(patch_date_str)
    except Exception:
        return False
    
    before_rows = df_t[df_t["date_dt"] <= target_dt]
    after_rows = df_t[df_t["date_dt"] > target_dt]
    
    if before_rows.empty or after_rows.empty:
        return False
        
    p_before = before_rows.iloc[-1]["close"]
    p_after = after_rows.iloc[0]["close"]
    
    if pd.isna(p_before) or pd.isna(p_after) or p_before == 0:
        return False
        
    r_raw = p_after / p_before
    r_adjusted = p_after / (p_before * multiplier)
    
    if abs(r_adjusted - 1.0) <= abs(r_raw - 1.0) - threshold:
        return True
    return False

# =====================================================================
# 🛠️ Raw & Active 2層同期およびアサーション検証エンジン
# =====================================================================

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
        alerts.append(f"⚠️ [銘柄喪失] 以下の銘柄がデータから消失しています: {list(missing_tickers)[:10]}")

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

def adjust_ticker_splits_backward_in_memory(df_ticker: pd.DataFrame) -> pd.DataFrame:
    """メモリ上で配信された株式分割情報に基づき過去データを修正。"""
    if df_ticker.empty or len(df_ticker) < 2:
        return df_ticker
        
    df = df_ticker.sort_values("date").reset_index(drop=True)
    price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df.columns]
    
    if "split_multiplier" not in df.columns:
        df["split_multiplier"] = 1.0
    if "patched_multiplier" not in df.columns:
        df["patched_multiplier"] = 1.0
    
    if "stock splits" in df.columns:
        split_events = df[(df["stock splits"] > 0) & (df["stock splits"] != 1.0)]
        if not split_events.empty:
            for idx in sorted(split_events.index, reverse=True):
                if idx == 0:
                    continue
                
                split_val = df.loc[idx, "stock splits"]
                if split_val <= 0:
                    continue
                
                pre_close = df.loc[idx - 1, "close"]
                post_close = df.loc[idx, "close"]
                
                if pd.isna(pre_close) or pd.isna(post_close) or pre_close <= 0 or post_close <= 0:
                    continue
                
                actual_ratio = pre_close / post_close
                unadjusted_mask = (df.index < idx) & (df["split_multiplier"] == 1.0)
                
                if unadjusted_mask.any() and abs(actual_ratio - split_val) / split_val <= 0.15:
                    ratio = 1.0 / split_val
                    for col in price_cols:
                        df.loc[unadjusted_mask, col] = df.loc[unadjusted_mask, col] * ratio
                    if "volume" in df.columns:
                        df.loc[unadjusted_mask, "volume"] = df.loc[unadjusted_mask, "volume"] / ratio
                        
                    df.loc[unadjusted_mask, "split_multiplier"] = ratio

    return df

def apply_saved_patches_to_df(df: pd.DataFrame, is_jp: bool = True, repair_log_df: pd.DataFrame = None) -> pd.DataFrame:
    """
    保存されたパッチ定義を適用します。
    API 503エラー防止のため、事前に取得した修復ログ(repair_log_df)をキャッシュ利用可能にしました。
    """
    log_df = repair_log_df
    if log_df is None:
        try:
            from data_access.sheets_api import load_repair_log_from_sheets
            log_df = load_repair_log_from_sheets()
        except Exception:
            return df

    if log_df is None or log_df.empty:
        return df

    market_str = "JP" if is_jp else "US"
    df_result = df.copy()
    
    if "patched_multiplier" not in df_result.columns:
        df_result["patched_multiplier"] = 1.0
    if "split_multiplier" not in df_result.columns:
        df_result["split_multiplier"] = 1.0
    
    log_df["parsed_date"] = pd.to_datetime(log_df["cliff_date"], errors="coerce")
    log_df = log_df.dropna(subset=["parsed_date"]).sort_values("parsed_date", ascending=False)

    for _, row in log_df.iterrows():
        if str(row.get("market", "")).strip().upper() != market_str:
            continue
        ticker = str(row.get("ticker", "")).strip()
        cliff_date = row["parsed_date"]
        try:
            multiplier = float(row.get("multiplier", 1.0))
            if multiplier <= 0 or multiplier == 1.0:
                continue
        except ValueError:
            continue

        ticker_mask = df_result["ticker"] == ticker
        if not ticker_mask.any():
            continue

        df_result["date_dt"] = pd.to_datetime(df_result["date"])
        pre_mask = ticker_mask & (df_result["date_dt"] <= cliff_date) & (df_result["patched_multiplier"] == 1.0)
        
        if pre_mask.any():
            price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df_result.columns]
            for col in price_cols:
                df_result.loc[pre_mask, col] = df_result.loc[pre_mask, col] * multiplier
            if "volume" in df_result.columns:
                df_result.loc[pre_mask, "volume"] = df_result.loc[pre_mask, "volume"] / multiplier
            
            df_result.loc[pre_mask, "patched_multiplier"] = multiplier
        
        df_result = df_result.drop(columns=["date_dt"])

    return df_result

def propagate_stop_allocation_bars_in_memory(df_1d_active: pd.DataFrame, df_intra_active: pd.DataFrame, is_jp: bool = True) -> pd.DataFrame:
    """
    日足のストップ高安発生日を検出し、対象の分足データを一括で合成バーに置き換えます（メモリ最適化版）。
    無関係な銘柄のデータコピーを発生させず、瞬間的なメモリスパイクを極限まで低減します。
    """
    import gc
    import pandas as pd
    
    if df_intra_active.empty:
        return df_intra_active

    # ストップ高安発生日のリストを取得（ごく少数の行数）
    stop_days_df = detect_allocation_stop_days(df_1d_active)
    if stop_days_df.empty:
        return df_intra_active

    stop_days_df = stop_days_df.copy()
    stop_days_df["date_norm"] = pd.to_datetime(stop_days_df["date"]).dt.normalize()
    
    # 処理を高速化するため { 銘柄 : {対象日のセット} } の形にまとめる（極めて軽量な辞書）
    stop_dict = stop_days_df.groupby("ticker")["date_norm"].apply(set).to_dict()
    stop_tickers = set(stop_dict.keys())

    # ストップ高安が一度も発生していない無関係な銘柄を特定
    mask_has_stop = df_intra_active["ticker"].isin(stop_tickers)
    
    # ストップ高安がデータ範囲内で一度もオーバーラップしていない場合は即時返却
    if not mask_has_stop.any():
        return df_intra_active

    # 1. 安全なデータ（全体の95%以上）: コピー（.copy()）をせず、単なるスライス参照として取り出す（メモリを消費しない）
    df_intra_safe = df_intra_active[~mask_has_stop]
    
    # 2. 対象データ（ごく一部の銘柄のみ）: こちらだけをコピーして加工対象とする
    df_intra_target = df_intra_active[mask_has_stop].copy()

    # 対象データのみ日付オブジェクトの正規化を適用（変換コストとメモリを最小限に制限）
    df_intra_target["date"] = pd.to_datetime(df_intra_target["date"])
    df_intra_target["day_normalize"] = df_intra_target["date"].dt.normalize()

    # 高速かつ省メモリな判定（Pythonのタプル生成数を極小化）
    is_stop_bar = [
        t in stop_dict and d in stop_dict[t]
        for t, d in zip(df_intra_target["ticker"], df_intra_target["day_normalize"])
    ]
    
    # ストップ日に合致する分足バーを削除
    df_cleaned = df_intra_target[~pd.Series(is_stop_bar, index=df_intra_target.index)].copy()

    # 合成バー（大引け15:00/15:30などのバー）を生成
    schema_cols = df_intra_active.columns.tolist()
    new_rows = []
    
    for _, row in stop_days_df.iterrows():
        ticker = row["ticker"]
        day_date = row["date_norm"]
        
        # 処理中の分足データに存在する銘柄のみ処理
        ticker_data = df_intra_target[df_intra_target["ticker"] == ticker]
        if ticker_data.empty:
            continue
            
        t_min = ticker_data["day_normalize"].min()
        t_max = ticker_data["day_normalize"].max()
        
        # データの収録期間内に収まっている場合のみ、合成バーを作成
        if t_min <= day_date <= t_max:
            new_row = _build_synthetic_15h_bar_row(
                schema_cols, ticker, day_date, row["close"], row["volume"], is_jp=is_jp
            )
            new_rows.append(new_row)

    if new_rows:
        df_synthetic = pd.DataFrame(new_rows)
        df_synthetic["date"] = pd.to_datetime(df_synthetic["date"])
        df_result_target = pd.concat([df_cleaned, df_synthetic], ignore_index=True)
    else:
        df_result_target = df_cleaned

    if "day_normalize" in df_result_target.columns:
        df_result_target = df_result_target.drop(columns=["day_normalize"])

    # 触っていなかった大部分の安全なデータと、加工済みの対象データを再結合
    df_result = pd.concat([df_intra_safe, df_result_target], ignore_index=True)
    
    # 不要になった変数をローカルスコープから完全に抹消
    del df_intra_target, df_intra_safe, df_cleaned, new_rows, stop_dict, stop_tickers
    gc.collect()
    
    return df_result

def rebuild_active_from_raw(interval: str, is_jp: bool = True, dry_run: bool = False, skip_assertion: bool = False, status_callback=None, log_accumulator: list = None, repair_log_df: pd.DataFrame = None) -> bool:
    """RawデータからActiveデータの加工ビルドとアサーション検証（Dry Run時の一時ファイル保存・プレビュー制限対応）。"""
    import gc
    import sys
    import pandas as pd
    
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

    log(f"🏗️ [{interval}] RawデータからActiveデータの加工ビルドを開始します...")
    
    df_raw = load_price_db(interval, is_jp=is_jp, is_raw=True)
    if df_raw.empty:
        log("❌ Rawデータベースファイルが空、または検出されません。")
        return False

    log(f"Rawデータのロード完了。サイズ: {df_raw.shape}, ユニーク数: {df_raw['ticker'].nunique()}")

    df_raw["is_finalized"] = compute_is_finalized(df_raw["date"], interval, is_jp=is_jp)

    if "split_multiplier" not in df_raw.columns:
        df_raw["split_multiplier"] = 1.0
    if "patched_multiplier" not in df_raw.columns:
        df_raw["patched_multiplier"] = 1.0

    split_tickers = []
    if "stock splits" in df_raw.columns:
        split_tickers = df_raw[(df_raw["stock splits"] > 0) & (df_raw["stock splits"] != 1.0)]["ticker"].unique().tolist()

    if split_tickers:
        log(f"株式分割イベントを検知しました。対象銘柄数: {len(split_tickers)} / {df_raw['ticker'].nunique()}")
        
        price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df_raw.columns]
        cols_to_write = price_cols + ["volume", "split_multiplier", "patched_multiplier"]
        
        total_split_tickers = len(split_tickers)
        for idx, ticker in enumerate(split_tickers):
            ticker_mask = df_raw["ticker"] == ticker
            if not ticker_mask.any():
                continue
                
            group = df_raw[ticker_mask].copy()
            group_sorted = group.sort_values("date")
            adjusted_group = adjust_ticker_splits_backward_in_memory(group_sorted)
            
            if not adjusted_group.empty:
                adjusted_group.index = group_sorted.index
                valid_cols = [c for c in cols_to_write if c in df_raw.columns and c in adjusted_group.columns]
                df_raw.loc[adjusted_group.index, valid_cols] = adjusted_group[valid_cols]
            
            if idx % 100 == 0 or idx == total_split_tickers - 1:
                print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [REBUILD_ACTIVE]   -> インプレース分割処理進捗: {idx+1}/{total_split_tickers} ({ticker})")
                sys.stdout.flush()
                
        df_processed = df_raw
    else:
        log("データセット全体に株式分割イベントは検出されませんでした。インプレーススキップします。")
        df_processed = df_raw
        
    print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [REBUILD_ACTIVE] 遡及修正計算完了。総行数: {len(df_processed)}")
    sys.stdout.flush()

    if not skip_assertion:
        print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [REBUILD_ACTIVE] パッチ定義を反映中...")
        sys.stdout.flush()
        df_processed = apply_saved_patches_to_df(df_processed, is_jp=is_jp, repair_log_df=repair_log_df)

    if interval != "1d":
        try:
            print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [REBUILD_ACTIVE] ストップ高安バーの補完を開始...")
            sys.stdout.flush()
            
            # 🚀 1dアクティブデータから必要最小限の列だけを省メモリでロードする
            cols_needed = ["ticker", "date", "open", "high", "low", "close", "volume"]
            try:
                # 'is_finalized' カラムがある場合は一緒に読み込む
                df_1d_active = load_price_db("1d", is_jp=is_jp, is_raw=False, is_temp=dry_run, columns=cols_needed + ["is_finalized"])
            except Exception:
                # 存在しない、あるいは読み込み失敗した場合は基本カラムのみ
                df_1d_active = load_price_db("1d", is_jp=is_jp, is_raw=False, is_temp=dry_run, columns=cols_needed)
                
            df_processed = propagate_stop_allocation_bars_in_memory(df_1d_active, df_processed, is_jp=is_jp)
            del df_1d_active
            gc.collect()
        except Exception as e:
            print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [REBUILD_ACTIVE] ストップ高安補完中に警告: {e}")
            sys.stdout.flush()
            log(f"⚠️ ストップ高安バーの自動移植はスキップされました: {e}")

    if not dry_run:
        print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [REBUILD_ACTIVE] 保存前にTradingView確定値をマージ適用中...")
        sys.stdout.flush()
        df_processed = finalize_latest_with_tradingview_in_df(df_processed, interval, is_jp=is_jp)

    if not skip_assertion:
        try:
            df_old_active = load_price_db(interval, is_jp=is_jp, is_raw=False)
        except FileNotFoundError:
            df_old_active = pd.DataFrame()

        alerts = check_processed_data_health(df_old_active, df_processed)
        if alerts:
            print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [REBUILD_ACTIVE] 🚨 整合性アラートを検出:")
            for a in alerts:
                print(f"  {a}")
            sys.stdout.flush()
            log("💥 【警告】ビルド後の健康診断チェックで異常を検出しました:")
            for alert in alerts:
                log(f"   {alert}")
            if any("🚨" in a for a in alerts):
                log("🛑 深刻なデータ不整合（ジャンプなど）を検出したため、破損防止のため同期を強制中断しました。")
                return False
        del df_old_active
        gc.collect()
    else:
        log("✨ [白紙構築] 新旧データの整合性比較、および過去パッチの干渉をスキップしてクリーン処理します。")

    if dry_run:
        log(f"🧪 [DRY RUN] {interval} 加工・アサーション検証を正常に通過。ディスク（_temp.parquet）に一時保存します...")
        df_processed = df_processed.sort_values(["ticker", "date"]).reset_index(drop=True)
        
        # 巨大DFはメモリから直ちに退避（Googleドライブ同期はスキップ）
        local_success, local_msg = save_price_db(df_processed, interval, is_jp=is_jp, is_raw=False, is_temp=True)
        
        if local_success:
            log(f"💾 一時ファイルをローカルに保存しました。メモリから完全解放します。")
        else:
            log(f"⚠️ 一時保存に失敗しました（本番適用時に動作しない可能性があります）: {local_msg}")
            
        if settings.HAS_STREAMLIT:
            import streamlit as st
            # 🚀 .copy(deep=True) を追加して元の配列データとの参照を遮断し、メモリを開放可能にする
            st.session_state[f"temp_verified_active_preview_{interval}"] = df_processed.head(100).copy(deep=True)
            st.session_state[f"temp_verified_active_exists_{interval}"] = True
            
        del df_processed
        gc.collect()
        return True
    else:
        print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [REBUILD_ACTIVE] 加工済データのソート及び保存処理中...")
        sys.stdout.flush()
        df_processed = df_processed.sort_values(["ticker", "date"]).reset_index(drop=True)
        cloud_success, cloud_msg = save_price_db(df_processed, interval, is_jp=is_jp, is_raw=False)
        
        del df_processed
        gc.collect()
        
        if cloud_success:
            log(f"✅ [{interval}] ActiveデータベースをGoogleドライブへ正常に保存しました。")
        else:
            log(f"⚠️ 【重要警告】[{interval}] Googleドライブへの同期に失敗しました（一時的にローカルフォルダに保存）。")
            log(f"   ❌ エラー詳細: {cloud_msg}")
        return True

# =====================================================================
# 📥 Rawデータ更新 ＆ 統合同期システム
# =====================================================================

def update_raw_database(is_jp: bool = True, target_tickers: list = None, force_refetch: bool = False, status_callback=None, target_interval: str = None, log_accumulator: list = None):
    """yfinanceからのRawデータ差分取得（フッター台帳メタデータ対応によりOOMを完全排除）。"""
    market_name = "JP" if is_jp else "US"
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
            
    if is_jp and not tickers:
        tickers = get_all_collection_tickers()
    if not tickers:
        log(f"[{market_name}] 更新対象銘柄がありません。")
        return

    now_tz, local_today = get_market_localized_now(is_jp)
    now = now_tz.replace(tzinfo=None)
    suffix = ".T" if is_jp else ""
    tickers = [sanitize_ticker(t, is_jp) for t in tickers]

    print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [UPDATE_RAW] 全体処理対象の銘柄数: {len(tickers)}")
    sys.stdout.flush()

    timeframes_to_run = [target_interval] if target_interval else settings.TIMEFRAMES

    for interval in timeframes_to_run:
        log(f"⏱️ 【{market_name}】{interval} Rawデータ差分収集判定を開始...")
        
        # 🚀 【リファクタリング】数百万行をロードせず、メタデータフッター（台帳）のみを高速取得
        from data_access.local_db import load_price_db_ledger
        ledger = load_price_db_ledger(interval, is_jp=is_jp, is_raw=True)
        db_max_date_str = ledger.get("db_max_date")
        db_max_date = pd.to_datetime(db_max_date_str) if db_max_date_str else None
        
        if db_max_date is not None:
            bm_last_date = get_benchmark_latest_date(interval, is_jp=is_jp)
            log(f"  🔍 ベンチマーク最新: {bm_last_date} | Raw DB最新(Ledger): {db_max_date}")
            if bm_last_date is not None:
                if bm_last_date <= db_max_date:
                    log(f"  ✨ 最新状態のため、差分ダウンロードはスキップします。")
                    continue

        last_updates_map = ledger.get("last_updates_map", {})
        if not last_updates_map:
            last_updates_map = {}

        # グループ分け、差分取得開始日の特定ロジック
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

        print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [UPDATE_RAW] グループ数: {len(groups)}")
        sys.stdout.flush()

        all_downloaded = []  # ⚠️ 処理グループを回す直前に初期化
        for group_idx, (t_last, chunk_tickers) in enumerate(groups.items()):
            if t_last is None:
                if interval == "1m": start_date_dt = now - timedelta(days=6)
                elif interval == "5m": start_date_dt = now - timedelta(days=58)
                elif interval == "60m": start_date_dt = now - timedelta(days=718)
                else: start_date_dt = datetime(2016, 1, 1)
                start_date_str = start_date_dt.strftime("%Y-%m-%d")
            else:
                if interval == "1d":
                    start_date_dt = t_last + timedelta(days=1)
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
                            f"yfinanceの上限（{limit_days}日）を超えたため差分同期できません。手動リビルドを行ってください。"
                        )
                        continue

            BATCH_SIZE = 100
            for i in range(0, len(chunk_tickers), BATCH_SIZE):
                chunk = chunk_tickers[i:i+BATCH_SIZE]
                symbols = [f"{t}{suffix}" for t in chunk]
                
                print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [START] BATCH {i//BATCH_SIZE + 1} for Group {group_idx+1}. (Interval: {interval})")
                sys.stdout.flush()
                
                try:
                    print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [API_CALL] Requesting symbols: {chunk[:5]}...")
                    sys.stdout.flush()
                    
                    df_raw = yf.download(
                        symbols, 
                        start=start_date_str,
                        interval=interval, 
                        auto_adjust=False, 
                        actions=True, 
                        progress=False, 
                        threads=True, 
                        timeout=30
                    )
                    
                    print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [API_SUCCESS] df_raw shape: {df_raw.shape if not df_raw.empty else 'EMPTY'}")
                    sys.stdout.flush()
                    
                    if not df_raw.empty:
                        print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [PARSE] Parsing dataframe for {len(chunk)} tickers...")
                        sys.stdout.flush()
                        
                        chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
                        
                        print(f"[CONSOLE_DEBUG] [Mem: {get_current_memory_usage()}] [PARSE_SUCCESS] Processed rows: {len(chunk_processed)}")
                        sys.stdout.flush()
                        
                        if not chunk_processed.empty:
                            all_downloaded.append(chunk_processed)
                    else:
                        print(f"[CONSOLE_DEBUG] [API_WARNING] Returned DataFrame is EMPTY.")
                        sys.stdout.flush()
                except Exception as e:
                    print(f"[CONSOLE_DEBUG] [BATCH_ERROR] Error: {e}")
                    import traceback
                    traceback.print_exc()
                    sys.stdout.flush()
                    log(f"     Batch Error: {e}")
                time.sleep(1)

        if all_downloaded:
            new_combined = pd.concat(all_downloaded, ignore_index=True)
            filtered_parts = []
            for ticker, group in new_combined.groupby("ticker"):
                t_last = last_updates_map.get(ticker)
                if t_last is not None:
                    group = group[pd.to_datetime(group["date"]) > pd.to_datetime(t_last)]
                filtered_parts.append(group)
            
            if filtered_parts:
                new_combined = pd.concat(filtered_parts, ignore_index=True)
            else:
                new_combined = pd.DataFrame()
            
            detected_split_tickers = []
            if interval == "1d" and not new_combined.empty:
                if "stock splits" in new_combined.columns:
                    split_rows = new_combined[(new_combined["stock splits"] > 0) & (new_combined["stock splits"] != 1.0)]
                    if not split_rows.empty:
                        detected_split_tickers = split_rows["ticker"].unique().tolist()
                        for st_ticker in detected_split_tickers:
                            log(f"🔔 [株式分割検知] 銘柄 {st_ticker} に分割を検知しました。日足(1d)のみフル再ダウンロードを実行します。")
                            rebuild_single_ticker_1d_raw(st_ticker, is_jp=is_jp)
            
            if not new_combined.empty:
                try:
                    df_raw_db = load_price_db(interval, is_jp=is_jp, is_raw=True)
                except FileNotFoundError:
                    df_raw_db = pd.DataFrame()
                
                if not df_raw_db.empty:
                    df_raw_db = pd.concat([df_raw_db, new_combined], ignore_index=True)
                    df_raw_db = df_raw_db.drop_duplicates(subset=["date", "ticker"], keep="last")
                else:
                    df_raw_db = new_combined
                
                df_raw_db = df_raw_db.sort_values(["ticker", "date"]).reset_index(drop=True)
                cloud_success, cloud_msg = save_price_db(df_raw_db, interval, is_jp=is_jp, is_raw=True)
                
                if cloud_success:
                    log(f"  📥 Rawデータ差分保存完了。({len(new_combined):,}件追加)")
                    
                    if not detected_split_tickers:
                        log(f"  🛠️ 差分データのみをActiveデータベースへインクリメンタル反映します...")
                        incremental_update_active(interval, is_jp=is_jp, new_raw_diff=new_combined)
                    else:
                        log(f"  🛠️ 分割発生銘柄のみ部分遡及処理（部分上書き）を実行します...")
                        partial_rebuild_active_for_tickers(interval, detected_split_tickers, is_jp=is_jp)
                else:
                    log(f"  ⚠️ [Raw保存警告] Googleドライブへの同期に失敗しました（ローカルのみ）。エラー: {cloud_msg}")
            else:
                log(f"  📥 yfinanceからの新規差分データはありません。")
        else:
            log(f"  📥 yfinanceからの差分データはありません。")

def update_price_database(is_jp: bool = True, target_tickers: list = None, force_refetch: bool = False, status_callback=None, dry_run: bool = False):
    """時間足自己完結型＆APIレート制御キャッシュ版 データベース更新プロセス。"""
    import gc
    from data_access.sheets_api import upload_sync_log_to_drive
    
    # セッション内の前回のログ履歴をクリア
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

    log("📡 1. 【パッチ定義】の事前ロード（キャッシュ化）を開始します...")
    try:
        from data_access.sheets_api import load_repair_log_from_sheets
        repair_log_df = load_repair_log_from_sheets()
        log("   ✅ スプレッドシートからパッチ定義の事前取得に成功しました。503エラーを防止するためにメモリ共有します。")
    except Exception as e:
        repair_log_df = None
        log(f"   ⚠️ パッチの事前ロードに失敗しました。各足で個別ダウンロードを行います: {e}")

    # 🚀 【1d足】差分取得とActiveビルドを最優先で確定させる
    log("📡 2. 【日足 (1d)】のRawデータ差分取得を開始します...")
    update_raw_database(is_jp=is_jp, target_tickers=target_tickers, force_refetch=force_refetch, status_callback=status_callback, target_interval="1d", log_accumulator=accumulated_logs)

    log("🛠️ 3. 【日足 (1d)】のActiveデータベース加工・検証ビルドを実行します...")
    rebuild_active_from_raw("1d", is_jp=is_jp, dry_run=dry_run, skip_assertion=False, status_callback=status_callback, log_accumulator=accumulated_logs, repair_log_df=repair_log_df)
    
    gc.collect()

    # 🚀 【分足】1つずつ「取得➔ビルド➔保存➔メモリ完全破棄」で完結させ、同時データ居座りをゼロにする
    intraday_timeframes = [tf for tf in settings.TIMEFRAMES if tf != "1d"]
    log(f"📡 4. 以下の分足データに関して、完全自己完結ロード＆ビルド処理を開始します: {intraday_timeframes}")

    for interval in intraday_timeframes:
        log(f"⏱️ 【{interval}】の個別同期プロセスを開始します...")
        
        # Raw差分取得
        update_raw_database(is_jp=is_jp, target_tickers=target_tickers, force_refetch=force_refetch, status_callback=status_callback, target_interval=interval, log_accumulator=accumulated_logs)
        
        # Active検証ビルド（キャッシュした修復ログと、ローカル退避済みの1d_tempデータを参照）
        rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=dry_run, skip_assertion=False, status_callback=status_callback, log_accumulator=accumulated_logs, repair_log_df=repair_log_df)
        
        gc.collect()
        log(f"✅ 【{interval}】の個別同期とビルドを安全に終了し、メモリをクリアしました。")

    log("✨ 全ての時間足に対するデータベース更新プロセスが正常に完了しました。")
    
    # 📝 Googleドライブの logs フォルダに累積詳細ログをバッチアップロード保存
    try:
        log("📤 蓄積された詳細実行ログをGoogleドライブへアップロード保存しています...")
        prefix = "sync_dryrun" if dry_run else "sync"
        log_filename = upload_sync_log_to_drive(accumulated_logs, is_jp=is_jp, prefix=prefix)
        if log_filename:
            log(f"💾 ログファイル '{log_filename}' をGoogleドライブに保存しました。")
        else:
            log("⚠️ ログの自動アップロードがスキップ、または失敗しました。")
    except Exception as e:
        log(f"⚠️ ログファイルの自動転送中に例外エラーが発生しました: {e}")

def execute_apply_verified_temp_dbs_to_active(is_jp: bool = True, status_callback=None) -> dict:
    """
    Dry Runで検証完了し、ローカル作業フォルダに一時保存されている Parquet ファイル（_temp）を、
    Activeデータベースとして本番確定（Google Driveへ一括アップロード）します（一瞬で完了します）。
    """
    import gc
    from data_access.local_db import promote_temp_db_to_active
    from data_access.sheets_api import upload_sync_log_to_drive
    
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
                
    log("🚀 [本番適用] ローカルに退避している検証済み一時ファイルを、Googleドライブへ一括で確定アップロードします...")
    
    results = {}
    success_count = 0
    
    for interval in settings.TIMEFRAMES:
        log(f"📦 [{interval}] 一時ファイルの本番確定処理中...")
        success, msg = promote_temp_db_to_active(interval, is_jp=is_jp)
        results[interval] = {"success": success, "message": msg}
        
        if success:
            success_count += 1
            log(f"   ✅ [{interval}] の本番確定が正常に完了しました。")
            if settings.HAS_STREAMLIT:
                import streamlit as st
                # UI表示用のプレビューフラグやサンプル100行をクリーンアップ
                if f"temp_verified_active_exists_{interval}" in st.session_state:
                    st.session_state[f"temp_verified_active_exists_{interval}"] = False
                if f"temp_verified_active_preview_{interval}" in st.session_state:
                    del st.session_state[f"temp_verified_active_preview_{interval}"]
        else:
            log(f"   ❌ [{interval}] の本番確定に失敗しました: {msg}")
            
    gc.collect()
    log(f"✨ 本番確定同期が終了しました。成功: {success_count} / {len(settings.TIMEFRAMES)}")
    
    # 本番適用の実行ログもGoogleドライブへ自動保存
    try:
        upload_sync_log_to_drive(accumulated_logs, is_jp=is_jp, prefix="apply_active")
    except Exception:
        pass
        
    return results

# =====================================================================
# 💥 クリーンビルド（RawもActiveも完全にダウンロードし直す）
# =====================================================================

def full_rebuild_all_database(is_jp: bool = True, interval: str = "1d", status_callback=None, dry_run: bool = False) -> bool:
    """物理削除からの完全な白紙クリーンビルド。"""
    def log(msg):
        print(f"[CONSOLE_DEBUG] [FULL_REBUILD] {msg}")
        sys.stdout.flush()
        if status_callback: status_callback(msg)

    market_name = "JP" if is_jp else "US"
    if is_jp:
        try:
            from core.collector import sync_extra_tickers_to_local
            sync_extra_tickers_to_local()
            log("🔄 Google Sheetsから最新の追加収集ETFマスタを取得し、同期しました。")
        except Exception as e:
            log(f"⚠️ 追加収集ETFマスタの同期に失敗したため、既存のローカルキャッシュを使用します: {e}")
            
        tickers = get_all_collection_tickers()
    else:
        tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
    
    if not tickers:
        log("❌ [フル再構築] 銘柄が検出されません。")
        return False
        
    tickers = [sanitize_ticker(t, is_jp) for t in tickers]
    suffix = ".T" if is_jp else ""
    now = datetime.now()
    
    if interval == "1m": start_date_dt = now - timedelta(days=6)
    elif interval == "5m": start_date_dt = now - timedelta(days=58)
    elif interval == "60m": start_date_dt = now - timedelta(days=718)
    else: start_date_dt = datetime(2016, 1, 1)
        
    log(f"🚨 [フル再構築] {market_name} ({interval}) Rawデータダウンロード開始。総数: {len(tickers)}")
    
    all_downloaded = []
    BATCH_SIZE = 30
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i+BATCH_SIZE]
        symbols = [f"{t}{suffix}" for t in chunk]
        
        print(f"[CONSOLE_DEBUG] [START_FULL_REBUILD] Batch {i//BATCH_SIZE + 1} / {(len(tickers)-1)//BATCH_SIZE + 1} (Tickers: {chunk})")
        sys.stdout.flush()
        
        log(f"  📥 ダウンロード中 ({i + 1}〜{min(i + BATCH_SIZE, len(tickers))}): {', '.join(chunk[:5])}...")
        
        try:
            print(f"[CONSOLE_DEBUG] [API_CALL_REBUILD] Requesting {len(symbols)} symbols...")
            sys.stdout.flush()
            
            df_raw = yf.download(
                symbols,
                start=start_date_dt.strftime("%Y-%m-%d"),
                interval=interval,
                auto_adjust=False,
                actions=True,
                progress=False,
                threads=True,
                timeout=30
            )
            
            print(f"[CONSOLE_DEBUG] [API_SUCCESS_REBUILD] df_raw shape: {df_raw.shape if not df_raw.empty else 'EMPTY'}")
            sys.stdout.flush()
            
            if not df_raw.empty:
                print(f"[CONSOLE_DEBUG] [PARSE_REBUILD] Parsing downloaded chunk...")
                sys.stdout.flush()
                
                chunk_processed = parse_yfinance_batch(df_raw, chunk, is_jp=is_jp)
                
                print(f"[CONSOLE_DEBUG] [PARSE_SUCCESS_REBUILD] Processed rows: {len(chunk_processed)}")
                sys.stdout.flush()
                
                if not chunk_processed.empty:
                    all_downloaded.append(chunk_processed)
            else:
                print(f"[CONSOLE_DEBUG] [API_WARNING_REBUILD] Returned DataFrame is EMPTY.")
                sys.stdout.flush()
        except Exception as e:
            print(f"[CONSOLE_DEBUG] [BATCH_ERROR_REBUILD] Error during rebuild: {e}")
            import traceback
            traceback.print_exc()
            sys.stdout.flush()
            log(f"    -> ⚠️ エラー: {e}")
        time.sleep(1.5)
        
    if all_downloaded:
        final_df = pd.concat(all_downloaded, ignore_index=True)
        final_df = final_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        
        print(f"[CONSOLE_DEBUG] [REBUILD_COMPLETE] All chunks merged. Saving raw database...")
        sys.stdout.flush()
        
        cloud_success, cloud_msg = save_price_db(final_df, interval, is_jp=is_jp, is_raw=True)
        if cloud_success:
            log("📥 Rawデータベースのフル構築に成功しました。続いてActiveデータのビルドと検証に入ります。")
        else:
            log(f"⚠️ [Raw保存警告] RawデータのGoogleドライブ同期に失敗しました。エラー: {cloud_msg}")
        
        return rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=dry_run, skip_assertion=True, status_callback=status_callback)
    
    return False

# =====================================================================
# 🩹 手動修復と一括パッチ適用
# =====================================================================

def test_forced_scale_patch_in_memory(ticker: str, patch_date_str: str, multiplier: float, is_jp: bool = True) -> tuple[dict, dict]:
    """
    メモリ上で特定銘柄（全時間足）の崖調整パッチ（倍率補正）の適用テストを実行します。
    ディスク上（Google Drive / ローカル作業ファイル）のデータは書き換えずに、
    テスト結果のプレビュー用辞書と、調整後のデータフレーム辞書を返します。
    """
    if multiplier <= 0:
        return {"error": "倍率に0以下の数値は指定できません。"}, {}

    pure_ticker = sanitize_ticker(ticker, is_jp)
    try:
        target_dt = pd.to_datetime(patch_date_str)
    except Exception as e:
        return {"error": f"要補正Close日時のパースに失敗しました: {e}"}, {}

    test_results = {}
    temp_repaired_dfs = {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            db_df = load_price_db(interval, is_jp=is_jp, is_raw=False)
        except FileNotFoundError:
            continue
            
        if db_df.empty:
            continue

        mask = db_df["ticker"] == pure_ticker
        ticker_data = db_df[mask].copy()
        if ticker_data.empty:
            continue

        if "patched_multiplier" not in ticker_data.columns:
            ticker_data["patched_multiplier"] = 1.0

        ticker_data["date_dt"] = pd.to_datetime(ticker_data["date"])
        
        # 適用条件：対象日時（含む）以前で、まだパッチが適用されていない部分
        pre_mask = (ticker_data["date_dt"] <= target_dt) & (ticker_data["patched_multiplier"] == 1.0)
        
        if not pre_mask.any():
            # すでにパッチ適用済み、または対象データなし
            continue

        applied_count = pre_mask.sum()
        
        # 調整前の状態をディープコピーして保持
        before_ticker_data = ticker_data.copy()
        
        # パッチ適用を実行
        price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in ticker_data.columns]
        for col in price_cols:
            ticker_data.loc[pre_mask, col] = ticker_data.loc[pre_mask, col] * multiplier
        if "volume" in ticker_data.columns:
            ticker_data.loc[pre_mask, "volume"] = ticker_data.loc[pre_mask, "volume"] / multiplier

        ticker_data.loc[pre_mask, "patched_multiplier"] = multiplier

        # プレビュー表示用のサンプル作成
        # 調整されたデータの最新5行と、調整されなかった（target_dtより後）データの最古5行、計10行程度を並べる
        adjusted_idx = ticker_data[pre_mask].index
        unadjusted_idx = ticker_data[~pre_mask].index
        
        # 調整前のDFから、該当インデックスを抽出
        sample_indices = list(adjusted_idx[-5:]) + list(unadjusted_idx[:5])
        sample_indices = [idx for idx in sample_indices if idx in ticker_data.index]
        
        before_sample = before_ticker_data.loc[sample_indices].drop(columns=["date_dt"], errors="ignore")
        after_sample = ticker_data.loc[sample_indices].drop(columns=["date_dt"], errors="ignore")

        # 時間系列順にソート
        if "date" in before_sample.columns:
            before_sample = before_sample.sort_values("date")
            after_sample = after_sample.sort_values("date")

        test_results[interval] = {
            "applied_count": applied_count,
            "before_sample": before_sample,
            "after_sample": after_sample
        }

        # 戻り用の完全な調整後データフレームを構築
        repaired_ticker_data = ticker_data.drop(columns=["date_dt"], errors="ignore")
        
        # 全体DFと差し替え
        full_repaired_df = db_df[~mask].copy()
        full_repaired_df = pd.concat([full_repaired_df, repaired_ticker_data], ignore_index=True)
        full_repaired_df = full_repaired_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        
        temp_repaired_dfs[interval] = full_repaired_df

    if not test_results:
        return {"error": "対象銘柄または適用可能な未調整データが見つかりませんでした。"}, {}

    return test_results, temp_repaired_dfs

def apply_forced_scale_patch_to_all_timeframes(ticker: str, patch_date: str, multiplier: float, is_jp: bool = True) -> dict:
    """特定の日付以前の価格に一括パッチ適用。"""
    if multiplier <= 0:
        return {"error": f"処理を中断しました。倍率に 0 以下の数値（{multiplier}）は指定できません。"}

    pure_ticker = sanitize_ticker(ticker, is_jp)
    results = {}
    try:
        target_dt = pd.to_datetime(patch_date)
    except Exception as e:
        return {"error": f"要補正Close日時のパース失敗: {e}"}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            db_df = load_price_db(interval, is_jp=is_jp, is_raw=False)
        except FileNotFoundError:
            results[interval] = "DBなし"
            continue
        if db_df.empty:
            results[interval] = "データ空"
            continue

        mask = db_df["ticker"] == pure_ticker
        ticker_data = db_df[mask].copy()
        if ticker_data.empty:
            results[interval] = "対象データなし"
            continue

        if "patched_multiplier" not in ticker_data.columns:
            ticker_data["patched_multiplier"] = 1.0

        ticker_data["date_dt"] = pd.to_datetime(ticker_data["date"])
        pre_mask = (ticker_data["date_dt"] <= target_dt) & (ticker_data["patched_multiplier"] == 1.0)
        
        if not pre_mask.any():
            results[interval] = "スキップ（既に調整済み、または対象期間のデータなし）"
            continue

        price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in db_df.columns]
        for col in price_cols:
            ticker_data.loc[pre_mask, col] = ticker_data.loc[pre_mask, col] * multiplier
        if "volume" in db_df.columns:
            ticker_data.loc[pre_mask, "volume"] = ticker_data.loc[pre_mask, "volume"] / multiplier

        ticker_data.loc[pre_mask, "patched_multiplier"] = multiplier
        ticker_data = ticker_data.drop(columns=["date_dt"])

        db_df = db_df[~mask]
        db_df = pd.concat([db_df, ticker_data], ignore_index=True)
        db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
        save_price_db(db_df, interval, is_jp=is_jp, is_raw=False)
        results[interval] = f"{pre_mask.sum()}件補正適用完了"
    return results

def apply_all_saved_patches(is_jp: bool = True, status_callback=None) -> int:
    def log(msg):
        print(f"[CONSOLE_DEBUG] [APPLY_PATCH] {msg}")
        sys.stdout.flush()
        if status_callback: status_callback(msg)

    log("🛠️ [パッチ復元] 保存されたパッチ定義に基づいてActiveデータベースを安全に再構築します...")
    
    success_count = 0
    for interval in settings.TIMEFRAMES:
        success = rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=False, skip_assertion=False, status_callback=status_callback)
        if success:
            success_count += 1
            log(f"  👉 [{interval}] のパッチ適用およびActiveの再生成が正常に完了しました。")
            
    return success_count

def repair_single_ticker_all_timeframes(ticker: str, is_jp: bool = True, forced_split_ratio: float = None) -> dict:
    pure_ticker = sanitize_ticker(ticker, is_jp)
    symbol = get_download_symbol(pure_ticker, is_jp)
    now = datetime.now()
    results = {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            try:
                raw_db = load_price_db(interval, is_jp=is_jp, is_raw=True)
            except FileNotFoundError:
                raw_db = pd.DataFrame()

            old_raw = raw_db[raw_db["ticker"] == pure_ticker].copy() if not raw_db.empty else pd.DataFrame()
            if interval == "1m": start_date_dt = now - timedelta(days=6)
            elif interval == "5m": start_date_dt = now - timedelta(days=58)
            elif interval == "60m": start_date_dt = now - timedelta(days=718)
            else: start_date_dt = datetime(2016, 1, 1)

            df_raw = yf.download(symbol, start=start_date_dt.strftime("%Y-%m-%d"), interval=interval, auto_adjust=False, actions=True, progress=False)
            if df_raw.empty:
                results[interval] = "新規データ空（置換なし）"
                continue
            new_df = parse_yfinance_batch(df_raw, [pure_ticker], is_jp=is_jp)
            if new_df.empty:
                results[interval] = "パース結果空（置換なし）"
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
            
            save_price_db(raw_db, interval, is_jp=is_jp, is_raw=True)
            
            rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=False)
            results[interval] = f"個別ダウンロード・Active再生成成功 ({len(merged_raw):,}件)"
        except Exception as e:
            results[interval] = f"エラー: {str(e)}"

    return results

def delete_data_before_date(ticker: str, limit_date_str: str, is_jp: bool = True) -> dict:
    pure_ticker = sanitize_ticker(ticker, is_jp)
    limit_dt = pd.to_datetime(limit_date_str)
    results = {}

    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            df_raw = load_price_db(interval, is_jp=is_jp, is_raw=True)
            if not df_raw.empty:
                df_raw["temp_date"] = pd.to_datetime(df_raw["date"])
                mask_to_delete = (df_raw["ticker"] == pure_ticker) & (df_raw["temp_date"] <= limit_dt)
                deleted_count = mask_to_delete.sum()
                if deleted_count > 0:
                    df_raw = df_raw[~mask_to_delete].copy()
                    df_raw = df_raw.drop(columns=["temp_date"])
                    df_raw = df_raw.sort_values(["ticker", "date"]).reset_index(drop=True)
                    save_price_db(df_raw, interval, is_jp=is_jp, is_raw=True)
                    
                    rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=False)
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
# 🔍 診断関連関数群
# =====================================================================

def repair_stop_allocation_bars_full(is_jp: bool = True, status_callback=None) -> dict:
    def log(msg):
        print(f"[CONSOLE_DEBUG] [STOP_ALLOC] {msg}")
        sys.stdout.flush()
        if status_callback: status_callback(msg)

    log("📡 ストップ高安バーの修復処理として、Activeデータベースのリビルドを開始します...")
    results = {}
    for interval in ["60m", "5m", "1m"]:
        success = rebuild_active_from_raw(interval, is_jp=is_jp, dry_run=False, skip_assertion=False, status_callback=status_callback)
        if success:
            results[interval] = 1 
    return results

def run_database_health_scan(is_jp: bool) -> list:
    anomalies = []
    for interval in ["1d", "60m", "5m", "1m"]:
        try:
            df = load_price_db(interval, is_jp=is_jp, is_raw=False) 
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

def scan_all_anomalies(is_jp: bool = True, interval: str = "1d", threshold: float = 0.35) -> pd.DataFrame:
    try:
        db_df = load_price_db(interval, is_jp=is_jp, is_raw=False) 
    except FileNotFoundError:
        return pd.DataFrame()
    if db_df.empty:
        return pd.DataFrame()

    db_df = db_df.sort_values(["ticker", "date"]).reset_index(drop=True)
    has_adj = "adj close" in db_df.columns
    result_rows = []

    negative_mask = db_df["close"] < 0
    shifted_neg_mask_for_pos = db_df.groupby("ticker")["close"].apply(lambda x: (x < 0).shift(1, fill_value=True)).reset_index(level=0, drop=True)
    pos_to_neg = negative_mask & (~shifted_neg_mask_for_pos)
    shifted_neg_mask_for_neg = db_df.groupby("ticker")["close"].apply(lambda x: (x < 0).shift(1, fill_value=False)).reset_index(level=0, drop=True)
    neg_to_pos = (~negative_mask) & shifted_neg_mask_for_neg
    boundary_mask = pos_to_neg | neg_to_pos
    
    if boundary_mask.any():
        boundary_rows = db_df[boundary_mask].copy()
        boundary_rows["before_date"] = db_df.groupby("ticker")["date"].shift(1)[boundary_mask].values
        boundary_rows["before_close"] = db_df.groupby("ticker")["close"].shift(1)[boundary_mask].values
        boundary_rows["after_close"] = boundary_rows["close"]
        if has_adj:
            boundary_rows["before_adj_close"] = db_df.groupby("ticker")["adj close"].shift(1)[boundary_mask].values
            boundary_rows["after_adj_close"] = boundary_rows["adj close"]
        else:
            boundary_rows["before_adj_close"] = float("nan")
            boundary_rows["after_adj_close"] = float("nan")

        boundary_rows["pct_change"] = float("nan")
        for col in ["open", "high", "low", "volume"]:
            if col not in boundary_rows.columns:
                boundary_rows[col] = float("nan")
        result_rows.append(boundary_rows[["ticker", "date", "before_date", "before_close", "after_close", "before_adj_close", "after_adj_close", "open", "high", "low", "volume", "pct_change"]])

    abs_close = db_df["close"].abs()
    pct = abs_close.groupby(db_df["ticker"]).pct_change()
    cliff_mask = pct.abs() >= threshold

    if cliff_mask.any():
        cliff_rows = db_df[cliff_mask].copy()
        cliff_rows["before_date"] = db_df.groupby("ticker")["date"].shift(1)[cliff_mask].values
        cliff_rows["before_close"] = db_df.groupby("ticker")["close"].shift(1)[cliff_mask].values
        cliff_rows["after_close"] = cliff_rows["close"]
        if has_adj:
            cliff_rows["before_adj_close"] = db_df.groupby("ticker")["adj close"].shift(1)[cliff_mask].values
            cliff_rows["after_adj_close"] = cliff_rows["adj close"]
        else:
            cliff_rows["before_adj_close"] = float("nan")
            cliff_rows["after_adj_close"] = float("nan")

        cliff_rows["pct_change"] = pct[cliff_mask].values
        for col in ["open", "high", "low", "volume"]:
            if col not in cliff_rows.columns:
                cliff_rows[col] = float("nan")
        result_rows.append(cliff_rows[["ticker", "date", "before_date", "before_close", "after_close", "before_adj_close", "after_adj_close", "open", "high", "low", "volume", "pct_change"]])

    if not result_rows:
        return pd.DataFrame()
        
    result = pd.concat(result_rows, ignore_index=True).rename(columns={"date": "cliff_date"})
    
    def aggregate_anomalies(group):
        pct_vals = group["pct_change"].dropna()
        pct_val = pct_vals.iloc[0] if not pct_vals.empty else float("nan")
        
        before_date_val = group["before_date"].dropna().iloc[0] if not group["before_date"].dropna().empty else pd.NaT
        before_close_val = group["before_close"].dropna().iloc[0] if not group["before_close"].dropna().empty else float("nan")
        after_close_val = group["after_close"].dropna().iloc[0] if not group["after_close"].dropna().empty else float("nan")
        before_adj_val = group["before_adj_close"].dropna().iloc[0] if not group["before_adj_close"].dropna().empty else float("nan")
        after_adj_val = group["after_adj_close"].dropna().iloc[0] if not group["after_adj_close"].dropna().empty else float("nan")
        
        open_val = group["open"].dropna().iloc[0] if not group["open"].dropna().empty else float("nan")
        high_val = group["high"].dropna().iloc[0] if not group["high"].dropna().empty else float("nan")
        low_val = group["low"].dropna().iloc[0] if not group["low"].dropna().empty else float("nan")
        vol_val = group["volume"].dropna().iloc[0] if not group["volume"].dropna().empty else float("nan")
        
        est_multiplier = float("nan")
        if before_close_val != 0 and pd.notna(before_close_val) and pd.notna(after_close_val):
            est_multiplier = after_close_val / before_close_val
            
        return pd.Series({
            "before_date": before_date_val,
            "before_close": before_close_val, 
            "after_close": after_close_val, 
            "before_adj_close": before_adj_val, 
            "after_adj_close": after_adj_val, 
            "open": open_val,
            "high": high_val,
            "low": low_val,
            "volume": vol_val,
            "est_multiplier": est_multiplier,
            "pct_change": pct_val
        })
        
    result = result.groupby(["ticker", "cliff_date"], as_index=False).apply(aggregate_anomalies)
    result["patch_date"] = pd.to_datetime(result["before_date"])
    return result.sort_values(["ticker", "cliff_date"]).reset_index(drop=True)

# =====================================================================
# 🌐 TradingView 照合付き統合スキャン ＆ 一括自動修復エンジン
# =====================================================================

_TV_CLIENT_FALLBACK = None

def _create_tv_client_instance():
    try:
        from tvDatafeed import TvDatafeed
        return TvDatafeed()
    except Exception:
        return False

if settings.HAS_STREAMLIT:
    import streamlit as st

    @st.cache_resource(show_spinner=False)
    def _get_tv_client_cached():
        return _create_tv_client_instance()

def _get_tv_client():
    if settings.HAS_STREAMLIT:
        return _get_tv_client_cached()

    global _TV_CLIENT_FALLBACK
    if _TV_CLIENT_FALLBACK is None:
        _TV_CLIENT_FALLBACK = _create_tv_client_instance()
    return _TV_CLIENT_FALLBACK

JP_INDEX_TICKER_TV_MAP = {
    "^N225": {"symbol": "NI225", "exchange": "TVC"},
    "1306.T": {"symbol": "1306", "exchange": "TSE"},
}
US_INDEX_TICKER_TV_MAP = {
    "^GSPC": {"symbol": "SPX", "exchange": "TVC"},
    "^NDX": {"symbol": "NDX", "exchange": "NASDAQ"},
    "^DJI": {"symbol": "DJI", "exchange": "TVC"},
}

def map_ticker_to_tv_symbol(ticker: str, is_jp: bool = True) -> dict:
    raw_ticker = str(ticker).strip()
    index_map = JP_INDEX_TICKER_TV_MAP if is_jp else US_INDEX_TICKER_TV_MAP
    if raw_ticker in index_map:
        return index_map[raw_ticker]

    pure_ticker = sanitize_ticker(raw_ticker, is_jp)
    if is_jp:
        return {"symbol": pure_ticker, "exchange": "TSE"}
    tv_symbol = pure_ticker.replace("-", ".") if "-" in pure_ticker else pure_ticker
    return {"symbol": tv_symbol, "exchange": None}

def fetch_tv_close_price(ticker: str, cliff_date, is_jp: bool = True):
    tv = _get_tv_client()
    if not tv:
        return None

    try:
        from tvDatafeed import Interval as TvInterval
    except Exception:
        return None

    try:
        target_date = pd.Timestamp(cliff_date).normalize()
    except Exception:
        return None

    mapped = map_ticker_to_tv_symbol(ticker, is_jp)
    symbol = mapped["symbol"]
    fixed_exchange = mapped.get("exchange")
    exchange_candidates = [fixed_exchange] if fixed_exchange else ["NASDAQ", "NYSE", "AMEX"]
    days_back = int(max((pd.Timestamp.now().normalize() - target_date).days + 30, 60))

    for exchange in exchange_candidates:
        try:
            hist = tv.get_hist(symbol=symbol, exchange=exchange, interval=TvInterval.in_daily, n_bars=days_back)
        except Exception:
            continue
        if hist is None or hist.empty:
            continue
        hist = hist.copy()
        hist.index = pd.to_datetime(hist.index).normalize()
        if target_date in hist.index:
            try:
                return float(hist.loc[target_date, "close"])
            except Exception:
                continue
    return None

def fetch_tv_close_pair(ticker: str, patch_date, is_jp: bool = True) -> dict:
    empty = {"tv_before_close": None, "tv_after_close": None}
    tv = _get_tv_client()
    if not tv:
        return empty

    try:
        from tvDatafeed import Interval as TvInterval
    except Exception:
        return empty

    try:
        before_date = pd.Timestamp(patch_date).normalize()
    except Exception:
        return empty

    mapped = map_ticker_to_tv_symbol(ticker, is_jp)
    symbol = mapped["symbol"]
    fixed_exchange = mapped.get("exchange")
    exchange_candidates = [fixed_exchange] if fixed_exchange else ["NASDAQ", "NYSE", "AMEX"]
    days_back = int(max((pd.Timestamp.now().normalize() - before_date).days + 30, 60))

    for exchange in exchange_candidates:
        try:
            hist = tv.get_hist(symbol=symbol, exchange=exchange, interval=TvInterval.in_daily, n_bars=days_back)
        except Exception:
            continue
        if hist is None or hist.empty:
            continue
        hist = hist.copy()
        hist.index = pd.to_datetime(hist.index).normalize()
        hist = hist.sort_index()

        tv_before_close = None
        if before_date in hist.index:
            try:
                tv_before_close = float(hist.loc[before_date, "close"])
            except Exception:
                tv_before_close = None

        tv_after_close = None
        after_candidates = hist[hist.index > before_date]
        if not after_candidates.empty:
            try:
                tv_after_close = float(after_candidates.iloc[0]["close"])
            except Exception:
                tv_after_close = None

        if tv_before_close is not None or tv_after_close is not None:
            return {"tv_before_close": tv_before_close, "tv_after_close": tv_after_close}

    return empty

def scan_and_diagnose_cliffs_with_tv(is_jp: bool = True, intervals: list = None) -> pd.DataFrame:
    target_intervals = intervals if intervals else list(settings.TIMEFRAMES)
    per_interval_dfs = {}
    for iv in target_intervals:
        df_iv = scan_all_anomalies(is_jp=is_jp, interval=iv)
        if not df_iv.empty:
            df_iv = df_iv.copy()
            df_iv["interval"] = iv
        per_interval_dfs[iv] = df_iv

    if "1d" in per_interval_dfs and not per_interval_dfs["1d"].empty:
        df_1d = per_interval_dfs["1d"]
        daily_flagged_pairs = set(
            zip(df_1d["ticker"], pd.to_datetime(df_1d["cliff_date"]).dt.normalize())
        )
        
        for iv in target_intervals:
            if iv == "1d" or per_interval_dfs.get(iv) is None or per_interval_dfs[iv].empty:
                continue
            df_iv = per_interval_dfs[iv]
            
            iv_dates = pd.to_datetime(df_iv["cliff_date"]).dt.normalize()
            iv_pairs = list(zip(df_iv["ticker"], iv_dates))
            
            keep_mask = [pair not in daily_flagged_pairs for pair in iv_pairs]
            per_interval_dfs[iv] = df_iv[keep_mask].reset_index(drop=True)

    non_empty = [df for df in per_interval_dfs.values() if df is not None and not df.empty]
    if not non_empty:
        return pd.DataFrame()

    result = pd.concat(non_empty, ignore_index=True)
    result["tv_close"] = float("nan")       
    result["tv_after_close"] = float("nan")  
    result["true_multiplier"] = float("nan")

    for idx, row in result.iterrows():
        interval = row["interval"]
        ticker = row["ticker"]
        patch_date = row.get("patch_date")
        before_close = row.get("before_close", float("nan"))
        after_close = row.get("after_close", float("nan"))

        if interval != "1d" or pd.isna(patch_date):
            result.at[idx, "true_multiplier"] = row.get("est_multiplier", float("nan"))
            continue

        tv_pair = fetch_tv_close_pair(ticker, patch_date, is_jp=is_jp)
        tv_before_close = tv_pair.get("tv_before_close")
        tv_after_close = tv_pair.get("tv_after_close")

        if tv_before_close is not None:
            result.at[idx, "tv_close"] = tv_before_close
        if tv_after_close is not None:
            result.at[idx, "tv_after_close"] = tv_after_close

        if tv_before_close is not None and pd.notna(before_close) and before_close != 0:
            result.at[idx, "true_multiplier"] = tv_before_close / before_close
        elif (
            tv_before_close is not None and tv_after_close is not None and tv_before_close != 0
            and pd.notna(before_close) and pd.notna(after_close) and before_close != 0
        ):
            tv_ratio = tv_after_close / tv_before_close
            self_ratio = after_close / before_close
            if self_ratio != 0:
                result.at[idx, "true_multiplier"] = tv_ratio / self_ratio
        else:
            result.at[idx, "true_multiplier"] = row.get("est_multiplier", float("nan"))

    drop_cols = [c for c in ["open", "high", "low", "pct_change"] if c in result.columns]
    result = result.drop(columns=drop_cols)
    return result.sort_values(["ticker", "cliff_date", "interval"]).reset_index(drop=True)

def apply_bulk_selected_patches(patches: list, is_jp: bool = True, status_callback=None) -> dict:
    """一括パッチ適用。"""
    def log(msg):
        print(f"[CONSOLE_DEBUG] [BULK_PATCH] {msg}")
        sys.stdout.flush()
        if status_callback: status_callback(msg)

    market_str = "JP" if is_jp else "US"
    repaired_count = 0
    skipped_count = 0
    log_rows = []

    for patch in patches:
        ticker = patch.get("ticker")
        patch_date = patch.get("patch_date")
        multiplier = patch.get("multiplier")

        if not ticker or not patch_date or multiplier is None or pd.isna(multiplier) or multiplier <= 0:
            log(f"⚠️ [{ticker}] 真の倍率が取得できていないため、この行はスキップしました。")
            skipped_count += 1
            continue

        pure_t = sanitize_ticker(ticker, is_jp)
        try:
            patch_dt_str = pd.to_datetime(patch_date).strftime("%Y-%m-%d")
        except Exception:
            log(f"⚠️ [{ticker}] 要補正Close日時が不正なためスキップしました。")
            skipped_count += 1
            continue

        log(f"🔧 [{pure_t}] {patch_dt_str}（要補正Close日時）以前の一括修復パッチを判定・適用中（倍率: {multiplier:.6f}）...")
        results = apply_forced_scale_patch_to_all_timeframes(pure_t, patch_dt_str, multiplier, is_jp=is_jp)
        applied_intervals = [iv for iv, msg in results.items() if "補正適用完了" in str(msg)]

        if applied_intervals:
            repaired_count += 1
            log(f"   ✅ 適用完了 ({', '.join(applied_intervals)})")
            log_rows.append({
                "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ticker": pure_t,
                "market": market_str,
                "cliff_date": patch_dt_str,
                "interval": ",".join(applied_intervals),
                "before_close": "",
                "after_close": "",
                "multiplier": multiplier,
                "memo": "統合スキャン・一括自動修復（TradingView照合）",
            })
        else:
            skipped_count += 1
            log(f"   ⏭️ スキップ（すでに修復済み、または対象データなし）")

    if log_rows:
        try:
            from data_access.sheets_api import save_repair_log_to_sheets
            save_repair_log_to_sheets(log_rows)
            log(f"📝 実際に修復が実行された {len(log_rows)} 件のみをrepair_logへ記録しました。")
        except Exception as e:
            log(f"⚠️ 修復ログの保存に失敗しました: {e}")

    return {"repaired": repaired_count, "skipped": skipped_count}

def analyze_db_update_needs(is_jp: bool = True) -> dict:
    try:
        db_df = load_price_db("1d", is_jp=is_jp, is_raw=True) 
        all_tickers = get_all_collection_tickers() if is_jp else ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"]
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

def rebuild_single_ticker_1d_raw(ticker: str, is_jp: bool = True):
    pure_ticker = sanitize_ticker(ticker, is_jp)
    symbol = get_download_symbol(pure_ticker, is_jp)
    start_date = "2016-01-01"
    try:
        df_new = yf.download(symbol, start=start_date, interval="1d", auto_adjust=False, actions=True, progress=False)
        if not df_new.empty:
            parsed = parse_yfinance_batch(df_new, [pure_ticker], is_jp=is_jp)
            if not parsed.empty:
                try:
                    raw_db = load_price_db("1d", is_jp=is_jp, is_raw=True)
                except FileNotFoundError:
                    raw_db = pd.DataFrame()
                    
                if not raw_db.empty:
                    raw_db = raw_db[raw_db["ticker"] != pure_ticker]
                    
                raw_db = pd.concat([raw_db, parsed], ignore_index=True)
                raw_db = raw_db.sort_values(["ticker", "date"]).reset_index(drop=True)
                save_price_db(raw_db, "1d", is_jp=is_jp, is_raw=True)
                print(f"✅ [rebuild_single_ticker_1d_raw] 銘柄 {pure_ticker} の 1d Raw データベースをフル再構築しました。")
    except Exception as e:
        print(f"❌ [rebuild_single_ticker_1d_raw] エラー: {e}")

def finalize_latest_with_tradingview_in_df(df: pd.DataFrame, interval: str, is_jp: bool = True) -> pd.DataFrame:
    if df.empty:
        return df
        
    try:
        from tradingview_screener import Query
        tickers = df["ticker"].unique().tolist()
        if not tickers:
            return df
            
        tv_symbols = []
        symbol_to_ticker = {}
        for t in tickers:
            if is_jp:
                tv_sym = f"TSE:{t}"
            else:
                clean_t = t.replace("-", ".")
                us_exchanges = {
                    "AAPL": "NASDAQ", "MSFT": "NASDAQ", "NVDA": "NASDAQ", "GOOGL": "NASDAQ", "META": "NASDAQ",
                    "AMZN": "NASDAQ", "AMD": "NASDAQ", "AVGO": "NASDAQ", "QCOM": "NASDAQ", "MU": "NASDAQ",
                    "INTC": "NASDAQ", "TSLA": "NASDAQ", "NFLX": "NASDAQ",
                    "JPM": "NYSE", "BAC": "NYSE", "GS": "NYSE", "MS": "NYSE", "WFC": "NYSE",
                    "BRK.B": "NYSE", "BRK-B": "NYSE", "JNJ": "NYSE", "UNH": "NYSE", "LLY": "NYSE",
                    "ABBV": "NYSE", "MRK": "NYSE", "XOM": "NYSE", "CVX": "NYSE", "COP": "NYSE",
                    "SLB": "NYSE", "EOG": "NYSE", "HD": "NYSE", "MCD": "NYSE", "NKE": "NYSE",
                    "T": "NYSE", "VZ": "NYSE", "NEE": "NYSE", "DUK": "NYSE", "SO": "NYSE",
                    "AEP": "NYSE", "D": "NYSE", "LIN": "NYSE", "APD": "NYSE", "FCX": "NYSE",
                    "NEM": "NYSE", "DOW": "NYSE"
                }
                exc = us_exchanges.get(clean_t, "NASDAQ")
                tv_sym = f"{exc}:{clean_t}"
            tv_symbols.append(tv_sym)
            symbol_to_ticker[tv_sym] = t
            symbol_to_ticker[t.replace("-", ".")] = t

        total_rows, tv_df = Query().set_tickers(*tv_symbols).select('open', 'high', 'low', 'close', 'volume').get_scanner_data()
        if tv_df.empty:
            return df
        
        tv_data = {}
        for idx, r in tv_df.iterrows():
            ticker_key = symbol_to_ticker.get(idx)
            if not ticker_key:
                clean_idx = idx.split(":")[-1] if ":" in idx else idx
                ticker_key = symbol_to_ticker.get(clean_idx)
            if ticker_key:
                tv_data[ticker_key] = {
                    "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                    "close": float(r["close"]), "volume": float(r["volume"])
                }

        df = df.copy()
        df["date_dt"] = pd.to_datetime(df["date"])
        
        latest_day = df["date_dt"].dt.normalize().max()
        latest_day_mask = df["date_dt"].dt.normalize() == latest_day
        
        for ticker, data in tv_data.items():
            ticker_today_mask = latest_day_mask & (df["ticker"] == ticker)
            if not ticker_today_mask.any():
                continue
                
            if interval == "1d":
                df.loc[ticker_today_mask, "open"] = data["open"]
                df.loc[ticker_today_mask, "high"] = data["high"]
                df.loc[ticker_today_mask, "low"] = data["low"]
                df.loc[ticker_today_mask, "close"] = data["close"]
                df.loc[ticker_today_mask, "volume"] = data["volume"]
                if "adj close" in df.columns:
                    df.loc[ticker_today_mask, "adj close"] = data["close"]
                    
            else:
                ticker_today_rows = df[ticker_today_mask]
                max_timestamp = ticker_today_rows["date_dt"].max()
                
                last_bar_mask = ticker_today_mask & (df["date_dt"] == max_timestamp)
                previous_bars_mask = ticker_today_mask & (df["date_dt"] < max_timestamp)
                
                previous_vol_sum = df.loc[previous_bars_mask, "volume"].sum()
                calculated_vol = max(0.0, data["volume"] - previous_vol_sum)
                
                existing_high = df.loc[last_bar_mask, "high"].values[0] if not df.loc[last_bar_mask, "high"].empty else data["high"]
                existing_low = df.loc[last_bar_mask, "low"].values[0] if not df.loc[last_bar_mask, "low"].empty else data["low"]
                
                df.loc[last_bar_mask, "close"] = data["close"]
                df.loc[last_bar_mask, "high"] = max(existing_high, data["close"])
                df.loc[last_bar_mask, "low"] = min(existing_low, data["close"])
                df.loc[last_bar_mask, "volume"] = calculated_vol
                if "adj close" in df.columns:
                    df.loc[last_bar_mask, "adj close"] = data["close"]

        df = df.drop(columns=["date_dt"])
        return df
    except Exception as e:
        print(f"⚠️ [finalize_latest_with_tradingview_in_df] エラー: {e}")
        return df

def get_current_memory_usage() -> str:
    """現在のプロセスが物理的に使用しているメモリ量（RSS）をMB単位で返します。"""
    # ── 1. Linuxシステムファイルを最優先で直接読み込み (Streamlit Cloudなどの環境) ──
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        kb = int(parts[1])
                        return f"{kb / 1024:.1f} MB"
    except Exception:
        pass

    # ── 2. psutil を予備として実行 (Windows/Mac などのローカル環境向け) ──
    try:
        import os
        import psutil
        process = psutil.Process(os.getpid())
        mem_bytes = process.memory_info().rss
        return f"{mem_bytes / (1024 * 1024):.1f} MB"
    except Exception:
        pass

    return "取得不可"

# ── 追加関数1: 日常インクリメンタル更新用Active加工アペンド ──
def incremental_update_active(interval: str, is_jp: bool = True, new_raw_diff: pd.DataFrame = None, repair_log_df: pd.DataFrame = None) -> bool:
    """
    日常インクリメンタル更新用:
    ダウンロードされた最新差分データ(new_raw_diff)に対してのみActive加工処理を施し、
    既存のActiveデータベースの末尾にアペンド（結合）して上書き保存します。
    """
    if new_raw_diff is None or new_raw_diff.empty:
        return True
        
    import gc
    import pandas as pd
    
    # 1. 差分データに基本的な finalized フラグを付与
    df_diff = new_raw_diff.copy()
    df_diff["is_finalized"] = compute_is_finalized(df_diff["date"], interval, is_jp=is_jp)
    
    if "split_multiplier" not in df_diff.columns:
        df_diff["split_multiplier"] = 1.0
    if "patched_multiplier" not in df_diff.columns:
        df_diff["patched_multiplier"] = 1.0
        
    # 2. 過去パッチの適用判定（最新の差分期間に含まれている場合のみ）
    df_diff = apply_saved_patches_to_df(df_diff, is_jp=is_jp, repair_log_df=repair_log_df)
        
    # 3. ストップ高安の補完（分足のみ、差分期間内の対象日Closeを参照して合成）
    if interval != "1d":
        try:
            # 差分銘柄に限定して 1d active をフィルタロード
            target_tickers = df_diff["ticker"].unique().tolist()
            df_1d_active = load_price_db_for_tickers("1d", target_tickers, is_jp=is_jp, is_raw=False)
            df_diff = propagate_stop_allocation_bars_in_memory(df_1d_active, df_diff, is_jp=is_jp)
            del df_1d_active
        except Exception:
            pass
            
    # 4. TradingView終値の反映・確定
    df_diff = finalize_latest_with_tradingview_in_df(df_diff, interval, is_jp=is_jp)
    
    # 5. 既存のActiveデータベースに結合して保存（台帳メタデータ自動更新）
    try:
        df_active = load_price_db(interval, is_jp=is_jp, is_raw=False)
    except FileNotFoundError:
        df_active = pd.DataFrame()
        
    if not df_active.empty:
        df_active = pd.concat([df_active, df_diff], ignore_index=True)
        df_active = df_active.drop_duplicates(subset=["date", "ticker"], keep="last")
    else:
        df_active = df_diff
        
    df_active = df_active.sort_values(["ticker", "date"]).reset_index(drop=True)
    success, msg = save_price_db(df_active, interval, is_jp=is_jp, is_raw=False)
    
    del df_active, df_diff
    gc.collect()
    return success

# ── 追加関数2: 株式分割や手動パッチ時の部分上書き遡及加工フロー ──
def partial_rebuild_active_for_tickers(interval: str, tickers: list, is_jp: bool = True, repair_log_df: pd.DataFrame = None) -> bool:
    """
    株式分割やパッチ修正が発生した特定銘柄(tickers)に対してのみ、遡及計算を実行してACTIVE Parquetを部分上書き更新します。
    他銘柄の実データはメモリに載せないため、省メモリ且つ安全に動作します。
    """
    import gc
    from data_access.local_db import load_price_db_for_tickers, load_price_db_excluding_tickers
    
    if not tickers:
        return True
        
    print(f"[CONSOLE_DEBUG] 🛠️ [{interval}] 対象銘柄の部分上書き遡及加工を開始します: {tickers}")
    
    # 1. RAWから対象銘柄の過去データのみをフィルタロード
    df_raw_target = load_price_db_for_tickers(interval, tickers, is_jp=is_jp, is_raw=True)
    if df_raw_target.empty:
        return True
        
    # 2. 遡及計算の適用
    df_raw_target["is_finalized"] = compute_is_finalized(df_raw_target["date"], interval, is_jp=is_jp)
    
    if "split_multiplier" not in df_raw_target.columns:
        df_raw_target["split_multiplier"] = 1.0
    if "patched_multiplier" not in df_raw_target.columns:
        df_raw_target["patched_multiplier"] = 1.0
        
    # 株式分割の過去適用
    if "stock splits" in df_raw_target.columns:
        split_events = df_raw_target[(df_raw_target["stock splits"] > 0) & (df_raw_target["stock splits"] != 1.0)]
        if not split_events.empty:
            price_cols = [c for c in ["open", "high", "low", "close", "adj close"] if c in df_raw_target.columns]
            for ticker in tickers:
                t_mask = df_raw_target["ticker"] == ticker
                group = df_raw_target[t_mask].copy().sort_values("date")
                adjusted = adjust_ticker_splits_backward_in_memory(group)
                if not adjusted.empty:
                    df_raw_target.loc[df_raw_target["ticker"] == ticker, price_cols + ["volume", "split_multiplier"]] = adjusted[price_cols + ["volume", "split_multiplier"]]

    # パッチの遡及適用
    df_raw_target = apply_saved_patches_to_df(df_raw_target, is_jp=is_jp, repair_log_df=repair_log_df)
    
    # ストップ高安の補完
    if interval != "1d":
        try:
            df_1d_active = load_price_db_for_tickers("1d", tickers, is_jp=is_jp, is_raw=False)
            df_raw_target = propagate_stop_allocation_bars_in_memory(df_1d_active, df_raw_target, is_jp=is_jp)
            del df_1d_active
        except Exception:
            pass

    # 3. ACTIVE Parquetから対象銘柄以外をロードし、作成したデータと結合して保存
    df_active_other = load_price_db_excluding_tickers(interval, tickers, is_jp=is_jp)
    df_active_new = pd.concat([df_active_other, df_raw_target], ignore_index=True)
    df_active_new = df_active_new.sort_values(["ticker", "date"]).reset_index(drop=True)
    
    success, msg = save_price_db(df_active_new, interval, is_jp=is_jp, is_raw=False)
    
    del df_raw_target, df_active_other, df_active_new
    gc.collect()
    return success