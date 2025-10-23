# main.py — MNQ/NQ Breakout Bot (24/7 Scanning)
import os
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
# CONFIGURATION
# ======================
ACCOUNT_SIZE = 25000
RISK_PERCENT = 0.01
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "")
SYMBOL = "/MNQ"      # Change to "/NQ" if needed
POINT_VALUE = 2      # MNQ = $2 | NQ = $20

app = Flask(__name__)
data_client = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

def get_bars(symbol, minutes=60):
    try:
        request_params = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=(datetime.now() - timedelta(minutes=minutes)).isoformat() + "Z",
            limit=minutes
        )
        bars = data_client.get_crypto_bars(request_params).df
        return bars.reset_index() if not bars.empty else None
    except Exception as e:
        print(f"Alpaca error: {e}")
        return None

def has_high_impact_news():
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        events = requests.get(url, timeout=5).json()
        now = datetime.utcnow()
        for e in events:
            if not e.get('date'): continue
            try:
                event_time = datetime.fromisoformat(e['date'].replace('Z', '+00:00'))
            except: continue
            if 0 <= (event_time - now).total_seconds() <= 900 and e.get('impact') == 'High':
                return True
        return False
    except:
        return False

def is_market_closed():
    """Skip Friday 5PM ET to Sunday 6PM ET (CME maintenance)"""
    et = pytz.timezone('US/Eastern')
    now_et = datetime.now(et)
    if now_et.weekday() == 4 and now_et.hour >= 17:  # Friday after 5 PM
        return True
    if now_et.weekday() == 5:  # Saturday
        return True
    if now_et.weekday() == 6 and now_et.hour < 18:  # Sunday before 6 PM
        return True
    return False

def check_setup():
    if is_market_closed():
        print("🌙 Market closed (weekend)")
        return None
    if has_high_impact_news():
        print("🚫 Skipping: High-impact news")
        return None

    df = get_bars(SYMBOL, minutes=60)
    if df is None or len(df) < 20:
        print("⚠️ Not enough data")
        return None

    recent = df.tail(50)
    current_price = recent['close'].iloc[-1]
    current_vol = recent['volume'].iloc[-1]
    avg_vol = recent['volume'][-20:].mean()

    lookback = recent.tail(15)
    recent_high = lookback['high'].max()
    recent_low = lookback['low'].min()

    # Long setup
    if current_price > recent_high and current_vol > avg_vol * 1.5:
        stop_dist = max(3, min(10, (current_price - recent_low) / 2))
        return {
            "symbol": SYMBOL.replace("/", ""),
            "direction": "long",
            "price": round(current_price, 1),
            "stop_dist": round(stop_dist, 1)
        }

    # Short setup
    if current_price < recent_low and current_vol > avg_vol * 1.5:
        stop_dist = max(3, min(10, (recent_high - current_price) / 2))
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
        "footer": {"text": "24/7 MNQ Breakout Bot • Volume Confirmed"}
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=5)
    except:
        pass

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
        "notes": "24/7 breakout signal"
    }
    try:
        requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=5)
    except:
        pass

def scan_and_alert():
    """Scan 24/7 (except weekend maintenance)"""
    print(f"🔍 Scanning at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    trade = check_setup()
    if trade:
        print(f"✅ Signal: {trade}")
        send_discord_alert(trade)
        log_to_sheets(trade)
    else:
        print("⏸️ No signal")

# Run every 2 minutes, 24/7
scheduler = BackgroundScheduler()
scheduler.add_job(scan_and_alert, 'interval', minutes=2)
scheduler.start()

@app.route('/')
def home():
    return "24/7 MNQ Breakout Bot is running!"

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