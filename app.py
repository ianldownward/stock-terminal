import os, json, time, urllib.request, threading, re
import xml.etree.ElementTree as ET
import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from pymongo import MongoClient
from concurrent.futures import ThreadPoolExecutor

template_dir = 'templates' if os.path.isdir('templates') else '.'
app = Flask(__name__, template_folder=template_dir)
app.secret_key = os.environ.get('SECRET_KEY', 'idc-hub-production-master-secret-2026')

# MASTER ADMIN CREDENTIALS (Pre-seeded & Protected)
ADMIN_USER = "ianldownward@gmail.com"
ADMIN_ALIAS = "ian"
ADMIN_PW = "ild81!MKH"
ADMIN_HASH = generate_password_hash(ADMIN_PW)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

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
        cache_duration = 180 if interval in ['1m', '2m', '5m'] else 400
        if cache_key in YF_CACHE:
            cached_time, df = YF_CACHE[cache_key]
            if not df.empty and (now - cached_time < cache_duration): return df.copy()
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval)
            if not df.empty: YF_CACHE[cache_key] = (now, df)
            return df.copy()
        except Exception: return pd.DataFrame()

VAPID_CLAIMS = {"sub": "mailto:ianldownward@gmail.com"}

VAPID_KEY_FILE = os.path.abspath("vapid_private_key.pem")

def get_vapid_keypair():
    """Generates and writes a valid NIST P-256 EC key directly to a .pem file on disk to prevent ASN.1 parsing errors."""
    portfolio_store.reload()
    vk = portfolio_store.data.get('vapid_keys') or {}
    
    if vk.get('private_pem') and vk.get('public_key'):
        try:
            from cryptography.hazmat.primitives import serialization
            serialization.load_pem_private_key(vk['private_pem'].encode('utf-8'), password=None)
            # Ensure the PEM file exists on disk for pywebpush
            if not os.path.exists(VAPID_KEY_FILE):
                with open(VAPID_KEY_FILE, "w") as f:
                    f.write(vk['private_pem'])
            return VAPID_KEY_FILE, vk['public_key']
        except Exception:
            pass

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    import base64

    # Generate standard SECP256R1 NIST P-256 keypair
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')

    pub = priv.public_key()
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint
    )
    pub_b64 = base64.urlsafe_b64encode(pub_bytes).decode('utf-8').rstrip('=')

    # Save to disk as .pem file
    with open(VAPID_KEY_FILE, "w") as f:
        f.write(priv_pem)

    vk = {'private_pem': priv_pem, 'public_key': pub_b64}
    portfolio_store.data['vapid_keys'] = vk
    portfolio_store.save_data(portfolio_store.data)
    print(f"[VAPID READY] Saved fresh VAPID key file to {VAPID_KEY_FILE}")
    return VAPID_KEY_FILE, pub_b64

def send_native_web_push(title, message):
    """Sends native Apple / Web Push notifications with full APNs priority headers."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("[PUSH ALERT ERROR] 'pywebpush' library is not installed.")
        return {'status': 'error', 'message': 'pywebpush library not installed on server'}

    portfolio_store.reload()
    subs = portfolio_store.data.get('push_subscriptions') or []
    print(f"[PUSH ATTEMPT] {title} | Subscribed devices: {len(subs)}")
    if not subs:
        print("[PUSH ALERT NOTICE] No devices registered in database. Tap Setup Alerts on iPhone.")
        return {'status': 'error', 'message': 'No registered devices in database. Tap Re-Sync Device on iPhone.'}

    key_file_path, _ = get_vapid_keypair()
    dead_subs = []
    payload = json.dumps({'title': title, 'body': message, 'url': '/'})
    
    # Apple APNs High-Priority Wake Headers
    apns_headers = {
        "TTL": "60",
        "Urgency": "high",
        "apns-priority": "10",
        "apns-push-type": "alert"
    }

    sent_count = 0
    errors = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=key_file_path,
                vapid_claims=VAPID_CLAIMS,
                headers=apns_headers,
                timeout=8
            )
            sent_count += 1
            print(f"[PUSH SUCCESS] Notification delivered to APNs.")
        except Exception as ex:
            print(f"[PUSH EXCEPTION] Delivery failed: {ex}")
            errors.append(str(ex))
            if hasattr(ex, 'response') and getattr(ex.response, 'status_code', None) in [404, 410]:
                dead_subs.append(sub)

    if dead_subs:
        portfolio_store.data['push_subscriptions'] = [s for s in subs if s not in dead_subs]
        portfolio_store.save_data(portfolio_store.data)

    if sent_count > 0:
        return {'status': 'ok', 'sent': sent_count}
    return {'status': 'error', 'message': errors[0] if errors else 'Push failed'}

def send_push_notification(topic, title, message):
    if not topic: return
    try:
        url = f"https://ntfy.sh/{topic.strip()}"
        req = urllib.request.Request(url, data=message.encode('utf-8'), headers={'Title': title})
        with urllib.request.urlopen(req, timeout=5): pass
    except Exception: pass

def send_alpaca_order(api_key, secret_key, mode, ticker, action, shares, bot_name="BOT", trail_pct=None):
    """Sends live orders directly to Alpaca with automatic Paper/Live host detection."""
    if not api_key or not secret_key or not ticker or shares <= 0:
        print("[ALPACA REJECTED] Missing keys, ticker, or shares <= 0.")
        return None
    if ticker.endswith('.L'):
        return None  

    api_key_clean = api_key.strip()
    secret_key_clean = secret_key.strip()

    # AUTO-DETECT: If key starts with PK it MUST go to paper; if AK it goes to live
    if api_key_clean.upper().startswith('PK'):
        base_url = "https://paper-api.alpaca.markets"
    elif api_key_clean.upper().startswith('AK'):
        base_url = "https://api.alpaca.markets"
    else:
        base_url = "https://paper-api.alpaca.markets" if mode == 'paper' else "https://api.alpaca.markets"

    # Clean bot prefix so Alpaca's table clearly identifies the bot (e.g. TEST_R, TEST_S, TEST_TQM)
    short_bot_tag = re.sub(r'[^a-zA-Z0-9_]', '', str(bot_name).split(' - ')[0].strip().upper().replace(' ', '_'))
    clean_client_id = f"{short_bot_tag}_{action.upper()}_{ticker.upper()}_{int(time.time())}"
    
    headers = {
        "APCA-API-KEY-ID": api_key_clean,
        "APCA-API-SECRET-KEY": secret_key_clean,
        "Content-Type": "application/json",
        "User-Agent": "IDCTradingTerminal/1.0"
    }

    print(f"[ALPACA ATTEMPT] {action.upper()} {shares} {ticker} via {base_url} (Bot: {bot_name})")

    # Clean liquidation: If selling, close directly via Alpaca position endpoint to automatically cancel resting stops
    if action.lower() == 'sell':
        try:
            # 1. First cancel any resting open orders for this symbol so shares are unlocked
            req_cancel_sym = urllib.request.Request(f"{base_url}/v2/orders?status=open", headers=headers)
            with urllib.request.urlopen(req_cancel_sym, timeout=4) as r_c:
                all_o = json.loads(r_c.read().decode('utf-8'))
                for o in all_o:
                    if (o.get('symbol') or '').upper() == ticker.upper():
                        d_req = urllib.request.Request(f"{base_url}/v2/orders/{o['id']}", headers=headers, method="DELETE")
                        with urllib.request.urlopen(d_req, timeout=4): pass
            time.sleep(0.5)
            
            # 2. Use DELETE /v2/positions/{symbol} which liquidates directly without race conditions
            req_close_pos = urllib.request.Request(f"{base_url}/v2/positions/{ticker.upper()}?qty={int(shares)}", headers=headers, method="DELETE")
            with urllib.request.urlopen(req_close_pos, timeout=6) as resp_c:
                res_data = json.loads(resp_c.read().decode('utf-8'))
                print(f"[ALPACA SUCCESS] Closed position {shares} {ticker} -> ID: {res_data.get('id')}")
                return res_data
        except urllib.error.HTTPError as he:
            # If 404, position is already flat; otherwise fall back to market sell order
            if he.code == 404:
                return {'status': 'ok', 'message': 'Position already closed'}
            print(f"[ALPACA CLOSE NOTICE {he.code}] Falling back to standard market sell: {he.read().decode('utf-8', errors='ignore')}")
        except Exception as e:
            print(f"[ALPACA CLOSE ERROR]: {e}")

    payload = json.dumps({
        "symbol": ticker.upper(),
        "qty": int(shares),
        "side": action.lower(),
        "type": "market",
        "time_in_force": "day",
        "client_order_id": clean_client_id
    }).encode('utf-8')

    req = urllib.request.Request(f"{base_url}/v2/orders", data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            res_data = json.loads(resp.read().decode('utf-8'))
            print(f"[ALPACA SUCCESS] {action.upper()} {shares} {ticker} -> ID: {res_data.get('id')}")
            
            # Attach native trailing stop order on Alpaca exchange servers after BUY fills
            if action.lower() == 'buy' and trail_pct:
                time.sleep(1) # Brief pause to allow entry fill
                trail_payload = json.dumps({
                    "symbol": ticker.upper(),
                    "qty": int(shares),
                    "side": "sell",
                    "type": "trailing_stop",
                    "trail_percent": round(trail_pct, 2),
                    "time_in_force": "day",
                    "client_order_id": f"{short_bot_tag}_TRAIL_{ticker.upper()}_{int(time.time())}"
                }).encode('utf-8')
                req_trail = urllib.request.Request(f"{base_url}/v2/orders", data=trail_payload, headers=headers, method="POST")
                with urllib.request.urlopen(req_trail, timeout=6):
                    print(f"[ALPACA TRAIL SECURED] Native {trail_pct}% Stop active on exchange.")
            return res_data
    except urllib.error.HTTPError as he:
        err_body = he.read().decode('utf-8', errors='ignore')
        print(f"[ALPACA HTTP ERROR {he.code}] {err_body}")
        return {'status': 'error', 'code': he.code, 'message': err_body}
    except Exception as e:
        print(f"[ALPACA ERROR] Execution failed: {e}")
        return {'status': 'error', 'message': str(e)}

def alpaca_cancel_and_liquidate(api_key, secret_key, mode):
    """Emergency Panic Switch: Cancels all open orders and liquidates all open positions with pre-market limit routing."""
    if not api_key or not secret_key:
        return {'status': 'error', 'message': 'Missing API credentials.'}
    base_url = "https://paper-api.alpaca.markets" if mode == 'paper' else "https://api.alpaca.markets"
    headers_auth = {"APCA-API-KEY-ID": api_key.strip(), "APCA-API-SECRET-KEY": secret_key.strip(), "Content-Type": "application/json"}

    # 1. Cancel all open orders
    try:
        req_cancel = urllib.request.Request(
            f"{base_url}/v2/orders",
            headers=headers_auth,
            method="DELETE"
        )
        with urllib.request.urlopen(req_cancel, timeout=5): pass
    except Exception: pass

    # 2. Close all open positions
    try:
        req_close = urllib.request.Request(
            f"{base_url}/v2/positions?cancel_orders=true",
            headers=headers_auth,
            method="DELETE"
        )
        with urllib.request.urlopen(req_close, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))

            # Check if in pre-market (09:00 - 14:30 BST) where standard market orders queue
            now_uk = pd.Timestamp.now(tz='Europe/London')
            cur_mins = now_uk.hour * 60 + now_uk.minute
            if (9 * 60) <= cur_mins < (14 * 60 + 30):
                time.sleep(0.5)
                # Fetch positions and submit extended-hours limit sells to fill immediately
                try:
                    req_pos = urllib.request.Request(f"{base_url}/v2/positions", headers=headers_auth)
                    with urllib.request.urlopen(req_pos, timeout=4) as p_resp:
                        p_list = json.loads(p_resp.read().decode('utf-8'))
                        for p in p_list:
                            sym = p['symbol'].upper()
                            q = int(p.get('qty', 0))
                            cur_p = float(p.get('current_price', 0.0))
                            if q > 0 and cur_p > 0:
                                lim_p = round(cur_p * 0.985, 2) # Cross pre-market spread to fill immediately
                                ext_order = json.dumps({
                                    "symbol": sym, "qty": q, "side": "sell",
                                    "type": "limit", "time_in_force": "day",
                                    "limit_price": lim_p, "extended_hours": True
                                }).encode('utf-8')
                                req_ext = urllib.request.Request(f"{base_url}/v2/orders", data=ext_order, headers=headers_auth, method="POST")
                                with urllib.request.urlopen(req_ext, timeout=4): pass
                except Exception: pass

            return {'status': 'ok', 'data': data}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    
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
        wl = ['TQQQ', 'SOXL', 'SQQQ'] # Clean 3x leveraged index ETF universe only

        return {
            'master_budget': 500.0,
            'watchlist': wl, 
            'initial_positions': {}, 
            'holdings': {}, 
            'history': [], 
            'drawings': {},
            'notified_signals': {},
            'settings': {
                'period': '1d', 
                'interval': '1m' if any(x in prof for x in ['test s', 'test r', 'test tqm']) else '5m', 
                'style': 'candlestick', 
                'refresh': '10000', 
                'ntfy_topic': ''
            }
        }

    def blank_client_state(self, username=""):
        return {
            'username': username,
            'master_budget': 0.0,
            'starting_capital': 0.0,
            'is_configured': False,
            'watchlist': ['TQQQ', 'SOXL', 'SQQQ'],
            'broker': {
                'connected': False,
                'provider': 'alpaca',
                'mode': 'paper',
                'api_key': '',
                'secret_key': '',
                'strategy': 'test s'
            },
            'initial_positions': {},
            'holdings': {},
            'history': [],
            'drawings': {},
            'notified_signals': {},
            'settings': {
                'period': '1d',
                'interval': '5m',
                'style': 'candlestick',
                'refresh': '15000'
            }
        }

    def get_client_store(self, username):
        if not username or username.strip().lower() in [ADMIN_USER.lower(), ADMIN_ALIAS.lower()]:
            return None
        doc_id = f"client_{username.strip().lower()}"
        if self.client:
            try:
                doc = self.client['stock_terminal']['accounts'].find_one({"_id": doc_id})
                if doc and 'portfolio' in doc: return doc['portfolio']
            except Exception: pass
        return self.blank_client_state(username)

    def save_client_store(self, username, client_portfolio):
        if not username or username.strip().lower() in [ADMIN_USER.lower(), ADMIN_ALIAS.lower()]:
            return
        doc_id = f"client_{username.strip().lower()}"
        if self.client:
            try:
                self.client['stock_terminal']['accounts'].update_one(
                    {"_id": doc_id},
                    {"$set": {"portfolio": client_portfolio}},
                    upsert=True
                )
            except Exception: pass

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
                'Vector Hybrid',
                'Dual Axis',
                'Velocity Pulse',
                'Sovereign Core'
            ]
            needs_save = False
            if 'users' not in self.data or not isinstance(self.data['users'], dict):
                self.data['users'] = {}
                needs_save = True

            # Ensure default profiles exist with £500 budget baseline
            existing_users = list(self.data['users'].keys())

            for p in allowed_profiles:
                if p not in self.data['users'] or not self.data['users'][p].get('watchlist'):
                    self.data['users'][p] = self.default_user_state(p)
                    needs_save = True
                else:
                    if self.data['users'][p].get('master_budget') != 500.0:
                        self.data['users'][p]['master_budget'] = 500.0
                        needs_save = True

            if self.data.get('active_user') not in self.data['users']:
                self.data['active_user'] = 'Ian'
                needs_save = True
                
            if needs_save: self.save_data(self.data)
        except Exception as e: print(f"Cleanup error: {e}")

    def save_data(self, data_to_save):
        if self.client:
            try: 
                # Use replace_one so obsolete profiles are completely wiped from MongoDB
                self.collection.replace_one({"_id": "main_store"}, data_to_save, upsert=True)
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
            self.data['users'][u] = self.default_user_state(u)
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

    def get_drawings(self, ticker, username=None):
        self.reload()
        ud = self.user_data(username)
        if 'drawings' not in ud:
            ud['drawings'] = {}
        return ud['drawings'].get(ticker.upper(), [])

    def save_drawings(self, ticker, drawings_list, username=None):
        self.reload()
        ud = self.user_data(username)
        if 'drawings' not in ud:
            ud['drawings'] = {}
        ud['drawings'][ticker.upper()] = drawings_list
        self.save_data(self.data)
        return True

    def clear_drawings(self, ticker, username=None):
        self.reload()
        ud = self.user_data(username)
        if 'drawings' in ud and ticker.upper() in ud['drawings']:
            ud['drawings'][ticker.upper()] = []
            self.save_data(self.data)
        return True
    
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

    def execute_trade(self, ticker, action_type, shares, price, username=None, metadata=None):
        with self.trade_lock:
            self.reload()
            if not ticker or shares <= 0: return None
            ud = self.user_data(username)
            action = 'BUY' if 'BUY' in action_type.upper() else 'SELL'
            cost_per_sh = price / 100.0 if ticker.endswith('.L') and price > 100 else price
            
            if action == 'SELL':
                curr_tot = self.get_shares(ticker, username)
                if curr_tot < shares: shares = curr_tot
                if shares <= 0: return None
                
            if action == 'BUY':
                mb = ud.get('master_budget', 500.0)
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
            if metadata: entry.update(metadata)
            
            if 'history' not in ud: ud['history'] = []
            ud['history'].insert(0, entry)

            curr_tot = self.get_shares(ticker, username)
            if 'holdings' not in ud: ud['holdings'] = {}
            if curr_tot > 0: 
                hw = (ud['holdings'].get(ticker) or {}).get('high_water', price)
                ud['holdings'][ticker] = {'shares': curr_tot, 'manual_val': round(curr_tot * cost_per_sh, 2), 'high_water': max(hw, price)}
            else: ud['holdings'].pop(ticker, None)
            
            self.save_data(self.data)
            # Multi-Bot Broker Dispatch Engine
            global_broker = self.data.get('broker') or {}
            
            # Fallback to user credentials if top-level store is missing keys
            if not global_broker.get('api_key') or not global_broker.get('secret_key'):
                for u in self.data.get('users', {}).values():
                    if isinstance(u, dict) and u.get('broker', {}).get('api_key'):
                        global_broker.update(u['broker'])
                        self.data['broker'] = global_broker
                        break

            is_paused = global_broker.get('paused', False)
            allocations = global_broker.get('allocations') or {}
            current_profile = str(username or self.active_username()).lower()

            # Strictly obey user allocations: If a bot is unchecked, DO NOT route to Alpaca
            matched_alloc = None
            should_route_alpaca = False
            for s_key, s_data in allocations.items():
                if s_key in current_profile:
                    matched_alloc = s_data
                    should_route_alpaca = bool(s_data.get('enabled', False))
                    break

            if should_route_alpaca and not is_paused and global_broker.get('api_key') and global_broker.get('secret_key') and not (metadata and metadata.get('source') == 'alpaca_sync'):
                strat_prof = str(current_profile).lower()
                if any(x in strat_prof for x in ['test e6', 'test e17', 'test e22', 'test e23', 'test e24', 'test e25', 'test e26', 'test e27', 'test e28', 'test tq', 'test s2', 'test m']):
                    t_pct = 0.75
                elif 'test e5' in strat_prof:
                    t_pct = 0.35
                elif any(x in strat_prof for x in ['test e', 'test s']):
                    t_pct = 1.00
                else:
                    t_pct = 0.50

                threading.Thread(
                    target=send_alpaca_order,
                    args=(
                        global_broker['api_key'],
                        global_broker['secret_key'],
                        global_broker.get('mode', 'paper'),
                        ticker,
                        action,
                        shares,
                        current_profile,
                        t_pct
                    ),
                    daemon=True
                ).start()

            # 1. Clean Notification Layout (No Colored Circle Emojis)
            prof_short = (username or self.active_username()).split(' - ')[0].strip()
            action_word = "Buy" if action == "BUY" else "Sell"
            push_title = f"{prof_short} - {action_word} {shares} - {ticker}"

            pnl_line = ""
            if action == 'SELL':
                # Match against open buys FIFO to determine realized P&L
                hist_prior = [tr for tr in ud.get('history', []) if tr.get('id') != entry.get('id')]
                buy_queue = []
                for t in reversed(hist_prior):
                    tk = t.get('ticker')
                    if tk != ticker or tk == 'CALIBRATION': continue
                    if t.get('action') == 'BUY':
                        buy_queue.append({'shares': t.get('shares', 0), 'amount': t.get('amount', 0.0)})
                    elif t.get('action') == 'SELL':
                        rem = t.get('shares', 0)
                        while buy_queue and rem > 0:
                            b = buy_queue[0]
                            m = min(b['shares'], rem)
                            b['shares'] -= m
                            rem -= m
                            if b['shares'] <= 0: buy_queue.pop(0)

                cost_basis = 0.0
                matched_sh = 0
                rem_sh = shares
                while buy_queue and rem_sh > 0:
                    b = buy_queue[0]
                    m = min(b['shares'], rem_sh)
                    unit_c = (b['amount'] / b['shares']) if b['shares'] > 0 else 0.0
                    cost_basis += m * unit_c
                    matched_sh += m
                    rem_sh -= m
                    b['shares'] -= m
                    b['amount'] -= m * unit_c
                    if b['shares'] <= 0: buy_queue.pop(0)

                if matched_sh > 0 and cost_basis > 0:
                    diff_val = tot_amt - cost_basis
                    diff_pct = (diff_val / cost_basis) * 100.0
                    arrow = "▲" if diff_val >= 0 else "▼"
                    sign = "+" if diff_val >= 0 else "-"
                    pnl_line = f"\n{arrow} {sign}£{abs(diff_val):.2f} ({sign}{abs(diff_pct):.1f}%)"

            push_message = f"£{cost_per_sh:.2f} (Total £{tot_amt:.2f}){pnl_line}"

            send_native_web_push(
                title=push_title,
                message=push_message
            )
            # 2. Legacy fallback if topic configured
            ntfy_topic = (ud.get('settings') or {}).get('ntfy_topic', '')
            if ntfy_topic: send_push_notification(ntfy_topic, push_title, push_message)
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
            'GLEN.L': 'Glencore plc', 'RIO.L': 'Rio Tinto plc', 'HSBA.L': 'HSBC Holdings', 'GSK.L': 'GSK plc', 'ULVR.L': 'Unilever plc',
            'MU': 'Micron Technology', 'SMCI': 'Super Micro Computer', 'GEV': 'GE Vernova',
            'RDW': 'Redwire Space', 'STM': 'STMicroelectronics', 'NBIS': 'Nebius Group', 'SNDK': 'SanDisk Corp',
            'CEG': 'Constellation Energy', 'VST': 'Vistra Corp', 'VRT': 'Vertiv Holdings', 'ANET': 'Arista Networks',
            'RKLB': 'Rocket Lab USA', 'ASTS': 'AST SpaceMobile', 'LUNR': 'Intuitive Machines',
            'FLY': 'Firefly Aerospace', 'YSS': 'York Space Systems', 'TSAT': 'Telesat Corp', 'MDA': 'MDA Space'
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
                
                if score <= 20: state, color, code = "Deep Freeze (Capitulation)", "#ffd400", "BEAR_FREEZE"
                elif score <= 40: state, color, code = "Cooling (Pullback)", "#ff9900", "BEAR"
                elif score <= 60: state, color, code = "Room Temp (Neutral)", "#8a8a9e", "NEUTRAL"
                elif score <= 80: state, color, code = "Heating Up (Expansion)", "#0ecb81", "BULL"
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
            return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': '0.00%', 'reason': 'Insufficient intraday price history.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Tranche 0)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'No Data'}

        prof = profile.lower()
        pct_change_5d = ((current_price - df_5m['Close'].iloc[0]) / df_5m['Close'].iloc[0]) * 100.0
        now_uk = pd.Timestamp.now(tz='Europe/London')

        ema9 = df_5m['Close'].ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = df_5m['Close'].ewm(span=21, adjust=False).mean().iloc[-1]
        delta = df_5m['Close'].diff()
        rs = (delta.where(delta > 0, 0)).rolling(14).mean() / (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + rs.iloc[-1])) if not rs.empty else 50

        # DYNAMIC ASSET-SPECIFIC TRAILING STOPS & HARD STOPS
        peak_gain_pct = ((highest_price - avg_buy_price) / avg_buy_price * 100.0) if avg_buy_price > 0 and highest_price > 0 else 0.0

        if 'test w' in prof:
            if is_3x_etf: trail_pct, hard_pct = 1.25, -1.00
            elif ticker.endswith('.L') or ticker in ['PHYS', 'PSLV', 'CEF', 'GLD']: trail_pct, hard_pct = 0.30, -0.50
            else: trail_pct, hard_pct = 1.00, -1.00
        elif 'test e14' in prof:
            # When gain touches +1.00%, trail expands to 1.50% to let runners breathe
            trail_pct, hard_pct = (1.50, -1.00) if peak_gain_pct >= 1.00 else (1.00, -1.00)
        elif any(x in prof for x in ['test e6', 'test e17', 'test e22', 'test e23', 'test e24', 'test e25', 'test e26', 'test e27', 'test e28', 'test tq', 'test s2', 'test m']): trail_pct, hard_pct = 0.75, -0.75
        elif 'test e7' in prof: trail_pct, hard_pct = 0.50, -0.50
        elif 'test e5' in prof: trail_pct, hard_pct = 0.35, -0.35
        elif 'test u' in prof or 'test x' in prof: trail_pct, hard_pct = 0.50, -0.50
        elif any(x in prof for x in ['test e', 'test s']): trail_pct, hard_pct = 1.00, -1.00
        else: trail_pct, hard_pct = 0.50, -0.50

        health_pct, health_color, health_text = 0, "#8a8a9e", "Scanning..."

        # Calculate Rotator Health Bar & PnL from Buy Position
        if avg_buy_price > 0 and highest_price > 0:
            pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
            drop_from_peak_pct = ((highest_price - current_price) / highest_price) * 100.0
            peak_pnl_pct = ((highest_price - avg_buy_price) / avg_buy_price) * 100.0
            
            # TEST X: OPTIMAL DYNAMIC TARGET HYBRID (STAGE 1 -> STAGE 2 SWITCH)
            if 'test x' in prof:
                if peak_pnl_pct >= 0.50:
                    breakeven_price = avg_buy_price * 1.0020  # Lock +0.20% minimum profit once +0.50% hit
                    if current_price <= breakeven_price:
                        return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"TEST X DYNAMIC LOCK TRIPPED ({pnl_pct:.2f}%). Peak was +{peak_pnl_pct:.2f}%.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Dynamic Lock', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Lock +0.20%)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': f"Dynamic Lock (+{pnl_pct:.2f}%)"}

            # TEST S2: VELOCITY RATCHET (ONCE PEAK HITS +1.20%, LOCK +1.00% FLOOR & CHOKE TRAIL TO 0.40%)
            if 'test s2' in prof and peak_pnl_pct >= 1.20:
                trail_pct = 0.40
                s2_floor = avg_buy_price * 1.0100
                if current_price <= s2_floor:
                    return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"S2 VELOCITY FLOOR HIT (+{pnl_pct:.2f}%). Protected +1.00% gain (Peak +{peak_pnl_pct:.2f}%).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Velocity Floor Lock', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Lock +1.00%)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': f"Velocity Lock (+{pnl_pct:.2f}%)"}

            # SIMULATION-OPTIMIZED BREAKEVEN LOCK (+0.18% LOCK ONCE +0.50% PEAK HIT)
            # Protects early winning turns from turning into losses across Vector Hybrid and Dual Axis
            if any(x in prof for x in ['vector', 'dual', 'tqm', 'tq']) and peak_pnl_pct >= 0.50:
                breakeven_price = avg_buy_price * 1.0018  # Locks +0.18% minimum profit
                if current_price <= breakeven_price:
                    return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"BREAKEVEN LOCK HIT (+{pnl_pct:.2f}%). Peak was +{peak_pnl_pct:.2f}%.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Breakeven Lock', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Lock +0.18%)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': f"Breakeven Lock (+{pnl_pct:.2f}%)"}

            # VELOCITY CHOKE ON RUNNERS: Choke trail to 0.40% once peak reaches >= +1.20%
            if any(x in prof for x in ['vector', 'dual', 'velocity', 'tqm', 'tq', 'test s']) and peak_pnl_pct >= 1.20:
                trail_pct = 0.40

            # TIERED CHOKE-TRAIL, RATCHET PROFIT FLOORS & 18:30 BANK-AND-WALK
            if any(x in prof for x in ['test e22', 'test e23', 'test e24', 'test e25', 'test e26', 'test e27', 'test e28', 'test tq', 'test s2', 'test m']):
                # 1. EVENING BANK-AND-WALK: Past 18:30 BST with >= +2.80% profit -> Exit & shut down
                if (now_uk.hour > 18 or (now_uk.hour == 18 and now_uk.minute >= 30)) and pnl_pct >= 2.80:
                    return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"E22 BANK-AND-WALK: Banked +{pnl_pct:.2f}% after 18:30 BST.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Bank & Walk', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Bank Win)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 100, 'health_color': '#0ecb81', 'health_text': f"Banked (+{pnl_pct:.2f}%)"}

                # 2. TIER 3 (3.0% WINDFALL): Peak touched >= +3.00%
                if peak_pnl_pct >= 3.00:
                    trail_pct = 0.30  # Choke trailing stop to 0.30%
                    tier3_floor = avg_buy_price * 1.0280  # Hard floor at +2.80% net profit
                    if current_price <= tier3_floor:
                        return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"E22 TIER 3 FLOOR HIT (+{pnl_pct:.2f}%). Protected +2.80% floor (Peak +{peak_pnl_pct:.2f}%).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Tier 3 Floor Lock', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Lock +2.80%)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': f"Tier 3 Floor (+{pnl_pct:.2f}%)"}

                # 3. TIER 2: Peak touched >= +2.50%
                elif peak_pnl_pct >= 2.50:
                    trail_pct = 0.30  # Choke trailing stop to 0.30%
                    tier2_floor = avg_buy_price * 1.0200  # Hard floor at +2.00% net profit
                    if current_price <= tier2_floor:
                        return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"E22 TIER 2 FLOOR HIT (+{pnl_pct:.2f}%). Protected +2.00% floor (Peak +{peak_pnl_pct:.2f}%).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Tier 2 Floor Lock', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Lock +2.00%)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': f"Tier 2 Floor (+{pnl_pct:.2f}%)"}
                
                # 4. TIER 1 (DAILY TARGET REACHED): Peak touched >= +1.50%
                elif peak_pnl_pct >= 1.50:
                    tier1_floor = avg_buy_price * 1.0100  # Hard floor at +1.00% net profit
                    if current_price <= tier1_floor:
                        return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"E22 TIER 1 FLOOR HIT (+{pnl_pct:.2f}%). Protected +1.00% floor (Peak +{peak_pnl_pct:.2f}%).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Tier 1 Floor Lock', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Lock +1.00%)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': f"Tier 1 Floor (+{pnl_pct:.2f}%)"}
                    
            # TEST E16: CALIBRATED SPREAD-AWARE BREAKEVEN (+0.18% LOCK ONCE +0.35% PEAK HIT)
            if 'test e16' in prof and peak_pnl_pct >= 0.35:
                breakeven_price = avg_buy_price * 1.0018  # Lock +0.18% profit to absorb bid/ask spread
                if current_price <= breakeven_price:
                    return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"TEST E16 SPREAD LOCK TRIPPED ({pnl_pct:.2f}%). Peak was +{peak_pnl_pct:.2f}%.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Spread Lock', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Lock +0.18%)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': f"Spread Lock (+{pnl_pct:.2f}%)"}

            # BREAKEVEN LOCK RULE FOR TEST E7 ONLY (+0.50%)
            if 'test e7' in prof and peak_pnl_pct >= 0.50:
                breakeven_price = avg_buy_price * 1.0010  # Lock +0.10% profit minimum
                if current_price <= breakeven_price:
                    return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"BREAKEVEN LOCK TRIPPED ({pnl_pct:.2f}%). Peak was +{peak_pnl_pct:.2f}%.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Breakeven Lock', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Lock Breakeven)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': f"Breakeven Lock (+{pnl_pct:.2f}%)"}

            # TEST E14: SCALED EXIT RUNNER FLOOR LOCK (+0.20% MINIMUM FLOOR)
            if 'test e14' in prof and peak_pnl_pct >= 1.00:
                floor_stop_e14 = avg_buy_price * 1.0020
                if current_price <= floor_stop_e14:
                    return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"TEST E14 FLOOR STOP TRIPPED ({pnl_pct:.2f}%). Peak: +{peak_pnl_pct:.2f}%.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Runner Floor Lock', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Floor Lock)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': f"Locked (+{pnl_pct:.2f}%)"}
            health_pct = int(max(0, min(100, 100 - (drop_from_peak_pct / trail_pct * 100))))
            pnl_str = f"({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)"
            
            if drop_from_peak_pct >= trail_pct or pnl_pct <= hard_pct:
                return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"STOP TRIPPED {pnl_str}. Peak: £{highest_price:.2f}.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Stop Tripped', 'color': '#ff5364', 'action_main': 'SELL', 'action_sub': '(Stop Loss)', 'action_color': '#ff5364', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff5364', 'health_text': f"Stop Tripped {pnl_str}"}
            elif health_pct >= 70:
                health_color, health_text = "#0ecb81", f"Strong Trend {pnl_str}"
            elif health_pct >= 40:
                health_color, health_text = "#ff9900", f"Pullback {pnl_str}"
            else:
                health_color, health_text = "#ff5364", f"Danger Zone {pnl_str}"

        # OVERRIDE: LSE CROSS-MARKET SWEEP FOR TEST W
        if 'test w' in prof and ticker.endswith('.L') and now_uk.hour == 16 and now_uk.minute >= 20 and now_uk.minute < 30:
            if avg_buy_price > 0:
                pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                return {'type': 'LSE Sweep', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': "LSE CROSS-MARKET SWEEP.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'LSE Cash Sweep', 'color': '#ff9900', 'action_main': 'SELL', 'action_sub': '(LSE Sweep)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'Forced LSE Sell'}

        # OVERRIDE: LATE-DAY ENTRY CURFEW (NO NEW ENTRIES AFTER 20:00 BST)
        if any(x in prof for x in ['test e', 'test s', 'test u', 'test w', 'test x']) and now_uk.hour >= 20 and avg_buy_price == 0:
            return {'type': 'EOD Curfew', 'score': 0, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': "LATE-DAY CURFEW: No new entries permitted after 20:00 BST.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Curfew Active', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(Curfew Active)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'Curfew Active'}

        # OVERRIDE: EOD SWEEP RULE FOR ROTATORS (SWEEPS ALL ROTATORS TO 100% CASH AT 20:50 BST)
        if any(x in prof for x in ['test e', 'test s', 'test tq', 'test r', 'test tqm']) and now_uk.hour == 20 and now_uk.minute >= 50:
            if avg_buy_price > 0:
                pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                return {'type': 'EOD Sweep', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': "EOD ROTATOR SWEEP: Liquidating to 100% cash.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'EOD Cash Sweep', 'color': '#ff9900', 'action_main': 'SELL', 'action_sub': '(EOD Sweep)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'Forced EOD Sell'}
            return {'type': 'EOD Sweep', 'score': 0, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': "EOD ROTATOR SWEEP: Blocking new entries.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'EOD Block Active', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(EOD Blocked)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'EOD Blocked'}

        # OVERRIDE: QUICK 1.2% TARGET FOR TIGHT TARGET PROFILES ONLY (EXCLUDES S2 WHICH USES VELOCITY RATCHET)
        if any(x in prof for x in ['test u', 'test s']) and 'test s2' not in prof and is_3x_etf and avg_buy_price > 0:
            pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
            if pnl_pct >= 1.20:
                return {'type': 'Rotator Target', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': 'Quick 1.2% Target Hit on 3x ETF.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Take Profit', 'color': '#ffd400', 'action_main': 'SELL', 'action_sub': '(Target Hit)', 'action_color': '#ffd400', 'regime': regime, 'trade_type': trade_type, 'health_pct': 100, 'health_color': '#ffd400', 'health_text': 'Target Hit (Selling)'}
            
        # MAIN HOLDING LOGIC
        if avg_buy_price > 0 and highest_price > 0:
            if drop_from_peak_pct >= trail_pct:
                return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"TRAILING STOP TRIPPED. Dropped {drop_from_peak_pct:.2f}% from peak.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Trailing Stop', 'color': '#ffd400', 'action_main': 'SELL', 'action_sub': '(Lock Profits)', 'action_color': '#ffd400', 'regime': regime, 'trade_type': trade_type, 'health_pct': health_pct, 'health_color': health_color, 'health_text': health_text}
            if pnl_pct <= hard_pct: 
                return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"HARD STOP LOSS TRIPPED ({pnl_pct:.2f}%).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Stop Loss', 'color': '#ff5364', 'action_main': 'SELL', 'action_sub': '(Stop Loss)', 'action_color': '#ff5364', 'regime': regime, 'trade_type': trade_type, 'health_pct': health_pct, 'health_color': health_color, 'health_text': health_text}
            return {'type': 'Intraday Momentum', 'score': 80, 'tranches': 1, 'discount': f"{pnl_pct:.2f}%", 'reason': f"RIDING TREND. High Water Mark: £{highest_price:.2f} (Stop: {trail_pct:.2f}%).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Trailing Stop Active', 'color': '#ffd400', 'action_main': 'HOLD / WAIT', 'action_sub': '(Riding Winner)', 'action_color': '#ffd400', 'regime': regime, 'trade_type': trade_type, 'health_pct': health_pct, 'health_color': health_color, 'health_text': health_text}

        # ENTRY LOGIC: Intraday EMA alignment (ignores obsolete 5-day drag)
        intraday_pct = ((current_price - df_5m['Close'].iloc[-12]) / df_5m['Close'].iloc[-12]) * 100.0 if len(df_5m) >= 12 else 0.5
        if ema9 > ema21 and current_price > ema9 and rsi < 68 and intraday_pct >= -1.50:
            vol_20ma = df_5m['Volume'].tail(20).mean() if len(df_5m) >= 20 else 1.0
            # Use iloc[-2] (completed candle) or max of last 2 to prevent incomplete-bar delay
            cur_vol = max(df_5m['Volume'].iloc[-1], df_5m['Volume'].iloc[-2]) if len(df_5m) >= 2 else df_5m['Volume'].iloc[-1]
            vol_ratio = (cur_vol / vol_20ma) if vol_20ma > 0 else 1.0

            # DYNAMIC ANTI-CHASE: 0.65% for high-beta 3x ETFs, 0.35% for single stocks
            ema_extension_pct = ((current_price - ema9) / ema9) * 100.0
            max_extension = 0.65 if is_3x_etf else 0.35

            if any(x in prof for x in ['test e6', 'test e17', 'test e22', 'test e23', 'test e24', 'test e25', 'test e26', 'test e27', 'test e28', 'test tq', 'test s2', 'test m']) and ema_extension_pct > max_extension:
                return {'type': 'Intraday Momentum', 'score': 45, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"Anti-Chase Gate: Price is {ema_extension_pct:.2f}% above 9-EMA (max {max_extension:.2f}%). Awaiting pullback.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Overextended', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Pullback)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': f"Overextended (+{ema_extension_pct:.2f}%)"}
            
            if 'test e12' in prof and rsi < 50:
                return {'type': 'Intraday Momentum', 'score': 40, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"E12 Momentum Filter: RSI ({rsi:.1f}) below 50. Entry blocked.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Low RSI Filter', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(RSI < 50)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

            # Relative volume threshold check
            if any(x in prof for x in ['test e6', 'test e7', 'test e17', 'test e22', 'test e23', 'test e24', 'test e25', 'test e26', 'test e27', 'test e28', 'test tq', 'test s2', 'test m']) and vol_ratio < 1.20:
                return {'type': 'Intraday Momentum', 'score': 45, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"EMA Surge set, but Volume ({vol_ratio:.1f}x) below 1.2x threshold.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Low Volume', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Vol)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

            # UNIVERSAL 14:30 - 14:50 BST OPENING MORATORIUM (Blocks opening whipsaws across all bots)
            if any(x in prof for x in ['vector', 'dual', 'velocity', 'tqm', 'tq', 'test s']):
                if now_uk.weekday() < 5 and now_uk.hour == 14 and 30 <= now_uk.minute < 50:
                    return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': 'Opening Moratorium: Waiting until 14:50 BST for opening range confirmation.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Opening Lock', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(Opening Lock)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'Opening Lock'}

            # TEST E17: 19:45 BST LATE-SESSION CURFEW
            if 'test e17' in prof and (now_uk.hour > 19 or (now_uk.hour == 19 and now_uk.minute >= 45)):
                return {'type': 'Curfew', 'score': 0, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': 'E17 Session Curfew: No new entries permitted after 19:45 BST.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Curfew Active', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(Curfew Active)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'Curfew Active'}

            # CAPITULATION DOWNSIDE SWITCH: Only allow SQQQ buys when sentiment <= 35
            reg_score = regime.get('score', 50) if isinstance(regime, dict) else 50
            if is_inverse and reg_score > 35:
                return {'type': 'Intraday Momentum', 'score': 20, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"SQQQ blocked: Macro sentiment ({reg_score}/100) not in capitulation freeze (≤35 required).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Regime Blocked', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Inverse Blocked)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

            # INDEX RUNNER BOOST: If market sentiment >= 60, grant broad index 3x ETFs a +25 priority bonus
            etf_priority_bonus = 25 if (is_3x_etf and ticker in ['TQQQ', 'SOXL'] and reg_score >= 60) else (25 if is_inverse and reg_score <= 25 else 0)
            buy_score = min(100, max(50, round(50 + pct_change_5d * 10 + (70 - rsi) + etf_priority_bonus)))
            return {'type': 'Intraday Momentum', 'score': buy_score, 'tranches': 1, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"SURGE DETECTED: Price > 9-EMA > 21-EMA, RSI {rsi:.1f}, Vol {vol_ratio:.1f}x{' [ETF PRIORITY]' if etf_priority_bonus else ''}.", 'is_smart': True, 'rec_buy': round(current_price*0.99, 2), 'rec_sell': round(current_price*1.02, 2), 'status': f"Fast {trade_type} Surge", 'color': '#0ecb81', 'action_main': 'BUY', 'action_sub': f"({trade_type} Surge)", 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

        return {'type': 'Intraday Momentum', 'score': 10, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"Awaiting fast EMA crossover surge.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'No Setup', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Setup)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

    def score_nav_asset(self, ticker, current_price, volume_ratio):
        nav = self.nav_bases.get(ticker, current_price * 1.10)
        implied_discount = ((nav - current_price) / nav) * 100.0
        buy_score = min(max(round(min(max((implied_discount/20.0)*80.0, 0), 80) + min(max((volume_ratio/2.0)*20.0, 0), 20), 2), 0), 100)
        tranches = 0 if implied_discount <= 0 else min(5, int(buy_score // 20) + 1)

        if implied_discount <= 5.0 and implied_discount > -50.0:
            action_main, action_sub, status, color = "SELL", "(Take Profit)", "Target Reached", "#ff5364"
            reason, action_color, tranches = f"Profit Target Triggered. NAV discount shrunk to {implied_discount:.1f}%.", "#ff5364", 0
        elif buy_score >= 40:
            action_main, action_sub = "BUY", f"(Tranche {tranches})"
            status, color = ('Deep Value Anomaly', '#0ecb81') if buy_score >= 60 else ('Moderate Value', '#ff9900')
            reason, action_color = f"Physical NAV Anomaly. Trading at {implied_discount:.1f}% discount to NAV ({nav}).", "#0ecb81"
        else:
            action_main, action_sub = "HOLD / WAIT", f"(Tranche {tranches})"
            status, color = ('Trading at Premium', '#ff5364') if implied_discount < 0 else ('Low Value', '#8a8a9e')
            reason, action_color = f"Trading at {implied_discount:.1f}% NAV discount. Active Tranches: {tranches}.", "#8a8a9e"

        return {'type': 'Physical Trust', 'score': buy_score, 'tranches': tranches, 'discount': f"{implied_discount:.2f}%" if implied_discount>0 else f"+{abs(implied_discount):.2f}%", 'reason': reason, 'is_smart': True, 'rec_buy': round(current_price*0.98, 2), 'rec_sell': round(nav*0.95, 2), 'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color, 'regime': {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e', 'sparkline': [], 'trend': '► Stable'}, 'trade_type': 'LONG', 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'N/A'}

    def score_equity(self, df, current_price):
        if df.empty or 'Close' not in df:
            return {'type': 'Global Equity', 'score': 0, 'tranches': 0, 'discount': '0.00%', 'reason': 'Awaiting data.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '', 'action_color': '#8a8a9e', 'regime': {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e', 'sparkline': [], 'trend': '► Stable'}, 'trade_type': 'LONG', 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'N/A'}
            
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
            status, color = ('Deep Value Anomaly', '#0ecb81') if buy_score >= 60 else ('Moderate Value', '#ff9900')
            reason, action_color = f"Value Anomaly. Trading at {implied_discount:.1f}% discount to 200d-DMA.", "#0ecb81"
        elif implied_discount <= -10.0:
            action_main, action_sub, status, color, tranches = "SELL", "(Take Profit)", 'Overextended (High)', '#ff5364', 0
            reason, action_color = f"Overextended ({abs(implied_discount):.1f}% above 200d-DMA). Take profits.", "#ff5364"
        else:
            action_main, action_sub, status, color = "HOLD / WAIT", f"(Tranche {tranches})", 'Fair Value', '#8a8a9e'
            reason, action_color = f"No Value Anomaly. Near 200d-DMA.", "#8a8a9e"
            
        return {'type': 'Global Equity', 'score': buy_score, 'tranches': tranches, 'discount': f"{implied_discount:.2f}%" if implied_discount>0 else f"+{abs(implied_discount):.2f}%", 'reason': reason, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color, 'regime': {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e', 'sparkline': [], 'trend': '► Stable'}, 'trade_type': 'LONG', 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'N/A'}

portfolio_store = PortfolioManager()

def process_auto_profile(prof_name):
    try:
        ud = portfolio_store.user_data(prof_name)
        engine = MarketScoringEngine()
        active_profile = prof_name.strip().lower()
        
        is_auto_profile = any(x in active_profile for x in ['vector', 'dual', 'velocity', 'pulse', 'hybrid', 'axis', 'test e', 'test s', 'test tq', 'test r', 'test tqm'])
        if not is_auto_profile: return

        # TEST B: PASSIVE BUY-AND-HOLD BENCHMARK
        if 'test b' in active_profile:
            sh_b = portfolio_store.get_shares('TQQQ', prof_name)
            mb_b = ud.get('master_budget', 5000.0)
            hist_b = ud.get('history') or []
            net_hist_b = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist_b)
            cash_b = mb_b + net_hist_b
            if sh_b == 0 and cash_b >= 4500:
                now_uk = pd.Timestamp.now(tz='Europe/London')
                cur_m = now_uk.hour * 60 + now_uk.minute
                if now_uk.weekday() < 5 and (14 * 60 + 30) <= cur_m < (21 * 60):
                    df_b = fetch_yf_data('TQQQ', '1d', '1m')
                    if not df_b.empty:
                        cur_p = df_b['Close'].iloc[-1]
                        b_shares = int((cash_b * 0.98) // cur_p)
                        if b_shares > 0:
                            portfolio_store.execute_trade('TQQQ', 'BUY', b_shares, cur_p, prof_name)
            return

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

        # DYNAMIC PERCENTAGE CIRCUIT BREAKER (SCALABLE TO ANY CAPITAL SIZE)
        now_uk = pd.Timestamp.now(tz='Europe/London')
        today_str = now_uk.strftime('%Y-%m-%d')
        trades_today = [tr for tr in hist if tr.get('date_str') == today_str]
        net_cash_today = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in trades_today)
        cash_start = cash_balance - net_cash_today

        # Calculate Start-Of-Day (SOD) equity
        sod_equity = max(50.0, cash_start)
        daily_pnl = total_equity - sod_equity
        daily_pnl_pct = (daily_pnl / sod_equity) * 100.0 if sod_equity > 0 else 0.0

        # Maximum allowable daily drawdown: -2.5% of starting daily equity
        CIRCUIT_BREAKER_PCT = -2.50
        circuit_breaker_tripped = (daily_pnl_pct <= CIRCUIT_BREAKER_PCT)

        # LATE-DAY ENTRY CURFEW (NO NEW BUYS AFTER 20:00 BST / 3:00 PM EST)
        is_curfew_active = (now_uk.hour >= 20) and any(x in active_profile for x in ['test e', 'test s', 'test u', 'test w', 'test x'])

        # TEST E17: BANKED PROFIT GUARD (+1.50% AFTER 18:00 BST) & 19:45 HARD CURFEW
        if 'test e17' in active_profile:
            cur_mins = now_uk.hour * 60 + now_uk.minute
            # 1. Banked Profit Guard: Up >= +1.50% after 18:00 BST -> Lock gains and stop buying
            if cur_mins >= (18 * 60) and daily_pnl_pct >= 1.50:
                is_curfew_active = True
            # 2. Core Curfew: Only stop at 19:45 BST (leaving 65 mins before 20:50 sweep)
            elif cur_mins >= (19 * 60 + 45):
                is_curfew_active = True

                # PHASE 2 BANK-AND-WALK HARVEST CURFEWS
        cur_mins = now_uk.hour * 60 + now_uk.minute
        # Vector Hybrid locks gains at +2.20% (+£11 on £500)
        if any(x in active_profile for x in ['vector', 'test tqm']) and daily_pnl_pct >= 2.20:
            is_curfew_active = True
        # Dual Axis locks gains at +2.20%
        elif any(x in active_profile for x in ['dual', 'test tq']) and daily_pnl_pct >= 2.20:
            is_curfew_active = True
        # Velocity Pulse locks at +3.00%
        elif any(x in active_profile for x in ['velocity', 'pulse', 'test s']) and daily_pnl_pct >= 3.00:
            is_curfew_active = True

        # ADAPTIVE MODE RESOLUTION (MOMENTUM VS REVERSION SWITCHBOARD)
        current_mode = 'momentum'
        # True loss strike counter: Only count trades closed at an actual loss (sell price < buy price)
        today_loss_stops = 0
        for i, h in enumerate(hist):
            if h.get('action') == 'SELL' and h.get('date_str') == today_str:
                matching_buy = next((b for b in hist[i+1:] if b.get('ticker') == h.get('ticker') and b.get('action') == 'BUY'), None)
                if matching_buy:
                    if float(h.get('price', 0)) < float(matching_buy.get('price', 0)):
                        today_loss_stops += 1
                else:
                    today_loss_stops += 1
        today_stops_cnt = today_loss_stops

        if 'test e25' in active_profile:
            reg_check = engine.check_market_regime()
            current_mode = 'momentum' if reg_check.get('score', 50) >= 60 else 'reversion'
        elif 'test tqm' in active_profile:
            # TQM SESSION SHIFTER: 14:30–15:15 BST = 1m Reversion Scalp; 15:15–20:00 BST = 5m Index Momentum
            current_mode = 'reversion' if (now_uk.hour == 14 or (now_uk.hour == 15 and now_uk.minute < 15)) else 'momentum'
        elif 'test r' in active_profile:
            current_mode = 'reversion'

        dirs, buys, held_scores = [], [], []
        MIN_BUY_VALUE = 20.0
        regime = engine.check_market_regime()

        dfs = {}
        def fetch_data_thread(tick): 
            use_1m = (current_mode == 'reversion') or any(x in active_profile for x in ['test p', 'test t'])
            interval = "1m" if use_1m else ("1d" if 'test a' in active_profile else "5m")
            period = "1d" if use_1m else ("1y" if 'test a' in active_profile else "5d")
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
                    if t_buys:
                        avg_buy_p = t_buys[0].get('price', 0.0)
                        buy_ts = t_buys[0].get('timestamp', 0)
                        if not df.empty and 'High' in df.columns:
                            try:
                                post_buy_df = df[df.index.astype('int64') // 10**9 >= (buy_ts - 300)]
                                candle_high = float(post_buy_df['High'].max()) if not post_buy_df.empty else cur
                            except Exception:
                                candle_high = cur
                        else:
                            candle_high = cur
                    else:
                        candle_high = cur

                    stored_hw = (ud.get('holdings', {}).get(t) or {}).get('high_water', cur)
                    highest_p = max(stored_hw, candle_high, cur)
                    if 'holdings' not in ud: ud['holdings'] = {}
                    if t not in ud['holdings']: ud['holdings'][t] = {}
                    if highest_p > stored_hw:
                        ud['holdings'][t]['high_water'] = highest_p
                        portfolio_store.save_data(portfolio_store.data)

                # Determine effective scoring profile based on active mode
                eff_profile = active_profile
                if any(x in active_profile for x in ['test e24', 'test e25', 'test e26', 'test m']):
                    eff_profile = 'test p' if current_mode == 'reversion' else ('test e23' if 'test m' in active_profile else active_profile)

                if 'test a' in active_profile:
                    avg_v = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
                    v_r = (df['Volume'].iloc[-1] / avg_v) if avg_v > 0 else 1.0
                    st = engine.score_nav_asset(t, cur, v_r) if t in engine.nav_bases else engine.score_equity(df, cur)
                else:
                    st = engine.score_momentum(df, cur, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=eff_profile, regime=regime)

                if sh > 0:
                    held_scores.append({'ticker': t, 'shares': sh, 'price': cur, 'cps': cps, 'value': vo, 'score': st['score'], 'action': st['action_main']})

                if st['action_main'] == 'SELL' and sh > 0 and vo >= MIN_BUY_VALUE:
                    if is_open or 'test a' in active_profile:
                        dirs.append({'ticker': t, 'action': 'SELL', 'shares': sh, 'price': cur, 'amount': round(vo, 2), 'status': st.get('status', ''), 'reason': st.get('reason', ''), 'highest_price': highest_p})
                elif st['action_main'] == 'BUY':
                    if is_open or 'test a' in active_profile:
                        buys.append({'t': t, 'cps': cps, 'p': cur, 's': st['score']})
            except Exception: pass

        max_allowed_holds = 2 if any(x in active_profile for x in ['test e27', 'test e28']) else (5 if 'test a' in active_profile else (1 if any(x in active_profile for x in ['test e', 'test s', 'test u', 'test w', 'test x', 'test tq', 'test s2', 'test m']) else 3))
        
        swap_hurdle = 5 if 'test e3' in active_profile else 10
        
        if buys and held_scores and len(held_scores) >= max_allowed_holds:
            top_candidate = max(buys, key=lambda x: x['s'])
            weakest_holding = min(held_scores, key=lambda x: x['score'])
            if top_candidate['s'] >= (weakest_holding['score'] + swap_hurdle) and top_candidate['s'] >= 65:
                dirs.append({'ticker': weakest_holding['ticker'], 'action': 'SELL', 'shares': weakest_holding['shares'], 'price': weakest_holding['price'], 'amount': round(weakest_holding['value'], 2)})

        current_hold_count = len([x for x in held_scores if x['shares'] > 0])
        slots_available = max(0, max_allowed_holds - current_hold_count)

        # DEFENSIVE CHOP SHIELDS & COOLDOWNS
        if any(x in active_profile for x in ['test e23', 'test e24', 'test e25', 'test e26', 'test e27', 'test e28', 'test tq', 'test s2', 'test m']):
            cur_mins = now_uk.hour * 60 + now_uk.minute
            
            # 1. 19:45 BST Late Curfew
            if cur_mins >= (19 * 60 + 45):
                is_curfew_active = True

            # 2. Three-Strike Loss Lockout (Enforced on E23, E28, and TQ)
            if any(x in active_profile for x in ['test e23', 'test e28', 'test tq']) and today_stops_cnt >= 3:
                is_curfew_active = True

            # 3. 15-Minute Post-Loss Cooldown
            if hist and hist[0].get('action') == 'SELL':
                if (time.time() - hist[0].get('timestamp', 0)) < 900:
                    buys = []

            # 4. 60-Minute Ticker Wash Rule
            recent_stopped_tickers = {
                h.get('ticker') for h in hist
                if h.get('action') == 'SELL' and (time.time() - h.get('timestamp', 0)) < 3600
            }
            buys = [b for b in buys if b.get('t') not in recent_stopped_tickers]

        # Emergency Shutdown: If circuit breaker tripped or curfew active, block ALL buys
        if circuit_breaker_tripped or is_curfew_active:
            buys = []

        # If circuit breaker tripped while holding, liquidate to preserve cash
        if circuit_breaker_tripped and held_scores:
            for h in held_scores:
                if not any(d['ticker'] == h['ticker'] and d['action'] == 'SELL' for d in dirs):
                    dirs.append({'ticker': h['ticker'], 'action': 'SELL', 'shares': h['shares'], 'price': h['price'], 'amount': round(h['value'], 2)})

        if buys and rem_cash >= MIN_BUY_VALUE and slots_available > 0:
            buys.sort(key=lambda x: x['s'], reverse=True)
            # Filter out any ticker currently under 15-minute cooldown
            valid_buys = [b for b in buys if time.time() >= ud.get('cooldowns', {}).get(b['t'], 0)]
            top_buys = valid_buys[:slots_available]
            if top_buys:
                if 'test e28' in active_profile:
                    # CONVICTION SIZING ENGINE (TEST E28):
                    # If top candidate is TQQQ/SOXL, market sentiment >= 65, score >= 85, and account is 100% cash -> 98% ALL-IN
                    top_b = top_buys[0]
                    reg_score = regime.get('score', 50) if isinstance(regime, dict) else 50
                    is_conviction_all_in = (top_b['t'] in ['TQQQ', 'SOXL'] and reg_score >= 65 and top_b['s'] >= 85 and current_hold_count == 0)
                    
                    if is_conviction_all_in:
                        per_stock_budget = min(rem_cash, total_equity * 0.98)
                        top_buys = [top_b]
                    else:
                        # Standard 2-Slot sizing: 49% allocation per stock
                        per_stock_budget = min(rem_cash, (total_equity * 0.49))
                else:
                    live_allocation_pct = 0.98 if any(x in active_profile for x in ['test e', 'test s', 'test tq', 'test r', 'test tqm']) else 0.33
                    compound_capital = min(rem_cash, total_equity * live_allocation_pct)
                    per_stock_budget = compound_capital / len(top_buys)

                for b in top_buys:
                    bs = int(per_stock_budget // b['cps'])
                    amt = round(bs * b['cps'], 2)
                    if bs > 0 and amt >= MIN_BUY_VALUE and amt <= rem_cash:
                        dirs.append({'ticker': b['t'], 'action': 'BUY', 'shares': bs, 'price': b['p'], 'amount': amt})

        for d in dirs:
            if d['shares'] > 0:
                portfolio_store.execute_trade(d['ticker'], d['action'], d['shares'], d['price'], prof_name, metadata={'status': d.get('status', 'Manual/EOD'), 'reason': d.get('reason', ''), 'highest_price': d.get('highest_price', 0.0)})
                # Stamp 15-minute cooldown on stop-outs to avoid whipsaw chop
                if d['action'] == 'SELL':
                    if 'cooldowns' not in ud or not isinstance(ud['cooldowns'], dict):
                        ud['cooldowns'] = {}
                    ud['cooldowns'][d['ticker']] = int(time.time()) + 900
                    portfolio_store.save_data(portfolio_store.data)
    except Exception: pass

def global_background_worker():
    time.sleep(3)
    while True:
        try:
            # Prefetch only core index ETFs
            bot_tickers = ['TQQQ', 'SOXL', 'SQQQ', 'QQQ']
            def prefetch(tk): 
                fetch_yf_data(tk, "5d", "5m")
                fetch_yf_data(tk, "1d", "1m")
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(prefetch, set(bot_tickers)))

            portfolio_store.reload()
            # Scan active profiles dynamically
            for prof_name in list(portfolio_store.data.get('users', {}).keys()):
                p_lower = prof_name.lower()
                if any(k in p_lower for k in ['vector', 'dual', 'velocity', 'tqm', 'tq', 'test s']):
                    process_auto_profile(prof_name)
        except Exception as e: 
            print(f"Background Loop Error: {e}")
        time.sleep(20) # 20-second high-speed cycle

threading.Thread(target=global_background_worker, daemon=True).start()

def match_bot_from_order_id(client_order_id, profiles):
    """Matches Alpaca client_order_id back to the specific bot profile or custom version."""
    if not client_order_id:
        return None
    coid_clean = client_order_id.lower().replace('-', '_').replace(' ', '_')
    sorted_profs = sorted(profiles, key=lambda p: len(p.lower().replace(' ', '_')), reverse=True)
    for p in sorted_profs:
        p_clean = p.lower().replace(' ', '_').replace('(', '').replace(')', '')
        if p_clean in coid_clean:
            return p
    # Prefix fallbacks
    if 'vector' in coid_clean: return 'Vector Hybrid'
    if 'dual' in coid_clean: return 'Dual Axis'
    if 'velocity' in coid_clean or 'pulse' in coid_clean: return 'Velocity Pulse'
    return None

def reconcile_alpaca_positions_and_orders():
    """Two-way sync loop: reconciles Alpaca fills, trailing stops, and positions with local state."""
    portfolio_store.reload()
    broker = portfolio_store.data.get('broker') or {}
    api_key = str(broker.get('api_key', '')).strip()
    secret_key = str(broker.get('secret_key', '')).strip()
    mode = broker.get('mode', 'paper')
    
    if not api_key or not secret_key:
        return

    base_url = "https://paper-api.alpaca.markets" if mode == 'paper' else "https://api.alpaca.markets"
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "Content-Type": "application/json"
    }

    auto_profiles = [
        'Test TQM - Pure Index Hybrid', 
        'Test TQ - BiDirectional Specialist',
        'Test E25 - Chameleon Pro', 
        'Test S - Index Scalper', 
        'Test R - Reversion Lab'
    ]

    # 1. Reconcile Closed Orders (Native Trailing Stops & Exact Fill Prices)
    try:
        req_orders = urllib.request.Request(f"{base_url}/v2/orders?status=closed&limit=30&direction=desc", headers=headers)
        with urllib.request.urlopen(req_orders, timeout=5) as resp:
            closed_orders = json.loads(resp.read().decode('utf-8'))
            
            for o in closed_orders:
                if o.get('status') != 'filled':
                    continue
                coid = o.get('client_order_id') or ''
                side = (o.get('side') or '').lower()
                symbol = (o.get('symbol') or '').upper()
                filled_qty = int(o.get('filled_qty') or 0)
                filled_price = float(o.get('filled_avg_price') or o.get('price') or 0.0)
                order_id = o.get('id')
                
                if not coid or filled_qty <= 0 or filled_price <= 0:
                    continue
                    
                matched_prof = match_bot_from_order_id(coid, auto_profiles)
                if not matched_prof:
                    continue
                    
                ud = portfolio_store.user_data(matched_prof)
                hist = ud.get('history') or []
                
                # Check for Trailing Stop SELLs executed on Alpaca
                if side == 'sell':
                    current_shares = portfolio_store.get_shares(symbol, matched_prof)
                    already_logged = any(h.get('alpaca_order_id') == order_id for h in hist)
                    if current_shares > 0 and not already_logged:
                        print(f"[RECONCILE SYNC] Detected Alpaca filled SELL for {matched_prof}: {filled_qty} {symbol} @ £{filled_price:.2f}")
                        portfolio_store.execute_trade(
                            ticker=symbol,
                            action_type='SELL',
                            shares=min(current_shares, filled_qty),
                            price=filled_price,
                            username=matched_prof,
                            metadata={
                                'status': 'Alpaca Stop Filled',
                                'reason': f"Native {o.get('order_type', 'stop').title()} Executed on Alpaca",
                                'alpaca_order_id': order_id,
                                'source': 'alpaca_sync'
                            }
                        )
                # Reconcile BUY fill prices to exact Alpaca execution price
                elif side == 'buy':
                    for h in hist:
                        if h.get('action') == 'BUY' and h.get('ticker') == symbol and not h.get('price_reconciled'):
                            if abs(h.get('timestamp', 0) - int(time.time())) < 900:
                                old_p = h.get('price', filled_price)
                                if abs(old_p - filled_price) > 0.005:
                                    h['price'] = round(filled_price, 2)
                                    cps = filled_price / 100.0 if symbol.endswith('.L') else filled_price
                                    h['amount'] = round(h['shares'] * cps, 2)
                                    h['price_reconciled'] = True
                                    h['alpaca_order_id'] = order_id
                                    portfolio_store.save_data(portfolio_store.data)
                                    print(f"[RECONCILE BUY PRICE] Updated {matched_prof} {symbol} to exact fill: £{filled_price:.2f}")
                            break
    except Exception as e:
        pass

    # 2. True Two-Way Reconciliation & Overnight Orphan Sweep
    try:
        req_pos = urllib.request.Request(f"{base_url}/v2/positions", headers=headers)
        with urllib.request.urlopen(req_pos, timeout=5) as resp:
            live_pos = json.loads(resp.read().decode('utf-8'))
            alpaca_holdings = {p['symbol'].upper(): int(p.get('qty', 0)) for p in live_pos}
            allocations = broker.get('allocations') or {}
            
            # Step 2A: Clear local positions if Alpaca already sold them
            for prof in auto_profiles:
                is_enabled = any(s_key in prof.lower() and s_data.get('enabled') for s_key, s_data in allocations.items())
                if not is_enabled:
                    continue
                    
                ud = portfolio_store.user_data(prof)
                hist = ud.get('history') or []
                active_tickers = list(set([tr.get('ticker') for tr in hist if portfolio_store.get_shares(tr.get('ticker'), prof) > 0]))
                
                for tk in active_tickers:
                    if alpaca_holdings.get(tk, 0) == 0:
                        local_sh = portfolio_store.get_shares(tk, prof)
                        if local_sh > 0:
                            df_p = fetch_yf_data(tk, "1d", "1m")
                            cur_p = df_p['Close'].iloc[-1] if not df_p.empty else 1.0
                            print(f"[RECONCILE SYNC] Clearing orphan local position for {prof}: {local_sh} {tk} (Alpaca balance = 0)")
                            portfolio_store.execute_trade(
                                ticker=tk,
                                action_type='SELL',
                                shares=local_sh,
                                price=cur_p,
                                username=prof,
                                metadata={
                                    'status': 'Alpaca Reconciled Flat',
                                    'reason': 'Alpaca confirmed 0 shares',
                                    'source': 'alpaca_sync'
                                }
                            )

            # Step 2B: Check the REVERSE — Alpaca holds shares that NO bot claims or after 20:50 sweep
            now_uk = pd.Timestamp.now(tz='Europe/London')
            is_post_sweep = (now_uk.hour == 20 and now_uk.minute >= 50) or (now_uk.hour >= 21) or (now_uk.hour < 14)
            
            for p in live_pos:
                tk = p['symbol'].upper()
                qty = int(p.get('qty', 0))
                if qty <= 0: continue

                total_local_shares_claimed = sum(portfolio_store.get_shares(tk, prof) for prof in auto_profiles)

                # If after 20:50 sweep OR no bot owns these shares, auto-liquidate on Alpaca
                if is_post_sweep or total_local_shares_claimed == 0:
                    print(f"[ORPHAN SWEEP] Liquidating unowned/overnight position on Alpaca: {qty} {tk} (Local claimed: {total_local_shares_claimed})")
                    try:
                        req_del = urllib.request.Request(f"{base_url}/v2/positions/{tk}", headers=headers, method="DELETE")
                        with urllib.request.urlopen(req_del, timeout=5):
                            print(f"[ORPHAN SWEEP SUCCESS] Flattened {tk} on Alpaca.")
                    except Exception as e_del:
                        print(f"[ORPHAN SWEEP ERROR]: {e_del}")
    except Exception as e:
        pass

def alpaca_reconciliation_worker():
    time.sleep(5)
    while True:
        try:
            reconcile_alpaca_positions_and_orders()
        except Exception as e:
            print(f"[RECONCILIATION WORKER ERROR]: {e}")
        time.sleep(25)

threading.Thread(target=alpaca_reconciliation_worker, daemon=True).start()

SW_JAVASCRIPT = """
self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(clients.claim()); });
self.addEventListener('push', (e) => {
    let d = { title: 'IDC Trading Alert', body: 'New trade executed.', url: '/' };
    if (e.data) {
        try { d = e.data.json(); } catch(err) { d.body = e.data.text(); }
    }
    const opts = {
        body: d.body,
        icon: '/static/apple-touch-icon.png',
        badge: '/static/apple-touch-icon.png',
        data: { url: d.url || '/' },
        tag: 'idc-alert-' + Date.now(),
        renotify: true
    };
    e.waitUntil(self.registration.showNotification(d.title, opts));
});
self.addEventListener('notificationclick', (e) => {
    e.notification.close();
    e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then((cl) => {
        for (let c of cl) {
            if (c.url.includes(self.location.origin) && 'focus' in c) return c.focus();
        }
        if (clients.openWindow) return clients.openWindow(e.notification.data.url || '/');
    }));
});
"""

@app.route('/sw.js')
def service_worker_file():
    from flask import Response
    resp = Response(SW_JAVASCRIPT.strip(), mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp

@app.route('/manifest.json')
def manifest_file():
    return app.send_static_file('manifest.json')

@app.route('/api/push/vapid_public_key', methods=['GET'])
def get_vapid_public_key():
    _, pub_key = get_vapid_keypair()
    return jsonify({'public_key': pub_key})

@app.route('/api/push/subscribe', methods=['POST'])
def save_push_subscription():
    sub = request.get_json() or {}
    if not sub or not sub.get('endpoint'):
        return jsonify({'status': 'error', 'message': 'Invalid subscription'}), 400
    
    portfolio_store.reload()
    subs = portfolio_store.data.setdefault('push_subscriptions', [])
    if not any(s.get('endpoint') == sub.get('endpoint') for s in subs):
        subs.append(sub)
        portfolio_store.save_data(portfolio_store.data)
    return jsonify({'status': 'ok'})

@app.route('/api/push/unsubscribe', methods=['POST'])
def remove_push_subscription():
    sub = request.get_json() or {}
    endpoint = sub.get('endpoint')
    portfolio_store.reload()
    subs = portfolio_store.data.get('push_subscriptions') or []
    if endpoint:
        portfolio_store.data['push_subscriptions'] = [s for s in subs if s.get('endpoint') != endpoint]
        portfolio_store.save_data(portfolio_store.data)
    return jsonify({'status': 'ok'})

@app.route('/api/push/test', methods=['POST'])
def test_push_notification():
    res = send_native_web_push(
        title="Test R - Sell 10 - SQQQ",
        message="£47.33 (Total £473.35)\n▲ +£10.22 (+2.3%)"
    )
    return jsonify(res or {'status': 'ok'})

@app.route('/api/broker/test_order', methods=['POST'])
@login_required
def test_alpaca_order():
    portfolio_store.reload()
    broker = portfolio_store.data.get('broker') or {}
    api_k = str(broker.get('api_key', '')).strip()
    sec_k = str(broker.get('secret_key', '')).strip()
    b_mode = broker.get('mode', 'paper')

    if not api_k or not sec_k:
        return jsonify({'status': 'error', 'message': 'Missing Alpaca API credentials. Enter keys and click Save & Launch Engine first.'}), 400

    res = send_alpaca_order(api_k, sec_k, b_mode, 'TQQQ', 'buy', 1, bot_name="TEST_ORDER")
    if isinstance(res, dict) and res.get('id'):
        return jsonify({'status': 'ok', 'message': f"Alpaca Order Placed! ID: {res.get('id')}", 'data': res})
    return jsonify({'status': 'error', 'message': f"Order failed: {res.get('message') if isinstance(res, dict) else res}"}), 400

@app.route('/health')
def health_check():
    return "OK", 200

@app.route('/login')
def login_page():
    if 'user' in session:
        return redirect('/')
    return render_template('login.html')

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    identifier = data.get('username', '').strip().lower()
    password = data.get('password', '').strip()

    # 1. Master Administrator Check (Ian)
    if identifier in [ADMIN_USER.lower(), ADMIN_ALIAS.lower()]:
        if check_password_hash(ADMIN_HASH, password) or password == ADMIN_PW:
            session['user'] = 'Ian'
            session['email'] = ADMIN_USER
            session['role'] = 'admin'
            return jsonify({'status': 'ok', 'role': 'admin'})
        return jsonify({'status': 'error', 'message': 'Invalid administrator password.'}), 401

    # 2. Client / Family Account Check
    if portfolio_store.client:
        accounts_col = portfolio_store.client['stock_terminal']['accounts']
        doc = accounts_col.find_one({"username": identifier})
        if doc and check_password_hash(doc.get('password_hash', ''), password):
            session['user'] = doc['username']
            session['email'] = doc['username']
            session['role'] = 'client'
            return jsonify({'status': 'ok', 'role': 'client'})

    return jsonify({'status': 'error', 'message': 'Invalid username or password.'}), 401

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.get_json() or {}
    username = data.get('username', '').strip().lower()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({'status': 'error', 'message': 'Username and password required.'}), 400

    if username in [ADMIN_USER.lower(), ADMIN_ALIAS.lower()]:
        return jsonify({'status': 'error', 'message': 'Username reserved.'}), 400

    if not portfolio_store.client:
        return jsonify({'status': 'error', 'message': 'Database offline.'}), 500

    accounts_col = portfolio_store.client['stock_terminal']['accounts']
    if accounts_col.find_one({"username": username}):
        return jsonify({'status': 'error', 'message': 'Username already taken.'}), 400

    now_uk_str = pd.Timestamp.now(tz='Europe/London').strftime('%Y-%m-%d %H:%M:%S')

    # 1. Append directly to user_signups.txt in project folder
    try:
        with open('user_signups.txt', 'a') as f:
            f.write(f"[{now_uk_str}] Username: {username} | Password: {password}\n")
    except Exception as e:
        print(f"File log error: {e}")

    # 2. Print to Render dashboard live console
    print(f"[NEW USER REGISTRATION] [{now_uk_str}] Username: {username} | Password: {password}")

    # 3. Store in MongoDB with plain_password for permanent backup across redeploys
    blank_state = portfolio_store.blank_client_state(username)
    accounts_col.insert_one({
        "_id": f"client_{username}",
        "username": username,
        "password_hash": generate_password_hash(password),
        "plain_password": password,
        "role": "client",
        "portfolio": blank_state,
        "registered_at": now_uk_str,
        "created_at": time.time()
    })

    session['user'] = username
    session['email'] = username
    session['role'] = 'client'
    return jsonify({'status': 'ok', 'role': 'client'})

@app.route('/admin/users_log')
@login_required
def view_admin_users_log():
    """Admin-only portal to view all registered usernames and passwords."""
    if session.get('role') != 'admin':
        return "Unauthorized", 403
    
    entries = []
    # Check MongoDB database
    if portfolio_store.client:
        try:
            accounts_col = portfolio_store.client['stock_terminal']['accounts']
            for doc in accounts_col.find():
                u = doc.get('username')
                pw = doc.get('plain_password') or "(Pre-update hash)"
                dt = doc.get('registered_at') or "Prior Session"
                entries.append(f"[{dt}] Username: {u:<16} | Password: {pw}")
        except Exception: pass

    # Check local text file
    if os.path.exists('user_signups.txt'):
        try:
            with open('user_signups.txt', 'r') as f:
                entries.append("\n--- Local Text File Log ---")
                entries.extend([l.strip() for l in f.readlines() if l.strip()])
        except Exception: pass

    if not entries:
        return "<pre style='font-family:monospace; padding:20px;'>No user signups recorded yet.</pre>"

    return f"<pre style='font-family:monospace; background:#0f1115; color:#0ecb81; padding:24px; font-size:13px; line-height:1.6;'>IDC TRADING - USER ACCESS DIRECTORY\n======================================================\n" + "\n".join(entries) + "</pre>"

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/')
@login_required
def index():
    return render_template('index.html', user_role=session.get('role', 'client'), current_user=session.get('user', 'Ian'))

@app.route('/api/client/settings', methods=['POST'])
@login_required
def api_client_settings():
    portfolio_store.reload()
    data = request.get_json() or {}
    
    b_mode = data.get('broker_mode', 'paper')
    api_key = data.get('api_key', '').strip()
    secret_key = data.get('secret_key', '').strip()
    allocations = data.get('allocations') or {}
    
    # Save into the global broker store so Test TQM, Test S, etc. can all execute
    broker = portfolio_store.data.setdefault('broker', {})
    broker['mode'] = b_mode
    if api_key: broker['api_key'] = api_key
    if secret_key: broker['secret_key'] = secret_key
    broker['allocations'] = allocations
    
    # Dynamically update the capital for each enabled bot
    for prof, pdata in portfolio_store.data.get('users', {}).items():
        for s_key, s_cfg in allocations.items():
            if s_key in prof.lower():
                new_budget = float(s_cfg.get('budget', 500.0))
                pdata['master_budget'] = new_budget

    portfolio_store.save_data(portfolio_store.data)
    return jsonify({'status': 'ok', 'message': 'Broker configuration and bot allocations updated.'})

@app.route('/api/users', methods=['GET'])
@login_required
def get_users(): 
    if session.get('role') == 'admin':
        portfolio_store.reload()
        return jsonify({'users': list(portfolio_store.data.get('users', {}).keys()), 'active_user': portfolio_store.active_username()})
    u = session.get('user', 'Client')
    return jsonify({'users': [u], 'active_user': u})

@app.route('/api/users/select', methods=['POST'])
@login_required
def select_user():
    if session.get('role') == 'admin':
        portfolio_store.reload()
        portfolio_store.switch_user((request.get_json() or {}).get('username', ''))
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'ok'})

@app.route('/api/users/add', methods=['POST'])
@login_required
def add_user():
    if session.get('role') == 'admin':
        portfolio_store.reload()
        portfolio_store.add_user((request.get_json() or {}).get('username', ''))
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error'})

@app.route('/api/users/add_v2', methods=['POST'])
@login_required
def add_user_v2():
    data = request.get_json() or {}
    prof_name = data.get('profile_name', '').strip()
    method = data.get('method', 'Vector Hybrid').strip()
    capital = float(data.get('capital', 500.0))

    if not prof_name:
        return jsonify({'status': 'error', 'message': 'Profile name required'}), 400

    portfolio_store.reload()
    users = portfolio_store.data.setdefault('users', {})
    
    if prof_name in users:
        return jsonify({'status': 'error', 'message': 'A profile with this name already exists'}), 400

    state = portfolio_store.default_user_state(method)
    state['master_budget'] = capital
    state['method'] = method
    users[prof_name] = state
    portfolio_store.data['active_user'] = prof_name
    portfolio_store.save_data(portfolio_store.data)

    return jsonify({'status': 'ok', 'profile': prof_name})

@app.route('/api/portfolio/add_capital', methods=['POST'])
@login_required
def add_capital_route():
    data = request.get_json() or {}
    username = data.get('username') or portfolio_store.active_username()
    amount = float(data.get('amount', 0.0))

    if amount <= 0:
        return jsonify({'status': 'error', 'message': 'Invalid deposit amount'}), 400

    portfolio_store.reload()
    ud = portfolio_store.user_data(username)
    
    ud['master_budget'] = float(ud.get('master_budget', 500.0)) + amount
    
    now_lon = pd.Timestamp.now(tz='Europe/London')
    ud.setdefault('history', []).insert(0, {
        'id': str(int(time.time() * 1000)),
        'ticker': 'CAPITAL_INJECTION',
        'trade_type': 'DEPOSIT',
        'action': 'BUY',
        'shares': 0,
        'price': amount,
        'amount': amount,
        'time': now_lon.strftime('%d %b %H:%M'),
        'date_str': now_lon.strftime('%Y-%m-%d'),
        'timestamp': int(now_lon.timestamp())
    })
    
    portfolio_store.save_data(portfolio_store.data)
    return jsonify({'status': 'ok', 'new_budget': ud['master_budget']})

@app.route('/api/users/delete', methods=['POST'])
@login_required
def delete_user():
    if session.get('role') == 'admin':
        portfolio_store.reload()
        return jsonify({'status': 'ok' if portfolio_store.delete_user((request.get_json() or {}).get('username', '')) else 'error'})
    return jsonify({'status': 'error'})

@app.route('/api/portfolio', methods=['GET'])
@login_required
def get_portfolio():
    if session.get('role') == 'admin':
        portfolio_store.reload()
        return jsonify(portfolio_store.user_data())
    return jsonify(portfolio_store.get_client_store(session.get('user')))

@app.route('/api/portfolio/reset', methods=['POST'])
def reset_portfolio():
    portfolio_store.reload()
    req_data = request.get_json(silent=True) or {}
    prof = req_data.get('username') or portfolio_store.active_username()
    
    if not req_data or 'total_equity' not in req_data:
        ud = portfolio_store.user_data(prof)
        wl = ud.get('watchlist', [])
        st = ud.get('settings', {})
        portfolio_store.data['users'][prof] = portfolio_store.default_user_state(prof)
        if wl: portfolio_store.data['users'][prof]['watchlist'] = wl
        if st: portfolio_store.data['users'][prof]['settings'] = st
        portfolio_store.save_data(portfolio_store.data)
        return jsonify({'status': 'ok', 'portfolio': portfolio_store.user_data(prof)})
        
    try:
        target_equity = float(req_data.get('total_equity', 500.0))
        today_pnl = float(req_data.get('today_pnl', 0.0))
        pos_type = req_data.get('position_type', 'cash').lower()
        ticker = req_data.get('ticker', '').strip().upper()
        shares = int(req_data.get('shares', 0))
        buy_price = float(req_data.get('buy_price', 0.0))
        
        ud = portfolio_store.user_data(prof)
        wl = ud.get('watchlist', [])
        st = ud.get('settings', {})
        portfolio_store.data['users'][prof] = portfolio_store.default_user_state(prof)
        ud = portfolio_store.data['users'][prof]
        if wl: ud['watchlist'] = wl
        if st: ud['settings'] = st
        
        mb = float(ud.get('master_budget', 5000.0))
        now_lon = pd.Timestamp.now(tz='Europe/London')
        today_str = now_lon.strftime('%Y-%m-%d')
        yesterday_str = (now_lon - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
        
        hist = []
        holdings = {}
        sod_equity = round(target_equity - today_pnl, 2)
        prior_pnl = round(sod_equity - mb, 2)
        
        # 1. Prior days P&L entry (preserves All-Time balance)
        if prior_pnl != 0:
            act = 'SELL' if prior_pnl > 0 else 'BUY'
            amt = abs(prior_pnl)
            hist.append({
                'id': str(int(time.time() * 1000) - 86400000),
                'ticker': 'CALIBRATION',
                'trade_type': 'LONG',
                'action': act,
                'shares': 0,
                'price': amt,
                'amount': amt,
                'time': 'Prior Session Close',
                'date_str': yesterday_str,
                'timestamp': int(now_lon.timestamp()) - 86400
            })
            
        # 2. Position or Cash state for today
        if pos_type == 'cash' or not ticker or shares <= 0:
            if today_pnl != 0:
                act = 'SELL' if today_pnl > 0 else 'BUY'
                amt = abs(today_pnl)
                hist.append({
                    'id': str(int(time.time() * 1000)),
                    'ticker': 'CALIBRATION',
                    'trade_type': 'LONG',
                    'action': act,
                    'shares': 0,
                    'price': amt,
                    'amount': amt,
                    'time': now_lon.strftime('%d %b %H:%M'),
                    'date_str': today_str,
                    'timestamp': int(now_lon.timestamp())
                })
        else:
            cps = buy_price / 100.0 if ticker.endswith('.L') and buy_price > 100 else buy_price
            buy_amt = round(shares * cps, 2)
            
            # Fetch current price to value open position
            df_cur = fetch_yf_data(ticker, "1d", "1m")
            cur_p = df_cur['Close'].iloc[-1] if not df_cur.empty else buy_price
            cur_cps = cur_p / 100.0 if ticker.endswith('.L') and cur_p > 100 else cur_p
            open_val = round(shares * cur_cps, 2)
            
            today_cash_adj = round(today_pnl - (open_val - buy_amt), 2)
            if today_cash_adj != 0:
                act = 'SELL' if today_cash_adj > 0 else 'BUY'
                amt = abs(today_cash_adj)
                hist.append({
                    'id': str(int(time.time() * 1000) - 100),
                    'ticker': 'CALIBRATION',
                    'trade_type': 'LONG',
                    'action': act,
                    'shares': 0,
                    'price': amt,
                    'amount': amt,
                    'time': now_lon.strftime('%d %b %H:%M'),
                    'date_str': today_str,
                    'timestamp': int(now_lon.timestamp()) - 1
                })
                
            trade_type = 'SHORT' if ticker in ['SQQQ', '3SUS.L'] else 'LONG'
            hist.append({
                'id': str(int(time.time() * 1000)),
                'ticker': ticker,
                'trade_type': trade_type,
                'action': 'BUY',
                'shares': shares,
                'price': buy_price,
                'amount': buy_amt,
                'time': now_lon.strftime('%d %b %H:%M'),
                'date_str': today_str,
                'timestamp': int(now_lon.timestamp())
            })
            holdings[ticker] = {
                'shares': shares,
                'manual_val': open_val,
                'high_water': max(buy_price, cur_p)
            }
            
        ud['history'] = hist
        ud['holdings'] = holdings
        portfolio_store.save_data(portfolio_store.data)
        return jsonify({'status': 'ok', 'portfolio': ud})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/portfolio/reset_all_profiles', methods=['POST'])
def reset_all_profiles():
    portfolio_store.reload()
    portfolio_store.reset_all_profiles_to_5000()
    return jsonify({'status': 'ok'})

@app.route('/api/drawings', methods=['GET'])
def get_drawings_route():
    ticker = request.args.get('ticker', '').strip().upper()
    ud = portfolio_store.user_data()
    drawings = ud.get('drawings', {}).get(ticker, []) if isinstance(ud.get('drawings'), dict) else []
    return jsonify({'status': 'ok', 'ticker': ticker, 'drawings': drawings})

@app.route('/api/drawings/save', methods=['POST'])
def save_drawings_route():
    b = request.get_json() or {}
    ticker = b.get('ticker', '').strip().upper()
    drawings = b.get('drawings', [])
    ud = portfolio_store.user_data()
    if 'drawings' not in ud or not isinstance(ud['drawings'], dict):
        ud['drawings'] = {}
    ud['drawings'][ticker] = drawings
    portfolio_store.save_data(portfolio_store.data)
    return jsonify({'status': 'ok'})

@app.route('/api/drawings/clear', methods=['POST'])
def clear_drawings_route():
    b = request.get_json() or {}
    ticker = b.get('ticker', '').strip().upper()
    ud = portfolio_store.user_data()
    if 'drawings' in ud and isinstance(ud['drawings'], dict) and ticker in ud['drawings']:
        ud['drawings'][ticker] = []
        portfolio_store.save_data(portfolio_store.data)
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
        is_momentum = any(x in active_profile for x in ['test e', 'test s', 'test tq', 'test r', 'test tqm'])
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
        is_momentum = any(x in active_profile for x in ['test e', 'test s', 'test tq', 'test r', 'test tqm'])
        
        dfs = {}
        def fetch_t(tick): 
            interval = "1m" if any(x in active_profile for x in ['test p', 'test t']) else "5m"
            period = "1d" if any(x in active_profile for x in ['test p', 'test t']) else "5d"
            return tick, fetch_yf_data(tick, period if is_momentum else "1y", interval if is_momentum else "1d")
            
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
                st = engine.score_nav_asset(t, cur, v_rat) if t in engine.nav_bases else engine.score_equity(df, cur)
            
            tot_own = portfolio_store.get_total_portfolio_value(prof_name)
            total_eq = cash_balance + tot_own
            
            if st['action_main'] == 'BUY' and sh == 0 and rem_cash >= 20:
                if not is_momentum or is_open:
                    allocation = 0.98 if any(x in active_profile for x in ['test e', 'test s', 'test u', 'test w', 'test x']) else (0.33 if is_momentum else 0.20)
                    bs = int(min(rem_cash, total_eq * allocation) // cps) 
                    if bs > 0: dirs.append({'ticker': t, 'action': 'BUY', 'shares': bs, 'price': cur, 'amount': bs * cps, 'score': st['score']})
            elif st['action_main'] == 'SELL' and sh > 0:
                if not is_momentum or is_open:
                    dirs.append({'ticker': t, 'action': 'SELL', 'shares': sh, 'price': cur, 'amount': sh * cps, 'score': st['score']})

    dirs.sort(key=lambda x: (0 if x['action'] == 'SELL' else 1, -x.get('score', 0)))
    return jsonify({'directives': dirs, 'ian_directives': []})

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
@login_required
def get_data():
    try:
        is_client = (session.get('role') != 'admin')
        client_name = session.get('user', 'Client')
        req_user = request.args.get('user', '').strip()

        if is_client:
            ud = portfolio_store.get_client_store(client_name)
        else:
            portfolio_store.reload()
            if req_user and req_user in portfolio_store.data.get('users', {}):
                portfolio_store.data['active_user'] = req_user
            ud = portfolio_store.user_data(req_user if req_user else None)

        wl = ud.get('watchlist') or []
        t = request.args.get('t', '').upper().strip()
        active_profile = (req_user or portfolio_store.active_username()).strip().lower() if not is_client else client_name.lower()
        is_momentum = any(x in active_profile for x in ['vector', 'dual', 'velocity', 'pulse', 'hybrid', 'axis', 'test e', 'test s', 'test tq', 'test r', 'test tqm'])
        is_rotator = is_momentum

        hist = ud.get('history') or []
        init_pos = ud.get('initial_positions') or {}
        mb = ud.get('master_budget', 5000.0)
        active_holds = list(set([tr.get('ticker') for tr in hist if portfolio_store.get_shares(tr.get('ticker')) > 0]))

        # Never enter empty ALL_SHARES mode: default to active hold if present, or top watchlist stock (TQQQ)
        if not t or t == 'ALL_SHARES':
            t = active_holds[0] if active_holds else (wl[0] if wl else 'TQQQ')
        if 'holdings' not in ud or not isinstance(ud['holdings'], dict): ud['holdings'] = {}
        for tk in active_holds:
            sh_cnt = portfolio_store.get_shares(tk)
            if sh_cnt > 0:
                if tk not in ud['holdings']: ud['holdings'][tk] = {}
                ud['holdings'][tk]['shares'] = sh_cnt
        ud['active_holds'] = active_holds

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
        
        all_user_holds = set()
        for u, u_data in portfolio_store.data.get('users', {}).items():
            for tr in u_data.get('history', []): all_user_holds.add(tr.get('ticker'))
                
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
            mb_lb = u_data.get('master_budget', 500.0)
            hist_lb = u_data.get('history') or []
            init_pos_lb = u_data.get('initial_positions') or {}
            
            nh_lb = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist_lb)
            im_lb = sum(pos.get('manual_val', 0.0) for pos in init_pos_lb.values())
            cash_now = mb_lb + nh_lb - im_lb
            
            trades_today = [tr for tr in hist_lb if tr.get('date_str') == today_str]
            net_cash_today = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in trades_today)
            cash_start = cash_now - net_cash_today
            
            equity_now, equity_start = 0.0, 0.0
            shares_now_map = {}
            for tr in hist_lb:
                tk = tr.get('ticker')
                if tk not in shares_now_map: shares_now_map[tk] = portfolio_store.get_shares(tk, u)
                
            for tk, sh_now in shares_now_map.items():
                if sh_now <= 0 and not any(tr.get('ticker') == tk for tr in trades_today): continue
                p_data = prices.get(tk, {'cur': 0.0, 'start': 0.0})
                equity_now += sh_now * p_data['cur']
                
                sh_start = sh_now
                for tr in trades_today:
                    if tr.get('ticker') == tk:
                        if tr.get('action') == 'BUY': sh_start -= tr.get('shares', 0)
                        else: sh_start += tr.get('shares', 0)
                equity_start += max(0, sh_start) * p_data['start']
                
            tot_eq_now = max(0, cash_now) + equity_now
            tot_eq_start = max(0, cash_start) + equity_start

            # Calculate active holdings for user u to power HELD icons
            user_holds = [tk for tk, sh in shares_now_map.items() if sh > 0]
            held_sym = user_holds[0] if user_holds else ''
            day_diff = round(tot_eq_now - tot_eq_start, 2)
            day_pct = round((day_diff / tot_eq_start * 100.0), 2) if tot_eq_start > 0 else 0.0
            day_col = '#0ecb81' if day_diff > 0 else ('#ff5364' if day_diff < 0 else '#8a8a9e')

            # Calculate True Daily Average since strategy start date
            trade_dates = sorted(list(set(tr.get('date_str') for tr in hist_lb if tr.get('date_str'))))
            start_date = trade_dates[0] if trade_dates else today_str
            active_days = max(1, len(pd.bdate_range(start_date, today_str)))

            cum_gain_val = tot_eq_now - mb_lb
            cum_gain_pct = (cum_gain_val / mb_lb * 100.0) if mb_lb > 0 else 0.0
            daily_avg_pct = round(cum_gain_pct / active_days, 2)
            daily_avg_val = round(cum_gain_val / active_days, 2)

            leaderboard.append({
                'user': u, 
                'equity': round(tot_eq_now, 2), 
                'budget': mb_lb, 
                'daily_pnl': day_diff,
                'daily_pct': day_pct,
                'daily_color': day_col,
                'daily_avg_pct': daily_avg_pct,
                'daily_avg_pnl': daily_avg_val,
                'days_active': active_days,
                'holding': held_sym,
                'held_ticker': held_sym,
                'active_holds': user_holds,
                'has_holding': len(user_holds) > 0
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
        
        for tk in active_holds:
            sh_h = portfolio_store.get_shares(tk)
            if sh_h <= 0: continue
            t_buys_today = [tr for tr in hist if tr.get('ticker') == tk and tr.get('action') == 'BUY' and tr.get('date_str') == now_lon.strftime('%Y-%m-%d')]
            df_5m = pnl_dfs_5m.get(tk)
            if df_5m is not None and not df_5m.empty:
                cur_price = df_5m['Close'].iloc[-1]
                div = 100.0 if tk.endswith('.L') and cur_price > 100 else 1.0
                cur_price_pounds = cur_price / div
                
                if t_buys_today: p_today_base_pounds = sum(tr.get('amount', 0) for tr in t_buys_today) / sum(tr.get('shares', 1) for tr in t_buys_today)
                else:
                    last_date_str = now_lon.strftime('%Y-%m-%d')
                    prev_sessions = df_5m[df_5m.index.tz_convert('Europe/London').strftime('%Y-%m-%d') < last_date_str]
                    p_today_base_pence = prev_sessions['Close'].iloc[-1] if not prev_sessions.empty else df_5m['Close'].iloc[0]
                    p_today_base_pounds = p_today_base_pence / div

                target_ts = now_utc - pd.Timedelta(hours=1)
                prior_df = df_5m[df_5m.index <= target_ts]
                p_1h_base_pence = prior_df['Close'].iloc[-1] if not prior_df.empty else (df_5m['Close'].iloc[0] if not df_5m.empty else cur_price)
                p_1h_base_pounds = p_1h_base_pence / div

                tot_today_diff += sh_h * (cur_price_pounds - p_today_base_pounds)
                tot_1h_diff += sh_h * (cur_price_pounds - p_1h_base_pounds)

        master_today_pct = (tot_today_diff / mb * 100.0) if mb > 0 else 0.0
        master_1h_pct = (tot_1h_diff / mb * 100.0) if mb > 0 else 0.0

        master_pnl_data = {
            'all': {'val': f"{'+' if master_pnl_val>0 else ''}£{master_pnl_val:.2f} ({'+' if master_pnl_pct>0 else ''}{master_pnl_pct:.2f}%)", 'color': '#0ecb81' if master_pnl_val > 0 else ('#ff5364' if master_pnl_val < 0 else '#8a8a9e')},
            'today': {'val': f"{'+' if tot_today_diff>0 else ''}£{tot_today_diff:.2f} ({'+' if master_today_pct>0 else ''}{master_today_pct:.2f}%)", 'color': '#0ecb81' if tot_today_diff > 0 else ('#ff5364' if tot_today_diff < 0 else '#8a8a9e')},
            '1h': {'val': f"{'+' if tot_1h_diff>0 else ''}£{tot_1h_diff:.2f} ({'+' if master_1h_pct>0 else ''}{master_1h_pct:.2f}%)", 'color': '#0ecb81' if tot_1h_diff > 0 else ('#ff5364' if tot_1h_diff < 0 else '#8a8a9e')}
        }

        engine = MarketScoringEngine()
        regime = engine.check_market_regime()

        if t == 'ALL_SHARES':
            health_pct, health_color, health_text = 0, '#8a8a9e', 'No Data'
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
                        health_color = '#0ecb81' if top_score >= 65 else ('#ff9900' if top_score >= 40 else '#ffd400')
                        health_text = f"Hunting: {top_stock} ({top_score}/100)"

            lines = []
            if wl:
                def fetch_t(tick): return tick, fetch_yf_data(tick, req_p, req_i)
                dfs = {}
                with ThreadPoolExecutor(max_workers=4) as ex:
                    for tick, df_t in ex.map(fetch_t, wl[:15]): dfs[tick] = df_t
                
                colors = ['#ffd400', '#0ecb81', '#ff5364', '#ff9900', '#b388ff', '#ffff00', '#ff4081', '#18ffff']
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
                'health_pct': health_pct, 'health_color': health_color, 'health_text': health_text, 'is_rotator_profile': is_rotator
            }})

        df = fetch_yf_data(t, req_p, req_i)
        if df.empty:
            return jsonify({'ohlc': [], 'wl_status': wl_status, 'name': f'{t} Data Loading...', 'portfolio': ud, 'leaderboard': leaderboard, 'metrics': {
                'price': 0, 'price_display': '£0.00', 'discount': '--', 'buy_score': '0 / 100', 'tranches': 0,
                'status': 'Loading Data', 'color': '#787e8e', 'reason': 'Fetching fresh market quotes.',
                'action_main': 'WAIT', 'action_sub': '', 'action_color': '#787e8e', 'shares_owned': 0, 'value_owned': 0.0,
                'pnl_display': '£0.00 (0.00%)', 'pnl_color': '#8a8a9e', 'pnl_data': master_pnl_data, 'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data,
                'master_budget': mb, 'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime,
                'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': '', 'is_rotator_profile': is_rotator
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
            if t_buys: 
                avg_buy_p = t_buys[0].get('price', 0.0)
                buy_ts = t_buys[0].get('timestamp', 0)
                df_check = pnl_dfs_5m.get(t) if pnl_dfs_5m.get(t) is not None else fetch_yf_data(t, "5d", "5m")
                if df_check is not None and not df_check.empty and 'High' in df_check.columns:
                    try:
                        post_buy_df = df_check[df_check.index.astype('int64') // 10**9 >= (buy_ts - 300)]
                        candle_high = float(post_buy_df['High'].max()) if not post_buy_df.empty else last_p
                    except Exception:
                        candle_high = last_p
                else:
                    candle_high = last_p
            else:
                candle_high = last_p

            stored_hw = (holds_dict.get(t) or {}).get('high_water', last_p)
            highest_p = max(stored_hw, candle_high, last_p)
            if highest_p > stored_hw:
                if 'holdings' not in ud: ud['holdings'] = {}
                if t not in ud['holdings']: ud['holdings'][t] = {}
                ud['holdings'][t]['high_water'] = highest_p
                portfolio_store.save_data(portfolio_store.data)

        if is_momentum:
            df_score = pnl_dfs_5m.get(t) if pnl_dfs_5m.get(t) is not None else fetch_yf_data(t, "5d", "5m")
            if df_score is None or df_score.empty: df_score = df
            st = engine.score_momentum(df_score, last_p, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=active_profile, regime=regime)
        else:
            av = df['Volume'].tail(20).mean() if len(df)>=20 else 1.0
            st = engine.score_nav_asset(t, last_p, (df['Volume'].iloc[-1]/av) if av>0 else 1.0) if t in engine.nav_bases else engine.score_equity(fetch_yf_data(t, "1y", "1d"), last_p)

        cb_t = (init_pos.get(t) or {}).get('manual_val', 0.0) + sum(tr.get('amount', 0) if tr.get('action')=='BUY' else -tr.get('amount', 0) for tr in hist if tr.get('ticker')==t)
        
        ticker_pnl_data = {}
        if sh_own > 0 and cb_t > 0:
            pv, pp = round(val_own - cb_t, 2), round((val_own - cb_t) / cb_t * 100, 2)
            c = '#0ecb81' if pv > 0 else ('#ff5364' if pv < 0 else '#8a8a9e')
            pnl_d = f"{'+' if pv>0 else ''}£{pv:.2f} ({'+' if pp>0 else ''}{pp:.2f}%)"

            df_5m = pnl_dfs_5m.get(t)
            if df_5m is not None and not df_5m.empty:
                if df_5m.index.tz is None: df_5m.index = df_5m.index.tz_localize('UTC')
                else: df_5m.index = df_5m.index.tz_convert('UTC')
                
                cur_p = df_5m['Close'].iloc[-1]
                div = 100.0 if t.endswith('.L') and cur_p > 100 else 1.0
                cur_p_pounds = cur_p / div
                last_ts = df_5m.index[-1]
                last_lon = last_ts.tz_convert('Europe/London')
                
                t_buys_today_t = [tr for tr in hist if tr.get('ticker') == t and tr.get('action') == 'BUY' and tr.get('date_str') == now_lon.strftime('%Y-%m-%d')]
                avg_b_today_t_pounds = (sum(tr.get('amount', 0) for tr in t_buys_today_t) / sum(tr.get('shares', 1) for tr in t_buys_today_t)) if t_buys_today_t else 0.0

                if now_lon.date() > last_lon.date():
                    p_today_pounds = cur_p_pounds
                    p_1h_pounds = cur_p_pounds
                else:
                    last_date_str = last_lon.strftime('%Y-%m-%d')
                    prev_sessions = df_5m[df_5m.index.tz_convert('Europe/London').strftime('%Y-%m-%d') < last_date_str]
                    p_today_mkt_pence = prev_sessions['Close'].iloc[-1] if not prev_sessions.empty else df_5m['Close'].iloc[0]
                    p_today_pounds = avg_b_today_t_pounds if avg_b_today_t_pounds > 0 else (p_today_mkt_pence / div)
                    
                    if (now_utc - last_ts).total_seconds() > 4200: p_1h_pounds = cur_p_pounds
                    else:
                        target_ts = now_utc - pd.Timedelta(hours=1)
                        prior_df = df_5m[df_5m.index <= target_ts]
                        p_1h_mkt_pence = prior_df['Close'].iloc[-1] if not prior_df.empty else p_today_mkt_pence
                        p_1h_pounds = avg_b_today_t_pounds if avg_b_today_t_pounds > 0 else (p_1h_mkt_pence / div)
                
                pv_today = sh_own * (cur_p_pounds - p_today_pounds)
                pv_1h = sh_own * (cur_p_pounds - p_1h_pounds)
                val_today_start = sh_own * p_today_pounds
                val_1h_start = sh_own * p_1h_pounds
                
                pp_today = (pv_today / val_today_start * 100.0) if val_today_start > 0 else 0.0
                pp_1h = (pv_1h / val_1h_start * 100.0) if val_1h_start > 0 else 0.0
                
                ticker_pnl_data = {
                    'all': {'val': pnl_d, 'color': c},
                    'today': {'val': f"{'+' if pv_today>0 else ''}£{pv_today:.2f} ({'+' if pp_today>0 else ''}{pp_today:.2f}%)", 'color': '#0ecb81' if pv_today > 0 else ('#ff5364' if pv_today < 0 else '#8a8a9e')},
                    '1h': {'val': f"{'+' if pv_1h>0 else ''}£{pv_1h:.2f} ({'+' if pp_1h>0 else ''}{pp_1h:.2f}%)", 'color': '#0ecb81' if pv_1h > 0 else ('#ff5364' if pv_1h < 0 else '#8a8a9e')}
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

        return jsonify({'ohlc': data, 'ticker': t, 'mathLine': mathLine, 'wl_status': wl_status, 'name': engine.asset_names.get(t, t), 'portfolio': ud, 'leaderboard': leaderboard, 'metrics': {
            'price': last_p, 'price_display': f"{last_p:.2f}p (£{(last_p/100.0):.2f})" if t.endswith('.L') and last_p>100 else f"£{last_p:.2f}",
            'discount': st['discount'], 'buy_score': f"{st['score']} / 100", 'tranches': st['tranches'],
            'status': st['status'], 'color': st['color'], 'reason': st['reason'],
            'action_main': st['action_main'], 'action_sub': st['action_sub'], 'action_color': st['action_color'],
            'shares_owned': sh_own, 'value_owned': val_own, 'pnl_display': ticker_pnl_data['all']['val'], 'pnl_color': ticker_pnl_data['all']['color'], 'pnl_data': ticker_pnl_data,
            'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data, 'master_budget': mb,
            'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime, 'trade_type': st.get('trade_type', 'LONG'),
            'health_pct': st.get('health_pct', 0), 'health_color': st.get('health_color', '#8a8a9e'), 'health_text': st.get('health_text', 'N/A'), 'is_rotator_profile': is_rotator
        }})
    except Exception as e: 
        return jsonify({'error': str(e)}), 500

@app.route('/api/report/forensic', methods=['GET'])
def export_forensic_report():
    import io, re
    from flask import send_file
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.chart import LineChart, Reference
    from openpyxl.utils import get_column_letter

    portfolio_store.reload()
    now_lon = pd.Timestamp.now(tz='Europe/London')
    users_data = portfolio_store.data.get('users', {})
    
    wb = openpyxl.Workbook()

    # Sheet 1: Fleet Master Overview
    ws_master = wb.active
    ws_master.title = "Fleet Master Overview"
    ws_master.views.sheetView[0].showGridLines = True

    # Sheet 2: Complete Forensic Raw Audit Log (All 18 parameters for sweep)
    ws_raw = wb.create_sheet(title="Forensic Raw Audit Log")
    ws_raw.views.sheetView[0].showGridLines = True

    dark_fill = PatternFill(start_color="14171F", end_color="14171F", fill_type="solid")
    sub_header_fill = PatternFill(start_color="1E222D", end_color="1E222D", fill_type="solid")
    kpi_label_fill = PatternFill(start_color="1A1D24", end_color="1A1D24", fill_type="solid")
    kpi_val_fill = PatternFill(start_color="0F1115", end_color="0F1115", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin', color='262B36'), right=Side(style='thin', color='262B36'),
        top=Side(style='thin', color='262B36'), bottom=Side(style='thin', color='262B36')
    )

    # Master Overview Header
    ws_master.merge_cells('A1:I1')
    t_cell = ws_master['A1']
    t_cell.value = "IDC TRADING • FLEET INTELLIGENCE & FORENSIC AUDIT"
    t_cell.font = Font(name="Calibri", size=12, bold=True, color="FFD400")
    t_cell.fill = dark_fill
    t_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws_master.row_dimensions[1].height = 28

    ov_headers = ["Profile", "Budget", "Current Equity", "Net Return (£)", "Return %", "Trades", "Win Rate %", "Profit Factor", "Status"]
    for c_i, h in enumerate(ov_headers, 1):
        cell = ws_master.cell(row=3, column=c_i, value=h)
        cell.font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        cell.fill = sub_header_fill
        cell.alignment = Alignment(horizontal="center" if c_i > 1 else "left", vertical="center")
        cell.border = thin_border
    ws_master.row_dimensions[3].height = 24

    # Raw Audit Log Headers
    raw_headers = [
        'Trade_Date', 'Profile', 'Ticker', 'Shares', 'Entry_Time', 'Exit_Time', 'Hold_Mins',
        'Entry_Price', 'Exit_Price', 'Peak_Price', 'Peak_PnL_%', 'Realized_PnL_£', 'Realized_PnL_%',
        'Capture_Efficiency_%', 'Exit_Trigger', 'Exit_Reason', 'Profile_Daily_PnL_£', 'Profile_Daily_PnL_%'
    ]
    for c_i, h in enumerate(raw_headers, 1):
        cell = ws_raw.cell(row=1, column=c_i, value=h)
        cell.font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        cell.fill = sub_header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border
    ws_raw.row_dimensions[1].height = 24

    ov_row = 4
    raw_row = 2
    daily_pnl_map = {}

    # PASS 1: Build trade details & accumulate daily P&L
    for u_name, u_data in users_data.items():
        if not isinstance(u_data, dict): continue
        hist = u_data.get('history') or []
        buy_q = []
        for t in reversed(hist):
            tk = t.get('ticker')
            if not tk or tk == 'CALIBRATION': continue
            if t.get('action') == 'BUY':
                buy_q.append(t)
            elif t.get('action') == 'SELL' and buy_q:
                b = buy_q.pop(0)
                diff = round((float(t.get('price', 1)) - float(b.get('price', 1))) * int(t.get('shares', 1)), 2)
                d_key = t.get('date_str', '')
                daily_pnl_map[(u_name, d_key)] = daily_pnl_map.get((u_name, d_key), 0.0) + diff

    # PASS 2: Populate Master Sheet & Dedicated Profile Worksheets
    for u_name, u_data in users_data.items():
        if not isinstance(u_data, dict): continue
        hist = u_data.get('history') or []
        budget = float(u_data.get('master_budget', 500.0))

        buy_q = []
        p_trades = []
        wins, losses, win_val, loss_val = 0, 0, 0.0, 0.0

        for t in reversed(hist):
            tk = t.get('ticker')
            if not tk or tk == 'CALIBRATION': continue
            if t.get('action') == 'BUY':
                buy_q.append(t)
            elif t.get('action') == 'SELL' and buy_q:
                b = buy_q.pop(0)
                b_p = float(b.get('price', 1.0))
                s_p = float(t.get('price', 1.0))
                sh = int(t.get('shares', 1))
                diff = round((s_p - b_p) * sh, 2)
                diff_pct = round(((s_p - b_p) / b_p) * 100.0, 2)
                dur = max(1, int((t.get('timestamp', 0) - b.get('timestamp', 0)) / 60))
                peak_p = max(float(t.get('highest_price', s_p)), s_p, b_p)
                peak_pct = round(((peak_p - b_p) / b_p) * 100.0, 2)
                cap_eff = round((diff_pct / peak_pct * 100.0), 2) if peak_pct > 0 else 0.0
                d_key = t.get('date_str', '')

                if diff >= 0: wins += 1; win_val += diff
                else: losses += 1; loss_val += abs(diff)

                t_record = {
                    'date': d_key, 'time_in': b.get('time', ''), 'time_out': t.get('time', ''),
                    'ticker': tk, 'shares': sh, 'buy_price': b_p, 'sell_price': s_p,
                    'peak_price': peak_p, 'peak_pct': peak_pct, 'pnl_val': diff, 'pnl_pct': diff_pct,
                    'cap_eff': cap_eff, 'mins': dur, 'trigger': t.get('status', 'Take Profit/Stop'),
                    'reason': t.get('reason', '')
                }
                p_trades.append(t_record)

                # Append to raw audit log
                ws_raw.cell(row=raw_row, column=1, value=d_key).border = thin_border
                ws_raw.cell(row=raw_row, column=2, value=u_name).border = thin_border
                ws_raw.cell(row=raw_row, column=3, value=tk).border = thin_border
                ws_raw.cell(row=raw_row, column=4, value=sh).border = thin_border
                ws_raw.cell(row=raw_row, column=5, value=b.get('time', '')).border = thin_border
                ws_raw.cell(row=raw_row, column=6, value=t.get('time', '')).border = thin_border
                ws_raw.cell(row=raw_row, column=7, value=dur).border = thin_border
                ws_raw.cell(row=raw_row, column=8, value=b_p).number_format = '£#,##0.00'
                ws_raw.cell(row=raw_row, column=9, value=s_p).number_format = '£#,##0.00'
                ws_raw.cell(row=raw_row, column=10, value=peak_p).number_format = '£#,##0.00'
                ws_raw.cell(row=raw_row, column=11, value=peak_pct / 100.0).number_format = '0.00%'
                ws_raw.cell(row=raw_row, column=12, value=diff).number_format = '£#,##0.00; -£#,##0.00; £0.00'
                ws_raw.cell(row=raw_row, column=13, value=diff_pct / 100.0).number_format = '0.00%'
                ws_raw.cell(row=raw_row, column=14, value=cap_eff / 100.0).number_format = '0.00%'
                ws_raw.cell(row=raw_row, column=15, value=t.get('status', 'Exit')).border = thin_border
                ws_raw.cell(row=raw_row, column=16, value=t.get('reason', '')).border = thin_border
                
                day_tot = daily_pnl_map.get((u_name, d_key), 0.0)
                ws_raw.cell(row=raw_row, column=17, value=day_tot).number_format = '£#,##0.00; -£#,##0.00; £0.00'
                ws_raw.cell(row=raw_row, column=18, value=(day_tot / budget)).number_format = '0.00%'
                raw_row += 1

        net_gain = win_val - loss_val
        current_eq = budget + net_gain
        tot_trades = wins + losses
        win_rate = (wins / tot_trades * 100.0) if tot_trades > 0 else 0.0
        profit_factor = (win_val / loss_val) if loss_val > 0 else (win_val if win_val > 0 else 1.0)
        ret_pct = (net_gain / budget * 100.0) if budget > 0 else 0.0

        # Master Overview Row
        ws_master.cell(row=ov_row, column=1, value=u_name).border = thin_border
        vals_master = [
            (budget, '£#,##0.00'), (current_eq, '£#,##0.00'), (net_gain, '£#,##0.00'),
            (ret_pct / 100.0, '0.00%'), (tot_trades, '#,##0'), (win_rate / 100.0, '0.0%'),
            (round(profit_factor, 2), '0.00'), ("Active (Auto)", '@')
        ]
        for c_i, (v, fmt) in enumerate(vals_master, 2):
            c = ws_master.cell(row=ov_row, column=c_i, value=v)
            c.number_format = fmt
            c.border = thin_border
            if c_i == 4:
                c.font = Font(name="Calibri", size=10, bold=True, color="0ECB81" if net_gain >= 0 else "FF5364")
        ov_row += 1

        # Individual Profile Sheet
        safe_name = re.sub(r'[^a-zA-Z0-9 ]', '', u_name.split(' - ')[0])[:30].strip()
        ws_p = wb.create_sheet(title=safe_name)
        ws_p.views.sheetView[0].showGridLines = True

        # Header Title
        ws_p.merge_cells('A1:L1')
        p_t = ws_p['A1']
        p_t.value = f"{u_name.upper()} • PERFORMANCE AUDIT & COMPOUNDING INTELLIGENCE"
        p_t.font = Font(name="Calibri", size=11, bold=True, color="FFD400")
        p_t.fill = dark_fill
        p_t.alignment = Alignment(horizontal="center", vertical="center")
        ws_p.row_dimensions[1].height = 26

        # Full-Width 6-Card KPI Row (Merged across pairs to cover A through L)
        kpis = [
            ("STARTING BUDGET", f"£{budget:,.2f}", "A", "B"),
            ("CURRENT EQUITY", f"£{current_eq:,.2f}", "C", "D"),
            ("TOTAL NET P&L", f"{'+' if net_gain>=0 else ''}£{net_gain:,.2f} ({ret_pct:+.2f}%)", "E", "F"),
            ("WIN RATE", f"{win_rate:.1f}% ({wins}W / {losses}L)", "G", "H"),
            ("PROFIT FACTOR", f"{profit_factor:.2f}", "I", "J"),
            ("DAILY RUN-RATE", f"{'+' if net_gain>=0 else ''}{(ret_pct/5):.2f}% / Day", "K", "L")
        ]
        ws_p.row_dimensions[3].height = 16
        ws_p.row_dimensions[4].height = 22

        for label, val, c1, c2 in kpis:
            ws_p.merge_cells(f"{c1}3:{c2}3")
            cl = ws_p[f"{c1}3"]
            cl.value = label
            cl.font = Font(name="Calibri", size=8, bold=True, color="8A8A9E")
            cl.fill = kpi_label_fill
            cl.alignment = Alignment(horizontal="center", vertical="center")
            cl.border = thin_border

            ws_p.merge_cells(f"{c1}4:{c2}4")
            cv = ws_p[f"{c1}4"]
            cv.value = val
            cv.font = Font(name="Calibri", size=10, bold=True, color="0ECB81" if ('+' in val or '£' in val and net_gain>=0) else ("FF5364" if '-' in val else "FFFFFF"))
            cv.fill = kpi_val_fill
            cv.alignment = Alignment(horizontal="center", vertical="center")
            cv.border = thin_border

        # Trade History Table Headers
        p_headers = ["Date", "Time In", "Time Out", "Ticker", "Shares", "Buy (£)", "Sell (£)", "Peak (£)", "P&L (£)", "Return %", "Hold (Mins)", "Exit Reason / Trigger"]
        for c_i, h in enumerate(p_headers, 1):
            cell = ws_p.cell(row=6, column=c_i, value=h)
            cell.font = Font(name="Calibri", size=9, bold=True, color="FFFFFF")
            cell.fill = sub_header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border
        ws_p.row_dimensions[6].height = 22

        p_row = 7
        for tr in p_trades:
            ws_p.cell(row=p_row, column=1, value=tr['date']).border = thin_border
            ws_p.cell(row=p_row, column=2, value=tr['time_in']).border = thin_border
            ws_p.cell(row=p_row, column=3, value=tr['time_out']).border = thin_border
            ws_p.cell(row=p_row, column=4, value=tr['ticker']).border = thin_border
            ws_p.cell(row=p_row, column=5, value=tr['shares']).border = thin_border
            ws_p.cell(row=p_row, column=6, value=tr['buy_price']).number_format = '£#,##0.00'
            ws_p.cell(row=p_row, column=7, value=tr['sell_price']).number_format = '£#,##0.00'
            ws_p.cell(row=p_row, column=8, value=tr['peak_price']).number_format = '£#,##0.00'
            c_diff = ws_p.cell(row=p_row, column=9, value=tr['pnl_val'])
            c_diff.number_format = '£#,##0.00; -£#,##0.00; £0.00'
            c_diff.font = Font(name="Calibri", size=9, bold=True, color="0A3622" if tr['pnl_val']>=0 else "78281F")
            ws_p.cell(row=p_row, column=10, value=tr['pnl_pct'] / 100.0).number_format = '0.00%'
            ws_p.cell(row=p_row, column=11, value=tr['mins']).border = thin_border
            
            # Formatted Reason Column with wrap
            c_rsn = ws_p.cell(row=p_row, column=12, value=tr['reason'])
            c_rsn.border = thin_border
            c_rsn.alignment = Alignment(wrap_text=True, vertical="center")
            p_row += 1

        # 3-Line Native Vector Excel Chart
        chart_start_row = max(10, p_row + 2)
        ws_p.cell(row=chart_start_row, column=1, value="Sequence").font = Font(name="Calibri", size=9, bold=True)
        ws_p.cell(row=chart_start_row, column=2, value="Actual Equity").font = Font(name="Calibri", size=9, bold=True)
        ws_p.cell(row=chart_start_row, column=3, value="Projected Run-Rate").font = Font(name="Calibri", size=9, bold=True)
        ws_p.cell(row=chart_start_row, column=4, value="1.0% Daily Target").font = Font(name="Calibri", size=9, bold=True)

        c_data_r = chart_start_row + 1
        ws_p.cell(row=c_data_r, column=1, value="Start")
        ws_p.cell(row=c_data_r, column=2, value=budget)
        ws_p.cell(row=c_data_r, column=3, value=budget)
        ws_p.cell(row=c_data_r, column=4, value=budget)
        c_data_r += 1

        cum = budget
        actual_trade_list = list(reversed(p_trades))
        total_chart_points = max(20, len(actual_trade_list) + 10)
        r_daily_avg = (net_gain / budget / 5.0) if budget > 0 else 0.01

        for idx in range(1, total_chart_points + 1):
            seq_lbl = f"T+{idx}"
            ws_p.cell(row=c_data_r, column=1, value=seq_lbl)

            if idx <= len(actual_trade_list):
                cum += actual_trade_list[idx - 1]['pnl_val']
                ws_p.cell(row=c_data_r, column=2, value=round(cum, 2))
                ws_p.cell(row=c_data_r, column=3, value=round(cum, 2))
            else:
                ws_p.cell(row=c_data_r, column=2, value=None) # Blank for future actuals
                proj_val = current_eq * ((1 + r_daily_avg) ** (idx - len(actual_trade_list)))
                ws_p.cell(row=c_data_r, column=3, value=round(proj_val, 2))

            tgt_val = budget * ((1.01) ** idx)
            ws_p.cell(row=c_data_r, column=4, value=round(tgt_val, 2))
            c_data_r += 1

        chart = LineChart()
        chart.title = f"{safe_name}: Actual vs Run-Rate vs 1.0% Daily Target"
        chart.style = 13
        chart.y_axis.title = "Portfolio Value (£)"
        chart.x_axis.title = "Trade Sequence / Sessions"
        chart.width = 19
        chart.height = 9.5

        data_ref = Reference(ws_p, min_col=2, min_row=chart_start_row, max_col=4, max_row=c_data_r - 1)
        cats_ref = Reference(ws_p, min_col=1, min_row=chart_start_row + 1, max_row=c_data_r - 1)
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)

        if len(chart.series) >= 3:
            # Series 1: Actual Equity (Solid Green)
            chart.series[0].graphicalProperties.line.solidFill = "0ECB81"
            chart.series[0].graphicalProperties.line.width = 28000
            # Series 2: Projected Run-Rate (Dashed Cyan)
            chart.series[1].graphicalProperties.line.solidFill = "38BDF8"
            chart.series[1].graphicalProperties.line.dashStyle = "dash"
            chart.series[1].graphicalProperties.line.width = 22000
            # Series 3: Target Line (Dotted Gold)
            chart.series[2].graphicalProperties.line.solidFill = "FFD400"
            chart.series[2].graphicalProperties.line.dashStyle = "dot"
            chart.series[2].graphicalProperties.line.width = 20000

        ws_p.add_chart(chart, f"F{chart_start_row}")

        # Explicit Auto-Fitted Column Widths (No Clipping)
        col_w_map = {
            'A': 12, 'B': 14, 'C': 14, 'D': 8, 'E': 8, 'F': 11,
            'G': 11, 'H': 11, 'I': 11, 'J': 10, 'K': 11, 'L': 52
        }
        for col_letter, width in col_w_map.items():
            ws_p.column_dimensions[col_letter].width = width

    # Auto-fit Master Sheet & Raw Audit Sheet
    for ci in range(1, len(ov_headers) + 1):
        ws_master.column_dimensions[get_column_letter(ci)].width = 30 if ci == 1 else 15
    for ci in range(1, len(raw_headers) + 1):
        ws_raw.column_dimensions[get_column_letter(ci)].width = 16

    out_buf = io.BytesIO()
    wb.save(out_buf)
    out_buf.seek(0)

    filename = f"IDC_Trading_Fleet_Forensic_Compounding_{now_lon.strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        out_buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.route('/api/broker/status', methods=['GET'])
def get_broker_status():
    portfolio_store.reload()
    broker = portfolio_store.data.get('broker') or {}
    
    # Fallback to user-level broker keys if top-level dictionary is empty
    if not broker.get('api_key') or not broker.get('secret_key'):
        for u in portfolio_store.data.get('users', {}).values():
            if isinstance(u, dict) and u.get('broker', {}).get('api_key') and u.get('broker', {}).get('secret_key'):
                broker.update(u['broker'])
                portfolio_store.data['broker'] = broker
                portfolio_store.save_data(portfolio_store.data)
                break

    api_key = str(broker.get('api_key', '')).strip()
    secret_key = str(broker.get('secret_key', '')).strip()
    mode = broker.get('mode', 'paper')
    paused = broker.get('paused', False)
    allocations = broker.get('allocations') or {}
    
    if not api_key or not secret_key:
        return jsonify({
            'connected': False, 
            'status': 'OFFLINE', 
            'buying_power': 0, 
            'paused': paused, 
            'api_key': api_key,
            'secret_key': secret_key,
            'allocations': allocations
        })

    base_url = "https://paper-api.alpaca.markets" if mode == 'paper' else "https://api.alpaca.markets"
    req = urllib.request.Request(
        f"{base_url}/v2/account",
        headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            acc = json.loads(resp.read().decode('utf-8'))
            
            # Fetch live positions directly from Alpaca
            live_positions = []
            try:
                req_p = urllib.request.Request(f"{base_url}/v2/positions", headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key})
                with urllib.request.urlopen(req_p, timeout=4) as r_p:
                    raw_pos = json.loads(r_p.read().decode('utf-8'))
                    live_positions = [{
                        'symbol': p['symbol'],
                        'qty': int(p.get('qty', 0)),
                        'market_value': float(p.get('market_value', 0.0)),
                        'unrealized_pl': float(p.get('unrealized_pl', 0.0))
                    } for p in raw_pos]
            except Exception: pass

            # Fetch open/queued orders directly from Alpaca
            has_queued_orders = False
            try:
                req_o = urllib.request.Request(f"{base_url}/v2/orders?status=open", headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key})
                with urllib.request.urlopen(req_o, timeout=4) as r_o:
                    open_orders = json.loads(r_o.read().decode('utf-8'))
                    if len(open_orders) > 0:
                        has_queued_orders = True
            except Exception: pass

            # Audit desync against all active bots
            claimed_symbols = set()
            for u in portfolio_store.data.get('users', {}).values():
                if isinstance(u, dict):
                    for tr in u.get('history', []):
                        tk = tr.get('ticker')
                        if tk and portfolio_store.get_shares(tk) > 0:
                            claimed_symbols.add(tk)

            has_desync = any(p['symbol'] not in claimed_symbols for p in live_positions)

            return jsonify({
                'connected': True,
                'status': acc.get('status', 'ACTIVE'),
                'buying_power': float(acc.get('buying_power', 0.0)),
                'cash': float(acc.get('cash', 0.0)),
                'portfolio_value': float(acc.get('portfolio_value', 0.0)),
                'positions': live_positions,
                'has_desync': has_desync,
                'has_queued_orders': has_queued_orders,
                'paused': paused,
                'mode': mode,
                'api_key': api_key,
                'secret_key': secret_key,
                'allocations': allocations
            })
    except Exception as e:
        return jsonify({
            'connected': False, 
            'status': 'AUTH_ERROR', 
            'error': str(e), 
            'paused': paused, 
            'api_key': api_key,
            'secret_key': secret_key,
            'allocations': allocations
        })

@app.route('/api/watchlist/scan_all', methods=['GET'])
def get_watchlist_scanner():
    portfolio_store.reload()
    scope = request.args.get('scope', '').strip().lower()
    req_user = request.args.get('user', '').strip()
    active_profile = (req_user or portfolio_store.active_username()).strip()
    engine = MarketScoringEngine()
    regime = engine.check_market_regime()
    users_dict = portfolio_store.data.get('users', {})

    # MOBILE SCOPE: Scan ONLY the active profile's own watchlist
    if scope in ['active', 'local', 'profile']:
        ud = portfolio_store.user_data(active_profile)
        prof_wl = ud.get('watchlist') or ['TQQQ', 'SOXL', 'SQQQ']
        holds = ud.get('holdings') or {}
        hist = ud.get('history') or []
        results = []

        for tick in prof_wl:
            tick = tick.upper().strip()
            if not tick: continue
            try:
                df = fetch_yf_data(tick, "5d", "5m")
                if df.empty: continue
                cur = df['Close'].iloc[-1]
                sh = portfolio_store.get_shares(tick, active_profile)
                is_held = (sh > 0)
                
                avg_p, high_p = 0.0, cur
                if is_held:
                    buys = [tr for tr in hist if tr.get('ticker') == tick and tr.get('action') == 'BUY']
                    if buys: avg_p = buys[0].get('price', 0.0)
                    high_p = (holds.get(tick) or {}).get('high_water', cur)

                st = engine.score_momentum(df, cur, avg_buy_price=avg_p, highest_price=high_p, profile=active_profile.lower(), regime=regime)
                
                trail_pct = 0.75 if any(x in active_profile.lower() for x in ['test e', 'test tq', 'test s2']) else 1.0
                stop_price = high_p * (1.0 - (trail_pct / 100.0))
                
                results.append({
                    'ticker': tick,
                    'user_tag': '',
                    'price': round(cur, 2),
                    'shares': sh,
                    'is_held': is_held,
                    'score': st.get('score', 0),
                    'target_score': 65,
                    'health_pct': st.get('health_pct', 0),
                    'health_text': st.get('health_text', ''),
                    'reason': st.get('reason', ''),
                    'action_main': st.get('action_main', 'HOLD'),
                    'stop_price': round(stop_price, 2),
                    'peak_price': round(high_p, 2)
                })
            except Exception: pass

        return jsonify({'items': results, 'profile': active_profile})

    # WEB (DESKTOP) SCOPE: Full cross-test scanner across all profiles
    held_positions = []
    all_wl_tickers = set()

    for u, u_data in users_dict.items():
        if not isinstance(u_data, dict): continue
        for tk in u_data.get('watchlist', []):
            all_wl_tickers.add(tk.upper())
        for tr in u_data.get('history', []):
            tk = tr.get('ticker', '').upper()
            if not tk or tk == 'CALIBRATION': continue
            sh = portfolio_store.get_shares(tk, u)
            if sh > 0 and not any(p['ticker'] == tk and p['user'] == u for p in held_positions):
                buys = [b for b in u_data.get('history', []) if b.get('ticker') == tk and b.get('action') == 'BUY']
                avg_p = buys[0].get('price', 0.0) if buys else 0.0
                hw = (u_data.get('holdings', {}).get(tk) or {}).get('high_water', avg_p)
                short_tag = u.split(' - ')[0].replace('Test ', '').strip()
                held_positions.append({
                    'ticker': tk,
                    'user': u,
                    'user_tag': short_tag,
                    'shares': sh,
                    'avg_price': avg_p,
                    'high_water': hw
                })

    results = []

    for p in held_positions:
        try:
            df = fetch_yf_data(p['ticker'], "5d", "5m")
            if df.empty: continue
            cur = df['Close'].iloc[-1]
            st = engine.score_momentum(df, cur, avg_buy_price=p['avg_price'], highest_price=p['high_water'], profile=p['user'].lower(), regime=regime)
            
            trail_pct = 0.75 if any(x in p['user'].lower() for x in ['test e', 'test tq', 'test s2']) else 1.0
            stop_price = p['high_water'] * (1.0 - (trail_pct / 100.0))
            
            results.append({
                'ticker': p['ticker'],
                'user_tag': p['user_tag'],
                'price': round(cur, 2),
                'shares': p['shares'],
                'is_held': True,
                'score': st.get('score', 0),
                'target_score': 65,
                'health_pct': st.get('health_pct', 0),
                'health_text': st.get('health_text', ''),
                'reason': st.get('reason', ''),
                'action_main': st.get('action_main', 'HOLD'),
                'stop_price': round(stop_price, 2),
                'peak_price': round(p['high_water'], 2)
            })
        except Exception: pass

    candidate_tickers = [t for t in all_wl_tickers if not any(p['ticker'] == t and p['user'].lower() == active_profile.lower() for p in held_positions)]
    
    def scan_candidate(tick):
        try:
            df = fetch_yf_data(tick, "5d", "5m")
            if df.empty: return None
            cur = df['Close'].iloc[-1]
            st = engine.score_momentum(df, cur, avg_buy_price=0.0, highest_price=cur, profile=active_profile.lower(), regime=regime)
            return {
                'ticker': tick,
                'user_tag': '',
                'price': round(cur, 2),
                'shares': 0,
                'is_held': False,
                'score': st.get('score', 0),
                'target_score': 65,
                'health_pct': 0,
                'health_text': '',
                'reason': st.get('reason', ''),
                'action_main': st.get('action_main', 'HOLD'),
                'stop_price': 0.0,
                'peak_price': round(cur, 2)
            }
        except Exception: return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(scan_candidate, candidate_tickers):
            if r: results.append(r)

    return jsonify({'items': results})

@app.route('/api/broker/pause', methods=['POST'])
def toggle_broker_pause():
    portfolio_store.reload()
    broker = portfolio_store.data.setdefault('broker', {})
    broker['paused'] = not broker.get('paused', False)
    portfolio_store.save_data(portfolio_store.data)
    return jsonify({'status': 'ok', 'paused': broker['paused']})

@app.route('/api/broker/liquidate', methods=['POST'])
def handle_broker_liquidate():
    portfolio_store.reload()
    broker = portfolio_store.data.get('broker') or {}
    res = alpaca_cancel_and_liquidate(broker.get('api_key'), broker.get('secret_key'), broker.get('mode', 'paper'))
    return jsonify(res)

@app.route('/api/broker/liquidate_bot', methods=['POST'])
def handle_bot_liquidate():
    """Emergency individual strategy liquidator: closes open positions for one specific bot."""
    portfolio_store.reload()
    req = request.get_json() or {}
    strat_key = req.get('strategy', '').strip().lower()
    broker = portfolio_store.data.get('broker') or {}
    api_k = broker.get('api_key', '').strip()
    sec_k = broker.get('secret_key', '').strip()
    b_mode = broker.get('mode', 'paper')

    matched_profile = None
    for p in portfolio_store.data.get('users', {}):
        if strat_key in p.lower():
            matched_profile = p
            break

    if not matched_profile:
        return jsonify({'status': 'error', 'message': f'Profile matching {strat_key} not found.'}), 404

    ud = portfolio_store.data['users'][matched_profile]
    hist = ud.get('history') or []
    active_tickers = list(set([tr.get('ticker') for tr in hist if portfolio_store.get_shares(tr.get('ticker'), matched_profile) > 0]))

    if not active_tickers:
        return jsonify({'status': 'ok', 'message': f'{matched_profile} has no open positions to liquidate.'})

    for tick in active_tickers:
        sh = portfolio_store.get_shares(tick, matched_profile)
        if sh > 0:
            df = fetch_yf_data(tick, '1d', '1m')
            cur_p = df['Close'].iloc[-1] if not df.empty else 1.0
            # execute_trade already dispatches the sell to Alpaca internally; no duplicate call needed
            portfolio_store.execute_trade(tick, 'SELL', sh, cur_p, matched_profile, metadata={'status': 'Manual Liquidation', 'reason': 'Individual Bot Liquidate Triggered'})

    portfolio_store.save_data(portfolio_store.data)
    return jsonify({'status': 'ok', 'message': f'Successfully liquidated {len(active_tickers)} position(s) for {matched_profile}.'})

@app.route('/api/broker/disconnect', methods=['POST'])
def handle_broker_disconnect():
    portfolio_store.reload()
    portfolio_store.data['broker'] = {'paused': False, 'allocations': {}}
    portfolio_store.save_data(portfolio_store.data)
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8095))
    app.run(host='0.0.0.0', port=port, debug=False)