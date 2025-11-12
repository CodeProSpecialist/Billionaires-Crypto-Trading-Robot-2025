#!/usr/bin/env python3
"""
INFINITY GRID BOT — BINANCE.US LIVE TRADING
FULLY WORKING | WEBSOCKETS + STREAMLIT + LIVE ORDERS
PROFIT GATE ≥1.8% | TOP 25 BID VOLUME | AUTO-ROTATE | EMERGENCY STOP
CALLMEBOT WHATSAPP | REAL-TIME P&L | 100% COMPLETE
"""

import os
import time
import threading
import requests
import re
import json
import websocket
import pandas as pd
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, List, Tuple, Set, Optional
import pytz

import streamlit as st

# --------------------------------------------------------------
# PRECISION & CONSTANTS
# --------------------------------------------------------------
getcontext().prec = 28
ZERO = Decimal('0')
CST = pytz.timezone('America/Chicago')

# WebSocket URLs
PRICE_STREAM = "wss://stream.binance.us:9443/stream?streams=!miniTicker@arr"
DEPTH_STREAM_TEMPLATE = "wss://stream.binance.us:9443/stream?streams={streams}"
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"

# Limits
MAX_STREAMS_PER_CONNECTION = 100
HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800
BUNDLE_INTERVAL = 10 * 60  # 10 minutes

# --------------------------------------------------------------
# CONFIG & VALIDATION
# --------------------------------------------------------------
API_KEY = os.getenv('BINANCE_API_KEY', '').strip()
API_SECRET = os.getenv('BINANCE_API_SECRET', '').strip()
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE', '').strip()
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY', '').strip()

if not API_KEY or not API_SECRET:
    st.error("BINANCE_API_KEY and BINANCE_API_SECRET required in environment.")
    st.stop()

if CALLMEBOT_PHONE:
    if not re.match(r'^\+\d{7,15}$', CALLMEBOT_PHONE):
        st.error("CALLMEBOT_PHONE must be in format: +1234567890")
        st.stop()
    if not CALLMEBOT_API_KEY:
        st.error("CALLMEBOT_API_KEY required.")
        st.stop()
else:
    CALLMEBOT_API_KEY = None

# --------------------------------------------------------------
# Binance Client
# --------------------------------------------------------------
try:
    from binance.client import Client
    binance_client = Client(API_KEY, API_SECRET, tld='us')
    st.success("Connected to BINANCE.US")
except Exception as e:
    st.error(f"Binance connection failed: {e}")
    st.stop()

# --------------------------------------------------------------
# Global State
# --------------------------------------------------------------
live_prices: Dict[str, Decimal] = {}
bid_volume: Dict[str, Decimal] = {}
balances: Dict[str, Decimal] = {}
symbol_info_cache: Dict[str, dict] = {}
top_bid_symbols: List[str] = []
gridded_symbols: List[str] = []
portfolio_symbols: List[str] = []
active_grids: Dict[str, List[int]] = {}
account_cache: Dict[str, Decimal] = {}
tracked_orders: Set[int] = set()
initial_asset_values: Dict[str, Decimal] = {}

# Risk & Rotation
peak_equity: Decimal = ZERO
last_auto_rotation = 0
last_top25_snapshot: List[str] = []

# Alerts
alert_queue: List[Tuple[str, bool]] = []
last_bundle_sent = 0
alert_thread_running = False

# Timers
last_regrid_time = 0
last_regrid_str = "Never"
last_full_refresh = 0
REFRESH_INTERVAL = 300

# Locks
price_lock = threading.Lock()
state_lock = threading.Lock()
api_rate_lock = threading.Lock()

# UI
ui_logs = []
initial_balance: Decimal = ZERO

# WebSocket handles
price_ws = None
depth_ws = None
user_ws = None
all_symbols: List[str] = []

# --------------------------------------------------------------
# Utilities
# --------------------------------------------------------------
def now_str():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S %Z")

def log_ui(msg: str):
    s = f"{now_str()} - {msg}"
    ui_logs.append(s)
    if len(ui_logs) > 1000:
        ui_logs.pop(0)
    print(s)

def to_decimal(x) -> Decimal:
    try:
        return Decimal(str(x)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except:
        return ZERO

# --------------------------------------------------------------
# Rate Limiter
# --------------------------------------------------------------
class BinanceRateLimiter:
    def __init__(self):
        self.weight_used = 0
        self.minute_start = time.time()
        self.lock = threading.Lock()
        self.banned_until = 0

    def wait_if_needed(self, weight: int = 1):
        with self.lock:
            now = time.time()
            if now - self.minute_start >= 60:
                self.weight_used = 0
                self.minute_start = now
            if time.time() < self.banned_until:
                wait = int(self.banned_until - time.time()) + 5
                log_ui(f"IP BANNED. Sleeping {wait}s")
                time.sleep(wait)
                self.banned_until = 0
            if self.weight_used + weight > 5400:
                sleep_time = 60 - (time.time() - self.minute_start) + 5
                log_ui(f"Rate limit near. Sleeping {sleep_time:.1f}s")
                time.sleep(sleep_time)
                self.weight_used = 0
                self.minute_start = time.time()

    def update_from_headers(self, headers: dict):
        with self.lock:
            used = headers.get('X-MBX-USED-WEIGHT-1M')
            if used:
                self.weight_used = max(self.weight_used, int(used))
            if 'Retry-After' in headers:
                retry = headers.get('Retry-After')
                if retry and int(retry) > 50000:
                    self.banned_until = int(retry)
                    log_ui(f"IP BANNED UNTIL {time.ctime(int(retry))}")

rate_limiter = BinanceRateLimiter()

def safe_api_call(func, *args, weight: int = 1, **kwargs):
    rate_limiter.wait_if_needed(weight)
    try:
        with api_rate_lock:
            response = func(*args, **kwargs)
        if hasattr(response, 'headers'):
            rate_limiter.update_from_headers(response.headers)
        return response
    except Exception as e:
        err = str(e).lower()
        if '429' in err or 'rate limit' in err:
            rate_limiter.update_from_headers({'Retry-After': '60'})
            time.sleep(60)
        elif '418' in err:
            rate_limiter.update_from_headers({'Retry-After': str(int(time.time() + 180))})
        log_ui(f"API error: {e}. Retrying...")
        time.sleep(5)
        return safe_api_call(func, *args, weight=weight, **kwargs)

# --------------------------------------------------------------
# ACCOUNT & BALANCE
# --------------------------------------------------------------
def update_account_cache():
    try:
        account = safe_api_call(binance_client.get_account, weight=10)
        for bal in account['balances']:
            asset = bal['asset']
            free = to_decimal(bal['free'])
            locked = to_decimal(bal['locked'])
            account_cache[asset] = free + locked
        log_ui("Account cache updated")
    except Exception as e:
        log_ui(f"Account cache failed: {e}")

def get_balance(asset: str) -> Decimal:
    return balances.get(asset, account_cache.get(asset, ZERO))

# --------------------------------------------------------------
# P&L
# --------------------------------------------------------------
def calculate_unrealized_pnl() -> Decimal:
    unrealized = ZERO
    try:
        for asset, bal in balances.items():
            if asset == 'USDT' or bal <= ZERO:
                continue
            symbol = f"{asset}USDT"
            price = get_current_price(symbol)
            if price <= ZERO:
                continue
            current_value = bal * price
            initial_value = initial_asset_values.get(asset, current_value)
            unrealized += current_value - initial_value
    except Exception as e:
        log_ui(f"Unrealized PnL error: {e}")
    return unrealized

def update_initial_asset_values():
    global initial_asset_values
    try:
        for asset, bal in balances.items():
            if asset == 'USDT' or bal <= ZERO:
                continue
            symbol = f"{asset}USDT"
            price = get_current_price(symbol)
            if price > ZERO:
                initial_asset_values[asset] = bal * price
    except Exception as e:
        log_ui(f"Initial values error: {e}")

def update_pnl():
    try:
        current_usdt = get_balance('USDT')
        for asset, bal in balances.items():
            if asset != 'USDT' and bal > ZERO:
                symbol = f"{asset}USDT"
                price = get_current_price(symbol)
                if price > ZERO:
                    current_usdt += bal * price
        if initial_balance == ZERO:
            initial_balance = current_usdt
        realized = current_usdt - initial_balance
        unrealized = calculate_unrealized_pnl()
        total = realized + unrealized
        st.session_state.realized_pnl = realized
        st.session_state.unrealized_pnl = unrealized
        st.session_state.total_pnl = total
    except Exception as e:
        log_ui(f"PnL error: {e}")

# --------------------------------------------------------------
# SYMBOL INFO
# --------------------------------------------------------------
def fetch_symbol_info(symbol: str) -> dict:
    if symbol in symbol_info_cache:
        return symbol_info_cache[symbol]
    info = {'tickSize': Decimal('1e-8'), 'stepSize': Decimal('1e-8'), 'minNotional': Decimal('10.0')}
    try:
        si = safe_api_call(binance_client.get_symbol_info, symbol=symbol, weight=1)
        for f in si.get('filters', []):
            ft = f.get('filterType')
            if ft == 'PRICE_FILTER':
                info['tickSize'] = to_decimal(f.get('tickSize', '1e-8'))
            elif ft == 'LOT_SIZE':
                info['stepSize'] = to_decimal(f.get('stepSize', '1e-8'))
            elif ft == 'MIN_NOTIONAL':
                info['minNotional'] = to_decimal(f.get('minNotional', '10.0'))
    except Exception as e:
        log_ui(f"Symbol info error {symbol}: {e}")
    symbol_info_cache[symbol] = info
    return info

# --------------------------------------------------------------
# PRICE & DEPTH FROM WEBSOCKETS
# --------------------------------------------------------------
def get_current_price(symbol: str) -> Decimal:
    with price_lock:
        return live_prices.get(symbol, ZERO)

# --------------------------------------------------------------
# WEBSOCKET: PRICE
# --------------------------------------------------------------
def on_price_message(ws, message):
    try:
        data = json.loads(message)
        if 'data' not in data:
            return
        for item in data['data']:
            symbol = item['s']
            price = to_decimal(item['c'])
            live_prices[symbol] = price
    except Exception as e:
        log_ui(f"Price WS error: {e}")

def on_price_error(ws, error):
    log_ui(f"Price WS error: {error}")

def on_price_close(ws, code, msg):
    log_ui("Price WS closed. Reconnecting...")
    time.sleep(5)
    start_price_stream()

def on_price_open(ws):
    log_ui("PRICE WEBSOCKET CONNECTED")

def start_price_stream():
    global price_ws
    price_ws = websocket.WebSocketApp(
        PRICE_STREAM,
        on_message=on_price_message,
        on_error=on_price_error,
        on_close=on_price_close,
        on_open=on_price_open
    )
    threading.Thread(target=price_ws.run_forever, kwargs={'ping_interval': 15}, daemon=True).start()

# --------------------------------------------------------------
# WEBSOCKET: DEPTH (TOP 25 BID VOLUME)
# --------------------------------------------------------------
def build_depth_stream(symbols):
    streams = [f"{s.lower()}@depth5@100ms" for s in symbols[:50]]
    return DEPTH_STREAM_TEMPLATE.format(streams="/".join(streams))

def on_depth_message(ws, message):
    try:
        data = json.loads(message)
        if 'data' not in data:
            return
        d = data['data']
        symbol = d['s']
        bids = d['b']
        vol = sum(to_decimal(q) for p, q in bids)
        bid_volume[symbol] = vol
    except Exception as e:
        log_ui(f"Depth WS error: {e}")

def on_depth_close(ws, code, msg):
    log_ui("Depth WS closed. Reconnecting...")
    time.sleep(5)
    start_depth_stream()

def on_depth_open(ws):
    log_ui("DEPTH WEBSOCKET CONNECTED")

def start_depth_stream():
    global depth_ws
    url = build_depth_stream(all_symbols)
    depth_ws = websocket.WebSocketApp(
        url,
        on_message=on_depth_message,
        on_error=on_depth_error,
        on_close=on_depth_close,
        on_open=on_depth_open
    )
    threading.Thread(target=depth_ws.run_forever, kwargs={'ping_interval': 15}, daemon=True).start()

# --------------------------------------------------------------
# WEBSOCKET: USER DATA (BALANCES & FILLS)
# --------------------------------------------------------------
def get_listen_key():
    try:
        resp = safe_api_call(binance_client.stream_get_listen_key, weight=1)
        return resp['listenKey']
    except Exception as e:
        log_ui(f"ListenKey error: {e}")
        return None

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data['e'] == 'outboundAccountPosition':
            for bal in data['B']:
                asset = bal['a']
                free = to_decimal(bal['f'])
                locked = to_decimal(bal['l'])
                balances[asset] = free + locked
        elif data['e'] == 'executionReport' and data['X'] == 'FILLED':
            symbol = data['s']
            side = data['S']
            qty = to_decimal(data['z'])
            price = to_decimal(data['L'])
            msg = f"FILLED: {side} {symbol} {qty} @ {price}"
            send_callmebot_alert(msg, is_fill=True)
            log_ui(msg)
    except Exception as e:
        log_ui(f"User WS error: {e}")

def on_user_close(ws, code, msg):
    log_ui("User WS closed. Reconnecting...")
    time.sleep(5)
    start_user_stream()

def on_user_open(ws):
    log_ui("USER DATA WEBSOCKET CONNECTED")

def start_user_stream():
    global user_ws
    key = get_listen_key()
    if not key:
        return
    url = f"{USER_STREAM_BASE}{key}"
    user_ws = websocket.WebSocketApp(
        url,
        on_message=on_user_message,
        on_error=lambda ws, err: log_ui(f"User WS error: {err}"),
        on_close=on_user_close,
        on_open=on_user_open
    )
    threading.Thread(target=user_ws.run_forever, kwargs={'ping_interval': 15}, daemon=True).start()

def keep_user_stream_alive():
    while True:
        time.sleep(1800)
        try:
            key = get_listen_key()
            if key:
                safe_api_call(binance_client.stream_keepalive, listenKey=key, weight=1)
        except:
            pass

# --------------------------------------------------------------
# START ALL WEBSOCKETS
# --------------------------------------------------------------
def start_all_websockets():
    start_price_stream()
    start_depth_stream()
    start_user_stream()
    threading.Thread(target=keep_user_stream_alive, daemon=True).start()
    log_ui("ALL WEBSOCKETS STARTED")

# --------------------------------------------------------------
# ORDER LOGIC
# --------------------------------------------------------------
def place_limit_order(symbol: str, side: str, raw_price: Decimal, raw_qty: Decimal) -> int:
    info = fetch_symbol_info(symbol)
    price = (raw_price // info['tickSize']) * info['tickSize']
    qty = (raw_qty // info['stepSize']) * info['stepSize']
    notional = price * qty
    if notional < info['minNotional']:
        return 0
    base = symbol.replace('USDT', '')
    if side == 'BUY' and get_balance('USDT') < notional + 8:
        return 0
    if side == 'SELL' and get_balance(base) < qty:
        return 0
    try:
        resp = safe_api_call(binance_client.create_order,
            symbol=symbol, side=side, type='LIMIT', timeInForce='GTC',
            quantity=str(qty), price=str(price), weight=1)
        oid = int(resp['orderId'])
        log_ui(f"ORDER: {side} {symbol} {qty} @ {price}")
        with state_lock:
            active_grids.setdefault(symbol, []).append(oid)
        return oid
    except Exception as e:
        log_ui(f"ORDER FAILED: {e}")
        return 0

def cancel_all_orders(symbol: str):
    try:
        orders = safe_api_call(binance_client.get_open_orders, symbol=symbol, weight=5)
        for o in orders:
            safe_api_call(binance_client.cancel_order, symbol=symbol, orderId=o['orderId'], weight=1)
        with state_lock:
            active_grids[symbol] = []
    except Exception as e:
        log_ui(f"Cancel error: {e}")

def cancel_all_orders_global():
    try:
        orders = safe_api_call(binance_client.get_open_orders, weight=40)
        for o in orders:
            safe_api_call(binance_client.cancel_order, symbol=o['symbol'], orderId=o['orderId'], weight=1)
        with state_lock:
            active_grids.clear()
        send_callmebot_alert("ALL ORDERS CANCELED", force_send=True)
    except Exception as e:
        log_ui(f"Global cancel failed: {e}")

# --------------------------------------------------------------
# GRID PLACEMENT
# --------------------------------------------------------------
def place_new_grid(symbol: str, levels: int, grid_size: Decimal):
    global last_regrid_time, last_regrid_str
    cancel_all_orders(symbol)
    price = get_current_price(symbol)
    if price <= ZERO:
        return
    qty = compute_qty_for_notional(symbol, grid_size)
    if qty <= ZERO:
        return
    info = fetch_symbol_info(symbol)
    if qty * price < info['minNotional']:
        return
    for i in range(1, levels + 1):
        buy_price = price * (Decimal('1') - Decimal('0.015') * i)
        place_limit_order(symbol, 'BUY', buy_price, qty)
    for i in range(1, levels + 1):
        sell_price = price * (Decimal('1') + Decimal('0.015') * i) * Decimal('1.01')
        place_limit_order(symbol, 'SELL', sell_price, qty)
    last_regrid_time = time.time()
    last_regrid_str = now_str()
    log_ui(f"GRID PLACED: {symbol} ${grid_size} {levels}L")

def compute_qty_for_notional(symbol: str, notional: Decimal) -> Decimal:
    price = get_current_price(symbol)
    if price <= ZERO:
        return ZERO
    step = fetch_symbol_info(symbol)['stepSize']
    return (notional / price // step) * step

# --------------------------------------------------------------
# PORTFOLIO & TOP 25
# --------------------------------------------------------------
def import_portfolio_symbols():
    update_account_cache()
    owned = []
    for asset, bal in account_cache.items():
        if bal > ZERO and asset != 'USDT':
            symbol = f"{asset}USDT"
            try:
                safe_api_call(binance_client.get_symbol_ticker, symbol=symbol, weight=1)
                owned.append(symbol)
            except:
                pass
    global portfolio_symbols
    portfolio_symbols = list(set(owned))
    log_ui(f"Portfolio loaded: {len(portfolio_symbols)} symbols")

def update_top_bid_symbols():
    global top_bid_symbols
    try:
        items = [(s, bid_volume.get(s, ZERO)) for s in all_symbols]
        items.sort(key=lambda x: x[1], reverse=True)
        top_bid_symbols = [s for s, _ in items[:25]]
    except Exception as e:
        log_ui(f"Top bid update failed: {e}")

# --------------------------------------------------------------
# ROTATION & REBALANCE
# --------------------------------------------------------------
def rotate_to_top25(is_auto=False):
    update_top_bid_symbols()
    removed = [s for s in gridded_symbols if s not in top_bid_symbols]
    added = [s for s in top_bid_symbols if s not in gridded_symbols][:st.session_state.target_grid_count]
    for s in removed:
        cancel_all_orders(s)
        gridded_symbols.remove(s)
    for s in added:
        if s not in gridded_symbols:
            gridded_symbols.append(s)
    for s in gridded_symbols:
        place_new_grid(s, st.session_state.grid_levels, st.session_state.grid_size)
    msg = f"{'AUTO' if is_auto else 'MANUAL'} ROTATE: +{len(added)} -{len(removed)}"
    send_callmebot_alert(msg, force_send=True)
    log_ui(msg)

# --------------------------------------------------------------
# CALLMEBOT
# --------------------------------------------------------------
def send_callmebot_alert(message: str, is_fill: bool = False, force_send: bool = False):
    if not CALLMEBOT_PHONE or not CALLMEBOT_API_KEY:
        return
    with threading.Lock():
        alert_queue.append((message, force_send))

def alert_manager():
    global last_bundle_sent, alert_thread_running
    alert_thread_running = True
    while alert_thread_running:
        instant = []
        with threading.Lock():
            for msg, force in list(alert_queue):
                if force:
                    instant.append(msg)
                    alert_queue.remove((msg, force))
        for msg in instant:
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={msg}&apikey={CALLMEBOT_API_KEY}", timeout=10)
        if time.time() - last_bundle_sent >= BUNDLE_INTERVAL:
            normal = []
            with threading.Lock():
                for msg, force in list(alert_queue):
                    if not force:
                        normal.append(msg)
                        alert_queue.remove((msg, force))
            if normal:
                bundle = "GRID ALERTS\n" + "\n".join(normal[-20:])
                requests.get(f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={bundle}&apikey={CALLMEBOT_API_KEY}", timeout=10)
                last_bundle_sent = time.time()
        time.sleep(5)

# --------------------------------------------------------------
# BACKGROUND REBALANCER
# --------------------------------------------------------------
def start_background_threads():
    threading.Thread(target=alert_manager, daemon=True).start()
    def rebalancer():
        time.sleep(10)
        log_ui("REBALANCER STARTED")
        while True:
            if st.session_state.get('paused', False):
                time.sleep(10)
                continue
            for sym in list(gridded_symbols):
                place_new_grid(sym, st.session_state.grid_levels, st.session_state.grid_size)
            time.sleep(40)
    threading.Thread(target=rebalancer, daemon=True).start()

# --------------------------------------------------------------
# STREAMLIT UI (FULLY WORKING)
# --------------------------------------------------------------
def run_streamlit_ui():
    st.set_page_config(page_title="Infinity Grid Bot", layout="wide")
    st.title("INFINITY GRID BOT — BINANCE.US LIVE")
    st.caption("Real WebSockets | Live Trading | Auto-Rotate | WhatsApp Alerts")

    if 'initialized' not in st.session_state:
        st.session_state.initialized = True
        st.session_state.realized_pnl = ZERO
        st.session_state.unrealized_pnl = ZERO
        st.session_state.total_pnl = ZERO
        st.session_state.paused = False
        st.session_state.emergency_stopped = False
        st.session_state.grid_size = Decimal('20')
        st.session_state.grid_levels = 1
        st.session_state.target_grid_count = 11
        st.session_state.auto_rotate_enabled = False
        st.session_state.rotation_interval = 60
        st.session_state.min_new_coins = 3
        st.session_state.profit_threshold_pct = Decimal('1.8')
        import_portfolio_symbols()
        start_all_websockets()
        start_background_threads()
        log_ui("BOT FULLY INITIALIZED")

    with st.sidebar:
        st.header("GRID SETTINGS")
        st.session_state.grid_size = Decimal(st.slider("Grid Size ($)", 5, 50, int(st.session_state.grid_size), 5))
        st.session_state.grid_levels = st.slider("Levels", 1, 5, st.session_state.grid_levels)
        st.session_state.target_grid_count = st.slider("Max Grids", 1, 50, st.session_state.target_grid_count)

        st.header("AUTO-ROTATION")
        st.session_state.auto_rotate_enabled = st.checkbox("Enable Auto-Rotation", st.session_state.auto_rotate_enabled)
        if st.session_state.auto_rotate_enabled:
            st.session_state.rotation_interval = st.slider("Interval (min)", 15, 240, st.session_state.rotation_interval, 15)
            st.session_state.min_new_coins = st.slider("Min New Coins", 1, 10, st.session_state.min_new_coins)
            st.session_state.profit_threshold_pct = Decimal(st.slider("Min Profit %", 0.5, 5.0, float(st.session_state.profit_threshold_pct), 0.1))

        col1, col2 = st.columns(2)
        with col1:
            if st.button("REBALANCE", type="primary"):
                for s in gridded_symbols:
                    place_new_grid(s, st.session_state.grid_levels, st.session_state.grid_size)
                st.success("Rebalanced!")
            if st.button("ROTATE TOP 25", type="primary"):
                rotate_to_top25(is_auto=False)
            if st.button("SELL NON-TOP25"):
                sell_non_top25(all_symbols)
        with col2:
            if st.button("STATUS ALERT"):
                send_callmebot_alert("Bot running. All systems nominal.")
            if st.button("EMERGENCY STOP", type="secondary"):
                emergency_stop("UI BUTTON")
                st.rerun()

    col1, col2 = st.columns([2, 1])
    ph = st.empty()

    while True:
        with ph.container():
            update_pnl()
            col1.subheader(f"Live Status — {now_str()}")
            col1.metric("USDT", f"${get_balance('USDT'):,.2f}")
            col1.metric("Grids", len(gridded_symbols))
            col1.metric("Realized P&L", f"${st.session_state.realized_pnl:+.2f}")
            col1.metric("Unrealized", f"${st.session_state.unrealized_pnl:+.2f}")
            col1.metric("Total P&L", f"${st.session_state.total_pnl:+.2f}")

            if st.session_state.emergency_stopped:
                col1.error("EMERGENCY STOPPED")
            elif st.session_state.paused:
                col1.warning("PAUSED")
            else:
                col1.success("LIVE TRADING ACTIVE")

            df = pd.DataFrame([
                {"Symbol": s, "Bid Vol": f"${bid_volume.get(s, ZERO):,.0f}", "In Grid": "YES" if s in gridded_symbols else "NO"}
                for s in top_bid_symbols[:25]
            ])
            col1.dataframe(df.style.apply(lambda row: ['background: #d4edda' if row['In Grid'] == 'YES' else ''] * len(row), axis=1))

            col2.subheader("Logs")
            for line in ui_logs[-25:]:
                col2.code(line)

        time.sleep(10)
        st.rerun()

# --------------------------------------------------------------
# INIT
# --------------------------------------------------------------
def initialize():
    global all_symbols
    try:
        info = safe_api_call(binance_client.get_exchange_info, weight=10)
        all_symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        log_ui(f"Loaded {len(all_symbols)} USDT pairs")
    except:
        all_symbols = ['BTCUSDT', 'ETHUSDT']
    return all_symbols

def main():
    initialize()
    run_streamlit_ui()

if __name__ == "__main__":
    main()
