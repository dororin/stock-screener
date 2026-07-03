# core/calculator.py
import pandas as pd
import yfinance as yf
import numpy as np  # npを明示的にインポート
import streamlit as st  # キャッシュ処理 st.cache_data のためにインポート
from datetime import datetime, timedelta
from config import settings
from data_access.local_db import load_price_db
from core.database_service import get_jp_session_close_time as _get_jp_session_close_time

@st.cache_data(ttl=3600)
def fetch_proxy_market_value(proxy_ticker: str, start_date: datetime, end_date: datetime) -> pd.Series:
    """
    市場全体の総売買代金の代理（プロキシ）として、1306.T や SPY の
    過去売買代金（Close * Volume）をyfinanceから取得し、1時間キャッシュします。
    """
    try:
        # 余裕を持って前後数日を余分に取得
        df = yf.download(
            proxy_ticker, 
            start=start_date.strftime("%Y-%m-%d"), 
            end=(end_date + timedelta(days=2)).strftime("%Y-%m-%d"), 
            auto_adjust=True, 
            progress=False
        )
        if df.empty:
            return pd.Series(dtype=float)
        
        # カラム名の小文字化（MultiIndex対策含む）
        df.columns = [str(c).lower() if not isinstance(c, tuple) else str(c[0]).lower() for c in df.columns]
        df = df.reset_index()
        date_col = "date" if "date" in df.columns else "datetime"
        df = df.rename(columns={date_col: "date"})
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.set_index("date").sort_index()
        
        # 売買代金（価格 * 出来高）の算出
        return df["close"] * df["volume"]
    except Exception:
        return pd.Series(dtype=float)

def compute_sector_index_from_df(db_df: pd.DataFrame, tickers: list, period_days: int, resample_weekly: bool) -> pd.Series:
    """指定された複数のティッカーの等金額分散投資指数を計算します。"""
    if db_df.empty:
        return pd.Series(dtype=float)
    db_df = db_df.copy()
    db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
    end_date = db_df["date"].max()
    start_date = end_date - timedelta(days=period_days)
    target_df = db_df[(db_df["date"] >= start_date) & (db_df["ticker"].isin(tickers))].copy()
    if target_df.empty:
        return pd.Series(dtype=float)
    
    if resample_weekly:
        target_df = target_df.set_index("date")
        target_df = target_df.groupby("ticker").resample("W-FRI").agg({"close": "last"}).reset_index()
        
    close_pivot = target_df.pivot_table(index="date", columns="ticker", values="close")
    close_pivot = close_pivot.sort_index()
    daily_returns = close_pivot.pct_change()
    sector_return = daily_returns.mean(axis=1)
    index_series = (1 + sector_return).cumprod() * 100
    if len(index_series) > 0:
        index_series.iloc[0] = 100.0
    return index_series

def get_sector_momentum(index_series: pd.Series, days: int = 5) -> float:
    """直近指定日数における合成インデックスの騰落率(%)を計算します。"""
    if len(index_series) < 2:
        return 0.0
    recent = index_series.iloc[-min(days, len(index_series)):]
    if recent.iloc[0] == 0:
        return 0.0
    return float((recent.iloc[-1] / recent.iloc[0] - 1) * 100)

def get_benchmark_data(ticker: str, period_days: int, interval: str) -> pd.Series:
    """
    基準となるベンチマーク指数の累積リターン推移をDBまたはyfinanceから取得します。
    分割等のノイズによる突発的な急落（40%以上）を自動で排除して計算します。
    """
    try:
        is_jp = False
        pure_ticker = str(ticker).strip()
        if pure_ticker.upper().endswith(".T"):
            pure_ticker = pure_ticker[:-2]
            is_jp = True
        elif pure_ticker.isdigit():
            is_jp = True
            
        try:
            db_df = load_price_db(interval, is_jp=is_jp)
        except Exception:
            db_df = pd.DataFrame()

        if not db_df.empty and "ticker" in db_df.columns:
            ticker_db = db_df[db_df["ticker"] == pure_ticker].copy()
            if not ticker_db.empty and len(ticker_db) >= 10:
                ticker_db["date"] = pd.to_datetime(ticker_db["date"]).dt.tz_localize(None)
                end_date = ticker_db["date"].max()
                start_date = end_date - timedelta(days=period_days + 365)
                ticker_db = ticker_db[ticker_db["date"] >= start_date].sort_values("date")
                
                if not ticker_db.empty:
                    close = ticker_db.set_index("date")["close"]
                    ret = close.pct_change()
                    idx = (1 + ret).cumprod() * 100
                    if len(idx) > 0: 
                        idx.iloc[0] = 100.0
                    return idx

        end = datetime.now()
        start = end - timedelta(days=period_days + 365)
        df_raw = yf.download(ticker, start=start.strftime("%Y-%m-%d"), interval=interval, auto_adjust=True, progress=False)
        if df_raw.empty: 
            return pd.Series(dtype=float)
        
        df_raw = df_raw.reset_index()
        df_raw.columns = [str(c).lower() if not isinstance(c, tuple) else str(c[0]).lower() for c in df_raw.columns]
        date_col = "date" if "date" in df_raw.columns else "datetime"
        df_raw = df_raw.rename(columns={date_col: "date"})
        df_raw["date"] = pd.to_datetime(df_raw["date"]).dt.tz_localize(None)
        close = df_raw.set_index("date")["close"].copy()
        
        if len(close) > 1:
            raw_pct = close.pct_change().copy()
            anomaly_mask = raw_pct <= -0.40
            if anomaly_mask.any():
                for idx_loc in raw_pct[anomaly_mask].index:
                    raw_pct.loc[idx_loc] = 0.0
            ret = raw_pct
        else:
            ret = close.pct_change()
            
        idx = (1 + ret).cumprod() * 100
        if len(idx) > 0: 
            idx.iloc[0] = 100.0
        return idx
    except Exception:
        return pd.Series(dtype=float)

def relativize_series(idx_series: pd.Series, bm_series: pd.Series) -> pd.Series:
    """基準指数に対する相対強度(RS)のインデックス推移を計算します。"""
    if bm_series is None or bm_series.empty:
        return idx_series
    bm_aligned = bm_series.reindex(idx_series.index, method='ffill')
    bm_aligned = bm_aligned.bfill()
    if bm_aligned.isna().all() or (bm_aligned == 0).all():
        return idx_series
    rel = idx_series / bm_aligned
    rel = rel / rel.iloc[0] * 100
    return rel

def _generate_intraday_time_grid(trading_dates, freq_minutes: int, is_jp: bool = True) -> pd.DatetimeIndex:
    """
    実在する営業日の集合(trading_dates)を基準に、完全な定刻の取引時間グリッドを生成します（仕様書 4-1）。
    ・日本株: 9:00〜11:30 / 12:30〜大引け（昼休みを除外）
      大引け時刻は日付に応じて自動判定します（2024/11/5のarrowhead4.0稼働に伴う取引時間延伸）:
        - 2024/11/5以降: 15:30
        - それより前     : 15:00
      （yfinanceの取得可能上限の関係で、60m足は当面この境界日をまたぐ期間を含み得るため必要な分岐です）
    ・米国株: 9:30〜16:00（昼休みなし）
    休日を新規に作り出さないよう、実際にDBへ存在する営業日のみを対象とします。
    """
    if len(trading_dates) == 0:
        return pd.DatetimeIndex([])

    freq = f"{freq_minutes}min"
    all_slots = []
    for d in trading_dates:
        day = pd.Timestamp(d).normalize()
        if is_jp:
            close_t = _get_jp_session_close_time(day)
            close_delta = pd.Timedelta(hours=close_t.hour, minutes=close_t.minute)

            morning = pd.date_range(day + pd.Timedelta(hours=9), day + pd.Timedelta(hours=11, minutes=30), freq=freq)
            afternoon = pd.date_range(day + pd.Timedelta(hours=12, minutes=30), day + close_delta, freq=freq)
            # freqの刻み次第では境界時刻(11:30引け前 / 大引け)がステップに乗らず脱落するため、
            # 実データ（大引け固定の合成バー含む）を確実に拾えるよう明示的に含める
            boundary = pd.DatetimeIndex([day + pd.Timedelta(hours=11, minutes=30), day + close_delta])
            all_slots.append(morning)
            all_slots.append(afternoon)
            all_slots.append(boundary)
        else:
            session = pd.date_range(day + pd.Timedelta(hours=9, minutes=30), day + pd.Timedelta(hours=16), freq=freq)
            boundary = pd.DatetimeIndex([day + pd.Timedelta(hours=16)])
            all_slots.append(session)
            all_slots.append(boundary)

    combined = set()
    for s in all_slots:
        combined.update(s)
    return pd.DatetimeIndex(sorted(combined))

def continuize_intraday_grid(close_pivot: pd.DataFrame, volume_pivot: pd.DataFrame, interval: str, is_jp: bool = True) -> tuple:
    """
    短期足（60m/5m/1m）のみを対象に、時間軸を営業時間の完全な定刻グリッドへ連続化します（仕様書 4）。
    ・株価(Close): 直近の既知の価格で前方補完(ffill)
    ・出来高(Volume): 0埋め
    1d（日足）の場合は連続化不要のため、何もせずそのまま返します。
    """
    freq_map = {"60m": 60, "5m": 5, "1m": 1}
    if interval not in freq_map or close_pivot.empty:
        return close_pivot, volume_pivot

    trading_dates = pd.Series(close_pivot.index).dt.normalize().unique()
    full_grid = _generate_intraday_time_grid(trading_dates, freq_map[interval], is_jp=is_jp)
    if len(full_grid) == 0:
        return close_pivot, volume_pivot

    close_continuous = close_pivot.reindex(full_grid).ffill()
    volume_continuous = volume_pivot.reindex(full_grid).fillna(0.0)
    return close_continuous, volume_continuous

def compute_sector_absolute_data(db_df: pd.DataFrame, tickers: list, period_days: int, resample_weekly: bool, interval: str = "1d", is_jp: bool = True) -> tuple:
    """指定された構成群から絶対価格平均、移動平均、WVF、合算売買代金などを一挙算出します。"""
    if db_df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=bool), pd.Series(dtype=float)
    db_df = db_df.copy()
    db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
    end_date = db_df["date"].max()
    fetch_start = end_date - timedelta(days=period_days + 365)
    target_df = db_df[(db_df["date"] >= fetch_start) & (db_df["ticker"].isin(tickers))].copy()
    if target_df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=bool), pd.Series(dtype=float)

    close_pivot = target_df.pivot_table(index="date", columns="ticker", values="close").sort_index()
    volume_pivot = target_df.pivot_table(index="date", columns="ticker", values="volume").sort_index()

    # --- 追加: 短期足の時間軸連続化（仕様書 4） SMA/WVF算出直前のメモリ上でのみ適用 ---
    close_pivot, volume_pivot = continuize_intraday_grid(close_pivot, volume_pivot, interval=interval, is_jp=is_jp)

    if resample_weekly:
        close_pivot = close_pivot.resample("W-FRI").last()
        volume_pivot = volume_pivot.resample("W-FRI").sum()

    sector_abs = close_pivot.mean(axis=1)
    trading_val = (close_pivot * volume_pivot).sum(axis=1)

    sma75  = sector_abs.rolling(window=75).mean()
    sma200 = sector_abs.rolling(window=200).mean()

    highest_close = sector_abs.rolling(window=11).max()
    wvf = (highest_close - sector_abs) / highest_close * 100
    wvf_std   = wvf.rolling(window=20).std(ddof=0)
    wvf_mid   = wvf.rolling(window=20).mean()
    wvf_upper = wvf_mid + (2.0 * wvf_std)
    range_high = wvf.rolling(window=100).max() * 0.85
    is_wvf_lit = (wvf >= wvf_upper) | (wvf >= range_high)

    display_start = end_date - timedelta(days=period_days)
    sector_abs = sector_abs[sector_abs.index >= display_start]
    sma75      = sma75[sma75.index           >= display_start]
    sma200     = sma200[sma200.index         >= display_start]
    is_wvf_lit = is_wvf_lit[is_wvf_lit.index >= display_start]
    trading_val = trading_val[trading_val.index >= display_start]

    return sector_abs, sma75, sma200, is_wvf_lit, trading_val

def compute_macro_cores_from_db(db_df: pd.DataFrame, period_days: int, resample_weekly: bool = False) -> dict:
    """TOPIX-17業種データから、5大コアセクターの累積騰落指標を数学的に合成算出します。"""
    all_etfs = [t for etfs in settings.TOPIX17_ETF_MAPPING.values() for t in etfs]
    etf_df = pd.DataFrame()
    if not db_df.empty:
        db_df = db_df.copy()
        db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
        end_date = db_df["date"].max()
        start_date = end_date - timedelta(days=period_days + 30)
        etf_df = db_df[(db_df["date"] >= start_date) & (db_df["ticker"].isin(all_etfs))].copy()
        
    if etf_df.empty or len(etf_df["ticker"].unique()) < 10:
        symbols = [f"{t}.T" for t in all_etfs]
        end = datetime.now()
        start = end - timedelta(days=period_days + 30)
        try:
            df_raw = yf.download(symbols, start=start.strftime("%Y-%m-%d"), progress=False)
            if not df_raw.empty:
                df_raw = df_raw.xs("Close", axis=1, level=0) if isinstance(df_raw.columns, pd.MultiIndex) else df_raw
                df_raw = df_raw.reset_index()
                rows = []
                for _, row in df_raw.iterrows():
                    dt = row["Date"]
                    for sym in symbols:
                        val = row[sym]
                        if not pd.isna(val):
                            rows.append({"date": dt, "ticker": sym.replace(".T", ""), "close": val})
                etf_df = pd.DataFrame(rows)
        except Exception:
            pass

    if etf_df.empty:
        return {}
        
    close_pivot = etf_df.pivot_table(index="date", columns="ticker", values="close").sort_index()
    if resample_weekly:
        close_pivot = close_pivot.resample("W-FRI").last()
        
    display_start = close_pivot.index.max() - timedelta(days=period_days)
    close_pivot = close_pivot[close_pivot.index >= display_start]
    
    returns_df = close_pivot.pct_change()
    macro_cores = {}
    for core_name, tickers in settings.TOPIX17_ETF_MAPPING.items():
        existing_tickers = [t for t in tickers if t in returns_df.columns]
        if not existing_tickers:
            continue
        core_return = returns_df[existing_tickers].mean(axis=1)
        core_index = (1 + core_return).cumprod() * 100
        if len(core_index) > 0:
            core_index.iloc[0] = 100.0
        macro_cores[core_name] = core_index
        
    return macro_cores

def compute_theme_equal_weighted_return_rate(
    db_df: pd.DataFrame, 
    tickers: list, 
    period_days: int, 
    resample_weekly: bool,
    is_jp: bool = True  # 市場モード（日本株/米国株）を判定するため追加
) -> tuple:
    """
    指定された構成銘柄（等金額投資）の、基準日（表示期間期首）からの
    リターン率（％）を計算します。値がさ株に支配されないよう、期首価格で規格化します。
    また、出来高乖離率（25日中央値）と売買代金シェア（1306/SPYプロキシ基準）から
    4ステージ判定のLWC用出来高データを同時に生成します。
    """
    if db_df.empty or not tickers:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), []
        
    db_df = db_df.copy()
    db_df["date"] = pd.to_datetime(db_df["date"]).dt.tz_localize(None)
    
    # 75SMA, 200SMAや出来高の25日中央値を正しく計算するため、表示期間より365日前からデータを取得
    end_date = db_df["date"].max()
    fetch_start = end_date - timedelta(days=period_days + 365)
    
    target_df = db_df[(db_df["date"] >= fetch_start) & (db_df["ticker"].isin(tickers))].copy()
    if target_df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), []

    # 各銘柄の個別売買代金（val）を計算
    if "volume" in target_df.columns:
        target_df["val"] = target_df["close"] * target_df["volume"]
    else:
        target_df["val"] = 0.0

    close_pivot = target_df.pivot_table(index="date", columns="ticker", values="close").sort_index()
    val_pivot = target_df.pivot_table(index="date", columns="ticker", values="val").sort_index()

    if resample_weekly:
        close_pivot = close_pivot.resample("W-FRI").last().ffill()
        val_pivot = val_pivot.resample("W-FRI").sum().fillna(0)

    if close_pivot.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), []

    # 規格化の基準日（表示開始日）を判定
    display_start = end_date - timedelta(days=period_days)
    
    # 表示期間内の価格データ・売買代金データを抽出
    display_close = close_pivot[close_pivot.index >= display_start]
    display_val = val_pivot[val_pivot.index >= display_start]
    
    if display_close.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), []

    # 基準日時点の株価を100（0%）として規格化（既存ロジック）
    base_prices = display_close.bfill().iloc[0]
    base_prices = base_prices.apply(lambda x: x if (pd.notna(x) and x > 0) else np.nan)

    # 基準日を 0% とするリターン率に変換
    all_normalized = close_pivot.div(base_prices) * 100.0 - 100.0
    all_index_series = all_normalized.mean(axis=1)
    return_rate_series = all_index_series[all_index_series.index >= display_start]
    
    # SMA算出
    sma75 = all_index_series.rolling(window=75, min_periods=1).mean()
    sma200 = all_index_series.rolling(window=200, min_periods=1).mean()

    # 表示期間で再度スライス
    sma75 = sma75[sma75.index >= display_start]
    sma200 = sma200[sma200.index >= display_start]

    # =========================================================================
    # 🔄 4ステージ出来高マトリクスの算出
    # =========================================================================
    # 1. 分母（市場総売買代金プロキシ）の取得
    proxy_ticker = "1306.T" if is_jp else "SPY"
    proxy_m_val = fetch_proxy_market_value(proxy_ticker, fetch_start, end_date)
    
    if resample_weekly and not proxy_m_val.empty:
        proxy_m_val = proxy_m_val.resample("W-FRI").sum()

    # 2. ② 出来高乖離率の平均 (VDR) の計算
    # 各銘柄の、当日を除く過去25期間の売買代金「中央値」を算出して乖離率を出す（0除算回避）
    vdr_df = pd.DataFrame(index=val_pivot.index)
    for col in val_pivot.columns:
        med = val_pivot[col].shift(1).rolling(window=25, min_periods=5).median()
        vdr_df[col] = val_pivot[col] / med.replace(0, np.nan)
    
    theme_vdr = vdr_df.mean(axis=1).fillna(1.0) # テーマ内平均の乖離率

    # 3. ③ 売買代金シェア (VS) の計算
    theme_total_val = val_pivot.sum(axis=1) # テーマ構成銘柄の合計売買代金
    if not proxy_m_val.empty:
        # インデックスを同期
        proxy_m_val_aligned = proxy_m_val.reindex(theme_total_val.index, method="ffill").bfill()
        theme_vs = (theme_total_val / proxy_m_val_aligned.replace(0, np.nan)) * 100.0
    else:
        # 万が一プロキシ取得に失敗した場合は、合計売買代金をおおまかにスケーリングして表示
        theme_vs = theme_total_val / 1e6

    # シェアの20日移動平均 (VS_MA)
    theme_vs_ma = theme_vs.rolling(window=20, min_periods=1).mean()

    # 4. 表示期間でスライスしてマトリクス判定
    display_vdr = theme_vdr[theme_vdr.index >= display_start]
    display_vs = theme_vs[theme_vs.index >= display_start]
    display_vs_ma = theme_vs_ma[theme_vs_ma.index >= display_start]

    # LWC Histogram 用の辞書リスト構築
    lwc_volume_data = []
    for dt, vdr, vs, vs_ma in zip(display_vs.index, display_vdr, display_vs, display_vs_ma):
        if pd.isna(vs):
            continue
        
        # 4ステージ判定ロジック
        if vdr >= 1.5 and vs >= vs_ma:
            color = "rgba(239, 83, 80, 0.85)"      # ステージA: 赤 (初動・ブレイク)
        elif vdr < 1.5 and vs >= vs_ma:
            color = "rgba(38, 166, 154, 0.60)"     # ステージB: 緑 (巡航・新ステージ)
        elif vdr < 1.5 and vs < vs_ma:
            color = "rgba(128, 128, 128, 0.25)"    # ステージC: 灰 (冷え込み・手仕舞い)
        else: # vdr >= 1.5 and vs < vs_ma
            color = "rgba(255, 167, 38, 0.50)"     # ステージD: 黄 (地合い・一時的ノイズ)

        lwc_volume_data.append({
            "time": str(dt)[:10],
            "value": float(round(vs, 4)),
            "color": color
        })

    return return_rate_series, sma75, sma200, lwc_volume_data