import os
import json
import time
import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

class PortfolioManager:
    def __init__(self, filename='portfolio.json'):
        self.filename = filename
        self.data = self.load()

    def default_user_state(self):
        return {
            'master_budget': 10000.0,
            'watchlist': [],
            'initial_positions': {},
            'holdings': {},
            'history': [],
            'settings': {
                'period': '1mo',
                'interval': '1d',
                'style': 'candlestick',
                'refresh': '10000'
            }
        }

    def load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    if 'users' not in data:
                        migrated_user = {
                            'master_budget': data.get('master_budget', 10000.0),
                            'watchlist': data.get('watchlist', []),
                            'initial_positions': data.get('initial_positions', {}),
                            'holdings': data.get('holdings', {}),
                            'history': data.get('history', []),
                            'settings': data.get('settings', self.default_user_state()['settings'])
                        }
                        data = {
                            'active_user': 'Ian',
                            'users': { 'Ian': migrated_user }
                        }
                        self.save_data(data)
                    return data
            except Exception: pass
        
        initial = {
            'active_user': 'Ian',
            'users': {
                'Ian': self.default_user_state()
            }
        }
        return initial

    def save_data(self, data_to_save):
        try:
            with open(self.filename, 'w') as f:
                json.dump(data_to_save, f, indent=2)
        except Exception as e:
            print(f"Error saving portfolio: {e}")

    def save(self):
        self.save_data(self.data)

    def active_username(self):
        if 'users' not in self.data or not self.data['users']:
            self.data['users'] = {'Ian': self.default_user_state()}
            self.data['active_user'] = 'Ian'
        if self.data.get('active_user') not in self.data['users']:
            self.data['active_user'] = list(self.data['users'].keys())[0]
        return self.data['active_user']

    def user_data(self):
        username = self.active_username()
        return self.data['users'][username]

    def add_user(self, username):
        username = username.strip()
        if not username: return
        if 'users' not in self.data: self.data['users'] = {}
        if username not in self.data['users']:
            self.data['users'][username] = self.default_user_state()
        self.data['active_user'] = username
        self.save()

    def delete_user(self, username):
        username = username.strip()
        if 'users' in self.data and username in self.data['users']:
            if len(self.data['users']) > 1:
                del self.data['users'][username]
                if self.data.get('active_user') == username:
                    self.data['active_user'] = list(self.data['users'].keys())[0]
                self.save()
                return True
        return False

    def switch_user(self, username):
        if 'users' in self.data and username in self.data['users']:
            self.data['active_user'] = username
            self.save()

    def reset_all(self):
        username = self.active_username()
        self.data['users'][username] = self.default_user_state()
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
        if 'watchlist' in ud and ticker in ud['watchlist']:
            ud['watchlist'].remove(ticker)
        if 'holdings' in ud:
            ud['holdings'].pop(ticker, None)
        if 'initial_positions' in ud:
            ud['initial_positions'].pop(ticker, None)
        if 'history' in ud:
            ud['history'] = [h for h in ud['history'] if h.get('ticker') != ticker]
        self.save()

    def get_net_trade_shares(self, ticker):
        ud = self.user_data()
        trade_shares = 0
        for t in ud.get('history', []):
            if t['ticker'] == ticker:
                if t['action'] == 'BUY': trade_shares += t['shares']
                elif t['action'] == 'SELL': trade_shares -= t['shares']
        return trade_shares

    def get_shares(self, ticker):
        if not ticker: return 0
        ud = self.user_data()
        init_shares = ud.get('initial_positions', {}).get(ticker, {}).get('shares', 0)
        net_trades = self.get_net_trade_shares(ticker)
        return max(0, init_shares + net_trades)

    def update_budget(self, budget):
        ud = self.user_data()
        ud['master_budget'] = float(budget)
        self.save()

    def set_holding_value(self, ticker, value_owned, current_price):
        if not ticker: return 0
        ud = self.user_data()
        value_owned = float(value_owned)
        current_price = float(current_price)
        is_lse_pence = ticker.endswith('.L') and current_price > 100
        price_per_share = current_price / 100.0 if is_lse_pence else current_price

        target_total_shares = round(value_owned / price_per_share) if price_per_share > 0 else 0
        net_trades = self.get_net_trade_shares(ticker)
        baseline_shares = target_total_shares - net_trades
        
        if 'initial_positions' not in ud: ud['initial_positions'] = {}
        if target_total_shares > 0 or value_owned > 0:
            ud['initial_positions'][ticker] = {'shares': baseline_shares, 'manual_val': value_owned}
        else:
            ud['initial_positions'].pop(ticker, None)

        current_total = self.get_shares(ticker)
        live_val = round(current_total * price_per_share, 2)
        if 'holdings' not in ud: ud['holdings'] = {}
        if current_total > 0:
            ud['holdings'][ticker] = {'shares': current_total, 'manual_val': live_val}
        else:
            ud['holdings'].pop(ticker, None)
        self.save()
        return current_total

    def execute_trade(self, ticker, action_type, shares, price):
        if not ticker: return None
        ud = self.user_data()
        shares = int(shares)
        price = float(price)

        is_lse_pence = ticker.endswith('.L') and price > 100
        cost_per_share = price / 100.0 if is_lse_pence else price
        total_amount = round(shares * cost_per_share, 2)

        entry = {
            'id': str(int(time.time() * 1000)),
            'ticker': ticker,
            'action': 'BUY' if 'BUY' in action_type.upper() else 'SELL',
            'shares': shares,
            'price': price,
            'amount': total_amount,
            'time': pd.Timestamp.now().strftime('%d %b %H:%M')
        }
        if 'history' not in ud: ud['history'] = []
        ud['history'].insert(0, entry)

        current_total = self.get_shares(ticker)
        current_val = round(current_total * cost_per_share, 2)
        if 'holdings' not in ud: ud['holdings'] = {}
        if current_total > 0:
            ud['holdings'][ticker] = {'shares': current_total, 'manual_val': current_val}
        else:
            ud['holdings'].pop(ticker, None)
        self.save()
        return entry

    def undo_trade(self, trade_id):
        ud = self.user_data()
        history = ud.get('history', [])
        trade_to_remove = None
        for t in history:
            if t['id'] == str(trade_id):
                trade_to_remove = t
                break
        if not trade_to_remove: return False

        history.remove(trade_to_remove)
        ticker = trade_to_remove['ticker']

        current_total = self.get_shares(ticker)
        if 'holdings' not in ud: ud['holdings'] = {}
        if current_total > 0:
            cost_per_share = trade_to_remove['price'] / 100.0 if ticker.endswith('.L') else trade_to_remove['price']
            ud['holdings'][ticker] = {'shares': current_total, 'manual_val': round(current_total * cost_per_share, 2)}
        else:
            ud['holdings'].pop(ticker, None)

        self.save()
        return True

    def get_total_portfolio_value(self):
        ud = self.user_data()
        total = 0.0
        watchlist = ud.get('watchlist', [])
        holdings = ud.get('holdings', {})
        for t in list(holdings.keys()):
            if t in watchlist and self.get_shares(t) > 0:
                total += holdings[t].get('manual_val', 0.0)
            else:
                holdings.pop(t, None)
        return round(total, 2)

class MarketScoringEngine:
    def __init__(self):
        self.nav_bases = {
            'YCA.L': 634.0, 'U-UN.TO': 28.50, 'PHYS': 33.00, 'PSLV': 21.50, 'CEF': 22.00, 'SGLN.L': 3150.0, 'SSLN.L': 2350.0
        }
        self.asset_names = {
            'YCA.L': 'Yellow Cake plc', 'U-UN.TO': 'Sprott Physical Uranium Trust', 'PHYS': 'Sprott Physical Gold Trust',
            'PSLV': 'Sprott Physical Silver Trust', 'CEF': 'Sprott Physical Gold & Silver', 'GLD': 'SPDR Gold Shares',
            'SLV': 'iShares Silver Trust', 'SGLN.L': 'iShares Physical Gold ETC', 'SSLN.L': 'iShares Physical Silver ETC',
            'MSFT': 'Microsoft Corporation', 'AAPL': 'Apple Inc.', 'NVDA': 'NVIDIA Corporation', 'TSLA': 'Tesla, Inc.',
            'AMZN': 'Amazon.com, Inc.', 'META': 'Meta Platforms, Inc.', 'GOOGL': 'Alphabet Inc.', 'BHP': 'BHP Group Limited', 'RIO': 'Rio Tinto Group', 'NKE': 'Nike, Inc.', 'BA': 'The Boeing Company'
        }

    def score_nav_asset(self, ticker, current_price, volume_ratio):
        nav = self.nav_bases.get(ticker, current_price * 1.10)
        implied_discount = ((nav - current_price) / nav) * 100.0
        
        discount_comp = min(max((implied_discount / 20.0) * 80.0, 0.0), 80.0)
        volume_comp = min(max((volume_ratio / 2.0) * 20.0, 0.0), 20.0)
        buy_score = round(discount_comp + volume_comp, 2)
        buy_score = min(max(buy_score, 0.0), 100.0)
        tranches = int(buy_score // 20)
        
        if implied_discount <= 5.0 and implied_discount > -50.0:
            action_main = "SELL"
            action_sub = "(Take Profit)"
            action_color = "#ff3d00"
            status = "Target Reached"
            color = "#ff3d00"
            tranches = 0
            reason = f"Profit Target Triggered. NAV discount shrunk to {implied_discount:.1f}%. Algorithm dictates taking profits."
        elif buy_score >= 40:
            action_main = "BUY"
            action_sub = f"(Tranche {min(tranches, 5)})"
            action_color = "#00c853"
            status = 'Deep Value Anomaly' if buy_score >= 60 else 'Moderate Value'
            color = '#00c853' if buy_score >= 60 else '#ff9900'
            reason = f"Physical NAV Anomaly. Trading at {implied_discount:.1f}% discount to NAV ({nav}). Scaling into Tranche {min(tranches, 5)}."
        else:
            action_main = "HOLD / WAIT"
            action_sub = f"(Tranche {min(tranches, 5)})"
            action_color = "#8a8a9e"
            status = 'Trading at Premium' if implied_discount < 0 else 'Low Value'
            color = '#ff4a4a' if implied_discount < 0 else '#8a8a9e'
            tranches = 0
            reason = f"No Discount Anomaly. Trading at {implied_discount:.1f}% NAV discount. Recommend 0 tranches until deeper discount."

        return {
            'type': 'Physical Trust', 'score': buy_score, 'tranches': tranches,
            'discount': f"{implied_discount:.2f}%" if implied_discount > 0 else f"+{abs(implied_discount):.2f}% Premium", 
            'reason': reason, 'is_smart': True, 'rec_buy': round(current_price * 0.98, 2), 'rec_sell': round(nav * 0.95, 2),
            'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color
        }

    def score_equity(self, df, current_price):
        baseline_dma = df['Close'].tail(200).mean() if len(df) >= 200 else df['Close'].mean()
        implied_discount = ((baseline_dma - current_price) / baseline_dma) * 100.0
        
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs.iloc[-1]))
        
        avg_vol = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
        vol_rat = (df['Volume'].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
        
        discount_comp = min(max((implied_discount / 25.0) * 50.0, 0.0), 50.0) 
        rsi_comp = max(0, (40 - rsi) / 40 * 30) if pd.notna(rsi) else 0 
        vol_comp = min(max((vol_rat / 2.0) * 20.0, 0.0), 20.0) 
        
        buy_score = round(discount_comp + rsi_comp + vol_comp, 2)
        buy_score = min(max(buy_score, 0.0), 100.0)
        tranches = int(buy_score // 20)
        
        if buy_score >= 40:
            action_main = "BUY"
            action_sub = f"(Tranche {min(tranches, 5)})"
            action_color = "#00c853"
            status = 'Deep Value Anomaly' if buy_score >= 60 else 'Moderate Value'
            color = '#00c853' if buy_score >= 60 else '#ff9900'
            reason = f"Value Anomaly. Trading at {implied_discount:.1f}% discount to 200-day DMA (RSI: {rsi:.1f}). Scaling into Tranche {min(tranches, 5)}."
        elif implied_discount <= -10.0:
            action_main = "SELL"
            action_sub = "(Take Profit)"
            action_color = "#ff3d00"
            status = 'Overextended (High)'
            color = '#ff3d00'
            tranches = 0
            reason = f"Overextended ({abs(implied_discount):.1f}% above 200-day DMA). Take profits."
        else:
            action_main = "HOLD / WAIT"
            action_sub = f"(Tranche {min(tranches, 5)})"
            action_color = "#8a8a9e"
            status = 'Fair Value'
            color = '#8a8a9e'
            tranches = 0
            reason = f"No Value Anomaly. Stock near 200-day DMA. Recommend 0 tranches until price pulls back."
            
        return {
            'type': 'Global Equity', 'score': buy_score, 'tranches': tranches,
            'discount': f"{implied_discount:.2f}%" if implied_discount > 0 else f"+{abs(implied_discount):.2f}% Premium", 
            'reason': reason, 'is_smart': True, 'rec_buy': round(baseline_dma * 0.90, 2), 'rec_sell': round(baseline_dma * 1.05, 2),
            'status': status, 'color': color, 'action_main': action_main, 'action_sub': action_sub, 'action_color': action_color
        }

portfolio_store = PortfolioManager()

@app.route('/')
def index():
    return render_template_string(HTML_FRONTEND)

@app.route('/api/users', methods=['GET'])
def get_users():
    return jsonify({
        'users': list(portfolio_store.data.get('users', {}).keys()),
        'active_user': portfolio_store.active_username()
    })

@app.route('/api/users/select', methods=['POST'])
def select_user():
    body = request.get_json() or {}
    portfolio_store.switch_user(body.get('username', ''))
    return jsonify({
        'status': 'ok',
        'active_user': portfolio_store.active_username(),
        'portfolio': portfolio_store.user_data()
    })

@app.route('/api/users/add', methods=['POST'])
def add_user():
    body = request.get_json() or {}
    portfolio_store.add_user(body.get('username', ''))
    return jsonify({
        'status': 'ok',
        'users': list(portfolio_store.data.get('users', {}).keys()),
        'active_user': portfolio_store.active_username(),
        'portfolio': portfolio_store.user_data()
    })

@app.route('/api/users/delete', methods=['POST'])
def delete_user():
    body = request.get_json() or {}
    success = portfolio_store.delete_user(body.get('username', ''))
    return jsonify({
        'status': 'ok' if success else 'error',
        'users': list(portfolio_store.data.get('users', {}).keys()),
        'active_user': portfolio_store.active_username(),
        'portfolio': portfolio_store.user_data()
    })

@app.route('/api/portfolio', methods=['GET'])
def get_portfolio():
    return jsonify(portfolio_store.user_data())

@app.route('/api/portfolio/reset', methods=['POST'])
def reset_portfolio():
    data = portfolio_store.reset_all()
    return jsonify({'status': 'ok', 'portfolio': data})

@app.route('/api/portfolio/settings', methods=['POST'])
def update_settings():
    body = request.get_json() or {}
    portfolio_store.update_settings(body.get('settings', {}))
    return jsonify({'status': 'ok'})

@app.route('/api/portfolio/budget', methods=['POST'])
def update_budget():
    body = request.get_json() or {}
    portfolio_store.update_budget(body.get('budget', 10000))
    return jsonify({'status': 'ok'})

@app.route('/api/watchlist/add', methods=['POST'])
def add_watchlist():
    body = request.get_json() or {}
    portfolio_store.add_watchlist(body.get('ticker', ''))
    return jsonify({'status': 'ok', 'watchlist': portfolio_store.user_data().get('watchlist', [])})

@app.route('/api/watchlist/delete', methods=['POST'])
def delete_watchlist():
    body = request.get_json() or {}
    portfolio_store.remove_watchlist(body.get('ticker', ''))
    return jsonify({'status': 'ok', 'watchlist': portfolio_store.user_data().get('watchlist', [])})

@app.route('/api/portfolio/holding', methods=['POST'])
def update_holding():
    body = request.get_json() or {}
    shares = portfolio_store.set_holding_value(
        ticker=body.get('ticker'), value_owned=body.get('value_owned', 0), current_price=body.get('price', 1)
    )
    total_owned = portfolio_store.get_total_portfolio_value()
    return jsonify({'status': 'ok', 'shares': shares, 'total_owned': total_owned})

@app.route('/api/trade/execute', methods=['POST'])
def execute_trade():
    body = request.get_json() or {}
    entry = portfolio_store.execute_trade(
        ticker=body.get('ticker'), action_type=body.get('action'), shares=body.get('shares'), price=body.get('price')
    )
    return jsonify({'status': 'ok', 'entry': entry})

@app.route('/api/trade/undo', methods=['POST'])
def undo_trade():
    body = request.get_json() or {}
    success = portfolio_store.undo_trade(body.get('id'))
    return jsonify({'status': 'ok' if success else 'error'})

@app.route('/api/score', methods=['GET'])
def get_score():
    ticker = request.args.get('t', '').upper()
    try:
        t_obj = yf.Ticker(ticker)
        df = t_obj.history(period="1y")
        if df.empty:
            return jsonify({'error': 'Ticker not found.'}), 400
        
        current = df['Close'].iloc[-1]
        avg_vol = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
        vol_rat = (df['Volume'].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
        
        engine = MarketScoringEngine()
        if ticker in engine.nav_bases: res = engine.score_nav_asset(ticker, current, vol_rat)
        else: res = engine.score_equity(df, current)
        
        try: name = t_obj.info.get('shortName', ticker)
        except: name = ticker
        name = engine.asset_names.get(ticker, name)

        res['ticker'] = ticker
        res['name'] = name
        res['price'] = round(current, 2)
        return jsonify(res)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/directives', methods=['GET'])
def get_directives():
    ud = portfolio_store.user_data()
    watchlist = ud.get('watchlist', [])
    engine = MarketScoringEngine()
    master_budget = ud.get('master_budget', 10000.0)
    
    total_owned = portfolio_store.get_total_portfolio_value()
    remaining_cash = max(0, master_budget - total_owned)

    final_directives = []
    buy_targets = []
    total_buy_demand = 0.0

    for t in watchlist:
        t = t.strip().upper()
        if not t: continue
        try:
            df = yf.Ticker(t).history(period="1y")
            if df.empty: continue
            current = df['Close'].iloc[-1]
            avg_vol = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
            vol_rat = (df['Volume'].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0

            if t in engine.nav_bases: state = engine.score_nav_asset(t, current, vol_rat)
            else: state = engine.score_equity(df, current)

            shares_owned = portfolio_store.get_shares(t)
            is_lse_pence = t.endswith('.L') and current > 100
            cost_per_share = current / 100.0 if is_lse_pence else current
            val_owned = shares_owned * cost_per_share

            tranches = state['tranches']
            target_val = (tranches / 5.0) * master_budget
            diff_val = target_val - val_owned

            if state['action_main'] == 'SELL':
                if shares_owned > 0:
                    final_directives.append({
                        'ticker': t, 'name': engine.asset_names.get(t, t), 'action': 'SELL',
                        'shares': shares_owned, 'price': current, 'amount': round(shares_owned * cost_per_share, 2)
                    })
            elif diff_val > 5 and tranches > 0 and cost_per_share > 0:
                buy_targets.append({
                    'ticker': t, 'name': engine.asset_names.get(t, t), 'action': 'BUY',
                    'diff_val': diff_val, 'cost_per_share': cost_per_share, 'price': current
                })
                total_buy_demand += diff_val
            elif val_owned > 0 and (tranches == 0 or diff_val < -5):
                excess_val = abs(diff_val)
                sell_shares = shares_owned if tranches == 0 else min(shares_owned, int(excess_val // cost_per_share))
                if sell_shares > 0:
                    final_directives.append({
                        'ticker': t, 'name': engine.asset_names.get(t, t), 'action': 'SELL',
                        'shares': sell_shares, 'price': current, 'amount': round(sell_shares * cost_per_share, 2)
                    })
        except Exception: pass

    if total_buy_demand > 0:
        scale_ratio = min(1.0, remaining_cash / total_buy_demand) if remaining_cash > 0 else 0.0
        for b in buy_targets:
            allowed_spend = b['diff_val'] * scale_ratio
            buy_shares = int(allowed_spend // b['cost_per_share'])
            if buy_shares > 0:
                final_directives.append({
                    'ticker': b['ticker'], 'name': b['name'], 'action': 'BUY',
                    'shares': buy_shares, 'price': b['price'], 'amount': round(buy_shares * b['cost_per_share'], 2)
                })

    return jsonify({'directives': final_directives})

@app.route('/api/recommend', methods=['GET'])
def get_recommendations():
    scan_list = ['YCA.L', 'U-UN.TO', 'SGLN.L', 'SSLN.L', 'PHYS', 'PSLV', 'CEF', 'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'GOOGL', 'META', 'BHP', 'RIO', 'VALE', 'XOM', 'CVX', 'OXY', 'JPM', 'BAC', 'GS', 'PFE', 'JNJ', 'UNH', 'DIS', 'NKE', 'SBUX', 'BA', 'LMT']
    results = []
    engine = MarketScoringEngine()
    
    for t in scan_list:
        try:
            df = yf.Ticker(t).history(period="1y")
            if df.empty: continue
            current = df['Close'].iloc[-1]
            avg_vol = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
            vol_rat = (df['Volume'].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
            
            if t in engine.nav_bases: res = engine.score_nav_asset(t, current, vol_rat)
            else: res = engine.score_equity(t, current)
            
            res['ticker'] = t
            res['name'] = engine.asset_names.get(t, t)
            res['price'] = round(current, 2)
            results.append(res)
        except: pass
    
    results.sort(key=lambda x: x['score'], reverse=True)
    return jsonify({'recommendations': results})

@app.route('/api/data', methods=['GET'])
def get_data():
    ud = portfolio_store.user_data()
    watchlist = ud.get('watchlist', [])
    ticker = request.args.get('t', '').upper()
    if not ticker and watchlist: ticker = watchlist[0]

    if not ticker or ticker not in watchlist:
        total_portfolio_owned = portfolio_store.get_total_portfolio_value()
        master_budget = ud.get('master_budget', 10000.0)
        return jsonify({
            'ohlc': [],
            'name': 'No Asset Loaded',
            'metrics': {
                'price': 0, 'price_display': '£0.00', 'currency_symbol': '£',
                'discount': '--', 'buy_score': '0 / 100', 'tranches': 0,
                'status': 'Watchlist Empty', 'color': '#787e8e',
                'reason': 'Add a stock to your watchlist to begin tracking.',
                'action_main': 'NO ASSET', 'action_sub': '', 'action_color': '#787e8e',
                'shares_owned': 0, 'value_owned': 0.0,
                'master_budget': master_budget,
                'total_portfolio_owned': total_portfolio_owned,
                'budget_remaining': round(master_budget - total_portfolio_owned, 2)
            },
            'portfolio': ud
        })

    saved_settings = ud.get('settings', {})
    period = request.args.get('p', saved_settings.get('period', '1mo'))
    interval = request.args.get('i', saved_settings.get('interval', '1d'))

    try:
        t_obj = yf.Ticker(ticker)
        df = t_obj.history(period=period, interval=interval)
        df = df.dropna(subset=['Open', 'High', 'Low', 'Close'])
        df = df[~df.index.duplicated(keep='first')].sort_index()
        
        if df.index.tz is not None: df.index = df.index.tz_convert('UTC')
        
        data = []
        seen_times = set()
        
        for index, row in df.iterrows():
            time_val = index.strftime('%Y-%m-%d') if interval in ['1d', '5d', '1wk', '1mo', '3mo'] else int(index.timestamp()) 
            if time_val in seen_times: continue
            seen_times.add(time_val)
            data.append({'time': time_val, 'open': round(row['Open'], 2), 'high': round(row['High'], 2), 'low': round(row['Low'], 2), 'close': round(row['Close'], 2)})
        
        data.sort(key=lambda x: x['time'])
        last_price = data[-1]['close'] if data else 0

        engine = MarketScoringEngine()
        avg_vol = df['Volume'].tail(20).mean() if len(df) >= 20 else 1.0
        vol_rat = (df['Volume'].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
        
        if ticker in engine.nav_bases: state = engine.score_nav_asset(ticker, last_price, vol_rat)
        else:
            df_full = yf.Ticker(ticker).history(period="1y")
            state = engine.score_equity(df_full, last_price)

        name = engine.asset_names.get(ticker)
        if not name:
            try: name = t_obj.info.get('shortName', ticker)
            except: name = ticker

        shares_owned = portfolio_store.get_shares(ticker)

        is_lse_pence = ticker.endswith('.L') and last_price > 100
        cost_per_share = last_price / 100.0 if is_lse_pence else last_price
        current_value_owned = round(shares_owned * cost_per_share, 2)

        if shares_owned > 0:
            if 'holdings' not in ud: ud['holdings'] = {}
            ud['holdings'][ticker] = {'shares': shares_owned, 'manual_val': current_value_owned}
        else:
            ud.get('holdings', {}).pop(ticker, None)

        if is_lse_pence: price_display = f"{last_price:.2f}p (£{(last_price / 100.0):.2f})"
        else: price_display = f"£{last_price:.2f}"

        total_portfolio_owned = portfolio_store.get_total_portfolio_value()
        master_budget = ud.get('master_budget', 10000.0)
        budget_remaining = round(master_budget - total_portfolio_owned, 2)

        metrics = {
            'price': last_price, 'price_display': price_display, 'currency_symbol': "£",
            'discount': state['discount'], 'buy_score': f"{state['score']} / 100", 'tranches': state['tranches'],
            'status': state['status'], 'color': state['color'], 'reason': state['reason'],
            'action_main': state['action_main'], 'action_sub': state['action_sub'], 'action_color': state['action_color'],
            'shares_owned': shares_owned, 'value_owned': current_value_owned, 'master_budget': master_budget,
            'total_portfolio_owned': total_portfolio_owned, 'budget_remaining': budget_remaining
        }

        return jsonify({'ohlc': data, 'metrics': metrics, 'name': name, 'portfolio': ud})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

HTML_FRONTEND = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pro Stock Terminal</title>
    <script src="https://unpkg.com/lightweight-charts@3.8.0/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { font-family: -apple-system, sans-serif; background: #0f1115; color: #e1e3e6; margin: 0; display: flex; height: 100vh; overflow: hidden; }
        
        .sidebar { width: 300px; background: #171a21; border-right: 1px solid #262b36; display: flex; flex-direction: column; flex-shrink: 0; }
        .sidebar-top { padding: 15px; flex: 1; display: flex; flex-direction: column; overflow-y: auto; }
        .sidebar-bottom { padding: 15px; border-top: 1px solid #262b36; background: #1a1d24; flex-shrink: 0; }
        
        .right-drawer { width: 280px; background: #171a21; border-left: 1px solid #262b36; display: flex; flex-direction: column; padding: 15px; flex-shrink: 0; }
        .right-drawer h2 { font-size: 12px; color: #787e8e; margin-top: 10px; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px; }

        .drawer-card { background: #1a1d24; border: 1px solid #262b36; border-radius: 8px; padding: 12px; margin-bottom: 10px; text-align: center; }
        .drawer-card h3 { font-size: 10px; color: #787e8e; margin: 0 0 6px 0; text-transform: uppercase; }

        .total-shares-box { background: #1e222d; padding: 12px; border-radius: 8px; border: 1px solid #00d2ff; margin-bottom: 10px; text-align: center; }
        .total-shares-box label { font-size: 10px; color: #00d2ff; text-transform: uppercase; font-weight: bold; display: block; margin-bottom: 4px; }
        .total-shares-box div { font-size: 18px; font-weight: bold; color: #fff; }

        .user-profile-box { background: #1a1d24; padding: 10px 12px; border-radius: 8px; border: 1px solid #262b36; margin-bottom: 10px; }
        .user-profile-box label { font-size: 10px; color: #00d2ff; text-transform: uppercase; font-weight: bold; }
        .user-btn { padding: 3px 8px; font-size: 10px; font-weight: bold; border: none; border-radius: 4px; cursor: pointer; }
        .user-btn.new { background: #00d2ff; color: #000; }
        .user-btn.del { background: #ff3d00; color: #fff; }

        .master-budget-box { background: #171a21; padding: 12px; border-radius: 8px; border: 1px solid #262b36; margin-bottom: 15px; }
        .master-budget-box label { font-size: 11px; color: #787e8e; text-transform: uppercase; font-weight: bold; display: block; margin-bottom: 6px; }
        .master-budget-box input { width: 100%; background: #0f1115; border: 1px solid #262b36; color: #fff; padding: 8px; border-radius: 6px; font-weight: bold; font-size: 15px; }
        .budget-sub-stats { margin-top: 10px; padding-top: 8px; border-top: 1px solid #262b36; display: flex; flex-direction: column; gap: 6px; font-size: 11px; }
        .budget-sub-stats div { display: flex; justify-content: space-between; color: #787e8e; }
        .budget-sub-stats b { color: #e1e3e6; }

        .directives-box { background: #1e222d; padding: 12px; border-radius: 8px; border: 1px solid #262b36; margin-bottom: 12px; }
        .directives-box h3 { font-size: 11px; color: #00c853; margin: 0 0 8px 0; text-transform: uppercase; letter-spacing: 0.5px; }
        .directive-item { background: #0f1115; border: 1px solid #262b36; border-radius: 6px; padding: 8px; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; }
        .directive-info { font-size: 11px; color: #fff; }
        .directive-info b { color: #00d2ff; }
        .directive-btn { background: #00c853; color: #fff; border: none; font-weight: bold; padding: 4px 8px; border-radius: 4px; font-size: 10px; cursor: pointer; }
        .directive-btn:hover { background: #00e676; }
        .directive-btn.sell { background: #ff3d00; }
        .directive-btn.sell:hover { background: #ff5252; }

        .sidebar h2 { font-size: 12px; color: #787e8e; margin-top: 0; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
        .search-box { display: flex; gap: 8px; margin-bottom: 15px; }
        .search-box input { flex: 1; background: #0f1115; border: 1px solid #262b36; color: #fff; padding: 8px; border-radius: 6px; }
        .search-box button { background: #00d2ff; border: none; color: #000; font-weight: bold; padding: 8px 12px; border-radius: 6px; cursor: pointer; }
        
        .watchlist { flex: 1; list-style: none; padding: 0; margin: 0; }
        .watchlist-item { padding: 10px 12px; border-radius: 6px; background: #1e222d; margin-bottom: 8px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; border: 1px solid transparent; }
        .watchlist-item:hover, .watchlist-item.active { border-color: #00d2ff; background: #252a37; }
        .btn-delete { background: none; border: none; color: #ff4a4a; font-weight: bold; cursor: pointer; padding: 0 5px; }
        
        .main-content { flex: 1; display: flex; flex-direction: column; padding: 15px; overflow-y: auto; position: relative; }
        .top-nav { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; background: #171a21; padding: 10px 16px; border-radius: 10px; border: 1px solid #262b36; }
        .controls { display: flex; gap: 8px; align-items: center; flex-wrap: nowrap; white-space: nowrap; }
        select, button.btn-control { background: #0f1115; border: 1px solid #262b36; color: #fff; padding: 6px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; white-space: nowrap; }
        select:hover, button.btn-control:hover { border-color: #00d2ff; }
        button.btn-alert { background: #0f1115; border: 1px solid #ff9900; color: #ff9900; font-weight: bold; }
        button.btn-alert:hover { background: #ff9900; color: #000; }
        button.btn-refresh { background: #0f1115; border: 1px solid #00d2ff; color: #00d2ff; font-weight: bold; }
        button.btn-refresh:hover { background: #00d2ff; color: #000; }
        button.btn-reset { background: #0f1115; border: 1px solid #ff4a4a; color: #ff4a4a; font-weight: bold; }
        button.btn-reset:hover { background: #ff4a4a; color: #fff; }
        button.btn-rec { background: #00d2ff; color: #000; font-weight: bold; width: 100%; display: flex; align-items: center; justify-content: center; gap: 8px; padding: 10px; border: none; border-radius: 6px; cursor: pointer; margin-top: 10px;}
        
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 12px; }
        .card { background: #171a21; padding: 12px; border-radius: 10px; border: 1px solid #262b36; text-align: center; transition: 0.2s; }
        .card h3 { font-size: 10px; color: #787e8e; margin: 0 0 6px 0; text-transform: uppercase; }
        .card p { font-size: 15px; font-weight: bold; margin: 0; color: #fff; }
        .card input { width: 100%; background: #0f1115; border: 1px solid #262b36; color: #00d2ff; padding: 4px; border-radius: 4px; font-weight: bold; font-size: 14px; text-align: center; }
        .val-highlight { color: #00d2ff; }
        .clickable-card { cursor: pointer; border: 1px solid #363c4a; }
        .clickable-card:hover { border-color: #00d2ff; background: #1e222d; }
        
        .btn-execute { background: #00c853; color: #fff; border: none; font-weight: bold; padding: 6px 10px; border-radius: 6px; cursor: pointer; width: 100%; font-size: 11px; margin-top: 4px; }
        .btn-execute:hover { background: #00e676; }
        .btn-execute.sell-btn { background: #ff3d00; }
        .btn-execute.sell-btn:hover { background: #ff5252; }

        .chart-container { flex: 1; background: #171a21; padding: 10px; border-radius: 10px; border: 1px solid #262b36; position: relative; }
        #tvChart { position: absolute; top: 10px; left: 10px; right: 10px; bottom: 10px; }
        #loader { display: none; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: #00d2ff; font-weight: bold; z-index: 10; background: rgba(23, 26, 33, 0.9); padding: 10px 20px; border-radius: 8px; border: 1px solid #00d2ff; }

        .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 100; justify-content: center; align-items: center; }
        .modal { background: #171a21; width: 600px; padding: 25px; border-radius: 12px; border: 1px solid #262b36; box-shadow: 0 10px 30px rgba(0,0,0,0.5); max-height: 90vh; overflow-y: auto; }
        .modal h2 { margin-top: 0; color: #fff; font-size: 18px; border-bottom: 1px solid #262b36; padding-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
        
        .rec-item { background: #1e222d; border: 1px solid #262b36; border-radius: 8px; padding: 15px; margin-bottom: 15px; }
        .rec-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }
        .rec-title-block { display: flex; flex-direction: column; gap: 4px; }
        .rec-title-block h3 { margin: 0; color: #fff; font-size: 16px; }
        .rec-score { background: rgba(0, 210, 255, 0.1); color: #00d2ff; font-weight: bold; padding: 4px 8px; border-radius: 4px; font-size: 12px; border: 1px solid #00d2ff; height: 26px; display: flex; align-items: center; }
        .rec-desc { font-size: 13px; color: #8a8a9e; line-height: 1.4; margin-bottom: 15px; }
        .btn-load { background: #00c853; border: none; color: #fff; font-weight: bold; padding: 8px 12px; border-radius: 6px; cursor: pointer; width: 100%; font-size: 13px; }
        .btn-load:hover { background: #00e676; }

        .history-list { list-style: none; padding: 0; margin: 0; overflow-y: auto; flex: 1; }
        .history-item { background: #1e222d; border: 1px solid #262b36; border-radius: 6px; padding: 10px; margin-bottom: 8px; position: relative; }
        .history-item .h-action { font-weight: bold; font-size: 12px; }
        .history-item .h-buy { color: #00c853; }
        .history-item .h-sell { color: #ff3d00; }
        .history-item .h-meta { font-size: 11px; color: #787e8e; margin-top: 4px; }
        .history-item .btn-undo { position: absolute; top: 8px; right: 8px; background: none; border: none; color: #ff4a4a; font-weight: bold; cursor: pointer; }

        .pagination-row { display:none; justify-content:space-between; align-items:center; margin-top:15px; border-top:1px solid #262b36; padding-top:15px; }

        .quick-check-box { background: #1e222d; padding: 12px; border-radius: 8px; border: 1px solid #262b36; margin-bottom: 10px; }
        .qc-header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .qc-header-row h3 { font-size: 11px; color: #787e8e; margin: 0; text-transform: uppercase; }
        .qc-close-btn { color: #787e8e; cursor: pointer; font-size: 12px; font-weight: bold; padding: 0 4px; }
        .qc-close-btn:hover { color: #fff; }
        .qc-input-row { display: flex; gap: 8px; }
        .qc-input-row input { flex: 1; background: #0f1115; border: 1px solid #262b36; color: #fff; padding: 6px 8px; border-radius: 6px; font-size: 12px; }
        .qc-input-row button { background: #333947; border: 1px solid #787e8e; color: #fff; border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; }
        .qc-input-row button:hover { background: #4a5265; }
        #qsResult { display: none; margin-top: 10px; font-size: 12px; background: #0f1115; padding: 10px; border-radius: 6px; border: 1px solid #262b36; }

        /* --- MOBILE RESPONSIVE LAYOUT --- */
        @media (max-width: 900px) {
            body { display: block; overflow-y: auto; overflow-x: hidden; height: auto; }
            
            .sidebar { width: 100%; border-right: none; display: block; }
            .sidebar-top { overflow-y: visible; padding-bottom: 0; }
            .sidebar-bottom { border-top: none; padding-top: 5px; }
            
            .main-content { width: 100%; overflow-y: visible; display: block; padding-top: 5px; }
            
            .right-drawer { width: 100%; border-left: none; border-top: 1px solid #262b36; display: block; }
            
            .top-nav { flex-direction: column; align-items: stretch; gap: 12px; padding: 12px; }
            .controls { flex-wrap: wrap; justify-content: space-between; gap: 8px; white-space: normal; }
            .controls > label { display: none; /* Hide labels on mobile to save space */ }
            select, button.btn-control { flex: 1 1 30%; font-size: 11px; padding: 8px 6px; text-align: center; }
            
            .chart-container { height: 400px; margin-top: 10px; flex: none; }
            .grid { grid-template-columns: repeat(2, 1fr); }
            
            .modal { width: 95%; padding: 15px; }
            .alert-section > div { flex-wrap: wrap; flex-direction: column; }
            .alert-section label { width: 100%; }
        }
    </style>
</head>
<body>
    <div class="modal-overlay" id="recModal">
        <div class="modal">
            <h2>
                <span>Market Anomaly Screener</span>
                <button class="btn-cancel" style="padding:4px 8px; font-size:12px" onclick="document.getElementById('recModal').style.display='none'">Close</button>
            </h2>
            <div id="recLoader" style="text-align:center; padding: 20px; color:#00d2ff;">Scanning global assets for anomalies...</div>
            <div id="recContent"></div>
            
            <div class="pagination-row" id="recPagination">
                <button id="btnPrevPage" class="btn-cancel" onclick="changeRecPage(-1)" style="padding: 6px 12px; font-size:12px;">← Previous 5</button>
                <span id="pageInfo" style="font-size:12px; color:#8a8a9e;"></span>
                <button id="btnNextPage" class="btn-cancel" onclick="changeRecPage(1)" style="padding: 6px 12px; font-size:12px;">Next 5 →</button>
            </div>
        </div>
    </div>

    <!-- ALERTS MODAL -->
    <div class="modal-overlay" id="alertModal">
        <div class="modal">
            <h2><span id="modalTitle">Configure Alerts</span></h2>
            
            <div class="alert-section" id="smartAlertsSection" style="margin-bottom:20px; padding-bottom:15px; border-bottom:1px solid #262b36;">
                <h3 style="font-size:13px; color:#00d2ff; margin-bottom:15px; text-transform:uppercase;">Smart Tranche Alerts</h3>
                <div style="display:flex; align-items:flex-start; margin-bottom:12px; font-size:14px;">
                    <input type="checkbox" id="chkBuy" style="margin-right:12px; margin-top:3px;">
                    <div><b style="color:#fff;">Anomaly Buy Signal</b><span style="color:#8a8a9e; font-size:12px; display:block;">Alert me when mathematical deviation dictates scaling into next tranche.</span></div>
                </div>
                <div style="display:flex; align-items:flex-start; margin-bottom:12px; font-size:14px;">
                    <input type="checkbox" id="chkSell" style="margin-right:12px; margin-top:3px;">
                    <div><b style="color:#fff;">Take Profit Target</b><span style="color:#8a8a9e; font-size:12px; display:block;">Alert me when baseline discount shrinks and premium returns.</span></div>
                </div>
            </div>

            <div class="alert-section" style="margin-bottom:20px; padding-bottom:15px; border-bottom:1px solid #262b36;">
                <h3 style="font-size:13px; color:#00d2ff; margin-bottom:15px; text-transform:uppercase;">Manual Price Overrides</h3>
                <div style="display:flex; gap:15px;">
                    <label style="display:flex; flex-direction:column; font-size:12px; color:#787e8e; gap:6px; flex:1;">
                        <div style="display:flex; align-items:center; gap:6px;"><input type="checkbox" id="chkManualBuy"> Buy below price (£/p):</div>
                        <input type="number" id="manualBuyPrice" placeholder="0.00" style="background:#0f1115; border:1px solid #262b36; color:#fff; padding:10px; border-radius:6px; width:100%;">
                    </label>
                    <label style="display:flex; flex-direction:column; font-size:12px; color:#787e8e; gap:6px; flex:1;">
                        <div style="display:flex; align-items:center; gap:6px;"><input type="checkbox" id="chkManualSell"> Sell above price (£/p):</div>
                        <input type="number" id="manualSellPrice" placeholder="0.00" style="background:#0f1115; border:1px solid #262b36; color:#fff; padding:10px; border-radius:6px; width:100%;">
                    </label>
                </div>
            </div>

            <div class="modal-actions" style="display:flex; justify-content:flex-end; gap:10px;">
                <button class="btn-cancel" onclick="document.getElementById('alertModal').style.display='none'">Cancel</button>
                <button class="btn-save" style="background:#00d2ff; border:none; color:#000; font-weight:bold; padding:10px 18px; border-radius:6px; cursor:pointer;" onclick="saveAlerts()">Save Alerts</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="reasonModal">
        <div class="modal" style="width: 420px;">
            <h2>Anomaly Breakdown <button class="btn-cancel" style="padding:4px 8px; font-size:12px" onclick="document.getElementById('reasonModal').style.display='none'">Close</button></h2>
            <p id="reasonText" style="color: #e1e3e6; line-height: 1.6; font-size: 14px; margin-top: 10px;"></p>
        </div>
    </div>

    <!-- LEFT SIDEBAR -->
    <div class="sidebar">
        <div class="sidebar-top">
            <div class="user-profile-box">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                    <label>Active Profile</label>
                    <div style="display:flex; gap:4px;">
                        <button onclick="addUserPrompt()" class="user-btn new">+ New</button>
                        <button onclick="deleteUserPrompt()" class="user-btn del">Delete</button>
                    </div>
                </div>
                <select id="userSelect" onchange="onUserChange(this.value)" style="width:100%; background:#0f1115; color:#fff; border:1px solid #262b36; padding:6px; border-radius:6px; font-size:13px; font-weight:bold;"></select>
            </div>

            <div class="total-shares-box">
                <label>Current Value of All Shares Held</label>
                <div id="mTotalSharesHeldVal">£0.00</div>
            </div>

            <div class="master-budget-box">
                <label>Master Portfolio Budget (£)</label>
                <input type="number" id="masterBudgetInput" value="10000" oninput="onMasterBudgetInput()">
                <div class="budget-sub-stats">
                    <div><span>Budget Remaining:</span> <b id="mBudgetRemaining" style="color:#00c853;">£10,000.00</b></div>
                </div>
            </div>

            <h2>Live Watchlist</h2>
            <div class="search-box">
                <input type="text" id="addTickerInput" placeholder="Add Symbol...">
                <button id="addBtn">+</button>
            </div>
            <ul class="watchlist" id="watchlistUI"></ul>
        </div>
        
        <div class="sidebar-bottom">
            <!-- PORTFOLIO ACTION DIRECTIVES -->
            <div class="directives-box">
                <h3>Portfolio Action Directives</h3>
                <div id="directivesList" style="max-height: 120px; overflow-y: auto;">
                    <div style="font-size:11px; color:#787e8e; text-align:center; padding:5px;">Scanning portfolio...</div>
                </div>
            </div>

            <div class="quick-check-box">
                <div class="qc-header-row">
                    <h3>Instant Anomaly Check</h3>
                    <span class="qc-close-btn" onclick="closeQuickScore()">✕</span>
                </div>
                <div class="qc-input-row">
                    <input type="text" id="qsTicker" placeholder="e.g. SSLN.L or MSFT">
                    <button onclick="checkQuickScore()">Score</button>
                </div>
                <div id="qsResult"></div>
            </div>
            
            <button class="btn-rec" onclick="openRecommendModal()">
                Search for recommendations
            </button>
        </div>
    </div>
    
    <!-- MAIN CONTENT -->
    <div class="main-content">
        <div class="top-nav">
            <h2 id="activeTitle" style="margin:0;">No Stock Loaded</h2>
            <div class="controls">
                <label>Range:</label>
                <select id="periodSelect" onchange="saveUISettings(); updateIntervals(); fetchData(false);">
                    <option value="1d">1 Day</option>
                    <option value="5d">5 Days</option>
                    <option value="1mo" selected>1 Month</option>
                    <option value="6mo">6 Months</option>
                    <option value="1y">1 Year</option>
                    <option value="5y">5 Years</option>
                    <option value="max">Max</option>
                </select>
                <label>Interval:</label>
                <select id="intervalSelect" onchange="saveUISettings(); fetchData(false);"></select>
                <label>Style:</label>
                <select id="styleSelect" onchange="saveUISettings(); renderChart();">
                    <option value="candlestick">Candlestick</option>
                    <option value="heikin-ashi">Heikin-Ashi</option>
                    <option value="line">Line</option>
                    <option value="area">Area</option>
                    <option value="bar">Bar</option>
                </select>
                <label>Auto Update:</label>
                <select id="refreshSelect" onchange="saveUISettings(); setupAutoRefresh();">
                    <option value="0">Manual</option>
                    <option value="5000">5 secs</option>
                    <option value="10000" selected>10 secs</option>
                    <option value="30000">30 secs</option>
                    <option value="60000">1 min</option>
                    <option value="300000">5 mins</option>
                </select>

                <button class="btn-control btn-refresh" onclick="fetchData(false)">Refresh</button>
                <button class="btn-control btn-alert" onclick="openAlertModal(false)">Setup Alerts</button>
                <button class="btn-control btn-reset" onclick="resetTerminalData()">Reset</button>
            </div>
        </div>
        
        <div class="grid">
            <div class="card"><h3>Live Price</h3><p id="mPrice" class="val-highlight">--</p></div>
            <div class="card"><h3>Math Discount</h3><p id="mDisc" class="val-highlight">--</p></div>
            <div class="card"><h3>Buy Score</h3><p id="mBuy" class="val-highlight">--</p></div>
            <div class="card"><h3>Active Tranches</h3><p id="mTranches" class="val-highlight">--</p></div>
            <div class="card">
                <h3>Current Value Owned (£)</h3>
                <input type="number" id="inputHeldVal" value="0" oninput="onValOwnedInput()">
                <span id="mSharesOwned" style="font-size:10px; color:#787e8e; display:block; margin-top:4px;">0 shares</span>
            </div>
            <div class="card clickable-card" onclick="showAnomalyReason()">
                <h3>Macro Status ⓘ</h3>
                <p id="mMacro" style="font-size:12px; font-weight:bold; display:flex; justify-content:center; align-items:center; gap:6px;">--</p>
            </div>
        </div>
        
        <div class="chart-container">
            <div id="loader">Fetching...</div>
            <div id="tvChart"></div>
        </div>
    </div>

    <!-- RIGHT DRAWER: STACKED ACTION CARDS & TRADE HISTORY LOG -->
    <div class="right-drawer">
        <div class="drawer-card" id="actionCard">
            <h3>Recommended Action</h3>
            <p id="mActionMain" style="font-size:18px; font-weight:bold;">--</p>
            <span id="mActionSub" style="font-size:11px; color:#8a8a9e; font-weight:normal; display:block; margin-bottom:2px;"></span>
            <div id="actionBtnContainer" style="margin-top:2px;"></div>
        </div>

        <div class="drawer-card">
            <h3>Recommended Rec.</h3>
            <p id="mRecTradeAmt" style="color:#00c853; font-size:15px; font-weight:bold;">£0.00</p>
            <span id="mRecShares" style="font-size:10px; color:#787e8e; display:block; margin-top:2px;">0 shares</span>
        </div>

        <h2>Trade Log History</h2>
        <ul class="history-list" id="historyUI"></ul>
    </div>

    <script>
        let currentTicker = '';
        let tvChart = null; let tvSeries = null; let masterData = [];
        let currentAnomalyReason = "Loading...";
        let currentLivePrice = 0;
        let currentActiveTranches = 0;
        let currentSharesOwned = 0;
        let currentValOwned = 0;
        let globalPortfolioData = { master_budget: 10000, history: [], holdings: {}, watchlist: [], settings: {} };
        let autoRefreshTimer = null;
        let isSettingsLoaded = false;

        async function fetchUsers() {
            try {
                let res = await fetch('/api/users');
                let data = await res.json();
                let sel = document.getElementById('userSelect');
                sel.innerHTML = '';
                (data.users || []).forEach(u => {
                    let opt = document.createElement('option');
                    opt.value = u;
                    opt.innerText = u;
                    if (u === data.active_user) opt.selected = true;
                    sel.appendChild(opt);
                });
            } catch(e) {}
        }

        async function onUserChange(username) {
            if (!username) return;
            await fetch('/api/users/select', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: username})
            });
            currentTicker = '';
            isSettingsLoaded = false;
            await fetchUsers();
            fetchData(false);
        }

        async function addUserPrompt() {
            let name = prompt("Enter name for the new profile:");
            if (!name) return;
            name = name.trim();
            if (!name) return;
            await fetch('/api/users/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: name})
            });
            currentTicker = '';
            isSettingsLoaded = false;
            await fetchUsers();
            fetchData(false);
        }

        async function deleteUserPrompt() {
            let activeUser = document.getElementById('userSelect').value;
            if (!activeUser) return;
            if (confirm(`Are you sure you want to delete profile "${activeUser}"? This cannot be undone.`)) {
                let res = await fetch('/api/users/delete', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username: activeUser})
                });
                let data = await res.json();
                if (data.status === 'error') {
                    alert("Cannot delete the last remaining user profile.");
                    return;
                }
                currentTicker = '';
                isSettingsLoaded = false;
                await fetchUsers();
                fetchData(false);
            }
        }

        function triggerBrowserNotification(title, body) {
            if ("Notification" in window) {
                if (Notification.permission === "granted") {
                    new Notification(title, { body: body });
                } else if (Notification.permission !== "denied") {
                    Notification.requestPermission().then(permission => {
                        if (permission === "granted") {
                            new Notification(title, { body: body });
                        }
                    });
                }
            }
        }

        function saveUISettings() {
            let settings = {
                period: document.getElementById('periodSelect').value,
                interval: document.getElementById('intervalSelect').value,
                style: document.getElementById('styleSelect').value,
                refresh: document.getElementById('refreshSelect').value
            };
            fetch('/api/portfolio/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({settings: settings})
            });
        }

        async function resetTerminalData() {
            if (confirm("Reset all portfolio data, trade logs, and position baselines for this user?")) {
                await fetch('/api/portfolio/reset', { method: 'POST' });
                currentTicker = '';
                isSettingsLoaded = false;
                fetchData(false);
            }
        }

        function setupAutoRefresh() {
            if (autoRefreshTimer) clearInterval(autoRefreshTimer);
            let intervalMs = parseInt(document.getElementById('refreshSelect').value) || 0;
            if (intervalMs > 0) {
                autoRefreshTimer = setInterval(() => {
                    fetchData(true); // Silent auto-update
                }, intervalMs);
            }
        }

        function renderWatchlist(watchlist) {
            let ul = document.getElementById('watchlistUI');
            ul.innerHTML = '';
            let list = watchlist || [];
            if (list.length === 0) {
                ul.innerHTML = '<li style="font-size:11px; color:#787e8e; text-align:center; padding:10px;">Watchlist is empty</li>';
                return;
            }
            list.forEach(symbol => {
                let li = document.createElement('li');
                li.className = `watchlist-item ${symbol === currentTicker ? 'active' : ''}`;
                li.setAttribute('data-symbol', symbol);
                li.innerHTML = `<span class="ticker">${symbol}</span><button class="btn-delete">✕</button>`;
                ul.appendChild(li);
            });
        }

        function openAlertModal(isPreFilled) {
            if (!currentTicker) return;
            document.getElementById('modalTitle').innerText = `Configure Alerts: ${currentTicker}`;
            if ("Notification" in window && Notification.permission !== "granted") {
                Notification.requestPermission();
            }
            document.getElementById('alertModal').style.display = 'flex';
        }

        function saveAlerts() {
            document.getElementById('alertModal').style.display = 'none';
            if (currentTicker) {
                triggerBrowserNotification("Alerts Configured", `Monitoring ${currentTicker} anomalies actively.`);
            }
        }

        function onMasterBudgetInput() {
            let val = parseFloat(document.getElementById('masterBudgetInput').value) || 10000;
            globalPortfolioData.master_budget = val;
            calculateSizing();
            fetch('/api/portfolio/budget', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({budget: val})
            }).then(() => fetchDirectives());
        }

        function onValOwnedInput() {
            if (!currentTicker) return;
            let val = parseFloat(document.getElementById('inputHeldVal').value) || 0;
            
            let isLsePence = currentTicker.endsWith('.L') && currentLivePrice > 100;
            let pricePerShare = isLsePence ? (currentLivePrice / 100.0) : currentLivePrice;
            currentSharesOwned = pricePerShare > 0 ? Math.round(val / pricePerShare) : 0;
            currentValOwned = roundTwo(currentSharesOwned * pricePerShare);

            document.getElementById('mSharesOwned').innerText = `${currentSharesOwned} shares`;

            if (!globalPortfolioData.holdings) globalPortfolioData.holdings = {};
            if (currentSharesOwned > 0) {
                globalPortfolioData.holdings[currentTicker] = {
                    shares: currentSharesOwned,
                    manual_val: currentValOwned
                };
            } else {
                delete globalPortfolioData.holdings[currentTicker];
            }

            calculateSizing();

            fetch('/api/portfolio/holding', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    ticker: currentTicker,
                    value_owned: val,
                    price: currentLivePrice
                })
            }).then(() => fetchDirectives());
        }

        function roundTwo(num) {
            return Math.round((num + Number.EPSILON) * 100) / 100;
        }

        function calculateSizing() {
            let totalBudget = globalPortfolioData.master_budget || 10000;
            
            let totalOwnedAll = 0;
            if (globalPortfolioData.holdings && globalPortfolioData.watchlist) {
                for (let t of globalPortfolioData.watchlist) {
                    if (globalPortfolioData.holdings[t]) {
                        totalOwnedAll += (globalPortfolioData.holdings[t].manual_val || 0);
                    }
                }
            }
            let remaining = totalBudget - totalOwnedAll;

            let trancheTargetVal = (currentActiveTranches / 5.0) * totalBudget;
            let netDiffVal = trancheTargetVal - currentValOwned;

            let isLsePence = currentTicker.endsWith('.L') && currentLivePrice > 100;
            let costPerShare = isLsePence ? (currentLivePrice / 100.0) : currentLivePrice;

            let recAmtText = "£0.00";
            let recSharesText = "0 shares";
            let btnContainer = document.getElementById('actionBtnContainer');
            btnContainer.innerHTML = "";

            let actionMain = document.getElementById('mActionMain');
            let actionSub = document.getElementById('mActionSub');

            if (!currentTicker) {
                actionMain.innerText = "NO ASSET";
                actionMain.style.color = "#787e8e";
                actionSub.innerText = "";
            } else {
                let buyShares = 0;
                if (netDiffVal > 5 && currentActiveTranches > 0 && costPerShare > 0) {
                    let allowedSpend = Math.min(netDiffVal, Math.max(0, remaining));
                    buyShares = Math.floor(allowedSpend / costPerShare);
                }
                
                let sellShares = 0;
                if (currentValOwned > 0 && (currentActiveTranches === 0 || netDiffVal < -5)) {
                    let excessVal = Math.abs(netDiffVal);
                    sellShares = currentActiveTranches === 0 ? currentSharesOwned : Math.min(currentSharesOwned, Math.floor(excessVal / costPerShare));
                }

                if (buyShares > 0) {
                    actionMain.innerText = "BUY";
                    actionMain.style.color = "#00c853";
                    actionSub.innerText = `(Tranche ${currentActiveTranches})`;

                    let buyAmount = buyShares * costPerShare;
                    recAmtText = `+£${buyAmount.toFixed(2)}`;
                    recSharesText = `Buy ${buyShares} shares`;
                    
                    btnContainer.innerHTML = `<button class="btn-execute" onclick="executeTradeDirect('${currentTicker}', 'BUY', ${buyShares}, ${currentLivePrice})">Confirm Buy (${buyShares} Shs)</button>`;
                } else if (sellShares > 0) {
                    actionMain.innerText = "SELL";
                    actionMain.style.color = "#ff3d00";
                    actionSub.innerText = "(Take Profit)";

                    let sellAmount = sellShares * costPerShare;
                    recAmtText = `-£${sellAmount.toFixed(2)}`;
                    recSharesText = `Sell ${sellShares} shares`;

                    btnContainer.innerHTML = `<button class="btn-execute sell-btn" onclick="executeTradeDirect('${currentTicker}', 'SELL', ${sellShares}, ${currentLivePrice})">Confirm Sell (${sellShares} Shs)</button>`;
                } else {
                    actionMain.innerText = "HOLD / WAIT";
                    actionMain.style.color = "#8a8a9e";
                    if (netDiffVal > 5 && remaining <= 0) {
                        actionSub.innerText = `(Insufficient Budget)`;
                        actionMain.style.color = "#ff9900";
                    } else if (currentActiveTranches > 0 && currentValOwned > 0) {
                        actionSub.innerText = `(Tranche ${currentActiveTranches} Filled)`;
                    } else {
                        actionSub.innerText = `(Tranche ${currentActiveTranches})`;
                    }
                }
            }

            document.getElementById('mRecTradeAmt').innerText = recAmtText;
            document.getElementById('mRecShares').innerText = recSharesText;

            document.getElementById('mTotalSharesHeldVal').innerText = `£${totalOwnedAll.toFixed(2)}`;
            document.getElementById('mBudgetRemaining').innerText = `£${remaining.toFixed(2)}`;
            if (remaining < 0) {
                document.getElementById('mBudgetRemaining').style.color = "#ff3d00";
            } else {
                document.getElementById('mBudgetRemaining').style.color = "#00c853";
            }
        }

        async function executeTradeDirect(ticker, action, shares, price) {
            if (shares <= 0 || !ticker) return;
            await fetch('/api/trade/execute', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    ticker: ticker, action: action, shares: shares, price: price
                })
            });
            fetchData(false);
        }

        async function fetchDirectives() {
            try {
                let res = await fetch(`/api/directives`);
                let data = await res.json();
                renderDirectives(data.directives || []);
            } catch(e) {}
        }

        function renderDirectives(directives) {
            let container = document.getElementById('directivesList');
            container.innerHTML = "";
            if (directives.length === 0) {
                container.innerHTML = `<div style="font-size:11px; color:#787e8e; text-align:center; padding:5px;">All positions aligned with target tranches.</div>`;
                return;
            }

            directives.forEach(d => {
                let isBuy = d.action === 'BUY';
                let div = document.createElement('div');
                div.className = 'directive-item';
                div.innerHTML = `
                    <div class="directive-info">
                        <b>${d.ticker}</b>: ${d.action} ${d.shares} shs (£${d.amount.toFixed(2)})
                    </div>
                    <button class="directive-btn ${isBuy ? '' : 'sell'}" onclick="executeTradeDirect('${d.ticker}', '${d.action}', ${d.shares}, ${d.price})">
                        Confirm
                    </button>
                `;
                container.appendChild(div);
            });
        }

        async function undoTrade(tradeId) {
            await fetch('/api/trade/undo', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: tradeId})
            });
            fetchData(false);
        }

        function renderTradeHistory() {
            let ui = document.getElementById('historyUI');
            ui.innerHTML = "";
            if (!currentTicker) {
                ui.innerHTML = `<li style="font-size:11px; color:#787e8e; text-align:center; padding:20px;">No stock loaded.</li>`;
                return;
            }
            let history = globalPortfolioData.history || [];
            let tickerHistory = history.filter(h => h.ticker === currentTicker);

            if (tickerHistory.length === 0) {
                ui.innerHTML = `<li style="font-size:11px; color:#787e8e; text-align:center; padding:20px;">No trades logged for ${currentTicker}.</li>`;
                return;
            }

            tickerHistory.forEach(item => {
                let li = document.createElement('li');
                li.className = 'history-item';
                let isBuy = item.action === 'BUY';
                li.innerHTML = `
                    <span class="btn-undo" onclick="undoTrade('${item.id}')">✕</span>
                    <div class="h-action ${isBuy ? 'h-buy' : 'h-sell'}">${item.action} ${item.shares} Shares</div>
                    <div class="h-meta">Total: £${item.amount.toFixed(2)} @ ${item.price}</div>
                    <div class="h-meta" style="font-size:9px; color:#525866;">${item.time}</div>
                `;
                ui.appendChild(li);
            });
        }

        function showAnomalyReason() {
            if (!currentAnomalyReason) return;
            document.getElementById('reasonText').innerText = currentAnomalyReason;
            document.getElementById('reasonModal').style.display = 'flex';
        }

        function closeQuickScore() {
            document.getElementById('qsResult').style.display = 'none';
            document.getElementById('qsTicker').value = '';
        }
        
        async function checkQuickScore() {
            let t = document.getElementById('qsTicker').value.trim().toUpperCase();
            if(!t) return;
            let resDiv = document.getElementById('qsResult');
            resDiv.style.display = 'block';
            resDiv.innerHTML = '<span style="color:#00d2ff">Calculating anomaly engine...</span>';
            
            try {
                let res = await fetch(`/api/score?t=${t}`);
                let data = await res.json();
                if(data.error) { resDiv.innerHTML = `<span style="color:#ff4a4a">Error: ${data.error}</span>`; return; }
                
                let recStatus = data.score >= 60 ? '<span style="color:#00c853; font-weight:bold;">Strong Anomaly</span>' : (data.score >= 40 ? '<span style="color:#ff9900; font-weight:bold;">Moderate Anomaly</span>' : '<span style="color:#ff4a4a; font-weight:bold;">No Anomaly</span>');
                
                resDiv.innerHTML = `
                    <div style="margin-bottom:6px; color:#fff;"><b>${data.name}</b> <span style="color:#00d2ff">(${data.ticker})</span></div>
                    <div style="display:flex; justify-content:space-between; margin-bottom:8px; padding-bottom:6px; border-bottom:1px solid #262b36;">
                        <span>Score: <b style="color:#00d2ff">${data.score}/100</b></span>
                        <span>${recStatus}</span>
                    </div>
                    <div style="color:#8a8a9e; margin-bottom:10px; line-height:1.4;">${data.reason}</div>
                    <button class="btn-load" style="padding:6px; font-size:11px;" onclick="loadRecommended('${data.ticker}', ${data.is_smart}, ${data.rec_buy}, ${data.rec_sell})">Load to Terminal</button>
                `;
            } catch(e) { resDiv.innerHTML = `<span style="color:#ff4a4a">Failed to fetch data.</span>`; }
        }

        let allRecommendations = [];
        let recCurrentPage = 0;
        const RECS_PER_PAGE = 5;

        async function openRecommendModal() {
            document.getElementById('recModal').style.display = 'flex';
            document.getElementById('recContent').innerHTML = '';
            document.getElementById('recPagination').style.display = 'none';
            document.getElementById('recLoader').style.display = 'block';

            try {
                let res = await fetch('/api/recommend');
                let data = await res.json();
                document.getElementById('recLoader').style.display = 'none';
                allRecommendations = data.recommendations;
                recCurrentPage = 0;
                renderRecPage();
            } catch (err) { document.getElementById('recLoader').innerText = "Error scanning market."; }
        }

        function renderRecPage() {
            let start = recCurrentPage * RECS_PER_PAGE;
            let end = start + RECS_PER_PAGE;
            let pageItems = allRecommendations.slice(start, end);
            
            let html = '';
            pageItems.forEach(rec => {
                html += `
                <div class="rec-item">
                    <div class="rec-header">
                        <div class="rec-title-block">
                            <h3>${rec.name} <span style="color:#00d2ff; font-weight:normal;">(${rec.ticker})</span></h3>
                            <div><span style="font-size:11px; background:#262b36; color:#e1e3e6; padding:2px 6px; border-radius:4px;">${rec.type}</span></div>
                        </div>
                        <span class="rec-score">Buy Score: ${rec.score}/100</span>
                    </div>
                    <div class="rec-desc"><b>Why we flagged it:</b> ${rec.reason}</div>
                    <button class="btn-load" onclick="loadRecommended('${rec.ticker}', ${rec.is_smart}, ${rec.rec_buy}, ${rec.rec_sell})">
                        Load Chart & Pre-fill Alerts
                    </button>
                </div>`;
            });
            document.getElementById('recContent').innerHTML = html;
            
            let totalPages = Math.ceil(allRecommendations.length / RECS_PER_PAGE);
            if (totalPages > 1) {
                document.getElementById('recPagination').style.display = 'flex';
                document.getElementById('pageInfo').innerText = `Page ${recCurrentPage + 1} of ${totalPages}`;
                document.getElementById('btnPrevPage').disabled = (recCurrentPage === 0);
                document.getElementById('btnNextPage').disabled = (end >= allRecommendations.length);
            }
        }

        function changeRecPage(dir) { recCurrentPage += dir; renderRecPage(); }

        async function loadRecommended(ticker, isSmart, recBuy, recSell) {
            document.getElementById('recModal').style.display = 'none';
            await fetch('/api/watchlist/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ticker: ticker})
            });
            selectStock(ticker);
            setTimeout(() => { openAlertModal(true); }, 500);
        }

        function initChart() {
            let container = document.getElementById('tvChart');
            tvChart = LightweightCharts.createChart(container, {
                width: container.clientWidth, height: container.clientHeight || 450,
                layout: { backgroundColor: '#171a21', textColor: '#787e8e' },
                grid: { vertLines: { color: '#262b36' }, horzLines: { color: '#262b36' } },
                crosshair: { mode: 0 }, timeScale: { borderColor: '#262b36', timeVisible: true }
            });
            window.addEventListener('resize', () => { tvChart.applyOptions({ width: container.clientWidth, height: container.clientHeight }); });
        }

        function updateIntervals() {
            let period = document.getElementById('periodSelect').value;
            let intervalSelect = document.getElementById('intervalSelect');
            let currentInterval = intervalSelect.value;
            
            intervalSelect.innerHTML = '';
            let options = [];

            if (period === '1d' || period === '5d') {
                options = [['1m', '1 Min'], ['5m', '5 Mins'], ['15m', '15 Mins'], ['30m', '30 Mins'], ['1h', '1 Hour']];
            } else if (period === '1mo') {
                options = [['5m', '5 Mins'], ['15m', '15 Mins'], ['30m', '30 Mins'], ['1h', '1 Hour'], ['1d', '1 Day']];
            } else {
                options = [['1d', '1 Day'], ['1wk', '1 Week'], ['1mo', '1 Month']];
            }

            options.forEach(opt => {
                let option = document.createElement('option');
                option.value = opt[0];
                option.text = opt[1];
                intervalSelect.appendChild(option);
            });

            if (Array.from(intervalSelect.options).some(o => o.value === currentInterval)) {
                intervalSelect.value = currentInterval;
            } else if (period === '1mo' || period === '6mo') {
                intervalSelect.value = '1d';
            }
        }

        function convertToHeikinAshi(ohlc) {
            let haData = [];
            for (let i = 0; i < ohlc.length; i++) {
                let curr = ohlc[i];
                let haClose = (curr.open + curr.high + curr.low + curr.close) / 4;
                let haOpen = (i === 0) ? (curr.open + curr.close) / 2 : (haData[i - 1].open + haData[i - 1].close) / 2;
                haData.push({ time: curr.time, open: haOpen, high: Math.max(curr.high, haOpen, haClose), low: Math.min(curr.low, haOpen, haClose), close: haClose });
            }
            return haData;
        }

        async function fetchData(isSilent = false) {
            if (!isSilent) {
                document.getElementById('loader').style.display = 'block';
            }
            try {
                let res = await fetch(`/api/data?t=${currentTicker}&p=${document.getElementById('periodSelect').value}&i=${document.getElementById('intervalSelect').value}`);
                let payload = await res.json();
                masterData = payload.ohlc;
                globalPortfolioData = payload.portfolio;

                if (!isSettingsLoaded && globalPortfolioData.settings) {
                    let s = globalPortfolioData.settings;
                    if (s.period) document.getElementById('periodSelect').value = s.period;
                    updateIntervals();
                    if (s.interval) document.getElementById('intervalSelect').value = s.interval;
                    if (s.style) document.getElementById('styleSelect').value = s.style;
                    if (s.refresh) document.getElementById('refreshSelect').value = s.refresh;
                    setupAutoRefresh();
                    isSettingsLoaded = true;
                }
                
                renderWatchlist(globalPortfolioData.watchlist);

                if (!currentTicker && globalPortfolioData.watchlist && globalPortfolioData.watchlist.length > 0) {
                    currentTicker = globalPortfolioData.watchlist[0];
                    fetchData(isSilent);
                    return;
                }

                document.getElementById('activeTitle').innerText = payload.name;
                currentAnomalyReason = payload.metrics.reason;
                currentLivePrice = payload.metrics.price;
                currentActiveTranches = payload.metrics.tranches;
                currentSharesOwned = payload.metrics.shares_owned;
                currentValOwned = payload.metrics.value_owned;
                
                document.getElementById('masterBudgetInput').value = payload.metrics.master_budget;
                document.getElementById('mPrice').innerText = payload.metrics.price_display;
                document.getElementById('mDisc').innerText = payload.metrics.discount;
                document.getElementById('mBuy').innerText = payload.metrics.buy_score;
                document.getElementById('mTranches').innerText = `${payload.metrics.tranches} / 5`;
                
                document.getElementById('inputHeldVal').value = currentValOwned;
                document.getElementById('mSharesOwned').innerText = `${currentSharesOwned} shares`;

                document.getElementById('mMacro').innerHTML = `<span style="display:inline-block; width:10px; height:10px; border-radius:50%; background-color:${payload.metrics.color};"></span> ${payload.metrics.status}`;
                
                calculateSizing();
                renderTradeHistory();
                fetchDirectives();
                renderChart();
            } catch (err) {}
            if (!isSilent) {
                document.getElementById('loader').style.display = 'none';
            }
        }

        function renderChart() {
            if (tvSeries) tvChart.removeSeries(tvSeries);
            if (!masterData.length) return;
            let style = document.getElementById('styleSelect').value;
            
            if (style === 'candlestick' || style === 'heikin-ashi') {
                let plotData = style === 'heikin-ashi' ? convertToHeikinAshi(masterData) : masterData;
                let cleanData = plotData.map(d => ({time: d.time, open: d.open, high: d.high, low: d.low, close: d.close}));
                tvSeries = tvChart.addCandlestickSeries({ upColor: '#00c853', downColor: '#ff3d00', borderVisible: false, wickUpColor: '#00c853', wickDownColor: '#ff3d00' });
                tvSeries.setData(cleanData);
            } else if (style === 'bar') {
                let cleanData = masterData.map(d => ({time: d.time, open: d.open, high: d.high, low: d.low, close: d.close}));
                tvSeries = tvChart.addBarSeries({ upColor: '#00c853', downColor: '#ff3d00' });
                tvSeries.setData(cleanData);
            } else if (style === 'area') {
                let cleanData = masterData.map(d => ({time: d.time, value: d.close}));
                tvSeries = tvChart.addAreaSeries({ topColor: 'rgba(0, 210, 255, 0.4)', bottomColor: 'rgba(0, 210, 255, 0.0)', lineColor: '#00d2ff', lineWidth: 2 });
                tvSeries.setData(cleanData);
            } else {
                let cleanData = masterData.map(d => ({time: d.time, value: d.close}));
                tvSeries = tvChart.addLineSeries({ color: '#00d2ff', lineWidth: 2 });
                tvSeries.setData(cleanData);
            }
            tvChart.timeScale().fitContent();
        }

        function selectStock(ticker, elem) {
            currentTicker = ticker;
            document.querySelectorAll('.watchlist-item').forEach(el => el.classList.remove('active'));
            if(elem) elem.classList.add('active');
            fetchData(false);
        }

        document.getElementById('watchlistUI').addEventListener('click', async function(e) {
            let li = e.target.closest('li.watchlist-item');
            if (!li) return;
            let symbol = li.getAttribute('data-symbol');
            if (e.target.classList.contains('btn-delete')) { 
                e.stopPropagation(); 
                await fetch('/api/watchlist/delete', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ticker: symbol})
                });
                let remaining = (globalPortfolioData.watchlist || []).filter(s => s !== symbol);
                currentTicker = remaining.length > 0 ? remaining[0] : '';
                fetchData(false);
                return; 
            }
            selectStock(symbol, li);
        });

        document.getElementById('addBtn').addEventListener('click', async () => {
            let val = document.getElementById('addTickerInput').value.trim().toUpperCase();
            if(!val) return;
            document.getElementById('addTickerInput').value = '';
            await fetch('/api/watchlist/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ticker: val})
            });
            selectStock(val);
            setTimeout(() => { openAlertModal(true); }, 500);
        });

        document.getElementById('qsTicker').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') checkQuickScore();
        });

        setTimeout(() => { initChart(); fetchUsers(); updateIntervals(); fetchData(false); setupAutoRefresh(); }, 100);
    </script>
</body>
</html>"""

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8095))
    app.run(host='0.0.0.0', port=port, debug=False)