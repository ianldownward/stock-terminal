import os
import json
import pandas as pd
import yfinance as yf
from pymongo import MongoClient
import warnings

# Suppress yfinance timezone warnings for clean output
warnings.simplefilter(action='ignore', category=FutureWarning)

def generate_excel_report():
    print("Loading portfolio data...")
    # 1. Connect to DB or local JSON
    mongo_uri = os.environ.get('MONGO_URI')
    if mongo_uri:
        client = MongoClient(mongo_uri)
        db_data = client['stock_terminal']['portfolio'].find_one({"_id": "main_store"})
    else:
        if not os.path.exists('portfolio.json'):
            print("Error: portfolio.json not found.")
            return
        with open('portfolio.json', 'r') as f:
            db_data = json.load(f)

    users = db_data.get('users', {})
    
    # 2. Extract all tickers and dates to bound our data pull
    all_tickers = set()
    all_dates = set()
    for u, udata in users.items():
        for tr in udata.get('history', []):
            all_tickers.add(tr['ticker'])
            all_dates.add(tr['date_str'])

    if not all_dates:
        print("No trade history found to report on.")
        return

    min_date = min(all_dates)
    # Extend end date slightly to ensure we capture today
    max_date = (pd.Timestamp.now() + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    date_range = pd.date_range(start=min_date, end=pd.Timestamp.now(), freq='B') # Business days

    # 3. Fetch historical pricing data for all traded assets
    print(f"Fetching historical EOD data for {len(all_tickers)} assets...")
    yf_data = {}
    for tk in all_tickers:
        try:
            df = yf.Ticker(tk).history(start=min_date, end=max_date, interval="1d")
            if not df.empty:
                # Standardize index to date strings for easy lookup
                df.index = df.index.strftime('%Y-%m-%d')
                yf_data[tk] = df['Close']
        except Exception as e:
            print(f"Failed to fetch data for {tk}: {e}")

    # 4. Reconstruct day-by-day equity for each profile
    print("Reconstructing historical equity curves...")
    results = {}

    for u, udata in users.items():
        hist = list(reversed(udata.get('history', []))) # Oldest to newest
        budget = udata.get('master_budget', 5000.0)
        
        # Deduct manual initial positions to get baseline starting cash
        init_manual = sum(pos.get('manual_val', 0.0) for pos in udata.get('initial_positions', {}).values())
        cash = budget - init_manual
        holdings = {}
        
        daily_equity = {}
        trade_idx = 0
        
        for current_date in date_range:
            date_str = current_date.strftime('%Y-%m-%d')
            
            # Apply all trades that happened on or before this day
            while trade_idx < len(hist) and hist[trade_idx]['date_str'] <= date_str:
                tr = hist[trade_idx]
                tk = tr['ticker']
                if tr['action'] == 'BUY':
                    cash -= tr['amount']
                    holdings[tk] = holdings.get(tk, 0) + tr['shares']
                elif tr['action'] == 'SELL':
                    cash += tr['amount']
                    holdings[tk] = holdings.get(tk, 0) - tr['shares']
                trade_idx += 1
            
            # Calculate End-of-Day Holdings Value
            eod_holdings_val = 0.0
            for tk, shares in holdings.items():
                if shares > 0:
                    prices = yf_data.get(tk)
                    if prices is not None and not prices.empty:
                        # Get latest price available on or before this date
                        valid_prices = prices[prices.index <= date_str]
                        if not valid_prices.empty:
                            p = float(valid_prices.iloc[-1])
                            div = 100.0 if tk.endswith('.L') and p > 100 else 1.0
                            eod_holdings_val += shares * (p / div)
            
            daily_equity[date_str] = cash + eod_holdings_val
            
        results[u] = daily_equity

    # 5. Calculate Gains and Format Excel
    print("Formatting report...")
    formatted_data = {}
    for u, eq_dict in results.items():
        formatted_data[u] = {}
        prev_eq = users[u].get('master_budget', 5000.0)
        
        for date_str, eq in eq_dict.items():
            gain_val = eq - prev_eq
            gain_pct = (gain_val / prev_eq * 100.0) if prev_eq > 0 else 0.0
            
            # Store tuple keys so Pandas creates a clean MultiIndex (Date -> £, %)
            formatted_data[u][(date_str, '£')] = round(gain_val, 2)
            formatted_data[u][(date_str, '%')] = round(gain_pct, 2)
            
            prev_eq = eq

    df_report = pd.DataFrame.from_dict(formatted_data, orient='index')
    
    # Sort dates chronologically
    df_report = df_report.reindex(sorted(df_report.columns, key=lambda x: x[0]), axis=1)
    df_report.columns = pd.MultiIndex.from_tuples(df_report.columns, names=['Date', 'Metric'])
    
    # Sort rows by final equity (highest performing profiles at the top)
    if not df_report.empty:
        last_date = sorted(list(set([c[0] for c in df_report.columns])))[-1]
        df_report['Sort_Key'] = df_report[(last_date, '£')]
        df_report = df_report.sort_values(by='Sort_Key', ascending=False).drop(columns=['Sort_Key'])

    out_file = 'Performance_Report.xlsx'
    df_report.to_excel(out_file)
    print(f"Success! Report saved as: {out_file}")

if __name__ == '__main__':
    generate_excel_report()