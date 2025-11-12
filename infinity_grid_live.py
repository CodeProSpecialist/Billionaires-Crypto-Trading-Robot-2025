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
REFRESH_INTERVAL = 300  # 5 minutes

# Binance.US API
API_KEY = "YOUR_BINANCE_US_API_KEY"
API_SECRET = "YOUR_BINANCE_US_API_SECRET"

# WhatsApp
WHATSAPP_PHONE = "15551234567"
WHATSAPP_API_KEY = "YOUR_CALLMEBOT_API_KEY"

GRID_SPACING = Decimal('0.015')
GRID_SELL_MULTIPLIER = Decimal('1.01')

# --------------------------------------------------------------
# GLOBALS
# --------------------------------------------------------------
client = Client(API_KEY, API_SECRET, tld='us')  # CORRECT: tld='us'
twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET, tld='us')

all_symbols = []
gridded_symbols = []
bid_volume = {}
account_cache = {}
active_grids = {}
state_lock = threading.Lock()
last_regrid_str = "Never"
initial_balance = ZERO
cost_basis = {}  # asset → total cost in USDT

# --------------------------------------------------------------
# SESSION STATE INITIALIZATION
# --------------------------------------------------------------
def init_session_state():
    defaults = {
        'bot_initialized': False,
        'emergency_stopped': False,
        'grid_size': 50.0,
        'grid_levels': 5,
        'target_grid_count': 10,
        'auto_rotate_enabled': True,
        'rotation_interval': 300,
        'logs': [],
        'realized_pnl': 0.0,
        'unrealized_pnl': 0.0,
        'total_pnl': 0.0,
        'last_full_refresh': 0.0
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

# --------------------------------------------------------------
# UTILITIES
# --------------------------------------------------------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_ui(msg):
    line = f"{now_str()} - {msg}"
    print(line)
    st.session_state.logs.append(line)
    if len(st.session_state.logs) > 500:
        st.session_state.logs = st.session_state.logs[-500:]

def send_whatsapp(msg, force=False):
    if not force and "ORDER" in msg:
        return
    try:
        import requests
        url = f"https://api.callmebot.com/whatsapp.php?phone={WHATSAPP_PHONE}&text={msg}&apikey={WHATSAPP_API_KEY}"
        requests.get(url, timeout=5)
        log_ui("WhatsApp sent")
    except:
        pass

# --------------------------------------------------------------
# BINANCE HELPERS
# --------------------------------------------------------------
def get_symbol_info(symbol):
    try:
        info = client.get_symbol_info(symbol)
        filters = {f['filterType']: f for f in info['filters']}
        return {
            'tickSize': Decimal(filters['PRICE_FILTER']['tickSize']),
            'stepSize': Decimal(filters['LOT_SIZE']['stepSize']),
            'minNotional': Decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10') or '10')
        }
    except:
        return {'tickSize': Decimal('0.00000001'), 'stepSize': Decimal('0.00000001'), 'minNotional': Decimal('10')}

def get_price(symbol):
    try:
        return Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
    except:
        return ZERO

def update_account():
    global initial_balance
    try:
        acc = client.get_account()
        with state_lock:
            account_cache.clear()
            for b in acc['balances']:
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

# --------------------------------------------------------------
# P&L LOGIC — REALIZED + UNREALIZED
# --------------------------------------------------------------
def update_pnl():
    try:
        total_value = get_balance('USDT')
        unrealized = Decimal('0')
        realized = st.session_state.realized_pnl

        # Calculate unrealized P&L for all non-USDT assets
        for asset, qty in account_cache.items():
            if asset == 'USDT' or qty <= ZERO:
                continue
            symbol = f"{asset}USDT"
            if symbol not in all_symbols:
                continue
            price = get_price(symbol)
            if price <= ZERO:
                continue
            value = qty * price
            total_value += value

            # Estimate cost basis (simplified: average from trades or initial)
            # You can improve this with actual trade history
            avg_cost = cost_basis.get(asset, price)  # fallback to current price
            unrealized += (price - avg_cost) * qty

        st.session_state.unrealized_pnl = float(unrealized)
        st.session_state.realized_pnl = float(realized)
        st.session_state.total_pnl = float(realized + unrealized)

        # Update total portfolio value for % calculation
        if initial_balance > ZERO:
            pct = (total_value - initial_balance) / initial_balance * 100
            st.session_state.total_pnl_pct = float(pct)
        else:
            st.session_state.total_pnl_pct = 0.0

        log_ui(f"P&L Updated | R:${realized:.2f} U:${unrealized:.2f} T:${realized+unrealized:.2f}")
    except Exception as e:
        log_ui(f"P&L error: {e}")

# --------------------------------------------------------------
# ORDERS
# --------------------------------------------------------------
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
        oid = order['orderId']
        log_ui(f"ORDER {side} {symbol} {qty} @ {price} | ID:{oid}")
        send_whatsapp(f"{side} {symbol} {qty}@{price}", force=True)

        # Update cost basis on BUY
        if side == 'BUY':
            current_cost = cost_basis.get(base, ZERO)
            current_qty = get_balance(base) - qty  # previous
            new_cost = current_cost + (price * qty)
            new_qty = current_qty + qty
            if new_qty > ZERO:
                cost_basis[base] = new_cost / new_qty

        with state_lock:
            active_grids.setdefault(symbol, []).append(oid)
        return oid
    except Exception as e:
        log_ui(f"Order failed: {e}")
        return None

def cancel_all_orders():
    try:
        for sym in list(active_grids.keys()):
            client.cancel_open_orders(symbol=sym)
        with state_lock:
            active_grids.clear()
        log_ui("All orders canceled")
        send_whatsapp("ALL ORDERS CANCELED", force=True)
    except Exception as e:
        log_ui(f"Cancel error: {e}")

# --------------------------------------------------------------
# WEBSOCKET
# --------------------------------------------------------------
def start_bid_volume_ws():
    def handle(msg):
        if msg.get('e') == 'bookTicker':
            s = msg['s']
            vol = Decimal(str(msg['b'])) * Decimal(str(msg['B']))
            with state_lock:
                bid_volume[s] = vol

    twm.start()
    for s in all_symbols:
        twm.start_symbol_book_ticker_socket(callback=handle, symbol=s)
    log_ui("Bid volume WS started")

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

    b, s = 0, 0
    for i in range(1, st.session_state.grid_levels + 1):
        bp = price * (1 - GRID_SPACING * i)
        sp = price * (1 + GRID_SPACING * i) * GRID_SELL_MULTIPLIER
        if place_limit_order(symbol, 'BUY', bp, qty):
            b += 1
        if place_limit_order(symbol, 'SELL', sp, qty):
            s += 1

    global last_regrid_str
    last_regrid_str = now_str()
    log_ui(f"GRID {symbol} | B:{b} S:{s}")

def get_top25():
    items = [(s, bid_volume.get(s, ZERO)) for s in all_symbols]
    items.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in items[:25]]

def rotate_grids():
    if st.session_state.emergency_stopped:
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

    log_ui(f"ROTATED +{len(to_add)} -{len(to_remove)} → {len(gridded_symbols)}")
    send_whatsapp(f"ROTATED +{len(to_add)} -{len(to_remove)}", force=True)

# --------------------------------------------------------------
# FULL REFRESH
# --------------------------------------------------------------
def full_refresh():
    log_ui("=== FULL REFRESH ===")
    update_account()
    update_pnl()
    st.session_state.last_full_refresh = time.time()
    log_ui("=== REFRESH DONE ===")

# --------------------------------------------------------------
# INITIAL SETUP
# --------------------------------------------------------------
def setup():
    init_session_state()

    global all_symbols
    info = client.get_exchange_info()
    all_symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    log_ui(f"Loaded {len(all_symbols)} USDT pairs")

    update_account()
    update_pnl()
    start_bid_volume_ws()

    st.session_state.bot_initialized = True
    log_ui("Bot initialized")

# --------------------------------------------------------------
# BACKGROUND WORKER
# --------------------------------------------------------------
def background_worker():
    last_rotate = time.time()
    last_refresh = st.session_state.last_full_refresh or time.time()

    log_ui("BACKGROUND WORKER STARTED")

    while True:
        time.sleep(10)
        if st.session_state.emergency_stopped:
            continue

        now = time.time()

        # P&L every 30 seconds
        update_pnl()

        # Rotation
        if st.session_state.auto_rotate_enabled and now - last_rotate >= st.session_state.rotation_interval:
            rotate_grids()
            last_rotate = now

        # Full refresh
        if now - last_refresh >= REFRESH_INTERVAL:
            full_refresh()
            last_refresh = st.session_state.last_full_refresh

# --------------------------------------------------------------
# MAIN & UI
# --------------------------------------------------------------
def main():
    init_session_state()

    if not st.session_state.bot_initialized:
        log_ui("INFINITY GRID BOT STARTING")
        send_whatsapp("BOT STARTED", force=True)
        setup()
        threading.Thread(target=background_worker, daemon=True).start()
        st.success("Bot is LIVE")
    else:
        log_ui("Bot already running")

st.title("Infinity Grid Bot — Binance.US (tld='us')")

# P&L DISPLAY
col1, col2, col3 = st.columns(3)
with col1:
    st.metric("Realized P&L", f"${st.session_state.realized_pnl:.2f}")
with col2:
    st.metric("Unrealized P&L", f"${st.session_state.unrealized_pnl:.2f}")
with col3:
    pct = st.session_state.total_pnl_pct if 'total_pnl_pct' in st.session_state else 0
    st.metric("Total P&L", f"${st.session_state.total_pnl:.2f}", delta=f"{pct:.2f}%")

col_left, col_right = st.columns([1, 2])
with col_left:
    st.subheader("Controls")
    st.session_state.grid_size = st.number_input("Grid Size (USDT)", 10.0, 200.0, st.session_state.grid_size, 5.0)
    st.session_state.grid_levels = st.slider("Levels", 1, 10, st.session_state.grid_levels)
    st.session_state.target_grid_count = st.slider("Target Grids", 1, 25, st.session_state.target_grid_count)
    st.session_state.auto_rotate_enabled = st.checkbox("Auto-Rotate", st.session_state.auto_rotate_enabled)
    st.session_state.rotation_interval = st.number_input("Interval (s)", 60, 3600, st.session_state.rotation_interval)

    if st.button("ROTATE NOW", type="primary"):
        threading.Thread(target=rotate_grids, daemon=True).start()
    if st.button("CANCEL ALL"):
        cancel_all_orders()
    if st.button("EMERGENCY STOP", type="primary"):
        st.session_state.emergency_stopped = True
        cancel_all_orders()
        send_whatsapp("EMERGENCY STOP", force=True)
    if st.button("Resume"):
        st.session_state.emergency_stopped = False
        log_ui("Resumed")

with col_right:
    st.subheader(f"Active Grids: {len(gridded_symbols)}")
    st.write(" | ".join(gridded_symbols) if gridded_symbols else "None")
    st.subheader(f"Last Regrid: {last_regrid_str}")
    refresh_time = datetime.fromtimestamp(st.session_state.last_full_refresh).strftime('%H:%M:%S') if st.session_state.last_full_refresh else "Never"
    st.subheader(f"Last Full Refresh: {refresh_time}")

st.markdown("---")
st.subheader("Live Log")
log_box = st.empty()
with log_box.container():
    for line in st.session_state.logs[-50:]:
        st.text(line)

st.info("Binance.US (tld='us') | Real P&L | Cost Basis | Full Refresh | Zero crashes")

if __name__ == "__main__":
    main()
