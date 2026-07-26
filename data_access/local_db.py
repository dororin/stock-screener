# data_access/local_db.py

import os
import shutil
import json
import glob
import re
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from config import settings
from data_access.drive_api import (
    download_from_drive_api, 
    upload_to_drive_api,
    get_or_create_drive_folder,
    list_drive_diff_files,
    delete_file_from_drive
)

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

def get_db_filename(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False, year_month: str = None) -> str:
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    temp_suffix = "_temp" if is_temp else ""
    ym_suffix = f"_{year_month}" if (interval in ["1m", "5m"] and year_month) else ""
    return f"price_{market}_{interval}{ym_suffix}{suffix}{temp_suffix}.parquet"

def get_db_filename_pattern(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False) -> str:
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    temp_suffix = "_temp" if is_temp else ""
    return f"price_{market}_{interval}_[0-9][0-9][0-9][0-9]_[0-9][0-9]{suffix}{temp_suffix}*.parquet"

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

# --- 🚀 完全フラット設計仕様：日本株は時間足フォルダ（例：1m）直下に「差分のみ」を即時保存 ---
def save_price_db(df: pd.DataFrame, interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False, custom_ledger: dict = None) -> tuple[bool, str]:
    if df.empty:
        return False, "保存対象のデータが空（Empty）です。"
        
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    temp_suffix = "_temp" if is_temp else ""

    # ───── 日本株（JP）の差分ファイル・時間足直下フラット保存 ─────
    if is_jp and not is_temp and "date" in df.columns:
        timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        df_target = df.copy()
        df_target["date"] = pd.to_datetime(df_target["date"])
        
        # 1. ローカル作業ディレクトリに一時Parquet出力
        filename = f"price_jp_{interval}_diff_{timestamp}.parquet"
        local_work_path = os.path.join(settings.WORK_DIR, filename)
        try:
            table = pa.Table.from_pandas(df_target, preserve_index=False)
            pq.write_table(table, local_work_path, use_dictionary=False, compression="SNAPPY")
        except Exception as e:
            return False, f"Parquetのローカル差分書き出しに失敗: {e}"
            
        # 2. Google Drive上に時間足フォルダ（例: settings.FOLDER_ID / 1m）のみを検出・自動作成
        try:
            tf_folder_id = get_or_create_drive_folder(interval, settings.FOLDER_ID)
        except Exception as ex:
            return False, f"Google Drive上の時間足フォルダ自動作成に失敗: {ex}"

        # 3. 月別フォルダは「作らず」、時間足フォルダの直下へ差分ファイルを直接アップロード
        success, msg = upload_to_drive_api(filename, local_work_path, parent_id=tf_folder_id)
        
        # 4. アップロード完了後、独立JSON台帳の最終更新日時を同期
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
                
        # 正常に終了
        return success, msg

    # 米国株（US）または一時検証データ（_temp）の場合は、従来通りのモノリス保存（従来互換）
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


# ─── 🚀 刷新：日本株専用 手動「上書きマージ（後勝ち・自動消去）」完全フラット型エンジン ───

def execute_jp_merge(interval: str, status_callback=None) -> dict:
    """
    時間足フォルダ（例: 1m）の直下から、すべての未マージ差分（_diff_）ファイルを古い順にロードし、
    中身をレコード単位で自動で年月（YYYY_MM）に完全仕分けした上で、対応する本番ファイルへ
    重複を排除（keep='last'：新優先）して安全マージ。
    処理が完了した差分ファイルは Google Drive から自動で物理消去します。
    """
    def log(msg):
        if status_callback:
            try: status_callback(msg)
            except Exception: pass
        print(f"[CONSOLE_DEBUG] [JP_MERGE_FLAT] {msg}")

    log(f"⚙️ 【日本株 {interval}】のフラット型手動マージ（コンパクション）を開始します...")

    # 1. 時間足フォルダ（例: settings.FOLDER_ID / 1m）のフォルダIDを取得
    try:
        tf_folder_id = get_or_create_drive_folder(interval, settings.FOLDER_ID)
    except Exception as e:
        log(f"❌ 親フォルダの検出に失敗しました: {e}")
        return {"success": False, "message": str(e)}

    # 2. 時間足フォルダの直下にある未処理の差分ファイル（_diff_を含むもの）を検索取得
    all_diff_files = list_drive_diff_files(tf_folder_id)

    if not all_diff_files:
        log("✅ 未処理の差分ファイルは検出されませんでした（最新状態です）。")
        return {"success": True, "message": "マージ対象の差分ファイルはありません。"}

    # 3. 差分ファイルを「ダウンロード日時（古い順）」にソート
    def extract_timestamp(f_meta):
        name = f_meta['name']
        m = re.search(r'_diff_(\d{8}_\d{6})', name)
        if m:
            return m.group(1)
        return f_meta.get('createdTime', '')

    all_diff_files = sorted(all_diff_files, key=extract_timestamp)
    log(f"📂 未処理の差分ファイルを検出しました。総計: {len(all_diff_files)} 件。古い順にマージを開始します...")

    # メモリ上に統合用の本番ベースデータをキャッシュする辞書
    # 構造: { "2026_07": pd.DataFrame, "2026_06": pd.DataFrame }
    loaded_bases = {}

    processed_file_ids = []
    error_occurred = False
    err_msg = ""

    # 4. 差分ファイルを古い順に累積ループマージ（ケースA・B・Dを解決）
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
            # 差分データを読み込み
            diff_df = pd.read_parquet(local_temp_path)
            if diff_df.empty:
                os.remove(local_temp_path)
                processed_file_ids.append(file_id)
                continue
                
            diff_df["date"] = pd.to_datetime(diff_df["date"]).dt.tz_localize(None)
            diff_df["_ym_group"] = diff_df["date"].dt.strftime("%Y_%m")
            
            # 差分データに含まれる年月グループごとに処理（月またぎ差分の自動仕分け・ケースDの解決）
            for ym_group, group_df in diff_df.groupby("_ym_group"):
                # 他の月のデータとの混合を絶対に防ぐ（ケースCの解決）
                group_clean = group_df.drop(columns=["_ym_group"])
                
                # キャッシュされているか確認、無ければ統合ファイルをロード（ケースBの解決）
                if ym_group not in loaded_bases:
                    base_filename = f"price_jp_{interval}_{ym_group}.parquet"
                    local_base_path = os.path.join(settings.WORK_DIR, f"base_merge_{base_filename}")
                    
                    # 時間足フォルダ直下から、本番月別結合ファイルのダウンロードを試行
                    exists = download_from_drive_api(base_filename, local_base_path, parent_id=tf_folder_id)
                    if exists and os.path.exists(local_base_path):
                        try:
                            base_df = pd.read_parquet(local_base_path)
                            base_df["date"] = pd.to_datetime(base_df["date"]).dt.tz_localize(None)
                            loaded_bases[ym_group] = {
                                "df": base_df,
                                "filename": base_filename,
                                "local_path": local_base_path
                            }
                        except Exception:
                            # ファイル破損などの場合
                            loaded_bases[ym_group] = {
                                "df": pd.DataFrame(),
                                "filename": base_filename,
                                "local_path": local_base_path
                            }
                    else:
                        # 本番ファイルがまだ存在しない場合（新規作成・ケースAの解決）
                        loaded_bases[ym_group] = {
                            "df": pd.DataFrame(),
                            "filename": base_filename,
                            "local_path": local_base_path
                        }
                        
                # メモリ上での結合・重複排除（古いデータを先、新しいグループを後に結合してkeep="last"：新優先上書き）
                base_info = loaded_bases[ym_group]
                old_base_df = base_info["df"]
                
                if not old_base_df.empty:
                    merged_df = pd.concat([old_base_df, group_clean], ignore_index=True)
                else:
                    merged_df = group_clean
                    
                # 日時とティッカーによる後勝ち重複排除（最新日の重複ダウンロードもこれで最新確定値へ上書きされます）
                merged_clean = merged_df.drop_duplicates(subset=["date", "ticker"], keep="last")
                base_info["df"] = merged_clean
                
            # メモリ上への同期完了、ローカル一時ファイルをクリーンアップ
            if os.path.exists(local_temp_path):
                os.remove(local_temp_path)
            processed_file_ids.append(file_id)
            
        except Exception as e:
            log(f"  ❌ [{filename}] のマージ演算中にエラー: {e}")
            error_occurred = True
            err_msg = str(e)
            if os.path.exists(local_temp_path):
                os.remove(local_temp_path)
            break

    # 5. エラー無く全マージ処理が累積完了した場合のみ、本番結合ファイルの上書き確定保存を行う
    if not error_occurred and loaded_bases:
        log("💾 すべての差分マージ計算が完了しました。統合ファイルを上書き保存中...")
        for ym, b_info in loaded_bases.items():
            final_df = b_info["df"].sort_values(["ticker", "date"]).reset_index(drop=True)
            local_save_path = b_info["local_path"]
            f_name = b_info["filename"]
            
            try:
                table = pa.Table.from_pandas(final_df, preserve_index=False)
                pq.write_table(table, local_save_path, use_dictionary=False, compression="SNAPPY")
                
                # Google Driveの本番ファイルを同期更新（年月フォルダではなく、tf_folder_id直下にフラット保存）
                up_success, up_msg = upload_to_drive_api(f_name, local_save_path, parent_id=tf_folder_id)
                if up_success:
                    log(f"   ✅ [{f_name}] 本番ファイルをGoogleドライブへ確定保存しました。({len(final_df):,}件)")
                    if os.path.exists(local_save_path):
                        os.remove(local_save_path)
                else:
                    log(f"   ❌ [{f_name}] 本番同期に失敗: {up_msg}")
                    error_occurred = True
                    err_msg = f"本番同期エラー: {up_msg}"
            except Exception as e:
                log(f"   ❌ [{f_name}] 保存書き出し中に例外発生: {e}")
                error_occurred = True
                err_msg = str(e)

    # 6. 【安全削除】確定保存が「完全に成功」した場合のみ、Drive上の元差分ファイルを物理削除（無限ループ防止）
    if not error_occurred and processed_file_ids:
        log("🧹 データベースの確定保存を確認しました。Googleドライブ上の元差分ファイルを自動消去中...")
        del_count = 0
        for f_id in processed_file_ids:
            success = delete_file_from_drive(f_id)
            if success:
                del_count += 1
        log(f"   👉 使用済みの差分ファイル {del_count} 件をGoogleドライブから安全消去しました。")
        return {"success": True, "message": f"計 {len(processed_file_ids)} 件の差分マージと自動消去が正常に完了しました。"}

    return {"success": False, "message": err_msg if err_msg else "マージ処理を中断しました。"}


# --- 🚀 フラット設計仕様：本番統合ファイルを直下から一括ロード ---
def load_price_db(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False, columns: list = None, filters: list = None) -> pd.DataFrame:
    """1m, 5m 等の本番統合Parquetファイルをロードします（時間足フォルダ直下にある年月ファイルを一括結合）。"""
    if is_jp:
        tf_folder_id = get_or_create_drive_folder(interval, settings.FOLDER_ID)
        service = get_drive_service()
        if not service:
            return pd.DataFrame()
            
        try:
            # 時間足フォルダの直下にある本番年月Parquetファイル（_diff_ を含まないもの）を検出
            query = f"'{tf_folder_id}' in parents and name contains 'price_jp_' and not name contains '_diff_' and name contains '.parquet' and trashed=false"
            results = service.files().list(q=query, fields="files(id, name)").execute()
            base_files = results.get('files', [])
        except Exception:
            return pd.DataFrame()
            
        dfs = []
        for b_file in base_files:
            b_name = b_file['name']
            local_path = os.path.join(settings.WORK_DIR, b_name)
            
            # 無ければDriveから自動ダウンロード
            if not os.path.exists(local_path):
                download_from_drive_api(b_name, local_path, parent_id=tf_folder_id)
                
            if os.path.exists(local_path):
                try:
                    df = pd.read_parquet(local_path, columns=columns)
                    if not df.empty:
                        dfs.append(df)
                except Exception:
                    pass
                    
        if not dfs:
            return pd.DataFrame()
        combined = pd.concat(dfs, ignore_index=True)
        if "date" in combined.columns:
            combined["date"] = pd.to_datetime(combined["date"]).dt.tz_localize(None)
        return combined.drop_duplicates(subset=["date", "ticker"], keep="last")

    # 米国株（従来互換）
    if interval in ["1m", "5m"]:
        pattern = get_db_filename_pattern(interval, is_jp, is_raw, is_temp)
        search_path = os.path.join(settings.WORK_DIR, pattern)
        files = glob.glob(search_path)
        
        if not files and not is_temp:
            now_ym = pd.Timestamp.now().strftime("%Y_%m")
            temp_filename = get_db_filename(interval, is_jp, is_raw, is_temp, year_month=now_ym)
            temp_work_file = os.path.join(settings.WORK_DIR, temp_filename)
            download_from_drive_api(temp_filename, temp_work_file)
            files = glob.glob(search_path)
            
        if not files:
            if is_raw: return pd.DataFrame()
            raise FileNotFoundError(f"【DB未検出】{interval} Parquet")
            
        dfs = []
        for filepath in files:
            try:
                df = pd.read_parquet(filepath, columns=columns)
                if not df.empty: dfs.append(df)
            except Exception: pass
        if not dfs: return pd.DataFrame()
        combined_df = pd.concat(dfs, ignore_index=True)
        if "date" in combined_df.columns:
            combined_df["date"] = pd.to_datetime(combined_df["date"]).dt.tz_localize(None)
        return combined_df.drop_duplicates(subset=["date", "ticker"], keep="last")

    filename = get_db_filename(interval, is_jp, is_raw, is_temp)
    work_file = os.path.join(settings.WORK_DIR, filename)
    if os.path.exists(work_file):
        df = pd.read_parquet(work_file, columns=columns)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        return df
    return pd.DataFrame()