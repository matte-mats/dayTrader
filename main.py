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
TRADE_THRESHOLD = 0.001  # Adjusted to 0.1%
LOOKBACK_PERIOD = 10  # Increased to 10 data points
MIN_TRADE_AMOUNT = 5  # Minimum trade amount in USD
TRADE_PERCENTAGE = 0.5  # Increased trade percentage to 50%

# Allowed trading currencies
TRADE_CURRENCIES = {"btc", "eth", "xrp", "sol", "ltc", "doge", "ada", "hbar", "link", "shib", "matic"}


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
        crypto_balances = {currency.replace('_balance', ''): float(amount)
                           for currency, amount in balance_data.items()
                           if currency.endswith('_balance') and float(amount) > 0}
        return {"usd": float(balance_data.get("usd_balance", 0)), "crypto": crypto_balances}
    return None


def get_price(pair):
    response = requests.get(f"{BASE_URL}/ticker/{pair}/")
    return round(float(response.json()["last"]), 2) if response.status_code == 200 else None


def trade_logic():
    balance = get_balance()
    if not balance:
        return

    crypto_balances = balance["crypto"]
    usd_balance = balance["usd"]
    active_currencies = set(crypto_balances.keys()) & TRADE_CURRENCIES

    if len(active_currencies) < 3:
        # Buy more currencies to reach at least 3 different holdings
        to_buy = TRADE_CURRENCIES - active_currencies
        if usd_balance > MIN_TRADE_AMOUNT and to_buy:
            currency = to_buy.pop()
            buy_currency(currency, usd_balance * TRADE_PERCENTAGE)
    else:
        # Sell one and buy another to rotate holdings
        to_sell = list(active_currencies)[0]  # Pick first currency to sell
        to_buy = (TRADE_CURRENCIES - active_currencies).pop()  # Pick new currency to buy
        sell_currency(to_sell, crypto_balances[to_sell] * TRADE_PERCENTAGE)
        if usd_balance > MIN_TRADE_AMOUNT:
            buy_currency(to_buy, usd_balance * TRADE_PERCENTAGE)


def buy_currency(currency, amount):
    price = get_price(f"{currency}usd")
    if not price or amount < MIN_TRADE_AMOUNT:
        transaction_log.append(f"Skipped buying {currency} due to low trade amount")
        return
    signature, nonce = create_signature()
    requests.post(f"{BASE_URL}/buy/{currency}usd/", data={
        'key': API_KEY,
        'signature': signature,
        'nonce': nonce,
        'amount': round(amount / price, 6),
        'price': round(price * 1.005, 2),
        'type': '1'
    })
    transaction_log.append(f"Bought {amount / price} {currency} for USD")


def sell_currency(currency, amount):
    price = get_price(f"{currency}usd")
    if not price or amount * price < MIN_TRADE_AMOUNT:
        transaction_log.append(f"Skipped selling {currency} due to low trade amount")
        return
    signature, nonce = create_signature()
    requests.post(f"{BASE_URL}/sell/{currency}usd/", data={
        'key': API_KEY,
        'signature': signature,
        'nonce': nonce,
        'amount': round(amount, 6),
        'price': round(price * 0.995, 2),
        'type': '1'
    })
    transaction_log.append(f"Sold {amount} {currency} for USD")


def trading_bot():
    while True:
        trade_logic()
        time.sleep(3600)  # Run every hour


threading.Thread(target=trading_bot, daemon=True).start()


@app.route("/dashboard")
def dashboard():
    balance = get_balance()
    return jsonify({
        "latest_action": latest_action,
        "balance": balance,
        "transaction_log": transaction_log
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
