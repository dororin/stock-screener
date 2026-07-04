# data_access/local_db.py
import os
import shutil
import pandas as pd
from config import settings
from data_access.drive_api import download_from_drive_api, upload_to_drive_api

def get_db_filename(interval: str, is_jp: bool = True, is_raw: bool = False) -> str:
    """時間足、市場フラグ、Raw/Activeフラグから統一されたParquetファイル名を生成します。"""
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    return f"price_{market}_{interval}{suffix}.parquet"

def load_price_db(interval: str, is_jp: bool = True, is_raw: bool = False) -> pd.DataFrame:
    """
    Drive、またはローカルフォルダから該当するParquetデータベースをロードし、
    DataFrameにパースして返します。is_raw=True時にファイルが存在しない場合は、新規作成用に空のDataFrameを返します。
    """
    filename = get_db_filename(interval, is_jp, is_raw)
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
    
    # Rawデータがまだ存在しない新規構築時は、例外を出さず空のDataFrameを返す
    if is_raw:
        return pd.DataFrame()
    
    raise FileNotFoundError(
        f"【データベースファイル未検出】'{filename}' が見つかりませんでした。意図しない全件ダウンロードを避けるため中断します。"
    )

def save_price_db(df: pd.DataFrame, interval: str, is_jp: bool = True, is_raw: bool = False) -> tuple:
    """
    DataFrameをParquetとして書き出し、Google Driveへ同期保存します。
    戻り値: (成功フラグ: bool, エラーメッセージ/詳細: str)
    """
    if df.empty:
        return False, "保存対象のデータが空（Empty）です。"
        
    filename = get_db_filename(interval, is_jp, is_raw)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)
    
    try:
        # 辞書エンコーディングをオフにして、複数辞書ページの重複書き込みバグを強制回避
        df.to_parquet(work_file, index=False, use_dictionary=False)
    except Exception as e:
        return False, f"ローカルParquetファイルの書き出しに失敗しました: {e}"
    
    # Driveへ自動バックアップ
    try:
        api_success = upload_to_drive_api(filename, work_file)
        if not api_success:
            # APIが利用できない環境や失敗時はローカルデータフォルダ間コピーで代用
            try:
                shutil.copy2(work_file, drive_file)
            except Exception as e:
                return False, f"Google Drive APIが失敗し、ローカル共有フォルダ(data_drive)へのコピーも失敗しました: {e}"
            return True, ""
        return True, ""
    except Exception as e:
        return False, f"GoogleドライブAPIアップロード中に予期せぬ例外エラーが発生しました: {e}"