import os, json, time, urllib.request, threading, re, socket, gc, math
socket.setdefaulttimeout(6)

def _sanitize_json_payload(obj):
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return 0.0 if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {str(k): _sanitize_json_payload(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize_json_payload(v) for v in obj]
    if hasattr(obj, 'item'):
        try:
            return _sanitize_json_payload(obj.item())
        except Exception:
            return str(obj)
    return str(obj)
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

# The account used to feed live performance stats into the public Strategy Wizard
PAPER_STATS_ACCOUNT = "ian_paper@gmail.com"
PERFORMANCE_START_DATE = "2026-11-16"  # Baseline reset date: all profiles treat this morning as Day 1
MONGO_DB_NAME = os.environ.get('MONGO_DB_NAME', 'stock_terminal')

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
        # Prune stale cache entries to prevent unbounded RAM growth on 512MB instances
        if len(YF_CACHE) > 24:
            stale_keys = [k for k, (ts, _) in list(YF_CACHE.items()) if (now - ts) > 180]
            for k in stale_keys:
                YF_CACHE.pop(k, None)

        cache_duration = 8 if interval in ['1m', '2m'] else (15 if interval == '5m' else 180)
        
        if cache_key in YF_CACHE:
            cached_time, df = YF_CACHE[cache_key]
            if not df.empty and (now - cached_time < cache_duration): 
                return df.copy()

        try:
            try:
                df = yf.Ticker(ticker).history(period=period, interval=interval, timeout=5)
            except TypeError:
                df = yf.Ticker(ticker).history(period=period, interval=interval)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = df.dropna(subset=[c for c in ['Open', 'High', 'Low', 'Close'] if c in df.columns])
                df = df.sort_index()
                # Reject stale intraday responses (>240s old during US session) & never overwrite fresher cache with older Yahoo edge data
                if interval in ['1m', '2m', '5m', '15m']:
                    last_ts = df.index[-1]
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.tz_localize('UTC')
                    if cache_key in YF_CACHE:
                        c_time, c_df = YF_CACHE[cache_key]
                        if not c_df.empty:
                            c_last = c_df.index[-1]
                            if c_last.tzinfo is None:
                                c_last = c_last.tz_localize('UTC')
                            if last_ts < c_last and (now - c_time) < 120:
                                return c_df.copy()
                    now_lon_chk = pd.Timestamp.now(tz='Europe/London')
                    mins_lon_chk = now_lon_chk.hour * 60 + now_lon_chk.minute
                    is_us_live_chk = (now_lon_chk.weekday() < 5) and ((14 * 60 + 30) <= mins_lon_chk < (21 * 60))
                    if is_us_live_chk and (now - last_ts.timestamp()) > 240:
                        if cache_key in YF_CACHE:
                            c_time, c_df = YF_CACHE[cache_key]
                            if not c_df.empty and (now - c_time) < 120:
                                return c_df.copy()
                        return pd.DataFrame()
                    if len(df) >= 2:
                        prev_c = float(df['Close'].iloc[-2])
                        last_c = float(df['Close'].iloc[-1])
                        if prev_c > 0 and abs(last_c - prev_c) / prev_c > 0.045:
                            if cache_key in YF_CACHE:
                                c_time, c_df = YF_CACHE[cache_key]
                                if not c_df.empty and (now - c_time) < 90:
                                    return c_df.copy()
                            df = df.iloc[:-1]
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
            
            # Poll until BUY shares settle on Alpaca (up to 6s) before attaching native exchange trailing stop
            if action.lower() == 'buy' and trail_pct:
                settled_qty = 0
                for _ in range(6):
                    time.sleep(1.0)
                    try:
                        req_chk = urllib.request.Request(f"{base_url}/v2/positions/{ticker.upper()}", headers=headers)
                        with urllib.request.urlopen(req_chk, timeout=4) as r_chk:
                            pos_d = json.loads(r_chk.read().decode('utf-8'))
                            settled_qty = int(float(pos_d.get('qty_available') or pos_d.get('qty') or 0))
                            if settled_qty >= int(shares):
                                break
                    except Exception:
                        pass
                stop_qty = min(int(shares), settled_qty) if settled_qty > 0 else int(shares)
                trail_payload = json.dumps({
                    "symbol": ticker.upper(),
                    "qty": stop_qty,
                    "side": "sell",
                    "type": "trailing_stop",
                    "trail_percent": round(trail_pct, 2),
                    "time_in_force": "day",
                    "client_order_id": f"{short_bot_tag}_TRAIL_{ticker.upper()}_{int(time.time())}"
                }).encode('utf-8')
                req_trail = urllib.request.Request(f"{base_url}/v2/orders", data=trail_payload, headers=headers, method="POST")
                with urllib.request.urlopen(req_trail, timeout=6):
                    print(f"[ALPACA TRAIL SECURED] Native {trail_pct}% Stop active on exchange ({stop_qty} sh).")
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
        self.filename = 'portfolio.json'
        self.mongo_uri = os.environ.get('MONGO_URI')
        if self.mongo_uri:
            try:
                self.client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=5000)
                self.collection = self.client[MONGO_DB_NAME]['portfolio']
            except Exception: self.client = None
        else:
            self.client = None
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
                'interval': '1m' if any(x in prof for x in ['1m reversal', 'pulse 1m', 'test s', 'test r', 'test tqm']) else '5m',
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
                accounts_col = self.client[MONGO_DB_NAME]['accounts']
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
                accounts_col = self.client[MONGO_DB_NAME]['accounts']
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
            except Exception:
                pass
            initial = {'active_user': 'Vector Hybrid', 'users': {'Vector Hybrid': self.default_user_state('Vector Hybrid')}}
            self.save_data(initial)
            return initial
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    if 'users' not in data:
                        data = {'active_user': 'Vector Hybrid', 'users': {'Vector Hybrid': data}}
                        self.save_data(data)
                    return data
            except Exception:
                pass
        initial = {'active_user': 'Vector Hybrid', 'users': {'Vector Hybrid': self.default_user_state('Vector Hybrid')}}
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

            # Purge legacy prototype profiles (e.g. Test A, Test C, Test E - Rotator) and leaked emails
            legacy_prototypes = {'test e', 'test s', 'test r', 'test tqm', 'test m', 'test u', 'test w', 'test x', 'test p', 'test t', 'ian', 'sovereign core'}
            existing_users = list(self.data['users'].keys())
            for u in existing_users:
                u_clean = u.strip().lower()
                if u_clean in legacy_prototypes or u_clean.startswith('test ') or '@' in u_clean:
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
                # Use a 2.50% disaster-only exchange stop on Alpaca so sub-second 3x ETF spread flickers never snipe positions;
                # IDC's 8-second candle engine manages the exact 0.65%/0.75% trailing stop, breakeven lock, and ratchet floors.
                t_pct = 2.50

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

            # Only send iPhone push alerts for primary admin account (silence background recon/client accounts)
            if not is_client:
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
    def get_macro_pulse(self):
        global NEWS_CACHE
        now = time.time()
        cached = NEWS_CACHE.get('_macro_pulse')
        if cached and (now - cached.get('ts', 0)) < 45:
            return cached['data']

        out = {
            'nq': {'pct': 0.0, 'text': '0.00% ►', 'color': '#8a8a9e'},
            'es': {'pct': 0.0, 'text': '0.00% ►', 'color': '#8a8a9e'},
            'vix': {'val': 15.0, 'text': '15.00 Calm', 'color': '#ffb700'}
        }
        try:
            def _fetch_m(sym):
                return sym, fetch_yf_data(sym, '5d', '1d')

            with ThreadPoolExecutor(max_workers=3) as ex:
                res_map = dict(ex.map(_fetch_m, ['NQ=F', 'ES=F', '^VIX']))

            for key, sym in [('nq', 'NQ=F'), ('es', 'ES=F')]:
                df_m = res_map.get(sym)
                if df_m is not None and len(df_m) >= 2:
                    prev_c = float(df_m['Close'].iloc[-2])
                    cur_c = float(df_m['Close'].iloc[-1])
                    if prev_c > 0:
                        pct = ((cur_c - prev_c) / prev_c) * 100.0
                        arrow = '▲' if pct > 0.02 else ('▼' if pct < -0.02 else '►')
                        col = '#0ecb81' if pct > 0.02 else ('#ff5364' if pct < -0.02 else '#8a8a9e')
                        sign = '+' if pct > 0 else ''
                        out[key] = {'pct': round(pct, 2), 'text': f"{sign}{pct:.2f}% {arrow}", 'color': col}

            df_vix = res_map.get('^VIX')
            if df_vix is not None and not df_vix.empty:
                v_val = float(df_vix['Close'].iloc[-1])
                if v_val < 16:
                    v_lbl, v_col = 'Calm', '#0ecb81'
                elif v_val < 22:
                    v_lbl, v_col = 'Normal', '#ffb700'
                elif v_val < 30:
                    v_lbl, v_col = 'Elevated', '#ff9900'
                else:
                    v_lbl, v_col = 'High Fear', '#ff5364'
                out['vix'] = {'val': round(v_val, 2), 'text': f"{v_val:.2f} {v_lbl}", 'color': v_col}
        except Exception:
            pass

        NEWS_CACHE['_macro_pulse'] = {'ts': now, 'data': out}
        return out

    def check_market_regime(self):
        try:
            df_qqq = fetch_yf_data('QQQ', '5d', '5m')
            if not df_qqq.empty and len(df_qqq) >= 21:
                now_uk_r = pd.Timestamp.now(tz='Europe/London')
                df_q_tmp = df_qqq.copy()
                if df_q_tmp.index.tz is None:
                    df_q_tmp.index = df_q_tmp.index.tz_localize('UTC')
                today_q = df_q_tmp[df_q_tmp.index.tz_convert('Europe/London').strftime('%Y-%m-%d') == now_uk_r.strftime('%Y-%m-%d')]
                q_src = today_q['Close'] if len(today_q) >= 4 else df_qqq['Close']

                ema9 = q_src.ewm(span=9, adjust=False).mean()
                ema21 = q_src.ewm(span=21, adjust=False).mean()
                delta = df_qqq['Close'].diff()
                gain = delta.where(delta > 0, 0).rolling(14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                rs = gain / loss.replace(0, float('nan'))
                rsi_full = (100 - (100 / (1 + rs))).fillna(50)
                rsi_aligned = rsi_full.reindex(q_src.index).fillna(50)
                scores = (50 + (ema9 > ema21).astype(int)*50 - 25 + (rsi_aligned - 50)*0.5).clip(0, 100).round(1)
                
                score = int(scores.iloc[-1])
                ema9_5d = df_qqq['Close'].ewm(span=9, adjust=False).mean()
                ema21_5d = df_qqq['Close'].ewm(span=21, adjust=False).mean()
                scores_5d = (50 + (ema9_5d > ema21_5d).astype(int)*50 - 25 + (rsi_full - 50)*0.5).clip(0, 100).round(1)
                sparkline = [round(x, 1) for x in scores_5d.tail(78).tolist()]
                
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

    def evaluate_opening_window(self, df_5m, current_price, ticker, is_inverse, prof, regime, pct_change_5d):
        """14:30-14:50 UK Opening Correlation Engine:
        - 14:30-14:33 UK: 3-minute opening spike calibration (lets market-on-open orders settle).
        - 14:33-14:50 UK (Small Gaps 0.15% to 0.65% 1x-equiv): Gap-Fade Reversal (~80% historical fill odds).
          * Small Gap Up (+0.15% to +0.65%): Buy SQQQ when index crosses back below 14:30 open.
          * Small Gap Down (-0.65% to -0.15%): Buy TQQQ/SOXL when index crosses back above 14:30 open.
        - 14:45-14:50 UK (Big Gaps > 0.65% 1x-equiv): 15-Minute Opening Range Breakout (ORB).
          * Big Gap Up (> +0.65%): Buy TQQQ/SOXL on breakout above 14:30-14:45 Opening Range High.
          * Big Gap Down (< -0.65%): Buy SQQQ on breakdown below 14:30-14:45 Opening Range Low.
        """
        trade_type = 'SHORT' if is_inverse else 'LONG'
        now_uk = pd.Timestamp.now(tz='Europe/London')
        today_uk_str = now_uk.strftime('%Y-%m-%d')

        use_qqq_ref = ticker in ['TQQQ', 'SQQQ']
        df_ref = fetch_yf_data('QQQ', '5d', '5m') if use_qqq_ref else df_5m
        if df_ref is None or df_ref.empty:
            df_ref = df_5m

        df_r = df_ref.copy()
        if df_r.index.tz is None:
            df_r.index = df_r.index.tz_localize('UTC')
        df_r_uk = df_r.index.tz_convert('Europe/London')
        prev_bars = df_r[df_r_uk.strftime('%Y-%m-%d') < today_uk_str]
        today_bars = df_r[df_r_uk.strftime('%Y-%m-%d') == today_uk_str]

        df_tk_1m = fetch_yf_data(ticker, '1d', '1m')
        tk_open_1430 = None
        if df_tk_1m is not None and not df_tk_1m.empty:
            df_tk1 = df_tk_1m.copy()
            if df_tk1.index.tz is None:
                df_tk1.index = df_tk1.index.tz_localize('UTC')
            tk1_today = df_tk1[df_tk1.index.tz_convert('Europe/London').strftime('%Y-%m-%d') == today_uk_str]
            if not tk1_today.empty:
                tk_open_1430 = float(tk1_today['Open'].iloc[0])
        if tk_open_1430 is None and not df_5m.empty:
            df_t5 = df_5m.copy()
            if df_t5.index.tz is None:
                df_t5.index = df_t5.index.tz_localize('UTC')
            t5_today = df_t5[df_t5.index.tz_convert('Europe/London').strftime('%Y-%m-%d') == today_uk_str]
            if not t5_today.empty:
                tk_open_1430 = float(t5_today['Open'].iloc[0])
        if tk_open_1430 is None:
            tk_open_1430 = current_price

        if prev_bars.empty or today_bars.empty:
            return {'type': 'Opening Window', 'score': 25, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': 'Opening Window: Syncing 14:30 opening print vs prior close.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Opening Sync', 'color': '#ffd400', 'action_main': 'HOLD / WAIT', 'action_sub': '(Opening Sync)', 'action_color': '#ffd400', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ffd400', 'health_text': 'Opening Sync'}

        prev_close = float(prev_bars['Close'].iloc[-1])
        open_1430 = float(today_bars['Open'].iloc[0])
        cur_ref = float(today_bars['Close'].iloc[-1])
        if use_qqq_ref:
            df_qqq_1m = fetch_yf_data('QQQ', '1d', '1m')
            if df_qqq_1m is not None and not df_qqq_1m.empty:
                df_q1 = df_qqq_1m.copy()
                if df_q1.index.tz is None:
                    df_q1.index = df_q1.index.tz_localize('UTC')
                q1_today = df_q1[df_q1.index.tz_convert('Europe/London').strftime('%Y-%m-%d') == today_uk_str]
                if not q1_today.empty:
                    open_1430 = float(q1_today['Open'].iloc[0])
                    cur_ref = float(q1_today['Close'].iloc[-1])

        raw_gap_pct = ((open_1430 - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
        gap_1x_pct = raw_gap_pct if use_qqq_ref else (raw_gap_pct / 3.0)
        ref_label = 'QQQ' if use_qqq_ref else f'{ticker} (1x eq)'

        if now_uk.minute < 33:
            return {
                'type': 'Opening Window',
                'score': 35,
                'tranches': 0,
                'discount': f"{gap_1x_pct:+.2f}%",
                'reason': f"3m Spike Filter ({ref_label} Gap {gap_1x_pct:+.2f}%): Letting opening bell orders settle until 14:33 UK.",
                'is_smart': True,
                'rec_buy': current_price,
                'rec_sell': current_price,
                'status': '3m Spike Filter',
                'color': '#ffd400',
                'action_main': 'HOLD / WAIT',
                'action_sub': '(3m Spike Wait)',
                'action_color': '#ffd400',
                'regime': regime,
                'trade_type': trade_type,
                'health_pct': 0,
                'health_color': '#ffd400',
                'health_text': '3m Spike Filter'
            }

        tk_1m_confirmed = True
        if df_tk_1m is not None and not df_tk_1m.empty and len(df_tk_1m) >= 2:
            tk_1m_confirmed = (current_price > float(df_tk_1m['Open'].iloc[-1])) and (current_price > float(df_tk_1m['Close'].iloc[-2]))

        # OPENING BREAKAWAY OVERRIDE (14:33 - 14:50 UK): Catch explosive >=1.20% surges off today's session low above the 3m opening range
        if df_tk_1m is not None and not df_tk_1m.empty:
            df_tk1_b = df_tk_1m.copy()
            if df_tk1_b.index.tz is None:
                df_tk1_b.index = df_tk1_b.index.tz_localize('UTC')
            tk1_b_uk = df_tk1_b.index.tz_convert('Europe/London')
            tk1_b_today = df_tk1_b[tk1_b_uk.strftime('%Y-%m-%d') == today_uk_str]
            if len(tk1_b_today) >= 4:
                sess_low_1m = float(tk1_b_today['Low'].min())
                sess_high_1m = float(tk1_b_today['High'].max())
                open_3m_bars = tk1_b_today[tk1_b_uk[tk1_b_uk.strftime('%Y-%m-%d') == today_uk_str].minute < 33]
                open_3m_high = float(open_3m_bars['High'].max()) if not open_3m_bars.empty else float(tk1_b_today['High'].iloc[:3].max())
                surge_from_low_pct = ((current_price - sess_low_1m) / sess_low_1m * 100.0) if sess_low_1m > 0 else 0.0
                drop_from_high_pct = ((sess_high_1m - current_price) / sess_high_1m * 100.0) if sess_high_1m > 0 else 0.0
                bar1_green = (current_price > float(tk1_b_today['Open'].iloc[-1])) and (current_price > float(tk1_b_today['Close'].iloc[-2]))
                two_green_1m = bar1_green and (float(tk1_b_today['Close'].iloc[-2]) > float(tk1_b_today['Open'].iloc[-2]))
                ema9_1m_s = tk1_b_today['Close'].ewm(span=9, adjust=False).mean()
                ema21_1m_s = tk1_b_today['Close'].ewm(span=21, adjust=False).mean()
                ema1m_aligned = (current_price > float(ema9_1m_s.iloc[-1])) and (float(ema9_1m_s.iloc[-1]) > float(ema21_1m_s.iloc[-1])) and (float(ema9_1m_s.iloc[-1]) > float(ema9_1m_s.iloc[-2]))
                vol_med_1m = float(tk1_b_today['Volume'].iloc[:-1].median()) if len(tk1_b_today) >= 4 else 1.0
                secs_in_1m = max(10, now_uk.second)
                raw_vol_1m = float(tk1_b_today['Volume'].iloc[-1])
                proj_vol_1m = min(raw_vol_1m * (60.0 / secs_in_1m), raw_vol_1m * 3.0)
                cur_vol_1m = max(raw_vol_1m, proj_vol_1m)
                if (1.20 <= surge_from_low_pct <= 4.00) and (drop_from_high_pct <= 0.30) and (current_price > open_3m_high) and (two_green_1m or (bar1_green and ema1m_aligned)) and (vol_med_1m <= 0 or cur_vol_1m >= vol_med_1m * 1.05):
                    brk_score = min(94, max(80, round(76 + surge_from_low_pct * 6)))
                    return {
                        'type': 'Opening Breakaway',
                        'score': brk_score,
                        'tranches': 1,
                        'discount': f"+{surge_from_low_pct:.2f}%",
                        'reason': f"OPENING BREAKAWAY ({ticker} +{surge_from_low_pct:.2f}% off ${sess_low_1m:.2f} low): Cleared 3m range (${open_3m_high:.2f}) on 2 green 1m bars.",
                        'is_smart': True,
                        'rec_buy': round(current_price * 0.995, 2),
                        'rec_sell': round(current_price * 1.02, 2),
                        'status': 'Opening Breakaway',
                        'color': '#0ecb81',
                        'action_main': 'BUY',
                        'action_sub': '(Opening Breakaway)',
                        'action_color': '#0ecb81',
                        'regime': regime,
                        'trade_type': trade_type,
                        'health_pct': 0,
                        'health_color': '#0ecb81',
                        'health_text': 'Opening Breakaway'
                    }

        if 0.15 <= gap_1x_pct <= 0.65:
            not_filled_yet = (cur_ref > prev_close * 1.0005)
            crossed_below_open = (cur_ref <= open_1430 * 0.9992) and (current_price >= tk_open_1430 * 1.0010 if is_inverse else current_price <= tk_open_1430 * 0.9990)
            if is_inverse and crossed_below_open and not_filled_yet and tk_1m_confirmed:
                fade_score = min(92, max(74, round(74 + gap_1x_pct * 25)))
                return {
                    'type': 'Opening Gap-Fade',
                    'score': fade_score,
                    'tranches': 1,
                    'discount': f"{gap_1x_pct:+.2f}%",
                    'reason': f"GAP-FADE SHORT ({ref_label} +{gap_1x_pct:.2f}% gap): Reversed below 14:30 open (${open_1430:.2f}) -> Shorting fade toward ${prev_close:.2f}.",
                    'is_smart': True,
                    'rec_buy': round(current_price * 0.995, 2),
                    'rec_sell': round(current_price * 1.015, 2),
                    'status': 'Gap-Fade Short',
                    'color': '#0ecb81',
                    'action_main': 'BUY',
                    'action_sub': '(Gap-Fade Short)',
                    'action_color': '#0ecb81',
                    'regime': regime,
                    'trade_type': trade_type,
                    'health_pct': 0,
                    'health_color': '#0ecb81',
                    'health_text': 'Gap-Fade Trigger'
                }
            wait_msg = (
                f"Gap-Fade Watch ({ref_label} +{gap_1x_pct:.2f}%): Watching for drop below ${open_1430:.2f} open to trigger SQQQ fade."
                if is_inverse else
                f"Gap-Fade Watch ({ref_label} +{gap_1x_pct:.2f}%): Small gap up favours SQQQ fade over {ticker} long."
            )
            return {
                'type': 'Opening Gap-Fade',
                'score': 48 if is_inverse else 20,
                'tranches': 0,
                'discount': f"{gap_1x_pct:+.2f}%",
                'reason': wait_msg,
                'is_smart': True,
                'rec_buy': current_price,
                'rec_sell': current_price,
                'status': 'Gap-Fade Watch',
                'color': '#ffd400' if is_inverse else '#8a8a9e',
                'action_main': 'HOLD / WAIT',
                'action_sub': '(Fade Watch)',
                'action_color': '#ffd400' if is_inverse else '#8a8a9e',
                'regime': regime,
                'trade_type': trade_type,
                'health_pct': 0,
                'health_color': '#ffd400',
                'health_text': 'Gap-Fade Watch'
            }

        if -0.65 <= gap_1x_pct <= -0.15:
            not_filled_yet = (cur_ref < prev_close * 0.9995)
            crossed_above_open = (cur_ref >= open_1430 * 1.0008) and (current_price >= tk_open_1430 * 1.0010 if not is_inverse else current_price <= tk_open_1430 * 0.9990)
            if (not is_inverse) and crossed_above_open and not_filled_yet and tk_1m_confirmed:
                bounce_score = min(92, max(74, round(74 + abs(gap_1x_pct) * 25)))
                return {
                    'type': 'Opening Gap-Bounce',
                    'score': bounce_score,
                    'tranches': 1,
                    'discount': f"{gap_1x_pct:+.2f}%",
                    'reason': f"GAP-BOUNCE LONG ({ref_label} {gap_1x_pct:.2f}% gap): Reclaimed 14:30 open (${open_1430:.2f}) -> Riding bounce toward ${prev_close:.2f}.",
                    'is_smart': True,
                    'rec_buy': round(current_price * 0.995, 2),
                    'rec_sell': round(current_price * 1.015, 2),
                    'status': 'Gap-Bounce Long',
                    'color': '#0ecb81',
                    'action_main': 'BUY',
                    'action_sub': '(Gap-Bounce)',
                    'action_color': '#0ecb81',
                    'regime': regime,
                    'trade_type': trade_type,
                    'health_pct': 0,
                    'health_color': '#0ecb81',
                    'health_text': 'Gap-Bounce Trigger'
                }
            wait_msg = (
                f"Gap-Bounce Watch ({ref_label} {gap_1x_pct:.2f}%): Watching for reclaim above ${open_1430:.2f} open to buy {ticker}."
                if not is_inverse else
                f"Gap-Bounce Watch ({ref_label} {gap_1x_pct:.2f}%): Small dip favours TQQQ/SOXL bounce over SQQQ."
            )
            return {
                'type': 'Opening Gap-Bounce',
                'score': 48 if not is_inverse else 20,
                'tranches': 0,
                'discount': f"{gap_1x_pct:+.2f}%",
                'reason': wait_msg,
                'is_smart': True,
                'rec_buy': current_price,
                'rec_sell': current_price,
                'status': 'Gap-Bounce Watch',
                'color': '#ffd400' if not is_inverse else '#8a8a9e',
                'action_main': 'HOLD / WAIT',
                'action_sub': '(Bounce Watch)',
                'action_color': '#ffd400' if not is_inverse else '#8a8a9e',
                'regime': regime,
                'trade_type': trade_type,
                'health_pct': 0,
                'health_color': '#ffd400',
                'health_text': 'Gap-Bounce Watch'
            }

        if abs(gap_1x_pct) > 0.65:
            if now_uk.minute < 45:
                return {
                    'type': 'Opening 15m ORB',
                    'score': 42,
                    'tranches': 0,
                    'discount': f"{gap_1x_pct:+.2f}%",
                    'reason': f"Big Gap ({ref_label} {gap_1x_pct:+.2f}%): Building 15m Opening Range until 14:45 UK for breakout.",
                    'is_smart': True,
                    'rec_buy': current_price,
                    'rec_sell': current_price,
                    'status': 'Building 15m ORB',
                    'color': '#ffd400',
                    'action_main': 'HOLD / WAIT',
                    'action_sub': '(15m ORB Setup)',
                    'action_color': '#ffd400',
                    'regime': regime,
                    'trade_type': trade_type,
                    'health_pct': 0,
                    'health_color': '#ffd400',
                    'health_text': 'Building 15m ORB'
                }

            or_bars = today_bars.iloc[:3]
            or_high = float(or_bars['High'].max())
            or_low = float(or_bars['Low'].min())
            df_orb_1m = fetch_yf_data('QQQ' if use_qqq_ref else ticker, '1d', '1m')
            if df_orb_1m is not None and not df_orb_1m.empty:
                df_o1 = df_orb_1m.copy()
                if df_o1.index.tz is None:
                    df_o1.index = df_o1.index.tz_localize('UTC')
                o1_uk = df_o1.index.tz_convert('Europe/London')
                o1_pre45 = df_o1[(o1_uk.strftime('%Y-%m-%d') == today_uk_str) & (o1_uk.hour == 14) & (o1_uk.minute < 45)]
                if not o1_pre45.empty:
                    or_high = max(or_high, float(o1_pre45['High'].max()))
                    or_low = min(or_low, float(o1_pre45['Low'].min()))

            if (not is_inverse) and cur_ref > or_high:
                return {
                    'type': 'Opening 15m ORB',
                    'score': 85,
                    'tranches': 1,
                    'discount': f"{gap_1x_pct:+.2f}%",
                    'reason': f"15M ORB BREAKOUT ({ref_label} {gap_1x_pct:+.2f}% gap): Broke above 15m range high (${or_high:.2f}) -> Riding breakout.",
                    'is_smart': True,
                    'rec_buy': round(current_price * 0.995, 2),
                    'rec_sell': round(current_price * 1.02, 2),
                    'status': '15m ORB Breakout',
                    'color': '#0ecb81',
                    'action_main': 'BUY',
                    'action_sub': '(15m ORB Long)',
                    'action_color': '#0ecb81',
                    'regime': regime,
                    'trade_type': trade_type,
                    'health_pct': 0,
                    'health_color': '#0ecb81',
                    'health_text': '15m ORB Breakout'
                }
            if is_inverse and cur_ref < or_low:
                return {
                    'type': 'Opening 15m ORB',
                    'score': 85,
                    'tranches': 1,
                    'discount': f"{gap_1x_pct:+.2f}%",
                    'reason': f"15M ORB BREAKDOWN ({ref_label} {gap_1x_pct:+.2f}% gap): Broke below 15m range low (${or_low:.2f}) -> Riding breakdown.",
                    'is_smart': True,
                    'rec_buy': round(current_price * 0.995, 2),
                    'rec_sell': round(current_price * 1.02, 2),
                    'status': '15m ORB Breakdown',
                    'color': '#0ecb81',
                    'action_main': 'BUY',
                    'action_sub': '(15m ORB Short)',
                    'action_color': '#0ecb81',
                    'regime': regime,
                    'trade_type': trade_type,
                    'health_pct': 0,
                    'health_color': '#0ecb81',
                    'health_text': '15m ORB Breakdown'
                }

            return {
                'type': 'Opening 15m ORB',
                'score': 45,
                'tranches': 0,
                'discount': f"{gap_1x_pct:+.2f}%",
                'reason': f"15m ORB Ready ({ref_label} {gap_1x_pct:+.2f}%): Watching ${or_low:.2f} – ${or_high:.2f} range breakout.",
                'is_smart': True,
                'rec_buy': current_price,
                'rec_sell': current_price,
                'status': '15m ORB Watch',
                'color': '#ffd400',
                'action_main': 'HOLD / WAIT',
                'action_sub': '(15m ORB Watch)',
                'action_color': '#ffd400',
                'regime': regime,
                'trade_type': trade_type,
                'health_pct': 0,
                'health_color': '#ffd400',
                'health_text': '15m ORB Watch'
            }

        return {
            'type': 'Opening Window',
            'score': 25,
            'tranches': 0,
            'discount': f"{gap_1x_pct:+.2f}%",
            'reason': f"Flat Open ({ref_label} {gap_1x_pct:+.2f}% gap < 0.15%): No overnight gap edge — awaiting 14:50 trend window.",
            'is_smart': True,
            'rec_buy': current_price,
            'rec_sell': current_price,
            'status': 'Flat Open Wait',
            'color': '#8a8a9e',
            'action_main': 'HOLD / WAIT',
            'action_sub': '(Flat Open)',
            'action_color': '#8a8a9e',
            'regime': regime,
            'trade_type': trade_type,
            'health_pct': 0,
            'health_color': '#8a8a9e',
            'health_text': 'Flat Open Wait'
        }

    def get_pulse_1m_snapshot(self, ticker, hist=None, current_price=0.0):
        """Returns last 5 completed 1m bars + live bar with pattern roles and BUY/SELL annotations for Pulse 1M Reversal."""
        df_1m = fetch_yf_data(ticker, "1d", "1m")
        if df_1m is None or df_1m.empty or len(df_1m) < 6:
            df_1m = fetch_yf_data(ticker, "5d", "1m")
        if df_1m is None or df_1m.empty or len(df_1m) < 3:
            return {'bars': [], 'pattern_stage': 'Awaiting 1m Data', 'stage_color': '#8a8a9e', 'last_bar_chg': 0.0, 'last_bar_pct': 0.0, 'live_bar_chg': 0.0, 'avg_body': 0.0, 'avg_pct': 0.0, 'step_idx': 0}

        tail_df = df_1m.tail(6).copy()
        if tail_df.index.tz is None:
            tail_df.index = tail_df.index.tz_localize('UTC')
        else:
            tail_df.index = tail_df.index.tz_convert('UTC')

        tk_trades = []
        for tr in (hist or []):
            if (tr.get('ticker') or '').upper() == ticker.upper() and tr.get('action') in ['BUY', 'SELL']:
                tk_trades.append({
                    'action': tr.get('action'),
                    'price': float(tr.get('price') or 0.0),
                    'ts': int(tr.get('timestamp') or 0),
                    'time': tr.get('time', '')
                })

        raw_bars = []
        n_rows = len(tail_df)
        for idx_i in range(n_rows):
            row = tail_df.iloc[idx_i]
            ts_utc = tail_df.index[idx_i]
            ts_uk = ts_utc.tz_convert('Europe/London')
            bar_ts = int(ts_utc.timestamp())
            if pd.isna(row.get('Open')) or pd.isna(row.get('Close')):
                continue
            o_p = round(float(row['Open']), 2)
            h_p = round(float(row['High']) if pd.notna(row.get('High')) else max(o_p, float(row['Close'])), 2)
            l_p = round(float(row['Low']) if pd.notna(row.get('Low')) else min(o_p, float(row['Close'])), 2)
            c_p = round(float(row['Close']), 2)
            if idx_i == n_rows - 1 and current_price > 0:
                c_p = round(float(current_price), 2)
                h_p = max(h_p, c_p)
                l_p = min(l_p, c_p)
            is_live = (idx_i == n_rows - 1)
            is_doji = (c_p == o_p)
            is_green = (c_p > o_p)
            body = round(abs(c_p - o_p), 4)

            bar_annots = []
            for tr in tk_trades:
                if bar_ts <= tr['ts'] < (bar_ts + 60):
                    bar_annots.append({'action': tr['action'], 'price': tr['price']})

            raw_bars.append({
                'ts': bar_ts,
                'time_uk': ts_uk.strftime('%H:%M'),
                'open': o_p,
                'high': h_p,
                'low': l_p,
                'close': c_p,
                'is_green': is_green,
                'is_live': is_live,
                'body': body,
                'tag': 'LIVE' if is_live else ('GREEN' if is_green else 'RED'),
                'annots': bar_annots
            })

        closed_bars = [b for b in raw_bars if not b['is_live']]
        dir_bars = [b for b in closed_bars if b['body'] > 0]
        for j in range(len(closed_bars)):
            b = closed_bars[j]
            if b['body'] == 0:
                b['tag'] = 'DOJI'
                continue
            d_idx = dir_bars.index(b)
            if not b['is_green']:
                if d_idx + 1 < len(dir_bars) and (not dir_bars[d_idx + 1]['is_green']) and (0 < dir_bars[d_idx + 1]['body'] < b['body']):
                    b['tag'] = 'BIG RED'
                elif d_idx > 0 and (not dir_bars[d_idx - 1]['is_green']) and (0 < b['body'] < dir_bars[d_idx - 1]['body']):
                    b['tag'] = 'SML RED'
                elif d_idx > 0 and dir_bars[d_idx - 1]['is_green']:
                    b['tag'] = 'STOP RED'
                else:
                    b['tag'] = 'RED'
            else:
                if d_idx >= 2 and dir_bars[d_idx - 1]['tag'] == 'SML RED':
                    b['tag'] = 'GO GRN'
                else:
                    b['tag'] = 'GREEN'

        stage_txt = 'Scanning 1m'
        stage_col = '#8a8a9e'
        step_idx = 0
        if len(dir_bars) >= 3:
            c1, c2, c3 = dir_bars[-3], dir_bars[-2], dir_bars[-1]
            if (not c1['is_green']) and (not c2['is_green']) and (0 < c2['body'] < c1['body']) and c3['is_green']:
                stage_txt = '3/3 GO TRIGGER'
                stage_col = '#0ecb81'
                step_idx = 3
            elif (not c2['is_green']) and (not c3['is_green']) and (0 < c3['body'] < c2['body']):
                stage_txt = '2/3 AWAITING GREEN'
                stage_col = '#ffd400'
                step_idx = 2
            elif not c3['is_green']:
                stage_txt = '1/3 BIG RED'
                stage_col = '#ff9900'
                step_idx = 1
            else:
                stage_txt = 'GREEN WAVE'
                stage_col = '#38bdf8'
                step_idx = 0

        last_closed = closed_bars[-1] if closed_bars else raw_bars[-1]
        live_b = raw_bars[-1]
        last_chg = round(last_closed['close'] - last_closed['open'], 2)
        last_pct = round((last_chg / last_closed['open'] * 100.0), 2) if last_closed['open'] > 0 else 0.0
        live_chg = round(live_b['close'] - live_b['open'], 2)
        avg_body = round(sum(b['body'] for b in closed_bars) / max(1, len(closed_bars)), 2)
        avg_pct = round(sum((b['body'] / b['open'] * 100.0) if b['open'] > 0 else 0.0 for b in closed_bars) / max(1, len(closed_bars)), 2)

        return {
            'bars': raw_bars,
            'pattern_stage': stage_txt,
            'stage_color': stage_col,
            'step_idx': step_idx,
            'last_bar_chg': last_chg,
            'last_bar_pct': last_pct,
            'live_bar_chg': live_chg,
            'avg_body': avg_body,
            'avg_pct': avg_pct
        }

    def score_pulse_1m_reversal(self, ticker, current_price, avg_buy_price=0.0, highest_price=0.0, regime=None, overnight_sweep=True, buy_timestamp=0):
        """1-Minute Pure Candle Reversal (Pulse 1M Reversal) across TQQQ, SOXL & SQQQ:
        - Entry (GO): Red (-4) -> Smaller Red (-3) -> Green (-2) on completed 1-minute candles.
        - Exit (STOP): First completed 1-minute Red candle (-2) that closes AFTER entry (or 20:50 UK overnight sweep).
        """
        if regime is None:
            regime = {'score': 50, 'state': 'Room Temp (Neutral)', 'color': '#8a8a9e', 'code': 'NEUTRAL', 'sparkline': [], 'trend': '► Stable'}
        is_inverse = ticker in ['SQQQ', '3SUS.L']
        trade_type = 'SHORT' if is_inverse else 'LONG'
        now_uk = pd.Timestamp.now(tz='Europe/London')

        df_1m = fetch_yf_data(ticker, "1d", "1m")
        if df_1m is None or df_1m.empty or len(df_1m) < 6:
            return {'type': '1m Reversal', 'score': 0, 'tranches': 0, 'discount': '0.00%', 'reason': 'Awaiting 1m candle history.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(1m Data)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'No Data'}

        pct_change_1d = ((current_price - float(df_1m['Close'].iloc[0])) / float(df_1m['Close'].iloc[0])) * 100.0 if float(df_1m['Close'].iloc[0]) > 0 else 0.0

        # Holding position: SELL immediately on the first completed red 1-minute candle that closed AFTER entry
        if avg_buy_price > 0:
            pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
            pnl_str = f"({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)"

            if overnight_sweep and now_uk.hour == 20 and now_uk.minute >= 50:
                return {'type': 'EOD Sweep', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': 'OVERNIGHT SWEEP: Liquidating to 100% cash.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Overnight Sweep', 'color': '#ff9900', 'action_main': 'SELL', 'action_sub': '(Overnight Sweep)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'Overnight Sweep'}

            prev_bar_close_ts = int(df_1m.index[-2].timestamp()) + 60
            c_prev = float(df_1m['Close'].iloc[-2])
            o_prev = float(df_1m['Open'].iloc[-2])
            if c_prev < o_prev and (int(buy_timestamp or 0) <= 0 or prev_bar_close_ts > int(buy_timestamp)):
                return {'type': '1m Reversal', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f'1M RED STOP {pnl_str}: 1m candle closed red (${o_prev:.2f} -> ${c_prev:.2f}).', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': '1m Red Stop', 'color': '#ff5364', 'action_main': 'SELL', 'action_sub': '(1m Red Stop)', 'action_color': '#ff5364', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff5364', 'health_text': f'1m Red Stop {pnl_str}'}

            return {'type': '1m Reversal', 'score': 80, 'tranches': 1, 'discount': f"{pnl_pct:.2f}%", 'reason': f'RIDING GREEN 1M WAVE {pnl_str}: Holding until first closed red 1m candle.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Riding 1m Green', 'color': '#0ecb81', 'action_main': 'HOLD / WAIT', 'action_sub': '(Riding 1m Green)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 85, 'health_color': '#0ecb81', 'health_text': f'Riding 1m Green {pnl_str}'}

        if overnight_sweep and now_uk.hour == 20 and now_uk.minute >= 50:
            return {'type': 'EOD Sweep', 'score': 0, 'tranches': 0, 'discount': f"{pct_change_1d:.2f}%", 'reason': 'OVERNIGHT SWEEP: Blocking new entries before close.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'EOD Block Active', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(EOD Blocked)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'EOD Blocked'}

        # Filter to today's regular US session (>= 14:30 UK) so overnight gaps never pair yesterday's 20:59 bar with today's open
        df_1m_uk = df_1m.copy()
        if df_1m_uk.index.tz is None:
            df_1m_uk.index = df_1m_uk.index.tz_localize('UTC')
        idx_lon_1m = df_1m_uk.index.tz_convert('Europe/London')
        today_reg_1m = df_1m_uk[
            (idx_lon_1m.strftime('%Y-%m-%d') == now_uk.strftime('%Y-%m-%d'))
            & ((idx_lon_1m.hour > 14) | ((idx_lon_1m.hour == 14) & (idx_lon_1m.minute >= 30)))
        ]

        # Need at least 2 completed regular-session 1m candles (14:30 & 14:31 -> ready at 14:32:00)
        if now_uk.weekday() < 5 and now_uk.hour == 14 and 30 <= now_uk.minute < 32:
            return {'type': 'Opening Window', 'score': 30, 'tranches': 0, 'discount': f"{pct_change_1d:.2f}%", 'reason': '2-Bar Seeding (14:30–14:32 UK): Building first 2 regular-session 1m candles.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': '2-Bar Seeding', 'color': '#ffd400', 'action_main': 'HOLD / WAIT', 'action_sub': '(2-Bar Seeding)', 'action_color': '#ffd400', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ffd400', 'health_text': '2-Bar Seeding'}

        # Early 3rd-bar catch at 14:32:xx: if 14:30 (Big Red) & 14:31 (Smaller Red) closed and 14:32 is actively breaking green above 14:31 close
        if len(today_reg_1m) == 3:
            b_r1_o, b_r1_c = float(today_reg_1m['Open'].iloc[0]), float(today_reg_1m['Close'].iloc[0])
            b_r2_o, b_r2_c = float(today_reg_1m['Open'].iloc[1]), float(today_reg_1m['Close'].iloc[1])
            b_g1_o, b_g1_c = float(today_reg_1m['Open'].iloc[2]), float(current_price)
            r1_b = b_r1_o - b_r1_c
            r2_b = b_r2_o - b_r2_c
            g1_b = b_g1_c - b_g1_o
            if (b_r1_c < b_r1_o) and (r1_b > 0) and (b_r2_c < b_r2_o) and (0 < r2_b < r1_b) and (b_g1_c > b_g1_o) and (b_g1_c > b_r2_c) and (g1_b > 0):
                bounce_pct = ((b_g1_c - b_g1_o) / b_g1_o * 100.0) if b_g1_o > 0 else 0.0
                buy_score = min(98, max(78, round(80 + bounce_pct * 20)))
                return {'type': '1m Reversal', 'score': buy_score, 'tranches': 1, 'discount': f"{pct_change_1d:.2f}%", 'reason': f'1M 3RD-BAR GO: 14:30 Red (-${r1_b:.2f}) -> 14:31 Smaller Red (-${r2_b:.2f}) -> 14:32 Green (+${g1_b:.2f}).', 'is_smart': True, 'rec_buy': round(current_price * 0.995, 2), 'rec_sell': round(current_price * 1.012, 2), 'status': '1m Reversal Go', 'color': '#0ecb81', 'action_main': 'BUY', 'action_sub': '(1m 3rd-Bar Go)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': '1m Reversal Go'}
            if (b_r1_c < b_r1_o) and (r1_b > 0) and (b_r2_c < b_r2_o) and (0 < r2_b < r1_b):
                return {'type': '1m Reversal', 'score': 45, 'tranches': 0, 'discount': f"{pct_change_1d:.2f}%", 'reason': '1m Exhaustion Ready: 14:30 & 14:31 Red -> Smaller Red printed, awaiting Green 3rd bar.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Awaiting Green', 'color': '#ffd400', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Green)', 'action_color': '#ffd400', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ffd400', 'health_text': 'Awaiting Green'}
            return {'type': '1m Reversal', 'score': 10, 'tranches': 0, 'discount': f"{pct_change_1d:.2f}%", 'reason': "Scanning today's 1m candles for Red -> Smaller Red -> Green (Go).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'No Setup', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting 1m Pattern)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

        # Pure candle entry across all 3 tickers (TQQQ, SOXL, SQQQ): Red -> Smaller Red -> Green on completed non-doji bars
        src_1m = today_reg_1m if len(today_reg_1m) >= 4 else df_1m
        closed_src = src_1m.iloc[:-1]
        non_doji_src = closed_src[closed_src['Close'] != closed_src['Open']]
        if len(non_doji_src) >= 3:
            b_r1_o, b_r1_c = float(non_doji_src['Open'].iloc[-3]), float(non_doji_src['Close'].iloc[-3])
            b_r2_o, b_r2_c = float(non_doji_src['Open'].iloc[-2]), float(non_doji_src['Close'].iloc[-2])
            b_g1_o, b_g1_c = float(non_doji_src['Open'].iloc[-1]), float(non_doji_src['Close'].iloc[-1])
        else:
            b_r1_o, b_r1_c = float(src_1m['Open'].iloc[-4]), float(src_1m['Close'].iloc[-4])
            b_r2_o, b_r2_c = float(src_1m['Open'].iloc[-3]), float(src_1m['Close'].iloc[-3])
            b_g1_o, b_g1_c = float(src_1m['Open'].iloc[-2]), float(src_1m['Close'].iloc[-2])

        r1_body = b_r1_o - b_r1_c
        r2_body = b_r2_o - b_r2_c
        g1_body = b_g1_c - b_g1_o

        red1_ok = (b_r1_c < b_r1_o) and (r1_body > 0)
        red2_ok = (b_r2_c < b_r2_o) and (0 < r2_body < r1_body)
        grn1_ok = (b_g1_c > b_g1_o) and (g1_body > 0) and (current_price >= b_g1_o)

        if red1_ok and red2_ok and grn1_ok:
            bounce_pct = ((b_g1_c - b_g1_o) / b_g1_o * 100.0) if b_g1_o > 0 else 0.0
            buy_score = min(98, max(75, round(78 + bounce_pct * 20)))
            return {'type': '1m Reversal', 'score': buy_score, 'tranches': 1, 'discount': f"{pct_change_1d:.2f}%", 'reason': f'1M GO TRIGGER: Red (-${r1_body:.2f}) -> Smaller Red (-${r2_body:.2f}) -> Green (+${g1_body:.2f}).', 'is_smart': True, 'rec_buy': round(current_price * 0.995, 2), 'rec_sell': round(current_price * 1.012, 2), 'status': '1m Reversal Go', 'color': '#0ecb81', 'action_main': 'BUY', 'action_sub': '(1m Reversal Go)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': '1m Reversal Go'}

        if red1_ok and red2_ok:
            return {'type': '1m Reversal', 'score': 45, 'tranches': 0, 'discount': f"{pct_change_1d:.2f}%", 'reason': '1m Exhaustion Ready: Red -> Smaller Red printed, awaiting Green 1m candle to Go.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Awaiting Green', 'color': '#ffd400', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Green)', 'action_color': '#ffd400', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ffd400', 'health_text': 'Awaiting Green'}

        return {'type': '1m Reversal', 'score': 10, 'tranches': 0, 'discount': f"{pct_change_1d:.2f}%", 'reason': 'Scanning 1m candles for Red -> Smaller Red -> Green (Go).', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'No Setup', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting 1m Pattern)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

    def score_momentum(self, df_5m, current_price, avg_buy_price=0.0, highest_price=0.0, profile='test e', regime=None, overnight_sweep=True, buy_timestamp=0):
        if regime is None: regime = {'score': 50, 'state': 'Room Temp (Neutral)', 'color': '#8a8a9e', 'code': 'NEUTRAL', 'sparkline': [], 'trend': '► Stable'}
        ticker = getattr(df_5m, 'name', '')
        prof = profile.lower()
        if any(x in prof for x in ['1m reversal', 'pulse 1m']):
            return self.score_pulse_1m_reversal(ticker, current_price, avg_buy_price=avg_buy_price, highest_price=highest_price, regime=regime, overnight_sweep=overnight_sweep, buy_timestamp=buy_timestamp)

        is_inverse = ticker in ['SQQQ', '3SUS.L']
        is_3x_etf = ticker in ['TQQQ', 'SOXL', 'NVDL', 'CONL', 'MSTX', 'BITX', 'SQQQ', '3SUS.L']
        trade_type = 'SHORT' if is_inverse else 'LONG'

        if df_5m.empty or len(df_5m) < 21:
            return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': '0.00%', 'reason': 'Insufficient intraday price history.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Tranche 0)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'No Data'}

        pct_change_5d = ((current_price - df_5m['Close'].iloc[0]) / df_5m['Close'].iloc[0]) * 100.0
        now_uk = pd.Timestamp.now(tz='Europe/London')

        ema_src = df_5m['Close']
        try:
            df_s5 = df_5m.copy()
            if df_s5.index.tz is None:
                df_s5.index = df_s5.index.tz_localize('UTC')
            today_s5 = df_s5[df_s5.index.tz_convert('Europe/London').strftime('%Y-%m-%d') == now_uk.strftime('%Y-%m-%d')]
            if len(today_s5) >= 4:
                ema_src = today_s5['Close']
        except Exception:
            ema_src = df_5m['Close']

        ema9_series = ema_src.ewm(span=9, adjust=False).mean()
        ema9 = float(ema9_series.iloc[-1])
        prev_ema9 = float(ema9_series.iloc[-2]) if len(ema9_series) >= 2 else ema9
        ema21 = float(ema_src.ewm(span=21, adjust=False).mean().iloc[-1])
        delta = df_5m['Close'].diff()
        gain_s = (delta.where(delta > 0, 0)).rolling(14, min_periods=2).mean()
        loss_s = (-delta.where(delta < 0, 0)).rolling(14, min_periods=2).mean()
        rs = gain_s / loss_s.replace(0, float('nan'))
        rsi = float((100 - (100 / (1 + rs))).fillna(58.0).iloc[-1]) if not rs.empty else 55.0

        # DYNAMIC ASSET-SPECIFIC TRAILING STOPS & HARD STOPS
        peak_gain_pct = ((highest_price - avg_buy_price) / avg_buy_price * 100.0) if avg_buy_price > 0 and highest_price > 0 else 0.0

        # Standardized Production Trailing Stops (0.75% across 3x ETFs prevents premature wiggle stop-outs)
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

            # 3X-CALIBRATED BREAKEVEN-PLUS LOCK (+0.35% LOCK ONCE +1.10% PEAK HIT)
            # Gives full -0.75% trailing room during early 3x ETF wiggles so runners are never shaken out at +0.50%
            if any(x in prof for x in ['vector', 'apex', 'tqm', 'tq']) and peak_pnl_pct >= 1.10:
                breakeven_price = avg_buy_price * 1.0035  # Locks +0.35% minimum profit while preserving 0.75% pullback room
                if current_price <= breakeven_price:
                    return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%", 'reason': f"BREAKEVEN LOCK HIT (+{pnl_pct:.2f}%). Peak was +{peak_pnl_pct:.2f}%.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Breakeven Lock', 'color': '#0ecb81', 'action_main': 'SELL', 'action_sub': '(Lock +0.35%)', 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#0ecb81', 'health_text': f"Breakeven Lock (+{pnl_pct:.2f}%)"}

            # DYNAMIC 0.50% STEP-RATCHET FLOOR (MOVES UP INDEFINITELY EVERY +0.50% FROM +2.50%)
            if any(x in prof for x in ['vector', 'apex', 'velocity', 'pulse', 'tqm', 'tq', 'test s']) and peak_pnl_pct >= 2.50:
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
            if any(x in prof for x in ['vector', 'apex', 'velocity', 'pulse', 'tqm', 'tq', 'test s']) and peak_pnl_pct >= 1.20:
                trail_pct = 0.40

            # TIERED CHOKE-TRAIL, RATCHET PROFIT FLOORS & 18:30 BANK-AND-WALK
            if any(x in prof for x in ['apex', 'test e22', 'test e23', 'test e24', 'test e25', 'test e26', 'test e27', 'test e28', 'test tq', 'test s2', 'test m']):
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
        if any(x in prof for x in ['velocity', 'pulse', 'test u', 'test s']) and 'test s2' not in prof and is_3x_etf and avg_buy_price > 0:
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

        # OPENING WINDOW ENGINE (14:30 - 14:50 UK): Overnight Gap-Fade Reversal + 15m Opening Range Breakout (ORB)
        if any(x in prof for x in ['vector', 'apex', 'velocity', 'pulse', 'tqm', 'tq', 'test s']) and is_3x_etf:
            if now_uk.weekday() < 5 and now_uk.hour == 14 and 30 <= now_uk.minute < 50:
                open_res = self.evaluate_opening_window(df_5m, current_price, ticker, is_inverse, prof, regime, pct_change_5d)
                if open_res is not None:
                    return open_res

        reg_score = regime.get('score', 50) if isinstance(regime, dict) else 50
        intraday_session_gain = ((current_price - float(today_s5['Open'].iloc[0])) / float(today_s5['Open'].iloc[0]) * 100.0) if ('Open' in df_5m.columns and 'today_s5' in locals() and not today_s5.empty and float(today_s5['Open'].iloc[0]) > 0) else 0.0
        if is_inverse and reg_score > 45 and intraday_session_gain < 0.90:
            return {'type': 'Intraday Momentum', 'score': 20, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"SQQQ blocked: Macro sentiment ({reg_score}/100) not in cooling/bear regime (≤45 required).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Regime Blocked', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Inverse Blocked)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

        # ENTRY LOGIC: Rising 9/21-EMA Alignment + Session-High / 1H-Shelf Breakout Confirmation
        intraday_pct = ((current_price - df_5m['Close'].iloc[-12]) / df_5m['Close'].iloc[-12]) * 100.0 if len(df_5m) >= 12 else 0.5
        prev_close_5m = float(df_5m['Close'].iloc[-2]) if len(df_5m) >= 2 else current_price
        bar_is_green = (
            (current_price > float(df_5m['Open'].iloc[-1]) and current_price > prev_close_5m)
            if 'Open' in df_5m.columns else (current_price > prev_close_5m)
        )
        two_green_bars = (
            len(df_5m) >= 2 and 'Open' in df_5m.columns
            and current_price > float(df_5m['Open'].iloc[-1])
            and current_price > prev_close_5m
            and float(df_5m['Close'].iloc[-2]) > float(df_5m['Open'].iloc[-2])
        )
        swing_low_6 = float(df_5m['Low'].tail(6).min()) if 'Low' in df_5m.columns and len(df_5m) >= 6 else current_price
        bounce_from_low_pct = ((current_price - swing_low_6) / swing_low_6 * 100.0) if swing_low_6 > 0 else 0.0

        if 'High' in df_5m.columns and 'today_s5' in locals() and len(today_s5) >= 2:
            prior_high_12 = float(today_s5['High'].iloc[:-1].tail(12).max())
        else:
            prior_high_12 = float(df_5m['High'].iloc[-13:-1].max()) if 'High' in df_5m.columns and len(df_5m) >= 13 else 0.0
        session_high = float(today_s5['High'].iloc[:-1].max()) if ('High' in df_5m.columns and 'today_s5' in locals() and len(today_s5) >= 2) else prior_high_12
        below_session_high_pct = ((session_high - current_price) / session_high * 100.0) if session_high > 0 else 0.0
        breaks_1h_shelf = (prior_high_12 > 0 and current_price > prior_high_12)

        at_double_top = (
            prior_high_12 > 0
            and current_price <= prior_high_12
            and ((prior_high_12 - current_price) / prior_high_12 * 100.0) < 0.25
            and bounce_from_low_pct > 0.85
        )
        session_low = float(today_s5['Low'].min()) if ('Low' in df_5m.columns and 'today_s5' in locals() and len(today_s5) >= 2) else swing_low_6
        swing_low_4 = float(df_5m['Low'].tail(4).min()) if 'Low' in df_5m.columns and len(df_5m) >= 4 else swing_low_6
        swing_low_8 = float(df_5m['Low'].tail(8).min()) if 'Low' in df_5m.columns and len(df_5m) >= 8 else swing_low_4
        session_drop_pct = ((session_high - session_low) / session_high * 100.0) if session_high > 0 else 0.0
        fresh_session_bottom = (session_low > 0 and swing_low_8 <= session_low * 1.0020)

        ema_spread_pct = ((ema9 - ema21) / ema21 * 100.0) if ema21 > 0 else 0.0
        ema_rising = (ema9 > prev_ema9) and (ema_spread_pct >= (0.12 if is_3x_etf else 0.05))

        # Capitulation V-Reversal: after >=2.0% intraday selloff to a fresh session low, price surges >=0.75% off the bottom on 2 green 5m bars + rising 1m 9/21-EMA
        bounce_from_session_low_pct = ((current_price - session_low) / session_low * 100.0) if session_low > 0 else 0.0
        fast_1m_reclaim = False
        if is_3x_etf and (now_uk.hour < 19) and (session_drop_pct >= 2.00) and fresh_session_bottom and two_green_bars and (0.75 <= bounce_from_session_low_pct <= 3.10):
            if (current_price > ema9) and (ema9 > prev_ema9):
                fast_1m_reclaim = True
            else:
                df_vr_1m = fetch_yf_data(ticker, "1d", "1m")
                if df_vr_1m is not None and not df_vr_1m.empty and len(df_vr_1m) >= 5:
                    df_vr1 = df_vr_1m.copy()
                    if df_vr1.index.tz is None:
                        df_vr1.index = df_vr1.index.tz_localize('UTC')
                    vr1_today = df_vr1[df_vr1.index.tz_convert('Europe/London').strftime('%Y-%m-%d') == now_uk.strftime('%Y-%m-%d')]
                    if len(vr1_today) >= 4:
                        e9_1m = vr1_today['Close'].ewm(span=9, adjust=False).mean()
                        e21_1m = vr1_today['Close'].ewm(span=21, adjust=False).mean()
                        fast_1m_reclaim = (
                            current_price > float(e9_1m.iloc[-1])
                            and float(e9_1m.iloc[-1]) > float(e21_1m.iloc[-1])
                            and float(e9_1m.iloc[-1]) > float(e9_1m.iloc[-2])
                        )

        has_cap_vreversal = (
            is_3x_etf
            and (now_uk.hour < 19)
            and (session_drop_pct >= 2.00)
            and fresh_session_bottom
            and (0.75 <= bounce_from_session_low_pct <= 3.10)
            and two_green_bars
            and fast_1m_reclaim
        )

        # Higher-Low Arch Reclaim: catch U-shaped recoveries mid-arch once a higher low holds above session bottom and 9-EMA turns up
        post_open_ready = (not is_3x_etf) or ('today_s5' not in locals()) or (len(today_s5) >= 6) or (session_high > 0 and current_price > session_high)
        has_higher_low = (session_low > 0 and swing_low_4 >= session_low * 1.0020)
        ema9_catching_up = (ema9 > prev_ema9) and (ema9 >= ema21 * 0.9985)
        has_arch_reclaim = (
            post_open_ready
            and is_3x_etf
            and (now_uk.hour < 19)
            and (session_drop_pct >= 1.10)
            and has_higher_low
            and (0.65 <= bounce_from_session_low_pct <= 2.40)
            and (below_session_high_pct >= 0.25)
            and ema9_catching_up
            and (current_price > ema9)
            and two_green_bars
        )

        # Block lower-high dead-cat bounces trapped below today's session high unless breaking a 1h shelf, V-reversing, or launching a confirmed Higher-Low Arch Reclaim
        in_lower_high_trap = is_3x_etf and (below_session_high_pct > 0.15) and (not (breaks_1h_shelf and below_session_high_pct <= 0.45)) and (not has_cap_vreversal) and (not has_arch_reclaim)

        # Require 2-bar follow-through (or 1h-shelf breakout) + at least 6 completed today bars (14:55+ UK) so 14:50 gap-distorted EMAs don't fire
        has_ema_trend = post_open_ready and (ema9 > ema21) and ema_rising and (current_price > ema9) and (two_green_bars or (bar_is_green and breaks_1h_shelf)) and (intraday_pct >= -1.50)
        has_v_reclaim = post_open_ready and is_3x_etf and (current_price > ema9) and ema_rising and two_green_bars and breaks_1h_shelf and (0.55 <= bounce_from_low_pct <= 2.85)

        if (has_ema_trend or has_v_reclaim or has_cap_vreversal or has_arch_reclaim) and not at_double_top and not in_lower_high_trap and rsi < (92 if is_3x_etf else 78):
            # Completed 1-minute candle confirmation: evaluate on last closed 1m bar so Live, Staging & Recon stay deterministic
            if is_3x_etf:
                df_1m_ent = fetch_yf_data(ticker, "1d", "1m")
                if df_1m_ent is not None and not df_1m_ent.empty and len(df_1m_ent) >= 4:
                    e9_1m_ent = df_1m_ent['Close'].ewm(span=9, adjust=False).mean()
                    prev_1m_close = float(df_1m_ent['Close'].iloc[-2])
                    prev_1m_open = float(df_1m_ent['Open'].iloc[-2])
                    closed_1m_green = (prev_1m_close > prev_1m_open) and (current_price >= prev_1m_open)
                    ema_1m_up = (prev_1m_close > float(e9_1m_ent.iloc[-2])) and (float(e9_1m_ent.iloc[-2]) >= float(e9_1m_ent.iloc[-3]))
                    if not (closed_1m_green and ema_1m_up):
                        return {'type': 'Intraday Momentum', 'score': 48, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': '1m Confirm Filter: Awaiting closed green 1m candle & rising 1m 9-EMA.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': '1m Confirm Wait', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(1m Confirm)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

            vol_20ma = df_5m['Volume'].tail(20).median() if len(df_5m) >= 20 else 1.0
            secs_in_bar = max(90, (now_uk.minute % 5) * 60 + now_uk.second)
            raw_cur_vol = float(df_5m['Volume'].iloc[-1])
            proj_cur_vol = min(raw_cur_vol * (300.0 / secs_in_bar), raw_cur_vol * 2.0)
            cur_vol = max(raw_cur_vol, proj_cur_vol)
            vol_ratio = (cur_vol / vol_20ma) if vol_20ma > 0 else 1.0

            if has_cap_vreversal and vol_ratio < 1.15:
                return {'type': 'Intraday Momentum', 'score': 45, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"V-Reversal Watch: Reclaimed EMAs off low, awaiting >=1.30x volume ({vol_ratio:.1f}x).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Low Volume', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Vol)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

            # DYNAMIC ANTI-CHASE: tighter after 18:00 UK to avoid late-day range tops; allow 1.65% on fresh 1H-shelf breakouts
            ema_extension_pct = ((current_price - ema9) / ema9) * 100.0
            late_chop_window = (now_uk.hour >= 18)
            if any(x in prof for x in ['apex', 'test e6', 'test e17', 'test e22', 'test e23', 'test e24', 'test e25', 'test e26', 'test e27', 'test e28', 'test tq', 'test s2', 'test m']):
                max_extension = (0.65 if late_chop_window else 0.95) if is_3x_etf else 0.35
            else:
                early_session = (now_uk.hour == 14 and now_uk.minute >= 50) or (now_uk.hour == 15 and now_uk.minute < 20)
                max_extension = (0.65 if late_chop_window else (1.45 if early_session else 1.15)) if is_3x_etf else 0.50
            if is_3x_etf and breaks_1h_shelf and not late_chop_window:
                max_extension = max(max_extension, 1.65)

            if ema_extension_pct > max_extension:
                return {'type': 'Intraday Momentum', 'score': 45, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"Anti-Chase Gate: Price is {ema_extension_pct:.2f}% above 9-EMA (max {max_extension:.2f}%). Awaiting pullback.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Overextended', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Pullback)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': f"Overextended (+{ema_extension_pct:.2f}%)"}
            
            if 'test e12' in prof and rsi < 50:
                return {'type': 'Intraday Momentum', 'score': 40, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"E12 Momentum Filter: RSI ({rsi:.1f}) below 50. Entry blocked.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Low RSI Filter', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(RSI < 50)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

            # Relative volume & range-chop filter (uses current bar volume pace so stale prior-bar spikes don't trigger stalled entries)
            if any(x in prof for x in ['apex', 'test e6', 'test e7', 'test e17', 'test e22', 'test e23', 'test e24', 'test e25', 'test e26', 'test e27', 'test e28', 'test tq', 'test s2', 'test m']) and vol_ratio < 0.95:
                return {'type': 'Intraday Momentum', 'score': 45, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"EMA Surge set, but Current Bar Volume ({vol_ratio:.1f}x) below 0.95x threshold.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Low Volume', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Vol)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

            # Sideways consolidation shield for Vector Hybrid / Velocity Pulse: block low-volume mid-range chop
            recent_high_6 = float(df_5m['High'].tail(6).max()) if 'High' in df_5m.columns and len(df_5m) >= 6 else current_price
            near_range_high = (recent_high_6 > 0 and current_price >= recent_high_6 * 0.9975)
            if not near_range_high and not (has_arch_reclaim and vol_ratio >= 0.95) and vol_ratio < 1.15:
                return {'type': 'Intraday Momentum', 'score': 42, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"Chop Shield: Mid-range consolidation (${current_price:.2f} vs ${recent_high_6:.2f} high) on {vol_ratio:.1f}x vol.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Range Chop Filter', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Breakout)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

            # 14:30 - 14:50 UK is governed exclusively by evaluate_opening_window() above
            if any(x in prof for x in ['vector', 'apex', 'velocity', 'pulse', 'tqm', 'tq', 'test s']):
                if now_uk.weekday() < 5 and now_uk.hour == 14 and 30 <= now_uk.minute < 50:
                    return {'type': 'Intraday Momentum', 'score': 30, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': 'Opening Window: Awaiting Gap-Fade cross or 15m ORB trigger.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Opening Window', 'color': '#ffd400', 'action_main': 'HOLD / WAIT', 'action_sub': '(Opening Window)', 'action_color': '#ffd400', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ffd400', 'health_text': 'Opening Window'}

            # TEST E17: 19:45 BST LATE-SESSION CURFEW
            if 'test e17' in prof and (now_uk.hour > 19 or (now_uk.hour == 19 and now_uk.minute >= 45)):
                return {'type': 'Curfew', 'score': 0, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': 'E17 Session Curfew: No new entries permitted after 19:45 BST.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'Curfew Active', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(Curfew Active)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#ff9900', 'health_text': 'Curfew Active'}

            # INDEX RUNNER BOOST: If market sentiment >= 60, grant broad index 3x ETFs a +25 priority bonus
            etf_priority_bonus = 25 if (is_3x_etf and ticker in ['TQQQ', 'SOXL'] and reg_score >= 60) else (25 if is_inverse and reg_score <= 25 else 0)
            buy_score = min(100, max(65, round(55 + max(0.0, intraday_pct) * 8 + max(0.0, 78 - rsi) * 0.5 + etf_priority_bonus)))
            return {'type': 'Intraday Momentum', 'score': buy_score, 'tranches': 1, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"SURGE DETECTED: Price > 9-EMA > 21-EMA, RSI {rsi:.1f}, Vol {vol_ratio:.1f}x{' [ETF PRIORITY]' if etf_priority_bonus else ''}.", 'is_smart': True, 'rec_buy': round(current_price*0.99, 2), 'rec_sell': round(current_price*1.02, 2), 'status': f"Fast {trade_type} Surge", 'color': '#0ecb81', 'action_main': 'BUY', 'action_sub': f"({trade_type} Surge)", 'action_color': '#0ecb81', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

        return {'type': 'Intraday Momentum', 'score': 10, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%", 'reason': f"Awaiting fast EMA crossover surge (RSI {rsi:.0f}).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price, 'status': 'No Setup', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Awaiting Setup)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...'}

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

def update_user_state_atomic(prof_name, client_email, mutator_fn):
    """Thread-safe read-modify-write under trade_lock so parallel bots never overwrite each other's MongoDB state."""
    is_client = bool(client_email and client_email.strip().lower() not in [ADMIN_USER.lower(), ADMIN_ALIAS.lower()])
    with portfolio_store.trade_lock:
        if is_client:
            fresh_cs = portfolio_store.get_client_store(client_email)
            if not isinstance(fresh_cs, dict):
                return False
            fresh_ud = (fresh_cs.get('users') or {}).get(prof_name)
            if not isinstance(fresh_ud, dict):
                return False
            if mutator_fn(fresh_ud) is not False:
                portfolio_store.save_client_store(client_email, fresh_cs)
                return True
        else:
            portfolio_store.reload()
            fresh_ud = (portfolio_store.data.get('users') or {}).get(prof_name)
            if not isinstance(fresh_ud, dict):
                return False
            if mutator_fn(fresh_ud) is not False:
                portfolio_store.save_data(portfolio_store.data)
                return True
    return False

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
        
        is_auto_profile = any(x in active_profile for x in ['vector', 'apex', 'precision', 'velocity', 'pulse', 'hybrid', 'test e', 'test s', 'test tq', 'test r', 'test tqm'])
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

        # Isolated total equity & intra-candle peak equity calculation
        now_uk = pd.Timestamp.now(tz='Europe/London')
        today_str = now_uk.strftime('%Y-%m-%d')
        trades_today = [tr for tr in hist if tr.get('date_str') == today_str and tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION', 'ALPACASYNC']]
        net_cash_today = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in trades_today)
        cash_start = cash_balance - net_cash_today

        b_cfg_top = (c_store.get('broker') or {}) if is_client else (portfolio_store.data.get('broker') or {})
        live_acc_top = None
        if b_cfg_top.get('api_key') and b_cfg_top.get('secret_key'):
            live_acc_top = get_live_alpaca_account(str(b_cfg_top['api_key']).strip(), str(b_cfg_top['secret_key']).strip(), b_cfg_top.get('mode', 'paper'))

        tot_own = 0.0
        peak_tot_own = 0.0
        equity_start = 0.0
        held_tks_now = list(set([tr.get('ticker') for tr in hist if tr.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC'] and (resolve_shares(tr.get('ticker')) > 0 or any(t2.get('ticker') == tr.get('ticker') for t2 in trades_today))]))
        for h_tk in held_tks_now:
            sh_c = resolve_shares(h_tk)
            cur_df = fetch_yf_data(h_tk, "1d", "1m")
            cp = float(cur_df['Close'].iloc[-1]) if not cur_df.empty else 0.0
            if sh_c > 0 and live_acc_top and isinstance(live_acc_top.get('positions'), list):
                alp_cp_top = next((float(p.get('current_price') or 0) for p in live_acc_top['positions'] if (p.get('symbol') or '').upper() == h_tk), 0.0)
                if alp_cp_top > 0:
                    cp = alp_cp_top
            if cp > 0:
                div = 100.0 if h_tk.endswith('.L') and cp > 100 else 1.0
                tot_own += round(sh_c * (cp / div), 2)

                # Highest price reached AFTER entry timestamp (captures intra-bar spikes above 2.5% without pre-buy lookback)
                peak_cp = cp
                if sh_c > 0:
                    stored_hw = float((ud.get('holdings', {}).get(h_tk) or {}).get('high_water') or 0.0)
                    t_buys_h = [tr for tr in hist if tr.get('ticker') == h_tk and tr.get('action') == 'BUY']
                    buy_ts_h = int(t_buys_h[0].get('timestamp') or 0) if t_buys_h else 0
                    candle_hi = cp
                    if not cur_df.empty and 'High' in cur_df.columns and buy_ts_h > 0:
                        try:
                            post_df_h = cur_df[cur_df.index.astype('int64') // 10**9 >= (buy_ts_h + 60)]
                            if not post_df_h.empty:
                                candle_hi = float(post_df_h['High'].max())
                        except Exception:
                            candle_hi = cp
                    peak_cp = max(cp, stored_hw, candle_hi)
                peak_tot_own += round(sh_c * (peak_cp / div), 2)

                # Start-of-day held equity if shares were carried overnight
                sh_start = sh_c
                for tr in trades_today:
                    if tr.get('ticker') == h_tk:
                        if tr.get('action') == 'BUY': sh_start -= int(tr.get('shares') or 0)
                        else: sh_start += int(tr.get('shares') or 0)
                if sh_start > 0:
                    df_5m_sod = fetch_yf_data(h_tk, "5d", "5m")
                    if not df_5m_sod.empty:
                        df_tmp_s = df_5m_sod.copy()
                        if df_tmp_s.index.tz is None: df_tmp_s.index = df_tmp_s.index.tz_localize('UTC')
                        else: df_tmp_s.index = df_tmp_s.index.tz_convert('UTC')
                        prev_s = df_tmp_s[df_tmp_s.index.tz_convert('Europe/London').strftime('%Y-%m-%d') < today_str]
                        start_p = float(prev_s['Close'].iloc[-1]) if not prev_s.empty else float(df_tmp_s['Close'].iloc[0])
                        equity_start += max(0, sh_start) * (start_p / div)

        total_equity = cash_balance + tot_own
        peak_total_equity = cash_balance + peak_tot_own

        # GEAR 4 ($1,000,000+): Shift to ultra-deep liquidity ETFs (QQQ / SPY)
        if total_equity >= 1000000:
            held_symbols = [tr.get('ticker') for tr in hist if resolve_shares(tr.get('ticker')) > 0]
            scan_list = list(dict.fromkeys(['QQQ', 'SPY'] + held_symbols))

        # Calculate Start-Of-Day (SOD) equity & instantaneous + peak daily %
        sod_equity = max(50.0, cash_start + equity_start)
        daily_pnl = total_equity - sod_equity
        daily_pnl_pct = (daily_pnl / sod_equity) * 100.0 if sod_equity > 0 else 0.0
        peak_daily_pnl_pct = ((peak_total_equity - sod_equity) / sod_equity) * 100.0 if sod_equity > 0 else daily_pnl_pct

        # Maximum allowable daily drawdown (-5.0% for Pulse 1M Reversal; -2.5% for 5m bots)
        is_p1m_risk = any(x in active_profile for x in ['1m reversal', 'pulse 1m'])
        CIRCUIT_BREAKER_PCT = -5.00 if is_p1m_risk else -2.50
        circuit_breaker_tripped = (daily_pnl_pct <= CIRCUIT_BREAKER_PCT)

        # TOTAL DAILY ACCOUNT STEP-RATCHET (+5.0% start & +5.0% steps for Pulse 1M Reversal; +2.5% start & +0.5% steps for 5m bots)
        r_start_pct = 5.00 if is_p1m_risk else 2.50
        r_step_pct = 5.00 if is_p1m_risk else 0.50
        r_clear_buf = 5.10 if is_p1m_risk else 0.60
        force_daily_floor_sell = False
        daily_floor_locked = 0.0
        if any(x in active_profile for x in ['vector', 'apex', 'velocity', 'pulse', 'tqm', 'tq', 'test s']):
            if ud.get('last_ratchet_date') != today_str:
                ud['last_ratchet_date'] = today_str
                ud['daily_peak_pct'] = 0.0
                ud['manual_locked_day'] = None
                ud['ratchet_floor_override'] = None
                ud['ratchet_locked'] = False
                ud['is_locked'] = False

            observed_peak_pct = round(max(daily_pnl_pct, peak_daily_pnl_pct), 2)
            if observed_peak_pct > float(ud.get('daily_peak_pct') or 0.0):
                ud['daily_peak_pct'] = observed_peak_pct
                if ud.get('ratchet_floor_override') is not None and observed_peak_pct >= (float(ud['ratchet_floor_override']) + r_clear_buf):
                    ud['ratchet_floor_override'] = None
                def _mut_peak(f_ud, pk=observed_peak_pct, c_buf=r_clear_buf):
                    if pk <= float(f_ud.get('daily_peak_pct') or 0.0):
                        return False
                    f_ud['daily_peak_pct'] = pk
                    if f_ud.get('ratchet_floor_override') is not None and pk >= (float(f_ud['ratchet_floor_override']) + c_buf):
                        f_ud['ratchet_floor_override'] = None
                    return True
                update_user_state_atomic(prof_name, client_email, _mut_peak)

            daily_peak = float(ud.get('daily_peak_pct') or 0.0)

            if ud.get('ratchet_floor_override') is not None:
                daily_floor_locked = float(ud['ratchet_floor_override'])
            elif daily_peak >= r_start_pct:
                daily_floor_locked = round(int(daily_peak / r_step_pct) * r_step_pct, 2)
            elif ud.get('manual_locked_day') != today_str and (ud.get('ratchet_locked') or ud.get('is_locked')):
                # Clear stale pre-5.0% ratchet lock from before threshold upgrade
                ud['ratchet_locked'] = False
                ud['is_locked'] = False
                ud['locked_floor_val'] = None
                def _mut_clear_stale_lock(f_ud, r_min=r_start_pct, t_str=today_str):
                    if f_ud.get('manual_locked_day') == t_str or f_ud.get('ratchet_floor_override') is not None:
                        return False
                    if float(f_ud.get('daily_peak_pct') or 0.0) >= r_min:
                        return False
                    if not (f_ud.get('ratchet_locked') or f_ud.get('is_locked')):
                        return False
                    f_ud['ratchet_locked'] = False
                    f_ud['is_locked'] = False
                    f_ud['locked_floor_val'] = None
                    return True
                update_user_state_atomic(prof_name, client_email, _mut_clear_stale_lock)

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
        # True loss strike counter: Only count real trades closed at an actual loss (sell price < buy price)
        today_loss_stops = 0
        for i, h in enumerate(hist):
            if h.get('ticker') in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']:
                continue
            if h.get('action') == 'SELL' and h.get('date_str') == today_str:
                matching_buy = next((b for b in hist[i+1:] if b.get('ticker') == h.get('ticker') and b.get('action') == 'BUY'), None)
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
            # Always score momentum on 5d/5m candles so background entries match the UI Readiness bar and avoid 1m noise
            return tick, fetch_yf_data(tick, "5d", "5m")

        with ThreadPoolExecutor(max_workers=4) as ex:
            for tick, df in ex.map(fetch_data_thread, scan_list): dfs[tick] = df

        for t in scan_list:
            t = t.strip().upper()
            if not t: continue
            try:
                df = dfs.get(t)
                if df is None or df.empty: continue
                df.name = t
                cur = float(df['Close'].iloc[-1])
                is_pulse_1m = any(x in active_profile for x in ['1m reversal', 'pulse 1m'])
                if is_pulse_1m or (now_uk.weekday() < 5 and now_uk.hour == 14 and 30 <= now_uk.minute < 50):
                    df_1m_open = fetch_yf_data(t, "1d", "1m")
                    if df_1m_open is not None and not df_1m_open.empty:
                        cand_1m = float(df_1m_open['Close'].iloc[-1])
                        # Guard against stale 1m edge cache diverging >1.2% from the 5m bar
                        if cur > 0 and abs(cand_1m - cur) / cur <= 0.012:
                            cur = cand_1m
                        elif df_1m_open.index[-1] >= df.index[-1]:
                            cur = cand_1m
                sh = resolve_shares(t)
                stale_pre_buy_quote = False
                # Prefer Alpaca's real-time position quote when holding shares; fallback to fresh 1m quote (never lagging 5m close)
                if sh > 0:
                    alp_pos_cur = 0.0
                    b_cfg = (c_store.get('broker') or {}) if is_client else (portfolio_store.data.get('broker') or {})
                    if b_cfg.get('api_key') and b_cfg.get('secret_key'):
                        live_acc = get_live_alpaca_account(str(b_cfg['api_key']).strip(), str(b_cfg['secret_key']).strip(), b_cfg.get('mode', 'paper'))
                        if live_acc and isinstance(live_acc.get('positions'), list):
                            alp_pos_cur = next((float(p.get('current_price') or 0) for p in live_acc['positions'] if (p.get('symbol') or '').upper() == t), 0.0)
                    df_1m_hold = fetch_yf_data(t, "1d", "1m")
                    yf_1m_cur = float(df_1m_hold['Close'].iloc[-1]) if (df_1m_hold is not None and not df_1m_hold.empty) else 0.0
                    if alp_pos_cur > 0 and yf_1m_cur > 0:
                        # Blend/clamp raw Alpaca last-print within 0.25% of 1m candle close to filter sub-second spread flickers
                        if abs(alp_pos_cur - yf_1m_cur) / yf_1m_cur <= 0.0025:
                            cur = alp_pos_cur
                        else:
                            cur = yf_1m_cur
                    elif alp_pos_cur > 0:
                        cur = alp_pos_cur
                    elif yf_1m_cur > 0:
                        cur = yf_1m_cur
                        t_buys_chk = [tr for tr in hist if tr.get('ticker') == t and tr.get('action') == 'BUY']
                        b_ts_chk = int(t_buys_chk[0].get('timestamp') or 0) if t_buys_chk else 0
                        last_bar_ts = int(df_1m_hold.index[-1].timestamp())
                        if b_ts_chk > 0 and (last_bar_ts + 60) < b_ts_chk:
                            stale_pre_buy_quote = True
                cps = cur / 100.0 if t.endswith('.L') and cur > 100 else cur
                vo = sh * cps

                now_uk = pd.Timestamp.now(tz='Europe/London')
                is_us_stock = not t.endswith('.L')
                current_mins = now_uk.hour * 60 + now_uk.minute
                
                if now_uk.weekday() >= 5: is_open = False
                elif is_us_stock: is_open = (14 * 60 + 30) <= current_mins < (21 * 60)
                else: is_open = (8 * 60) <= current_mins < (16 * 60 + 30)

                avg_buy_p, highest_p, buy_ts = 0.0, cur, 0
                if sh > 0:
                    t_buys = [tr for tr in hist if tr.get('ticker') == t and tr.get('action') == 'BUY']
                    if t_buys:
                        avg_buy_p = t_buys[0].get('price', 0.0)
                        buy_ts = int(t_buys[0].get('timestamp') or 0)
                        df_1m_hw = fetch_yf_data(t, "1d", "1m")
                        hw_src = df_1m_hw if (df_1m_hw is not None and not df_1m_hw.empty) else df
                        if not hw_src.empty and 'High' in hw_src.columns:
                            try:
                                post_buy_df = hw_src[hw_src.index.astype('int64') // 10**9 >= (buy_ts + 60)]
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
                        def _mut_hw(f_ud, sym=t, hw_val=highest_p):
                            h_map = f_ud.get('holdings')
                            if not isinstance(h_map, dict) or sym not in h_map:
                                return False
                            cur_hw = float(h_map[sym].get('high_water') or 0.0)
                            if hw_val <= cur_hw:
                                return False
                            h_map[sym]['high_water'] = hw_val
                            return True
                        update_user_state_atomic(prof_name, client_email, _mut_hw)

                # Determine effective scoring profile based on active mode
                eff_profile = active_profile
                if any(x in active_profile for x in ['test e24', 'test e25', 'test e26', 'test m']):
                    eff_profile = 'test p' if current_mode == 'reversion' else ('test e23' if 'test m' in active_profile else active_profile)

                if 'test a' in active_profile:
                    avg_v = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
                    v_r = (df['Volume'].iloc[-1] / avg_v) if avg_v > 0 else 1.0
                    st = engine.score_nav_asset(t, cur, v_r) if t in engine.nav_bases else engine.score_equity(df, cur)
                else:
                    st = engine.score_momentum(df, cur, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=eff_profile, regime=regime, overnight_sweep=bool(ud.get('overnight_sweep', True)), buy_timestamp=buy_ts)

                if sh > 0:
                    held_scores.append({'ticker': t, 'shares': sh, 'price': cur, 'cps': cps, 'value': vo, 'score': st['score'], 'action': st['action_main']})

                if st['action_main'] == 'SELL' and sh > 0 and vo >= MIN_BUY_VALUE and not stale_pre_buy_quote:
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

        # DEFENSIVE CHOP SHIELDS & COOLDOWNS FOR ACTIVE FLEET (Exempt Pulse 1M Reversal so it repeats freely until 20:50 sweep)
        is_pulse_1m_prof = any(x in active_profile for x in ['1m reversal', 'pulse 1m'])
        cur_mins = now_uk.hour * 60 + now_uk.minute
        if not is_pulse_1m_prof and cur_mins >= (19 * 60 + 45):
            is_curfew_active = True

        # Three-Strike Loss Lockout (5m profiles only)
        if not is_pulse_1m_prof and today_stops_cnt >= 3:
            is_curfew_active = True

        # Post-Sell Cooldown: 60s (1 candle) for Pulse 1M Reversal so it repeats cleanly; 15 mins (900s) for 5m profiles
        cd_duration = 60 if any(x in active_profile for x in ['1m reversal', 'pulse 1m']) else 900
        real_hist_for_cd = [h for h in hist if h.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']]
        if real_hist_for_cd and real_hist_for_cd[0].get('action') == 'SELL':
            if (time.time() - real_hist_for_cd[0].get('timestamp', 0)) < cd_duration:
                buys = []

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
            def _mut_ratchet_lock(f_ud, fl=daily_floor_locked):
                f_ud['ratchet_locked'] = True
                f_ud['locked_floor_val'] = fl
                return True
            update_user_state_atomic(prof_name, client_email, _mut_ratchet_lock)
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
                executed = portfolio_store.execute_trade(
                    d['ticker'], d['action'], d['shares'], d['price'], prof_name,
                    metadata={'status': d.get('status', 'Manual/EOD'), 'reason': d.get('reason', ''), 'highest_price': d.get('highest_price', 0.0), 'slices': d.get('slices', 1)},
                    client_email=client_email
                )
                # Stamp post-sell cooldown atomically on fresh user state so execute_trade history is never overwritten
                if executed and d['action'] == 'SELL':
                    cd_until = int(time.time()) + cd_duration
                    def _mut_cd(f_ud, sym=d['ticker'], exp_ts=cd_until):
                        f_ud.setdefault('cooldowns', {})[sym] = exp_ts
                        return True
                    update_user_state_atomic(prof_name, client_email, _mut_cd)
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
            
            # 2. Collect active strategy targets in strict priority tiers (Live Primary -> Live Clients -> Background Recon)
            live_admin_tasks = []
            live_client_tasks = []
            recon_tasks = []

            # Priority 1: Primary Live Admin profiles
            for prof_name in list(portfolio_store.data.get('users', {}).keys()):
                p_lower = prof_name.lower()
                if any(k in p_lower for k in ['vector', 'apex', 'velocity', 'pulse', 'tqm', 'tq', 'test s']):
                    live_admin_tasks.append((prof_name, None))

            # Priority 2 & 3: Live Client accounts first, background Recon (ian_paper@gmail.com) strictly last
            if portfolio_store.client:
                try:
                    accounts_col = portfolio_store.client[MONGO_DB_NAME]['accounts']
                    for acc in accounts_col.find({"role": "client"}):
                        c_email = acc.get('username')
                        c_store = acc.get('portfolio') or {}
                        if not c_store.get('is_configured'): continue
                        users_dict = c_store.get('users') or {}
                        is_recon_acc = (str(c_email or '').strip().lower() == PAPER_STATS_ACCOUNT.lower())
                        for p_name in list(users_dict.keys()):
                            p_low = p_name.lower()
                            if any(k in p_low for k in ['vector', 'apex', 'velocity', 'pulse', 'tqm', 'tq', 'test s']):
                                if is_recon_acc:
                                    recon_tasks.append((p_name, c_email))
                                else:
                                    live_client_tasks.append((p_name, c_email))
                except Exception as ex_c:
                    print(f"Client Background Scan Error: {ex_c}")

            def execute_task(task):
                p_n, c_m = task
                process_auto_profile(p_n, client_email=c_m)

            # 3. Priority-Ordered Execution: Live Admin runs first with zero lock contention
            if live_admin_tasks:
                with ThreadPoolExecutor(max_workers=min(4, len(live_admin_tasks))) as ex:
                    list(ex.map(execute_task, live_admin_tasks))

            if live_client_tasks:
                with ThreadPoolExecutor(max_workers=min(8, len(live_client_tasks))) as ex:
                    list(ex.map(execute_task, live_client_tasks))

            if recon_tasks:
                with ThreadPoolExecutor(max_workers=min(4, len(recon_tasks))) as ex:
                    list(ex.map(execute_task, recon_tasks))

        except Exception as e: 
            print(f"Background Loop Error: {e}")
        gc.collect()
        now_uk_w = pd.Timestamp.now(tz='Europe/London')
        w_mins = now_uk_w.hour * 60 + now_uk_w.minute
        is_us_open_w = (now_uk_w.weekday() < 5) and ((14 * 60 + 30) <= w_mins < (21 * 60))
        # Wall-clock aligned sleep (:00, :10, :20, :30, :40, :50) so Live & Staging evaluate identical candles simultaneously
        step_sec = 10.0 if is_us_open_w else 20.0
        sleep_sec = max(1.0, step_sec - (time.time() % step_sec))
        time.sleep(sleep_sec)

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
    # Prefix fallbacks (check 1m reversal before generic 'pulse' so Pulse 1M Reversal never routes to Velocity Pulse)
    if '1m_reversal' in coid_clean or 'pulse_1m' in coid_clean: return 'Pulse 1M Reversal'
    if 'vector' in coid_clean: return 'Vector Hybrid'
    if 'apex' in coid_clean: return 'Apex Precision'
    if 'velocity' in coid_clean or 'pulse' in coid_clean: return 'Velocity Pulse'
    return None

def sync_profile_with_alpaca_non_destructive(ud, acc, live_pos, closed_orders, today_str, now_lon):
    """Non-destructive two-way reconciliation: preserves today's trade history while aligning exact fills, positions, equity, and ratchet state to Alpaca."""
    changed = False
    hist = ud.get('history') or []
    init_pos = ud.get('initial_positions') or {}

    used_order_ids = set()
    for o in (closed_orders or []):
        if o.get('status') != 'filled':
            continue
        coid = o.get('client_order_id') or ''
        side = (o.get('side') or '').lower()
        symbol = (o.get('symbol') or '').upper()
        try:
            filled_qty = int(float(o.get('filled_qty') or 0))
            filled_price = float(o.get('filled_avg_price') or o.get('price') or 0.0)
        except Exception:
            continue
        order_id = o.get('id')
        if side not in ['buy', 'sell'] or filled_qty <= 0 or filled_price <= 0 or order_id in used_order_ids:
            continue

        order_ts = 0
        try:
            coid_parts = coid.rsplit('_', 1)
            if len(coid_parts) == 2 and coid_parts[1].isdigit():
                order_ts = int(coid_parts[1])
            elif o.get('filled_at'):
                order_ts = int(pd.Timestamp(o['filled_at']).timestamp())
            elif o.get('created_at'):
                order_ts = int(pd.Timestamp(o['created_at']).timestamp())
        except Exception:
            order_ts = 0

        target_act = side.upper()
        best_h = None
        best_diff = 999999
        for h in hist:
            if h.get('action') != target_act or h.get('ticker') != symbol:
                continue
            if h.get('alpaca_order_id') and h.get('alpaca_order_id') != order_id:
                continue
            if int(float(h.get('shares') or 0)) != filled_qty:
                continue
            h_ts = int(h.get('timestamp') or 0)
            t_diff = abs(order_ts - h_ts) if (order_ts > 0 and h_ts > 0) else 0
            if order_ts > 0 and h_ts > 0 and t_diff > 2400:
                continue
            if t_diff < best_diff:
                best_diff = t_diff
                best_h = h

        if best_h is not None:
            used_order_ids.add(order_id)
            new_p = round(filled_price, 2)
            cps = filled_price / 100.0 if symbol.endswith('.L') else filled_price
            new_amt = round(int(float(best_h.get('shares') or filled_qty)) * cps, 2)
            if float(best_h.get('price') or 0) != new_p or float(best_h.get('amount') or 0) != new_amt:
                best_h['price'] = new_p
                best_h['amount'] = new_amt
                changed = True
            best_h['price_reconciled'] = True
            best_h['alpaca_order_id'] = order_id

    alpaca_pos_map = {p['symbol'].upper(): p for p in (live_pos or []) if int(p.get('qty', 0)) > 0}

    def calc_local_sh(tk):
        i_s = int((init_pos.get(tk) or {}).get('shares') or 0)
        n_s = sum(int(t.get('shares') or 0) if t.get('action') == 'BUY' else -int(t.get('shares') or 0) for t in hist if t.get('ticker') == tk)
        return max(0, i_s + n_s)

    all_tks = set([tr.get('ticker') for tr in hist if tr.get('ticker') and tr.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']]) | set(alpaca_pos_map.keys())
    if 'holdings' not in ud or not isinstance(ud['holdings'], dict):
        ud['holdings'] = {}

    now_ts_sync = int(time.time())
    for tk in all_tks:
        loc_sh = calc_local_sh(tk)
        alp_p = alpaca_pos_map.get(tk)
        alp_sh = int(alp_p.get('qty', 0)) if alp_p else 0

        # Skip reconciliation if an order for this ticker was placed within the last 75s (still settling on Alpaca)
        recent_tk_tr = next((tr for tr in hist if tr.get('ticker') == tk and tr.get('source') != 'alpaca_sync'), None)
        if recent_tk_tr and (now_ts_sync - int(recent_tk_tr.get('timestamp') or 0)) < 75:
            if alp_sh > 0:
                mv = round(float(alp_p.get('market_value') or 0.0), 2)
                hw = max(float((ud['holdings'].get(tk) or {}).get('high_water') or 0.0), float(alp_p.get('current_price') or 0.0))
                ud['holdings'][tk] = {'shares': alp_sh, 'manual_val': mv, 'high_water': hw}
            continue

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
    mb_val = float(ud.get('master_budget') or 700.0)
    if mb_val <= 0 and alpaca_last_equity > 0:
        ud['master_budget'] = alpaca_last_equity
        mb_val = alpaca_last_equity
        changed = True

    # Skip residual equity calibration while an order is still settling (<90s) so cash + open position never double-count
    has_unsettled_trade = any(
        tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION', 'ALPACASYNC']
        and (now_ts_sync - int(tr.get('timestamp') or 0)) < 90
        for tr in hist
    )

    if not has_unsettled_trade:
        # Remove ALL historical ALPACASYNC entries so cross-day drift never double-counts
        non_sync_hist = [tr for tr in hist if tr.get('ticker') != 'ALPACASYNC']
        if len(non_sync_hist) != len(hist):
            hist = non_sync_hist
            changed = True

        net_real = sum(-float(tr.get('amount') or 0.0) if tr.get('action') == 'BUY' else float(tr.get('amount') or 0.0)
                       for tr in hist if tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION', 'ALPACASYNC'])
        init_man = sum(float(pos.get('manual_val') or 0.0) for pos in init_pos.values())
        open_mv = sum(round(float(p.get('market_value') or 0.0), 2) for p in alpaca_pos_map.values())
        local_eq_before_adj = round(mb_val + net_real - init_man + open_mv, 2)

        residual = round(alpaca_equity - local_eq_before_adj, 2) if alpaca_equity > 0 else 0.0
        if abs(residual) >= 0.02:
            # Anchor ALPACASYNC before PERFORMANCE_START_DATE so historical Alpaca baseline drift never distorts TODAY or DAILY AVG
            pre_epoch_date = (pd.Timestamp(PERFORMANCE_START_DATE) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
            hist.insert(0, {
                'id': str(int(time.time() * 1000) + 4),
                'ticker': 'ALPACASYNC',
                'trade_type': 'LONG',
                'action': 'SELL' if residual >= 0 else 'BUY',
                'shares': 0,
                'price': abs(residual),
                'amount': abs(residual),
                'time': now_lon.strftime('%d %b %H:%M'),
                'date_str': pre_epoch_date,
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
        is_p1m_sync = any(x in str(ud.get('method') or '').lower() for x in ['1m reversal', 'pulse 1m']) or (ud.get('settings') or {}).get('interval') == '1m'
        min_unlock_pct = 4.75 if is_p1m_sync else 2.25
        if ud.get('manual_locked_day') != today_str and (ud.get('ratchet_locked') or ud.get('is_locked')):
            if true_today_pct < min_unlock_pct and ud.get('ratchet_floor_override') is None:
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
            try:
                filled_qty = int(float(o.get('filled_qty') or 0))
                filled_price = float(o.get('filled_avg_price') or o.get('price') or 0.0)
            except Exception:
                continue
            order_id = o.get('id')
            
            if filled_qty <= 0 or filled_price <= 0:
                continue
                
            matched_prof = match_bot_from_order_id(coid, auto_profiles)
            if not matched_prof:
                _allocs = broker.get('allocations') or {}
                _en_profs = [
                    p for p in auto_profiles
                    if any((s_k == p.lower() or s_k in p.lower() or p.lower() in s_k) and s_d.get('enabled')
                           for s_k, s_d in _allocs.items())
                ]
                if len(_en_profs) == 1:
                    matched_prof = _en_profs[0]
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

                        new_px_r = round(filled_price, 2)
                        cps = filled_price / 100.0 if symbol.endswith('.L') else filled_price
                        new_amt_r = round(h['shares'] * cps, 2)
                        h['price'] = new_px_r
                        h['amount'] = new_amt_r
                        h['price_reconciled'] = True
                        h['alpaca_order_id'] = order_id
                        reconciled_existing = True
                        tr_id_r = h.get('id')
                        def _mut_rec_fill(f_ud, tid=tr_id_r, px_v=new_px_r, amt_v=new_amt_r, oid=order_id):
                            for item in (f_ud.get('history') or []):
                                if item.get('id') == tid:
                                    item['price'] = px_v
                                    item['amount'] = amt_v
                                    item['price_reconciled'] = True
                                    item['alpaca_order_id'] = oid
                                    return True
                            return False
                        update_user_state_atomic(matched_prof, c_email, _mut_rec_fill)
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
                    recent_buy_tr = next((tr for tr in h_list if tr.get('ticker') == tk and tr.get('action') == 'BUY'), None)
                    buy_t_stamp = int(recent_buy_tr.get('timestamp') or 0) if recent_buy_tr else 0
                    if recent_buy_tr and (int(time.time()) - buy_t_stamp) < 90:
                        continue
                    if alpaca_holdings.get(tk, 0) == 0:
                        # Only close locally if Alpaca actually executed a filled SELL order for this symbol after our BUY
                        alp_sell_fill = None
                        for o in (closed_orders or []):
                            if o.get('status') == 'filled' and (o.get('symbol') or '').upper() == tk and (o.get('side') or '').lower() == 'sell':
                                o_ts = 0
                                try:
                                    if o.get('filled_at'):
                                        o_ts = int(pd.Timestamp(o['filled_at']).timestamp())
                                    elif o.get('created_at'):
                                        o_ts = int(pd.Timestamp(o['created_at']).timestamp())
                                except Exception:
                                    o_ts = 0
                                if buy_t_stamp == 0 or o_ts >= (buy_t_stamp - 15):
                                    alp_sell_fill = o
                                    break
                        if alp_sell_fill is not None:
                            local_sh = chk_shares(tk)
                            if local_sh > 0:
                                fill_px = float(alp_sell_fill.get('filled_avg_price') or 0.0)
                                if fill_px <= 0:
                                    df_p = fetch_yf_data(tk, "1d", "1m")
                                    fill_px = float(df_p['Close'].iloc[-1]) if not df_p.empty else 1.0
                                portfolio_store.execute_trade(
                                    ticker=tk,
                                    action_type='SELL',
                                    shares=local_sh,
                                    price=round(fill_px, 2),
                                    username=prof,
                                    metadata={
                                        'status': 'Alpaca Reconciled Flat',
                                        'reason': 'Synced Alpaca exchange stop/exit fill',
                                        'alpaca_order_id': alp_sell_fill.get('id'),
                                        'price_reconciled': True,
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
            for acc in portfolio_store.client[MONGO_DB_NAME]['accounts'].find({"role": "client"}):
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
        # Always reconcile Live Admin first, then Live Clients, and background Recon (ian_paper@gmail.com) last
        live_targets = [t for t in account_targets if str(t.get('client_email') or '').strip().lower() != PAPER_STATS_ACCOUNT.lower()]
        recon_targets = [t for t in account_targets if str(t.get('client_email') or '').strip().lower() == PAPER_STATS_ACCOUNT.lower()]
        if live_targets:
            with ThreadPoolExecutor(max_workers=min(6, len(live_targets))) as ex:
                list(ex.map(reconcile_single_target, live_targets))
        if recon_targets:
            with ThreadPoolExecutor(max_workers=min(4, len(recon_targets))) as ex:
                list(ex.map(reconcile_single_target, recon_targets))


def alpaca_reconciliation_worker():
    time.sleep(5)
    while True:
        try:
            reconcile_alpaca_positions_and_orders()
        except Exception as e:
            print(f"[RECONCILIATION WORKER ERROR]: {e}")
        time.sleep(25)

def _start_singleton_workers():
    """Ensures only ONE Gunicorn worker process runs background trading & reconciliation loops (prevents 2x RAM OOM and duplicate orders)."""
    try:
        import fcntl
        lock_fp = open('/tmp/idc_trading_bg_workers.lock', 'w')
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        app._bg_worker_lock_fp = lock_fp  # Keep descriptor open for lifetime of leader process
    except ImportError:
        pass  # Windows local dev fallback
    except Exception:
        return
    threading.Thread(target=global_background_worker, daemon=True).start()
    threading.Thread(target=alpaca_reconciliation_worker, daemon=True).start()

_start_singleton_workers()

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
        accounts_col = portfolio_store.client[MONGO_DB_NAME]['accounts']
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

    accounts_col = portfolio_store.client[MONGO_DB_NAME]['accounts']
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

            accounts_col = portfolio_store.client[MONGO_DB_NAME]['accounts']
            for doc in accounts_col.find():
                u = doc.get('username')
                if not u or u in ['ianldownward@gmail.com', 'ian']: continue
                pw = doc.get('plain_password') or "(hash)"
                dt = doc.get('registered_at') or "Prior Session"
                
                # 2. Fetch user profile data
                c_store = doc.get('portfolio') or {}
                users_dict = c_store.get('users') or {}
                
                prof_tds = ""
                for target_prof in ['Vector Hybrid', 'Apex Precision', 'Velocity Pulse', 'Pulse 1M Reversal']:
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
                            <th style='padding:12px; border-left:1px solid #262b36;'>Apex Precision</th>
                            <th style='padding:12px; border-left:1px solid #262b36;'>Velocity Pulse</th>
                            <th style='padding:12px; border-left:1px solid #262b36;'>Pulse 1M Reversal</th>
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
            accounts_col = portfolio_store.client[MONGO_DB_NAME]['accounts']
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
    is_staging = (os.environ.get('MONGO_DB_NAME', 'stock_terminal') != 'stock_terminal')
    is_recon = (display_user.lower() == PAPER_STATS_ACCOUNT.lower())
    return render_template(
        'index.html',
        user_role=session.get('role', 'client'),
        current_user=display_user,
        is_configured=is_configured,
        is_staging=is_staging,
        is_recon=is_recon
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

    is_p1m_ov = any(x in prof_name.lower() for x in ['1m reversal', 'pulse 1m'])
    r_start_ov = 5.0 if is_p1m_ov else 2.5
    r_step_ov = 5.0 if is_p1m_ov else 0.5
    raw_floor = ud.get('locked_floor_val') if ud.get('locked_floor_val') is not None else (
        round(int(cur_day_pct / r_step_ov) * r_step_ov, 2) if cur_day_pct >= r_start_ov else r_start_ov
    )
    current_floor = float(raw_floor)
    new_floor = max(0.0, round(current_floor - r_step_ov, 2))

    ud['ratchet_floor_override'] = new_floor
    ud['locked_floor_val'] = new_floor
    ud['ratchet_locked'] = False
    ud['is_locked'] = False
    ud['manual_locked_day'] = None
    # Keep daily_peak_pct >= cur_day_pct so process_auto_profile does not wipe ratchet_floor_override on the next tick
    ud['daily_peak_pct'] = round(max(cur_day_pct, new_floor + (r_step_ov * 0.8)), 2)

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
    headers = {"APCA-API-KEY-ID": api_k, "APCA-API-SECRET-KEY": sec_k, "User-Agent": "IDCTradingTerminal/1.0"}

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

        with portfolio_store.trade_lock:
            if is_client:
                c_store = portfolio_store.get_client_store(client_name)
                users = c_store.setdefault('users', {})
                ud = users.setdefault(prof_name, portfolio_store.default_user_state(prof_name))
            else:
                portfolio_store.reload()
                ud = portfolio_store.user_data(prof_name)

            ud['master_budget'] = alpaca_last_equity
            ud['daily_peak_pct'] = max(0.0, today_pct)
            ud['ratchet_locked'] = False
            ud['is_locked'] = False
            ud['manual_locked_day'] = None
            ud['ratchet_floor_override'] = None
            ud['locked_floor_val'] = None

            # Clean stale ALPACASYNC adjustments on sibling profiles so shared-account totals stay exact
            all_profs_map = (c_store.get('users') or {}) if is_client else (portfolio_store.data.get('users') or {})
            for other_p, other_ud in all_profs_map.items():
                if other_p != prof_name and isinstance(other_ud, dict):
                    o_hist = other_ud.get('history') or []
                    cleaned_o = [tr for tr in o_hist if tr.get('ticker') != 'ALPACASYNC']
                    if len(cleaned_o) != len(o_hist):
                        other_ud['history'] = cleaned_o

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
        is_momentum = any(x in active_profile for x in ['vector', 'apex', 'precision', 'velocity', 'pulse', 'hybrid', 'test e', 'test s', 'test tq', 'test r', 'test tqm'])
        interval = "1m" if any(x in active_profile for x in ['1m reversal', 'pulse 1m', 'test p', 'test t']) else "5m"
        period = "1d" if any(x in active_profile for x in ['1m reversal', 'pulse 1m', 'test p', 'test t']) else "5d"
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
        is_momentum = any(x in active_profile for x in ['vector', 'apex', 'precision', 'velocity', 'pulse', 'hybrid', 'test e', 'test s', 'test tq', 'test r', 'test tqm'])
        
        dfs = {}
        def fetch_t(tick): 
            interval = "1m" if any(x in active_profile for x in ['1m reversal', 'pulse 1m', 'test p', 'test t']) else "5m"
            period = "1d" if any(x in active_profile for x in ['1m reversal', 'pulse 1m', 'test p', 'test t']) else "5d"
            return tick, fetch_yf_data(tick, period if is_momentum else "1y", interval if is_momentum else "1d")
            
        with ThreadPoolExecutor(max_workers=4) as ex:
            for tick, df_t in ex.map(fetch_t, scan_list): dfs[tick] = df_t
            
        for t in scan_list:
            df = dfs.get(t)
            if df is None or df.empty: continue
            df.name = t
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
                    allocation = 0.98 if any(x in active_profile for x in ['vector', 'apex', 'velocity', 'pulse', 'test e', 'test s', 'test u', 'test w', 'test x']) else (0.33 if is_momentum else 0.20)
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
        is_admin = is_admin_session()
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
            ud['strategy_name'] = active_name or ''
            ud['active_user'] = active_name or ''
            ud['account_username'] = client_name
            mb = float(ud.get('master_budget') or 0.0)
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
        is_momentum = any(x in active_profile for x in ['vector', 'apex', 'precision', 'velocity', 'pulse', 'hybrid', 'test e', 'test s', 'test tq', 'test r', 'test tqm'])
        is_rotator = is_momentum

        hist = ud.get('history') or []
        init_pos = ud.get('initial_positions') or {}

        def resolve_profile_shares(profile_ud, tk):
            if not isinstance(profile_ud, dict) or not tk:
                return 0
            i_pos_map = profile_ud.get('initial_positions') or {}
            i_sh = int((i_pos_map.get(tk) or {}).get('shares') or 0)
            n_sh = sum(int(tr.get('shares') or 0) if tr.get('action') == 'BUY' else -int(tr.get('shares') or 0) 
                       for tr in (profile_ud.get('history') or []) if tr.get('ticker') == tk)
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

        alp_live_px_map = {}
        if alpaca_acc and isinstance(alpaca_acc.get('positions'), list):
            for ap in alpaca_acc['positions']:
                sym_a = (ap.get('symbol') or '').upper()
                px_a = float(ap.get('current_price') or 0.0)
                if sym_a and px_a > 0:
                    alp_live_px_map[sym_a] = px_a

        tot_own = 0.0
        for tk in active_holds:
            sh_c = resolve_profile_shares(ud, tk)
            df_c = pnl_dfs_5m.get(tk)
            cur_p = alp_live_px_map.get(tk, 0.0)
            if cur_p <= 0 and df_c is not None and not df_c.empty:
                cur_p = float(df_c['Close'].iloc[-1])
            if cur_p > 0:
                div = 100.0 if str(tk).endswith('.L') and cur_p > 100 else 1.0
                tot_own += round(sh_c * (cur_p / div), 2)
        total_equity = cash_balance + tot_own
        enabled_live_top = [
            u_n for u_n in ((c_store.get('users', {}) if c_store else {}) if is_client else portfolio_store.data.get('users', {})).keys()
            if is_profile_alpaca_enabled(u_n)
        ]
        active_disp_name = active_name if is_client else (req_user or portfolio_store.active_username())
        if alpaca_acc and len(enabled_live_top) == 1 and active_disp_name == enabled_live_top[0]:
            alp_eq_top = float(alpaca_acc.get('equity') or 0.0)
            if alp_eq_top > 0:
                total_equity = alp_eq_top
                cash_balance = round(total_equity - tot_own, 2)
        master_pnl_val = total_equity - mb
        master_pnl_pct = (master_pnl_val / mb * 100.0) if mb > 0 else 0.0

        now_lon = pd.Timestamp.now(tz='Europe/London')
        today_str = now_lon.strftime('%Y-%m-%d')
        
        all_user_holds = set(['TQQQ', 'SOXL', 'SQQQ'])
        source_for_holds = (c_store.get('users', {}) if c_store else {}) if is_client else portfolio_store.data.get('users', {})
        for u, u_data in source_for_holds.items():
            if isinstance(u_data, dict):
                for tr in (u_data.get('history') or []):
                    if not isinstance(tr, dict):
                        continue
                    tk_sym = tr.get('ticker')
                    if tk_sym and tk_sym not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']:
                        all_user_holds.add(tk_sym)
                
        prices = {}
        def fetch_lb_price(tick):
            if not tick or tick in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']:
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

        # Pre-warm all needed market series in ONE parallel batch so /api/data responds in ~2s even on cold cache
        settings = ud.get('settings') or {}
        pre_p = request.args.get('p')
        if not pre_p or pre_p == 'undefined': pre_p = settings.get('period', '1d')
        pre_i = request.args.get('i')
        if not pre_i or pre_i == 'undefined': pre_i = settings.get('interval', '5m')
        if pre_p in ['1y', '5y', 'max'] and pre_i in ['1m', '2m', '5m', '15m', '30m', '60m', '1h']: pre_i = '1d'
        elif pre_p in ['1mo', '3mo', '6mo'] and pre_i in ['1m', '2m']: pre_i = '5m'
        pre_t = 'TQQQ' if t == 'ALL_SHARES' else t

        warm_tasks = set()
        for sym_w in all_user_holds:
            if sym_w and sym_w not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']:
                warm_tasks.add((sym_w, "5d", "5m"))
        warm_tasks.add(("QQQ", "5d", "5m"))
        warm_tasks.add(("NQ=F", "5d", "1d"))
        warm_tasks.add(("ES=F", "5d", "1d"))
        warm_tasks.add(("^VIX", "5d", "1d"))
        if pre_t:
            warm_tasks.add((pre_t, pre_p, pre_i))
            warm_tasks.add((pre_t, "1d", "1m"))

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda args: fetch_yf_data(args[0], args[1], args[2]), warm_tasks))
            for tick, cur_d, start_d in ex.map(fetch_lb_price, all_user_holds): 
                if tick in alp_live_px_map and alp_live_px_map[tick] > 0:
                    cur_d = alp_live_px_map[tick]
                prices[tick] = {'cur': cur_d, 'start': start_d}

        cached_fx = NEWS_CACHE.get('_usd_gbp')
        if cached_fx and (time.time() - cached_fx.get('ts', 0)) < 600:
            usd_gbp_rate = cached_fx['rate']
        else:
            usd_gbp_rate = 0.7499
            try:
                df_fx = fetch_yf_data("GBPUSD=X", "5d", "5m")
                if not df_fx.empty:
                    gbp_per_usd = 1.0 / float(df_fx['Close'].iloc[-1])
                    if gbp_per_usd > 0:
                        usd_gbp_rate = round(gbp_per_usd, 4)
                        NEWS_CACHE['_usd_gbp'] = {'ts': time.time(), 'rate': usd_gbp_rate}
            except Exception: pass

        leaderboard = []
        source_users = (c_store.get('users', {}) if c_store else {}) if is_client else portfolio_store.data.get('users', {})
        if not source_users and not is_client:
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
            
            trades_today = [tr for tr in hist_lb if tr.get('date_str') == today_str and tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION', 'ALPACASYNC']]
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

            enabled_live_list = [
                u_n for u_n in source_users.keys()
                if is_profile_alpaca_enabled(u_n)
            ]
            if alpaca_acc and len(enabled_live_list) == 1 and u == enabled_live_list[0]:
                alp_eq_live = float(alpaca_acc.get('equity') or 0.0)
                alp_last_live = float(alpaca_acc.get('last_equity') or 0.0)
                if alp_eq_live > 0:
                    tot_eq_now = alp_eq_live
                if alp_last_live > 0:
                    tot_eq_start = alp_last_live

            user_holds = [tk for tk, sh in shares_now_map.items() if sh > 0]
            held_sym = user_holds[0] if user_holds else ''
            day_diff = round(tot_eq_now - tot_eq_start, 2)
            day_pct = round((day_diff / tot_eq_start * 100.0), 2) if tot_eq_start > 0 else 0.0
            day_col = '#0ecb81' if day_diff > 0 else ('#ff5364' if day_diff < 0 else '#8a8a9e')

            nh_before_epoch = sum(
                -float(tr.get('amount') or 0.0) if tr.get('action') == 'BUY' else float(tr.get('amount') or 0.0)
                for tr in hist_lb
                if (tr.get('date_str') or '') < PERFORMANCE_START_DATE and tr.get('ticker') not in ['CAPITAL_INJECTION', 'CALIBRATION']
            )
            epoch_budget = max(50.0, mb_lb + nh_before_epoch - im_lb)

            trade_dates = sorted(list(set(
                tr.get('date_str') for tr in hist_lb
                if tr.get('date_str') and tr.get('date_str') >= PERFORMANCE_START_DATE
                and tr.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']
            )))
            start_date = max(trade_dates[0], PERFORMANCE_START_DATE) if trade_dates else max(today_str, PERFORMANCE_START_DATE)
            active_days = max(1, len(pd.bdate_range(start_date, today_str)))

            if active_days == 1:
                daily_avg_pct = day_pct
                daily_avg_val = day_diff
            else:
                cum_gain_val = tot_eq_now - epoch_budget
                cum_gain_pct = (cum_gain_val / epoch_budget * 100.0) if epoch_budget > 0 else 0.0
                daily_avg_pct = round(cum_gain_pct / active_days, 2)
                daily_avg_val = round(cum_gain_val / active_days, 2)

            # Persist daily_peak_pct monotonically so UI & background engine share the exact same Ratchet high-water mark
            if u_data.get('last_ratchet_date') != today_str:
                u_data['last_ratchet_date'] = today_str
                u_data['daily_peak_pct'] = max(0.0, day_pct)
            elif day_pct > float(u_data.get('daily_peak_pct') or 0.0):
                new_peak_val = round(day_pct, 2)
                u_data['daily_peak_pct'] = new_peak_val
                with portfolio_store.trade_lock:
                    if is_client:
                        fresh_cs = portfolio_store.get_client_store(client_name) or {}
                        fresh_u = (fresh_cs.get('users') or {}).get(u)
                        if isinstance(fresh_u, dict) and new_peak_val > float(fresh_u.get('daily_peak_pct') or 0.0):
                            fresh_u['daily_peak_pct'] = new_peak_val
                            portfolio_store.save_client_store(client_name, fresh_cs)
                    else:
                        portfolio_store.reload()
                        fresh_u = (portfolio_store.data.get('users') or {}).get(u)
                        if isinstance(fresh_u, dict) and new_peak_val > float(fresh_u.get('daily_peak_pct') or 0.0):
                            fresh_u['daily_peak_pct'] = new_peak_val
                            portfolio_store.save_data(portfolio_store.data)

            is_p1m_lb = any(x in u.lower() for x in ['1m reversal', 'pulse 1m'])
            r_start_lb = 5.00 if is_p1m_lb else 2.50
            r_step_lb = 5.00 if is_p1m_lb else 0.50
            peak_pct_today = max(float(u_data.get('daily_peak_pct') or 0.0), day_pct)
            stored_locked_floor = float(u_data.get('locked_floor_val') or 0.0)
            computed_floor = round(int(peak_pct_today / r_step_lb) * r_step_lb, 2) if peak_pct_today >= r_start_lb else stored_locked_floor

            has_override = (u_data.get('ratchet_floor_override') is not None)
            is_manually_locked = (u_data.get('manual_locked_day') == today_str)
            is_ratchet_locked = bool(u_data.get('ratchet_locked') and len(user_holds) == 0 and (peak_pct_today >= r_start_lb or has_override))
            is_locked = is_manually_locked or is_ratchet_locked or (len(user_holds) == 0 and day_pct >= r_start_lb and not has_override)

            if has_override:
                active_floor_val = float(u_data.get('ratchet_floor_override'))
            elif computed_floor > 0:
                active_floor_val = computed_floor
            elif is_ratchet_locked:
                active_floor_val = r_start_lb
            else:
                active_floor_val = 0.0

            leaderboard.append({
                'user': u, 
                'equity': round(tot_eq_now, 2), 
                'budget': mb_lb,
                'epoch_budget': round(epoch_budget, 2),
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
                'active_floor': active_floor_val,
                'daily_peak_pct': round(peak_pct_today, 2),
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
        macro_pulse = engine.get_macro_pulse()

        if t == 'ALL_SHARES':
            t = 'TQQQ'

        df = fetch_yf_data(t, req_p, req_i)
        if df.empty:
            return jsonify(_sanitize_json_payload({'ohlc': [], 'wl_status': wl_status, 'name': f'{t}', 'portfolio': ud, 'leaderboard': leaderboard, 'usd_gbp_rate': usd_gbp_rate, 'metrics': {
                'price': 0, 'price_display': '$0.00', 'discount': '--', 'buy_score': '0 / 100', 'tranches': 0,
                'status': 'Loading Data', 'color': '#787e8e', 'reason': 'Fetching fresh market quotes.',
                'action_main': 'WAIT', 'action_sub': '', 'action_color': '#787e8e', 'shares_owned': 0, 'value_owned': 0.0,
                'pnl_display': '$0.00 (0.00%)', 'pnl_color': '#8a8a9e', 'pnl_data': master_pnl_data, 'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data,
                'master_budget': mb, 'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime,
                'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': '', 'is_rotator_profile': is_rotator, 'macro_pulse': macro_pulse
            }}))

        df_chart = df.copy()
        if df_chart.index.tz is not None: df_chart.index = df_chart.index.tz_convert('UTC')
        df_chart.name = t
        data = [{'time': i.strftime('%Y-%m-%d') if req_i in ['1d','5d','1wk','1mo','3mo'] else int(i.timestamp()), 
                 'open': round(float(r['Open']),2), 'high': round(float(r['High']),2), 'low': round(float(r['Low']),2), 'close': round(float(r['Close']),2)} 
                for i, r in df_chart.iterrows()
                if pd.notna(r.get('Open')) and pd.notna(r.get('High')) and pd.notna(r.get('Low')) and pd.notna(r.get('Close'))]
        seen = set(); data = [x for x in data if x['time'] not in seen and not seen.add(x['time'])]
        last_p = round(data[-1]['close'], 2) if data else 0.0

        sh_own = resolve_profile_shares(ud, t)
        cps = last_p / 100.0 if str(t).endswith('.L') and last_p > 100 else last_p
        val_own = round(sh_own * cps, 2)

        avg_buy_p, highest_p, buy_ts = 0.0, last_p, 0
        holds_dict = ud.get('holdings') or {}
        if sh_own > 0:
            t_buys = [tr for tr in hist if tr.get('ticker') == t and tr.get('action') == 'BUY']
            if t_buys: 
                avg_buy_p = float(t_buys[0].get('price') or 0.0)
                buy_ts = int(t_buys[0].get('timestamp') or 0)
                df_1m_chk = fetch_yf_data(t, "1d", "1m")
                df_check = df_1m_chk if (df_1m_chk is not None and not df_1m_chk.empty) else (pnl_dfs_5m.get(t) if pnl_dfs_5m.get(t) is not None else fetch_yf_data(t, "5d", "5m"))
                if df_check is not None and not df_check.empty and 'High' in df_check.columns:
                    try:
                        post_buy_df = df_check[df_check.index.astype('int64') // 10**9 >= (buy_ts + 60)]
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
                with portfolio_store.trade_lock:
                    if is_client:
                        fresh_cs = portfolio_store.get_client_store(client_name) or {}
                        fresh_u = (fresh_cs.get('users') or {}).get(active_name)
                        if isinstance(fresh_u, dict) and t in (fresh_u.get('holdings') or {}):
                            fresh_u['holdings'][t]['high_water'] = max(highest_p, float(fresh_u['holdings'][t].get('high_water') or 0.0))
                            portfolio_store.save_client_store(client_name, fresh_cs)
                    else:
                        portfolio_store.reload()
                        fresh_u = portfolio_store.user_data(req_user if req_user else None)
                        if isinstance(fresh_u, dict) and t in (fresh_u.get('holdings') or {}):
                            fresh_u['holdings'][t]['high_water'] = max(highest_p, float(fresh_u['holdings'][t].get('high_water') or 0.0))
                            portfolio_store.save_data(portfolio_store.data)

        if is_momentum:
            df_score = pnl_dfs_5m.get(t) if pnl_dfs_5m.get(t) is not None else fetch_yf_data(t, "5d", "5m")
            if df_score is None or df_score.empty: df_score = df
            df_score.name = t
            score_px = last_p
            if now_lon.weekday() < 5 and now_lon.hour == 14 and 30 <= now_lon.minute < 50:
                df_1m_ui = fetch_yf_data(t, "1d", "1m")
                if df_1m_ui is not None and not df_1m_ui.empty:
                    score_px = float(df_1m_ui['Close'].iloc[-1])
            st = engine.score_momentum(df_score, score_px, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=active_profile, regime=regime, overnight_sweep=bool(ud.get('overnight_sweep', True)), buy_timestamp=buy_ts)
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
                ema21_dict = {idx.strftime('%Y-%m-%d') if req_i in ['1d','5d','1wk','1mo','3mo'] else int(idx.timestamp()): float(val) for idx, val in ema21_series.items() if pd.notna(val)}
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

        # Compute real-time Buying Block Timers (15m Post-Sell Cooldown) + 3-Strike Loss Count
        now_ts_guard = int(time.time())
        real_hist_guard = [
            h for h in hist
            if h.get('ticker') and h.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']
        ]
        cd_total_guard = 60 if any(x in active_profile for x in ['1m reversal', 'pulse 1m']) else 900
        cd_15m_rem = 0
        if real_hist_guard and real_hist_guard[0].get('action') == 'SELL':
            last_sell_ts = int(real_hist_guard[0].get('timestamp') or 0)
            if last_sell_ts > 0:
                cd_15m_rem = max(0, cd_total_guard - (now_ts_guard - last_sell_ts))

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
            'cd_15m_total': cd_total_guard,
            'strikes_used': min(3, guard_loss_stops),
            'strikes_max': 3,
            'server_ts': now_ts_guard
        }

        return jsonify(_sanitize_json_payload({
            'ohlc': data, 'ticker': t, 'mathLine': mathLine, 'wl_status': wl_status, 'name': engine.asset_names.get(t, t), 'portfolio': ud, 'leaderboard': leaderboard, 'usd_gbp_rate': usd_gbp_rate, 'metrics': {
                'price': last_p, 'price_display': f"{last_p:.2f}p (£{(last_p/100.0):.2f})" if str(t).endswith('.L') and last_p>100 else f"${last_p:.2f}",
                'discount': st.get('discount', '--'), 'buy_score': f"{st.get('score', 0)} / 100", 'tranches': st.get('tranches', 0),
                'status': st.get('status', '--'), 'color': st.get('color', '#8a8a9e'), 'reason': st.get('reason', ''),
                'action_main': st.get('action_main', 'HOLD'), 'action_sub': st.get('action_sub', ''), 'action_color': st.get('action_color', '#8a8a9e'),
                'shares_owned': sh_own, 'value_owned': val_own, 'pnl_display': ticker_pnl_data['all']['val'], 'pnl_color': ticker_pnl_data['all']['color'], 'pnl_data': ticker_pnl_data,
                'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data, 'master_budget': mb,
                'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime, 'trade_type': st.get('trade_type', 'LONG'),
                'health_pct': st.get('health_pct', 0), 'health_color': st.get('health_color', '#8a8a9e'), 'health_text': st.get('health_text', 'N/A'), 'is_rotator_profile': is_rotator,
                'buy_guards': buy_guards, 'macro_pulse': macro_pulse,
                'pulse_1m_bars': engine.get_pulse_1m_snapshot(t, hist=hist, current_price=last_p) if any(x in active_profile for x in ['1m reversal', 'pulse 1m']) else None
            }
        }))
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

@app.route('/api/report/tax_estimator', methods=['GET'])
@login_required
def export_tax_estimator():
    import io, calendar
    from flask import send_file
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    portfolio_store.reload()
    now_lon = pd.Timestamp.now(tz='Europe/London')
    is_client = not is_admin_session()
    client_name = session.get('email') or session.get('user', 'Client')

    if is_client:
        c_store = portfolio_store.get_client_store(client_name) or portfolio_store.blank_client_state(client_name)
        users_data = c_store.get('users') or {}
    else:
        users_data = portfolio_store.data.get('users') or {}

    # Live USD -> GBP FX rate (fallback 0.7499)
    usd_gbp_rate = 0.7499
    try:
        df_fx = fetch_yf_data("GBPUSD=X", "5d", "5m")
        if not df_fx.empty:
            gbp_per_usd = 1.0 / float(df_fx['Close'].iloc[-1])
            if gbp_per_usd > 0:
                usd_gbp_rate = round(gbp_per_usd, 4)
    except Exception:
        pass

    # Active UK Tax Year (6 April -> 5 April)
    fy_start_year = now_lon.year if (now_lon.month > 4 or (now_lon.month == 4 and now_lon.day >= 6)) else (now_lon.year - 1)
    fy_end_year = fy_start_year + 1
    fy_start_str = f"{fy_start_year}-04-06"
    fy_end_str = f"{fy_end_year}-04-05"
    fy_label = f"{fy_start_year}/{str(fy_end_year)[-2:]}"
    sa_deadline_str = f"31 January {fy_end_year + 1}"

    # Statutory Trading Allowance: £1,000 up to 5 April 2027; rises to £3,000 from 6 April 2027 (2027/28+)
    default_trading_allowance = 3000.0 if fy_start_year >= 2027 else 1000.0

    # Build chronological list of 13 UK tax-year month tabs (Apr YYYY -> Apr YYYY+1)
    month_specs = []
    for offset in range(13):
        m_num = ((4 - 1 + offset) % 12) + 1
        y_num = fy_start_year + ((4 - 1 + offset) // 12)
        m_abbr = calendar.month_abbr[m_num]
        tab_name = f"{m_abbr} {y_num}"
        if offset == 0:
            d_from, d_to = f"{y_num}-04-06", f"{y_num}-04-30"
            period_desc = f"6 Apr – 30 Apr {y_num}"
        elif offset == 12:
            d_from, d_to = f"{y_num}-04-01", f"{y_num}-04-05"
            period_desc = f"1 Apr – 5 Apr {y_num}"
        else:
            last_day = calendar.monthrange(y_num, m_num)[1]
            d_from, d_to = f"{y_num}-{m_num:02d}-01", f"{y_num}-{m_num:02d}-{last_day:02d}"
            period_desc = f"1 {m_abbr} – {last_day} {m_abbr} {y_num}"
        month_specs.append({
            'tab': tab_name,
            'd_from': d_from,
            'd_to': d_to,
            'desc': period_desc,
            'trades': [],
            'same_day_buckets': {}
        })

    def get_month_bucket(date_str):
        for ms in month_specs:
            if ms['d_from'] <= date_str <= ms['d_to']:
                return ms
        return None

    # Extract all closed trades across every strategy profile for the active UK tax year (HMRC same-day matched)
    all_tickers_seen = set(['TQQQ', 'SOXL', 'SQQQ'])
    for u_name, u_data in users_data.items():
        if not isinstance(u_data, dict):
            continue
        hist = u_data.get('history') or []
        buy_queues = {}
        for tr in reversed(hist):
            tk = (tr.get('ticker') or '').strip().upper()
            if not tk or tk in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']:
                continue
            all_tickers_seen.add(tk)
            act = (tr.get('action') or '').upper()
            sh = int(float(tr.get('shares') or 0))
            pr = float(tr.get('price') or 0.0)
            amt = float(tr.get('amount') or (sh * pr))
            if sh <= 0 or pr <= 0:
                continue

            if act == 'BUY':
                buy_queues.setdefault(tk, []).append({'shares': sh, 'amount': amt, 'price': pr})
            elif act == 'SELL':
                rem = sh
                cost_basis = 0.0
                matched_sh = 0
                entry_px_sum = 0.0
                q = buy_queues.get(tk) or []
                while q and rem > 0:
                    b = q[0]
                    m = min(b['shares'], rem)
                    unit_c = (b['amount'] / b['shares']) if b['shares'] > 0 else b['price']
                    cost_basis += m * unit_c
                    entry_px_sum += m * b['price']
                    matched_sh += m
                    rem -= m
                    b['shares'] -= m
                    b['amount'] -= m * unit_c
                    if b['shares'] <= 0:
                        q.pop(0)

                if matched_sh > 0:
                    sell_proceeds = amt * (matched_sh / sh)
                    pnl_usd = round(sell_proceeds - cost_basis, 2)
                    avg_buy_px = round(entry_px_sum / matched_sh, 2)
                    d_str = tr.get('date_str') or now_lon.strftime('%Y-%m-%d')
                    if fy_start_str <= d_str <= fy_end_str:
                        ms = get_month_bucket(d_str)
                        if ms is not None:
                            ms['trades'].append({
                                'date': d_str,
                                'profile': u_name,
                                'ticker': tk,
                                'shares': matched_sh,
                                'buy_price': avg_buy_px,
                                'sell_price': round(pr, 2),
                                'pnl_usd': pnl_usd
                            })
                            ms['same_day_buckets'][(tk, d_str)] = round(ms['same_day_buckets'].get((tk, d_str), 0.0) + pnl_usd, 2)

    ordered_tickers = ['TQQQ', 'SOXL', 'SQQQ'] + sorted([x for x in all_tickers_seen if x not in ['TQQQ', 'SOXL', 'SQQQ']])
    num_tk = len(ordered_tickers)

    wb = openpyxl.Workbook()

    # Clean, spacious light executive palette (zero black tables)
    navy_banner = PatternFill(start_color="1E3A5F", end_color="1E3A5F", fill_type="solid")
    sub_banner = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    sec_fill = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")
    tbl_hdr_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    input_fill = PatternFill(start_color="FFFBEB", end_color="FFFBEB", fill_type="solid")
    subtotal_fill = PatternFill(start_color="EFF6FF", end_color="EFF6FF", fill_type="solid")
    final_due_fill = PatternFill(start_color="ECFDF5", end_color="ECFDF5", fill_type="solid")

    title_font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    subtitle_font = Font(name="Calibri", size=9.5, italic=True, color="475569")
    sec_font = Font(name="Calibri", size=10.5, bold=True, color="0F172A")
    tbl_hdr_font = Font(name="Calibri", size=10, bold=True, color="1E293B")
    lbl_font = Font(name="Calibri", size=10, bold=True, color="1E293B")
    note_font = Font(name="Calibri", size=9.5, italic=True, color="64748B")
    input_font = Font(name="Calibri", size=11, bold=True, color="0F172A")
    val_font = Font(name="Calibri", size=10, bold=True, color="0F172A")
    green_font = Font(name="Calibri", size=10, bold=True, color="047857")
    red_font = Font(name="Calibri", size=10, bold=True, color="B91C1C")
    final_font = Font(name="Calibri", size=12, bold=True, color="065F46")

    light_border = Border(
        left=Side(style='thin', color='CBD5E1'), right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'), bottom=Side(style='thin', color='CBD5E1')
    )
    hdr_border = Border(
        left=Side(style='thin', color='CBD5E1'), right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='94A3B8'), bottom=Side(style='medium', color='94A3B8')
    )
    input_border = Border(
        left=Side(style='medium', color='D97706'), right=Side(style='medium', color='D97706'),
        top=Side(style='medium', color='D97706'), bottom=Side(style='medium', color='D97706')
    )
    subtotal_border = Border(
        left=Side(style='thin', color='93C5FD'), right=Side(style='thin', color='93C5FD'),
        top=Side(style='thin', color='60A5FA'), bottom=Side(style='double', color='2563EB')
    )
    final_border = Border(
        left=Side(style='medium', color='059669'), right=Side(style='medium', color='059669'),
        top=Side(style='medium', color='059669'), bottom=Side(style='medium', color='059669')
    )

    def add_section_bar(ws_t, row_idx, end_col, title_txt):
        ws_t.merge_cells(start_row=row_idx, start_column=2, end_row=row_idx, end_column=end_col)
        c = ws_t.cell(row=row_idx, column=2, value=title_txt)
        c.font = sec_font
        c.fill = sec_fill
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        for ci in range(2, end_col + 1):
            ws_t.cell(row=row_idx, column=ci).fill = sec_fill
            ws_t.cell(row=row_idx, column=ci).border = light_border
        ws_t.row_dimensions[row_idx].height = 26

    qs_tab_name = "Quick Tax Summary"
    fy_tab_name = f"FY {fy_start_year}-{str(fy_end_year)[-2:]} Total"
    qs_ref = f"'{qs_tab_name}'"
    fy_ref = f"'{fy_tab_name}'"

    m_share_first = 12
    m_share_last = m_share_first + num_tk - 1
    m_tr_sec_r = m_share_last + 2

    # ==========================================================================
    # TAB 1: QUICK TAX SUMMARY (SIMPLE OVERVIEW & EXACT HMRC CALCULATION)
    # ==========================================================================
    ws_qs = wb.active
    ws_qs.title = qs_tab_name
    ws_qs.views.sheetView[0].showGridLines = True

    qs_widths = {'A': 3, 'B': 48, 'C': 18, 'D': 18, 'E': 18, 'F': 18, 'G': 48}
    for cl, wv in qs_widths.items():
        ws_qs.column_dimensions[cl].width = wv

    ws_qs.row_dimensions[1].height = 10
    ws_qs.merge_cells('B2:G2')
    ws_qs['B2'] = f"IDC TRADING • UK TAX SUMMARY ({fy_label} TAX YEAR: 6 APR {fy_start_year} – 5 APR {fy_end_year})"
    ws_qs['B2'].font = title_font; ws_qs['B2'].fill = navy_banner; ws_qs['B2'].alignment = Alignment(horizontal="center", vertical="center")
    ws_qs.row_dimensions[2].height = 34

    ws_qs.merge_cells('B3:G3')
    ws_qs['B3'] = "Enter your Annual Salary and Extra Invoices total in the highlighted boxes. Salary PAYE tax is excluded — your Salary is used only to place Extra Invoices & Alpaca in the right UK tax bands."
    ws_qs['B3'].font = subtitle_font; ws_qs['B3'].fill = sub_banner; ws_qs['B3'].alignment = Alignment(horizontal="center", vertical="center")
    ws_qs.row_dimensions[3].height = 22
    ws_qs.row_dimensions[4].height = 12

    add_section_bar(ws_qs, 5, 7, f"1. SALARY, EXTRA INVOICES & ALLOWANCES ({fy_label})")

    qs_sec1_rows = [
        (6, "Annual Salary (PAYE) (£)", 0.0, '£#,##0.00', "Places your extra earnings in the right UK tax band & caps Class 4 NI (PAYE tax already paid monthly)", True),
        (7, "Extra Invoices Total (£)", 0.0, '£#,##0.00', "Enter your gross extra invoices total for the tax year from your invoicing system", True),
        (8, "Allowable Business Expenses (£)", 0.0, '£#,##0.00', "Actual expenses against extra invoices (HMRC uses whichever is higher: Expenses or Trading Allowance)", True),
        (9, "HMRC Trading Allowance (£)", default_trading_allowance, '£#,##0.00', "£1,000 for 2026/27 (rises to £3,000 from 6 Apr 2027 — editable to model both). Tax-free deduction!", True),
        (10, "Net Taxable Extra Invoice Profit (£)", "=MAX(0, C7-MAX(C8, C9))", '£#,##0.00', "Gross Extra Invoices minus MAX(Expenses, Trading Allowance) — never below £0.00", False),
        (11, "Gross SIPP / Pension Contributions (£)", 0.0, '£#,##0.00', "Optional pension contributions (extends 20% band & restores £100k Personal Allowance)", True),
        (12, "USD → GBP Exchange Rate", usd_gbp_rate, '0.0000', f"Live Yahoo FX rate ({usd_gbp_rate:.4f}) used across all tabs to convert Alpaca USD profits to £", True),
    ]

    qs_unlocked = []
    for r_i, lbl_t, val_v, fmt_s, hint_t, is_inp in qs_sec1_rows:
        ws_qs.row_dimensions[r_i].height = 25
        c_l = ws_qs.cell(row=r_i, column=2, value=lbl_t)
        c_l.font = lbl_font; c_l.fill = white_fill if is_inp else zebra_fill; c_l.border = light_border; c_l.alignment = Alignment(vertical="center", indent=1)

        c_v = ws_qs.cell(row=r_i, column=3, value=val_v)
        c_v.font = input_font if is_inp else val_font
        c_v.fill = input_fill if is_inp else zebra_fill
        c_v.border = input_border if is_inp else light_border
        c_v.number_format = fmt_s
        c_v.alignment = Alignment(horizontal="right", vertical="center")
        if is_inp:
            qs_unlocked.append((r_i, 3))

        ws_qs.merge_cells(start_row=r_i, start_column=4, end_row=r_i, end_column=7)
        c_h = ws_qs.cell(row=r_i, column=4, value=hint_t)
        c_h.font = note_font; c_h.fill = white_fill if is_inp else zebra_fill; c_h.alignment = Alignment(vertical="center", indent=1)
        for ci in range(4, 8):
            ws_qs.cell(row=r_i, column=ci).fill = white_fill if is_inp else zebra_fill
            ws_qs.cell(row=r_i, column=ci).border = light_border

    ws_qs.row_dimensions[13].height = 14

    add_section_bar(ws_qs, 14, 7, f"2. ALPACA TRADING TOTALS ({fy_label} — AUTO-SYNCED FROM MONTHLY TABS)")

    qs_hdr_r2 = 15
    ws_qs.row_dimensions[qs_hdr_r2].height = 25
    qs_s2_headers = ["Share / ETF", "Closed Trades", "Taxable Wins ($)", "Taxable Wins (£)", "Capital Losses (£)", "UK Tax Treatment"]
    for idx, h_txt in enumerate(qs_s2_headers, start=2):
        c = ws_qs.cell(row=qs_hdr_r2, column=idx, value=h_txt)
        c.font = tbl_hdr_font; c.fill = tbl_hdr_fill; c.border = hdr_border
        c.alignment = Alignment(horizontal="center" if idx in [3, 4, 5, 6] else "left", vertical="center", indent=0 if idx in [3, 4, 5, 6] else 1)

    qs_share_first = 16
    fy_share_first = 6
    for idx_t, tk in enumerate(ordered_tickers):
        r_q = qs_share_first + idx_t
        r_f = fy_share_first + idx_t
        ws_qs.row_dimensions[r_q].height = 23
        r_bg = white_fill if idx_t % 2 == 0 else zebra_fill

        c_tk = ws_qs.cell(row=r_q, column=2, value=tk)
        c_tk.font = lbl_font; c_tk.fill = r_bg; c_tk.border = light_border; c_tk.alignment = Alignment(vertical="center", indent=1)

        c_tr = ws_qs.cell(row=r_q, column=3, value=f"={fy_ref}!C{r_f}")
        c_tr.font = val_font; c_tr.fill = r_bg; c_tr.border = light_border; c_tr.number_format = '#,##0'; c_tr.alignment = Alignment(horizontal="center", vertical="center")

        c_wu = ws_qs.cell(row=r_q, column=4, value=f"={fy_ref}!D{r_f}")
        c_wu.font = green_font; c_wu.fill = r_bg; c_wu.border = light_border; c_wu.number_format = '$#,##0.00'; c_wu.alignment = Alignment(horizontal="right", vertical="center")

        c_wg = ws_qs.cell(row=r_q, column=5, value=f"={fy_ref}!E{r_f}")
        c_wg.font = green_font; c_wg.fill = r_bg; c_wg.border = light_border; c_wg.number_format = '£#,##0.00'; c_wg.alignment = Alignment(horizontal="right", vertical="center")

        c_lg = ws_qs.cell(row=r_q, column=6, value=f"={fy_ref}!G{r_f}")
        c_lg.font = red_font; c_lg.fill = r_bg; c_lg.border = light_border; c_lg.number_format = '£#,##0.00'; c_lg.alignment = Alignment(horizontal="right", vertical="center")

        c_nt = ws_qs.cell(row=r_q, column=7, value=f'=IF(D{r_q}>0, "Offshore Income Gain (20%–45%, 0% NI)", IF(F{r_q}>0, "Capital loss carried forward separately", "No closed trades in tax year"))')
        c_nt.font = note_font; c_nt.fill = r_bg; c_nt.border = light_border; c_nt.alignment = Alignment(vertical="center", indent=1)

    qs_share_last = qs_share_first + num_tk - 1
    qs_alp_tot_r = qs_share_last + 1
    ws_qs.row_dimensions[qs_alp_tot_r].height = 26

    ws_qs.cell(row=qs_alp_tot_r, column=2, value="TOTAL ALPACA EARNINGS").font = lbl_font
    ws_qs.cell(row=qs_alp_tot_r, column=2).fill = subtotal_fill; ws_qs.cell(row=qs_alp_tot_r, column=2).border = subtotal_border; ws_qs.cell(row=qs_alp_tot_r, column=2).alignment = Alignment(vertical="center", indent=1)

    for col_i, fmt_s, f_use in [
        (3, '#,##0', val_font), (4, '$#,##0.00', green_font), (5, '£#,##0.00', green_font), (6, '£#,##0.00', red_font)
    ]:
        col_let = get_column_letter(col_i)
        c_s = ws_qs.cell(row=qs_alp_tot_r, column=col_i, value=f"=SUM({col_let}{qs_share_first}:{col_let}{qs_share_last})")
        c_s.font = f_use; c_s.fill = subtotal_fill; c_s.border = subtotal_border; c_s.number_format = fmt_s
        c_s.alignment = Alignment(horizontal="center" if col_i == 3 else "right", vertical="center")

    c_qnt = ws_qs.cell(row=qs_alp_tot_r, column=7, value=f'=IF(E{qs_alp_tot_r}>0, "Stacked above Salary & Extra Invoices below (0% NI)", "No taxable Alpaca gains yet")')
    c_qnt.font = note_font; c_qnt.fill = subtotal_fill; c_qnt.border = subtotal_border; c_qnt.alignment = Alignment(vertical="center", indent=1)

    ws_qs.row_dimensions[qs_alp_tot_r + 1].height = 14

    qs_sec3_r = qs_alp_tot_r + 2
    add_section_bar(ws_qs, qs_sec3_r, 7, f"3. EXACT UK TAX & NI BREAKDOWN (EXTRA INVOICES + ALPACA ONLY)")

    r_sal_adj = qs_sec3_r + 1
    r_comb_adj = qs_sec3_r + 2
    r_pa_rem = qs_sec3_r + 3
    r_marg_rt = qs_sec3_r + 4

    r_b_hdr = qs_sec3_r + 6
    r_b_20 = r_b_hdr + 1
    r_b_40 = r_b_hdr + 2
    r_b_60 = r_b_hdr + 3
    r_b_45 = r_b_hdr + 4
    r_it_tot = r_b_hdr + 5
    r_c4_ni = r_b_hdr + 6
    r_grand_tot = r_b_hdr + 7
    r_monthly_pot = r_b_hdr + 8

    top_calc_rows = [
        (r_sal_adj, "Adjusted Salary (PAYE — Tax Already Paid Monthly) (£)", "=MAX(0, C6-C11)", val_font, white_fill, "Your salary uses the £12,570 Personal Allowance & 20% Basic Band first"),
        (r_comb_adj, "Total Combined Adjusted Income (Salary + Extra + Alpaca − SIPP) (£)", f"=MAX(0, C6+C10+E{qs_alp_tot_r}-C11)", val_font, zebra_fill, "Determines how your Extra Invoices & Alpaca slice across UK tax bands"),
        (r_pa_rem, "Personal Allowance Remaining After £100k Taper (£)", f"=MAX(0, 12570 - MAX(0, (C{r_comb_adj}-100000)/2))", val_font, white_fill, "£12,570 standard allowance; reduces by £1 for every £2 earned above £100,000"),
        (r_marg_rt, "Your Marginal Income Tax Rate on Next £1 Earned", f"=IF(C{r_comb_adj}>125140, 0.45, IF(C{r_comb_adj}>100000, 0.60, IF(C{r_comb_adj}>50270, 0.40, IF(C{r_comb_adj}>12570, 0.20, 0))))", val_font, zebra_fill, "Used by monthly tabs to estimate your monthly tax ring-fence"),
    ]

    for r_i, lbl_t, fml_s, f_col, bg_u, hint_t in top_calc_rows:
        ws_qs.row_dimensions[r_i].height = 24
        c_l = ws_qs.cell(row=r_i, column=2, value=lbl_t)
        c_l.font = lbl_font; c_l.fill = bg_u; c_l.border = light_border; c_l.alignment = Alignment(vertical="center", indent=1)
        c_v = ws_qs.cell(row=r_i, column=3, value=fml_s)
        c_v.font = f_col; c_v.fill = bg_u; c_v.border = light_border
        c_v.number_format = '0.0%' if r_i == r_marg_rt else '£#,##0.00'
        c_v.alignment = Alignment(horizontal="right", vertical="center")
        ws_qs.merge_cells(start_row=r_i, start_column=4, end_row=r_i, end_column=7)
        c_h = ws_qs.cell(row=r_i, column=4, value=hint_t)
        c_h.font = note_font; c_h.fill = bg_u; c_h.alignment = Alignment(vertical="center", indent=1)
        for ci in range(4, 8):
            ws_qs.cell(row=r_i, column=ci).fill = bg_u
            ws_qs.cell(row=r_i, column=ci).border = light_border

    ws_qs.row_dimensions[qs_sec3_r + 5].height = 10

    ws_qs.row_dimensions[r_b_hdr].height = 25
    ws_qs.cell(row=r_b_hdr, column=2, value="UK Tax / NI Band (Extra Earnings Slice Only)").font = tbl_hdr_font
    ws_qs.cell(row=r_b_hdr, column=2).fill = tbl_hdr_fill; ws_qs.cell(row=r_b_hdr, column=2).border = hdr_border; ws_qs.cell(row=r_b_hdr, column=2).alignment = Alignment(vertical="center", indent=1)
    ws_qs.cell(row=r_b_hdr, column=3, value="Taxable Slice (£)").font = tbl_hdr_font
    ws_qs.cell(row=r_b_hdr, column=3).fill = tbl_hdr_fill; ws_qs.cell(row=r_b_hdr, column=3).border = hdr_border; ws_qs.cell(row=r_b_hdr, column=3).alignment = Alignment(horizontal="center", vertical="center")
    ws_qs.cell(row=r_b_hdr, column=4, value="Tax / NI Due (£)").font = tbl_hdr_font
    ws_qs.cell(row=r_b_hdr, column=4).fill = tbl_hdr_fill; ws_qs.cell(row=r_b_hdr, column=4).border = hdr_border; ws_qs.cell(row=r_b_hdr, column=4).alignment = Alignment(horizontal="center", vertical="center")
    ws_qs.merge_cells(start_row=r_b_hdr, start_column=5, end_row=r_b_hdr, end_column=7)
    ws_qs.cell(row=r_b_hdr, column=5, value="How HMRC Calculates This Band").font = tbl_hdr_font
    ws_qs.cell(row=r_b_hdr, column=5).fill = tbl_hdr_fill; ws_qs.cell(row=r_b_hdr, column=5).alignment = Alignment(vertical="center", indent=1)
    for ci in range(5, 8):
        ws_qs.cell(row=r_b_hdr, column=ci).fill = tbl_hdr_fill
        ws_qs.cell(row=r_b_hdr, column=ci).border = hdr_border

    pa_sal_expr = f"MAX(0, 12570 - MAX(0, (C{r_sal_adj}-100000)/2))"
    lost_pa_expr = f"MAX(0, {pa_sal_expr} - C{r_pa_rem})"

    # Exact Class 4 NI with HMRC Regulation 100 / NIM24175 Annual Maximum Cap (when Employed + Self-Employed)
    step4_pos_expr = "MAX(0, 2262 - MAX(0, MIN(50270, C6) - 12570)*0.08)"
    c4_main_uncapped_expr = "MAX(0, MIN(50270, C10) - 12570)*0.06"
    c4_reg100_expr = (
        f"ROUND(MIN({step4_pos_expr}, {c4_main_uncapped_expr}) + "
        f"MAX(0, (MIN(50270, C10) - 12570) - ({step4_pos_expr}*100/6))*0.02 + "
        f"MAX(0, C10 - 50270)*0.02, 2)"
    )

    band_slice_rows = [
        (r_b_20, "Basic Rate Income Tax (20%)", f"=MAX(0, MIN(50270, C{r_comb_adj}) - MAX(12570, C{r_sal_adj}))", f"=ROUND(C{r_b_20}*0.20, 2)", white_fill, "20% on any unused basic-rate headroom between £12,570 and £50,270"),
        (r_b_40, "Higher Rate Income Tax (40%)", f"=MAX(0, MIN(125140, C{r_comb_adj}) - MAX(50270, C{r_sal_adj}))", f"=ROUND(C{r_b_40}*0.40, 2)", zebra_fill, "40% on extra earnings sitting between £50,270 and £125,140"),
        (r_b_60, "Personal Allowance Taper Charge (£100k–£125,140)", f"=ROUND({lost_pa_expr}*2, 2)", f"=ROUND({lost_pa_expr}*0.40, 2)", white_fill, "Extra 20% effective tax (making 60% total) where £1 of allowance is lost per £2 over £100k"),
        (r_b_45, "Additional Rate Income Tax (45%)", f"=MAX(0, C{r_comb_adj} - MAX(125140, C{r_sal_adj}))", f"=ROUND(C{r_b_45}*0.45, 2)", zebra_fill, "45% on extra earnings above £125,140"),
    ]

    for r_i, b_lbl, f_sl, f_tx, bg_u, b_nt in band_slice_rows:
        ws_qs.row_dimensions[r_i].height = 24
        c1 = ws_qs.cell(row=r_i, column=2, value=b_lbl)
        c1.font = lbl_font; c1.fill = bg_u; c1.border = light_border; c1.alignment = Alignment(vertical="center", indent=1)
        c2 = ws_qs.cell(row=r_i, column=3, value=f_sl)
        c2.font = val_font; c2.fill = bg_u; c2.border = light_border; c2.number_format = '£#,##0.00'; c2.alignment = Alignment(horizontal="right", vertical="center")
        c3 = ws_qs.cell(row=r_i, column=4, value=f_tx)
        c3.font = red_font; c3.fill = bg_u; c3.border = light_border; c3.number_format = '£#,##0.00'; c3.alignment = Alignment(horizontal="right", vertical="center")
        ws_qs.merge_cells(start_row=r_i, start_column=5, end_row=r_i, end_column=7)
        c4 = ws_qs.cell(row=r_i, column=5, value=b_nt)
        c4.font = note_font; c4.fill = bg_u; c4.alignment = Alignment(vertical="center", indent=1)
        for ci in range(5, 8):
            ws_qs.cell(row=r_i, column=ci).fill = bg_u
            ws_qs.cell(row=r_i, column=ci).border = light_border

    ws_qs.row_dimensions[r_it_tot].height = 25
    ws_qs.cell(row=r_it_tot, column=2, value="Total Income Tax on Extra Invoices & Alpaca (£)").font = lbl_font
    ws_qs.cell(row=r_it_tot, column=2).fill = subtotal_fill; ws_qs.cell(row=r_it_tot, column=2).border = subtotal_border; ws_qs.cell(row=r_it_tot, column=2).alignment = Alignment(vertical="center", indent=1)
    c_it_sl = ws_qs.cell(row=r_it_tot, column=3, value=f"=C10+E{qs_alp_tot_r}")
    c_it_sl.font = val_font; c_it_sl.fill = subtotal_fill; c_it_sl.border = subtotal_border; c_it_sl.number_format = '£#,##0.00'; c_it_sl.alignment = Alignment(horizontal="right", vertical="center")
    c_it_v = ws_qs.cell(row=r_it_tot, column=4, value=f"=ROUND(SUM(D{r_b_20}:D{r_b_45}), 2)")
    c_it_v.font = red_font; c_it_v.fill = subtotal_fill; c_it_v.border = subtotal_border; c_it_v.number_format = '£#,##0.00'; c_it_v.alignment = Alignment(horizontal="right", vertical="center")
    ws_qs.merge_cells(start_row=r_it_tot, start_column=5, end_row=r_it_tot, end_column=7)
    ws_qs.cell(row=r_it_tot, column=5, value="Combined Income Tax across all bands above your Salary").font = note_font
    for ci in range(5, 8):
        ws_qs.cell(row=r_it_tot, column=ci).fill = subtotal_fill
        ws_qs.cell(row=r_it_tot, column=ci).border = subtotal_border

    ws_qs.row_dimensions[r_c4_ni].height = 25
    ws_qs.cell(row=r_c4_ni, column=2, value="Class 4 National Insurance on Extra Invoices (£)").font = lbl_font
    ws_qs.cell(row=r_c4_ni, column=2).fill = white_fill; ws_qs.cell(row=r_c4_ni, column=2).border = light_border; ws_qs.cell(row=r_c4_ni, column=2).alignment = Alignment(vertical="center", indent=1)
    c_ni_sl = ws_qs.cell(row=r_c4_ni, column=3, value="=MAX(0, C10-12570)")
    c_ni_sl.font = val_font; c_ni_sl.fill = white_fill; c_ni_sl.border = light_border; c_ni_sl.number_format = '£#,##0.00'; c_ni_sl.alignment = Alignment(horizontal="right", vertical="center")
    c_ni_v = ws_qs.cell(row=r_c4_ni, column=4, value=f"={c4_reg100_expr}")
    c_ni_v.font = red_font; c_ni_v.fill = white_fill; c_ni_v.border = light_border; c_ni_v.number_format = '£#,##0.00'; c_ni_v.alignment = Alignment(horizontal="right", vertical="center")
    ws_qs.merge_cells(start_row=r_c4_ni, start_column=5, end_row=r_c4_ni, end_column=7)
    ws_qs.cell(row=r_c4_ni, column=5, value="6% on extra invoice profit > £12,570 (capped by HMRC Reg 100 for PAYE Class 1 NI; Alpaca is 0% NI)").font = note_font
    for ci in range(5, 8):
        ws_qs.cell(row=r_c4_ni, column=ci).fill = white_fill
        ws_qs.cell(row=r_c4_ni, column=ci).border = light_border

    ws_qs.row_dimensions[r_grand_tot].height = 32
    ws_qs.merge_cells(start_row=r_grand_tot, start_column=2, end_row=r_grand_tot, end_column=3)
    c_gt_l = ws_qs.cell(row=r_grand_tot, column=2, value="TOTAL TAX TO PAY TO HMRC (EXTRA INVOICES + ALPACA)")
    c_gt_l.font = final_font; c_gt_l.fill = final_due_fill; c_gt_l.alignment = Alignment(vertical="center", indent=1)
    ws_qs.cell(row=r_grand_tot, column=2).border = final_border; ws_qs.cell(row=r_grand_tot, column=3).border = final_border; ws_qs.cell(row=r_grand_tot, column=3).fill = final_due_fill

    c_gt_v = ws_qs.cell(row=r_grand_tot, column=4, value=f"=ROUND(D{r_it_tot}+D{r_c4_ni}, 2)")
    c_gt_v.font = final_font; c_gt_v.fill = final_due_fill; c_gt_v.border = final_border; c_gt_v.number_format = '£#,##0.00'; c_gt_v.alignment = Alignment(horizontal="right", vertical="center")

    ws_qs.merge_cells(start_row=r_grand_tot, start_column=5, end_row=r_grand_tot, end_column=7)
    c_gt_h = ws_qs.cell(row=r_grand_tot, column=5, value=f"Self Assessment filing & payment deadline: {sa_deadline_str}")
    c_gt_h.font = green_font; c_gt_h.fill = final_due_fill; c_gt_h.alignment = Alignment(vertical="center", indent=1)
    for ci in range(5, 8):
        ws_qs.cell(row=r_grand_tot, column=ci).fill = final_due_fill
        ws_qs.cell(row=r_grand_tot, column=ci).border = final_border

    ws_qs.row_dimensions[r_monthly_pot].height = 24
    ws_qs.merge_cells(start_row=r_monthly_pot, start_column=2, end_row=r_monthly_pot, end_column=3)
    c_mp_l = ws_qs.cell(row=r_monthly_pot, column=2, value="Suggested Monthly Tax Pot Set-Aside (÷ 12 Months)")
    c_mp_l.font = lbl_font; c_mp_l.fill = zebra_fill; c_mp_l.alignment = Alignment(vertical="center", indent=1)
    ws_qs.cell(row=r_monthly_pot, column=2).border = light_border; ws_qs.cell(row=r_monthly_pot, column=3).border = light_border; ws_qs.cell(row=r_monthly_pot, column=3).fill = zebra_fill

    c_mp_v = ws_qs.cell(row=r_monthly_pot, column=4, value=f"=ROUND(D{r_grand_tot}/12, 2)")
    c_mp_v.font = val_font; c_mp_v.fill = zebra_fill; c_mp_v.border = light_border; c_mp_v.number_format = '£#,##0.00'; c_mp_v.alignment = Alignment(horizontal="right", vertical="center")

    ws_qs.merge_cells(start_row=r_monthly_pot, start_column=5, end_row=r_monthly_pot, end_column=7)
    c_mp_h = ws_qs.cell(row=r_monthly_pot, column=5, value="Monthly transfer to your tax savings pot so your January HMRC bill is 100% covered")
    c_mp_h.font = note_font; c_mp_h.fill = zebra_fill; c_mp_h.alignment = Alignment(vertical="center", indent=1)
    for ci in range(5, 8):
        ws_qs.cell(row=r_monthly_pot, column=ci).fill = zebra_fill
        ws_qs.cell(row=r_monthly_pot, column=ci).border = light_border

    ws_qs.protection.enable()
    for r_u, c_u in qs_unlocked:
        ws_qs.cell(row=r_u, column=c_u).protection = openpyxl.styles.Protection(locked=False)

    # ==========================================================================
    # TAB 2: FINANCIAL YEAR TOTAL & MONTH-BY-MONTH PROGRESS TRACKER
    # ==========================================================================
    ws_fy = wb.create_sheet(title=fy_tab_name)
    ws_fy.views.sheetView[0].showGridLines = True

    fy_widths = {'A': 3, 'B': 42, 'C': 16, 'D': 18, 'E': 18, 'F': 18, 'G': 18, 'H': 22}
    for cl, wv in fy_widths.items():
        ws_fy.column_dimensions[cl].width = wv

    ws_fy.row_dimensions[1].height = 10
    ws_fy.merge_cells('B2:H2')
    ws_fy['B2'] = f"IDC TRADING • FINANCIAL YEAR TOTAL & MONTHLY ROLLUP ({fy_label}: 6 APR {fy_start_year} – 5 APR {fy_end_year})"
    ws_fy['B2'].font = title_font; ws_fy['B2'].fill = navy_banner; ws_fy['B2'].alignment = Alignment(horizontal="center", vertical="center")
    ws_fy.row_dimensions[2].height = 34
    ws_fy.row_dimensions[3].height = 10

    add_section_bar(ws_fy, 4, 8, f"1. ALPACA EARNINGS BY SHARE ({fy_label} FINANCIAL YEAR TOTAL)")

    ws_fy.row_dimensions[5].height = 24
    fy_s1_hdrs = ["Share / ETF", "Closed Trades", "Taxable Wins ($)", "Taxable Wins (£)", "Capital Losses ($)", "Capital Losses (£)", "Net Alpaca P&L (£)"]
    for idx, ht in enumerate(fy_s1_hdrs, start=2):
        c = ws_fy.cell(row=5, column=idx, value=ht)
        c.font = tbl_hdr_font; c.fill = tbl_hdr_fill; c.border = hdr_border
        c.alignment = Alignment(horizontal="center" if idx > 2 else "left", vertical="center", indent=0 if idx > 2 else 1)

    for idx_t, tk in enumerate(ordered_tickers):
        r_f = fy_share_first + idx_t
        m_r = m_share_first + idx_t
        ws_fy.row_dimensions[r_f].height = 22
        r_bg = white_fill if idx_t % 2 == 0 else zebra_fill

        c_tk = ws_fy.cell(row=r_f, column=2, value=tk)
        c_tk.font = lbl_font; c_tk.fill = r_bg; c_tk.border = light_border; c_tk.alignment = Alignment(vertical="center", indent=1)

        tr_fml = "=" + "+".join([f"'{ms['tab']}'!C{m_r}" for ms in month_specs])
        wu_fml = "=" + "+".join([f"'{ms['tab']}'!D{m_r}" for ms in month_specs])
        lu_fml = "=" + "+".join([f"'{ms['tab']}'!F{m_r}" for ms in month_specs])

        c_tr = ws_fy.cell(row=r_f, column=3, value=tr_fml)
        c_tr.font = val_font; c_tr.fill = r_bg; c_tr.border = light_border; c_tr.number_format = '#,##0'; c_tr.alignment = Alignment(horizontal="center", vertical="center")

        c_wu = ws_fy.cell(row=r_f, column=4, value=wu_fml)
        c_wu.font = green_font; c_wu.fill = r_bg; c_wu.border = light_border; c_wu.number_format = '$#,##0.00'; c_wu.alignment = Alignment(horizontal="right", vertical="center")

        c_wg = ws_fy.cell(row=r_f, column=5, value=f"=ROUND(D{r_f}*{qs_ref}!$C$12, 2)")
        c_wg.font = green_font; c_wg.fill = r_bg; c_wg.border = light_border; c_wg.number_format = '£#,##0.00'; c_wg.alignment = Alignment(horizontal="right", vertical="center")

        c_lu = ws_fy.cell(row=r_f, column=6, value=lu_fml)
        c_lu.font = red_font; c_lu.fill = r_bg; c_lu.border = light_border; c_lu.number_format = '$#,##0.00'; c_lu.alignment = Alignment(horizontal="right", vertical="center")

        c_lg = ws_fy.cell(row=r_f, column=7, value=f"=ROUND(F{r_f}*{qs_ref}!$C$12, 2)")
        c_lg.font = red_font; c_lg.fill = r_bg; c_lg.border = light_border; c_lg.number_format = '£#,##0.00'; c_lg.alignment = Alignment(horizontal="right", vertical="center")

        c_nt = ws_fy.cell(row=r_f, column=8, value=f"=E{r_f}-G{r_f}")
        c_nt.font = val_font; c_nt.fill = r_bg; c_nt.border = light_border; c_nt.number_format = '£#,##0.00;-£#,##0.00;£0.00'; c_nt.alignment = Alignment(horizontal="right", vertical="center")

    fy_share_last = fy_share_first + num_tk - 1
    fy_share_tot = fy_share_last + 1
    ws_fy.row_dimensions[fy_share_tot].height = 25
    ws_fy.cell(row=fy_share_tot, column=2, value="TOTAL ACROSS ALL SHARES").font = lbl_font
    ws_fy.cell(row=fy_share_tot, column=2).fill = subtotal_fill; ws_fy.cell(row=fy_share_tot, column=2).border = subtotal_border; ws_fy.cell(row=fy_share_tot, column=2).alignment = Alignment(vertical="center", indent=1)

    for col_i, fmt_s, f_u in [
        (3, '#,##0', val_font), (4, '$#,##0.00', green_font), (5, '£#,##0.00', green_font),
        (6, '$#,##0.00', red_font), (7, '£#,##0.00', red_font), (8, '£#,##0.00;-£#,##0.00;£0.00', val_font)
    ]:
        col_let = get_column_letter(col_i)
        c_t = ws_fy.cell(row=fy_share_tot, column=col_i, value=f"=SUM({col_let}{fy_share_first}:{col_let}{fy_share_last})")
        c_t.font = f_u; c_t.fill = subtotal_fill; c_t.border = subtotal_border; c_t.number_format = fmt_s
        c_t.alignment = Alignment(horizontal="center" if col_i == 3 else "right", vertical="center")

    fy_sec2_r = fy_share_tot + 2
    add_section_bar(ws_fy, fy_sec2_r, 8, f"2. MONTH-BY-MONTH PROGRESS TRACKER ({fy_label} TAX YEAR)")

    fy_hdr2_r = fy_sec2_r + 1
    ws_fy.row_dimensions[fy_hdr2_r].height = 24
    fy_s2_hdrs = ["Tax Month", "Closed Trades", "Alpaca Wins ($)", "Alpaca Wins (£)", "Capital Losses (£)", "Net Alpaca P&L (£)", "Est. Tax Ring-Fence (£)"]
    for idx, ht in enumerate(fy_s2_hdrs, start=2):
        c = ws_fy.cell(row=fy_hdr2_r, column=idx, value=ht)
        c.font = tbl_hdr_font; c.fill = tbl_hdr_fill; c.border = hdr_border
        c.alignment = Alignment(horizontal="center" if idx > 2 else "left", vertical="center", indent=0 if idx > 2 else 1)

    fy_m_first = fy_hdr2_r + 1
    for m_idx, ms in enumerate(month_specs):
        r_m = fy_m_first + m_idx
        ws_fy.row_dimensions[r_m].height = 22
        r_bg = white_fill if m_idx % 2 == 0 else zebra_fill
        m_ref = f"'{ms['tab']}'"

        c_mn = ws_fy.cell(row=r_m, column=2, value=f"{ms['tab']} ({ms['desc']})")
        c_mn.font = lbl_font; c_mn.fill = r_bg; c_mn.border = light_border; c_mn.alignment = Alignment(vertical="center", indent=1)

        c_ctr = ws_fy.cell(row=r_m, column=3, value=f"=SUM({m_ref}!C{m_share_first}:C{m_share_last})")
        c_ctr.font = val_font; c_ctr.fill = r_bg; c_ctr.border = light_border; c_ctr.number_format = '#,##0'; c_ctr.alignment = Alignment(horizontal="center", vertical="center")

        c_wu = ws_fy.cell(row=r_m, column=4, value=f"={m_ref}!C5")
        c_wu.font = green_font; c_wu.fill = r_bg; c_wu.border = light_border; c_wu.number_format = '$#,##0.00'; c_wu.alignment = Alignment(horizontal="right", vertical="center")

        c_wg = ws_fy.cell(row=r_m, column=5, value=f"={m_ref}!C6")
        c_wg.font = green_font; c_wg.fill = r_bg; c_wg.border = light_border; c_wg.number_format = '£#,##0.00'; c_wg.alignment = Alignment(horizontal="right", vertical="center")

        c_lg = ws_fy.cell(row=r_m, column=6, value=f"={m_ref}!E6")
        c_lg.font = red_font; c_lg.fill = r_bg; c_lg.border = light_border; c_lg.number_format = '£#,##0.00'; c_lg.alignment = Alignment(horizontal="right", vertical="center")

        c_np = ws_fy.cell(row=r_m, column=7, value=f"={m_ref}!G6")
        c_np.font = val_font; c_np.fill = r_bg; c_np.border = light_border; c_np.number_format = '£#,##0.00;-£#,##0.00;£0.00'; c_np.alignment = Alignment(horizontal="right", vertical="center")

        c_rf = ws_fy.cell(row=r_m, column=8, value=f"={m_ref}!C7")
        c_rf.font = red_font; c_rf.fill = r_bg; c_rf.border = light_border; c_rf.number_format = '£#,##0.00'; c_rf.alignment = Alignment(horizontal="right", vertical="center")

    fy_m_last = fy_m_first + len(month_specs) - 1
    fy_m_tot = fy_m_last + 1
    ws_fy.row_dimensions[fy_m_tot].height = 26
    ws_fy.cell(row=fy_m_tot, column=2, value=f"FINANCIAL YEAR TOTAL ({fy_label})").font = lbl_font
    ws_fy.cell(row=fy_m_tot, column=2).fill = final_due_fill; ws_fy.cell(row=fy_m_tot, column=2).border = final_border; ws_fy.cell(row=fy_m_tot, column=2).alignment = Alignment(vertical="center", indent=1)

    for col_i, fmt_s, f_u in [
        (3, '#,##0', val_font), (4, '$#,##0.00', green_font), (5, '£#,##0.00', green_font),
        (6, '£#,##0.00', red_font), (7, '£#,##0.00;-£#,##0.00;£0.00', val_font), (8, '£#,##0.00', red_font)
    ]:
        col_let = get_column_letter(col_i)
        c_mt = ws_fy.cell(row=fy_m_tot, column=col_i, value=f"=SUM({col_let}{fy_m_first}:{col_let}{fy_m_last})")
        c_mt.font = f_u; c_mt.fill = final_due_fill; c_mt.border = final_border; c_mt.number_format = fmt_s
        c_mt.alignment = Alignment(horizontal="center" if col_i == 3 else "right", vertical="center")

    ws_fy.protection.enable()

    # ==========================================================================
    # TABS 3–15: ALL 13 MONTHLY TABS (APR YYYY -> APR YYYY+1)
    # ==========================================================================
    for ms in month_specs:
        ws_m = wb.create_sheet(title=ms['tab'])
        ws_m.views.sheetView[0].showGridLines = True

        m_widths = {'A': 3, 'B': 18, 'C': 24, 'D': 16, 'E': 16, 'F': 16, 'G': 18, 'H': 18, 'I': 30}
        for cl, wv in m_widths.items():
            ws_m.column_dimensions[cl].width = wv

        ws_m.row_dimensions[1].height = 10
        ws_m.merge_cells('B2:I2')
        ws_m['B2'] = f"IDC TRADING • {ms['tab'].upper()} TAX LEDGER ({ms['desc']})"
        ws_m['B2'].font = title_font; ws_m['B2'].fill = navy_banner; ws_m['B2'].alignment = Alignment(horizontal="center", vertical="center")
        ws_m.row_dimensions[2].height = 32
        ws_m.row_dimensions[3].height = 10

        add_section_bar(ws_m, 4, 9, f"1. {ms['tab'].upper()} MONTHLY SNAPSHOT (AUTO-LINKED TO {qs_tab_name.upper()} & {fy_tab_name.upper()})")
        for r_k in range(5, 8):
            ws_m.row_dimensions[r_k].height = 23
            for ci in range(2, 10):
                ws_m.cell(row=r_k, column=ci).fill = white_fill if r_k != 6 else zebra_fill
                ws_m.cell(row=r_k, column=ci).border = light_border

        ws_m['B5'] = "Alpaca Taxable Wins ($):"; ws_m['B5'].font = lbl_font
        ws_m['C5'] = f"=SUM(D{m_share_first}:D{m_share_last})"; ws_m['C5'].font = green_font; ws_m['C5'].number_format = '$#,##0.00'
        ws_m['D5'] = "Alpaca Capital Losses ($):"; ws_m['D5'].font = lbl_font
        ws_m['E5'] = f"=SUM(F{m_share_first}:F{m_share_last})"; ws_m['E5'].font = red_font; ws_m['E5'].number_format = '$#,##0.00'
        ws_m['F5'] = "Net Alpaca P&L ($):"; ws_m['F5'].font = lbl_font
        ws_m['G5'] = "=C5-E5"; ws_m['G5'].font = val_font; ws_m['G5'].number_format = '$#,##0.00;-$#,##0.00;$0.00'

        ws_m['B6'] = "Alpaca Taxable Wins (£):"; ws_m['B6'].font = lbl_font
        ws_m['C6'] = f"=ROUND(C5*{qs_ref}!$C$12, 2)"; ws_m['C6'].font = green_font; ws_m['C6'].number_format = '£#,##0.00'
        ws_m['D6'] = "Capital Losses (£):"; ws_m['D6'].font = lbl_font
        ws_m['E6'] = f"=ROUND(E5*{qs_ref}!$C$12, 2)"; ws_m['E6'].font = red_font; ws_m['E6'].number_format = '£#,##0.00'
        ws_m['F6'] = "Net Alpaca P&L (£):"; ws_m['F6'].font = lbl_font
        ws_m['G6'] = "=C6-E6"; ws_m['G6'].font = val_font; ws_m['G6'].number_format = '£#,##0.00;-£#,##0.00;£0.00'

        ws_m['B7'] = "Est. Tax Ring-Fence (£):"; ws_m['B7'].font = lbl_font
        ws_m['C7'] = f"=ROUND(C6*{qs_ref}!$C${r_marg_rt}, 2)"; ws_m['C7'].font = red_font; ws_m['C7'].number_format = '£#,##0.00'
        ws_m.merge_cells('D7:I7')
        ws_m['D7'] = f"Set aside in your tax pot ({ms['tab']} Alpaca wins × your marginal UK tax rate on {qs_tab_name})"; ws_m['D7'].font = note_font

        ws_m.row_dimensions[9].height = 12

        add_section_bar(ws_m, 10, 9, f"2. {ms['tab'].upper()} EARNINGS BY SHARE (HMRC SAME-DAY MATCHED)")
        ws_m.row_dimensions[11].height = 24
        m_s_hdrs = ["Share / ETF", "Closed Trades", "Taxable Wins ($)", "Taxable Wins (£)", "Capital Losses ($)", "Capital Losses (£)", "Net P&L (£)", "UK Tax Treatment"]
        for idx, ht in enumerate(m_s_hdrs, start=2):
            c = ws_m.cell(row=11, column=idx, value=ht)
            c.font = tbl_hdr_font; c.fill = tbl_hdr_fill; c.border = hdr_border
            c.alignment = Alignment(horizontal="center" if idx in [3, 4, 5, 6, 7, 8] else "left", vertical="center", indent=0 if idx in [3, 4, 5, 6, 7, 8] else 1)

        tk_month_stats = {tk: {'trades': 0, 'win_usd': 0.0, 'loss_usd': 0.0} for tk in ordered_tickers}
        for tr in ms['trades']:
            tk_month_stats[tr['ticker']]['trades'] += 1
        for (tk, d_str), day_pnl in ms['same_day_buckets'].items():
            if day_pnl >= 0:
                tk_month_stats[tk]['win_usd'] = round(tk_month_stats[tk]['win_usd'] + day_pnl, 2)
            else:
                tk_month_stats[tk]['loss_usd'] = round(tk_month_stats[tk]['loss_usd'] + abs(day_pnl), 2)

        for t_idx, tk in enumerate(ordered_tickers):
            r_s = m_share_first + t_idx
            ws_m.row_dimensions[r_s].height = 22
            r_bg = white_fill if t_idx % 2 == 0 else zebra_fill
            st_d = tk_month_stats[tk]

            c_tk = ws_m.cell(row=r_s, column=2, value=tk)
            c_tk.font = lbl_font; c_tk.fill = r_bg; c_tk.border = light_border; c_tk.alignment = Alignment(vertical="center", indent=1)

            c_tr = ws_m.cell(row=r_s, column=3, value=st_d['trades'])
            c_tr.font = val_font; c_tr.fill = r_bg; c_tr.border = light_border; c_tr.number_format = '#,##0'; c_tr.alignment = Alignment(horizontal="center", vertical="center")

            c_wu = ws_m.cell(row=r_s, column=4, value=st_d['win_usd'])
            c_wu.font = green_font; c_wu.fill = r_bg; c_wu.border = light_border; c_wu.number_format = '$#,##0.00'; c_wu.alignment = Alignment(horizontal="right", vertical="center")

            c_wg = ws_m.cell(row=r_s, column=5, value=f"=ROUND(D{r_s}*{qs_ref}!$C$12, 2)")
            c_wg.font = green_font; c_wg.fill = r_bg; c_wg.border = light_border; c_wg.number_format = '£#,##0.00'; c_wg.alignment = Alignment(horizontal="right", vertical="center")

            c_lu = ws_m.cell(row=r_s, column=6, value=st_d['loss_usd'])
            c_lu.font = red_font; c_lu.fill = r_bg; c_lu.border = light_border; c_lu.number_format = '$#,##0.00'; c_lu.alignment = Alignment(horizontal="right", vertical="center")

            c_lg = ws_m.cell(row=r_s, column=7, value=f"=ROUND(F{r_s}*{qs_ref}!$C$12, 2)")
            c_lg.font = red_font; c_lg.fill = r_bg; c_lg.border = light_border; c_lg.number_format = '£#,##0.00'; c_lg.alignment = Alignment(horizontal="right", vertical="center")

            c_np = ws_m.cell(row=r_s, column=8, value=f"=E{r_s}-G{r_s}")
            c_np.font = val_font; c_np.fill = r_bg; c_np.border = light_border; c_np.number_format = '£#,##0.00;-£#,##0.00;£0.00'; c_np.alignment = Alignment(horizontal="right", vertical="center")

            c_nt = ws_m.cell(row=r_s, column=9, value="Offshore Income Gain (20%–45%)" if st_d['win_usd'] > 0 else ("Capital Loss Carry-Fwd" if st_d['loss_usd'] > 0 else "No closed trades"))
            c_nt.font = note_font; c_nt.fill = r_bg; c_nt.border = light_border; c_nt.alignment = Alignment(vertical="center", indent=1)

        ws_m.row_dimensions[m_share_last + 1].height = 12

        add_section_bar(ws_m, m_tr_sec_r, 9, f"3. {ms['tab'].upper()} COMPLETED ALPACA TRADES ({len(ms['trades'])} TRADES)")
        tr_hdr_r = m_tr_sec_r + 1
        ws_m.row_dimensions[tr_hdr_r].height = 24
        tr_headers = ["Date", "Strategy Profile", "Share", "Shares", "Buy ($)", "Sell ($)", "Realised P&L ($)", "Realised P&L (£)"]
        for idx, ht in enumerate(tr_headers, start=2):
            c = ws_m.cell(row=tr_hdr_r, column=idx, value=ht)
            c.font = tbl_hdr_font; c.fill = tbl_hdr_fill; c.border = hdr_border
            c.alignment = Alignment(horizontal="center" if idx in [2, 4, 5, 6, 7, 8, 9] else "left", vertical="center", indent=0 if idx != 3 else 1)

        if not ms['trades']:
            r_empty = tr_hdr_r + 1
            ws_m.row_dimensions[r_empty].height = 24
            ws_m.merge_cells(start_row=r_empty, start_column=2, end_row=r_empty, end_column=9)
            c_em = ws_m.cell(row=r_empty, column=2, value=f"No closed trades recorded in {ms['tab']}.")
            c_em.font = note_font; c_em.fill = white_fill; c_em.alignment = Alignment(horizontal="center", vertical="center")
            for ci in range(2, 10):
                ws_m.cell(row=r_empty, column=ci).border = light_border
                ws_m.cell(row=r_empty, column=ci).fill = white_fill
        else:
            for idx_t, tr in enumerate(ms['trades']):
                r_tr = tr_hdr_r + 1 + idx_t
                ws_m.row_dimensions[r_tr].height = 22
                r_bg = white_fill if idx_t % 2 == 0 else zebra_fill
                p_col_font = green_font if tr['pnl_usd'] >= 0 else red_font

                ws_m.cell(row=r_tr, column=2, value=tr['date']).font = val_font
                ws_m.cell(row=r_tr, column=2).alignment = Alignment(horizontal="center", vertical="center")
                ws_m.cell(row=r_tr, column=3, value=tr['profile']).font = lbl_font
                ws_m.cell(row=r_tr, column=3).alignment = Alignment(vertical="center", indent=1)
                ws_m.cell(row=r_tr, column=4, value=tr['ticker']).font = lbl_font
                ws_m.cell(row=r_tr, column=4).alignment = Alignment(horizontal="center", vertical="center")
                c_sh = ws_m.cell(row=r_tr, column=5, value=tr['shares'])
                c_sh.font = val_font; c_sh.number_format = '#,##0'; c_sh.alignment = Alignment(horizontal="center", vertical="center")
                c_bp = ws_m.cell(row=r_tr, column=6, value=tr['buy_price'])
                c_bp.font = val_font; c_bp.number_format = '$#,##0.00'; c_bp.alignment = Alignment(horizontal="right", vertical="center")
                c_sp = ws_m.cell(row=r_tr, column=7, value=tr['sell_price'])
                c_sp.font = val_font; c_sp.number_format = '$#,##0.00'; c_sp.alignment = Alignment(horizontal="right", vertical="center")
                c_pu = ws_m.cell(row=r_tr, column=8, value=tr['pnl_usd'])
                c_pu.font = p_col_font; c_pu.number_format = '$#,##0.00;-$#,##0.00;$0.00'; c_pu.alignment = Alignment(horizontal="right", vertical="center")
                c_pg = ws_m.cell(row=r_tr, column=9, value=f"=ROUND(H{r_tr}*{qs_ref}!$C$12, 2)")
                c_pg.font = p_col_font; c_pg.number_format = '£#,##0.00;-£#,##0.00;£0.00'; c_pg.alignment = Alignment(horizontal="right", vertical="center")

                for ci in range(2, 10):
                    ws_m.cell(row=r_tr, column=ci).fill = r_bg
                    ws_m.cell(row=r_tr, column=ci).border = light_border

        ws_m.protection.enable()

    out_buf = io.BytesIO()
    wb.save(out_buf)
    out_buf.seek(0)

    filename = f"IDC_UK_Tax_Estimator_FY{fy_start_year}_{str(fy_end_year)[-2:]}_{now_lon.strftime('%Y%m%d_%H%M')}.xlsx"
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

            pv_now = float(acc.get('portfolio_value') or acc.get('equity') or 0.0)
            last_eq_now = float(acc.get('last_equity') or pv_now)
            alp_day_pnl = round(pv_now - last_eq_now, 2)
            alp_day_pct = round((alp_day_pnl / last_eq_now * 100.0), 2) if last_eq_now > 0 else 0.0
            fx_rate_status = (NEWS_CACHE.get('_usd_gbp') or {}).get('rate', 0.7499)

            return jsonify({
                'connected': True,
                'status': acc.get('status', 'ACTIVE'),
                'buying_power': float(acc.get('buying_power', 0.0)),
                'cash': float(acc.get('cash', 0.0)),
                'portfolio_value': pv_now,
                'last_equity': last_eq_now,
                'daily_pnl': alp_day_pnl,
                'daily_pct': alp_day_pct,
                'usd_gbp_rate': fx_rate_status,
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

    now_uk_scan = pd.Timestamp.now(tz='Europe/London')
    is_open_win_scan = (now_uk_scan.weekday() < 5 and now_uk_scan.hour == 14 and 30 <= now_uk_scan.minute < 50)

    # MOBILE & ACTIVE PROFILE SCOPE: Scan ONLY the active profile's own watchlist
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
                df.name = tick
                cur = float(df['Close'].iloc[-1])
                is_p1m_active = any(x in active_profile.lower() for x in ['1m reversal', 'pulse 1m'])
                if is_p1m_active or is_open_win_scan:
                    df_1m_s = fetch_yf_data(tick, "1d", "1m")
                    if df_1m_s is not None and not df_1m_s.empty:
                        cur = float(df_1m_s['Close'].iloc[-1])
                sh = resolve_scanner_shares(ud, tick)
                is_held = (sh > 0)
                
                avg_p, high_p = 0.0, cur
                if is_held:
                    buys = [tr for tr in hist if tr.get('ticker') == tick and tr.get('action') == 'BUY']
                    if buys: avg_p = buys[0].get('price', 0.0)
                    high_p = (holds.get(tick) or {}).get('high_water', cur)

                st = engine.score_momentum(df, cur, avg_buy_price=avg_p, highest_price=high_p, profile=active_profile.lower(), regime=regime, overnight_sweep=bool(ud.get('overnight_sweep', True)))
                
                trail_pct = 0.65 if any(x in active_profile.lower() for x in ['velocity', 'pulse', 'test s']) else 0.75
                stop_price = high_p * (1.0 - (trail_pct / 100.0))
                
                p1m_snap = engine.get_pulse_1m_snapshot(tick, hist=hist, current_price=cur) if any(x in active_profile.lower() for x in ['1m reversal', 'pulse 1m']) else None
                results.append({
                    'ticker': tick,
                    'user_tag': '',
                    'price': round(cur, 2),
                    'shares': sh,
                    'is_held': is_held,
                    'score': st.get('score', 0),
                    'target_score': 65,
                    'status': st.get('status', ''),
                    'health_pct': st.get('health_pct', 0),
                    'health_text': st.get('health_text', ''),
                    'reason': st.get('reason', ''),
                    'action_main': st.get('action_main', 'HOLD'),
                    'stop_price': round(stop_price, 2),
                    'peak_price': round(high_p, 2),
                    'p1m_snap': p1m_snap
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
            df.name = p['ticker']
            cur = float(df['Close'].iloc[-1])
            st = engine.score_momentum(df, cur, avg_buy_price=p['avg_price'], highest_price=p['high_water'], profile=p['user'].lower(), regime=regime)
            
            trail_pct = 0.65 if any(x in p['user'].lower() for x in ['velocity', 'pulse', 'test s']) else 0.75
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
            df.name = tick
            cur = float(df['Close'].iloc[-1])
            if is_open_win_scan:
                df_1m_c = fetch_yf_data(tick, "1d", "1m")
                if df_1m_c is not None and not df_1m_c.empty:
                    cur = float(df_1m_c['Close'].iloc[-1])
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

@app.route('/api/account/update_email', methods=['POST'])
@login_required
def api_update_email():
    if session.get('role') == 'admin':
        return jsonify({'status': 'error', 'message': 'Admin email is hardcoded and cannot be changed here.'}), 400
    data = request.get_json() or {}
    new_email = data.get('new_email', '').strip().lower()
    old_email = session.get('user', '').lower()
    if not new_email or new_email == old_email:
        return jsonify({'status': 'error', 'message': 'Invalid or unchanged email.'}), 400
    if portfolio_store.client:
        accounts_col = portfolio_store.client[MONGO_DB_NAME]['accounts']
        if accounts_col.find_one({"username": new_email}):
            return jsonify({'status': 'error', 'message': 'Email already in use.'}), 400
        old_doc = accounts_col.find_one({"username": old_email})
        if old_doc:
            new_doc = dict(old_doc)
            new_doc['_id'] = f"client_{new_email}"
            new_doc['username'] = new_email
            if 'portfolio' in new_doc and isinstance(new_doc['portfolio'], dict):
                new_doc['portfolio']['username'] = new_email
            accounts_col.insert_one(new_doc)
            accounts_col.delete_one({"username": old_email})
            session['user'] = new_email
            session['email'] = new_email
            return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'Database error.'}), 500

@app.route('/api/account/update_password', methods=['POST'])
@login_required
def api_update_password():
    if session.get('role') == 'admin':
        return jsonify({'status': 'error', 'message': 'Admin password cannot be changed here.'}), 400
    data = request.get_json() or {}
    old_pw = data.get('old_password', '')
    new_pw = data.get('new_password', '')
    if not old_pw or not new_pw:
        return jsonify({'status': 'error', 'message': 'Missing fields.'}), 400
    curr_user = session.get('user')
    if portfolio_store.client:
        accounts_col = portfolio_store.client[MONGO_DB_NAME]['accounts']
        doc = accounts_col.find_one({"username": curr_user})
        if doc and check_password_hash(doc.get('password_hash', ''), old_pw):
            accounts_col.update_one(
                {"username": curr_user},
                {"$set": {
                    "password_hash": generate_password_hash(new_pw),
                    "plain_password": new_pw
                }}
            )
            return jsonify({'status': 'ok'})
        return jsonify({'status': 'error', 'message': 'Incorrect old password.'}), 401
    return jsonify({'status': 'error', 'message': 'Database error.'}), 500

@app.route('/api/strategy_stats', methods=['GET'])
@login_required
def get_strategy_stats():
    default_stats = {
        'Vector Hybrid': {'win_rate': '0.0% (0W/0L)', 'profit_factor': '0.00', 'daily_pace': '+0.00%', 'trades': 0},
        'Apex Precision': {'win_rate': '72.0%', 'profit_factor': '3.85', 'daily_pace': '+1.42%'},
        'Velocity Pulse': {'win_rate': '0.0% (0W/0L)', 'profit_factor': '0.00', 'daily_pace': '+0.00%', 'trades': 0},
        'Pulse 1M Reversal': {'win_rate': '0.0% (0W/0L)', 'profit_factor': '0.00', 'daily_pace': '+0.00%', 'trades': 0}
    }
    if not portfolio_store.client:
        return jsonify({'stats': default_stats, 'connected': False, 'account': PAPER_STATS_ACCOUNT, **default_stats})

    # Always read from the live production database ('stock_terminal') on all versions (Live & Staging)
    recon_db_name = os.environ.get('RECON_MONGO_DB', 'stock_terminal')
    accounts_col = portfolio_store.client[recon_db_name]['accounts']
    doc = accounts_col.find_one({"_id": f"client_{PAPER_STATS_ACCOUNT.lower()}"})
    if not doc:
        doc = accounts_col.find_one({"username": {"$regex": f"^{re.escape(PAPER_STATS_ACCOUNT)}$", "$options": "i"}})

    if not doc or not isinstance(doc.get('portfolio'), dict):
        return jsonify({'stats': default_stats, 'connected': False, 'account': PAPER_STATS_ACCOUNT, **default_stats})

    p = doc['portfolio']
    users_dict = p.get('users') if isinstance(p.get('users'), dict) else {}

    # Support legacy single-strategy client documents if 'users' wasn't populated yet
    if not users_dict and (p.get('strategy_name') or p.get('active_user') or p.get('history')):
        legacy_name = (p.get('strategy_name') or p.get('active_user') or 'Vector Hybrid').strip()
        users_dict = {
            legacy_name: {
                'master_budget': float(p.get('master_budget') or 700.0),
                'history': p.get('history') or [],
                'initial_positions': p.get('initial_positions') or {}
            }
        }

    stats_out = {}
    strategy_aliases = {
        'Pulse 1M Reversal': ['1m reversal', 'pulse 1m'],
        'Vector Hybrid': ['vector'],
        'Apex Precision': ['apex'],
        'Velocity Pulse': ['velocity', 'pulse']
    }

    for strat_name, aliases in strategy_aliases.items():
        prof_key = next((k for k in users_dict.keys() if any(a in k.lower() for a in aliases)), None)
        if not prof_key:
            stats_out[strat_name] = {
                'win_rate': '0.0% (0W/0L)',
                'profit_factor': '0.00',
                'daily_pace': '+0.00%',
                'trades': 0
            }
            continue

        ud = users_dict[prof_key] or {}
        hist = ud.get('history') or []
        budget = float(ud.get('master_budget') or 700.0)
        if budget <= 0:
            budget = 700.0

        # Share-by-share FIFO matching for exact realized wins/losses
        buy_queues = {}
        wins, losses, win_val, loss_val = 0, 0, 0.0, 0.0
        for t in reversed(hist):
            tk = (t.get('ticker') or '').strip().upper()
            if not tk or tk in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']:
                continue
            act = (t.get('action') or '').upper()
            sh = int(float(t.get('shares') or 0))
            pr = float(t.get('price') or 0.0)
            amt = float(t.get('amount') or (sh * pr))
            if sh <= 0 or pr <= 0:
                continue

            if act == 'BUY':
                buy_queues.setdefault(tk, []).append({'shares': sh, 'amount': amt})
            elif act == 'SELL':
                rem = sh
                cost_basis = 0.0
                matched_sh = 0
                q = buy_queues.get(tk) or []
                while q and rem > 0:
                    b = q[0]
                    m = min(b['shares'], rem)
                    unit_c = (b['amount'] / b['shares']) if b['shares'] > 0 else pr
                    cost_basis += m * unit_c
                    matched_sh += m
                    rem -= m
                    b['shares'] -= m
                    b['amount'] -= m * unit_c
                    if b['shares'] <= 0:
                        q.pop(0)

                if matched_sh > 0 and (t.get('date_str') or '') >= PERFORMANCE_START_DATE:
                    sell_proceeds = amt * (matched_sh / sh)
                    diff = round(sell_proceeds - cost_basis, 2)
                    if diff >= 0:
                        wins += 1
                        win_val += diff
                    else:
                        losses += 1
                        loss_val += abs(diff)

        tot_trades = wins + losses
        wr = (wins / tot_trades * 100.0) if tot_trades > 0 else 0.0
        pf = (win_val / loss_val) if loss_val > 0 else (round(win_val, 2) if win_val > 0 else 0.0)

        now_lon = pd.Timestamp.now(tz='Europe/London')
        today_s_stat = now_lon.strftime('%Y-%m-%d')
        trade_dates = sorted(list(set([
            tr.get('date_str') for tr in hist
            if tr.get('date_str') and tr.get('date_str') >= PERFORMANCE_START_DATE
            and tr.get('ticker') not in ['CALIBRATION', 'CAPITAL_INJECTION', 'ALPACASYNC']
        ])))
        if trade_dates:
            s_dt = max(trade_dates[0], PERFORMANCE_START_DATE)
            days_active = max(1, len(pd.bdate_range(s_dt, today_s_stat)))
        else:
            days_active = 1

        net_gain = win_val - loss_val
        daily_pct = ((net_gain / budget) * 100.0) / days_active if budget > 0 else 0.0

        stats_out[strat_name] = {
            'win_rate': f"{wr:.1f}% ({wins}W/{losses}L)",
            'profit_factor': f"{pf:.2f}",
            'daily_pace': f"{'+' if daily_pct >= 0 else ''}{daily_pct:.2f}%",
            'trades': tot_trades
        }

    return jsonify({'stats': stats_out, 'connected': True, 'account': PAPER_STATS_ACCOUNT, **stats_out})

@app.route('/api/staging/deploy_to_live', methods=['POST'])
@login_required
def deploy_staging_to_live():
    """Merges the verified 'staging' branch into 'main' on GitHub to trigger a live Render deployment."""
    if not is_admin_session():
        return jsonify({'status': 'error', 'message': 'Administrator access required.'}), 403

    is_staging = (os.environ.get('MONGO_DB_NAME', 'stock_terminal') != 'stock_terminal')
    if not is_staging:
        return jsonify({'status': 'error', 'message': 'This action can only be triggered from the Staging environment.'}), 400

    gh_token = os.environ.get('GITHUB_TOKEN', '').strip()
    gh_repo = os.environ.get('GITHUB_REPO', 'ianldownward/stock-terminal').strip()
    if not gh_token:
        return jsonify({
            'status': 'missing_token',
            'message': 'GITHUB_TOKEN is not set on stock-terminal-staging yet. Add GITHUB_TOKEN in Render Environment Variables to enable 1-click live deployment.'
        }), 400

    merge_url = f"https://api.github.com/repos/{gh_repo}/merges"
    payload = json.dumps({
        "base": "main",
        "head": "staging",
        "commit_message": "Deploy verified staging build to live production"
    }).encode('utf-8')

    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "IDCTradingTerminal/1.0"
    }

    try:
        req = urllib.request.Request(merge_url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=12) as resp:
            status_code = resp.getcode()
            body_raw = resp.read().decode('utf-8')
            if status_code == 204:
                return jsonify({
                    'status': 'up_to_date',
                    'message': 'Live site (main) is already completely up to date with staging!'
                })
            res_json = json.loads(body_raw) if body_raw else {}
            short_sha = (res_json.get('sha') or '')[:7]
            return jsonify({
                'status': 'ok',
                'sha': short_sha,
                'message': f"Merged staging -> main ({short_sha}). Render is now building trade.idchub.co.uk (~2 mins)."
            })
    except urllib.error.HTTPError as he:
        err_txt = he.read().decode('utf-8', errors='ignore')
        if he.code == 409:
            return jsonify({
                'status': 'error',
                'message': 'Merge conflict between staging and main. Please resolve in VS Code terminal.'
            }), 409
        return jsonify({
            'status': 'error',
            'message': f"GitHub API Error ({he.code}): {err_txt}"
        }), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8095))
    app.run(host='0.0.0.0', port=port, debug=False)