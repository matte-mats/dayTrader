import csv
import hashlib
import hmac
import os
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, Response


BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "trading_history.csv"
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
latest_reason = "Botten har inte fattat något beslut ännu"
latest_signal = {
    "action": "HOLD",
    "reason": latest_reason,
    "timestamp": None
}

# ============================================================
# Calmer strategy
#
# One price sample every 15 minutes.
#  8 samples  = 2-hour average
# 32 samples  = 8-hour average
# 96 samples  = 24-hour average
#
# A signal must be repeated three times before a trade.
# At least six hours must pass between completed trades.
# Instead of trading 75%, the bot moves toward target exposure.
# ============================================================

PRICE_UPDATE_SECONDS = 900

FAST_WINDOW = 8
SLOW_WINDOW = 32
LONG_WINDOW = 96
MAX_PRICE_HISTORY = LONG_WINDOW

BUY_BUFFER = 0.005          # Fast average must be 0.5% above slow average
SELL_BUFFER = 0.007         # Fast average must be 0.7% below slow average
CONFIRMATION_CYCLES = 3     # Signal must remain for 45 minutes
TRADE_COOLDOWN_SECONDS = 6 * 60 * 60

BUY_TARGET_BTC_EXPOSURE = 0.65
SELL_TARGET_BTC_EXPOSURE = 0.20
MIN_TRADE_AMOUNT = 10.0

price_history = {"btc": []}
last_trade_time = 0.0
pending_signal = None
pending_signal_count = 0

state_lock = threading.Lock()


def log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = "{} - {}".format(timestamp, message)
    print(entry)

    with state_lock:
        transaction_log.append(entry)
        if len(transaction_log) > 150:
            del transaction_log[:-150]


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
        return requests.post(
            BASE_URL + endpoint,
            data=payload,
            timeout=15
        )
    except requests.RequestException as exc:
        log("Bitstamp POST error {}: {}".format(endpoint, exc))
        return None


def bitstamp_get(endpoint):
    try:
        return requests.get(BASE_URL + endpoint, timeout=15)
    except requests.RequestException as exc:
        log("Bitstamp GET error {}: {}".format(endpoint, exc))
        return None


def get_price(pair, store_history=False):
    """
    Fetches a current price.

    Only the trading loop calls this with store_history=True.
    Dashboard requests and order validation no longer distort
    the evenly spaced price history.
    """
    response = bitstamp_get("/ticker/{}/".format(pair))
    if not response or response.status_code != 200:
        log("Could not get price for {}".format(pair))
        return None

    try:
        price = float(response.json()["last"])
    except (ValueError, KeyError):
        log("Invalid price response for {}".format(pair))
        return None

    if pair == "btcusd" and store_history:
        with state_lock:
            price_history["btc"].append(price)
            if len(price_history["btc"]) > MAX_PRICE_HISTORY:
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


def get_portfolio_snapshot(btc_price=None):
    balance = get_balance()
    if not balance:
        return None

    if btc_price is None:
        btc_price = get_price("btcusd", store_history=False)

    if not btc_price:
        return None

    usd_balance = balance["usd"]
    btc_balance = balance["crypto"].get("btc", 0.0)
    btc_value = btc_balance * btc_price
    portfolio_value = usd_balance + btc_value

    exposure = 0.0
    if portfolio_value > 0:
        exposure = btc_value / portfolio_value

    return {
        "usd_balance": usd_balance,
        "btc_balance": btc_balance,
        "btc_price": btc_price,
        "btc_value_usd": btc_value,
        "portfolio_value_usd": portfolio_value,
        "btc_exposure": exposure
    }


def simple_average(values, window):
    if len(values) < window:
        return None

    selected = values[-window:]
    return sum(selected) / float(window)


def calculate_indicators():
    with state_lock:
        prices = list(price_history["btc"])

    fast_ma = simple_average(prices, FAST_WINDOW)
    slow_ma = simple_average(prices, SLOW_WINDOW)
    long_ma = simple_average(prices, LONG_WINDOW)

    return {
        "sample_count": len(prices),
        "fast_ma": fast_ma,
        "slow_ma": slow_ma,
        "long_ma": long_ma
    }


def determine_signal(indicators):
    fast_ma = indicators["fast_ma"]
    slow_ma = indicators["slow_ma"]
    long_ma = indicators["long_ma"]

    if fast_ma is None or slow_ma is None:
        return "HOLD", "Väntar på minst {} jämna prisprover".format(SLOW_WINDOW)

    buy_level = slow_ma * (1.0 + BUY_BUFFER)
    sell_level = slow_ma * (1.0 - SELL_BUFFER)

    # During the first 24 hours, the long average is not available.
    # We allow signals, but require the stronger fast/slow buffer.
    long_buy_ok = long_ma is None or slow_ma >= long_ma
    long_sell_ok = long_ma is None or slow_ma <= long_ma

    if fast_ma > buy_level and long_buy_ok:
        return (
            "BUY",
            "2h-snittet ligger tydligt över 8h-snittet"
        )

    if fast_ma < sell_level and long_sell_ok:
        return (
            "SELL",
            "2h-snittet ligger tydligt under 8h-snittet"
        )

    return "HOLD", "Glidande medelvärden ger ingen tillräckligt tydlig trend"


def update_confirmation(signal):
    global pending_signal
    global pending_signal_count

    if signal not in ("BUY", "SELL"):
        pending_signal = None
        pending_signal_count = 0
        return 0

    if signal == pending_signal:
        pending_signal_count += 1
    else:
        pending_signal = signal
        pending_signal_count = 1

    return pending_signal_count


def ensure_history_file():
    if HISTORY_FILE.exists():
        return

    with HISTORY_FILE.open("w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow([
            "timestamp",
            "btc_price",
            "portfolio_value_usd",
            "usd_balance",
            "btc_balance",
            "btc_exposure",
            "fast_ma",
            "slow_ma",
            "long_ma",
            "raw_signal",
            "confirmation_count",
            "decision",
            "reason"
        ])


def append_history(snapshot, indicators, raw_signal,
                   confirmation_count, decision, reason):
    ensure_history_file()

    def optional_number(value, decimals):
        if value is None:
            return ""
        return ("{:.%df}" % decimals).format(value)

    with HISTORY_FILE.open("a", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            "{:.2f}".format(snapshot["btc_price"]),
            "{:.2f}".format(snapshot["portfolio_value_usd"]),
            "{:.2f}".format(snapshot["usd_balance"]),
            "{:.8f}".format(snapshot["btc_balance"]),
            "{:.6f}".format(snapshot["btc_exposure"]),
            optional_number(indicators["fast_ma"], 2),
            optional_number(indicators["slow_ma"], 2),
            optional_number(indicators["long_ma"], 2),
            raw_signal,
            confirmation_count,
            decision,
            reason
        ])


def buy_currency(currency, usd_amount):
    price = get_price("{}usd".format(currency), store_history=False)
    if not price:
        return False

    if usd_amount < MIN_TRADE_AMOUNT:
        log("Skipped buying {}: amount too low ({:.2f} USD)".format(
            currency, usd_amount
        ))
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
        log("Bought {:.8f} {} for {:.2f} USD".format(
            crypto_amount, currency, usd_amount
        ))
        return True

    log("Buy order failed for {}: {}".format(
        currency,
        response.text if response else "no response"
    ))
    return False


def sell_currency(currency, amount):
    price = get_price("{}usd".format(currency), store_history=False)
    if not price:
        return False

    usd_value = amount * price
    if usd_value < MIN_TRADE_AMOUNT:
        log("Skipped selling {}: value too low ({:.2f} USD)".format(
            currency, usd_value
        ))
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
        log("Sold {:.8f} {} for approximately {:.2f} USD".format(
            amount, currency, usd_value
        ))
        return True

    log("Sell order failed for {}: {}".format(
        currency,
        response.text if response else "no response"
    ))
    return False


def sell_all_non_btc_to_usd():
    balance = get_balance()
    if not balance:
        return

    for currency, amount in balance["crypto"].items():
        if currency in ("btc", "usd"):
            continue

        price = get_price("{}usd".format(currency), store_history=False)

        if price and amount * price >= MIN_TRADE_AMOUNT:
            log("Converting {} to USD before BTC strategy".format(currency))
            sell_currency(currency, amount)


def trade_toward_target(snapshot, signal):
    """
    Move only the amount needed to approach a target allocation.
    This prevents repeated 75% buying and selling.
    """
    portfolio_value = snapshot["portfolio_value_usd"]
    btc_value = snapshot["btc_value_usd"]
    btc_price = snapshot["btc_price"]

    if signal == "BUY":
        target_value = portfolio_value * BUY_TARGET_BTC_EXPOSURE
        usd_to_buy = target_value - btc_value

        if usd_to_buy < MIN_TRADE_AMOUNT:
            return False, "BTC-andelen ligger redan nära köp-målet"

        usd_to_buy = min(usd_to_buy, snapshot["usd_balance"])
        success = buy_currency("btc", usd_to_buy)
        return success, "Flyttar portföljen mot {:.0%} BTC".format(
            BUY_TARGET_BTC_EXPOSURE
        )

    if signal == "SELL":
        target_value = portfolio_value * SELL_TARGET_BTC_EXPOSURE
        usd_to_sell = btc_value - target_value

        if usd_to_sell < MIN_TRADE_AMOUNT:
            return False, "BTC-andelen ligger redan nära sälj-målet"

        btc_to_sell = usd_to_sell / btc_price
        btc_to_sell = min(btc_to_sell, snapshot["btc_balance"])
        success = sell_currency("btc", btc_to_sell)
        return success, "Flyttar portföljen mot {:.0%} BTC".format(
            SELL_TARGET_BTC_EXPOSURE
        )

    return False, "Ingen handel för HOLD-signal"


def trade_logic():
    global latest_action
    global latest_reason
    global latest_signal
    global last_trade_time
    global pending_signal
    global pending_signal_count

    sell_all_non_btc_to_usd()

    # This is the only regularly scheduled history sample.
    btc_price = get_price("btcusd", store_history=True)
    if not btc_price:
        latest_action = "ERROR"
        latest_reason = "Kunde inte hämta BTC-priset"
        return

    snapshot = get_portfolio_snapshot(btc_price)
    if not snapshot:
        latest_action = "ERROR"
        latest_reason = "Kunde inte läsa portföljen"
        return

    indicators = calculate_indicators()
    raw_signal, signal_reason = determine_signal(indicators)
    confirmation_count = update_confirmation(raw_signal)

    decision = "HOLD"
    reason = signal_reason

    cooldown_remaining = (
        TRADE_COOLDOWN_SECONDS - (time.time() - last_trade_time)
    )

    enough_confirmation = (
        raw_signal in ("BUY", "SELL") and
        confirmation_count >= CONFIRMATION_CYCLES
    )

    if not enough_confirmation:
        if raw_signal in ("BUY", "SELL"):
            reason = "{}; bekräftelse {}/{}".format(
                signal_reason,
                confirmation_count,
                CONFIRMATION_CYCLES
            )
    elif cooldown_remaining > 0:
        reason = "{}; cooldown återstår {:.1f} timmar".format(
            signal_reason,
            cooldown_remaining / 3600.0
        )
    else:
        success, trade_reason = trade_toward_target(snapshot, raw_signal)
        reason = "{}; {}".format(signal_reason, trade_reason)

        if success:
            decision = raw_signal
            last_trade_time = time.time()
            pending_signal = None
            pending_signal_count = 0
        else:
            reason = "{}; ingen order genomfördes".format(reason)

    latest_action = decision
    latest_reason = reason
    latest_signal = {
        "action": decision,
        "raw_signal": raw_signal,
        "reason": reason,
        "confirmation_count": confirmation_count,
        "btc_price": snapshot["btc_price"],
        "portfolio_value_usd": snapshot["portfolio_value_usd"],
        "usd_balance": snapshot["usd_balance"],
        "btc_balance": snapshot["btc_balance"],
        "btc_exposure": snapshot["btc_exposure"],
        "fast_ma": indicators["fast_ma"],
        "slow_ma": indicators["slow_ma"],
        "long_ma": indicators["long_ma"],
        "sample_count": indicators["sample_count"],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    append_history(
        snapshot,
        indicators,
        raw_signal,
        confirmation_count,
        decision,
        reason
    )

    log(
        "Decision: {} | Raw signal: {} | Exposure: {:.1%} | "
        "Portfolio: {:.2f} USD | {}".format(
            decision,
            raw_signal,
            snapshot["btc_exposure"],
            snapshot["portfolio_value_usd"],
            reason
        )
    )


def trading_bot():
    log("Trading bot started")
    ensure_history_file()

    while True:
        try:
            trade_logic()
        except Exception as exc:
            log("Unexpected error in trading_bot: {}".format(exc))

        time.sleep(PRICE_UPDATE_SECONDS)


@app.route("/dashboard")
def dashboard():
    snapshot = get_portfolio_snapshot()
    portfolio = snapshot["portfolio_value_usd"] if snapshot else 0.0
    usd = snapshot["usd_balance"] if snapshot else 0.0
    btc = snapshot["btc_balance"] if snapshot else 0.0
    btc_price = snapshot["btc_price"] if snapshot else 0.0
    exposure = snapshot["btc_exposure"] if snapshot else 0.0

    fast_ma = latest_signal.get("fast_ma")
    slow_ma = latest_signal.get("slow_ma")
    long_ma = latest_signal.get("long_ma")
    raw_signal = latest_signal.get("raw_signal", "HOLD")
    confirmations = latest_signal.get("confirmation_count", 0)
    samples = latest_signal.get("sample_count", 0)

    def value_text(value):
        return "Väntar" if value is None else "{:.2f}".format(value)

    with state_lock:
        recent_entries = list(reversed(transaction_log[-20:]))

    recent = "".join("<li>{}</li>".format(x) for x in recent_entries)

    html = """<!doctype html>
<html lang='sv'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>BTC Trading Bot</title>
<style>
body{{font-family:Arial;margin:24px;background:#f5f5f5}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}}
.card{{background:white;border-radius:10px;padding:18px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
.big{{font-size:2rem;font-weight:bold}}
.label{{color:#666;margin-bottom:8px}}
.small{{font-size:.9rem;color:#555}}
</style>
</head>
<body>
<h1>BTC Trading Bot – lugnare strategi</h1>
<div class='grid'>
<div class='card'><div class='label'>Portföljvärde</div><div class='big'>{portfolio:.2f} USD</div></div>
<div class='card'><div class='label'>BTC-exponering</div><div class='big'>{exposure:.1%}</div></div>
<div class='card'><div class='label'>USD</div><div class='big'>{usd:.2f}</div></div>
<div class='card'><div class='label'>BTC</div><div class='big'>{btc:.8f}</div></div>
<div class='card'><div class='label'>BTC-pris</div><div class='big'>{btc_price:.2f}</div></div>
<div class='card'><div class='label'>Senaste beslut</div><div class='big'>{action}</div><p>{reason}</p></div>
<div class='card'><div class='label'>Rå signal</div><div class='big'>{raw_signal}</div><div class='small'>Bekräftelse {confirmations}/{required}</div></div>
<div class='card'><div class='label'>Prisprover</div><div class='big'>{samples}/{long_window}</div><div class='small'>15 minuter mellan prover</div></div>
<div class='card'><div class='label'>2h-snitt</div><div class='big'>{fast_ma}</div></div>
<div class='card'><div class='label'>8h-snitt</div><div class='big'>{slow_ma}</div></div>
<div class='card'><div class='label'>24h-snitt</div><div class='big'>{long_ma}</div></div>
</div>
<div class='card' style='margin-top:16px'><h2>Senaste logg</h2><ul>{recent}</ul></div>
</body>
</html>""".format(
        portfolio=portfolio,
        exposure=exposure,
        usd=usd,
        btc=btc,
        btc_price=btc_price,
        action=latest_action,
        reason=latest_reason,
        raw_signal=raw_signal,
        confirmations=confirmations,
        required=CONFIRMATION_CYCLES,
        samples=samples,
        long_window=LONG_WINDOW,
        fast_ma=value_text(fast_ma),
        slow_ma=value_text(slow_ma),
        long_ma=value_text(long_ma),
        recent=recent
    )

    return Response(html, mimetype="text/html")


@app.route("/api/dashboard")
def dashboard_api():
    snapshot = get_portfolio_snapshot()

    return jsonify({
        "latest_action": latest_action,
        "latest_reason": latest_reason,
        "latest_signal": latest_signal,
        "portfolio": snapshot,
        "btc_price_history_count": len(price_history["btc"]),
        "settings": {
            "sample_seconds": PRICE_UPDATE_SECONDS,
            "fast_window": FAST_WINDOW,
            "slow_window": SLOW_WINDOW,
            "long_window": LONG_WINDOW,
            "buy_buffer": BUY_BUFFER,
            "sell_buffer": SELL_BUFFER,
            "confirmation_cycles": CONFIRMATION_CYCLES,
            "cooldown_hours": TRADE_COOLDOWN_SECONDS / 3600,
            "buy_target_exposure": BUY_TARGET_BTC_EXPOSURE,
            "sell_target_exposure": SELL_TARGET_BTC_EXPOSURE
        },
        "transaction_log": transaction_log
    })


@app.route("/")
def home():
    return dashboard()


if __name__ == "__main__":
    threading.Thread(target=trading_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
