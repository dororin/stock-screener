# views/sector_rotation.py

import os
import time
import streamlit as st
import pandas as pd
import numpy as np

from config import settings
from data_access.local_db import get_price_data_cached
from data_access.sheets_api import (
    load_watchlist_from_sheets,
    save_watchlist_to_sheets,
    load_sector_master_from_sheets,
    load_events_from_sheets
)
from core.calculator import (
    prepare_candle_indicators,
    extract_bb_dict,
    get_sector_momentum,
    relativize_series,
    get_sector_index_cached,
    get_theme_return_rate_cached,
    get_sector_absolute_data_cached,
    get_macro_cores_cached,
    get_benchmark_data_cached
)
from core.event_collector import (
    get_earnings_countdown_badge,
    build_event_markers
)
from core.screener import get_jpx_full_list
from utils.ui_components import (
    card_container, 
    render_card_header, 
    build_wvf_badge_html
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
    events_map = load_events_from_sheets()
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

            with grid_cols[ci]:
                df_stock = pd.DataFrame()
                if not db_df.empty and "ticker" in db_df.columns:
                    mask = db_df["ticker"] == clean_code
                    if mask.any():
                        df_stock = db_df[mask].copy().sort_values("date").reset_index(drop=True)

                df_calc, s_mom, wvf_summary = prepare_candle_indicators(df_stock, resample_weekly=resample_weekly)
                
                # 指定期間（period_days）でスライス
                df_display = df_calc[df_calc["date"] >= display_start].copy().reset_index(drop=True) if not df_calc.empty else pd.DataFrame()
                
                # 💡 スライス後のdf_displayからbb_dictを生成することで、X軸の期間が完全に一致！
                bb_dict = extract_bb_dict(df_display)

                wvf_badge_html = build_wvf_badge_html(wvf_summary)
                ev_info = (events_map or {}).get(clean_code, {})
                earnings_badge_html = get_earnings_countdown_badge(ev_info.get("next_earnings", ""))
                event_markers = build_event_markers(ev_info.get("prev_earnings", ""), ev_info.get("prev_dividend", ""))

                badge_items = [b for b in [wvf_badge_html, earnings_badge_html] if b]
                badges_combined = "&nbsp;&nbsp;".join(badge_items)

                with card_container(border=True):
                    render_card_header(
                        title=display_label,
                        code=clean_code,
                        is_jp=is_jp,
                        mom_value=s_mom,
                        badge_html=badges_combined
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
                            bb_dict=bb_dict,
                            event_markers=event_markers
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

                with card_container(border=True):
                    # 💡 ヘッダー：左側に「展開ボタン付きタイトル」、右側に「騰落率 ＆ 非表示ボタン」を整然と配置
                    h_col1, h_col2, h_col3 = st.columns([3.0, 1.2, 0.8])

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
                        color_e = "#26a69a" if etf_mom >= 0 else "#ef5350"

                        with h_col1:
                            if st.button(f"{badge_e} {code} {name} 🔍", key=f"btn_dlg_etf_{code}", help="クリックして構成銘柄のミニチャート一覧を展開します", use_container_width=True):
                                show_constituents_dialog(
                                    title=f"{code} {name}",
                                    constituent_codes=constituent_codes,
                                    interval=interval,
                                    period_days=period_days,
                                    resample_weekly=resample_weekly,
                                    is_jp=is_jp
                                )
                        with h_col2:
                            st.markdown(f"<div style='text-align:right; font-size:0.84rem; font-weight:bold; color:{color_e}; padding-top:4px;'>{etf_mom:+.2f}%</div>", unsafe_allow_html=True)
                        with h_col3:
                            st.button("非表示", key=f"vis_btn_{code}", use_container_width=True, on_click=toggle_etf_visibility, args=(code,))

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
                        with h_col1:
                            st.markdown(f"<div style='font-size:0.85rem; color:#9e9e9e; padding-top:4px;'>{code} {name} (非表示)</div>", unsafe_allow_html=True)
                        with h_col3:
                            st.button("表示", key=f"vis_btn_{code}", use_container_width=True, on_click=toggle_etf_visibility, args=(code,))

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
                    with card_container(border=True):
                        h_col1, h_col2, h_col3 = st.columns([3.0, 1.2, 0.8])

                        if visible:
                            ret_rate, sma75, sma200, total_val = get_theme_return_rate_cached(
                                interval, tuple(tickers), period_days, resample_weekly, is_jp=is_jp
                            )
                            sma25 = ret_rate.rolling(window=25, min_periods=1).mean() if not ret_rate.empty else pd.Series(dtype=float)
                            last_ret = ret_rate.iloc[-1] if not ret_rate.empty else 0.0
                            badge_t = "🟢" if last_ret >= 0 else "🔴"
                            color_t = "#26a69a" if last_ret >= 0 else "#ef5350"

                            with h_col1:
                                if st.button(f"{badge_t} {t_name} 🔍", key=f"btn_dlg_theme_{t_name}", help="クリックして構成銘柄のミニチャート一覧を展開します", use_container_width=True):
                                    show_constituents_dialog(
                                        title=t_name,
                                        constituent_codes=tickers,
                                        interval=interval,
                                        period_days=period_days,
                                        resample_weekly=resample_weekly,
                                        is_jp=is_jp
                                    )
                            with h_col2:
                                st.markdown(f"<div style='text-align:right; font-size:0.84rem; font-weight:bold; color:{color_t}; padding-top:4px;'>{last_ret:+.2f}%</div>", unsafe_allow_html=True)
                            with h_col3:
                                st.button("非表示", key=f"theme_btn_{t_name}", use_container_width=True, on_click=toggle_theme_visibility, args=(t_name,))

                            if not ret_rate.empty:
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
                            with h_col1:
                                st.markdown(f"<div style='font-size:0.85rem; color:#9e9e9e; padding-top:4px;'>{t_name} (非表示)</div>", unsafe_allow_html=True)
                            with h_col3:
                                st.button("表示", key=f"theme_btn_{t_name}", use_container_width=True, on_click=toggle_theme_visibility, args=(t_name,))

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
                    with card_container(border=True):
                        wvf_badge = " 🔥" if wvf_active else ""
                        c_h1, c_h2 = st.columns([3.5, 1.5])
                        with c_h1:
                            if st.button(f"{sname}{wvf_badge} 🔍", key=f"btn_dlg_us_{sname}", use_container_width=True):
                                show_constituents_dialog(
                                    title=sname,
                                    constituent_codes=tickers,
                                    interval=interval,
                                    period_days=period_days,
                                    resample_weekly=resample_weekly,
                                    is_jp=is_jp
                                )
                        with c_h2:
                            color_u = "#26a69a" if mom >= 0 else "#ef5350"
                            st.markdown(f"<div style='text-align:right; font-size:0.84rem; font-weight:bold; color:{color_u}; padding-top:4px;'>{mom:+.2f}%</div>", unsafe_allow_html=True)

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

            def remove_item(c=code):
                del st.session_state[CUSTOM_SECTOR_KEY][c]
                save_watchlist_to_sheets(st.session_state[CUSTOM_SECTOR_KEY])
                st.rerun(scope="fragment")

            single_series = get_sector_index_cached(interval, (code,), period_days, resample_weekly, is_jp=is_jp)
            mom_single = get_sector_momentum(single_series, days=min(5, period_days)) if not single_series.empty else 0.0

            with cols[col_i]:
                with card_container(border=True):
                    def right_act(c=code):
                        st.button("🗑️", key=f"wl_del_btn_{c}", help=f"{c}を削除", on_click=remove_item, args=(c,))

                    render_card_header(
                        title=f"{code} {name}",
                        code=code,
                        is_jp=is_jp,
                        mom_value=mom_single,
                        right_action_fn=right_act,
                        col_ratio=[3.2, 1.2, 0.6]
                    )

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