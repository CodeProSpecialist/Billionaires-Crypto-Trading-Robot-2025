#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — SAFE MONEY EDITION
- Rebalance on every fill
- Auto-rotate on timer
- WebSocket always alive + status
- gridded_symbols & total_positions updated in real-time
- Dashboard shows Active / Total
"""

import streamlit as st
import os
import time
import threading
import json
import requests
import websocket
from decimal import Decimal, getcontext
from datetime import datetime
import pytz
import logging
from logging.handlers import TimedRotatingFileHandler
import sys
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ========================= CONFIG =========================
getcontext().prec = 28
ZERO = Decimal('0')
SAFETY_BUFFER = Decimal('0.95')
COOLDOWN_SECONDS = 72 * 3600

LOG_FILE = os.path.expanduser("~/infinity_grid_bot.log")
COUNTER_FILE = "run_counter.txt"

def setup_logging():
    logger = logging.getLogger("infinity_bot")
    logger.setLevel(logging.INFO)
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, 'a').close()
    file_handler = TimedRotatingFileHandler(LOG_FILE, when='midnight', backupCount=14)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

logger = setup_logging()

# ========================= GLOBALS =========================
client = None
all_symbols = []
gridded_symbols = []
bid_volume = {}
symbol_info = {}
account_balances = {}
state_lock = threading.Lock()
last_regrid_str = "Never"

# ========================= UTILS =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    line = f"{now_str()} - {msg}"
    print(line)
    logger.info(msg)
    if 'logs' in st.session_state:
        st.session_state.logs.append(line)
        if len(st.session_state.logs) > 500:
            st.session_state.logs = st.session_state.logs[-500:]

def send_whatsapp(msg):
    phone = os.getenv('CALLMEBOT_PHONE')
    key = os.getenv('CALLMEBOT_API_KEY')
    if phone and key:
        try:
            msg = requests.utils.quote(msg)
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={msg}&apikey={key}", timeout=5)
        except Exception as e:
            log(f"WhatsApp send failed: {e}")

# ========================= RUN COUNTER WITH STATE SYNC =========================
def increment_run_counter(to_add, to_remove, new_grid_count, total_positions):
    """
    Updates:
    - run_counter.txt
    - WhatsApp alert
    - st.session_state.gridded_symbols (in sync)
    - st.session_state.total_positions
    """
    central_tz = pytz.timezone('US/Central')

    if os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE, 'r') as f:
            run_number = len(f.readlines()) + 1
    else:
        run_number = 1

    now_central = datetime.now(central_tz)
    timestamp = now_central.strftime("%B %d %Y %H:%M:%S Central Time")
    timestamp = timestamp.replace(" 0", " ").replace("  ", " ")

    summary = (f"+{len(to_add)} Buy, "
               f"-{len(to_remove)} Sell = "
               f"{new_grid_count} Total Grids for {total_positions} positions")

    line = f"Run #{run_number}: {timestamp} | {summary}\n"

    with open(COUNTER_FILE, 'a') as f:
        f.write(line)

    log(f"Rebalance recorded → {summary}")
    alert_msg = f"REBALANCE #{run_number}\n{summary}\n{timestamp}"
    send_whatsapp(alert_msg)

    # === CRITICAL: Sync Streamlit state ===
    st.session_state.gridded_symbols = gridded_symbols.copy()
    st.session_state.total_positions = total_positions
    st.session_state.last_regrid_str = now_str()

# ========================= BALANCE & INFO =========================
def update_balances():
    global account_balances
    try:
        info = client.get_account()['balances']
        account_balances = {
            a['asset']: Decimal(a['free']) for a in info if Decimal(a['free']) > ZERO
        }
        log(f"Updated balances: USDT={account_balances.get('USDT', ZERO):.2f}")
    except Exception as e:
        log(f"Balance update failed: {e}")

def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()['symbols']
        for s in info:
            if s['quoteAsset'] != 'USDT' or s['status'] != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s['filters']}
            symbol_info[s['symbol']] = {
                'stepSize': Decimal(filters['LOT_SIZE']['stepSize']),
                'tickSize': Decimal(filters['PRICE_FILTER']['tickSize']),
                'minQty': Decimal(filters['LOT_SIZE']['minQty']),
                'minNotional': Decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            }
        log(f"Loaded info for {len(symbol_info)} symbols")
    except Exception as e:
        log(f"Symbol info error: {e}")

# ========================= SAFE ORDER FUNCTION =========================
def round_step(value, step):
    return (value // step) * step

def place_limit_order(symbol, side, price, quantity):
    info = symbol_info.get(symbol)
    if not info:
        log(f"No symbol info for {symbol}")
        return False

    price = round_step(price, info['tickSize'])
    quantity = round_step(quantity, info['stepSize'])

    if quantity < info['minQty']:
        log(f"Qty too small {symbol}: {quantity} < {info['minQty']}")
        return False

    notional = price * quantity
    if notional < Decimal(info['minNotional']):
        log(f"Notional too low {symbol}: {notional} < {info['minNotional']}")
        return False

    if side == 'BUY':
        needed = notional * SAFETY_BUFFER
        usdt_free = account_balances.get('USDT', ZERO)
        if needed > usdt_free:
            log(f"NOT ENOUGH USDT for {symbol} BUY: need {needed:.2f}, have {usdt_free:.2f}")
            return False

    if side == 'SELL':
        base = symbol.replace('USDT', '')
        coin_free = account_balances.get(base, ZERO)
        if quantity > coin_free * SAFETY_BUFFER:
            log(f"NOT ENOUGH {base} for SELL: need {quantity}, have {coin_free}")
            return False

    try:
        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=f"{quantity:.8f}".rstrip('0').rstrip('.'),
            price=f"{price:.8f}".rstrip('0').rstrip('.')
        )
        log(f"PLACED {side} {symbol} {quantity} @ {price}")
        send_whatsapp(f"{side} {symbol} {quantity}@{price}")
        return True
    except BinanceAPIException as e:
        log(f"ORDER FAILED {symbol} {side}: {e.message} (Code: {e.code})")
        return False
    except Exception as e:
        log(f"ORDER ERROR {symbol}: {e}")
        return False

def place_market_sell(symbol):
    info = symbol_info.get(symbol)
    if not info:
        return False

    base = symbol.replace('USDT', '')
    quantity = account_balances.get(base, ZERO) * SAFETY_BUFFER
    quantity = round_step(quantity, info['stepSize'])

    if quantity < info['minQty']:
        log(f"Qty too small for market sell {symbol}: {quantity}")
        return False

    try:
        order = client.create_order(
            symbol=symbol,
            side='SELL',
            type='MARKET',
            quantity=f"{quantity:.8f}".rstrip('0').rstrip('.')
        )
        log(f"MARKET SOLD {symbol} {quantity}")
        send_whatsapp(f"MARKET SOLD {symbol} {quantity}")
        return True
    except BinanceAPIException as e:
        log(f"MARKET SELL FAILED {symbol}: {e.message} (Code: {e.code})")
        return False
    except Exception as e:
        log(f"MARKET SELL ERROR {symbol}: {e}")
        return False

# ========================= SAFE GRID =========================
def place_grid(symbol):
    price = bid_volume.get(symbol, ZERO)
    if price <= ZERO:
        log(f"No price for {symbol}")
        return

    qty_usdt = Decimal(str(st.session_state.grid_size))
    raw_qty = qty_usdt / price
    info = symbol_info.get(symbol)
    if not info:
        return

    qty = round_step(raw_qty, info['stepSize'])
    if qty < info['minQty']:
        log(f"Qty too small for {symbol}")
        return

    total_buy_cost = qty * price * st.session_state.grid_levels * SAFETY_BUFFER
    if total_buy_cost > account_balances.get('USDT', ZERO):
        log(f"SKIPPING {symbol}: Not enough USDT for {st.session_state.grid_levels} buy levels")
        return

    buys = sells = 0
    for i in range(1, st.session_state.grid_levels + 1):
        buy_price = price * (1 - Decimal('0.015') * i)
        sell_price = price * (1 + Decimal('0.015') * i) * Decimal('1.01')

        if place_limit_order(symbol, 'BUY', buy_price, qty):
            buys += 1
        if place_limit_order(symbol, 'SELL', sell_price, qty):
            sells += 1

    log(f"GRID {symbol} | B:{buys} S:{sells} | ${qty_usdt} per level")

# ========================= MAINTAIN MANDATORY BUYS =========================
def maintain_mandatory_buys():
    update_balances()
    for asset, bal in account_balances.items():
        if asset == 'USDT' or bal <= ZERO:
            continue
        symbol = asset + 'USDT'
        if symbol not in symbol_info:
            continue
        if symbol in st.session_state.blacklisted:
            timestamp = st.session_state.blacklisted[symbol]
            if time.time() - timestamp < COOLDOWN_SECONDS:
                continue
        try:
            open_orders = client.get_open_orders(symbol=symbol)
            has_buy = any(o['side'] == 'BUY' for o in open_orders)
            if has_buy:
                continue
        except Exception as e:
            log(f"Open orders check failed for {symbol}: {e}")
            continue
        price = bid_volume.get(symbol, ZERO)
        if price <= ZERO:
            continue
        buy_price = price * Decimal('0.985')
        qty_usdt = Decimal(str(st.session_state.grid_size))
        raw_qty = qty_usdt / buy_price
        info = symbol_info[symbol]
        qty = round_step(raw_qty, info['stepSize'])
        if place_limit_order(symbol, 'BUY', buy_price, qty):
            log(f"Placed mandatory BUY for owned {symbol} @ {buy_price}")

# ========================= ROTATE WITH SAFETY =========================
def rotate_grids():
    update_balances()
    if not bid_volume:
        log("No price data, skipping rotation")
        return

    top = sorted(bid_volume.items(), key=lambda x: x[1], reverse=True)[:25]
    targets = [s for s, _ in top][:st.session_state.target_grid_count]

    to_add = [s for s in targets if s not in gridded_symbols]
    to_remove = [s for s in gridded_symbols if s not in targets]

    for s in to_remove:
        try:
            client.cancel_open_orders(symbol=s)
            log(f"Canceled orders for {s}")
        except Exception as e:
            log(f"Cancel failed for {s}: {e}")
        place_market_sell(s)
        st.session_state.blacklisted[s] = time.time()
        gridded_symbols.remove(s)

    for s in to_add:
        if s not in symbol_info:
            continue
        if s in st.session_state.blacklisted:
            timestamp = st.session_state.blacklisted[s]
            if time.time() - timestamp < COOLDOWN_SECONDS:
                log(f"Skipping {s} due to cooldown")
                continue
            else:
                del st.session_state.blacklisted[s]
        gridded_symbols.append(s)
        place_grid(s)

    maintain_mandatory_buys()

    total_positions = len([s for s in all_symbols if s.endswith('USDT')])

    log(f"ROTATED +{len(to_add)} -{len(to_remove)} → {len(gridded_symbols)} GRIDS")

    # === SYNC STATE TO DASHBOARD ===
    increment_run_counter(
        to_add=to_add,
        to_remove=to_remove,
        new_grid_count=len(gridded_symbols),
        total_positions=total_positions
    )

# ========================= WEBSOCKET =========================
def start_websockets():
    if st.session_state.get('ws_thread_running', False):
        return

    streams = "/".join([f"{s.lower()}@ticker" for s in all_symbols[:100]])
    url = f"wss://stream.binance.us:9443/stream?streams={streams}"

    def run():
        st.session_state.ws_thread_running = True
        st.session_state.ws_last_connected = now_str()
        log("WebSocket thread started")
        ws = websocket.WebSocketApp(url, on_message=lambda ws, msg: on_msg(msg))
        while st.session_state.bot_running and not st.session_state.shutdown:
            try:
                ws.run_forever(ping_interval=25)
                if st.session_state.bot_running:
                    time.sleep(5)
            except Exception as e:
                log(f"WS crashed: {e}")
                time.sleep(5)
        st.session_state.ws_thread_running = False
        log("WebSocket thread terminated")

    threading.Thread(target=run, daemon=True).start()

def on_msg(msg):
    try:
        data = json.loads(msg)
        if 'data' not in data: return
        payload = data['data']
        sym = data['stream'].split('@')[0].upper()
        if payload.get('c'):
            with state_lock:
                bid_volume[sym] = Decimal(str(payload['c']))
    except: pass

# ========================= WEBSOCKET KEEPER =========================
def websocket_keeper():
    last_restart = 0
    while not st.session_state.shutdown:
        time.sleep(5)
        if st.session_state.bot_running:
            if (not st.session_state.get('ws_thread_running', False) or
                time.time() - last_restart > 3600):
                log("WebSocket keeper: starting/restarting...")
                start_websockets()
                last_restart = time.time()

# ========================= ORDER MONITOR =========================
def start_order_monitor():
    if st.session_state.get('order_monitor_running', False):
        return

    def keepalive(listen_key):
        while not st.session_state.shutdown:
            time.sleep(1800)
            try:
                client.stream_keepalive(listen_key)
                log("User stream keepalive sent")
            except Exception as e:
                log(f"Keepalive failed: {e}")

    def on_open(ws): log("User stream connected")
    def on_close(ws, *args): log("User stream closed")
    def on_error(ws, error): log(f"User stream error: {error}")

    def on_message(ws, msg):
        try:
            data = json.loads(msg)
            if data['e'] == 'executionReport' and data['X'] in ['FILLED', 'PARTIALLY_FILLED']:
                symbol = data['s']
                side = data['S']
                qty = data['q']
                price = data['p']
                log(f"Order filled for {symbol}: {side} {qty} @ {price}")
                rotate_grids()
        except Exception as e:
            log(f"User stream message error: {e}")

    try:
        listen_key = client.stream_get_listen_key()['listenKey']
    except Exception as e:
        log(f"Failed to get listen key: {e}")
        return

    url = f"wss://stream.binance.us:9443/ws/{listen_key}"
    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    threading.Thread(target=keepalive, args=(listen_key,), daemon=True).start()

    def run_ws():
        while not st.session_state.shutdown:
            try:
                ws.run_forever(ping_interval=25)
                time.sleep(5)
            except:
                time.sleep(5)

    threading.Thread(target=run_ws, daemon=True).start()
    st.session_state.order_monitor_running = True
    log("Order monitor thread started")

# ========================= AUTO-ROTATE =========================
def auto_rotate_loop():
    while not st.session_state.shutdown:
        time.sleep(st.session_state.rotation_interval)
        if st.session_state.auto_rotate and st.session_state.bot_running:
            log("AUTO-ROTATE triggered by timer")
            rotate_grids()

# ========================= MAIN =========================
def main():
    defaults = {
        'bot_running': False, 'grid_size': 50.0, 'grid_levels': 5,
        'target_grid_count': 10, 'auto_rotate': True, 'rotation_interval': 300,
        'logs': [], 'shutdown': False, 'blacklisted': {},
        'order_monitor_running': False, 'ws_thread_running': False,
        'gridded_symbols': [], 'total_positions': 0, 'last_regrid_str': "Never"
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    st.set_page_config(page_title="Safe Grid Bot", layout="wide")
    st.title("INFINITY GRID BOT — SAFE MONEY")

    if not os.getenv('BINANCE_API_KEY'):
        st.error("Set BINANCE_API_KEY and BINANCE_API_SECRET")
        st.stop()

    global client
    client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'), tld='us')

    if not st.session_state.bot_running:
        if st.button("START BOT", type="primary"):
            with st.spinner("Starting..."):
                info = client.get_exchange_info()['symbols']
                global all_symbols
                all_symbols = [s['symbol'] for s in info if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
                load_symbol_info()
                update_balances()
                start_websockets()
                start_order_monitor()
                threading.Thread(target=websocket_keeper, daemon=True).start()
                threading.Thread(target=auto_rotate_loop, daemon=True).start()
                time.sleep(3)
                rotate_grids()
                st.session_state.bot_running = True
                log("BOT STARTED WITH CASH SAFETY")
                send_whatsapp("SAFE GRID BOT STARTED")
            st.rerun()
    else:
        update_balances()
        usdt = account_balances.get('USDT', ZERO)

        # === LIVE METRICS ===
        colm1, colm2, colm3, colm4 = st.columns(4)
        with colm1:
            st.metric("USDT Available", f"${usdt:.2f}")
        with colm2:
            active = len(st.session_state.gridded_symbols)
            total = st.session_state.total_positions
            st.metric("Active Grids", active, f"/ {total} total")
        with colm3:
            st.metric("Last Rebalance", st.session_state.last_regrid_str)
        with colm4:
            ws_status = "CONNECTED" if st.session_state.get('ws_thread_running', False) else "OFFLINE"
            ws_color = "green" if ws_status == "CONNECTED" else "red"
            last_conn = st.session_state.get('ws_last_connected', 'Never')
            st.markdown(
                f"<div style='text-align: center;'><strong>WS</strong><br>"
                f"<span style='color: {ws_color}; font-size: 1.2em;'>{ws_status}</span><br>"
                f"<small>{last_conn}</small></div>",
                unsafe_allow_html=True
            )

        col1, col2 = st.columns([1, 2])
        with col1:
            st.session_state.grid_size = st.number_input("Grid Size", 10.0, 500.0, st.session_state.grid_size)
            st.session_state.grid_levels = st.slider("Levels", 1, 10, st.session_state.grid_levels)
            st.session_state.target_grid_count = st.slider("Max Grids", 1, 25, st.session_state.target_grid_count)
            st.session_state.auto_rotate = st.checkbox("Auto-rotate", value=st.session_state.auto_rotate)
            st.session_state.rotation_interval = st.number_input(
                "Rotate every (seconds)", 60, 3600, st.session_state.rotation_interval, step=60
            )
            if st.button("Rotate Now"):
                rotate_grids()

        with col2:
            active_symbols = st.session_state.gridded_symbols[:15]
            st.write("**Active:** " + " | ".join(active_symbols) if active_symbols else "None")

        st.markdown("---")
        st.subheader("Log")
        for line in st.session_state.logs[-25:]:
            st.code(line)

        st.success("CASH & QTY CHECKED — NO OVERSpending")

        if st.button("STOP BOT", type="primary"):
            st.session_state.bot_running = False
            st.session_state.shutdown = True
            st.rerun()
        else:
            time.sleep(10)
            st.rerun()

if __name__ == "__main__":
    main()
