import os, time
import pandas as pd
try:
    import yfinance as yf
except ImportError:
    yf = None
from flask import Flask, jsonify, request, render_template
from portfolio import portfolio_store, send_push_notification
from engine import MarketScoringEngine, safe_float, normalize_price
from worker import YF_CACHE, start_threads

app = Flask(__name__)

# Start background price cache & auto-trading threads
start_threads()

def get_last_traded_price(ud, ticker, default=10.0):
    if not isinstance(ud, dict) or not ticker:
        return default
    hist = ud.get('history') if isinstance(ud.get('history'), list) else []
    for tr in reversed(hist):
        if isinstance(tr, dict) and tr.get('ticker') == ticker:
            p = safe_float(tr.get('price', tr.get('exec_price', tr.get('amount'))))
            if tr.get('shares') and safe_float(tr.get('shares')) > 0 and tr.get('amount'):
                p = safe_float(tr.get('amount')) / safe_float(tr.get('shares'))
            p = normalize_price(ticker, p)
            if p > 0: return round(p, 2)
    return default

def find_matching_user(users_dict, requested_name):
    if not isinstance(users_dict, dict) or not users_dict:
        return 'Test E6 - Breakeven Rotator', {}
    if requested_name in users_dict:
        return requested_name, users_dict[requested_name]
    req_str = str(requested_name or '').strip().lower()
    for k, v in users_dict.items():
        if str(k).strip().lower() == req_str:
            return k, v
    for k, v in users_dict.items():
        k_str = str(k).strip().lower()
        if k_str in req_str or req_str in k_str:
            return k, v
    first_key = list(users_dict.keys())[0]
    return first_key, users_dict[first_key]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/users', methods=['GET'])
def get_users():
    users_dict = portfolio_store.data.get('users', {}) if isinstance(getattr(portfolio_store, 'data', None), dict) else {}
    return jsonify({'users': list(users_dict.keys()), 'active_user': portfolio_store.active_username()})

@app.route('/api/users/select', methods=['POST'])
def select_user():
    try:
        b = request.get_json() or {}
        real_key, _ = find_matching_user(portfolio_store.data.get('users', {}), str(b.get('username', '')).strip())
        portfolio_store.switch_user(real_key)
        return jsonify({'status': 'ok', 'active_user': real_key})
    except Exception:
        return jsonify({'status': 'ok', 'active_user': portfolio_store.active_username()})

@app.route('/api/users/add', methods=['POST'])
def add_user():
    b = request.get_json() or {}
    portfolio_store.add_user(b.get('username', ''))
    return jsonify({'status': 'ok'})

@app.route('/api/users/delete', methods=['POST'])
def delete_user():
    b = request.get_json() or {}
    success = portfolio_store.delete_user(b.get('username', ''))
    return jsonify({'status': 'ok' if success else 'error'})

@app.route('/api/portfolio', methods=['GET'])
def get_portfolio():
    return jsonify(portfolio_store.user_data())

@app.route('/api/portfolio/reset', methods=['POST'])
def reset_portfolio():
    return jsonify({'status': 'ok', 'portfolio': portfolio_store.reset_all()})

@app.route('/api/portfolio/reset_all_profiles', methods=['POST'])
def reset_all_profiles():
    portfolio_store.reset_all_profiles_to_5000()
    return jsonify({'status': 'ok'})

@app.route('/api/portfolio/settings', methods=['POST'])
def update_settings():
    b = request.get_json() or {}
    portfolio_store.update_settings(b.get('settings', {}))
    return jsonify({'status': 'ok'})

@app.route('/api/portfolio/budget', methods=['POST'])
def update_budget():
    b = request.get_json() or {}
    portfolio_store.update_budget(b.get('budget', 5000))
    return jsonify({'status': 'ok'})

@app.route('/api/watchlist/add', methods=['POST'])
def add_watchlist():
    b = request.get_json() or {}
    portfolio_store.add_watchlist(b.get('ticker', ''))
    return jsonify({'status': 'ok'})

@app.route('/api/watchlist/delete', methods=['POST'])
def delete_watchlist():
    b = request.get_json() or {}
    portfolio_store.remove_watchlist(b.get('ticker', ''))
    return jsonify({'status': 'ok'})

@app.route('/api/portfolio/holding', methods=['POST'])
def update_holding():
    b = request.get_json() or {}
    shares = portfolio_store.set_holding_value(b.get('ticker'), b.get('value_owned', 0), b.get('price', 1))
    return jsonify({'status': 'ok', 'shares': shares})

@app.route('/api/trade/execute', methods=['POST'])
def execute_trade():
    b = request.get_json() or {}
    entry = portfolio_store.execute_trade(b.get('ticker'), b.get('action'), safe_float(b.get('shares')), safe_float(b.get('price')), b.get('username'))
    return jsonify({'status': 'ok', 'entry': entry})

@app.route('/api/trade/undo', methods=['POST'])
def undo_trade():
    b = request.get_json() or {}
    success = portfolio_store.undo_trade(b.get('id'))
    return jsonify({'status': 'ok' if success else 'error'})

@app.route('/api/score', methods=['GET'])
def get_score():
    t = request.args.get('t', '').upper().strip()
    try:
        active_profile = portfolio_store.active_username()
        df = YF_CACHE.get(f"{t}_5m", (0, pd.DataFrame()))[1]
        if df.empty and yf:
            try:
                df = yf.Ticker(t).history(period="1d", interval="5m")
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
            except Exception:
                pass
        if df.empty:
            return jsonify({'error': 'Ticker not found.'}), 400
            
        cur = normalize_price(t, float(df['Close'].iloc[-1]))
        engine = MarketScoringEngine()
        df_qqq = YF_CACHE.get('QQQ_5m', (0, pd.DataFrame()))[1]
        regime = engine.check_market_regime(df_qqq)
        res = engine.score_momentum(df, cur, profile=active_profile, regime=regime)
        res.update({'ticker': t, 'name': engine.asset_names.get(t, t), 'price': round(cur, 2)})
        return jsonify(res)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/push_test', methods=['POST'])
def test_push():
    b = request.get_json() or {}
    topic = b.get('topic', '')
    if topic:
        send_push_notification(topic, "Terminal Alert Test", "Push notifications are working!")
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'No topic provided'})

@app.route('/api/directives', methods=['GET'])
def get_directives():
    return jsonify({'directives': [], 'ian_directives': [], 'all_pending_actions': []})

@app.route('/api/recommend', methods=['GET'])
def get_recommendations():
    return jsonify({'recommendations': []})

@app.route('/api/trade_journey', methods=['GET'])
def trade_journey():
    return jsonify({'trades': []})

@app.route('/api/data', methods=['GET'])
def get_data():
    try:
        users_dict = portfolio_store.data.get('users', {}) if isinstance(getattr(portfolio_store, 'data', None), dict) else {}
        target_user = request.args.get('user', '').strip() or getattr(portfolio_store, 'active_username', lambda: 'Test E6 - Breakeven Rotator')()
        real_key, ud = find_matching_user(users_dict, target_user)

        wl = ud.get('watchlist') if isinstance(ud.get('watchlist'), list) else ['NVDA', 'TQQQ', 'SOXL']
        hist = ud.get('history') if isinstance(ud.get('history'), list) else []
        mb = safe_float(ud.get('master_budget'), 5000.0)

        open_positions = {}
        net_history = 0.0
        for tr in hist:
            if isinstance(tr, dict):
                tk = tr.get('ticker')
                act = str(tr.get('action', '')).upper()
                amt = safe_float(tr.get('amount'))
                qty = safe_float(tr.get('shares', tr.get('qty', 0)))
                if act == 'BUY':
                    net_history -= amt
                    if tk: open_positions[tk] = open_positions.get(tk, 0.0) + qty
                elif act == 'SELL':
                    net_history += amt
                    if tk: open_positions[tk] = open_positions.get(tk, 0.0) - qty

        active_holds = [tk for tk, sh in open_positions.items() if round(sh, 4) >= 0.01]
        t = request.args.get('t', '').upper().strip()
        if not t:
            t = 'ALL_SHARES'

        cash_balance = mb + net_history
        tot_own = 0.0
        for tk in active_holds:
            sh = open_positions[tk]
            p = get_last_traded_price(ud, tk, default=10.0)
            df_tk = YF_CACHE.get(f"{tk}_5m", (0, pd.DataFrame()))[1]
            if not df_tk.empty: p = normalize_price(tk, float(df_tk['Close'].iloc[-1]))
            tot_own += sh * p

        total_equity = cash_balance + tot_own
        master_pnl_val = total_equity - mb
        pnl_formatted = f"{'+' if master_pnl_val >= 0 else ''}£{master_pnl_val:.2f}"
        pnl_color = '#00c853' if master_pnl_val >= 0 else '#ff3d00'
        master_pnl_data = {'all': {'val': pnl_formatted, 'color': pnl_color}}

        engine = MarketScoringEngine()
        df_qqq = YF_CACHE.get('QQQ_5m', (0, pd.DataFrame()))[1]
        regime = engine.check_market_regime(df_qqq)

        leaderboard = []
        if isinstance(users_dict, dict):
            for u_key, u_data in users_dict.items():
                if not isinstance(u_data, dict): continue
                mb_lb = safe_float(u_data.get('master_budget'), 5000.0)
                hist_lb = u_data.get('history') if isinstance(u_data.get('history'), list) else []
                
                nh_lb = 0.0
                sh_lb_map = {}
                for tr in hist_lb:
                    if isinstance(tr, dict):
                        amt = safe_float(tr.get('amount'))
                        act = str(tr.get('action', '')).upper()
                        tk_lb = tr.get('ticker')
                        qty = safe_float(tr.get('shares', tr.get('qty', 0)))
                        if act == 'BUY':
                            nh_lb -= amt
                            if tk_lb: sh_lb_map[tk_lb] = sh_lb_map.get(tk_lb, 0.0) + qty
                        elif act == 'SELL':
                            nh_lb += amt
                            if tk_lb: sh_lb_map[tk_lb] = sh_lb_map.get(tk_lb, 0.0) - qty

                cash_now = mb_lb + nh_lb
                holdings_val_lb = 0.0
                for tk_lb, sh_lb in sh_lb_map.items():
                    if round(sh_lb, 4) >= 0.01:
                        p_lb = get_last_traded_price(u_data, tk_lb, default=10.0)
                        df_lb = YF_CACHE.get(f"{tk_lb}_5m", (0, pd.DataFrame()))[1]
                        if not df_lb.empty: p_lb = normalize_price(tk_lb, float(df_lb['Close'].iloc[-1]))
                        holdings_val_lb += sh_lb * p_lb

                tot_eq_now = cash_now + holdings_val_lb
                pnl_lb = round(tot_eq_now - mb_lb, 2)
                has_holds = any(round(v, 4) >= 0.01 for v in sh_lb_map.values())

                leaderboard.append({
                    'user': u_key, 'equity': round(tot_eq_now, 2),
                    'budget': mb_lb, 'daily_pnl': pnl_lb, 'has_active_holds': has_holds
                })

        # --- ALL SHARES / MULTI-LINE PERFORMANCE ---
        if t == 'ALL_SHARES':
            lines = []
            colors = ['#00d2ff', '#00c853', '#ff3d00', '#ff9900', '#b388ff', '#ffff00', '#ff4081', '#18ffff']
            c_idx = 0
            for tick in wl[:15]:
                df_t = YF_CACHE.get(f"{tick}_5m", (0, pd.DataFrame()))[1]
                if df_t.empty and yf:
                    try:
                        df_t = yf.Ticker(tick).history(period="1d", interval="5m")
                        if isinstance(df_t.columns, pd.MultiIndex):
                            df_t.columns = df_t.columns.get_level_values(0)
                        YF_CACHE[f"{tick}_5m"] = (time.time(), df_t)
                    except Exception:
                        pass
                if isinstance(df_t, pd.DataFrame) and not df_t.empty:
                    base_price = normalize_price(tick, float(df_t['Close'].iloc[0]))
                    if base_price > 0:
                        line_data = []
                        for idx, row in df_t.iterrows():
                            cp = normalize_price(tick, float(row['Close']))
                            val = round(((cp - base_price) / base_price) * 100.0, 2)
                            line_data.append({'time': int(idx.timestamp()), 'value': val})
                        lines.append({'ticker': tick, 'color': colors[c_idx % len(colors)], 'data': line_data})
                        c_idx += 1

            return jsonify({
                'is_multi': True, 'lines': lines, 'wl_status': {}, 'name': 'Relative Performance (Watchlist)', 'portfolio': ud, 'leaderboard': leaderboard,
                'metrics': {
                    'price': 0, 'price_display': 'Normalized %', 'discount': '--', 'buy_score': '--', 'tranches': 0,
                    'status': regime.get('state', 'Room Temp'), 'color': regime.get('color', '#8a8a9e'), 'reason': f"Sentiment Thermometer: {regime.get('score', 50)}/100 ({regime.get('state', 'Room Temp')}).",
                    'action_main': '--', 'action_sub': '', 'action_color': '#8a8a9e',
                    'shares_owned': sum(open_positions.values()), 'value_owned': round(tot_own, 2),
                    'pnl_display': pnl_formatted, 'pnl_color': pnl_color, 'total_pnl_display': pnl_formatted, 'total_pnl_color': pnl_color,
                    'master_pnl_data': master_pnl_data, 'pnl_data': master_pnl_data, 'master_budget': mb,
                    'total_portfolio_owned': round(tot_own, 2), 'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2),
                    'regime': regime, 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'All Shares View',
                    'hard_pct': -0.50, 'stop_price': 0.0
                }
            })

        # --- SINGLE TICKER OHLC ---
        cache_df = YF_CACHE.get(f"{t}_5m", (0, pd.DataFrame()))[1]
        if cache_df.empty and yf:
            try:
                df_direct = yf.Ticker(t).history(period="1d", interval="5m")
                if isinstance(df_direct, pd.DataFrame) and not df_direct.empty:
                    if isinstance(df_direct.columns, pd.MultiIndex):
                        df_direct.columns = df_direct.columns.get_level_values(0)
                    YF_CACHE[f"{t}_5m"] = (time.time(), df_direct)
                    cache_df = df_direct
            except Exception:
                pass

        data = []
        if isinstance(cache_df, pd.DataFrame) and not cache_df.empty:
            data = [{'time': int(i.timestamp()), 'open': normalize_price(t, r['Open']), 'high': normalize_price(t, r['High']), 'low': normalize_price(t, r['Low']), 'close': normalize_price(t, r['Close'])} for i, r in cache_df.iterrows()]

        hist_fallback = get_last_traded_price(ud, t, default=10.0)
        if not data:
            now_ts = int(time.time())
            data = [{'time': now_ts - 300, 'open': hist_fallback, 'high': hist_fallback, 'low': hist_fallback, 'close': hist_fallback}, {'time': now_ts, 'open': hist_fallback, 'high': hist_fallback, 'low': hist_fallback, 'close': hist_fallback}]

        last_p = round(safe_float(data[-1]['close'], hist_fallback), 2)
        
        t_buys = [tr for tr in hist if isinstance(tr, dict) and tr.get('ticker') == t and str(tr.get('action')).upper() == 'BUY']
        avg_buy_p = normalize_price(t, t_buys[0].get('price', last_p)) if t_buys else 0.0
        highest_p = (ud.get('holdings', {}).get(t) or {}).get('high_water', avg_buy_p if avg_buy_p > 0 else last_p)

        if t in engine.nav_bases:
            st = engine.score_nav_asset(t, last_p, 1.0, avg_buy_p, highest_p)
        else:
            st = engine.score_momentum(cache_df if not cache_df.empty else pd.DataFrame(), last_p, avg_buy_p, highest_p, profile=real_key, regime=regime)

        sh_own = open_positions.get(t, 0.0)

        return jsonify({
            'is_multi': False, 'ohlc': data, 'mathLine': [], 'wl_status': {}, 'name': t, 'portfolio': ud, 'leaderboard': leaderboard,
            'metrics': {
                'price': last_p, 'price_display': f"£{last_p:.2f}",
                'discount': st.get('discount', '--'),
                'buy_score': str(st.get('score', '--')),
                'tranches': st.get('tranches', 0),
                'status': st.get('status', 'Standby'),
                'color': st.get('color', '#8a8a9e'),
                'reason': st.get('reason', 'Awaiting data.'),
                'action_main': st.get('action_main', 'HOLD / WAIT'),
                'action_sub': st.get('action_sub', ''),
                'action_color': st.get('action_color', '#8a8a9e'),
                'shares_owned': sh_own,
                'value_owned': round(sh_own * last_p, 2),
                'pnl_display': pnl_formatted, 'pnl_color': pnl_color,
                'total_pnl_display': pnl_formatted, 'total_pnl_color': pnl_color,
                'master_pnl_data': master_pnl_data, 'pnl_data': master_pnl_data,
                'master_budget': mb, 'total_portfolio_owned': round(tot_own, 2),
                'budget_remaining': round(cash_balance, 2), 'total_equity': round(total_equity, 2),
                'regime': regime,
                'health_pct': st.get('health_pct', 0),
                'health_color': st.get('health_color', '#8a8a9e'),
                'health_text': st.get('health_text', 'No Position'),
                'stop_price': st.get('stop_price', 0.0),
                'hard_pct': st.get('hard_pct', -0.50)
            }
        })
    except Exception as err:
        fallback_pnl = {'all': {'val': '£0.00', 'color': '#8a8a9e'}}
        return jsonify({
            'is_multi': False,
            'ohlc': [{'time': int(time.time()), 'open': 100.0, 'high': 100.0, 'low': 100.0, 'close': 100.0}],
            'mathLine': [], 'wl_status': {}, 'name': 'NVDA', 'portfolio': {'watchlist': ['NVDA'], 'history': [], 'holdings': {}},
            'leaderboard': [],
            'metrics': {
                'price': 100.0, 'price_display': '£100.00', 'discount': '--', 'buy_score': '--', 'tranches': 0,
                'status': 'Standby', 'color': '#8a8a9e', 'reason': f'Recovered: {str(err)}', 'action_main': 'HOLD',
                'action_sub': '', 'action_color': '#8a8a9e', 'shares_owned': 0, 'value_owned': 0.0,
                'pnl_display': '£0.00', 'pnl_color': '#8a8a9e', 'total_pnl_display': '£0.00', 'total_pnl_color': '#8a8a9e',
                'master_pnl_data': fallback_pnl, 'pnl_data': fallback_pnl, 'master_budget': 5000.0, 'total_portfolio_owned': 0.0,
                'budget_remaining': 5000.0, 'total_equity': 5000.0,
                'regime': {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e'}
            }
        }), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)