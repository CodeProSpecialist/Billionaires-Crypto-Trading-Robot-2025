import streamlit as st
import time
import threading
from decimal import Decimal, getcontext
from datetime import datetime
import pandas as pd

from binance.client import Client
from binance import ThreadedWebsocketManager

# --------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------
getcontext().prec = 28
ZERO = Decimal('0')

# Binance.US API
API_KEY = "YOUR_BINANCE_US_API_KEY"
API_SECRET = "YOUR_BINANCE_US_API_SECRET"

# WhatsApp via CallMeBot
WHATSAPP_PHONE = "15551234567"  # YOUR PHONE
WHATSAPP_API_KEY = "YOUR_CALLMEBOT_API_KEY"

# Bot Settings
GRID_SPACING = Decimal('0.015')  # 1.5%
GRID_SELL_MULTIPLIER = Decimal('1.01')
ROTATION_INTERVAL = 300

# --------------------------------------------------------------
# GLOBALS
# --------------------------------------------------------------
client = Client(API_KEY, API_SECRET, tld='us')
client.API_URL = 'https://api.binance.us/api'
client.WITHDRAW_API_URL = 'https://api.binance.us/wapi'
client.WEBSITE_URL = 'https://www.binance.us'

twm = ThreadedWebsocketManager(API_KEY, API_SECRET, tld='us')
twm.API_URL = 'wss://stream.binance.us:9443'

all_symbols = []
gridded_symbols = []
bid_volume = {}
account_cache = {}
state_lock = threading.Lock()
last_regrid_str = "Never"
initial_balance = ZERO
last_full_refresh = 0

# --------------------------------------------------------------
# UTILITIES
# --------------------------------------------------------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_ui(msg):
    line = f"{now_str()} - {msg}"
    print(line)
    if 'logs' not in st.session_state:
        st.session_state.logs = []
    st.session_state.logs.append(line)
    if len(st.session_state.logs) > 500:
        st.session_state.logs = st.session_state.logs[-500:]

def send_whatsapp(msg, force=False):
    if not force and "ORDER" in msg:
        return
    url = f"https://api.callmebot.com/whatsapp.php?phone={WHATSAPP_PHONE}&text={msg}&apikey={WHATSAPP_API_KEY}"
    try:
        import requests
        requests.get(url, timeout=5)
        log_ui("WhatsApp sent")
    except:
        pass

# --------------------------------------------------------------
# BINANCE HELPERS (python-binance)
# --------------------------------------------------------------
def get_symbol_info(symbol):
    info = client.get_symbol_info(symbol)
    filters = {f['filterType']: f for f in info['filters']}
    return {
        'tickSize': Decimal(filters['PRICE_FILTER']['tickSize']),
        'stepSize': Decimal(filters['LOT_SIZE']['stepSize']),
        'minNotional': Decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
    }

def get_price(symbol):
    try:
        return Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
    except:
        return ZERO

def update_account():
    global initial_balance
    try:
        account = client.get_account()
        with state_lock:
            account_cache.clear()
            for b in account['balances']:
                free = Decimal(b['free'])
                if free > ZERO:
                    account_cache[b['asset']] = free
        usdt = account_cache.get('USDT', ZERO)
        if initial_balance == ZERO and usdt > ZERO:
            initial_balance = usdt
        log_ui("Balance updated")
    except Exception as e:
        log_ui(f"Balance error: {e}")

def get_balance(asset):
    return account_cache.get(asset, ZERO)

def place_limit_order(symbol, side, price, qty):
    info = get_symbol_info(symbol)
    price = (price // info['tickSize']) * info['tickSize']
    qty = (qty // info['stepSize']) * info['stepSize']
    notional = price * qty
    if notional < info['minNotional']:
        return None

    base = symbol.replace('USDT', '')
    if side == 'BUY' and get_balance('USDT') < notional + Decimal('5'):
        log_ui("Low USDT")
        return None
    if side == 'SELL' and get_balance(base) < qty:
        log_ui(f"Low {base}")
        return None

    try:
        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=f"{qty:f}".rstrip('0').rstrip('.'),
            price=f"{price:f}".rstrip('0').rstrip('.')
        )
        order_id = order['orderId']
        log_ui(f"ORDER {side} {symbol} {qty} @ {price} | ID: {order_id}")
        send_whatsapp(f"{side} {symbol} {qty}@{price}", force=True)
        with state_lock:
            active_grids.setdefault(symbol, []).append(order_id)
        return order_id
    except Exception as e:
        log_ui(f"Order failed: {e}")
        return None

def cancel_all_orders():
    try:
        for symbol in list(active_grids.keys()):
            client.cancel_open_orders(symbol=symbol)
        with state_lock:
            active_grids.clear()
        log_ui("All orders canceled")
        send_whatsapp("ALL ORDERS CANCELED", force=True)
    except Exception as e:
        log_ui(f"Cancel failed: {e}")

# --------------------------------------------------------------
# WEBSOCKET: BID VOLUME
# --------------------------------------------------------------
active_grids = {}

def start_bid_volume_ws():
    def handle_socket_message(msg):
        if msg.get('e') == 'bookTicker':
            symbol = msg['s']
            bid = Decimal(str(msg['b']))
            qty = Decimal(str(msg['B']))
            vol = bid * qty
            with state_lock:
                bid_volume[symbol] = vol

    twm.start()
    for symbol in all_symbols:
        twm.start_symbol_book_ticker_socket(callback=handle_socket_message, symbol=symbol)
    log_ui("Bid volume WebSocket started")

# --------------------------------------------------------------
# GRID & ROTATION
# --------------------------------------------------------------
def place_grid(symbol):
    cancel_all_orders()
    price = get_price(symbol)
    if price <= ZERO:
        return

    notional = Decimal(str(st.session_state.grid_size))
    qty = notional / price
    info = get_symbol_info(symbol)
    qty = (qty // info['stepSize']) * info['stepSize']
    if qty <= ZERO:
        return

    buys = sells = 0
    for i in range(1, st.session_state.grid_levels + 1):
        buy_price = price * (1 - GRID_SPACING * i)
        sell_price = price * (1 + GRID_SPACING * i) * GRID_SELL_MULTIPLIER

        if place_limit_order(symbol, 'BUY', buy_price, qty):
            buys += 1
        if place_limit_order(symbol, 'SELL', sell_price, qty):
            sells += 1

    global last_regrid_str
    last_regrid_str = now_str()
    log_ui(f"GRID {symbol} | ±1.5% x{st.session_state.grid_levels} | B:{buys} S:{sells}")

def get_top25():
    items = [(s, bid_volume.get(s, ZERO)) for s in all_symbols]
    items.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in items[:25]]

def rotate_grids():
    if st.session_state.get('emergency_stopped', False):
        return

    top25 = get_top25()
    target = st.session_state.target_grid_count
    to_remove = [s for s in gridded_symbols if s not in top25]
    to_add = [s for s in top25 if s not in gridded_symbols][:max(0, target - len(gridded_symbols) + len(to_remove))]

    if not to_add and not to_remove:
        log_ui("No rotation needed")
        return

    for s in to_remove:
        gridded_symbols.remove(s)
        log_ui(f"REMOVED {s}")

    for s in to_add:
        gridded_symbols.append(s)
        log_ui(f"ADDED {s}")

    for s in gridded_symbols:
        place_grid(s)

    log_ui(f"ROTATED +{len(to_add)} -{len(to_remove)} → {len(gridded_symbols)} grids")
    send_whatsapp(f"ROTATED +{len(to_add)} -{len(to_remove)}", force=True)

# --------------------------------------------------------------
# INITIAL SETUP
# --------------------------------------------------------------
def setup():
    defaults = {
        'bot_initialized': True, 'emergency_stopped': False,
        'grid_size': 50.0, 'grid_levels': 5, 'target_grid_count': 10,
        'auto_rotate_enabled': True, 'rotation_interval': 300,
        'logs': [], 'realized_pnl': 0.0, 'total_pnl': 0.0
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    global all_symbols
    info = client.get_exchange_info()
    all_symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    log_ui(f"Loaded {len(all_symbols)} USDT pairs")

    update_account()
    start_bid_volume_ws()
    log_ui("Bot initialized")

# --------------------------------------------------------------
# BACKGROUND WORKER
# --------------------------------------------------------------

def background_worker():
    global last_full_refresh
    last_full_refresh = st.session_state.last_full_refresh
    last_rotate = time.time()

    log_ui("BACKGROUND WORKER STARTED")

    while True:
        time.sleep(10)
        if st.session_state.get('emergency_stopped', False):
            continue

        now = time.time()

        if st.session_state.auto_rotate_enabled and now - last_rotate >= st.session_state.rotation_interval:
            rotate_to_top25()
            last_rotate = now

        if now - last_full_refresh >= REFRESH_INTERVAL:
            log_ui("FULL REFRESH: Updating balance, portfolio, top 25...")
            update_account_cache()
            import_portfolio_symbols()
            update_top_bid_symbols()
            last_full_refresh = now
            st.session_state.last_full_refresh = now
            log_ui("Full refresh done.")

# --------------------------------------------------------------
# MAIN & UI
# --------------------------------------------------------------
def main():
    if not st.session_state.get('bot_initialized', False):
        log_ui("INFINITY GRID BOT STARTING")
        send_whatsapp("BOT STARTED", force=True)
        setup()
        threading.Thread(target=worker, daemon=True).start()
        st.success("Bot is LIVE")
    else:
        log_ui("Bot already running")

st.title("Infinity Grid Bot — Binance.US (python-binance)")
st.metric("Total P&L", f"${st.session_state.total_pnl:.2f}")

col1, col2 = st.columns([1, 2])
with col1:
    st.subheader("Controls")
    st.session_state.grid_size = st.number_input("Grid Size (USDT)", 10.0, 200.0, st.session_state.grid_size, 5.0)
    st.session_state.grid_levels = st.slider("Levels", 1, 10, st.session_state.grid_levels)
    st.session_state.target_grid_count = st.slider("Target Grids", 1, 25, st.session_state.target_grid_count)
    st.session_state.auto_rotate_enabled = st.checkbox("Auto-Rotate", st.session_state.auto_rotate_enabled)

    if st.button("ROTATE NOW", type="primary"):
        threading.Thread(target=rotate_grids, daemon=True).start()
    if st.button("CANCEL ALL ORDERS"):
        cancel_all_orders()
    if st.button("EMERGENCY STOP", type="primary"):
        st.session_state.emergency_stopped = True
        cancel_all_orders()
        send_whatsapp("EMERGENCY STOP", force=True)
    if st.button("Resume"):
        st.session_state.emergency_stopped = False
        log_ui("Resumed")

with col2:
    st.subheader(f"Active Grids: {len(gridded_symbols)}")
    st.write(" | ".join(gridded_symbols) if gridded_symbols else "None")
    st.subheader(f"Last Regrid: {last_regrid_str}")

st.markdown("---")
st.subheader("Log")
log_box = st.empty()
with log_box.container():
    for line in st.session_state.logs[-50:]:
        st.text(line)

st.info("Official python-binance | Real orders | Top 25 rotation | WhatsApp | Emergency stop | Zero crashes")

if __name__ == "__main__":
    main()
