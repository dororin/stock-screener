# data_access/local_db.py

import os
import shutil
import json
import glob
import re
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import streamlit as st
from config import settings
from data_access.drive_api import (
    download_from_drive_api, 
    upload_to_drive_api,
    get_or_create_drive_folder,
    list_drive_diff_files,
    delete_file_from_drive
)

def clear_local_parquet_cache(interval: str = None, is_jp: bool = True):
    """
    ローカル作業フォルダ（settings.WORK_DIR）内のParquetキャッシュファイルを削除し、
    次回ロード時にGoogle Driveから最新ファイルを強制再取得させます。
    """
    market = "jp" if is_jp else "us"
    if interval:
        pattern = f"price_{market}_{interval}*.parquet"
    else:
        pattern = f"price_{market}_*.parquet"
        
    search_path = os.path.join(settings.WORK_DIR, pattern)
    for f in glob.glob(search_path):
        if "_diff_" not in f and "_temp" not in f:
            try:
                os.remove(f)
            except Exception:
                pass

def get_ledger_filename(interval: str, is_jp: bool = True, is_raw: bool = False) -> str:
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    return f"ledger_{market}_{interval}{suffix}.json"

def load_price_db_ledger(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False) -> dict:
    if is_temp:
        return {"db_max_date": None, "last_updates_map": {}}
        
    filename = get_ledger_filename(interval, is_jp, is_raw)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)

    if not os.path.exists(work_file):
        api_success = download_from_drive_api(filename, work_file)
        if not api_success and os.path.exists(drive_file):
            shutil.copy2(drive_file, work_file)
                
    if os.path.exists(work_file):
        try:
            with open(work_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
            
    return {"db_max_date": None, "last_updates_map": {}}

def save_price_db_ledger(ledger_data: dict, interval: str, is_jp: bool = True, is_raw: bool = False) -> tuple[bool, str]:
    filename = get_ledger_filename(interval, is_jp, is_raw)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)
    
    try:
        with open(work_file, "w", encoding="utf-8") as f:
            json.dump(ledger_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return False, f"ローカルJSON台帳の書き込み失敗: {e}"
        
    try:
        api_success, msg = upload_to_drive_api(filename, work_file)
        if not api_success:
            try:
                shutil.copy2(work_file, drive_file)
            except Exception as e:
                return False, f"ローカル共有フォルダーへのJSON台帳保存失敗: {e}"
        return True, ""
    except Exception as e:
        return False, str(e)

def get_db_filename(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False, year_month: str = None, year: str = None) -> str:
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    temp_suffix = "_temp" if is_temp else ""
    
    ym_suffix = ""
    if interval in ["1m", "5m"] and year_month:
        ym_suffix = f"_{year_month}"
    elif interval == "60m" and year:
        ym_suffix = f"_{year}"
    elif interval in ["1m", "5m"] and year:
        ym_suffix = f"_{year}"
        
    return f"price_{market}_{interval}{ym_suffix}{suffix}{temp_suffix}.parquet"

def get_db_filename_pattern(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False) -> str:
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    temp_suffix = "_temp" if is_temp else ""
    if interval == "60m":
        return f"price_{market}_60m_[0-9][0-9][0-9][0-9]{suffix}{temp_suffix}*.parquet"
    elif interval in ["1m", "5m"]:
        return f"price_{market}_{interval}_[0-9][0-9][0-9][0-9]_[0-9][0-9]{suffix}{temp_suffix}*.parquet"
    return f"price_{market}_{interval}{suffix}{temp_suffix}.parquet"

def compute_ledger_from_df(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"db_max_date": None, "last_updates_map": {}}
    
    sub_df = df[["ticker", "date"]].copy()
    sub_df["date_dt"] = pd.to_datetime(sub_df["date"])
    
    db_max_date = sub_df["date_dt"].max()
    db_max_str = db_max_date.strftime("%Y-%m-%d %H:%M:%S") if pd.notna(db_max_date) else None
    
    grouped = sub_df.groupby("ticker")["date_dt"].max()
    last_updates_map = {
        ticker: dt.strftime("%Y-%m-%d %H:%M:%S") if pd.notna(dt) else None
        for ticker, dt in grouped.items()
    }
    
    return {
        "db_max_date": db_max_str,
        "last_updates_map": last_updates_map
    }

def save_price_db(df: pd.DataFrame, interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False, custom_ledger: dict = None) -> tuple[bool, str]:
    if df.empty:
        return False, "保存対象のデータが空（Empty）です。"
        
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    temp_suffix = "_temp" if is_temp else ""

    # 日本株の差分ファイル保存
    if is_jp and not is_temp and "date" in df.columns:
        timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        df_target = df.copy()
        df_target["date"] = pd.to_datetime(df_target["date"])
        
        if "patched_multiplier" not in df_target.columns:
            df_target["patched_multiplier"] = 1.0
        
        filename = f"price_jp_{interval}_diff_{timestamp}.parquet"
        local_work_path = os.path.join(settings.WORK_DIR, filename)
        try:
            table = pa.Table.from_pandas(df_target, preserve_index=False)
            pq.write_table(table, local_work_path, use_dictionary=False, compression="SNAPPY")
        except Exception as e:
            return False, f"Parquetのローカル差分書き出しに失敗: {e}"
            
        try:
            tf_folder_id = get_or_create_drive_folder(interval, settings.FOLDER_ID)
        except Exception as ex:
            return False, f"Google Drive上の時間足フォルダ自動作成に失敗: {ex}"

        success, msg = upload_to_drive_api(filename, local_work_path, parent_id=tf_folder_id)
        
        if success:
            try:
                ledger = load_price_db_ledger(interval, is_jp=True, is_raw=is_raw)
                new_ledger = compute_ledger_from_df(df_target)
                
                old_max = ledger.get("db_max_date")
                new_max = new_ledger.get("db_max_date")
                dates = [pd.to_datetime(d) for d in [old_max, new_max] if d]
                ledger["db_max_date"] = max(dates).strftime("%Y-%m-%d %H:%M:%S") if dates else None
                
                ledger.setdefault("last_updates_map", {}).update(new_ledger.get("last_updates_map", {}))
                save_price_db_ledger(ledger, interval, is_jp=True, is_raw=is_raw)
            except Exception as le:
                print(f"⚠️ 独立JSON台帳の更新に失敗しました: {le}")
                
        return success, msg

    # 米国株または一時データ（_temp）
    filename = get_db_filename(interval, is_jp, is_raw, is_temp)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)
    
    try:
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(table, work_file, use_dictionary=False, compression="SNAPPY")
    except Exception as e:
        return False, f"Parquet書き出しに失敗: {e}"
        
    if is_temp:
        return True, ""
        
    try:
        api_success, msg = upload_to_drive_api(filename, work_file)
        if not api_success:
            try:
                shutil.copy2(work_file, drive_file)
            except Exception as e:
                return False, f"ローカル共有フォルダへの保存失敗: {e}"
        return True, ""
    except Exception as e:
        return False, str(e)

def execute_jp_merge(interval: str, status_callback=None) -> dict:
    """
    時間足フォルダの直下からすべての未マージ差分（_diff_）ファイルを古い順にロードし、
    本番結合ファイルへ重複排除して安全マージ。
    Driveへのアップロード成功後、ローカル（WORK_DIR）の正規Parquetファイルも最新データで上書き同期します。
    """
    def log(msg):
        if status_callback:
            try: status_callback(msg)
            except Exception: pass
        print(f"[CONSOLE_DEBUG] [JP_MERGE_FLAT] {msg}")

    log(f"⚙️ 【日本株 {interval}】の最適化手動マージ（コンパクション）を開始します...")

    try:
        tf_folder_id = get_or_create_drive_folder(interval, settings.FOLDER_ID)
    except Exception as e:
        log(f"❌ 親フォルダの検出に失敗しました: {e}")
        return {"success": False, "message": str(e)}

    all_diff_files = list_drive_diff_files(tf_folder_id)

    if not all_diff_files:
        log("✅ 未処理の差分ファイルは検出されませんでした（最新状態です）。")
        return {"success": True, "message": "マージ対象 of 差分ファイルはありません。"}

    def extract_timestamp(f_meta):
        name = f_meta['name']
        m = re.search(r'_diff_(\d{8}_\d{6})', name)
        if m:
            return m.group(1)
        return f_meta.get('createdTime', '')

    all_diff_files = sorted(all_diff_files, key=extract_timestamp)
    log(f"📂 未処理の差分ファイルを検出しました。総計: {len(all_diff_files)} 件。古い順にマージを開始します...")

    loaded_bases = {}
    processed_file_ids = []
    error_occurred = False
    err_msg = ""

    for diff_meta in all_diff_files:
        filename = diff_meta['name']
        file_id = diff_meta['id']
        
        log(f"  📥 差分ファイルをダウンロード中: {filename} ...")
        local_temp_path = os.path.join(settings.WORK_DIR, f"temp_merge_{filename}")
        
        dl_success = download_from_drive_api(filename, local_temp_path, parent_id=tf_folder_id)
        if not dl_success:
            log(f"  ❌ [{filename}] のダウンロードに失敗しました。マージを安全に中断します。")
            error_occurred = True
            err_msg = f"ファイルのダウンロード失敗: {filename}"
            break
            
        try:
            diff_df = pd.read_parquet(local_temp_path)
            if diff_df.empty:
                os.remove(local_temp_path)
                processed_file_ids.append(file_id)
                continue
                
            diff_df["date"] = pd.to_datetime(diff_df["date"]).dt.tz_localize(None)
            
            if "patched_multiplier" not in diff_df.columns:
                diff_df["patched_multiplier"] = 1.0
            
            if interval == "1d":
                diff_df["_group_key"] = "all"
            elif interval == "60m":
                diff_df["_group_key"] = diff_df["date"].dt.strftime("%Y")
            else:
                diff_df["_group_key"] = diff_df["date"].dt.strftime("%Y_%m")
            
            for group_key, group_df in diff_df.groupby("_group_key"):
                group_clean = group_df.drop(columns=["_group_key"])
                
                if group_key not in loaded_bases:
                    if interval == "1d":
                        base_filename = "price_jp_1d.parquet"
                    elif interval == "60m":
                        base_filename = f"price_jp_60m_{group_key}.parquet"
                    else:
                        base_filename = f"price_jp_{interval}_{group_key}.parquet"
                        
                    local_base_path = os.path.join(settings.WORK_DIR, f"base_merge_{base_filename}")
                    
                    exists = download_from_drive_api(base_filename, local_base_path, parent_id=tf_folder_id)
                    if exists and os.path.exists(local_base_path):
                        try:
                            base_df = pd.read_parquet(local_base_path)
                            base_df["date"] = pd.to_datetime(base_df["date"]).dt.tz_localize(None)
                            
                            if "patched_multiplier" not in base_df.columns:
                                base_df["patched_multiplier"] = 1.0
                            
                            orig_tickers = set(base_df["ticker"].unique()) if not base_df.empty else set()
                            orig_count = len(base_df)
                            
                            loaded_bases[group_key] = {
                                "df": base_df,
                                "original_tickers": orig_tickers,
                                "original_count": orig_count,
                                "filename": base_filename,
                                "local_path": local_base_path
                            }
                        except Exception as e_read:
                            log(f"  ❌ [{base_filename}] のロードに失敗しました: {e_read}")
                            error_occurred = True
                            err_msg = f"既存本番データの読み込み失敗: {base_filename}"
                            break
                    else:
                        loaded_bases[group_key] = {
                            "df": pd.DataFrame(),
                            "original_tickers": set(),
                            "original_count": 0,
                            "filename": base_filename,
                            "local_path": local_base_path
                        }
                
                if error_occurred:
                    break
                        
                base_info = loaded_bases[group_key]
                old_base_df = base_info["df"]
                
                if not old_base_df.empty:
                    merged_df = pd.concat([old_base_df, group_clean], ignore_index=True)
                else:
                    merged_df = group_clean
                    
                merged_clean = merged_df.drop_duplicates(subset=["date", "ticker"], keep="last")
                base_info["df"] = merged_clean
                
            if os.path.exists(local_temp_path):
                os.remove(local_temp_path)
                
            if error_occurred:
                break
                
            processed_file_ids.append(file_id)
            
        except Exception as e:
            log(f"  ❌ [{filename}] のマージ演算中にエラー: {e}")
            error_occurred = True
            err_msg = str(e)
            if os.path.exists(local_temp_path):
                os.remove(local_temp_path)
            break

    if not error_occurred and loaded_bases:
        log("🔍 クラウド保存前のインメモリデータ健全性スキャンを実行します...")
        for g_key, b_info in loaded_bases.items():
            final_df = b_info["df"]
            f_name = b_info["filename"]
            orig_tickers = b_info["original_tickers"]
            orig_count = b_info["original_count"]
            new_count = len(final_df)

            if final_df.empty:
                error_occurred = True
                err_msg = f"{f_name} のマージ後データが空になりました。"
                break
            new_tickers = set(final_df["ticker"].unique())
            missing_tickers = orig_tickers - new_tickers
            if missing_tickers:
                error_occurred = True
                err_msg = f"{f_name} から一部の銘柄データが消失しました。"
                break
            if orig_count > 0 and new_count < orig_count * 0.99:
                error_occurred = True
                err_msg = f"{f_name} の行数が異常に減少しました。"
                break
            if final_df["close"].isna().any() or final_df["ticker"].isna().any() or final_df["date"].isna().any():
                error_occurred = True
                err_msg = f"{f_name} に NULL値が混入しました。"
                break
            if (final_df["close"] <= 0).any():
                error_occurred = True
                err_msg = f"{f_name} に 0 以下の異常価格が含まれています。"
                break

    if not error_occurred and loaded_bases:
        log("💾 すべての安全アサーション検証をクリアしました。統合ファイルを上書き保存中...")
        for g_key, b_info in loaded_bases.items():
            final_df = b_info["df"].sort_values(["ticker", "date"]).reset_index(drop=True)
            local_save_path = b_info["local_path"]
            f_name = b_info["filename"]
            official_local_path = os.path.join(settings.WORK_DIR, f_name)
            
            try:
                table = pa.Table.from_pandas(final_df, preserve_index=False)
                pq.write_table(table, local_save_path, use_dictionary=False, compression="SNAPPY")
                
                up_success, up_msg = upload_to_drive_api(f_name, local_save_path, parent_id=tf_folder_id)
                if up_success:
                    # ローカル正規キャッシュファイルも最新版で上書き同期
                    pq.write_table(table, official_local_path, use_dictionary=False, compression="SNAPPY")
                    log(f"   ✅ [{f_name}] Googleドライブおよびローカル正規キャッシュを最新データに同期しました。({len(final_df):,}件)")
                    
                    if os.path.exists(local_save_path):
                        os.remove(local_save_path)
                else:
                    log(f"   ❌ [{f_name}] 本番同期に失敗: {up_msg}")
                    error_occurred = True
                    err_msg = f"本番同期エラー: {up_msg}"
                    break
            except Exception as e:
                log(f"   ❌ [{f_name}] 保存書き出し中に例外発生: {e}")
                error_occurred = True
                err_msg = str(e)
                break

    if not error_occurred and processed_file_ids:
        log("🧹 データベースの確定保存を確認しました。Googleドライブ上の元差分ファイルを自動消去中...")
        del_count = 0
        for f_id in processed_file_ids:
            if delete_file_from_drive(f_id):
                del_count += 1
        log(f"   👉 使用済みの差分ファイル {del_count} 件をGoogleドライブから安全消去しました。")
        
        try:
            st.cache_data.clear()
        except Exception:
            pass
            
        return {"success": True, "message": f"計 {len(processed_file_ids)} 件の差分健全マージとキャッシュ同期が正常に完了しました。"}

    return {"success": False, "message": err_msg if err_msg else "マージ処理を安全に中断・ロールバックしました。"}

def load_price_db(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False, columns: list = None, filters: list = None) -> pd.DataFrame:
    """
    1m, 5m, 60m, 1d 等の本番Parquetファイルを投影ロード・フィルタリングロードします。
    ローカルにない場合はGoogle Driveから自動取得します。
    """
    def _apply_pandas_filters_safe(df_target: pd.DataFrame) -> pd.DataFrame:
        if df_target.empty or not filters:
            return df_target
        for f in filters:
            if isinstance(f, (tuple, list)) and len(f) == 3:
                col, op, val = f
                if col in df_target.columns:
                    if op == "==":
                        df_target = df_target[df_target[col] == val]
                    elif op == ">=":
                        df_target = df_target[df_target[col] >= val]
                    elif op == "<=":
                        df_target = df_target[df_target[col] <= val]
        return df_target

    min_date_limit = None
    if filters:
        for f in filters:
            if isinstance(f, (tuple, list)) and len(f) == 3:
                col, op, val = f
                if col == "date" and op in [">=", ">"]:
                    min_date_limit = pd.to_datetime(val)
                    break

    # 日本株（JP）のロード処理
    if is_jp:
        tf_folder_id = get_or_create_drive_folder(interval, settings.FOLDER_ID)
        from data_access.drive_api import get_drive_service
        service = get_drive_service()
        if not service:
            return pd.DataFrame()
            
        try:
            query = f"'{tf_folder_id}' in parents and name contains 'price_jp_' and not name contains '_diff_' and name contains '.parquet' and trashed=false"
            results = service.files().list(q=query, fields="files(id, name)").execute()
            base_files = results.get('files', [])
        except Exception:
            base_files = []

        dfs = []
        for b_file in base_files:
            b_name = b_file['name']
            
            if min_date_limit is not None:
                m_ym = re.search(r'price_jp_\w+_(\d{4})_(\d{2})\.parquet', b_name)
                if m_ym:
                    file_year = int(m_ym.group(1))
                    file_month = int(m_ym.group(2))
                    if (file_year < min_date_limit.year) or (file_year == min_date_limit.year and file_month < min_date_limit.month):
                        continue
                else:
                    m_y = re.search(r'price_jp_\w+_(\d{4})\.parquet', b_name)
                    if m_y:
                        file_year = int(m_y.group(1))
                        if file_year < min_date_limit.year:
                            continue
            
            local_path = os.path.join(settings.WORK_DIR, b_name)
            if not os.path.exists(local_path):
                download_from_drive_api(b_name, local_path, parent_id=tf_folder_id)
                
            if os.path.exists(local_path):
                try:
                    df = pd.read_parquet(local_path, columns=columns, filters=filters, engine='pyarrow')
                except Exception:
                    try:
                        df = pd.read_parquet(local_path, columns=columns, engine='pyarrow')
                        df = _apply_pandas_filters_safe(df)
                    except Exception:
                        df = pd.DataFrame()
                if not df.empty:
                    dfs.append(df)
                    
        if not dfs:
            return pd.DataFrame()
        combined = pd.concat(dfs, ignore_index=True)
        if "date" in combined.columns:
            combined["date"] = pd.to_datetime(combined["date"]).dt.tz_localize(None)
            if min_date_limit is not None:
                combined = combined[combined["date"] >= min_date_limit]
        return combined.drop_duplicates(subset=["date", "ticker"], keep="last")

    # 米国株（US）のロード処理
    if interval in ["1m", "5m", "60m"]:
        pattern = get_db_filename_pattern(interval, is_jp, is_raw, is_temp)
        search_path = os.path.join(settings.WORK_DIR, pattern)
        files = glob.glob(search_path)
        
        if not files and not is_temp:
            if interval == "60m":
                now_y = pd.Timestamp.now().strftime("%Y")
                temp_filename = get_db_filename(interval, is_jp, is_raw, is_temp, year=now_y)
            else:
                now_ym = pd.Timestamp.now().strftime("%Y_%m")
                temp_filename = get_db_filename(interval, is_jp, is_raw, is_temp, year_month=now_ym)
            temp_work_file = os.path.join(settings.WORK_DIR, temp_filename)
            download_from_drive_api(temp_filename, temp_work_file)
            files = glob.glob(search_path)
            
        if not files:
            mono_filename = get_db_filename(interval, is_jp, is_raw, is_temp)
            mono_work_file = os.path.join(settings.WORK_DIR, mono_filename)
            if not os.path.exists(mono_work_file) and not is_temp:
                download_from_drive_api(mono_filename, mono_work_file)
            if os.path.exists(mono_work_file):
                files = [mono_work_file]
            else:
                return pd.DataFrame()
            
        dfs = []
        for filepath in files:
            fname = os.path.basename(filepath)
            if min_date_limit is not None:
                m_ym = re.search(r'price_us_\w+_(\d{4})_(\d{2})', fname)
                if m_ym:
                    file_year = int(m_ym.group(1))
                    file_month = int(m_ym.group(2))
                    if (file_year < min_date_limit.year) or (file_year == min_date_limit.year and file_month < min_date_limit.month):
                        continue
                else:
                    m_y = re.search(r'price_us_\w+_(\d{4})', fname)
                    if m_y:
                        file_year = int(m_y.group(1))
                        if file_year < min_date_limit.year:
                            continue
            try:
                df = pd.read_parquet(filepath, columns=columns, filters=filters, engine='pyarrow')
            except Exception:
                try:
                    df = pd.read_parquet(filepath, columns=columns, engine='pyarrow')
                    df = _apply_pandas_filters_safe(df)
                except Exception:
                    df = pd.DataFrame()
            if not df.empty:
                dfs.append(df)
                
        if not dfs:
            return pd.DataFrame()
        combined_df = pd.concat(dfs, ignore_index=True)
        if "date" in combined_df.columns:
            combined_df["date"] = pd.to_datetime(combined_df["date"]).dt.tz_localize(None)
            if min_date_limit is not None:
                combined_df = combined_df[combined_df["date"] >= min_date_limit]
        return combined_df.drop_duplicates(subset=["date", "ticker"], keep="last")

    # 米国株 日足（1d）
    filename = get_db_filename(interval, is_jp, is_raw, is_temp)
    work_file = os.path.join(settings.WORK_DIR, filename)
    if not os.path.exists(work_file) and not is_temp:
        download_from_drive_api(filename, work_file)

    if os.path.exists(work_file):
        try:
            df = pd.read_parquet(work_file, columns=columns, filters=filters, engine='pyarrow')
        except Exception:
            try:
                df = pd.read_parquet(work_file, columns=columns, engine='pyarrow')
                df = _apply_pandas_filters_safe(df)
            except Exception:
                df = pd.DataFrame()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            if min_date_limit is not None:
                df = df[df["date"] >= min_date_limit]
        return df
    return pd.DataFrame()

# =====================================================================
# ⚡ レイヤー1：材料（ロード）キャッシュ設計
# =====================================================================

def _fetch_price_data_internal(interval: str, limit_days: int, is_jp: bool) -> pd.DataFrame:
    """Parquetから必要なカラムと足切り期間だけを投影・フィルタリングロードします。"""
    limit_dt = pd.Timestamp.now() - pd.Timedelta(days=limit_days)
    limit_start_date = limit_dt.strftime("%Y-%m-%d %H:%M:%S")
    
    target_columns = ["date", "ticker", "open", "high", "low", "close", "volume"]
    if not is_jp:
        target_columns.extend(["adj close", "stock splits"])
        
    filters = [("date", ">=", limit_start_date)]
    
    df = load_price_db(
        interval=interval,
        is_jp=is_jp,
        columns=target_columns,
        filters=filters
    )
    if df.empty:
        return pd.DataFrame()
        
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df[df["date"] >= limit_dt]
        
    return df.sort_values(["ticker", "date"]).reset_index(drop=True)

@st.cache_data(ttl=3600)
def _get_price_data_1d_cached(limit_days: int, is_jp: bool) -> pd.DataFrame:
    return _fetch_price_data_internal("1d", limit_days, is_jp)

@st.cache_data(ttl=300)
def _get_price_data_intraday_cached(interval: str, limit_days: int, is_jp: bool) -> pd.DataFrame:
    return _fetch_price_data_internal(interval, limit_days, is_jp)

def get_price_data_cached(interval: str, limit_days: int = None, is_jp: bool = True) -> pd.DataFrame:
    """
    レイヤー1：材料ロードキャッシュ
    時間足に応じた最大ロード期間（足切りバッファ）と必要最小限のカラム指定でParquetを部分ロードし、
    DataFrameをメモリ上に一時保存・返却します。
    """
    if limit_days is None:
        if interval == "1d":
            limit_days = 3650  # 直近10年
        elif interval == "60m":
            limit_days = 180   # 直近6ヶ月
        elif interval == "5m":
            limit_days = 14    # 直近2週間
        elif interval == "1m":
            limit_days = 3     # 直近3日
            
    if interval == "1d":
        return _get_price_data_1d_cached(limit_days, is_jp)
    else:
        return _get_price_data_intraday_cached(interval, limit_days, is_jp)