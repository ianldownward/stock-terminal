import os, json, time, urllib.request, threading, re
import xml.etree.ElementTree as ET
import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, request, render_template
from pymongo import MongoClient
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

YF_CACHE = {}
FETCH_LOCKS = {}
GLOBAL_LOCK = threading.Lock()
NEWS_CACHE = {}

def fetch_yf_data(ticker, period="1y", interval="1d"):
    if not interval or interval == 'undefined': interval = "1d"
    if not period or period == 'undefined': period = "1y"
    if period in ['1y', '5y', 'max'] and interval in ['1m', '2m', '5m', '15m', '30m', '60m', '1h']: interval = '1d'
    if period in ['1mo', '3mo', '6mo'] and interval in ['1m', '2m']: interval = '5m'
    cache_key = f"{ticker}_{period}_{interval}"
    
    with GLOBAL_LOCK:
        if cache_key not in FETCH_LOCKS: FETCH_LOCKS[cache_key] = threading.Lock()
        lock = FETCH_LOCKS[cache_key]
        
    with lock:
        now = time.time()
        cache_duration = 115 if interval in ['1m', '2m', '5m'] else 300
        if cache_key in YF_CACHE:
            cached_time, df = YF_CACHE[cache_key]
            if not df.empty and (now - cached_time < cache_duration): return df.copy()
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval)
            if not df.empty: YF_CACHE[cache_key] = (now, df)
            return df.copy()
        except Exception: return pd.DataFrame()

def send_push_notification(topic, title, message):
    if not topic: return
    try:
        url = f"https://ntfy.sh/{topic.strip()}"
        req = urllib.request.Request(url, data=message.encode('utf-8'), headers={'Title': title})
        urllib.request.urlopen(req, timeout=5)
    except Exception: pass

class PortfolioManager:
    def __init__(self):
        self.trade_lock = threading.Lock()
        self.mongo_uri = os.environ.get('MONGO_URI')
        if self.mongo_uri:
            try:
                self.client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=5000)
                self.collection = self.client['stock_terminal']['portfolio']
            except Exception: self.client = None
        else:
            self.client = None
            self.filename = 'portfolio.json'
        self.data = self.load()
        self._ensure_default_user()

    def default_user_state(self, username=""):
        prof = username.strip().lower()
        if 'test e8' in prof:
            wl = ['TQQQ', 'SOXL', 'NVDL', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'META', 'MSTR', 'PLTR', 'COIN', 'AVGO', 'SQQQ', '3SUS.L', 'SGLN.L', 'SSLN.L', 'RR.L', 'SHEL.L', 'CONL', 'MSTX', 'BITX']
        elif any(x in prof for x in ['test e9', 'test e10', 'test e11']):
            wl = ['RR.L', 'SHEL.L', 'BP.L', 'AZN.L', 'BARC.L', 'LLOY.L', 'GLEN.L', 'RIO.L', 'HSBA.L', 'GSK.L', 'ULVR.L', 'SGLN.L', 'SSLN.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'GOOGL', 'PLTR', 'MSTR', 'TQQQ', 'SOXL', 'NVDL', 'SQQQ', '3SUS.L', 'CONL', 'MSTX', 'BITX', 'JPM', 'BAC', 'AVGO']
        elif 'test q' in prof: wl = ['NVDA', 'AMD', 'GLEN.L', 'RIO.L', 'JPM', 'BAC']
        elif 'test s' in prof: wl = ['TQQQ', 'SOXL', 'NVDL', 'NVDA', 'TSLA', 'AMD', 'META', 'MSFT']
        elif any(x in prof for x in ['test p', 'test t']): wl = ['TQQQ', 'SOXL', 'NVDL', 'MSTR', 'SQQQ', '3SUS.L', 'CONL', 'MSTX', 'BITX']
        elif any(x in prof for x in ['test w', 'test w-inverse']): wl = ['TQQQ', 'SOXL', 'NVDL', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'META', 'MSTR', 'PLTR', 'COIN', 'AVGO', 'SQQQ', '3SUS.L', 'SGLN.L', 'SSLN.L', 'RR.L', 'SHEL.L']
        elif any(x in prof for x in ['test e', 'test u']): wl = ['TQQQ', 'SOXL', 'NVDL', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'META', 'MSTR', 'PLTR', 'COIN', 'AVGO', 'SQQQ', '3SUS.L']
        elif 'test c' in prof: wl = ['AZN.L', 'RR.L', 'SHEL.L', 'BP.L', 'BARC.L', 'LLOY.L', 'GLEN.L', 'RIO.L', 'HSBA.L', 'GSK.L', 'ULVR.L', 'SGLN.L', 'SSLN.L']
        elif any(x in prof for x in ['test a', 'ian']): wl = ['YCA.L', 'U-UN.TO', 'PHYS', 'PSLV', 'CEF', 'SGLN.L', 'SSLN.L', 'RIO.L', 'BP.L', 'SHEL.L', 'AZN.L']
        else: wl = ['RR.L', 'SHEL.L', 'BP.L', 'AZN.L', 'BARC.L', 'LLOY.L', 'GLEN.L', 'RIO.L', 'HSBA.L', 'GSK.L', 'ULVR.L', 'SGLN.L', 'SSLN.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'GOOGL', 'PLTR', 'MSTR', 'TQQQ', 'SOXL', 'NVDL']

        return {
            'master_budget': 5000.0,
            'watchlist': wl, 'initial_positions': {}, 'holdings': {}, 'history': [], 'notified_signals': {},
            'settings': {'period': '1d', 'interval': '1m' if any(x in prof for x in ['test p', 'test t']) else '5m', 'style': 'candlestick', 'refresh': '10000', 'ntfy_topic': ''}
        }

    def load(self):
        if self.client:
            try:
                doc = self.collection.find_one({"_id": "main_store"})
                if doc:
                    doc.pop('_id', None)
                    return doc
            except Exception: pass
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    if 'users' not in data:
                        data = {'active_user': 'Ian', 'users': { 'Ian': data }}
                        self.save_data(data)
                    return data
            except Exception: pass
        initial = {'active_user': 'Ian', 'users': {'Ian': self.default_user_state('Ian')}}
        self.save_data(initial)
        return initial

    def reload(self):
        self.data = self.load()
        self._ensure_default_user()

    def _ensure_default_user(self):
        try:
            allowed_profiles = [
                'Ian', 'Test A - Deep Value', 'Test C - 24/5 Global', 
                'Test E - Rotator', 'Test E2 - EOD Rotator', 'Test E3 - Hair-Trigger Rotator', 
                'Test E4 - Clean EOD Rotator', 'Test E5 - Micro-Stop EOD Rotator', 'Test E6 - Breakeven Rotator', 'Test E7 - Breakeven 0.5% Rotator', 'Test E8 - Meta-Adaptive Rotator', 'Test E9 - 0.2% Scalp Rotator', 'Test E10 - 0.1% Hyper-Scalp Rotator', 'Test E11 - Unfiltered Hyper-Scalper',
                'Test P - 1-Minute BB Reversion', 'Test Q - Market-Neutral StatArb', 
                'Test S - Apex Rotator', 'Test T - Elasticity Sniper', 'Test U - Tight Rotator',
                'Test W - Adaptive Volatility Rotator', 'Test W-Inverse - Opposite Adaptive Volatility'
            ]
            needs_save = False
            if 'users' not in self.data or not isinstance(self.data['users'], dict):
                self.data['users'] = {}
                needs_save = True

            existing_users = list(self.data['users'].keys())
            for u in existing_users:
                if u not in allowed_profiles and not u.startswith('Test S -'):
                    del self.data['users'][u]
                    needs_save = True

            for p in allowed_profiles:
                if p not in self.data['users'] or not self.data['users'][p].get('watchlist'):
                    self.data['users'][p] = self.default_user_state(p)
                    needs_save = True
                    
            if 'Ian' in self.data['users']:
                ian_ref = self.data['users']['Ian']
                if ian_ref.get('master_budget', 0) != 5000.0:
                    ian_ref['master_budget'] = 5000.0
                    needs_save = True

            if self.data.get('active_user') not in self.data['users']:
                self.data['active_user'] = 'Ian'
                needs_save = True
                
            if needs_save: self.save_data(self.data)
        except Exception as e: print(f"Cleanup error: {e}")

    def save_data(self, data_to_save):
        if self.client:
            try: self.collection.update_one({"_id": "main_store"}, {"$set": data_to_save}, upsert=True)
            except Exception: pass
        else:
            try:
                with open(self.filename, 'w') as f: json.dump(data_to_save, f, indent=2)
            except Exception: pass

    def active_username(self): return self.data.get('active_user', 'Ian')

    def user_data(self, username=None):
        au = username if username else self.active_username()
        if 'users' not in self.data: self.data['users'] = {}
        if au not in self.data['users'] or not self.data['users'][au].get('watchlist'):
            self.data['users'][au] = self.default_user_state(au)
            self.save_data(self.data)
        return self.data['users'][au]

    def add_user(self, username):
        if not username.strip(): return
        self.reload()
        if 'users' not in self.data: self.data['users'] = {}
        if username.strip() not in self.data['users'] or not self.data['users'][username.strip()].get('watchlist'): 
            self.data['users'][username.strip()] = self.default_user_state(username.strip())
        self.data['active_user'] = username.strip()
        self.save_data(self.data)

    def delete_user(self, username):
        self.reload()
        if 'users' in self.data and username in self.data['users'] and len(self.data['users']) > 1:
            del self.data['users'][username]
            if self.data.get('active_user') == username: self.data['active_user'] = list(self.data['users'].keys())[0]
            self.save_data(self.data)
            return True
        return False

    def switch_user(self, username):
        self.reload()
        if 'users' in self.data and username in self.data['users']:
            self.data['active_user'] = username
            self.save_data(self.data)

    def reset_all(self):
        self.reload()
        au = self.active_username()
        self.data['users'][au] = self.default_user_state(au)
        self.save_data(self.data)
        return self.user_data()

    def reset_all_profiles_to_5000(self):
        self.reload()
        for u in list(self.data.get('users', {}).keys()):
            wl = self.data['users'][u].get('watchlist', [])
            st = self.data['users'][u].get('settings', {})
            self.data['users'][u] = self.default_user_state(u)
            if wl: self.data['users'][u]['watchlist'] = wl
            if st: self.data['users'][u]['settings'] = st
            self.data['users'][u]['master_budget'] = 5000.0
        self.save_data(self.data)
        return True

    def update_settings(self, settings):
        self.reload()
        ud = self.user_data()
        if 'settings' not in ud: ud['settings'] = {}
        ud['settings'].update(settings)
        self.save_data(self.data)

    def update_budget(self, budget):
        self.reload()
        ud = self.user_data()
        ud['master_budget'] = float(budget)
        self.save_data(self.data)

    def add_watchlist(self, ticker):
        self.reload()
        ud = self.user_data()
        if 'watchlist' not in ud: ud['watchlist'] = []
        if ticker.upper() not in ud['watchlist']:
            ud['watchlist'].append(ticker.upper())
            self.save_data(self.data)

    def remove_watchlist(self, ticker):
        self.reload()
        ticker = ticker.upper()
        ud = self.user_data()
        if 'watchlist' in ud and ticker in ud['watchlist']: ud['watchlist'].remove(ticker)
        if 'holdings' in ud: ud['holdings'].pop(ticker, None)
        if 'initial_positions' in ud: ud['initial_positions'].pop(ticker, None)
        if 'history' in ud: ud['history'] = [h for h in ud['history'] if h.get('ticker') != ticker]
        if 'notified_signals' in ud: ud['notified_signals'].pop(ticker, None)
        self.save_data(self.data)

    def get_shares(self, ticker, username=None):
        if not ticker: return 0
        ud = self.user_data(username)
        init_pos = ud.get('initial_positions') or {}
        init_sh = (init_pos.get(ticker) or {}).get('shares', 0)
        net_sh = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in ud.get('history', []) if t.get('ticker') == ticker)
        return max(0, init_sh + net_sh)

    def set_holding_value(self, ticker, value_owned, current_price, username=None):
        self.reload()
        if not ticker: return 0
        ud = self.user_data(username)
        value_owned, current_price = float(value_owned), float(current_price)
        price_per_share = current_price / 100.0 if ticker.endswith('.L') and current_price > 100 else current_price
        target_sh = round(value_owned / price_per_share) if price_per_share > 0 else 0
        baseline_sh = target_sh - sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in ud.get('history', []) if t.get('ticker') == ticker)
        
        if 'initial_positions' not in ud: ud['initial_positions'] = {}
        if target_sh > 0 or value_owned > 0: ud['initial_positions'][ticker] = {'shares': baseline_sh, 'manual_val': value_owned}
        else: ud['initial_positions'].pop(ticker, None)

        curr_tot = self.get_shares(ticker, username)
        if 'holdings' not in ud: ud['holdings'] = {}
        if curr_tot > 0: 
            hw = (ud['holdings'].get(ticker) or {}).get('high_water', current_price)
            ud['holdings'][ticker] = {'shares': curr_tot, 'manual_val': round(curr_tot * price_per_share, 2), 'high_water': max(hw, current_price)}
        else: ud['holdings'].pop(ticker, None)
        self.save_data(self.data)
        return curr_tot

    def execute_trade(self, ticker, action_type, shares, price, username=None):
        with self.trade_lock:
            self.reload()
            if not ticker or shares <= 0: return None
            ud = self.user_data(username)
            
            # --- 15-SECOND REAL-WORLD BROKER LIMITER ---
            now_ts = int(time.time())
            if 'trade_cooldowns' not in ud: ud['trade_cooldowns'] = {}
            if now_ts - ud['trade_cooldowns'].get(ticker, 0) < 15:
                return None  # Block trade: within 15s cooldown
            # -------------------------------------------
            
            action = 'BUY' if 'BUY' in action_type.upper() else 'SELL'
            cost_per_sh = price / 100.0 if ticker.endswith('.L') and price > 100 else price
            
            if action == 'SELL':
                curr_tot = self.get_shares(ticker, username)
                if curr_tot < shares: shares = curr_tot
                if shares <= 0: return None
                
            if action == 'BUY':
                mb = ud.get('master_budget', 5000.0)
                hist = ud.get('history') or []
                init_pos = ud.get('initial_positions') or {}
                net_hist = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist)
                init_man = sum(pos.get('manual_val', 0.0) for pos in init_pos.values())
                avail_cash = mb + net_hist - init_man
                if (shares * cost_per_sh) > avail_cash: shares = int(avail_cash // cost_per_sh)
                if shares <= 0: return None
                
            tot_amt = round(shares * cost_per_sh, 2)
            trade_type = 'SHORT' if ticker in ['SQQQ', '3SUS.L'] else 'LONG'
            now = pd.Timestamp.now(tz='Europe/London')
            entry = {'id': str(int(time.time() * 1000)), 'ticker': ticker, 'trade_type': trade_type, 'action': action, 'shares': shares, 'price': price, 'amount': tot_amt, 'time': now.strftime('%d %b %H:%M'), 'date_str': now.strftime('%Y-%m-%d'), 'timestamp': int(now.timestamp())}
            
            if 'history' not in ud: ud['history'] = []
            ud['history'].insert(0, entry)

            curr_tot = self.get_shares(ticker, username)
            if 'holdings' not in ud: ud['holdings'] = {}
            if curr_tot > 0: 
                hw = (ud['holdings'].get(ticker) or {}).get('high_water', price)
                ud['holdings'][ticker] = {'shares': curr_tot, 'manual_val': round(curr_tot * cost_per_sh, 2), 'high_water': max(hw, price)}
            else: ud['holdings'].pop(ticker, None)
            
            # --- UPDATE COOLDOWN TRACKER ---
            ud['trade_cooldowns'][ticker] = now_ts
            
            self.save_data(self.data)
            ntfy_topic = (ud.get('settings') or {}).get('ntfy_topic', '')
            if ntfy_topic: send_push_notification(ntfy_topic, f"[{trade_type}] Trade Executed ({username or self.active_username()}): {ticker}", f"{entry['action']} {shares} shares @ £{tot_amt}")
            return entry

    def undo_trade(self, trade_id):
        self.reload()
        ud = self.user_data()
        hist = ud.get('history') or []
        trade = next((t for t in hist if t.get('id') == str(trade_id)), None)
        if not trade: return False
        hist.remove(trade)
        ud['history'] = hist
        
        curr_tot = self.get_shares(trade.get('ticker'))
        if 'holdings' not in ud: ud['holdings'] = {}
        if curr_tot > 0:
            cost = trade['price'] / 100.0 if trade['ticker'].endswith('.L') else trade['price']
            hw = (ud['holdings'].get(trade['ticker']) or {}).get('high_water', trade['price'])
            ud['holdings'][trade['ticker']] = {'shares': curr_tot, 'manual_val': round(curr_tot * cost, 2), 'high_water': hw}
        else: ud['holdings'].pop(trade['ticker'], None)
        self.save_data(self.data)
        return True

    def get_total_portfolio_value(self, username=None):
        ud = self.user_data(username)
        active_tickers = list(set([t.get('ticker') for t in ud.get('history', []) if self.get_shares(t.get('ticker'), username) > 0]))
        if not active_tickers: return 0.0

        total = 0.0
        holds = ud.get('holdings') or {}
        def fetch_val(t):
            sh = self.get_shares(t, username)
            try:
                df = fetch_yf_data(t, "1d", "1d")
                if not df.empty:
                    cps = df['Close'].iloc[-1] / (100.0 if t.endswith('.L') and df['Close'].iloc[-1] > 100 else 1.0)
                    return t, round(sh * cps, 2)
            except: pass
            return t, 0.0

        with ThreadPoolExecutor(max_workers=4) as ex:
            for t, val in ex.map(fetch_val, active_tickers):
                total += val
                if t in holds: holds[t]['manual_val'] = val
        return round(total, 2)

class MarketScoringEngine:
    def __init__(self):
        self.nav_bases = {'YCA.L': 634.0, 'U-UN.TO': 28.50, 'PHYS': 33.00, 'PSLV': 21.50, 'CEF': 22.00, 'SGLN.L': 3150.0, 'SSLN.L': 2350.0}
        self.asset_names = {
            'YCA.L': 'Yellow Cake plc', 'U-UN.TO': 'Sprott Physical Uranium Trust', 'PHYS': 'Sprott Physical Gold Trust',
            'PSLV': 'Sprott Physical Silver Trust', 'CEF': 'Sprott Physical Gold & Silver', 'GLD': 'SPDR Gold Shares',
            'SGLN.L': 'iShares Physical Gold ETC', 'SSLN.L': 'iShares Physical Silver ETC', 'MSFT': 'Microsoft Corp',
            'AAPL': 'Apple Inc.', 'NVDA': 'NVIDIA Corp', 'TSLA': 'Tesla', 'AMZN': 'Amazon', 'META': 'Meta Platforms', 'GOOGL': 'Alphabet', 'AMD': 'Advanced Micro Devices',
            'NFLX': 'Netflix', 'PLTR': 'Palantir Tech', 'COIN': 'Coinbase', 'MSTR': 'MicroStrategy', 'TQQQ': 'ProShares UltraPro QQQ',
            'SOXL': 'Direxion Daily Semi Bull 3X', 'NVDL': 'GraniteShares 2x Long NVDA', 'SQQQ': 'ProShares UltraPro Short QQQ (3x Short)',
            '3SUS.L': 'WisdomTree US NASDAQ 3x Short', 'CONL': 'GraniteShares 2x Long COIN', 'MSTX': 'Defiance 2x Daily Long MSTR', 'BITX': '2x Bitcoin Strategy ETF',
            'RR.L': 'Rolls-Royce Holdings', 'SHEL.L': 'Shell plc', 'BP.L': 'BP plc', 'BARC.L': 'Barclays plc', 'LLOY.L': 'Lloyds Banking Group', 'AZN.L': 'AstraZeneca',
            'GLEN.L': 'Glencore plc', 'RIO.L': 'Rio Tinto plc', 'HSBA.L': 'HSBC Holdings', 'GSK.L': 'GSK plc', 'ULVR.L': 'Unilever plc'
        }

    def check_market_regime(self):
        try:
            df_qqq = fetch_yf_data('QQQ', '5d', '5m')
            if not df_qqq.empty and len(df_qqq) >= 21:
                ema9 = df_qqq['Close'].ewm(span=9, adjust=False).mean()
                ema21 = df_qqq['Close'].ewm(span=21, adjust=False).mean()
                delta = df_qqq['Close'].diff()
                gain = delta.where(delta > 0, 0).rolling(14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                rs = gain / loss
                rsi = 100 - (100 / (1 + rs)).fillna(50)
                scores = (50 + (ema9 > ema21).astype(int)*50 - 25 + (rsi - 50)*0.5).clip(0, 100).round(1)
                
                score = int(scores.iloc[-1])
                sparkline = [round(x, 1) for x in scores.tail(78).tolist()]
                
                if score <= 20: state, color, code = "Deep Freeze (Capitulation)", "#00d2ff", "BEAR_FREEZE"
                elif score <= 40: state, color, code = "Cooling (Pullback)", "#ff9900", "BEAR"
                elif score <= 60: state, color, code = "Room Temp (Neutral)", "#8a8a9e", "NEUTRAL"
                elif score <= 80: state, color, code = "Heating Up (Expansion)", "#00c853", "BULL"
                else: state, color, code = "Overheating (Euphoria)", "#b388ff", "BULL_OVERHEAT"
                
                trend_diff = score - sparkline[0] if sparkline else 0
                trend_dir = "▲ Trending Up" if trend_diff > 2 else ("▼ Trending Down" if trend_diff < -2 else "► Stable")
                return {'score': score, 'state': state, 'color': color, 'code': code, 'sparkline': sparkline, 'trend': trend_dir}
        except Exception: pass
        return {'score': 50, 'state': 'Room Temp (Neutral)', 'color': '#8a8a9e', 'code': 'NEUTRAL', 'sparkline': [], 'trend': '► Stable'}

    def score_momentum(self, df_5m, current_price, avg_buy_price=0.0, highest_price=0.0, profile='test e', regime=None):
        if regime is None: regime = {'score': 50, 'state': 'Room Temp (Neutral)', 'color': '#8a8a9e', 'code': 'NEUTRAL', 'sparkline': [], 'trend': '► Stable'}
        ticker = getattr(df_5m, 'name', '')
        is_inverse = ticker in ['SQQQ', '3SUS.L']
        is_3x_etf = ticker in ['TQQQ', 'SOXL', 'NVDL', 'CONL', 'MSTX', 'BITX', 'SQQQ', '3SUS.L']
        trade_type = 'SHORT' if is_inverse else 'LONG'

        if df_5m.empty or len(df_5m) < 21:
            return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': '0.00%', 'reason': 'Insufficient intraday price history.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Tranche 0)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'No Data', 'hard_pct': -0.50, 'stop_price': current_price}

        prof = profile.lower()
        regime_score = regime.get('score', 50)
        pct_change_5d = ((current_price - df_5m['Close'].iloc[0]) / df_5m['Close'].iloc[0]) * 100.0
        now_uk = pd.Timestamp.now(tz='Europe/London')

        ema9 = df_5m['Close'].ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = df_5m['Close'].ewm(span=21, adjust=False).mean().iloc[-1]
        delta = df_5m['Close'].diff()
        rs = (delta.where(delta > 0, 0)).rolling(14).mean() / (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + rs.iloc[-1])) if not rs.empty else 50

        # EXACT OPPOSITE/INVERTED LOGIC FOR TEST W-INVERSE
        if 'test w-inverse' in prof:
            is_w_buy = (ema9 > ema21 and current_price > ema9 and rsi < 65 and pct_change_5d > 0)
            
            if is_w_buy:
                if avg_buy_price > 0:
                    return {'type': 'Inverted Volatility', 'score': 0, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': 'TEST W-INVERSE: Test W triggered BUY. Forcing OPPOSITE liquidation.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Inverted Liquidation', 'color': '#ff3d00', 'action_main': 'SELL', 'action_sub': '(Inverse Mirror)', 'action_color': '#ff3d00', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff3d00', 'health_text': 'Inverted Sell', 'hard_pct': -0.50, 'stop_price': current_price}
                return {'type': 'Inverted Volatility', 'score': 10, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': 'TEST W-INVERSE: Test W triggered BUY. Blocking Entry.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Inverted Wait', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Inverse Wait)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Inverted Wait', 'hard_pct': -0.50, 'stop_price': current_price}

            else:
                buy_score = min(100, max(50, round(50 + abs(pct_change_5d) * 10 + rsi)))
                return {'type': 'Inverted Volatility', 'score': buy_score, 'tranches': 1, 'discount': f"{pct_change_5d:.2f}%", 'reason': 'TEST W-INVERSE: Test W in WAIT mode. Executing CONTRARIAN BUY.', 'is_smart': True, 'rec_buy': round(current_price*0.99, 2), 'rec_sell': round(current_price*1.02, 2), 'status': f"Contrarian {trade_type} Entry", 'color': '#00c853', 'action_main': 'BUY', 'action_sub': f"(Contrarian Surge)", 'action_color': '#00c853', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Contrarian Tracking', 'hard_pct': -0.50, 'stop_price': round(current_price * 0.995, 2)}

        # DYNAMIC STRATEGY SWITCHING FOR TEST E8
        if 'test e8' in prof:
            if regime_score < 25:
                if avg_buy_price > 0:
                    return {'type': 'Meta Protection', 'score': 0, 'tranches': 0, 'discount': '0.00%', 'reason': f"REGIME CRASH ({regime_score}/100): Liquidating to Cash.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Crash Protection', 'color': '#ff3d00', 'action_main': 'SELL', 'action_sub': '(Cash Lock)', 'action_color': '#ff3d00', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff3d00', 'health_text': 'Crash Lock', 'hard_pct': -0.50, 'stop_price': current_price}
                return {'type': 'Meta Protection', 'score': 0, 'tranches': 0, 'discount': '0.00%', 'reason': f"REGIME CRASH ({regime_score}/100): Holding 100% Cash.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Cash Lock Active', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Crash Lock)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Cash Lock', 'hard_pct': -0.50, 'stop_price': current_price}

            elif regime_score >= 65:
                trail_pct, hard_pct = 0.75, -0.75
                if is_inverse: return {'type': 'Meta Switcher', 'score': 0, 'tranches': 0, 'discount': '0.00%', 'reason': 'Bull Regime Active: Disabling Inverse Assets.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Bull Mode', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Bull Mode)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Bull Mode', 'hard_pct': -0.75, 'stop_price': current_price}

            elif 40 <= regime_score < 65:
                trail_pct, hard_pct = 0.40, -0.40
                if rsi > 45 and avg_buy_price == 0:
                    return {'type': 'Meta Switcher', 'score': 20, 'tranches': 0, 'discount': '0.00%', 'reason': f"Chop Regime ({regime_score}/100): Awaiting Oversold Dip (RSI < 40).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Chop Filter Active', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(Chop Filter)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Chop Filter', 'hard_pct': -0.40, 'stop_price': current_price}

            else:
                trail_pct, hard_pct = 0.50, -0.50
                if not is_inverse and avg_buy_price > 0:
                    return {'type': 'Meta Switcher', 'score': 0, 'tranches': 0, 'discount': '0.00%', 'reason': f"Bear Regime ({regime_score}/100): Liquidating Long Position.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Bear Liquidation', 'color': '#ff3d00', 'action_main': 'SELL', 'action_sub': '(Bear Mode)', 'action_color': '#ff3d00', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff3d00', 'health_text': 'Bear Mode', 'hard_pct': -0.50, 'stop_price': current_price}

        # DYNAMIC ASSET-SPECIFIC TRAILING STOPS & HARD STOPS
        elif 'test w' in prof:
            if is_3x_etf: trail_pct, hard_pct = 1.25, -1.00
            elif ticker.endswith('.L') or ticker in ['PHYS', 'PSLV', 'CEF', 'GLD']: trail_pct, hard_pct = 0.30, -0.50
            else: trail_pct, hard_pct = 1.00, -1.00
        elif 'test e6' in prof: trail_pct, hard_pct = 0.75, -0.75
        elif 'test e7' in prof: trail_pct, hard_pct = 0.50, -0.50
        elif 'test e5' in prof: trail_pct, hard_pct = 0.35, -0.35
        elif 'test e9' in prof: trail_pct, hard_pct = 0.20, -0.20
        elif any(x in prof for x in ['test e10', 'test e11']): trail_pct, hard_pct = 0.10, -0.10
        elif 'test u' in prof: trail_pct, hard_pct = 0.50, -0.50
        elif any(x in prof for x in ['test e', 'test s']): trail_pct, hard_pct = 1.00, -1.00
        else: trail_pct, hard_pct = 0.50, -0.50

        health_pct, health_color, health_text = 0, "#8a8a9e", "Scanning..."

        # BREAKEVEN LOCK & TRAILING STOP EXECUTIONS
        if avg_buy_price > 0 and highest_price > 0:
            pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
            drop_from_peak_pct = ((highest_price - current_price) / highest_price) * 100.0
            peak_pnl_pct = ((highest_price - avg_buy_price) / avg_buy_price) * 100.0
            
            effective_hard_pct = hard_pct
            effective_stop_price = avg_buy_price * (1 + hard_pct / 100.0)

            if any(x in prof for x in ['test e6', 'test e7', 'test e8']) and peak_pnl_pct >= 0.50:
                effective_hard_pct = 0.10
                effective_stop_price = avg_buy_price * 1.0010
                if current_price <= effective_stop_price:
                    return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"BREAKEVEN LOCK TRIPPED ({pnl_pct:.2f}%). Peak was +{peak_pnl_pct:.2f}%.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Breakeven Lock', 'color': '#00c853', 'action_main': 'SELL', 'action_sub': '(Lock Breakeven)', 'action_color': '#00c853', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#00c853', 'health_text': f"Breakeven Lock (+{pnl_pct:.2f}%)", 'hard_pct': effective_hard_pct, 'stop_price': round(effective_stop_price, 2)}

            health_pct = int(max(0, min(100, 100 - (drop_from_peak_pct / trail_pct * 100))))
            pnl_str = f"({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)"
            
            if drop_from_peak_pct >= trail_pct or pnl_pct <= hard_pct:
                return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"STOP TRIPPED {pnl_str}. Peak: £{highest_price:.2f}.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Stop Tripped', 'color': '#ff3d00', 'action_main': 'SELL', 'action_sub': '(Stop Loss)', 'action_color': '#ff3d00', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff3d00', 'health_text': f"Stop Tripped {pnl_str}", 'hard_pct': effective_hard_pct, 'stop_price': round(effective_stop_price, 2)}
            elif health_pct >= 70:
                health_color, health_text = "#00c853", f"Strong Trend {pnl_str}"
            elif health_pct >= 40:
                health_color, health_text = "#ff9900", f"Pullback {pnl_str}"
            else:
                health_color, health_text = "#ff4a4a", f"Danger Zone {pnl_str}"
            
            return {'type': 'Intraday Momentum', 'score': 80, 'tranches': 1, 'discount': f"{pnl_pct:.2f}%", 'reason': f"RIDING TREND. High Water Mark: £{highest_price:.2f} (Stop: {trail_pct:.2f}%).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Trailing Stop Active', 'color': '#00d2ff', 'action_main': 'HOLD / WAIT', 'action_sub': '(Riding Winner)', 'action_color': '#00d2ff', 'regime': regime, 'trade_type': trade_type, 'health_pct': health_pct, 'health_color': health_color, 'health_text': health_text, 'hard_pct': effective_hard_pct, 'stop_price': round(effective_stop_price, 2)}

        # OPENING RANGE BLOCK (ORB) - Prevents buying the first 15 minutes of the session
        current_mins = now_uk.hour * 60 + now_uk.minute
        is_us_orb = (not ticker.endswith('.L')) and (870 <= current_mins < 885)
        is_uk_orb = ticker.endswith('.L') and (480 <= current_mins < 495)
        
        if is_us_orb or is_uk_orb:
            return {'type': 'Intraday Momentum', 'score': 20, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': "ORB ACTIVE: Blocking new entries during opening 15 minutes of market volatility.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'ORB Blocked', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(ORB Wait)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'ORB Filtering', 'hard_pct': hard_pct, 'stop_price': round(current_price * (1 + hard_pct / 100.0), 2)}

        # OVERRIDE: LSE CROSS-MARKET SWEEP FOR TEST W
        if 'test w' in prof and ticker.endswith('.L') and now_uk.hour == 16 and now_uk.minute >= 20 and now_uk.minute < 30:
            if avg_buy_price > 0:
                pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                return {'type': 'LSE Sweep', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': "LSE CROSS-MARKET SWEEP.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'LSE Cash Sweep', 'color': '#ff9900', 'action_main': 'SELL', 'action_sub': '(LSE Sweep)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'Forced LSE Sell', 'hard_pct': 0.0, 'stop_price': current_price}

        # OVERRIDE: EOD SWEEP RULE (INCLUDES TEST U)
        if any(x in prof for x in ['test e2', 'test e4', 'test e5', 'test e6', 'test e7', 'test e8', 'test e9', 'test e10', 'test e11', 'test u']) and now_uk.hour == 20 and now_uk.minute >= 50:
            if avg_buy_price > 0:
                pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                return {'type': 'EOD Sweep', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': "EOD ROTATOR SWEEP: Liquidating to 100% cash.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'EOD Cash Sweep', 'color': '#ff9900', 'action_main': 'SELL', 'action_sub': '(EOD Sweep)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'Forced EOD Sell', 'hard_pct': 0.0, 'stop_price': current_price}
            return {'type': 'EOD Sweep', 'score': 0, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': "EOD ROTATOR SWEEP: Blocking new entries.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'EOD Block Active', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(EOD Blocked)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'EOD Blocked', 'hard_pct': 0.0, 'stop_price': current_price}

        # OVERRIDE: QUICK 1.2% TARGET FOR 3x ETFs
        if any(x in prof for x in ['test e', 'test s', 'test u', 'test w']) and is_3x_etf and avg_buy_price > 0:
            pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
            if pnl_pct >= 1.20:
                return {'type': 'Rotator Target', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': 'Quick 1.2% Target Hit on 3x ETF.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Take Profit', 'color': '#00d2ff', 'action_main': 'SELL', 'action_sub': '(Target Hit)', 'action_color': '#00d2ff', 'regime': regime, 'trade_type': trade_type, 'health_pct': 100, 'health_color': '#00d2ff', 'health_text': 'Target Hit (Selling)', 'hard_pct': 1.20, 'stop_price': round(avg_buy_price * 1.012, 2)}

        # ENTRY LOGIC (EMA + Volume Filter)
        if ema9 > ema21 and current_price > ema9 and rsi < 65 and pct_change_5d > 0:
            vol_20ma = df_5m['Volume'].tail(20).mean() if len(df_5m) >= 20 else 1.0
            cur_vol = df_5m['Volume'].iloc[-1]
            vol_ratio = (cur_vol / vol_20ma) if vol_20ma > 0 else 1.0
            
            if 'test e11' not in prof and any(x in prof for x in ['test e4', 'test e5', 'test e6', 'test e7', 'test e8', 'test e9', 'test e10']) and vol_ratio < 1.20:
                return {'type': 'Intraday Momentum', 'score': 45, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"EMA Surge set, but Volume ({vol_ratio:.1f}x) below 1.2x threshold.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Low Volume', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Vol)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...', 'hard_pct': hard_pct, 'stop_price': round(current_price * (1 + hard_pct / 100.0), 2)}

            buy_score = min(100, max(50, round(50 + pct_change_5d * 10 + (70 - rsi))))
            return {'type': 'Intraday Momentum', 'score': buy_score, 'tranches': 1, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"SURGE DETECTED: Price > 9-EMA > 21-EMA, RSI {rsi:.1f}, Vol {vol_ratio:.1f}x.", 'is_smart': True, 'rec_buy': round(current_price*0.99, 2), 'rec_sell': round(current_price*1.02, 2), 'status': f"Fast {trade_type} Surge", 'color': '#00c853', 'action_main': 'BUY', 'action_sub': f"({trade_type} Surge)", 'action_color': '#00c853', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...', 'hard_pct': hard_pct, 'stop_price': round(current_price * (1 + hard_pct / 100.0), 2)}

        return {'type': 'Intraday Momentum', 'score': 10, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"Awaiting fast EMA crossover surge.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'No Setup', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Setup)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...', 'hard_pct': hard_pct, 'stop_price': round(current_price * (1 + hard_pct / 100.0), 2)}

    def score_nav_asset(self, ticker, current_price, volume_ratio, avg_buy_price=0.0, highest_price=0.0):
        nav = self.nav_bases.get(ticker, current_price * 1.10)
        implied_discount = ((nav - current_price) / nav) * 100.0
        buy_score = min(max(round(min(max((implied_discount/20.0)*80.0, 0), 80) + min(max((volume_ratio/2.0)*20.0, 0), 20), 2), 0), 100)
        tranches = 0 if implied_discount <= 0 else min(5, int(buy_score // 20) + 1)
        
        hard_pct = -0.50
        stop_price = round(avg_buy_price * (1 + hard_pct / 100.0), 2) if avg_buy_price > 0 else current_price
        health_pct, health_color, health_text = 0, "#8a8a9e", "Scanning..."

        if avg_buy_price > 0 and highest_price > 0:
            pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
            drop_from_peak_pct = ((highest_price - current_price) / highest_price) * 100.0
            pnl_str = f"({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)"
            trail_pct = 0.50

            health_pct = int(max(0, min(100, 100 - (drop_from_peak_pct / trail_pct * 100))))
            if drop_from_peak_pct >= trail_pct or pnl_pct <= hard_pct:
                health_color, health_text = "#ff3d00", f"Stop Tripped {pnl_str}"
            elif health_pct >= 70:
                health_color, health_text = "#00c853", f"Strong Trend {pnl_str}"
            elif health_pct >= 40:
                health_color, health_text = "#ff9900", f"Pullback {pnl_str}"
            else:
                health_color, health_text = "#ff4a4a", f"Danger Zone {pnl_str}"

        if implied_discount <= 5.0 and implied_discount > -50.0:
            action_main, action_sub, status, color = "SELL", "(Take Profit)", "Target Reached", "#ff3d00"
            reason, action_color, tranches = f"Profit Target Triggered. NAV discount shrunk to {implied_discount:.1f}%.", "#ff3d00", 0
        elif buy_score >= 40:
            action_main, action_sub = "BUY", f"(Tranche {tranches})"
            status, color = ('Deep Value Anomaly', '#00c853') if buy_score >= 60 else ('Moderate Value', '#ff9900')
            reason, action_color = f"Physical NAV Anomaly. Trading at {implied_discount:.1f}% discount to NAV ({nav}).", "#00c853"
        else:
            action_main, action_sub = "HOLD / WAIT", f"(Tranche {tranches})"
            status, color = ('Trading at Premium', '#ff4a4a') if implied_discount < 0 else ('Low Value', '#8a8a9e')
            reason, action_color = f"Trading at {implied_discount:.1f}% NAV discount. Active Tranches: {tranches}.", "#8a8a9e"

        return {'type': 'Physical Trust', 'score': buy_score, 'tranches': tranches, 'discount': f"{implied_discount:.2f}%" if implied_discount>0 else f"+{abs(implied_discount):.2f}%", 'reason': reason, 'is_smart': True, 'rec_buy': round(current_price*0.98, 2), 'rec_sell': round(nav*0.95, 2), 'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color, 'regime': {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e', 'sparkline': [], 'trend': '► Stable'}, 'trade_type': 'LONG', 'health_pct': health_pct, 'health_color': health_color, 'health_text': health_text, 'hard_pct': hard_pct, 'stop_price': stop_price}

    def score_equity(self, df, current_price, avg_buy_price=0.0, highest_price=0.0):
        if df.empty or 'Close' not in df:
            return {'type': 'Global Equity', 'score': 0, 'tranches': 0, 'discount': '0.00%', 'reason': 'Awaiting data.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '', 'action_color': '#8a8a9e', 'regime': {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e', 'sparkline': [], 'trend': '► Stable'}, 'trade_type': 'LONG', 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'N/A', 'hard_pct': -0.50, 'stop_price': round(current_price * 0.995, 2)}
            
        dma = df['Close'].tail(200).mean() if len(df) >= 200 else df['Close'].mean()
        implied_discount = ((dma - current_price) / dma) * 100.0
        delta = df['Close'].diff()
        rs = (delta.where(delta > 0, 0)).rolling(14).mean() / (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + rs.iloc[-1]))
        
        avg_vol = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
        vol_rat = (df['Volume'].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
        
        buy_score = min(max(round(min(max((implied_discount/25.0)*50.0, 0), 50) + (max(0, (40-rsi)/40*30) if pd.notna(rsi) else 0) + min(max((vol_rat/2.0)*20.0, 0), 20), 2), 0), 100)
        tranches = 0 if implied_discount < 0 else min(5, int(buy_score // 20) + 1)
        
        hard_pct = -0.50
        stop_price = round(avg_buy_price * (1 + hard_pct / 100.0), 2) if avg_buy_price > 0 else current_price
        health_pct, health_color, health_text = 0, "#8a8a9e", "Scanning..."

        if avg_buy_price > 0 and highest_price > 0:
            pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
            drop_from_peak_pct = ((highest_price - current_price) / highest_price) * 100.0
            pnl_str = f"({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)"
            trail_pct = 0.50

            health_pct = int(max(0, min(100, 100 - (drop_from_peak_pct / trail_pct * 100))))
            if drop_from_peak_pct >= trail_pct or pnl_pct <= hard_pct:
                health_color, health_text = "#ff3d00", f"Stop Tripped {pnl_str}"
            elif health_pct >= 70:
                health_color, health_text = "#00c853", f"Strong Trend {pnl_str}"
            elif health_pct >= 40:
                health_color, health_text = "#ff9900", f"Pullback {pnl_str}"
            else:
                health_color, health_text = "#ff4a4a", f"Danger Zone {pnl_str}"

        if buy_score >= 40:
            action_main, action_sub = "BUY", f"(Tranche {tranches})"
            status, color = ('Deep Value Anomaly', '#00c853') if buy_score >= 60 else ('Moderate Value', '#ff9900')
            reason, action_color = f"Value Anomaly. Trading at {implied_discount:.1f}% discount to 200d-DMA.", "#00c853"
        elif implied_discount <= -10.0:
            action_main, action_sub, status, color, tranches = "SELL", "(Take Profit)", 'Overextended (High)', '#ff3d00', 0
            reason, action_color = f"Overextended ({abs(implied_discount):.1f}% above 200d-DMA). Take profits.", "#ff3d00"
        else:
            action_main, action_sub, status, color = "HOLD / WAIT", f"(Tranche {tranches})", 'Fair Value', '#8a8a9e'
            reason, action_color = f"No Value Anomaly. Near 200d-DMA.", "#8a8a9e"
            
        return {'type': 'Global Equity', 'score': buy_score, 'tranches': tranches, 'discount': f"{implied_discount:.2f}%" if implied_discount>0 else f"+{abs(implied_discount):.2f}%", 'reason': reason, 'is_smart': True, 'rec_buy': round(dma*0.9, 2), 'rec_sell': round(dma*1.05, 2), 'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color, 'regime': {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e', 'sparkline': [], 'trend': '► Stable'}, 'trade_type': 'LONG', 'health_pct': health_pct, 'health_color': health_color, 'health_text': health_text, 'hard_pct': hard_pct, 'stop_price': stop_price}

portfolio_store = PortfolioManager()

def process_auto_profile(prof_name):
    try:
        ud = portfolio_store.user_data(prof_name)
        engine = MarketScoringEngine()
        active_profile = prof_name.strip().lower()
        
        is_auto_profile = any(x in active_profile for x in ['test a', 'test c', 'test e', 'test p', 'test q', 'test s', 'test t', 'test u', 'test w'])
        if not is_auto_profile: return

        scan_list = ud.get('watchlist') or []
        mb = ud.get('master_budget', 5000.0)
        hist = ud.get('history') or []
        init_pos = ud.get('initial_positions') or {}

        net_history = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist)
        init_manual = sum(pos.get('manual_val', 0.0) for pos in init_pos.values())
        cash_balance = mb + net_history - init_manual
        rem_cash = max(0, cash_balance)

        tot_own = portfolio_store.get_total_portfolio_value(prof_name)
        total_equity = cash_balance + tot_own

        dirs, buys, held_scores = [], [], []
        MIN_BUY_VALUE = 20.0
        regime = engine.check_market_regime()

        dfs = {}
        def fetch_data_thread(tick): 
            interval = "1m" if any(x in active_profile for x in ['test p', 'test t']) else ("1d" if 'test a' in active_profile else "5m")
            period = "1d" if any(x in active_profile for x in ['test p', 'test t']) else ("1y" if 'test a' in active_profile else "5d")
            return tick, fetch_yf_data(tick, period, interval)

        with ThreadPoolExecutor(max_workers=4) as ex:
            for tick, df in ex.map(fetch_data_thread, scan_list): dfs[tick] = df

        for t in scan_list:
            t = t.strip().upper()
            if not t: continue
            try:
                df = dfs.get(t)
                if df is None or df.empty: continue
                df.name = t
                cur = df['Close'].iloc[-1]
                sh = portfolio_store.get_shares(t, prof_name)
                cps = cur / 100.0 if t.endswith('.L') and cur > 100 else cur
                vo = sh * cps

                now_uk = pd.Timestamp.now(tz='Europe/London')
                is_us_stock = not t.endswith('.L')
                current_mins = now_uk.hour * 60 + now_uk.minute
                
                if now_uk.weekday() >= 5: is_open = False
                elif is_us_stock: is_open = (14 * 60 + 30) <= current_mins < (21 * 60)
                else: is_open = (8 * 60) <= current_mins < (16 * 60 + 30)

                avg_buy_p, highest_p = 0.0, cur
                if sh > 0:
                    t_buys = [tr for tr in hist if tr.get('ticker') == t and tr.get('action') == 'BUY']
                    if t_buys: avg_buy_p = t_buys[0].get('price', 0.0)
                    highest_p = (ud.get('holdings', {}).get(t) or {}).get('high_water', cur)
                    if cur > highest_p:
                        highest_p = cur
                        if 'holdings' not in ud: ud['holdings'] = {}
                        if t not in ud['holdings']: ud['holdings'][t] = {}
                        ud['holdings'][t]['high_water'] = highest_p

                if 'test a' in active_profile:
                    avg_v = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
                    v_r = (df['Volume'].iloc[-1] / avg_v) if avg_v > 0 else 1.0
                    st = engine.score_nav_asset(t, cur, v_r, avg_buy_price=avg_buy_p, highest_price=highest_p) if t in engine.nav_bases else engine.score_equity(df, cur, avg_buy_price=avg_buy_p, highest_price=highest_p)
                else:
                    st = engine.score_momentum(df, cur, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=active_profile, regime=regime)

                if sh > 0:
                    held_scores.append({'ticker': t, 'shares': sh, 'price': cur, 'cps': cps, 'value': vo, 'score': st['score'], 'action': st['action_main']})

                if st['action_main'] == 'SELL' and sh > 0 and vo >= MIN_BUY_VALUE:
                    if is_open or 'test a' in active_profile:
                        portfolio_store.execute_trade(t, 'SELL', sh, cur, prof_name)
                elif st['action_main'] == 'BUY':
                    if is_open or 'test a' in active_profile:
                        buys.append({'t': t, 'cps': cps, 'p': cur, 's': st['score']})
            except Exception: pass

        max_allowed_holds = 5 if 'test a' in active_profile else (1 if any(x in active_profile for x in ['test e', 'test s', 'test u', 'test w']) else 3)
        swap_hurdle = 5 if 'test e3' in active_profile or 'test e11' in active_profile else 10

        if buys and held_scores:
            top_candidate = max(buys, key=lambda x: x['s'])
            weakest_holding = min(held_scores, key=lambda x: x['score'])
            
            if top_candidate['s'] >= (weakest_holding['score'] + swap_hurdle) and top_candidate['s'] >= 60:
                portfolio_store.execute_trade(weakest_holding['ticker'], 'SELL', weakest_holding['shares'], weakest_holding['price'], prof_name)
                
                ud = portfolio_store.user_data(prof_name)
                hist = ud.get('history') or []
                net_history = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist)
                init_manual = sum(pos.get('manual_val', 0.0) for pos in (ud.get('initial_positions') or {}).values())
                rem_cash = max(0, mb + net_history - init_manual)
                
                bs = int(rem_cash // top_candidate['cps'])
                amt = round(bs * top_candidate['cps'], 2)
                if bs > 0 and amt >= MIN_BUY_VALUE:
                    portfolio_store.execute_trade(top_candidate['t'], 'BUY', bs, top_candidate['p'], prof_name)
                return

        current_hold_count = len([x for x in held_scores if x['shares'] > 0])
        slots_available = max(0, max_allowed_holds - current_hold_count)

        if buys and rem_cash >= MIN_BUY_VALUE and slots_available > 0:
            buys.sort(key=lambda x: x['s'], reverse=True)
            top_candidate = buys[0]
            
            per_stock_budget = min(rem_cash, total_equity * (0.98 if any(x in active_profile for x in ['test e', 'test s', 'test u', 'test w']) else 0.33))
            bs = int(per_stock_budget // top_candidate['cps'])
            amt = round(bs * top_candidate['cps'], 2)
            if bs > 0 and amt >= MIN_BUY_VALUE and amt <= rem_cash:
                portfolio_store.execute_trade(top_candidate['t'], 'BUY', bs, top_candidate['p'], prof_name)
    except Exception: pass

def global_background_worker():
    time.sleep(3)
    while True:
        try:
            bot_tickers = ['SQQQ', '3SUS.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'PLTR', 'COIN', 'CONL', 'MSTX', 'BITX', 'AZN.L', 'BARC.L', 'LLOY.L', 'GLEN.L', 'RIO.L', 'HSBA.L', 'GSK.L', 'ULVR.L', 'SGLN.L', 'SSLN.L', 'TQQQ', 'SOXL', 'NVDL', 'QQQ', 'YCA.L', 'U-UN.TO', 'PHYS', 'PSLV', 'CEF', 'JPM', 'BAC', 'AVGO']
            def prefetch(tk): 
                fetch_yf_data(tk, "5d", "5m")
                fetch_yf_data(tk, "1d", "1m")
            with ThreadPoolExecutor(max_workers=4) as ex:
                ex.map(prefetch, set(bot_tickers))

            auto_profiles = ['Test A - Deep Value', 'Test C - 24/5 Global', 'Test E - Rotator', 'Test E2 - EOD Rotator', 'Test E3 - Hair-Trigger Rotator', 'Test E4 - Clean EOD Rotator', 'Test E5 - Micro-Stop EOD Rotator', 'Test E6 - Breakeven Rotator', 'Test E7 - Breakeven 0.5% Rotator', 'Test E8 - Meta-Adaptive Rotator', 'Test E9 - 0.2% Scalp Rotator', 'Test E10 - 0.1% Hyper-Scalp Rotator', 'Test E11 - Unfiltered Hyper-Scalper', 'Test P - 1-Minute BB Reversion', 'Test Q - Market-Neutral StatArb', 'Test S - Apex Rotator', 'Test T - Elasticity Sniper', 'Test U - Tight Rotator', 'Test W - Adaptive Volatility Rotator', 'Test W-Inverse - Opposite Adaptive Volatility']
            for prof in auto_profiles:
                process_auto_profile(prof)
        except Exception as e: 
            print(f"Background Loop Error: {e}")
        time.sleep(120)

threading.Thread(target=global_background_worker, daemon=True).start()

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/users', methods=['GET'])
def get_users(): 
    portfolio_store.reload()
    return jsonify({'users': list(portfolio_store.data.get('users', {}).keys()), 'active_user': portfolio_store.active_username()})

@app.route('/api/users/select', methods=['POST'])
def select_user():
    portfolio_store.reload()
    portfolio_store.switch_user((request.get_json() or {}).get('username', ''))
    return jsonify({'status': 'ok'})

@app.route('/api/users/add', methods=['POST'])
def add_user():
    portfolio_store.reload()
    portfolio_store.add_user((request.get_json() or {}).get('username', ''))
    return jsonify({'status': 'ok'})

@app.route('/api/users/delete', methods=['POST'])
def delete_user():
    portfolio_store.reload()
    return jsonify({'status': 'ok' if portfolio_store.delete_user((request.get_json() or {}).get('username', '')) else 'error'})

@app.route('/api/portfolio', methods=['GET'])
def get_portfolio():
    portfolio_store.reload()
    return jsonify(portfolio_store.user_data())

@app.route('/api/portfolio/reset', methods=['POST'])
def reset_portfolio():
    portfolio_store.reload()
    return jsonify({'status': 'ok', 'portfolio': portfolio_store.reset_all()})

@app.route('/api/portfolio/reset_all_profiles', methods=['POST'])
def reset_all_profiles():
    portfolio_store.reload()
    portfolio_store.reset_all_profiles_to_5000()
    return jsonify({'status': 'ok'})

@app.route('/api/portfolio/settings', methods=['POST'])
def update_settings():
    portfolio_store.reload()
    portfolio_store.update_settings((request.get_json() or {}).get('settings', {}))
    return jsonify({'status': 'ok'})

@app.route('/api/portfolio/budget', methods=['POST'])
def update_budget():
    portfolio_store.reload()
    portfolio_store.update_budget((request.get_json() or {}).get('budget', 5000))
    return jsonify({'status': 'ok'})

@app.route('/api/watchlist/add', methods=['POST'])
def add_watchlist():
    portfolio_store.reload()
    portfolio_store.add_watchlist((request.get_json() or {}).get('ticker', ''))
    return jsonify({'status': 'ok'})

@app.route('/api/watchlist/delete', methods=['POST'])
def delete_watchlist():
    portfolio_store.reload()
    portfolio_store.remove_watchlist((request.get_json() or {}).get('ticker', ''))
    return jsonify({'status': 'ok'})

@app.route('/api/portfolio/holding', methods=['POST'])
def update_holding():
    portfolio_store.reload()
    b = request.get_json() or {}
    shares = portfolio_store.set_holding_value(b.get('ticker'), b.get('value_owned', 0), b.get('price', 1))
    return jsonify({'status': 'ok', 'shares': shares})

@app.route('/api/trade/execute', methods=['POST'])
def execute_trade():
    portfolio_store.reload()
    b = request.get_json() or {}
    return jsonify({'status': 'ok', 'entry': portfolio_store.execute_trade(b.get('ticker'), b.get('action'), b.get('shares'), b.get('price'), b.get('username'))})

@app.route('/api/trade/undo', methods=['POST'])
def undo_trade(): 
    portfolio_store.reload()
    return jsonify({'status': 'ok' if portfolio_store.undo_trade((request.get_json() or {}).get('id')) else 'error'})

@app.route('/api/score', methods=['GET'])
def get_score():
    t = request.args.get('t', '').upper()
    try:
        active_profile = portfolio_store.active_username().strip().lower()
        is_momentum = any(x in active_profile for x in ['test c', 'test e', 'test p', 'test q', 'test s', 'test t', 'test u', 'test w'])
        interval = "1m" if any(x in active_profile for x in ['test p', 'test t']) else "5m"
        period = "1d" if any(x in active_profile for x in ['test p', 'test t']) else "5d"
        df = fetch_yf_data(t, period if is_momentum else "1y", interval if is_momentum else "1d")
        if df.empty: return jsonify({'error': 'Ticker not found.'}), 400
        df.name = t
        cur = df['Close'].iloc[-1]
        engine = MarketScoringEngine()
        
        if is_momentum:
            regime = engine.check_market_regime()
            res = engine.score_momentum(df, cur, profile=active_profile, regime=regime)
        else:
            avg_vol = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
            v_rat = (df['Volume'].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
            res = engine.score_nav_asset(t, cur, v_rat) if t in engine.nav_bases else engine.score_equity(df, cur)
            
        res.update({'ticker': t, 'name': engine.asset_names.get(t, t), 'price': round(cur, 2)})
        return jsonify(res)
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/directives', methods=['GET'])
def get_directives():
    portfolio_store.reload()
    ud = portfolio_store.user_data()
    prof_name = portfolio_store.active_username()
    active_profile = prof_name.strip().lower()
    engine = MarketScoringEngine()
    
    mb = ud.get('master_budget', 5000.0)
    hist = ud.get('history') or []
    init_pos = ud.get('initial_positions') or {}

    net_history = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist)
    init_manual = sum(pos.get('manual_val', 0.0) for pos in init_pos.values())
    cash_balance = mb + net_history - init_manual
    rem_cash = max(0, cash_balance)
    
    wl = ud.get('watchlist') or []
    active_holds = list(set([tr.get('ticker') for tr in hist if portfolio_store.get_shares(tr.get('ticker')) > 0]))
    scan_list = list(set(wl + active_holds))
    
    dirs = []
    if scan_list:
        regime = engine.check_market_regime()
        is_momentum = any(x in active_profile for x in ['test c', 'test e', 'test p', 'test q', 'test s', 'test t', 'test u', 'test w'])
        
        dfs = {}
        def fetch_t(tick): 
            interval = "1m" if any(x in active_profile for x in ['test p', 'test t']) else "5m"
            period = "1d" if any(x in active_profile for x in ['test p', 'test t']) else "5d"
            return tick, fetch_yf_data(tick, period if is_momentum else "1y", interval if is_momentum else "5d")
            
        with ThreadPoolExecutor(max_workers=4) as ex:
            for tick, df_t in ex.map(fetch_t, scan_list): dfs[tick] = df_t
            
        for t in scan_list:
            df = dfs.get(t)
            if df is None or df.empty: continue
            cur = df['Close'].iloc[-1]
            sh = portfolio_store.get_shares(t)
            cps = cur / 100.0 if t.endswith('.L') and cur > 100 else cur
            
            now_uk = pd.Timestamp.now(tz='Europe/London')
            is_us_stock = not t.endswith('.L')
            current_mins = now_uk.hour * 60 + now_uk.minute
            
            if now_uk.weekday() >= 5: is_open = False
            elif is_us_stock: is_open = (14 * 60 + 30) <= current_mins < (21 * 60)
            else: is_open = (8 * 60) <= current_mins < (16 * 60 + 30)
            
            if is_momentum:
                avg_buy_p, highest_p = 0.0, cur
                if sh > 0:
                    t_buys = [tr for tr in hist if tr.get('ticker') == t and tr.get('action') == 'BUY']
                    if t_buys: avg_buy_p = t_buys[0].get('price', 0.0)
                    highest_p = (ud.get('holdings', {}).get(t) or {}).get('high_water', cur)
                st = engine.score_momentum(df, cur, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=active_profile, regime=regime)
            else:
                avg_vol = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
                v_rat = (df['Volume'].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
                avg_buy_p, highest_p = 0.0, cur
                if sh > 0:
                    t_buys = [tr for tr in hist if tr.get('ticker') == t and tr.get('action') == 'BUY']
                    if t_buys: avg_buy_p = t_buys[0].get('price', 0.0)
                    highest_p = (ud.get('holdings', {}).get(t) or {}).get('high_water', cur)
                st = engine.score_nav_asset(t, cur, v_rat, avg_buy_price=avg_buy_p, highest_price=highest_p) if t in engine.nav_bases else engine.score_equity(df, cur, avg_buy_price=avg_buy_p, highest_price=highest_p)
            
            tot_own = portfolio_store.get_total_portfolio_value(prof_name)
            total_eq = cash_balance + tot_own
            
            is_automated = active_profile != 'ian' and 'test a' not in active_profile
            
            if not is_automated:
                if st['action_main'] == 'BUY' and sh == 0 and rem_cash >= 20:
                    allocation = 0.33 if is_momentum else 0.20
                    bs = int(min(rem_cash, total_eq * allocation) // cps) 
                    if bs > 0: dirs.append({'ticker': t, 'action': 'BUY', 'shares': bs, 'price': cur, 'amount': bs * cps, 'score': st['score']})
                elif st['action_main'] == 'SELL' and sh > 0:
                    dirs.append({'ticker': t, 'action': 'SELL', 'shares': sh, 'price': cur, 'amount': sh * cps, 'score': st['score']})

    dirs.sort(key=lambda x: (0 if x['action'] == 'SELL' else 1, -x.get('score', 0)))
    
    all_pending_actions = []
    now_lon = pd.Timestamp.now(tz='Europe/London')
    current_mins_now = now_lon.hour * 60 + now_lon.minute

    for u, u_data in portfolio_store.data.get('users', {}).items():
        if not isinstance(u_data, dict): continue
        u_prof_clean = u.strip().lower()
        
        if u_prof_clean != 'ian' and 'test a' not in u_prof_clean: continue

        u_hist = u_data.get('history') or []
        u_holds = list(set([tr.get('ticker') for tr in u_hist if portfolio_store.get_shares(tr.get('ticker'), u) > 0]))
        for tk in u_holds:
            try:
                is_us = not tk.endswith('.L')
                mkt_open = ((14 * 60 + 30) <= current_mins_now < (21 * 60)) if is_us else ((8 * 60) <= current_mins_now < (16 * 60 + 30))
                if not mkt_open: continue

                df_u = fetch_yf_data(tk, "5d", "5m")
                if not df_u.empty:
                    df_u.name = tk
                    cur_u = df_u['Close'].iloc[-1]
                    sh_u = portfolio_store.get_shares(tk, u)
                    buys_u = [tr for tr in u_hist if tr.get('ticker') == tk and tr.get('action') == 'BUY']
                    avg_b_u = buys_u[0].get('price', 0.0) if buys_u else cur_u
                    hw_u = (u_data.get('holdings', {}).get(tk) or {}).get('high_water', cur_u)
                    st_u = engine.score_equity(df_u, cur_u, avg_buy_price=avg_b_u, highest_price=hw_u)
                    if st_u['action_main'] == 'SELL' and sh_u > 0:
                        all_pending_actions.append({'user': u, 'ticker': tk, 'action': 'SELL', 'shares': sh_u, 'price': cur_u, 'reason': st_u['reason']})
            except: pass

    return jsonify({'directives': dirs, 'ian_directives': dirs if active_profile == 'ian' else [], 'all_pending_actions': all_pending_actions})

@app.route('/api/recommend', methods=['GET'])
def get_recommendations():
    res, engine = [], MarketScoringEngine()
    tickers = ['YCA.L', 'U-UN.TO', 'SGLN.L', 'SSLN.L', 'PHYS', 'PSLV', 'CEF', 'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'GOOGL', 'META', 'BHP', 'RIO', 'VALE', 'XOM', 'CVX', 'OXY', 'JPM', 'BAC', 'GS', 'PFE', 'JNJ', 'UNH', 'DIS', 'NKE', 'SBUX', 'BA', 'LMT']
    
    def fetch_rec(tick): return tick, fetch_yf_data(tick, "1y", "1d")
        
    with ThreadPoolExecutor(max_workers=4) as ex:
        for t, df in ex.map(fetch_rec, tickers):
            if not df.empty:
                cur, avg_vol = df['Close'].iloc[-1], df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
                st = engine.score_nav_asset(t, cur, (df['Volume'].iloc[-1]/avg_vol) if avg_vol > 0 else 1.0) if t in engine.nav_bases else engine.score_equity(df, cur)
                st.update({'ticker': t, 'name': engine.asset_names.get(t, t), 'price': round(cur, 2)})
                res.append(st)
    return jsonify({'recommendations': sorted(res, key=lambda x: x['score'], reverse=True)})

@app.route('/api/data', methods=['GET'])
def get_data():
    try:
        portfolio_store.reload()
        ud = portfolio_store.user_data()
        wl = ud.get('watchlist') or []
        t = request.args.get('t', '').upper().strip()
        active_profile = portfolio_store.active_username().strip().lower()
        is_momentum = any(x in active_profile for x in ['test c', 'test e', 'test p', 'test q', 'test s', 'test t', 'test u', 'test w'])
        is_rotator = any(x in active_profile for x in ['test e', 'test s', 'test u', 'test w'])
        
        if not t: t = 'ALL_SHARES'

        hist = ud.get('history') or []
        init_pos = ud.get('initial_positions') or {}
        mb = ud.get('master_budget', 5000.0)
        active_holds = list(set([tr.get('ticker') for tr in hist if portfolio_store.get_shares(tr.get('ticker')) > 0]))

        pnl_dfs_5m = {}
        def fetch_pnl_data_5m(tick): return tick, fetch_yf_data(tick, "5d", "5m")
        if active_holds:
            with ThreadPoolExecutor(max_workers=4) as ex:
                for tick, df_5m in ex.map(fetch_pnl_data_5m, active_holds): pnl_dfs_5m[tick] = df_5m

        net_history = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist)
        init_manual = sum(pos.get('manual_val', 0.0) for pos in init_pos.values())
        cash_balance = mb + net_history - init_manual
        tot_own = portfolio_store.get_total_portfolio_value()
        total_equity = cash_balance + tot_own
        master_pnl_val = total_equity - mb
        master_pnl_pct = (master_pnl_val / mb) * 100.0 if mb > 0 else 0.0

        now_lon = pd.Timestamp.now(tz='Europe/London')
        today_str = now_lon.strftime('%Y-%m-%d')
        current_mins = now_lon.hour * 60 + now_lon.minute
        
        all_user_holds = set()
        user_active_hold_status = {}
        for u, u_data in portfolio_store.data.get('users', {}).items():
            hist_u = u_data.get('history', [])
            u_holds = [tr.get('ticker') for tr in hist_u if portfolio_store.get_shares(tr.get('ticker'), u) > 0]
            
            user_active_hold_status[u] = False
            for tr_tk in u_holds:
                all_user_holds.add(tr_tk)
                is_us = not tr_tk.endswith('.L')
                is_open = ((14 * 60 + 30) <= current_mins < (21 * 60)) if is_us else ((8 * 60) <= current_mins < (16 * 60 + 30))
                if is_open: user_active_hold_status[u] = True

            for tr in hist_u:
                if tr.get('date_str') == today_str:
                    all_user_holds.add(tr.get('ticker'))

        prices = {}
        def fetch_lb_price(tick):
            df_lb = fetch_yf_data(tick, "5d", "5m")
            if df_lb.empty: return tick, 0.0, 0.0
            cur_p = df_lb['Close'].iloc[-1]
            div = 100.0 if tick.endswith('.L') and cur_p > 100 else 1.0
            cur_pounds = cur_p / div
            
            if df_lb.index.tz is None: df_lb.index = df_lb.index.tz_localize('UTC')
            else: df_lb.index = df_lb.index.tz_convert('UTC')
            
            last_lon = df_lb.index[-1].tz_convert('Europe/London')
            if now_lon.date() > last_lon.date(): start_pounds = cur_pounds
            else:
                prev_sessions = df_lb[df_lb.index.tz_convert('Europe/London').strftime('%Y-%m-%d') < today_str]
                start_p = prev_sessions['Close'].iloc[-1] if not prev_sessions.empty else df_lb['Close'].iloc[0]
                start_pounds = start_p / div
            return tick, cur_pounds, start_pounds

        with ThreadPoolExecutor(max_workers=6) as ex:
            for tick, cur_pnd, start_pnd in ex.map(fetch_lb_price, all_user_holds): prices[tick] = {'cur': cur_pnd, 'start': start_pnd}

        leaderboard = []
        for u, u_data in portfolio_store.data.get('users', {}).items():
            if not isinstance(u_data, dict): continue
            mb_lb = u_data.get('master_budget', 5000.0)
            hist_lb = u_data.get('history') or []
            init_pos_lb = u_data.get('initial_positions') or {}
            
            nh_lb = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist_lb)
            im_lb = sum(pos.get('manual_val', 0.0) for pos in init_pos_lb.values())
            cash_now = mb_lb + nh_lb - im_lb
            
            equity_now = 0.0
            shares_now_map = {}
            for tr in hist_lb:
                tk = tr.get('ticker')
                if tk not in shares_now_map: shares_now_map[tk] = portfolio_store.get_shares(tk, u)
                
            for tk, sh_now in shares_now_map.items():
                if sh_now <= 0: continue
                p_data = prices.get(tk, {'cur': 0.0, 'start': 0.0})
                equity_now += sh_now * p_data['cur']
                
            tot_eq_now = max(0, cash_now) + equity_now

            trades_today = [tr for tr in hist_lb if tr.get('date_str') == today_str]
            net_trade_cash_today = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in trades_today)
            
            held_at_8am = {}
            for tk, sh_now in shares_now_map.items():
                if sh_now > 0 or any(tr.get('ticker') == tk for tr in trades_today):
                    sh_8am = sh_now
                    for tr in trades_today:
                        if tr.get('ticker') == tk:
                            if tr.get('action') == 'BUY': sh_8am -= tr.get('shares', 0)
                            else: sh_8am += tr.get('shares', 0)
                    if sh_8am > 0: held_at_8am[tk] = sh_8am
            
            cash_8am = cash_now - net_trade_cash_today
            equity_8am = sum(sh * prices.get(tk, {}).get('start', prices.get(tk, {}).get('cur', 0.0)) for tk, sh in held_at_8am.items())
            tot_eq_8am = cash_8am + equity_8am
            
            daily_pnl_val = round(tot_eq_now - tot_eq_8am, 2) if tot_eq_8am > 0 else 0.0

            leaderboard.append({
                'user': u, 
                'equity': round(tot_eq_now, 2), 
                'budget': mb_lb, 
                'daily_pnl': daily_pnl_val,
                'has_active_holds': user_active_hold_status.get(u, False)
            })
        leaderboard.sort(key=lambda x: x['equity'], reverse=True)

        settings = ud.get('settings') or {}
        req_p = request.args.get('p')
        if not req_p or req_p == 'undefined': req_p = settings.get('period', '1d')
        req_i = request.args.get('i')
        if not req_i or req_i == 'undefined': req_i = settings.get('interval', '1m' if any(x in active_profile for x in ['test p', 'test t']) else '5m')

        if req_p in ['1y', '5y', 'max'] and req_i in ['1m', '2m', '5m', '15m', '30m', '60m', '1h']: req_i = '1d'
        elif req_p in ['1mo', '3mo', '6mo'] and req_i in ['1m', '2m']: req_i = '5m'

        wl_status = {}
        if wl:
            def check_status(tick):
                try:
                    df_stat = fetch_yf_data(tick, "5d", "5m")
                    if df_stat.empty: return tick, 'closed'
                    if df_stat.index.tz is not None: df_stat.index = df_stat.index.tz_convert('UTC')
                    if (time.time() - df_stat.index[-1].timestamp()) > 1800: return tick, 'closed' 
                    if len(df_stat) > 1:
                        if df_stat['Close'].iloc[-1] > df_stat['Close'].iloc[-2]: return tick, 'up'
                        elif df_stat['Close'].iloc[-1] < df_stat['Close'].iloc[-2]: return tick, 'down'
                    return tick, 'closed'
                except Exception: return tick, 'closed'

            with ThreadPoolExecutor(max_workers=4) as ex:
                for tick, status in ex.map(check_status, wl): wl_status[tick] = status
            
        tot_today_diff, tot_1h_diff = 0.0, 0.0
        now_utc = pd.Timestamp.now(tz='UTC')
        
        active_holds_and_traded = list(set([tr.get('ticker') for tr in hist if portfolio_store.get_shares(tr.get('ticker')) > 0 or tr.get('date_str') == today_str]))

        for tk in active_holds_and_traded:
            sh_h = portfolio_store.get_shares(tk)
            t_buys_today = [tr for tr in hist if tr.get('ticker') == tk and tr.get('action') == 'BUY' and tr.get('date_str') == now_lon.strftime('%Y-%m-%d')]
            t_sells_today = [tr for tr in hist if tr.get('ticker') == tk and tr.get('action') == 'SELL' and tr.get('date_str') == now_lon.strftime('%Y-%m-%d')]
            
            df_5m = pnl_dfs_5m.get(tk)
            if df_5m is None: df_5m = fetch_yf_data(tk, "5d", "5m")
            
            if df_5m is not None and not df_5m.empty:
                cur_price = df_5m['Close'].iloc[-1]
                div = 100.0 if tk.endswith('.L') and cur_price > 100 else 1.0
                cur_price_pounds = cur_price / div
                
                target_ts = now_utc - pd.Timedelta(hours=1)
                prior_df = df_5m[df_5m.index <= target_ts]
                p_1h_base_pence = prior_df['Close'].iloc[-1] if not prior_df.empty else (df_5m['Close'].iloc[0] if not df_5m.empty else cur_price)
                p_1h_base_pounds = p_1h_base_pence / div

                if sh_h > 0:
                    tot_1h_diff += sh_h * (cur_price_pounds - p_1h_base_pounds)

        active_lb_user = next((x for x in leaderboard if x['user'].strip().lower() == active_profile), None)
        tot_today_diff = active_lb_user['daily_pnl'] if active_lb_user else 0.0
        
        master_today_pct = (tot_today_diff / mb * 100.0) if mb > 0 else 0.0
        master_1h_pct = (tot_1h_diff / mb * 100.0) if mb > 0 else 0.0

        master_pnl_data = {
            'all': {'val': f"{'+' if master_pnl_val>0 else ''}£{master_pnl_val:.2f} ({'+' if master_pnl_pct>0 else ''}{master_pnl_pct:.2f}%)", 'color': '#00c853' if master_pnl_val > 0 else ('#ff3d00' if master_pnl_val < 0 else '#8a8a9e')},
            'today': {'val': f"{'+' if tot_today_diff>0 else ''}£{tot_today_diff:.2f} ({'+' if master_today_pct>0 else ''}{master_today_pct:.2f}%)", 'color': '#00c853' if tot_today_diff > 0 else ('#ff3d00' if tot_today_diff < 0 else '#8a8a9e')},
            '1h': {'val': f"{'+' if tot_1h_diff>0 else ''}£{tot_1h_diff:.2f} ({'+' if master_1h_pct>0 else ''}{master_1h_pct:.2f}%)", 'color': '#00c853' if tot_1h_diff > 0 else ('#ff3d00' if tot_1h_diff < 0 else '#8a8a9e')}
        }

        engine = MarketScoringEngine()
        regime = engine.check_market_regime()

        if t == 'ALL_SHARES':
            health_pct, health_color, health_text = 0, '#8a8a9e', 'No Data'
            hard_pct_val, stop_price_val = -0.50, 0.0
            if is_rotator:
                if active_holds:
                    held_t = active_holds[0]
                    df_held = pnl_dfs_5m.get(held_t) if pnl_dfs_5m.get(held_t) is not None else fetch_yf_data(held_t, req_p, req_i)
                    if df_held is not None and not df_held.empty:
                        last_p = df_held['Close'].iloc[-1]
                        avg_buy_p = 0.0
                        t_buys = [tr for tr in hist if tr.get('ticker') == held_t and tr.get('action') == 'BUY']
                        if t_buys: avg_buy_p = t_buys[0].get('price', 0.0)
                        highest_p = (ud.get('holdings', {}).get(held_t) or {}).get('high_water', last_p)
                        st = engine.score_momentum(df_held, last_p, avg_buy_p, highest_p, active_profile, regime)
                        health_pct = st.get('health_pct', 0)
                        health_color = st.get('health_color', '#8a8a9e')
                        health_text = f"{held_t} - {st.get('health_text', 'Tracking')}"
                        hard_pct_val = st.get('hard_pct', -0.50)
                        stop_price_val = st.get('stop_price', 0.0)
                else:
                    top_score = 0
                    top_stock = None
                    for tick in wl[:10]:
                        df_t = fetch_yf_data(tick, req_p, req_i)
                        if not df_t.empty:
                            st = engine.score_momentum(df_t, df_t['Close'].iloc[-1], 0, 0, active_profile, regime)
                            if st['score'] > top_score:
                                top_score = st['score']
                                top_stock = tick
                    if top_stock:
                        health_pct = top_score
                        health_color = '#00c853' if top_score >= 65 else ('#ff9900' if top_score >= 40 else '#00d2ff')
                        health_text = f"Hunting: {top_stock} ({top_score}/100)"

            lines = []
            if wl:
                def fetch_t(tick): return tick, fetch_yf_data(tick, req_p, req_i)
                dfs = {}
                with ThreadPoolExecutor(max_workers=4) as ex:
                    for tick, df_t in ex.map(fetch_t, wl[:15]): dfs[tick] = df_t
                
                colors = ['#00d2ff', '#00c853', '#ff3d00', '#ff9900', '#b388ff', '#ffff00', '#ff4081', '#18ffff']
                c_idx = 0
                for tick in wl[:15]: 
                    df_t = dfs.get(tick)
                    if df_t is not None and not df_t.empty:
                        if df_t.index.tz is not None: df_t.index = df_t.index.tz_convert('UTC')
                        base_price = df_t['Close'].iloc[0]
                        if base_price > 0:
                            line_data = []
                            seen = set()
                            for idx, row in df_t.iterrows():
                                ts = idx.strftime('%Y-%m-%d') if req_i in ['1d','5d','1wk','1mo','3mo'] else int(idx.timestamp())
                                if ts not in seen:
                                    seen.add(ts)
                                    pct_change = ((row['Close'] - base_price) / base_price) * 100.0
                                    line_data.append({'time': ts, 'value': round(pct_change, 2)})
                            lines.append({ 'ticker': tick, 'color': colors[c_idx % len(colors)], 'data': line_data })
                            c_idx += 1
            
            return jsonify({'is_multi': True, 'lines': lines, 'wl_status': wl_status, 'name': 'Relative Performance (Watchlist)', 'portfolio': ud, 'leaderboard': leaderboard, 'metrics': {
                'price': 0, 'price_display': f"Normalized %", 'discount': '--', 'buy_score': '--', 'tranches': 0,
                'status': regime['state'], 'color': regime['color'], 'reason': f"Sentiment Thermometer: {regime['score']}/100 ({regime['state']}).",
                'action_main': '--', 'action_sub': '', 'action_color': '#8a8a9e',
                'shares_owned': sum(portfolio_store.get_shares(tk) for tk in active_holds), 'value_owned': tot_own, 
                'pnl_display': master_pnl_data['all']['val'], 'pnl_color': master_pnl_data['all']['color'], 'pnl_data': master_pnl_data,
                'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data, 'master_budget': mb,
                'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime,
                'health_pct': health_pct, 'health_color': health_color, 'health_text': health_text, 'is_rotator_profile': is_rotator,
                'hard_pct': hard_pct_val, 'stop_price': stop_price_val
            }})

        df = fetch_yf_data(t, req_p, req_i)
        if df.empty:
            return jsonify({'ohlc': [], 'wl_status': wl_status, 'name': f'{t} Data Loading...', 'portfolio': ud, 'leaderboard': leaderboard, 'metrics': {
                'price': 0, 'price_display': '£0.00', 'discount': '--', 'buy_score': '0 / 100', 'tranches': 0,
                'status': 'Loading Data', 'color': '#787e8e', 'reason': 'Fetching fresh market quotes.',
                'action_main': 'WAIT', 'action_sub': '', 'action_color': '#787e8e', 'shares_owned': 0, 'value_owned': 0.0,
                'pnl_display': '£0.00 (0.00%)', 'pnl_color': '#8a8a9e', 'pnl_data': master_pnl_data, 'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data,
                'master_budget': mb, 'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime,
                'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': '', 'is_rotator_profile': is_rotator,
                'hard_pct': -0.50, 'stop_price': 0.0
            }})

        if df.index.tz is not None: df.index = df.index.tz_convert('UTC')
        df.name = t
        data = [{'time': i.strftime('%Y-%m-%d') if req_i in ['1d','5d','1wk','1mo','3mo'] else int(i.timestamp()), 'open': round(r['Open'],2), 'high': round(r['High'],2), 'low': round(r['Low'],2), 'close': round(r['Close'],2)} for i, r in df.iterrows()]
        seen = set(); data = [x for x in data if x['time'] not in seen and not seen.add(x['time'])]
        last_p = round(data[-1]['close'], 2) if data else 0

        sh_own = portfolio_store.get_shares(t)
        cps = last_p / 100.0 if t.endswith('.L') and last_p > 100 else last_p
        val_own = round(sh_own * cps, 2)

        avg_buy_p, highest_p = 0.0, last_p
        holds_dict = ud.get('holdings') or {}
        if sh_own > 0:
            t_buys = [tr for tr in hist if tr.get('ticker') == t and tr.get('action') == 'BUY']
            if t_buys: avg_buy_p = t_buys[0].get('price', 0.0)
            highest_p = (holds_dict.get(t) or {}).get('high_water', last_p)

        if is_momentum:
            st = engine.score_momentum(df, last_p, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=active_profile, regime=regime)
        else:
            av = df['Volume'].tail(20).mean() if len(df)>=20 else 1.0
            st = engine.score_nav_asset(t, last_p, (df['Volume'].iloc[-1]/av) if av>0 else 1.0, avg_buy_price=avg_buy_p, highest_price=highest_p) if t in engine.nav_bases else engine.score_equity(fetch_yf_data(t, "1y", "1d"), last_p, avg_buy_price=avg_buy_p, highest_price=highest_p)

        cb_t = (init_pos.get(t) or {}).get('manual_val', 0.0) + sum(tr.get('amount', 0) if tr.get('action')=='BUY' else -tr.get('amount', 0) for tr in hist if tr.get('ticker')==t)
        
        ticker_pnl_data = {}
        if sh_own > 0 and cb_t > 0:
            pv, pp = round(val_own - cb_t, 2), round((val_own - cb_t) / cb_t * 100, 2)
            c = '#00c853' if pv > 0 else ('#ff3d00' if pv < 0 else '#8a8a9e')
            pnl_d = f"{'+' if pv>0 else ''}£{pv:.2f} ({'+' if pp>0 else ''}{pp:.2f}%)"

            df_5m = pnl_dfs_5m.get(t)
            if df_5m is not None and not df_5m.empty:
                if df_5m.index.tz is None: df_5m.index = df_5m.index.tz_localize('UTC')
                else: df_5m.index = df_5m.index.tz_convert('UTC')
                
                cur_p = df_5m['Close'].iloc[-1]
                div = 100.0 if t.endswith('.L') and cur_p > 100 else 1.0
                cur_p_pounds = cur_p / div
                last_ts = df_5m.index[-1]
                
                pv_1h = 0.0
                ticker_pnl_data = {
                    'all': {'val': pnl_d, 'color': c},
                    'today': {'val': master_pnl_data['today']['val'], 'color': master_pnl_data['today']['color']},
                    '1h': {'val': f"{'+' if pv_1h>0 else ''}£{pv_1h:.2f} (0.00%)", 'color': '#8a8a9e'}
                }
            else:
                ticker_pnl_data = {'all': {'val': pnl_d, 'color': c}, 'today': master_pnl_data['today'], '1h': master_pnl_data['1h']}
        else: 
            pnl_d, c = "£0.00 (0.00%)", '#8a8a9e'
            empty_pnl = {'val': pnl_d, 'color': c}
            ticker_pnl_data = {'all': empty_pnl, 'today': master_pnl_data['today'], '1h': master_pnl_data['1h']}

        if sh_own > 0:
            if 'holdings' not in ud: ud['holdings'] = {}
            if t not in ud['holdings']: ud['holdings'][t] = {}
            ud['holdings'][t]['shares'] = sh_own
            ud['holdings'][t]['manual_val'] = val_own

        mathLine = []
        try:
            if is_momentum:
                ema21_series = df['Close'].ewm(span=21, adjust=False).mean()
                ema21_dict = {idx.strftime('%Y-%m-%d') if req_i in ['1d','5d','1wk','1mo','3mo'] else int(idx.timestamp()): val for idx, val in ema21_series.items()}
                for d in data:
                    if d['time'] in ema21_dict: mathLine.append({'time': d['time'], 'value': round(ema21_dict[d['time']], 2)})
            else:
                if t in engine.nav_bases:
                    target = engine.nav_bases[t]
                    for d in data: mathLine.append({'time': d['time'], 'value': round(((target - d['close']) / target) * 100.0, 2)})
                else:
                    df_1y = fetch_yf_data(t, "1y", "1d")
                    if not df_1y.empty:
                        sma200 = df_1y['Close'].rolling(200, min_periods=1).mean()
                        sma_dict = {idx.strftime('%Y-%m-%d'): val for idx, val in sma200.items() if pd.notna(val)}
                        last_valid = sma200.dropna().iloc[-1] if not sma200.dropna().empty else last_p
                    else:
                        sma_dict, last_valid = {}, last_p
                    for d in data:
                        time_str = d['time'] if isinstance(d['time'], str) else pd.to_datetime(d['time'], unit='s').strftime('%Y-%m-%d')
                        val = sma_dict.get(time_str, last_valid)
                        mathLine.append({'time': d['time'], 'value': round(((val - d['close']) / val) * 100.0 if val > 0 else 0, 2)})
        except Exception: mathLine = []

        return jsonify({'ohlc': data, 'mathLine': mathLine, 'wl_status': wl_status, 'name': engine.asset_names.get(t, t), 'portfolio': ud, 'leaderboard': leaderboard, 'metrics': {
            'price': last_p, 'price_display': f"{last_p:.2f}p (£{(last_p/100.0):.2f})" if t.endswith('.L') and last_p>100 else f"£{last_p:.2f}",
            'discount': st['discount'], 'buy_score': f"{st['score']} / 100", 'tranches': st['tranches'],
            'status': st['status'], 'color': st['color'], 'reason': st['reason'],
            'action_main': st['action_main'], 'action_sub': st['action_sub'], 'action_color': st['action_color'],
            'shares_owned': sh_own, 'value_owned': val_own, 'pnl_display': ticker_pnl_data['all']['val'], 'pnl_color': ticker_pnl_data['all']['color'], 'pnl_data': ticker_pnl_data,
            'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data, 'master_budget': mb,
            'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime, 'trade_type': st.get('trade_type', 'LONG'),
            'health_pct': st.get('health_pct', 0), 'health_color': st.get('health_color', '#8a8a9e'), 'health_text': st.get('health_text', 'N/A'), 'is_rotator_profile': is_rotator,
            'hard_pct': st.get('hard_pct', -0.50), 'stop_price': st.get('stop_price', 0.0)
        }})
    except Exception as e: 
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8095))
    app.run(host='0.0.0.0', port=port, debug=False)