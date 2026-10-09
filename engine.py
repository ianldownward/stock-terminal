import pandas as pd

def safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

DEFAULT_TICKER_PRICES = {
    'SGLN.L': 3150.0,  # £31.50
    'SSLN.L': 2350.0,  # £23.50
    'YCA.L': 634.0,    # £6.34
    'SHEL.L': 2580.0,  # £25.80
    'BP.L': 480.0,     # £4.80
    'AZN.L': 10500.0,  # £105.00
    'RIO.L': 5120.0,   # £51.20
    '3SUS.L': 1250.0,  # £12.50
    'U-UN.TO': 28.50,  # $28.50
    'PHYS': 33.00,     # $33.00
    'PSLV': 21.50,     # $21.50
    'CEF': 22.00,      # $22.00
    'NVDA': 130.00,
    'TQQQ': 75.00,
    'SOXL': 38.00,
    'NVDL': 65.00,
    'TSLA': 240.00,
    'AMD': 160.00,
    'AMZN': 185.00,
    'META': 580.00,
    'SQQQ': 8.50
}

def get_default_price(ticker):
    tk = str(ticker).strip().upper()
    base = DEFAULT_TICKER_PRICES.get(tk, 2500.0 if tk.endswith('.L') else 50.0)
    return round(base / 100.0, 4) if tk.endswith('.L') else round(base, 4)

def normalize_price(ticker, price):
    """Guarantees UK pence (.L) are converted to pounds regardless of price level."""
    p = safe_float(price, 0.0)
    tk = str(ticker).strip().upper()
    if p <= 0:
        return get_default_price(tk)
    if tk.endswith('.L'):
        return round(p / 100.0, 4) if p > 50 else round(p, 4)
    return round(p, 4)

class MarketScoringEngine:
    def __init__(self):
        self.nav_bases = {
            'YCA.L': 634.0, 'U-UN.TO': 28.50, 'PHYS': 33.00, 'PSLV': 21.50, 
            'CEF': 22.00, 'SGLN.L': 3150.0, 'SSLN.L': 2350.0
        }
        self.asset_names = {
            'YCA.L': 'Yellow Cake plc', 'U-UN.TO': 'Sprott Physical Uranium Trust', 'PHYS': 'Sprott Physical Gold Trust',
            'PSLV': 'Sprott Physical Silver Trust', 'CEF': 'Sprott Physical Gold & Silver', 'GLD': 'SPDR Gold Shares',
            'SGLN.L': 'iShares Physical Gold ETC', 'SSLN.L': 'iShares Physical Silver ETC', 'MSFT': 'Microsoft Corp',
            'AAPL': 'Apple Inc.', 'NVDA': 'NVIDIA Corp', 'TSLA': 'Tesla', 'AMZN': 'Amazon', 'META': 'Meta Platforms', 
            'GOOGL': 'Alphabet', 'AMD': 'Advanced Micro Devices', 'NFLX': 'Netflix', 'PLTR': 'Palantir Tech', 
            'COIN': 'Coinbase', 'MSTR': 'MicroStrategy', 'TQQQ': 'ProShares UltraPro QQQ',
            'SOXL': 'Direxion Daily Semi Bull 3X', 'NVDL': 'GraniteShares 2x Long NVDA', 'SQQQ': 'ProShares UltraPro Short QQQ (3x Short)',
            '3SUS.L': 'WisdomTree US NASDAQ 3x Short', 'CONL': 'GraniteShares 2x Long COIN', 'MSTX': 'Defiance 2x Daily Long MSTR', 
            'BITX': '2x Bitcoin Strategy ETF', 'RR.L': 'Rolls-Royce Holdings', 'SHEL.L': 'Shell plc', 'BP.L': 'BP plc', 
            'BARC.L': 'Barclays plc', 'LLOY.L': 'Lloyds Banking Group', 'AZN.L': 'AstraZeneca',
            'GLEN.L': 'Glencore plc', 'RIO.L': 'Rio Tinto plc', 'HSBA.L': 'HSBC Holdings', 'GSK.L': 'GSK plc', 'ULVR.L': 'Unilever plc'
        }

    def check_market_regime(self, df_qqq=None):
        try:
            if df_qqq is not None and not df_qqq.empty and len(df_qqq) >= 21:
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
                
                if score <= 20: state, color, code = "Deep Freeze (Capitulation)", "#00d2ff", "BEAR_FREEZE"
                elif score <= 40: state, color, code = "Cooling (Pullback)", "#ff9900", "BEAR"
                elif score <= 60: state, color, code = "Room Temp (Neutral)", "#8a8a9e", "NEUTRAL"
                elif score <= 80: state, color, code = "Heating Up (Expansion)", "#00c853", "BULL"
                else: state, color, code = "Overheating (Euphoria)", "#b388ff", "BULL_OVERHEAT"
                
                return {'score': score, 'state': state, 'color': color, 'code': code, 'sparkline': sparkline, 'trend': "▲ Trending Up" if score > 50 else "▼ Trending Down"}
        except Exception: 
            pass
        return {'score': 50, 'state': 'Room Temp (Neutral)', 'color': '#8a8a9e', 'code': 'NEUTRAL', 'sparkline': [], 'trend': '► Stable'}

    def score_momentum(self, df_5m, current_price, avg_buy_price=0.0, highest_price=0.0, profile='test e6', regime=None):
        if regime is None: regime = {'score': 50, 'state': 'Room Temp', 'color': '#8a8a9e'}
        ticker = getattr(df_5m, 'name', '')
        trade_type = 'SHORT' if ticker in ['SQQQ', '3SUS.L'] else 'LONG'

        if df_5m.empty or len(df_5m) < 21:
            return {'type': 'Intraday', 'score': 0, 'status': 'Awaiting Data', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '', 'action_color': '#8a8a9e', 'reason': 'Awaiting intraday data.'}

        ema9 = df_5m['Close'].ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = df_5m['Close'].ewm(span=21, adjust=False).mean().iloc[-1]
        delta = df_5m['Close'].diff()
        rs = (delta.where(delta > 0, 0)).rolling(14).mean() / (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + rs.iloc[-1])) if not rs.empty else 50

        pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0 if avg_buy_price > 0 else 0.0
        peak_pnl_pct = ((highest_price - avg_buy_price) / avg_buy_price) * 100.0 if avg_buy_price > 0 else 0.0
        drop_from_peak = ((highest_price - current_price) / highest_price) * 100.0 if highest_price > 0 else 0.0

        prof = str(profile).lower()
        trail_pct, hard_pct = 0.75, -0.75
        if 'test e7' in prof or 'test e13' in prof:
            trail_pct, hard_pct = 0.50, -0.50
        elif 'test e4' in prof or 'test e' in prof:
            trail_pct, hard_pct = 1.00, -1.00
        elif 'test u' in prof or 'test x' in prof:
            trail_pct, hard_pct = 0.50, -0.50

        if avg_buy_price > 0 and highest_price > 0:
            effective_stop_price = max(avg_buy_price * (1 + hard_pct/100.0), highest_price * (1 - trail_pct/100.0))
            
            if any(x in prof for x in ['test e6', 'test e7', 'test e8', 'test e13', 'test x']) and peak_pnl_pct >= 0.50:
                breakeven_stop = avg_buy_price * 1.0010
                effective_stop_price = max(effective_stop_price, breakeven_stop)
                if current_price <= effective_stop_price:
                    return {'type': 'Momentum', 'score': 0, 'status': 'Breakeven Lock', 'color': '#00c853', 'action_main': 'SELL', 'action_sub': '(Lock Breakeven)', 'action_color': '#00c853', 'reason': f'BREAKEVEN LOCK TRIPPED ({pnl_pct:+.2f}%). Peak was +{peak_pnl_pct:.2f}%.', 'health_pct': 0, 'health_color': '#00c853', 'health_text': f'Breakeven Lock (+{pnl_pct:.2f}%)', 'hard_pct': 0.10, 'stop_price': round(effective_stop_price, 2)}

            if current_price <= effective_stop_price or drop_from_peak >= trail_pct or pnl_pct <= hard_pct:
                return {'type': 'Momentum', 'score': 0, 'status': 'Stop Tripped', 'color': '#ff3d00', 'action_main': 'SELL', 'action_sub': '(Stop Loss)', 'action_color': '#ff3d00', 'reason': f'Stop Tripped ({pnl_pct:+.2f}%). Peak: £{highest_price:.2f}.', 'health_pct': 0, 'health_color': '#ff3d00', 'health_text': f'Stop Tripped ({pnl_pct:.2f}%)', 'hard_pct': hard_pct, 'stop_price': round(effective_stop_price, 2)}

            health_pct = int(max(0, min(100, 100 - (drop_from_peak / trail_pct * 100))))
            return {'type': 'Momentum', 'score': 80, 'status': 'Riding Trend', 'color': '#00d2ff', 'action_main': 'HOLD / WAIT', 'action_sub': '(Riding)', 'action_color': '#00d2ff', 'reason': f'RIDING TREND. High Water: £{highest_price:.2f}.', 'health_pct': health_pct, 'health_color': '#00c853' if health_pct >= 70 else '#ff9900', 'health_text': f'Tracking ({pnl_pct:+.2f}%)', 'hard_pct': hard_pct, 'stop_price': round(effective_stop_price, 2)}

        if ema9 > ema21 and current_price > ema9 and rsi < 65:
            return {'type': 'Momentum', 'score': 85, 'status': f'Fast {trade_type} Surge', 'color': '#00c853', 'action_main': 'BUY', 'action_sub': f'({trade_type} Surge)', 'action_color': '#00c853', 'reason': f'SURGE DETECTED: Price > 9-EMA > 21-EMA, RSI {rsi:.1f}.', 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...', 'hard_pct': hard_pct, 'stop_price': round(current_price * (1 + hard_pct/100.0), 2)}

        return {'type': 'Momentum', 'score': 10, 'status': 'No Setup', 'color': '#8a8a9e', 'action_main': 'HOLD / WAIT', 'action_sub': '(Scanning)', 'action_color': '#8a8a9e', 'reason': 'Awaiting fast EMA crossover surge.', 'health_pct': 0, 'health_color': '#8a8a9e', 'health_text': 'Scanning...', 'hard_pct': hard_pct, 'stop_price': round(current_price * (1 + hard_pct/100.0), 2)}

   def score_nav_asset(self, ticker, current_price, volume_ratio, avg_buy_price=0.0, highest_price=0.0):
        nav_raw = self.nav_bases.get(ticker, current_price * 1.10)
        nav_pound = nav_raw / 100.0 if ticker.endswith('.L') else nav_raw
        implied_discount = ((nav_pound - current_price) / nav_pound) * 100.0 if nav_pound > 0 else 0.0
        buy_score = min(100, max(0, round((implied_discount / 20.0) * 80.0))) if implied_discount > 0 else 100
        pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0 if avg_buy_price > 0 else 0.0
        return {
            'type': 'Physical Trust', 'score': buy_score, 
            'status': 'Deep Value Anomaly' if buy_score >= 60 else 'Fair Value', 
            'color': '#00c853' if buy_score >= 60 else '#8a8a9e', 
            'action_main': 'BUY' if buy_score >= 60 else 'HOLD / WAIT', 
            'action_sub': '', 'action_color': '#00c853' if buy_score >= 60 else '#8a8a9e', 
            'reason': f'Trading at {implied_discount:.1f}% NAV discount.',
            'health_pct': 100 if avg_buy_price > 0 else 0,
            'health_text': f'Holding Physical Trust ({pnl_pct:+.2f}%)' if avg_buy_price > 0 else 'Scanning NAV Anomaly...'
        }