# core/event_collector.py

import os
import json
import time
from datetime import datetime, date
import pandas as pd
from tradingview_screener import Query
from config import settings

EVENT_COLUMNS = ["銘柄コード", "銘柄名", "次回決算日", "直近決算日", "次回配当日", "直近配当日", "更新日時"]

def fetch_events_from_tradingview(tickers: list, is_jp: bool = True) -> pd.DataFrame:
    """
    TradingView Screener APIを利用し、指定された銘柄群の
    決算発表日・配当権利日スケジュールをバルククエリで一括取得します。
    """
    if not tickers:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    clean_tickers = [str(t).strip().upper().split(".")[0] for t in tickers if str(t).strip()]
    clean_tickers = list(dict.fromkeys(clean_tickers))

    symbol_map = {}
    tv_symbols = []

    for t in clean_tickers:
        if is_jp:
            tv_sym = f"TSE:{t}"
        else:
            tv_sym = t.replace("-", ".")
        tv_symbols.append(tv_sym)
        symbol_map[tv_sym] = t

    select_fields = [
        "name",
        "description",
        "earnings_release_next_date",
        "earnings_release_date",
        "dividends_ex_date",
        "dividends_ex_date_recent"
    ]

    all_rows = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 400件ずつの安全なチャンク分割でバルク取得
    CHUNK_SIZE = 400
    for i in range(0, len(tv_symbols), CHUNK_SIZE):
        chunk = tv_symbols[i:i + CHUNK_SIZE]
        try:
            query = Query().set_tickers(*chunk).select(*select_fields)
            total_count, df_res = query.get_scanner_data()

            if df_res is None or df_res.empty:
                continue

            for idx, r in df_res.iterrows():
                tv_ticker = r.get("ticker", idx)
                if not isinstance(tv_ticker, str):
                    continue

                code = symbol_map.get(tv_ticker)
                if not code:
                    clean_id = tv_ticker.split(":")[-1] if ":" in tv_ticker else tv_ticker
                    code = symbol_map.get(clean_id, clean_id)

                def _to_date_str(val):
                    if val is None or pd.isna(val):
                        return ""
                    try:
                        ts = float(val)
                        if ts <= 0:
                            return ""
                        # UNIXタイムスタンプ秒をYYYY-MM-DDに変換
                        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    except Exception:
                        return ""

                next_earn = _to_date_str(r.get("earnings_release_next_date"))
                prev_earn = _to_date_str(r.get("earnings_release_date"))
                next_div = _to_date_str(r.get("dividends_ex_date"))
                prev_div = _to_date_str(r.get("dividends_ex_date_recent"))

                # 直近配当日がブランクで、次回配当落ち日が既に過去日として返っている場合の補正
                today_str = date.today().strftime("%Y-%m-%d")
                if not prev_div and next_div and next_div < today_str:
                    prev_div = next_div
                    next_div = ""

                comp_name = str(r.get("description", "") or r.get("name", "")).strip()

                all_rows.append({
                    "銘柄コード": code,
                    "銘柄名": comp_name,
                    "次回決算日": next_earn,
                    "直近決算日": prev_earn,
                    "次回配当日": next_div,
                    "直近配当日": prev_div,
                    "更新日時": now_str
                })
        except Exception as ex:
            print(f"⚠️ [event_collector] チャンク取得中にエラー: {ex}")
            continue

    if not all_rows:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    df_out = pd.DataFrame(all_rows).drop_duplicates(subset=["銘柄コード"]).reset_index(drop=True)
    return df_out[EVENT_COLUMNS]


def get_earnings_countdown_badge(next_earnings_date_str: str, base_date: date = None) -> str:
    """
    次回決算日文字列 (YYYY-MM-DD) から実日数差分 N を算出し、
    仕様書準拠の警告HTMLバッジ文字列を生成して返します（N > 7 または未定は空文字）。
    """
    if not next_earnings_date_str or pd.isna(next_earnings_date_str):
        return ""

    try:
        target_date = datetime.strptime(str(next_earnings_date_str).strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return ""

    if base_date is None:
        base_date = date.today()

    diff_days = (target_date - base_date).days

    if diff_days < 0 or diff_days > 7:
        return ""

    md_str = target_date.strftime("%m/%d").lstrip("0").replace("/0", "/")

    if diff_days == 0:
        # N = 0 (当日)
        return (
            f"<span style='font-size:0.75rem; background:#d50000; color:#ffffff; "
            f"padding:2px 6px; border-radius:3px; font-weight:bold; letter-spacing:0.02em;' "
            f"title='次回決算発表日: {target_date}'>🚨 本日 決算発表！</span>"
        )
    elif 1 <= diff_days <= 3:
        # 1 <= N <= 3 日前
        return (
            f"<span style='font-size:0.75rem; background:#c62828; color:#ffffff; "
            f"padding:2px 6px; border-radius:3px; font-weight:bold;' "
            f"title='次回決算発表日: {target_date}'>🔴 決算直前！ あと {diff_days}日 ({md_str})</span>"
        )
    elif 4 <= diff_days <= 7:
        # 4 <= N <= 7 日前
        return (
            f"<span style='font-size:0.75rem; background:#ff8f00; color:#000000; "
            f"padding:2px 6px; border-radius:3px; font-weight:bold;' "
            f"title='次回決算発表日: {target_date}'>⚠️ 決算まであと {diff_days}日 ({md_str})</span>"
        )

    return ""


def build_event_markers(prev_earnings_date_str: str, prev_dividend_date_str: str) -> list:
    """
    直近決算日および直近配当落ち日から、Lightweight Charts (LWC) 用の
    極小ドットマーカー（size: 0.6）リストを生成します。
    同日重複時は🟡決算日マーカーを優先します。
    """
    markers = []

    clean_earn = str(prev_earnings_date_str).strip()[:10] if prev_earnings_date_str and not pd.isna(prev_earnings_date_str) else ""
    clean_div = str(prev_dividend_date_str).strip()[:10] if prev_dividend_date_str and not pd.isna(prev_dividend_date_str) else ""

    # 1. 過去の決算日マーカー（🟡黄色ドット）
    if clean_earn and clean_earn != "":
        markers.append({
            "time": clean_earn,
            "position": "belowBar",
            "color": "#ffd600",
            "shape": "circle",
            "size": 0.6
        })

    # 2. 過去の配当権利落ち日マーカー（🔵水色ドット）
    # ※決算日と同日の場合は決算日を優先しスキップ
    if clean_div and clean_div != "" and clean_div != clean_earn:
        markers.append({
            "time": clean_div,
            "position": "belowBar",
            "color": "#00e5ff",
            "shape": "circle",
            "size": 0.6
        })

    return markers