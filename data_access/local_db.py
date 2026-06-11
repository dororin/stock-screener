# data_access/local_db.py
import os
import shutil
import pandas as pd
from config import settings
from data_access.drive_api import download_from_drive_api, upload_to_drive_api

def get_db_filename(interval: str, is_jp: bool = True) -> str:
    """時間足と市場フラグから、統一されたParquetファイル名を生成します。"""
    market = "jp" if is_jp else "us"
    return f"price_{market}_{interval}.parquet"

def load_price_db(interval: str, is_jp: bool = True) -> pd.DataFrame:
    """
    Drive、またはローカルフォルダから該当するParquetデータベースをロードし、
    DataFrameにパースして返します。
    """
    filename = get_db_filename(interval, is_jp)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)

    # API経由でのダウンロードを第一優先
    api_success = download_from_drive_api(filename, work_file)
    if not api_success and os.path.exists(drive_file):
        shutil.copy2(drive_file, work_file)
    
    if os.path.exists(work_file):
        df = pd.read_parquet(work_file)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        return df
    
    raise FileNotFoundError(
        f"【データベースファイル未検出】'{filename}' が見つかりませんでした。意図しない全件ダウンロードを避けるため中断します。"
    )

def save_price_db(df: pd.DataFrame, interval: str, is_jp: bool = True):
    """DataFrameをParquetとして書き出し、Google Driveへ同期保存します。"""
    if df.empty:
        return
    filename = get_db_filename(interval, is_jp)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)
    
    # 辞書エンコーディングをオフにして、複数辞書ページの重複書き込みバグを強制回避
    df.to_parquet(work_file, index=False, use_dictionary=False)
    
    # Driveへ自動バックアップ
    api_success = upload_to_drive_api(filename, work_file)
    if not api_success:
        try:
            shutil.copy2(work_file, drive_file)
        except Exception as e:
            print(f"Failed copy to drive directory: {e}")