# core/jp_price_corrector.py

import os
import re
import time
from datetime import datetime, timedelta
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

def get_dynamic_threshold(s: float) -> tuple:
    """
    公式の分割比率Sに応じて、許容する段差判定価格比R (前日Close / 当日Close) の範囲を返します。
    """
    if s >= 2.0:
        # 大規模分割: バッファ ±15% でノイズを徹底除外
        return s * 0.85, s * 1.15
    elif s >= 1.30:
        # 中規模分割: バッファ ±15%
        return s * 0.85, s * 1.15
    else:
        # 微小分割（1.01 〜 1.30未満）: 通常の乱高下に埋もれるため、
        # 下限を一律1.03 (下落率約3%) まで拡張し「見逃しゼロ」を最優先にします。
        return 1.03, s * 1.15

def scan_jp_anomalies_with_yfinance(status_callback=None) -> pd.DataFrame:
    """
    本番日足データ(price_jp_1d.parquet)から監視銘柄リストのデータをロードし、
    yfinanceから公式の株式分割履歴を一時取得して、過去に遡ったルックバック走査診断を行います。
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
    
    # ── 仕様書4.1：イベントターゲット絞り込み（直近3ヶ月間：90日に限定） ──
    # スキャンの処理負荷を抑えるため、そもそも直近に公式分割イベントがある銘柄のみにフィルタリングします。
    target_start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    log(f"🔎 データベース内から {len(tickers)} 銘柄を検出し、直近3ヶ月間（{target_start_date}以降）の公式分割イベントを高速フィルタ中...")

    batch_size = 100
    ticker_batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]
    
    event_tickers_with_splits = {}  # {ticker: {ex_date_timestamp: split_ratio}}

    for b_idx, batch in enumerate(ticker_batches):
        batch_tickers_T = [f"{t}.T" for t in batch]
        try:
            # yf.downloadを用いて一括アクション情報の取得（auto_adjust=FalseでCloseとAdj Closeの双方を確保）
            df_download = yf.download(
                batch_tickers_T,
                start=target_start_date,
                interval="1d",
                auto_adjust=False,
                actions=True,
                progress=False,
                threads=True,
                timeout=30
            )
            
            if df_download.empty:
                continue
            
            # Stock Splitsの抽出
            df_splits = pd.DataFrame()
            if isinstance(df_download.columns, pd.MultiIndex):
                if "Stock Splits" in df_download.columns.get_level_values(0):
                    df_splits = df_download["Stock Splits"]
            else:
                if "Stock Splits" in df_download.columns:
                    df_splits = pd.DataFrame(df_download["Stock Splits"])
                    df_splits.columns = batch_tickers_T[:1]
            
            if df_splits.empty:
                continue

            # 各銘柄に対して分割イベントがあるか走査
            for ticker_with_T in df_splits.columns:
                splits_series_raw = df_splits[ticker_with_T]
                splits_series = splits_series_raw[(splits_series_raw > 0) & (splits_series_raw != 1.0)].dropna()
                
                if not splits_series.empty:
                    ticker = ticker_with_T.split(".")[0]
                    event_tickers_with_splits[ticker] = splits_series.to_dict()

        except Exception as ex:
            log(f"  ⚠️ バッチ [{b_idx + 1}] 処理中に例外検出 (スキップ): {ex}")
            continue

    if not event_tickers_with_splits:
        log("✅ 直近3ヶ月間に株式分割イベントが検知された銘柄はありません。スキャンを正常終了します。")
        return pd.DataFrame()

    log(f"🎯 公式分割イベントを検出。対象 {len(event_tickers_with_splits)} 銘柄に対して、詳細なインメモリ遡及走査を開始します。")
    results = []

    # ── 仕様書4.2：日足のインメモリ投影走査（イベント適合銘柄のみに限定ループ） ──
    for ticker, splits_dict in event_tickers_with_splits.items():
        df_ticker = df_1d[df_1d["ticker"] == ticker].sort_values("date_dt").reset_index(drop=True)
        if df_ticker.empty or len(df_ticker) < 2:
            continue

        for ex_date, s_val in splits_dict.items():
            if pd.isna(s_val) or s_val <= 0.0 or s_val == 1.0:
                continue
            
            planned_dt = pd.to_datetime(ex_date).tz_localize(None)
            planned_date_str = planned_dt.strftime("%Y-%m-%d")
            
            # ── 仕様書2.1：走査ロジックの改修（「点」から「面」への移行：過去30営業日） ──
            ex_date_idx_list = df_ticker[df_ticker["date_dt"] <= planned_dt].index.tolist()
            if not ex_date_idx_list:
                continue
                
            ex_date_idx = ex_date_idx_list[-1]
            # Ex-Dateを含めた、過去最大30営業日分をルックバック期間とする
            lookback_start_idx = max(0, ex_date_idx - 30)
            df_window = df_ticker.iloc[lookback_start_idx : ex_date_idx + 1].copy()
            
            if len(df_window) < 2:
                continue
                
            detected_idx = -1
            detected_R = 1.0
            
            # 分割倍率に応じた動的バッファ閾値の取得 [仕様書2.2]
            min_R, max_R = get_dynamic_threshold(s_val)
            window_indices = df_window.index.tolist()
            
            # ── 仕様書2.3：複数検出時のセーフガード（逆向き走査により最もEx-Dateに近い新しい日付を検出） ──
            for i in reversed(range(1, len(window_indices))):
                idx_T = window_indices[i]
                idx_prev = window_indices[i-1]
                
                row_T = df_ticker.loc[idx_T]
                row_prev = df_ticker.loc[idx_prev]
                
                # 休場・取引停止が長すぎる場合は境界不整合と判定せず除外
                gap_days = (row_T["date_dt"] - row_prev["date_dt"]).days
                if gap_days > 15:
                    continue
                    
                close_T = row_T["close"]
                close_prev = row_prev["close"]
                
                if pd.isna(close_T) or pd.isna(close_prev) or close_T <= 0 or close_prev <= 0:
                    continue
                    
                R_price = close_prev / close_T
                
                # 動的バッファ閾値内の不連続性を検出
                if min_R <= R_price <= max_R:
                    detected_idx = idx_T
                    detected_R = R_price
                    break
            
            mode = None
            actual_dt = planned_dt
            cliff_dt = planned_dt
            close_T_val = np.nan
            close_prev_val = np.nan
            status_label = "[正常分割]"
            
            M = 1.0 / s_val
            
            # ── [安全・重複検知防止チェック] 既に累積乗数（マーク）が書き換わっているか判定 ──
            is_already_patched = False
            if detected_idx != -1:
                row_T = df_ticker.loc[detected_idx]
                row_prev = df_ticker.loc[detected_idx - 1]
                mult_T = row_T["patched_multiplier"]
                mult_prev = row_prev["patched_multiplier"]
                R_multiplier = mult_prev / mult_T
                
                # すでに等倍（1.0）付近を離れて累積補正乗数が適用済みである場合
                if abs(R_multiplier - 1.0) > 0.15:
                    is_already_patched = True
            else:
                row_T = df_ticker.iloc[ex_date_idx]
                row_prev = df_ticker.iloc[max(0, ex_date_idx - 1)]
                mult_T = row_T["patched_multiplier"]
                mult_prev = row_prev["patched_multiplier"]
                R_multiplier = mult_prev / mult_T
                if abs(R_multiplier - 1.0) > 0.15:
                    is_already_patched = True

            if is_already_patched:
                # すでに処理済みのマーク（patched_multiplier）があれば、スキャン重複防止のために結果から自動除外
                continue
            
            # ── 仕様書2.4：yfinance調整後終値との突合・整合性チェック仕様（アプローチ③） ──
            if detected_idx != -1:
                row_T = df_ticker.loc[detected_idx]
                row_prev = df_ticker.loc[detected_idx - 1]
                
                actual_dt = row_T["date_dt"]  # 実質的な段差発生日（当日）
                cliff_dt = row_prev["date_dt"]  # 真の境界日（段差前日）
                
                close_T_val = row_T["close"]
                close_prev_val = row_prev["close"]
                
                # 突合用 yfinance Adj Close（公式調整値）をピンポイント取得
                try:
                    df_verify = yf.download(
                        f"{ticker}.T",
                        start=(actual_dt - timedelta(days=2)).strftime("%Y-%m-%d"),
                        end=(actual_dt + timedelta(days=3)).strftime("%Y-%m-%d"),
                        auto_adjust=False,
                        actions=False,
                        progress=False,
                        timeout=15
                    )
                    if not df_verify.empty and "Adj Close" in df_verify.columns:
                        actual_date_str = actual_dt.strftime("%Y-%m-%d")
                        matching_rows = df_verify[df_verify.index.strftime("%Y-%m-%d") == actual_date_str]
                        if not matching_rows.empty:
                            C_tv = matching_rows["Adj Close"].iloc[0]
                            C_db = close_T_val
                            
                            # 突合条件判定（すでに調整価格で書き換わっているか判定）
                            if abs(C_db - C_tv) <= C_tv * 0.15:
                                # ① 誤差15%以内：先回り調整状態
                                mode = "要パッチ"
                            else:
                                # ② 未調整状態
                                mode = "要パッチ"
                        else:
                            mode = "要パッチ"
                    else:
                        mode = "要パッチ"
                except Exception:
                    mode = "要パッチ"

                # 警告表示のステータス判定 [仕様書3.1]
                if actual_dt < planned_dt:
                    status_label = "[⚠️先回り調整混入（警告）]"
                else:
                    status_label = "[正常分割]"
                    
            else:
                # 崖がルックバック期間内に検出されなかった場合（すでに全データが完璧に先回り調整されているなど） [質問2への対応]
                mode = "メタデータのみ更新"
                actual_dt = planned_dt
                cliff_dt = planned_dt
                
                row_T = df_ticker.iloc[ex_date_idx]
                close_T_val = row_T["close"]
                if ex_date_idx > 0:
                    close_prev_val = df_ticker.iloc[ex_date_idx - 1]["close"]
                
                status_label = "[正常分割]"

            if mode:
                results.append({
                    "ticker": ticker,
                    "interval": "1d",
                    "ex_date": planned_date_str,                  # 公式予定日
                    "actual_date": actual_dt.strftime("%Y-%m-%d"), # 実質段差日
                    "cliff_date": cliff_dt.strftime("%Y-%m-%d"),   # 真の境界日（段差前日）
                    "splits": s_val,
                    "mode": mode,
                    "multiplier": M,
                    "before_close": round(close_prev_val, 2) if not pd.isna(close_prev_val) else 0.0,
                    "after_close": round(close_T_val, 2) if not pd.isna(close_T_val) else 0.0,
                    "status": status_label
                })
            
            time.sleep(1.0)

    log(f"🎉 日本株段差スキャン完了。自動検出された修正候補: {len(results)} 件")
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
                                
                        # multiplierの履歴を過去レコード1つ1つに累積（実施済み刻印マーク）
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