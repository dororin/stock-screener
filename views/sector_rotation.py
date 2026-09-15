# sector_rotation.py

import streamlit as st
import pandas as pd
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
    get_benchmark_data_cached
)
from utils.plotting import (
    render_lwc_rs_overlay,
    render_lwc_sector_mini
)

CUSTOM_SECTOR_KEY = "custom_sector_tickers"

# セッション状態にウォッチリストがなければSheetsから読み込み
if CUSTOM_SECTOR_KEY not in st.session_state:
    st.session_state[CUSTOM_SECTOR_KEY] = load_watchlist_from_sheets()


# =====================================================================
# 🪟 【共通モーダルダイアログ】構成個別株ミニチャート一覧展開
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
    """
    テーマ名クリック時に最前面にオーバーレイ展開する共通モーダルダイアログ。
    17業種ETF、厳選テーマ、米国株セクターのすべてで共通して利用されます。
    """
    st.subheader(f"📊 {title}（構成: {len(constituent_codes)} 銘柄）")
    st.caption(f"足種: {interval} ｜ 表示期間: {period_days}日")

    if not constituent_codes:
        st.info("構成銘柄が登録されていません。")
        return

    # 日本株の場合はJPXリストから会社名辞書を生成
    name_map = {}
    if is_jp:
        try:
            jpx_df = get_jpx_full_list()
            if not jpx_df.empty:
                name_map = dict(zip(jpx_df["symbol"].astype(str), jpx_df["name"]))
        except Exception:
            name_map = {}

    cols_per_row = 3  # モーダル内は3列でゆったりと配置
    rows = [constituent_codes[i:i + cols_per_row] for i in range(0, len(constituent_codes), cols_per_row)]

    for row_codes in rows:
        grid_cols = st.columns(cols_per_row)
        for ci, stock_code in enumerate(row_codes):
            with grid_cols[ci]:
                try:
                    s_abs, s_sma75, s_sma200, s_wvf, s_vol = get_sector_absolute_data_cached(
                        interval, (stock_code,), period_days, resample_weekly, is_jp=is_jp
                    )
                except Exception:
                    s_abs = pd.Series(dtype=float)
                    s_sma75 = s_sma200 = s_wvf = s_vol = pd.Series(dtype=float)

                s_mom = get_sector_momentum(
                    get_sector_index_cached(interval, (stock_code,), period_days, resample_weekly, is_jp=is_jp),
                    days=min(5, period_days)
                )
                s_badge = "🟢" if s_mom >= 3.0 else "🔴" if s_mom <= -3.0 else "⚪"
                s_color = "#26a69a" if s_mom >= 3.0 else "#ef5350" if s_mom <= -3.0 else "#9e9e9e"

                stock_name = name_map.get(str(stock_code), "")
                display_label = f"{stock_code} {stock_name}".strip()

                with st.container(border=True):
                    hc1, hc2 = st.columns([3, 2])
                    hc1.markdown(
                        f"<div style='font-size:0.85rem; font-weight:600; color:{s_color}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;' title='{display_label}'>"
                        f"{s_badge} {display_label}</div>",
                        unsafe_allow_html=True
                    )
                    hc2.markdown(
                        f"<div style='font-size:0.8rem; text-align:right; color:{s_color}; font-weight:bold;'>"
                        f"{s_mom:+.2f}%</div>",
                        unsafe_allow_html=True
                    )

                    if not s_abs.empty:
                        render_lwc_sector_mini(
                            s_abs, sma_fast=s_sma75, sma_slow=s_sma200,
                            wvf_lit=s_wvf, volume_series=s_vol,
                            key=f"dlg_mini_{title}_{stock_code}", height=140
                        )
                    else:
                        st.caption("データなし")


# =====================================================================
# 📌 【フラグメント3】ウォッチリスト編集パネル（完全独立）
# =====================================================================
@st.fragment
def render_watchlist_editor_fragment():
    """ウォッチリストの登録・削除だけを行う独立フラグメント。上部チャートにリランを一切伝播させない。"""
    st.subheader("📌 ウォッチリスト登録・削除")
    
    # 検索・追加フォーム
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
# 📊 【フラグメント1】重ね合わせ比較チャート（完全独立）
# =====================================================================
@st.fragment
def render_overlay_chart_fragment(is_jp: bool):
    """重ね合わせ比較チャートの計算と描画をカプセル化。ウィジェット操作時に下部チャートは再計算されません。"""
    
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
# 📈 【フラグメント2】セクターミニチャート一覧（完全独立）
# =====================================================================
@st.fragment
def render_sector_mini_charts_fragment(is_jp: bool):
    """17業種ETF、厳選テーマなどのミニチャート群を描画。リラン時に上部重ね書きに影響を与えません。"""
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

    # ─── 日本株モード ───
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

            # 表示/非表示トグル状態の初期化
            for _code in all_etf_codes:
                if f"etf_visible_{_code}" not in st.session_state:
                    st.session_state[f"etf_visible_{_code}"] = True

            def toggle_etf_visibility(code):
                st.session_state[f"etf_visible_{code}"] = not st.session_state[f"etf_visible_{code}"]

            # 事前にセクター構成辞書をロード
            sectors_loaded = load_sector_master_from_sheets(True)

            def render_etf_card(code, name):
                visible = st.session_state[f"etf_visible_{code}"]
                
                # 構成銘柄の特定
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

                        etf_mom = get_sector_momentum(
                            get_sector_index_cached(interval, (code,), period_days, resample_weekly, is_jp=is_jp),
                            days=min(5, period_days)
                        )
                        badge_e = "🟢" if etf_mom >= 0 else "🔴"
                        color_e = "#26a69a" if etf_mom >= 0 else "#ef5350"

                        # 💡 テーマ名ボタン：クリックで構成銘柄のモーダルダイアログを展開
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
                                etf_abs, sma_fast=etf_sma75, sma_slow=etf_sma200,
                                wvf_lit=etf_wvf, volume_series=etf_vol,
                                key=f"etf_abs_mini_{code}", height=150
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
            # 厳選テーマ (シートA)
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
                            if not ret_rate.empty:
                                last_ret = ret_rate.iloc[-1]
                                badge_t = "🟢" if last_ret >= 0 else "🔴"
                                color_t = "#26a69a" if last_ret >= 0 else "#ef5350"

                                # 💡 テーマ名ボタン：クリックで構成銘柄のモーダルダイアログを展開
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
                                    ret_rate, sma_fast=sma75, sma_slow=sma200,
                                    wvf_lit=None, volume_series=total_val,
                                    key=f"theme_ret_mini_{t_name}", height=150
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

    # ─── 米国株モード ───
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
                color_theme = "#26a69a" if mom >= 3.0 else "#ef5350" if mom <= -3.0 else "#9e9e9e"

                try:
                    sec_abs, sma75, sma200, is_wvf_lit, trading_val = get_sector_absolute_data_cached(
                        interval, tuple(tickers), period_days, resample_weekly, is_jp=is_jp
                    )
                    wvf_active = bool(is_wvf_lit.iloc[-1]) if (is_wvf_lit is not None and not is_wvf_lit.empty) else False
                except Exception:
                    sec_abs = sma75 = sma200 = pd.Series(dtype=float)
                    is_wvf_lit = pd.Series(dtype=bool)
                    trading_val = pd.Series(dtype=float)
                    wvf_active = False

                with cols[col_i]:
                    with st.container(border=True):
                        hc1, hc2 = st.columns([3, 1])
                        wvf_badge = " 🔥" if wvf_active else ""
                        
                        # 💡 米国株セクター名ボタン：クリックで構成銘柄のモーダルダイアログを展開
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
                                sec_abs, sma_fast=sma75, sma_slow=sma200,
                                wvf_lit=is_wvf_lit, volume_series=trading_val,
                                key=f"mini_chart_{sname}", height=150
                            )
                        else:
                            st.caption("データなし")


# =====================================================================
# 📌 【フラグメント4】ウォッチリスト個別ミニチャート（完全独立）
# =====================================================================
@st.fragment
def render_watchlist_mini_charts_fragment(is_jp: bool):
    """登録されている個別銘柄のミニチャートを描画。削除トリガー時にこのエリア内のみで即時更新が完結します。"""
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
                    except Exception:
                        w_abs = w_sma75 = w_sma200 = pd.Series(dtype=float)
                        w_wvf_lit = pd.Series(dtype=bool)
                        w_trading_val = pd.Series(dtype=float)

                    if not w_abs.empty:
                        render_lwc_sector_mini(
                            w_abs, sma_fast=w_sma75, sma_slow=w_sma200,
                            wvf_lit=w_wvf_lit, volume_series=w_trading_val,
                            key=f"wl_chart_mini_{code}", height=150
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

# ── 1. 重ね合わせ比較チャートフラグメントを実行
render_overlay_chart_fragment(is_jp=is_jp)

st.write("---")

# ── 2. セクターミニチャート一覧フラグメントを実行
render_sector_mini_charts_fragment(is_jp=is_jp)

st.write("---")

# ── 3. ウォッチリスト編集パネルフラグメントを実行
render_watchlist_editor_fragment()

# ── 4. ウォッチリスト個別ミニチャート一覧フラグメントを実行
render_watchlist_mini_charts_fragment(is_jp=is_jp)