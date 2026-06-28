# scripts/sync_etf_master.py
import sys
import os

# プロジェクトルートをインポートパスに追加
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# Streamlitのsecretsを非GUIバッチ環境用にフォールバック
import toml
try:
    import streamlit as st
    if not hasattr(st, "secrets") or not st.secrets:
        secrets_path = os.path.join(project_root, ".streamlit", "secrets.toml")
        if os.path.exists(secrets_path):
            st.secrets = toml.load(secrets_path)
except Exception:
    pass

from data_access.sheets_api import sync_etf_sectors_consolidated

def main():
    print("🔄 [sync_etf_master] sector_JP シートからの自律型自動同期を開始します...")
    
    # スプレッドシートの記述をもとに、自動的に対象ETFを走査・マージします
    results = sync_etf_sectors_consolidated(is_jp=True)
    
    if "error" in results:
        print(f"❌ エラーにより同期を中断しました: {results['error']}")
        return
        
    if "info" in results:
        print(f"ℹ️ {results['info']}")
        return
        
    print("\n■ 各セクターの同期処理結果:")
    for sector, status in results.items():
        print(f"  * 【{sector}】: {status}")
        
    print("\n🎉 同期・書き換え処理が正常に終了しました。")

if __name__ == "__main__":
    main()