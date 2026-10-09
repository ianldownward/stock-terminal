import time, threading, json, urllib.request
import pandas as pd

from portfolio import portfolio_store
from engine import MarketScoringEngine, safe_float, normalize_price

YF_CACHE = {}
FETCH_REQUESTS = set()

def fetch_yahoo_v8(ticker, period="5d", interval="5m"):
    """
    Direct, pure-python Yahoo Finance API v8 fetcher with custom User-Agent.
    Bypasses cloud server scraper blocks and yfinance library bugs.
    """
    ticker = str(ticker).strip().upper()
    range_map = {"1d": "1d", "5d": "5d", "1mo": "1mo", "6mo": "6mo", "1y": "1y"}
    r = range_map.get(period, "5d")
    
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range={r}&interval={interval}&includePrePost=false"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            res = json.loads(response.read().decode('utf-8'))
            chart = res.get('chart', {}).get('result', [])
            if not chart:
                return pd.DataFrame()
            
            chart_data = chart[0]
            timestamps = chart_data.get('timestamp', [])
            quote = chart_data.get('indicators', {}).get('quote', [{}])[0]
            
            if not timestamps or not quote:
                return pd.DataFrame()
                
            opens = quote.get('open', [])
            highs = quote.get('high', [])
            lows = quote.get('low', [])
            closes = quote.get('close', [])
            
            records = []
            for t, o, h, l, c in zip(timestamps, opens, highs, lows, closes):
                if c is not None and o is not None and h is not None and l is not None:
                    dt = pd.to_datetime(t, unit='s', utc=True).tz_convert('Europe/London')
                    records.append({
                        'Date': dt,
                        'Open': float(o),
                        'High': float(h),
                        'Low': float(l),
                        'Close': float(c)
                    })
            
            if not records:
                return pd.DataFrame()
                
            df = pd.DataFrame(records).set_index('Date')
            return df
    except Exception:
        return pd.DataFrame()

def is_market_open(ticker):
    now_uk = pd.Timestamp.now(tz='Europe/London')
    current_mins = now_uk.hour * 60 + now_uk.minute
    is_us = not (ticker.endswith('.L') or ticker.endswith('.TO'))
    if is_us:
        return 870 <= current_mins < 1260 # 14:30 - 21:00 BST
    else:
        return 480 <= current_mins < 990  # 08:00 - 16:30 BST

def is_eod_sweep_time():
    now_uk = pd.Timestamp.now(tz='Europe/London')
    return now_uk.hour == 20 and now_uk.minute >= 50

def start_unified_background_worker():
    def _worker():
        while True:
            try:
                users_dict = portfolio_store.data.get('users', {}) if isinstance(getattr(portfolio_store, 'data', None), dict) else {}
                all_tickers = set(['NVDA', 'TQQQ', 'SOXL', 'QQQ', 'AMZN', 'AAPL', 'MSFT', 'TSLA', 'AMD', 'SHEL.L', 'BP.L', 'SSLN.L', 'SGLN.L', 'YCA.L', 'RIO.L', 'AZN.L', 'PHYS', 'PSLV', 'CEF', 'U-UN.TO'])
                if isinstance(users_dict, dict):
                    for u_data in users_dict.values():
                        if isinstance(u_data, dict):
                            wl = u_data.get('watchlist') or []
                            hist = u_data.get('history') or []
                            for tk in wl + [tr.get('ticker') for tr in hist if isinstance(tr, dict) and tr.get('ticker')]:
                                if isinstance(tk, str) and tk.strip():
                                    all_tickers.add(tk.strip().upper())

                # Process dynamically requested timeframes/tickers
                while FETCH_REQUESTS:
                    req = FETCH_REQUESTS.pop()
                    if isinstance(req, tuple) and len(req) == 3:
                        tk, p, i = req
                        df_req = fetch_yahoo_v8(tk, period=p, interval=i)
                        if not df_req.empty:
                            YF_CACHE[f"{tk}_{p}_{i}"] = (time.time(), df_req)

                for tk in list(all_tickers):
                    try:
                        df_5d = fetch_yahoo_v8(tk, period="5d", interval="5m")
                        if not df_5d.empty:
                            YF_CACHE[f"{tk}_5d_5m"] = (time.time(), df_5d)
                            YF_CACHE[f"{tk}_1d_5m"] = (time.time(), df_5d.tail(78))
                            YF_CACHE[f"{tk}_5m"] = (time.time(), df_5d)
                    except Exception:
                        pass
                    time.sleep(0.05)
            except Exception:
                pass
            time.sleep(10)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def process_auto_profile():
    engine = MarketScoringEngine()
    while True:
        try:
            portfolio_store.reload()
            df_qqq = YF_CACHE.get('QQQ_5d_5m', YF_CACHE.get('QQQ_5m', (0, pd.DataFrame())))[1]
            regime = engine.check_market_regime(df_qqq)
            eod_sweep = is_eod_sweep_time()
            
            users = portfolio_store.data.get('users', {})
            if isinstance(users, dict):
                for u in list(users.keys()):
                    ud = portfolio_store.user_data(u)
                    hist = ud.get('history', []) if isinstance(ud.get('history'), list) else []
                    holds = list(set([tr.get('ticker') for tr in hist if isinstance(tr, dict) and portfolio_store.get_shares(tr.get('ticker'), u) > 0]))
                    
                    # 1. PROCESS SELLS
                    for t in holds:
                        df = YF_CACHE.get(f"{t}_5d_5m", YF_CACHE.get(f"{t}_5m", (0, pd.DataFrame())))[1]
                        if not df.empty:
                            last_p = normalize_price(t, float(df['Close'].iloc[-1]))
                            t_buys = [tr for tr in hist if isinstance(tr, dict) and tr.get('ticker') == t and str(tr.get('action')).upper() == 'BUY']
                            avg_buy_p = normalize_price(t, t_buys[0].get('price', last_p)) if t_buys else last_p
                            hw = (ud.get('holdings', {}).get(t) or {}).get('high_water', avg_buy_p)
                            if last_p > hw:
                                if 'holdings' not in ud or not isinstance(ud['holdings'], dict): ud['holdings'] = {}
                                if t not in ud['holdings'] or not isinstance(ud['holdings'][t], dict): ud['holdings'][t] = {}
                                ud['holdings'][t]['high_water'] = last_p
                                portfolio_store.save_data(portfolio_store.data)
                            
                            sh = portfolio_store.get_shares(t, u)
                            # EOD Cash Sweep (Excludes Test A so value positions can be held long term)
                            if eod_sweep and sh > 0 and 'test a' not in u.lower():
                                portfolio_store.execute_trade(t, 'SELL', sh, last_p, username=u)
                                continue

                            if t in engine.nav_bases:
                                st = engine.score_nav_asset(t, last_p, 1.0, avg_buy_p, hw)
                            else:
                                st = engine.score_momentum(df, last_p, avg_buy_p, hw, profile=u, regime=regime)

                            if st.get('action_main') == 'SELL' and sh > 0:
                                portfolio_store.execute_trade(t, 'SELL', sh, last_p, username=u)
                                    
                    # 2. PROCESS BUYS
                    if not eod_sweep:
                        active_holds = list(set([tr.get('ticker') for tr in hist if isinstance(tr, dict) and portfolio_store.get_shares(tr.get('ticker'), u) > 0]))
                        if len(active_holds) == 0:
                            wl = ud.get('watchlist', []) if isinstance(ud.get('watchlist'), list) else []
                            best_buy, best_score, best_price = None, 0, 0
                            for t in wl:
                                if not is_market_open(t):
                                    continue
                                df = YF_CACHE.get(f"{t}_5d_5m", YF_CACHE.get(f"{t}_5m", (0, pd.DataFrame())))[1]
                                if not df.empty:
                                    last_p = normalize_price(t, float(df['Close'].iloc[-1]))
                                    if t in engine.nav_bases:
                                        st = engine.score_nav_asset(t, last_p, 1.0, 0.0, 0.0)
                                    else:
                                        st = engine.score_momentum(df, last_p, 0.0, 0.0, profile=u, regime=regime)

                                    if st.get('action_main') == 'BUY' and safe_float(st.get('score', 0)) > best_score:
                                        best_score = safe_float(st['score'])
                                        best_buy = t
                                        best_price = last_p
                                        
                            if best_buy:
                                mb = float(ud.get('master_budget', 5000.0))
                                net_hist = sum(-float(tr.get('amount', 0)) if str(tr.get('action')).upper() == 'BUY' else float(tr.get('amount', 0)) for tr in hist if isinstance(tr, dict))
                                avail_cash = mb + net_hist
                                if best_price > 0 and avail_cash >= best_price:
                                    sh = int(avail_cash // best_price)
                                    if sh > 0:
                                        portfolio_store.execute_trade(best_buy, 'BUY', sh, best_price, username=u)
        except Exception:
            pass
        time.sleep(15)

def start_threads():
    start_unified_background_worker()
    t = threading.Thread(target=process_auto_profile, daemon=True)
    t.start()