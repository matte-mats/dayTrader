import hashlib
import hmac
import os
import threading
import time
from typing import Optional

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify

import os

print("Current directory:", os.getcwd())
print("key.env exists:", os.path.exists("key.env"))
# -------------------------
# Configuration for raspberry pie
# -------------------------
load_dotenv("key.env")

print("API_KEY =", os.getenv("BITSTAMP_API_KEY"))
print("SECRET =", os.getenv("BITSTAMP_API_SECRET"))
print("CUSTOMER =", os.getenv("BITSTAMP_CUSTOMER_ID"))

API_KEY = os.getenv("BITSTAMP_API_KEY")
API_SECRET = os.getenv("BITSTAMP_API_SECRET")
CUSTOMER_ID = os.getenv("BITSTAMP_CUSTOMER_ID")

if not API_KEY or not API_SECRET or not CUSTOMER_ID:
    raise ValueError("API keys are missing. Please check your key.env file.")

BASE_URL = "https://www.bitstamp.net/api/v2"

LOOKBACK_PERIOD = 12             # 12 price points
PRICE_UPDATE_SECONDS = 300       # every 5 minutes
BUY_THRESHOLD = 0.003            # +0.3% over lookback period
SELL_THRESHOLD = -0.001          # -0.1% over lookback period
TRADE_PERCENTAGE = 0.75          # trade 75% of available USD/BTC
MIN_TRADE_AMOUNT_USD = 5         # Bitstamp minimum safety limit
MAX_LOG_ITEMS = 100

app = Flask(__name__)
transaction_log: list[str] = []
price_history: list[float] = []
latest_action = "No action yet"


# -------------------------
# Helpers
# -------------------------
def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"{timestamp} - {message}"
    print(entry, flush=True)
    transaction_log.append(entry)
    del transaction_log[:-MAX_LOG_ITEMS]


def create_signature() -> tuple[str, str]:
    nonce = str(int(time.time() * 1000))
    message = nonce + CUSTOMER_ID + API_KEY
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()
    return signature, nonce


def private_post(endpoint: str, data: Optional[dict] = None) -> Optional[dict]:
    signature, nonce = create_signature()
    payload = {
        "key": API_KEY,
        "signature": signature,
        "nonce": nonce,
    }
    if data:
        payload.update(data)

    try:
        response = requests.post(f"{BASE_URL}/{endpoint}/", data=payload, timeout=20)
        result = response.json()
    except Exception as exc:
        log(f"API error at {endpoint}: {exc}")
        return None

    if response.status_code != 200:
        log(f"API HTTP error at {endpoint}: {response.status_code} {result}")
        return None

    return result


def public_get(endpoint: str) -> Optional[dict]:
    try:
        response = requests.get(f"{BASE_URL}/{endpoint}/", timeout=20)
        result = response.json()
    except Exception as exc:
        log(f"API error at {endpoint}: {exc}")
        return None

    if response.status_code != 200:
        log(f"API HTTP error at {endpoint}: {response.status_code} {result}")
        return None

    return result


# -------------------------
# Market and account data
# -------------------------
def get_price(pair: str = "btcusd") -> Optional[float]:
    data = public_get(f"ticker/{pair}")
    if not data or "last" not in data:
        return None

    try:
        return float(data["last"])
    except ValueError:
        return None


def update_btc_price_history() -> Optional[float]:
    price = get_price("btcusd")
    if price is None:
        log("Could not fetch BTC/USD price")
        return None

    price_history.append(price)
    if len(price_history) > LOOKBACK_PERIOD:
        price_history.pop(0)

    return price


def btc_trend() -> Optional[float]:
    if len(price_history) < LOOKBACK_PERIOD:
        return None

    first = price_history[0]
    last = price_history[-1]

    if first <= 0:
        return None

    return (last - first) / first


def get_balance() -> Optional[dict]:
    data = private_post("balance")
    if not data:
        return None

    balances: dict[str, float] = {}
    for key, value in data.items():
        if key.endswith("_balance"):
            currency = key.replace("_balance", "")
            try:
                amount = float(value)
            except ValueError:
                continue
            if amount > 0:
                balances[currency] = amount

    return {
        "usd": float(data.get("usd_balance", 0)),
        "btc": float(data.get("btc_balance", 0)),
        "balances": balances,
    }


# -------------------------
# Trading
# -------------------------
def buy_btc(usd_amount: float) -> bool:
    global latest_action

    if usd_amount < MIN_TRADE_AMOUNT_USD:
        log(f"Skipped BTC buy: amount too low (${usd_amount:.2f})")
        return False

    price = get_price("btcusd")
    if not price:
        log("Skipped BTC buy: missing BTC price")
        return False

    btc_amount = usd_amount / price
    result = private_post(
        "buy/btcusd",
        {
            "amount": round(btc_amount, 8),
            "price": round(price * 1.005, 2),
            "type": "1",
        },
    )

    if not result:
        log("BTC buy failed")
        return False

    latest_action = f"Bought BTC for ${usd_amount:.2f}"
    log(f"Bought {btc_amount:.8f} BTC for about ${usd_amount:.2f}: {result}")
    return True


def sell_btc(btc_amount: float) -> bool:
    global latest_action

    price = get_price("btcusd")
    if not price:
        log("Skipped BTC sell: missing BTC price")
        return False

    usd_value = btc_amount * price
    if usd_value < MIN_TRADE_AMOUNT_USD:
        log(f"Skipped BTC sell: value too low (${usd_value:.2f})")
        return False

    result = private_post(
        "sell/btcusd",
        {
            "amount": round(btc_amount, 8),
            "price": round(price * 0.995, 2),
            "type": "1",
        },
    )

    if not result:
        log("BTC sell failed")
        return False

    latest_action = f"Sold {btc_amount:.8f} BTC"
    log(f"Sold {btc_amount:.8f} BTC for about ${usd_value:.2f}: {result}")
    return True


def sell_currency_to_usd(currency: str, amount: float) -> bool:
    if currency in {"usd", "btc"}:
        return False

    pair = f"{currency}usd"
    price = get_price(pair)
    if not price:
        log(f"Skipped selling {currency}: no {pair} price")
        return False

    usd_value = amount * price
    if usd_value < MIN_TRADE_AMOUNT_USD:
        log(f"Skipped selling {currency}: value too low (${usd_value:.2f})")
        return False

    result = private_post(
        f"sell/{pair}",
        {
            "amount": round(amount, 8),
            "price": round(price * 0.995, 8 if price < 0.01 else 2),
            "type": "1",
        },
    )

    if not result:
        log(f"Selling {currency} failed")
        return False

    log(f"Sold {amount:.8f} {currency.upper()} to USD: {result}")
    return True


def sell_all_non_btc_to_usd() -> None:
    balance = get_balance()
    if not balance:
        return

    for currency, amount in balance["balances"].items():
        if currency not in {"usd", "btc"}:
            sell_currency_to_usd(currency, amount)


def trade_logic() -> None:
    sell_all_non_btc_to_usd()

    price = update_btc_price_history()
    if price is None:
        return

    trend = btc_trend()
    if trend is None:
        log(f"Waiting for BTC history: {len(price_history)}/{LOOKBACK_PERIOD}")
        return

    balance = get_balance()
    if not balance:
        return

    usd_balance = balance["usd"]
    btc_amount = balance["btc"]
    btc_value = btc_amount * price

    log(
        f"BTC trend {trend:.4%}, price ${price:.2f}, "
        f"USD ${usd_balance:.2f}, BTC value ${btc_value:.2f}"
    )

    if trend > BUY_THRESHOLD and usd_balance >= MIN_TRADE_AMOUNT_USD:
        buy_btc(usd_balance * TRADE_PERCENTAGE)
    elif trend < SELL_THRESHOLD and btc_value >= MIN_TRADE_AMOUNT_USD:
        sell_btc(btc_amount * TRADE_PERCENTAGE)
    else:
        log("No trade: trend not strong enough")


# -------------------------
# Background loop and dashboard
# -------------------------
def trading_bot() -> None:
    log("BTC trading bot started")
    while True:
        try:
            trade_logic()
        except Exception as exc:
            log(f"Unexpected bot error: {exc}")
        time.sleep(PRICE_UPDATE_SECONDS)


threading.Thread(target=trading_bot, daemon=True).start()


@app.route("/dashboard")
def dashboard():
    return jsonify(
        {
            "latest_action": latest_action,
            "balance": get_balance(),
            "btc_price_history": price_history,
            "transaction_log": transaction_log,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
