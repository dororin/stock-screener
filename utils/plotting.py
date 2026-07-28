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
        # 100基準を0基準（騰落率%）にリベース
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

def build_lwc_candle_chart(df: pd.DataFrame, sma_fast: pd.Series = None, sma_slow: pd.Series = None, height: int = 200) -> dict:
    """ローソク足＋移動平均2本＋出来高（色連動）の LWC 構成定義を生成します。"""
    if df is None or df.empty:
        return {}

    df = df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        times = _to_lwc_time(df["date"])
    else:
        times = _to_lwc_time(df.index)

    candle_data = [
        {"time": t, "open": round(float(o), 2), "high": round(float(h), 2),
         "low": round(float(l), 2), "close": round(float(c), 2)}
        for t, o, h, l, c in zip(times, df["open"], df["high"], df["low"], df["close"])
        if not any(pd.isna(v) for v in [o, h, l, c])
    ]

    series = [
        {
            "type": "Candlestick",
            "data": candle_data,
            "options": {
                "upColor": "#26a69a", "downColor": "#ef5350",
                "borderUpColor": "#26a69a", "borderDownColor": "#ef5350",
                "wickUpColor": "#26a69a", "wickDownColor": "#ef5350",
            },
        }
    ]

    if sma_fast is not None and not sma_fast.dropna().empty:
        sma_times = _to_lwc_time(sma_fast.index)
        series.append({
            "type": "Line",
            "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(sma_times, sma_fast.values) if not pd.isna(v)],
            "options": {"color": "#FFA726", "lineWidth": 1, "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False},
        })

    if sma_slow is not None and not sma_slow.dropna().empty:
        sma_times = _to_lwc_time(sma_slow.index)
        series.append({
            "type": "Line",
            "data": [{"time": t, "value": round(float(v), 2)} for t, v in zip(sma_times, sma_slow.values) if not pd.isna(v)],
            "options": {"color": "#ef5350", "lineWidth": 1, "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False},
        })

    if "volume" in df.columns:
        vol_data = []
        for t, row in zip(times, df.itertuples()):
            o, c, v = row.open, row.close, row.volume
            if pd.isna(v):
                continue
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

def build_lwc_line_chart(price_series: pd.Series, sma_fast: pd.Series = None, sma_slow: pd.Series = None, wvf_lit: pd.Series = None, volume_series = None, height: int = 160) -> dict:
    """折れ線（セクター値）＋移動平均2本＋出来高（または4ステージ出来高）の LWC 構成定義を生成します。"""
    if price_series is None or price_series.empty:
        return {}

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
            "options": {"color": "#ef5350", "lineWidth": 1, "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False},
        })

    # --- 出来高(Volume)オーバーレイ描画の分岐 ---
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

def render_lwc_sector_mini(price_series: pd.Series, sma_fast: pd.Series = None, sma_slow: pd.Series = None, wvf_lit: pd.Series = None, volume_series: pd.Series = None, key: str = "lwc", height: int = 160):
    """セクター絶対値用のLWCミニチャートをレンダリングします。"""
    chart_def = build_lwc_line_chart(price_series, sma_fast=sma_fast, sma_slow=sma_slow, wvf_lit=wvf_lit, volume_series=volume_series, height=height)
    if not chart_def:
        st.caption("データなし")
        return
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

def render_lwc_candle_mini(df: pd.DataFrame, sma_fast: pd.Series = None, sma_slow: pd.Series = None, key: str = "lwc_candle", height: int = 200):
    """個別ローソク足用のLWCミニチャートをレンダリングします。"""
    chart_def = build_lwc_candle_chart(df, sma_fast=sma_fast, sma_slow=sma_slow, height=height)
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
        # 重い描画エンジンのインポートをこの内部に限定することで、通常のページロードをノーウェイト化
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
            add_plots.append(mpf.make_addplot(plot_df['sma200'], color='red', width=1.0))
            
        fig, axlist = mpf.plot(plot_df, type='candle', style=s, addplot=add_plots, figsize=(4, 2.5), tight_layout=True, returnfig=True, axisoff=True)
        fig.set_facecolor('#f0f2f6')
        for ax in axlist: 
            ax.set_facecolor('#f0f2f6')
        fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0)
        plt.close(fig)
        return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"
    except Exception: 
        return ""