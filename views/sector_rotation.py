# views/sector_rotation.py

import os
import time
import re
import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from config import settings
from data_access.local_db import get_price_data_cached
from data_access.sheets_api import (
    load_watchlist_from_sheets,
    save_watchlist_to_sheets,
    load_sector_master_from_sheets
)
from core.screener import get_jpx_full_list
from core.calculator import (
    get_sector_momentum,
    relativize_series,
    get_sector_index_cached,
    get_theme_return_rate_cached,
    get_sector_absolute_data_cached,
    get_macro_cores_cached,
    get_benchmark_data_cached,
    compute_wvf_signals
)
from utils.plotting import (
    render_lwc_rs_overlay,
    render_lwc_sector_mini,
    render_lwc_candle_mini
)

CUSTOM_SECTOR_KEY = "custom_sector_tickers"

if CUSTOM_SECTOR_KEY not in st.session_state:
    st.session_state[CUSTOM_SECTOR_KEY] = load_watchlist_from_sheets()


# =====================================================================
# 🏷️ 【東証全銘柄・日本語社名マスタ】
# =====================================================================
@st.cache_data(ttl=86400)
def get_all_stock_names_map(is_jp: bool = True) -> dict:
    name_map = {}
    if not is_jp:
        return name_map

    try:
        from data_access.sheets_api import get_sector_spreadsheet
        sh = get_sector_spreadsheet()
        if sh:
            ws = sh.worksheet("sector_JP")
            all_vals = ws.get_all_values()
            if all_vals and len(all_vals) > 1:
                headers = [str(h).strip() for h in all_vals[0]]
                code_idx = next((i for i, h in enumerate(headers) if h in ["銘柄コード", "code", "ticker", "コード"]), -1)
                memo_idx = next((i for i, h in enumerate(headers) if h in ["備考", "銘柄名", "name", "memo"]), -1)
                if code_idx != -1 and memo_idx != -1:
                    for row in all_vals[1:]:
                        if len(row) > max(code_idx, memo_idx):
                            c = str(row[code_idx]).strip().split(".")[0].upper()
                            m = str(row[memo_idx]).strip()
                            if c and m:
                                name_map[c] = m
    except Exception:
        pass

    try:
        jpx_path = os.path.join(settings.DRIVE_DIR, "jpx_stock_list_raw.xls")
        need_download = not os.path.exists(jpx_path)
        if not need_download:
            file_age_days = (time.time() - os.path.getmtime(jpx_path)) / 86400.0
            if file_age_days > 7.0:
                need_download = True

        if need_download:
            try:
                import requests
                headers = {"User-Agent": "Mozilla/5.0"}
                resp = requests.get(settings.JPX_URL, headers=headers, timeout=10)
                if resp.status_code == 200 and len(resp.content) > 10000:
                    with open(jpx_path, "wb") as f:
                        f.write(resp.content)
            except Exception:
                pass

        if os.path.exists(jpx_path):
            df_full = pd.read_excel(jpx_path)
            if df_full.shape[1] >= 3:
                for _, r in df_full.iterrows():
                    code_raw = str(r.iloc[1]).strip()
                    if code_raw.endswith(".0"):
                        code_raw = code_raw[:-2]
                    code_clean = code_raw.upper()
                    name_raw = str(r.iloc[2]).strip()
                    if code_clean and name_raw and code_clean not in ["CODE", "コード", "SYMBOL", "証券コード"]:
                        name_map[code_clean] = name_raw
    except Exception:
        pass

    return name_map


# =====================================================================
# 🪟 【共通モーダルダイアログ】個別株ローソク足ミニチャート一覧展開
# =====================================================================
@st.dialog("📊 構成銘柄ミニチャート一覧", width="large")
def show_constituents_dialog(
    title: str,
    constituent_codes: list,
    interval: str,
    period_days: int,
    resample_weekly: bool,
    is_jp: bool = True
):
    st.subheader(f"📊 {title}（構成: {len(constituent_codes)} 銘柄）")
    tf_display_name = "週足" if resample_weekly else ("日足" if interval == "1d" else interval)
    st.caption(f"足種: {tf_display_name} ｜ 表示期間: {period_days}日")

    if not constituent_codes:
        st.info("構成銘柄が登録されていません。")
        return

    name_map = get_all_stock_names_map(is_jp)
    db_df = get_price_data_cached(interval, limit_days=period_days + 365, is_jp=is_jp)
    display_start = pd.Timestamp.now() - pd.Timedelta(days=period_days)

    cols_per_row = 3
    rows = [constituent_codes[i:i + cols_per_row] for i in range(0, len(constituent_codes), cols_per_row)]

    for row_codes in rows:
        grid_cols = st.columns(cols_per_row)
        for ci, stock_code in enumerate(row_codes):
            clean_code = str(stock_code).strip().upper()
            stock_name = name_map.get(clean_code, "")
            display_label = f"{clean_code}　{stock_name}" if stock_name else clean_code

            if is_jp:
                tv_url = f"https://jp.tradingview.com/chart/?symbol=TSE%3A{clean_code}"
            else:
                tv_url = f"https://jp.tradingview.com/chart/?symbol={clean_code}"

            with grid_cols[ci]:
                df_stock = pd.DataFrame()
                if not db_df.empty and "ticker" in db_df.columns:
                    mask = db_df["ticker"] == clean_code
                    if mask.any():
                        df_stock = db_df[mask].copy().sort_values("date").reset_index(drop=True)

                s_mom = 0.0
                df_display = pd.DataFrame()
                wvf_badge_html = ""
                bb_dict = None

                if not df_stock.empty and len(df_stock) >= 2:
                    df_stock = compute_wvf_signals(df_stock)

                    if resample_weekly:
                        df_stock = df_stock.set_index("date").resample("W-FRI").agg({
                            "open": "first", "high": "max", "low": "min",
                            "close": "last", "volume": "sum", "ticker": "last",
                            "is_lime": "any", "is_fuchsia": "any", "is_normal_off": "any", "ext_price": "last"
                        }).dropna().reset_index()

                    df_stock["sma25"]  = df_stock["close"].rolling(window=25, min_periods=1).mean()
                    df_stock["sma75"]  = df_stock["close"].rolling(window=75, min_periods=1).mean()
                    df_stock["sma200"] = df_stock["close"].rolling(window=200, min_periods=1).mean()

                    bb_mid = df_stock["close"].rolling(window=20, min_periods=1).mean()
                    bb_std = df_stock["close"].rolling(window=20, min_periods=1).std(ddof=0)
                    df_stock["bb_p2"] = bb_mid + (2.0 * bb_std)
                    df_stock["bb_m2"] = bb_mid - (2.0 * bb_std)
                    df_stock["bb_p3"] = bb_mid + (3.0 * bb_std)
                    df_stock["bb_m3"] = bb_mid - (3.0 * bb_std)

                    recent_closes = df_stock["close"].tail(min(5, len(df_stock))).values
                    if len(recent_closes) >= 2 and recent_closes[0] > 0:
                        s_mom = float((recent_closes[-1] / recent_closes[0] - 1) * 100)

                    # 💡 WVFシグナル状態判定（最新日のみ反応、消灯目安は最新日点灯時のみ表示）
                    latest_row = df_stock.iloc[-1]
                    ext_price_val = latest_row.get("ext_price", np.nan)
                    ext_str = f"¥{ext_price_val:,.1f}" if pd.notna(ext_price_val) else "-"

                    if latest_row.get("is_lime", False):
                        lime_streak = int((df_stock["is_lime"].iloc[::-1].cumprod()).sum())
                        wvf_badge_html = (
                            f"<span style='font-size:0.75rem; background:#00e676; color:#000; padding:2px 6px; border-radius:3px; font-weight:bold;'>"
                            f"🟢 点灯中({lime_streak}日目)</span> "
                            f"<span style='font-size:0.75rem; color:#b0bec5;'>翌日消灯目安: {ext_str}</span>"
                        )
                    elif latest_row.get("is_fuchsia", False):
                        # 最新1日のみ反応する反発消灯シグナル
                        wvf_badge_html = (
                            f"<span style='font-size:0.75rem; background:#37474f; color:#ffffff; border:1px solid #78909c; padding:2px 6px; border-radius:3px; font-weight:bold;'>"
                            f"⚪ 反発消灯</span>"
                        )
                    elif latest_row.get("is_normal_off", False):
                        # 最新1日のみ反応する通常消灯シグナル
                        wvf_badge_html = (
                            f"<span style='font-size:0.75rem; background:#37474f; color:#ffffff; border:1px solid #78909c; padding:2px 6px; border-radius:3px; font-weight:bold;'>"
                            f"⚪ 消灯</span>"
                        )
                    else:
                        # 平常時は消灯目安もバッジも非表示
                        wvf_badge_html = ""

                    df_display = df_stock[df_stock["date"] >= display_start].copy().reset_index(drop=True)

                    if not df_display.empty:
                        disp_indexed = df_display.set_index("date")
                        bb_dict = {
                            "p2": disp_indexed["bb_p2"],
                            "m2": disp_indexed["bb_m2"],
                            "p3": disp_indexed["bb_p3"],
                            "m3": disp_indexed["bb_m3"]
                        }

                s_badge = "🟢" if s_mom >= 3.0 else "🔴" if s_mom <= -3.0 else "⚪"
                s_color = "#26a69a" if s_mom >= 3.0 else "#ef5350" if s_mom <= -3.0 else "#9e9e9e"

                with st.container(border=True):
                    hc1, hc2 = st.columns([3.8, 1.2])
                    hc1.markdown(
                        f"<div style='font-size:0.86rem; font-weight:600; color:{s_color}; line-height:1.4; "
                        f"white-space:nowrap; overflow:hidden; text-overflow:ellipsis;' title='TradingViewで開く: {display_label}'>"
                        f"{s_badge} <a href='{tv_url}' target='_blank' rel='noopener noreferrer' "
                        f"style='color:{s_color}; text-decoration:none; border-bottom:1px dotted {s_color};'>"
                        f"{display_label}</a></div>",
                        unsafe_allow_html=True
                    )
                    hc2.markdown(
                        f"<div style='font-size:0.83rem; text-align:right; color:{s_color}; font-weight:bold; "
                        f"line-height:1.4;'>"
                        f"{s_mom:+.2f}%</div>",
                        unsafe_allow_html=True
                    )

                    st.markdown(
                        f"<div style='margin-top:2px; margin-bottom:4px; height:18px; line-height:18px; overflow:hidden;'>"
                        f"{wvf_badge_html}</div>",
                        unsafe_allow_html=True
                    )

                    if not df_display.empty and len(df_display) >= 2:
                        sma25_s = df_display.set_index("date")["sma25"]
                        sma75_s = df_display.set_index("date")["sma75"]
                        sma200_s = df_display.set_index("date")["sma200"]
                        render_lwc_candle_mini(
                            df_display,
                            sma25=sma25_s,
                            sma75=sma75_s,
                            sma200=sma200_s,
                            key=f"dlg_candle_{title}_{clean_code}",
                            height=170,
                            is_jp=is_jp,
                            wvf_df=df_display,
                            bb_dict=bb_dict
                        )
                    else:
                        st.caption("データなし")


# =====================================================================
# 📌 【フラグメント3】ウォッチリスト編集パネル
# =====================================================================
@st.fragment
def render_watchlist_editor_fragment():
    st.subheader("📌 ウォッチリスト登録・削除")
    
    search_query = st.text_input(
        "銘柄コード・名前で検索",
        placeholder="例: 7203 / トヨタ / 三菱",
        key="watch_search_input"
    )
    q = search_query.strip() if search_query else ""

    if len(q) >= 2:
        jpx_df = get_jpx_full_list()
        if jpx_df.empty:
            st.caption("⚠️ JPXリスト取得失敗。コードを直接入力してください。")
            if q.isdigit():
                if st.button(f"➕ {q} を追加", key="btn_add_direct", use_container_width=True):
                    st.session_state[CUSTOM_SECTOR_KEY][q] = q
                    save_watchlist_to_sheets(st.session_state[CUSTOM_SECTOR_KEY])
                    st.success(f"{q} を登録しました。")
                    st.rerun(scope="fragment")
        else:
            mask = (
                jpx_df["name"].str.contains(q, na=False, case=False) |
                jpx_df["symbol"].str.contains(q, na=False)
            )
            found = jpx_df[mask].head(8)
            if not found.empty:
                PLACEHOLDER = "── 選択してください ──"
                options = [PLACEHOLDER] + [
                    f"{row['symbol']}　{row['name']}" for _, row in found.iterrows()
                ]
                code_map = {
                    f"{row['symbol']}　{row['name']}": (str(row['symbol']), str(row['name']))
                    for _, row in found.iterrows()
                }
                selected = st.selectbox(
                    "候補",
                    options,
                    key="watch_search_select",
                    label_visibility="collapsed"
                )
                if selected != PLACEHOLDER:
                    sel_code, sel_name = code_map[selected]
                    if sel_code not in st.session_state[CUSTOM_SECTOR_KEY]:
                        st.session_state[CUSTOM_SECTOR_KEY][sel_code] = sel_name
                        save_watchlist_to_sheets(st.session_state[CUSTOM_SECTOR_KEY])
                        st.success(f"{sel_code} を登録しました。")
                        st.rerun(scope="fragment")
                    else:
                        st.caption(f"✅ {sel_code} はすでに登録済みです")
            else:
                st.caption(f"「{q}」の候補なし（TOPIX500内で検索中）")
                if q.isdigit():
                    if st.button(f"➕ {q} をコードとして追加", key="btn_add_direct_num", use_container_width=True):
                        st.session_state[CUSTOM_SECTOR_KEY][q] = q
                        save_watchlist_to_sheets(st.session_state[CUSTOM_SECTOR_KEY])
                        st.success(f"{q} を登録しました。")
                        st.rerun(scope="fragment")
    elif len(q) == 1:
        st.caption("もう1文字以上入力すると候補が表示されます")

    custom_tickers = st.session_state[CUSTOM_SECTOR_KEY]
    if custom_tickers:
        st.caption(f"登録済み: {len(custom_tickers)}銘柄")
        to_delete = []
        for code, name in list(custom_tickers.items()):
            col_a, col_b = st.columns([4, 1])
            col_a.markdown(f"**{code}** {name}")
            if col_b.button("🗑️", key=f"del_{code}", help=f"{code}を削除"):
                to_delete.append(code)
        
        for code in to_delete:
            del st.session_state[CUSTOM_SECTOR_KEY][code]
        if to_delete:
            save_watchlist_to_sheets(st.session_state[CUSTOM_SECTOR_KEY])
            st.success("削除しました。")
            st.rerun(scope="fragment")
    else:
        st.caption("まだ銘柄が登録されていません")


# =====================================================================
# 📊 【フラグメント1】重ね合わせ比較チャート
# =====================================================================
@st.fragment
def render_overlay_chart_fragment(is_jp: bool):
    title_col, refresh_col = st.columns([3, 1])
    with title_col:
        st.markdown("### 📊 セクター・テーマ相対強度（RS）重ね合わせ比較")
    with refresh_col:
        if st.button(
            "🔄 最新データ再読込", 
            help="ローカルParquetおよび計算キャッシュを全クリアし、最新データで画面全体を再読み込みします", 
            use_container_width=True
        ):
            from data_access.local_db import clear_local_parquet_cache
            clear_local_parquet_cache(is_jp=is_jp)
            st.cache_data.clear()
            st.toast("✅ キャッシュをクリアしました。最新データを再読み込みします。")
            st.rerun(scope="app")
    
    col_ctrl1, col_ctrl2, col_ctrl3, col_ctrl4 = st.columns(4)
    with col_ctrl1:
        period_label = st.radio("表示期間", ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "全期間"], index=1, horizontal=True, key="ov_period")
    with col_ctrl2:
        tf_label = st.radio("時間足", ["日足", "週足", "1時間足"], horizontal=True, key="ov_tf")
    with col_ctrl3:
        benchmarks = settings.JP_BENCHMARKS if is_jp else settings.US_BENCHMARKS
        bm_label = st.selectbox("相対強度の基準", list(benchmarks.keys()), key="ov_bm")
    with col_ctrl4:
        if is_jp:
            overlay_target = st.radio("重ね書き対象", ["5大マクロ・コア", "17業種 (個別)", "厳選テーマ (シートA)"], index=0, key="ov_target")
        else:
            overlay_target = None

    period_map = {"1ヶ月": 30, "3ヶ月": 90, "6ヶ月": 180, "1年": 365, "全期間": 9999}
    period_days = period_map[period_label]
    interval_map = {"日足": "1d", "週足": "1d", "1時間足": "60m"}
    interval = interval_map[tf_label]
    resample_weekly = (tf_label == "週足")
    bm_ticker = benchmarks[bm_label]

    bm_series = get_benchmark_data_cached(bm_ticker, period_days, interval, is_jp=is_jp) if bm_ticker else None

    overlay_series_cache = {}
    with st.spinner("重ね書きデータを算出中..."):
        if is_jp:
            if overlay_target == "5大マクロ・コア":
                cores = get_macro_cores_cached(interval, period_days, resample_weekly, is_jp=is_jp)
                for sname, idx_series in cores.items():
                    if not idx_series.empty:
                        overlay_series_cache[sname] = relativize_series(idx_series, bm_series)
            elif overlay_target == "17業種 (個別)":
                for code in list(settings.TOPIX17_NAMES.keys()):
                    idx_series = get_sector_index_cached(interval, (code,), period_days, resample_weekly, is_jp=is_jp)
                    if not idx_series.empty:
                        name = settings.TOPIX17_NAMES.get(code, code)
                        overlay_series_cache[name] = relativize_series(idx_series, bm_series)
            elif overlay_target == "厳選テーマ (シートA)":
                sectors_loaded = load_sector_master_from_sheets(is_jp=True)
                if sectors_loaded:
                    for t_name, tickers in sectors_loaded.items():
                        idx_series = get_sector_index_cached(interval, tuple(tickers), period_days, resample_weekly, is_jp=is_jp)
                        if not idx_series.empty:
                            overlay_series_cache[t_name] = relativize_series(idx_series, bm_series)
        else:
            sectors = load_sector_master_from_sheets(is_jp=False)
            for sname, tickers in sectors.items():
                idx_series = get_sector_index_cached(interval, tuple(tickers), period_days, resample_weekly, is_jp=is_jp)
                if not idx_series.empty:
                    overlay_series_cache[sname] = relativize_series(idx_series, bm_series)

    if overlay_series_cache:
        all_target_names = list(overlay_series_cache.keys())
        selected_targets = st.multiselect(
            "表示するターゲットを選択",
            options=all_target_names,
            default=all_target_names[:min(6, len(all_target_names))],
            key="ov_multiselect"
        )
        render_lwc_rs_overlay(
            sector_index_cache=overlay_series_cache,
            selected_sectors=selected_targets,
            height=400,
            key="ov_chart_lwc"
        )
    else:
        st.caption("表示対象のデータがありません。")


# =====================================================================
# 📈 【フラグメント2】セクターミニチャート一覧
# =====================================================================
@st.fragment
def render_sector_mini_charts_fragment(is_jp: bool):
    st.markdown("### 📈 セクター・テーマ ミニチャート")

    col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([1.5, 1.5, 1])
    with col_ctrl1:
        period_label = st.radio("表示期間", ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "全期間"], index=1, horizontal=True, key="mini_period")
    with col_ctrl2:
        tf_label = st.radio("時間足", ["日足", "週足", "1時間足"], horizontal=True, key="mini_tf")
    with col_ctrl3:
        n_cols = st.slider("グリッド列数", 2, 4, 3, key="mini_cols")

    period_map = {"1ヶ月": 30, "3ヶ月": 90, "6ヶ月": 180, "1年": 365, "全期間": 9999}
    period_days = period_map[period_label]
    interval_map = {"日足": "1d", "週足": "1d", "1時間足": "60m"}
    interval = interval_map[tf_label]
    resample_weekly = (tf_label == "週足")

    if is_jp:
        view_mode = st.radio(
            "表示データを選択",
            ["📊 17業種ETF（絶対価格表示）", "📈 厳選テーマ (シートA)（オリジナル指数リターン率%表示）"],
            horizontal=True,
            key="jp_view_mode_selector"
        )

        if view_mode == "📊 17業種ETF（絶対価格表示）":
            TOPIX17_TO_JP_SECTOR = {
                "1617": "食品", "1618": "エネルギー", "1619": "建設・インフラ",
                "1620": "素材", "1621": "医薬品", "1622": "自動車", "1623": "鉄鋼",
                "1624": "機械", "1625": "電気機器", "1626": "通信", "1627": "公益",
                "1628": "運輸", "1629": "商社", "1630": "小売", "1631": "銀行",
                "1632": "保険", "1633": "不動産",
            }
            all_etf_codes = list(settings.TOPIX17_NAMES.keys())

            for _code in all_etf_codes:
                if f"etf_visible_{_code}" not in st.session_state:
                    st.session_state[f"etf_visible_{_code}"] = True

            def toggle_etf_visibility(code):
                st.session_state[f"etf_visible_{code}"] = not st.session_state[f"etf_visible_{code}"]

            sectors_loaded = load_sector_master_from_sheets(True)

            def render_etf_card(code, name):
                visible = st.session_state[f"etf_visible_{code}"]
                
                jp_sector_name = TOPIX17_TO_JP_SECTOR.get(code)
                constituent_codes = settings.JP_SECTORS.get(jp_sector_name, []) if jp_sector_name else []
                if not constituent_codes and jp_sector_name:
                    constituent_codes = sectors_loaded.get(jp_sector_name, [])
                if not constituent_codes:
                    constituent_codes = sectors_loaded.get(name, [])

                with st.container(border=True):
                    hc1, hc2 = st.columns([5, 1])
                    vis_label = "表示" if not visible else "非表示"
                    hc2.button(vis_label, key=f"vis_btn_{code}", use_container_width=True, on_click=toggle_etf_visibility, args=(code,))

                    if visible:
                        try:
                            etf_abs, etf_sma75, etf_sma200, etf_wvf, etf_vol = get_sector_absolute_data_cached(
                                interval, (code,), period_days, resample_weekly, is_jp=is_jp
                            )
                        except Exception:
                            etf_abs = pd.Series(dtype=float)
                            etf_sma75 = etf_sma200 = etf_wvf = etf_vol = pd.Series(dtype=float)

                        etf_sma25 = etf_abs.rolling(window=25, min_periods=1).mean() if not etf_abs.empty else pd.Series(dtype=float)

                        etf_mom = get_sector_momentum(
                            get_sector_index_cached(interval, (code,), period_days, resample_weekly, is_jp=is_jp),
                            days=min(5, period_days)
                        )
                        badge_e = "🟢" if etf_mom >= 0 else "🔴"

                        btn_label = f"{badge_e} {code} {name} ({etf_mom:+.2f}%) 🔍"
                        if hc1.button(btn_label, key=f"btn_dlg_etf_{code}", help="クリックして構成銘柄のミニチャート一覧を展開します", use_container_width=True):
                            show_constituents_dialog(
                                title=f"{code} {name}",
                                constituent_codes=constituent_codes,
                                interval=interval,
                                period_days=period_days,
                                resample_weekly=resample_weekly,
                                is_jp=is_jp
                            )

                        if not etf_abs.empty:
                            render_lwc_sector_mini(
                                etf_abs, 
                                sma25=etf_sma25,
                                sma75=etf_sma75, 
                                sma200=etf_sma200,
                                wvf_lit=etf_wvf, 
                                volume_series=etf_vol,
                                key=f"etf_abs_mini_{code}", 
                                height=150, 
                                is_jp=is_jp
                            )
                        else:
                            st.caption("データなし")
                    else:
                        hc1.markdown(f"<span style='font-size:0.85rem; color:#9e9e9e;'>{code} {name}</span>", unsafe_allow_html=True)

            rows_17 = [all_etf_codes[i:i + n_cols] for i in range(0, len(all_etf_codes), n_cols)]
            for row_codes in rows_17:
                row_cols = st.columns(n_cols)
                for ci, code in enumerate(row_codes):
                    name = settings.TOPIX17_NAMES.get(code, code)
                    with row_cols[ci]:
                        render_etf_card(code, name)

        else:
            sectors_loaded = load_sector_master_from_sheets(is_jp=True)
            if not sectors_loaded:
                st.info("テーマデータが読み取れませんでした。")
            else:
                theme_names = list(sectors_loaded.keys())

                for t_name in theme_names:
                    if f"theme_visible_{t_name}" not in st.session_state:
                        st.session_state[f"theme_visible_{t_name}"] = True

                def toggle_theme_visibility(t_name):
                    st.session_state[f"theme_visible_{t_name}"] = not st.session_state[f"theme_visible_{t_name}"]

                def render_theme_card(t_name, tickers):
                    visible = st.session_state[f"theme_visible_{t_name}"]
                    with st.container(border=True):
                        hc1, hc2 = st.columns([5, 1])
                        vis_label = "表示" if not visible else "非表示"
                        hc2.button(vis_label, key=f"theme_btn_{t_name}", use_container_width=True, on_click=toggle_theme_visibility, args=(t_name,))

                        if visible:
                            ret_rate, sma75, sma200, total_val = get_theme_return_rate_cached(
                                interval, tuple(tickers), period_days, resample_weekly, is_jp=is_jp
                            )
                            sma25 = ret_rate.rolling(window=25, min_periods=1).mean() if not ret_rate.empty else pd.Series(dtype=float)

                            if not ret_rate.empty:
                                last_ret = ret_rate.iloc[-1]
                                badge_t = "🟢" if last_ret >= 0 else "🔴"

                                btn_label = f"{badge_t} {t_name} ({last_ret:+.2f}%) 🔍"
                                if hc1.button(btn_label, key=f"btn_dlg_theme_{t_name}", help="クリックして構成銘柄のミニチャート一覧を展開します", use_container_width=True):
                                    show_constituents_dialog(
                                        title=t_name,
                                        constituent_codes=tickers,
                                        interval=interval,
                                        period_days=period_days,
                                        resample_weekly=resample_weekly,
                                        is_jp=is_jp
                                    )

                                render_lwc_sector_mini(
                                    ret_rate, 
                                    sma25=sma25,
                                    sma75=sma75, 
                                    sma200=sma200,
                                    wvf_lit=None, 
                                    volume_series=total_val,
                                    key=f"theme_ret_mini_{t_name}", 
                                    height=150, 
                                    is_jp=is_jp
                                )
                            else:
                                st.caption("データなし")
                        else:
                            hc1.markdown(f"<span style='font-size:0.85rem; color:#9e9e9e;'>{t_name} (非表示)</span>", unsafe_allow_html=True)

                rows_theme = [theme_names[i:i + n_cols] for i in range(0, len(theme_names), n_cols)]
                for row_themes in rows_theme:
                    row_cols = st.columns(n_cols)
                    for ci, t_name in enumerate(row_themes):
                        tickers = sectors_loaded[t_name]
                        with row_cols[ci]:
                            render_theme_card(t_name, tickers)

    else:
        sectors = load_sector_master_from_sheets(is_jp)
        
        sector_index_cache = {}
        momentum_scores = {}
        for sname, tickers in sectors.items():
            idx_series = get_sector_index_cached(interval, tuple(tickers), period_days, resample_weekly, is_jp=is_jp)
            if not idx_series.empty:
                sector_index_cache[sname] = idx_series
                momentum_scores[sname] = get_sector_momentum(idx_series, days=min(5, period_days))

        if momentum_scores:
            sorted_sectors = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
            st.markdown("#### 🏆 モメンタム順位（直近5日）")
            rank_cols = st.columns(6)
            for i, (sname, mom) in enumerate(sorted_sectors[:3]):
                with rank_cols[i]:
                    st.metric(f"🟢 #{i+1}", sname, f"{mom:+.2f}%")
            for i, (sname, mom) in enumerate(sorted_sectors[-3:]):
                with rank_cols[i+3]:
                    st.metric(f"🔴 #{len(sorted_sectors)-2+i}", sname, f"{mom:+.2f}%")

        sector_list = list(sectors.items())
        rows_needed = (len(sector_list) + n_cols - 1) // n_cols

        for row_i in range(rows_needed):
            cols = st.columns(n_cols)
            for col_i in range(n_cols):
                idx = row_i * n_cols + col_i
                if idx >= len(sector_list):
                    break
                sname, tickers = sector_list[idx]
                mom = momentum_scores.get(sname, 0.0)
                badge = "🟢" if mom >= 3.0 else "🔴" if mom <= -3.0 else "⚪"

                try:
                    sec_abs, sma75, sma200, is_wvf_lit, trading_val = get_sector_absolute_data_cached(
                        interval, tuple(tickers), period_days, resample_weekly, is_jp=is_jp
                    )
                    sec_sma25 = sec_abs.rolling(window=25, min_periods=1).mean() if not sec_abs.empty else pd.Series(dtype=float)
                    wvf_active = bool(is_wvf_lit.iloc[-1]) if (is_wvf_lit is not None and not is_wvf_lit.empty) else False
                except Exception:
                    sec_abs = sma75 = sma200 = sec_sma25 = pd.Series(dtype=float)
                    is_wvf_lit = pd.Series(dtype=bool)
                    trading_val = pd.Series(dtype=float)
                    wvf_active = False

                with cols[col_i]:
                    with st.container(border=True):
                        hc1, hc2 = st.columns([3, 1])
                        wvf_badge = " 🔥" if wvf_active else ""
                        
                        btn_label = f"{badge} {sname}{wvf_badge} 🔍"
                        if hc1.button(btn_label, key=f"btn_dlg_us_{sname}", help="クリックして構成銘柄のミニチャート一覧を展開します", use_container_width=True):
                            show_constituents_dialog(
                                title=sname,
                                constituent_codes=tickers,
                                interval=interval,
                                period_days=period_days,
                                resample_weekly=resample_weekly,
                                is_jp=is_jp
                            )
                        hc2.metric("", f"{mom:+.2f}%", label_visibility="collapsed")

                        if not sec_abs.empty:
                            render_lwc_sector_mini(
                                sec_abs, 
                                sma25=sec_sma25,
                                sma75=sma75, 
                                sma200=sma200,
                                wvf_lit=is_wvf_lit, 
                                volume_series=trading_val,
                                key=f"mini_chart_{sname}", 
                                height=150, 
                                is_jp=is_jp
                            )
                        else:
                            st.caption("データなし")


# =====================================================================
# 📌 【フラグメント4】ウォッチリスト個別ミニチャート
# =====================================================================
@st.fragment
def render_watchlist_mini_charts_fragment(is_jp: bool):
    custom_tickers = st.session_state.get(CUSTOM_SECTOR_KEY, {})
    if not custom_tickers:
        return

    st.markdown("### 📌 ウォッチリスト個別銘柄")
    
    col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([1.5, 1.5, 1])
    with col_ctrl1:
        period_label = st.radio("表示期間", ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "全期間"], index=1, horizontal=True, key="wl_period")
    with col_ctrl2:
        tf_label = st.radio("時間足", ["日足", "週足", "1時間足"], horizontal=True, key="wl_tf")
    with col_ctrl3:
        n_cols = st.slider("グリッド列数", 2, 4, 3, key="wl_cols")

    period_map = {"1ヶ月": 30, "3ヶ月": 90, "6ヶ月": 180, "1年": 365, "全期間": 9999}
    period_days = period_map[period_label]
    interval_map = {"日足": "1d", "週足": "1d", "1時間足": "60m"}
    interval = interval_map[tf_label]
    resample_weekly = (tf_label == "週足")

    custom_codes = list(custom_tickers.keys())
    custom_rows = (len(custom_codes) + n_cols - 1) // n_cols

    for row_i in range(custom_rows):
        cols = st.columns(n_cols)
        for col_i in range(n_cols):
            idx = row_i * n_cols + col_i
            if idx >= len(custom_codes):
                break
            code = custom_codes[idx]
            name = custom_tickers[code]

            def remove_item(c):
                del st.session_state[CUSTOM_SECTOR_KEY][c]
                save_watchlist_to_sheets(st.session_state[CUSTOM_SECTOR_KEY])
                st.rerun(scope="fragment")

            single_series = get_sector_index_cached(interval, (code,), period_days, resample_weekly, is_jp=is_jp)
            mom_single = get_sector_momentum(single_series, days=min(5, period_days)) if not single_series.empty else 0.0
            badge = "🟢" if mom_single >= 3.0 else "🔴" if mom_single <= -3.0 else "⚪"
            color_theme = "#26a69a" if mom_single >= 3.0 else "#ef5350" if mom_single <= -3.0 else "#9e9e9e"

            with cols[col_i]:
                with st.container(border=True):
                    hc1, hc2, hc3 = st.columns([3, 1, 1])
                    hc1.markdown(f"<span style='font-weight:600;color:{color_theme}'>{badge} {code} {name}</span>", unsafe_allow_html=True)
                    hc2.metric("", f"{mom_single:+.2f}%", label_visibility="collapsed")
                    hc3.button("🗑️", key=f"wl_del_btn_{code}", help=f"{code}を削除", on_click=remove_item, args=(code,))

                    try:
                        w_abs, w_sma75, w_sma200, w_wvf_lit, w_trading_val = get_sector_absolute_data_cached(
                            interval, (code,), period_days, resample_weekly, is_jp=is_jp
                        )
                        w_sma25 = w_abs.rolling(window=25, min_periods=1).mean() if not w_abs.empty else pd.Series(dtype=float)
                    except Exception:
                        w_abs = w_sma75 = w_sma200 = w_sma25 = pd.Series(dtype=float)
                        w_wvf_lit = pd.Series(dtype=bool)
                        w_trading_val = pd.Series(dtype=float)

                    if not w_abs.empty:
                        render_lwc_sector_mini(
                            w_abs, 
                            sma25=w_sma25,
                            sma75=w_sma75, 
                            sma200=w_sma200,
                            wvf_lit=w_wvf_lit, 
                            volume_series=w_trading_val,
                            key=f"wl_chart_mini_{code}", 
                            height=150, 
                            is_jp=is_jp
                        )
                    else:
                        st.caption("データなし")


# =====================================================================
# 🛠️ メイン画面描画制御
# =====================================================================

with st.sidebar:
    st.subheader("🌐 市場の選択")
    market_mode = st.radio("マーケット", ["日本株 🇯🇵", "米国株 🇺🇸"], horizontal=True, label_visibility="collapsed")
    is_jp = (market_mode == "日本株 🇯🇵")

sample_df = get_price_data_cached("1d", limit_days=None, is_jp=is_jp)
if sample_df.empty:
    st.warning("⚠️ データベースが見つかりません。Google Drive上にデータが存在するか確認、または「データ管理・保守」画面で構築を行ってください。")
    st.stop()

render_overlay_chart_fragment(is_jp=is_jp)

st.write("---")

render_sector_mini_charts_fragment(is_jp=is_jp)

st.write("---")

render_watchlist_editor_fragment()

render_watchlist_mini_charts_fragment(is_jp=is_jp)