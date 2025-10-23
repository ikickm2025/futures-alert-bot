from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

# === CONFIG ===
ACCOUNT_SIZE = 25000  # Your assumed account size (change as needed)
RISK_PERCENT = 0.01   # 1% risk per trade

# Futures point values
POINT_VALUES = {
    "ES": 50, "NQ": 20, "RTY": 50, "YM": 5,
    "MES": 5, "MNQ": 2, "M2K": 1, "MYM": 0.5
}

# Your Google Apps Script URL from Step 2
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "PASTE_YOUR_URL_HERE")

@app.route('/webhook', methods=['POST'])
def handle_alert():
    try:
        data = request.get_json()
        
        # Extract TradingView alert data
        symbol_raw = data.get('symbol', '').replace('CME:', '')
        symbol = symbol_raw[:2]  # e.g., "ES", "NQ"
        direction = data.get('direction', '').lower()
        entry_price = float(data.get('price', 0))
        stop_dist = float(data.get('stop_dist', 5))  # in points

        # Get point value
        point_value = POINT_VALUES.get(symbol, 1)

        # Calculate risk and size
        risk_amount = ACCOUNT_SIZE * RISK_PERCENT
        contracts = int(risk_amount / (stop_dist * point_value))
        contracts = max(1, contracts)

        # Prepare log data
        log_data = {
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "stop_dist": stop_dist,
            "contracts": contracts,
            "risk": round(risk_amount, 2),
            "notes": f"ATR-based setup from TV"
        }

        # Send to Google Sheets
        if GOOGLE_SCRIPT_URL != "https://script.google.com/macros/s/AKfycbx7ofYAJuxZfit75VNi2rLxq0tFPqwgeVYlaCjdYG4lZJvJPJS9e5BVv2ChqvhBDsOQvQ/exec":
            requests.post(GOOGLE_SCRIPT_URL, json=log_data)

        return jsonify({"status": "logged", "contracts": contracts}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Health check
@app.route('/')
def home():
    return "Futures Alert Bot is running!"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))