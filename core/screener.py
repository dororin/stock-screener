# core/screener.py
import os
import io
import requests
import pandas as pd
import numpy as np
import streamlit as st
from config import settings

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
}

def _fetch_jpx_raw_df() -> pd.DataFrame:
    """
    JPX公式Excelを読み込みます。
    ローカルキャッシュが存在すれば優先し、無ければ公式URLからダウンロードします。
    取得またはパースに失敗した場合は例外を送出して安全に停止します。
    """
    cache_path = os.path.join(settings.DRIVE_DIR, "jpx_stock_list_raw.xlsx")

    # 1. 既存キャッシュが存在する場合は読み込み検証
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 10000:
        for eng in ["openpyxl", None, "xlrd"]:
            try:
                df = pd.read_excel(cache_path, engine=eng)
                if df is not None and not df.empty:
                    return df
            except Exception:
                continue

    # 2. キャッシュが無い（または破損している）場合は公式URLからダウンロード
    try:
        resp = requests.get(settings.JPX_URL, headers=HEADERS, timeout=15)
    except Exception as e:
        raise RuntimeError(f"JPX銘柄マスタのダウンロード通信に失敗しました ({settings.JPX_URL}): {e}") from e

    if resp.status_code != 200:
        raise RuntimeError(f"JPX銘柄マスタの取得に失敗しました (HTTP {resp.status_code}): {settings.JPX_URL}")

    if len(resp.content) < 10000:
        raise RuntimeError(f"ダウンロードされたJPXファイルが小さすぎます（HTMLエラーページの可能性）: {len(resp.content)} bytes")

    # キャッシュ保存
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(resp.content)
    except Exception as e:
        print(f"⚠️ [screener.py] JPXキャッシュ保存失敗（メモリ展開で継続します）: {e}")

    # Excelパース
    for eng in ["openpyxl", None, "xlrd"]:
        try:
            df = pd.read_excel(io.BytesIO(resp.content), engine=eng)
            if df is not None and not df.empty:
                return df
        except Exception:
            continue

    raise RuntimeError("JPX銘柄マスタ(Excel)のパースに失敗しました。ファイル破損またはExcelエンジンの不足です。")


@st.cache_data(ttl=86400)
def get_jpx_list() -> pd.DataFrame:
    """規模区分に基づくスクリーニング基礎銘柄マスタ(TOPIX500)を取得します。失敗時は例外を出して停止します。"""
    df_full = _fetch_jpx_raw_df()

    if df_full.shape[1] < 10:
        raise RuntimeError(f"JPX銘柄マスタの列数が不足しています (列数: {df_full.shape[1]} < 10)。形式が変更された可能性があります。")

    df_scale = df_full.iloc[:, [1, 2, 9]].copy()
    df_scale.columns = ['symbol', 'name', 'scale_type']
    target_scales = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']

    df = df_scale[df_scale["scale_type"].isin(target_scales)][['symbol', 'name']].copy()
    df['symbol'] = df['symbol'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
    df['name'] = df['name'].astype(str).str.strip()
    df = df[df['symbol'].str.len() > 0].dropna(subset=['symbol']).reset_index(drop=True)

    if df.empty:
        raise RuntimeError("JPX銘柄マスタからTOPIX500（Core30/Large70/Mid400）の銘柄が1件も抽出できませんでした。")

    return df


@st.cache_data(ttl=86400)
def get_jpx_full_list() -> pd.DataFrame:
    """ETFやCore/Large/Midすべてのティッカー情報が内包された完全マスタを取得します。失敗時は例外を出して停止します。"""
    df_full = _fetch_jpx_raw_df()

    if df_full.shape[1] < 10:
        raise RuntimeError(f"JPX銘柄マスタの列数が不足しています (列数: {df_full.shape[1]} < 10)。")

    df_scale = df_full.iloc[:, [1, 2, 9]].copy()
    df_scale.columns = ['symbol', 'name', 'scale_type']
    target_scales = ['TOPIX Core30', 'TOPIX Large70', 'TOPIX Mid400']
    topix = df_scale[df_scale['scale_type'].isin(target_scales)][['symbol', 'name']]

    df_market = df_full.iloc[:, [1, 2, 3]].copy()
    df_market.columns = ['symbol', 'name', 'market']
    etf = df_market[df_market['market'] == 'ETF・ETN'][['symbol', 'name']]

    combined = pd.concat([topix, etf]).drop_duplicates(subset=['symbol']).copy()
    combined['symbol'] = combined['symbol'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
    combined['name'] = combined['name'].astype(str).str.strip()
    combined = combined[combined['symbol'].str.len() > 0].dropna(subset=['symbol']).reset_index(drop=True)

    if combined.empty:
        raise RuntimeError("JPX銘柄マスタから銘柄情報を1件も抽出できませんでした。")

    return combined


def run_fast_screening(db_df: pd.DataFrame, log_accumulator: list = None) -> pd.DataFrame:
    """WVF点灯＆200SMA上向き／上乗せ条件で高速スキャンを実行します。"""
    def log(msg):
        if log_accumulator is not None:
            log_accumulator.append(msg)
        print(f"[SCREENER] {msg}")

    if db_df.empty:
        log("❌ データベースが空のため、判定処理を中止します。")
        return pd.DataFrame()

    # ── JPXマスタ取得（失敗時は例外が発生し即座に停止する） ──
    try:
        jpx_list = get_jpx_list()
    except Exception as e:
        log(f"❌ JPX銘柄マスタの取得に失敗したため処理を中断します: {e}")
        raise e

    name_map = dict(zip(jpx_list['symbol'].astype(str), jpx_list['name']))

    results = []
    tickers = db_df['ticker'].unique()
    db_df = db_df.sort_values(["ticker", "date"])

    progress_bar = st.progress(0)
    status_text = st.empty()
    total = len(tickers)

    log(f"🔎 判定プロセスを開始します。総判定対象: {total} 銘柄")

    for idx, ticker in enumerate(tickers):
        if idx % 20 == 0:
            progress_bar.progress((idx + 1) / total)
            status_text.text(f"判定中: {ticker} ({idx+1}/{total})")

        try:
            df = db_df[db_df['ticker'] == ticker].copy()

            if len(df) < 220:
                log(f"⏭️ [{ticker}] スキップ：時系列データが不足しています（実績: {len(df)} 件 / 最小必要数: 220 件）")
                continue

            df['sma50'] = df['close'].rolling(window=50).mean()
            df['sma200'] = df['close'].rolling(window=200).mean()
            df['highest_close'] = df['close'].rolling(window=11).max()
            df['wvf'] = (df['highest_close'] - df['low']) / df['highest_close'] * 100
            df['wvf_std'] = df['wvf'].rolling(window=20).std(ddof=0)
            df['wvf_mid'] = df['wvf'].rolling(window=20).mean()
            df['wvf_upper'] = df['wvf_mid'] + (2.0 * df['wvf_std'])
            df['range_high'] = df['wvf'].rolling(window=100).max() * 0.85

            latest = df.iloc[-1]
            sma200_win = df['sma200'].tail(20).values
            slope, _ = np.polyfit(np.arange(len(sma200_win)), sma200_win, 1)
            slope_rate = slope / latest['close']

            is_uptrend = (latest['close'] > latest['sma200']) or (slope_rate >= -0.0001)
            is_wvf_lit = latest['wvf'] >= latest['wvf_upper'] or latest['wvf'] >= latest['range_high']

            param_details = (
                f"Close={latest['close']:.1f}, SMA200={latest['sma200']:.1f}, "
                f"WVF={latest['wvf']:.2f}%, Upper={latest['wvf_upper']:.2f}%, RangeHigh={latest['range_high']:.2f}%, "
                f"SlopeRate={slope_rate:.6f}"
            )

            if is_uptrend and is_wvf_lit and latest['wvf'] >= 5.0:
                ext_price = min(max(latest['highest_close'] * (1 - latest['wvf_upper'] / 100), latest['highest_close'] * (1 - latest['range_high'] / 100)), latest['highest_close'] * (1 - 5.0 / 100))

                results.append({
                    'チャート': df.tail(60)[['date','open','high','low','close','sma50','sma200','volume']].to_json(orient='records', date_format='iso'),
                    'シグナル日': latest["date"].strftime('%Y-%m-%d'),
                    'コード': ticker,
                    '銘柄': name_map.get(ticker, "-"),
                    '現在値': round(latest['close'], 1),
                    '消灯目安(安値)': round(ext_price, 1),
                    'SMA200': round(latest['sma200'], 1),
                    '乖離率(%)': round((latest['close'] - latest['sma200']) / latest['sma200'] * 100, 2),
                    '200MA傾き率': round(slope_rate, 6),
                    'WVF': round(latest['wvf'], 2),
                    'WVF Upper': round(latest['wvf_upper'], 2),
                    'お気に入り': False
                })
                log(f"✅ [{ticker}] {name_map.get(ticker, '-')} ➔ 点灯！条件クリア ({param_details})")
            else:
                reasons = []
                if not is_uptrend:
                    reasons.append(f"トレンド条件未達 (Close {latest['close']:.1f} <= SMA200 {latest['sma200']:.1f} かつ 200MA傾き率 {slope_rate:.6f} < -0.0001)")
                if not is_wvf_lit:
                    reasons.append(f"WVF未点灯 (WVF {latest['wvf']:.2f}% が Upper {latest['wvf_upper']:.2f}% および RangeHigh {latest['range_high']:.2f}% をともに下回る)")
                if latest['wvf'] < 5.0:
                    reasons.append(f"WVF値が5.0%未満 (WVF: {latest['wvf']:.2f}%)")

                log(f"⏭️ [{ticker}] {name_map.get(ticker, '-')} ➔ スキップ ({param_details}) 理由: {' / '.join(reasons)}")

        except Exception as e:
            log(f"❌ [{ticker}] 判定処理中に例外エラーが発生しました: {e}")
            continue

    progress_bar.empty()
    status_text.empty()
    log(f"🎉 判定処理が完了しました。合致数: {len(results)} 件")
    return pd.DataFrame(results)