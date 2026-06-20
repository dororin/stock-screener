# views/sector_rotation.py
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta

from config import settings
from data_access.local_db import load_price_db
from data_access.sheets_api import (
    load_watchlist_from_sheets,
    save_watchlist_to_sheets,
    load_sector_master_from_sheets
)
from core.screener import get_jpx_full_list
from core.calculator import (
    compute_macro_cores_from_db,
    get_sector_momentum,
    compute_sector_index_from_df,
    relativize_series,
    compute_sector_absolute_data,
    get_benchmark_data,
    compute_theme_equal_weighted_return_rate
)
from utils.plotting import (
    render_lwc_rs_overlay,
    render_lwc_sector_mini
)

CUSTOM_SECTOR_KEY = "custom_sector_tickers"

@st.cache_data(ttl=300)
def load_unified_db(interval: str, is_jp: bool = True) -> pd.DataFrame:
    """データベースから該当するParquetデータを取得します（簡易セッションキャッシュ付き）。"""
    try:
        return load_price_db(interval, is_jp=is_jp)
    except FileNotFoundError as e:
        st.warning(str(e))
        return pd.DataFrame()

@st.fragment
def _watchlist_ui():
    """部分レンダリング（st.fragment）を利用した、ウォッチリストの個別登録・削除UIです [1]。"""
    st.divider()
    st.subheader("📌 ウォッチリスト")

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
                    st.rerun(scope="app")
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
                        st.rerun(scope="app")
                    else:
                        st.caption(f"✅ {sel_code} はすでに登録済みです")
            else:
                st.caption(f"「{q}」の候補なし（TOPIX500内で検索中）")
                if q.isdigit():
                    if st.button(f"➕ {q} をコードとして追加", key="btn_add_direct", use_container_width=True):
                        st.session_state[CUSTOM_SECTOR_KEY][q] = q
                        save_watchlist_to_sheets(st.session_state[CUSTOM_SECTOR_KEY])
                        st.rerun(scope="app")
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
            st.rerun(scope="app")
    else:
        st.caption("まだ銘柄が登録されていません")

# =====================================================================
# 🔄 画面構築処理
# =====================================================================

st.title("🔄 セクターローテーション分析")

# セッション状態にウォッチリストがなければSheetsから読み込み
if CUSTOM_SECTOR_KEY not in st.session_state:
    st.session_state[CUSTOM_SECTOR_KEY] = load_watchlist_from_sheets()

# サイドバーによる設定制御
with st.sidebar:
    st.subheader("⚙️ 表示設定")
    market_mode = st.radio("マーケット", ["日本株 🇯🇵", "米国株 🇺🇸"], horizontal=True)
    is_jp = (market_mode == "日本株 🇯🇵")

    period_label = st.radio("表示期間", ["1ヶ月", "3ヶ月", "6ヶ月", "1年", "全期間"], index=1, horizontal=True)
    period_map = {"1ヶ月": 30, "3ヶ月": 90, "6ヶ月": 180, "1年": 365, "全期間": 9999}
    period_days = period_map[period_label]

    tf_label = st.radio("時間足", ["日足", "週足", "1時間足"], horizontal=True)
    interval_map = {"日足": "1d", "週足": "1d", "1時間足": "60m"}
    interval = interval_map[tf_label]
    resample_weekly = (tf_label == "週足")

    benchmarks = settings.JP_BENCHMARKS if is_jp else settings.US_BENCHMARKS
    bm_label = st.selectbox("相対強度の基準", list(benchmarks.keys()))
    bm_ticker = benchmarks[bm_label]

    st.divider()
    if is_jp:
        overlay_target = st.radio(
            "重ね書きチャートの対象", 
            ["5大マクロ・コア", "17業種 (個別)", "厳選テーマ (シートA)"],
            index=0,
            help="上部の重ね書きチャートに表示するターゲットを切り替えます。"
        )
    else:
        overlay_target = None

    st.divider()
    n_cols = st.slider("グリッド列数", 2, 4, 3)

    # ウォッチリストUI呼び出し
    _watchlist_ui()

# データベースのロード
db_df = load_unified_db(interval, is_jp=is_jp)
if db_df.empty:
    st.info("💡 データベースがまだ作成されていません。「データ管理・保守」画面で差分ダウンロードを実行してください。")
    st.stop()

# ベンチマーク基準系列の取得
bm_series = get_benchmark_data(bm_ticker, period_days, interval) if bm_ticker else None

# =========================================================================
# 🇯🇵 日本株モード
# =========================================================================
if is_jp:
    # (中略: Layer 1 マクロコア、相対強度重ね合わせ比較チャート の描画処理の後)

    st.divider()

    # ─── 中段: 業種・厳選テーマの排他切り替え表示 ───
    st.markdown("### 📂 ミニチャート分析（切り替え表示）")
    
    # メイン表示を排他選択するラジオボタン
    view_mode = st.radio(
        "表示データの種類を選択してください",
        ["📊 17業種ETF（絶対価格表示）", "📈 厳選テーマ (シートA)（オリジナル指数リターン率%表示）"],
        horizontal=True,
        key="jp_view_mode_selector"
    )

    # 1. 17業種ETF の表示ブロック
    if view_mode == "📊 17業種ETF（絶対価格表示）":
        st.caption(f"TOPIX-17構成ETFの絶対価格推移を表示しています。（表示期間: {period_label} / {tf_label}）")
        
        TOPIX17_TO_JP_SECTOR = {
            "1617": "食品", "1618": "エネルギー", "1619": "建設・インフラ",
            "1621": "医薬品", "1622": "自動車", "1625": "電気機器",
            "1626": "通信", "1629": "商社", "1630": "小売",
            "1631": "銀行", "1632": "保険", "1633": "不動産",
        }
        all_etf_codes = list(settings.TOPIX17_NAMES.keys())

        # トグル制御用の状態をセッションに仕込む
        for _code in all_etf_codes:
            if f"etf_visible_{_code}" not in st.session_state:
                st.session_state[f"etf_visible_{_code}"] = True

        def toggle_etf_visibility(code):
            st.session_state[f"etf_visible_{code}"] = not st.session_state[f"etf_visible_{code}"]

        @st.fragment
        def render_etf_card_fragment(code, name):
            visible = st.session_state[f"etf_visible_{code}"]
            with st.container(border=True):
                hc1, hc2 = st.columns([5, 1])
                vis_label = "表示" if not visible else "非表示"
                hc2.button(vis_label, key=f"vis_{code}", use_container_width=True, on_click=toggle_etf_visibility, args=(code,))

                if visible:
                    try:
                        etf_abs, etf_sma75, etf_sma200, etf_wvf, etf_vol = compute_sector_absolute_data(
                            db_df, [code], period_days, resample_weekly
                        )
                    except Exception:
                        etf_abs = pd.Series(dtype=float)
                        etf_sma75 = etf_sma200 = etf_wvf = etf_vol = pd.Series(dtype=float)

                    etf_mom = get_sector_momentum(
                        compute_sector_index_from_df(db_df, [code], period_days, resample_weekly),
                        days=min(5, period_days)
                    )
                    badge_e = "🟢" if etf_mom >= 0 else "🔴"
                    color_e = "#26a69a" if etf_mom >= 0 else "#ef5350"

                    hc1.markdown(
                        f"<span style='font-size:0.9rem; font-weight:600; color:{color_e}'>"
                        f"{badge_e} {code} {name}</span>"
                        f"<span style='font-size:0.8rem; color:{color_e}; margin-left:6px;'>{etf_mom:+.2f}%</span>",
                        unsafe_allow_html=True
                    )

                    if not etf_abs.empty:
                        render_lwc_sector_mini(
                            etf_abs, sma_fast=etf_sma75, sma_slow=etf_sma200,
                            wvf_lit=etf_wvf, volume_series=etf_vol,
                            key=f"etf_abs_mini_{code}", height=150
                        )
                    else:
                        st.caption("データなし")

                    jp_sector_name = TOPIX17_TO_JP_SECTOR.get(code)
                    constituent_codes = settings.JP_SECTORS.get(jp_sector_name, []) if jp_sector_name else []
                    if not constituent_codes:
                        sectors_loaded = load_sector_master_from_sheets(True)
                        constituent_codes = sectors_loaded.get(jp_sector_name, []) if jp_sector_name else []

                    if constituent_codes:
                        with st.popover(f"🔍 構成{len(constituent_codes)}銘柄の一覧", use_container_width=True):
                            st.markdown(
                                f"<div style='border-left: 3px solid #42a5f5; padding-left: 10px; "
                                f"margin: 4px 0 12px; font-size:0.95rem; font-weight:600; color:#42a5f5;'>"
                                f"↳ {code} {name} の構成銘柄一覧（マウスで右下を引き伸ばせます）</div>",
                                unsafe_allow_html=True
                            )

                            p_cols = st.columns(5)
                            for s_idx, stock_code in enumerate(constituent_codes):
                                col_to_use = p_cols[s_idx % 5]
                                with col_to_use:
                                    try:
                                        s_abs, s_sma75, s_sma200, s_wvf, s_vol = compute_sector_absolute_data(
                                            db_df, [stock_code], period_days, resample_weekly
                                        )
                                    except Exception:
                                        s_abs = pd.Series(dtype=float)
                                        s_sma75 = s_sma200 = s_wvf = s_vol = pd.Series(dtype=float)

                                    s_mom = get_sector_momentum(
                                        compute_sector_index_from_df(db_df, [stock_code], period_days, resample_weekly),
                                        days=min(5, period_days)
                                    )
                                    s_badge = "🟢" if s_mom >= 3.0 else "🔴" if s_mom <= -3.0 else "⚪"
                                    s_color = "#26a69a" if s_mom >= 3.0 else "#ef5350" if s_mom <= -3.0 else "#9e9e9e"

                                    with st.container(border=True):
                                        st.markdown(
                                            f"<div style='font-size:0.78rem; font-weight:600; color:{s_color};'>"
                                            f"{s_badge} {stock_code}</div>"
                                            f"<div style='font-size:0.75rem; color:#9e9e9e;'>{s_mom:+.2f}%</div>",
                                            unsafe_allow_html=True
                                        )
                                        if not s_abs.empty:
                                            render_lwc_sector_mini(
                                                s_abs, sma_fast=s_sma75, sma_slow=s_sma200,
                                                wvf_lit=s_wvf, volume_series=s_vol,
                                                key=f"stock_pop_mini_{code}_{stock_code}", height=140
                                            )
                                        else:
                                            st.caption("データなし")
                else:
                    hc1.markdown(f"<span style='font-size:0.85rem; color:#9e9e9e;'>{code} {name}</span>", unsafe_allow_html=True)

        ETF_GRID_COLS = n_cols
        rows_17 = [all_etf_codes[i:i + ETF_GRID_COLS] for i in range(0, len(all_etf_codes), ETF_GRID_COLS)]
        for row_codes in rows_17:
            row_cols = st.columns(ETF_GRID_COLS)
            for ci, code in enumerate(row_codes):
                name = settings.TOPIX17_NAMES.get(code, code)
                with row_cols[ci]:
                    render_etf_card_fragment(code, name)

    # 2. 厳選テーマ (シートA) の表示ブロック
    else:
        st.caption(f"スプレッドシート定義の厳選テーマを「等金額規格化」したリターン率（％）推移です。（期首＝0.0%基準）")
        
        sectors_loaded = load_sector_master_from_sheets(is_jp=True)
        if not sectors_loaded:
            st.info("厳選テーマ（シートA）のデータがスプレッドシートから読み取れませんでした。")
        else:
            theme_names = list(sectors_loaded.keys())

            # トグル制御用の状態をセッションに仕込む
            for t_name in theme_names:
                if f"theme_visible_{t_name}" not in st.session_state:
                    st.session_state[f"theme_visible_{t_name}"] = True

            def toggle_theme_visibility(t_name):
                st.session_state[f"theme_visible_{t_name}"] = not st.session_state[f"theme_visible_{t_name}"]

            @st.fragment
            def render_theme_card_fragment(t_name, tickers):
                visible = st.session_state[f"theme_visible_{t_name}"]
                with st.container(border=True):
                    hc1, hc2 = st.columns([5, 1])
                    vis_label = "表示" if not visible else "非表示"
                    hc2.button(vis_label, key=f"theme_vis_{t_name}", use_container_width=True, on_click=toggle_theme_visibility, args=(t_name,))

                    if visible:
                        # 新規作成した値がさ株対応の等金額規格化リターン算出
                        ret_rate, sma75, sma200, total_val = compute_theme_equal_weighted_return_rate(
                            db_df, tickers, period_days, resample_weekly
                        )

                        if not ret_rate.empty:
                            last_ret = ret_rate.iloc[-1]
                            badge_t = "🟢" if last_ret >= 0 else "🔴"
                            color_t = "#26a69a" if last_ret >= 0 else "#ef5350"

                            hc1.markdown(
                                f"<span style='font-size:0.9rem; font-weight:600; color:{color_t}'>"
                                f"{badge_t} {t_name}</span>"
                                f"<span style='font-size:0.8rem; color:{color_t}; margin-left:6px;'>リターン: {last_ret:+.2f}%</span>",
                                unsafe_allow_html=True
                            )

                            # LWCにリターン率%（折れ線）、SMA、売買代金（ボリュームヒストグラム）を渡し描画
                            render_lwc_sector_mini(
                                ret_rate, sma_fast=sma75, sma_slow=sma200,
                                wvf_lit=None, volume_series=total_val,  # 規格化指数のためWVF（買われすぎシグナル）はNone
                                key=f"theme_ret_mini_{t_name}", height=150
                            )
                        else:
                            st.caption("データなし")
                    else:
                        hc1.markdown(f"<span style='font-size:0.85rem; color:#9e9e9e;'>{t_name} (非表示)</span>", unsafe_allow_html=True)

            THEME_GRID_COLS = n_cols
            rows_theme = [theme_names[i:i + THEME_GRID_COLS] for i in range(0, len(theme_names), THEME_GRID_COLS)]
            for row_themes in rows_theme:
                row_cols = st.columns(THEME_GRID_COLS)
                for ci, t_name in enumerate(row_themes):
                    tickers = sectors_loaded[t_name]
                    with row_cols[ci]:
                        render_theme_card_fragment(t_name, tickers)

# =========================================================================
# 🇺🇸 米国株モード
# =========================================================================
else:
    with st.spinner("セクター構成をスプレッドシートから読み込み中..."):
        sectors = load_sector_master_from_sheets(is_jp)

    sector_index_cache = {}
    momentum_scores = {}
    for sname, tickers in sectors.items():
        idx_series = compute_sector_index_from_df(db_df, tickers, period_days, resample_weekly)
        if not idx_series.empty:
            plot_series = relativize_series(idx_series, bm_series)
            sector_index_cache[sname] = plot_series
            momentum_scores[sname] = get_sector_momentum(plot_series, days=min(5, period_days))

    if momentum_scores:
        sorted_sectors = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
        st.markdown("### 📊 モメンタムランキング（直近5日）")
        rank_cols = st.columns(6)
        for i, (sname, mom) in enumerate(sorted_sectors[:3]):
            with rank_cols[i]: 
                st.metric(f"🟢 #{i+1}", sname, f"{mom:+.2f}%")
        for i, (sname, mom) in enumerate(sorted_sectors[-3:]):
            with rank_cols[i+3]: 
                st.metric(f"🔴 #{len(sorted_sectors)-2+i}", sname, f"{mom:+.2f}%")
        st.divider()

    if sector_index_cache:
        st.markdown("### 📊 セクター相対強度（RS）重ね合わせ比較（リベース表示）")
        all_sector_names = list(sector_index_cache.keys())
        selected_sectors = st.multiselect(
            "表示するセクターを選択",
            options=all_sector_names,
            default=all_sector_names[:min(6, len(all_sector_names))],
            key="rs_overlay_multiselect"
        )
        render_lwc_rs_overlay(
            sector_index_cache=sector_index_cache,
            selected_sectors=selected_sectors,
            height=450,
            key="rs_overlay_lwc"
        )
        st.divider()

    st.markdown(f"### 📈 セクターミニチャート（{period_label} / {tf_label}）")
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
                sec_abs, sma75, sma200, is_wvf_lit, trading_val = compute_sector_absolute_data(db_df, tickers, period_days, resample_weekly)
                wvf_active = bool(is_wvf_lit.iloc[-1]) if (is_wvf_lit is not None and not is_wvf_lit.empty) else False
            except Exception:
                sec_abs, sma75, sma200, is_wvf_lit, trading_val = pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=bool), pd.Series(dtype=float)
                wvf_active = False

            with cols[col_i]:
                with st.container(border=True):
                    hc1, hc2 = st.columns([3, 1])
                    wvf_badge = " <span style='color:#ef5350;font-weight:bold;'>🔥 押し目</span>" if wvf_active else ""
                    hc1.markdown(f"<span style='font-weight:600;color:{color_theme}'>{badge} {sname}</span>{wvf_badge}", unsafe_allow_html=True)
                    hc2.metric("", f"{mom:+.2f}%", label_visibility="collapsed")

                    if not sec_abs.empty:
                        render_lwc_sector_mini(
                            sec_abs, sma_fast=sma75, sma_slow=sma200,
                            wvf_lit=is_wvf_lit, volume_series=trading_val,
                            key=f"mini_{sname}", height=160
                        )
                    else:
                        st.caption("データなし")

# =========================================================================
# 📌 共通ウォッチリスト表示処理
# =====================================================================
custom_tickers = st.session_state.get(CUSTOM_SECTOR_KEY, {})
if custom_tickers:
    st.divider()
    st.markdown("### 📌 ウォッチリスト（個別銘柄）")

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

            single_series = compute_sector_index_from_df(db_df, [code], period_days, resample_weekly)
            single_series = relativize_series(single_series, bm_series)
            mom_single = get_sector_momentum(single_series, days=min(5, period_days)) if not single_series.empty else 0.0
            badge = "🟢" if mom_single >= 3.0 else "🔴" if mom_single <= -3.0 else "⚪"
            color_theme = "#26a69a" if mom_single >= 3.0 else "#ef5350" if mom_single <= -3.0 else "#9e9e9e"

            with cols[col_i]:
                with st.container(border=True):
                    hc1, hc2, hc3 = st.columns([3, 1, 1])
                    hc1.markdown(f"<span style='font-weight:600;color:{color_theme}'>{badge} {code} {name}</span>", unsafe_allow_html=True)
                    hc2.metric("", f"{mom_single:+.2f}%", label_visibility="collapsed")
                    if hc3.button("🗑️", key=f"watchlist_del_{code}", help=f"{code}を削除"):
                        del st.session_state[CUSTOM_SECTOR_KEY][code]
                        save_watchlist_to_sheets(st.session_state[CUSTOM_SECTOR_KEY])
                        st.rerun()

                    try:
                        w_abs, w_sma75, w_sma200, w_wvf_lit, w_trading_val = compute_sector_absolute_data(db_df, [code], period_days, resample_weekly)
                    except Exception:
                        w_abs, w_sma75, w_sma200, w_wvf_lit, w_trading_val = pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=bool), pd.Series(dtype=float)
                    
                    if not w_abs.empty:
                        render_lwc_sector_mini(
                            w_abs, sma_fast=w_sma75, sma_slow=w_sma200,
                            wvf_lit=w_wvf_lit, volume_series=w_trading_val,
                            key=f"watch_mini_{code}", height=160
                        )
                    else:
                        st.caption("データなし")