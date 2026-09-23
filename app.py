import os, json, time, urllib.request
import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, request, render_template_string
from pymongo import MongoClient
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- IN-MEMORY CACHING LAYER ---
YF_CACHE = {}

def fetch_yf_data(ticker, period="1y", interval="1d"):
    if not interval or interval == 'undefined': interval = "1d"
    if not period or period == 'undefined': period = "1y"
    
    # HARD YFINANCE API LIMIT OVERRIDES
    if period in ['1y', '5y', 'max'] and interval in ['1m', '2m', '5m', '15m', '30m', '60m', '1h']:
        interval = '1d'
    if period in ['1mo', '3mo', '6mo'] and interval in ['1m', '2m']:
        interval = '5m'

    cache_key = f"{ticker}_{period}_{interval}"
    now = time.time()
    
    if cache_key in YF_CACHE:
        cached_time, df = YF_CACHE[cache_key]
        if not df.empty and now - cached_time < 15:  
            return df.copy()
            
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval)
        if not df.empty:
            YF_CACHE[cache_key] = (now, df)
        return df.copy()
    except:
        return pd.DataFrame()

def send_push_notification(topic, title, message):
    if not topic: return
    try:
        url = f"https://ntfy.sh/{topic.strip()}"
        req = urllib.request.Request(url, data=message.encode('utf-8'), headers={'Title': title})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Notification push error: {e}")

class PortfolioManager:
    def __init__(self):
        self.mongo_uri = os.environ.get('MONGO_URI')
        if self.mongo_uri:
            try:
                self.client = MongoClient(self.mongo_uri)
                self.db = self.client['stock_terminal']
                self.collection = self.db['portfolio']
            except Exception as e:
                print(f"MongoDB connection error: {e}")
                self.client = None
        else:
            self.client = None
            self.filename = 'portfolio.json'
        self.data = self.load()
        self._ensure_default_user()

    def default_user_state(self, username=""):
        return {
            'master_budget': 5000.0 if 'test' in username.lower() else 10000.0,
            'watchlist': [], 'initial_positions': {}, 'holdings': {}, 'history': [],
            'notified_signals': {}, 'settings': {'period': '1mo', 'interval': '1d', 'style': 'candlestick', 'refresh': '10000', 'ntfy_topic': ''}
        }

    def load(self):
        if self.client:
            try:
                doc = self.collection.find_one({"_id": "main_store"})
                if doc:
                    doc.pop('_id', None)
                    if 'users' in doc:
                        for u in doc['users'].values():
                            if 'notified_signals' not in u: u['notified_signals'] = {}
                    return doc
            except: pass
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    if 'users' not in data or not isinstance(data['users'], dict):
                        if 'users' not in data:
                            mig_u = {
                                'master_budget': data.get('master_budget', 10000.0), 'watchlist': data.get('watchlist', []),
                                'initial_positions': data.get('initial_positions', {}), 'holdings': data.get('holdings', {}),
                                'history': data.get('history', []), 'notified_signals': {}, 'settings': data.get('settings', self.default_user_state()['settings'])
                            }
                            data = {'active_user': 'Ian', 'users': { 'Ian': mig_u }}
                            self.save_data(data)
                        return data
            except: pass
        initial = {'active_user': 'Ian', 'users': {'Ian': self.default_user_state('Ian')}}
        self.save_data(initial)
        return initial

    def reload(self):
        self.data = self.load()
        self._ensure_default_user()

    def _ensure_default_user(self):
        try:
            default_profiles = ['Ian', 'Test', 'Test 2', 'Test 3', 'Test 4 - Volatility Breakout', 'Test 5 - Rel Strength Rotator']
            if 'users' not in self.data or not self.data['users']:
                self.data['users'] = {}
                for p in default_profiles:
                    self.data['users'][p] = self.default_user_state(p)
                self.data['active_user'] = 'Ian'
                self.save_data(self.data)
            
            needs_save = False
            for profile in default_profiles:
                if profile not in self.data['users']:
                    self.data['users'][profile] = self.default_user_state(profile)
                    needs_save = True
                    
            if self.data.get('active_user') not in self.data['users']:
                self.data['active_user'] = list(self.data['users'].keys())[0]
                needs_save = True
                
            if needs_save:
                self.save_data(self.data)
        except: pass

    def save_data(self, data_to_save):
        if self.client:
            try: self.collection.update_one({"_id": "main_store"}, {"$set": data_to_save}, upsert=True)
            except Exception as e: print(f"Error saving to MongoDB: {e}")
        else:
            try:
                with open(self.filename, 'w') as f: json.dump(data_to_save, f, indent=2)
            except Exception as e: print(f"Error saving locally: {e}")

    def save(self):
        try:
            fresh = self.load()
            if fresh:
                mod_user = self.data.get('active_user')
                self.data['active_user'] = fresh.get('active_user', mod_user)
                if 'users' not in self.data: self.data['users'] = {}
                for u, u_data in fresh.get('users', {}).items():
                    if u != mod_user:
                        self.data['users'][u] = u_data
        except: pass
        self.save_data(self.data)

    def active_username(self):
        return self.data.get('active_user', 'Ian')

    def user_data(self):
        au = self.active_username()
        if 'users' not in self.data: self.data['users'] = {}
        if au not in self.data['users']:
            self.data['users'][au] = self.default_user_state(au)
            self.save_data(self.data)
        return self.data['users'][au]

    def add_user(self, username):
        username = username.strip()
        if not username: return
        if 'users' not in self.data: self.data['users'] = {}
        if username not in self.data['users']: self.data['users'][username] = self.default_user_state(username)
        self.data['active_user'] = username
        self.save_data(self.data)

    def delete_user(self, username):
        username = username.strip()
        if 'users' in self.data and username in self.data['users'] and len(self.data['users']) > 1:
            del self.data['users'][username]
            if self.data.get('active_user') == username: self.data['active_user'] = list(self.data['users'].keys())[0]
            self.save_data(self.data)
            return True
        return False

    def switch_user(self, username):
        username = username.strip()
        if 'users' in self.data and username in self.data['users']:
            self.data['active_user'] = username
            self.save_data(self.data)

    def reset_all(self):
        au = self.active_username()
        self.data['users'][au] = self.default_user_state(au)
        self.save()
        return self.user_data()

    def update_settings(self, settings):
        ud = self.user_data()
        if 'settings' not in ud: ud['settings'] = {}
        ud['settings'].update(settings)
        self.save()

    def add_watchlist(self, ticker):
        ticker = ticker.upper()
        ud = self.user_data()
        if 'watchlist' not in ud: ud['watchlist'] = []
        if ticker not in ud['watchlist']:
            ud['watchlist'].append(ticker)
            self.save()

    def remove_watchlist(self, ticker):
        ticker = ticker.upper()
        ud = self.user_data()
        if 'watchlist' in ud and ticker in ud['watchlist']: ud['watchlist'].remove(ticker)
        ud.get('holdings', {}).pop(ticker, None)
        ud.get('initial_positions', {}).pop(ticker, None)
        if 'history' in ud: ud['history'] = [h for h in ud['history'] if h.get('ticker') != ticker]
        ud.get('notified_signals', {}).pop(ticker, None)
        self.save()

    def get_net_trade_shares(self, ticker):
        return sum(t['shares'] if t['action'] == 'BUY' else -t['shares'] for t in self.user_data().get('history', []) if t['ticker'] == ticker)

    def get_shares(self, ticker):
        if not ticker: return 0
        init_sh = self.user_data().get('initial_positions', {}).get(ticker, {}).get('shares', 0)
        return max(0, init_sh + self.get_net_trade_shares(ticker))

    def update_budget(self, budget):
        self.user_data()['master_budget'] = float(budget)
        self.save()

    def set_holding_value(self, ticker, value_owned, current_price):
        if not ticker: return 0
        ud = self.user_data()
        value_owned, current_price = float(value_owned), float(current_price)
        price_per_share = current_price / 100.0 if ticker.endswith('.L') and current_price > 100 else current_price

        target_sh = round(value_owned / price_per_share) if price_per_share > 0 else 0
        baseline_sh = target_sh - self.get_net_trade_shares(ticker)
        
        if 'initial_positions' not in ud: ud['initial_positions'] = {}
        if target_sh > 0 or value_owned > 0: ud['initial_positions'][ticker] = {'shares': baseline_sh, 'manual_val': value_owned}
        else: ud['initial_positions'].pop(ticker, None)

        curr_tot = self.get_shares(ticker)
        if 'holdings' not in ud: ud['holdings'] = {}
        if curr_tot > 0: 
            hw = ud['holdings'].get(ticker, {}).get('high_water', current_price)
            ud['holdings'][ticker] = {'shares': curr_tot, 'manual_val': round(curr_tot * price_per_share, 2), 'high_water': max(hw, current_price)}
        else: ud['holdings'].pop(ticker, None)
        self.save()
        return curr_tot

    def execute_trade(self, ticker, action_type, shares, price):
        if not ticker or shares <= 0: return None
        ud = self.user_data()
        shares, price = int(shares), round(float(price), 2)
        cost_per_sh = price / 100.0 if ticker.endswith('.L') and price > 100 else price
        tot_amt = round(shares * cost_per_sh, 2)

        now = pd.Timestamp.now(tz='Europe/London')
        entry = {
            'id': str(int(time.time() * 1000)), 'ticker': ticker, 'action': 'BUY' if 'BUY' in action_type.upper() else 'SELL',
            'shares': shares, 'price': price, 'amount': tot_amt, 'time': now.strftime('%d %b %H:%M'),
            'date_str': now.strftime('%Y-%m-%d'), 'timestamp': int(now.timestamp())
        }
        if 'history' not in ud: ud['history'] = []
        ud['history'].insert(0, entry)

        curr_tot = self.get_shares(ticker)
        if 'holdings' not in ud: ud['holdings'] = {}
        if curr_tot > 0: 
            hw = ud['holdings'].get(ticker, {}).get('high_water', price)
            ud['holdings'][ticker] = {'shares': curr_tot, 'manual_val': round(curr_tot * cost_per_sh, 2), 'high_water': max(hw, price)}
        else: ud['holdings'].pop(ticker, None)
        self.save()

        ntfy_topic = ud.get('settings', {}).get('ntfy_topic', '')
        if ntfy_topic: send_push_notification(ntfy_topic, f"Trade Executed: {ticker}", f"{entry['action']} {shares} shares @ £{tot_amt}")
        return entry

    def undo_trade(self, trade_id):
        ud = self.user_data()
        hist = ud.get('history', [])
        trade = next((t for t in hist if t['id'] == str(trade_id)), None)
        if not trade: return False
        hist.remove(trade)
        
        curr_tot = self.get_shares(trade['ticker'])
        if 'holdings' not in ud: ud['holdings'] = {}
        if curr_tot > 0:
            cost = trade['price'] / 100.0 if trade['ticker'].endswith('.L') else trade['price']
            hw = ud['holdings'].get(trade['ticker'], {}).get('high_water', trade['price'])
            ud['holdings'][trade['ticker']] = {'shares': curr_tot, 'manual_val': round(curr_tot * cost, 2), 'high_water': hw}
        else: ud['holdings'].pop(trade['ticker'], None)
        self.save()
        return True

    def get_total_portfolio_value(self):
        ud = self.user_data()
        hd = ud.get('holdings', {})
        active_tickers = [t for t in list(hd.keys()) if self.get_shares(t) > 0]
        
        if not active_tickers:
            return 0.0
            
        def fetch_val(t):
            sh = self.get_shares(t)
            try:
                df = fetch_yf_data(t, "1d", "1d")
                if not df.empty:
                    p = df['Close'].iloc[-1]
                    cps = p / 100.0 if t.endswith('.L') and p > 100 else p
                    return t, round(sh * cps, 2)
            except: pass
            return t, hd[t].get('manual_val', 0.0)

        total = 0.0
        with ThreadPoolExecutor(max_workers=min(5, max(1, len(active_tickers)))) as ex:
            for t, val in ex.map(fetch_val, active_tickers):
                total += val
                if t in hd:
                    hd[t]['manual_val'] = val
        return round(total, 2)

class MarketScoringEngine:
    def __init__(self):
        self.nav_bases = {'YCA.L': 634.0, 'U-UN.TO': 28.50, 'PHYS': 33.00, 'PSLV': 21.50, 'CEF': 22.00, 'SGLN.L': 3150.0, 'SSLN.L': 2350.0}
        self.asset_names = {
            'YCA.L': 'Yellow Cake plc', 'U-UN.TO': 'Sprott Physical Uranium Trust', 'PHYS': 'Sprott Physical Gold Trust',
            'PSLV': 'Sprott Physical Silver Trust', 'CEF': 'Sprott Physical Gold & Silver', 'GLD': 'SPDR Gold Shares',
            'SGLN.L': 'iShares Physical Gold ETC', 'SSLN.L': 'iShares Physical Silver ETC', 'MSFT': 'Microsoft Corp',
            'AAPL': 'Apple Inc.', 'NVDA': 'NVIDIA Corp', 'TSLA': 'Tesla', 'AMZN': 'Amazon', 'META': 'Meta', 'GOOGL': 'Alphabet', 'AMD': 'Advanced Micro Devices',
            'NFLX': 'Netflix', 'PLTR': 'Palantir Tech', 'COIN': 'Coinbase', 'MSTR': 'MicroStrategy', 'TQQQ': 'ProShares UltraPro QQQ',
            'SOXL': 'Direxion Daily Semi Bull 3X', 'NVDL': 'GraniteShares 2x Long NVDA', 'TSM': 'TSMC ADR', 'SONY': 'Sony Group', 'BABA': 'Alibaba Group',
            'ASML': 'ASML Holding', 'SAP': 'SAP SE',
            'RR.L': 'Rolls-Royce Holdings', 'SHEL.L': 'Shell plc', 'BP.L': 'BP plc', 'BARC.L': 'Barclays plc', 'LLOY.L': 'Lloyds Banking Group', 'AZN.L': 'AstraZeneca',
            'SBUX': 'Starbucks Corp', 'NKE': 'Nike Inc', 'BA': 'Boeing Co'
        }

    def score_momentum(self, df_5m, current_price, avg_buy_price=0.0, highest_price=0.0, profile='test 2'):
        if df_5m.empty or len(df_5m) < 21:
            return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': '0.00%', 
                    'reason': 'Insufficient intraday price history.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                    'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Tranche 0)', 'action_color': '#8a8a9e'}

        prof = profile.lower()
        pct_change_5d = ((current_price - df_5m['Close'].iloc[0]) / df_5m['Close'].iloc[0]) * 100.0

        # --- DYNAMIC RISK PARAMETERS BY PROFILE ---
        trail_pct = 0.50
        hard_pct = -1.00
        if 'test 4' in prof:
            trail_pct = 0.75
            hard_pct = -0.50
        elif 'test 5' in prof:
            trail_pct = 1.00

        if avg_buy_price > 0 and highest_price > 0:
            pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
            drop_from_peak_pct = ((highest_price - current_price) / highest_price) * 100.0

            if drop_from_peak_pct >= trail_pct:
                return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"+{pnl_pct:.2f}%" if pnl_pct > 0 else f"{pnl_pct:.2f}%",
                        'reason': f"TRAILING STOP TRIPPED. Dropped {drop_from_peak_pct:.2f}% from peak of £{highest_price:.2f}.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                        'status': 'Trailing Stop', 'color': '#00d2ff', 'action_main': 'SELL', 'action_sub': '(Lock Profits)', 'action_color': '#00d2ff'}
            
            if pnl_pct <= hard_pct: 
                return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%",
                        'reason': f"HARD STOP LOSS TRIPPED ({pnl_pct:.2f}%). Cutting losses immediately.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                        'status': 'Stop Loss', 'color': '#ff3d00', 'action_main': 'SELL', 'action_sub': '(Stop Loss)', 'action_color': '#ff3d00'}

            return {'type': 'Intraday Momentum', 'score': 80, 'tranches': 1, 'discount': f"{pnl_pct:.2f}%",
                    'reason': f"RIDING TREND. High Water Mark: £{highest_price:.2f} (Trailing Drop: -{drop_from_peak_pct:.2f}%).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                    'status': 'Trailing Stop Active', 'color': '#00d2ff', 'action_main': 'HOLD / WAIT', 'action_sub': '(Riding Winner)', 'action_color': '#00d2ff'}

        # --- ENTRY CONDITIONS BY PROFILE ---
        if 'test 4' in prof:
            sma20 = df_5m['Close'].rolling(20).mean().iloc[-1]
            std20 = df_5m['Close'].rolling(20).std().iloc[-1]
            upper_bb = sma20 + (2 * std20)
            
            avg_vol = df_5m['Volume'].rolling(20).mean().iloc[-1]
            cur_vol = df_5m['Volume'].iloc[-1]
            
            if current_price > upper_bb and cur_vol > (1.5 * avg_vol):
                buy_score, tranches = 85, 1
                action_main, action_sub = "BUY", "(Vol Breakout)"
                status, color = "Volatility Breakout", "#00c853"
                reason = f"BREAKOUT DETECTED: Price pierced upper Bollinger Band on 1.5x average volume."
                action_color = "#00c853"
            else:
                buy_score, tranches = 10, 0
                action_main, action_sub = "HOLD / WAIT", "(Awaiting Setup)"
                status, color = "Consolidating", "#8a8a9e"
                reason = f"Awaiting volume breakout above upper Bollinger Band."
                action_color = "#8a8a9e"

        elif 'test 5' in prof:
            ret_60m = ((current_price - df_5m['Close'].iloc[-12]) / df_5m['Close'].iloc[-12]) * 100.0 if len(df_5m) >= 12 else 0
            buy_score = min(100, max(0, int(50 + (ret_60m * 15))))
            if buy_score >= 65:
                tranches, action_main, action_sub = 1, "BUY", "(Rel Strength)"
                status, color, action_color = "High Relative Strength", "#00c853", "#00c853"
                reason = f"STRONG ROTATION: 60-min return is +{ret_60m:.2f}%. Candidate for 100% allocation."
            else:
                tranches, action_main, action_sub = 0, "HOLD / WAIT", "(Weak Momentum)"
                status, color, action_color = "Weak Rotation", "#8a8a9e", "#8a8a9e"
                reason = f"60-min return is {ret_60m:.2f}%. Awaiting stronger relative performance."

        else:
            ema9 = df_5m['Close'].ewm(span=9, adjust=False).mean().iloc[-1]
            ema21 = df_5m['Close'].ewm(span=21, adjust=False).mean().iloc[-1]
            delta = df_5m['Close'].diff()
            rs = (delta.where(delta > 0, 0)).rolling(14).mean() / (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi = 100 - (100 / (1 + rs.iloc[-1])) if not rs.empty else 50
            
            if ema9 > ema21 and rsi < 65 and pct_change_5d > 0:
                buy_score = min(100, max(50, round(50 + pct_change_5d * 10 + (70 - rsi))))
                tranches, action_main, action_sub = 1, "BUY", "(Momentum Surge)"
                status, color, action_color = "Fast Momentum Surge", "#00c853", "#00c853"
                reason = f"SURGE DETECTED: 9-EMA ({ema9:.2f}) > 21-EMA ({ema21:.2f}), RSI {rsi:.1f}."
            else:
                buy_score, tranches = 10, 0
                action_main, action_sub = "HOLD / WAIT", "(Awaiting Setup)"
                status, color, action_color = "No Setup", "#8a8a9e", "#8a8a9e"
                reason = f"Awaiting fast EMA crossover surge."

        return {'type': 'Intraday Momentum', 'score': buy_score, 'tranches': tranches, 'discount': f"{pct_change_5d:.2f}%",
                'reason': reason, 'is_smart': True, 'rec_buy': round(current_price*0.99, 2), 'rec_sell': round(current_price*1.02, 2),
                'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color}

    def score_nav_asset(self, ticker, current_price, volume_ratio):
        nav = self.nav_bases.get(ticker, current_price * 1.10)
        implied_discount = ((nav - current_price) / nav) * 100.0
        buy_score = min(max(round(min(max((implied_discount/20.0)*80.0, 0), 80) + min(max((volume_ratio/2.0)*20.0, 0), 20), 2), 0), 100)
        tranches = 0 if implied_discount <= 0 else min(5, int(buy_score // 20) + 1)
            
        macro_triggered, uun_discount = False, 0.0
        if ticker == 'YCA.L':
            try:
                u_df = fetch_yf_data('U-UN.TO', '1d', '1d')
                if not u_df.empty:
                    u_price = u_df['Close'].iloc[-1]
                    u_nav = self.nav_bases.get('U-UN.TO', 28.50)
                    uun_discount = ((u_nav - u_price) / u_nav) * 100.0
                    if uun_discount > 10.0: macro_triggered = True
            except: pass

        if macro_triggered:
            action_main, action_sub, status, color, tranches = "SELL", "(Sentinel Active)", "MACRO SENTINEL TRIPPED", "#ff3d00", 0
            reason = f"EMERGENCY STOP: Sprott U.UN discount exceeded 10% ({uun_discount:.1f}%). Liquidating to 0 tranches."
            action_color = "#ff3d00"
        elif implied_discount <= 5.0 and implied_discount > -50.0:
            action_main, action_sub, status, color, tranches = "SELL", "(Take Profit)", "Target Reached", "#ff3d00", 0
            reason = f"Profit Target Triggered. NAV discount shrunk to {implied_discount:.1f}%."
            action_color = "#ff3d00"
        elif buy_score >= 40:
            action_main, action_sub = "BUY", f"(Tranche {tranches})"
            status, color = ('Deep Value Anomaly', '#00c853') if buy_score >= 60 else ('Moderate Value', '#ff9900')
            reason = f"Physical NAV Anomaly. Trading at {implied_discount:.1f}% discount to NAV ({nav})."
            action_color = "#00c853"
        else:
            action_main, action_sub = "HOLD / WAIT", f"(Tranche {tranches})"
            status, color = ('Trading at Premium', '#ff4a4a') if implied_discount < 0 else ('Low Value', '#8a8a9e')
            reason = f"Trading at {implied_discount:.1f}% NAV discount. Active Tranches: {tranches}."
            action_color = "#8a8a9e"

        return {'type': 'Physical Trust', 'score': buy_score, 'tranches': tranches, 'discount': f"{implied_discount:.2f}%" if implied_discount>0 else f"+{abs(implied_discount):.2f}%", 
                'reason': reason, 'is_smart': True, 'rec_buy': round(current_price*0.98, 2), 'rec_sell': round(nav*0.95, 2),
                'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color}

    def score_equity(self, df, current_price):
        if df.empty or 'Close' not in df:
            return {'type': 'Global Equity', 'score': 0, 'tranches': 0, 'discount': '0.00%', 
                    'reason': 'Awaiting data.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                    'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '', 'action_color': '#8a8a9e'}
            
        dma = df['Close'].tail(200).mean() if len(df) >= 200 else df['Close'].mean()
        implied_discount = ((dma - current_price) / dma) * 100.0
        delta = df['Close'].diff()
        rs = (delta.where(delta > 0, 0)).rolling(14).mean() / (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + rs.iloc[-1]))
        
        avg_vol = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
        vol_rat = (df['Volume'].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
        
        buy_score = min(max(round(min(max((implied_discount/25.0)*50.0, 0), 50) + (max(0, (40-rsi)/40*30) if pd.notna(rsi) else 0) + min(max((vol_rat/2.0)*20.0, 0), 20), 2), 0), 100)
        tranches = 0 if implied_discount < 0 else min(5, int(buy_score // 20) + 1)
        
        if buy_score >= 40:
            action_main, action_sub = "BUY", f"(Tranche {tranches})"
            status, color = ('Deep Value Anomaly', '#00c853') if buy_score >= 60 else ('Moderate Value', '#ff9900')
            reason = f"Value Anomaly. Trading at {implied_discount:.1f}% discount to 200d-DMA."
            action_color = "#00c853"
        elif implied_discount <= -10.0:
            action_main, action_sub, status, color, tranches = "SELL", "(Take Profit)", 'Overextended (High)', '#ff3d00', 0
            reason = f"Overextended ({abs(implied_discount):.1f}% above 200d-DMA). Take profits."
            action_color = "#ff3d00"
        else:
            action_main, action_sub, status, color = "HOLD / WAIT", f"(Tranche {tranches})", 'Fair Value', '#8a8a9e'
            reason = f"No Value Anomaly. Near 200d-DMA."
            action_color = "#8a8a9e"
            
        return {'type': 'Global Equity', 'score': buy_score, 'tranches': tranches, 'discount': f"{implied_discount:.2f}%" if implied_discount>0 else f"+{abs(implied_discount):.2f}%", 
                'reason': reason, 'is_smart': True, 'rec_buy': round(dma*0.9, 2), 'rec_sell': round(dma*1.05, 2),
                'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color}

portfolio_store = PortfolioManager()

@app.route('/')
def index(): return render_template_string(HTML_FRONTEND)

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

@app.route('/api/push_test', methods=['POST'])
def push_test():
    topic = (request.get_json() or {}).get('topic', '')
    if topic: send_push_notification(topic, "Stock Terminal Push Alert", "Test notification connected successfully!")
    return jsonify({'status': 'ok' if topic else 'error'})

@app.route('/api/portfolio', methods=['GET'])
def get_portfolio():
    portfolio_store.reload()
    return jsonify(portfolio_store.user_data())

@app.route('/api/portfolio/reset', methods=['POST'])
def reset_portfolio():
    portfolio_store.reload()
    return jsonify({'status': 'ok', 'portfolio': portfolio_store.reset_all()})

@app.route('/api/portfolio/settings', methods=['POST'])
def update_settings():
    portfolio_store.reload()
    portfolio_store.update_settings((request.get_json() or {}).get('settings', {}))
    return jsonify({'status': 'ok'})

@app.route('/api/portfolio/budget', methods=['POST'])
def update_budget():
    portfolio_store.reload()
    portfolio_store.update_budget((request.get_json() or {}).get('budget', 10000))
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
    return jsonify({'status': 'ok', 'entry': portfolio_store.execute_trade(b.get('ticker'), b.get('action'), b.get('shares'), b.get('price'))})

@app.route('/api/trade/undo', methods=['POST'])
def undo_trade(): 
    portfolio_store.reload()
    return jsonify({'status': 'ok' if portfolio_store.undo_trade((request.get_json() or {}).get('id')) else 'error'})

@app.route('/api/score', methods=['GET'])
def get_score():
    t = request.args.get('t', '').upper()
    try:
        active_profile = portfolio_store.active_username().strip().lower()
        is_momentum = any(x in active_profile for x in ['test 2', 'test 3', 'test 4', 'test 5'])
        df = fetch_yf_data(t, "5d" if is_momentum else "1y", "5m" if is_momentum else "1d")
        if df.empty: return jsonify({'error': 'Ticker not found.'}), 400
        cur = df['Close'].iloc[-1]
        engine = MarketScoringEngine()
        
        if is_momentum:
            res = engine.score_momentum(df, cur, profile=active_profile)
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
    engine = MarketScoringEngine()
    active_profile = portfolio_store.active_username().strip().lower()
    is_momentum = any(x in active_profile for x in ['test 2', 'test 3', 'test 4', 'test 5'])
    
    if 'test 3' in active_profile:
        scan_list = ['TSM', 'SONY', 'BABA', 'ASML', 'SAP', 'AZN.L', 'RR.L', 'SHEL.L', 'BP.L', 'BARC.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'GOOGL', 'NFLX', 'PLTR', 'COIN', 'MSTR', 'TQQQ', 'SOXL', 'NVDL']
    elif is_momentum:
        scan_list = ['RR.L', 'SHEL.L', 'BP.L', 'BARC.L', 'LLOY.L', 'AZN.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'GOOGL', 'NFLX', 'PLTR', 'COIN', 'MSTR', 'TQQQ', 'SOXL', 'NVDL']
    else:
        scan_list = ud.get('watchlist', [])
    
    mb = ud.get('master_budget', 10000.0)
    active_holds = [tk for tk, hd in ud.get('holdings', {}).items() if hd.get('shares', 0) > 0]
    tot_cb = sum(ud.get('initial_positions', {}).get(w, {}).get('manual_val', 0.0) + sum(tr['amount'] if tr['action']=='BUY' else -tr['amount'] for tr in ud.get('history', []) if tr.get('ticker')==w) for w in active_holds)
    
    cash_balance = mb - tot_cb
    rem_cash = max(0, cash_balance)
    total_equity = cash_balance + portfolio_store.get_total_portfolio_value()

    if 'notified_signals' not in ud: ud['notified_signals'] = {}
    dirs, buys, held_scores = [], [], []
    MIN_BUY_VALUE = 20.0

    dfs = {}
    def fetch_data_thread(tick): 
        return tick, fetch_yf_data(tick, "5d" if is_momentum else "1y", "5m" if is_momentum else "1d")
        
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(scan_list)))) as ex:
        for tick, df in ex.map(fetch_data_thread, scan_list): dfs[tick] = df

    for t in scan_list:
        t = t.strip().upper()
        if not t: continue
        try:
            df = dfs.get(t)
            if df is None or df.empty: continue
            cur = df['Close'].iloc[-1]
            sh = portfolio_store.get_shares(t)
            cps = cur / 100.0 if t.endswith('.L') and cur > 100 else cur
            vo = sh * cps

            avg_buy_p, highest_p = 0.0, cur
            if sh > 0:
                t_buys = [tr for tr in ud.get('history', []) if tr.get('ticker') == t and tr.get('action') == 'BUY']
                if t_buys: avg_buy_p = t_buys[0]['price']
                
                highest_p = ud.get('holdings', {}).get(t, {}).get('high_water', cur)
                if cur > highest_p:
                    highest_p = cur
                    ud['holdings'][t]['high_water'] = highest_p

            if is_momentum:
                st = engine.score_momentum(df, cur, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=active_profile)
            else:
                avg_vol = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
                v_rat = (df['Volume'].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
                st = engine.score_nav_asset(t, cur, v_rat) if t in engine.nav_bases else engine.score_equity(df, cur)

            last_trade_time = next((h['timestamp'] for h in ud.get('history', []) if h['ticker'] == t), 0)
            is_auto = any(x in active_profile for x in ['test', 'test 2', 'test 3', 'test 4', 'test 5'])
            if is_auto and (int(time.time()) - last_trade_time < 300):
                continue

            if sh > 0:
                held_scores.append({'ticker': t, 'shares': sh, 'price': cur, 'cps': cps, 'value': vo, 'score': st['score'], 'action': st['action_main']})

            if st['action_main'] == 'SELL':
                if sh > 0 and vo >= MIN_BUY_VALUE:
                    dirs.append({'ticker': t, 'name': engine.asset_names.get(t, t), 'action': 'SELL', 'shares': sh, 'price': cur, 'amount': round(vo, 2)})
            elif st['action_main'] == 'BUY' and rem_cash >= MIN_BUY_VALUE:
                buys.append({'t': t, 'n': engine.asset_names.get(t, t), 'cps': cps, 'p': cur, 's': st['score']})
        except: pass

    if 'test 5' in active_profile and buys:
        buys.sort(key=lambda x: x['s'], reverse=True)
        top_candidate = buys[0]
        
        held_scores.sort(key=lambda x: x['score'])
        if held_scores:
            weakest = held_scores[0]
            if top_candidate['s'] > (weakest['score'] + 20) and weakest['shares'] > 0 and weakest['ticker'] != top_candidate['t']:
                dirs.append({'ticker': weakest['ticker'], 'name': engine.asset_names.get(weakest['ticker'], weakest['ticker']), 'action': 'SELL', 'shares': weakest['shares'], 'price': weakest['price'], 'amount': round(weakest['value'], 2)})
    
    elif is_momentum and buys and 'test 5' not in active_profile:
        buys.sort(key=lambda x: x['s'], reverse=True)
        top_candidate = buys[0]
        
        held_scores.sort(key=lambda x: x['score'])
        if held_scores and top_candidate['s'] > (held_scores[0]['score'] + 15):
            weakest = held_scores[0]
            if weakest['shares'] > 0 and weakest['ticker'] != top_candidate['t']:
                dirs.append({'ticker': weakest['ticker'], 'name': engine.asset_names.get(weakest['ticker'], weakest['ticker']), 'action': 'SELL', 'shares': weakest['shares'], 'price': weakest['price'], 'amount': round(weakest['value'], 2)})
                alloc_amount = max(weakest['value'], 1000.0)
                buy_sh = int(alloc_amount // top_candidate['cps'])
                if buy_sh > 0:
                    dirs.append({'ticker': top_candidate['t'], 'name': top_candidate['n'], 'action': 'BUY', 'shares': buy_sh, 'price': top_candidate['p'], 'amount': round(buy_sh * top_candidate['cps'], 2)})

    if buys and rem_cash >= MIN_BUY_VALUE:
        buys.sort(key=lambda x: x['s'], reverse=True)
        top_buys = buys[:1] if 'test 5' in active_profile else buys[:2]  
        
        if 'test 5' in active_profile:
            per_stock_budget = min(rem_cash, total_equity * 0.98)
        else:
            per_stock_budget = min(rem_cash / len(top_buys), total_equity * 0.50)
        
        for b in top_buys:
            bs = int(per_stock_budget // b['cps'])
            amt = round(bs * b['cps'], 2)
            if bs > 0 and amt >= MIN_BUY_VALUE and amt <= rem_cash:
                dirs.append({'ticker': b['t'], 'name': b['n'], 'action': 'BUY', 'shares': bs, 'price': b['p'], 'amount': amt})

    is_auto = any(x in active_profile for x in ['test', 'test 2', 'test 3', 'test 4', 'test 5'])
    if is_auto and dirs:
        for d in dirs:
            if d['action'] == 'BUY':
                curr_cb = sum(ud.get('initial_positions', {}).get(w, {}).get('manual_val', 0.0) + sum(tr['amount'] if tr['action']=='BUY' else -tr['amount'] for tr in ud.get('history', []) if tr.get('ticker')==w) for w in [tk for tk, hd in ud.get('holdings', {}).items() if hd.get('shares', 0) > 0])
                curr_cash = ud['master_budget'] - curr_cb
                if curr_cash < MIN_BUY_VALUE:
                    continue
                if d['amount'] > curr_cash:
                    d['shares'] = int(curr_cash // (d['price'] / 100.0 if d['ticker'].endswith('.L') else d['price']))
                    d['amount'] = round(d['shares'] * (d['price'] / 100.0 if d['ticker'].endswith('.L') else d['price']), 2)
            if d['shares'] > 0:
                portfolio_store.execute_trade(d['ticker'], d['action'], d['shares'], d['price'])
        dirs = [] 
        
    if is_momentum:
        ud = portfolio_store.user_data()
        current_holdings = [t for t, h_data in ud.get('holdings', {}).items() if h_data.get('shares', 0) > 0]
        ud['watchlist'] = current_holdings

    portfolio_store.save()

    curr_keys, topic = set(), ud.get('settings', {}).get('ntfy_topic', '')
    for d in dirs:
        k = f"{d['action']}_{d['shares']}"
        curr_keys.add(d['ticker'])
        if ud['notified_signals'].get(d['ticker']) != k:
            if topic: send_push_notification(topic, f"SIGNAL ALERT: {d['ticker']}", f"Directive for {portfolio_store.active_username()}: {d['action']} {d['shares']} shares (£{d['amount']:.2f})")
            ud['notified_signals'][d['ticker']] = k
            portfolio_store.save()

    for k in [k for k in ud['notified_signals'] if k not in curr_keys]: del ud['notified_signals'][k]
    portfolio_store.save()
    return jsonify({'directives': dirs})

@app.route('/api/recommend', methods=['GET'])
def get_recommendations():
    res, engine = [], MarketScoringEngine()
    tickers = ['YCA.L', 'U-UN.TO', 'SGLN.L', 'SSLN.L', 'PHYS', 'PSLV', 'CEF', 'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'GOOGL', 'META', 'BHP', 'RIO', 'VALE', 'XOM', 'CVX', 'OXY', 'JPM', 'BAC', 'GS', 'PFE', 'JNJ', 'UNH', 'DIS', 'NKE', 'SBUX', 'BA', 'LMT']
    
    def fetch_rec(tick): return tick, fetch_yf_data(tick, "1y", "1d")
        
    with ThreadPoolExecutor(max_workers=10) as ex:
        for t, df in ex.map(fetch_rec, tickers):
            if not df.empty:
                cur, avg_vol = df['Close'].iloc[-1], df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
                st = engine.score_nav_asset(t, cur, (df['Volume'].iloc[-1]/avg_vol) if avg_vol>0 else 1.0) if t in engine.nav_bases else engine.score_equity(df, cur)
                st.update({'ticker': t, 'name': engine.asset_names.get(t, t), 'price': round(cur, 2)})
                res.append(st)
    return jsonify({'recommendations': sorted(res, key=lambda x: x['score'], reverse=True)})

@app.route('/api/data', methods=['GET'])
def get_data():
    portfolio_store.reload()
    ud = portfolio_store.user_data()
    wl = ud.get('watchlist', [])
    t = request.args.get('t', '').upper().strip()
    active_profile = portfolio_store.active_username().strip().lower()
    is_momentum = any(x in active_profile for x in ['test 2', 'test 3', 'test 4', 'test 5'])
    if not t: t = 'ALL_SHARES'

    tot_own = portfolio_store.get_total_portfolio_value()
    mb = ud.get('master_budget', 10000.0)

    active_holds = [tk for tk, hd in ud.get('holdings', {}).items() if hd.get('shares', 0) > 0]
    tot_cb = sum(ud.get('initial_positions', {}).get(w, {}).get('manual_val', 0.0) + sum(tr['amount'] if tr['action']=='BUY' else -tr['amount'] for tr in ud.get('history', []) if tr.get('ticker')==w) for w in active_holds)
    
    cash_balance = mb - tot_cb
    rem_cash = max(0, cash_balance)
    total_equity = cash_balance + tot_own

    master_pnl_val = total_equity - mb
    master_pnl_pct = (master_pnl_val / mb) * 100.0 if mb > 0 else 0.0
    master_pc = '#00c853' if master_pnl_val > 0 else ('#ff3d00' if master_pnl_val < 0 else '#8a8a9e')

    req_p = request.args.get('p')
    if not req_p or req_p == 'undefined': req_p = ud.get('settings', {}).get('period', '5d' if is_momentum else '1mo')
    req_i = request.args.get('i')
    if not req_i or req_i == 'undefined': req_i = ud.get('settings', {}).get('interval', '5m' if is_momentum else '1d')

    if req_p in ['1y', '5y', 'max'] and req_i in ['1m', '2m', '5m', '15m', '30m', '60m', '1h']: req_i = '1d'
    elif req_p in ['1mo', '3mo', '6mo'] and req_i in ['1m', '2m']: req_i = '5m'

    wl_status = {}
    if wl:
        def check_status(tick):
            try:
                df_stat = fetch_yf_data(tick, "5d", "5m")
                if df_stat.empty: return tick, 'closed'
                if df_stat.index.tz is not None:
                    df_stat.index = df_stat.index.tz_convert('UTC')
                last_ts = df_stat.index[-1].timestamp()
                if (time.time() - last_ts) > 1800:
                    return tick, 'closed' 
                if len(df_stat) > 1:
                    last_price = df_stat['Close'].iloc[-1]
                    prev_price = df_stat['Close'].iloc[-2]
                    if last_price > prev_price: return tick, 'up'
                    elif last_price < prev_price: return tick, 'down'
                return tick, 'closed'
            except:
                return tick, 'closed'

        with ThreadPoolExecutor(max_workers=min(5, max(1, len(wl)))) as ex:
            for tick, status in ex.map(check_status, wl):
                wl_status[tick] = status

    pnl_dfs = {}
    def fetch_pnl_data(tick): return tick, fetch_yf_data(tick, "5d", "5m")
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(active_holds)))) as ex:
        for tick, df_pnl in ex.map(fetch_pnl_data, active_holds): pnl_dfs[tick] = df_pnl
        
    tot_today_diff, tot_1h_diff = 0.0, 0.0
    for tk in active_holds:
        df_ch = pnl_dfs.get(tk)
        if df_ch is not None and not df_ch.empty and len(df_ch) >= 2:
            cur_price = df_ch['Close'].iloc[-1]
            last_date = df_ch.index[-1].date()
            today_df = df_ch[df_ch.index.date == last_date]
            p_today = today_df['Close'].iloc[0] if not today_df.empty else cur_price
            
            target_ts = df_ch.index[-1] - pd.Timedelta(hours=1)
            prior_df = df_ch[df_ch.index <= target_ts]
            p_1h = prior_df['Close'].iloc[-1] if not prior_df.empty else p_today
            
            div = 100.0 if tk.endswith('.L') and cur_price > 100 else 1.0
            sh_h = ud.get('holdings', {}).get(tk, {}).get('shares', 0)
            
            tot_today_diff += sh_h * ((cur_price - p_today)/div)
            tot_1h_diff += sh_h * ((cur_price - p_1h)/div)

    prev_equity_today = total_equity - tot_today_diff
    master_today_pct = (tot_today_diff / prev_equity_today * 100.0) if prev_equity_today > 0 else 0.0
    
    prev_equity_1h = total_equity - tot_1h_diff
    master_1h_pct = (tot_1h_diff / prev_equity_1h * 100.0) if prev_equity_1h > 0 else 0.0

    master_pnl_data = {
        'all': {'val': f"{'+' if master_pnl_val>0 else ''}£{master_pnl_val:.2f} ({'+' if master_pnl_pct>0 else ''}{master_pnl_pct:.2f}%)", 'color': '#00c853' if master_pnl_val > 0 else ('#ff3d00' if master_pnl_val < 0 else '#8a8a9e')},
        'today': {'val': f"{'+' if tot_today_diff>0 else ''}£{tot_today_diff:.2f} ({'+' if master_today_pct>0 else ''}{master_today_pct:.2f}%)", 'color': '#00c853' if tot_today_diff > 0 else ('#ff3d00' if tot_today_diff < 0 else '#8a8a9e')},
        '1h': {'val': f"{'+' if tot_1h_diff>0 else ''}£{tot_1h_diff:.2f} ({'+' if master_1h_pct>0 else ''}{master_1h_pct:.2f}%)", 'color': '#00c853' if tot_1h_diff > 0 else ('#ff3d00' if tot_1h_diff < 0 else '#8a8a9e')}
    }

    if t == 'ALL_SHARES':
        lines = []
        if wl:
            def fetch_t(tick):
                return tick, fetch_yf_data(tick, req_p, req_i)
            
            dfs = {}
            with ThreadPoolExecutor(max_workers=min(5, max(1, len(wl)))) as ex:
                for tick, df_t in ex.map(fetch_t, wl):
                    dfs[tick] = df_t
            
            colors = ['#00d2ff', '#00c853', '#ff3d00', '#ff9900', '#b388ff', '#ffff00', '#ff4081', '#18ffff']
            c_idx = 0
            for tick in wl:
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
        
        return jsonify({'is_multi': True, 'lines': lines, 'wl_status': wl_status, 'name': 'Relative Performance (Watchlist)', 'portfolio': ud, 'metrics': {
            'price': 0, 'price_display': f"Normalized %",
            'discount': '--', 'buy_score': '--', 'tranches': 0,
            'status': 'Comparative View', 'color': '#00d2ff', 'reason': 'Viewing normalized percentage growth of all watchlist assets to compare relative momentum.',
            'action_main': '--', 'action_sub': '', 'action_color': '#8a8a9e',
            'shares_owned': sum(h.get('shares',0) for h in ud.get('holdings',{}).values()), 'value_owned': tot_own, 
            'pnl_display': master_pnl_data['all']['val'], 'pnl_color': master_pnl_data['all']['color'], 'pnl_data': master_pnl_data,
            'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data, 'master_budget': mb,
            'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2)
        }})

    try:
        df = fetch_yf_data(t, req_p, req_i)
        if df.empty:
            return jsonify({'ohlc': [], 'wl_status': wl_status, 'name': f'{t} Data Loading...', 'portfolio': ud, 'metrics': {
                'price': 0, 'price_display': '£0.00', 'discount': '--', 'buy_score': '0 / 100', 'tranches': 0,
                'status': 'Loading Data', 'color': '#787e8e', 'reason': 'Fetching fresh market quotes.',
                'action_main': 'WAIT', 'action_sub': '', 'action_color': '#787e8e', 'shares_owned': 0, 'value_owned': 0.0,
                'pnl_display': '£0.00 (0.00%)', 'pnl_color': '#8a8a9e', 'pnl_data': master_pnl_data, 'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data,
                'master_budget': mb, 'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2)
            }})

        if df.index.tz is not None: df.index = df.index.tz_convert('UTC')
        
        data = [{'time': i.strftime('%Y-%m-%d') if req_i in ['1d','5d','1wk','1mo','3mo'] else int(i.timestamp()), 'open': round(r['Open'],2), 'high': round(r['High'],2), 'low': round(r['Low'],2), 'close': round(r['Close'],2)} for i, r in df.iterrows()]
        seen = set(); data = [x for x in data if x['time'] not in seen and not seen.add(x['time'])]
        last_p = round(data[-1]['close'], 2) if data else 0

        engine = MarketScoringEngine()
        sh_own = portfolio_store.get_shares(t)
        cps = last_p / 100.0 if t.endswith('.L') and last_p > 100 else last_p
        val_own = round(sh_own * cps, 2)

        avg_buy_p, highest_p = 0.0, last_p
        if sh_own > 0:
            t_buys = [tr for tr in ud.get('history', []) if tr.get('ticker') == t and tr.get('action') == 'BUY']
            if t_buys: avg_buy_p = t_buys[0]['price']

            highest_p = ud.get('holdings', {}).get(t, {}).get('high_water', last_p)
            if last_p > highest_p:
                highest_p = last_p
                if 'holdings' not in ud: ud['holdings'] = {}
                if t not in ud['holdings']: ud['holdings'][t] = {}
                ud['holdings'][t]['high_water'] = highest_p
                portfolio_store.save()

        if is_momentum:
            st = engine.score_momentum(df, last_p, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=active_profile)
        else:
            av = df['Volume'].tail(20).mean() if len(df)>=20 else 1.0
            st = engine.score_nav_asset(t, last_p, (df['Volume'].iloc[-1]/av) if av>0 else 1.0) if t in engine.nav_bases else engine.score_equity(fetch_yf_data(t, "1y", "1d"), last_p)

        cb = ud.get('initial_positions', {}).get(t, {}).get('manual_val', 0.0) + sum(tr['amount'] if tr['action']=='BUY' else -tr['amount'] for tr in ud.get('history', []) if tr.get('ticker')==t)
        
        ticker_pnl_data = {}
        if sh_own > 0 and cb > 0:
            pv, pp = round(val_own - cb, 2), round((val_own - cb) / cb * 100, 2)
            c = '#00c853' if pv > 0 else ('#ff3d00' if pv < 0 else '#8a8a9e')
            pnl_d = f"{'+' if pv>0 else ''}£{pv:.2f} ({'+' if pp>0 else ''}{pp:.2f}%)"

            df_ch = fetch_yf_data(t, "5d", "5m")
            if not df_ch.empty and len(df_ch) >= 2:
                cur_p = df_ch['Close'].iloc[-1]
                last_date = df_ch.index[-1].date()
                today_df = df_ch[df_ch.index.date == last_date]
                p_today = today_df['Close'].iloc[0] if not today_df.empty else cur_p
                
                target_ts = df_ch.index[-1] - pd.Timedelta(hours=1)
                prior_df = df_ch[df_ch.index <= target_ts]
                p_1h = prior_df['Close'].iloc[-1] if not prior_df.empty else p_today
                
                div = 100.0 if t.endswith('.L') and cur_p > 100 else 1.0
                
                pv_today = sh_own * ((cur_p - p_today)/div)
                pv_1h = sh_own * ((cur_p - p_1h)/div)
                
                val_today_start = sh_own * (p_today/div)
                val_1h_start = sh_own * (p_1h/div)
                
                pp_today = (pv_today / val_today_start * 100.0) if val_today_start > 0 else 0.0
                pp_1h = (pv_1h / val_1h_start * 100.0) if val_1h_start > 0 else 0.0
                
                ticker_pnl_data = {
                    'all': {'val': pnl_d, 'color': c},
                    'today': {'val': f"{'+' if pv_today>0 else ''}£{pv_today:.2f} ({'+' if pp_today>0 else ''}{pp_today:.2f}%)", 'color': '#00c853' if pv_today > 0 else ('#ff3d00' if pv_today < 0 else '#8a8a9e')},
                    '1h': {'val': f"{'+' if pv_1h>0 else ''}£{pv_1h:.2f} ({'+' if pp_1h>0 else ''}{pp_1h:.2f}%)", 'color': '#00c853' if pv_1h > 0 else ('#ff3d00' if pv_1h < 0 else '#8a8a9e')}
                }
            else:
                ticker_pnl_data = {'all': {'val': pnl_d, 'color': c}, 'today': {'val': pnl_d, 'color': c}, '1h': {'val': pnl_d, 'color': c}}
        else: 
            pnl_d, c = "£0.00 (0.00%)", '#8a8a9e'
            empty_pnl = {'val': pnl_d, 'color': c}
            ticker_pnl_data = {'all': empty_pnl, 'today': empty_pnl, '1h': empty_pnl}

        if sh_own > 0:
            if 'holdings' not in ud: ud['holdings'] = {}
            if t not in ud['holdings']: ud['holdings'][t] = {}
            ud['holdings'][t]['shares'] = sh_own
            ud['holdings'][t]['manual_val'] = val_own
        else: ud.get('holdings', {}).pop(t, None)

        mathLine = []
        try:
            if is_momentum:
                ema21_series = df['Close'].ewm(span=21, adjust=False).mean()
                ema21_dict = {}
                for idx, val in ema21_series.items():
                    ts = idx.strftime('%Y-%m-%d') if req_i in ['1d','5d','1wk','1mo','3mo'] else int(idx.timestamp())
                    ema21_dict[ts] = val
                for d in data:
                    if d['time'] in ema21_dict:
                        mathLine.append({'time': d['time'], 'value': round(ema21_dict[d['time']], 2)})
            else:
                if t in engine.nav_bases:
                    target = engine.nav_bases[t]
                    for d in data:
                        disc = ((target - d['close']) / target) * 100.0
                        mathLine.append({'time': d['time'], 'value': round(disc, 2)})
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
                        disc = ((val - d['close']) / val) * 100.0 if val > 0 else 0
                        mathLine.append({'time': d['time'], 'value': round(disc, 2)})
        except:
            mathLine = []

        return jsonify({'ohlc': data, 'mathLine': mathLine, 'wl_status': wl_status, 'name': engine.asset_names.get(t, t), 'portfolio': ud, 'metrics': {
            'price': last_p, 'price_display': f"{last_p:.2f}p (£{(last_p/100.0):.2f})" if t.endswith('.L') and last_p>100 else f"£{last_p:.2f}",
            'discount': st['discount'], 'buy_score': f"{st['score']} / 100", 'tranches': st['tranches'],
            'status': st['status'], 'color': st['color'], 'reason': st['reason'],
            'action_main': st['action_main'], 'action_sub': st['action_sub'], 'action_color': st['action_color'],
            'shares_owned': sh_own, 'value_owned': val_own, 'pnl_display': ticker_pnl_data['all']['val'], 'pnl_color': ticker_pnl_data['all']['color'], 'pnl_data': ticker_pnl_data,
            'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data, 'master_budget': mb,
            'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2)
        }})
    except Exception as e: return jsonify({'error': str(e)}), 500

HTML_FRONTEND = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Pro Stock Terminal</title><script src="https://unpkg.com/lightweight-charts@3.8.0/dist/lightweight-charts.standalone.production.js"></script><style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;background:#0f1115;color:#e1e3e6;margin:0;display:flex;height:100vh;overflow:hidden}.sidebar{width:300px;background:#171a21;border-right:1px solid #262b36;display:flex;flex-direction:column;flex-shrink:0}.sidebar-top{padding:15px;flex:1;display:flex;flex-direction:column;overflow-y:auto}.sidebar-bottom{padding:15px;border-top:1px solid #262b36;background:#1a1d24;flex-shrink:0}.right-drawer{width:280px;background:#171a21;border-left:1px solid #262b36;display:flex;flex-direction:column;padding:15px;flex-shrink:0}.right-drawer h2,.sidebar h2{font-size:12px;color:#787e8e;margin:10px 0;text-transform:uppercase;letter-spacing:.5px}.sidebar h2{margin-top:0}.drawer-card{background:#1a1d24;border:1px solid #262b36;border-radius:8px;padding:12px;margin-bottom:10px;text-align:center;position:relative}.drawer-card h3{font-size:10px;color:#787e8e;margin:0 0 6px 0;text-transform:uppercase}.total-shares-box{background:#1e222d;padding:12px;border-radius:8px;border:1px solid #00d2ff;margin-bottom:10px;text-align:center}.total-shares-box label{font-size:10px;color:#00d2ff;text-transform:uppercase;font-weight:bold;display:block;margin-bottom:4px}.total-shares-box div.total-val{font-size:18px;font-weight:bold;color:#fff}.total-shares-box div.total-pnl{font-size:12px;font-weight:bold;color:#8a8a9e;margin-top:4px}.user-profile-box{background:#1a1d24;padding:10px 12px;border-radius:8px;border:1px solid #262b36;margin-bottom:10px}.user-profile-box label{font-size:10px;color:#00d2ff;text-transform:uppercase;font-weight:bold}.user-btn{padding:3px 8px;font-size:10px;font-weight:bold;border:none;border-radius:4px;cursor:pointer}.user-btn.new{background:#00d2ff;color:#000}.user-btn.del{background:#ff3d00;color:#fff}.master-budget-box{background:#171a21;padding:12px;border-radius:8px;border:1px solid #262b36;margin-bottom:15px}.master-budget-box label{font-size:11px;color:#787e8e;text-transform:uppercase;font-weight:bold;display:block;margin-bottom:6px}.master-budget-box input{width:100%;background:#0f1115;border:1px solid #262b36;color:#fff;padding:8px;border-radius:6px;font-weight:bold;font-size:15px}.budget-sub-stats{margin-top:10px;padding-top:8px;border-top:1px solid #262b36;display:flex;flex-direction:column;gap:6px;font-size:11px}.budget-sub-stats div{display:flex;justify-content:space-between;color:#787e8e}.budget-sub-stats b{color:#e1e3e6}.directives-box{background:#1e222d;padding:12px;border-radius:8px;border:1px solid #262b36;margin-bottom:12px}.directives-box h3{font-size:11px;color:#00c853;margin:0 0 8px 0;text-transform:uppercase;text-align:center}.directive-item{background:#0f1115;border:1px solid #262b36;border-radius:6px;padding:8px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}.directive-info{font-size:11px;color:#fff}.directive-info b{color:#00d2ff}.directive-btn{background:#00c853;color:#fff;border:none;font-weight:bold;padding:4px 8px;border-radius:4px;font-size:10px;cursor:pointer}.directive-btn:hover{background:#00e676}.directive-btn.sell{background:#ff3d00}.directive-btn.sell:hover{background:#ff5252}.search-box{display:flex;gap:8px;margin-bottom:15px}.search-box input{flex:1;background:#0f1115;border:1px solid #262b36;color:#fff;padding:8px;border-radius:6px}.search-box button{background:#00d2ff;border:none;color:#000;font-weight:bold;padding:8px 12px;border-radius:6px;cursor:pointer}.watchlist{flex:1;list-style:none;padding:0;margin:0}.watchlist-item{padding:10px 12px;border-radius:6px;background:#1e222d;margin-bottom:8px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;border:1px solid transparent}.watchlist-item:hover,.watchlist-item.active{border-color:#00d2ff;background:#252a37}.btn-delete{background:none;border:none;color:#ff4a4a;font-weight:bold;cursor:pointer;padding:0 5px}.main-content{flex:1;display:flex;flex-direction:column;padding:15px;overflow-y:auto;position:relative}.top-nav{display:flex;flex-direction:column;gap:10px;margin-bottom:12px;background:#171a21;padding:12px 16px;border-radius:10px;border:1px solid #262b36}.top-nav-row1{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}.top-nav-row2{display:flex;align-items:center;flex-wrap:wrap;gap:10px;border-top:1px solid #262b36;padding-top:8px}.action-buttons{display:flex;gap:8px;align-items:center;flex-wrap:wrap}select,button.btn-control{background:#0f1115;border:1px solid #262b36;color:#fff;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;white-space:nowrap}select:hover,button.btn-control:hover{border-color:#00d2ff}button.btn-trades{background:#0f1115;border:1px solid #262b36;color:#787e8e;font-weight:bold;transition:background .2s,color .2s}button.btn-trades.active{border-color:#00d2ff;background:#00d2ff;color:#000}button.btn-alert{background:#0f1115;border:1px solid #ff9900;color:#ff9900;font-weight:bold;transition:background .2s,color .2s}button.btn-alert:hover{background:#ff9900;color:#000}button.btn-draw.active{border-color:#ffeb3b;background:#ffeb3b;color:#000}button.btn-manual.active{border-color:#ffeb3b;background:#ffeb3b;color:#000}button.btn-fullscreen{background:#0f1115;border:1px solid #b388ff;color:#b388ff;font-weight:bold;transition:background .2s,color .2s}button.btn-fullscreen:hover{background:#b388ff;color:#000}button.btn-refresh{background:#0f1115;border:1px solid #00d2ff;color:#00d2ff;font-weight:bold;transition:background .2s,color .2s}button.btn-refresh:hover{background:#00d2ff;color:#000}button.btn-reset{background:#0f1115;border:1px solid #ff4a4a;color:#ff4a4a;font-weight:bold;transition:background .2s,color .2s}button.btn-reset:hover{background:#ff4a4a;color:#fff}button.btn-rec{background:#00d2ff;color:#000;font-weight:bold;width:100%;display:flex;align-items:center;justify-content:center;gap:8px;padding:10px;border:none;border-radius:6px;cursor:pointer;margin-top:10px}.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;white-space:nowrap}.controls label{font-size:11px;color:#787e8e;text-transform:uppercase;font-weight:bold}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:12px}.card{background:#171a21;padding:12px;border-radius:10px;border:1px solid #262b36;text-align:center;transition:.2s;display:flex;flex-direction:column;justify-content:center}.card h3{font-size:10px;color:#787e8e;margin:0 0 6px 0;text-transform:uppercase}.card p{font-size:15px;font-weight:bold;margin:0;color:#fff}.card input{width:100%;background:#0f1115;border:1px solid #262b36;color:#00d2ff;padding:4px;border-radius:4px;font-weight:bold;font-size:14px;text-align:center}.val-highlight{color:#00d2ff}.clickable-card{cursor:pointer;border:1px solid #363c4a}.clickable-card:hover{border-color:#00d2ff;background:#1e222d}.segmented-control{display:flex;background:#0f1115;border:1px solid #262b36;border-radius:6px;overflow:hidden;width:100%}.segmented-control input[type="radio"]{display:none}.segmented-control label{flex:1;text-align:center;padding:4px 0;cursor:pointer;font-size:9px;font-weight:bold;color:#787e8e;border-right:1px solid #262b36;transition:.2s;margin:0}.segmented-control label:last-child{border-right:none}.segmented-control input[type="radio"]:checked+label{background:#1e222d;color:#00d2ff}.tranche-bars-container{display:flex;flex-direction:column-reverse;gap:3px;height:35px;justify-content:center;align-items:center;margin-top:5px}.tranche-bar{width:80%;height:5px;border-radius:2px;transition:background .3s}.tranche-bar.active{background:#00c853}.tranche-bar.inactive{background:#363c4a}.btn-execute{background:#00c853;color:#fff;border:none;font-weight:bold;padding:6px 10px;border-radius:6px;cursor:pointer;width:100%;font-size:11px;margin-top:4px}.btn-execute:hover{background:#00e676}.btn-execute.sell-btn{background:#ff3d00}.btn-execute.sell-btn:hover{background:#ff5252}.chart-container{flex:1;background:#171a21;padding:10px;border-radius:10px;border:1px solid #262b36;position:relative;overflow:hidden}#tvChart{position:absolute;top:10px;left:10px;right:10px;bottom:10px}#chartCanvas{position:absolute;top:10px;left:10px;right:10px;bottom:10px;pointer-events:none;z-index:5}#loader{display:none;position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#00d2ff;font-weight:bold;z-index:10;background:rgba(23,26,33,.9);padding:10px 20px;border-radius:8px;border:1px solid #00d2ff}.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:100;justify-content:center;align-items:center}.modal{background:#171a21;width:600px;padding:25px;border-radius:12px;border:1px solid #262b36;box-shadow:0 10px 30px rgba(0,0,0,.5);max-height:90vh;overflow-y:auto}.modal h2{margin-top:0;color:#fff;font-size:18px;border-bottom:1px solid #262b36;padding-bottom:12px;display:flex;justify-content:space-between;align-items:center}.rec-item{background:#1e222d;border:1px solid #262b36;border-radius:8px;padding:15px;margin-bottom:15px}.rec-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}.rec-title-block{display:flex;flex-direction:column;gap:4px}.rec-title-block h3{margin:0;color:#fff;font-size:16px}.rec-score{background:rgba(0,210,255,.1);color:#00d2ff;font-weight:bold;padding:4px 8px;border-radius:4px;font-size:12px;border:1px solid #00d2ff;height:26px;display:flex;align-items:center}.rec-desc{font-size:13px;color:#8a8a9e;line-height:1.4;margin-bottom:15px}.btn-load{background:#00c853;border:none;color:#fff;font-weight:bold;padding:8px 12px;border-radius:6px;cursor:pointer;width:100%;font-size:13px}.btn-load:hover{background:#00e676}.history-list{list-style:none;padding:0;margin:0;overflow-y:auto;flex:1}.history-item{background:#1e222d;border:1px solid #262b36;border-radius:6px;padding:10px;margin-bottom:8px;position:relative}.history-item .h-action{font-weight:bold;font-size:12px}.history-item .h-buy{color:#00c853}.history-item .h-sell{color:#ff3d00}.history-item .h-meta{font-size:11px;color:#787e8e;margin-top:4px}.history-item .btn-undo{position:absolute;top:8px;right:8px;background:none;border:none;color:#ff4a4a;font-weight:bold;cursor:pointer}.pagination-row{display:none;justify-content:space-between;align-items:center;margin-top:15px;border-top:1px solid #262b36;padding-top:15px}.quick-check-box{background:#1e222d;padding:12px;border-radius:8px;border:1px solid #262b36;margin-bottom:10px}.qc-header-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}.qc-header-row h3{font-size:11px;color:#787e8e;margin:0;text-transform:uppercase}.qc-close-btn{color:#787e8e;cursor:pointer;font-size:12px;font-weight:bold;padding:0 4px}.qc-close-btn:hover{color:#fff}.qc-input-row{display:flex;gap:8px}.qc-input-row input{flex:1;background:#0f1115;border:1px solid #262b36;color:#fff;padding:6px 8px;border-radius:6px;font-size:12px}.qc-input-row button{background:#333947;border:1px solid #787e8e;color:#fff;border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer}.qc-input-row button:hover{background:#4a5265}#qsResult{display:none;margin-top:10px;font-size:12px;background:#0f1115;padding:10px;border-radius:6px;border:1px solid #262b36}@media(max-width:900px){body{display:block;overflow-y:auto;overflow-x:hidden;height:auto}.sidebar{width:100%;border-right:none;display:block}.sidebar-top{overflow-y:visible;padding-bottom:0}.sidebar-bottom{border-top:none;padding-top:5px}.main-content{width:100%;overflow-y:visible;display:block;padding-top:5px}.right-drawer{width:100%;border-left:none;border-top:1px solid #262b36;display:block}.top-nav-row1{flex-direction:column;align-items:flex-start;gap:8px}.controls{flex-wrap:wrap;justify-content:space-between;gap:8px}select,button.btn-control{flex:1 1 45%;font-size:11px;padding:8px 6px;text-align:center}.chart-container{height:400px;margin-top:10px;flex:none}.grid{grid-template-columns:repeat(2,1fr)}.modal{width:95%;padding:15px}}</style></head><body><div class="modal-overlay" id="recModal"><div class="modal"><h2><span>Market Anomaly Screener</span><button class="btn-cancel" style="padding:4px 8px; font-size:12px" onclick="document.getElementById('recModal').style.display='none'">Close</button></h2><div id="recLoader" style="text-align:center; padding: 20px; color:#00d2ff;">Scanning global assets for anomalies...</div><div id="recContent"></div><div class="pagination-row" id="recPagination"><button id="btnPrevPage" class="btn-cancel" onclick="changeRecPage(-1)" style="padding: 6px 12px; font-size:12px;">← Previous 5</button><span id="pageInfo" style="font-size:12px; color:#8a8a9e;"></span><button id="btnNextPage" class="btn-cancel" onclick="changeRecPage(1)" style="padding: 6px 12px; font-size:12px;">Next 5 →</button></div></div></div><div class="modal-overlay" id="alertModal"><div class="modal"><h2><span id="modalTitle">Configure Alerts</span></h2><div class="alert-section" style="margin-bottom:20px; padding-bottom:15px; border-bottom:1px solid #262b36;"><h3 style="font-size:13px; color:#00d2ff; margin-bottom:10px; text-transform:uppercase;">iPhone Lock Screen Alerts (ntfy.sh)</h3><p style="font-size:11px; color:#8a8a9e; margin-top:0; margin-bottom:10px; line-height:1.4;">1. Install free <b>ntfy</b> app on iPhone.<br>2. Subscribe to a topic name (e.g. <code>ian_stock_terminal</code>).<br>3. Enter exact topic name below:</p><div style="display:flex; gap:8px;"><input type="text" id="ntfyTopicInput" placeholder="e.g. ian_stock_terminal" style="background:#0f1115; border:1px solid #262b36; color:#00d2ff; padding:8px; border-radius:6px; flex:1; font-weight:bold;"><button onclick="testPushNotification()" style="background:#00d2ff; color:#000; border:none; font-weight:bold; padding:8px 12px; border-radius:6px; cursor:pointer; font-size:11px;">Test Push</button></div></div><div class="alert-section" id="smartAlertsSection" style="margin-bottom:20px; padding-bottom:15px; border-bottom:1px solid #262b36;"><h3 style="font-size:13px; color:#00d2ff; margin-bottom:15px; text-transform:uppercase;">Smart Tranche Alerts</h3><div style="display:flex; align-items:flex-start; margin-bottom:12px; font-size:14px;"><input type="checkbox" id="chkBuy" style="margin-right:12px; margin-top:3px;" checked><div><b style="color:#fff;">Anomaly Buy Signal</b><span style="color:#8a8a9e; font-size:12px; display:block;">Alert me when mathematical deviation dictates scaling into next tranche.</span></div></div><div style="display:flex; align-items:flex-start; margin-bottom:12px; font-size:14px;"><input type="checkbox" id="chkSell" style="margin-right:12px; margin-top:3px;" checked><div><b style="color:#fff;">Take Profit Target</b><span style="color:#8a8a9e; font-size:12px; display:block;">Alert me when baseline discount shrinks and premium returns.</span></div></div></div><div class="modal-actions" style="display:flex; justify-content:flex-end; gap:10px;"><button class="btn-cancel" onclick="document.getElementById('alertModal').style.display='none'">Cancel</button><button class="btn-save" style="background:#00d2ff; border:none; color:#000; font-weight:bold; padding:10px 18px; border-radius:6px; cursor:pointer;" onclick="saveAlerts()">Save Alerts</button></div></div></div><div class="modal-overlay" id="reasonModal"><div class="modal" style="width: 420px;"><h2>Anomaly Breakdown <button class="btn-cancel" style="padding:4px 8px; font-size:12px" onclick="document.getElementById('reasonModal').style.display='none'">Close</button></h2><p id="reasonText" style="color: #e1e3e6; line-height: 1.6; font-size: 14px; margin-top: 10px;"></p></div></div><div class="sidebar"><div class="sidebar-top"><div class="user-profile-box"><div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;"><label>Active Profile</label><div style="display:flex; gap:4px;"><button onclick="addUserPrompt()" class="user-btn new">+ New</button><button onclick="deleteUserPrompt()" class="user-btn del">Delete</button></div></div><select id="userSelect" onchange="onUserChange(this.value)" style="width:100%; background:#0f1115; color:#fff; border:1px solid #262b36; padding:6px; border-radius:6px; font-size:13px; font-weight:bold;"></select></div><div class="total-shares-box"><label>Total Portfolio Equity</label><div id="mTotalSharesHeldVal" class="total-val">£0.00</div><div id="mTotalSharesPnL" class="total-pnl">Cash: £0.00 | Shares: £0.00</div><div id="mTotalPnLDisplay" class="total-pnl" style="margin-top:6px; font-size:14px;">£0.00 (0.00%)</div><div style="display:flex; justify-content:center; margin-top:8px;"><div class="segmented-control" id="mPnlTogglesSidebar"><input type="radio" name="master_pnl_tf" id="mpnl_all" value="all" checked onchange="updateMasterPnLDisplay()"><label for="mpnl_all">All Time</label><input type="radio" name="master_pnl_tf" id="mpnl_today" value="today" onchange="updateMasterPnLDisplay()"><label for="mpnl_today">Today</label><input type="radio" name="master_pnl_tf" id="mpnl_1h" value="1h" onchange="updateMasterPnLDisplay()"><label for="mpnl_1h">1 Hour</label></div></div></div><div class="master-budget-box"><label>Starting Capital (£)</label><input type="number" id="masterBudgetInput" value="10000" oninput="onMasterBudgetInput()"><div class="budget-sub-stats"><div><span>Live Cash Balance:</span> <b id="mBudgetRemaining" style="color:#00c853;">£10,000.00</b></div></div></div><h2>Live Watchlist</h2><div class="search-box"><input type="text" id="addTickerInput" placeholder="Add Symbol..."><button id="addBtn">+</button></div><ul class="watchlist" id="watchlistUI"></ul></div><div class="sidebar-bottom"><div class="directives-box"><h3>Portfolio Action Directives</h3><div id="directivesList" style="max-height: 120px; overflow-y: auto;"><div style="font-size:11px; color:#787e8e; text-align:center; padding:5px;">Scanning portfolio...</div></div></div><div class="quick-check-box"><div class="qc-header-row"><h3>Instant Anomaly Check</h3><span class="qc-close-btn" onclick="closeQuickScore()">✕</span></div><div class="qc-input-row"><input type="text" id="qsTicker" placeholder="e.g. SSLN.L or MSFT"><button onclick="checkQuickScore()">Score</button></div><div id="qsResult"></div></div><button class="btn-rec" onclick="openRecommendModal()">Search for recommendations</button></div></div><div class="main-content"><div class="top-nav"><div class="top-nav-row1"><h2 id="activeTitle" style="margin:0;">No Stock Loaded</h2><div class="action-buttons"><button id="btnShowTrades" class="btn-control btn-trades active" onclick="toggleShowTrades()">Trades</button><button id="btnShowMath" class="btn-control btn-trades" onclick="toggleShowMath()">Math Overlay</button><button class="btn-control btn-refresh" onclick="fetchData(false)">Refresh</button><button class="btn-control btn-alert" onclick="openAlertModal(false)">Setup Alerts</button><button id="btnDraw" class="btn-control btn-draw" style="border-color:#ffeb3b; color:#ffeb3b; display:flex; align-items:center; gap:4px;" onclick="toggleDrawMode()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="17" y1="3" x2="21" y2="7"></line><polygon points="21 7 7 21 3 21 3 17 17 3"></polygon></svg><span id="drawBtnText">Draw Line</span></button><button id="btnToggleManual" class="btn-control btn-manual active" style="border-color:#ffeb3b;" onclick="toggleManualLines()">Manual</button><button class="btn-control" style="border-color:#ffeb3b; color:#ffeb3b;" onclick="clearManualLines()">Clear</button><button id="btnFullscreen" class="btn-control btn-fullscreen" onclick="toggleFullScreen()">Full Screen</button><button class="btn-control btn-reset" onclick="resetTerminalData()">Reset</button></div></div><div class="top-nav-row2"><div class="controls"><label>Range:</label><select id="periodSelect" onchange="saveUISettings(); updateIntervals(); fetchData(false);"><option value="1d">1 Day</option><option value="5d">5 Days</option><option value="1mo" selected>1 Month</option><option value="6mo">6 Months</option><option value="1y">1 Year</option><option value="5y">5 Years</option><option value="max">Max</option></select><label>Interval:</label><select id="intervalSelect" onchange="saveUISettings(); fetchData(false);"></select><label>Style:</label><select id="styleSelect" onchange="saveUISettings(); renderChart();"><option value="candlestick">Candlestick</option><option value="heikin-ashi">Heikin-Ashi</option><option value="line">Line</option><option value="area">Area</option><option value="bar">Bar</option></select><label>Auto Update:</label><select id="refreshSelect" onchange="saveUISettings(); setupAutoRefresh();"><option value="0">Manual</option><option value="5000">5 secs</option><option value="10000" selected>10 secs</option><option value="30000">30 secs</option><option value="60000">1 min</option><option value="300000">5 mins</option></select></div></div></div><div class="grid"><div class="card"><h3>Live Price</h3><p id="mPrice" class="val-highlight">--</p></div><div class="card"><h3>Math Discount</h3><p id="mDisc" class="val-highlight">--</p></div><div class="card"><h3>Buy Score</h3><p id="mBuy" class="val-highlight">--</p></div><div class="card clickable-card" onclick="showAnomalyReason()"><h3>Active Tranches ⓘ</h3><div id="mTranchesContainer" class="tranche-bars-container"></div><p id="mTranchesText" style="font-size:10px; color:#787e8e; margin-top:4px;">--</p></div><div class="card"><h3>Current Value Owned (£)</h3><input type="number" id="inputHeldVal" value="0" oninput="onValOwnedInput()"><span id="mSharesOwned" style="font-size:10px; color:#787e8e; display:block; margin-top:4px;">0 shares</span></div><div class="card" style="justify-content: flex-start;"><h3>Profit / Loss</h3><p id="mPnL" style="font-size:15px; font-weight:bold; color:#8a8a9e; margin-top:2px;">£0.00 (0.00%)</p><div style="display:flex; justify-content:center; margin-top:8px;"><div class="segmented-control" id="pnlTogglesMain"><input type="radio" name="pnl_tf" id="pnl_all" value="all" checked onchange="updatePnLDisplay()"><label for="pnl_all">All Time</label><input type="radio" name="pnl_tf" id="pnl_today" value="today" onchange="updatePnLDisplay()"><label for="pnl_today">Today</label><input type="radio" name="pnl_tf" id="pnl_1h" value="1h" onchange="updatePnLDisplay()"><label for="pnl_1h">1 Hour</label></div></div></div><div class="card clickable-card" onclick="showAnomalyReason()"><h3>Macro Status ⓘ</h3><p id="mMacro" style="font-size:12px; font-weight:bold; display:flex; justify-content:center; align-items:center; gap:6px;">--</p></div></div><div class="chart-container"><div id="chartLegend" style="position: absolute; top: 15px; left: 15px; z-index: 8; display: flex; flex-direction: column; gap: 4px; pointer-events: none;"></div><div id="loader">Fetching...</div><div id="tvChart"></div><canvas id="chartCanvas"></canvas></div></div><div class="right-drawer"><div class="drawer-card" id="actionCard"><h3>Recommended Action</h3><p id="mActionMain" style="font-size:18px; font-weight:bold;">--</p><span id="mActionSub" style="font-size:11px; color:#8a8a9e; font-weight:normal; display:block; margin-bottom:2px;"></span><div id="actionBtnContainer" style="margin-top:2px;"></div></div><div class="drawer-card"><h3>Recommended Rec.</h3><p id="mRecTradeAmt" style="color:#00c853; font-size:15px; font-weight:bold;">£0.00</p><span id="mRecShares" style="font-size:10px; color:#787e8e; display:block; margin-top:2px;">0 shares</span></div><h2>Trade Log History</h2><ul class="history-list" id="historyUI"></ul><h2 style="cursor:pointer; margin-top:15px; border-top:1px solid #262b36; padding-top:10px;" onclick="document.getElementById('allHistoryUI').style.display = document.getElementById('allHistoryUI').style.display === 'none' ? 'block' : 'none'; this.querySelector('span').innerText = document.getElementById('allHistoryUI').style.display === 'none' ? '▼' : '▲';">All Trades History <span style="float:right;">▼</span></h2><ul class="history-list" id="allHistoryUI" style="display:none; margin-top:10px;"></ul></div><script>let currentChartStyle='';let lastRenderedTicker='';let multiSeries=[];let currentTicker='ALL_SHARES';let tvChart=null;let tvSeries=null;let masterData=[];let showTrades=true;let showMath=false;let masterMathLine=[];let tvMathSeries=null;let currentAnomalyReason="Loading...";let currentLivePrice=0;let currentActiveTranches=0;let currentSharesOwned=0;let currentValOwned=0;let globalPortfolioData={master_budget:10000,history:[],holdings:{},watchlist:[],settings:{}};let autoRefreshTimer=null;let isSettingsLoaded=false;let currentPnLData={};let currentMasterPnLData={};let manualLines={};let isDrawing=false;let currentLine=null;let showManual=true;function updatePnLDisplay(){if(!currentPnLData||!currentPnLData['all'])return;let tf=document.querySelector('input[name="pnl_tf"]:checked').value;let data=currentPnLData[tf]||currentPnLData['all'];let pnlEl=document.getElementById('mPnL');pnlEl.innerText=data.val;pnlEl.style.color=data.color;}function updateMasterPnLDisplay(){if(!currentMasterPnLData||!currentMasterPnLData['all'])return;let tf=document.querySelector('input[name="master_pnl_tf"]:checked').value;let data=currentMasterPnLData[tf]||currentMasterPnLData['all'];let pnlEl=document.getElementById('mTotalPnLDisplay');pnlEl.innerText=data.val;pnlEl.style.color=data.color;}function toggleFullScreen(){let elem=document.documentElement;if(!document.fullscreenElement&&!document.webkitFullscreenElement){if(elem.requestFullscreen){elem.requestFullscreen();}else if(elem.webkitRequestFullscreen){elem.webkitRequestFullscreen();}else if(elem.msRequestFullscreen){elem.msRequestFullscreen();}}else{if(document.exitFullscreen){document.exitFullscreen();}else if(document.webkitExitFullscreen){document.webkitExitFullscreen();}else if(document.msExitFullscreen){document.msExitFullscreen();}}}document.addEventListener('fullscreenchange',()=>{let btn=document.getElementById('btnFullscreen');if(btn){btn.innerText=document.fullscreenElement?"Exit Full Screen":"Full Screen";}});document.addEventListener('webkitfullscreenchange',()=>{let btn=document.getElementById('btnFullscreen');if(btn){btn.innerText=document.webkitFullscreenElement?"Exit Full Screen":"Full Screen";}});function toggleDrawMode(){if(currentTicker==='ALL_SHARES'){alert("Please select a single stock to draw lines.");return;}isDrawing=!isDrawing;currentLine=null;let btnText=document.getElementById('drawBtnText');let btn=document.getElementById('btnDraw');btnText.innerText=isDrawing?"Cancel":"Draw Line";if(isDrawing){btn.classList.add('active');}else{btn.classList.remove('active');}drawCanvasOverlay();}function toggleManualLines(){showManual=!showManual;let btn=document.getElementById('btnToggleManual');if(btn){if(showManual){btn.classList.add('active');}else{btn.classList.remove('active');}}drawCanvasOverlay();}function clearManualLines(){if(currentTicker&&manualLines[currentTicker]){manualLines[currentTicker]=[];currentLine=null;isDrawing=false;let btn=document.getElementById('btnDraw');let btnText=document.getElementById('drawBtnText');btnText.innerText="Draw Line";btn.classList.remove('active');drawCanvasOverlay();}}function toggleShowTrades(){showTrades=!showTrades;let btn=document.getElementById('btnShowTrades');if(btn){showTrades?btn.classList.add('active'):btn.classList.remove('active');}drawCanvasOverlay();}function toggleShowMath(){showMath=!showMath;let btn=document.getElementById('btnShowMath');if(btn){showMath?btn.classList.add('active'):btn.classList.remove('active');}renderChart();}async function fetchUsers(){try{let res=await fetch('/api/users');let data=await res.json();let sel=document.getElementById('userSelect');sel.innerHTML='';(data.users||[]).forEach(u=>{let opt=document.createElement('option');opt.value=u;opt.innerText=u;if(u===data.active_user)opt.selected=true;sel.appendChild(opt);});}catch(e){}}async function onUserChange(username){if(!username)return;if(autoRefreshTimer)clearInterval(autoRefreshTimer);await fetch('/api/users/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username})});currentTicker='ALL_SHARES';isSettingsLoaded=false;await fetchData(false);await fetchUsers();}async function addUserPrompt(){let name=prompt("Enter name for the new profile:");if(!name||!name.trim())return;if(autoRefreshTimer)clearInterval(autoRefreshTimer);await fetch('/api/users/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:name.trim()})});currentTicker='ALL_SHARES';isSettingsLoaded=false;await fetchUsers();fetchData(false);}async function deleteUserPrompt(){let au=document.getElementById('userSelect').value;if(!au)return;if(confirm(`Are you sure you want to delete profile "${au}"?`)){if(autoRefreshTimer)clearInterval(autoRefreshTimer);let res=await fetch('/api/users/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:au})});let data=await res.json();if(data.status==='error'){alert("Cannot delete the last remaining user profile.");return;}currentTicker='ALL_SHARES';isSettingsLoaded=false;await fetchUsers();fetchData(false);}}function saveUISettings(){let ntfyTopic=document.getElementById('ntfyTopicInput')?document.getElementById('ntfyTopicInput').value.trim():'';fetch('/api/portfolio/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({settings:{period:document.getElementById('periodSelect').value,interval:document.getElementById('intervalSelect').value,style:document.getElementById('styleSelect').value,refresh:document.getElementById('refreshSelect').value,ntfy_topic:ntfyTopic}})});}async function testPushNotification(){let topic=document.getElementById('ntfyTopicInput').value.trim();if(!topic){alert("Please enter a topic name first!");return;}saveUISettings();let res=await fetch('/api/push_test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topic:topic})});let data=await res.json();alert(data.status==='ok'?"Test push sent! Check your iPhone's ntfy app.":"Error sending test push.");}async function resetTerminalData(){if(confirm("Reset all portfolio data for this user?")){await fetch('/api/portfolio/reset',{method:'POST'});currentTicker='ALL_SHARES';isSettingsLoaded=false;fetchData(false);}}function setupAutoRefresh(){if(autoRefreshTimer)clearInterval(autoRefreshTimer);let ms=parseInt(document.getElementById('refreshSelect').value)||0;if(ms>0)autoRefreshTimer=setInterval(()=>{fetchData(true);},ms);}function renderWatchlist(watchlist,wl_status={}){let ul=document.getElementById('watchlistUI');ul.innerHTML='';let allLi=document.createElement('li');allLi.className=`watchlist-item ${currentTicker==='ALL_SHARES'?'active':''}`;allLi.setAttribute('data-symbol','ALL_SHARES');allLi.innerHTML=`<span class="ticker" style="color:#00d2ff; font-weight:bold; display:flex; align-items:center;"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:6px;"><path d="M3 3v18h18"/><path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3"/></svg> All Shares</span>`;ul.appendChild(allLi);if(!(watchlist||[]).length){ul.innerHTML+='<li style="font-size:11px; color:#787e8e; text-align:center; padding:10px;">Watchlist is empty</li>';return;}watchlist.forEach(symbol=>{let li=document.createElement('li');li.className=`watchlist-item ${symbol===currentTicker?'active':''}`;li.setAttribute('data-symbol',symbol);let arrow=`<span style="width:14px; margin-right:6px; display:inline-block;"></span>`;if(wl_status[symbol]==='up'){arrow=`<span style="color:#00c853; font-size:12px; width:14px; margin-right:6px; display:inline-block; text-align:center;">▲</span>`;}else if(wl_status[symbol]==='down'){arrow=`<span style="color:#ff3d00; font-size:12px; width:14px; margin-right:6px; display:inline-block; text-align:center;">▼</span>`;}li.innerHTML=`<div style="display:flex; align-items:center;">${arrow}<span class="ticker">${symbol}</span></div><button class="btn-delete">✕</button>`;ul.appendChild(li);});}function openAlertModal(){if(!currentTicker||currentTicker==='ALL_SHARES')return;document.getElementById('modalTitle').innerText=`Configure Alerts: ${currentTicker}`;if(globalPortfolioData.settings&&globalPortfolioData.settings.ntfy_topic)document.getElementById('ntfyTopicInput').value=globalPortfolioData.settings.ntfy_topic;document.getElementById('alertModal').style.display='flex';}function saveAlerts(){saveUISettings();document.getElementById('alertModal').style.display='none';}function onMasterBudgetInput(){let val=parseFloat(document.getElementById('masterBudgetInput').value)||10000;globalPortfolioData.master_budget=val;calculateSizing();fetch('/api/portfolio/budget',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({budget:val})}).then(fetchDirectives);}function onValOwnedInput(){if(!currentTicker||currentTicker==='ALL_SHARES')return;let val=parseFloat(document.getElementById('inputHeldVal').value)||0;let pps=(currentTicker.endsWith('.L')&&currentLivePrice>100)?(currentLivePrice/100.0):currentLivePrice;currentSharesOwned=pps>0?Math.round(val/pps):0;currentValOwned=Math.round((currentSharesOwned*pps+Number.EPSILON)*100)/100;document.getElementById('mSharesOwned').innerText=`${currentSharesOwned} shares`;if(!globalPortfolioData.holdings)globalPortfolioData.holdings={};if(currentSharesOwned>0)globalPortfolioData.holdings[currentTicker]={shares:currentSharesOwned,manual_val:currentValOwned};else delete globalPortfolioData.holdings[currentTicker];calculateSizing();fetch('/api/portfolio/holding',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:currentTicker,value_owned:val,price:currentLivePrice})}).then(fetchDirectives);}function calculateSizing(){let tb=globalPortfolioData.master_budget||10000;let net_hist=0;(globalPortfolioData.history||[]).forEach(h=>{net_hist+=(h.action==='BUY'?-h.amount:h.amount);});let init_man=0;if(globalPortfolioData.initial_positions){Object.values(globalPortfolioData.initial_positions).forEach(p=>{init_man+=(p.manual_val||0);});}let cash_balance=tb+net_hist-init_man;let toa=0;if(globalPortfolioData.holdings){for(let t of Object.keys(globalPortfolioData.holdings)){if(globalPortfolioData.holdings[t]&&globalPortfolioData.holdings[t].shares>0){toa+=(globalPortfolioData.holdings[t].manual_val||0);}}}let rem=cash_balance;let total_equity=cash_balance+toa;if(currentTicker==='ALL_SHARES'){let am=document.getElementById('mActionMain'),as=document.getElementById('mActionSub');let bc=document.getElementById('actionBtnContainer');am.innerText="PORTFOLIO";am.style.color="#00d2ff";as.innerText="Aggregate View";document.getElementById('mRecTradeAmt').innerText="£0.00";document.getElementById('mRecShares').innerText="0 shares";bc.innerHTML="";document.getElementById('inputHeldVal').value=currentValOwned.toFixed(2);document.getElementById('inputHeldVal').disabled=true;document.getElementById('mSharesOwned').innerText="All held shares";document.getElementById('mTotalSharesHeldVal').innerText=`£${total_equity.toFixed(2)}`;document.getElementById('mTotalSharesPnL').innerText=`Cash: £${rem.toFixed(2)} | Shares: £${toa.toFixed(2)}`;let mb_el=document.getElementById('mBudgetRemaining');mb_el.innerText=`£${rem.toFixed(2)}`;mb_el.style.color=rem<0?"#ff3d00":"#00c853";return;}else{document.getElementById('inputHeldVal').disabled=false;}let target=(currentActiveTranches/5.0)*total_equity;let diff=target-currentValOwned;let pps=(currentTicker.endsWith('.L')&&currentLivePrice>100)?(currentLivePrice/100.0):currentLivePrice;let recAmt="£0.00",recSh="0 shares",bc=document.getElementById('actionBtnContainer');bc.innerHTML="";let am=document.getElementById('mActionMain'),as=document.getElementById('mActionSub');let userSel=document.getElementById('userSelect');let activeProf=userSel?userSel.value.trim().toLowerCase():'';let isAuto=['test 2','test 3','test 4','test 5'].some(prefix=>activeProf.includes(prefix));let autoBtnHTML=`<button class="btn-execute" style="background:#00d2ff; color:#000; cursor:default; display:flex; justify-content:center; align-items:center;" disabled><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-right:4px;"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> AI Auto-Executing</button>`;if(!currentTicker){am.innerText="NO ASSET";am.style.color="#787e8e";as.innerText="";}else{let bs=0,ss=0,mv=20.0;if(diff>=mv&&currentActiveTranches>0&&pps>0)bs=Math.floor(Math.min(diff,Math.max(0,rem))/pps);if(currentValOwned>0&&am.innerText==="SELL")ss=currentSharesOwned;else if(currentValOwned>0&&currentActiveTranches===0)ss=currentSharesOwned;else if(diff<=-mv&&pps>0)ss=Math.floor(Math.abs(diff)/pps);if(bs>0&&(bs*pps)>=mv){am.innerText="BUY";am.style.color="#00c853";as.innerText=`(Tranche ${currentActiveTranches})`;recAmt=`+£${(bs*pps).toFixed(2)}`;recSh=`Buy ${bs} shares`;if(isAuto)bc.innerHTML=autoBtnHTML;else bc.innerHTML=`<button class="btn-execute" onclick="executeTradeDirect('${currentTicker}', 'BUY', ${bs}, ${currentLivePrice})">Confirm Buy (${bs} Shs)</button>`;}else if(ss>0&&(ss*pps)>=mv){am.innerText="SELL";am.style.color="#ff3d00";if(ss===currentSharesOwned&&currentActiveTranches>0)as.innerText="(Take Profit / Sentinel)";else as.innerText="(Trim Excess Allocation)";recAmt=`-£${(ss*pps).toFixed(2)}`;recSh=`Sell ${ss} shares`;if(isAuto)bc.innerHTML=autoBtnHTML;else bc.innerHTML=`<button class="btn-execute sell-btn" onclick="executeTradeDirect('${currentTicker}', 'SELL', ${ss}, ${currentLivePrice})">Confirm Sell (${ss} Shs)</button>`;}else{am.innerText="HOLD / WAIT";am.style.color="#8a8a9e";if(diff>5&&rem<=0){as.innerText=`(Insufficient Budget)`;am.style.color="#ff9900";}else if(currentActiveTranches>0&&currentValOwned>0)as.innerText=`(Tranche ${currentActiveTranches} Filled)`;else as.innerText=`(Tranche ${currentActiveTranches})`;}}document.getElementById('mRecTradeAmt').innerText=recAmt;document.getElementById('mRecShares').innerText=recSh;document.getElementById('mTotalSharesHeldVal').innerText=`£${total_equity.toFixed(2)}`;document.getElementById('mTotalSharesPnL').innerText=`Cash: £${rem.toFixed(2)} | Shares: £${toa.toFixed(2)}`;let mb_el=document.getElementById('mBudgetRemaining');mb_el.innerText=`£${rem.toFixed(2)}`;mb_el.style.color=rem<0?"#ff3d00":"#00c853";}async function executeTradeDirect(ticker,action,shares,price){if(shares<=0||!ticker)return;await fetch('/api/trade/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:ticker,action:action,shares:shares,price:price})});fetchData(false);}async function fetchDirectives(){try{let res=await fetch(`/api/directives`);let data=await res.json();let cont=document.getElementById('directivesList');cont.innerHTML="";let userSel=document.getElementById('userSelect');let activeProf=userSel?userSel.value.trim().toLowerCase():'';let isAuto=['test 2','test 3','test 4','test 5'].some(prefix=>activeProf.includes(prefix));if(!(data.directives||[]).length){cont.innerHTML=`<div style="font-size:11px; color:#787e8e; text-align:center; padding:5px;">All positions aligned.</div>`;return;}data.directives.forEach(d=>{let isBuy=d.action==='BUY';let div=document.createElement('div');div.className='directive-item';let btnHTML=isAuto?`<button class="directive-btn" style="background:#00d2ff; color:#000; cursor:default; display:flex; align-items:center;" disabled><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-right:2px;"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Auto</button>`:`<button class="directive-btn ${isBuy?'':'sell'}" onclick="executeTradeDirect('${d.ticker}', '${d.action}', ${d.shares}, ${d.price})">Confirm</button>`;div.innerHTML=`<div class="directive-info"><b>${d.ticker}</b>: ${d.action} ${d.shares} shs (£${d.amount.toFixed(2)})</div>${btnHTML}`;cont.appendChild(div);});}catch(e){}}async function undoTrade(id){await fetch('/api/trade/undo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});fetchData(false);}function renderTradeHistory(){let ui=document.getElementById('historyUI');ui.innerHTML="";let aui=document.getElementById('allHistoryUI');aui.innerHTML="";let hist=globalPortfolioData.history||[];if(!hist.length){aui.innerHTML=`<li style="font-size:11px; color:#787e8e; text-align:center; padding:20px;">No trades logged.</li>`;ui.innerHTML=`<li style="font-size:11px; color:#787e8e; text-align:center; padding:20px;">No trades logged.</li>`;}else{hist.forEach(i=>{let buy=i.action==='BUY';let li=document.createElement('li');li.className='history-item';li.innerHTML=`<span class="btn-undo" onclick="undoTrade('${i.id}')">✕</span><div style="font-size:10px; color:#00d2ff; font-weight:bold; margin-bottom:2px;">${i.ticker}</div><div class="h-action ${buy?'h-buy':'h-sell'}">${i.action} ${i.shares} Shares</div><div class="h-meta">Total: £${i.amount.toFixed(2)} @ ${parseFloat(i.price).toFixed(2)}</div><div class="h-meta" style="font-size:9px; color:#525866;">${i.time}</div>`;aui.appendChild(li);});if(currentTicker==='ALL_SHARES'){hist.forEach(item=>{let isBuy=item.action==='BUY';let li=document.createElement('li');li.className='history-item';li.innerHTML=`<span class="btn-undo" onclick="undoTrade('${item.id}')">✕</span><div style="font-size:10px; color:#00d2ff; font-weight:bold; margin-bottom:2px;">${item.ticker}</div><div class="h-action ${isBuy?'h-buy':'h-sell'}">${item.action} ${item.shares} Shares</div><div class="h-meta">Total: £${item.amount.toFixed(2)} @ ${parseFloat(item.price).toFixed(2)}</div><div class="h-meta" style="font-size:9px; color:#525866;">${item.time}</div>`;ui.appendChild(li);});}else{let th=hist.filter(h=>h.ticker===currentTicker);if(!th.length){ui.innerHTML=`<li style="font-size:11px; color:#787e8e; text-align:center; padding:20px;">No trades logged.</li>`;}else{th.forEach(item=>{let isBuy=item.action==='BUY';let li=document.createElement('li');li.className='history-item';li.innerHTML=`<span class="btn-undo" onclick="undoTrade('${item.id}')">✕</span><div class="h-action ${isBuy?'h-buy':'h-sell'}">${item.action} ${item.shares} Shares</div><div class="h-meta">Total: £${item.amount.toFixed(2)} @ ${parseFloat(item.price).toFixed(2)}</div><div class="h-meta" style="font-size:9px; color:#525866;">${item.time}</div>`;ui.appendChild(li);});}}}}function showAnomalyReason(){if(currentAnomalyReason){document.getElementById('reasonText').innerText=currentAnomalyReason;document.getElementById('reasonModal').style.display='flex';}}function closeQuickScore(){document.getElementById('qsResult').style.display='none';document.getElementById('qsTicker').value='';}async function checkQuickScore(){let t=document.getElementById('qsTicker').value.trim().toUpperCase();if(!t)return;let rd=document.getElementById('qsResult');rd.style.display='block';rd.innerHTML='<span style="color:#00d2ff">Calculating...</span>';try{let res=await fetch(`/api/score?t=${t}`);let data=await res.json();if(data.error){rd.innerHTML=`<span style="color:#ff4a4a">Error: ${data.error}</span>`;return;}let rs=data.score>=60?'<span style="color:#00c853; font-weight:bold;">Strong Anomaly</span>':(data.score>=40?'<span style="color:#ff9900; font-weight:bold;">Moderate</span>':'<span style="color:#ff4a4a; font-weight:bold;">No Anomaly</span>');rd.innerHTML=`<div style="margin-bottom:6px; color:#fff;"><b>${data.name}</b> <span style="color:#00d2ff">(${data.ticker})</span></div><div style="display:flex; justify-content:space-between; margin-bottom:8px; padding-bottom:6px; border-bottom:1px solid #262b36;"><span>Score: <b style="color:#00d2ff">${data.score}/100</b></span><span>${rs}</span></div><div style="color:#8a8a9e; margin-bottom:10px; line-height:1.4;">${data.reason}</div><button class="btn-load" style="padding:6px; font-size:11px;" onclick="loadRecommended('${data.ticker}', ${data.is_smart}, ${data.rec_buy}, ${data.rec_sell})">Load</button>`;}catch(e){rd.innerHTML=`<span style="color:#ff4a4a">Failed.</span>`;}}let allRecs=[],recPg=0,RPP=5;async function openRecommendModal(){document.getElementById('recModal').style.display='flex';document.getElementById('recContent').innerHTML='';document.getElementById('recPagination').style.display='none';document.getElementById('recLoader').style.display='block';try{let res=await fetch('/api/recommend');let data=await res.json();document.getElementById('recLoader').style.display='none';allRecs=data.recommendations;recPg=0;renderRecPage();}catch(e){document.getElementById('recLoader').innerText="Error scanning market.";}}function renderRecPage(){let st=recPg*RPP,en=st+RPP,html='';allRecs.slice(st,en).forEach(r=>{html+=`<div class="rec-item"><div class="rec-header"><div class="rec-title-block"><h3>${r.name} <span style="color:#00d2ff; font-weight:normal;">(${r.ticker})</span></h3><div><span style="font-size:11px; background:#262b36; color:#e1e3e6; padding:2px 6px; border-radius:4px;">${r.type}</span></div></div><span class="rec-score">Score: ${r.score}/100</span></div><div class="rec-desc"><b>Why:</b> ${r.reason}</div><button class="btn-load" onclick="loadRecommended('${r.ticker}', ${r.is_smart}, ${r.rec_buy}, ${r.rec_sell})">Load to Terminal</button></div>`;});document.getElementById('recContent').innerHTML=html;let tps=Math.ceil(allRecs.length/RPP);if(tps>1){document.getElementById('recPagination').style.display='flex';document.getElementById('pageInfo').innerText=`Page ${recPg+1} of ${tps}`;document.getElementById('btnPrevPage').disabled=(recPg===0);document.getElementById('btnNextPage').disabled=(en>=allRecs.length);}}function changeRecPage(dir){recPg+=dir;renderRecPage();}async function loadRecommended(t,s,b,sell){document.getElementById('recModal').style.display='none';await fetch('/api/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:t})});selectStock(t);setTimeout(openAlertModal,500);}function initChart(){let c=document.getElementById('tvChart');tvChart=LightweightCharts.createChart(c,{width:c.clientWidth,height:c.clientHeight||450,layout:{backgroundColor:'#171a21',textColor:'#787e8e'},grid:{vertLines:{color:'#262b36'},horzLines:{color:'#262b36'}},crosshair:{mode:0},timeScale:{borderColor:'#262b36',timeVisible:true},rightPriceScale:{visible:true,borderColor:'#262b36'},leftPriceScale:{visible:false,borderColor:'#262b36'}});tvChart.timeScale().subscribeVisibleTimeRangeChange(drawCanvasOverlay);tvChart.timeScale().subscribeVisibleLogicalRangeChange(drawCanvasOverlay);window.addEventListener('resize',()=>{tvChart.applyOptions({width:c.clientWidth,height:c.clientHeight});drawCanvasOverlay();});tvChart.subscribeClick((param)=>{if(!isDrawing||!param.point||currentTicker==='ALL_SHARES')return;let logical=tvChart.timeScale().coordinateToLogical(param.point.x);if(!tvSeries)return;let price=tvSeries.coordinateToPrice(param.point.y);if(!currentLine){currentLine={l1:logical,p1:price,l2:logical,p2:price};}else{currentLine.l2=logical;currentLine.p2=price;if(!manualLines[currentTicker])manualLines[currentTicker]=[];manualLines[currentTicker].push({...currentLine});currentLine=null;toggleDrawMode();drawCanvasOverlay();}});tvChart.subscribeCrosshairMove((param)=>{if(isDrawing&&currentLine&&param.point&&currentTicker!=='ALL_SHARES'&&tvSeries){currentLine.l2=tvChart.timeScale().coordinateToLogical(param.point.x);currentLine.p2=tvSeries.coordinateToPrice(param.point.y);drawCanvasOverlay();}});}function drawCanvasOverlay(){let cv=document.getElementById('chartCanvas');if(!cv||!tvChart)return;let c=document.getElementById('tvChart');cv.width=c.clientWidth;cv.height=c.clientHeight;let ctx=cv.getContext('2d');ctx.clearRect(0,0,cv.width,cv.height);if(currentTicker==='ALL_SHARES')return;if(showManual&&tvSeries){let lines=manualLines[currentTicker]||[];ctx.setLineDash([]);ctx.strokeStyle='#ffeb3b';ctx.lineWidth=2;lines.forEach(l=>{let x1=tvChart.timeScale().logicalToCoordinate(l.l1);let y1=tvSeries.priceToCoordinate(l.p1);let x2=tvChart.timeScale().logicalToCoordinate(l.l2);let y2=tvSeries.priceToCoordinate(l.p2);if(x1!==null&&y1!==null&&x2!==null&&y2!==null){ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();}});if(isDrawing&&currentLine){let x1=tvChart.timeScale().logicalToCoordinate(currentLine.l1);let y1=tvSeries.priceToCoordinate(currentLine.p1);let x2=tvChart.timeScale().logicalToCoordinate(currentLine.l2);let y2=tvSeries.priceToCoordinate(currentLine.p2);if(x1!==null&&y1!==null&&x2!==null&&y2!==null){ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();}}}if(!showTrades||!globalPortfolioData||!globalPortfolioData.history||!masterData||!masterData.length)return;let th=globalPortfolioData.history.filter(h=>h.ticker===currentTicker);if(!th.length)return;let pm=masterData.map(d=>({rawTime:d.time,ts:typeof d.time==='number'?d.time:Math.floor(new Date(d.time+'T00:00:00Z').getTime()/1000)}));let g={};th.forEach(i=>{let ts=i.timestamp?i.timestamp:Math.floor(new Date((i.date_str||i.time)+'T00:00:00Z').getTime()/1000);if(isNaN(ts))return;let cl=pm[0],md=Math.abs(ts-cl.ts);for(let p of pm){let d=Math.abs(ts-p.ts);if(d<md){md=d;cl=p;}}let x=tvChart.timeScale().timeToCoordinate(cl.rawTime);if(x!==null&&x>=0&&x<=cv.width){let rx=Math.round(x);if(!g[rx])g[rx]=[];g[rx].push(i);}});Object.keys(g).forEach(xs=>{let x=parseFloat(xs),itms=g[xs];ctx.beginPath();ctx.setLineDash([4,4]);ctx.moveTo(x,0);ctx.lineTo(x,cv.height-25);ctx.strokeStyle='#ffffff';ctx.lineWidth=1;ctx.stroke();let sy=cv.height-45;itms.forEach(i=>{let buy=i.action==='BUY',txt=`${i.action} ${i.shares}sh @ £${parseFloat(i.price).toFixed(2)}`;ctx.font='bold 10px -apple-system, sans-serif';let tw=ctx.measureText(txt).width,bw=tw+10,bh=18;ctx.setLineDash([]);ctx.fillStyle=buy?'#00c853':'#ff3d00';ctx.fillRect(x-(bw/2),sy-bh,bw,bh);ctx.fillStyle='#ffffff';ctx.fillText(txt,x-(tw/2),sy-5);sy-=22;});});}function updateTrancheVisual(count){let c=document.getElementById('mTranchesContainer'),t=document.getElementById('mTranchesText');if(!c||!t)return;c.innerHTML='';t.innerText=`${count} / 5 Tranches Active`;for(let i=1;i<=5;i++){let b=document.createElement('div');b.className='tranche-bar '+(i<=count?'active':'inactive');c.appendChild(b);}}function updateIntervals(){let p=document.getElementById('periodSelect').value,s=document.getElementById('intervalSelect'),ci=s.value;s.innerHTML='';let o=(p==='1d'||p==='5d')?[['1m','1 Min'],['5m','5 Mins'],['15m','15 Mins'],['30m','30 Mins'],['1h','1 Hour']]:(p==='1mo'?[['5m','5 Mins'],['15m','15 Mins'],['30m','30 Mins'],['1h','1 Hour'],['1d','1 Day']]:[['1d','1 Day'],['1wk','1 Week'],['1mo','1 Month']]);o.forEach(x=>{let opt=document.createElement('option');opt.value=x[0];opt.text=x[1];s.appendChild(opt);});if(Array.from(s.options).some(x=>x.value===ci))s.value=ci;else s.value=s.options[0].value;}function convertToHeikinAshi(o){let r=[];for(let i=0;i<o.length;i++){let c=o[i],hc=(c.open+c.high+c.low+c.close)/4,ho=i===0?(c.open+c.close)/2:(r[i-1].open+r[i-1].close)/2;r.push({time:c.time,open:ho,high:Math.max(c.high,ho,hc),low:Math.min(c.low,ho,hc),close:hc});}return r;}function renderChart(){if(tvSeries){tvChart.removeSeries(tvSeries);tvSeries=null;}if(tvMathSeries){tvChart.removeSeries(tvMathSeries);tvMathSeries=null;}if(multiSeries&&multiSeries.length){multiSeries.forEach(s=>tvChart.removeSeries(s));multiSeries=[];}let lg=document.getElementById('chartLegend');lg.innerHTML='';if(!masterData.length){let cv=document.getElementById('chartCanvas');if(cv){let ctx=cv.getContext('2d');ctx.clearRect(0,0,cv.width,cv.height);}return;}let s=document.getElementById('styleSelect').value;let activeProf=document.getElementById('userSelect').value.trim().toLowerCase();let isMomentum=['test 2','test 3','test 4','test 5'].some(prefix=>activeProf.includes(prefix));if(currentTicker==='ALL_SHARES'){tvChart.applyOptions({leftPriceScale:{visible:false}});masterData.forEach(line=>{let sr=tvChart.addLineSeries({color:line.color,lineWidth:2});sr.setData(line.data);multiSeries.push(sr);let finalVal=line.data.length>0?line.data[line.data.length-1].value:0;let sign=finalVal>0?'+':'';lg.innerHTML+=`<div style="font-size: 11px; color: ${line.color}; font-weight: bold; text-shadow: 1px 1px 2px #000;">${line.ticker} (${sign}${finalVal.toFixed(2)}%)</div>`;});if(currentChartStyle!=='multi'||lastRenderedTicker!=='ALL_SHARES'){tvChart.timeScale().fitContent();}currentChartStyle='multi';lastRenderedTicker='ALL_SHARES';}else{let needNew=(!tvSeries||currentChartStyle!==s||lastRenderedTicker!==currentTicker);let md=(s==='heikin-ashi'?convertToHeikinAshi(masterData):masterData).map(d=>({time:d.time,open:d.open,high:d.high,low:d.low,close:d.close}));if(s==='area'||s==='line')md=masterData.map(d=>({time:d.time,value:d.close}));if(needNew){if(s==='candlestick'||s==='heikin-ashi')tvSeries=tvChart.addCandlestickSeries({upColor:'#00c853',downColor:'#ff3d00',borderVisible:false,wickUpColor:'#00c853',wickDownColor:'#ff3d00'});else if(s==='bar')tvSeries=tvChart.addBarSeries({upColor:'#00c853',downColor:'#ff3d00'});else if(s==='area')tvSeries=tvChart.addAreaSeries({topColor:'rgba(0, 210, 255, 0.4)',bottomColor:'rgba(0, 210, 255, 0.0)',lineColor:'#00d2ff',lineWidth:2});else tvSeries=tvChart.addLineSeries({color:'#00d2ff',lineWidth:2});tvSeries.setData(md);tvChart.timeScale().fitContent();currentChartStyle=s;lastRenderedTicker=currentTicker;}else{tvSeries=tvChart.addLineSeries({color:'#00d2ff',lineWidth:2});tvSeries.setData(md);}if(showMath&&masterMathLine&&masterMathLine.length>0){tvChart.applyOptions({leftPriceScale:{visible:!isMomentum}});tvMathSeries=tvChart.addLineSeries({color:'#ff9900',lineWidth:2,lineStyle:1,priceScaleId:isMomentum?'right':'left'});tvMathSeries.setData(masterMathLine);}else{tvChart.applyOptions({leftPriceScale:{visible:false}});}}setTimeout(drawCanvasOverlay,50);}async function fetchData(silent=false){let loader=document.getElementById('loader');if(!silent&&loader)loader.style.display='block';try{let res=await fetch(`/api/data?t=${currentTicker}&p=${document.getElementById('periodSelect').value}&i=${document.getElementById('intervalSelect').value}`);let p=await res.json();if(p.is_multi)masterData=p.lines;else masterData=p.ohlc;masterMathLine=p.mathLine||[];globalPortfolioData=p.portfolio;if(!isSettingsLoaded&&globalPortfolioData.settings){let s=globalPortfolioData.settings;if(s.period)document.getElementById('periodSelect').value=s.period;updateIntervals();if(s.interval){let iSel=document.getElementById('intervalSelect');if(Array.from(iSel.options).some(opt=>opt.value===s.interval))iSel.value=s.interval;}if(s.style)document.getElementById('styleSelect').value=s.style;if(s.refresh)document.getElementById('refreshSelect').value=s.refresh;if(s.ntfy_topic&&document.getElementById('ntfyTopicInput'))document.getElementById('ntfyTopicInput').value=s.ntfy_topic;setupAutoRefresh();isSettingsLoaded=true;}let wl_status=p.wl_status||{};renderWatchlist(globalPortfolioData.watchlist,wl_status);let activeElem=document.querySelector(`li.watchlist-item[data-symbol="${currentTicker}"]`);if(activeElem){document.querySelectorAll('.watchlist-item').forEach(x=>x.classList.remove('active'));activeElem.classList.add('active');}document.getElementById('activeTitle').innerText=p.name;currentAnomalyReason=p.metrics.reason;currentLivePrice=p.metrics.price;currentActiveTranches=p.metrics.tranches;currentSharesOwned=p.metrics.shares_owned;currentValOwned=p.metrics.value_owned;document.getElementById('masterBudgetInput').value=p.metrics.master_budget;document.getElementById('mPrice').innerText=p.metrics.price_display;document.getElementById('mDisc').innerText=p.metrics.discount;document.getElementById('mBuy').innerText=p.metrics.buy_score;updateTrancheVisual(p.metrics.tranches);document.getElementById('inputHeldVal').value=currentValOwned;document.getElementById('mSharesOwned').innerText=currentTicker==='ALL_SHARES'?"All held shares":`${currentSharesOwned} shares`;currentMasterPnLData=p.metrics.master_pnl_data||{'all':{val:p.metrics.total_pnl_display,color:p.metrics.total_pnl_color}};updateMasterPnLDisplay();currentPnLData=p.metrics.pnl_data||{'all':{val:p.metrics.pnl_display,color:p.metrics.pnl_color}};updatePnLDisplay();document.getElementById('mMacro').innerHTML=`<span style="display:inline-block; width:10px; height:10px; border-radius:50%; background-color:${p.metrics.color};"></span> ${p.metrics.status}`;calculateSizing();renderTradeHistory();fetchDirectives();renderChart();}catch(e){}if(!silent&&loader)loader.style.display='none';}function selectStock(t,e){currentTicker=t;document.querySelectorAll('.watchlist-item').forEach(x=>x.classList.remove('active'));let activeElem=e||document.querySelector(`li.watchlist-item[data-symbol="${t}"]`);if(activeElem)activeElem.classList.add('active');fetchData(false);}document.getElementById('watchlistUI').addEventListener('click',async e=>{let li=e.target.closest('li.watchlist-item');if(!li)return;let sym=li.getAttribute('data-symbol');if(e.target.classList.contains('btn-delete')){e.stopPropagation();await fetch('/api/watchlist/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:sym})});let rem=(globalPortfolioData.watchlist||[]).filter(s=>s!==sym);currentTicker='ALL_SHARES';fetchData(false);return;}selectStock(sym,li);});document.getElementById('addBtn').addEventListener('click',async()=>{let v=document.getElementById('addTickerInput').value.trim().toUpperCase();if(!v)return;document.getElementById('addTickerInput').value='';await fetch('/api/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:v})});selectStock(v);setTimeout(openAlertModal,500);});document.getElementById('qsTicker').addEventListener('keypress',e=>{if(e.key==='Enter')checkQuickScore();});setTimeout(()=>{initChart();fetchUsers();updateIntervals();fetchData(false);setupAutoRefresh();},100);</script></body></html>"""

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8095))
    app.run(host='0.0.0.0', port=port, debug=False)
"""

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8095))
    app.run(host='0.0.0.0', port=port, debug=False)