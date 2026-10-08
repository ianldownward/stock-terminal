import time, threading
import pandas as pd
import yfinance as yf
from portfolio import portfolio_store
from engine import MarketScoringEngine, normalize_price

YF_CACHE = {}

def start_unified_background_worker():
    def _worker():
        while True:
            try:
                users_dict = portfolio_store.data.get('users', {}) if isinstance(getattr(portfolio_store, 'data', None), dict) else {}
                all_tickers = set(['NVDA', 'TQQQ', 'SOXL', 'QQQ', 'AMZN', 'AAPL', 'MSFT', 'TSLA', 'AMD'])
                if isinstance(users_dict, dict):
                    for u_data in users_dict.values():
                        if isinstance(u_data, dict):
                            wl = u_data.get('watchlist') or []
                            hist = u_data.get('history') or []
                            for tk in wl + [tr.get('ticker') for tr in hist if isinstance(tr, dict) and tr.get('ticker')]:
                                if isinstance(tk, str) and tk.strip():
                                    all_tickers.add(tk.strip().upper())

                for tk in list(all_tickers):
                    try:
                        df_single = yf.Ticker(tk).history(period="1d", interval="5m")
                        if isinstance(df_single, pd.DataFrame) and not df_single.empty:
                            if isinstance(df_single.columns, pd.MultiIndex):
                                df_single.columns = df_single.columns.get_level_values(0)
                            YF_CACHE[f"{tk}_5m"] = (time.time(), df_single)
                    except Exception:
                        pass
                    time.sleep(0.3)
            except Exception:
                pass
            time.sleep(15)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def process_auto_profile():
    engine = MarketScoringEngine()
    while True:
        try:
            portfolio_store.reload()
            df_qqq = YF_CACHE.get('QQQ_5m', (0, pd.DataFrame()))[1]
            regime = engine.check_market_regime(df_qqq)
            
            for u in list(portfolio_store.data.get('users', {}).keys()):
                if 'test a' in u.lower(): 
                    continue
                    
                ud = portfolio_store.user_data(u)
                hist = ud.get('history', [])
                holds = list(set([tr.get('ticker') for tr in hist if portfolio_store.get_shares(tr.get('ticker'), u) > 0]))
                
                # Sells
                for t in holds:
                    df = YF_CACHE.get(f"{t}_5m", (0, pd.DataFrame()))[1]
                    if not df.empty:
                        last_p = normalize_price(t, df['Close'].iloc[-1])
                        t_buys = [tr for tr in hist if tr.get('ticker') == t and str(tr.get('action')).upper() == 'BUY']
                        avg_buy_p = normalize_price(t, t_buys[0].get('price', last_p)) if t_buys else last_p
                        hw = (ud.get('holdings', {}).get(t) or {}).get('high_water', avg_buy_p)
                        if last_p > hw:
                            if 'holdings' not in ud: ud['holdings'] = {}
                            if t not in ud['holdings']: ud['holdings'][t] = {}
                            ud['holdings'][t]['high_water'] = last_p
                            portfolio_store.save_data(portfolio_store.data)
                            
                        st = engine.score_momentum(df, last_p, avg_buy_p, hw, profile=u, regime=regime)
                        if st.get('action_main') == 'SELL':
                            sh = portfolio_store.get_shares(t, u)
                            if sh > 0:
                                portfolio_store.execute_trade(t, 'SELL', sh, last_p, username=u)
                                
                # Buys
                active_holds = list(set([tr.get('ticker') for tr in hist if portfolio_store.get_shares(tr.get('ticker'), u) > 0]))
                if len(active_holds) == 0:
                    wl = ud.get('watchlist', [])
                    best_buy, best_score, best_price = None, 0, 0
                    for t in wl:
                        df = YF_CACHE.get(f"{t}_5m", (0, pd.DataFrame()))[1]
                        if not df.empty:
                            last_p = normalize_price(t, df['Close'].iloc[-1])
                            st = engine.score_momentum(df, last_p, 0.0, 0.0, profile=u, regime=regime)
                            if st.get('action_main') == 'BUY' and st.get('score', 0) > best_score:
                                best_score = st['score']
                                best_buy = t
                                best_price = last_p
                                
                    if best_buy:
                        mb = safe_float(ud.get('master_budget'), 5000.0)
                        net_hist = sum(-safe_float(tr.get('amount', 0)) if str(tr.get('action')).upper() == 'BUY' else safe_float(tr.get('amount', 0)) for tr in hist)
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