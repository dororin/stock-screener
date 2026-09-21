# streamlit_app.py
import streamlit as st

# --- アプリ共通ページ設定 ---
st.set_page_config(
    page_title="WVF Stock Screener Pro",
    page_icon="📈",
    layout="wide"
)

# --- 共通カスタムCSS ---
st.markdown("""
    <style>
    html, body, [class*="st-"] { font-size: 0.95rem !important; }
    [data-testid="stMetricValue"] { font-size: 1.1rem !important; font-weight: 600; }
    [data-testid="stMetricLabel"] { font-size: 0.8rem !important; }
    .stMainContainer { padding-top: 2rem !important; }
    .stVerticalBlock { gap: 0.5rem !important; }
    hr { margin: 0.8rem 0 !important; }
    h3 { font-size: 1.1rem !important; margin-bottom: 0.3rem !important; }
    </style>
    """, unsafe_allow_html=True)

# --- 各画面（st.Page）の定義 ---
screening_page = st.Page("views/screening.py", title="スクリーニング", icon="🔍", default=True)
holdings_page = st.Page("views/holdings.py", title="所持中株", icon="💼")  # 💡 保有株画面を追加
market_page = st.Page("views/market_info.py", title="マーケット情報", icon="📈")
sector_page = st.Page("views/sector_rotation.py", title="セクターローテーション", icon="🔄")
maintenance_page = st.Page("views/maintenance.py", title="データ管理・保守", icon="🗄️")

# --- ナビゲーションの構成 ---
pg = st.navigation({
    "分析機能": [screening_page, holdings_page, market_page, sector_page],
    "管理": [maintenance_page]
})

# 実行
pg.run()