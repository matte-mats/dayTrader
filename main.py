import requests
import time
import hmac
import hashlib
import json
import os
import numpy as np
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify
import threading

# Load API keys from .env file
load_dotenv("key.env")
API_KEY = os.getenv("BITSTAMP_API_KEY").strip()
API_SECRET = os.getenv("BITSTAMP_API_SECRET").strip()
CUSTOMER_ID = os.getenv("BITSTAMP_CUSTOMER_ID").strip()

# Check if API keys are loaded correctly
if not API_KEY or not API_SECRET or not CUSTOMER_ID:
    raise ValueError("API keys are missing. Please check your .env file.")

BASE_URL = "https://www.bitstamp.net/api/v2"

app = Flask(__name__)
prices = {}  # Dictionary to store historical prices per currency
latest_action = "No action yet"
nonce_counter = int(time.time() * 1000)  # Ensure unique nonce
last_trade_time = 0  # Timestamp for last trade execution
trade_interval = 1800  # Minimum time between trades (30 min)
transaction_log = []  # Store transaction history

# Initial portfolio setup
INITIAL_CURRENCIES = ["eth", "ltc", "xrp", "ada"]  # Buy evenly among four currencies
MIN_ORDER_SIZE_BTC = 0.0002  # Minimum order size requirement

# Trading configuration
TRADE_THRESHOLD = 0.005  # Adjusted minimum percentage change to trigger a trade
LOOKBACK_PERIOD = 5  # Number of past price points to analyze
MIN_BALANCE_USD = 10.0  # Minimum equivalent value to trade


# Helper function to create signature
def create_signature():
    global nonce_counter
    nonce_counter += 1
    nonce = str(nonce_counter)
    message = (nonce + CUSTOMER_ID + API_KEY).encode('utf-8')
    secret = API_SECRET.encode('utf-8')
    signature = hmac.new(secret, msg=message, digestmod=hashlib.sha256).hexdigest().upper()
    return nonce, signature

# Get current price
def get_price(pair):
    response = requests.get(f"{BASE_URL}/ticker/{pair}/")
    if response.status_code == 200:
        data = response.json()
        return float(data["last"])
    return None

# Initialize portfolio
def initialize_portfolio():
    balance = get_balance()
    if "btc" in balance:
        btc_amount = balance["btc"] / len(INITIAL_CURRENCIES)
        if btc_amount >= MIN_ORDER_SIZE_BTC:
            for currency in INITIAL_CURRENCIES:
                pair = f"{currency}btc"
                price = get_price(pair)
                if price:
                    amount = round(btc_amount / price, 8)  # Calculate how much currency to buy
                    print(f"Buying {amount} {currency} using {btc_amount} BTC")
                    buy_currency("btc", currency, amount)
                else:
                    print(f"Error fetching price for {pair}, skipping buy.")
        else:
            print("Not enough BTC to place the minimum order for all currencies.")


# Get total account value in USD
def get_total_value_in_usd():
    balance = get_balance()
    total_value = 0.0
    for currency, amount in balance.items():
        if currency == "usd":
            total_value += amount
        else:
            ticker_url = f"{BASE_URL}/ticker/{currency}usd/"
            response = requests.get(ticker_url)
            if response.status_code == 200:
                try:
                    price_data = response.json()
                    total_value += amount * float(price_data['last'])
                except Exception as e:
                    print(f"Error fetching price for {currency}: {e}")
    return round(total_value, 2)


# Get list of valid trading pairs
def get_all_trading_pairs():
    response = requests.get(f"{BASE_URL}/trading-pairs-info/")
    valid_pairs = []
    if response.status_code == 200:
        try:
            data = response.json()
            for pair in data:
                if pair.get("trading") == "Enabled":  # Ensure trading is enabled
                    valid_pairs.append(pair["name"].replace("/", "").lower())
        except Exception as e:
            print("Error parsing trading pairs:", e)
    return valid_pairs


# Get account balance
def get_balance():
    nonce, signature = create_signature()
    payload = {'key': API_KEY, 'signature': signature, 'nonce': nonce}
    response = requests.post(f"{BASE_URL}/account_balances/", data=payload)
    try:
        balance_data = response.json()
        if isinstance(balance_data, list):
            balance_dict = {item['currency'].lower(): float(item['available']) for item in balance_data if
                            float(item['available']) > 0}
            return balance_dict
        return balance_data
    except Exception as e:
        print("Error parsing balance data:", e)
        return {"error": "Failed to parse balance data"}

# Get the USD value of a given currency
def get_usd_value(currency, amount):
    pair = f"{currency}usd"
    price = get_price(pair)
    if price:
        return amount * price
    return 0


# Place a market buy order
def buy_currency(from_currency, to_currency, amount):
    pair = f"{to_currency}{from_currency}".lower()
    amount = round(amount, 8)  # Ensure max 8 decimal places
    if amount < MIN_BALANCE_USD:
        print(f"Skipping buy: Amount {amount} is below minimum trade value.")
        return None
    nonce, signature = create_signature()
    payload = {
        'key': API_KEY,
        'signature': signature,
        'nonce': nonce,
        'amount': amount,
    }
    response = requests.post(f"{BASE_URL}/buy/market/{pair}/", data=payload)
    result = response.json()
    transaction_log.append(
        {"action": "buy", "from": from_currency, "to": to_currency, "amount": amount, "result": result})
    print(f"Buy order executed: {result}")
    return result


# Place a market sell order
def sell_currency(from_currency, to_currency, amount):
    pair = f"{to_currency}{from_currency}".lower()
    amount = round(amount, 8)  # Ensure max 8 decimal places
    nonce, signature = create_signature()
    payload = {
        'key': API_KEY,
        'signature': signature,
        'nonce': nonce,
        'amount': amount,
    }
    response = requests.post(f"{BASE_URL}/sell/market/{pair}/", data=payload)
    result = response.json()
    transaction_log.append(
        {"action": "sell", "from": from_currency, "to": to_currency, "amount": amount, "result": result})
    print(f"Sell order executed: {result}")
    return result


# Trading logic with validation of valid trading pairs
def trading_loop():
    global prices, last_trade_time
    available_pairs = get_all_trading_pairs()
    valid_currencies = set([pair.replace("usd", "") for pair in available_pairs if pair.endswith("usd")])
    while True:
        print("Running trading logic...")
        try:
            balance = get_balance()
            tradable_currencies = [currency for currency in balance if currency not in ["usd", "eur", "btc"]]

            price_changes = {}
            for pair in available_pairs:
                if pair.endswith("usd"):  # Only consider USD pairs
                    currency = pair.replace("usd", "")
                    price = get_price(pair)
                    if price:
                        if currency not in prices:
                            prices[currency] = [price]
                        else:
                            prices[currency].append(price)
                            if len(prices[currency]) > LOOKBACK_PERIOD:
                                prices[currency].pop(0)

                        if len(prices[currency]) >= LOOKBACK_PERIOD:
                            avg_price = np.mean(prices[currency])
                            price_change = (prices[currency][-1] - avg_price) / avg_price
                            price_changes[currency] = price_change
                            print(
                                f"Price analysis for {currency}: current={prices[currency][-1]}, avg={avg_price}, change={price_change:.4f}")

            if price_changes:
                worst_currency = min([c for c in tradable_currencies if c in price_changes], key=price_changes.get,
                                     default=None)
                best_currency = max(price_changes, key=price_changes.get)

                if worst_currency and best_currency and worst_currency in balance:
                    usd_value = get_usd_value(worst_currency, balance[worst_currency])
                    if usd_value >= MIN_BALANCE_USD:
                        print(f"Swapping {worst_currency} to {best_currency} for optimization.")
                        sell_currency(worst_currency, "usd", balance[worst_currency])
                        balance = get_balance()  # Refresh balance after selling
                        if "usd" in balance and balance["usd"] >= MIN_BALANCE_USD:
                            buy_currency("usd", best_currency, balance["usd"])
                        last_trade_time = time.time()
                    else:
                        print("Skipping trade: Insufficient balance for trading.")
        except Exception as e:
            print("Error in trading loop:", e)
        time.sleep(60)


# Flask routes
@app.route('/')
def dashboard():
    return jsonify({"total_value": get_total_value_in_usd(), "current_holdings": get_balance()})

@app.route('/transactions')
def transactions():
    return jsonify(transaction_log)

if __name__ == "__main__":
    print("Checking API Keys...")
    print("Account Balance:", get_balance())
    initialize_portfolio()  # Ensure BTC is converted at start
    trading_thread = threading.Thread(target=trading_loop, daemon=True)
    trading_thread.start()
    app.run(debug=True)
