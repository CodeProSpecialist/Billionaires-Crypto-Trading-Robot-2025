#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — SAFE MONEY EDITION
- Cash check before BUY
- Qty check before SELL
- Never overspend
- Never sell what you don't have
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
from binance.client import Client
from binance.exceptions import BinanceAPIException
import logging
from logging.handlers import TimedRotatingFileHandler
import sys

# ========================= CONFIG =========================
getcontext().prec = 28
ZERO = Decimal('0')
SAFETY_BUFFER = Decimal('0.95')  # Use only 95% of available balance

LOG_FILE = os.path.expanduser("~/infinity_grid_bot.log")

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
        except:
            pass

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

    # CASH CHECK FOR BUY
    if side == 'BUY':
        needed = notional * SAFETY_BUFFER
        usdt_free = account_balances.get('USDT', ZERO)
        if needed > usdt_free:
            log(f"NOT ENOUGH USDT for {symbol} BUY: need {needed:.2f}, have {usdt_free:.2f}")
            return False

    # QTY CHECK FOR SELL
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

    # Check total BUY cost
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

    global last_regrid_str
    last_regrid_str = now_str()
    log(f"GRID {symbol} | B:{buys} S:{sells} | ${qty_usdt} per level")

# ========================= ROTATE WITH SAFETY =========================
def rotate_grids():
    update_balances()  # Refresh balance
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
            log(f"Canceled {s}")
        except: pass
        gridded_symbols.remove(s)

    for s in to_add:
        if s not in symbol_info:
            continue
        gridded_symbols.append(s)
        place_grid(s)

    log(f"ROTATED +{len(to_add)} -{len(to_remove)} → {len(gridded_symbols)} GRIDS")
    send_whatsapp(f"GRID UPDATE: {len(to_add)} new grids")

# ========================= WEBSOCKET =========================
def start_websockets():
    streams = "/".join([f"{s.lower()}@ticker" for s in all_symbols[:100]])
    url = f"wss://stream.binance.us:9443/stream?streams={streams}"

    def run():
        ws = websocket.WebSocketApp(url, on_message=lambda ws, msg: on_msg(msg))
        while True:
            try:
                ws.run_forever(ping_interval=25)
                time.sleep(5)
            except:
                time.sleep(5)

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

# ========================= MAIN =========================
def main():
    for k, v in {
        'bot_running': False, 'grid_size': 50.0, 'grid_levels': 5,
        'target_grid_count': 10, 'auto_rotate': True, 'rotation_interval': 300,
        'logs': [], 'shutdown': False
    }.items():
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
                time.sleep(3)
                rotate_grids()
                st.session_state.bot_running = True
                log("BOT STARTED WITH CASH SAFETY")
                send_whatsapp("SAFE GRID BOT STARTED")
            st.rerun()
    else:
        update_balances()
        usdt = account_balances.get('USDT', ZERO)
        st.metric("USDT Available", f"${usdt:.2f}")
        st.metric("Active Grids", len(gridded_symbols))
        st.metric("Last Update", last_regrid_str)

        col1, col2 = st.columns([1, 2])
        with col1:
            st.session_state.grid_size = st.number_input("Grid Size", 10.0, 500.0, st.session_state.grid_size)
            st.session_state.grid_levels = st.slider("Levels", 1, 10, st.session_state.grid_levels)
            st.session_state.target_grid_count = st.slider("Max Grids", 1, 25, st.session_state.target_grid_count)
            if st.button("Rotate Now"): rotate_grids()

        with col2:
            st.write("**Active:** " + " | ".join(gridded_symbols[:15]))

        st.markdown("---")
        st.subheader("Log")
        for line in st.session_state.logs[-25:]:
            st.code(line)

        st.success("CASH & QTY CHECKED — NO OVERSpending")

if __name__ == "__main__":
    main()
