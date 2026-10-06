# utils/plotting.py

import io
import base64
import pandas as pd
import numpy as np
import streamlit as st
from streamlit_lightweight_charts import renderLightweightCharts

# =====================================================================
# 📊 Lightweight Charts (LWC) 用ヘルパー
# =====================================================================

def _to_lwc_time(dt_index) -> list:
    """DatetimeIndex/SeriesをLWCのtime文字列（YYYY-MM-DD）リストに変換します。"""
    return [str(d)[:10] for d in dt_index]

def _lwc_base_options(height: int = 160, right_offset: int = 5, has_left_scale: bool = False) -> dict:
    """LWC共通レイアウトオプションを生成します。"""
    opts = {
        "height": height,
        "layout": {
            "background": {"type": "solid", "color": "transparent"},
            "textColor": "#9e9e9e",
            "fontSize": 10,
        },
        "grid": {
            "vertLines": {"color": "rgba(128,128,128,0.12)"},
            "horzLines": {"color": "rgba(128,128,128,0.12)"},
        },
        "crosshair": {"mode": 1},
        "rightPriceScale": {
            "borderColor": "rgba(128,128,128,0.3)", 
            "scaleMargins": {
                "top": 0.08, 
                "bottom": 0.15
            },
            "visible": True,
        },
        "overlayPriceScales": {
            "scaleMargins": {
                "top": 0.75,
                "bottom": 0,
            }
        },
        "timeScale": {
            "borderColor": "rgba(128,128,128,0.3)", 
            "rightOffset": right_offset, 
            "timeVisible": True, 
            "secondsVisible": False
        },
        "handleScroll": True,
        "handleScale": True,
    }
    if has_left_scale:
        opts["leftPriceScale"] = {
            "borderColor": "rgba(128,128,128,0.3)",
            "scaleMargins": {"top": 0.08, "bottom": 0.15},
            "visible": True,
        }
    return opts

def detect_price_format(prices, is_jp: bool = True) -> dict:
    """株価データから適切な小数点桁数と刻み幅を動的に判定します。"""
    if not is_jp:
        return {"type": "price", "precision": 2, "minMove": 0.01}

    if prices is None:
        return {"type": "price", "precision": 0, "minMove": 1}

    if isinstance(prices, pd.DataFrame):
        target_cols = [c for c in ["open", "high", "low", "close"] if c in prices.columns]
        vals = prices[target_cols].values.flatten() if target_cols else prices.values.flatten()
    elif isinstance(prices, pd.Series):
        vals = prices.values
    elif isinstance(prices, (list, tuple, np.ndarray)):
        vals = np.asarray(prices)
    else:
        return {"type": "price", "precision": 0, "minMove": 1}

    vals = vals[~np.isnan(vals)]
    if len(vals) == 0:
        return {"type": "price", "precision": 0, "minMove": 1}

    if np.all(np.isclose(vals, np.round(vals, 0), atol=1e-4)):
        return {"type": "price", "precision": 0, "minMove": 1}
    if np.all(np.isclose(vals, np.round(vals, 1), atol=1e-4)):
        return {"type": "price", "precision": 1, "minMove": 0.1}

    return {"type": "price", "precision": 2, "minMove": 0.01}

def build_lwc_rs_overlay_chart(sector_index_cache: dict, selected_sectors: list, height: int = 450) -> dict:
    if not sector_index_cache or not selected_sectors:
        return {}

    PLOTLY_COLORS = [
        "#636efa", "#EF553B", "#00cc96", "#ab63fa", "#FFA15A",
        "#19d3f3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52"
    ]

    series_list = []
    for i, sname in enumerate(selected_sectors):
        series = sector_index_cache.get(sname)
        if series is None or series.empty:
            continue
        
        times = _to_lwc_time(series.index)
        line_data = [
            {"time": t, "value": round(float(v) - 100.0, 2)}
            for t, v in zip(times, series.values) if not pd.isna(v)
        ]

        color = PLOTLY_COLORS[i % len(PLOTLY_COLORS)]
        series_list.append({
            "type": "Line",
            "data": line_data,
            "options": {
                "color": color,
                "lineWidth": 2,
                "title": sname,
                "priceLineVisible": False,
                "lastValueVisible": True,
                "crosshairMarkerVisible": True,
            }
        })

    if not series_list:
        return {}

    chart_options = _lwc_base_options(height=height, right_offset=10)
    chart_options["rightPriceScale"] = {
        "borderColor": "rgba(128,128,128,0.3)",
        "scaleMargins": {"top": 0.15, "bottom": 0.15},
    }

    return {"chart": chart_options, "series": series_list}

def render_lwc_rs_overlay(sector_index_cache: dict, selected_sectors: list, height: int = 450, key: str = "rs_overlay"):
    if not sector_index_cache or not selected_sectors:
        st.info("セクターを1つ以上選択すると、RS重ね合わせチャートが表示されます。")
        return

    chart_def = build_lwc_rs_overlay_chart(sector_index_cache, selected_sectors, height=height)
    if not chart_def:
        st.caption("表示可能なデータがありません。")
        return

    PLOTLY_COLORS = [
        "#636efa", "#EF553B", "#00cc96", "#ab63fa", "#FFA15A",
        "#19d3f3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52"
    ]
    legend_items = []
    
    for i, sname in enumerate(selected_sectors):
        series = sector_index_cache.get(sname)
        if series is not None and not series.empty:
            pct = float(series.iloc[-1]) - 100.0
            sign = "+" if pct >= 0 else ""
            color = PLOTLY_COLORS[i % len(PLOTLY_COLORS)]
            legend_items.append(
                f"<span style='display: inline-block; width: 11px; height: 11px; background-color: {color}; "
                f"margin-right: 4px; vertical-align: middle; border-radius: 2px;'></span>"
                f"<span style='font-size: 0.85rem; margin-right: 15px; color: #9e9e9e;'>{sname}: "
                f"<b style='color: {'#26a69a' if pct >= 0 else '#ef5350'}'>{sign}{pct:.2f}%</b></span>"
            )
            
    legend_html = f"<div style='margin-bottom: 15px; padding: 10px; background-color: rgba(255,255,255,0.03); border-radius: 4px; line-height: 1.6;'>{''.join(legend_items)}</div>"
    st.markdown(legend_html, unsafe_allow_html=True)

    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"LWC重ね合わせ描画エラー: {e}")

def build_lwc_candle_chart(
    df: pd.DataFrame, 
    sma25: pd.Series = None, 
    sma75: pd.Series = None, 
    sma200: pd.Series = None, 
    sma_fast: pd.Series = None, 
    sma_slow: pd.Series = None, 
    height: int = 200, 
    is_jp: bool = True, 
    wvf_df: pd.DataFrame = None, 
    bb_dict: dict = None, 
    event_markers: list = None
) -> dict:
    if df is None or df.empty:
        return {}

    df = df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        times = _to_lwc_time(df["date"])
    else:
        times = _to_lwc_time(df.index)

    def _safe_get_times(s: pd.Series) -> list:
        if s is None or s.empty:
            return []
        if isinstance(s.index, pd.DatetimeIndex) or (len(s.index) > 0 and hasattr(s.index[0], 'strftime')):
            return _to_lwc_time(s.index)
        if len(s) == len(times):
            return times
        return _to_lwc_time(s.index)

    price_format = detect_price_format(df, is_jp=is_jp)

    if sma75 is None and sma_fast is not None:
        sma75 = sma_fast
    if sma200 is None and sma_slow is not None:
        sma200 = sma_slow

    series = []

    # 1. ボリンジャーバンド
    if bb_dict:
        for key_name in ["p3", "m3"]:
            s_band = bb_dict.get(key_name)
            if s_band is not None and not s_band.dropna().empty:
                b_times = _safe_get_times(s_band)
                series.append({
                    "type": "Line",
                    "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(b_times, s_band.values) if not pd.isna(v)],
                    "options": {
                        "color": "rgba(200, 200, 200, 0.08)", 
                        "lineWidth": 1, 
                        "title": "",
                        "priceLineVisible": False, 
                        "lastValueVisible": False, 
                        "crosshairMarkerVisible": False,
                        "priceFormat": price_format,
                    },
                })

        for key_name in ["p2", "m2"]:
            s_band = bb_dict.get(key_name)
            if s_band is not None and not s_band.dropna().empty:
                b_times = _safe_get_times(s_band)
                series.append({
                    "type": "Line",
                    "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(b_times, s_band.values) if not pd.isna(v)],
                    "options": {
                        "color": "rgba(200, 200, 200, 0.20)", 
                        "lineWidth": 1, 
                        "title": "",
                        "priceLineVisible": False, 
                        "lastValueVisible": False, 
                        "crosshairMarkerVisible": False,
                        "priceFormat": price_format,
                    },
                })

    # 2. 移動平均線
    if sma200 is not None and not sma200.dropna().empty:
        st_times = _safe_get_times(sma200)
        series.append({
            "type": "Line",
            "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(st_times, sma200.values) if not pd.isna(v)],
            "options": {
                "color": "rgba(171, 71, 188, 0.80)", 
                "lineWidth": 1, 
                "title": "",
                "priceLineVisible": False, 
                "lastValueVisible": False, 
                "crosshairMarkerVisible": False,
                "priceFormat": price_format,
            },
        })

    if sma75 is not None and not sma75.dropna().empty:
        st_times = _safe_get_times(sma75)
        series.append({
            "type": "Line",
            "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(st_times, sma75.values) if not pd.isna(v)],
            "options": {
                "color": "rgba(255, 167, 38, 0.75)", 
                "lineWidth": 1, 
                "title": "",
                "priceLineVisible": False, 
                "lastValueVisible": False, 
                "crosshairMarkerVisible": False,
                "priceFormat": price_format,
            },
        })

    if sma25 is not None and not sma25.dropna().empty:
        s25_times = _safe_get_times(sma25)  # 👈 ここを st_times から s25_times に修正
        series.append({
            "type": "Line",
            "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(s25_times, sma25.values) if not pd.isna(v)],
            "options": {
                "color": "rgba(239, 83, 80, 0.75)", 
                "lineWidth": 1, 
                "title": "",
                "priceLineVisible": False, 
                "lastValueVisible": False, 
                "crosshairMarkerVisible": False,
                "priceFormat": price_format,
            },
        })

    # 3. ローソク足
    candle_data = [
        {"time": t, "open": round(float(o), 2), "high": round(float(h), 2),
         "low": round(float(l), 2), "close": round(float(c), 2)}
        for t, o, h, l, c in zip(times, df["open"], df["high"], df["low"], df["close"])
        if not any(pd.isna(v) for v in [o, h, l, c])
    ]

    candlestick_series = {
        "type": "Candlestick",
        "data": candle_data,
        "options": {
            "upColor": "#26a69a", "downColor": "#ef5350",
            "borderUpColor": "#26a69a", "borderDownColor": "#ef5350",
            "wickUpColor": "#26a69a", "wickDownColor": "#ef5350",
            "priceFormat": price_format,
            "priceLineVisible": False,
            "lastValueVisible": True,
        },
    }

    if event_markers:
        valid_times = set(times)
        filtered_markers = [
            m for m in event_markers
            if m.get("time") in valid_times
        ]
        if filtered_markers:
            candlestick_series["markers"] = sorted(filtered_markers, key=lambda x: x["time"])

    series.append(candlestick_series)

    # 4. 出来高
    if "volume" in df.columns:
        wvf_map = {}
        if wvf_df is not None and not wvf_df.empty:
            w_df = wvf_df.copy()
            w_times = _to_lwc_time(pd.to_datetime(w_df["date"])) if "date" in w_df.columns else _to_lwc_time(w_df.index)
            for wt, (_, wr) in zip(w_times, w_df.iterrows()):
                wvf_map[wt] = {
                    "lime": bool(wr.get("is_lime", False))
                }

        vol_data = []
        for t, row in zip(times, df.itertuples()):
            o, c, v = row.open, row.close, row.volume
            if pd.isna(v):
                continue

            sig = wvf_map.get(t, {})
            if sig.get("lime"):
                color = "rgba(0, 230, 118, 0.95)"
            else:
                color = "rgba(38, 166, 154, 0.2)" if (pd.isna(o) or pd.isna(c) or c >= o) else "rgba(239, 83, 80, 0.2)"

            vol_data.append({"time": t, "value": float(v), "color": color})

        series.append({
            "type": "Histogram",
            "data": vol_data,
            "options": {
                "priceFormat": {"type": "volume"},
                "priceScaleId": "",
                "priceLineVisible": False,
                "lastValueVisible": False,
            }
        })

    return {"chart": _lwc_base_options(height=height), "series": series}

def render_lwc_sector_mini(
    price_series: pd.Series, 
    sma25: pd.Series = None,
    sma75: pd.Series = None, 
    sma200: pd.Series = None, 
    sma_fast: pd.Series = None, 
    sma_slow: pd.Series = None, 
    wvf_lit: pd.Series = None, 
    volume_series: pd.Series = None, 
    key: str = "lwc", 
    height: int = 160, 
    is_jp: bool = True
):
    chart_def = build_lwc_line_chart(
        price_series, 
        sma25=sma25,
        sma75=sma75, 
        sma200=sma200, 
        sma_fast=sma_fast, 
        sma_slow=sma_slow, 
        wvf_lit=wvf_lit, 
        volume_series=volume_series, 
        height=height, 
        is_jp=is_jp
    )
    if not chart_def:
        st.caption("データなし")
        return
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

def render_lwc_candle_mini(
    df: pd.DataFrame, 
    sma25: pd.Series = None,
    sma75: pd.Series = None, 
    sma200: pd.Series = None, 
    sma_fast: pd.Series = None, 
    sma_slow: pd.Series = None, 
    key: str = "lwc_candle", 
    height: int = 200, 
    is_jp: bool = True, 
    wvf_df: pd.DataFrame = None, 
    bb_dict: dict = None,
    event_markers: list = None
):
    chart_def = build_lwc_candle_chart(
        df, 
        sma25=sma25,
        sma75=sma75, 
        sma200=sma200, 
        sma_fast=sma_fast, 
        sma_slow=sma_slow, 
        height=height, 
        is_jp=is_jp, 
        wvf_df=wvf_df, 
        bb_dict=bb_dict,
        event_markers=event_markers
    )
    if not chart_def:
        st.caption("データなし")
        return
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

# =====================================================================
# 🚀 マーケット情報専用のLWC描画関数群
# =====================================================================

def render_lwc_dual_line_chart(
    series1_data: list,
    series2_data: list,
    title1: str = "",
    title2: str = "",
    color1: str = "#ffa726",
    color2: str = "#ef5350",
    height: int = 230,
    key: str = "dual_line_chart"
):
    """日経平均(左軸) & 裁定倍率(右軸)のような2軸折れ線チャートを描画します。"""
    chart_options = _lwc_base_options(height=height, right_offset=8, has_left_scale=True)
    chart_def = {
        "chart": chart_options,
        "series": [
            {
                "type": "Line",
                "data": series1_data,
                "options": {
                    "color": color1,
                    "lineWidth": 2,
                    "priceScaleId": "left",
                    "title": title1,
                    "lastValueVisible": True,
                    "crosshairMarkerVisible": True,
                }
            },
            {
                "type": "Line",
                "data": series2_data,
                "options": {
                    "color": color2,
                    "lineWidth": 2,
                    "priceScaleId": "right",
                    "title": title2,
                    "lastValueVisible": True,
                    "crosshairMarkerVisible": True,
                }
            }
        ]
    }
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

def render_lwc_area_chart(
    series_definitions: list,
    height: int = 160,
    key: str = "area_chart"
):
    """信用比率・NAAIM指数・信用残高などのエリア／ラインチャートを描画します。"""
    chart_options = _lwc_base_options(height=height, right_offset=8)
    chart_def = {
        "chart": chart_options,
        "series": series_definitions
    }
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

def render_lwc_histogram_chart(
    hist_data: list,
    color: str = "#42a5f5",
    height: int = 160,
    key: str = "hist_chart"
):
    """裁定買残などのヒストグラムチャートを描画します。"""
    chart_options = _lwc_base_options(height=height, right_offset=5)
    chart_def = {
        "chart": chart_options,
        "series": [{
            "type": "Histogram",
            "data": hist_data,
            "options": {
                "color": color,
                "priceFormat": {"type": "volume"},
                "lastValueVisible": True
            }
        }]
    }
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

# =====================================================================
# 🌟 【新設】裁定取引しきい値ライン・投資部門別・マクロミニチャート
# =====================================================================

def render_lwc_saitei_with_thresholds(
    df_sai: pd.DataFrame,
    high_threshold: float = 25000,
    low_threshold: float = 5000,
    height: int = 180,
    key: str = "saitei_chart"
):
    """
    裁定買残のヒストグラムに、過熱警戒ライン（赤破線）と底打ちライン（青破線）を
    重ね合わせて描画します。
    """
    if df_sai is None or df_sai.empty:
        st.caption("データなし")
        return

    times = _to_lwc_time(df_sai["date"])
    buy_col = "buy(oku-yen)" if "buy(oku-yen)" in df_sai.columns else "buy_sai"

    vol_data = [
        {"time": t, "value": float(v), "color": "rgba(66, 165, 245, 0.65)"}
        for t, v in zip(times, df_sai[buy_col]) if not pd.isna(v)
    ]
    
    line_high = [{"time": t, "value": float(high_threshold)} for t in times]
    line_low  = [{"time": t, "value": float(low_threshold)} for t in times]

    chart_options = _lwc_base_options(height=height, right_offset=5)
    chart_options["rightPriceScale"] = {
        "borderColor": "rgba(128,128,128,0.3)",
        "scaleMargins": {"top": 0.08, "bottom": 0.08},
        "visible": True,
    }

    chart_def = {
        "chart": chart_options,
        "series": [
            {
                "type": "Histogram",
                "data": vol_data,
                "options": {
                    "priceFormat": {"type": "volume"},
                    "lastValueVisible": True,
                    "title": "裁定買残",
                }
            },
            {
                "type": "Line",
                "data": line_high,
                "options": {
                    "color": "#ef5350",
                    "lineWidth": 1,
                    "lineStyle": 2,  # 破線
                    "priceLineVisible": False,
                    "lastValueVisible": False,
                    "crosshairMarkerVisible": False,
                    "title": "過熱警戒(2.5兆円)",
                }
            },
            {
                "type": "Line",
                "data": line_low,
                "options": {
                    "color": "#42a5f5",
                    "lineWidth": 1,
                    "lineStyle": 2,  # 破線
                    "priceLineVisible": False,
                    "lastValueVisible": False,
                    "crosshairMarkerVisible": False,
                    "title": "大底圏(0.5兆円)",
                }
            }
        ]
    }
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

def render_lwc_investor_type_chart(
    df_investor: pd.DataFrame, 
    height: int = 180, 
    key: str = "investor_chart"
):
    """
    投資部門別（海外勢・事業法人・個人信用）の週間純売買動向（億円）を
    カラーヒストグラムおよびラインとして描画します。
    """
    if df_investor is None or df_investor.empty:
        st.caption("データなし")
        return

    df_sorted = df_investor.sort_values("date").reset_index(drop=True)
    times = _to_lwc_time(pd.to_datetime(df_sorted["date"]))

    foreign_data = []
    corp_data = []
    margin_data = []

    for t, (_, r) in zip(times, df_sorted.iterrows()):
        f_val = r.get("海外投資家_差引(億円)", 0.0)
        c_val = r.get("事業法人_差引(億円)", 0.0)
        m_val = r.get("個人信用_差引(億円)", 0.0)

        f_color = "rgba(38, 166, 154, 0.85)" if f_val >= 0 else "rgba(239, 83, 80, 0.85)"
        foreign_data.append({"time": t, "value": float(f_val), "color": f_color})
        corp_data.append({"time": t, "value": float(c_val)})
        margin_data.append({"time": t, "value": float(m_val)})

    chart_options = _lwc_base_options(height=height, right_offset=5)
    chart_def = {
        "chart": chart_options,
        "series": [
            {
                "type": "Histogram",
                "data": foreign_data,
                "options": {
                    "priceFormat": {"type": "volume"},
                    "title": "海外投資家 純売買",
                    "lastValueVisible": True,
                }
            },
            {
                "type": "Line",
                "data": corp_data,
                "options": {
                    "color": "#ab47bc",  # 紫（自社株買い）
                    "lineWidth": 2,
                    "title": "事業法人(自社株買い)",
                    "lastValueVisible": True,
                }
            },
            {
                "type": "Line",
                "data": margin_data,
                "options": {
                    "color": "#ffa726",  # 橙（個人逆張り）
                    "lineWidth": 1,
                    "lineStyle": 1,
                    "title": "個人信用",
                    "lastValueVisible": True,
                }
            }
        ]
    }
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

def render_lwc_macro_mini(
    df: pd.DataFrame, 
    title: str = "", 
    is_area: bool = False, 
    color: str = "#42a5f5", 
    height: int = 140, 
    key: str = "macro_mini"
):
    """マクロ指標グリッド用の軽量ミニチャートを描画します。"""
    if df is None or df.empty or "close" not in df.columns:
        st.caption("データなし")
        return

    times = _to_lwc_time(df["date"] if "date" in df.columns else df.index)
    data = [{"time": t, "value": round(float(v), 4)} for t, v in zip(times, df["close"].values) if not pd.isna(v)]

    series_type = "Area" if is_area else "Line"
    series_options = {
        "color": color,
        "lineWidth": 2,
        "priceLineVisible": False,
        "lastValueVisible": True,
        "crosshairMarkerVisible": True,
    }
    if is_area:
        series_options.update({
            "lineColor": color,
            "topColor": "rgba(171, 71, 188, 0.35)" if color == "#ab47bc" else "rgba(66, 165, 245, 0.35)",
            "bottomColor": "rgba(171, 71, 188, 0.03)" if color == "#ab47bc" else "rgba(66, 165, 245, 0.03)",
        })

    chart_options = _lwc_base_options(height=height, right_offset=5)
    chart_def = {
        "chart": chart_options,
        "series": [{
            "type": series_type,
            "data": data,
            "options": series_options
        }]
    }
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

def generate_mini_chart_base64(df: pd.DataFrame) -> str:
    try:
        import matplotlib.pyplot as plt
        import mplfinance as mpf

        plot_df = df.tail(60).copy().set_index("date")
        buf = io.BytesIO()
        mc = mpf.make_marketcolors(up='green', down='red', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)
        add_plots = []
        if 'sma50' in plot_df.columns: 
            add_plots.append(mpf.make_addplot(plot_df['sma50'], color='orange', width=0.7))
        if 'sma200' in plot_df.columns: 
            add_plots.append(mpf.make_addplot(plot_df['sma200'], color='#ab47bc', width=0.7))
            
        fig, axlist = mpf.plot(plot_df, type='candle', style=s, addplot=add_plots, figsize=(4, 2.5), tight_layout=True, returnfig=True, axisoff=True)
        fig.set_facecolor('#f0f2f6')
        for ax in axlist: 
            ax.set_facecolor('#f0f2f6')
        fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0)
        plt.close(fig)
        return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"
    except Exception: 
        return ""