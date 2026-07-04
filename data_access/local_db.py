# data_access/local_db.py
import os
import shutil
import pandas as pd
from config import settings
from data_access.drive_api import download_from_drive_api, upload_to_drive_api

def get_db_filename(interval: str, is_jp: bool = True, is_raw: bool = False) -> str:
    """時間足、市場フラグ、Raw/Activeフラグから、統一されたParquetファイル名を生成します。"""
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    return f"price_{market}_{interval}{suffix}.parquet"

def load_price_db(interval: str, is_jp: bool = True, is_raw: bool = False) -> pd.DataFrame:
    """
    Drive、またはローカルフォルダから該当するParquetデータベースをロードし、
    DataFrameにパースして返します。
    """
    filename = get_db_filename(interval, is_jp, is_raw)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)

    api_success = download_from_drive_api(filename, work_file)
    if not api_success and os.path.exists(drive_file):
        shutil.copy2(drive_file, work_file)
    
    if os.path.exists(work_file):
        df = pd.read_parquet(work_file)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        return df
    
    if is_raw:
        # Rawデータがまだ存在しない最初の段階では空のDataFrameを返す
        return pd.DataFrame()
    
    raise FileNotFoundError(
        f"【データベースファイル未検出】'{filename}' が見つかりませんでした。意図しない全件ダウンロードを避けるため中断します。"
    )

def save_price_db(df: pd.DataFrame, interval: str, is_jp: bool = True, is_raw: bool = False) -> tuple[bool, str]:
    """
    DataFrameをParquetとして書き出し、Google Driveへ同期保存します。
    戻り値: (Google Drive同期の成功成否フラグ, 結果または例外エラーメッセージ)
    """
    if df.empty:
        return False, "データが空のため、書き込み処理をスキップしました。"
        
    filename = get_db_filename(interval, is_jp, is_raw)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)
    
    # 辞書エンコーディングをオフにして、複数辞書ページの重複書き込みバグを強制回避
    df.to_parquet(work_file, index=False, use_dictionary=False)
    
    # Driveへ自動バックアップ
    api_success, api_msg = upload_to_drive_api(filename, work_file)
    if not api_success:
        try:
            shutil.copy2(work_file, drive_file)
        except Exception as e:
            print(f"Failed copy to drive directory: {e}")
            
    return api_success, api_msg