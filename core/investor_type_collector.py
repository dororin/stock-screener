# core/investor_type_collector.py

import os
import re
import io
import requests
import pandas as pd
from bs4 import BeautifulSoup
from config import settings

def parse_jpx_investor_type_excel(file_path_or_bytes) -> dict:
    """
    JPX公式の『投資部門別 株式売買状況』Excelを解析し、
    主要部門の純売買額（億円単位）を抽出します。
    """
    df_raw = pd.read_excel(file_path_or_bytes, sheet_name=0, header=None)

    target_row_idx = None
    for r_idx in range(len(df_raw)):
        row_str = " ".join([str(val) for val in df_raw.iloc[r_idx].values])
        if ("東証プライム" in row_str or "Prime" in row_str) and ("金額" in row_str or "Value" in row_str):
            target_row_idx = r_idx
            break

    if target_row_idx is None:
        raise ValueError("「東証プライム 金額 Value」の行が見つかりませんでした。")

    row = df_raw.iloc[target_row_idx]

    # 週次ラベル（例: '2026094 -> 2026094）の取得
    week_label = str(df_raw.iloc[target_row_idx, 0]).strip().replace("'", "")

    def find_col_idx(parent_keyword, sub_keyword=None):
        for c in range(df_raw.shape[1]):
            col_text = " ".join([str(df_raw.iloc[r, c]) for r in range(0, target_row_idx)])
            if parent_keyword in col_text:
                if sub_keyword is None or sub_keyword in col_text:
                    if "差引" in col_text or "Balance" in col_text:
                        return c
        return None

    c_foreign_cash = find_col_idx("海外投資家", "現金") or find_col_idx("Foreigners", "Cash")
    c_foreign_margin = find_col_idx("海外投資家", "信用") or find_col_idx("Foreigners", "Margin")
    c_individual_cash = find_col_idx("個人", "現金") or find_col_idx("Individuals", "Cash")
    c_individual_margin = find_col_idx("個人", "信用") or find_col_idx("Individuals", "Margin")
    c_trust_bank = find_col_idx("信託銀行") or find_col_idx("Trust BK")
    c_business_corp = find_col_idx("事業法人") or find_col_idx("Business Cos")

    def get_val_in_oku(col_idx):
        if col_idx is None:
            return 0.0
        val = row.iloc[col_idx]
        try:
            # 千円単位の数値を「億円単位」に換算（1億円 = 100,000千円）
            return round(float(str(val).replace(",", "").strip()) / 100000.0, 1)
        except Exception:
            return 0.0

    foreign_net = get_val_in_oku(c_foreign_cash) + get_val_in_oku(c_foreign_margin)
    individual_cash_net = get_val_in_oku(c_individual_cash)
    individual_margin_net = get_val_in_oku(c_individual_margin)
    trust_bank_net = get_val_in_oku(c_trust_bank)
    business_corp_net = get_val_in_oku(c_business_corp)

    return {
        "week": week_label,
        "海外投資家_差引(億円)": foreign_net,
        "個人現金_差引(億円)": individual_cash_net,
        "個人信用_差引(億円)": individual_margin_net,
        "信託銀行_差引(億円)": trust_bank_net,
        "事業法人_差引(億円)": business_corp_net,
    }


def fetch_latest_jpx_investor_excel() -> tuple:
    """JPX公式サイトから最新の投資部門別Excelファイルを取得します。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    url = getattr(settings, "JPX_INVESTOR_TYPE_PAGE", "https://www.jpx.co.jp/markets/statistics-equities/investor-type/index.html")
    
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return None, ""
            
        soup = BeautifulSoup(res.text, "html.parser")
        links = soup.find_all("a", href=re.compile(r"stock_1_w_.*\.xlsx$", re.IGNORECASE))
        if not links:
            return None, ""
            
        latest_rel_url = links[0].get("href")
        full_url = latest_rel_url if latest_rel_url.startswith("http") else f"https://www.jpx.co.jp{latest_rel_url}"
        
        file_res = requests.get(full_url, headers=headers, timeout=20)
        if file_res.status_code == 200 and len(file_res.content) > 5000:
            return file_res.content, os.path.basename(full_url)
    except Exception as e:
        print(f"⚠️ [investor_type_collector] 最新Excel取得例外: {e}")
        
    return None, ""


def sync_jpx_investor_type_data() -> pd.DataFrame:
    """最新のJPX投資部門別データを取得・パースし、Google Sheetsとマージして返します。"""
    from data_access.sheets_api import load_investor_type_data, save_investor_type_data
    
    existing_df = load_investor_type_data()
    content, filename = fetch_latest_jpx_investor_excel()
    
    if not content:
        return existing_df
        
    try:
        parsed_dict = parse_jpx_investor_type_excel(content)
        week_val = parsed_dict.get("week")
        if not week_val:
            return existing_df

        # 日付ラベルの自動推定（week: '2026094' -> 2026-09 第4週の金曜日付近）
        # ファイル名（例: stock_1_w_20260924_20260925.xlsx）から確定日を取得
        m = re.search(r"(\d{8})\.xlsx", filename)
        if m:
            date_str = pd.to_datetime(m.group(1)).strftime("%Y-%m-%d")
        else:
            date_str = pd.Timestamp.now().strftime("%Y-%m-%d")

        parsed_dict["date"] = date_str
        new_row = pd.DataFrame([parsed_dict])

        if existing_df.empty:
            merged_df = new_row
        else:
            merged_df = pd.concat([existing_df, new_row], ignore_index=True)
            merged_df = merged_df.drop_duplicates(subset=["week"], keep="last")

        merged_df = merged_df.sort_values("week").reset_index(drop=True)
        save_investor_type_data(merged_df)
        return merged_df
    except Exception as ex:
        print(f"❌ [investor_type_collector] パース・保存エラー: {ex}")
        return existing_df