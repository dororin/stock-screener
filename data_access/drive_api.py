# data_access/drive_api.py
import os
from google.oauth2.service_account import Credentials as SACredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from config import settings

def get_drive_service():
    """
    環境（Streamlitか、Kaggle/Colab/ローカルスクリプトか）を判別し、
    Google Drive APIサービスインスタンスを返します。
    """
    if not settings.HAS_STREAMLIT:
        # ローカル/スクリプト実行時
        try:
            import toml
            secrets_path = os.path.join(settings.PROJECT_ROOT, ".streamlit", "secrets.toml")
            if os.path.exists(secrets_path):
                cfg = toml.load(secrets_path)["connections"]["gsheets"]
                sa_info = {k: cfg[k] for k in ["type", "project_id", "private_key_id", "private_key", "client_email", "client_id", "auth_uri", "token_uri"] if k in cfg}
                sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
                creds = SACredentials.from_service_account_info(sa_info, scopes=["https://www.googleapis.com/auth/drive"])
                return build('drive', 'v3', credentials=creds)
        except Exception:
            pass
        return None

    # Streamlit Cloud環境
    try:
        import streamlit as st
        if "connections" in st.secrets and "gsheets" in st.secrets["connections"]:
            cfg = dict(st.secrets["connections"]["gsheets"])
            sa_keys = ["type", "project_id", "private_key_id", "private_key", "client_email", "client_id", "auth_uri", "token_uri"]
            sa_info = {k: cfg[k] for k in sa_keys if k in cfg}
            if "private_key" in sa_info:
                sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
            creds = SACredentials.from_service_account_info(sa_info, scopes=["https://www.googleapis.com/auth/drive"])
            return build('drive', 'v3', credentials=creds)
    except Exception:
        pass
    return None

def download_from_drive_api(filename: str, local_path: str) -> bool:
    """指定されたファイルをGoogle Driveからダウンロードし、ローカルに保存します。"""
    service = get_drive_service()
    if not service or not settings.FOLDER_ID:
        return False
    try:
        query = f"name='{filename}' and '{settings.FOLDER_ID}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        items = results.get('files', [])
        if not items:
            return False
        
        file_id = items[0]['id']
        request = service.files().get_media(fileId=file_id)
        with open(local_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True
    except Exception:
        return False

def upload_to_drive_api(filename: str, local_path: str) -> bool:
    """指定されたローカルファイルをGoogle Driveにアップロード（または上書き）します。"""
    service = get_drive_service()
    if not service:
        print("⚠️ [Drive API] Google Drive サービスインスタンスの作成に失敗しました。認証情報(secrets)を確認してください。")
        return False
    if not settings.FOLDER_ID:
        print("⚠️ [Drive API] FOLDER_ID が設定されていません。")
        return False
        
    try:
        query = f"name='{filename}' and '{settings.FOLDER_ID}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        items = results.get('files', [])
        
        media = MediaFileUpload(local_path, mimetype='application/octet-stream', resumable=True)
        if items:
            file_id = items[0]['id']
            service.files().update(fileId=file_id, media_body=media).execute()
            print(f"☁️ [Drive API] 既存ファイル {filename} を正常に上書き（update）しました。")
        else:
            file_metadata = {'name': filename, 'parents': [settings.FOLDER_ID]}
            service.files().create(body=file_metadata, media_body=media).execute()
            print(f"☁️ [Drive API] 新規ファイル {filename} を正常に作成（create）しました。")
        return True
    except Exception as e:
        # ⚠️ 例外を隠さず、Streamlitのログ（黒い画面）に具体的なエラー内容を書き出す
        print(f"❌ [Drive API] Google Driveへのアップロード中にエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()
        return False