# data_access/drive_api.py
import os
from google.oauth2.credentials import Credentials as OAuth2Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from config import settings

try:
    import streamlit as st
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

def get_drive_credentials():
    """secrets.tomlの[google_oauth]セクションからOAuth2接続情報をロードして生成します。"""
    cfg = None
    
    # 1. Streamlit環境からの読み込み試行
    if HAS_STREAMLIT:
        try:
            if hasattr(st, "secrets") and "google_oauth" in st.secrets:
                cfg = dict(st.secrets["google_oauth"])
        except Exception:
            pass
            
    # 2. 非GUI（バックグラウンドバッチ）環境からの直接読み込み試行
    if cfg is None:
        try:
            import toml
            secrets_path = os.path.join(settings.PROJECT_ROOT, ".streamlit", "secrets.toml")
            if os.path.exists(secrets_path):
                cfg = toml.load(secrets_path).get("google_oauth")
        except Exception:
            pass

    if not cfg or "refresh_token" not in cfg:
        print("❌ [drive_api] OAuth2の設定(google_oauth)が見つかりません。")
        return None

    try:
        # 個人アカウント（authorized_user形式）として認証インスタンスを作成
        creds = OAuth2Credentials(
            token=None,
            refresh_token=cfg["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"]
        )
        return creds
    except Exception as e:
        print(f"❌ [drive_api] OAuth2認証オブジェクトの生成に失敗しました: {e}")
        return None

def get_drive_service():
    """Google Drive API 操作用のサービスクライアントを作成して返します。"""
    creds = get_drive_credentials()
    if not creds:
        return None
    try:
        return build('drive', 'v3', credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"❌ [drive_api] Google Drive サービスの作成に失敗しました: {e}")
        return None

def download_from_drive_api(filename: str, local_path: str, parent_id: str = None) -> bool:
    """指定の親フォルダからファイルをローカルにダウンロードします。"""
    service = get_drive_service()
    p_id = parent_id if parent_id else settings.FOLDER_ID
    if not service or not p_id:
        return False
    try:
        query = f"name='{filename}' and '{p_id}' in parents and trashed=false"
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

def upload_to_drive_api(filename: str, local_path: str, parent_id: str = None) -> tuple[bool, str]:
    """指定のフォルダへファイルをアップロード（上書きまたは新規）します。"""
    service = get_drive_service()
    p_id = parent_id if parent_id else settings.FOLDER_ID
    if not service:
        return False, "Google Drive サービスインスタンスの作成に失敗しました。"
    if not p_id:
        return False, "親フォルダIDが設定されていません。"
        
    try:
        query = f"name='{filename}' and '{p_id}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        items = results.get('files', [])
        
        media = MediaFileUpload(local_path, mimetype='application/octet-stream', resumable=True)
        if items:
            file_id = items[0]['id']
            service.files().update(fileId=file_id, media_body=media).execute()
            return True, "既存ファイルを正常に上書き更新しました。"
        else:
            file_metadata = {'name': filename, 'parents': [p_id]}
            service.files().create(body=file_metadata, media_body=media).execute()
            return True, "新規ファイルを正常に作成アップロードしました。"
    except Exception as e:
        return False, str(e)

def get_or_create_drive_folder(folder_name: str, parent_id: str) -> str:
    """指定された親フォルダ配下に、対象の名前のフォルダ（時間足フォルダなど）を検索・自動作成します。"""
    service = get_drive_service()
    if not service:
        raise ConnectionError("Google Driveサービスを起動できません。")
        
    try:
        query = f"name='{folder_name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        items = results.get('files', [])
        if items:
            return items[0]['id']
            
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = service.files().create(body=folder_metadata, fields='id').execute()
        return folder.get('id')
    except Exception as e:
        raise IOError(f"Google Drive上のフォルダ「{folder_name}」の作成に失敗しました: {e}")

def list_drive_diff_files(parent_id: str) -> list:
    """指定フォルダ（例：1m 時間足フォルダ）の直下にある未処理差分ファイル（_diff_を含むもの）を検索取得します。"""
    service = get_drive_service()
    if not service:
        return []
    try:
        query = f"'{parent_id}' in parents and name contains '_diff_' and name contains '.parquet' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name, createdTime)").execute()
        return results.get('files', [])
    except Exception:
        return []

def delete_file_from_drive(file_id: str) -> bool:
    """Google Drive上から特定のファイルを完全に削除します。"""
    service = get_drive_service()
    if not service:
        return False
    try:
        service.files().delete(fileId=file_id).execute()
        return True
    except Exception:
        return False