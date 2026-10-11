# core/jp_price_corrector.py

import os
import re
import sys
import time
import logging
import traceback
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import yfinance as yf
from config import settings
from data_access.local_db import (
    load_price_db, 
    save_price_db, 
    clear_local_parquet_cache
)
from data_access.drive_api import (
    download_from_drive_api,
    upload_to_drive_api,
    get_or_create_drive_folder,
    get_drive_service
)

# ══════════════════════════════════════════════════════════════════════════
# 🪵 デバッグロギング設定
# ══════════════════════════════════════════════════════════════════════════
logger = logging.getLogger("jp_price_corrector")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    _formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-7s] %(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setLevel(logging.DEBUG)
    _console_handler.setFormatter(_formatter)
    logger.addHandler(_console_handler)

    try:
        _log_dir = getattr(settings, "WORK_DIR", ".")
        os.makedirs(_log_dir, exist_ok=True)
        _log_path = os.path.join(_log_dir, "jp_price_corrector_debug.log")
        _file_handler = logging.FileHandler(_log_path, encoding="utf-8")
        _file_handler.setLevel(logging.DEBUG)
        _file_handler.setFormatter(_formatter)
        logger.addHandler(_file_handler)
    except Exception as _e:
        logger.addHandler(logging.NullHandler())
        print(f"⚠️ [jp_price_corrector] デバッグログファイルの作成に失敗しました: {_e}")

logger.propagate = False


def _emit(status_callback, msg: str, level: str = "info", exc: bool = False):
    level = level.lower()
    log_fn = {
        "debug": logger.debug,
        "info": logger.info,
        "warning": logger.warning,
        "error": logger.error,
        "critical": logger.critical,
    }.get(level, logger.info)

    if exc:
        log_fn(f"{msg}\n{traceback.format_exc()}")
    else:
        log_fn(msg)

    print(f"[JP_PATCH_ENGINE/{level.upper()}] {msg}", flush=True)

    if status_callback and level != "debug":
        try:
            status_callback(msg)
        except Exception:
            pass


VERIFY_DEVIATION_THRESHOLD_PCT = 2.0
ADJCLOSE_JUMP_THRESHOLD_PCT = 3.0
INTRADAY_TIMEFRAMES = ["60m", "5m", "1m"]


def get_dynamic_threshold(s: float) -> tuple:
    if s >= 2.0:
        result = (s * 0.85, s * 1.15)
    elif s >= 1.30:
        result = (s * 0.85, s * 1.15)
    else:
        result = (1.03, s * 1.15)
    logger.debug(f"get_dynamic_threshold: s={s} -> min_R={result[0]:.4f}, max_R={result[1]:.4f}")
    return result


def _find_cliff_reverse(rows: list, min_R: float, max_R: float, max_gap_days: float = 15) -> int:
    logger.debug(f"_find_cliff_reverse: 走査開始 rows={len(rows)}件, min_R={min_R:.4f}, max_R={max_R:.4f}, max_gap_days={max_gap_days}")
    checked = 0
    skipped_gap = 0
    skipped_nan = 0
    for i in reversed(range(1, len(rows))):
        row_T = rows[i]
        row_prev = rows[i - 1]
        checked += 1

        gap = row_T["dt"] - row_prev["dt"]
        gap_days = gap.total_seconds() / 86400.0
        if gap_days > max_gap_days:
            skipped_gap += 1
            continue

        close_T = row_T["close"]
        close_prev = row_prev["close"]
        if pd.isna(close_T) or pd.isna(close_prev) or close_T <= 0 or close_prev <= 0:
            skipped_nan += 1
            continue

        R_price = close_prev / close_T
        if min_R <= R_price <= max_R:
            logger.debug(
                f"_find_cliff_reverse: 段差検出 idx={i}, dt={row_T['dt']}, "
                f"close_prev={close_prev}, close_T={close_T}, R={R_price:.4f} "
                f"(走査{checked}件中 / gap除外{skipped_gap} / nan除外{skipped_nan})"
            )
            return i

    logger.debug(
        f"_find_cliff_reverse: 段差検出なし（走査{checked}件 / gap除外{skipped_gap} / nan除外{skipped_nan}）"
    )
    return -1


def _extract_close_series(df: pd.DataFrame):
    if df is None or df.empty:
        return None

    close_data = None
    if isinstance(df.columns, pd.MultiIndex):
        if "Close" in df.columns.get_level_values(0):
            close_data = df["Close"]
    elif "Close" in df.columns:
        close_data = df["Close"]

    if close_data is None:
        return None

    if isinstance(close_data, pd.DataFrame):
        close_data = close_data.iloc[:, 0]

    idx = pd.to_datetime(close_data.index)
    try:
        if idx.tz is not None:
            idx = idx.tz_convert("Asia/Tokyo").tz_localize(None)
        else:
            idx = idx.tz_localize(None)
    except Exception:
        try:
            idx = idx.tz_localize(None)
        except Exception:
            pass

    close_data = close_data.copy()
    close_data.index = idx
    return close_data.sort_index()


def _extract_close_on_date(df: pd.DataFrame, date_str: str):
    s = _extract_close_series(df)
    if s is None:
        return None
    mask = s.index.strftime("%Y-%m-%d") == date_str
    matched = s[mask].dropna()
    matched = matched[matched > 0]
    if matched.empty:
        return None
    return float(matched.iloc[0])


def _extract_close_at_timestamp(df: pd.DataFrame, target_ts, tolerance_minutes: int = 90):
    s = _extract_close_series(df)
    if s is None or s.empty:
        return None
    s = s[s > 0].dropna()
    if s.empty:
        return None

    target_dt = pd.to_datetime(target_ts)
    if getattr(target_dt, "tz", None) is not None:
        target_dt = target_dt.tz_convert("Asia/Tokyo").tz_localize(None)

    diff_minutes = np.abs((s.index - target_dt).total_seconds()) / 60.0
    min_idx = int(np.argmin(diff_minutes))

    if diff_minutes[min_idx] > tolerance_minutes:
        return None
    return float(s.iloc[min_idx])


def _find_floor_value(s: pd.Series, target_ts):
    if s is None or s.empty:
        return None, None
    target_ts = pd.to_datetime(target_ts)
    pos = s.index.searchsorted(target_ts, side="right") - 1
    if pos < 0:
        return None, None
    return s.index[pos], float(s.iloc[pos])


def _find_ceil_value(s: pd.Series, target_ts):
    if s is None or s.empty:
        return None, None
    target_ts = pd.to_datetime(target_ts)
    pos = s.index.searchsorted(target_ts, side="left")
    if pos >= len(s):
        return None, None
    return s.index[pos], float(s.iloc[pos])


def _verify_against_yfinance_pure_close(ticker: str, target_dt, detected_close: float, cache: dict,
                                         interval: str = "1d", s_val: float = 1.0) -> dict:
    target_ts = pd.to_datetime(target_dt)
    date_str = target_ts.strftime("%Y-%m-%d")
    cache_key = f"{ticker}_{interval}_{target_ts.strftime('%Y-%m-%d_%H%M')}"

    if cache_key not in cache:
        logger.debug(f"_verify_against_yfinance_pure_close: キャッシュ未ヒット。yfinanceへ問い合わせ中 ({cache_key})")
        yf_close = None
        is_daily_fallback = (interval == "1d")
        try:
            if interval != "1d":
                intraday_start = (target_ts - timedelta(days=2)).strftime("%Y-%m-%d")
                intraday_end = (target_ts + timedelta(days=2)).strftime("%Y-%m-%d")
                df_intraday = yf.download(
                    f"{ticker}.T",
                    start=intraday_start,
                    end=intraday_end,
                    interval=interval,
                    auto_adjust=False,
                    actions=False,
                    progress=False,
                    timeout=15
                )
                yf_close = _extract_close_at_timestamp(df_intraday, target_ts)
                if yf_close is None:
                    is_daily_fallback = True

            if yf_close is None:
                df_daily = yf.download(
                    f"{ticker}.T",
                    start=(target_ts - timedelta(days=3)).strftime("%Y-%m-%d"),
                    end=(target_ts + timedelta(days=3)).strftime("%Y-%m-%d"),
                    auto_adjust=False,
                    actions=False,
                    progress=False,
                    timeout=15
                )
                yf_close = _extract_close_on_date(df_daily, date_str)

            cache[cache_key] = {"yf_close": yf_close, "is_daily_fallback": is_daily_fallback}
        except Exception as e:
            logger.warning(f"_verify_against_yfinance_pure_close: yfinance取得失敗 ticker={ticker}: {e}", exc_info=True)
            cache[cache_key] = {"yf_close": None, "is_daily_fallback": is_daily_fallback}

    cached = cache[cache_key]
    yf_close = cached["yf_close"]
    is_daily_fallback = cached["is_daily_fallback"]

    if yf_close is None or pd.isna(yf_close) or yf_close <= 0 or detected_close is None or pd.isna(detected_close):
        return {"yf_close": None, "deviation_pct": None, "passed": False, "is_daily_fallback": is_daily_fallback}

    dev_direct = abs(detected_close - yf_close) / yf_close * 100.0
    dev_scaled = abs(detected_close * s_val - yf_close) / yf_close * 100.0 if s_val > 0 else 999.0

    if dev_direct <= VERIFY_DEVIATION_THRESHOLD_PCT:
        deviation_pct = dev_direct
        passed = True
    elif dev_scaled <= VERIFY_DEVIATION_THRESHOLD_PCT:
        deviation_pct = dev_scaled
        passed = True
    else:
        deviation_pct = min(dev_direct, dev_scaled)
        passed = False

    return {
        "yf_close": round(yf_close, 2),
        "deviation_pct": round(deviation_pct, 3),
        "passed": passed,
        "is_daily_fallback": is_daily_fallback
    }


ADJ_VERIFY_FALLBACK_INTERVALS = ["1m", "5m", "60m", "1d"]


def _verify_split_via_local_cliff_adjclose(ticker: str, cliff_dt, actual_dt,
                                            before_close: float, after_close: float,
                                            cache: dict,
                                            match_tolerance_pct: float = ADJCLOSE_JUMP_THRESHOLD_PCT) -> dict:
    cliff_ts = pd.to_datetime(cliff_dt)
    actual_ts = pd.to_datetime(actual_dt)
    cache_key = f"adjpair_{ticker}_{cliff_ts.strftime('%Y-%m-%d_%H%M')}_{actual_ts.strftime('%Y-%m-%d_%H%M')}"

    if cache_key in cache:
        logger.debug(f"_verify_split_via_local_cliff_adjclose: キャッシュヒット ({cache_key})")
        return cache[cache_key]

    logger.debug(f"_verify_split_via_local_cliff_adjclose: キャッシュ未ヒット。フォールバック探索開始 ({cache_key})")
    result = {"yf_adj_ratio": None, "local_raw_ratio": None, "ratio_gap_pct": None,
              "passed": False, "used_interval": None}

    local_raw_ratio = None
    if before_close and not pd.isna(before_close) and before_close > 0 and after_close is not None and not pd.isna(after_close):
        local_raw_ratio = float(after_close) / float(before_close)

    if local_raw_ratio is None:
        cache[cache_key] = result
        return result

    for interval in ADJ_VERIFY_FALLBACK_INTERVALS:
        if interval == "1d" and cliff_ts.normalize() == actual_ts.normalize():
            logger.debug(
                f"_verify_split_via_local_cliff_adjclose: cliff_dtとactual_dtが同日内のため"
                f"日足フォールバックを断念（判定不能） cliff_ts={cliff_ts}, actual_ts={actual_ts}"
            )
            break

        try:
            buffer_days = 5 if interval != "1d" else 15
            start = (cliff_ts - timedelta(days=buffer_days)).strftime("%Y-%m-%d")
            end = (actual_ts + timedelta(days=buffer_days)).strftime("%Y-%m-%d")
            download_kwargs = {} if interval == "1d" else {"interval": interval}
            df_adj = yf.download(
                f"{ticker}.T", start=start, end=end,
                auto_adjust=True, actions=False, progress=False, timeout=15,
                **download_kwargs
            )
            s_adj = _extract_close_series(df_adj)
        except Exception as e:
            logger.warning(
                f"_verify_split_via_local_cliff_adjclose: interval={interval} 取得失敗 ticker={ticker}: {e}",
                exc_info=True
            )
            s_adj = None

        if s_adj is None or s_adj.empty:
            logger.debug(f"_verify_split_via_local_cliff_adjclose: interval={interval} でデータ取得不可。次の足へフォールバック")
            continue

        _, adj_before = _find_floor_value(s_adj, cliff_ts)
        _, adj_after = _find_ceil_value(s_adj, actual_ts)

        if adj_before is None or adj_after is None or adj_before <= 0:
            logger.debug(f"_verify_split_via_local_cliff_adjclose: interval={interval} でfloor/ceilが見つからず。次の足へフォールバック")
            continue

        yf_adj_ratio = adj_after / adj_before
        local_change_pct = (1.0 - local_raw_ratio) * 100.0
        yf_change_pct = (1.0 - yf_adj_ratio) * 100.0
        ratio_gap_pct = abs(local_change_pct - yf_change_pct)
        passed = ratio_gap_pct >= match_tolerance_pct
        result = {
            "yf_adj_ratio": round(yf_adj_ratio, 4),
            "local_raw_ratio": round(local_raw_ratio, 4),
            "ratio_gap_pct": round(ratio_gap_pct, 3),
            "passed": passed,
            "used_interval": interval
        }
        logger.debug(f"_verify_split_via_local_cliff_adjclose: interval={interval} で判定成功 -> {result}")
        break

    cache[cache_key] = result
    return result


def _scan_intraday_cliff(ticker: str, interval: str, ex_date, actual_day_dt, s_val: float,
                          daily_after_close: float, yf_cache: dict, status_callback=None,
                          force_reload: bool = False) -> dict:
    """下位時間足（60m/5m/1m）の段差オートフォーカス走査。"""
    logger.debug(f"_scan_intraday_cliff: 開始 ticker={ticker}, interval={interval}, ex_date={ex_date}, actual_day_dt={actual_day_dt}, s_val={s_val}")

    ex_date_ts = pd.to_datetime(ex_date)
    lookback_bdays = settings.SPLIT_SCAN_LOOKBACK_BDAYS.get(interval, 30)
    lookback_start = (ex_date_ts - pd.tseries.offsets.BDay(lookback_bdays)).strftime("%Y-%m-%d")
    lookback_end = pd.Timestamp.now().strftime("%Y-%m-%d")

    try:
        df_tf = load_price_db(
            interval=interval,
            is_jp=True,
            is_raw=False,
            columns=["date", "ticker", "close", "patched_multiplier"],
            filters=[("ticker", "==", ticker), ("date", ">=", lookback_start), ("date", "<=", lookback_end)],
            force_reload=force_reload
        )
    except Exception as e:
        _emit(status_callback, f"  ⚠️ [{interval}] ピンポイント投影ロード失敗 (ticker={ticker}): {e}", level="warning", exc=True)
        df_tf = pd.DataFrame()

    if df_tf is None or df_tf.empty:
        return {"mode": "メタデータのみ更新", "actual_dt": None, "cliff_dt": None,
                "before_close": np.nan, "after_close": np.nan, "verify": {"yf_close": None, "deviation_pct": None, "passed": True},
                "status": "[データ無し・スキップ]"}

    if "patched_multiplier" not in df_tf.columns:
        df_tf["patched_multiplier"] = 1.0

    df_tf["date_dt"] = pd.to_datetime(df_tf["date"])
    try:
        df_tf["date_dt"] = df_tf["date_dt"].dt.tz_localize(None)
    except Exception:
        pass
    df_tf = df_tf.sort_values("date_dt").reset_index(drop=True)

    tail_limits = {"1m": 25000, "5m": 8000, "60m": 2500}
    max_tail = tail_limits.get(interval, 25000)
    df_window = df_tf.tail(max_tail).reset_index(drop=True)

    if len(df_window) < 2:
        return {"mode": "メタデータのみ更新", "actual_dt": None, "cliff_dt": None,
                "before_close": np.nan, "after_close": np.nan, "verify": {"yf_close": None, "deviation_pct": None, "passed": True},
                "status": "[データ不足・スキップ]"}

    min_R, max_R = get_dynamic_threshold(s_val)
    rows = [{"dt": r["date_dt"], "close": r["close"]} for _, r in df_window.iterrows()]
    
    idx = _find_cliff_reverse(rows, min_R, max_R, max_gap_days=15)

    if idx == -1:
        return {"mode": "メタデータのみ更新", "actual_dt": None, "cliff_dt": None,
                "before_close": np.nan, "after_close": np.nan, "verify": {"yf_close": None, "deviation_pct": None, "passed": True},
                "status": "[正常分割（段差未検出＝調整済み）]"}

    row_T = df_window.iloc[idx]
    row_prev = df_window.iloc[idx - 1]
    actual_dt = row_T["date_dt"]
    cliff_dt = row_prev["date_dt"]
    before_close = row_prev["close"]
    after_close = row_T["close"]

    # 重複判定
    mult_T = row_T["patched_multiplier"]
    mult_prev = row_prev["patched_multiplier"]
    if mult_T > 0 and abs((mult_prev / mult_T) - 1.0) > 0.15:
        return {"mode": None, "actual_dt": actual_dt, "cliff_dt": cliff_dt,
                "before_close": before_close, "after_close": after_close,
                "verify": {"yf_close": None, "deviation_pct": None, "passed": True},
                "status": "[処理済み・スキップ]"}

    verify = _verify_against_yfinance_pure_close(ticker, actual_dt, after_close, yf_cache, interval=interval, s_val=s_val)
    adjfactor = _verify_split_via_local_cliff_adjclose(ticker, cliff_dt, actual_dt, before_close, after_close, yf_cache)
    verify["split_explain_gap_pct"] = adjfactor["ratio_gap_pct"]
    verify["split_explain_gap_passed"] = adjfactor["passed"]

    pure_passed = verify["passed"]
    gap_passed = adjfactor["passed"]
    verify["passed"] = bool(pure_passed) and bool(gap_passed)

    if pure_passed and gap_passed:
        status_label = "[パッチ対象・分割整合OK（日足代用突合）]" if verify.get("is_daily_fallback") else "[パッチ対象・分割整合OK]"
    elif not pure_passed:
        status_label = "[要確認：生値不一致（暴落またはノイズ疑い）]"
    else:
        status_label = "[要確認：分割説明ギャップ乖離疑い]"

    logger.debug(f"_scan_intraday_cliff: {interval} 判定完了 ticker={ticker}, status={status_label}, verify={verify}")

    return {
        "mode": "要パッチ",
        "actual_dt": actual_dt,
        "cliff_dt": cliff_dt,
        "before_close": before_close,
        "after_close": after_close,
        "verify": verify,
        "status": status_label
    }


def scan_jp_anomalies_with_yfinance(status_callback=None, force_reload: bool = False) -> pd.DataFrame:
    """
    日本株の株式分割段差スキャンを実行します。
    force_reload=True の場合、Google Driveから最新の日足データを強制ダウンロードして検査します。
    """
    def log(msg, level="info", exc=False):
        _emit(status_callback, msg, level=level, exc=exc)

    run_started_at = time.time()
    log("📊 日本株本番日足データ (price_jp_1d) をロード中...", level="info")

    try:
        df_1d = load_price_db(
            interval="1d",
            is_jp=True,
            is_raw=False,
            columns=["date", "ticker", "close", "patched_multiplier"],
            force_reload=force_reload
        )
    except Exception as e:
        log(f"❌ 日足データのロードに失敗しました: {e}", level="error", exc=True)
        return pd.DataFrame()

    if df_1d.empty:
        log("⚠️ 日本株日足データが見つかりません。先にデータ収集・マージを行ってください。", level="warning")
        return pd.DataFrame()

    if "patched_multiplier" not in df_1d.columns:
        df_1d["patched_multiplier"] = 1.0

    df_1d["date_dt"] = pd.to_datetime(df_1d["date"]).dt.tz_localize(None)
    tickers = df_1d["ticker"].unique().tolist()

    target_start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    log(f"🔎 データベース内から {len(tickers)} 銘柄を検出し、直近3ヶ月間（{target_start_date}以降）の公式分割イベントを高速フィルタ中...", level="info")

    batch_size = 100
    ticker_batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]
    event_tickers_with_splits = {}

    for b_idx, batch in enumerate(ticker_batches):
        batch_tickers_T = [f"{t}.T" for t in batch]
        try:
            df_download = yf.download(
                batch_tickers_T,
                start=target_start_date,
                interval="1d",
                auto_adjust=False,
                actions=True,
                progress=False,
                threads=True,
                timeout=30
            )

            if df_download.empty:
                continue

            df_splits = pd.DataFrame()
            if isinstance(df_download.columns, pd.MultiIndex):
                if "Stock Splits" in df_download.columns.get_level_values(0):
                    df_splits = df_download["Stock Splits"]
            else:
                if "Stock Splits" in df_download.columns:
                    df_splits = pd.DataFrame(df_download["Stock Splits"])
                    df_splits.columns = batch_tickers_T[:1]

            if df_splits.empty:
                continue

            for ticker_with_T in df_splits.columns:
                splits_series_raw = df_splits[ticker_with_T]
                splits_series = splits_series_raw[(splits_series_raw > 0) & (splits_series_raw != 1.0)].dropna()
                if not splits_series.empty:
                    ticker = ticker_with_T.split(".")[0]
                    event_tickers_with_splits[ticker] = splits_series.to_dict()

        except Exception as ex:
            log(f"  ⚠️ バッチ [{b_idx + 1}] 処理中に例外検出 (スキップ): {ex}", level="warning", exc=True)
            continue

    if not event_tickers_with_splits:
        log("✅ 直近3ヶ月間に株式分割イベントが検知された銘柄はありません。スキャンを正常終了します。", level="info")
        return pd.DataFrame()

    log(f"🎯 公式分割イベントを検出。対象 {len(event_tickers_with_splits)} 銘柄に対して、詳細なインメモリ遡及走査を開始します。", level="info")

    results = []
    yf_cache = {}

    for ticker_idx, (ticker, splits_dict) in enumerate(event_tickers_with_splits.items()):
        df_ticker = df_1d[df_1d["ticker"] == ticker].sort_values("date_dt").reset_index(drop=True)
        if df_ticker.empty or len(df_ticker) < 2:
            continue

        for ex_date, s_val in splits_dict.items():
            if pd.isna(s_val) or s_val <= 0.0 or s_val == 1.0:
                continue

            planned_dt = pd.to_datetime(ex_date).tz_localize(None)
            planned_date_str = planned_dt.strftime("%Y-%m-%d")

            ex_date_idx_list = df_ticker[df_ticker["date_dt"] <= planned_dt].index.tolist()
            if not ex_date_idx_list:
                continue

            ex_date_idx = ex_date_idx_list[-1]
            lookback_start_idx = max(0, ex_date_idx - settings.SPLIT_SCAN_LOOKBACK_BDAYS.get("1d", 30))
            df_window = df_ticker.iloc[lookback_start_idx: ex_date_idx + 1].copy()

            if len(df_window) < 2:
                continue

            min_R, max_R = get_dynamic_threshold(s_val)
            rows = [{"dt": r["date_dt"], "close": r["close"]} for _, r in df_window.iterrows()]
            rel_idx = _find_cliff_reverse(rows, min_R, max_R, max_gap_days=15)
            detected_idx = df_window.index[rel_idx] if rel_idx != -1 else -1

            M = 1.0 / s_val

            # パッチ適用済みチェック
            check_idx = detected_idx if detected_idx != -1 else ex_date_idx
            check_prev_idx = max(0, check_idx - 1)
            mult_T = df_ticker.loc[check_idx, "patched_multiplier"]
            mult_prev = df_ticker.loc[check_prev_idx, "patched_multiplier"]
            if mult_T > 0 and abs((mult_prev / mult_T) - 1.0) > 0.15:
                continue

            mode = None
            actual_dt = planned_dt
            cliff_dt = planned_dt
            close_T_val = np.nan
            close_prev_val = np.nan
            verify = {"yf_close": None, "deviation_pct": None, "passed": True}
            status_label = "[正常分割]"

            if detected_idx != -1:
                row_T = df_ticker.loc[detected_idx]
                row_prev = df_ticker.loc[detected_idx - 1]

                actual_dt = row_T["date_dt"]
                cliff_dt = row_prev["date_dt"]
                close_T_val = row_T["close"]
                close_prev_val = row_prev["close"]

                verify = _verify_against_yfinance_pure_close(ticker, actual_dt, close_T_val, yf_cache, interval="1d")
                mode = "要パッチ"

                adjfactor = _verify_split_via_local_cliff_adjclose(ticker, cliff_dt, actual_dt, close_prev_val, close_T_val, yf_cache)
                verify["split_explain_gap_pct"] = adjfactor["ratio_gap_pct"]
                verify["split_explain_gap_passed"] = adjfactor["passed"]

                pure_passed = verify["passed"]
                gap_passed = adjfactor["passed"]
                verify["passed"] = bool(pure_passed) and bool(gap_passed)

                if pure_passed and gap_passed:
                    status_label = "[パッチ対象・分割整合OK]"
                elif not pure_passed:
                    status_label = "[要確認：生値不一致（暴落またはノイズ疑い）]"
                else:
                    status_label = "[要確認：分割説明ギャップ乖離疑い]"
            else:
                mode = "メタデータのみ更新"
                actual_dt = planned_dt
                cliff_dt = planned_dt

                row_T = df_ticker.iloc[ex_date_idx]
                close_T_val = row_T["close"]
                if ex_date_idx > 0:
                    close_prev_val = df_ticker.iloc[ex_date_idx - 1]["close"]

                verify = _verify_against_yfinance_pure_close(ticker, actual_dt, close_T_val, yf_cache, interval="1d")
                status_label = "[正常分割]" if verify["passed"] else "[要確認：生値不一致]"

            if mode:
                results.append({
                    "ticker": ticker,
                    "interval": "1d",
                    "ex_date": planned_date_str,
                    "actual_date": actual_dt.strftime("%Y-%m-%d %H:%M:%S") if actual_dt is not None else None,
                    "cliff_date": cliff_dt.strftime("%Y-%m-%d") if cliff_dt is not None else None,
                    "splits": s_val,
                    "mode": mode,
                    "multiplier": M,
                    "before_close": round(close_prev_val, 2) if not pd.isna(close_prev_val) else 0.0,
                    "after_close": round(close_T_val, 2) if not pd.isna(close_T_val) else 0.0,
                    "yf_close": verify["yf_close"],
                    "deviation_pct": verify["deviation_pct"],
                    "split_explain_gap_pct": verify.get("split_explain_gap_pct"),
                    "status": status_label,
                    "is_selectable": True,
                })

                if detected_idx != -1:
                    for tf in INTRADAY_TIMEFRAMES:
                        log(f"  🔬 [{ticker}] {tf} 足の段差オートフォーカス走査中...", level="info")
                        try:
                            tf_result = _scan_intraday_cliff(
                                ticker=ticker,
                                interval=tf,
                                ex_date=planned_dt,
                                actual_day_dt=actual_dt,
                                s_val=s_val,
                                daily_after_close=close_T_val,
                                yf_cache=yf_cache,
                                status_callback=status_callback,
                                force_reload=force_reload
                            )
                        except Exception as e:
                            log(f"  ❌ [{ticker}] {tf} 足のオートフォーカス走査中に予期しない例外: {e}", level="error", exc=True)
                            continue

                        if tf_result["mode"] is None:
                            continue

                        tf_actual_dt = tf_result["actual_dt"]
                        tf_cliff_dt = tf_result["cliff_dt"]
                        tf_verify = tf_result["verify"]

                        if tf_result["mode"] == "メタデータのみ更新" and tf_actual_dt is None:
                            tf_cliff_dt = pd.to_datetime(f"{actual_dt.strftime('%Y-%m-%d')} 09:00:00")
                            tf_actual_dt = tf_cliff_dt

                        results.append({
                            "ticker": ticker,
                            "interval": tf,
                            "ex_date": planned_date_str,
                            "actual_date": tf_actual_dt.strftime("%Y-%m-%d %H:%M:%S") if tf_actual_dt is not None else None,
                            "cliff_date": tf_cliff_dt.strftime("%Y-%m-%d %H:%M:%S") if tf_cliff_dt is not None else None,
                            "splits": s_val,
                            "mode": tf_result["mode"],
                            "multiplier": M,
                            "before_close": round(tf_result["before_close"], 2) if not pd.isna(tf_result["before_close"]) else 0.0,
                            "after_close": round(tf_result["after_close"], 2) if not pd.isna(tf_result["after_close"]) else 0.0,
                            "yf_close": tf_verify.get("yf_close"),
                            "deviation_pct": tf_verify.get("deviation_pct"),
                            "split_explain_gap_pct": tf_verify.get("split_explain_gap_pct"),
                            "status": tf_result["status"],
                            "is_selectable": True,
                        })

            time.sleep(1.0)

    elapsed = time.time() - run_started_at
    log(f"🎉 日本株段差スキャン完了。自動検出された修正候補: {len(results)} 件（全時間足合算）", level="info")
    return pd.DataFrame(results)


def apply_jp_patch_to_all_timeframes(ticker: str, patch_rows: list, status_callback=None) -> dict:
    """日本株の全時間足（1d, 60m, 5m, 1m）の本番Parquetに対し、安全かつ可逆的に遡及一括調整パッチを適用します。"""
    def log(msg, level="info", exc=False):
        _emit(status_callback, msg, level=level, exc=exc)

    logger.debug(f"apply_jp_patch_to_all_timeframes: 開始 ticker={ticker}, patch_rows件数={len(patch_rows)}")
    logger.debug(f"apply_jp_patch_to_all_timeframes: patch_rows内容={patch_rows}")

    patches_by_interval = {}
    for row in patch_rows:
        multiplier = row.get("multiplier")
        if multiplier is None or multiplier <= 0.0:
            logger.error(f"apply_jp_patch_to_all_timeframes: 不正な倍率を検出し中断します。row={row}")
            raise ValueError(f"⚠️ [安全ロック作動] 不適切な調整倍率 ({multiplier}) が検出されました。倍率は必ず0より大きい必要があります。")
        patches_by_interval[row["interval"]] = row

    timeframes = ["1d", "60m", "5m", "1m"]
    results = {}
    repair_log_rows = []
    executed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_started_at = time.time()

    for interval in timeframes:
        patch = patches_by_interval.get(interval)
        if not patch:
            logger.debug(f"interval={interval}: 承認済みパッチが無いためスキップ")
            continue

        log(f"⏱️ 【日本株 {interval}】パッチ適用処理中...", level="info")

        multiplier = patch["multiplier"]
        mode = patch["mode"]
        cliff_date = patch.get("cliff_date")
        actual_date = patch.get("actual_date")

        # 💡【境界日時設定】新スケール開始日時（actual_date）を最優先境界とする
        if actual_date:
            boundary_dt = pd.to_datetime(actual_date)
        elif cliff_date:
            boundary_dt = pd.to_datetime(cliff_date)
        else:
            logger.error(f"interval={interval}: 日時情報が存在しません。スキップします。")
            continue

        boundary_year = boundary_dt.year
        boundary_month = boundary_dt.month
        logger.debug(f"interval={interval}: boundary_dt={boundary_dt}, multiplier={multiplier}, mode={mode}")

        try:
            tf_folder_id = get_or_create_drive_folder(interval, settings.FOLDER_ID)
            logger.debug(f"interval={interval}: Driveフォルダ取得 tf_folder_id={tf_folder_id}")
            service = get_drive_service()
            if not service:
                raise ConnectionError("Google Driveサービスにアクセスできません。")

            query = f"'{tf_folder_id}' in parents and name contains 'price_jp_' and name contains '.parquet' and not name contains '_diff_' and trashed=false"
            logger.debug(f"interval={interval}: Drive files().list query={query}")
            drive_results = service.files().list(q=query, fields="files(id, name)").execute()
            base_files = drive_results.get('files', [])
            logger.debug(f"interval={interval}: 対象候補ファイル {len(base_files)}件 -> {[f['name'] for f in base_files]}")

            applied_files_count = 0

            for b_file in base_files:
                b_name = b_file['name']

                should_process = False
                all_records = False
                conditional_records = False

                if interval == "1d":
                    if b_name == "price_jp_1d.parquet":
                        should_process = True
                        conditional_records = True

                elif interval == "60m":
                    m_y = re.search(r'price_jp_60m_(\d{4})\.parquet', b_name)
                    if m_y:
                        file_year = int(m_y.group(1))
                        if file_year < boundary_year:
                            should_process = True
                            all_records = True
                        elif file_year == boundary_year:
                            should_process = True
                            conditional_records = True

                else:  # 5m or 1m
                    m_ym = re.search(r'price_jp_(?:5m|1m)_(\d{4})_(\d{2})\.parquet', b_name)
                    if m_ym:
                        file_year = int(m_ym.group(1))
                        file_month = int(m_ym.group(2))

                        if (file_year < boundary_year) or (file_year == boundary_year and file_month < boundary_month):
                            should_process = True
                            all_records = True
                        elif file_year == boundary_year and file_month == boundary_month:
                            should_process = True
                            conditional_records = True

                if not should_process:
                    logger.debug(f"  file={b_name}: 対象外のためスキップ")
                    continue

                logger.debug(f"  file={b_name}: 処理対象 (all_records={all_records}, conditional_records={conditional_records})")

                local_path = os.path.join(settings.WORK_DIR, b_name)
                t_dl = time.time()
                dl_success = download_from_drive_api(b_name, local_path, parent_id=tf_folder_id)
                logger.debug(f"  file={b_name}: ダウンロード{'成功' if dl_success else '失敗'} ({time.time() - t_dl:.2f}秒)")
                if not dl_success or not os.path.exists(local_path):
                    log(f"  ⚠️ ファイル {b_name} のダウンロードに失敗しました。", level="warning")
                    continue

                try:
                    import pyarrow as pa
                    import pyarrow.parquet as pq
                    df = pd.read_parquet(local_path)
                    if df.empty:
                        logger.debug(f"  file={b_name}: 読み込んだデータフレームが空のためスキップ")
                        continue

                    original_row_count = len(df)
                    logger.debug(f"  file={b_name}: 読み込み完了 {original_row_count}行, columns={list(df.columns)}")

                    if "patched_multiplier" not in df.columns:
                        df["patched_multiplier"] = 1.0

                    df["date_dt"] = pd.to_datetime(df["date"])
                    try:
                        df["date_dt"] = df["date_dt"].dt.tz_localize(None)
                    except Exception:
                        pass

                    # 💡【重要修正】actual_date があれば `< boundary_dt`、cliff_date だけなら `<= boundary_dt` を適用して直前バーを確実に含める
                    if all_records:
                        mask = (df["ticker"] == ticker)
                    elif conditional_records:
                        if actual_date:
                            mask = (df["ticker"] == ticker) & (df["date_dt"] < boundary_dt)
                        else:
                            mask = (df["ticker"] == ticker) & (df["date_dt"] <= boundary_dt)
                    else:
                        mask = pd.Series([False] * len(df))

                    n_affected_preview = int(mask.sum())
                    logger.debug(f"  file={b_name}: マスク対象レコード数={n_affected_preview}")

                    if mask.any():
                        n_affected = int(mask.sum())

                        if mode == "要パッチ":
                            price_cols = [c for c in ["open", "high", "low", "close"] if c in df.columns]
                            logger.debug(f"  file={b_name}: 価格列を multiplier={multiplier} で書き換え中 (対象列={price_cols})")
                            for col in price_cols:
                                df.loc[mask, col] = df.loc[mask, col] * multiplier
                            if "volume" in df.columns:
                                df.loc[mask, "volume"] = df.loc[mask, "volume"] / multiplier
                        else:
                            logger.debug(f"  file={b_name}: mode={mode} のため価格・出来高は書き換えず、メタデータのみ更新")

                        df.loc[mask, "patched_multiplier"] = df.loc[mask, "patched_multiplier"] * multiplier

                        # ── インメモリ健全性アサーション検証 ──
                        health_ok = True
                        health_reason = ""
                        if df.empty:
                            health_ok = False
                            health_reason = "更新後データフレームが空です。"
                        elif ticker not in df["ticker"].values:
                            health_ok = False
                            health_reason = "対象銘柄のデータが消失しました。"
                        elif len(df) < original_row_count * 0.99:
                            health_ok = False
                            health_reason = f"総行数が異常に減少しました（{original_row_count} → {len(df)}）。"
                        else:
                            required_cols = [c for c in ["date", "ticker", "close"] if c in df.columns]
                            for col in required_cols:
                                if df[col].isna().any():
                                    health_ok = False
                                    health_reason = f"必須列 '{col}' にNULLが混入しました。"
                                    break
                            if health_ok and "close" in df.columns and (df["close"] <= 0).any():
                                health_ok = False
                                health_reason = "close列に0以下の不正な価格が混入しました。"

                        logger.debug(f"  file={b_name}: 健全性検証結果 health_ok={health_ok} reason='{health_reason}'")

                        if not health_ok:
                            log(f"  🛑 [安全ロック] {b_name} の健全性検証に失敗したため、保存・アップロードを中断しました: {health_reason}", level="error")
                            if os.path.exists(local_path):
                                os.remove(local_path)
                            continue

                        df_cleaned = df.drop(columns=["date_dt"], errors="ignore")
                        table = pa.Table.from_pandas(df_cleaned, preserve_index=False)
                        pq.write_table(table, local_path, use_dictionary=False, compression="SNAPPY")
                        logger.debug(f"  file={b_name}: ローカルParquet再書き出し完了")

                        t_up = time.time()
                        up_success, up_msg = upload_to_drive_api(b_name, local_path, parent_id=tf_folder_id)
                        logger.debug(f"  file={b_name}: アップロード{'成功' if up_success else '失敗'} ({time.time() - t_up:.2f}秒) msg={up_msg}")
                        if up_success:
                            applied_files_count += 1
                            log(f"  ✅ パッチ適用完了 ({b_name} | 対象: {n_affected:,}件)", level="info")

                            memo_parts = [f"deviation={patch.get('deviation_pct')}%" if patch.get("deviation_pct") is not None else "deviation=N/A"]
                            memo_parts.append(str(patch.get("status", "")))
                            repair_log_rows.append({
                                "executed_at": executed_at,
                                "ticker": ticker,
                                "market": "JP",
                                "cliff_date": cliff_date or actual_date,
                                "interval": interval,
                                "before_close": patch.get("before_close"),
                                "after_close": patch.get("after_close"),
                                "multiplier": multiplier,
                                "memo": " / ".join(memo_parts),
                            })
                        else:
                            log(f"  ❌ アップロード同期失敗 ({b_name}): {up_msg}", level="error")
                    else:
                        logger.debug(f"  file={b_name}: マスク該当レコードが0件のため書き換えなし")

                    # 💡 ローカルに残骸を残さないよう必ず削除
                    if os.path.exists(local_path):
                        os.remove(local_path)

                except Exception as ex_file:
                    log(f"  ❌ ファイル {b_name} 処理中のエラー: {ex_file}", level="error", exc=True)
                    if os.path.exists(local_path):
                        os.remove(local_path)
                    continue

            results[interval] = f"正常に修復 ({applied_files_count}件)"
            logger.debug(f"interval={interval}: 完了。適用ファイル数={applied_files_count}")

            # 💡 ローカルキャッシュをクリア
            clear_local_parquet_cache(interval=interval, is_jp=True)

        except Exception as e:
            results[interval] = f"エラー: {str(e)}"
            log(f"❌ 【{interval}】 一括書き換え適用失敗: {e}", level="error", exc=True)

    if repair_log_rows:
        logger.debug(f"repair_log 書き込み対象: {repair_log_rows}")
        try:
            from data_access.sheets_api import save_repair_log_to_sheets
            saved = save_repair_log_to_sheets(repair_log_rows)
            if saved:
                log(f"📝 repair_log シートへ {len(repair_log_rows)} 件の監査ログを記録しました。", level="info")
            else:
                log("⚠️ repair_log シートへの記録に失敗しました（スプレッドシート接続を確認してください）。", level="warning")
        except Exception as e:
            log(f"⚠️ repair_log 記録中に例外が発生しました: {e}", level="error", exc=True)

    # 💡 パッチ適用後はStreamlitキャッシュを完全クリア
    try:
        import streamlit as st
        if hasattr(st, "cache_data"):
            st.cache_data.clear()
            logger.debug("st.cache_data.clear() を実行しました。")
        if hasattr(st, "rerun"):
            logger.debug("st.rerun() を実行します。")
            st.rerun()
    except Exception:
        pass

    elapsed = time.time() - run_started_at
    logger.debug(f"apply_jp_patch_to_all_timeframes: 終了 ticker={ticker}, 所要時間={elapsed:.2f}秒, results={results}")
    return results