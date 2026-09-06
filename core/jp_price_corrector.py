# core/jp_price_corrector.py

import os
import re
import time
from datetime import datetime
import pandas as pd
import numpy as np
import yfinance as yf
from config import settings
from data_access.local_db import load_price_db, save_price_db
from data_access.drive_api import (
    download_from_drive_api,
    upload_to_drive_api,
    get_or_create_drive_folder,
    get_drive_service
)

def scan_jp_anomalies_with_yfinance(status_callback=None) -> pd.DataFrame:
    """
    本番日足データ(price_jp_1d.parquet)から監視銘柄リストのデータをロードし、
    yfinanceから一括（バルク）で株式分割履歴を一時取得して自動診断スキャンを行います。
    100銘柄ずつのバッチ処理により、外部通信回数を最小限に抑えます。
    """
    def log(msg):
        if status_callback:
            try: status_callback(msg)
            except Exception: pass
        print(f"[JP_ANOMALIES_SCAN] {msg}")

    log("📊 日本株本番日足データ (price_jp_1d) をロード中...")
    try:
        # 日足の診断に必要なカラムのみを投影ロード
        df_1d = load_price_db(
            interval="1d",
            is_jp=True,
            is_raw=False,
            columns=["date", "ticker", "close", "patched_multiplier"]
        )
    except Exception as e:
        log(f"❌ 日足データのロードに失敗しました: {e}")
        return pd.DataFrame()

    if df_1d.empty:
        log("⚠️ 日本株日足データが見つかりません。先にデータ収集・マージを行ってください。")
        return pd.DataFrame()

    # patched_multiplier カラムが存在しない場合は一律 1.0 で初期化
    if "patched_multiplier" not in df_1d.columns:
        df_1d["patched_multiplier"] = 1.0

    df_1d["date_dt"] = pd.to_datetime(df_1d["date"]).dt.tz_localize(None)
    tickers = df_1d["ticker"].unique().tolist()
    
    total = len(tickers)
    batch_size = 100
    
    # 照合の開始日をDBの最も古い日付から動的に決定（安全策）
    start_date_str = df_1d["date_dt"].min().strftime("%Y-%m-%d") if not df_1d.empty else "2016-01-01"
    
    log(f"🔎 データベース内から {total} 銘柄を検出。")
    log(f"🚀 {batch_size} 銘柄ずつのバッチで一括照合を開始します... (取得期間: {start_date_str} 〜 現在)")

    # 100銘柄ごとのバッチに分割
    ticker_batches = [tickers[i:i + batch_size] for i in range(0, total, batch_size)]
    results = []

    for b_idx, batch in enumerate(ticker_batches):
        batch_tickers_T = [f"{t}.T" for t in batch]
        log(f"📥 バッチ [{b_idx + 1}/{len(ticker_batches)}] 処理中... (対象: {len(batch)} 銘柄 | {', '.join(batch[:4])}...)")
        
        try:
            # yf.download を用いて100銘柄分のデータを一括取得
            # actions=True により、Stock Splits (分割履歴) データを同時に取得します。
            df_download = yf.download(
                batch_tickers_T,
                start=start_date_str,
                interval="1d",
                auto_adjust=False,
                actions=True,
                progress=False,
                threads=True,
                timeout=30
            )
            
            if df_download.empty:
                log(f"  ⚠️ バッチ [{b_idx + 1}] のデータが空です。次のバッチに進みます。")
                continue
            
            # yfinanceのバルクダウンロード時の列構造（MultiIndex）から Stock Splits 部分を抽出
            df_splits = pd.DataFrame()
            if isinstance(df_download.columns, pd.MultiIndex):
                if "Stock Splits" in df_download.columns.get_level_values(0):
                    df_splits = df_download["Stock Splits"]
            else:
                # 1銘柄しか該当しないバッチの場合の安全なカラム処理
                if "Stock Splits" in df_download.columns:
                    df_splits = pd.DataFrame(df_download["Stock Splits"])
                    df_splits.columns = batch_tickers_T[:1]
            
            if df_splits.empty:
                log(f"  ⚠️ バッチ [{b_idx + 1}] の結果に分割データが見つかりませんでした。")
                continue

            # 各銘柄に対して照合・判定
            for ticker in batch:
                ticker_with_T = f"{ticker}.T"
                if ticker_with_T not in df_splits.columns:
                    continue
                
                # splitsから0.0やNaNを除外して有効な分割履歴を抽出
                splits_series_raw = df_splits[ticker_with_T]
                splits_series = splits_series_raw[(splits_series_raw > 0) & (splits_series_raw != 1.0)].dropna()
                
                if splits_series.empty:
                    continue
                
                df_ticker = df_1d[df_1d["ticker"] == ticker].sort_values("date_dt").reset_index(drop=True)
                if df_ticker.empty or len(df_ticker) < 2:
                    continue

                for ex_date, s_val in splits_series.items():
                    if pd.isna(s_val) or s_val <= 0.0 or s_val == 1.0:
                        continue
                    
                    # T_dtは分割権利落ち日(ex-date)
                    T_dt = pd.to_datetime(ex_date).tz_localize(None)
                    
                    # 1dデータの中から境界日 T に最も近い、かつ >= T である最初の営業日を探す
                    future_df = df_ticker[df_ticker["date_dt"] >= T_dt]
                    if future_df.empty:
                        continue
                    
                    idx_T = future_df.index[0] # ex-date当日のインデックス
                    if idx_T == 0:
                        # 分割実施前の日足データが存在しない
                        continue
                    
                    row_T = df_ticker.loc[idx_T]
                    row_prev = df_ticker.loc[idx_T - 1]
                    
                    # 前後の営業日の乖離が大きすぎる(15日超)場合は取引停止中などのため除外
                    gap_days = (row_T["date_dt"] - row_prev["date_dt"]).days
                    if gap_days > 15:
                        continue
                    
                    close_T = row_T["close"]
                    close_prev = row_prev["close"]
                    mult_T = row_T["patched_multiplier"]
                    mult_prev = row_prev["patched_multiplier"]
                    
                    if pd.isna(close_T) or pd.isna(close_prev) or close_T <= 0 or close_prev <= 0:
                        continue
                        
                    R_price = close_prev / close_T
                    R_multiplier = mult_prev / mult_T
                    
                    S = float(s_val)
                    M = 1.0 / S
                    
                    # 各条件の判定 (マージン 15%)
                    is_adjusted = (abs(R_multiplier - M) <= M * 0.15) and (abs(R_price - 1.0) <= 0.15)
                    is_unadjusted = (abs(R_multiplier - 1.0) <= 0.15) and (abs(R_price - S) <= S * 0.15)
                    is_pre_adjusted = (abs(R_multiplier - 1.0) <= 0.15) and (abs(R_price - 1.0) <= 0.15)
                    
                    mode = None
                    if is_unadjusted:
                        mode = "要パッチ"
                    elif is_pre_adjusted:
                        mode = "メタデータのみ更新"
                        
                    if mode:
                        results.append({
                            "ticker": ticker,
                            "interval": "1d",
                            "cliff_date": T_dt.strftime("%Y-%m-%d"),
                            "splits": S,
                            "mode": mode,
                            "multiplier": M,
                            "before_close": round(close_prev, 2),
                            "after_close": round(close_T, 2)
                        })
            
            # アクセス間の負荷調整用のわずかなウェイト
            time.sleep(1.0)

        except Exception as ex:
            log(f"  ⚠️ バッチ [{b_idx + 1}] 処理中に例外検出 (スキップ): {ex}")
            continue

    log(f"🎉 日本株段差スキャン完了。修正候補: {len(results)} 件")
    return pd.DataFrame(results)

def apply_jp_patch_to_all_timeframes(ticker: str, cliff_date: str, multiplier: float, mode: str, status_callback=None) -> dict:
    """
    日本株の全時間足（1d, 60m, 5m, 1m）の本番Parquetに対し、
    安全かつ可逆的に遡及一括調整パッチを適用します。
    """
    def log(msg):
        if status_callback:
            try: status_callback(msg)
            except Exception: pass
        print(f"[JP_PATCH_ENGINE] {msg}")

    # 入力値の下限物理アサーションロック
    if multiplier <= 0.0:
        raise ValueError(f"⚠️ [安全ロック作動] 不適切な調整倍率 ({multiplier}) が検出されました。倍率は必ず0より大きい必要があります。")

    cliff_dt = pd.to_datetime(cliff_date)
    cliff_year = cliff_dt.year
    cliff_month = cliff_dt.month
    
    timeframes = ["1d", "60m", "5m", "1m"]
    results = {}

    for interval in timeframes:
        log(f"⏱️ 【日本株 {interval}】パッチ適用処理中...")
        
        try:
            tf_folder_id = get_or_create_drive_folder(interval, settings.FOLDER_ID)
            service = get_drive_service()
            if not service:
                raise ConnectionError("Google Driveサービスにアクセスできません。")
            
            # 本番 parquet ファイル（_diff_ を含まないもの）を一覧取得
            query = f"'{tf_folder_id}' in parents and name contains 'price_jp_' and name contains '.parquet' and not name contains '_diff_' and trashed=false"
            drive_results = service.files().list(q=query, fields="files(id, name)").execute()
            base_files = drive_results.get('files', [])
            
            applied_files_count = 0
            
            for b_file in base_files:
                b_name = b_file['name']
                
                should_process = False
                all_records = False
                conditional_records = False
                
                # 時間足ごとの判定ルール
                if interval == "1d":
                    if b_name == "price_jp_1d.parquet":
                        should_process = True
                        conditional_records = True
                        
                elif interval == "60m":
                    m_y = re.search(r'price_jp_60m_(\d{4})\.parquet', b_name)
                    if m_y:
                        file_year = int(m_y.group(1))
                        if file_year < cliff_year:
                            should_process = True
                            all_records = True
                        elif file_year == cliff_year:
                            should_process = True
                            conditional_records = True
                            
                else: # 5m or 1m
                    m_ym = re.search(r'price_jp_(?:5m|1m)_(\d{4})_(\d{2})\.parquet', b_name)
                    if m_ym:
                        file_year = int(m_ym.group(1))
                        file_month = int(m_ym.group(2))
                        
                        if (file_year < cliff_year) or (file_year == cliff_year and file_month < cliff_month):
                            should_process = True
                            all_records = True
                        elif file_year == cliff_year and file_month == cliff_month:
                            should_process = True
                            conditional_records = True
                
                if not should_process:
                    continue
                    
                local_path = os.path.join(settings.WORK_DIR, b_name)
                dl_success = download_from_drive_api(b_name, local_path, parent_id=tf_folder_id)
                if not dl_success or not os.path.exists(local_path):
                    log(f"  ⚠️ ファイル {b_name} のダウンロードに失敗しました。")
                    continue
                    
                try:
                    import pyarrow as pa
                    import pyarrow.parquet as pq
                    df = pd.read_parquet(local_path)
                    if df.empty:
                        continue
                        
                    if "patched_multiplier" not in df.columns:
                        df["patched_multiplier"] = 1.0
                        
                    df["date_dt"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
                    
                    if all_records:
                        mask = (df["ticker"] == ticker)
                    elif conditional_records:
                        if interval in ["1d", "60m"]:
                            mask = (df["ticker"] == ticker) & (df["date_dt"] < cliff_dt)
                        else: # 5m, 1m (境界日時 09:00:00 より前を調整。不等号 '<' 適用)
                            market_start_dt = pd.to_datetime(f"{cliff_date} 09:00:00")
                            mask = (df["ticker"] == ticker) & (df["date_dt"] < market_start_dt)
                    else:
                        mask = pd.Series([False] * len(df))
                        
                    if mask.any():
                        if mode == "要パッチ":
                            price_cols = [c for c in ["open", "high", "low", "close"] if c in df.columns]
                            for col in price_cols:
                                df.loc[mask, col] = df.loc[mask, col] * multiplier
                            if "volume" in df.columns:
                                df.loc[mask, "volume"] = df.loc[mask, "volume"] / multiplier
                                
                        # multiplierの履歴を累積
                        df.loc[mask, "patched_multiplier"] = df.loc[mask, "patched_multiplier"] * multiplier
                        
                        df_cleaned = df.drop(columns=["date_dt"], errors="ignore")
                        table = pa.Table.from_pandas(df_cleaned, preserve_index=False)
                        pq.write_table(table, local_path, use_dictionary=False, compression="SNAPPY")
                        
                        up_success, up_msg = upload_to_drive_api(b_name, local_path, parent_id=tf_folder_id)
                        if up_success:
                            applied_files_count += 1
                            log(f"  ✅ パッチ適用完了 ({b_name} | 対象: {len(df[mask]):,}件)")
                        else:
                            log(f"  ❌ アップロード同期失敗 ({b_name}): {up_msg}")
                    
                    if os.path.exists(local_path):
                        os.remove(local_path)
                        
                except Exception as ex_file:
                    log(f"  ❌ ファイル {b_name} 処理中のエラー: {ex_file}")
                    if os.path.exists(local_path):
                        os.remove(local_path)
                    continue
                    
            results[interval] = f"正常に修復 ({applied_files_count}件)"
            
        except Exception as e:
            results[interval] = f"エラー: {str(e)}"
            log(f"❌ 【{interval}】 一括書き換え適用失敗: {e}")
            
    return results