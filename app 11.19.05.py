import os
import sys
import webbrowser
import pandas as pd
import yfinance as yf

class YCACockpitSystem:
    def __init__(self, total_tranches=5):
        self.total_tranches = total_tranches
        self.exit_threshold_pct = 5.00
        self.sput_freeze_limit = -10.0

    def calculate_state(self, yca_price_p, nav_p, sput_discount_pct, volume_ratio):
        implied_discount_pct = ((nav_p - yca_price_p) / nav_p) * 100.0
        sentinel_frozen = sput_discount_pct < self.sput_freeze_limit

        if sentinel_frozen:
            buying_score = 0.0
            active_tranches = 0
            sentinel_status = "⚠️ FROZEN (SPUT Panic Floor)"
        else:
            discount_comp = min(max((implied_discount_pct / 20.0) * 80.0, 0.0), 80.0)
            volume_comp = min(max((volume_ratio / 2.0) * 20.0, 0.0), 20.0)
            buying_score = round(discount_comp + volume_comp, 2)
            buying_score = min(max(buying_score, 0.0), 100.0)
            active_tranches = int(buying_score // 20)
            if active_tranches > self.total_tranches:
                active_tranches = self.total_tranches
            sentinel_status = "🟢 GREEN (Stable)"

        selling_score = 100.0 if implied_discount_pct <= self.exit_threshold_pct else 0.0

        return {
            "implied_discount": implied_discount_pct,
            "buying_score": buying_score,
            "active_tranches": active_tranches,
            "selling_score": selling_score,
            "sentinel_status": sentinel_status
        }

def build_pro_terminal():
    target_dir = os.path.expanduser('~/Desktop/stocks')
    os.makedirs(target_dir, exist_ok=True)
    
    html_content = """<!DOCTYPE html>
<html>
<head>
    <title>Pro Stock Terminal & Cockpit</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1115; color: #e1e3e6; margin: 0; display: flex; height: 100vh; overflow: hidden; }
        
        /* Left Sidebar */
        .sidebar { width: 260px; background: #171a21; border-right: 1px solid #262b36; display: flex; flex-direction: column; padding: 15px; }
        .sidebar h2 { font-size: 14px; text-transform: uppercase; color: #787e8e; margin-bottom: 12px; }
        .search-box { display: flex; gap: 8px; margin-bottom: 15px; }
        .search-box input { flex: 1; background: #0f1115; border: 1px solid #262b36; color: #fff; padding: 8px; border-radius: 6px; }
        .search-box button { background: #00d2ff; border: none; color: #000; font-weight: bold; padding: 8px 12px; border-radius: 6px; cursor: pointer; }
        .watchlist { flex: 1; overflow-y: auto; list-style: none; padding: 0; margin: 0; }
        .watchlist-item { padding: 12px; border-radius: 6px; background: #1e222d; margin-bottom: 8px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; border: 1px solid transparent; }
        .watchlist-item:hover, .watchlist-item.active { border-color: #00d2ff; background: #252a37; }
        .watchlist-item .ticker { font-weight: bold; }
        
        /* Main View */
        .main-content { flex: 1; display: flex; flex-direction: column; padding: 20px; overflow-y: auto; }
        .top-nav { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; background: #171a21; padding: 12px 20px; border-radius: 10px; border: 1px solid #262b36; }
        .controls { display: flex; gap: 12px; align-items: center; }
        select, button.btn-control { background: #0f1115; border: 1px solid #262b36; color: #fff; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
        button.btn-alert { background: #ff9900; color: #000; font-weight: bold; }
        
        /* Grid Metrics */
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .card { background: #171a21; padding: 15px; border-radius: 10px; border: 1px solid #262b36; text-align: center; }
        .card h3 { font-size: 11px; color: #787e8e; margin: 0 0 6px 0; text-transform: uppercase; }
        .card p { font-size: 18px; font-weight: bold; margin: 0; color: #00d2ff; }
        
        /* Chart Canvas Container */
        .chart-container { flex: 1; background: #171a21; padding: 20px; border-radius: 10px; border: 1px solid #262b36; min-height: 400px; position: relative; }
    </style>
</head>
<body>
    <div class="sidebar">
        <h2>Watchlist</h2>
        <div class="search-box">
            <input type="text" id="addTickerInput" placeholder="Add symbol...">
            <button onclick="addTicker()">+</button>
        </div>
        <ul class="watchlist" id="watchlistUI">
            <li class="watchlist-item active" onclick="selectStock('YCA.L')"><span class="ticker">YCA.L</span><span>UK</span></li>
            <li class="watchlist-item" onclick="selectStock('MSFT')"><span class="ticker">MSFT</span><span>US</span></li>
            <li class="watchlist-item" onclick="selectStock('AVGO')"><span class="ticker">AVGO</span><span>US</span></li>
            <li class="watchlist-item" onclick="selectStock('ETN')"><span class="ticker">ETN</span><span>US</span></li>
        </ul>
    </div>
    
    <div class="main-content">
        <div class="top-nav">
            <h2 id="activeTitle" style="margin:0;">YCA.L - Terminal View</h2>
            <div class="controls">
                <label>Timeframe:</label>
                <select id="timeframeSelect" onchange="renderChart()">
                    <option value="6M">6 Months</option>
                    <option value="1Y">1 Year</option>
                    <option value="3Y">3 Years</option>
                    <option value="5Y">5 Years</option>
                    <option value="1M">1 Month</option>
                </select>
                
                <label>Chart Style:</label>
                <select id="styleSelect" onchange="renderChart()">
                    <option value="line">Line Chart</option>
                    <option value="bar">Bar / Column</option>
                </select>
                
                <button class="btn-control btn-alert" onclick="configureAlerts()">🔔 Setup Alerts</button>
            </div>
        </div>
        
        <div class="grid" id="metricsGrid">
            <div class="card"><h3>Live Price</h3><p id="mPrice">--</p></div>
            <div class="card"><h3>NAV Discount</h3><p id="mDisc">--</p></div>
            <div class="card"><h3>Buy Score</h3><p id="mBuy">--</p></div>
            <div class="card"><h3>Active Tranches</h3><p id="mTranches">--</p></div>
            <div class="card"><h3>Macro Status</h3><p id="mMacro" style="font-size:13px">🟢 Stable</p></div>
        </div>
        
        <div class="chart-container">
            <canvas id="mainChart"></canvas>
        </div>
    </div>

    <script>
        let currentTicker = 'YCA.L';
        let chartInstance = null;

        // Dynamic Mock Data Engine for smooth local demo preview
        function generateData(ticker, count) {
            let base = ticker === 'YCA.L' ? 550 : ticker === 'MSFT' ? 490 : 360;
            let dates = [], prices = [];
            let now = new Date();
            for (let i = count; i >= 0; i--) {
                let d = new Date();
                d.setDate(now.getDate() - i);
                dates.push(d.toISOString().split('T')[0]);
                base += (Math.random() - 0.49) * 4;
                prices.push(parseFloat(base.toFixed(2)));
            }
            return { dates, prices };
        }

        function selectStock(ticker) {
            currentTicker = ticker;
            document.getElementById('activeTitle').innerText = ticker + ' - Terminal View';
            document.querySelectorAll('.watchlist-item').forEach(el => el.classList.remove('active'));
            renderChart();
        }

        function addTicker() {
            let input = document.getElementById('addTickerInput');
            let val = input.value.trim().toUpperCase();
            if(!val) return;
            let ul = document.getElementById('watchlistUI');
            let li = document.createElement('li');
            li.className = 'watchlist-item';
            li.onclick = () => selectStock(val);
            li.innerHTML = `<span class="ticker">${val}</span><span>GLOBAL</span>`;
            ul.appendChild(li);
            input.value = '';
            selectStock(val);
        }

        function configureAlerts() {
            let buyAlert = prompt(`Set BUY Price Floor Alert for ${currentTicker}:`, "520.00");
            let sellAlert = prompt(`Set SELL Price Ceiling Alert for ${currentTicker}:`, "620.00");
            if(buyAlert && sellAlert) {
                alert(`✅ Alerts Configured for ${currentTicker}!\n• Buy Alert below: $${buyAlert}\n• Sell Alert above: $${sellAlert}`);
            }
        }

        function renderChart() {
            let tf = document.getElementById('timeframeSelect').value;
            let style = document.getElementById('styleSelect').value;
            let dataPoints = tf === '1M' ? 30 : tf === '6M' ? 180 : tf === '1Y' ? 365 : 700;
            
            let data = generateData(currentTicker, dataPoints);
            let lastPrice = data.prices[data.prices.length - 1];

            // Metrics Update
            document.getElementById('mPrice').innerText = (currentTicker === 'YCA.L') ? lastPrice + 'p' : '$' + lastPrice;
            document.getElementById('mDisc').innerText = (100 - (lastPrice / 634 * 100)).toFixed(2) + '%';
            document.getElementById('mBuy').innerText = Math.min(100, Math.max(0, (634 - lastPrice) * 1.2)).toFixed(1) + ' / 100';
            document.getElementById('mTranches').innerText = Math.floor(Math.min(100, Math.max(0, (634 - lastPrice) * 1.2)) / 20) + ' / 5';

            if (chartInstance) chartInstance.destroy();

            const ctx = document.getElementById('mainChart').getContext('2d');
            chartInstance = new Chart(ctx, {
                type: style,
                data: {
                    labels: data.dates,
                    datasets: [{
                        label: currentTicker + ' Price',
                        data: data.prices,
                        borderColor: '#00d2ff',
                        backgroundColor: style === 'bar' ? '#00d2ff' : 'rgba(0, 210, 255, 0.05)',
                        borderWidth: 2,
                        fill: true,
                        pointRadius: 0
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { labels: { color: '#8a8a9e' } } },
                    scales: {
                        x: { ticks: { color: '#787e8e' }, grid: { color: '#262b36' } },
                        y: { ticks: { color: '#787e8e' }, grid: { color: '#262b36' } }
                    }
                }
            });
        }

        window.onload = () => renderChart();
    </script>
</body>
</html>"""

    file_path = os.path.join(target_dir, 'pro_terminal.html')
    with open(file_path, 'w') as f:
        f.write(html_content)

    webbrowser.open(f'file://{file_path}')
    print("🚀 Pro Terminal launched successfully!")

if __name__ == '__main__':
    build_pro_terminal()