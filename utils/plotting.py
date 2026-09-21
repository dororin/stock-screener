# utils/plotting.py

import io
import base64
import pandas as pd
import numpy as np
import streamlit as st
from streamlit_lightweight_charts import renderLightweightCharts

# =====================================================================
# 📊 Lightweight Charts (LWC) 用ヘルパー (極めて軽量・高速)
# =====================================================================

def _to_lwc_time(dt_index) -> list:
    """DatetimeIndexをLWCのtime文字列（YYYY-MM-DD）リストに変換します。"""
    return [str(d)[:10] for d in dt_index]

def _lwc_base_options(height: int = 160, right_offset: int = 5) -> dict:
    """LWC共通レイアウトオプションを生成します。"""
    return {
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
                "bottom": 0.25  # 出来高オーバーレイ用の余白を確保
            }
        },
        "overlayPriceScales": {
            "scaleMargins": {
                "top": 0.75,   # 出来高を最下部25%に固定
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
    """複数セクターの相対強度（RS）のLWC重ね合わせ比較チャート定義を生成します。"""
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
    """セクターRSの重ね合わせ LWC をカラー凡例付きでレンダリングします。"""
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
    sma_fast: pd.Series = None,  # 互換用
    sma_slow: pd.Series = None,  # 互換用
    height: int = 200, 
    is_jp: bool = True, 
    wvf_df: pd.DataFrame = None,
    bb_dict: dict = None
) -> dict:
    """
    ローソク足チャート定義を生成します。
    💡 レイヤー順序: ボリンジャーバンド（最背面） ➔ 移動平均線 ➔ ローソク足（最前面） ➔ 出来高
    💡 右軸ラベル（lastValueVisible / priceLineVisible / title）は完全に無効化。
    """
    if df is None or df.empty:
        return {}

    df = df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        times = _to_lwc_time(df["date"])
    else:
        times = _to_lwc_time(df.index)

    price_format = detect_price_format(df, is_jp=is_jp)

    # 互換性フォールバック
    if sma75 is None and sma_fast is not None:
        sma75 = sma_fast
    if sma200 is None and sma_slow is not None:
        sma200 = sma_slow

    series = []

    # =========================================================================
    # 1. 【最背面レイヤー】ボリンジャーバンド (ミドル非表示, ±2σ/±3σ 半透明極薄グレー)
    # =========================================================================
    if bb_dict:
        # ±3σ (最外殻: 極薄の透過グレー)
        for key_name in ["p3", "m3"]:
            s_band = bb_dict.get(key_name)
            if s_band is not None and not s_band.dropna().empty:
                b_times = _to_lwc_time(s_band.index)
                series.append({
                    "type": "Line",
                    "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(b_times, s_band.values) if not pd.isna(v)],
                    "options": {
                        "color": "rgba(200, 200, 200, 0.08)", 
                        "lineWidth": 1, 
                        "title": "",  # 軸ラベル表示を抑止
                        "priceLineVisible": False, 
                        "lastValueVisible": False, 
                        "crosshairMarkerVisible": False
                    },
                })

        # ±2σ (半透明グレー)
        for key_name in ["p2", "m2"]:
            s_band = bb_dict.get(key_name)
            if s_band is not None and not s_band.dropna().empty:
                b_times = _to_lwc_time(s_band.index)
                series.append({
                    "type": "Line",
                    "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(b_times, s_band.values) if not pd.isna(v)],
                    "options": {
                        "color": "rgba(200, 200, 200, 0.20)", 
                        "lineWidth": 1, 
                        "title": "",  # 軸ラベル表示を抑止
                        "priceLineVisible": False, 
                        "lastValueVisible": False, 
                        "crosshairMarkerVisible": False
                    },
                })

    # =========================================================================
    # 2. 【中間レイヤー】移動平均線 (200SMA紫, 75SMAオレンジ, 25SMA赤 / 太さ1)
    # =========================================================================
    # 200SMA (紫)
    if sma200 is not None and not sma200.dropna().empty:
        st_times = _to_lwc_time(sma200.index)
        series.append({
            "type": "Line",
            "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(st_times, sma200.values) if not pd.isna(v)],
            "options": {
                "color": "#ab47bc", 
                "lineWidth": 1, 
                "title": "",  # 右軸ラベル非表示
                "priceLineVisible": False, 
                "lastValueVisible": False, 
                "crosshairMarkerVisible": False
            },
        })

    # 75SMA (オレンジ)
    if sma75 is not None and not sma75.dropna().empty:
        st_times = _to_lwc_time(sma75.index)
        series.append({
            "type": "Line",
            "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(st_times, sma75.values) if not pd.isna(v)],
            "options": {
                "color": "#ffa726", 
                "lineWidth": 1, 
                "title": "",  # 右軸ラベル非表示
                "priceLineVisible": False, 
                "lastValueVisible": False, 
                "crosshairMarkerVisible": False
            },
        })

    # 25SMA (赤)
    if sma25 is not None and not sma25.dropna().empty:
        st_times = _to_lwc_time(sma25.index)
        series.append({
            "type": "Line",
            "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(st_times, sma25.values) if not pd.isna(v)],
            "options": {
                "color": "#ef5350", 
                "lineWidth": 1, 
                "title": "",  # 右軸ラベル非表示
                "priceLineVisible": False, 
                "lastValueVisible": False, 
                "crosshairMarkerVisible": False
            },
        })

    # =========================================================================
    # 3. 【最前面レイヤー】ローソク足 (Candlestick)
    #    ※BBやMAの後に配置することで、ローソク足の実体やヒゲが手前にくっきり浮き出ます
    # =========================================================================
    candle_data = [
        {"time": t, "open": round(float(o), 2), "high": round(float(h), 2),
         "low": round(float(l), 2), "close": round(float(c), 2)}
        for t, o, h, l, c in zip(times, df["open"], df["high"], df["low"], df["close"])
        if not any(pd.isna(v) for v in [o, h, l, c])
    ]

    series.append({
        "type": "Candlestick",
        "data": candle_data,
        "options": {
            "upColor": "#26a69a", "downColor": "#ef5350",
            "borderUpColor": "#26a69a", "borderDownColor": "#ef5350",
            "wickUpColor": "#26a69a", "wickDownColor": "#ef5350",
            "priceFormat": price_format,
        },
    })

    # =========================================================================
    # 4. 【最下部オーバーレイ】出来高 (Histogram)
    # =========================================================================
    if "volume" in df.columns:
        wvf_map = {}
        if wvf_df is not None and not wvf_df.empty:
            w_df = wvf_df.copy()
            w_times = _to_lwc_time(pd.to_datetime(w_df["date"])) if "date" in w_df.columns else _to_lwc_time(w_df.index)
            for wt, (_, wr) in zip(w_times, w_df.iterrows()):
                wvf_map[wt] = {
                    "lime": bool(wr.get("is_lime", False)),
                    "fuchsia": bool(wr.get("is_fuchsia", False)),
                }

        vol_data = []
        for t, row in zip(times, df.itertuples()):
            o, c, v = row.open, row.close, row.volume
            if pd.isna(v):
                continue

            sig = wvf_map.get(t, {})
            if sig.get("fuchsia"):
                color = "rgba(233, 30, 99, 0.95)"   # 🌸 反発買いシグナル (Fuchsia)
            elif sig.get("lime"):
                color = "rgba(0, 230, 118, 0.95)"   # 🟢 パニック売り点灯 (Lime)
            else:
                color = "rgba(38, 166, 154, 0.2)" if (pd.isna(o) or pd.isna(c) or c >= o) else "rgba(239, 83, 80, 0.2)"

            vol_data.append({"time": t, "value": float(v), "color": color})

        series.append({
            "type": "Histogram",
            "data": vol_data,
            "options": {
                "priceFormat": {"type": "volume"},
                "priceScaleId": "",  # overlayPriceScalesにマッピング
                "priceLineVisible": False,
                "lastValueVisible": False,
            }
        })

    return {"chart": _lwc_base_options(height=height), "series": series}

def build_lwc_line_chart(price_series: pd.Series, sma_fast: pd.Series = None, sma_slow: pd.Series = None, wvf_lit: pd.Series = None, volume_series = None, height: int = 160, is_jp: bool = True) -> dict:
    """折れ線（セクター値）＋移動平均2本＋出来高の LWC 構成定義を生成します。"""
    if price_series is None or price_series.empty:
        return {}

    price_format = detect_price_format(price_series, is_jp=is_jp)

    times = _to_lwc_time(price_series.index)
    price_data = [
        {"time": t, "value": round(float(v), 2)}
        for t, v in zip(times, price_series.values) if not pd.isna(v)
    ]

    series = [
        {
            "type": "Line",
            "data": price_data,
            "options": {
                "color": "#42a5f5", "lineWidth": 2,
                "priceLineVisible": False, "lastValueVisible": True,
                "crosshairMarkerVisible": True,
                "priceFormat": price_format,
            },
        }
    ]

    if sma_fast is not None and not sma_fast.dropna().empty:
        ft = _to_lwc_time(sma_fast.index)
        series.append({
            "type": "Line",
            "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(ft, sma_fast.values) if not pd.isna(v)],
            "options": {"color": "#FFA726", "lineWidth": 1, "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False},
        })

    if sma_slow is not None and not sma_slow.dropna().empty:
        st2 = _to_lwc_time(sma_slow.index)
        series.append({
            "type": "Line",
            "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(st2, sma_slow.values) if not pd.isna(v)],
            "options": {"color": "#ab47bc", "lineWidth": 1, "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False},
        })

    if volume_series is not None:
        if isinstance(volume_series, list):
            series.append({
                "type": "Histogram",
                "data": volume_series,
                "options": {
                    "priceFormat": {"type": "volume"},
                    "priceScaleId": "",
                    "priceLineVisible": False,
                    "lastValueVisible": False,
                }
            })
        elif isinstance(volume_series, pd.Series) and not volume_series.empty:
            vol_times = _to_lwc_time(volume_series.index)
            price_diff = price_series.diff()
            
            vol_data = []
            for t, val, diff in zip(vol_times, volume_series.values, price_diff.values):
                if pd.isna(val):
                    continue
                color = "rgba(38, 166, 154, 0.2)" if (pd.isna(diff) or diff >= 0) else "rgba(239, 83, 80, 0.2)"
                vol_data.append({"time": t, "value": float(val), "color": color})
                
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

def render_lwc_sector_mini(price_series: pd.Series, sma_fast: pd.Series = None, sma_slow: pd.Series = None, wvf_lit: pd.Series = None, volume_series: pd.Series = None, key: str = "lwc", height: int = 160, is_jp: bool = True):
    """セクター絶対値用のLWCミニチャートをレンダリングします。"""
    chart_def = build_lwc_line_chart(price_series, sma_fast=sma_fast, sma_slow=sma_slow, wvf_lit=wvf_lit, volume_series=volume_series, height=height, is_jp=is_jp)
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
    bb_dict: dict = None
):
    """個別ローソク足用のLWCミニチャートをレンダリングします。"""
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
        bb_dict=bb_dict
    )
    if not chart_def:
        st.caption("データなし")
        return
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

# =====================================================================
# 🕯️ mplfinance / matplotlib (完全遅延インポート設計)
# =====================================================================

def generate_mini_chart_base64(df: pd.DataFrame) -> str:
    """PDF等に差し込む用のローソク足画像をBase64形式で出力。必要な時だけライブラリをロードします。"""
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