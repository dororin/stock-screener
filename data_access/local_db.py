# data_access/local_db.py の修正後コード

import os
import shutil
import json
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from config import settings
from data_access.drive_api import download_from_drive_api, upload_to_drive_api

def get_db_filename(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False) -> str:
    """時間足、市場フラグ、Raw/Activeフラグ、一時ファイルフラグから統一されたParquetファイル名を生成します。"""
    market = "jp" if is_jp else "us"
    suffix = "_raw" if is_raw else ""
    temp_suffix = "_temp" if is_temp else ""
    return f"price_{market}_{interval}{suffix}{temp_suffix}.parquet"

def read_parquet_ledger(filepath: str) -> dict:
    """
    Parquetのフッター（メタデータ領域）のみをピンポイントでスキャンし、台帳(Ledger)を取得します。
    実データはロードしないため、数百万行のファイルであってもメモリ消費はほぼ0MB、一瞬で完了します。
    """
    if not os.path.exists(filepath):
        return {}
    try:
        file_meta = pq.read_metadata(filepath)
        schema_metadata = file_meta.metadata
        if schema_metadata and b"stock_data_ledger" in schema_metadata:
            return json.loads(schema_metadata[b"stock_data_ledger"].decode('utf-8'))
    except Exception:
        pass
    return {}

def load_price_db_ledger(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False) -> dict:
    """
    実データをロードせず、メタデータフッター（台帳情報）のみを高速取得します。
    ファイルがローカルになければ必要最低限のダウンロードを行います。
    """
    filename = get_db_filename(interval, is_jp, is_raw, is_temp)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)

    if not is_temp and not os.path.exists(work_file):
        api_success = download_from_drive_api(filename, work_file)
        if not api_success and os.path.exists(drive_file):
            shutil.copy2(drive_file, work_file)
            
    return read_parquet_ledger(work_file)

def compute_ledger_from_df(df: pd.DataFrame) -> dict:
    """DataFrameから最新日付と銘柄ごとの最終更新日マップを高速に計算して台帳JSON形式にします。"""
    if df.empty:
        return {"db_max_date": None, "last_updates_map": {}}
    
    # 処理軽量化のために ticker と date のみ抽出して集計
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

def load_price_db(interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False, columns: list = None) -> pd.DataFrame:
    """Drive、またはローカルフォルダから該当するParquetデータベースをロードし、DataFrameにパースして返します。"""
    filename = get_db_filename(interval, is_jp, is_raw, is_temp)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)

    if is_temp:
        if os.path.exists(work_file):
            df = pd.read_parquet(work_file, columns=columns)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            return df
        return pd.DataFrame()

    api_success = download_from_drive_api(filename, work_file)
    if not api_success and os.path.exists(drive_file):
        shutil.copy2(drive_file, work_file)
    
    if os.path.exists(work_file):
        df = pd.read_parquet(work_file, columns=columns)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        return df
    
    if is_raw:
        return pd.DataFrame()
    
    raise FileNotFoundError(
        f"【データベースファイル未検出】'{filename}' が見つかりませんでした。"
    )

def load_price_db_for_tickers(interval: str, tickers: list, is_jp: bool = True, is_raw: bool = False) -> pd.DataFrame:
    """
    特定の銘柄群のみをParquetからフィルタロードします。
    余分なデータを一切ロードしないため、特定銘柄の遡及分割やパッチ処理時のメモリ消費が大幅に抑えられます。
    """
    filename = get_db_filename(interval, is_jp, is_raw, is_temp=False)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)

    if not os.path.exists(work_file):
        api_success = download_from_drive_api(filename, work_file)
        if not api_success and os.path.exists(drive_file):
            shutil.copy2(drive_file, work_file)

    if os.path.exists(work_file):
        try:
            table = pq.read_table(work_file, filters=[('ticker', 'in', tickers)])
            df = table.to_pandas()
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            return df
        except Exception:
            # PyArrowフィルタリングが失敗した場合のセーフティフォールバック
            df = pd.read_parquet(work_file)
            return df[df["ticker"].isin(tickers)].reset_index(drop=True)
            
    return pd.DataFrame()

def load_price_db_excluding_tickers(interval: str, tickers: list, is_jp: bool = True) -> pd.DataFrame:
    """特定の銘柄群を除外してActiveデータを高速ロードします（部分上書き用）。"""
    filename = get_db_filename(interval, is_jp, is_raw=False, is_temp=False)
    work_file = os.path.join(settings.WORK_DIR, filename)
    if os.path.exists(work_file):
        try:
            table = pq.read_table(work_file, filters=[('ticker', 'not in', tickers)])
            df = table.to_pandas()
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            return df
        except Exception:
            df = pd.read_parquet(work_file)
            return df[~df["ticker"].isin(tickers)].reset_index(drop=True)
    return pd.DataFrame()

def save_price_db(df: pd.DataFrame, interval: str, is_jp: bool = True, is_raw: bool = False, is_temp: bool = False, custom_ledger: dict = None) -> tuple:
    """
    DataFrameをParquetとして書き出します。
    メタデータ（フッター）領域に台帳JSONデータを新しく埋め込みます。
    """
    if df.empty:
        return False, "保存対象のデータが空（Empty）です。"
        
    filename = get_db_filename(interval, is_jp, is_raw, is_temp)
    work_file = os.path.join(settings.WORK_DIR, filename)
    drive_file = os.path.join(settings.DRIVE_DIR, filename)
    
    try:
        # PyArrowテーブルを生成してメタデータを注入
        table = pa.Table.from_pandas(df, preserve_index=False)
        
        # 台帳メタデータの生成
        ledger = custom_ledger if custom_ledger else compute_ledger_from_df(df)
        existing_meta = table.schema.metadata or {}
        new_meta = {
            **existing_meta,
            b"stock_data_ledger": json.dumps(ledger).encode('utf-8')
        }
        table = table.replace_schema_metadata(new_meta)
        
        # 圧縮形式にSNAPPYを指定して高速読み書き
        pq.write_table(table, work_file, use_dictionary=False, compression="SNAPPY")
    except Exception as e:
        return False, f"ローカルParquetファイルの書き出しに失敗しました: {e}"
    
    if is_temp:
        return True, ""
    
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
    """Dry Run時の検証済み一時Parquet（_temp）を本番Activeファイルとして正式昇格リネームします。"""
    temp_filename = get_db_filename(interval, is_jp, is_raw=False, is_temp=True)
    active_filename = get_db_filename(interval, is_jp, is_raw=False, is_temp=False)
    
    temp_work_file = os.path.join(settings.WORK_DIR, temp_filename)
    active_work_file = os.path.join(settings.WORK_DIR, active_filename)
    active_drive_file = os.path.join(settings.DRIVE_DIR, active_filename)
    
    if not os.path.exists(temp_work_file):
        return False, f"一時ファイル {temp_filename} が見つかりません。"
        
    try:
        if os.path.exists(active_work_file):
            os.remove(active_work_file)
        os.rename(temp_work_file, active_work_file)
        
        api_success = upload_to_drive_api(active_filename, active_work_file)
        if not api_success:
            try:
                shutil.copy2(active_work_file, active_drive_file)
            except Exception as e:
                return False, f"Google Drive API失敗後のローカル共有フォルダ保存に失敗しました: {e}"
            return True, "API失敗のため、ローカル共有フォルダへ保存しました。"
            
        return True, "本番Activeデータベースとして正常に同期・保存しました。"
    except Exception as e:
        return False, f"一時ファイルの本番確定処理中に例外が発生しました: {e}"