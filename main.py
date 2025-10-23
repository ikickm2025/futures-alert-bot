# main.py
import os
import json
import pandas as pd
import requests
from datetime import datetime, timedelta
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
import pytz

# ======================
# CONFIGURATION (SET ONCE)
# ======================
ACCOUNT_SIZE = 25000          # Your assumed account size
RISK_PERCENT = 0.01           # 1% risk per trade
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "YOUR_ALPACA_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "YOUR_ALPACA_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "")
SYMBOL = "/MNQ"               # Use "/NQ" for NQ, "/MNQ" for Micro
POINT_VALUE = 2               # MNQ = $2/point, NQ = $20

app = Flask(__name__)

# Alpaca client (free market data)
data_client = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

def get_bars(symbol, minutes=50):
    """Fetch 1-min bars from Alpaca (free)"""
    try:
        request_params = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=(datetime.now() - timedelta(minutes=minutes)).isoformat() + "Z",
            limit=minutes
        )
        bars = data_client.get_crypto_bars(request_params).df
        if bars.empty:
            return None
        bars = bars.reset_index()
        return bars
    except Exception as e:
        print(f"Alpaca error: {e}")
        return None

def has_high_impact_news():
    """Check for high-impact news in next 15 mins"""
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        response = requests.get(url, timeout=5)
        events = response.json()
        now = datetime.utcnow()
        for e in events:
            if not e.get('date'):
                continue
            try:
                event_time = datetime.fromisoformat(e['date'].replace('Z', '+00:00'))
            except:
                continue
            diff_sec = (event_time - now).total_seconds()
            if 0 <= diff_sec <= 900:  # next 15 mins
                if e.get('impact') == 'High':
                    return True
        return False
    except:
        return False  # If API fails, assume no news

def check_setup():
    """Main strategy logic: breakout with volume confirmation"""
    if has_high_impact_news():
        print("🚫 Skipping: High-impact news detected")
        return None

    df = get_bars(SYMBOL, minutes=60)
    if df is None or len(df) < 20:
        print("⚠️ Not enough data")
        return None

    # Use last 50 bars for analysis
    recent = df.tail(50).copy()
    current_price = recent['close'].iloc[-1]
    current_vol = recent['volume'].iloc[-1]
    avg_vol = recent['volume'][-20:].mean()

    # Define lookback window (last 15 mins)
    lookback = recent.tail(15)
    recent_high = lookback['high'].max()
    recent_low = lookback['low'].min()

    # Long: price > recent high + volume surge
    if current_price > recent_high and current_vol > avg_vol * 1.5:
        stop_dist = (current_price - recent_low) / 2  # half the range
        stop_dist = max(3, min(10, stop_dist))  # clamp between 3-10 points
        return {
            "symbol": SYMBOL.replace("/", ""),
            "direction": "long",
            "price": round(current_price, 1),
            "stop_dist": round(stop_dist, 1)
        }

    # Short: price < recent low + volume surge
    if current_price < recent_low and current_vol > avg_vol * 1.5:
        stop_dist = (recent_high - current_price) / 2
        stop_dist = max(3, min(10, stop_dist))
        return {
            "symbol": SYMBOL.replace("/", ""),
            "direction": "short",
            "price": round(current_price, 1),
            "stop_dist": round(stop_dist, 1)
        }

    return None

def send_discord_alert(trade):
    if not DISCORD_WEBHOOK_URL:
        return
    color = 0x00ff00 if trade["direction"] == "long" else 0xff0000
    embed = {
        "title": f"{'🟢 LONG' if trade['direction'] == 'long' else '🔴 SHORT'} {trade['symbol']}",
        "description": f"Entry: {trade['price']}\nStop: {trade['price'] - trade['stop_dist'] if trade['direction']=='long' else trade['price'] + trade['stop_dist']}\nStop Dist: {trade['stop_dist']} pts",
        "color": color,
        "footer": {"text": "MNQ/NQ Breakout Bot • R:1% • Volume Confirmed"}
    }
    requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]})

def log_to_sheets(trade):
    if not GOOGLE_SCRIPT_URL:
        return
    risk_amount = ACCOUNT_SIZE * RISK_PERCENT
    contracts = int(risk_amount / (trade["stop_dist"] * POINT_VALUE))
    contracts = max(1, contracts)
    payload = {
        "symbol": trade["symbol"],
        "direction": trade["direction"],
        "entry_price": trade["price"],
        "stop_dist": trade["stop_dist"],
        "contracts": contracts,
        "risk": round(risk_amount, 2),
        "notes": "Auto breakout signal"
    }
    try:
        requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=5)
    except:
        pass

def scan_and_alert():
    """Run every 2 minutes during market hours"""
    et = pytz.timezone('US/Eastern')
    now_et = datetime.now(et)
    # Only scan during RTH: 9:30 AM – 4:00 PM ET
    if not (9 <= now_et.hour < 16):
        return

    print(f"🔍 Scanning at {now_et.strftime('%H:%M:%S ET')}")
    trade = check_setup()
    if trade:
        print(f"✅ Signal: {trade}")
        send_discord_alert(trade)
        log_to_sheets(trade)
    else:
        print("⏸️ No signal")

# Background scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(scan_and_alert, 'interval', minutes=2)
scheduler.start()

# Health check endpoint
@app.route('/')
def home():
    return "MNQ/NQ Breakout Bot is running!"

# Manual trigger (optional)
@app.route('/trigger', methods=['POST'])
def manual_trigger():
    trade = check_setup()
    if trade:
        send_discord_alert(trade)
        log_to_sheets(trade)
        return jsonify(trade)
    return jsonify({"status": "no_signal"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))