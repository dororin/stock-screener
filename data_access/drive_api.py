# data_access/drive_api.py
import os
from google.oauth2.service_account import Credentials as SACredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from config import settings

def get_drive_service():
    if not settings.HAS_STREAMLIT:
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

def upload_to_drive_api(filename: str, local_path: str) -> tuple[bool, str]:
    """
    指定されたローカルファイルをGoogle Driveにアップロード（または上書き）します。
    戻り値: (成功成否のbool値, 処理メッセージまたは例外エラー詳細文字列)
    """
    service = get_drive_service()
    if not service:
        return False, "Google Drive サービスインスタンスの作成に失敗しました。secrets定義を確認してください。"
    if not settings.FOLDER_ID:
        return False, "FOLDER_ID（共有フォルダID）が設定されていません。"
        
    try:
        query = f"name='{filename}' and '{settings.FOLDER_ID}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        items = results.get('files', [])
        
        media = MediaFileUpload(local_path, mimetype='application/octet-stream', resumable=True)
        if items:
            file_id = items[0]['id']
            service.files().update(fileId=file_id, media_body=media).execute()
            return True, "既存ファイルを正常に上書き（update）しました。"
        else:
            file_metadata = {'name': filename, 'parents': [settings.FOLDER_ID]}
            service.files().create(body=file_metadata, media_body=media).execute()
            return True, "新規ファイルを正常に作成（create）しました。"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)