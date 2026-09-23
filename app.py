import os, json, time, urllib.request, threading
import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, request, render_template
from pymongo import MongoClient
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
YF_CACHE = {}

def fetch_yf_data(ticker, period="1y", interval="1d"):
    if not interval or interval == 'undefined': interval = "1d"
    if not period or period == 'undefined': period = "1y"
    
    if period in ['1y', '5y', 'max'] and interval in ['1m', '2m', '5m', '15m', '30m', '60m', '1h']: interval = '1d'
    if period in ['1mo', '3mo', '6mo'] and interval in ['1m', '2m']: interval = '5m'

    cache_key = f"{ticker}_{period}_{interval}"
    now = time.time()
    
    if cache_key in YF_CACHE:
        cached_time, df = YF_CACHE[cache_key]
        if not df.empty and (now - cached_time < 30): return df.copy()
            
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval)
        if not df.empty: YF_CACHE[cache_key] = (now, df)
        return df.copy()
    except: return pd.DataFrame()

def send_push_notification(topic, title, message):
    if not topic: return
    try:
        url = f"https://ntfy.sh/{topic.strip()}"
        req = urllib.request.Request(url, data=message.encode('utf-8'), headers={'Title': title})
        urllib.request.urlopen(req, timeout=5)
    except: pass

class PortfolioManager:
    def __init__(self):
        self.mongo_uri = os.environ.get('MONGO_URI')
        if self.mongo_uri:
            try:
                self.client = MongoClient(self.mongo_uri)
                self.collection = self.client['stock_terminal']['portfolio']
            except: self.client = None
        else:
            self.client = None
            self.filename = 'portfolio.json'
        self.data = self.load()
        self._ensure_default_user()

    def default_user_state(self, username=""):
        return {
            'master_budget': 5000.0 if 'test' in username.lower() else 10000.0,
            'watchlist': [], 'initial_positions': {}, 'holdings': {}, 'history': [], 'notified_signals': {},
            'settings': {'period': '1mo', 'interval': '1d', 'style': 'candlestick', 'refresh': '10000', 'ntfy_topic': ''}
        }

    def load(self):
        if self.client:
            try:
                doc = self.collection.find_one({"_id": "main_store"})
                if doc:
                    doc.pop('_id', None)
                    return doc
            except: pass
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    if 'users' not in data:
                        data = {'active_user': 'Ian', 'users': { 'Ian': data }}
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
            default_profiles = [
                'Ian', 'Test A - Deep Value', 'Test B - Momentum (UK & US)', 
                'Test C - 24/5 Global', 'Test D - Volatility', 'Test E - Rotator', 'Test F - EOD Sweep',
                'Test G - Long/Short Bi-Directional', 'Test H - Wick Reversal'
            ]
            needs_save = False
            if 'users' not in self.data or not isinstance(self.data['users'], dict):
                self.data['users'] = {}
                needs_save = True

            ghost_keys = ['Test', 'Test 2', 'Test 3', 'Test 4 - Volatility Breakout', 'Test 5 - Rel Strength Rotator', 'Test 6 - EOD Cash Sweep', 'Test B - US Momentum']
            for gk in ghost_keys:
                if gk in self.data['users']:
                    del self.data['users'][gk]
                    needs_save = True

            for p in default_profiles:
                if p not in self.data['users']:
                    self.data['users'][p] = self.default_user_state(p)
                    needs_save = True
                    
            if self.data.get('active_user') not in self.data['users']:
                self.data['active_user'] = 'Test B - Momentum (UK & US)'
                needs_save = True
            
            if needs_save: self.save_data(self.data)
        except Exception as e:
            print(f"Cleanup error: {e}")

    def save_data(self, data_to_save):
        if self.client:
            try: self.collection.update_one({"_id": "main_store"}, {"$set": data_to_save}, upsert=True)
            except: pass
        else:
            try:
                with open(self.filename, 'w') as f: json.dump(data_to_save, f, indent=2)
            except: pass

    def active_username(self): 
        return self.data.get('active_user', 'Ian')

    def user_data(self, username=None):
        au = username if username else self.active_username()
        if 'users' not in self.data: self.data['users'] = {}
        if au not in self.data['users']:
            self.data['users'][au] = self.default_user_state(au)
            self.save_data(self.data)
        return self.data['users'][au]

    def add_user(self, username):
        if not username.strip(): return
        self.reload()
        if 'users' not in self.data: self.data['users'] = {}
        if username.strip() not in self.data['users']: self.data['users'][username.strip()] = self.default_user_state(username.strip())
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

    def update_settings(self, settings):
        self.reload()
        ud = self.user_data()
        if 'settings' not in ud or not isinstance(ud['settings'], dict): ud['settings'] = {}
        ud['settings'].update(settings)
        self.save_data(self.data)

    def add_watchlist(self, ticker):
        self.reload()
        ud = self.user_data()
        if 'watchlist' not in ud or not isinstance(ud['watchlist'], list): ud['watchlist'] = []
        if ticker.upper() not in ud['watchlist']:
            ud['watchlist'].append(ticker.upper())
            self.save_data(self.data)

    def remove_watchlist(self, ticker):
        self.reload()
        ticker = ticker.upper()
        ud = self.user_data()
        if 'watchlist' in ud and isinstance(ud['watchlist'], list) and ticker in ud['watchlist']: ud['watchlist'].remove(ticker)
        if 'holdings' in ud and isinstance(ud['holdings'], dict): ud['holdings'].pop(ticker, None)
        if 'initial_positions' in ud and isinstance(ud['initial_positions'], dict): ud['initial_positions'].pop(ticker, None)
        if 'history' in ud and isinstance(ud['history'], list): ud['history'] = [h for h in ud['history'] if isinstance(h, dict) and h.get('ticker') != ticker]
        if 'notified_signals' in ud and isinstance(ud['notified_signals'], dict): ud['notified_signals'].pop(ticker, None)
        self.save_data(self.data)

    def get_shares(self, ticker, username=None):
        if not ticker: return 0
        ud = self.user_data(username)
        init_pos = ud.get('initial_positions') or {}
        init_sh = (init_pos.get(ticker) or {}).get('shares', 0)
        net_sh = sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in ud.get('history', []) if isinstance(t, dict) and t.get('ticker') == ticker)
        return max(0, init_sh + net_sh)

    def set_holding_value(self, ticker, value_owned, current_price, username=None):
        self.reload()
        if not ticker: return 0
        ud = self.user_data(username)
        value_owned, current_price = float(value_owned), float(current_price)
        price_per_share = current_price / 100.0 if ticker.endswith('.L') and current_price > 100 else current_price
        target_sh = round(value_owned / price_per_share) if price_per_share > 0 else 0
        baseline_sh = target_sh - sum(t.get('shares', 0) if t.get('action') == 'BUY' else -t.get('shares', 0) for t in ud.get('history', []) if isinstance(t, dict) and t.get('ticker') == ticker)
        
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
        self.reload()
        if not ticker or shares <= 0: return None
        ud = self.user_data(username)
        shares, price = int(shares), round(float(price), 2)
        cost_per_sh = price / 100.0 if ticker.endswith('.L') and price > 100 else price
        tot_amt = round(shares * cost_per_sh, 2)
        
        is_short = ticker in ['SQQQ', '3SUS.L']
        trade_type = 'SHORT' if is_short else 'LONG'

        now = pd.Timestamp.now(tz='Europe/London')
        entry = {'id': str(int(time.time() * 1000)), 'ticker': ticker, 'trade_type': trade_type, 'action': 'BUY' if 'BUY' in action_type.upper() else 'SELL', 'shares': shares, 'price': price, 'amount': tot_amt, 'time': now.strftime('%d %b %H:%M'), 'date_str': now.strftime('%Y-%m-%d'), 'timestamp': int(now.timestamp())}
        if 'history' not in ud: ud['history'] = []
        ud['history'].insert(0, entry)

        curr_tot = self.get_shares(ticker, username)
        if 'holdings' not in ud: ud['holdings'] = {}
        if curr_tot > 0: 
            hw = (ud['holdings'].get(ticker) or {}).get('high_water', price)
            ud['holdings'][ticker] = {'shares': curr_tot, 'manual_val': round(curr_tot * cost_per_sh, 2), 'high_water': max(hw, price)}
        else: ud['holdings'].pop(ticker, None)
        self.save_data(self.data)
        
        ntfy_topic = (ud.get('settings') or {}).get('ntfy_topic', '')
        if ntfy_topic: send_push_notification(ntfy_topic, f"[{trade_type}] Trade Executed ({username or self.active_username()}): {ticker}", f"{entry['action']} {shares} shares @ £{tot_amt}")
        return entry

    def undo_trade(self, trade_id):
        self.reload()
        ud = self.user_data()
        hist = ud.get('history') or []
        trade = next((t for t in hist if isinstance(t, dict) and t.get('id') == str(trade_id)), None)
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
        hist = ud.get('history') or []
        active_tickers = list(set([t.get('ticker') for t in hist if isinstance(t, dict) and self.get_shares(t.get('ticker'), username) > 0]))
        
        if not active_tickers:
            return 0.0
            
        def fetch_val(t):
            sh = self.get_shares(t, username)
            try:
                df = fetch_yf_data(t, "1d", "1d")
                if not df.empty:
                    p = df['Close'].iloc[-1]
                    cps = p / 100.0 if t.endswith('.L') and p > 100 else p
                    return t, round(sh * cps, 2)
            except: pass
            return t, 0.0

        total = 0.0
        holds = ud.get('holdings') or {}
        with ThreadPoolExecutor(max_workers=40) as ex:
            for t, val in ex.map(fetch_val, active_tickers):
                total += val
                if t in holds and isinstance(holds[t], dict):
                    holds[t]['manual_val'] = val
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
            'SOXL': 'Direxion Daily Semi Bull 3X', 'NVDL': 'GraniteShares 2x Long NVDA', 'SQQQ': 'ProShares UltraPro Short QQQ (3x Short)',
            '3SUS.L': 'WisdomTree US NASDAQ 3x Short', 'TSM': 'TSMC ADR', 'SONY': 'Sony Group', 'BABA': 'Alibaba Group',
            'ASML': 'ASML Holding', 'SAP': 'SAP SE', 'SMCI': 'Super Micro Computer', 'ARM': 'ARM Holdings', 'AVGO': 'Broadcom Inc.',
            'CONL': 'GraniteShares 2x Long COIN', 'MSTX': 'Defiance 2x Daily Long MSTR', 'BITX': '2x Bitcoin Strategy ETF',
            'RR.L': 'Rolls-Royce Holdings', 'SHEL.L': 'Shell plc', 'BP.L': 'BP plc', 'BARC.L': 'Barclays plc', 'LLOY.L': 'Lloyds Banking Group', 'AZN.L': 'AstraZeneca',
            'GLEN.L': 'Glencore plc', 'RIO.L': 'Rio Tinto plc', 'HSBA.L': 'HSBC Holdings', 'GSK.L': 'GSK plc', 'ULVR.L': 'Unilever plc',
            'SBUX': 'Starbucks Corp', 'NKE': 'Nike Inc', 'BA': 'Boeing Co'
        }

    def check_market_regime(self):
        try:
            df_qqq = fetch_yf_data('QQQ', '5d', '5m')
            if not df_qqq.empty and len(df_qqq) >= 21:
                ema9 = df_qqq['Close'].ewm(span=9, adjust=False).mean().iloc[-1]
                ema21 = df_qqq['Close'].ewm(span=21, adjust=False).mean().iloc[-1]
                
                delta = df_qqq['Close'].diff()
                rs = (delta.where(delta > 0, 0)).rolling(14).mean() / (-delta.where(delta < 0, 0)).rolling(14).mean()
                rsi = 100 - (100 / (1 + rs.iloc[-1])) if not rs.empty else 50
                
                score = 50
                if ema9 > ema21: score += 25
                else: score -= 25
                score += int((rsi - 50) * 0.5)
                score = min(100, max(0, score))
                
                if score <= 20: state, color, code = "Deep Freeze (Capitulation)", "#00d2ff", "BEAR_FREEZE"
                elif score <= 40: state, color, code = "Cooling (Pullback)", "#ff9900", "BEAR"
                elif score <= 60: state, color, code = "Room Temp (Neutral)", "#8a8a9e", "NEUTRAL"
                elif score <= 80: state, color, code = "Heating Up (Expansion)", "#00c853", "BULL"
                else: state, color, code = "Overheating (Euphoria)", "#b388ff", "BULL_OVERHEAT"
                
                return {'score': score, 'state': state, 'color': color, 'code': code}
        except: pass
        return {'score': 50, 'state': 'Room Temp (Neutral)', 'color': '#8a8a9e', 'code': 'NEUTRAL'}

    def score_momentum(self, df_5m, current_price, avg_buy_price=0.0, highest_price=0.0, profile='test b', regime=None):
        if regime is None: regime = {'score': 50, 'state': 'Room Temp (Neutral)', 'color': '#8a8a9e', 'code': 'NEUTRAL'}
        
        ticker = getattr(df_5m, 'name', '')
        is_inverse = ticker in ['SQQQ', '3SUS.L']
        trade_type = 'SHORT' if is_inverse else 'LONG'

        if df_5m.empty or len(df_5m) < 21:
            return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': '0.00%', 
                    'reason': 'Insufficient intraday price history.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                    'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Tranche 0)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type}

        prof = profile.lower()
        pct_change_5d = ((current_price - df_5m['Close'].iloc[0]) / df_5m['Close'].iloc[0]) * 100.0

        now_uk = pd.Timestamp.now(tz='Europe/London')
        is_us_stock = not ticker.endswith('.L')

        # TEST B PRE-CLOSE CAPITAL UNLOCK RULE
        if 'test b' in prof and is_us_stock and now_uk.hour == 20 and now_uk.minute >= 50:
            if avg_buy_price > 0:
                pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                if pnl_pct < 0.30:
                    return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%",
                            'reason': "US PRE-CLOSE CAPITAL UNLOCK: Liquidating weak US stock before 9:00 PM BST close to free capital for UK morning open.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                            'status': 'US Pre-Close Unlock', 'color': '#ff9900', 'action_main': 'SELL', 'action_sub': '(UK Capital Unlock)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type}

        if 'test f' in prof and now_uk.hour == 20 and now_uk.minute >= 55:
            return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%",
                    'reason': f"EOD CASH SWEEP TRIGGERED. Liquidating position to 100% cash before 9:00 PM BST close.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                    'status': 'EOD Cash Sweep', 'color': '#ff9900', 'action_main': 'SELL', 'action_sub': '(EOD Cash Sweep)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': trade_type}

        # TEST H WICK REVERSAL SPECIFIC SCORING ENGINE
        if 'test h' in prof:
            last_candle = df_5m.iloc[-2]
            c_open, c_close = last_candle['Open'], last_candle['Close']
            c_high, c_low = last_candle['High'], last_candle['Low']
            total_range = c_high - c_low

            if total_range > 0:
                upper_wick = c_high - max(c_open, c_close)
                lower_wick = min(c_open, c_close) - c_low

                if avg_buy_price > 0 and highest_price > 0:
                    pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                    drop_from_peak = ((highest_price - current_price) / highest_price) * 100.0

                    if (upper_wick / total_range) >= 0.50:
                        return {'type': 'Wick Reversal', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%",
                                'reason': f"TOP WICK EXHAUSTION DETECTED. Upper wick made up {((upper_wick/total_range)*100):.1f}% of candle range. Selling peak.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                                'status': 'Top Wick Rejection', 'color': '#ff3d00', 'action_main': 'SELL', 'action_sub': '(Top Exhaustion)', 'action_color': '#ff3d00', 'regime': regime, 'trade_type': trade_type}
                    
                    if drop_from_peak >= 0.40:
                        return {'type': 'Wick Reversal', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%",
                                'reason': f"TRAILING STOP TRIPPED. Dropped {drop_from_peak:.2f}% from peak of £{highest_price:.2f}.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                                'status': 'Trailing Stop', 'color': '#00d2ff', 'action_main': 'SELL', 'action_sub': '(Lock Profit)', 'action_color': '#00d2ff', 'regime': regime, 'trade_type': trade_type}

                    return {'type': 'Wick Reversal', 'score': 80, 'tranches': 1, 'discount': f"{pnl_pct:.2f}%",
                            'reason': f"HOLDING WICK REVERSAL. High Water Mark: £{highest_price:.2f}.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                            'status': 'Holding Reversal', 'color': '#00d2ff', 'action_main': 'HOLD / WAIT', 'action_sub': '(Riding Reversal)', 'action_color': '#00d2ff', 'regime': regime, 'trade_type': trade_type}

                if (lower_wick / total_range) >= 0.50 and current_price > c_low:
                    wick_score = min(100, max(65, round(60 + (lower_wick / total_range) * 40)))
                    return {'type': 'Wick Reversal', 'score': wick_score, 'tranches': 1, 'discount': f"{pct_change_5d:.2f}%",
                            'reason': f"BOTTOM WICK REVERSAL: Buyers rejected low prices. Lower wick ratio is {((lower_wick/total_range)*100):.1f}%.", 'is_smart': True, 'rec_buy': round(current_price*0.99, 2), 'rec_sell': round(current_price*1.02, 2),
                            'status': 'Bottom Wick Reversal', 'color': '#00c853', 'action_main': 'BUY', 'action_sub': '(Wick Entry)', 'action_color': '#00c853', 'regime': regime, 'trade_type': trade_type}

            return {'type': 'Wick Reversal', 'score': 10, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%",
                    'reason': "Scanning 5-minute wicks for lower buyer-rejection pin bars.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                    'status': 'Scanning Wicks', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(No Wick Setup)', 'action_color': '#8a8a9e', 'regime': regime, 'trade_type': trade_type}

        trail_pct = 0.30 if regime['code'] == 'BULL_OVERHEAT' else (0.75 if 'test d' in prof else (1.00 if 'test e' in prof or 'test f' in prof else 0.50))
        hard_pct = -0.50 if 'test d' in prof else -1.00

        if avg_buy_price > 0 and highest_price > 0:
            pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
            drop_from_peak_pct = ((highest_price - current_price) / highest_price) * 100.0

            if drop_from_peak_pct >= trail_pct:
                return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"+{pnl_pct:.2f}%" if pnl_pct > 0 else f"{pnl_pct:.2f}%",
                        'reason': f"TRAILING STOP TRIPPED. Dropped {drop_from_peak_pct:.2f}% from peak of £{highest_price:.2f}.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                        'status': 'Trailing Stop', 'color': '#00d2ff', 'action_main': 'SELL', 'action_sub': '(Lock Profits)', 'action_color': '#00d2ff', 'regime': regime, 'trade_type': trade_type}
            
            if pnl_pct <= hard_pct: 
                return {'type': 'Intraday Momentum', 'score': 0, 'tranches': 0, 'discount': f"{pnl_pct:.2f}%",
                        'reason': f"HARD STOP LOSS TRIPPED ({pnl_pct:.2f}%). Cutting losses immediately.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                        'status': 'Stop Loss', 'color': '#ff3d00', 'action_main': 'SELL', 'action_sub': '(Stop Loss)', 'action_color': '#ff3d00', 'regime': regime, 'trade_type': trade_type}

            return {'type': 'Intraday Momentum', 'score': 80, 'tranches': 1, 'discount': f"{pnl_pct:.2f}%",
                    'reason': f"RIDING TREND. High Water Mark: £{highest_price:.2f} (Trailing Drop: -{drop_from_peak_pct:.2f}%).", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                    'status': 'Trailing Stop Active', 'color': '#00d2ff', 'action_main': 'HOLD / WAIT', 'action_sub': '(Riding Winner)', 'action_color': '#00d2ff', 'regime': regime, 'trade_type': trade_type}

        # TEST G BI-DIRECTIONAL LOGIC
        if 'test g' in prof:
            if regime['code'] in ['BEAR', 'BEAR_FREEZE'] and not is_inverse:
                return {'type': 'Bi-Directional', 'score': 10, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%",
                        'reason': "LONG BUYS BLOCKED: Market Sentiment in Pullback mode. Scanning Inverse Short ETFs.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                        'status': 'Bearish Market - Short Mode', 'color': '#ff9900', 'action_main': 'HOLD / WAIT', 'action_sub': '(Seeking Short)', 'action_color': '#ff9900', 'regime': regime, 'trade_type': 'LONG'}
            elif regime['code'] in ['BULL', 'BULL_OVERHEAT'] and is_inverse:
                return {'type': 'Bi-Directional', 'score': 10, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%",
                        'reason': "SHORT BUYS BLOCKED: Market Sentiment in Bullish mode. Inverse ETFs paused.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                        'status': 'Bullish Market - Long Mode', 'color': '#00c853', 'action_main': 'HOLD / WAIT', 'action_sub': '(Seeking Long)', 'action_color': '#00c853', 'regime': regime, 'trade_type': 'SHORT'}

        elif regime['code'] in ['BEAR', 'BEAR_FREEZE']:
            return {'type': 'Intraday Momentum', 'score': 10, 'tranches': 0, 'discount': f"{pct_change_5d:.2f}%",
                    'reason': f"SENTIMENT THERMOMETER BLOCK ({regime['score']}/100 - {regime['state']}): QQQ in pullback. Tech buys blocked to defend cash.", 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                    'status': regime['state'], 'color': regime['color'], 'action_main': 'HOLD / WAIT', 'action_sub': '(Market Cooling)', 'action_color': regime['color'], 'regime': regime, 'trade_type': trade_type}

        ema9 = df_5m['Close'].ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = df_5m['Close'].ewm(span=21, adjust=False).mean().iloc[-1]
        delta = df_5m['Close'].diff()
        rs = (delta.where(delta > 0, 0)).rolling(14).mean() / (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + rs.iloc[-1])) if not rs.empty else 50
        
        if ema9 > ema21 and rsi < 65 and pct_change_5d > 0:
            buy_score = min(100, max(50, round(50 + pct_change_5d * 10 + (70 - rsi))))
            tranches, action_main, action_sub = 1, "BUY", f"({trade_type} Surge)"
            status, color, action_color = f"Fast {trade_type} Surge", "#00c853", "#00c853"
            reason = f"SURGE DETECTED ({trade_type}): 9-EMA ({ema9:.2f}) > 21-EMA ({ema21:.2f}), RSI {rsi:.1f}."
        else:
            buy_score, tranches = 10, 0
            action_main, action_sub = "HOLD / WAIT", "(Awaiting Setup)"
            status, color, action_color = "No Setup", "#8a8a9e", "#8a8a9e"
            reason = f"Awaiting fast EMA crossover surge."

        return {'type': 'Intraday Momentum', 'score': buy_score, 'tranches': tranches, 'discount': f"{pct_change_5d:.2f}%",
                'reason': reason, 'is_smart': True, 'rec_buy': round(current_price*0.99, 2), 'rec_sell': round(current_price*1.02, 2),
                'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color, 'regime': regime, 'trade_type': trade_type}

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
                'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color, 'regime': {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e'}, 'trade_type': 'LONG'}

    def score_equity(self, df, current_price):
        if df.empty or 'Close' not in df:
            return {'type': 'Global Equity', 'score': 0, 'tranches': 0, 'discount': '0.00%', 
                    'reason': 'Awaiting data.', 'is_smart': True, 'rec_buy': current_price, 'rec_sell': current_price,
                    'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '', 'action_color': '#8a8a9e', 'regime': {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e'}, 'trade_type': 'LONG'}
            
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
                'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color, 'regime': {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e'}, 'trade_type': 'LONG'}

portfolio_store = PortfolioManager()

def process_auto_profile(prof_name):
    try:
        ud = portfolio_store.user_data(prof_name)
        engine = MarketScoringEngine()
        active_profile = prof_name.strip().lower()
        is_momentum = any(x in active_profile for x in ['test b', 'test c', 'test d', 'test e', 'test f', 'test g', 'test h'])
        if not is_momentum: return

        if 'test g' in active_profile:
            scan_list = ['SQQQ', '3SUS.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'PLTR', 'MSTR', 'RR.L', 'SHEL.L', 'BP.L']
        elif 'test h' in active_profile:
            scan_list = ['MSTR', 'TQQQ', 'SOXL', 'NVDL', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'PLTR', 'COIN', 'CONL', 'MSTX', 'BITX', 'SMCI', 'ARM', 'AVGO', 'RR.L', 'SHEL.L', 'BP.L', 'AZN.L', 'BARC.L', 'LLOY.L', 'GLEN.L', 'RIO.L', 'HSBA.L', 'GSK.L', 'ULVR.L']
        elif 'test c' in active_profile:
            scan_list = ['TSM', 'SONY', 'BABA', 'ASML', 'SAP', 'AZN.L', 'RR.L', 'SHEL.L', 'BP.L', 'BARC.L', 'LLOY.L', 'GLEN.L', 'RIO.L', 'HSBA.L', 'GSK.L', 'ULVR.L', 'SGLN.L', 'SSLN.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'GOOGL', 'NFLX', 'PLTR', 'COIN', 'MSTR', 'TQQQ', 'SOXL', 'NVDL']
        else:
            scan_list = ['RR.L', 'SHEL.L', 'BP.L', 'AZN.L', 'BARC.L', 'LLOY.L', 'GLEN.L', 'RIO.L', 'HSBA.L', 'GSK.L', 'ULVR.L', 'SGLN.L', 'SSLN.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'GOOGL', 'NFLX', 'PLTR', 'COIN', 'MSTR', 'TQQQ', 'SOXL', 'NVDL', 'SMCI', 'ARM', 'AVGO', 'CONL', 'MSTX', 'BITX']

        mb = ud.get('master_budget', 5000.0)
        hist = ud.get('history') or []
        init_pos = ud.get('initial_positions') or {}

        net_history = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist if isinstance(tr, dict))
        init_manual = sum(pos.get('manual_val', 0.0) for pos in init_pos.values() if isinstance(pos, dict))
        cash_balance = mb + net_history - init_manual
        rem_cash = max(0, cash_balance)

        tot_own = portfolio_store.get_total_portfolio_value(prof_name)
        total_equity = cash_balance + tot_own

        dirs, buys, held_scores = [], [], []
        MIN_BUY_VALUE = 20.0
        regime = engine.check_market_regime()

        dfs = {}
        def fetch_data_thread(tick): return tick, fetch_yf_data(tick, "5d", "5m")
        with ThreadPoolExecutor(max_workers=40) as ex:
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
                
                if now_uk.weekday() >= 5:
                    is_open = False
                elif is_us_stock:
                    is_open = (14 * 60 + 30) <= current_mins < (21 * 60)
                else:
                    is_open = (8 * 60) <= current_mins < (16 * 60 + 30)

                avg_buy_p, highest_p = 0.0, cur
                if sh > 0:
                    t_buys = [tr for tr in hist if isinstance(tr, dict) and tr.get('ticker') == t and tr.get('action') == 'BUY']
                    if t_buys: avg_buy_p = t_buys[0].get('price', 0.0)
                    highest_p = (ud.get('holdings', {}).get(t) or {}).get('high_water', cur)
                    if cur > highest_p:
                        highest_p = cur
                        if 'holdings' not in ud: ud['holdings'] = {}
                        if t not in ud['holdings']: ud['holdings'][t] = {}
                        ud['holdings'][t]['high_water'] = highest_p

                st = engine.score_momentum(df, cur, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=active_profile, regime=regime)

                last_trade_time = next((h.get('timestamp', 0) for h in hist if isinstance(h, dict) and h.get('ticker') == t), 0)
                if int(time.time()) - last_trade_time < 300: continue

                if sh > 0:
                    held_scores.append({'ticker': t, 'shares': sh, 'price': cur, 'cps': cps, 'value': vo, 'score': st['score'], 'action': st['action_main']})

                if st['action_main'] == 'SELL' and sh > 0 and vo >= MIN_BUY_VALUE:
                    if is_open:
                        dirs.append({'ticker': t, 'action': 'SELL', 'shares': sh, 'price': cur, 'amount': round(vo, 2)})
                elif st['action_main'] == 'BUY':
                    if is_open:
                        buys.append({'t': t, 'cps': cps, 'p': cur, 's': st['score']})
            except: pass

        max_allowed_holds = 1 if 'test e' in active_profile else 2
        
        if buys and held_scores and len(held_scores) >= max_allowed_holds:
            top_candidate = max(buys, key=lambda x: x['s'])
            weakest_holding = min(held_scores, key=lambda x: x['score'])
            
            if top_candidate['s'] >= (weakest_holding['score'] + 15) and top_candidate['s'] >= 65:
                dirs.append({'ticker': weakest_holding['ticker'], 'action': 'SELL', 'shares': weakest_holding['shares'], 'price': weakest_holding['price'], 'amount': round(weakest_holding['value'], 2)})
                rem_cash += weakest_holding['value']

        if len(held_scores) > max_allowed_holds:
            held_scores.sort(key=lambda x: x['score'])
            for i in range(len(held_scores) - max_allowed_holds):
                weakest = held_scores[i]
                dirs.append({'ticker': weakest['ticker'], 'action': 'SELL', 'shares': weakest['shares'], 'price': weakest['price'], 'amount': round(weakest['value'], 2)})

        now_uk = pd.Timestamp.now(tz='Europe/London')
        is_eod_blocked = ('test f' in active_profile and now_uk.hour == 20 and now_uk.minute >= 50)
        current_hold_count = len([x for x in held_scores if x['shares'] > 0])
        slots_available = max(0, max_allowed_holds - current_hold_count)

        if buys and rem_cash >= MIN_BUY_VALUE and not is_eod_blocked and slots_available > 0:
            buys.sort(key=lambda x: x['s'], reverse=True)
            top_buys = buys[:slots_available]
            per_stock_budget = min(rem_cash / len(top_buys), total_equity * (0.98 if 'test e' in active_profile else 0.50))
            for b in top_buys:
                bs = int(per_stock_budget // b['cps'])
                amt = round(bs * b['cps'], 2)
                if bs > 0 and amt >= MIN_BUY_VALUE and amt <= rem_cash:
                    dirs.append({'ticker': b['t'], 'action': 'BUY', 'shares': bs, 'price': b['p'], 'amount': amt})

        for d in dirs:
            if d['action'] == 'BUY':
                curr_cb = sum((init_pos.get(w) or {}).get('manual_val', 0.0) + sum(tr.get('amount', 0) if tr.get('action')=='BUY' else -tr.get('amount', 0) for tr in hist if isinstance(tr, dict) and tr.get('ticker')==w) for w in [tk for tk, hd in ud.get('holdings', {}).items() if isinstance(hd, dict) and hd.get('shares', 0) > 0])
                curr_cash = mb - curr_cb
                if curr_cash < MIN_BUY_VALUE: continue
                if d['amount'] > curr_cash:
                    d['shares'] = int(curr_cash // (d['price'] / 100.0 if d['ticker'].endswith('.L') else d['price']))
            if d['shares'] > 0:
                portfolio_store.execute_trade(d['ticker'], d['action'], d['shares'], d['price'], prof_name)
    except: pass

def global_background_worker():
    while True:
        try:
            auto_profiles = ['Test B - Momentum (UK & US)', 'Test C - 24/5 Global', 'Test D - Volatility', 'Test E - Rotator', 'Test F - EOD Sweep', 'Test G - Long/Short Bi-Directional', 'Test H - Wick Reversal']
            for prof in auto_profiles:
                process_auto_profile(prof)
        except: pass
        time.sleep(10)

threading.Thread(target=global_background_worker, daemon=True).start()

@app.route('/')
def index(): 
    return render_template('index.html')

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
        is_momentum = any(x in active_profile for x in ['test b', 'test c', 'test d', 'test e', 'test f', 'test g', 'test h'])
        df = fetch_yf_data(t, "5d" if is_momentum else "1y", "5m" if is_momentum else "1d")
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
    dirs = []
    return jsonify({'directives': dirs})

@app.route('/api/recommend', methods=['GET'])
def get_recommendations():
    res, engine = [], MarketScoringEngine()
    tickers = ['YCA.L', 'U-UN.TO', 'SGLN.L', 'SSLN.L', 'PHYS', 'PSLV', 'CEF', 'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'GOOGL', 'META', 'BHP', 'RIO', 'VALE', 'XOM', 'CVX', 'OXY', 'JPM', 'BAC', 'GS', 'PFE', 'JNJ', 'UNH', 'DIS', 'NKE', 'SBUX', 'BA', 'LMT']
    
    def fetch_rec(tick): return tick, fetch_yf_data(tick, "1y", "1d")
        
    with ThreadPoolExecutor(max_workers=40) as ex:
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
        is_momentum = any(x in active_profile for x in ['test b', 'test c', 'test d', 'test e', 'test f', 'test g', 'test h'])
        if not t: t = 'ALL_SHARES'

        # FIX: SYNC UI WATCHLIST WITH FULL 34-ASSET BACKGROUND SCANNER LIST
        if is_momentum and not wl:
            if 'test g' in active_profile:
                wl = ['SQQQ', '3SUS.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'PLTR', 'MSTR', 'RR.L', 'SHEL.L', 'BP.L']
            elif 'test h' in active_profile:
                wl = ['MSTR', 'TQQQ', 'SOXL', 'NVDL', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'PLTR', 'COIN', 'CONL', 'MSTX', 'BITX', 'SMCI', 'ARM', 'AVGO', 'RR.L', 'SHEL.L', 'BP.L', 'AZN.L', 'BARC.L', 'LLOY.L', 'GLEN.L', 'RIO.L', 'HSBA.L', 'GSK.L', 'ULVR.L']
            elif 'test c' in active_profile:
                wl = ['TSM', 'SONY', 'BABA', 'ASML', 'SAP', 'AZN.L', 'RR.L', 'SHEL.L', 'BP.L', 'BARC.L', 'LLOY.L', 'GLEN.L', 'RIO.L', 'HSBA.L', 'GSK.L', 'ULVR.L', 'SGLN.L', 'SSLN.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'GOOGL', 'NFLX', 'PLTR', 'COIN', 'MSTR', 'TQQQ', 'SOXL', 'NVDL']
            else:
                wl = ['RR.L', 'SHEL.L', 'BP.L', 'AZN.L', 'BARC.L', 'LLOY.L', 'GLEN.L', 'RIO.L', 'HSBA.L', 'GSK.L', 'ULVR.L', 'SGLN.L', 'SSLN.L', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'AAPL', 'META', 'MSFT', 'GOOGL', 'NFLX', 'PLTR', 'COIN', 'MSTR', 'TQQQ', 'SOXL', 'NVDL', 'SMCI', 'ARM', 'AVGO', 'CONL', 'MSTX', 'BITX']
            
            for w in wl: 
                portfolio_store.add_watchlist(w)
            portfolio_store.reload()
            ud = portfolio_store.user_data()

        hist = ud.get('history') or []
        init_pos = ud.get('initial_positions') or {}
        mb = ud.get('master_budget', 5000.0 if is_momentum else 10000.0)

        active_holds = list(set([tr.get('ticker') for tr in hist if isinstance(tr, dict) and portfolio_store.get_shares(tr.get('ticker')) > 0]))

        pnl_dfs_5m = {}
        pnl_dfs_1d = {}
        def fetch_pnl_data_5m(tick): return tick, fetch_yf_data(tick, "5d", "5m")
        def fetch_pnl_data_1d(tick): return tick, fetch_yf_data(tick, "1mo", "1d")
        
        if active_holds:
            with ThreadPoolExecutor(max_workers=40) as ex:
                for tick, df_5m in ex.map(fetch_pnl_data_5m, active_holds): pnl_dfs_5m[tick] = df_5m
                for tick, df_1d in ex.map(fetch_pnl_data_1d, active_holds): pnl_dfs_1d[tick] = df_1d

        net_history = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist if isinstance(tr, dict))
        init_manual = sum(pos.get('manual_val', 0.0) for pos in init_pos.values() if isinstance(pos, dict))
        cash_balance = mb + net_history - init_manual

        tot_own = portfolio_store.get_total_portfolio_value()
        total_equity = cash_balance + tot_own
        master_pnl_val = total_equity - mb
        master_pnl_pct = (master_pnl_val / mb) * 100.0 if mb > 0 else 0.0

        leaderboard = []
        for u, u_data in portfolio_store.data.get('users', {}).items():
            if not isinstance(u_data, dict): continue
            mb_lb = u_data.get('master_budget', 5000.0 if 'test' in u.lower() else 10000.0)
            hist_lb = u_data.get('history') or []
            init_pos_lb = u_data.get('initial_positions') or {}
            
            nh_lb = sum(-tr.get('amount', 0) if tr.get('action') == 'BUY' else tr.get('amount', 0) for tr in hist_lb if isinstance(tr, dict))
            im_lb = sum(pos.get('manual_val', 0.0) for pos in init_pos_lb.values() if isinstance(pos, dict))
            cash_lb = mb_lb + nh_lb - im_lb
            
            holds_lb_val = portfolio_store.get_total_portfolio_value(u)
            leaderboard.append({
                'user': u, 
                'equity': round(max(0, cash_lb) + holds_lb_val, 2),
                'budget': mb_lb
            })
        leaderboard.sort(key=lambda x: x['equity'], reverse=True)

        settings = ud.get('settings') or {}
        req_p = request.args.get('p')
        if not req_p or req_p == 'undefined': req_p = settings.get('period', '5d' if is_momentum else '1mo')
        req_i = request.args.get('i')
        if not req_i or req_i == 'undefined': req_i = settings.get('interval', '5m' if is_momentum else '1d')

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
                except: return tick, 'closed'

            with ThreadPoolExecutor(max_workers=40) as ex:
                for tick, status in ex.map(check_status, wl):
                    wl_status[tick] = status
            
        tot_today_diff, tot_1h_diff = 0.0, 0.0
        now_utc = pd.Timestamp.now(tz='UTC')
        now_lon = pd.Timestamp.now(tz='Europe/London')
        
        for tk in active_holds:
            sh_h = portfolio_store.get_shares(tk)
            if sh_h <= 0: continue
            
            t_buys_today = [tr for tr in hist if isinstance(tr, dict) and tr.get('ticker') == tk and tr.get('action') == 'BUY' and tr.get('date_str') == now_lon.strftime('%Y-%m-%d')]
            
            df_5m = pnl_dfs_5m.get(tk)
            if df_5m is not None and not df_5m.empty:
                cur_price = df_5m['Close'].iloc[-1]
                div = 100.0 if tk.endswith('.L') and cur_price > 100 else 1.0
                
                if t_buys_today:
                    p_today_base = sum(tr.get('amount', 0) for tr in t_buys_today) / sum(tr.get('shares', 1) for tr in t_buys_today)
                else:
                    last_date_str = now_lon.strftime('%Y-%m-%d')
                    prev_sessions = df_5m[df_5m.index.tz_convert('Europe/London').strftime('%Y-%m-%d') < last_date_str]
                    p_today_base = prev_sessions['Close'].iloc[-1] if not prev_sessions.empty else df_5m['Close'].iloc[0]

                target_ts = now_utc - pd.Timedelta(hours=1)
                prior_df = df_5m[df_5m.index <= target_ts]
                p_1h_base = prior_df['Close'].iloc[-1] if not prior_df.empty else p_today_base

                tot_today_diff += sh_h * ((cur_price - p_today_base) / div)
                tot_1h_diff += sh_h * ((cur_price - p_1h_base) / div)

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
            lines = []
            if wl:
                def fetch_t(tick):
                    return tick, fetch_yf_data(tick, req_p, req_i)
                
                dfs = {}
                with ThreadPoolExecutor(max_workers=40) as ex:
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
            
            return jsonify({'is_multi': True, 'lines': lines, 'wl_status': wl_status, 'name': 'Relative Performance (Watchlist)', 'portfolio': ud, 'leaderboard': leaderboard, 'metrics': {
                'price': 0, 'price_display': f"Normalized %",
                'discount': '--', 'buy_score': '--', 'tranches': 0,
                'status': regime['state'], 'color': regime['color'], 'reason': f"Sentiment Thermometer: {regime['score']}/100 ({regime['state']}).",
                'action_main': '--', 'action_sub': '', 'action_color': '#8a8a9e',
                'shares_owned': sum(portfolio_store.get_shares(tk) for tk in active_holds), 'value_owned': tot_own, 
                'pnl_display': master_pnl_data['all']['val'], 'pnl_color': master_pnl_data['all']['color'], 'pnl_data': master_pnl_data,
                'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data, 'master_budget': mb,
                'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime
            }})

        df = fetch_yf_data(t, req_p, req_i)
        if df.empty:
            return jsonify({'ohlc': [], 'wl_status': wl_status, 'name': f'{t} Data Loading...', 'portfolio': ud, 'leaderboard': leaderboard, 'metrics': {
                'price': 0, 'price_display': '£0.00', 'discount': '--', 'buy_score': '0 / 100', 'tranches': 0,
                'status': 'Loading Data', 'color': '#787e8e', 'reason': 'Fetching fresh market quotes.',
                'action_main': 'WAIT', 'action_sub': '', 'action_color': '#787e8e', 'shares_owned': 0, 'value_owned': 0.0,
                'pnl_display': '£0.00 (0.00%)', 'pnl_color': '#8a8a9e', 'pnl_data': master_pnl_data, 'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data,
                'master_budget': mb, 'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime
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
            t_buys = [tr for tr in hist if isinstance(tr, dict) and tr.get('ticker') == t and tr.get('action') == 'BUY']
            if t_buys: avg_buy_p = t_buys[0].get('price', 0.0)

            highest_p = (holds_dict.get(t) or {}).get('high_water', last_p)
            if last_p > highest_p:
                highest_p = last_p

        if is_momentum:
            st = engine.score_momentum(df, last_p, avg_buy_price=avg_buy_p, highest_price=highest_p, profile=active_profile, regime=regime)
        else:
            av = df['Volume'].tail(20).mean() if len(df)>=20 else 1.0
            st = engine.score_nav_asset(t, last_p, (df['Volume'].iloc[-1]/av) if av>0 else 1.0) if t in engine.nav_bases else engine.score_equity(fetch_yf_data(t, "1y", "1d"), last_p)

        cb_t = (init_pos.get(t) or {}).get('manual_val', 0.0) + sum(tr.get('amount', 0) if tr.get('action')=='BUY' else -tr.get('amount', 0) for tr in hist if isinstance(tr, dict) and tr.get('ticker')==t)
        
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
                last_ts = df_5m.index[-1]
                last_lon = last_ts.tz_convert('Europe/London')
                
                t_buys_today_t = [tr for tr in hist if isinstance(tr, dict) and tr.get('ticker') == t and tr.get('action') == 'BUY' and tr.get('date_str') == now_lon.strftime('%Y-%m-%d')]
                avg_b_today_t = (sum(tr.get('amount', 0) for tr in t_buys_today_t) / sum(tr.get('shares', 1) for tr in t_buys_today_t)) if t_buys_today_t else 0.0

                if now_lon.date() > last_lon.date():
                    p_today = cur_p
                    p_1h = cur_p
                else:
                    last_date_str = last_lon.strftime('%Y-%m-%d')
                    prev_sessions = df_5m[df_5m.index.tz_convert('Europe/London').strftime('%Y-%m-%d') < last_date_str]
                    p_today_mkt = prev_sessions['Close'].iloc[-1] if not prev_sessions.empty else df_5m['Close'].iloc[0]
                    p_today = avg_b_today_t if avg_b_today_t > 0 else p_today_mkt
                    
                    if (now_utc - last_ts).total_seconds() > 4200:
                        p_1h = cur_p
                    else:
                        target_ts = now_utc - pd.Timedelta(hours=1)
                        prior_df = df_5m[df_5m.index <= target_ts]
                        p_1h_mkt = prior_df['Close'].iloc[-1] if not prior_df.empty else p_today
                        p_1h = avg_b_today_t if avg_b_today_t > 0 else p_1h_mkt
                
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
            if 'holdings' not in ud or not isinstance(ud['holdings'], dict): ud['holdings'] = {}
            if t not in ud['holdings'] or not isinstance(ud['holdings'][t], dict): ud['holdings'][t] = {}
            ud['holdings'][t]['shares'] = sh_own
            ud['holdings'][t]['manual_val'] = val_own

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

        return jsonify({'ohlc': data, 'mathLine': mathLine, 'wl_status': wl_status, 'name': engine.asset_names.get(t, t), 'portfolio': ud, 'leaderboard': leaderboard, 'metrics': {
            'price': last_p, 'price_display': f"{last_p:.2f}p (£{(last_p/100.0):.2f})" if t.endswith('.L') and last_p>100 else f"£{last_p:.2f}",
            'discount': st['discount'], 'buy_score': f"{st['score']} / 100", 'tranches': st['tranches'],
            'status': st['status'], 'color': st['color'], 'reason': st['reason'],
            'action_main': st['action_main'], 'action_sub': st['action_sub'], 'action_color': st['action_color'],
            'shares_owned': sh_own, 'value_owned': val_own, 'pnl_display': ticker_pnl_data['all']['val'], 'pnl_color': ticker_pnl_data['all']['color'], 'pnl_data': ticker_pnl_data,
            'total_pnl_display': master_pnl_data['all']['val'], 'total_pnl_color': master_pnl_data['all']['color'], 'master_pnl_data': master_pnl_data, 'master_budget': mb,
            'total_portfolio_owned': tot_own, 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2), 'regime': regime, 'trade_type': st.get('trade_type', 'LONG')
        }})
    except Exception as e: 
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8095))
    app.run(host='0.0.0.0', port=port, debug=False)