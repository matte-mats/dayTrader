import requests
import time
import hmac
import hashlib
import json
import os
import numpy as np
from dotenv import load_dotenv
from flask import Flask, jsonify
import threading
from sklearn.ensemble import RandomForestRegressor

# Load API keys from .env file
load_dotenv("key.env")
API_KEY = os.getenv("BITSTAMP_API_KEY").strip()
API_SECRET = os.getenv("BITSTAMP_API_SECRET").strip()
CUSTOMER_ID = os.getenv("BITSTAMP_CUSTOMER_ID").strip()

if not API_KEY or not API_SECRET or not CUSTOMER_ID:
    raise ValueError("API keys are missing. Please check your .env file.")

BASE_URL = "https://www.bitstamp.net/api/v2"

app = Flask(__name__)
latest_action = "No action yet"
nonce_counter = int(time.time() * 1000)
transaction_log = []
TRADE_THRESHOLD = 0.005
LOOKBACK_PERIOD = 5
ALLOWED_CURRENCIES = {"usd", "btc", "eth", "xrp", "sol", "usdc", "doge", "ada", "link"}
MIN_TRADE_AMOUNT = 5  # Minimum trade amount in USD

# AI Model for Predicting Market Trends
class AICryptoManager:
    def __init__(self):
        self.model = RandomForestRegressor(n_estimators=100)
        self.price_history = {}

    def update_model(self, historical_data):
        if len(historical_data) <= LOOKBACK_PERIOD:
            return
        X = np.array([historical_data[i:i+LOOKBACK_PERIOD] for i in range(len(historical_data)-LOOKBACK_PERIOD)])
        y = np.array(historical_data[LOOKBACK_PERIOD:])
        if len(X) > 0 and len(y) > 0:
            self.model.fit(X, y.ravel())

    def predict_next_move(self, recent_prices):
        if len(recent_prices) < LOOKBACK_PERIOD:
            return None
        X = np.array(recent_prices[-LOOKBACK_PERIOD:]).reshape(1, -1)
        return self.model.predict(X)[0]


def create_signature():
    nonce = str(int(time.time() * 1000))
    message = nonce + CUSTOMER_ID + API_KEY
    signature = hmac.new(
        API_SECRET.encode('utf-8'), message.encode('utf-8'), hashlib.sha256
    ).hexdigest().upper()
    return signature, nonce


def get_balance():
    signature, nonce = create_signature()
    response = requests.post(f"{BASE_URL}/balance/", data={
        'key': API_KEY,
        'signature': signature,
        'nonce': nonce
    })
    if response.status_code == 200:
        balance_data = response.json()
        total_balance_usd = float(balance_data.get("usd_balance", 0))
        crypto_balances = {}
        for currency, amount in balance_data.items():
            if currency.endswith('_balance') and float(amount) > 0:
                crypto = currency.replace('_balance', '')
                if crypto in ALLOWED_CURRENCIES:
                    price = get_price(f"{crypto}usd")
                    if price:
                        crypto_balances[crypto] = float(amount)
                        total_balance_usd += float(amount) * price
        return {"total_balance_usd": round(total_balance_usd, 2), "crypto_balances": crypto_balances}
    return None


def get_price(pair):
    response = requests.get(f"{BASE_URL}/ticker/{pair}/")
    return round(float(response.json()["last"]), 2) if response.status_code == 200 else None


def sell_currency(from_currency, amount):
    balance = get_balance()
    if not balance or from_currency not in balance["crypto_balances"] or balance["crypto_balances"][from_currency] < amount:
        return  # Skip if not enough balance
    price = get_price(f"{from_currency}usd")
    if not price or amount * price < MIN_TRADE_AMOUNT:
        return  # Skip trades that are too small
    signature, nonce = create_signature()
    requests.post(f"{BASE_URL}/sell/{from_currency}usd/", data={
        'key': API_KEY,
        'signature': signature,
        'nonce': nonce,
        'amount': round(amount, 2),
        'price': round(price * 0.995, 2),
        'type': '1'
    })
    transaction_log.append(f"Sold {amount} {from_currency} for USD")


def buy_currency(to_currency, amount):
    balance = get_balance()
    if not balance or balance["total_balance_usd"] < amount:
        return  # Skip if not enough USD balance
    price = get_price(f"{to_currency}usd")
    if not price or amount < MIN_TRADE_AMOUNT:
        return  # Skip trades that are too small
    signature, nonce = create_signature()
    requests.post(f"{BASE_URL}/buy/{to_currency}usd/", data={
        'key': API_KEY,
        'signature': signature,
        'nonce': nonce,
        'amount': round(amount / price, 2),
        'price': round(price * 1.005, 2),
        'type': '0'
    })
    transaction_log.append(f"Bought {amount} USD worth of {to_currency}")


def ai_crypto_manager():
    ai_manager = AICryptoManager()
    while True:
        time.sleep(1800)
        balance = get_balance()
        if not balance:
            continue
        prices_data = {}
        for currency in ALLOWED_CURRENCIES:
            price = get_price(f"{currency}usd")
            if price:
                prices_data[currency] = price
        ai_manager.update_model(list(prices_data.values()))
        for currency, value in balance["crypto_balances"].items():
            prediction = ai_manager.predict_next_move(list(prices_data.values()))
            if prediction and prediction > prices_data.get(currency, 0):
                buy_currency(currency, min(value * 0.1, balance["total_balance_usd"]))
            elif value > MIN_TRADE_AMOUNT:
                sell_currency(currency, value * 0.1)

threading.Thread(target=ai_crypto_manager, daemon=True).start()

@app.route("/dashboard")
def dashboard():
    balance = get_balance()
    return jsonify({
        "latest_action": latest_action,
        "total_balance_usd": balance["total_balance_usd"] if balance else 0.0,
        "transaction_log": transaction_log
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
