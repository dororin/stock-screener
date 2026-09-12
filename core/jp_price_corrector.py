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
from data_access.local_db import load_price_db, save_price_db
from data_access.drive_api import (
    download_from_drive_api,
    upload_to_drive_api,
    get_or_create_drive_folder,
    get_drive_service
)

# ══════════════════════════════════════════════════════════════════════════
# 🪵 デバッグロギング設定
#   ・コンソール（stdout）と、settings.WORK_DIR 配下のログファイルの両方に出力します。
#   ・トラブル発生時は jp_price_corrector_debug.log を確認すれば、
#     どのステップ・どの銘柄・どんな値で何が起きたかを追跡できます。
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
        # ログファイルが作成できない環境（権限等）でも処理自体は止めない
        logger.addHandler(logging.NullHandler())
        print(f"⚠️ [jp_price_corrector] デバッグログファイルの作成に失敗しました: {_e}")

logger.propagate = False


def _emit(status_callback, msg: str, level: str = "info", exc: bool = False):
    """
    ログ出力の共通ヘルパー。
    ・logger には常に指定levelで記録（DEBUGも含め、ファイル/コンソールに全て残る）
    ・status_callback（Streamlit等のUI表示）には info 以上のみ転送（DEBUGでUIが埋もれるのを防止）
    ・exc=True の場合はスタックトレース全文をログファイルに記録（例外解析用）
    """
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

    # 💡【最重要】UIコールバックより先に標準出力へ即座にフラッシュ出力する。
    # UI側（Streamlit）が再描画やセッション切断でコンテキストを失っていても、
    # バックエンドの走査・パッチ処理を絶対に停止させないための安全構造。
    print(f"[JP_PATCH_ENGINE/{level.upper()}] {msg}", flush=True)

    if status_callback and level != "debug":
        try:
            status_callback(msg)
        except Exception:
            # UIコンテナが消滅している等でコールバックが失敗しても、バックエンドは巻き込まない
            pass


# 突合の許容乖離率（仕様書Ⅰ-⑤）：これを超えると「不一致（暴落疑い）」として自動安全ロック
VERIFY_DEVIATION_THRESHOLD_PCT = 2.0

# Adj Close（auto_adjust=True）連続性チェックの許容ジャンプ率：
# 既知の分割はすべて遡及調整済みになっているはずなので、崖の前後でこれを超える断絶が
# 残っている場合は「既知の分割では説明できない崖（暴落・データ異常の疑い）」として扱う
ADJCLOSE_JUMP_THRESHOLD_PCT = 3.0

# 下位時間足オートフォーカス走査の対象（仕様書Ⅰ-④）
INTRADAY_TIMEFRAMES = ["60m", "5m", "1m"]


def get_dynamic_threshold(s: float) -> tuple:
    """
    公式の分割比率Sに応じて、許容する段差判定価格比R (前日Close / 当日Close) の範囲を返します。
    """
    if s >= 2.0:
        result = (s * 0.85, s * 1.15)
    elif s >= 1.30:
        result = (s * 0.85, s * 1.15)
    else:
        result = (1.03, s * 1.15)
    logger.debug(f"get_dynamic_threshold: s={s} -> min_R={result[0]:.4f}, max_R={result[1]:.4f}")
    return result


def _find_cliff_reverse(rows: list, min_R: float, max_R: float, max_gap_days: float = 15) -> int:
    """
    価格系列（[{'dt':..., 'close':...}, ...] の昇順リスト）を最新から過去に向かって逆向き走査し、
    動的バッファ閾値に合致する最初（＝Ex-Dateに最も近い）の段差インデックスを返します。
    見つからない場合は -1 。
    仕様書2.3：複数検出時のセーフガード（逆向き走査で最も新しい段差を採用）
    """
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
    """
    yf.download() の戻りDataFrameから、MultiIndex/通常カラムいずれの形状にも対応して
    "Close" 列をtz-naiveなDatetimeIndexの1次元Seriesとして抽出する共通ヘルパー。
    抽出できない場合は None を返す。
    """
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
        idx = idx.tz_localize(None)
    except Exception:
        pass
    close_data = close_data.copy()
    close_data.index = idx
    return close_data.sort_index()


def _extract_close_on_date(df: pd.DataFrame, date_str: str):
    """指定日付(YYYY-MM-DD)に一致する行から、最初の有効な正の値を抽出する。"""
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
    """
    指定タイムスタンプに最も近い時刻の値を抽出する（イントラデイ突合用）。
    許容誤差(tolerance_minutes)を超えて最も近い行が離れている場合は None を返す
    （＝データはあるが該当時刻の値としては使えないと判断）。
    """
    s = _extract_close_series(df)
    if s is None or s.empty:
        return None
    s = s[s > 0].dropna()
    if s.empty:
        return None
    target_np = np.datetime64(pd.to_datetime(target_ts))
    time_diffs_sec = np.abs((s.index.values - target_np).astype("timedelta64[s]").astype(float))
    min_idx = int(np.argmin(time_diffs_sec))
    if time_diffs_sec[min_idx] > tolerance_minutes * 60:
        return None
    return float(s.iloc[min_idx])


def _verify_against_yfinance_pure_close(ticker: str, target_dt, detected_close: float, cache: dict,
                                         interval: str = "1d") -> dict:
    """
    仕様書Ⅰ-⑤：yfinance「純粋な取引値Close」（配当ノイズを含まない、auto_adjust=False の Close）
    との突合により、乖離率を計算し、暴落誤認を排除する多重防護アサーションを行います。

    ⚠️【重要】interval を明示的に指定することで、60m/5m/1mの突合ではまずその足自体の
    イントラデイ値との照合を試みる。yfinance側の取得可能期間（1mは実測7〜8日程度など）を
    超えていて取得できない場合のみ、日足のCloseで代用する（is_daily_fallback=True で明示）。
    以前の実装は interval を一切指定していなかったため、常に日足のCloseと比較しており、
    intraday側の突合値が実質的に無意味だった。

    戻り値: {"yf_close": float|None, "deviation_pct": float|None, "passed": bool, "is_daily_fallback": bool}
    """
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
                    logger.debug(
                        f"_verify_against_yfinance_pure_close: interval={interval} のイントラデイ突合に失敗"
                        f"（取得可能期間外の可能性）。日足へフォールバックします。"
                    )
                    is_daily_fallback = True

            if yf_close is None:
                df_daily = yf.download(
                    f"{ticker}.T",
                    start=(target_ts - timedelta(days=3)).strftime("%Y-%m-%d"),
                    end=(target_ts + timedelta(days=3)).strftime("%Y-%m-%d"),
                    auto_adjust=False,   # 配当・分割の事後調整をかけない、実際の取引値そのもの
                    actions=False,
                    progress=False,
                    timeout=15
                )
                yf_close = _extract_close_on_date(df_daily, date_str)

            cache[cache_key] = {"yf_close": yf_close, "is_daily_fallback": is_daily_fallback}
            logger.debug(f"_verify_against_yfinance_pure_close: 取得結果 {cache_key} -> {cache[cache_key]}")
        except Exception as e:
            logger.warning(
                f"_verify_against_yfinance_pure_close: yfinance取得失敗 ticker={ticker}, target_dt={target_dt}, interval={interval}: {e}",
                exc_info=True
            )
            cache[cache_key] = {"yf_close": None, "is_daily_fallback": is_daily_fallback}
    else:
        logger.debug(f"_verify_against_yfinance_pure_close: キャッシュヒット ({cache_key})")

    cached = cache[cache_key]
    yf_close = cached["yf_close"]
    is_daily_fallback = cached["is_daily_fallback"]

    if yf_close is None or pd.isna(yf_close) or yf_close <= 0 or detected_close is None or pd.isna(detected_close):
        logger.debug(
            f"_verify_against_yfinance_pure_close: 突合材料不足のため不合格扱い "
            f"(yf_close={yf_close}, detected_close={detected_close})"
        )
        # 突合材料が無い場合は「未検証」として安全側（不合格）に倒す
        return {"yf_close": None, "deviation_pct": None, "passed": False, "is_daily_fallback": is_daily_fallback}

    deviation_pct = abs(detected_close - yf_close) / yf_close * 100.0
    passed = deviation_pct <= VERIFY_DEVIATION_THRESHOLD_PCT
    logger.debug(
        f"_verify_against_yfinance_pure_close: 突合結果 ticker={ticker}, date={date_str}, interval={interval}, "
        f"detected_close={detected_close}, yf_close={yf_close}, deviation_pct={deviation_pct:.3f}%, "
        f"passed={passed}, is_daily_fallback={is_daily_fallback} (閾値{VERIFY_DEVIATION_THRESHOLD_PCT}%)"
    )
    return {
        "yf_close": round(yf_close, 2),
        "deviation_pct": round(deviation_pct, 3),
        "passed": passed,
        "is_daily_fallback": is_daily_fallback
    }


def _verify_adjclose_continuity(ticker: str, cliff_dt, actual_dt, cache: dict,
                                 window_days: int = 10,
                                 jump_threshold_pct: float = ADJCLOSE_JUMP_THRESHOLD_PCT) -> dict:
    """
    仕様書Ⅰ-⑤拡張：Adj Close（auto_adjust=True）による独立した連続性チェック。

    既知の公式分割はすべてyfinance側で遡及調整（back-adjust）されているはずなので、
    「本当にその公式分割による崖」であれば、Adj Close系列は崖の前後で滑らか（断絶なし）になる。
    逆に、崖の位置が既知の分割では説明できない暴落やデータ異常であれば、
    Adj Closeで補正してもなお断絶（大きな変化率）が残る。

    ⚠️ これは _verify_against_yfinance_pure_close（生値の絶対一致チェック）とは独立した、
    別の観点の安全網。特に収集ラグにより崖の位置が公式ex_dateから大きくズレているケース
    （放置による崖ズレ）では、この連続性チェックが最も効く。

    戻り値: {"max_jump_pct": float|None, "passed": bool}
    """
    boundary_date = pd.to_datetime(actual_dt).normalize()
    cache_key = f"adjcont_{ticker}_{boundary_date.strftime('%Y-%m-%d')}"

    if cache_key not in cache:
        logger.debug(f"_verify_adjclose_continuity: キャッシュ未ヒット。yfinanceへ問い合わせ中 ({cache_key})")
        result = {"max_jump_pct": None, "passed": False}
        try:
            start = (boundary_date - timedelta(days=window_days * 2)).strftime("%Y-%m-%d")
            end = (boundary_date + timedelta(days=window_days * 2)).strftime("%Y-%m-%d")
            df_adj = yf.download(
                f"{ticker}.T", start=start, end=end,
                auto_adjust=True,  # 既知の分割・配当を遡及調整した「連続な」価格系列
                actions=False, progress=False, timeout=15
            )
            s = _extract_close_series(df_adj)
            if s is not None and len(s) >= 2:
                pct_changes = s.pct_change().abs() * 100.0
                # 境界日 ±window_days 営業日程度の範囲だけを見る（無関係な遠い変動を拾わないため）
                near_mask = (
                    (s.index >= boundary_date - pd.Timedelta(days=window_days)) &
                    (s.index <= boundary_date + pd.Timedelta(days=window_days))
                )
                near_changes = pct_changes[near_mask].dropna()
                if not near_changes.empty:
                    max_jump = float(near_changes.max())
                    result = {"max_jump_pct": round(max_jump, 3), "passed": max_jump <= jump_threshold_pct}
            cache[cache_key] = result
            logger.debug(f"_verify_adjclose_continuity: 取得結果 {cache_key} -> {result}")
        except Exception as e:
            logger.warning(
                f"_verify_adjclose_continuity: yfinance取得失敗 ticker={ticker}, boundary_date={boundary_date}: {e}",
                exc_info=True
            )
            cache[cache_key] = {"max_jump_pct": None, "passed": False}
    else:
        logger.debug(f"_verify_adjclose_continuity: キャッシュヒット ({cache_key})")

    return cache[cache_key]

def _scan_intraday_cliff(ticker: str, interval: str, ex_date, actual_day_dt, s_val: float,
                          daily_after_close: float, yf_cache: dict, status_callback=None) -> dict:
    """
    仕様書Ⅰ-④：下位時間足（60m/5m/1m）の段差オートフォーカス。
    該当銘柄・該当時間足のみをピンポイント投影ロードし、
    実際に価格が跳んでいる正確な日時（ミリ秒単位のタイムスタンプ）を自律特定します。

    ⚠️【重要】楽天RSSの実データ取得は暦日ではなく「営業日ベースの本数」で制限されるため
    （例：1mは実測9営業日分）、下位足の崖はex_date当日ではなく、そこから最大で
    settings.SPLIT_SCAN_LOOKBACK_BDAYS[interval] 営業日分「過去にズレた位置」に出現しうる。
    さらにメンテナンスジョブの実行が数日〜数週間放置されていた場合、そのズレは
    「今日」に近づく方向にも動くため、走査の前方境界は固定日数ではなく常に現在時刻まで開放する。
    """
    logger.debug(f"_scan_intraday_cliff: 開始 ticker={ticker}, interval={interval}, ex_date={ex_date}, actual_day_dt={actual_day_dt}, s_val={s_val}")

    ex_date_ts = pd.to_datetime(ex_date)
    lookback_bdays = settings.SPLIT_SCAN_LOOKBACK_BDAYS.get(interval, 30)
    lookback_start = (ex_date_ts - pd.tseries.offsets.BDay(lookback_bdays)).strftime("%Y-%m-%d")
    # 前方は固定日数で打ち切らず、常に「現在時刻」までフルカバーする
    # （収集ジョブの放置期間次第で崖の位置が未来方向へズレるのを見逃さないため）
    lookback_end = pd.Timestamp.now().strftime("%Y-%m-%d")
    logger.debug(
        f"_scan_intraday_cliff: ピンポイント投影ロード範囲 [{lookback_start} 〜 {lookback_end}] "
        f"(lookback_bdays={lookback_bdays})"
    )

    try:
        df_tf = load_price_db(
            interval=interval,
            is_jp=True,
            is_raw=False,
            columns=["date", "ticker", "close", "patched_multiplier"],
            filters=[("ticker", "==", ticker), ("date", ">=", lookback_start), ("date", "<=", lookback_end)]
        )
        logger.debug(f"_scan_intraday_cliff: load_price_db 完了 -> {len(df_tf) if df_tf is not None else 0} 行")
    except Exception as e:
        _emit(status_callback, f"  ⚠️ [{interval}] ピンポイント投影ロード失敗 (ticker={ticker}): {e}", level="warning", exc=True)
        df_tf = pd.DataFrame()

    if df_tf is None or df_tf.empty:
        logger.debug(f"_scan_intraday_cliff: {interval} データが空のためスキップ (ticker={ticker})")
        return {"mode": "メタデータのみ更新", "actual_dt": None, "cliff_dt": None,
                "before_close": np.nan, "after_close": np.nan, "verify": {"yf_close": None, "deviation_pct": None, "passed": True},
                "status": "[データ無し・スキップ]"}

    if "patched_multiplier" not in df_tf.columns:
        df_tf["patched_multiplier"] = 1.0

    df_tf["date_dt"] = pd.to_datetime(df_tf["date"])
    try:
        df_tf["date_dt"] = df_tf["date_dt"].dt.tz_localize(None)
    except Exception:
        logger.debug("_scan_intraday_cliff: tz_localize(None) をスキップ（すでにtz-naive）", exc_info=True)
    df_tf = df_tf.sort_values("date_dt").reset_index(drop=True)

    # 💡【重要】ここで対象日当日15時などに区切らない。上のload_price_db段階で
    # すでに [ex_date - 営業日lookback, 現在] という必要十分な範囲に絞り込み済みのため、
    # 取得した全行がそのまま走査対象のウィンドウになる。
    df_window = df_tf.tail(5000).reset_index(drop=True)
    logger.debug(f"_scan_intraday_cliff: ウィンドウ抽出後 {len(df_window)} 行")

    if len(df_window) < 2:
        logger.debug(f"_scan_intraday_cliff: {interval} データ不足のためスキップ (ticker={ticker}, rows={len(df_window)})")
        return {"mode": "メタデータのみ更新", "actual_dt": None, "cliff_dt": None,
                "before_close": np.nan, "after_close": np.nan, "verify": {"yf_close": None, "deviation_pct": None, "passed": True},
                "status": "[データ不足・スキップ]"}

    min_R, max_R = get_dynamic_threshold(s_val)
    rows = [{"dt": r["date_dt"], "close": r["close"]} for _, r in df_window.iterrows()]
    # 時間足内なので、隔たりは日単位ではなく分単位で緩めに設定（休場を跨いでも許容）
    idx = _find_cliff_reverse(rows, min_R, max_R, max_gap_days=6)

    if idx == -1:
        logger.debug(f"_scan_intraday_cliff: {interval} 段差未検出（すでに調整済みの可能性） ticker={ticker}")
        return {"mode": "メタデータのみ更新", "actual_dt": None, "cliff_dt": None,
                "before_close": np.nan, "after_close": np.nan, "verify": {"yf_close": None, "deviation_pct": None, "passed": True},
                "status": "[正常分割（段差未検出＝調整済み）]"}

    row_T = df_window.iloc[idx]
    row_prev = df_window.iloc[idx - 1]
    actual_dt = row_T["date_dt"]
    cliff_dt = row_prev["date_dt"]
    before_close = row_prev["close"]
    after_close = row_T["close"]
    logger.debug(
        f"_scan_intraday_cliff: {interval} 段差ピンポイント特定 ticker={ticker}, "
        f"cliff_dt={cliff_dt}, actual_dt={actual_dt}, before={before_close}, after={after_close}"
    )

    # 重複判定：すでに patched_multiplier が刻印済みならスキップ対象
    mult_T = row_T["patched_multiplier"]
    mult_prev = row_prev["patched_multiplier"]
    if mult_T > 0 and abs((mult_prev / mult_T) - 1.0) > 0.15:
        logger.debug(
            f"_scan_intraday_cliff: {interval} は既にパッチ適用済みと判定 "
            f"(mult_prev={mult_prev}, mult_T={mult_T}) -> スキップ"
        )
        return {"mode": None, "actual_dt": actual_dt, "cliff_dt": cliff_dt,
                "before_close": before_close, "after_close": after_close,
                "verify": {"yf_close": None, "deviation_pct": None, "passed": True},
                "status": "[処理済み・スキップ]"}

    # 仕様書Ⅰ-⑤：当該足自体のyfinance生値との突合（取得可能期間外なら日足に自動フォールバック）
    verify = _verify_against_yfinance_pure_close(ticker, actual_dt, after_close, yf_cache, interval=interval)

    # 仕様書Ⅰ-⑤拡張：Adj Close連続性チェック（既知の分割で本当に説明できる崖かの独立検証）
    adjcont = _verify_adjclose_continuity(ticker, cliff_dt, actual_dt, yf_cache)
    verify["adjclose_max_jump_pct"] = adjcont["max_jump_pct"]
    verify["adjclose_passed"] = adjcont["passed"]
    verify["passed"] = bool(verify["passed"]) and bool(adjcont["passed"])

    # 💡【重要】「要パッチ」は「これから正しい分割としてパッチ適用する対象」を意味するので、
    # 「メタデータのみ更新（何もしない）」側の「[正常分割]」とは別の文言にして混同を防ぐ
    status_label = "[パッチ対象・分割整合OK]" if verify["passed"] else "[不一致：暴落またはノイズ疑い]"
    if verify.get("is_daily_fallback") and verify["passed"]:
        status_label = "[パッチ対象・分割整合OK（日足代用突合）]"
    if actual_dt < pd.to_datetime(actual_day_dt):
        status_label = "[⚠️先回り調整混入（警告）]"

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


def scan_jp_anomalies_with_yfinance(status_callback=None) -> pd.DataFrame:
    """
    本番日足データ(price_jp_1d.parquet)から監視銘柄リストのデータをロードし、
    yfinanceから公式の株式分割履歴を一時取得して、過去に遡ったルックバック走査診断を行います。
    100銘柄ずつのバッチ処理により、外部通信回数を最小限に抑えます。

    仕様書Ⅰの①〜⑥の全ステップに対応：
      ① 1次フィルター（公式イベント銘柄のバルク高速抽出）
      ② 照合用マスターの取得（配当抜きの純粋な分割調整後Close）
      ③ 日足（1d）の面走査（Ex-Dateから過去30営業日）
      ④ 下位時間足（60m, 5m, 1m）の段差オートフォーカス（自動特定）
      ⑤ yfinance純粋Closeとの突合・多重防護アサーション（暴落の完全排除）
      ⑥ プレビュー用データフレームの構築（UI描画はこの戻り値をもとに呼び出し側で行う）

    戻り値の各行は「銘柄コード×時間足」の1組であり、1d・60m・5m・1mそれぞれについて
    実質段差日時・前後Close・突合結果・検証ステータス・選択可否(is_selectable)を含みます。

    ※ トラブル時は settings.WORK_DIR/jp_price_corrector_debug.log に詳細なDEBUGログが
       すべて記録されます（どの銘柄・どの時間足で何が起きたか、例外のスタックトレース含む）。
    """
    def log(msg, level="info", exc=False):
        _emit(status_callback, msg, level=level, exc=exc)

    run_started_at = time.time()
    log("📊 日本株本番日足データ (price_jp_1d) をロード中...", level="info")
    logger.debug("scan_jp_anomalies_with_yfinance: 処理開始")

    try:
        # 日足の診断に必要なカラムのみを投影ロード
        df_1d = load_price_db(
            interval="1d",
            is_jp=True,
            is_raw=False,
            columns=["date", "ticker", "close", "patched_multiplier"]
        )
        logger.debug(f"load_price_db(1d) 完了: {0 if df_1d is None else len(df_1d)} 行")
    except Exception as e:
        log(f"❌ 日足データのロードに失敗しました: {e}", level="error", exc=True)
        return pd.DataFrame()

    if df_1d.empty:
        log("⚠️ 日本株日足データが見つかりません。先にデータ収集・マージを行ってください。", level="warning")
        return pd.DataFrame()

    # patched_multiplier カラムが存在しない場合は一律 1.0 で初期化
    if "patched_multiplier" not in df_1d.columns:
        logger.debug("patched_multiplier カラムが存在しないため 1.0 で初期化します。")
        df_1d["patched_multiplier"] = 1.0

    df_1d["date_dt"] = pd.to_datetime(df_1d["date"]).dt.tz_localize(None)
    tickers = df_1d["ticker"].unique().tolist()
    logger.debug(f"監視対象ユニーク銘柄数: {len(tickers)}")

    # ── 仕様書Ⅰ-①：イベントターゲット絞り込み（直近3ヶ月間：90日に限定） ──
    target_start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    log(f"🔎 データベース内から {len(tickers)} 銘柄を検出し、直近3ヶ月間（{target_start_date}以降）の公式分割イベントを高速フィルタ中...", level="info")

    batch_size = 100
    ticker_batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]
    logger.debug(f"バッチ分割: {len(ticker_batches)} バッチ（{batch_size}銘柄/バッチ）")

    event_tickers_with_splits = {}  # {ticker: {ex_date_timestamp: split_ratio}}

    for b_idx, batch in enumerate(ticker_batches):
        batch_tickers_T = [f"{t}.T" for t in batch]
        logger.debug(f"バッチ[{b_idx + 1}/{len(ticker_batches)}] yf.download開始: {len(batch_tickers_T)}銘柄")
        try:
            t0 = time.time()
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
            logger.debug(f"バッチ[{b_idx + 1}] yf.download完了 ({time.time() - t0:.2f}秒), shape={getattr(df_download, 'shape', None)}")

            if df_download.empty:
                logger.debug(f"バッチ[{b_idx + 1}] ダウンロード結果が空のためスキップ")
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
                logger.debug(f"バッチ[{b_idx + 1}] Stock Splits列が空のためスキップ")
                continue

            batch_hit_count = 0
            for ticker_with_T in df_splits.columns:
                splits_series_raw = df_splits[ticker_with_T]
                splits_series = splits_series_raw[(splits_series_raw > 0) & (splits_series_raw != 1.0)].dropna()

                if not splits_series.empty:
                    ticker = ticker_with_T.split(".")[0]
                    event_tickers_with_splits[ticker] = splits_series.to_dict()
                    batch_hit_count += 1
                    logger.debug(f"  ➕ 分割イベント検出: {ticker} -> {splits_series.to_dict()}")

            logger.debug(f"バッチ[{b_idx + 1}] 完了: {batch_hit_count}銘柄で分割イベント検出")

        except Exception as ex:
            log(f"  ⚠️ バッチ [{b_idx + 1}] 処理中に例外検出 (スキップ): {ex}", level="warning", exc=True)
            continue

    if not event_tickers_with_splits:
        log("✅ 直近3ヶ月間に株式分割イベントが検知された銘柄はありません。スキャンを正常終了します。", level="info")
        logger.debug(f"scan_jp_anomalies_with_yfinance: 終了（対象0件, 所要{time.time() - run_started_at:.2f}秒）")
        return pd.DataFrame()

    log(f"🎯 公式分割イベントを検出。対象 {len(event_tickers_with_splits)} 銘柄に対して、詳細なインメモリ遡及走査を開始します。", level="info")
    logger.debug(f"分割イベント検出銘柄一覧: {list(event_tickers_with_splits.keys())}")

    results = []
    yf_cache = {}  # {"ticker_YYYY-MM-DD": yf_close|None} 突合キャッシュ（時間足間で使い回して通信を節約）

    # ── 仕様書Ⅰ-②〜③：日足のインメモリ投影走査（イベント適合銘柄のみに限定ループ） ──
    for ticker_idx, (ticker, splits_dict) in enumerate(event_tickers_with_splits.items()):
        logger.debug(f"[{ticker_idx + 1}/{len(event_tickers_with_splits)}] ticker={ticker} 走査開始 splits_dict={splits_dict}")
        df_ticker = df_1d[df_1d["ticker"] == ticker].sort_values("date_dt").reset_index(drop=True)
        if df_ticker.empty or len(df_ticker) < 2:
            logger.debug(f"ticker={ticker}: 日足データが不足（{len(df_ticker)}行）のためスキップ")
            continue

        for ex_date, s_val in splits_dict.items():
            if pd.isna(s_val) or s_val <= 0.0 or s_val == 1.0:
                logger.debug(f"ticker={ticker}: ex_date={ex_date} の split値が不正({s_val})のためスキップ")
                continue

            planned_dt = pd.to_datetime(ex_date).tz_localize(None)
            planned_date_str = planned_dt.strftime("%Y-%m-%d")
            logger.debug(f"ticker={ticker}: 分割イベント処理開始 ex_date={planned_date_str}, s_val={s_val}")

            # ── 仕様書Ⅰ-③：走査ロジック（「点」から「面」への移行：過去30営業日） ──
            ex_date_idx_list = df_ticker[df_ticker["date_dt"] <= planned_dt].index.tolist()
            if not ex_date_idx_list:
                logger.debug(f"ticker={ticker}: planned_dt={planned_dt} 以前のデータが存在しないためスキップ")
                continue

            ex_date_idx = ex_date_idx_list[-1]
            lookback_start_idx = max(0, ex_date_idx - settings.SPLIT_SCAN_LOOKBACK_BDAYS.get("1d", 30))
            df_window = df_ticker.iloc[lookback_start_idx: ex_date_idx + 1].copy()
            logger.debug(f"ticker={ticker}: ルックバックウィンドウ {len(df_window)}行 (idx {lookback_start_idx}〜{ex_date_idx})")

            if len(df_window) < 2:
                logger.debug(f"ticker={ticker}: ウィンドウ行数不足のためスキップ")
                continue

            min_R, max_R = get_dynamic_threshold(s_val)
            rows = [{"dt": r["date_dt"], "close": r["close"]} for _, r in df_window.iterrows()]
            rel_idx = _find_cliff_reverse(rows, min_R, max_R, max_gap_days=15)
            detected_idx = df_window.index[rel_idx] if rel_idx != -1 else -1
            logger.debug(f"ticker={ticker}: 日足段差検出結果 detected_idx={detected_idx}")

            M = 1.0 / s_val

            # ── [安全・重複検知防止チェック] 既に累積乗数（マーク）が書き換わっているか判定 ──
            check_idx = detected_idx if detected_idx != -1 else ex_date_idx
            check_prev_idx = max(0, check_idx - 1)
            mult_T = df_ticker.loc[check_idx, "patched_multiplier"]
            mult_prev = df_ticker.loc[check_prev_idx, "patched_multiplier"]
            is_already_patched = mult_T > 0 and abs((mult_prev / mult_T) - 1.0) > 0.15
            if is_already_patched:
                logger.debug(
                    f"ticker={ticker}: 既にパッチ適用済みと判定 (mult_prev={mult_prev}, mult_T={mult_T}) -> このイベントをスキップ"
                )
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

                # ── 仕様書Ⅰ-⑤：yfinance純粋Closeとの突合・多重防護アサーション ──
                verify = _verify_against_yfinance_pure_close(ticker, actual_dt, close_T_val, yf_cache, interval="1d")

                # ── 仕様書Ⅰ-⑤拡張：Adj Close連続性チェック（放置による崖ズレでも機能する独立検証） ──
                adjcont = _verify_adjclose_continuity(ticker, cliff_dt, actual_dt, yf_cache)
                verify["adjclose_max_jump_pct"] = adjcont["max_jump_pct"]
                verify["adjclose_passed"] = adjcont["passed"]
                verify["passed"] = bool(verify["passed"]) and bool(adjcont["passed"])

                mode = "要パッチ"

                if not verify["passed"]:
                    status_label = "[不一致：暴落またはノイズ疑い（選択不可）]"
                    logger.warning(
                        f"ticker={ticker}: 突合不一致を検出しました。安全ロックを適用します。 "
                        f"deviation_pct={verify['deviation_pct']}, yf_close={verify['yf_close']}, detected_close={close_T_val}, "
                        f"adjclose_max_jump_pct={verify['adjclose_max_jump_pct']}"
                    )
                elif actual_dt < planned_dt:
                    status_label = "[⚠️先回り調整混入（警告）]"
                    logger.debug(f"ticker={ticker}: 先回り調整混入を検出 actual_dt={actual_dt} < planned_dt={planned_dt}")
                else:
                    # 💡【重要】「メタデータのみ更新（何もしない）」側の「[正常分割]」と同じ文言だと、
                    # 「要パッチ」なのに何もしなくていいように見えてしまうため、専用の文言にする
                    status_label = "[パッチ対象・分割整合OK]"
            else:
                # 崖がルックバック期間内に検出されなかった場合（すでに全データが先回り調整済みなど）
                mode = "メタデータのみ更新"
                actual_dt = planned_dt
                cliff_dt = planned_dt

                row_T = df_ticker.iloc[ex_date_idx]
                close_T_val = row_T["close"]
                if ex_date_idx > 0:
                    close_prev_val = df_ticker.iloc[ex_date_idx - 1]["close"]

                verify = _verify_against_yfinance_pure_close(ticker, actual_dt, close_T_val, yf_cache, interval="1d")
                status_label = "[正常分割]" if verify["passed"] else "[不一致：要目視確認（選択不可）]"
                logger.debug(f"ticker={ticker}: 日足段差なし -> メタデータのみ更新モード, status={status_label}")

            if mode:
                is_selectable = bool(verify["passed"])
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
                    "adjclose_max_jump_pct": verify.get("adjclose_max_jump_pct"),
                    "status": status_label,
                    "is_selectable": is_selectable,
                })
                logger.debug(f"ticker={ticker}: 1d 結果行を追加 mode={mode}, status={status_label}, selectable={is_selectable}")

                # ── 仕様書Ⅰ-④：下位時間足（60m, 5m, 1m）の段差オートフォーカス ──
                # 日足側で正しく崖を確定できた場合のみ、下位足のピンポイント投影ロード＆走査を行う
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
                                status_callback=status_callback
                            )
                        except Exception as e:
                            log(f"  ❌ [{ticker}] {tf} 足のオートフォーカス走査中に予期しない例外: {e}", level="error", exc=True)
                            continue

                        if tf_result["mode"] is None:
                            logger.debug(f"ticker={ticker}, interval={tf}: 処理済みのためスキップ")
                            continue  # 処理済みスキップ

                        tf_actual_dt = tf_result["actual_dt"]
                        tf_cliff_dt = tf_result["cliff_dt"]
                        tf_verify = tf_result["verify"]
                        tf_selectable = bool(tf_verify.get("passed", True))

                        # メタデータのみ更新（＝下位足でも段差未検出）の場合は当日09:00を境界に採用
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
                            "adjclose_max_jump_pct": tf_verify.get("adjclose_max_jump_pct"),
                            "status": tf_result["status"],
                            "is_selectable": tf_selectable,
                        })
                        logger.debug(f"ticker={ticker}: {tf} 結果行を追加 mode={tf_result['mode']}, status={tf_result['status']}, selectable={tf_selectable}")

            time.sleep(1.0)

    elapsed = time.time() - run_started_at
    log(f"🎉 日本株段差スキャン完了。自動検出された修正候補: {len(results)} 件（全時間足合算）", level="info")
    logger.debug(f"scan_jp_anomalies_with_yfinance: 終了（所要時間 {elapsed:.2f}秒, 結果件数={len(results)}）")
    return pd.DataFrame(results)


def apply_jp_patch_to_all_timeframes(ticker: str, patch_rows: list, status_callback=None) -> dict:
    """
    日本株の全時間足（1d, 60m, 5m, 1m）の本番Parquetに対し、
    安全かつ可逆的に遡及一括調整パッチを適用します。

    仕様書Ⅱの①〜⑦の全ステップに対応：
      ① 承認済み行の抽出（呼び出し側が管理者チェック済みの patch_rows を渡す）
      ② 時間足ごとのParquetロードと条件抽出（不等号 '<' 処理）
      ③ 価格・出来高の書き換え、および過去全レコードへの刻印
      ④ インメモリ健全性アサーション検証
      ⑤ Google Driveへの確定アップロード＆ローカル破棄
      ⑥ Google Sheets「repair_log」への監査ログ永続記録
      ⑦ キャッシュクリアと画面の自動リフレッシュ（Streamlit環境がある場合のみ）

    patch_rows: この銘柄に対して管理者が承認した行のリスト。各要素は scan_jp_anomalies_with_yfinance
                が返す行と同じ形式（少なくとも interval, cliff_date, multiplier, mode, before_close,
                after_close, deviation_pct を含む）を想定。時間足ごとに個別の境界日時を使用します。

    ※ トラブル時は settings.WORK_DIR/jp_price_corrector_debug.log を参照してください。
       どのファイルの処理でエラーが起きたか、健全性チェックで何が引っかかったか、
       Drive/Sheetsとの通信で何が起きたかがすべてDEBUGレベルで記録されています。
    """
    def log(msg, level="info", exc=False):
        _emit(status_callback, msg, level=level, exc=exc)

    logger.debug(f"apply_jp_patch_to_all_timeframes: 開始 ticker={ticker}, patch_rows件数={len(patch_rows)}")
    logger.debug(f"apply_jp_patch_to_all_timeframes: patch_rows内容={patch_rows}")

    # ── 仕様書Ⅱ-①：管理者が目視承認した行のみを対象に、異常値の物理アサーション ──
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

        cliff_date = patch["cliff_date"]
        multiplier = patch["multiplier"]
        mode = patch["mode"]
        cliff_dt = pd.to_datetime(cliff_date)
        cliff_year = cliff_dt.year
        cliff_month = cliff_dt.month
        logger.debug(f"interval={interval}: cliff_dt={cliff_dt}, multiplier={multiplier}, mode={mode}")

        try:
            tf_folder_id = get_or_create_drive_folder(interval, settings.FOLDER_ID)
            logger.debug(f"interval={interval}: Driveフォルダ取得 tf_folder_id={tf_folder_id}")
            service = get_drive_service()
            if not service:
                raise ConnectionError("Google Driveサービスにアクセスできません。")

            # 本番 parquet ファイル（_diff_ を含まないもの）を一覧取得
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

                # ── 仕様書Ⅱ-②：時間足ごとの判定ルール（不等号 '<' 処理） ──
                if interval == "1d":
                    if b_name == "price_jp_1d.parquet":
                        should_process = True
                        conditional_records = True

                elif interval == "60m":
                    m_y = re.search(r'price_jp_60m_(\d{4})\.parquet', b_name)
                    if m_y:
                        file_year = int(m_y.group(1))
                        if file_year < cliff_year:
                            should_process = True
                            all_records = True
                        elif file_year == cliff_year:
                            should_process = True
                            conditional_records = True

                else:  # 5m or 1m
                    m_ym = re.search(r'price_jp_(?:5m|1m)_(\d{4})_(\d{2})\.parquet', b_name)
                    if m_ym:
                        file_year = int(m_ym.group(1))
                        file_month = int(m_ym.group(2))

                        if (file_year < cliff_year) or (file_year == cliff_year and file_month < cliff_month):
                            should_process = True
                            all_records = True
                        elif file_year == cliff_year and file_month == cliff_month:
                            should_process = True
                            conditional_records = True

                if not should_process:
                    logger.debug(f"  file={b_name}: 対象外（cliff_year={cliff_year}, cliff_month={cliff_month} と不一致）のためスキップ")
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
                        logger.debug(f"  file={b_name}: tz_localize(None) をスキップ（すでにtz-naive）", exc_info=True)

                    if all_records:
                        mask = (df["ticker"] == ticker)
                    elif conditional_records:
                        # 時間足ごとに個別特定された「実質段差日時」を境界として使用（不等号 '<' 適用）
                        mask = (df["ticker"] == ticker) & (df["date_dt"] < cliff_dt)
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

                        # multiplierの履歴を過去レコード1つ1つに累積（実施済みマーク）
                        df.loc[mask, "patched_multiplier"] = df.loc[mask, "patched_multiplier"] * multiplier

                        # ── 仕様書Ⅱ-④：インメモリ健全性アサーション検証 ──
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

                        # ── 仕様書Ⅱ-⑤：Google Driveへの確定アップロードと一時ファイル破棄 ──
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
                                "cliff_date": cliff_date,
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

                    if os.path.exists(local_path):
                        os.remove(local_path)

                except Exception as ex_file:
                    log(f"  ❌ ファイル {b_name} 処理中のエラー: {ex_file}", level="error", exc=True)
                    if os.path.exists(local_path):
                        os.remove(local_path)
                    continue

            results[interval] = f"正常に修復 ({applied_files_count}件)"
            logger.debug(f"interval={interval}: 完了。適用ファイル数={applied_files_count}")

        except Exception as e:
            results[interval] = f"エラー: {str(e)}"
            log(f"❌ 【{interval}】 一括書き換え適用失敗: {e}", level="error", exc=True)

    # ── 仕様書Ⅱ-⑥：Google Sheets「repair_log」への監査ログ永続記録（完全可逆性の担保） ──
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
    else:
        logger.debug("repair_log 書き込み対象なし（適用済みパッチが0件）")

    # ── 仕様書Ⅱ-⑦：キャッシュクリアと画面の自動リフレッシュ（Streamlit実行環境の場合のみ） ──
    try:
        import streamlit as st
        if hasattr(st, "cache_data"):
            st.cache_data.clear()
            logger.debug("st.cache_data.clear() を実行しました。")
        if hasattr(st, "rerun"):
            logger.debug("st.rerun() を実行します。")
            st.rerun()
    except Exception:
        logger.debug("Streamlit環境ではない、またはrerun中の例外のため、キャッシュクリア/リフレッシュをスキップしました。", exc_info=True)

    elapsed = time.time() - run_started_at
    logger.debug(f"apply_jp_patch_to_all_timeframes: 終了 ticker={ticker}, 所要時間={elapsed:.2f}秒, results={results}")
    return results
