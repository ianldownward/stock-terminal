import os, json, time, threading, urllib.request
from pymongo import MongoClient
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
        self.mongo_uri = os.environ.get('MONGO_URI')
        if self.mongo_uri:
            try:
                self.client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=3000)
                self.collection = self.client['stock_terminal']['portfolio']
            except Exception: 
                self.client = None
        else:
            self.client = None
            self.filename = 'portfolio.json'
        
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
        if self.client:
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
                    return json.load(f)
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
            # Your exact 10 approved strategy profiles
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
            
            if 'users' not in self.data or not isinstance(self.data['users'], dict):
                self.data['users'] = {}
                needs_save = True

            # Purge non-approved profiles
            existing = list(self.data['users'].keys())
            for u in existing:
                if u not in allowed_profiles:
                    del self.data['users'][u]
                    needs_save = True

            # Ensure all 10 profiles exist
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
        if self.client:
            try: 
                self.collection.update_one({"_id": "main_store"}, {"$set": data_to_save}, upsert=True)
            except Exception: 
                pass
        else:
            try:
                with open(self.filename, 'w') as f: 
                    json.dump(data_to_save, f, indent=2)
            except Exception: 
                pass

    def active_username(self): 
        return self.data.get('active_user', 'Test E6 - Breakeven Rotator')

    def user_data(self, username=None):
        au = username if username else self.active_username()
        if 'users' not in self.data: self.data['users'] = {}
        if au not in self.data['users']:
            self.data['users'][au] = self.default_user_state(au)
            self.save_data(self.data)
        return self.data['users'][au]

    def switch_user(self, username):
        if 'users' in self.data and username in self.data['users']:
            self.data['active_user'] = username
            self.save_data(self.data)

    def get_shares(self, ticker, username=None):
        if not ticker: return 0.0
        ud = self.user_data(username)
        init_pos = ud.get('initial_positions') or {}
        init_sh = safe_float((init_pos.get(ticker) or {}).get('shares', 0))
        net_sh = sum(safe_float(t.get('shares', 0)) if str(t.get('action')).upper() == 'BUY' else -safe_float(t.get('shares', 0)) for t in ud.get('history', []) if t.get('ticker') == ticker)
        return max(0.0, init_sh + net_sh)

    def execute_trade(self, ticker, action_type, shares, price, username=None):
        with self.trade_lock:
            if not ticker or shares <= 0: return None
            ud = self.user_data(username)
            action = 'BUY' if 'BUY' in action_type.upper() else 'SELL'
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
            
            if 'history' not in ud: ud['history'] = []
            ud['history'].insert(0, entry)
            self.save_data(self.data)
            return entry

portfolio_store = PortfolioManager()