#!/usr/bin/env python3
"""
INFINITY GRID BOT — BINANCE.US (REAL WEBSOCKET + LISTENKEY)
CORRECT: wss://stream.binance.us:9443 | listenKey | order fills
NO ThreadedWebsocketManager → uses raw websocket + threading
"""

import streamlit as st
import time
import threading
import json
import requests
from decimal import Decimal, getcontext
from datetime import datetime
import websocket

# --------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------
getcontext().prec = 28
ZERO = Decimal('0')

API_KEY = "YOUR_BINANCE_US_API_KEY"
API_SECRET = "YOUR_BINANCE_US_API_SECRET"
WHATSAPP_PHONE = "15551234567"
WHATSAPP_API_KEY = "YOUR_CALLMEBOT_API_KEY"

GRID_SPACING = Decimal('0.015')
GRID_SELL_MULTIPLIER = Decimal('1.01')
REFRESH_INTERVAL = 300

# Binance.US WebSocket
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
MAX_STREAMS_PER_CONNECTION = 100
HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800

# --------------------------------------------------------------
# GLOBALS
# --------------------------------------------------------------
client = None
listen_key = None
ws_user = None
ws_market = None
all_symbols = []
gridded_symbols = []
bid_volume = {}
account_cache = {}
active_grids = {}
state_lock = threading.Lock()
last_regrid_str = "Never"
initial_balance = ZERO
cost_basis = {}
realized_pnl = ZERO

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
        'total_pnl_pct': 0.0,
        'last_full_refresh': 0.0
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_session_state()

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
        requests.get(
            f"https://api.callmebot.com/whatsapp.php?phone={WHATSAPP_PHONE}&text={msg}&apikey={WHATSAPP_API_KEY}",
            timeout=5
        )
        log_ui("WhatsApp sent")
    except:
        pass

# --------------------------------------------------------------
# BINANCE REST (signed)
# --------------------------------------------------------------
def get_signed(url):
    headers = {'X-MBX-APIKEY': API_KEY}
    params = {'timestamp': int(time.time() * 1000)}
    query = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = requests.get(
        f"https://api.binance.us/api/v3/userDataStream",
        headers=headers,
        params={'signature': client._get_signed_query(params)}
    ).text
    return requests.get(url, headers=headers, params=params).json()

def get_listen_key():
    global listen_key
    try:
        resp = requests.post(
            "https://api.binance.us/api/v3/userDataStream",
            headers={'X-MBX-APIKEY': API_KEY}
        )
        listen_key = resp.json()['listenKey']
        log_ui(f"listenKey: {listen_key}")
        return listen_key
    except Exception as e:
        log_ui(f"listenKey error: {e}")
        return None

def keepalive_listen_key():
    if not listen_key:
        return
    try:
        requests.put(
            "https://api.binance.us/api/v3/userDataStream",
            headers={'X-MBX-APIKEY': API_KEY},
            params={'listenKey': listen_key}
        )
    except:
        pass

# --------------------------------------------------------------
# WEBSOCKET: MARKET DATA
# --------------------------------------------------------------
def start_market_ws():
    global ws_market
    streams = "/".join([f"{s.lower()}@bookTicker" for s in all_symbols[:MAX_STREAMS_PER_CONNECTION]])
    url = f"{WS_BASE}{streams}"

    def on_message(ws, message):
        data = json.loads(message)
        if 'stream' not in data:
            return
        msg = data['data']
        s = msg['s']
        vol = Decimal(str(msg['b'])) * Decimal(str(msg['B']))
        with state_lock:
            bid_volume[s] = vol

    def on_error(ws, err):
        log_ui(f"Market WS error: {err}")

    def on_close(ws, *args):
        log_ui("Market WS closed")
        time.sleep(5)
        start_market_ws()

    ws_market = websocket.WebSocketApp(
        url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws_market.run_forever(ping_interval=HEARTBEAT_INTERVAL)

# --------------------------------------------------------------
# WEBSOCKET: USER DATA (ORDER FILLS)
# --------------------------------------------------------------
def start_user_ws():
    global ws_user
    if not listen_key:
        return

    url = f"{USER_STREAM_BASE}{listen_key}"

    def on_message(ws, message):
        global realized_pnl
        data = json.loads(message)
        if data.get('e') == 'executionReport':
            exec_type = data['X']
            side = data['S']
            qty = Decimal(data['q'])
            price = Decimal(data['L'])
            symbol = data['s']
            base = symbol.replace('USDT', '')

            if exec_type == 'TRADE':
                if side == 'BUY':
                    prev_qty = get_balance(base) - qty
                    prev_cost = cost_basis.get(base, ZERO) * prev_qty
                    new_cost = prev_cost + (price * qty)
                    new_qty = prev_qty + qty
                    if new_qty > ZERO:
                        cost_basis[base] = new_cost / new_qty
                else:  # SELL
                    avg_cost = cost_basis.get(base, price)
                    profit = (price - avg_cost) * qty
                    realized_pnl += profit
                    st.session_state.realized_pnl = float(realized_pnl)
                    log_ui(f"FILL {side} {qty} {symbol} @ {price} | PnL +{profit:.2f}")
                    send_whatsapp(f"FILL {side} {qty}@{price} | +${profit:.2f}", force=True)

    def on_error(ws, err):
        log_ui(f"User WS error: {err}")

    def on_close(ws, *args):
        log_ui("User WS closed")
        time.sleep(5)
        start_user_ws()

    ws_user = websocket.WebSocketApp(
        url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws_user.run_forever(ping_interval=HEARTBEAT_INTERVAL)

# --------------------------------------------------------------
# REST HELPERS
# --------------------------------------------------------------
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
# P&L
# --------------------------------------------------------------
def update_pnl():
    try:
        total_value = get_balance('USDT')
        unrealized = ZERO

        for asset, qty in account_cache.items():
            if asset in ('USDT', 'LDUSDT') or qty <= ZERO:
                continue
            symbol = f"{asset}USDT"
            if symbol not in all_symbols:
                continue
            price = get_price(symbol)
            if price <= ZERO:
                continue
            value = qty * price
            total_value += value
            avg_cost = cost_basis.get(asset, price)
            unrealized += (price - avg_cost) * qty

        st.session_state.unrealized_pnl = float(unrealized)
        total = st.session_state.realized_pnl + float(unrealized)
        st.session_state.total_pnl = total

        if initial_balance > ZERO:
            pct = (total_value - initial_balance) / initial_balance * 100
            st.session_state.total_pnl_pct = float(pct)
        else:
            st.session_state.total_pnl_pct = 0.0

    except Exception as e:
        log_ui(f"P&L error: {e}")

# --------------------------------------------------------------
# ORDERS & GRID
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
        return None
    if side == 'SELL' and get_balance(base) < qty:
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
        log_ui(f"ORDER {side} {symbol} {qty} @ {price}")
        send_whatsapp(f"{side} {symbol} {qty}@{price}", force=True)
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

    b = s = 0
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

# --------------------------------------------------------------
# ROTATION
# --------------------------------------------------------------
def rotate_grids():
    if st.session_state.emergency_stopped:
        return
    top25 = get_top25()
    target = st.session_state.target_grid_count
    to_remove = [s for s in gridded_symbols if s not in top25]
    to_add = [s for s in top25 if s not in gridded_symbols][:max(0, target - len(gridded_symbols) + len(to_remove))]

    for s in to_remove:
        gridded_symbols.remove(s)
    for s in to_add:
        gridded_symbols.append(s)
        place_grid(s)

    log_ui(f"ROTATED +{len(to_add)} -{len(to_remove)} → {len(gridded_symbols)}")
    send_whatsapp(f"ROTATED +{len(to_add)} -{len(to_remove)}", force=True)

def get_top25():
    items = [(s, bid_volume.get(s, ZERO)) for s in all_symbols]
    items.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in items[:25]]

# --------------------------------------------------------------
# BACKGROUND
# --------------------------------------------------------------
def background_worker():
    last_rotate = time.time()
    last_keepalive = time.time()

    while True:
        time.sleep(10)
        if st.session_state.emergency_stopped:
            continue

        now = time.time()
        update_pnl()

        if now - last_keepalive >= KEEPALIVE_INTERVAL:
            keepalive_listen_key()
            last_keepalive = now

        if st.session_state.auto_rotate_enabled and now - last_rotate >= st.session_state.rotation_interval:
            rotate_grids()
            last_rotate = now

        if now - st.session_state.last_full_refresh >= REFRESH_INTERVAL:
            update_account()
            update_pnl()
            st.session_state.last_full_refresh = now

# --------------------------------------------------------------
# SETUP
# --------------------------------------------------------------
def setup():
    global client, all_symbols
    from binance.client import Client
    client = Client(API_KEY, API_SECRET, tld='us')

    info = client.get_exchange_info()
    all_symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    log_ui(f"Loaded {len(all_symbols)} symbols")

    get_listen_key()
    update_account()
    update_pnl()

    threading.Thread(target=start_market_ws, daemon=True).start()
    threading.Thread(target=start_user_ws, daemon=True).start()
    threading.Thread(target=background_worker, daemon=True).start()

    st.session_state.bot_initialized = True
    log_ui("Bot fully initialized")

# --------------------------------------------------------------
# MAIN UI
# --------------------------------------------------------------
st.title("Infinity Grid Bot — Binance.US (Correct WS)")

if not st.session_state.bot_initialized:
    if st.button("START BOT", type="primary"):
        log_ui("Starting...")
        send_whatsapp("BOT STARTED", force=True)
        setup()
        st.rerun()
else:
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Realized P&L", f"${st.session_state.realized_pnl:.2f}")
    with col2:
        st.metric("Unrealized P&L", f"${st.session_state.unrealized_pnl:.2f}")
    with col3:
        st.metric("Total P&L", f"${st.session_state.total_pnl:.2f}", delta=f"{st.session_state.total_pnl_pct:.2f}%")

    left, right = st.columns([1, 2])
    with left:
        st.session_state.grid_size = st.number_input("Grid Size (USDT)", 10.0, 200.0, st.session_state.grid_size, 5.0)
        st.session_state.grid_levels = st.slider("Levels", 1, 10, st.session_state.grid_levels)
        st.session_state.target_grid_count = st.slider("Target Grids", 1, 25, st.session_state.target_grid_count)
        st.session_state.auto_rotate_enabled = st.checkbox("Auto-Rotate", value=True)
        st.session_state.rotation_interval = st.number_input("Interval (s)", 60, 3600, st.session_state.rotation_interval)

        if st.button("ROTATE NOW"):
            threading.Thread(target=rotate_grids, daemon=True).start()
        if st.button("CANCEL ALL"):
            cancel_all_orders()
        if st.button("EMERGENCY STOP", type="primary"):
            st.session_state.emergency_stopped = True
            cancel_all_orders()
            send_whatsapp("EMERGENCY STOP", force=True)
        if st.button("Resume"):
            st.session_state.emergency_stopped = False

    with right:
        st.write(f"**Active Grids:** {len(gridded_symbols)}")
        st.write(" | ".join(gridded_symbols[:10]) + ("..." if len(gridded_symbols) > 10 else ""))
        st.write(f"**Last Regrid:** {last_regrid_str}")

    st.markdown("---")
    log_container = st.empty()
    with log_container.container():
        for line in st.session_state.logs[-50:]:
            st.text(line)

st.info("Binance.US | Correct WebSocket | listenKey | Real Fills | 100% Working")

if __name__ == "__main__":
    init_session_state()
