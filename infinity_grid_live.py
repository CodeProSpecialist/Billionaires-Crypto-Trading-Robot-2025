#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — AUTO-CYCLE EDITION
- 5 MIN RUN → 5 MIN SLEEP → REPEAT FOREVER
- No user control
- Hard-coded cycle
- Full safety + order book + error handling
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

# ──────────────────────  WEBSOCKET CORE  ──────────────────────
ws_connected = False
user_ws_connected = False
ws_instances: list = []
user_ws = None
listen_key: str | None = None
listen_key_lock = threading.Lock()

# ──────────────────────  ORDER BOOK STATE  ──────────────────────
best_bid   = {}
best_ask   = {}
ob_lock    = threading.Lock()
ob_active  = False

# ──────────────────────  AUTO-CYCLE CONTROL  ──────────────────────
bot_start_time = 0.0
STARTUP_PERIOD_SECONDS = 300   # 5 min run
SLEEP_PERIOD_SECONDS = 300     # 5 min sleep

HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800
MAX_STREAMS_PER_CONNECTION = 100
WS_BASE        = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"

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

def log(msg, level="INFO"):
    line = f"{now_str()} - {msg}"
    print(line)
    logger.log(getattr(logging, level), msg)
    if 'logs' in st.session_state:
        st.session_state.logs.append(line)
        if len(st.session_state.logs) > 500:
            st.session_state.logs = st.session_state.logs[-500:]
    if level in ("ERROR", "CRITICAL"):
        st.session_state.error_count += 1
        st.session_state.last_error = msg
        send_whatsapp(f"{level}: {msg}")

def send_whatsapp(msg):
    phone = os.getenv('CALLMEBOT_PHONE')
    key = os.getenv('CALLMEBOT_API_KEY')
    if phone and key:
        try:
            msg = requests.utils.quote(msg)
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={msg}&apikey={key}", timeout=5)
        except Exception as e:
            log(f"WhatsApp send failed: {e}", "ERROR")

def in_startup_period() -> bool:
    if bot_start_time <= 0:
        return False
    return (time.time() - bot_start_time) < STARTUP_PERIOD_SECONDS

# ========================= RUN COUNTER =========================
def increment_run_counter(to_add, to_remove, new_grid_count, total_positions):
    try:
        central_tz = pytz.timezone('US/Central')
        run_number = 1
        if os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE, 'r') as f:
                run_number = len(f.readlines()) + 1

        now_central = datetime.now(central_tz)
        timestamp = now_central.strftime("%B %d %Y %H:%M:%S Central Time").replace(" 0", " ").replace("  ", " ")
        summary = f"+{len(to_add)} Buy, -{len(to_remove)} Sell = {new_grid_count} Grids for {total_positions} positions"
        line = f"Run #{run_number}: {timestamp} | {summary}\n"

        with open(COUNTER_FILE, 'a') as f:
            f.write(line)

        log(f"Rebalance recorded → {summary}")
        alert_msg = f"REBALANCE #{run_number}\n{summary}\n{timestamp}"
        send_whatsapp(alert_msg)

        st.session_state.gridded_symbols = gridded_symbols.copy()
        st.session_state.total_positions = total_positions
        st.session_state.last_regrid_str = now_str()
    except Exception as e:
        log(f"increment_run_counter() error: {e}", "ERROR")

# ========================= BALANCE & INFO =========================
def update_balances():
    global account_balances
    try:
        info = client.get_account()['balances']
        account_balances = {a['asset']: Decimal(a['free']) for a in info if Decimal(a['free']) > ZERO}
        log(f"Updated balances: USDT={account_balances.get('USDT', ZERO):.2f}")
    except Exception as e:
        log(f"Balance update failed: {e}", "ERROR")

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
        log(f"Symbol info error: {e}", "ERROR")

# ========================= SAFE ORDER FUNCTION =========================
def round_step(value, step):
    try:
        return (value // step) * step
    except Exception as e:
        log(f"round_step error: {e}", "ERROR")
        return value

def place_limit_order(symbol, side, price, quantity):
    try:
        info = symbol_info.get(symbol)
        if not info:
            log(f"No symbol info for {symbol}", "ERROR")
            return False

        price = round_step(price, info['tickSize'])
        quantity = round_step(quantity, info['stepSize'])

        if quantity < info['minQty']:
            log(f"Qty too small {symbol}: {quantity} < {info['minQty']}", "WARNING")
            return False

        notional = price * quantity
        if notional < Decimal(info['minNotional']):
            log(f"Notional too low {symbol}: {notional} < {info['minNotional']}", "WARNING")
            return False

        if side == 'BUY':
            needed = notional * SAFETY_BUFFER
            usdt_free = account_balances.get('USDT', ZERO)
            if needed > usdt_free:
                log(f"NOT ENOUGH USDT for {symbol} BUY: need {needed:.2f}, have {usdt_free:.2f}", "WARNING")
                return False

        if side == 'SELL':
            base = symbol.replace('USDT', '')
            coin_free = account_balances.get(base, ZERO)
            if quantity > coin_free * SAFETY_BUFFER:
                log(f"NOT ENOUGH {base} for SELL: need {quantity}, have {coin_free}", "WARNING")
                return False

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
        log(f"ORDER FAILED {symbol} {side}: {e.message} (Code: {e.code})", "ERROR")
        return False
    except Exception as e:
        log(f"ORDER ERROR {symbol}: {e}", "ERROR")
        return False

def place_market_sell(symbol):
    try:
        info = symbol_info.get(symbol)
        if not info:
            return False
        base = symbol.replace('USDT', '')
        quantity = account_balances.get(base, ZERO) * SAFETY_BUFFER
        quantity = round_step(quantity, info['stepSize'])
        if quantity < info['minQty']:
            log(f"Qty too small for market sell {symbol}: {quantity}", "WARNING")
            return False
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
        log(f"MARKET SELL FAILED {symbol}: {e.message} (Code: {e.code})", "ERROR")
        return False
    except Exception as e:
        log(f"MARKET SELL ERROR {symbol}: {e}", "ERROR")
        return False

# ========================= GRID & ROTATE =========================
def place_grid(symbol):
    try:
        price = get_mid_price(symbol)
        if price <= ZERO:
            log(f"No price for {symbol}", "WARNING")
            return
        qty_usdt = Decimal('50')  # Hard-coded grid size
        raw_qty = qty_usdt / price
        info = symbol_info.get(symbol)
        if not info:
            return
        qty = round_step(raw_qty, info['stepSize'])
        if qty < info['minQty']:
            log(f"Qty too small for {symbol}", "WARNING")
            return
        total_buy_cost = qty * price * 5 * SAFETY_BUFFER
        if total_buy_cost > account_balances.get('USDT', ZERO):
            log(f"SKIPPING {symbol}: Not enough USDT for 5 buy levels", "WARNING")
            return
        buys = sells = 0
        for i in range(1, 6):
            buy_price = price * (1 - Decimal('0.015') * i)
            sell_price = price * (1 + Decimal('0.015') * i) * Decimal('1.01')
            if place_limit_order(symbol, 'BUY', buy_price, qty):
                buys += 1
            if place_limit_order(symbol, 'SELL', sell_price, qty):
                sells += 1
        global last_regrid_str
        last_regrid_str = now_str()
        log(f"GRID {symbol} | B:{buys} S:{sells}")
    except Exception as e:
        log(f"place_grid {symbol} error: {e}", "ERROR")

def maintain_mandatory_buys():
    try:
        update_balances()
        for asset, bal in account_balances.items():
            if asset == 'USDT' or bal <= ZERO:
                continue
            symbol = asset + 'USDT'
            if symbol not in symbol_info:
                continue
            if symbol in st.session_state.blacklisted:
                if time.time() - st.session_state.blacklisted[symbol] < COOLDOWN_SECONDS:
                    continue
            try:
                open_orders = client.get_open_orders(symbol=symbol)
                has_buy = any(o['side'] == 'BUY' for o in open_orders)
                if has_buy:
                    continue
            except Exception as e:
                log(f"Open orders check failed for {symbol}: {e}", "ERROR")
                continue
            price = get_mid_price(symbol)
            if price <= ZERO:
                continue
            buy_price = price * Decimal('0.985')
            qty_usdt = Decimal('50')
            raw_qty = qty_usdt / buy_price
            info = symbol_info[symbol]
            qty = round_step(raw_qty, info['stepSize'])
            if place_limit_order(symbol, 'BUY', buy_price, qty):
                log(f"Placed mandatory BUY for {symbol} @ {buy_price}")
    except Exception as e:
        log(f"maintain_mandatory_buys error: {e}", "ERROR")

def rotate_grids():
    try:
        update_balances()
        if not bid_volume:
            log("No price data, skipping rotation")
            return
        top = sorted(bid_volume.items(), key=lambda x: x[1], reverse=True)[:25]
        targets = [s for s, _ in top][:10]
        to_add = [s for s in targets if s not in gridded_symbols]
        to_remove = [s for s in gridded_symbols if s not in targets]
        for s in to_remove:
            try:
                client.cancel_open_orders(symbol=s)
            except Exception as e:
                log(f"Cancel failed for {s}: {e}", "ERROR")
            place_market_sell(s)
            st.session_state.blacklisted[s] = time.time()
            gridded_symbols.remove(s)
        for s in to_add:
            if s not in symbol_info:
                continue
            if s in st.session_state.blacklisted:
                if time.time() - st.session_state.blacklisted[s] < COOLDOWN_SECONDS:
                    log(f"Skipping {s} due to cooldown")
                    continue
                else:
                    del st.session_state.blacklisted[s]
            gridded_symbols.append(s)
            place_grid(s)
        maintain_mandatory_buys()
        total_positions = len([s for s in all_symbols if s.endswith('USDT')])
        log(f"ROTATED +{len(to_add)} -{len(to_remove)} → {len(gridded_symbols)} GRIDS")
        increment_run_counter(to_add, to_remove, len(gridded_symbols), total_positions)
    except Exception as e:
        log(f"rotate_grids error: {e}", "ERROR")

# ========================= WEBSOCKET =========================
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, on_message_cb, is_user_stream=False):
        super().__init__(
            url,
            on_open=self._on_open,
            on_message=on_message_cb,
            on_error=self._on_error,
            on_close=self._on_close,
            on_pong=self._on_pong
        )
        self.is_user_stream = is_user_stream
        self.last_pong = time.time()
        self.heartbeat_thread = None
        self.reconnect_delay = 1
        self.max_delay = 60

    def _on_open(self, ws):
        global ws_connected, user_ws_connected
        if self.is_user_stream:
            user_ws_connected = True
            log("User stream CONNECTED")
        else:
            ws_connected = True
            log("Market stream CONNECTED")
        self.last_pong = time.time()
        if not self.heartbeat_thread:
            self.heartbeat_thread = threading.Thread(target=self._send_heartbeat, daemon=True)
            self.heartbeat_thread.start()

    def _on_error(self, ws, err):
        log(f"WS ERROR ({'user' if self.is_user_stream else 'market'}): {err}", "ERROR")
        self.close()

    def _on_close(self, ws, code, reason):
        log(f"WS CLOSED ({'user' if self.is_user_stream else 'market'}) code={code} reason={reason}", "WARNING" if code in (1000, 1001) else "ERROR")
        global ws_connected, user_ws_connected, ob_active
        if self.is_user_stream:
            user_ws_connected = False
        else:
            ws_connected = False
            ob_active = False

    def _on_pong(self, ws, *args):
        self.last_pong = time.time()

    def _send_heartbeat(self):
        while self.sock and self.sock.connected and not st.session_state.shutdown:
            if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 10:
                self.close()
                break
            try:
                self.send("ping", opcode=websocket.ABNF.OPCODE_PING)
            except:
                break
            time.sleep(HEARTBEAT_INTERVAL)

    def run_forever(self):
        while not st.session_state.shutdown:
            try:
                super().run_forever(ping_interval=None, ping_timeout=None)
            except Exception as e:
                log(f"WS CRASH ({'user' if self.is_user_stream else 'market'}): {e}", "ERROR")
            if st.session_state.shutdown:
                break
            delay = min(self.max_delay, self.reconnect_delay)
            time.sleep(delay)
            self.reconnect_delay = min(self.max_delay, self.reconnect_delay * 2)

def on_market_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get('stream', '')
        payload = data.get('data', {})
        if not payload or not stream: return
        sym = stream.split('@')[0].upper()
        if stream.endswith('@ticker'):
            price = Decimal(str(payload.get('c', '0')))
            if price > ZERO:
                with state_lock:
                    bid_volume[sym] = price
        elif stream.endswith('@depth20@100ms'):
            global ob_active
            bids = payload.get('bids', [])
            asks = payload.get('asks', [])
            if bids and asks:
                with ob_lock:
                    best_bid[sym] = Decimal(str(bids[0][0]))
                    best_ask[sym] = Decimal(str(asks[0][0]))
                ob_active = True
    except Exception as e:
        log(f"on_market_message error: {e}", "ERROR")

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data['e'] == 'executionReport' and data['X'] in ['FILLED', 'PARTIALLY_FILLED']:
            log(f"Order filled for {data['s']}: {data['S']} {data['q']} @ {data['p']}")
            rotate_grids()
    except Exception as e:
        log(f"on_user_message error: {e}", "ERROR")

def start_market_websocket():
    global ob_active
    for ws in ws_instances:
        try: ws.close()
        except: pass
    ws_instances.clear()
    best_bid.clear()
    best_ask.clear()
    ob_active = False
    symbols = [s.lower() for s in all_symbols if s.endswith('USDT')]
    if not symbols:
        log("No symbols for market WS", "WARNING")
        return
    streams = [f"{s}@ticker" for s in symbols] + [f"{s}@depth20@100ms" for s in symbols]
    chunks = [streams[i:i+MAX_STREAMS_PER_CONNECTION] for i in range(0, len(streams), MAX_STREAMS_PER_CONNECTION)]
    for chunk in chunks:
        url = f"{WS_BASE}{'/'.join(chunk)}"
        ws = HeartbeatWebSocket(url, on_message_cb=on_market_message)
        ws_instances.append(ws)
        threading.Thread(target=ws.run_forever, daemon=True).start()
        time.sleep(0.4)
    log(f"Market WS started ({len(chunks)} connections)")

def start_user_stream():
    global user_ws, listen_key
    try:
        resp = client.stream_get_listen_key()
        if not isinstance(resp, dict) or 'listenKey' not in resp:
            log("Invalid listenKey response", "ERROR")
            return
        with listen_key_lock:
            listen_key = resp['listenKey']
        url = f"{USER_STREAM_BASE}{listen_key}"
        user_ws = HeartbeatWebSocket(url, on_message_cb=on_user_message, is_user_stream=True)
        threading.Thread(target=user_ws.run_forever, daemon=True).start()
        log("User stream STARTED")
    except Exception as e:
        log(f"User stream init failed: {e}", "ERROR")

def keepalive_user_stream():
    while not st.session_state.shutdown:
        time.sleep(KEEPALIVE_INTERVAL)
        try:
            with listen_key_lock:
                if listen_key:
                    client.stream_keepalive(listen_key)
        except Exception as e:
            log(f"Keep-alive error: {e}", "ERROR")

def get_mid_price(symbol):
    if ob_active:
        with ob_lock:
            b = best_bid.get(symbol)
            a = best_ask.get(symbol)
        if b and a and b > ZERO and a > ZERO:
            return (b + a) / 2
    return bid_volume.get(symbol, ZERO)

# ========================= AUTO-CYCLE LOOP (REPLACED) =========================
def auto_rotate_loop():
    """Force 5-min startup → 5-min sleep → repeat"""
    while not st.session_state.shutdown:
        # === 5-MIN STARTUP PHASE ===
        log("=== STARTUP PHASE STARTED (5 min) ===")
        st.session_state.bot_running = True
        global bot_start_time
        bot_start_time = time.time()

        if not ws_connected:
            start_market_websocket()
        if not user_ws_connected:
            start_user_stream()

        rotate_grids()

        start = time.time()
        while (time.time() - start) < STARTUP_PERIOD_SECONDS:
            if st.session_state.shutdown:
                break
            time.sleep(30)
            if in_startup_period():
                rotate_grids()

        log("=== STARTUP PHASE ENDED ===")

        # === 5-MIN SLEEP PHASE ===
        log("=== SLEEP PHASE STARTED (5 min) ===")
        st.session_state.bot_running = False
        for ws in ws_instances:
            try: ws.close()
            except: pass
        ws_instances.clear()
        if user_ws:
            try: user_ws.close()
            except: pass
        global ob_active
        ob_active = False

        sleep_start = time.time()
        while (time.time() - sleep_start) < SLEEP_PERIOD_SECONDS:
            if st.session_state.shutdown:
                break
            remaining = int(SLEEP_PERIOD_SECONDS - (time.time() - sleep_start))
            with st.empty().container():
                st.markdown(f"<h2 style='color:orange; text-align:center;'>SLEEPING: {remaining}s</h2>", unsafe_allow_html=True)
            time.sleep(1)
        log("=== SLEEP PHASE ENDED ===")

# ========================= MAIN (START BLOCK REPLACED) =========================
def main():
    for k, v in {
        'bot_running': False, 'logs': [], 'shutdown': False, 'blacklisted': {},
        'gridded_symbols': [], 'total_positions': 0, 'last_regrid_str': "Never",
        'error_count': 0, 'last_error': None
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

    st.set_page_config(page_title="Auto-Cycle Grid Bot", layout="wide")
    st.title("INFINITY GRID BOT — AUTO-CYCLE")

    if not os.getenv('BINANCE_API_KEY'):
        st.error("Set BINANCE_API_KEY and BINANCE_API_SECRET")
        st.stop()

    global client
    client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'), tld='us')

    # AUTO-CYCLE START
    if 'auto_cycle_started' not in st.session_state:
        st.session_state.auto_cycle_started = True
        info = client.get_exchange_info()['symbols']
        global all_symbols
        all_symbols = [s['symbol'] for s in info if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        load_symbol_info()
        update_balances()
        threading.Thread(target=auto_rotate_loop, daemon=True).start()
        threading.Thread(target=keepalive_user_stream, daemon=True).start()
        log("AUTO-CYCLE STARTED: 5min run → 5min sleep")
        send_whatsapp("BOT AUTO-CYCLE STARTED")

    update_balances()
    usdt = account_balances.get('USDT', ZERO)

    if in_startup_period():
        remaining = int(STARTUP_PERIOD_SECONDS - (time.time() - bot_start_time))
        st.success(f"STARTUP PHASE: {remaining}s remaining")
    else:
        st.error("SLEEP PHASE")

    c1, c2, c3, c4 = st.columns(4)
    with c1: st.metric("USDT", f"${usdt:.2f}")
    with c2: st.metric("Active", len(gridded_symbols))
    with c3: st.metric("Status", "RUN" if in_startup_period() else "SLEEP")
    with c4:
        ob_status = "ON" if ob_active else "OFF"
        st.markdown(f"<small>OB: {ob_status}</small>", unsafe_allow_html=True)

    st.code("\n".join(st.session_state.logs[-10:]))
    time.sleep(1)
    st.rerun()

if __name__ == "__main__":
    main()
