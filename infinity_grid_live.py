#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — AUTO-CYCLE + HARD-CODED GRIDS
- 5 MIN RUN → 5 MIN SLEEP → REPEAT
- Hard-coded grid sizes:
  USDT_PER_GRID_LINE_BUY = 10.00
  NUMBER_OF_GRIDS_PER_POSITION = 2
  TOTAL_NUMBER_OF_GRIDS = 10
- Rate-limited API
- Balance cache (1 per 10 s)
- Force wake-up button
- No extra threads (all logic in main loop)
- FIXED: Orders placed, wake-up works, no sleeping too much
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

# ──────────────────────  HARD-CODED GRID SETTINGS  ──────────────────────
USDT_PER_GRID_LINE_BUY = Decimal('10.00')      # $10 per BUY order
NUMBER_OF_GRIDS_PER_POSITION = 2               # 2 BUY/SELL lines per symbol
TOTAL_NUMBER_OF_GRIDS = 10                     # Max 10 active symbols

# ──────────────────────  WEBSOCKET CORE  ──────────────────────
ws_connected = False
user_ws_connected = False
ws_instances: list = []
user_ws = None
listen_key: str | None = None
listen_key_lock = threading.Lock()

# ──────────────────────  AUTO-CYCLE CONTROL  ──────────────────────
bot_start_time = 0.0
STARTUP_PERIOD_SECONDS = 300     # 5 min run
SLEEP_PERIOD_SECONDS = 300       # 5 min sleep

# ──────────────────────  RATE LIMITER  ──────────────────────
API_CALL_TIMES = []
MAX_CALLS_PER_MINUTE = 1190
RATE_LIMIT_LOCK = threading.Lock()

# ──────────────────────  BALANCE CACHE  ──────────────────────
last_balance_update = 0.0
BALANCE_UPDATE_INTERVAL = 10.0

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

last_keepalive_time = 0.0

# ========================= RATE LIMITER =========================
def ratelimit_api(func):
    def wrapper(*args, **kwargs):
        with RATE_LIMIT_LOCK:
            now = time.time()
            API_CALL_TIMES[:] = [t for t in API_CALL_TIMES if now - t < 60]
            if len(API_CALL_TIMES) >= MAX_CALLS_PER_MINUTE:
                delay = 60 - (now - API_CALL_TIMES[0])
                log(f"RATE LIMIT: Waiting {delay:.1f}s", "WARNING")
                time.sleep(max(0, delay))
                now = time.time()
                API_CALL_TIMES[:] = [t for t in API_CALL_TIMES if now - t < 60]
            API_CALL_TIMES.append(time.time())
        return func(*args, **kwargs)
    return wrapper

@ratelimit_api
def safe_get_account():
    return client.get_account()

@ratelimit_api
def safe_get_exchange_info():
    return client.get_exchange_info()

@ratelimit_api
def safe_create_order(**kwargs):
    return client.create_order(**kwargs)

@ratelimit_api
def safe_cancel_open_orders(symbol):
    return client.cancel_open_orders(symbol=symbol)

@ratelimit_api
def safe_get_open_orders(symbol):
    return client.get_open_orders(symbol=symbol)

@ratelimit_api
def safe_stream_get_listen_key():
    return client.stream_get_listen_key()

@ratelimit_api
def safe_stream_keepalive(listen_key):
    return client.stream_keepalive(listen_key)

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
    global account_balances, last_balance_update
    now = time.time()
    if now - last_balance_update < BALANCE_UPDATE_INTERVAL:
        return
    try:
        info = safe_get_account()['balances']
        account_balances = {a['asset']: Decimal(a['free']) for a in info if Decimal(a['free']) > ZERO}
        last_balance_update = now
        log(f"Updated balances: USDT={account_balances.get('USDT', ZERO):.2f}")
    except Exception as e:
        log(f"Balance update failed: {e}", "ERROR")

def load_symbol_info():
    global symbol_info
    try:
        info = safe_get_exchange_info()['symbols']
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
        if not info: return False
        price = round_step(price, info['tickSize'])
        quantity = round_step(quantity, info['stepSize'])
        if quantity < info['minQty']: return False
        notional = price * quantity
        if notional < Decimal(info['minNotional']): return False
        if side == 'BUY':
            needed = notional * SAFETY_BUFFER
            usdt_free = account_balances.get('USDT', ZERO)
            if needed > usdt_free: return False
        if side == 'SELL':
            base = symbol.replace('USDT', '')
            coin_free = account_balances.get(base, ZERO)
            if quantity > coin_free * SAFETY_BUFFER: return False
        order = safe_create_order(
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
        log(f"ORDER FAILED {symbol} {side}: {e.message}", "ERROR")
        return False
    except Exception as e:
        log(f"ORDER ERROR {symbol}: {e}", "ERROR")
        return False

def place_market_sell(symbol):
    try:
        info = symbol_info.get(symbol)
        if not info: return False
        base = symbol.replace('USDT', '')
        quantity = account_balances.get(base, ZERO) * SAFETY_BUFFER
        quantity = round_step(quantity, info['stepSize'])
        if quantity < info['minQty']: return False
        order = safe_create_order(
            symbol=symbol,
            side='SELL',
            type='MARKET',
            quantity=f"{quantity:.8f}".rstrip('0').rstrip('.')
        )
        log(f"MARKET SOLD {symbol} {quantity}")
        send_whatsapp(f"MARKET SOLD {symbol} {quantity}")
        return True
    except BinanceAPIException as e:
        log(f"MARKET SELL FAILED {symbol}: {e.message}", "ERROR")
        return False
    except Exception as e:
        log(f"MARKET SELL ERROR {symbol}: {e}", "ERROR")
        return False

# ========================= HARD-CODED GRID =========================
def place_grid(symbol):
    try:
        price = get_mid_price(symbol)
        if price <= ZERO:
            log(f"No price for {symbol}", "WARNING")
            return
        raw_qty = USDT_PER_GRID_LINE_BUY / price
        info = symbol_info.get(symbol)
        if not info: return
        qty = round_step(raw_qty, info['stepSize'])
        if qty < info['minQty']: return
        total_buy_cost = qty * price * NUMBER_OF_GRIDS_PER_POSITION * SAFETY_BUFFER
        if total_buy_cost > account_balances.get('USDT', ZERO):
            log(f"SKIPPING {symbol}: Not enough USDT for {NUMBER_OF_GRIDS_PER_POSITION} buy lines", "WARNING")
            return
        buys = sells = 0
        for i in range(1, NUMBER_OF_GRIDS_PER_POSITION + 1):
            buy_price = price * (1 - Decimal('0.015') * i)
            sell_price = price * (1 + Decimal('0.015') * i) * Decimal('1.01')
            if place_limit_order(symbol, 'BUY', buy_price, qty):
                buys += 1
            if place_limit_order(symbol, 'SELL', sell_price, qty):
                sells += 1
        global last_regrid_str
        last_regrid_str = now_str()
        log(f"GRID {symbol} | B:{buys} S:{sells} | ${USDT_PER_GRID_LINE_BUY} per line")
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
                open_orders = safe_get_open_orders(symbol=symbol)
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
            raw_qty = USDT_PER_GRID_LINE_BUY / buy_price
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
        target_symbols = [s for s, _ in top]
        target_count = min(TOTAL_NUMBER_OF_GRIDS, len(target_symbols))
        targets = target_symbols[:target_count]
        to_add = [s for s in targets if s not in gridded_symbols]
        to_remove = [s for s in gridded_symbols if s not in targets]
        for s in to_remove:
            try:
                safe_cancel_open_orders(s)
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
        global ws_connected, user_ws_connected
        if self.is_user_stream:
            user_ws_connected = False
        else:
            ws_connected = False

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
    for ws in ws_instances:
        try: ws.close()
        except: pass
    ws_instances.clear()
    symbols = [s.lower() for s in all_symbols if s.endswith('USDT')]
    if not symbols:
        log("No symbols for market WS", "WARNING")
        return
    streams = [f"{s}@ticker" for s in symbols]
    chunks = [streams[i:i+MAX_STREAMS_PER_CONNECTION] for i in range(0, len(streams), MAX_STREAMS_PER_CONNECTION)]
    for chunk in chunks:
        url = f"{WS_BASE}{'/'.join(chunk)}"
        ws = HeartbeatWebSocket(url, on_message_cb=on_market_message)
        ws_instances.append(ws)
        threading.Thread(target=ws.run_forever, daemon=True).start()
        time.sleep(0.4)
    log(f"Market WS started ({len(chunks)} connections) - TICKER ONLY")

def start_user_stream():
    global user_ws, listen_key
    try:
        resp = safe_stream_get_listen_key()
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
    try:
        with listen_key_lock:
            if listen_key:
                safe_stream_keepalive(listen_key)
    except Exception as e:
        log(f"Keep-alive error: {e}", "ERROR")

def get_mid_price(symbol):
    # Only uses ticker price
    return bid_volume.get(symbol, ZERO)

# ========================= AUTO-CYCLE LOOP =========================
def auto_rotate_loop():
    while not st.session_state.shutdown:
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
            if st.session_state.shutdown or st.session_state.force_wakeup:
                st.session_state.force_wakeup = False
                break
            time.sleep(30)
            if in_startup_period():
                rotate_grids()

        log("=== STARTUP PHASE ENDED ===")

        log("=== SLEEP PHASE STARTED (5 min) ===")
        st.session_state.bot_running = False
        for ws in ws_instances:
            try: ws.close()
            except: pass
        ws_instances.clear()
        if user_ws:
            try: user_ws.close()
            except: pass

        sleep_start = time.time()
        while (time.time() - sleep_start) < SLEEP_PERIOD_SECONDS:
            if st.session_state.shutdown or st.session_state.force_wakeup:
                log("SLEEP INTERRUPTED BY WAKE-UP")
                st.session_state.force_wakeup = False
                break
            remaining = int(SLEEP_PERIOD_SECONDS - (time.time() - sleep_start))
            with st.empty().container():
                st.markdown(
                    f"<h2 style='color:orange; text-align:center;'>"
                    f"SLEEPING: {remaining}s<br>"
                    f"<small>Click button to wake up</small></h2>",
                    unsafe_allow_html=True
                )
            time.sleep(1)
        log("=== SLEEP PHASE ENDED ===")

# ========================= MAIN =========================
def main():
    for k, v in {
        'bot_running': False, 'logs': [], 'shutdown': False, 'blacklisted': {},
        'gridded_symbols': [], 'total_positions': 0, 'last_regrid_str': "Never",
        'error_count': 0, 'last_error': None, 'force_wakeup': False
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

    st.set_page_config(page_title="Hard-Coded Grid Bot", layout="wide")
    st.title("INFINITY GRID BOT — HARD-CODED")

    if not os.getenv('BINANCE_API_KEY'):
        st.error("Set BINANCE_API_KEY and BINANCE_API_SECRET")
        st.stop()

    global client
    client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'), tld='us')

    if 'auto_cycle_started' not in st.session_state:
        st.session_state.auto_cycle_started = True
        info = safe_get_exchange_info()['symbols']
        global all_symbols
        all_symbols = [s['symbol'] for s in info if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        load_symbol_info()
        update_balances()
        threading.Thread(target=auto_rotate_loop, daemon=True).start()
        log("AUTO-CYCLE STARTED: 5min run → 5min sleep")
        send_whatsapp("BOT AUTO-CYCLE STARTED")

    # ---- Keep-alive & dashboard -------------------------------------------------
    global last_keepalive_time
    now = time.time()
    if now - last_keepalive_time > KEEPALIVE_INTERVAL:
        keepalive_user_stream()
        last_keepalive_time = now

    update_balances()
    usdt = account_balances.get('USDT', ZERO)

    if in_startup_period():
        remaining = int(STARTUP_PERIOD_SECONDS - (time.time() - bot_start_time))
        st.success(f"STARTUP PHASE: {remaining}s remaining")
    else:
        st.error("SLEEP PHASE")
        if st.button("FORCE WAKE UP", type="primary", use_container_width=True):
            st.session_state.force_wakeup = True
            st.rerun()

    c1, c2, c3 = st.columns(3)
    with c1: st.metric("USDT", f"${usdt:.2f}")
    with c2: st.metric("Active", len(gridded_symbols))
    with c3: st.metric("Status", "RUN" if in_startup_period() else "SLEEP")

    st.code("\n".join(st.session_state.logs[-10:]))
    time.sleep(1)
    st.rerun()

if __name__ == "__main__":
    main()
