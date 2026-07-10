# data_access/local_db.py
import os
import shutil
import pandas as pd
from config import settings
from data_access.drive_api import download_from_drive_api, upload_to_drive_api

def get_db_filename(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False) -> str:
    """時間足、市場フラグ、Raw/Activeフラグ、一時ファイルフラグから統一されたParquetファイル名を生成します。"""
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    temp_suffix = "_temp" if is_temp else ""
    return f"price_{market}_{interval}{suffix}{temp_suffix}.parquet"

def load_price_db(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False) -> pd.DataFrame:
    """
    Drive、またはローカルフォルダから該当するParquetデータベースをロードし、DataFrameにパースして返します。
    is_temp=True の場合、Drive APIアクセスを完全にスキップしてローカルの一時退避ファイルを直接ロードします。
    """
    filename = get_db_filename(interval, is_jp, is_raw, is_temp)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)

    # 一時ファイルの読み込み時はDriveとの同期・ダウンロードをスキップしてローカル完結
    if is_temp:
        if os.path.exists(work_file):
            df = pd.read_parquet(work_file)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            return df
        return pd.DataFrame()

    # 通常時のダウンロード第一優先
    api_success = download_from_drive_api(filename, work_file)
    if not api_success and os.path.exists(drive_file):
        shutil.copy2(drive_file, work_file)
    
    if os.path.exists(work_file):
        df = pd.read_parquet(work_file)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        return df
    
    if is_raw:
        return pd.DataFrame()
    
    raise FileNotFoundError(
        f"【データベースファイル未検出】'{filename}' が見つかりませんでした。意図しない全件ダウンロードを避けるため中断します。"
    )

def save_price_db(df: pd.DataFrame, interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False) -> tuple:
    """
    DataFrameをParquetとして書き出します。is_temp=Trueの場合、
    Googleドライブへのアップロードを完全にスキップし、ローカルの作業ディレクトリへの退避のみを行います。
    """
    if df.empty:
        return False, "保存対象のデータが空（Empty）です。"
        
    filename = get_db_filename(interval, is_jp, is_raw, is_temp)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)
    
    try:
        df.to_parquet(work_file, index=False, use_dictionary=False)
    except Exception as e:
        return False, f"ローカルParquetファイルの書き出しに失敗しました: {e}"
    
    # 一時ファイルの場合はGoogleドライブや共有フォルダへの自動同期をスキップ（メモリ解放最優先）
    if is_temp:
        return True, ""
    
    # 通常時のDriveバックアップ
    try:
        api_success = upload_to_drive_api(filename, work_file)
        if not api_success:
            try:
                shutil.copy2(work_file, drive_file)
            except Exception as e:
                return False, f"Google Drive APIが失敗し、ローカル共有フォルダへのコピーも失敗しました: {e}"
            return True, ""
        return True, ""
    except Exception as e:
        return False, f"GoogleドライブAPIアップロード中に予期せぬ例外エラーが発生しました: {e}"

def promote_temp_db_to_active(interval: str, is_jp: bool = True) -> tuple[bool, str]:
    """
    Dry Run時にローカルに一時保存されたParquetファイル（_temp）を、
    本番Activeファイルとして名前を変更し、Google Driveへ一括アップロード（本番確定）します。
    """
    temp_filename = get_db_filename(interval, is_jp, is_raw=False, is_temp=True)
    active_filename = get_db_filename(interval, is_jp, is_raw=False, is_temp=False)
    
    temp_work_file = os.path.join(settings.WORK_DIR, temp_filename)
    active_work_file = os.path.join(settings.WORK_DIR, active_filename)
    active_drive_file = os.path.join(settings.DRIVE_DIR, active_filename)
    
    if not os.path.exists(temp_work_file):
        return False, f"一時ファイル {temp_filename} が見つかりません。テスト同期が未実行の可能性があります。"
        
    try:
        # ローカルディスク上で、一時ファイルをActive用ファイルにリネーム
        if os.path.exists(active_work_file):
            os.remove(active_work_file)
        os.rename(temp_work_file, active_work_file)
        
        # 確定した本番ActiveファイルをGoogle Driveにアップロード
        api_success = upload_to_drive_api(active_filename, active_work_file)
        if not api_success:
            try:
                shutil.copy2(active_work_file, active_drive_file)
            except Exception as e:
                return False, f"Google Drive API失敗後の共有フォルダ(data_drive)への書き込みに失敗しました: {e}"
            return True, "API失敗のため、ローカル共有フォルダへ保存しました。"
            
        return True, "本番Activeデータベースとして正常に同期・保存しました。"
    except Exception as e:
        return False, f"一時ファイルの本番確定処理中に例外が発生しました: {e}"