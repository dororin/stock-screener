# utils/ui_components.py

from contextlib import contextmanager
import pandas as pd
import streamlit as st


@contextmanager
def card_container(border: bool = True):
    """
    全画面共通のカード外枠コンテナ（コンテキストマネージャ）。
    将来的なスタイル・余白変更を一元管理します。
    """
    with st.container(border=border):
        yield


def build_tradingview_link(
    code: str,
    display_name: str = None,
    is_jp: bool = True,
    color: str = "#ffffff"
) -> str:
    """
    TradingViewを別タブで開く安全な <a> タグHTMLを生成します。
    """
    clean_code = str(code).strip().upper()
    label = display_name if display_name else clean_code

    if is_jp:
        tv_url = f"https://jp.tradingview.com/chart/?symbol=TSE%3A{clean_code}"
    else:
        tv_url = f"https://jp.tradingview.com/chart/?symbol={clean_code}"

    return (
        f"<a href='{tv_url}' target='_blank' rel='noopener noreferrer' "
        f"style='color:{color}; text-decoration:none; border-bottom:1px dotted {color};' "
        f"title='TradingViewで開く: {label}'>{label}</a>"
    )


def build_status_badge(
    text: str,
    bg_color: str = "#37474f",
    text_color: str = "#ffffff",
    border_color: str = None,
    title: str = ""
) -> str:
    """
    統一デザイン規則に準拠した角丸バッジHTMLを生成します。
    """
    border_css = f"border: 1px solid {border_color};" if border_color else ""
    title_attr = f"title='{title}'" if title else ""
    return (
        f"<span style='display:inline-block; font-size:0.75rem; background:{bg_color}; "
        f"color:{text_color}; {border_css} padding:2px 6px; border-radius:3px; "
        f"font-weight:bold; white-space:nowrap;' {title_attr}>{text}</span>"
    )


def build_wvf_badge_html(wvf_summary: dict) -> str:
    """
    WVF（Williams Vix Fix）の点灯状態サマリーから統一ステータスバッジHTMLを生成します。
    """
    if not wvf_summary:
        return ""

    if wvf_summary.get("is_lime", False):
        streak = wvf_summary.get("lime_streak", 1)
        ext_val = wvf_summary.get("ext_price", 0.0)
        ext_str = f"¥{ext_val:,.1f}" if pd.notna(ext_val) and ext_val > 0 else "-"
        return (
            f"<span style='font-size:0.75rem; background:#00e676; color:#000000; "
            f"padding:2px 6px; border-radius:3px; font-weight:bold;'>"
            f"🟢 点灯中({streak}日目)</span> "
            f"<span style='font-size:0.75rem; color:#b0bec5;'>翌日消灯目安: {ext_str}</span>"
        )
    elif wvf_summary.get("is_fuchsia", False):
        return build_status_badge("⚪ 反発消灯", bg_color="#37474f", text_color="#ffffff", border_color="#78909c")
    elif wvf_summary.get("is_normal_off", False):
        return build_status_badge("⚪ 消灯", bg_color="#37474f", text_color="#ffffff", border_color="#78909c")

    return ""


def render_card_header(
    title: str,
    code: str = None,
    is_jp: bool = True,
    mom_value: float = None,
    mom_sub_text: str = None,
    badge_html: str = "",
    right_action_fn=None,
    col_ratio: list = None
):
    """
    全画面で統一されたカード上部ヘッダー（左: タイトル・リンク・バッジ、右: 騰落率/メトリクス/アクション）を描画します。
    """
    # 騰落率に応じたデザインカラーの決定
    if mom_value is not None:
        if mom_value >= 0.01:
            badge_icon = "🟢"
            theme_color = "#26a69a"
            sign_str = "+"
        elif mom_value <= -0.01:
            badge_icon = "🔴"
            theme_color = "#ef5350"
            sign_str = ""
        else:
            badge_icon = "⚪"
            theme_color = "#9e9e9e"
            sign_str = ""
        mom_display = f"{sign_str}{mom_value:.2f}%"
    else:
        badge_icon = ""
        theme_color = "#ffffff"
        mom_display = None

    # 左側タイトル部分の構築
    if code:
        link_html = build_tradingview_link(code=code, display_name=title, is_jp=is_jp, color=theme_color)
        left_main_html = (
            f"<div style='font-size:0.88rem; font-weight:600; color:{theme_color}; line-height:1.5; "
            f"white-space:nowrap; overflow:hidden; text-overflow:ellipsis;'>"
            f"{badge_icon + ' ' if badge_icon else ''}{link_html}</div>"
        )
    else:
        left_main_html = (
            f"<div style='font-size:0.88rem; font-weight:600; color:{theme_color}; line-height:1.5; "
            f"white-space:nowrap; overflow:hidden; text-overflow:ellipsis;'>"
            f"{badge_icon + ' ' if badge_icon else ''}{title}</div>"
        )

    # 左右カラム比率の調整
    if right_action_fn:
        ratio = col_ratio if col_ratio and len(col_ratio) == 3 else [3.4, 1.0, 0.6]
        c_left, c_right_val, c_right_act = st.columns(ratio)
    else:
        ratio = col_ratio if col_ratio and len(col_ratio) == 2 else [3.8, 1.2]
        c_left, c_right_val = st.columns(ratio)
        c_right_act = None

    with c_left:
        st.markdown(left_main_html, unsafe_allow_html=True)
        if badge_html:
            st.markdown(
                f"<div style='margin-top:2px; margin-bottom:4px; height:20px; line-height:20px; "
                f"overflow:hidden; white-space:nowrap;'>{badge_html}</div>",
                unsafe_allow_html=True
            )

    with c_right_val:
        if mom_display is not None or mom_sub_text is not None:
            val_html = (
                f"<div style='text-align:right; line-height:1.4;'>"
                f"{f'<div style=\"font-size:0.84rem; font-weight:bold; color:{theme_color};\">{mom_display}</div>' if mom_display else ''}"
                f"{f'<div style=\"font-size:0.75rem; color:#9e9e9e;\">{mom_sub_text}</div>' if mom_sub_text else ''}"
                f"</div>"
            )
            st.markdown(val_html, unsafe_allow_html=True)

    if right_action_fn and c_right_act is not None:
        with c_right_act:
            right_action_fn()