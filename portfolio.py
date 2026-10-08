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
        self.filename = 'portfolio.json'
        self.mongo_uri = os.environ.get('MONGO_URI')
        if self.mongo_uri:
            try:
                self.client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=3000)
                self.collection = self.client['stock_terminal']['portfolio']
            except Exception: 
                self.client = None
        else:
            self.client = None
        
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

    def add_user(self, username):
        if not username or not username.strip(): return
        self.reload()
        u = username.strip()
        if 'users' not in self.data: self.data['users'] = {}
        if u not in self.data['users']:
            self.data['users'][u] = self.default_user_state(u)
        self.data['active_user'] = u
        self.save_data(self.data)

    def delete_user(self, username):
        self.reload()
        if 'users' in self.data and username in self.data['users'] and len(self.data['users']) > 1:
            del self.data['users'][username]
            if self.data.get('active_user') == username:
                self.data['active_user'] = list(self.data['users'].keys())[0]
            self.save_data(self.data)
            return True
        return False

    def switch_user(self, username):
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

    def update_settings(self, settings):
        self.reload()
        ud = self.user_data()
        if 'settings' not in ud: ud['settings'] = {}
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