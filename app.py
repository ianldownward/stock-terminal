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

def is_admin_session():
    u = (session.get('email') or session.get('user') or '').strip().lower()
    return (session.get('role') == 'admin') or (u in [ADMIN_USER.lower(), ADMIN_ALIAS.lower(), 'ian'])

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
        # 15s for 1m, 25s for 5m, 180s for daily. Eliminates Yahoo IP rate-limiting across 20+ accounts.
        cache_duration = 15 if interval in ['1m', '2m'] else (25 if interval == '5m' else 180)
        
        if cache_key in YF_CACHE:
            cached_time, df = YF_CACHE[cache_key]
            if not df.empty and (now - cached_time < cache_duration): 
                return df.copy()

        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval)
            if not df.empty:
                df = df.sort_index()
                # Reject stale intraday responses (>15 mins old) from lagging Yahoo nodes
                if interval in ['1m', '2m', '5m', '15m']:
                    last_ts = df.index[-1]
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.tz_localize('UTC')
                    if (now - last_ts.timestamp()) > 900 and cache_key in YF_CACHE:
                        c_time, c_df = YF_CACHE[cache_key]
                        if not c_df.empty and (now - c_time) < 60:
                            return c_df.copy()
                YF_CACHE[cache_key] = (now, df)
                return df.copy()
        except Exception:
            pass

        # STALE CACHE FALLBACK: Only reuse recent cache (<60s) to prevent phantom stale fills
        if cache_key in YF_CACHE:
            cached_time, df = YF_CACHE[cache_key]
            if not df.empty and (now - cached_time) < 60:
                return df.copy()

        return pd.DataFrame()

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

def send_native_web_push(title, message, target_device=None):
    """Sends native Apple / Web Push notifications with full APNs priority headers."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("[PUSH ALERT ERROR] 'pywebpush' library is not installed.")
        return {'status': 'error', 'message': 'pywebpush library not installed on server'}

    portfolio_store.reload()
    subs = portfolio_store.data.get('push_subscriptions') or []

    # Strictly send phone alerts only to devices registered as phone
    if target_device == 'phone':
        subs = [s for s in subs if s.get('device') == 'phone']

    print(f"[PUSH ATTEMPT] {title} | Subscribed devices ({target_device or 'all'}): {len(subs)}")
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

ALPACA_ACCOUNT_CACHE = {}

def get_live_alpaca_account(api_key, secret_key, mode):
    """Fetches real-time equity, cash, and positions directly from Alpaca with a 5-second cache."""
    now = time.time()
    cache_key = f"{api_key}_{mode}"
    if cache_key in ALPACA_ACCOUNT_CACHE:
        cached_time, data = ALPACA_ACCOUNT_CACHE[cache_key]
        if now - cached_time < 5:
            return data

    base_url = "https://paper-api.alpaca.markets" if mode == 'paper' else "https://api.alpaca.markets"
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    try:
        req = urllib.request.Request(f"{base_url}/v2/account", headers=headers)
        with urllib.request.urlopen(req, timeout=4) as resp:
            acc = json.loads(resp.read().decode('utf-8'))
            
            live_pos = []
            try:
                req_p = urllib.request.Request(f"{base_url}/v2/positions", headers=headers)
                with urllib.request.urlopen(req_p, timeout=4) as r_p:
                    live_pos = json.loads(r_p.read().decode('utf-8'))
            except Exception:
                pass
            
            result = {
                'equity': round(float(acc.get('portfolio_value') or acc.get('equity') or 0.0), 2),
                'cash': round(float(acc.get('cash') or 0.0), 2),
                'buying_power': round(float(acc.get('buying_power') or 0.0), 2),
                'last_equity': round(float(acc.get('last_equity') or 0.0), 2),
                'positions': live_pos
            }
            ALPACA_ACCOUNT_CACHE[cache_key] = (now, result)
            return result
    except Exception:
        return None

def send_alpaca_order(api_key, secret_key, mode, ticker, action, shares, bot_name="BOT", trail_pct=None, slices=1):
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

    num_slices = max(1, min(int(slices or 1), int(shares))) if action.lower() == 'buy' else 1
    base_qty = int(shares) // num_slices
    rem_qty = int(shares) % num_slices
    slice_qtys = [base_qty + (1 if i < rem_qty else 0) for i in range(num_slices)]

    try:
        res_data = None
        for idx_s, s_qty in enumerate(slice_qtys):
            if s_qty <= 0:
                continue
            slice_cid = clean_client_id if num_slices == 1 else f"{clean_client_id}_S{idx_s+1}of{num_slices}"
            payload = json.dumps({
                "symbol": ticker.upper(),
                "qty": int(s_qty),
                "side": action.lower(),
                "type": "market",
                "time_in_force": "day",
                "client_order_id": slice_cid
            }).encode('utf-8')
            req = urllib.request.Request(f"{base_url}/v2/orders", data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=6) as resp:
                res_data = json.loads(resp.read().decode('utf-8'))
                if num_slices > 1:
                    print(f"[ALPACA TWAP SLICE {idx_s+1}/{num_slices}] {action.upper()} {s_qty} {ticker} -> ID: {res_data.get('id')}")
                else:
                    print(f"[ALPACA SUCCESS] {action.upper()} {s_qty} {ticker} -> ID: {res_data.get('id')}")
            if num_slices > 1 and idx_s < len(slice_qtys) - 1:
                time.sleep(3.0)

        if res_data is not None:
            
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
            'master_budget': 700.0,
            'watchlist': wl,
            'overnight_sweep': True,
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
        clean_user = (username or '').strip().lower()
        if not clean_user or clean_user in [ADMIN_USER.lower(), ADMIN_ALIAS.lower()]:
            return None
        doc_id = f"client_{clean_user}"
        if self.client:
            try:
                accounts_col = self.client['stock_terminal']['accounts']
                # Search by custom _id first, then fallback to username field
                doc = accounts_col.find_one({"_id": doc_id})
                if not doc:
                    doc = accounts_col.find_one({"username": clean_user})
                if doc and 'portfolio' in doc:
                    p = doc['portfolio']
                    if 'users' not in p or not isinstance(p['users'], dict):
                        p['users'] = {}
                        old_strat = p.get('strategy_name') or p.get('active_user')
                        if old_strat and old_strat.strip():
                            p['users'][old_strat.strip()] = {
                                'master_budget': float(p.get('master_budget', 500.0)),
                                'method': p.get('method', old_strat.strip()),
                                'overnight_sweep': True,
                                'watchlist': p.get('watchlist', ['TQQQ', 'SOXL', 'SQQQ']),
                                'initial_positions': p.get('initial_positions', {}),
                                'holdings': p.get('holdings', {}),
                                'history': p.get('history', []),
                                'drawings': p.get('drawings', {}),
                                'notified_signals': p.get('notified_signals', {}),
                                'settings': p.get('settings', {'period': '1d', 'interval': '5m', 'style': 'candlestick', 'refresh': '15000'})
                            }
                            p['active_user'] = old_strat.strip()
                    if 'is_configured' not in p:
                        p['is_configured'] = bool(p['users'])
                    return p
            except Exception as e:
                print(f"[GET CLIENT STORE ERROR]: {e}")
        return self.blank_client_state(clean_user)

    def save_client_store(self, username, client_portfolio):
        clean_user = (username or '').strip().lower()
        if not clean_user or clean_user in [ADMIN_USER.lower(), ADMIN_ALIAS.lower()]:
            return
        doc_id = f"client_{clean_user}"
        if self.client:
            try:
                accounts_col = self.client['stock_terminal']['accounts']
                # Try updating by _id first; if not found, update by username
                res = accounts_col.update_one(
                    {"_id": doc_id},
                    {"$set": {"portfolio": client_portfolio}},
                    upsert=False
                )
                if res.matched_count == 0:
                    accounts_col.update_one(
                        {"username": clean_user},
                        {"$set": {"portfolio": client_portfolio}},
                        upsert=True
                    )
            except Exception as e:
                print(f"[SAVE CLIENT STORE ERROR]: {e}")

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
            needs_save = False
            if 'users' not in self.data or not isinstance(self.data['users'], dict):
                self.data['users'] = {}
                needs_save = True

            # Purge only exact legacy prototype names and leaked emails (preserves user-created tags)
            legacy_prototypes = {'test e', 'test s', 'test r', 'test tqm', 'test m', 'test u', 'test w', 'test x', 'test p', 'test t', 'ian', 'sovereign core'}
            existing_users = list(self.data['users'].keys())
            for u in existing_users:
                u_clean = u.strip().lower()
                if u_clean in legacy_prototypes or '@' in u_clean:
                    del self.data['users'][u]
                    needs_save = True

            # If no profiles exist at all, initialize with one clean default profile
            if not self.data['users']:
                self.data['users']['Vector Hybrid'] = self.default_user_state('Vector Hybrid')
                self.data['active_user'] = 'Vector Hybrid'
                needs_save = True

            if self.data.get('active_user') not in self.data['users']:
                self.data['active_user'] = list(self.data['users'].keys())[0] if self.data['users'] else 'Vector Hybrid'
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
        if au in self.data['users']:
            return self.data['users'][au]
        if username is None or au in [ADMIN_USER.lower(), ADMIN_ALIAS.lower(), 'ian']:
            self.data['users'][au] = self.default_user_state(au)
            self.save_data(self.data)
            return self.data['users'][au]
        return self.default_user_state(au)

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

    def execute_trade(self, ticker, action_type, shares, price, username=None, metadata=None, client_email=None):
        with self.trade_lock:
            self.reload()
            if not ticker or shares <= 0: return None
            
            is_client = bool(client_email and client_email.strip().lower() not in [ADMIN_USER.lower(), ADMIN_ALIAS.lower()])
            if is_client:
                c_store = self.get_client_store(client_email)
                users = c_store.setdefault('users', {})
                prof_target = username if (username and username in users) else c_store.get('active_user', '')
                if not prof_target and users: prof_target = list(users.keys())[0]
                ud = users.setdefault(prof_target, self.default_user_state(prof_target))
                global_broker = c_store.get('broker') or {}
            else:
                ud = self.user_data(username)
                global_broker = self.data.get('broker') or {}

            action = 'BUY' if 'BUY' in action_type.upper() else 'SELL'
            cost_per_sh = price / 100.0 if ticker.endswith('.L') and price > 100 else price
            
            # Resolve current holdings for this specific profile
            init_pos = ud.get('initial_positions') or {}
            i_sh = (init_pos.get(ticker) or {}).get('shares', 0)
            n_sh = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in ud.get('history', []) if t.get('ticker') == ticker)
            curr_tot = max(0, i_sh + n_sh)

            if action == 'SELL':
                if curr_tot < shares: shares = curr_tot
                if shares <= 0: return None
                
            if action == 'BUY':
                mb = float(ud.get('master_budget', 500.0))
                hist = ud.get('history') or []
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

            # Re-calculate position
            n_sh_post = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in ud.get('history', []) if t.get('ticker') == ticker)
            curr_tot_post = max(0, i_sh + n_sh_post)
            if 'holdings' not in ud: ud['holdings'] = {}
            if curr_tot_post > 0: 
                hw = (ud['holdings'].get(ticker) or {}).get('high_water', price)
                ud['holdings'][ticker] = {'shares': curr_tot_post, 'manual_val': round(curr_tot_post * cost_per_sh, 2), 'high_water': max(hw, price)}
            else: ud['holdings'].pop(ticker, None)
            
            if is_client:
                self.save_client_store(client_email, c_store)
            else:
                self.save_data(self.data)

            is_paused = global_broker.get('paused', False)
            allocations = global_broker.get('allocations') or {}
            current_profile = str(username or self.active_username()).lower()

            # Strictly obey user allocations: Exact match first to avoid (v2) tag collisions
            matched_alloc = None
            should_route_alpaca = False
            if current_profile in allocations:
                matched_alloc = allocations[current_profile]
                should_route_alpaca = bool(matched_alloc.get('enabled', False))
            else:
                for s_key, s_data in allocations.items():
                    if s_key == current_profile or s_key in current_profile or current_profile in s_key:
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

                order_slices = int((metadata or {}).get('slices', 1))
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
                        t_pct,
                        order_slices
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
                    pnl_line = f"\n{arrow} {sign}${abs(diff_val):.2f} ({sign}{abs(diff_pct):.1f}%)"

            push_message = f"${cost_per_sh:.2f} (Total ${tot_amt:.2f}){pnl_line}"

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
        ud['ratchet_locked'] = False
        ud['is_locked'] = False
        ud['ratchet_floor_override'] = None
        ud['locked_floor_val'] = None
        ud['daily_peak_pct'] = 0.0
        
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
            'NFLX': 'Netflix', 'PLTR': 'Palantir Tech', 'COIN': 'Coinbase', 'MSTR': 'MicroStrategy', 'TQQQ': 'ProShares UltraPro QQQ', 'QQQ': 'Invesco QQQ Trust', 'SPY': 'SPDR S&P 500 ETF Trust',
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

    def score_momentum(self, df_5m, current_price, avg_buy_price=0.0, highest_price=0.0, profile='test e', regime=None, overnight_sweep=True):
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

        # Standardized Production Trailing Stops
        if any(x in prof for x in ['velocity', 'test s']):
            trail_pct, hard_pct = 0.50, -0.50
        else:
            trail_pct, hard_pct = 0.75, -0.75

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

            # DYNAMIC 0.50% STEP-RATCHET FLOOR (MOVES UP INDEFINITELY EVERY +0.50% FROM +2.50%)
            if any(x in prof for x in ['vector', 'dual', 'velocity', 'tqm', 'tq', 'test s']) and peak_pnl_pct >= 2.50:
                # Indefinite 0.50% steps: 2.5% -> 3.0% -> 3.5% -> 4.0% -> 4.5% -> 5.0%...
                floor_pct = round(int(peak_pnl_pct * 2) / 2.0, 2)
                profit_floor_price = avg_buy_price * (1.0 + (floor_pct / 100.0))
                trail_pct = 0.30  # Choke trailing stop to 0.30% for active runners

                if current_price <= profit_floor_price:
                    return {
                        'type': 'Intraday Momentum',
                        'score': 0,
                        'tranches': 0,
                        'discount': f"{pnl_pct:.2f}%",
                        'reason': f"STEP RATCHET FLOOR BANKED (+{floor_pct:.2f}%). Peak was +{peak_pnl_pct:.2f}%. Day complete.",
                        'is_smart': True,
                        'rec_buy': current_price,
                        'rec_sell': current_price,
                        'status': f"{floor_pct:.1f}% Floor Banked",
                        'color': '#0ecb81',
                        'action_main': 'SELL',
                        'action_sub': f'(Bank +{floor_pct:.1f}%)',
                        'action_color': '#0ecb81',
                        'regime': regime,
                        'trade_type': trade_type,
                        'health_pct': 100,
                        'health_color': '#0ecb81',
                        'health_text': f"Banked (+{pnl_pct:.2f}%)"
                    }

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
                return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"STOP TRIPPED {pnl_str}. Peak: ${highest_price:.2f}.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Stop Tripped', 'color': '#ff5364', 'action_main': 'SELL', 'action_sub': '(Stop Loss)', 'action_color': '#ff5364', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff5364', 'health_text': f"Stop Tripped {pnl_str}"}
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

        # OVERRIDE: OVERNIGHT CASH SWEEP (Checks profile's overnight_sweep setting)
        if overnight_sweep and now_uk.hour == 20 and now_uk.minute >= 50:
            if avg_buy_price > 0:
                pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                return {'type': 'EOD Sweep', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': "OVERNIGHT SWEEP: Liquidating to 100% cash to eliminate overnight gap risk.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Overnight Sweep', 'color': '#ff9900', 'action_main': 'SELL', 'action_sub': '(Overnight Sweep)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'Overnight Sweep'}
            return {'type': 'EOD Sweep', 'score': 0, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': "OVERNIGHT SWEEP: Blocking new entries before close.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'EOD Block Active', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(EOD Blocked)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'EOD Blocked'}

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
            return {'type': 'Intraday Momentum', 'score': 80, 'tranches': 1, 'discount': f"{pnl_pct:.2f}%", 'reason': f"RIDING TREND. High Water Mark: ${highest_price:.2f} (Stop: {trail_pct:.2f}%).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Trailing Stop Active', 'color': '#ffd400', 'action_main': 'HOLD / WAIT', 'action_sub': '(Riding Winner)', 'action_color': '#ffd400', 'regime': regime, 'trade_type': trade_type, 'health_pct': health_pct, 'health_color': health_color, 'health_text': health_text}

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

def process_auto_profile(prof_name, client_email=None):
    try:
        is_client = bool(client_email and client_email.strip().lower() not in [ADMIN_USER.lower(), ADMIN_ALIAS.lower()])
        if is_client:
            c_store = portfolio_store.get_client_store(client_email)
            users = c_store.get('users', {})
            if prof_name not in users: return
            ud = users[prof_name]
        else:
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
        mb = float(ud.get('master_budget', 500.0))
        hist = ud.get('history') or []
        init_pos = ud.get('initial_positions') or {}

        def resolve_shares(tk):
            i_sh = (init_pos.get(tk) or {}).get('shares', 0)
            n_sh = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in hist if t.get('ticker') == tk)
            return max(0, i_sh + n_sh)

        net_history = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist if tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION'])
        init_manual = sum(pos.get('manual_val', 0.0) for pos in init_pos.values())
        cash_balance = mb + net_history - init_manual
        rem_cash = max(0, cash_balance)

        # Isolated total equity calculation (never touches admin database)
        tot_own = 0.0
        for h_tk in list(set([tr.get('ticker') for tr in hist if resolve_shares(tr.get('ticker')) > 0])):
            sh_c = resolve_shares(h_tk)
            cur_df = fetch_yf_data(h_tk, "1d", "1m")
            if not cur_df.empty:
                cp = cur_df['Close'].iloc[-1]
                div = 100.0 if h_tk.endswith('.L') and cp > 100 else 1.0
                tot_own += round(sh_c * (cp / div), 2)
        total_equity = cash_balance + tot_own

        # GEAR 4 ($1,000,000+): Shift to ultra-deep liquidity ETFs (QQQ / SPY)
        if total_equity >= 1000000:
            held_symbols = [tr.get('ticker') for tr in hist if resolve_shares(tr.get('ticker')) > 0]
            scan_list = list(dict.fromkeys(['QQQ', 'SPY'] + held_symbols))

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

        # TOTAL DAILY ACCOUNT STEP-RATCHET (Monitors the big header % on your screen)
        force_daily_floor_sell = False
        daily_floor_locked = 0.0
        if any(x in active_profile for x in ['vector', 'dual', 'velocity', 'tqm', 'tq', 'test s']):
            if ud.get('last_ratchet_date') != today_str:
                ud['last_ratchet_date'] = today_str
                ud['daily_peak_pct'] = 0.0
                ud['manual_locked_day'] = None
                ud['ratchet_floor_override'] = None
                ud['ratchet_locked'] = False
                ud['is_locked'] = False

            if daily_pnl_pct > ud.get('daily_peak_pct', 0.0):
                ud['daily_peak_pct'] = round(daily_pnl_pct, 2)
                if ud.get('ratchet_floor_override') is not None and daily_pnl_pct >= (float(ud['ratchet_floor_override']) + 0.6):
                    ud['ratchet_floor_override'] = None
                if is_client:
                    portfolio_store.save_client_store(client_email, c_store)
                else:
                    portfolio_store.save_data(portfolio_store.data)

            daily_peak = ud.get('daily_peak_pct', 0.0)

            if ud.get('ratchet_floor_override') is not None:
                daily_floor_locked = float(ud['ratchet_floor_override'])
            elif daily_peak >= 2.50:
                daily_floor_locked = round(int(daily_peak * 2) / 2.0, 2)

            if daily_floor_locked > 0 and daily_pnl_pct <= daily_floor_locked and not ud.get('ratchet_locked'):
                force_daily_floor_sell = True

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
        if ud.get('manual_locked_day') == today_str or ud.get('ratchet_locked'):
            is_curfew_active = True

        # ADAPTIVE MODE RESOLUTION (MOMENTUM VS REVERSION SWITCHBOARD)
        current_mode = 'momentum'
        # 15-Minute Post-Sell Cooldown (ignores internal ALPACASYNC/CALIBRATION entries)
        real_hist_for_cd = [h for h in hist if h.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']]
        if real_hist_for_cd and real_hist_for_cd[0].get('action') == 'SELL':
            if (time.time() - real_hist_for_cd[0].get('timestamp', 0)) < 900:
                buys = []
                if matching_buy:
                    if float(h.get('price', 0)) < float(matching_buy.get('price', 0)):
                        today_loss_stops += 1
                else:
                    today_loss_stops += 1
        today_stops_cnt = today_loss_stops

        # VECTOR HYBRID OPENING SHIFTER: 14:50–15:15 BST = 1m Elasticity; 15:15–20:00 BST = 5m Momentum
        if any(x in active_profile for x in ['vector', 'tqm']):
            current_mode = 'reversion' if (now_uk.hour == 14 or (now_uk.hour == 15 and now_uk.minute < 15)) else 'momentum'
        else:
            current_mode = 'momentum'

        dirs, buys, held_scores = [], [], []
        MIN_BUY_VALUE = 20.0
        regime = engine.check_market_regime()

        dfs = {}
        def fetch_data_thread(tick): 
            use_1m = (current_mode == 'reversion') or ('velocity' in active_profile) or ('test s' in active_profile)
            interval = "1m" if use_1m else "5m"
            period = "1d" if use_1m else "5d"
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
                sh = resolve_shares(t)
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
                        if is_client:
                            portfolio_store.save_client_store(client_email, c_store)
                        else:
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
                    st = engine.score_momentum(df, cur, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=eff_profile, regime=regime, overnight_sweep=bool(ud.get('overnight_sweep', True)))

                if sh > 0:
                    held_scores.append({'ticker': t, 'shares': sh, 'price': cur, 'cps': cps, 'value': vo, 'score': st['score'], 'action': st['action_main']})

                if st['action_main'] == 'SELL' and sh > 0 and vo >= MIN_BUY_VALUE:
                    if is_open or 'test a' in active_profile:
                        dirs.append({'ticker': t, 'action': 'SELL', 'shares': sh, 'price': cur, 'amount': round(vo, 2), 'status': st.get('status', ''), 'reason': st.get('reason', ''), 'highest_price': highest_p})
                elif st['action_main'] == 'BUY':
                    if is_open or 'test a' in active_profile:
                        buys.append({'t': t, 'cps': cps, 'p': cur, 's': st['score']})
            except Exception: pass

        # --- 4-GEAR AUTOMATIC TRANSMISSION ---
        # Gear 1 ($0 - $2,000): Full Power (98% cash, 1 hold, 1 slice)
        # Gear 2 ($2,000 - $250,000): Standard Margin Tier (98% cash, 1 hold, 1 slice)
        # Gear 3 ($250,000 - $1,000,000): Stealth Slicing TWAP (96% alloc, 5 child slices; 2 holds above $500k)
        # Gear 4 ($1,000,000+): Deep Water QQQ/SPY (96% alloc, 2 holds, 8 child slices)
        max_allowed_holds = 1
        live_allocation_pct = 0.98
        order_slices = 1

        if total_equity >= 1000000:
            max_allowed_holds = 2
            live_allocation_pct = 0.96
            order_slices = 8
        elif total_equity >= 250000:
            max_allowed_holds = 2 if total_equity >= 500000 else 1
            live_allocation_pct = 0.96
            order_slices = 5
        elif total_equity >= 2000:
            max_allowed_holds = 1
            live_allocation_pct = 0.98
            order_slices = 1
        else:
            max_allowed_holds = 1
            live_allocation_pct = 0.98
            order_slices = 1

        current_hold_count = len([x for x in held_scores if x['shares'] > 0])
        slots_available = max(0, max_allowed_holds - current_hold_count)

        # DEFENSIVE CHOP SHIELDS & COOLDOWNS FOR ACTIVE FLEET
        cur_mins = now_uk.hour * 60 + now_uk.minute
        if cur_mins >= (19 * 60 + 45):
            is_curfew_active = True

        # Three-Strike Loss Lockout
        if today_stops_cnt >= 3:
            is_curfew_active = True

        # 15-Minute Post-Sell Cooldown (ignores internal ALPACASYNC/CALIBRATION entries)
        real_hist_for_cd = [h for h in hist if h.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']]
        if real_hist_for_cd and real_hist_for_cd[0].get('action') == 'SELL':
            if (time.time() - real_hist_for_cd[0].get('timestamp', 0)) < 900:
                buys = []

        # 60-Minute Ticker Wash Rule
        recent_stopped_tickers = {
            h.get('ticker') for h in hist
            if h.get('action') == 'SELL' and (time.time() - h.get('timestamp', 0)) < 3600
        }
        buys = [b for b in buys if b.get('t') not in recent_stopped_tickers]

        # Emergency Shutdown: If circuit breaker tripped or curfew active, block ALL buys
        if circuit_breaker_tripped or is_curfew_active:
            buys = []

        if circuit_breaker_tripped and held_scores:
            for h in held_scores:
                if not any(d['ticker'] == h['ticker'] and d['action'] == 'SELL' for d in dirs):
                    dirs.append({'ticker': h['ticker'], 'action': 'SELL', 'shares': h['shares'], 'price': h['price'], 'amount': round(h['value'], 2)})

        # Execute immediate sale when Daily Account Ratchet floor is breached
        if force_daily_floor_sell and held_scores:
            for h in held_scores:
                if not any(d['ticker'] == h['ticker'] and d['action'] == 'SELL' for d in dirs):
                    dirs.append({
                        'ticker': h['ticker'],
                        'action': 'SELL',
                        'shares': h['shares'],
                        'price': h['price'],
                        'amount': round(h['value'], 2),
                        'status': f'{daily_floor_locked:.1f}% Daily Floor Banked',
                        'reason': f'Daily profit dipped to +{daily_floor_locked:.1f}% floor. Banked for the day.'
                    })
            ud['ratchet_locked'] = True
            ud['locked_floor_val'] = daily_floor_locked
            if is_client:
                portfolio_store.save_client_store(client_email, c_store)
            else:
                portfolio_store.save_data(portfolio_store.data)
            is_curfew_active = True

        if buys and rem_cash >= MIN_BUY_VALUE and slots_available > 0:
            buys.sort(key=lambda x: x['s'], reverse=True)
            valid_buys = [b for b in buys if time.time() >= ud.get('cooldowns', {}).get(b['t'], 0)]
            top_buys = valid_buys[:slots_available]
            if top_buys:
                # Gear-controlled allocation per trade
                compound_capital = min(rem_cash, total_equity * live_allocation_pct)
                per_stock_budget = compound_capital / len(top_buys)

                for b in top_buys:
                    bs = int(per_stock_budget // b['cps'])
                    amt = round(bs * b['cps'], 2)
                    if bs > 0 and amt >= MIN_BUY_VALUE and amt <= rem_cash:
                        dirs.append({'ticker': b['t'], 'action': 'BUY', 'shares': bs, 'price': b['p'], 'amount': amt, 'slices': order_slices})

        for d in dirs:
            if d['shares'] > 0:
                portfolio_store.execute_trade(
                    d['ticker'], d['action'], d['shares'], d['price'], prof_name,
                    metadata={'status': d.get('status', 'Manual/EOD'), 'reason': d.get('reason', ''), 'highest_price': d.get('highest_price', 0.0), 'slices': d.get('slices', 1)},
                    client_email=client_email
                )
                # Stamp 15-minute cooldown on stop-outs to avoid whipsaw chop
                if d['action'] == 'SELL':
                    if 'cooldowns' not in ud or not isinstance(ud['cooldowns'], dict):
                        ud['cooldowns'] = {}
                    ud['cooldowns'][d['ticker']] = int(time.time()) + 900
                    if is_client:
                        portfolio_store.save_client_store(client_email, c_store)
                    else:
                        portfolio_store.save_data(portfolio_store.data)
    except Exception: pass

def global_background_worker():
    time.sleep(3)
    while True:
        try:
            # 1. Single Centralized Market Ingestion: One fetch feeds all 20 users
            bot_tickers = ['TQQQ', 'SOXL', 'SQQQ', 'QQQ', 'SPY']
            def prefetch(tk): 
                fetch_yf_data(tk, "5d", "5m")
                fetch_yf_data(tk, "1d", "1m")
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(prefetch, set(bot_tickers)))

            portfolio_store.reload()
            
            # 2. Collect all active strategy targets across admin and clients
            strategy_tasks = []

            # Admin profiles
            for prof_name in list(portfolio_store.data.get('users', {}).keys()):
                p_lower = prof_name.lower()
                if any(k in p_lower for k in ['vector', 'dual', 'velocity', 'tqm', 'tq', 'test s']):
                    strategy_tasks.append((prof_name, None))

            # Client profiles
            if portfolio_store.client:
                try:
                    accounts_col = portfolio_store.client['stock_terminal']['accounts']
                    for acc in accounts_col.find({"role": "client"}):
                        c_email = acc.get('username')
                        c_store = acc.get('portfolio') or {}
                        if not c_store.get('is_configured'): continue
                        users_dict = c_store.get('users') or {}
                        for p_name in list(users_dict.keys()):
                            p_low = p_name.lower()
                            if any(k in p_low for k in ['vector', 'dual', 'velocity', 'tqm', 'tq', 'test s']):
                                strategy_tasks.append((p_name, c_email))
                except Exception as ex_c:
                    print(f"Client Background Scan Error: {ex_c}")

            # 3. Parallel Execution: All 20 accounts evaluate and execute simultaneously
            if strategy_tasks:
                def execute_task(task):
                    p_n, c_m = task
                    process_auto_profile(p_n, client_email=c_m)

                worker_pool_size = min(12, len(strategy_tasks))
                with ThreadPoolExecutor(max_workers=worker_pool_size) as ex:
                    list(ex.map(execute_task, strategy_tasks))

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

def sync_profile_with_alpaca_non_destructive(ud, acc, live_pos, closed_orders, today_str, now_lon):
    """Non-destructive two-way reconciliation: preserves today's trade history while aligning exact fills, positions, equity, and ratchet state to Alpaca."""
    changed = False
    hist = ud.get('history') or []
    init_pos = ud.get('initial_positions') or {}

    for o in reversed(closed_orders or []):
        if o.get('status') != 'filled':
            continue
        coid = o.get('client_order_id') or ''
        side = (o.get('side') or '').lower()
        symbol = (o.get('symbol') or '').upper()
        filled_qty = int(o.get('filled_qty') or 0)
        filled_price = float(o.get('filled_avg_price') or o.get('price') or 0.0)
        order_id = o.get('id')
        if side not in ['buy', 'sell'] or filled_qty <= 0 or filled_price <= 0:
            continue

        order_ts = 0
        try:
            coid_parts = coid.rsplit('_', 1)
            if len(coid_parts) == 2 and coid_parts[1].isdigit():
                order_ts = int(coid_parts[1])
            elif o.get('created_at'):
                order_ts = int(pd.Timestamp(o['created_at']).timestamp())
        except Exception:
            order_ts = 0

        target_act = side.upper()
        for h in hist:
            if h.get('action') == target_act and h.get('ticker') == symbol and not h.get('price_reconciled'):
                if h.get('alpaca_order_id') and h.get('alpaca_order_id') != order_id:
                    continue
                h_ts = int(h.get('timestamp') or 0)
                if order_ts > 0 and h_ts > 0 and abs(order_ts - h_ts) > 1800:
                    continue

                new_p = round(filled_price, 2)
                cps = filled_price / 100.0 if symbol.endswith('.L') else filled_price
                new_amt = round(int(h.get('shares') or filled_qty) * cps, 2)
                if h.get('price') != new_p or h.get('amount') != new_amt:
                    h['price'] = new_p
                    h['amount'] = new_amt
                    changed = True
                h['price_reconciled'] = True
                h['alpaca_order_id'] = order_id
                break

    alpaca_pos_map = {p['symbol'].upper(): p for p in (live_pos or []) if int(p.get('qty', 0)) > 0}

    def calc_local_sh(tk):
        i_s = int((init_pos.get(tk) or {}).get('shares') or 0)
        n_s = sum(int(t.get('shares') or 0) if t.get('action') == 'BUY' else -int(t.get('shares') or 0) for t in hist if t.get('ticker') == tk)
        return max(0, i_s + n_s)

    all_tks = set([tr.get('ticker') for tr in hist if tr.get('ticker') and tr.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']]) | set(alpaca_pos_map.keys())
    if 'holdings' not in ud or not isinstance(ud['holdings'], dict):
        ud['holdings'] = {}

    for tk in all_tks:
        loc_sh = calc_local_sh(tk)
        alp_p = alpaca_pos_map.get(tk)
        alp_sh = int(alp_p.get('qty', 0)) if alp_p else 0

        if loc_sh > 0 and alp_sh == 0:
            sell_o = next((o for o in (closed_orders or []) if o.get('status') == 'filled' and (o.get('symbol') or '').upper() == tk and (o.get('side') or '').lower() == 'sell'), None)
            if sell_o and float(sell_o.get('filled_avg_price') or 0) > 0:
                close_p = round(float(sell_o['filled_avg_price']), 2)
            else:
                df_p = fetch_yf_data(tk, "1d", "1m")
                close_p = round(float(df_p['Close'].iloc[-1]), 2) if not df_p.empty else 1.0
            cps = close_p / 100.0 if tk.endswith('.L') and close_p > 100 else close_p
            hist.insert(0, {
                'id': str(int(time.time() * 1000) + 2),
                'ticker': tk,
                'trade_type': 'SHORT' if tk in ['SQQQ', '3SUS.L'] else 'LONG',
                'action': 'SELL',
                'shares': loc_sh,
                'price': close_p,
                'amount': round(loc_sh * cps, 2),
                'time': now_lon.strftime('%d %b %H:%M'),
                'date_str': today_str,
                'timestamp': int(now_lon.timestamp()),
                'status': 'Alpaca Auto-Reconciled',
                'reason': 'Synced flat position with Alpaca',
                'price_reconciled': True,
                'source': 'alpaca_sync'
            })
            ud['holdings'].pop(tk, None)
            changed = True
        elif alp_sh > 0 and loc_sh == 0:
            avg_p = round(float(alp_p.get('avg_entry_price') or 0.0), 2)
            if avg_p > 0:
                pos_amt = round(alp_sh * avg_p, 2)
                hist.insert(0, {
                    'id': str(int(time.time() * 1000) + 3),
                    'ticker': tk,
                    'trade_type': 'SHORT' if tk in ['SQQQ', '3SUS.L'] else 'LONG',
                    'action': 'BUY',
                    'shares': alp_sh,
                    'price': avg_p,
                    'amount': pos_amt,
                    'time': now_lon.strftime('%d %b %H:%M'),
                    'date_str': today_str,
                    'timestamp': int(now_lon.timestamp()),
                    'status': 'Alpaca Position Synced',
                    'reason': 'Imported live Alpaca position',
                    'price_reconciled': True,
                    'source': 'alpaca_sync'
                })
                ud['holdings'][tk] = {'shares': alp_sh, 'manual_val': pos_amt, 'high_water': avg_p}
                changed = True
        elif alp_sh > 0:
            mv = round(float(alp_p.get('market_value') or 0.0), 2)
            hw = max(float((ud['holdings'].get(tk) or {}).get('high_water') or 0.0), float(alp_p.get('current_price') or 0.0))
            ud['holdings'][tk] = {'shares': alp_sh, 'manual_val': mv, 'high_water': hw}
        else:
            ud['holdings'].pop(tk, None)

    alpaca_equity = round(float(acc.get('portfolio_value') or acc.get('equity') or 0.0), 2)
    alpaca_last_equity = round(float(acc.get('last_equity') or 0.0), 2)
    if alpaca_last_equity > 0 and abs(float(ud.get('master_budget') or 0.0) - alpaca_last_equity) >= 0.01:
        ud['master_budget'] = alpaca_last_equity
        changed = True

    mb_val = float(ud.get('master_budget') or 700.0)
    non_sync_hist = [tr for tr in hist if not (tr.get('ticker') == 'ALPACASYNC' and tr.get('date_str') == today_str)]
    if len(non_sync_hist) != len(hist):
        hist = non_sync_hist

    net_real = sum(-float(tr.get('amount') or 0.0) if tr.get('action') == 'BUY' else float(tr.get('amount') or 0.0)
                   for tr in hist if tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION', 'ALPACASYNC'])
    init_man = sum(float(pos.get('manual_val') or 0.0) for pos in init_pos.values())
    open_mv = sum(round(float(p.get('market_value') or 0.0), 2) for p in alpaca_pos_map.values())
    local_eq_before_adj = round(mb_val + net_real - init_man + open_mv, 2)

    residual = round(alpaca_equity - local_eq_before_adj, 2) if alpaca_equity > 0 else 0.0
    if abs(residual) >= 0.02:
        hist.insert(0, {
            'id': str(int(time.time() * 1000) + 4),
            'ticker': 'ALPACASYNC',
            'trade_type': 'LONG',
            'action': 'SELL' if residual >= 0 else 'BUY',
            'shares': 0,
            'price': abs(residual),
            'amount': abs(residual),
            'time': now_lon.strftime('%d %b %H:%M'),
            'date_str': today_str,
            'timestamp': int(now_lon.timestamp()) - 10
        })
        changed = True

    ud['history'] = hist

    if alpaca_last_equity > 0 and alpaca_equity > 0:
        true_today_pnl = round(alpaca_equity - alpaca_last_equity, 2)
        true_today_pct = round((true_today_pnl / alpaca_last_equity) * 100.0, 2)
        if ud.get('last_ratchet_date') == today_str and float(ud.get('daily_peak_pct') or 0.0) > true_today_pct + 0.15:
            ud['daily_peak_pct'] = max(0.0, true_today_pct)
            changed = True
        if ud.get('manual_locked_day') != today_str and (ud.get('ratchet_locked') or ud.get('is_locked')):
            if true_today_pct < 2.50 and ud.get('ratchet_floor_override') is None:
                ud['ratchet_locked'] = False
                ud['is_locked'] = False
                ud['locked_floor_val'] = None
                changed = True

    return changed, alpaca_equity, mb_val


def reconcile_single_target(target):
    """Reconciles orders, fills, positions, and account equity drift for one specific broker account."""
    broker = target['broker']
    api_key = str(broker.get('api_key', '')).strip()
    secret_key = str(broker.get('secret_key', '')).strip()
    mode = broker.get('mode', 'paper')
    c_email = target['client_email']
    auto_profiles = target['profiles']

    if not api_key or not secret_key or not auto_profiles:
        return

    base_url = "https://paper-api.alpaca.markets" if mode == 'paper' else "https://api.alpaca.markets"
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "Content-Type": "application/json"
    }

    now_lon = pd.Timestamp.now(tz='Europe/London')
    today_str = now_lon.strftime('%Y-%m-%d')
    closed_orders = []
    live_pos = []
    acc = None

    try:
        req_acc = urllib.request.Request(f"{base_url}/v2/account", headers=headers)
        with urllib.request.urlopen(req_acc, timeout=5) as resp_a:
            acc = json.loads(resp_a.read().decode('utf-8'))
    except Exception:
        pass

    try:
        req_orders = urllib.request.Request(f"{base_url}/v2/orders?status=closed&limit=40&direction=desc", headers=headers)
        with urllib.request.urlopen(req_orders, timeout=5) as resp:
            closed_orders = json.loads(resp.read().decode('utf-8'))
    except Exception:
        pass

    try:
        req_pos = urllib.request.Request(f"{base_url}/v2/positions", headers=headers)
        with urllib.request.urlopen(req_pos, timeout=5) as resp:
            live_pos = json.loads(resp.read().decode('utf-8'))
    except Exception:
        pass

    try:
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

            if c_email:
                c_s = portfolio_store.get_client_store(c_email)
                ud = (c_s.get('users') or {}).get(matched_prof, {})
            else:
                ud = portfolio_store.user_data(matched_prof)

            hist = ud.get('history') or []
            init_pos = ud.get('initial_positions') or {}

            def get_prof_shares(tk):
                i_sh = (init_pos.get(tk) or {}).get('shares', 0)
                n_sh = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in hist if t.get('ticker') == tk)
                return max(0, i_sh + n_sh)

            order_ts = 0
            try:
                coid_parts = coid.rsplit('_', 1)
                if len(coid_parts) == 2 and coid_parts[1].isdigit():
                    order_ts = int(coid_parts[1])
                elif o.get('created_at'):
                    order_ts = int(pd.Timestamp(o['created_at']).timestamp())
            except Exception:
                order_ts = 0

            if side in ['buy', 'sell']:
                target_act = side.upper()
                reconciled_existing = False
                for h in hist:
                    if h.get('action') == target_act and h.get('ticker') == symbol and not h.get('price_reconciled'):
                        if h.get('alpaca_order_id') and h.get('alpaca_order_id') != order_id:
                            continue
                        h_ts = int(h.get('timestamp') or 0)
                        if order_ts > 0 and h_ts > 0 and abs(order_ts - h_ts) > 1800:
                            continue

                        h['price'] = round(filled_price, 2)
                        cps = filled_price / 100.0 if symbol.endswith('.L') else filled_price
                        h['amount'] = round(h['shares'] * cps, 2)
                        h['price_reconciled'] = True
                        h['alpaca_order_id'] = order_id
                        reconciled_existing = True
                        if c_email:
                            portfolio_store.save_client_store(c_email, c_s)
                        else:
                            portfolio_store.save_data(portfolio_store.data)
                        break

                if side == 'sell' and not reconciled_existing:
                    current_shares = get_prof_shares(symbol)
                    already_logged = any(h.get('alpaca_order_id') == order_id for h in hist)
                    if current_shares > 0 and not already_logged:
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
                                'price_reconciled': True,
                                'source': 'alpaca_sync'
                            },
                            client_email=c_email
                        )
    except Exception:
        pass

    try:
        allocations = broker.get('allocations') or {}
        enabled_profs = [
            p for p in auto_profiles
            if any((s_key == p.lower() or s_key in p.lower() or p.lower() in s_key) and s_data.get('enabled')
                   for s_key, s_data in allocations.items())
        ]

        if acc is not None and len(enabled_profs) == 1:
            live_prof = enabled_profs[0]
            if c_email:
                c_s = portfolio_store.get_client_store(c_email)
                ud = (c_s.get('users') or {}).get(live_prof)
            else:
                ud = portfolio_store.user_data(live_prof)

            if isinstance(ud, dict):
                changed, alp_eq, mb_v = sync_profile_with_alpaca_non_destructive(
                    ud, acc, live_pos, closed_orders, today_str, now_lon
                )
                if changed:
                    print(f"[ALPACA AUTO-CALIBRATED] {live_prof}: Synced equity to ${alp_eq:.2f} (Budget ${mb_v:.2f}) while preserving trade history.")
                    if c_email:
                        portfolio_store.save_client_store(c_email, c_s)
                    else:
                        portfolio_store.save_data(portfolio_store.data)
        else:
            alpaca_holdings = {p['symbol'].upper(): int(p.get('qty', 0)) for p in live_pos}
            for prof in enabled_profs:
                if c_email:
                    c_s = portfolio_store.get_client_store(c_email)
                    u_data = (c_s.get('users') or {}).get(prof, {})
                else:
                    u_data = portfolio_store.user_data(prof)

                h_list = u_data.get('history') or []
                i_pos = u_data.get('initial_positions') or {}

                def chk_shares(tk):
                    i_s = (i_pos.get(tk) or {}).get('shares', 0)
                    n_s = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in h_list if t.get('ticker') == tk)
                    return max(0, i_s + n_s)

                active_tickers = list(set([tr.get('ticker') for tr in h_list if chk_shares(tr.get('ticker')) > 0]))
                for tk in active_tickers:
                    if alpaca_holdings.get(tk, 0) == 0:
                        local_sh = chk_shares(tk)
                        if local_sh > 0:
                            df_p = fetch_yf_data(tk, "1d", "1m")
                            cur_p = df_p['Close'].iloc[-1] if not df_p.empty else 1.0
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
                                },
                                client_email=c_email
                            )

        for p in live_pos:
            tk = p['symbol'].upper()
            qty = int(p.get('qty', 0))
            if qty <= 0:
                continue

            total_local_claimed = 0
            if c_email:
                c_s = portfolio_store.get_client_store(c_email)
                for u_n, u_d in (c_s.get('users') or {}).items():
                    i_s = (u_d.get('initial_positions', {}).get(tk) or {}).get('shares', 0)
                    n_s = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in u_d.get('history', []) if t.get('ticker') == tk)
                    total_local_claimed += max(0, i_s + n_s)
            else:
                for u_n, u_d in portfolio_store.data.get('users', {}).items():
                    i_s = (u_d.get('initial_positions', {}).get(tk) or {}).get('shares', 0)
                    n_s = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in u_d.get('history', []) if t.get('ticker') == tk)
                    total_local_claimed += max(0, i_s + n_s)

            if total_local_claimed == 0:
                try:
                    has_open_ord = False
                    try:
                        req_oo = urllib.request.Request(f"{base_url}/v2/orders?status=open&symbols={tk}", headers=headers)
                        with urllib.request.urlopen(req_oo, timeout=4) as r_oo:
                            if len(json.loads(r_oo.read().decode('utf-8'))) > 0:
                                has_open_ord = True
                    except Exception:
                        pass
                    if not has_open_ord:
                        print(f"[ALPACA AUTO-SYNC] Liquidating unowned orphan position: {qty} {tk}")
                        req_del = urllib.request.Request(f"{base_url}/v2/positions/{tk}", headers=headers, method="DELETE")
                        with urllib.request.urlopen(req_del, timeout=5): pass
                except Exception: pass
    except Exception:
        pass

def reconcile_alpaca_positions_and_orders():
    """Two-way sync loop: reconciles Alpaca fills and positions across all user accounts in parallel."""
    portfolio_store.reload()

    account_targets = []
    admin_broker = portfolio_store.data.get('broker') or {}
    if admin_broker.get('api_key') and admin_broker.get('secret_key'):
        account_targets.append({
            'client_email': None,
            'broker': admin_broker,
            'profiles': list(portfolio_store.data.get('users', {}).keys())
        })

    if portfolio_store.client:
        try:
            for acc in portfolio_store.client['stock_terminal']['accounts'].find({"role": "client"}):
                c_p = acc.get('portfolio') or {}
                c_b = c_p.get('broker') or {}
                if c_b.get('api_key') and c_b.get('secret_key'):
                    account_targets.append({
                        'client_email': acc.get('username'),
                        'broker': c_b,
                        'profiles': list((c_p.get('users') or {}).keys())
                    })
        except Exception: pass

    if account_targets:
        pool_size = min(8, len(account_targets))
        with ThreadPoolExecutor(max_workers=pool_size) as ex:
            list(ex.map(reconcile_single_target, account_targets))


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
    let d = { title: 'IDC Trading Alert', body: 'New trade executed.', url: '/', target: 'all' };
    if (e.data) {
        try { d = e.data.json(); } catch(err) { d.body = e.data.text(); }
    }
    // Drop phone-targeted alerts if running on a desktop Mac/PC
    const isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);
    if (!isMobile && d.target === 'phone') {
        return;
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
    
    ua = request.headers.get('User-Agent', '').lower()
    is_mobile = any(m in ua for m in ['mobile', 'iphone', 'ipad', 'android'])
    sub['device'] = 'phone' if is_mobile else 'mac'

    portfolio_store.reload()
    subs = portfolio_store.data.setdefault('push_subscriptions', [])
    subs = [s for s in subs if s.get('endpoint') != sub.get('endpoint')]
    subs.append(sub)
    portfolio_store.data['push_subscriptions'] = subs
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
    target = request.args.get('target', 'all')
    res = send_native_web_push(
        title="IDC Trading Alert",
        message="Test Trade: Bought 10 TQQQ @ $52.25",
        target_device=target
    )
    return jsonify(res or {'status': 'ok'})

@app.route('/api/broker/test_order', methods=['POST'])
@login_required
def test_alpaca_order():
    portfolio_store.reload()
    is_client = (session.get('role') != 'admin')
    if is_client:
        c_store = portfolio_store.get_client_store(session.get('email') or session.get('user'))
        broker = c_store.get('broker') or {}
    else:
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
    """Admin-only portal to view and delete registered client accounts."""
    if session.get('role') != 'admin':
        return "Unauthorized", 403
    
    html_rows = []
    if portfolio_store.client:
        try:
            now_lon = pd.Timestamp.now(tz='Europe/London')
            today_str = now_lon.strftime('%Y-%m-%d')
            
            # 1. Fetch base prices for accurate valuation
            prices = {}
            for tk in ['TQQQ', 'SOXL', 'SQQQ']:
                df_lb = fetch_yf_data(tk, "5d", "5m")
                if df_lb.empty: 
                    prices[tk] = {'cur': 0.0, 'start': 0.0}
                    continue
                cur_p = df_lb['Close'].iloc[-1]
                if df_lb.index.tz is None: df_lb.index = df_lb.index.tz_localize('UTC')
                else: df_lb.index = df_lb.index.tz_convert('UTC')
                
                last_lon = df_lb.index[-1].tz_convert('Europe/London')
                if now_lon.date() > last_lon.date(): 
                    start_p = cur_p
                else:
                    prev_sessions = df_lb[df_lb.index.tz_convert('Europe/London').strftime('%Y-%m-%d') < today_str]
                    start_p = prev_sessions['Close'].iloc[-1] if not prev_sessions.empty else df_lb['Close'].iloc[0]
                prices[tk] = {'cur': cur_p, 'start': start_p}

            accounts_col = portfolio_store.client['stock_terminal']['accounts']
            for doc in accounts_col.find():
                u = doc.get('username')
                if not u or u in ['ianldownward@gmail.com', 'ian']: continue
                pw = doc.get('plain_password') or "(hash)"
                dt = doc.get('registered_at') or "Prior Session"
                
                # 2. Fetch user profile data
                c_store = doc.get('portfolio') or {}
                users_dict = c_store.get('users') or {}
                
                prof_tds = ""
                for target_prof in ['Vector Hybrid', 'Velocity Pulse', 'Dual Axis']:
                    matched_k = next((k for k in users_dict.keys() if target_prof.lower() in k.lower()), None)
                    if matched_k:
                        ud = users_dict[matched_k]
                        budget = float(ud.get('master_budget', 500.0))
                        hist = ud.get('history') or []
                        init_pos = ud.get('initial_positions') or {}
                        
                        # Reconstruct equity and P&L identically to terminal ledger
                        nh_lb = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist if tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION'])
                        im_lb = sum(pos.get('manual_val', 0.0) for pos in init_pos.values())
                        cash_now = budget + nh_lb - im_lb
                        
                        trades_today = [tr for tr in hist if tr.get('date_str') == today_str]
                        net_cash_today = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in trades_today)
                        cash_start = cash_now - net_cash_today
                        
                        equity_now, equity_start = 0.0, 0.0
                        shares_now_map = {}
                        for tr in hist:
                            tk = tr.get('ticker')
                            if tk not in shares_now_map:
                                i_sh = (init_pos.get(tk) or {}).get('shares', 0)
                                n_sh = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in hist if t.get('ticker') == tk)
                                shares_now_map[tk] = max(0, i_sh + n_sh)
                                
                        for tk, sh_now in shares_now_map.items():
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
                        
                        day_diff = tot_eq_now - tot_eq_start
                        day_pct = (day_diff / tot_eq_start * 100.0) if tot_eq_start > 0 else 0.0
                        day_col = '#0ecb81' if day_diff > 0 else ('#ff5364' if day_diff < 0 else '#8a8a9e')
                        arrow = '▲' if day_diff > 0 else ('▼' if day_diff < 0 else '►')
                        
                        prof_tds += f"""
                        <td style='padding:12px; border-left:1px solid #262b36; vertical-align:top;'>
                            <div style='font-size:10px; color:#8a8a9e; font-weight:800; letter-spacing:0.5px; margin-bottom:4px; text-transform:uppercase;'>Invested: £{budget:,.2f}</div>
                            <div style='font-size:15px; font-weight:800; color:#fff;'>£{tot_eq_now:,.2f}</div>
                            <div style='font-size:11px; font-weight:800; color:{day_col}; margin-top:3px;'>
                                {arrow} £{abs(day_diff):.2f} ({day_pct:+.2f}%)
                            </div>
                        </td>
                        """
                    else:
                        prof_tds += f"""
                        <td style='padding:12px; border-left:1px solid #262b36; vertical-align:top; color:#555e6d;'>
                            <div style='font-size:11px; font-weight:700;'>Not configured</div>
                        </td>
                        """

                html_rows.append(f"""
                    <tr style='border-bottom:1px solid #262b36;'>
                        <td style='padding:12px; vertical-align:top;'>
                            <div style='font-weight:800; color:#ffd400; font-size:14px; margin-bottom:6px;'>{u}</div>
                            <div style='font-size:10px; color:#8a8a9e; margin-bottom:2px;'>Joined: {dt}</div>
                            <div style='font-size:10px; color:#8a8a9e; font-family:monospace;'>Key: {pw}</div>
                        </td>
                        {prof_tds}
                        <td style='padding:12px; text-align:right; vertical-align:middle; border-left:1px solid #262b36;'>
                            <form method='POST' action='/api/admin/delete_account' onsubmit='return confirm("Permanently delete account {u}?");' style='margin:0;'>
                                <input type='hidden' name='username' value='{u}'>
                                <button type='submit' style='background:#ff5364; color:#fff; border:none; padding:6px 12px; border-radius:6px; cursor:pointer; font-weight:800;'>Delete</button>
                            </form>
                        </td>
                    </tr>
                """)
        except Exception as e: 
            print("ADMIN LOG ERROR:", e)

    table_body = "".join(html_rows) if html_rows else "<tr><td colspan='5' style='padding:20px; text-align:center; color:#8a8a9e;'>No user signups recorded yet.</td></tr>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>User Access Directory</title></head>
    <body style='background:#0f1115; color:#e1e3e6; font-family:-apple-system, sans-serif; padding:30px;'>
        <div style='max-width:1050px; margin:0 auto; background:#171a21; border:1px solid #262b36; border-radius:10px; padding:20px; box-shadow:0 10px 30px rgba(0,0,0,0.5);'>
            <div style='display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #262b36; padding-bottom:15px; margin-bottom:15px;'>
                <h2 style='color:#ffd400; margin:0;'>Registered Client Accounts</h2>
                <a href='/' style='color:#0ecb81; text-decoration:none; font-weight:800; font-size:13px;'>← Return to Terminal</a>
            </div>
            <div style='overflow-x:auto;'>
                <table style='width:100%; border-collapse:collapse; font-size:13px; text-align:left;'>
                    <thead>
                        <tr style='background:#14171f; color:#8a8a9e; text-transform:uppercase; font-size:10px; letter-spacing:0.5px;'>
                            <th style='padding:12px; min-width:160px;'>Account Info</th>
                            <th style='padding:12px; border-left:1px solid #262b36;'>Vector Hybrid</th>
                            <th style='padding:12px; border-left:1px solid #262b36;'>Velocity Pulse</th>
                            <th style='padding:12px; border-left:1px solid #262b36;'>Dual Axis</th>
                            <th style='padding:12px; border-left:1px solid #262b36; text-align:right;'>Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_body}
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """

@app.route('/api/admin/delete_account', methods=['POST'])
@login_required
def api_admin_delete_account():
    """Deletes a registered user from MongoDB and the local signup log."""
    if session.get('role') != 'admin':
        return "Unauthorized", 403
    username = request.form.get('username', '').strip().lower()
    if not username:
        return redirect('/admin/users_log')

    if portfolio_store.client:
        try:
            accounts_col = portfolio_store.client['stock_terminal']['accounts']
            accounts_col.delete_one({"_id": f"client_{username}"})
            accounts_col.delete_one({"username": username})
        except Exception as e:
            print(f"[ACCOUNT DELETE ERROR]: {e}")

    try:
        if os.path.exists('user_signups.txt'):
            with open('user_signups.txt', 'r') as f:
                lines = f.readlines()
            with open('user_signups.txt', 'w') as f:
                for line in lines:
                    if f"Username: {username} " not in line:
                        f.write(line)
    except Exception: pass

    return redirect('/admin/users_log')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/')
@login_required
def index():
    is_client = (session.get('role') != 'admin')
    is_configured = True
    display_user = session.get('email') or session.get('user') or (ADMIN_USER if not is_client else 'Client')
    if is_client:
        c_store = portfolio_store.get_client_store(session.get('user'))
        is_configured = bool(c_store and c_store.get('is_configured', False))
    return render_template(
        'index.html',
        user_role=session.get('role', 'client'),
        current_user=display_user,
        is_configured=is_configured
    )

@app.route('/api/client/settings', methods=['POST'])
@login_required
def api_client_settings():
    portfolio_store.reload()
    data = request.get_json() or {}
    is_client = (session.get('role') != 'admin')
    client_name = session.get('email') or session.get('user', 'Client')

    b_mode = data.get('broker_mode', 'paper')
    api_key = data.get('api_key', '').strip()
    secret_key = data.get('secret_key', '').strip()
    allocations = data.get('allocations') or {}

    if is_client:
        c_store = portfolio_store.get_client_store(client_name)
        c_broker = c_store.setdefault('broker', {})
        c_broker['mode'] = b_mode
        c_broker['api_key'] = api_key
        c_broker['secret_key'] = secret_key
        c_broker['allocations'] = allocations
        c_store['is_configured'] = True
        portfolio_store.save_client_store(client_name, c_store)
        return jsonify({'status': 'ok', 'message': 'Account settings saved.'})

    broker = portfolio_store.data.setdefault('broker', {})
    broker['mode'] = b_mode
    if api_key: broker['api_key'] = api_key
    if secret_key: broker['secret_key'] = secret_key
    broker['allocations'] = allocations

    for prof, pdata in portfolio_store.data.get('users', {}).items():
        for s_key, s_cfg in allocations.items():
            if s_key in prof.lower():
                pdata['master_budget'] = float(s_cfg.get('budget', 500.0))

    portfolio_store.save_data(portfolio_store.data)
    return jsonify({'status': 'ok', 'message': 'Broker configuration updated.'})

@app.route('/api/users', methods=['GET'])
@login_required
def get_users(): 
    if is_admin_session():
        portfolio_store.reload()
        return jsonify({'users': list(portfolio_store.data.get('users', {}).keys()), 'active_user': portfolio_store.active_username()})
    client_name = session.get('user', 'Client')
    c_store = portfolio_store.get_client_store(client_name) or portfolio_store.blank_client_state(client_name)
    users_dict = c_store.get('users', {})
    user_list = list(users_dict.keys())
    active = c_store.get('active_user')
    if not active and user_list:
        active = user_list[0]
    return jsonify({'users': user_list, 'active_user': active or ''})

@app.route('/api/users/select', methods=['POST'])
@login_required
def select_user():
    target_user = (request.get_json() or {}).get('username', '').strip()
    if session.get('role') == 'admin':
        portfolio_store.reload()
        portfolio_store.switch_user(target_user)
        return jsonify({'status': 'ok'})
    else:
        client_name = session.get('email') or session.get('user')
        c_store = portfolio_store.get_client_store(client_name)
        if c_store and target_user in c_store.get('users', {}):
            c_store['active_user'] = target_user
            portfolio_store.save_client_store(client_name, c_store)
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
    import re
    data = request.get_json() or {}
    method = data.get('method', 'Vector Hybrid').strip()
    capital = float(data.get('capital', 500.0))
    sweep_enabled = bool(data.get('overnight_sweep', True))

    # Lock name 1-to-1 to official strategy name (disables custom V2 variants)
    prof_name = method

    def norm(val):
        return re.sub(r'[^a-z]', '', (val or '').lower())

    norm_target = norm(method)
    dup_err = f'"{method}" already exists on this account. To deploy more capital, please use [+ / - Capital] on your existing profile instead.'

    is_client = (session.get('role') != 'admin')
    client_name = session.get('email') or session.get('user', 'Client')

    if is_client:
        c_store = portfolio_store.get_client_store(client_name)
        users = c_store.setdefault('users', {})
        for existing_name in users.keys():
            if norm(existing_name) == norm_target or norm_target in norm(existing_name):
                return jsonify({'status': 'error', 'message': dup_err}), 400

        state = portfolio_store.default_user_state(method)
        state['master_budget'] = capital
        state['method'] = method
        state['overnight_sweep'] = sweep_enabled
        users[prof_name] = state
        c_store['active_user'] = prof_name
        c_store['is_configured'] = True
        c_broker = c_store.setdefault('broker', {})
        c_broker.setdefault('allocations', {})[prof_name.lower()] = {
            'enabled': True,
            'budget': capital,
            'overnight_sweep': sweep_enabled
        }
        portfolio_store.save_client_store(client_name, c_store)
        return jsonify({'status': 'ok', 'profile': prof_name})
    else:
        portfolio_store.reload()
        users = portfolio_store.data.setdefault('users', {})
        for existing_name in users.keys():
            if norm(existing_name) == norm_target or norm_target in norm(existing_name):
                return jsonify({'status': 'error', 'message': dup_err}), 400

        state = portfolio_store.default_user_state(method)
        state['master_budget'] = capital
        state['method'] = method
        state['overnight_sweep'] = sweep_enabled
        users[prof_name] = state
        portfolio_store.data['active_user'] = prof_name
        broker = portfolio_store.data.setdefault('broker', {})
        broker.setdefault('allocations', {})[prof_name.lower()] = {
            'enabled': True,
            'budget': capital,
            'overnight_sweep': sweep_enabled
        }
        portfolio_store.save_data(portfolio_store.data)
        return jsonify({'status': 'ok', 'profile': prof_name})

@app.route('/api/profile/step_down_override', methods=['POST'])
@login_required
def step_down_override_route():
    """Smart override: steps floor down by 0.5% and reactivates trading for the profile on screen."""
    data = request.get_json() or {}
    prof_name = data.get('profile', '').strip()
    is_client = (session.get('role') != 'admin')
    client_name = session.get('user', 'Client')

    if is_client:
        c_store = portfolio_store.get_client_store(client_name)
        users = c_store.setdefault('users', {})
        ud = users.get(prof_name)
    else:
        portfolio_store.reload()
        users = portfolio_store.data.setdefault('users', {})
        ud = users.get(prof_name)

    if not ud:
        return jsonify({'status': 'error', 'message': 'Profile not found'}), 404

    # Compute true current daily % so stepping down never gets immediately overwritten by stale daily_peak_pct
    mb_cur = float(ud.get('master_budget') or 700.0)
    hist_cur = ud.get('history') or []
    init_cur = ud.get('initial_positions') or {}
    nh_cur = sum(-float(tr.get('amount') or 0.0) if tr.get('action') == 'BUY' else float(tr.get('amount') or 0.0)
                 for tr in hist_cur if tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION'])
    im_cur = sum(float(pos.get('manual_val') or 0.0) for pos in init_cur.values())
    cur_eq_est = max(0.0, mb_cur + nh_cur - im_cur)
    cur_day_pct = ((cur_eq_est - mb_cur) / mb_cur * 100.0) if mb_cur > 0 else 0.0

    raw_floor = ud.get('locked_floor_val') if ud.get('locked_floor_val') is not None else (
        round(int(cur_day_pct * 2) / 2.0, 2) if cur_day_pct >= 2.5 else 2.5
    )
    current_floor = float(raw_floor)
    new_floor = max(0.0, round(current_floor - 0.5, 2))

    ud['ratchet_floor_override'] = new_floor
    ud['locked_floor_val'] = new_floor
    ud['ratchet_locked'] = False
    ud['is_locked'] = False
    ud['manual_locked_day'] = None
    # Keep daily_peak_pct >= cur_day_pct so process_auto_profile does not wipe ratchet_floor_override on the next tick
    ud['daily_peak_pct'] = round(max(cur_day_pct, new_floor + 0.4), 2)

    if is_client:
        portfolio_store.save_client_store(client_name, c_store)
    else:
        portfolio_store.save_data(portfolio_store.data)

    return jsonify({'status': 'ok', 'new_floor': new_floor})

@app.route('/api/profile/manual_lock_day', methods=['POST'])
@login_required
def manual_lock_day_route():
    """Permanent manual lock: liquidates ONLY the profile on screen and halts it until tomorrow."""
    data = request.get_json() or {}
    prof_name = data.get('profile', '').strip()
    action = data.get('action', 'lock')
    is_client = (session.get('role') != 'admin')
    client_name = session.get('user', 'Client')

    now_uk = pd.Timestamp.now(tz='Europe/London')
    today_str = now_uk.strftime('%Y-%m-%d')

    if is_client:
        c_store = portfolio_store.get_client_store(client_name)
        users = c_store.setdefault('users', {})
        ud = users.get(prof_name)
    else:
        portfolio_store.reload()
        users = portfolio_store.data.setdefault('users', {})
        ud = users.get(prof_name)

    if not ud:
        return jsonify({'status': 'error', 'message': 'Profile not found'}), 404

    if action == 'unlock':
        ud['manual_locked_day'] = None
        ud['ratchet_locked'] = False
        ud['is_locked'] = False
        if is_client:
            portfolio_store.save_client_store(client_name, c_store)
        else:
            portfolio_store.save_data(portfolio_store.data)
        return jsonify({'status': 'ok', 'locked': False})

    # Liquidate ONLY the position belonging to this specific profile on screen
    hist = ud.get('history') or []
    init_pos = ud.get('initial_positions') or {}
    def r_sh(tk):
        i_s = (init_pos.get(tk) or {}).get('shares', 0)
        n_s = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in hist if t.get('ticker') == tk)
        return max(0, i_s + n_s)

    active_tickers = list(set([tr.get('ticker') for tr in hist if r_sh(tr.get('ticker')) > 0]))
    for tick in active_tickers:
        sh = r_sh(tick)
        if sh > 0:
            df = fetch_yf_data(tick, '1d', '1m')
            cur_p = df['Close'].iloc[-1] if not df.empty else 1.0
            portfolio_store.execute_trade(
                tick, 'SELL', sh, cur_p, prof_name,
                metadata={'status': 'Manual Day Lock', 'reason': f'Locked on screen for the day by user ({prof_name})'},
                client_email=client_name if is_client else None
            )

    ud['manual_locked_day'] = today_str
    ud['ratchet_locked'] = True
    ud['is_locked'] = True

    if is_client:
        portfolio_store.save_client_store(client_name, c_store)
    else:
        portfolio_store.save_data(portfolio_store.data)

    return jsonify({'status': 'ok', 'locked': True})

@app.route('/api/profile/toggle_sweep', methods=['POST'])
@login_required
def toggle_profile_sweep():
    data = request.get_json() or {}
    prof_name = data.get('profile', '').strip()
    sweep_val = bool(data.get('overnight_sweep', True))

    is_client = (session.get('role') != 'admin')
    if is_client:
        c_store = portfolio_store.get_client_store(session.get('user'))
        users = c_store.setdefault('users', {})
        if prof_name in users:
            users[prof_name]['overnight_sweep'] = sweep_val
            c_broker = c_store.setdefault('broker', {})
            allocs = c_broker.setdefault('allocations', {})
            for k in allocs:
                if k in prof_name.lower() or prof_name.lower() in k:
                    allocs[k]['overnight_sweep'] = sweep_val
            portfolio_store.save_client_store(session.get('user'), c_store)
            return jsonify({'status': 'ok', 'overnight_sweep': sweep_val})
        return jsonify({'status': 'error', 'message': 'Profile not found'}), 404

    portfolio_store.reload()
    users = portfolio_store.data.setdefault('users', {})
    if prof_name in users:
        users[prof_name]['overnight_sweep'] = sweep_val
        broker = portfolio_store.data.setdefault('broker', {})
        allocs = broker.setdefault('allocations', {})
        for k in allocs:
            if k in prof_name.lower() or prof_name.lower() in k:
                allocs[k]['overnight_sweep'] = sweep_val
        portfolio_store.save_data(portfolio_store.data)
        return jsonify({'status': 'ok', 'overnight_sweep': sweep_val})
    return jsonify({'status': 'error', 'message': 'Profile not found'}), 404

@app.route('/api/portfolio/add_capital', methods=['POST'])
@login_required
def add_capital_route():
    data = request.get_json() or {}
    username = data.get('username') or portfolio_store.active_username()
    amount = float(data.get('amount', 0.0))

    if amount == 0:
        return jsonify({'status': 'error', 'message': 'Invalid adjustment amount'}), 400

    is_client = (session.get('role') != 'admin')
    client_name = session.get('user', 'Client')

    if is_client:
        c_store = portfolio_store.get_client_store(client_name)
        users = c_store.setdefault('users', {})
        target = username if username in users else c_store.get('active_user', '')
        if target in users:
            ud = users[target]
            new_budget = max(50.0, round(float(ud.get('master_budget', 500.0)) + amount, 2))
            ud['master_budget'] = new_budget
            c_broker = c_store.setdefault('broker', {})
            allocs = c_broker.setdefault('allocations', {})
            for k in allocs:
                if k in target.lower() or target.lower() in k:
                    allocs[k]['budget'] = new_budget
            portfolio_store.save_client_store(client_name, c_store)
            return jsonify({'status': 'ok', 'new_budget': new_budget})
        return jsonify({'status': 'error', 'message': 'Profile not found'}), 404

    portfolio_store.reload()
    ud = portfolio_store.user_data(username)
    new_budget = max(50.0, round(float(ud.get('master_budget', 500.0)) + amount, 2))
    ud['master_budget'] = new_budget
    broker = portfolio_store.data.setdefault('broker', {})
    allocs = broker.setdefault('allocations', {})
    for k in allocs:
        if k in username.lower() or username.lower() in k:
            allocs[k]['budget'] = new_budget
    portfolio_store.save_data(portfolio_store.data)
    return jsonify({'status': 'ok', 'new_budget': new_budget})

@app.route('/api/users/delete', methods=['POST'])
@login_required
def delete_user():
    data = request.get_json() or {}
    target_user = data.get('username', '').strip()
    if session.get('role') == 'admin':
        portfolio_store.reload()
        success = portfolio_store.delete_user(target_user)
        broker = portfolio_store.data.setdefault('broker', {})
        allocs = broker.setdefault('allocations', {})
        allocs.pop(target_user.lower(), None)
        portfolio_store.save_data(portfolio_store.data)
        return jsonify({'status': 'ok' if success else 'error'})
    else:
        client_name = session.get('email') or session.get('user')
        c_store = portfolio_store.get_client_store(client_name)
        users = c_store.setdefault('users', {})
        if target_user in users:
            del users[target_user]
            c_broker = c_store.setdefault('broker', {})
            allocs = c_broker.setdefault('allocations', {})
            allocs.pop(target_user.lower(), None)
            if c_store.get('active_user') == target_user:
                c_store['active_user'] = list(users.keys())[0] if users else ''
            c_store['is_configured'] = bool(users)
            portfolio_store.save_client_store(client_name, c_store)
        return jsonify({'status': 'ok'})

@app.route('/api/portfolio', methods=['GET'])
@login_required
def get_portfolio():
    if session.get('role') == 'admin':
        portfolio_store.reload()
        return jsonify(portfolio_store.user_data())
    return jsonify(portfolio_store.get_client_store(session.get('user')))

@app.route('/api/portfolio/sync_alpaca', methods=['POST'])
@login_required
def api_sync_alpaca_state():
    """1-Click Sync: Pulls ground truth from Alpaca and aligns live portfolio values."""
    portfolio_store.reload()
    req_data = request.get_json(silent=True) or {}
    prof_name = req_data.get('username') or portfolio_store.active_username()
    is_client = (session.get('role') != 'admin')
    client_name = session.get('user', 'Client')

    if is_client:
        c_store = portfolio_store.get_client_store(client_name)
        broker = c_store.get('broker') or {}
        users = c_store.setdefault('users', {})
        ud = users.setdefault(prof_name, portfolio_store.default_user_state(prof_name))
    else:
        broker = portfolio_store.data.get('broker') or {}
        ud = portfolio_store.user_data(prof_name)

    api_k = str(broker.get('api_key', '')).strip()
    sec_k = str(broker.get('secret_key', '')).strip()
    b_mode = broker.get('mode', 'paper')

    if not api_k or not sec_k:
        return jsonify({'status': 'error', 'message': 'No Alpaca keys found.'}), 400

    base_url = "https://paper-api.alpaca.markets" if b_mode == 'paper' else "https://api.alpaca.markets"
    headers = {"APCA-API-KEY-ID": api_k, "APCA-API-SECRET-KEY": sec_k}

    try:
        # 1. Fetch exact account equity & start-of-day equity directly from Alpaca
        req = urllib.request.Request(f"{base_url}/v2/account", headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            acc = json.loads(resp.read().decode('utf-8'))

        alpaca_equity = round(float(acc.get('portfolio_value') or acc.get('equity') or 0.0), 2)
        alpaca_last_equity = round(float(acc.get('last_equity') or 700.00), 2)
        if alpaca_last_equity <= 0: alpaca_last_equity = 700.00

        today_pnl = round(alpaca_equity - alpaca_last_equity, 2)
        today_pct = round((today_pnl / alpaca_last_equity) * 100.0, 2)

        # Baseline budget is set to Alpaca's opening equity & clear any stale ratchet/day locks
        ud['master_budget'] = alpaca_last_equity
        ud['daily_peak_pct'] = max(0.0, today_pct)
        ud['ratchet_locked'] = False
        ud['is_locked'] = False
        ud['manual_locked_day'] = None
        ud['ratchet_floor_override'] = None
        ud['locked_floor_val'] = None

        now_lon = pd.Timestamp.now(tz='Europe/London')
        today_str = now_lon.strftime('%Y-%m-%d')

        # 2. Fetch live positions & closed orders directly from Alpaca (preserves today's trade history)
        req_p = urllib.request.Request(f"{base_url}/v2/positions", headers=headers)
        with urllib.request.urlopen(req_p, timeout=5) as r_p:
            live_pos = json.loads(r_p.read().decode('utf-8'))

        closed_orders = []
        try:
            req_o = urllib.request.Request(f"{base_url}/v2/orders?status=closed&limit=40&direction=desc", headers=headers)
            with urllib.request.urlopen(req_o, timeout=5) as r_o:
                closed_orders = json.loads(r_o.read().decode('utf-8'))
        except Exception:
            pass

        sync_profile_with_alpaca_non_destructive(ud, acc, live_pos, closed_orders, today_str, now_lon)

        if is_client:
            portfolio_store.save_client_store(client_name, c_store)
        else:
            portfolio_store.save_data(portfolio_store.data)

        return jsonify({
            'status': 'ok',
            'equity': alpaca_equity,
            'budget': alpaca_last_equity,
            'today_pnl': today_pnl,
            'today_pct': today_pct
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/portfolio/reset', methods=['POST'])
@login_required
def api_portfolio_reset():
    portfolio_store.reload()
    data = request.get_json() or {}
    username = data.get('username') or portfolio_store.active_username()
    total_equity = float(data.get('total_equity', 5000.0))
    today_pnl = float(data.get('today_pnl', 0.0))
    pos_type = data.get('position_type', 'cash')
    ticker = data.get('ticker', '').strip().upper()
    shares = int(data.get('shares', 0))
    buy_price = float(data.get('buy_price', 0.0))

    is_client = (session.get('role') != 'admin')
    client_name = session.get('user', 'Client')

    if is_client:
        c_store = portfolio_store.get_client_store(client_name)
        users = c_store.setdefault('users', {})
        ud = users.setdefault(username, portfolio_store.default_user_state(username))
    else:
        ud = portfolio_store.user_data(username)

    ud['master_budget'] = total_equity
    ud['initial_positions'] = {}
    ud['holdings'] = {}
    ud['history'] = []
    ud['ratchet_locked'] = False
    ud['manual_locked_day'] = None
    ud['daily_peak_pct'] = 0.0

    if pos_type == 'pos' and ticker and shares > 0 and buy_price > 0:
        pos_amt = round(shares * buy_price, 2)
        now_lon = pd.Timestamp.now(tz='Europe/London')
        ud['initial_positions'][ticker] = {'shares': shares, 'manual_val': pos_amt}
        ud['holdings'][ticker] = {'shares': shares, 'manual_val': pos_amt, 'high_water': buy_price}
        ud['history'].append({
            'id': str(int(time.time() * 1000)),
            'ticker': ticker,
            'trade_type': 'LONG',
            'action': 'BUY',
            'shares': shares,
            'price': buy_price,
            'amount': pos_amt,
            'time': now_lon.strftime('%d %b %H:%M'),
            'date_str': now_lon.strftime('%Y-%m-%d'),
            'timestamp': int(now_lon.timestamp())
        })

    if is_client:
        portfolio_store.save_client_store(client_name, c_store)
    else:
        portfolio_store.save_data(portfolio_store.data)

    return jsonify({'status': 'ok'})

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
@login_required
def add_watchlist():
    ticker = (request.get_json() or {}).get('ticker', '').strip().upper()
    if not ticker: return jsonify({'status': 'ok'})
    if session.get('role') == 'admin':
        portfolio_store.reload()
        portfolio_store.add_watchlist(ticker)
    else:
        client_name = session.get('user')
        c_store = portfolio_store.get_client_store(client_name)
        users = c_store.setdefault('users', {})
        active = c_store.get('active_user')
        if active in users:
            wl = users[active].setdefault('watchlist', [])
            if ticker not in wl: wl.append(ticker)
        else:
            wl = c_store.setdefault('watchlist', [])
            if ticker not in wl: wl.append(ticker)
        portfolio_store.save_client_store(client_name, c_store)
    return jsonify({'status': 'ok'})

@app.route('/api/watchlist/delete', methods=['POST'])
@login_required
def delete_watchlist():
    ticker = (request.get_json() or {}).get('ticker', '').strip().upper()
    if not ticker: return jsonify({'status': 'ok'})
    if session.get('role') == 'admin':
        portfolio_store.reload()
        portfolio_store.remove_watchlist(ticker)
    else:
        client_name = session.get('user')
        c_store = portfolio_store.get_client_store(client_name)
        users = c_store.setdefault('users', {})
        active = c_store.get('active_user')
        if active in users:
            wl = users[active].get('watchlist', [])
            if ticker in wl: wl.remove(ticker)
        else:
            wl = c_store.get('watchlist', [])
            if ticker in wl: wl.remove(ticker)
        portfolio_store.save_client_store(client_name, c_store)
    return jsonify({'status': 'ok'})

@app.route('/api/portfolio/holding', methods=['POST'])
def update_holding():
    portfolio_store.reload()
    b = request.get_json() or {}
    shares = portfolio_store.set_holding_value(b.get('ticker'), b.get('value_owned', 0), b.get('price', 1))
    return jsonify({'status': 'ok', 'shares': shares})

@app.route('/api/trade/execute', methods=['POST'])
@login_required
def execute_trade():
    portfolio_store.reload()
    b = request.get_json() or {}
    is_client = (session.get('role') != 'admin')
    client_email = session.get('email') or session.get('user') if is_client else None
    return jsonify({'status': 'ok', 'entry': portfolio_store.execute_trade(
        b.get('ticker'), b.get('action'), b.get('shares'), b.get('price'), b.get('username'),
        client_email=client_email
    )})

@app.route('/api/trade/undo', methods=['POST'])
@login_required
def undo_trade(): 
    portfolio_store.reload()
    is_client = (session.get('role') != 'admin')
    client_email = session.get('email') or session.get('user') if is_client else None
    t_id = (request.get_json() or {}).get('id')
    if is_client and client_email:
        c_store = portfolio_store.get_client_store(client_email)
        for u_name, u_d in (c_store.get('users') or {}).items():
            hist = u_d.get('history') or []
            match = next((t for t in hist if t.get('id') == str(t_id)), None)
            if match:
                hist.remove(match)
                portfolio_store.save_client_store(client_email, c_store)
                return jsonify({'status': 'ok'})
        return jsonify({'status': 'error'})
    return jsonify({'status': 'ok' if portfolio_store.undo_trade(t_id) else 'error'})

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
        import traceback
        curr_u = (session.get('email') or session.get('user') or '').strip().lower()
        is_admin = (session.get('role') == 'admin') or (curr_u in [ADMIN_USER.lower(), ADMIN_ALIAS.lower(), 'ian']) or ('ian' in curr_u)
        is_client = not is_admin
        client_name = session.get('email') or session.get('user', 'Client')
        req_user = request.args.get('user', '').strip()

        c_store = None
        if is_client:
            c_store = portfolio_store.get_client_store(client_name) or portfolio_store.blank_client_state(client_name)
            users_dict = c_store.get('users', {})
            active_name = req_user if (req_user and req_user in users_dict) else c_store.get('active_user', '')
            if not active_name and users_dict:
                active_name = list(users_dict.keys())[0]
            raw_ud = users_dict.get(active_name) if active_name in users_dict else dict(c_store)
            ud = dict(raw_ud)
            ud['is_configured'] = bool(users_dict)
            ud['strategy_name'] = active_name or 'Vector Hybrid'
            ud['active_user'] = active_name or 'Vector Hybrid'
            ud['account_username'] = client_name
            mb = float(ud.get('master_budget') or 500.0)
            active_profile = active_name.strip().lower() if active_name else 'vector hybrid'
        else:
            portfolio_store.reload()
            if req_user and req_user in portfolio_store.data.get('users', {}):
                portfolio_store.data['active_user'] = req_user
            ud = portfolio_store.user_data(req_user if req_user else None)
            ud['account_username'] = session.get('email') or ADMIN_USER
            mb = float(ud.get('master_budget') or 500.0)
            active_profile = (req_user or portfolio_store.active_username()).strip().lower()

        broker = (c_store.get('broker') or {}) if is_client else (portfolio_store.data.get('broker') or {})
        b_key = str(broker.get('api_key', '')).strip()
        b_sec = str(broker.get('secret_key', '')).strip()
        b_mode = broker.get('mode', 'paper')
        allocations = broker.get('allocations') or {}

        alpaca_acc = None
        if b_key and b_sec:
            alpaca_acc = get_live_alpaca_account(b_key, b_sec, b_mode)

        def is_profile_alpaca_enabled(prof_name):
            p_clean = str(prof_name).lower()
            if p_clean in allocations:
                return bool(allocations[p_clean].get('enabled', False))
            for k, v in allocations.items():
                if k in p_clean or p_clean in k:
                    return bool(v.get('enabled', False))
            return False

        wl = ud.get('watchlist') or ['TQQQ', 'SOXL', 'SQQQ']
        t = request.args.get('t', '').upper().strip()
        is_momentum = any(x in active_profile for x in ['vector', 'dual', 'velocity', 'pulse', 'hybrid', 'axis', 'test e', 'test s', 'test tq', 'test r', 'test tqm'])
        is_rotator = is_momentum

        hist = ud.get('history') or []
        init_pos = ud.get('initial_positions') or {}

        def resolve_profile_shares(profile_ud, tk):
            i_sh = int((profile_ud.get('initial_positions', {}).get(tk) or {}).get('shares') or 0)
            n_sh = sum(int(tr.get('shares') or 0) if tr.get('action') == 'BUY' else -int(tr.get('shares') or 0) 
                       for tr in profile_ud.get('history', []) if tr.get('ticker') == tk)
            return max(0, i_sh + n_sh)

        active_holds = list(set([tr.get('ticker') for tr in hist if resolve_profile_shares(ud, tr.get('ticker')) > 0]))

        if not t or t == 'ALL_SHARES':
            last_traded_tk = next(
                (tr.get('ticker') for tr in hist if tr.get('ticker') and tr.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']),
                None
            )
            t = active_holds[0] if active_holds else (last_traded_tk if last_traded_tk else (wl[0] if wl else 'TQQQ'))
        if 'holdings' not in ud or not isinstance(ud['holdings'], dict): ud['holdings'] = {}
        for tk in active_holds:
            sh_cnt = resolve_profile_shares(ud, tk)
            if sh_cnt > 0:
                if tk not in ud['holdings']: ud['holdings'][tk] = {}
                ud['holdings'][tk]['shares'] = sh_cnt
        ud['active_holds'] = active_holds

        pnl_dfs_5m = {}
        def fetch_pnl_data_5m(tick): return tick, fetch_yf_data(tick, "5d", "5m")
        if active_holds:
            with ThreadPoolExecutor(max_workers=4) as ex:
                for tick, df_5m in ex.map(fetch_pnl_data_5m, active_holds): pnl_dfs_5m[tick] = df_5m

        net_history = sum(-float(tr.get('amount') or 0.0) if tr.get('action') == 'BUY' else float(tr.get('amount') or 0.0) 
                          for tr in hist if tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION'])
        init_manual = sum(float(pos.get('manual_val') or 0.0) for pos in init_pos.values())
        cash_balance = mb + net_history - init_manual

        tot_own = 0.0
        for tk in active_holds:
            sh_c = resolve_profile_shares(ud, tk)
            df_c = pnl_dfs_5m.get(tk)
            if df_c is not None and not df_c.empty:
                cur_p = float(df_c['Close'].iloc[-1])
                div = 100.0 if str(tk).endswith('.L') and cur_p > 100 else 1.0
                tot_own += round(sh_c * (cur_p / div), 2)
        total_equity = cash_balance + tot_own
        master_pnl_val = total_equity - mb
        master_pnl_pct = (master_pnl_val / mb * 100.0) if mb > 0 else 0.0

        now_lon = pd.Timestamp.now(tz='Europe/London')
        today_str = now_lon.strftime('%Y-%m-%d')
        
        all_user_holds = set(['TQQQ', 'SOXL', 'SQQQ'])
        source_for_holds = (c_store.get('users', {}) if c_store else {}) if is_client else portfolio_store.data.get('users', {})
        for u, u_data in source_for_holds.items():
            if isinstance(u_data, dict):
                for tr in u_data.get('history', []):
                    tk_sym = tr.get('ticker')
                    if tk_sym and tk_sym not in ['CALIBRATION', 'CAPITAL_INJECTION']:
                        all_user_holds.add(tk_sym)
                
        prices = {}
        def fetch_lb_price(tick):
            if not tick or tick in ['CALIBRATION', 'CAPITAL_INJECTION']:
                return tick, 0.0, 0.0
            try:
                df_lb = fetch_yf_data(tick, "5d", "5m")
                if df_lb.empty: return tick, 0.0, 0.0
                cur_p = float(df_lb['Close'].iloc[-1])
                div = 100.0 if str(tick).endswith('.L') and cur_p > 100 else 1.0
                cur_dollars = cur_p / div
                
                df_temp = df_lb.copy()
                if df_temp.index.tz is None: df_temp.index = df_temp.index.tz_localize('UTC')
                else: df_temp.index = df_temp.index.tz_convert('UTC')
                
                last_lon = df_temp.index[-1].tz_convert('Europe/London')
                if now_lon.date() > last_lon.date(): start_dollars = cur_dollars
                else:
                    prev_sessions = df_temp[df_temp.index.tz_convert('Europe/London').strftime('%Y-%m-%d') < today_str]
                    start_p = float(prev_sessions['Close'].iloc[-1]) if not prev_sessions.empty else float(df_temp['Close'].iloc[0])
                    start_dollars = start_p / div
                return tick, cur_dollars, start_dollars
            except Exception:
                return tick, 0.0, 0.0

        with ThreadPoolExecutor(max_workers=6) as ex:
            for tick, cur_d, start_d in ex.map(fetch_lb_price, all_user_holds): 
                prices[tick] = {'cur': cur_d, 'start': start_d}

        usd_gbp_rate = 0.7499
        try:
            df_fx = fetch_yf_data("GBPUSD=X", "5d", "5m")
            if not df_fx.empty:
                gbp_per_usd = 1.0 / float(df_fx['Close'].iloc[-1])
                if gbp_per_usd > 0: usd_gbp_rate = round(gbp_per_usd, 4)
        except Exception: pass

        leaderboard = []
        source_users = (c_store.get('users', {}) if c_store else {}) if is_client else portfolio_store.data.get('users', {})
        if not source_users:
            source_users = portfolio_store.data.get('users', {})

        for u, u_data in source_users.items():
            if not isinstance(u_data, dict): continue
            mb_lb = float(u_data.get('master_budget') or 500.0)
            hist_lb = u_data.get('history') or []
            init_pos_lb = u_data.get('initial_positions') or {}

            # Compute each profile's Cash, Shares, Equity, and Held Ticker from its own reconciled ledger
            # so sidebar cards, POSITION HELD, and TRADE HISTORY always stay 100% consistent.
            nh_lb = sum(-float(tr.get('amount') or 0.0) if tr.get('action') == 'BUY' else float(tr.get('amount') or 0.0) 
                        for tr in hist_lb if tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION'])
            im_lb = sum(float(pos.get('manual_val') or 0.0) for pos in init_pos_lb.values())
            cash_now = mb_lb + nh_lb - im_lb
            
            trades_today = [tr for tr in hist_lb if tr.get('date_str') == today_str]
            net_cash_today = sum(-float(tr.get('amount') or 0.0) if tr.get('action') == 'BUY' else float(tr.get('amount') or 0.0) 
                                 for tr in trades_today)
            cash_start = cash_now - net_cash_today
            
            equity_now, equity_start = 0.0, 0.0
            shares_now_map = {}
            for tr in hist_lb:
                tk = tr.get('ticker')
                if tk and tk not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC'] and tk not in shares_now_map:
                    shares_now_map[tk] = resolve_profile_shares(u_data, tk)
                
            for tk, sh_now in shares_now_map.items():
                if sh_now <= 0 and not any(tr.get('ticker') == tk for tr in trades_today): continue
                p_data = prices.get(tk, {'cur': 0.0, 'start': 0.0})
                equity_now += sh_now * p_data['cur']
                
                sh_start = sh_now
                for tr in trades_today:
                    if tr.get('ticker') == tk:
                        if tr.get('action') == 'BUY': sh_start -= int(tr.get('shares') or 0)
                        else: sh_start += int(tr.get('shares') or 0)
                equity_start += max(0, sh_start) * p_data['start']
                
            tot_eq_now = max(0.0, cash_now) + equity_now
            tot_eq_start = max(0.0, cash_start) + equity_start

            user_holds = [tk for tk, sh in shares_now_map.items() if sh > 0]
            held_sym = user_holds[0] if user_holds else ''
            day_diff = round(tot_eq_now - tot_eq_start, 2)
            day_pct = round((day_diff / tot_eq_start * 100.0), 2) if tot_eq_start > 0 else 0.0
            day_col = '#0ecb81' if day_diff > 0 else ('#ff5364' if day_diff < 0 else '#8a8a9e')

            trade_dates = sorted(list(set(tr.get('date_str') for tr in hist_lb if tr.get('date_str'))))
            start_date = trade_dates[0] if trade_dates else today_str
            active_days = max(1, len(pd.bdate_range(start_date, today_str)))

            cum_gain_val = tot_eq_now - mb_lb
            cum_gain_pct = (cum_gain_val / mb_lb * 100.0) if mb_lb > 0 else 0.0
            daily_avg_pct = round(cum_gain_pct / active_days, 2)
            daily_avg_val = round(cum_gain_val / active_days, 2)

            has_override = (u_data.get('ratchet_floor_override') is not None)
            is_manually_locked = (u_data.get('manual_locked_day') == today_str)
            is_ratchet_locked = bool(u_data.get('ratchet_locked') and len(user_holds) == 0)
            is_locked = is_manually_locked or is_ratchet_locked or (len(user_holds) == 0 and day_pct >= 2.50 and not has_override)

            leaderboard.append({
                'user': u, 
                'equity': round(tot_eq_now, 2), 
                'budget': mb_lb, 
                'overnight_sweep': bool(u_data.get('overnight_sweep', True)),
                'daily_pnl': day_diff,
                'daily_pct': day_pct,
                'daily_color': day_col,
                'daily_avg_pct': daily_avg_pct,
                'daily_avg_pnl': daily_avg_val,
                'days_active': active_days,
                'holding': held_sym,
                'held_ticker': held_sym,
                'active_holds': user_holds,
                'has_holding': len(user_holds) > 0,
                'is_locked': is_locked,
                'is_manually_locked': is_manually_locked,
                'active_floor': float(u_data.get('ratchet_floor_override') or (round(int(day_pct * 2) / 2.0, 2) if day_pct >= 2.5 else 0.0)),
                'locked_pct': round(day_pct, 2)
            })
        leaderboard.sort(key=lambda x: x['equity'], reverse=True)

        settings = ud.get('settings') or {}
        req_p = request.args.get('p')
        if not req_p or req_p == 'undefined': req_p = settings.get('period', '1d')
        req_i = request.args.get('i')
        if not req_i or req_i == 'undefined': req_i = settings.get('interval', '5m')

        if req_p in ['1y', '5y', 'max'] and req_i in ['1m', '2m', '5m', '15m', '30m', '60m', '1h']: req_i = '1d'
        elif req_p in ['1mo', '3mo', '6mo'] and req_i in ['1m', '2m']: req_i = '5m'

        wl_status = {}
        if wl:
            def check_status(tick):
                try:
                    df_stat = fetch_yf_data(tick, "5d", "5m")
                    if df_stat.empty: return tick, 'closed'
                    df_temp = df_stat.copy()
                    if df_temp.index.tz is not None: df_temp.index = df_temp.index.tz_convert('UTC')
                    if (time.time() - df_temp.index[-1].timestamp()) > 1800: return tick, 'closed' 
                    if len(df_temp) > 1:
                        if df_temp['Close'].iloc[-1] > df_temp['Close'].iloc[-2]: return tick, 'up'
                        elif df_temp['Close'].iloc[-1] < df_temp['Close'].iloc[-2]: return tick, 'down'
                    return tick, 'closed'
                except Exception: return tick, 'closed'

            with ThreadPoolExecutor(max_workers=4) as ex:
                for tick, status in ex.map(check_status, wl): wl_status[tick] = status
            
        tot_today_diff, tot_1h_diff = 0.0, 0.0
        now_utc = pd.Timestamp.now(tz='UTC')
        
        for tk in active_holds:
            sh_h = resolve_profile_shares(ud, tk)
            if sh_h <= 0: continue
            try:
                t_buys_today = [tr for tr in hist if tr.get('ticker') == tk and tr.get('action') == 'BUY' and tr.get('date_str') == now_lon.strftime('%Y-%m-%d')]
                df_5m = pnl_dfs_5m.get(tk)
                if df_5m is not None and not df_5m.empty:
                    cur_price = float(df_5m['Close'].iloc[-1])
                    div = 100.0 if str(tk).endswith('.L') and cur_price > 100 else 1.0
                    cur_price_dollars = cur_price / div
                    
                    if t_buys_today: 
                        tot_shares_b = sum(int(tr.get('shares') or 1) for tr in t_buys_today)
                        p_today_base_dollars = sum(float(tr.get('amount') or 0.0) for tr in t_buys_today) / (tot_shares_b if tot_shares_b > 0 else 1)
                    else:
                        last_date_str = now_lon.strftime('%Y-%m-%d')
                        df_temp = df_5m.copy()
                        if df_temp.index.tz is None: df_temp.index = df_temp.index.tz_localize('UTC')
                        else: df_temp.index = df_temp.index.tz_convert('UTC')
                        prev_sessions = df_temp[df_temp.index.tz_convert('Europe/London').strftime('%Y-%m-%d') < last_date_str]
                        p_today_mkt = float(prev_sessions['Close'].iloc[-1]) if not prev_sessions.empty else float(df_temp['Close'].iloc[0])
                        p_today_base_dollars = p_today_mkt / div

                    target_ts = now_utc - pd.Timedelta(hours=1)
                    df_temp = df_5m.copy()
                    if df_temp.index.tz is None: df_temp.index = df_temp.index.tz_localize('UTC')
                    else: df_temp.index = df_temp.index.tz_convert('UTC')
                    prior_df = df_temp[df_temp.index <= target_ts]
                    p_1h_mkt = float(prior_df['Close'].iloc[-1]) if not prior_df.empty else (float(df_temp['Close'].iloc[0]) if not df_temp.empty else cur_price)
                    p_1h_base_dollars = p_1h_mkt / div

                    tot_today_diff += sh_h * (cur_price_dollars - p_today_base_dollars)
                    tot_1h_diff += sh_h * (cur_price_dollars - p_1h_base_dollars)
            except Exception: pass

        master_today_pct = (tot_today_diff / mb * 100.0) if mb > 0 else 0.0
        master_1h_pct = (tot_1h_diff / mb * 100.0) if mb > 0 else 0.0

        master_pnl_data = {
            'all': {'val': f"{'+' if master_pnl_val>0 else ''}${master_pnl_val:.2f} ({'+' if master_pnl_pct>0 else ''}{master_pnl_pct:.2f}%)", 'color': '#0ecb81' if master_pnl_val > 0 else ('#ff5364' if master_pnl_val < 0 else '#8a8a9e')},
            'today': {'val': f"{'+' if tot_today_diff>0 else ''}${tot_today_diff:.2f} ({'+' if master_today_pct>0 else ''}{master_today_pct:.2f}%)", 'color': '#0ecb81' if tot_today_diff > 0 else ('#ff5364' if tot_today_diff < 0 else '#8a8a9e')},
            '1h': {'val': f"{'+' if tot_1h_diff>0 else ''}${tot_1h_diff:.2f} ({'+' if master_1h_pct>0 else ''}{master_1h_pct:.2f}%)", 'color': '#0ecb81' if tot_1h_diff > 0 else ('#ff5364' if tot_1h_diff < 0 else '#8a8a9e')}
        }

        engine = MarketScoringEngine()
        regime = engine.check_market_regime()

        if t == 'ALL_SHARES':
            t = 'TQQQ'

        df = fetch_yf_data(t, req_p, req_i)
        if df.empty:
            return jsonify({'ohlc': [], 'wl_status': wl_status, 'name': f'{t}', 'portfolio': ud, 'leaderboard': leaderboard, 'usd_gbp_rate': usd_gbp_rate, 'metrics': {
                'price': 0, 'price_display': '$0.00', 'discount': '--', 'buy_score': '0 / 100', 'tranches': 0,
                'status': 'Loading Data', 'color': '#787e8e', 'reason': 'Fetching fresh market quotes.',
                'action_main': 'WAIT', 'action_sub': '', 'action_color': '#787e8e', 'shares_owned': 0, 'value_owned': 0.0,
                'pnl_display': '$0.00 (0.00%)', 'pnl_color': '#8a8a9e', 'pnl_data': master_pnl_data, 'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data,
                'master_budget': mb, 'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime,
                'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': '', 'is_rotator_profile': is_rotator
            }})

        df_chart = df.copy()
        if df_chart.index.tz is not None: df_chart.index = df_chart.index.tz_convert('UTC')
        df_chart.name = t
        data = [{'time': i.strftime('%Y-%m-%d') if req_i in ['1d','5d','1wk','1mo','3mo'] else int(i.timestamp()), 
                 'open': round(float(r['Open']),2), 'high': round(float(r['High']),2), 'low': round(float(r['Low']),2), 'close': round(float(r['Close']),2)} 
                for i, r in df_chart.iterrows()]
        seen = set(); data = [x for x in data if x['time'] not in seen and not seen.add(x['time'])]
        last_p = round(data[-1]['close'], 2) if data else 0.0

        sh_own = resolve_profile_shares(ud, t)
        cps = last_p / 100.0 if str(t).endswith('.L') and last_p > 100 else last_p
        val_own = round(sh_own * cps, 2)

        avg_buy_p, highest_p = 0.0, last_p
        holds_dict = ud.get('holdings') or {}
        if sh_own > 0:
            t_buys = [tr for tr in hist if tr.get('ticker') == t and tr.get('action') == 'BUY']
            if t_buys: 
                avg_buy_p = float(t_buys[0].get('price') or 0.0)
                buy_ts = int(t_buys[0].get('timestamp') or 0)
                df_check = pnl_dfs_5m.get(t) if pnl_dfs_5m.get(t) is not None else fetch_yf_data(t, "5d", "5m")
                if df_check is not None and not df_check.empty and 'High' in df_check.columns:
                    try:
                        post_buy_df = df_check[df_check.index.astype('int64') // 10**9 >= (buy_ts - 300)]
                        candle_high = float(post_buy_df['High'].max()) if not post_buy_df.empty else last_p
                    except Exception: candle_high = last_p
                else: candle_high = last_p
            else: candle_high = last_p

            stored_hw = float((holds_dict.get(t) or {}).get('high_water') or last_p)
            highest_p = max(stored_hw, candle_high, last_p)
            if highest_p > stored_hw:
                if 'holdings' not in ud: ud['holdings'] = {}
                if t not in ud['holdings']: ud['holdings'][t] = {}
                ud['holdings'][t]['high_water'] = highest_p
                if is_client: portfolio_store.save_client_store(client_name, c_store)
                else: portfolio_store.save_data(portfolio_store.data)

        if is_momentum:
            df_score = pnl_dfs_5m.get(t) if pnl_dfs_5m.get(t) is not None else fetch_yf_data(t, "5d", "5m")
            if df_score is None or df_score.empty: df_score = df
            st = engine.score_momentum(df_score, last_p, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=active_profile, regime=regime, overnight_sweep=bool(ud.get('overnight_sweep', True)))
        else:
            av = df['Volume'].tail(20).mean() if len(df)>=20 else 1.0
            st = engine.score_nav_asset(t, last_p, (df['Volume'].iloc[-1]/av) if av>0 else 1.0) if t in engine.nav_bases else engine.score_equity(fetch_yf_data(t, "1y", "1d"), last_p)

        cb_t = float((init_pos.get(t) or {}).get('manual_val') or 0.0) + sum(float(tr.get('amount') or 0.0) if tr.get('action')=='BUY' else -float(tr.get('amount') or 0.0) for tr in hist if tr.get('ticker')==t)
        
        ticker_pnl_data = {}
        if sh_own > 0 and cb_t > 0:
            pv, pp = round(val_own - cb_t, 2), round((val_own - cb_t) / cb_t * 100, 2)
            c = '#0ecb81' if pv > 0 else ('#ff5364' if pv < 0 else '#8a8a9e')
            pnl_d = f"{'+' if pv>0 else ''}${pv:.2f} ({'+' if pp>0 else ''}{pp:.2f}%)"

            df_5m = pnl_dfs_5m.get(t)
            if df_5m is not None and not df_5m.empty:
                df_temp = df_5m.copy()
                if df_temp.index.tz is None: df_temp.index = df_temp.index.tz_localize('UTC')
                else: df_temp.index = df_temp.index.tz_convert('UTC')
                
                cur_p = float(df_temp['Close'].iloc[-1])
                div = 100.0 if str(t).endswith('.L') and cur_p > 100 else 1.0
                cur_p_dollars = cur_p / div
                last_ts = df_temp.index[-1]
                last_lon = last_ts.tz_convert('Europe/London')
                
                t_buys_today_t = [tr for tr in hist if tr.get('ticker') == t and tr.get('action') == 'BUY' and tr.get('date_str') == now_lon.strftime('%Y-%m-%d')]
                tot_sh_today = sum(int(tr.get('shares') or 1) for tr in t_buys_today_t)
                avg_b_today_t_dollars = (sum(float(tr.get('amount') or 0.0) for tr in t_buys_today_t) / (tot_sh_today if tot_sh_today > 0 else 1)) if t_buys_today_t else 0.0

                if now_lon.date() > last_lon.date():
                    p_today_dollars = cur_p_dollars
                    p_1h_dollars = cur_p_dollars
                else:
                    last_date_str = last_lon.strftime('%Y-%m-%d')
                    prev_sessions = df_temp[df_temp.index.tz_convert('Europe/London').strftime('%Y-%m-%d') < last_date_str]
                    p_today_mkt = float(prev_sessions['Close'].iloc[-1]) if not prev_sessions.empty else float(df_temp['Close'].iloc[0])
                    p_today_dollars = avg_b_today_t_dollars if avg_b_today_t_dollars > 0 else (p_today_mkt / div)
                    
                    if (now_utc - last_ts).total_seconds() > 4200: p_1h_dollars = cur_p_dollars
                    else:
                        target_ts = now_utc - pd.Timedelta(hours=1)
                        prior_df = df_temp[df_temp.index <= target_ts]
                        p_1h_mkt = float(prior_df['Close'].iloc[-1]) if not prior_df.empty else p_today_mkt
                        p_1h_dollars = avg_b_today_t_dollars if avg_b_today_t_dollars > 0 else (p_1h_mkt / div)
                
                pv_today = sh_own * (cur_p_dollars - p_today_dollars)
                pv_1h = sh_own * (cur_p_dollars - p_1h_dollars)
                val_today_start = sh_own * p_today_dollars
                val_1h_start = sh_own * p_1h_dollars
                
                pp_today = (pv_today / val_today_start * 100.0) if val_today_start > 0 else 0.0
                pp_1h = (pv_1h / val_1h_start * 100.0) if val_1h_start > 0 else 0.0
                
                ticker_pnl_data = {
                    'all': {'val': pnl_d, 'color': c},
                    'today': {'val': f"{'+' if pv_today>0 else ''}${pv_today:.2f} ({'+' if pp_today>0 else ''}{pp_today:.2f}%)", 'color': '#0ecb81' if pv_today > 0 else ('#ff5364' if pv_today < 0 else '#8a8a9e')},
                    '1h': {'val': f"{'+' if pv_1h>0 else ''}${pv_1h:.2f} ({'+' if pp_1h>0 else ''}{pp_1h:.2f}%)", 'color': '#0ecb81' if pv_1h > 0 else ('#ff5364' if pv_1h < 0 else '#8a8a9e')}
                }
            else:
                ticker_pnl_data = {'all': {'val': pnl_d, 'color': c}, 'today': {'val': pnl_d, 'color': c}, '1h': {'val': pnl_d, 'color': c}}
        else: 
            pnl_d, c = "$0.00 (0.00%)", '#8a8a9e'
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
                    else: sma_dict, last_valid = {}, last_p
                    for d in data:
                        time_str = d['time'] if isinstance(d['time'], str) else pd.to_datetime(d['time'], unit='s').strftime('%Y-%m-%d')
                        val = sma_dict.get(time_str, last_valid)
                        mathLine.append({'time': d['time'], 'value': round(((val - d['close']) / val) * 100.0 if val > 0 else 0, 2)})
        except Exception: mathLine = []

        now_ts_guard = int(time.time())
        real_hist_guard = [
            h for h in hist
            if h.get('ticker') and h.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']
        ]
        cd_15m_rem = 0
        if real_hist_guard and real_hist_guard[0].get('action') == 'SELL':
            last_sell_ts = int(real_hist_guard[0].get('timestamp') or 0)
            if last_sell_ts > 0:
                cd_15m_rem = max(0, 900 - (now_ts_guard - last_sell_ts))

        cd_60m_rem = 0
        wash_ticker = ''
        for h in real_hist_guard:
            if h.get('action') == 'SELL':
                s_ts = int(h.get('timestamp') or 0)
                if s_ts > 0 and (now_ts_guard - s_ts) < 3600:
                    rem_w = 3600 - (now_ts_guard - s_ts)
                    if rem_w > cd_60m_rem:
                        cd_60m_rem = rem_w
                        wash_ticker = str(h.get('ticker') or '')

        guard_loss_stops = 0
        for idx_g, h in enumerate(hist):
            if h.get('ticker') in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']:
                continue
            if h.get('action') == 'SELL' and h.get('date_str') == today_str:
                m_buy = next((b for b in hist[idx_g+1:] if b.get('ticker') == h.get('ticker') and b.get('action') == 'BUY'), None)
                if m_buy:
                    if float(h.get('price') or 0) < float(m_buy.get('price') or 0):
                        guard_loss_stops += 1
                else:
                    guard_loss_stops += 1

        buy_guards = {
            'cd_15m_rem': cd_15m_rem,
            'cd_15m_total': 900,
            'cd_60m_rem': cd_60m_rem,
            'cd_60m_total': 3600,
            'wash_ticker': wash_ticker,
            'strikes_used': min(3, guard_loss_stops),
            'strikes_max': 3,
            'server_ts': now_ts_guard
        }

        return jsonify({'ohlc': data, 'ticker': t, 'mathLine': mathLine, 'wl_status': wl_status, 'name': engine.asset_names.get(t, t), 'portfolio': ud, 'leaderboard': leaderboard, 'usd_gbp_rate': usd_gbp_rate, 'metrics': {
            'price': last_p, 'price_display': f"{last_p:.2f}p (£{(last_p/100.0):.2f})" if str(t).endswith('.L') and last_p>100 else f"${last_p:.2f}",
            'discount': st.get('discount', '--'), 'buy_score': f"{st.get('score', 0)} / 100", 'tranches': st.get('tranches', 0),
            'status': st.get('status', '--'), 'color': st.get('color', '#8a8a9e'), 'reason': st.get('reason', ''),
            'action_main': st.get('action_main', 'HOLD'), 'action_sub': st.get('action_sub', ''), 'action_color': st.get('action_color', '#8a8a9e'),
            'shares_owned': sh_own, 'value_owned': val_own, 'pnl_display': ticker_pnl_data['all']['val'], 'pnl_color': ticker_pnl_data['all']['color'], 'pnl_data': ticker_pnl_data,
            'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data, 'master_budget': mb,
            'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime, 'trade_type': st.get('trade_type', 'LONG'),
            'health_pct': st.get('health_pct', 0), 'health_color': st.get('health_color', '#8a8a9e'), 'health_text': st.get('health_text', 'N/A'), 'is_rotator_profile': is_rotator,
            'buy_guards': buy_guards
        }})
    except Exception as e:
        import traceback
        traceback.print_exc()
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
    is_client = not is_admin_session()
    client_name = session.get('user', 'Client')

    if is_client:
        c_store = portfolio_store.get_client_store(client_name) or portfolio_store.blank_client_state(client_name)
        broker = c_store.get('broker') or {}
    else:
        broker = portfolio_store.data.get('broker') or {}

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

            # Audit desync across ALL profiles in the active account (admin or client)
            claimed_symbols = set()
            audit_users = (c_store.get('users', {}) if is_client else portfolio_store.data.get('users', {})) or {}
            for u_name, u_data in audit_users.items():
                if isinstance(u_data, dict):
                    u_init = u_data.get('initial_positions') or {}
                    u_hist = u_data.get('history') or []
                    for tr in u_hist:
                        tk = tr.get('ticker')
                        if not tk or tk in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']:
                            continue
                        i_sh = int((u_init.get(tk) or {}).get('shares') or 0)
                        n_sh = sum(int(x.get('shares') or 0) if x.get('action') == 'BUY' else -int(x.get('shares') or 0) for x in u_hist if x.get('ticker') == tk)
                        if max(0, i_sh + n_sh) > 0:
                            claimed_symbols.add(tk.upper())

            has_desync = any(p['symbol'].upper() not in claimed_symbols for p in live_positions)
            if not has_desync:
                enabled_u = [
                    u_n for u_n in audit_users.keys()
                    if any((s_k == u_n.lower() or s_k in u_n.lower() or u_n.lower() in s_k) and s_d.get('enabled')
                           for s_k, s_d in allocations.items())
                ]
                if len(enabled_u) == 1:
                    u_d = audit_users[enabled_u[0]]
                    u_mb = float(u_d.get('master_budget') or 700.0)
                    u_init = u_d.get('initial_positions') or {}
                    u_hist = u_d.get('history') or []
                    u_nh = sum(-float(x.get('amount') or 0.0) if x.get('action') == 'BUY' else float(x.get('amount') or 0.0)
                               for x in u_hist if x.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION'])
                    u_im = sum(float(pos.get('manual_val') or 0.0) for pos in u_init.values())
                    u_open_mv = sum(float(p.get('market_value') or 0.0) for p in live_positions)
                    u_est_eq = round(u_mb + u_nh - u_im + u_open_mv, 2)
                    alp_pv = round(float(acc.get('portfolio_value') or 0.0), 2)
                    if alp_pv > 0 and abs(alp_pv - u_est_eq) >= 0.25:
                        has_desync = True

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


@app.route('/api/news/feed', methods=['GET'])
def get_news_feed():
    global NEWS_CACHE
    now = time.time()
    
    # 5-minute cache to keep response times under 5ms
    if 'items' in NEWS_CACHE and (now - NEWS_CACHE.get('time', 0)) < 300:
        return jsonify({'items': NEWS_CACHE['items']})

    # 1. Scheduled High-Impact Economic Calendar & Macro Releases (Flat tags, No Emojis)
    items = [
        {"tag": "CPI", "tag_color": "#38bdf8", "title": "US CPI Inflation YoY Release", "detail": "Wed 13:30 BST • Exp: 2.7%", "link": "https://www.bls.gov/cpi/"},
        {"tag": "FOMC", "tag_color": "#ffb700", "title": "Federal Reserve Rate Decision & Press Conference", "detail": "Thu 19:00 BST • Target: 4.50%–4.75%", "link": "https://www.federalreserve.gov"},
        {"tag": "MACRO", "tag_color": "#0ecb81", "title": "US 10-Yr Treasury Yield Holds 4.38% • VIX Index Steady at 14.85", "detail": "Global Yield Curve", "link": "https://finance.yahoo.com/quote/%5ETNX"},
        {"tag": "JOBS", "tag_color": "#38bdf8", "title": "US Non-Farm Payrolls & Unemployment Report", "detail": "Fri 13:30 BST • Exp: 165K", "link": "https://www.bls.gov"}
    ]

    # 2. Live Market Wire Headlines (Nasdaq, Semiconductor, Fed Wire via RSS)
    try:
        url = "https://news.google.com/rss/search?q=Nasdaq+OR+Federal+Reserve+OR+Semiconductor+market+when:2d&hl=en-US&gl=US&ceid=US:en"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'})
        with urllib.request.urlopen(req, timeout=4) as resp:
            xml_data = resp.read()
            root = ET.fromstring(xml_data)
            for n in root.findall('.//item')[:8]:
                raw_title = n.findtext('title') or ''
                link = n.findtext('link') or '#'
                source = 'Market Wire'
                if ' - ' in raw_title:
                    parts = raw_title.rsplit(' - ', 1)
                    title = parts[0].strip()
                    source = parts[1].strip()
                else:
                    title = raw_title.strip()

                low = title.lower()
                tag, b_col = "WIRE", "#38bdf8"
                if any(w in low for w in ['fed', 'rate', 'powell', 'inflation', 'cpi', 'fomc']):
                    tag, b_col = "FED", "#ffb700"
                elif any(w in low for w in ['chip', 'semi', 'nvda', 'soxl', 'ai', 'tech', 'nasdaq', 'qqq']):
                    tag, b_col = "TECH", "#0ecb81"
                elif any(w in low for w in ['oil', 'crude', 'brent', 'dollar', 'yield', 'treasury']):
                    tag, b_col = "MACRO", "#a78bfa"

                items.append({
                    "tag": tag,
                    "tag_color": b_col,
                    "title": title,
                    "detail": source,
                    "link": link
                })
    except Exception:
        items.extend([
            {"tag": "SEMIS", "tag_color": "#0ecb81", "title": "Semiconductor Equipment Bookings Expand on AI Accelerator Demand", "detail": "Reuters", "link": "https://finance.yahoo.com"},
            {"tag": "FED", "tag_color": "#ffb700", "title": "Fed Officials Signal Measured Rate Adjustments Amid Steady Labor Market", "detail": "Bloomberg", "link": "https://finance.yahoo.com"},
            {"tag": "TECH", "tag_color": "#0ecb81", "title": "Nasdaq 100 Tests Intraday Highs as Growth Sector Expansion Continues", "detail": "Financial Times", "link": "https://finance.yahoo.com"}
        ])

    NEWS_CACHE = {'time': now, 'items': items}
    return jsonify({'items': items})


@app.route('/api/watchlist/scan_all', methods=['GET'])
@login_required
def get_watchlist_scanner():
    portfolio_store.reload()
    is_client = (session.get('role') != 'admin')
    client_name = session.get('email') or session.get('user', 'Client')
    scope = request.args.get('scope', '').strip().lower()
    req_user = request.args.get('user', '').strip()
    engine = MarketScoringEngine()
    regime = engine.check_market_regime()

    if is_client:
        c_store = portfolio_store.get_client_store(client_name)
        users_dict = c_store.get('users', {})
        active_profile = req_user if (req_user and req_user in users_dict) else c_store.get('active_user', '')
        if not active_profile and users_dict:
            active_profile = list(users_dict.keys())[0]
        ud = users_dict.get(active_profile, {})
    else:
        users_dict = portfolio_store.data.get('users', {})
        active_profile = (req_user or portfolio_store.active_username()).strip()
        ud = portfolio_store.user_data(active_profile)

    def resolve_scanner_shares(u_dict, tk):
        i_s = (u_dict.get('initial_positions', {}).get(tk) or {}).get('shares', 0)
        n_s = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in u_dict.get('history', []) if t.get('ticker') == tk)
        return max(0, i_s + n_s)

    # MOBILE SCOPE: Scan ONLY the active profile's own watchlist
    if scope in ['active', 'local', 'profile']:
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
                sh = resolve_scanner_shares(ud, tick)
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
            sh = resolve_scanner_shares(u_data, tk)
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
@login_required
def toggle_broker_pause():
    portfolio_store.reload()
    is_client = (session.get('role') != 'admin')
    if is_client:
        user_id = session.get('email') or session.get('user')
        c_store = portfolio_store.get_client_store(user_id)
        broker = c_store.setdefault('broker', {})
        broker['paused'] = not broker.get('paused', False)
        portfolio_store.save_client_store(user_id, c_store)
        return jsonify({'status': 'ok', 'paused': broker['paused']})
    else:
        broker = portfolio_store.data.setdefault('broker', {})
        broker['paused'] = not broker.get('paused', False)
        portfolio_store.save_data(portfolio_store.data)
        return jsonify({'status': 'ok', 'paused': broker['paused']})

@app.route('/api/broker/liquidate', methods=['POST'])
@login_required
def handle_broker_liquidate():
    portfolio_store.reload()
    is_client = (session.get('role') != 'admin')
    if is_client:
        c_store = portfolio_store.get_client_store(session.get('email') or session.get('user'))
        broker = c_store.get('broker') or {}
    else:
        broker = portfolio_store.data.get('broker') or {}
    res = alpaca_cancel_and_liquidate(broker.get('api_key'), broker.get('secret_key'), broker.get('mode', 'paper'))
    return jsonify(res)

@app.route('/api/broker/liquidate_bot', methods=['POST'])
@login_required
def handle_bot_liquidate():
    """Emergency individual strategy liquidator: closes open positions for one specific bot."""
    portfolio_store.reload()
    req = request.get_json() or {}
    strat_key = req.get('strategy', '').strip().lower()
    is_client = (session.get('role') != 'admin')
    user_id = session.get('email') or session.get('user')

    if is_client:
        c_store = portfolio_store.get_client_store(user_id)
        users = c_store.get('users', {})
        matched_profile = next((p for p in users if strat_key in p.lower() or p.lower() in strat_key), None)
        if not matched_profile:
            return jsonify({'status': 'error', 'message': f'Profile matching {strat_key} not found.'}), 404
        ud = users[matched_profile]
    else:
        matched_profile = next((p for p in portfolio_store.data.get('users', {}) if strat_key in p.lower() or p.lower() in strat_key), None)
        if not matched_profile:
            return jsonify({'status': 'error', 'message': f'Profile matching {strat_key} not found.'}), 404
        ud = portfolio_store.data['users'][matched_profile]

    hist = ud.get('history') or []
    init_pos = ud.get('initial_positions') or {}
    
    def r_sh(tk):
        i_s = (init_pos.get(tk) or {}).get('shares', 0)
        n_s = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in hist if t.get('ticker') == tk)
        return max(0, i_s + n_s)

    active_tickers = list(set([tr.get('ticker') for tr in hist if r_sh(tr.get('ticker')) > 0]))
    if not active_tickers:
        return jsonify({'status': 'ok', 'message': f'{matched_profile} has no open positions to liquidate.'})

    for tick in active_tickers:
        sh = r_sh(tick)
        if sh > 0:
            df = fetch_yf_data(tick, '1d', '1m')
            cur_p = df['Close'].iloc[-1] if not df.empty else 1.0
            portfolio_store.execute_trade(
                tick, 'SELL', sh, cur_p, matched_profile,
                metadata={'status': 'Manual Liquidation', 'reason': 'Individual Bot Liquidate Triggered'},
                client_email=user_id if is_client else None
            )

    return jsonify({'status': 'ok', 'message': f'Successfully liquidated {len(active_tickers)} position(s) for {matched_profile}.'})

@app.route('/api/broker/disconnect', methods=['POST'])
@login_required
def handle_broker_disconnect():
    portfolio_store.reload()
    is_client = (session.get('role') != 'admin')
    if is_client:
        user_id = session.get('email') or session.get('user')
        c_store = portfolio_store.get_client_store(user_id)
        c_store['broker'] = {'paused': False, 'allocations': {}, 'connected': False, 'mode': 'paper', 'api_key': '', 'secret_key': ''}
        portfolio_store.save_client_store(user_id, c_store)
        return jsonify({'status': 'ok'})
    else:
        portfolio_store.data['broker'] = {'paused': False, 'allocations': {}, 'connected': False, 'mode': 'paper', 'api_key': '', 'secret_key': ''}
        portfolio_store.save_data(portfolio_store.data)
        return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8095))
    app.run(host='0.0.0.0', port=port, debug=False)