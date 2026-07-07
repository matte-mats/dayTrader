import hashlib
import hmac
import os
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / "key.env")

API_KEY = os.getenv("BITSTAMP_API_KEY")
API_SECRET = os.getenv("BITSTAMP_API_SECRET")
CUSTOMER_ID = os.getenv("BITSTAMP_CUSTOMER_ID")

if not API_KEY:
    raise ValueError("BITSTAMP_API_KEY saknas i key.env")
if not API_SECRET:
    raise ValueError("BITSTAMP_API_SECRET saknas i key.env")
if not CUSTOMER_ID:
    raise ValueError("BITSTAMP_CUSTOMER_ID saknas i key.env")

BASE_URL = "https://www.bitstamp.net/api/v2"

app = Flask(__name__)

transaction_log = []
latest_action = "No action yet"

LOOKBACK_PERIOD = 12
PRICE_UPDATE_SECONDS = 300
BUY_THRESHOLD = 0.003
SELL_THRESHOLD = -0.001
TRADE_PERCENTAGE = 0.75
MIN_TRADE_AMOUNT = 5.0

price_history = {"btc": []}


def log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = "{} - {}".format(timestamp, message)
    print(entry)
    transaction_log.append(entry)

    if len(transaction_log) > 100:
        del transaction_log[:-100]


def create_signature():
    nonce = str(int(time.time() * 1000))
    message = nonce + CUSTOMER_ID + API_KEY
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest().upper()
    return signature, nonce


def bitstamp_post(endpoint, data=None):
    signature, nonce = create_signature()

    payload = {
        "key": API_KEY,
        "signature": signature,
        "nonce": nonce
    }

    if data:
        payload.update(data)

    try:
        response = requests.post(BASE_URL + endpoint, data=payload, timeout=15)
        return response
    except requests.RequestException as exc:
        log("Bitstamp POST error {}: {}".format(endpoint, exc))
        return None


def bitstamp_get(endpoint):
    try:
        response = requests.get(BASE_URL + endpoint, timeout=15)
        return response
    except requests.RequestException as exc:
        log("Bitstamp GET error {}: {}".format(endpoint, exc))
        return None


def get_price(pair):
    response = bitstamp_get("/ticker/{}/".format(pair))
    if not response or response.status_code != 200:
        log("Could not get price for {}".format(pair))
        return None

    try:
        price = float(response.json()["last"])
    except (ValueError, KeyError):
        log("Invalid price response for {}".format(pair))
        return None

    if pair == "btcusd":
        price_history["btc"].append(price)
        if len(price_history["btc"]) > LOOKBACK_PERIOD:
            price_history["btc"].pop(0)

    return price


def get_balance():
    response = bitstamp_post("/balance/")
    if not response or response.status_code != 200:
        log("Could not get balance")
        return None

    try:
        balance_data = response.json()
    except ValueError:
        log("Invalid balance response")
        return None

    crypto_balances = {}
    for key, value in balance_data.items():
        if key.endswith("_balance"):
            currency = key.replace("_balance", "")
            try:
                amount = float(value)
            except ValueError:
                continue

            if amount > 0:
                crypto_balances[currency] = amount

    try:
        usd_balance = float(balance_data.get("usd_balance", 0))
    except ValueError:
        usd_balance = 0.0

    return {
        "usd": usd_balance,
        "crypto": crypto_balances
    }


def btc_trend():
    prices = price_history["btc"]

    if len(prices) < LOOKBACK_PERIOD:
        return None

    first = prices[0]
    last = prices[-1]

    if first <= 0:
        return None

    return (last - first) / first


def buy_currency(currency, usd_amount):
    price = get_price("{}usd".format(currency))
    if not price:
        return False

    if usd_amount < MIN_TRADE_AMOUNT:
        log("Skipped buying {}: amount too low ({:.2f} USD)".format(currency, usd_amount))
        return False

    crypto_amount = usd_amount / price

    response = bitstamp_post(
        "/buy/{}usd/".format(currency),
        {
            "amount": round(crypto_amount, 6),
            "price": round(price * 1.005, 2),
            "type": "1"
        }
    )

    if response and response.status_code == 200:
        log("Bought {:.8f} {} for {:.2f} USD".format(crypto_amount, currency, usd_amount))
        return True

    log("Buy order failed for {}: {}".format(currency, response.text if response else "no response"))
    return False


def sell_currency(currency, amount):
    price = get_price("{}usd".format(currency))
    if not price:
        return False

    usd_value = amount * price
    if usd_value < MIN_TRADE_AMOUNT:
        log("Skipped selling {}: value too low ({:.2f} USD)".format(currency, usd_value))
        return False

    response = bitstamp_post(
        "/sell/{}usd/".format(currency),
        {
            "amount": round(amount, 6),
            "price": round(price * 0.995, 2),
            "type": "1"
        }
    )

    if response and response.status_code == 200:
        log("Sold {:.8f} {} for approximately {:.2f} USD".format(amount, currency, usd_value))
        return True

    log("Sell order failed for {}: {}".format(currency, response.text if response else "no response"))
    return False


def sell_all_non_btc_to_usd():
    balance = get_balance()
    if not balance:
        return

    for currency, amount in balance["crypto"].items():
        if currency in ("btc", "usd"):
            continue

        pair = "{}usd".format(currency)
        price = get_price(pair)

        if price and amount * price >= MIN_TRADE_AMOUNT:
            log("Converting {} to USD before BTC strategy".format(currency))
            sell_currency(currency, amount)


def trade_logic():
    global latest_action

    sell_all_non_btc_to_usd()

    btc_price = get_price("btcusd")
    if not btc_price:
        latest_action = "Could not fetch BTC price"
        return

    trend = btc_trend()
    if trend is None:
        latest_action = "Waiting for enough BTC price history"
        log("{} ({}/{})".format(latest_action, len(price_history["btc"]), LOOKBACK_PERIOD))
        return

    balance = get_balance()
    if not balance:
        latest_action = "Could not fetch balance"
        return

    usd_balance = balance["usd"]
    btc_amount = balance["crypto"].get("btc", 0.0)
    btc_value = btc_amount * btc_price

    log("BTC trend: {:.4%}, USD: {:.2f}, BTC value: {:.2f}".format(
        trend, usd_balance, btc_value
    ))

    if trend > BUY_THRESHOLD and usd_balance >= MIN_TRADE_AMOUNT:
        latest_action = "BTC trend positive, buying BTC"
        buy_currency("btc", usd_balance * TRADE_PERCENTAGE)

    elif trend < SELL_THRESHOLD and btc_value >= MIN_TRADE_AMOUNT:
        latest_action = "BTC trend negative, selling BTC"
        sell_currency("btc", btc_amount * TRADE_PERCENTAGE)

    else:
        latest_action = "No trade: BTC trend not strong enough"
        log(latest_action)


def trading_bot():
    log("Trading bot started")
    while True:
        try:
            trade_logic()
        except Exception as exc:
            log("Unexpected error in trading_bot: {}".format(exc))

        time.sleep(PRICE_UPDATE_SECONDS)


@app.route("/dashboard")
def dashboard():
    balance = get_balance()
    return jsonify({
        "latest_action": latest_action,
        "balance": balance,
        "btc_price_history_count": len(price_history["btc"]),
        "transaction_log": transaction_log
    })


@app.route("/")
def home():
    return jsonify({
        "message": "BTC trading bot is running",
        "dashboard": "/dashboard"
    })


if __name__ == "__main__":
    threading.Thread(target=trading_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
