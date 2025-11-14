import streamlit as st
import threading
import time
import logging
from datetime import datetime
import pandas as pd
import numpy as np
from binance.client import Client
from binance import ThreadedWebsocketManager
from binance.exceptions import BinanceAPIException

# ========================= CONFIGURATION =========================
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Session state keys
KEYS = {
    "bot_running": "bot_running",
    "init_status": "init_status",
    "auto_rebalance": "auto_rebalance",
    "rebalance_interval": "rebalance_interval",
    "max_position_pct": "max_position_pct",
    "min_trade_value": "min_trade_value",
    "sample_limit": "sample_limit",
    "total_port": "total_port",
    "usdt_free": "usdt_free",
    "top_symbols": "top_symbols",
    "log_messages": "log_messages",
    "positions": "positions",  # {symbol: {"qty": 0.0, "avg_price": 0.0, "value": 0.0, "grid_levels": []}}
    "price_cache": "price_cache",  # {symbol: price}
    "klines_cache": "klines_cache",  # {symbol: df}
    "orderbook_cache": "orderbook_cache",  # {symbol: {"bids": [], "asks": []}}
    "signals": "signals",  # {symbol: "BUY"/"SELL"/None}
    "last_signal_time": "last_signal_time",  # {symbol: timestamp}
    "grid_settings": "grid_settings",  # {symbol: {"active": bool, "base_price": float, "grid_size": float, "num_levels": int, "qty_per_level": float, "levels": [], "last_update": 0}}
}

# Default values
DEFAULTS = {
    KEYS["bot_running"]: False,
    KEYS["init_status"]: "NOT STARTED",
    KEYS["auto_rebalance"]: True,
    KEYS["rebalance_interval"]: 30,
    KEYS["max_position_pct"]: 5.0,
    KEYS["min_trade_value"]: 10.0,
    KEYS["sample_limit"]: 25,
    KEYS["total_port"]: 0.0,
    KEYS["usdt_free"]: 0.0,
    KEYS["top_symbols"]: [],
    KEYS["log_messages"]: [],
    KEYS["positions"]: {},
    KEYS["price_cache"]: {},
    KEYS["klines_cache"]: {},
    KEYS["orderbook_cache"]: {},
    KEYS["signals"]: {},
    KEYS["last_signal_time"]: {},
    KEYS["grid_settings"]: {},
}

# Binance client
API_KEY = "YOUR_API_KEY"
API_SECRET = "YOUR_API_SECRET"
client = Client(API_KEY, API_SECRET)
# client.API_URL = 'https://testnet.binance.vision/api'  # Uncomment for testnet

# Global websocket managers
twm = None
user_twm = None
depth_twm = None  # For orderbook

# ========================= HELPER FUNCTIONS =========================
def add_log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} - {msg}"
    if KEYS["log_messages"] not in st.session_state:
        st.session_state[KEYS["log_messages"]] = []
    st.session_state[KEYS["log_messages"]].append(log_entry)
    logger.info(msg)

def init_session_state():
    for key, default in DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = default

def get_total_portfolio_value():
    try:
        account = client.get_account()
        total = 0.0
        positions = {}
        for asset in account['balances']:
            qty = float(asset['free']) + float(asset['locked'])
            if qty <= 0:
                continue
            if asset['asset'] == 'USDT':
                total += qty
            else:
                symbol = asset['asset'] + 'USDT'
                try:
                    ticker = client.get_symbol_ticker(symbol=symbol)
                    price = float(ticker['price'])
                    value = qty * price
                    total += value
                    positions[symbol] = {
                        "qty": qty,
                        "avg_price": price,
                        "value": round(value, 2),
                    }
                except:
                    pass
        st.session_state[KEYS["total_port"]] = round(total, 2)
        st.session_state[KEYS["positions"]] = positions
        return total
    except Exception as e:
        add_log(f"Portfolio value error: {e}")
        return 0.0

def get_usdt_free():
    try:
        balance = client.get_asset_balance(asset='USDT')
        free = float(balance['free'])
        st.session_state[KEYS["usdt_free"]] = round(free, 2)
        return free
    except Exception as e:
        add_log(f"USDT balance error: {e}")
        return 0.0

def load_symbols():
    try:
        info = client.get_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        add_log(f"Loaded {len(symbols)} symbols")
        return symbols
    except Exception as e:
        add_log(f"Symbol info error: {e}")
        return []

def get_top_volume_symbols(symbols, limit=25):
    try:
        tickers = client.get_ticker()
        df = pd.DataFrame([t for t in tickers if t['symbol'] in symbols])
        df['quoteVolume'] = df['quoteVolume'].astype(float)
        top = df.nlargest(limit, 'quoteVolume')['symbol'].tolist()
        st.session_state[KEYS["top_symbols"]] = top
        add_log(f"Top {len(top)} volume symbols refreshed")
        return top
    except Exception as e:
        add_log(f"Volume fetch error: {e}")
        return []

def get_klines(symbol, interval='1m', limit=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])
        df['close'] = pd.to_numeric(df['close'])
        df['volume'] = pd.to_numeric(df['volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        st.session_state[KEYS["klines_cache"]][symbol] = df
        return df
    except Exception as e:
        add_log(f"Kline error {symbol}: {e}")
        return pd.DataFrame()

def calculate_rsi(df, period=14):
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def calculate_ema(df, period=20):
    return df['close'].ewm(span=period, adjust=False).mean().iloc[-1]

def calculate_volatility_from_orderbook(symbol):
    """Calculate implied volatility from orderbook depth."""
    book = st.session_state[KEYS["orderbook_cache"]].get(symbol, {})
    if not book or not book.get("bids") or not book.get("asks"):
        return 0.005  # Default 0.5%

    bids = book["bids"][:10]
    asks = book["asks"][:10]

    bid_prices = [float(b[0]) for b in bids]
    ask_prices = [float(a[0]) for a in asks]

    mid_price = (bid_prices[0] + ask_prices[0]) / 2
    spread = (ask_prices[0] - bid_prices[0]) / mid_price

    # Weighted depth imbalance
    bid_qty = sum(float(b[1]) for b in bids)
    ask_qty = sum(float(a[1]) for a in asks)
    imbalance = abs(bid_qty - ask_qty) / (bid_qty + ask_qty + 1e-8)

    # Volatility proxy: spread + imbalance
    vol = spread * 2 + imbalance * 0.5
    vol = max(0.002, min(vol, 0.03))  # Clamp between 0.2% and 3.0%
    return vol

def generate_signal(symbol):
    if symbol not in st.session_state[KEYS["top_symbols"]][:10]:
        return None

    df = get_klines(symbol, limit=50)
    if df.empty or len(df) < 30:
        return None

    rsi = calculate_rsi(df)
    ema_fast = calculate_ema(df, 12)
    ema_slow = calculate_ema(df, 26)
    price = df['close'].iloc[-1]

    st.session_state[KEYS["price_cache"]][symbol] = price

    prev_signal = st.session_state[KEYS["signals"]].get(symbol)
    last_time = st.session_state[KEYS["last_signal_time"]].get(symbol, 0)
    now = time.time()

    if now - last_time < 300:
        return prev_signal

    if rsi < 30 and ema_fast > ema_slow:
        if prev_signal != "BUY":
            st.session_state[KEYS["signals"]][symbol] = "BUY"
            st.session_state[KEYS["last_signal_time"]][symbol] = now
            add_log(f"SIGNAL BUY {symbol} | RSI: {rsi:.1f} | Price: {price:.2f}")
            return "BUY"
    elif rsi > 70 and ema_fast < ema_slow:
        if prev_signal != "SELL":
            st.session_state[KEYS["signals"]][symbol] = "SELL"
            st.session_state[KEYS["last_signal_time"]][symbol] = now
            add_log(f"SIGNAL SELL {symbol} | RSI: {rsi:.1f} | Price: {price:.2f}")
            return "SELL"

    return prev_signal

# ========================= TRAILING INFINITI GRID =========================
def activate_trailing_grid(symbol, entry_price, usdt_amount):
    """Activate dynamic trailing grid."""
    vol = calculate_volatility_from_orderbook(symbol)
    grid_size = vol * 1.5  # 1.5x volatility as spacing
    grid_size = max(0.003, min(grid_size, 0.02))  # 0.3% to 2.0%
    num_levels = 8
    qty_per_level = usdt_amount / num_levels

    grid = {
        "active": True,
        "base_price": entry_price,
        "grid_size": grid_size,
        "num_levels": num_levels,
        "qty_per_level": qty_per_level,
        "last_update": time.time(),
        "levels": [
            {
                "level": i+1,
                "price": round(entry_price * (1 + (i+1) * grid_size), 6),
                "qty": 0.0,
                "sold": False
            } for i in range(num_levels)
        ]
    }

    st.session_state[KEYS["grid_settings"]][symbol] = grid
    add_log(f"TRAILING GRID ON {symbol} | Entry: ${entry_price:.2f} | Size: {grid_size*100:.2f}% | {num_levels} levels")

def update_trailing_grid(symbol):
    """Update grid levels to trail price."""
    grid = st.session_state[KEYS["grid_settings"]].get(symbol)
    if not grid or not grid["active"]:
        return

    price = st.session_state[KEYS["price_cache"]].get(symbol, 0)
    if price <= 0:
        return

    # Update every 30 seconds
    if time.time() - grid["last_update"] < 30:
        return

    pos = st.session_state[KEYS["positions"]].get(symbol, {})
    if not pos or pos["qty"] <= 0:
        grid["active"] = False
        return

    # Recalculate grid size from orderbook
    vol = calculate_volatility_from_orderbook(symbol)
    new_grid_size = vol * 1.5
    new_grid_size = max(0.003, min(new_grid_size, 0.02))

    # Trail up: shift base if price > highest level
    highest_level = max(l["price"] for l in grid["levels"])
    if price > highest_level * 1.01:  # 1% above
        shift = price * 0.005  # Trail up 0.5%
        grid["base_price"] += shift
        for level in grid["levels"]:
            if not level["sold"]:
                level["price"] = round(grid["base_price"] * (1 + (level["level"]) * new_grid_size), 6)
        add_log(f"GRID TRAIL UP {symbol} | New base: ${grid['base_price']:.2f}")

    # Trail down: only if no unsold levels below current price
    elif price < grid["base_price"] * 0.99 and all(l["sold"] or l["price"] > price for l in grid["levels"]):
        shift = price * 0.005
        grid["base_price"] -= shift
        for level in grid["levels"]:
            if not level["sold"]:
                level["price"] = round(grid["base_price"] * (1 + (level["level"]) * new_grid_size), 6)
        add_log(f"GRID TRAIL DOWN {symbol} | New base: ${grid['base_price']:.2f}")

    grid["grid_size"] = new_grid_size
    grid["last_update"] = time.time()

def check_trailing_grid_tp(symbol):
    """Check and execute trailing grid take-profit."""
    if symbol not in st.session_state[KEYS["grid_settings"]]:
        return

    grid = st.session_state[KEYS["grid_settings"]][symbol]
    if not grid["active"]:
        return

    price = st.session_state[KEYS["price_cache"]].get(symbol, 0)
    if price <= 0:
        return

    pos = st.session_state[KEYS["positions"]].get(symbol, {})
    if not pos or pos["qty"] <= 0:
        grid["active"] = False
        return

    executed = False
    for level in grid["levels"]:
        if not level["sold"] and price >= level["price"]:
            qty_to_sell = min(grid["qty_per_level"] / price, pos["qty"])
            if qty_to_sell > 0:
                execute_sell_partial(symbol, qty_to_sell, level["price"])
                level["sold"] = True
                level["qty"] = qty_to_sell
                executed = True

    if executed:
        sold_count = sum(1 for l in grid["levels"] if l["sold"])
        add_log(f"GRID TP {symbol} | Price: ${price:.2f} | {sold_count}/{grid['num_levels']} sold")

    if all(l["sold"] for l in grid["levels"]):
        grid["active"] = False
        add_log(f"GRID COMPLETE {symbol} | All levels sold")

def execute_buy(symbol, usdt_amount):
    try:
        price = st.session_state[KEYS["price_cache"]].get(symbol)
        if not price:
            ticker = client.get_symbol_ticker(symbol=symbol)
            price = float(ticker['price'])
        quantity = usdt_amount / price
        info = client.get_symbol_info(symbol)
        step_size = next(f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        step_size = float(step_size)
        quantity = round(quantity - (quantity % step_size), 8)

        if quantity * price < st.session_state[KEYS["min_trade_value"]]:
            add_log(f"BUY {symbol} skipped: below min trade value")
            return

        order = client.order_market_buy(
            symbol=symbol,
            quantity=quantity
        )
        add_log(f"BUY EXECUTED {symbol} | Qty: {quantity} | ~${usdt_amount:.2f}")

        # Activate Trailing Grid
        activate_trailing_grid(symbol, price, usdt_amount)

    except Exception as e:
        add_log(f"BUY FAILED {symbol}: {e}")

def execute_sell_partial(symbol, qty, target_price):
    try:
        info = client.get_symbol_info(symbol)
        step_size = next(f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        step_size = float(step_size)
        qty = round(qty - (qty % step_size), 8)

        if qty <= 0:
            return

        order = client.order_market_sell(
            symbol=symbol,
            quantity=qty
        )
        add_log(f"GRID SELL {symbol} | Qty: {qty} @ ~${target_price:.2f}")
    except Exception as e:
        add_log(f"GRID SELL FAILED {symbol}: {e}")

def execute_sell(symbol):
    try:
        pos = st.session_state[KEYS["positions"]].get(symbol, {})
        if not pos or pos["qty"] <= 0:
            return
        quantity = pos["qty"]
        info = client.get_symbol_info(symbol)
        step_size = next(f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        step_size = float(step_size)
        quantity = round(quantity - (quantity % step_size), 8)

        order = client.order_market_sell(
            symbol=symbol,
            quantity=quantity
        )
        add_log(f"SELL FULL {symbol} | Qty: {quantity}")

        if symbol in st.session_state[KEYS["grid_settings"]]:
            st.session_state[KEYS["grid_settings"]][symbol]["active"] = False

    except Exception as e:
        add_log(f"SELL FAILED {symbol}: {e}")

# ========================= SIGNAL & GRID ENGINE =========================
def signal_engine():
    while st.session_state[KEYS["bot_running"]]:
        if not st.session_state[KEYS["top_symbols"]]:
            time.sleep(5)
            continue

        total_value = st.session_state[KEYS["total_port"]]
        max_per_coin = total_value * (st.session_state[KEYS["max_position_pct"]] / 100)
        usdt_free = st.session_state[KEYS["usdt_free"]]

        for symbol in st.session_state[KEYS["top_symbols"]][:5]:
            signal = generate_signal(symbol)
            if signal:
                pos_value = st.session_state[KEYS["positions"]].get(symbol, {}).get("value", 0)

                if signal == "BUY":
                    if pos_value >= max_per_coin:
                        continue
                    buy_amount = min(usdt_free * 0.2, max_per_coin - pos_value)
                    if buy_amount >= st.session_state[KEYS["min_trade_value"]]:
                        execute_buy(symbol, buy_amount)

                elif signal == "SELL":
                    if pos_value > 0:
                        execute_sell(symbol)

            # Update & check trailing grid
            if symbol in st.session_state[KEYS["grid_settings"]]:
                update_trailing_grid(symbol)
                check_trailing_grid_tp(symbol)

        time.sleep(10)

# ========================= WEBSOCKET HANDLERS =========================
def handle_market_socket(msg):
    if msg.get('e') == 'error':
        add_log(f"Market WS error: {msg}")
    elif 's' in msg and 'c' in msg:
        symbol = msg['s']
        price = float(msg['c'])
        st.session_state[KEYS["price_cache"]][symbol] = price

def handle_depth_socket(msg, symbol):
    if msg.get('e') == 'depthUpdate':
        book = st.session_state[KEYS["orderbook_cache"]].setdefault(symbol, {"bids": [], "asks": []})
        for b in msg['b']:
            price, qty = b[0], b[1]
            if float(qty) == 0:
                book["bids"] = [x for x in book["bids"] if x[0] != price]
            else:
                book["bids"] = [x for x in book["bids"] if x[0] != price] + [[price, qty]]
                book["bids"] = sorted(book["bids"], key=lambda x: float(x[0]), reverse=True)[:20]
        for a in msg['a']:
            price, qty = a[0], a[1]
            if float(qty) == 0:
                book["asks"] = [x for x in book["asks"] if x[0] != price]
            else:
                book["asks"] = [x for x in book["asks"] if x[0] != price] + [[price, qty]]
                book["asks"] = sorted(book["asks"], key=lambda x: float(x[0]))[:20]

def handle_user_socket(msg):
    if msg.get('e') == 'executionReport':
        side = msg['S']
        symbol = msg['s']
        qty = msg['q']
        price = msg['L']
        add_log(f"FILL {side} {symbol} @ {price} | {qty}")
        get_total_portfolio_value()
        get_usdt_free()
    elif msg.get('e') == 'outboundAccountPosition':
        get_usdt_free()
        get_total_portfolio_value()

# ========================= BOT THREADS =========================
def market_websockets(symbols):
    global twm
    twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
    twm.start()
    for symbol in symbols:
        twm.start_symbol_ticker_socket(callback=handle_market_socket, symbol=symbol)

def depth_websockets(symbols):
    global depth_twm
    depth_twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
    depth_twm.start()
    for symbol in symbols:
        depth_twm.start_depth_socket(
            callback=lambda msg, s=symbol: handle_depth_socket(msg, s),
            symbol=symbol,
            depth='20'
        )

def user_stream():
    global user_twm
    listen_key = client.stream_get_listen_key()
    user_twm = ThreadedWebsocketManager()
    user_twm.start()
    user_twm.start_user_socket(callback=handle_user_socket, listen_key=listen_key)
    add_log("User WS open")
    st.session_state.listen_key = listen_key

def keepalive_user_stream():
    while st.session_state[KEYS["bot_running"]]:
        try:
            client.stream_keepalive(st.session_state.listen_key)
        except:
            pass
        time.sleep(1800)

def refresh_top_symbols():
    while st.session_state[KEYS["bot_running"]]:
        symbols = load_symbols()
        if symbols:
            get_top_volume_symbols(symbols, st.session_state[KEYS["sample_limit"]])
        time.sleep(3600)

def auto_rebalance():
    while st.session_state[KEYS["bot_running"]] and st.session_state[KEYS["auto_rebalance"]]:
        time.sleep(st.session_state[KEYS["rebalance_interval"]] * 60)
        add_log("Auto rebalance triggered")

def initialise_bot():
    try:
        add_log("=== INITIALISING PLATINUM TRADER + TRAILING GRID ===")
        st.session_state[KEYS["init_status"]] = "INITIALISING..."

        symbols = load_symbols()
        if not symbols:
            st.session_state[KEYS["init_status"]] = "ERROR: No symbols loaded"
            return

        get_usdt_free()
        get_total_portfolio_value()

        top_symbols = get_top_volume_symbols(symbols, st.session_state[KEYS["sample_limit"]])
        market_websockets(top_symbols)
        depth_websockets(top_symbols[:5])  # Only top 5 for performance
        user_stream()

        threading.Thread(target=keepalive_user_stream, daemon=True).start()
        threading.Thread(target=refresh_top_symbols, daemon=True).start()
        threading.Thread(target=auto_rebalance, daemon=True).start()
        threading.Thread(target=signal_engine, daemon=True).start()

        st.session_state[KEYS["bot_running"]] = True
        st.session_state[KEYS["init_status"]] = "RUNNING"
        add_log("TRAILING GRID BOT STARTED")
    except Exception as e:
        st.session_state[KEYS["init_status"]] = f"ERROR: {e}"
        add_log(f"Init failed: {e}")

def stop_bot():
    st.session_state[KEYS["bot_running"]] = False
    if twm:
        twm.stop()
    if depth_twm:
        depth_twm.stop()
    if user_twm:
        user_twm.stop()
    add_log("Bot stopped")

# ========================= STREAMLIT UI =========================
def main():
    st.set_page_config(page_title="Platinum Crypto Trader", layout="wide")
    st.title("PLATINUM TRADER + TRAILING INFINITI GRID")
    st.markdown("**RSI + EMA | Dynamic Grid Size (Orderbook Volatility) | Trailing Up/Down**")
    st.success("ADAPTIVE TRAILING GRID ENABLED")

    init_session_state()

    col1, col2 = st.columns([1, 2])

    with col1:
        st.subheader("Control Panel")
        if not st.session_state[KEYS["bot_running"]]:
            if st.button("START BOT", type="primary", use_container_width=True):
                threading.Thread(target=initialise_bot, daemon=True).start()
                st.session_state[KEYS["init_status"]] = "STARTING..."
        else:
            if st.button("STOP BOT", type="secondary", use_container_width=True):
                stop_bot()

        st.write("**Status:**", st.session_state[KEYS["init_status"]])

        st.checkbox("Auto Rebalance", value=st.session_state[KEYS["auto_rebalance"]], key=KEYS["auto_rebalance"])
        st.session_state[KEYS["rebalance_interval"]] = st.number_input("Rebalance Interval (min)", 1, 360, st.session_state[KEYS["rebalance_interval"]])
        st.session_state[KEYS["max_position_pct"]] = st.slider("Max % per Coin", 0.1, 20.0, st.session_state[KEYS["max_position_pct"]], 0.1)
        st.session_state[KEYS["min_trade_value"]] = st.number_input("Min Trade $", 1.0, 100.0, st.session_state[KEYS["min_trade_value"]], 0.5)
        st.session_state[KEYS["sample_limit"]] = st.number_input("Symbol sample size (top N)", 5, 100, st.session_state[KEYS["sample_limit"]], 1)

    with col2:
        st.subheader("Portfolio")
        col_a, col_b = st.columns(2)
        col_a.metric("Total Portfolio", f"${st.session_state[KEYS['total_port']]:,.2f}")
        col_b.metric("USDT Free", f"${st.session_state[KEYS['usdt_free']]:,.2f}")

        st.subheader("Live Signals & Trailing Grid")
        signal_cols = st.columns(5)
        for i, symbol in enumerate(st.session_state[KEYS["top_symbols"]][:5]):
            with signal_cols[i]:
                sig = st.session_state[KEYS["signals"]].get(symbol, "")
                price = st.session_state[KEYS["price_cache"]].get(symbol, 0)
                grid = st.session_state[KEYS["grid_settings"]].get(symbol, {})
                active = grid.get("active", False)
                sold = sum(1 for l in grid.get("levels", []) if l.get("sold", False)) if active else 0
                total = grid.get("num_levels", 0) if active else 0
                size = f"{grid.get('grid_size', 0)*100:.2f}%" if active else ""

                if sig == "BUY":
                    st.success(f"**{symbol}**\nBUY @ ${price:.2f}")
                elif sig == "SELL":
                    st.error(f"**{symbol}**\nSELL @ ${price:.2f}")
                else:
                    status = f"GRID {sold}/{total} | {size}" if active else ""
                    st.info(f"**{symbol}**\n${price:.2f}\n{status}")

        st.subheader("Activity Log")
        log_container = st.empty()
        if st.session_state[KEYS["log_messages"]]:
            log_text = "\n".join(reversed(st.session_state[KEYS["log_messages"]][-30:]))
            log_container.text(log_text)

    time.sleep(1)
    if st.session_state[KEYS["bot_running"]]:
        st.rerun()

if __name__ == "__main__":
    main()
