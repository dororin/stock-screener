# yfinance_collector_us.py

import os
import sys
import time
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf
import pytz

# プロジェクトルートをインポートパスに追加
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

# Streamlitのsecretsを非GUIバッチ環境用にフォールバック
import toml
secrets_path = os.path.join(current_dir, ".streamlit", "secrets.toml")
if os.path.exists(secrets_path):
    try:
        import streamlit as st
        st.secrets = toml.load(secrets_path)
    except Exception:
        pass

from config import settings
from data_access.local_db import load_price_db_ledger, load_price_db, save_price_db
from data_access.sheets_api import load_sector_master_from_sheets, upload_sync_log_to_drive
# 米国株専用補正モジュールからパースロジックをインポート
from core.us_price_corrector import parse_yfinance_batch

# ─── データ収集の設定パラメータ ───
TIMEFRAMES = ["1d", "60m", "5m", "1m"]
YFINANCE_GAP_LIMITS = {"1m": 7, "5m": 60, "60m": 730}

# デフォルトの主要米国銘柄リスト
DEFAULT_US_TICKERS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AMD", "AVGO", "QCOM", 
    "MU", "INTC", "JPM", "BAC", "GS", "MS", "WFC", "XOM", "CVX", "COP", 
    "SLB", "TSLA", "HD", "MCD", "NFLX", "NEE", "LIN"
]

def load_us_tickers_from_sheets() -> list:
    """Google Sheetsの sector_US タブから、重複を排除した一意な米国株収集対象リストをロードします。"""
    print("📡 [1/4] Google Sheetsから米国株の収集対象リストを読み込み中...")
    tickers = set()
    try:
        sector_master = load_sector_master_from_sheets(is_jp=False)
        for sname, t_list in sector_master.items():
            for t in t_list:
                tickers.add(str(t).strip().upper())
    except Exception as e:
        print(f"  ⚠️ sector_USシートの読み込みに失敗しました（デフォルトリストを使用します）: {e}")

    cleaned_list = sorted(list(tickers)) if tickers else DEFAULT_US_TICKERS
    print(f"  ✅ 米国株対象ティッカー確定。総計: {len(cleaned_list)} 銘柄")
    return cleaned_list

def get_market_localized_now_ny():
    """ニューヨーク時間を基準とした現在の日時情報を返します。"""
    tz = pytz.timezone("America/New_York")
    now_tz = datetime.now(pytz.utc).astimezone(tz)
    local_today = now_tz.date()
    return now_tz.replace(tzinfo=None), local_today

def collect_us_data_via_yfinance(tickers: list, interval: str) -> pd.DataFrame:
    """
    yfinance の API を用いて、指定された全米国株銘柄の差分データを一括バッチダウンロードします。
    """
    now_naive, local_today = get_market_localized_now_ny()
    print(f"📡 [yfinance] 【{interval}】の米国株データ差分収集を開始します...")

    # 既存Rawデータベースのフッター台帳（Ledger）から差分起点を特定
    ledger = load_price_db_ledger(interval, is_jp=False, is_raw=True)
    last_updates_map = ledger.get("last_updates_map", {}) if ledger else {}

    # yfinance の制約限界を初期起点にする
    if interval == "1m":
        default_start_dt = now_naive - timedelta(days=6)
    elif interval == "5m":
        default_start_dt = now_naive - timedelta(days=58)
    elif interval == "60m":
        default_start_dt = now_naive - timedelta(days=718)
    else:
        default_start_dt = datetime(2016, 1, 1)

    all_downloaded_parts = []
    
    BATCH_SIZE = 30
    for b_idx in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[b_idx : b_idx + BATCH_SIZE]
        chunk_updates = [pd.to_datetime(last_updates_map[t]) for t in chunk if t in last_updates_map]
        start_dt = min(chunk_updates) if chunk_updates else default_start_dt
        
        # yfinanceの取得限界日数制限
        if interval in YFINANCE_GAP_LIMITS:
            limit_days = YFINANCE_GAP_LIMITS[interval]
            gap_days = (local_today - start_dt.date()).days
            if gap_days > limit_days:
                start_dt = now_naive - timedelta(days=limit_days - 1)

        start_date_str = start_dt.strftime("%Y-%m-%d")
        print(f"  -> バッチ {b_idx//BATCH_SIZE + 1} / {(len(tickers)-1)//BATCH_SIZE + 1} ({len(chunk)} 銘柄) をダウンロード中... (開始基準日: {start_date_str})")

        try:
            df_raw = yf.download(
                chunk,
                start=start_date_str,
                interval=interval,
                auto_adjust=False,
                actions=True,
                progress=False,
                threads=False,
                timeout=30
            )

            if not df_raw.empty:
                chunk_processed = parse_yfinance_batch(df_raw, chunk)
                if not chunk_processed.empty:
                    all_downloaded_parts.append(chunk_processed)
            
        except Exception as e:
            print(f"    ⚠️ バッチダウンロード中にエラーが発生しました: {e}")
        
        time.sleep(1.5)

    if not all_downloaded_parts:
        return pd.DataFrame()

    combined_df = pd.concat(all_downloaded_parts, ignore_index=True)
    
    filtered_rows = []
    for ticker, group in combined_df.groupby("ticker"):
        t_last = last_updates_map.get(ticker)
        if t_last is not None:
            group = group[pd.to_datetime(group["date"]) > pd.to_datetime(t_last)]
        filtered_rows.append(group)

    if not filtered_rows:
        return pd.DataFrame()

    final_df = pd.concat(filtered_rows, ignore_index=True)
    return final_df.sort_values(["ticker", "date"]).reset_index(drop=True)

def main():
    print("=====================================================================")
    print("🚀 yfinance・米国株 統合データ同期エンジン（2層構造版） 起動")
    print(f"🕒 実行開始日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=====================================================================")
    
    logs_accumulator = []
    def log(msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line)
        logs_accumulator.append(line)

    try:
        tickers = load_us_tickers_from_sheets()
    except Exception as e:
        log(f"❌ 米国株監視リストのロードに失敗しました。同期を中止します: {e}")
        return

    for interval in TIMEFRAMES:
        log(f"⏱️ 【{interval}】の米国株同期セッションを開始します...")
        
        df_new = collect_us_data_via_yfinance(tickers, interval)
        if df_new.empty:
            log(f"  📥 【{interval}】 新規の追加データはありませんでした（最新状態です）。")
            continue
            
        log(f"  📥 【{interval}】 差分ダウンロード成功。新規データ: {len(df_new):,} 行")

        # 既存Rawデータベース（is_raw=True）へマージ・保存
        log(f"  🛠️ 【{interval}】 既存のRawデータベースへマージを開始します...")
        try:
            try:
                df_raw = load_price_db(interval, is_jp=False, is_raw=True)
            except FileNotFoundError:
                df_raw = pd.DataFrame()

            if not df_raw.empty:
                df_merged = pd.concat([df_raw, df_new], ignore_index=True)
                df_merged = df_merged.drop_duplicates(subset=["date", "ticker"], keep="last")
            else:
                df_merged = df_new

            df_merged = df_merged.sort_values(["ticker", "date"]).reset_index(drop=True)
            
            # 1. Raw DBへの書き込み
            success, msg = save_price_db(df_merged, interval, is_jp=False, is_raw=True)
            if success:
                log(f"  ✅ 【{interval}】 米国株Rawデータベースの保存同期が完了しました。")
                
                # 2. RawデータからActiveデータの自動加工・リビルドをキック
                log(f"  🛠️ 【{interval}】 RawデータからActiveデータベース（価格修正適用）をリビルド中...")
                from core.database_service import rebuild_active_from_raw
                rebuild_success = rebuild_active_from_raw(interval, is_jp=False, dry_run=False)
                if rebuild_success:
                    log(f"  🎉 【{interval}】 米国株Activeデータベースへの適用・ドライブ同期が正常完了しました。")
                else:
                    log(f"  ❌ 【{interval}】 米国株Activeデータベースのリビルド加工に失敗しました。")
            else:
                log(f"  ❌ 【{interval}】 Rawデータベースの保存同期に失敗しました: {msg}")

        except Exception as ex:
            log(f"  ❌ 【{interval}】 マージ・保存処理中にシステムエラーが発生しました: {ex}")

    # ログをGoogleドライブへバッチ転送
    try:
        log("📤 本日の米国株実行ログ履歴をGoogleドライブへアップロード同期しています...")
        log_filename = upload_sync_log_to_drive(logs_accumulator, is_jp=False, prefix="yf_sync_us")
        if log_filename:
            print(f"  ✅ ログファイル '{log_filename}' をGoogleドライブに保存完了しました。")
    except Exception as e:
        print(f"  ⚠️ ログのクラウド転送中に例外エラーを検知しました: {e}")

    print("\n=====================================================================")
    print("🎉 米国株に対するすべての時間足の同期・マージ・アップロード処理が正常に完了しました。")
    print("=====================================================================")

if __name__ == "__main__":
    main()