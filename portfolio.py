import os, json, time, threading, urllib.request
try:
    from pymongo import MongoClient
except ImportError:
    MongoClient = None

from engine import safe_float, normalize_price

def send_push_notification(topic, title, message):
    if not topic: 
        return
    try:
        url = f"https://ntfy.sh/{topic.strip()}"
        req = urllib.request.Request(url, data=message.encode('utf-8'), headers={'Title': title})
        urllib.request.urlopen(req, timeout=3)
    except Exception: 
        pass

class PortfolioManager:
    def __init__(self):
        self.trade_lock = threading.Lock()
        self.filename = 'portfolio.json'
        self.client = None
        self.collection = None
        
        mongo_uri = os.environ.get('MONGO_URI')
        if mongo_uri and MongoClient:
            try:
                self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
                self.collection = self.client['stock_terminal']['portfolio']
            except Exception: 
                self.client = None
                self.collection = None
        
        self.data = self.load()
        self._ensure_default_user()

    def default_user_state(self, username=""):
        prof = str(username).strip().lower()
        if 'test a' in prof:
            wl = ['YCA.L', 'U-UN.TO', 'PHYS', 'PSLV', 'CEF', 'SGLN.L', 'SSLN.L', 'RIO.L', 'BP.L', 'SHEL.L', 'AZN.L']
        else:
            wl = ['TQQQ', 'SOXL', 'NVDL', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'META', 'MSTR', 'PLTR', 'COIN', 'AVGO', 'SQQQ', '3SUS.L']

        return {
            'master_budget': 5000.0,
            'watchlist': wl, 
            'initial_positions': {}, 
            'holdings': {}, 
            'history': [], 
            'settings': {'period': '1d', 'interval': '5m', 'style': 'candlestick', 'refresh': '10000', 'ntfy_topic': ''}
        }

    def load(self):
        if self.client and self.collection is not None:
            try:
                doc = self.collection.find_one({"_id": "main_store"})
                if doc:
                    doc.pop('_id', None)
                    return doc
            except Exception: 
                pass
        
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception: 
                pass
        
        initial = {'active_user': 'Test E6 - Breakeven Rotator', 'users': {}}
        self.save_data(initial)
        return initial

    def reload(self):
        self.data = self.load()
        self._ensure_default_user()

    def _ensure_default_user(self):
        try:
            allowed_profiles = [
                'Test E6 - Breakeven Rotator',
                'Test E7 - Breakeven 0.5% Rotator',
                'Test E4 - Clean EOD Rotator',
                'Test E12 - E4 Momentum Hybrid',
                'Test E8 - Meta-Adaptive Rotator',
                'Test U - Tight Rotator',
                'Test E13 - E6 Breakeven 0.5% Hybrid',
                'Test A - Deep Value',
                'Test E - Rotator',
                'Test X - Dynamic Target Switch (15m ORB)'
            ]
            needs_save = False
            
            if not isinstance(self.data, dict):
                self.data = {'active_user': 'Test E6 - Breakeven Rotator', 'users': {}}
                needs_save = True

            if 'users' not in self.data or not isinstance(self.data['users'], dict):
                self.data['users'] = {}
                needs_save = True

            existing = list(self.data['users'].keys())
            for u in existing:
                if u not in allowed_profiles:
                    del self.data['users'][u]
                    needs_save = True

            for p in allowed_profiles:
                if p not in self.data['users']:
                    self.data['users'][p] = self.default_user_state(p)
                    needs_save = True

            if self.data.get('active_user') not in self.data['users']:
                self.data['active_user'] = allowed_profiles[0]
                needs_save = True

            if needs_save: 
                self.save_data(self.data)
        except Exception: 
            pass

    def save_data(self, data_to_save):
        if self.client and self.collection is not None:
            try: 
                self.collection.update_one({"_id": "main_store"}, {"$set": data_to_save}, upsert=True)
                return
            except Exception: 
                pass
        try:
            with open(self.filename, 'w') as f: 
                json.dump(data_to_save, f, indent=2)
        except Exception: 
            pass

    def active_username(self): 
        return self.data.get('active_user', 'Test E6 - Breakeven Rotator') if isinstance(self.data, dict) else 'Test E6 - Breakeven Rotator'

    def user_data(self, username=None):
        au = username if username else self.active_username()
        if 'users' not in self.data or not isinstance(self.data['users'], dict): 
            self.data['users'] = {}
        if au not in self.data['users']:
            self.data['users'][au] = self.default_user_state(au)
            self.save_data(self.data)
        return self.data['users'][au]

    def add_user(self, username):
        if not username or not username.strip(): return
        self.reload()
        u = username.strip()
        if 'users' not in self.data or not isinstance(self.data['users'], dict): 
            self.data['users'] = {}
        if u not in self.data['users']:
            self.data['users'][u] = self.default_user_state(u)
        self.data['active_user'] = u
        self.save_data(self.data)

    def delete_user(self, username):
        self.reload()
        if 'users' in self.data and isinstance(self.data['users'], dict) and username in self.data['users'] and len(self.data['users']) > 1:
            del self.data['users'][username]
            if self.data.get('active_user') == username:
                self.data['active_user'] = list(self.data['users'].keys())[0]
            self.save_data(self.data)
            return True
        return False

    def switch_user(self, username):
        if 'users' in self.data and isinstance(self.data['users'], dict) and username in self.data['users']:
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
        if 'users' in self.data and isinstance(self.data['users'], dict):
            for u in list(self.data['users'].keys()):
                wl = self.data['users'][u].get('watchlist', [])
                st = self.data['users'][u].get('settings', {})
                self.data['users'][u] = self.default_user_state(u)
                if wl: self.data['users'][u]['watchlist'] = wl
                if st: self.data['users'][u]['settings'] = st
                self.data['users'][u]['master_budget'] = 5000.0
            self.save_data(self.data)

    def update_settings(self, settings):
        self.reload()
        ud = self.user_data()
        if 'settings' not in ud or not isinstance(ud['settings'], dict): 
            ud['settings'] = {}
        ud['settings'].update(settings)
        self.save_data(self.data)

    def update_budget(self, budget):
        self.reload()
        ud = self.user_data()
        ud['master_budget'] = safe_float(budget, 5000.0)
        self.save_data(self.data)

    def add_watchlist(self, ticker):
        if not ticker: return
        self.reload()
        ud = self.user_data()
        tk = ticker.strip().upper()
        if 'watchlist' not in ud or not isinstance(ud['watchlist'], list): 
            ud['watchlist'] = []
        if tk not in ud['watchlist']:
            ud['watchlist'].append(tk)
            self.save_data(self.data)

    def remove_watchlist(self, ticker):
        if not ticker: return
        self.reload()
        tk = ticker.strip().upper()
        ud = self.user_data()
        if 'watchlist' in ud and isinstance(ud['watchlist'], list) and tk in ud['watchlist']:
            ud['watchlist'].remove(tk)
            self.save_data(self.data)

    def get_shares(self, ticker, username=None):
        if not ticker: return 0.0
        ud = self.user_data(username)
        init_pos = ud.get('initial_positions') or {}
        init_sh = safe_float((init_pos.get(ticker) or {}).get('shares', 0))
        hist = ud.get('history') if isinstance(ud.get('history'), list) else []
        net_sh = sum(safe_float(t.get('shares', 0)) if str(t.get('action')).upper() == 'BUY' else -safe_float(t.get('shares', 0)) for t in hist if isinstance(t, dict) and t.get('ticker') == ticker)
        return max(0.0, init_sh + net_sh)

    def set_holding_value(self, ticker, value_owned, current_price, username=None):
        self.reload()
        if not ticker: return 0
        ud = self.user_data(username)
        val = safe_float(value_owned)
        p = safe_float(current_price, 1.0)
        cps = normalize_price(ticker, p)
        target_sh = round(val / cps) if cps > 0 else 0
        
        if 'initial_positions' not in ud or not isinstance(ud['initial_positions'], dict):
            ud['initial_positions'] = {}
        if target_sh > 0:
            ud['initial_positions'][ticker] = {'shares': target_sh, 'manual_val': val}
        else:
            ud['initial_positions'].pop(ticker, None)
            
        self.save_data(self.data)
        return target_sh

    def execute_trade(self, ticker, action_type, shares, price, username=None):
        with self.trade_lock:
            if not ticker or shares <= 0: return None
            ud = self.user_data(username)
            action = 'BUY' if 'BUY' in str(action_type).upper() else 'SELL'
            cost_per_sh = normalize_price(ticker, price)
            
            if action == 'SELL':
                curr_tot = self.get_shares(ticker, username)
                if curr_tot < shares: shares = curr_tot
                if shares <= 0: return None
                
            tot_amt = round(shares * cost_per_sh, 2)
            now = pd.Timestamp.now(tz='Europe/London')
            
            entry = {
                'id': str(int(time.time() * 1000)), 
                'ticker': ticker, 
                'trade_type': 'SHORT' if ticker in ['SQQQ', '3SUS.L'] else 'LONG', 
                'action': action, 
                'shares': shares, 
                'price': price, 
                'amount': tot_amt, 
                'time': now.strftime('%d %b %H:%M'), 
                'date_str': now.strftime('%Y-%m-%d'), 
                'timestamp': int(now.timestamp())
            }
            
            if 'history' not in ud or not isinstance(ud['history'], list): 
                ud['history'] = []
            ud['history'].insert(0, entry)
            self.save_data(self.data)
            
            topic = (ud.get('settings') or {}).get('ntfy_topic', '')
            if topic:
                send_push_notification(topic, f"Trade Executed ({username or self.active_username()})", f"{action} {shares} sh {ticker} @ £{tot_amt:.2f}")
                
            return entry

    def undo_trade(self, trade_id):
        self.reload()
        ud = self.user_data()
        hist = ud.get('history') if isinstance(ud.get('history'), list) else []
        trade = next((t for t in hist if isinstance(t, dict) and str(t.get('id')) == str(trade_id)), None)
        if not trade: return False
        hist.remove(trade)
        ud['history'] = hist
        self.save_data(self.data)
        return True

portfolio_store = PortfolioManager()