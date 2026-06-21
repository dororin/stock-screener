# utils/plotting.py
import io
import base64
import pandas as pd
import numpy as np
import yfinance as yf
import streamlit as st
import matplotlib.pyplot as plt
import mplfinance as mpf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from streamlit_lightweight_charts import renderLightweightCharts

# =====================================================================
# 📊 Lightweight Charts (LWC) 用ヘルパー
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
            "vertLines": {"color": "rgba(128,128,128,0.15)"},
            "horzLines": {"color": "rgba(128,128,128,0.15)"},
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
    """セクターRSの重ね合わせLWCをカラー凡例付きでレンダリングします。"""
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
    """ローソク足＋移動平均2本＋出来高（色連動）のLWC構成定義を生成します。"""
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
            color = "rgba(38, 166, 154, 0.25)" if (pd.isna(o) or pd.isna(c) or c >= o) else "rgba(239, 83, 80, 0.25)"
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
    """
    折れ線（セクター値など）＋移動平均2本＋合算売買代金（または4ステージ出来高）のLWC構成定義を生成します。
    """
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
        # パターンX: 4ステージ出来高（リスト形式の辞書データ）が直接渡された場合
        if isinstance(volume_series, list):
            series.append({
                "type": "Histogram",
                "data": volume_series,
                "options": {
                    "priceFormat": {"type": "volume"},
                    "priceScaleId": "",  # overlayPriceScalesにマッピング
                    "priceLineVisible": False,
                    "lastValueVisible": False,
                }
            })
        # パターンY: 従来の pd.Series（単純売買代金）が渡された場合
        elif isinstance(volume_series, pd.Series) and not volume_series.empty:
            vol_times = _to_lwc_time(volume_series.index)
            price_diff = price_series.diff()
            
            vol_data = []
            for t, val, diff in zip(vol_times, volume_series.values, price_diff.values):
                if pd.isna(val):
                    continue
                color = "rgba(38, 166, 154, 0.25)" if (pd.isna(diff) or diff >= 0) else "rgba(239, 83, 80, 0.25)"
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
    """セクター絶対値用のLWCミニチャートを画面上にレンダリングします。"""
    chart_def = build_lwc_line_chart(price_series, sma_fast=sma_fast, sma_slow=sma_slow, wvf_lit=wvf_lit, volume_series=volume_series, height=height)
    if not chart_def:
        st.caption("データなし")
        return
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

def render_lwc_candle_mini(df: pd.DataFrame, sma_fast: pd.Series = None, sma_slow: pd.Series = None, key: str = "lwc_candle", height: int = 200):
    """個別ローソク足用のLWCミニチャートを画面上にレンダリングします。"""
    chart_def = build_lwc_candle_chart(df, sma_fast=sma_fast, sma_slow=sma_slow, height=height)
    if not chart_def:
        st.caption("データなし")
        return
    try:
        renderLightweightCharts([chart_def], key=key)
    except Exception as e:
        st.caption(f"描画エラー: {e}")

# =====================================================================
# 🕯️ mplfinance (PNGバイナリエンコード) 描画
# =====================================================================

def generate_mini_chart_base64(df: pd.DataFrame) -> str:
    """PDFやHTML、簡易セルに差し込む用のローソク足画像をBase64形式で出力します。"""
    try:
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

# =====================================================================
# 📈 Plotly 描画
# =====================================================================

def plot_individual_margin(df: pd.DataFrame, code: str) -> go.Figure:
    """IRBankなどから取得した、信用残高の二軸 Plotly 折れ線グラフを構築します。"""
    if df.empty: 
        return None
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df['Date'], y=df['Buy(Shares)'], mode='lines+markers', name='信用買い残', line=dict(color='red', width=2)))
    fig.add_trace(go.Scatter(x=df['Date'], y=df['Sell(Shares)'], mode='lines+markers', name='信用売り残', line=dict(color='blue', width=2)))
    fig.update_layout(
        title=f"銘柄コード {code} : 信用残高推移 (株)",
        height=400, margin=dict(l=20, r=20, t=50, b=20),
        hovermode='x', template='plotly_white',
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        spikedistance=-1, hoverdistance=-1
    )
    fig.update_xaxes(showspikes=True, spikemode='across', spikesnap='cursor', spikedash='solid', spikethickness=1, spikecolor='#ff4b4b')
    return fig

def plot_market_dashboard(saitei_df: pd.DataFrame, sinyou_df: pd.DataFrame, naaim_df: pd.DataFrame) -> go.Figure:
    """裁定、信用、NAAIMなどの複数の国別マクロ指標を縦軸整列した大型Plotlyサブプロットを作成します。"""
    if saitei_df.empty and sinyou_df.empty and naaim_df.empty: 
        return None
        
    has_naaim = not naaim_df.empty
    rows = 4 if has_naaim else 3
    row_heights = [0.55, 0.15, 0.15, 0.15] if has_naaim else [0.6, 0.2, 0.2]
    specs = [[{"secondary_y": True}], [{}], [{}]]
    titles = ['日経平均 & 裁定倍率 (右軸)', '裁定買残 (億円)', '信用比率 (買残 / 日経平均)']
    if has_naaim:
        specs.append([{"secondary_y": True}])
        titles.append('NAAIM Exposure Index')
    
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.05, row_heights=row_heights, specs=specs, subplot_titles=titles)
    
    d1 = saitei_df.copy() if not saitei_df.empty else pd.DataFrame()
    d2 = sinyou_df.copy() if not sinyou_df.empty else pd.DataFrame()
    if not d1.empty and not d2.empty:
        d1['Date'] = pd.to_datetime(d1['Date']).dt.normalize()
        d2['Date'] = pd.to_datetime(d2['Date']).dt.normalize()
        df_jp = pd.merge(d1, d2, on='Date', how='inner', suffixes=('_sai', '_sin')).sort_values('Date')
        df_jp = df_jp[~df_jp['Date'].duplicated(keep='last')]
        df_jp.columns = [str(c).lower().strip() for c in df_jp.columns]
        
        nik_col = 'nikkei225_sai' if 'nikkei225_sai' in df_jp.columns else 'nikkei225'
        buy_sai_col = 'buy(oku-yen)'
        buy_sin_col = 'buy(m-yen)'
        df_jp['ratio_sai'] = df_jp[buy_sai_col] / df_jp[nik_col]
        df_jp['ratio_sin'] = df_jp[buy_sin_col] / df_jp[nik_col]
        
        fig.add_hline(y=0.6, row=1, col=1, secondary_y=True, line_color='lightblue', line_dash='dash', line_width=1)
        fig.add_trace(go.Scatter(x=df_jp['date'], y=df_jp[nik_col], mode='lines', name='日経平均', line=dict(color='orange', width=2)), row=1, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(x=df_jp['date'], y=df_jp['ratio_sai'], mode='lines', name='裁定倍率', line=dict(color='red', width=2)), row=1, col=1, secondary_y=True)
        fig.add_trace(go.Bar(x=df_jp['date'], y=df_jp[buy_sai_col], name='裁定買残', marker_color='#1f77b4'), row=2, col=1)
        fig.add_trace(go.Scatter(x=df_jp['date'], y=df_jp['ratio_sin'], mode='lines', name='信用比率', line=dict(color='green', width=1.5), fill='tozeroy', fillcolor='rgba(0, 255, 0, 0.1)'), row=3, col=1)

    if has_naaim:
        n_df = naaim_df.copy()
        n_df['Date'] = pd.to_datetime(n_df['Date']).dt.normalize()
        try:
            sp500 = yf.download("^GSPC", start=n_df['Date'].min(), progress=False)
            if not sp500.empty:
                sp500 = sp500.reset_index()
                close_col = 'Close' if 'Close' in sp500.columns else sp500.columns[sp500.columns.get_level_values(0) == 'Close'][0]
                fig.add_trace(go.Scatter(x=sp500['Date'], y=sp500[close_col], mode='lines', name='S&P 500', line=dict(color='rgba(128, 128, 128, 0.4)', width=1, dash='dot')), row=4, col=1, secondary_y=True)
        except Exception:
            pass

        fig.add_trace(go.Scatter(x=n_df['Date'], y=n_df['NAAIM'], mode='lines', name='NAAIM', line=dict(color='#2E5BFF', width=2.5)), row=4, col=1, secondary_y=False)
        fig.add_hline(y=100, row=4, col=1, line_color='rgba(255, 0, 0, 0.3)', line_dash='dash', line_width=1)
        fig.add_hline(y=0, row=4, col=1, line_color='black', line_width=1)

    fig.update_layout(height=1000 if has_naaim else 800, margin=dict(l=20, r=60, t=50, b=20), showlegend=False, hovermode='x', dragmode='pan', hoverdistance=-1, spikedistance=-1)
    fig.update_xaxes(showticklabels=True, nticks=16, matches='x', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikecolor='#ff4b4b', spikedash='solid', showline=True)
    fig.update_yaxes(showspikes=False)
    fig.update_yaxes(title_text="株価", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="倍率", row=1, col=1, secondary_y=True, range=[0.2, 1.6])
    fig.update_yaxes(title_text="億円", row=2, col=1)
    if not d1.empty and not d2.empty:
        fig.update_yaxes(title_text="比率", row=3, col=1, range=[60, df_jp['ratio_sin'].max() * 1.05])
    if has_naaim:
        fig.update_yaxes(title_text="指数", row=4, col=1, secondary_y=False, range=[0, 120])
        fig.update_yaxes(title_text="S&P500", row=4, col=1, secondary_y=True)
        
    return fig

def plot_sector_mini_chart(index_series: pd.Series, sector_name: str, momentum_pct: float) -> go.Figure:
    """絶対リターンインデックスの背景面付き Plotly ミニ折れ線グラフを構築します。"""
    if index_series.empty: 
        return go.Figure()
    color = "#26a69a" if momentum_pct >= 0 else "#ef5350"
    fill_color = "rgba(38,166,154,0.15)" if momentum_pct >= 0 else "rgba(239,83,80,0.15)"
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=index_series.index, y=index_series.values, mode="lines",
        line=dict(color=color, width=2), fill="tozeroy", fillcolor=fill_color,
        hovertemplate="%{x|%m/%d}: %{y:.1f}<extra></extra>"
    ))
    fig.add_hline(y=100, line_dash="dot", line_color="gray", line_width=1, opacity=0.5)
    fig.update_layout(
        height=140, margin=dict(l=5, r=5, t=5, b=5), showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(showticklabels=True, showgrid=True, gridcolor="rgba(128,128,128,0.2)", zeroline=False, tickfont=dict(size=9)),
    )
    return fig

def plot_sector_absolute_mini_chart(sector_abs: pd.Series, sma75: pd.Series, sma200: pd.Series, is_wvf_lit: pd.Series, sector_name: str) -> go.Figure:
    """絶対価格推移＋WVFがシグナルONになっている日付範囲に薄い赤色の背景帯（vrect）を入れたミニ Plotly グラフを構築します。"""
    if sector_abs is None or sector_abs.empty:
        return go.Figure()

    fig = go.Figure()
    try:
        if is_wvf_lit is not None and not is_wvf_lit.empty:
            in_signal = False
            sig_start = None
            dates = is_wvf_lit.index.tolist()
            vals  = is_wvf_lit.tolist()
            for dt, lit in zip(dates, vals):
                if lit and not in_signal:
                    sig_start = dt
                    in_signal = True
                elif not lit and in_signal:
                    fig.add_vrect(x0=sig_start, x1=dt, fillcolor="rgba(255,0,0,0.13)", layer="below", line_width=0)
                    in_signal = False
            if in_signal and sig_start is not None:
                fig.add_vrect(x0=sig_start, x1=dates[-1], fillcolor="rgba(255,0,0,0.13)", layer="below", line_width=0)
    except Exception:
        pass

    fig.add_trace(go.Scatter(
        x=sector_abs.index, y=sector_abs.values, mode="lines", name="価格",
        line=dict(color="#42a5f5", width=1.8), hovertemplate="%{x|%m/%d} 価格:%{y:,.1f}<extra></extra>",
    ))

    if sma75 is not None and not sma75.dropna().empty:
        fig.add_trace(go.Scatter(
            x=sma75.index, y=sma75.values, mode="lines", name="75SMA",
            line=dict(color="#FFA726", width=1.2), hovertemplate="75SMA:%{y:,.1f}<extra></extra>",
        ))

    if sma200 is not None and not sma200.dropna().empty:
        fig.add_trace(go.Scatter(
            x=sma200.index, y=sma200.values, mode="lines", name="200SMA",
            line=dict(color="#ef5350", width=1.4), hovertemplate="200SMA:%{y:,.1f}<extra></extra>",
        ))

    fig.update_layout(
        height=155, margin=dict(l=5, r=5, t=5, b=5), showlegend=False, hovermode="x unified",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(showticklabels=True, showgrid=True, gridcolor="rgba(128,128,128,0.2)", zeroline=False, tickfont=dict(size=9)),
    )
    return fig

def plot_sector_detail_chart(index_series: pd.Series, benchmark_series: pd.Series, sector_name: str, benchmark_label: str) -> go.Figure:
    """単一セクターの詳細絶対値（上段）と、ベンチマークとの相対強度（下段）をセットにした Plotly グラフを構築します。"""
    fig = make_subplots(
        rows=2 if benchmark_series is not None and not benchmark_series.empty else 1,
        cols=1, shared_xaxes=True,
        row_heights=[0.7, 0.3] if benchmark_series is not None else [1.0]
    )
    fig.add_trace(go.Scatter(x=index_series.index, y=index_series.values, name=sector_name, line=dict(color="#2196F3", width=2)), row=1, col=1)
    if benchmark_series is not None and not benchmark_series.empty:
        common_dates = index_series.index.intersection(benchmark_series.index)
        if len(common_dates) > 0:
            rel = (index_series[common_dates] / benchmark_series[common_dates]) * 100
            fig.add_trace(go.Scatter(x=rel.index, y=rel.values, name=f"相対強度 vs {benchmark_label}", line=dict(color="#FF9800", width=1.5)), row=2, col=1)
            fig.add_hline(y=100, line_dash="dot", line_color="gray", row=2, col=1)
            
    fig.update_layout(height=400, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified", template="plotly_white", legend=dict(orientation="h", y=1.05))
    return fig