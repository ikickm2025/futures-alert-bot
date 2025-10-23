# main.py — Advanced 24/7 MNQ/NQ Trading Bot
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
ACCOUNT_SIZE = 25000          # Adjust to your assumed account size
RISK_PERCENT = 0.01           # 1% risk per trade
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "")
SYMBOL = "/MNQ"               # Use "/NQ" for NQ
POINT_VALUE = 2               # MNQ = $2 | NQ = $20

app = Flask(__name__)
data_client = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

# ======================
# HELPER FUNCTIONS
# ======================

def get_bars(symbol, minutes=60):
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
        # Ensure timestamp is timezone-aware
        bars['timestamp'] = pd.to_datetime(bars['timestamp'], utc=True)
        return bars
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

def get_fear_greed_index():
    try:
        url = "https://api.alternative.me/fng/"
        data = requests.get(url, timeout=5).json()
        return int(data['data'][0]['value'])
    except:
        return 50  # neutral

def is_market_closed():
    et = pytz.timezone('US/Eastern')
    now_et = datetime.now(et)
    if now_et.weekday() == 4 and now_et.hour >= 17:  # Friday after 5 PM ET
        return True
    if now_et.weekday() == 5:  # Saturday
        return True
    if now_et.weekday() == 6 and now_et.hour < 18:  # Sunday before 6 PM ET
        return True
    return False

def calculate_vwap(df):
    df = df.copy()
    df['tp'] = (df['high'] + df['low'] + df['close']) / 3
    df['vwap'] = (df['tp'] * df['volume']).cumsum() / df['volume'].cumsum()
    return df['vwap'].iloc[-1]

# ======================
# STRATEGY LOGIC
# ======================

def check_orb_setup(df, current_time_et):
    # Only during first 30 mins of RTH
    if not ((current_time_et.hour == 9 and current_time_et.minute >= 30) or
            (current_time_et.hour == 10 and current_time_et.minute == 0)):
        return None

    # Filter bars from 9:30 AM ET onward
    df_et = df.copy()
    df_et['timestamp_et'] = df_et['timestamp'].dt.tz_convert('US/Eastern')
    rth_bars = df_et[df_et['timestamp_et'].dt.hour >= 9]
    if len(rth_bars) < 5:
        return None

    orb_bars = rth_bars.head(5)
    orb_high = orb_bars['high'].max()
    orb_low = orb_bars['low'].min()
    current_price = df['close'].iloc[-1]
    current_vol = df['volume'].iloc[-1]
    avg_vol = df['volume'][-20:].mean()

    if current_price > orb_high and current_vol > avg_vol * 1.5:
        return {"type": "long", "price": current_price, "stop": orb_low, "strategy": "ORB"}
    if current_price < orb_low and current_vol > avg_vol * 1.5:
        return {"type": "short", "price": current_price, "stop": orb_high, "strategy": "ORB"}
    return None

def check_vwap_pullback(df):
    if len(df) < 30:
        return None
    try:
        vwap = calculate_vwap(df)
        current_price = df['close'].iloc[-1]
        prev_price = df['close'].iloc[-2]
        current_vol = df['volume'].iloc[-1]
        avg_vol = df['volume'][-20:].mean()

        # Uptrend pullback
        if df['close'].iloc[-5] > vwap and current_price <= vwap and prev_price > vwap and current_vol > avg_vol:
            stop = df['low'].iloc[-10:].min()
            return {"type": "long", "price": current_price, "stop": stop, "strategy": "VWAP"}
        # Downtrend rally
        if df['close'].iloc[-5] < vwap and current_price >= vwap and prev_price < vwap and current_vol > avg_vol:
            stop = df['high'].iloc[-10:].max()
            return {"type": "short", "price": current_price, "stop": stop, "strategy": "VWAP"}
    except:
        pass
    return None

def check_failed_auction(df):
    if len(df) < 10:
        return None
    last3 = df.tail(3)
    # Bullish thrust → bearish rejection
    if (last3['close'].iloc[0] < last3['close'].iloc[1] and
        last3['high'].iloc[1] > last3['high'].iloc[0] and
        last3['close'].iloc[2] < last3['low'].iloc[1]):
        stop = last3['high'].max()
        return {"type": "short", "price": last3['close'].iloc[2], "stop": stop, "strategy": "FailedAuction"}
    # Bearish thrust → bullish rejection
    if (last3['close'].iloc[0] > last3['close'].iloc[1] and
        last3['low'].iloc[1] < last3['low'].iloc[0] and
        last3['close'].iloc[2] > last3['high'].iloc[1]):
        stop = last3['low'].min()
        return {"type": "long", "price": last3['close'].iloc[2], "stop": stop, "strategy": "FailedAuction"}
    return None

def check_breakout(df):
    recent = df.tail(50)
    current_price = recent['close'].iloc[-1]
    current_vol = recent['volume'].iloc[-1]
    avg_vol = recent['volume'][-20:].mean()
    lookback = recent.tail(15)
    recent_high = lookback['high'].max()
    recent_low = lookback['low'].min()

    if current_price > recent_high and current_vol > avg_vol * 1.5:
        stop = recent_low
        return {"type": "long", "price": current_price, "stop": stop, "strategy": "Breakout"}
    if current_price < recent_low and current_vol > avg_vol * 1.5:
        stop = recent_high
        return {"type": "short", "price": current_price, "stop": stop, "strategy": "Breakout"}
    return None

# ======================
# MAIN SIGNAL CHECKER
# ======================

def check_setup():
    if is_market_closed():
        print("🌙 Market closed (weekend)")
        return None
    if has_high_impact_news():
        print("🚫 Skipping: High-impact news")
        return None

    fg_index = get_fear_greed_index()
    print(f"🧠 Fear & Greed: {fg_index}")

    df = get_bars(SYMBOL, minutes=70)
    if df is None or len(df) < 20:
        print("⚠️ Not enough data")
        return None

    et = pytz.timezone('US/Eastern')
    now_et = datetime.now(pytz.utc).astimezone(et)

    # Try strategies in priority order
    signal = None

    # 1. ORB (only during US open)
    if 9 <= now_et.hour <= 10:
        signal = check_orb_setup(df, now_et)

    # 2. VWAP Pullback
    if signal is None:
        signal = check_vwap_pullback(df)

    # 3. Failed Auction
    if signal is None:
        signal = check_failed_auction(df)

    # 4. General Breakout (fallback)
    if signal is None:
        signal = check_breakout(df)

    if signal is None:
        return None

    # Apply sentiment filter
    direction = signal["type"]
    if direction == "long" and fg_index < 20:
        print("📉 Skipping long: Extreme fear (F&G < 20)")
        return None
    if direction == "short" and fg_index > 80:
        print("📈 Skipping short: Extreme greed (F&G > 80)")
        return None

    stop_dist = abs(signal["price"] - signal["stop"])
    stop_dist = max(2, min(12, stop_dist))  # clamp

    return {
        "symbol": SYMBOL.replace("/", ""),
        "direction": direction,
        "price": round(signal["price"], 1),
        "stop_dist": round(stop_dist, 1),
        "sentiment": fg_index,
        "strategy": signal["strategy"]
    }

# ======================
# ALERT & LOGGING
# ======================

def send_discord_alert(trade):
    if not DISCORD_WEBHOOK_URL:
        return
    color = 0x00ff00 if trade["direction"] == "long" else 0xff0000
    embed = {
        "title": f"{'🟢 LONG' if trade['direction'] == 'long' else '🔴 SHORT'} {trade['symbol']}",
        "description": f"Entry: {trade['price']}\nStop: {trade['price'] - trade['stop_dist'] if trade['direction']=='long' else trade['price'] + trade['stop_dist']}\nStop Dist: {trade['stop_dist']} pts",
        "color": color,
        "footer": {"text": f"{trade['strategy']} • F&G: {trade['sentiment']} • Volume Confirmed"}
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=5)
    except:
        pass

def send_startup_message():
    """Send a message to Discord when the bot starts up"""
    if not DISCORD_WEBHOOK_URL:
        return
    embed = {
        "title": "✅ MNQ/NQ Bot Started",
        "description": "24/7 breakout scanner is now active.\nScanning every 2 minutes.",
        "color": 0x00ff00,
        "footer": {"text": f"Deployed at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"}
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=5)
    except Exception as e:
        print(f"Failed to send startup message: {e}")

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
        "notes": f"{trade['strategy']} | F&G:{trade['sentiment']}"
    }
    try:
        requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=5)
    except:
        pass

# ======================
# SCHEDULER & ENDPOINTS
# ======================

def scan_and_alert():
    print(f"🔍 Scanning at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    trade = check_setup()
    if trade:
        print(f"✅ Signal: {trade}")
        send_discord_alert(trade)
        log_to_sheets(trade)
    else:
        print("⏸️ No signal")

scheduler = BackgroundScheduler()
scheduler.add_job(scan_and_alert, 'interval', minutes=1)
scheduler.start()

# Send startup notification
send_startup_message()

@app.route('/')
def home():
    return "Advanced MNQ/NQ Bot — Running 24/7"

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