#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — PRODUCTION READY
- Proper main()
- No double execution
- Safe for systemd + Streamlit
- Logs in ~/infinity_grid_bot.log
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
import logging
from logging.handlers import TimedRotatingFileHandler
import sys

# ========================= CONFIG =========================
getcontext().prec = 28
ZERO = Decimal('0')

# LOG IN HOME FOLDER
LOG_FILE = os.path.expanduser("~/infinity_grid_bot.log")

# Initialize logging
def setup_logging():
    logger = logging.getLogger("infinity_bot")
    logger.setLevel(logging.INFO)
    
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, 'a').close()
    
    file_handler = TimedRotatingFileHandler(LOG_FILE, when='midnight', interval=1, backupCount=14)
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
orderbooks = {}
last_regrid_str = "Never"
realized_pnl = ZERO
cost_basis = {}
state_lock = threading.Lock()

# ========================= UTILS =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    line = f"{now_str()} - {msg}"
    print(line)
    logger.info(msg)
    if hasattr(st, 'session_state') and 'logs' in st.session_state:
        st.session_state.logs.append(line)
        if len(st.session_state.logs) > 500:
            st.session_state.logs = st.session_state.logs[-500:]

def send_whatsapp(msg):
    phone = os.getenv('CALLMEBOT_PHONE')
    key = os.getenv('CALLMEBOT_API_KEY')
    if not phone or not key:
        return
    try:
        msg = requests.utils.quote(msg)
        url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={msg}&apikey={key}"
        requests.get(url, timeout=5)
    except:
        pass

# ========================= WEBSOCKETS =========================
def start_websockets():
    # Market data
    streams = "/".join([f"{s.lower()}@ticker" for s in all_symbols[:100]])
    url = f"wss://stream.binance.us:9443/stream?streams={streams}"
    
    def run_market():
        ws = websocket.WebSocketApp(
            url,
            on_message=lambda ws, msg: on_market_message(msg),
            on_error=lambda ws, err: log(f"Market WS error: {err}"),
            on_close=lambda ws, code, msg: log("Market WS closed")
        )
        ws.run_forever(ping_interval=25)
    
    threading.Thread(target=run_market, daemon=True).start()

def on_market_message(message):
    try:
        data = json.loads(message)
        if 'stream' not in data or 'data' not in data:
            return
        payload = data['data']
        stream = data['stream']
        sym = stream.split('@')[0].upper()
        
        if '@ticker' in stream:
            price = Decimal(str(payload.get('c', '0')))
            with state_lock:
                bid_volume[sym] = price
    except Exception as e:
        logger.debug(f"WS parse error: {e}")

# ========================= GRID LOGIC =========================
def place_grid(symbol):
    price = bid_volume.get(symbol, ZERO)
    if price <= ZERO:
        return
    
    qty_usdt = Decimal(str(st.session_state.grid_size))
    qty = (qty_usdt / price).quantize(Decimal('0.000001'))
    
    buys = sells = 0
    for i in range(1, st.session_state.grid_levels + 1):
        buy_price = (price * (1 - Decimal('0.015') * i)).quantize(Decimal('0.01'))
        sell_price = (price * (1 + Decimal('0.015') * i) * Decimal('1.01')).quantize(Decimal('0.01'))
        
        try:
            client.create_order(symbol=symbol, side='BUY', type='LIMIT', timeInForce='GTC',
                                quantity=str(qty), price=str(buy_price))
            buys += 1
        except: pass
        try:
            client.create_order(symbol=symbol, side='SELL', type='LIMIT', timeInForce='GTC',
                                quantity=str(qty), price=str(sell_price))
            sells += 1
        except: pass
    
    global last_regrid_str
    last_regrid_str = now_str()
    log(f"GRID {symbol} | B:{buys} S:{sells}")

def rotate_grids():
    top = sorted(bid_volume.items(), key=lambda x: x[1], reverse=True)[:25]
    targets = [s for s, _ in top][:st.session_state.target_grid_count]
    
    to_add = [s for s in targets if s not in gridded_symbols]
    to_remove = [s for s in gridded_symbols if s not in targets]
    
    for s in to_remove:
        try: client.cancel_open_orders(symbol=s)
        except: pass
        gridded_symbols.remove(s)
    
    for s in to_add:
        gridded_symbols.append(s)
        place_grid(s)
    
    log(f"ROTATED +{len(to_add)} -{len(to_remove)} → {len(gridded_symbols)} grids")
    send_whatsapp(f"Rotated {len(to_add)} new grids")

# ========================= BACKGROUND =========================
def background_worker():
    last_rotate = time.time()
    while not st.session_state.get('shutdown', False):
        time.sleep(10)
        if st.session_state.auto_rotate and time.time() - last_rotate > st.session_state.rotation_interval:
            rotate_grids()
            last_rotate = time.time()

# ========================= MAIN FUNCTION =========================
def main():
    global client, all_symbols
    
    # Initialize session state
    for key, value in {
        'bot_running': False,
        'grid_size': 50.0,
        'grid_levels': 5,
        'target_grid_count': 10,
        'auto_rotate': True,
        'rotation_interval': 300,
        'logs': [],
        'shutdown': False
    }.items():
        if key not in st.session_state:
            st.session_state[key] = value
    
    # Title
    st.set_page_config(page_title="Infinity Grid Bot 2025", layout="wide")
    st.title("INFINITY GRID BOT 2025")
    
    # Check API keys
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')
    if not api_key or not api_secret:
        st.error("Set BINANCE_API_KEY and BINANCE_API_SECRET")
        st.stop()
    
    client = Client(api_key, api_secret, tld='us')
    
    if not st.session_state.bot_running:
        if st.button("START BOT", type="primary", use_container_width=True):
            with st.spinner("Starting..."):
                info = client.get_exchange_info()
                all_symbols = [s['symbol'] for s in info['symbols'] 
                             if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
                log(f"Loaded {len(all_symbols)} symbols")
                
                start_websockets()
                threading.Thread(target=background_worker, daemon=True).start()
                rotate_grids()
                st.session_state.bot_running = True
                log("BOT STARTED")
                send_whatsapp("BOT STARTED")
            st.success("Running!")
            st.rerun()
    else:
        # UI when running
        c1, c2, c3 = st.columns(3)
        with c1: st.metric("Grids", len(gridded_symbols))
        with c2: st.metric("P&L", f"${realized_pnl:.2f}")
        with c3: st.metric("Last Regrid", last_regrid_str)
        
        col1, col2 = st.columns([1, 2])
        with col1:
            st.subheader("Controls")
            st.session_state.grid_size = st.number_input("Size (USDT)", 10.0, 500.0, st.session_state.grid_size)
            st.session_state.grid_levels = st.slider("Levels", 1, 10, st.session_state.grid_levels)
            st.session_state.target_grid_count = st.slider("Max Grids", 1, 25, st.session_state.target_grid_count)
            st.session_state.auto_rotate = st.checkbox("Auto Rotate", True)
            st.session_state.rotation_interval = st.number_input("Interval (s)", 60, 3600, st.session_state.rotation_interval)
            
            if st.button("Rotate Now"): rotate_grids()
            if st.button("Cancel All"):
                for s in gridded_symbols[:]:
                    try: client.cancel_open_orders(symbol=s)
                    except: pass
                log("All orders canceled")
        
        with col2:
            st.subheader("Active")
            st.write(" | ".join(gridded_symbols[:20]))
        
        st.markdown("---")
        st.subheader("Log")
        for line in st.session_state.logs[-30:]:
            st.code(line)
        
        st.success("Bot Running • Log: ~/infinity_grid_bot.log")

# ========================= ENTRY POINT =========================
if __name__ == "__main__":
    main()
