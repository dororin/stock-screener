import requests
from bs4 import BeautifulSoup
import re
import pandas as pd

def fetch_naaim_data():
    base_url = "https://naaim.org/programs/naaim-exposure-index/"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(base_url, headers=headers, timeout=15)
        if res.status_code != 200: 
            print(f"Status Code: {res.status_code}")
            return pd.DataFrame()
        soup = BeautifulSoup(res.text, "html.parser")
        links = soup.find_all("a", href=re.compile(r"\.xlsx$"))
        excel_url = None
        for link in links:
            if "HERE" in link.get_text().upper():
                excel_url = link.get('href')
                break
        if not excel_url:
            if links: excel_url = links[0].get('href')
            else: 
                print("No excel link found")
                return pd.DataFrame()
        
        print(f"Excel URL: {excel_url}")
        # Try to download and read
        content = requests.get(excel_url, headers=headers).content
        from io import BytesIO
        df = pd.read_excel(BytesIO(content))
        print("Columns discovered:", df.columns.tolist())
        df.columns = [str(c).strip() for c in df.columns]
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
            df = df.dropna(subset=['Date'])
            val_col = next((c for c in df.columns if 'Mean' in c or 'Average' in c), None)
            if val_col:
                df = df[['Date', val_col]].rename(columns={val_col: 'NAAIM'})
                df = df.sort_values('Date').reset_index(drop=True)
                print(f"Success! Latest data: {df.iloc[-1].to_dict()}")
                return df
        return pd.DataFrame()
    except Exception as e:
        print(f"Error: {e}")
        return pd.DataFrame()

if __name__ == "__main__":
    df = fetch_naaim_data()
    if df.empty:
        print("Failed to fetch data.")
    else:
        print(f"Fetched {len(df)} rows.")
