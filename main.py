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
from collections import deque

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
TRADE_CURRENCIES = {"btc", "eth", "xrp", "sol", "ltc", "doge", "ada", "hbar", "link", "matic",
                    "xlm", "popcat", "avax", "sui", "smt", "near", "fet"}

# Store historical price data
price_history = {currency: deque(maxlen=LOOKBACK_PERIOD) for currency in TRADE_CURRENCIES}
MAX_CRYPTO_HOLDINGS = 5  # Maximum allowed crypto holdings

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
    if response.status_code != 200:
        return None

    price = round(float(response.json()["last"]), 8 if "shib" in pair else 2)

    currency = pair.replace("usd", "")

    # Spara endast historik för BTC
    if currency == "btc":
        price_history["btc"].append(price)
        if len(price_history["btc"]) > LOOKBACK_PERIOD:
            price_history["btc"].pop(0)

    return price


TRADE_CURRENCIES = {"btc"}
LOOKBACK_PERIOD = 12          # 12 datapunkter
PRICE_UPDATE_SECONDS = 300    # var 5:e minut
BUY_THRESHOLD = 0.003         # +0.1%
SELL_THRESHOLD = -0.001       # -0.1%
TRADE_PERCENTAGE = 0.75
MIN_TRADE_AMOUNT = 5

price_history = {"btc": []}


def update_btc_price_history():
    get_price("btcusd")


def btc_trend():
    prices = price_history["btc"]
    if len(prices) < LOOKBACK_PERIOD:
        return None

    first = prices[0]
    last = prices[-1]

    if first <= 0:
        return None

    return (last - first) / first


def sell_all_non_btc_to_usd():
    balance = get_balance()
    if not balance:
        return

    for currency, amount in balance["crypto"].items():
        if currency not in {"btc", "usd"}:
            pair = f"{currency}usd"
            price = get_price(pair)
            if price and amount * price >= MIN_TRADE_AMOUNT:
                sell_currency(currency, amount)


def trade_logic():
    sell_all_non_btc_to_usd()

    update_btc_price_history()
    trend = btc_trend()

    if trend is None:
        transaction_log.append("Waiting for enough BTC price history")
        return

    balance = get_balance()
    if not balance:
        return

    usd_balance = balance["usd"]
    btc_amount = balance["crypto"].get("btc", 0)
    btc_price = get_price("btcusd")
    btc_value = btc_amount * btc_price if btc_price else 0

    transaction_log.append(f"BTC trend: {trend:.4%}")

    if trend > BUY_THRESHOLD and usd_balance >= MIN_TRADE_AMOUNT:
        buy_currency("btc", usd_balance * TRADE_PERCENTAGE)

    elif trend < SELL_THRESHOLD and btc_value >= MIN_TRADE_AMOUNT:
        sell_currency("btc", btc_amount * TRADE_PERCENTAGE)

    else:
        transaction_log.append("No trade: trend not strong enough")

def update_price_history():
    print("update price history")
    while True:
        for currency in TRADE_CURRENCIES:
            get_price(f"{currency}usd")
        time.sleep(300)  # Uppdatera var 5:e minut


def predict_trend():
    trends = {}
    for currency in TRADE_CURRENCIES:
        print(f"Checking {currency}, history length: {len(price_history[currency])}")
        if len(price_history[currency]) < LOOKBACK_PERIOD:
            continue  # Not enough data

        X = np.arange(len(price_history[currency])).reshape(-1, 1)
        y = np.array(price_history[currency])

        model = RandomForestRegressor(n_estimators=50)
        model.fit(X, y)
        prediction = model.predict([[len(price_history[currency])]])

        # Säkerhetskontroll: Undvik NaN eller delning med noll
        last_price = price_history[currency][-1]
        if last_price == 0 or np.isnan(prediction[0]):
            print("Undvik NaN eller delning med noll")
            continue

        trends[currency] = (prediction[0] - last_price) / last_price

    if not trends:
        print("ingen trend")
        return None, None

    to_sell = min(trends, key=trends.get)  # Mest negativ trend
    to_buy = max(trends, key=trends.get)  # Mest positiv trend

    return to_sell, to_buy

def buy_currency(currency, amount):
    print("in buy_currency")
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
    print("in sell_currency")
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
        time.sleep(1200)  # Run every twenty minutes


threading.Thread(target=trading_bot, daemon=True).start()
threading.Thread(target=update_price_history, daemon=True).start()

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
