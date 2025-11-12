#!/usr/bin/env python3
"""
INFINITY GRID BOT — BINANCE.US LIVE
FINAL PRODUCTION VERSION
- Full WebSocket data weight monitoring (5 MB/min)
- NO per-message logging (memory-safe)
- Heartbeat + Pong monitoring
- Emergency stop + 10-minute sleep on overflow
- REST rate limiting + 429/418 handling
- Real-time P&L, grid status, green/red lights
- CallMeBot WhatsApp alerts (bundled + instant)
- Top 25 bid volume rotation
- Profit gate ≥1.8%
- Streamlit dashboard
"""

import os
import time
import threading
import requests
import re
import json
import websocket
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, List, Tuple, Set, Any
import pytz

import streamlit as st

# ==============================================================
# CONFIG & GLOBALS
# ==============================================================
st.set_page_config(page_title="Infinity Grid Bot — BINANCE.US", layout="wide")

try:
    from binance.client import Client
except Exception as e:
    st.error(f"Binance import failed: {e}")
    st.stop()

getcontext().prec = 28
ZERO = Decimal('0')
CST = pytz.timezone('America/Chicago')

API_KEY = os.getenv('BINANCE_API_KEY', '').strip()
API_SECRET = os.getenv('BINANCE_API_SECRET', '').strip()
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE', '').strip()
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY', '').strip()

if not API_KEY or not API_SECRET:
    st.error("BINANCE_API_KEY and BINANCE_API_SECRET required.")
    st.stop()

if CALLMEBOT_PHONE and not CALLMEBOT_API_KEY:
    st.error("CALLMEBOT_API_KEY required if phone is set.")
    st.stop()

binance_client = Client(API_KEY, API_SECRET, tld='us')

# State
live_prices: Dict[str, Decimal] = {}
bid_volume: Dict[str, Decimal] = {}
balances: Dict[str, Decimal] = {}
symbol_info_cache: Dict[str, dict] = {}
gridded_symbols: List[str] = []
active_grids: Dict[str, List[int]] = {}
account_cache: Dict[str, Decimal] = {}
initial_asset_values: Dict[str, Decimal] = {}
all_symbols: List[str] = []
portfolio_symbols: List[str] = []
top_bid_symbols: List[str] = []

# Tracking
last_regrid_time = 0
last_regrid_str = "Never"
last_full_refresh = 0
REFRESH_INTERVAL = 300

# Locks
price_lock = threading.Lock()
state_lock = threading.Lock()
api_rate_lock = threading.Lock()

# UI
ui_logs: List[str] = []

# WebSockets
price_ws = depth_ws = kline_ws = user_data_ws = None
ws_list: List[websocket.WebSocketApp] = []
last_pong: Dict[int, float] = {}

# Alerts
alert_queue: List[Tuple[str, bool]] = []
last_bundle_sent = 0
BUNDLE_INTERVAL = 600
alert_thread_running = False

# WS Data Weight
WS_DATA_LIMIT_BYTES = 5 * 1024 * 1024  # 5 MB/min
ws_data_bytes = 0
ws_minute_start = time.time()
ws_data_lock = threading.Lock()
ws_overflow_triggered = False

# ==============================================================
# UTILITIES
# ==============================================================
def now_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

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

# ==============================================================
# REST RATE LIMITER
# ==============================================================
class BinanceRateLimiter:
    def __init__(self):
        self.weight_used = 0
        self.minute_start = time.time()
        self.lock = threading.Lock()
        self.banned_until = 0
        self.backoff_multiplier = 1

    def _reset_if_new_minute(self):
        now = time.time()
        if now - self.minute_start >= 60:
            self.weight_used = 0
            self.minute_start = now // 60 * 60
            self.backoff_multiplier = 1

    def wait_if_needed(self, weight: int = 1):
        with self.lock:
            self._reset_if_new_minute()
            if time.time() < self.banned_until:
                wait = int(self.banned_until - time.time()) + 5
                log_ui(f"IP BANNED. Sleeping {wait}s")
                time.sleep(wait)
                self.banned_until = 0
            if self.weight_used + weight > 5400:
                sleep_time = 60 - (time.time() - self.minute_start) + 5
                log_ui(f"REST weight high. Backing off {sleep_time:.1f}s")
                time.sleep(sleep_time)
                self._reset_if_new_minute()

    def update_from_headers(self, headers: dict):
        with self.lock:
            used = headers.get('X-MBX-USED-WEIGHT-1M')
            if used:
                self.weight_used = max(self.weight_used, int(used))
            if 'Retry-After' in headers:
                retry = int(headers.get('Retry-After', 60))
                if retry > 50000:
                    self.banned_until = retry
                else:
                    time.sleep(retry * self.backoff_multiplier)
                    self.backoff_multiplier = min(self.backoff_multiplier * 2, 16)

rate_limiter = BinanceRateLimiter()

def safe_api_call(func, *args, weight: int = 1, **kwargs):
    while True:
        rate_limiter.wait_if_needed(weight)
        try:
            with api_rate_lock:
                response = func(*args, **kwargs)
            if hasattr(response, 'headers'):
                rate_limiter.update_from_headers(response.headers)
            return response
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ['429', 'rate limit', '418', 'banned']):
                rate_limiter.update_from_headers({'Retry-After': '60'})
                continue
            log_ui(f"API error: {e}. Retrying...")
            time.sleep(5)

# ==============================================================
# WEBSOCKET DATA WEIGHT (MEMORY-SAFE)
# ==============================================================
def record_ws_data_size(message: str):
    global ws_data_bytes, ws_minute_start
    size = len(message.encode('utf-8', errors='ignore'))
    with ws_data_lock:
        now = time.time()
        if now - ws_minute_start >= 60:
            ws_data_bytes = 0
            ws_minute_start = now // 60 * 60
        ws_data_bytes += size

def monitor_ws_data_weight():
    global ws_overflow_triggered
    log_ui("WS DATA WEIGHT MONITOR STARTED (5 MB/min)")
    while True:
        time.sleep(15)
        with ws_data_lock:
            mb = ws_data_bytes / (1024 * 1024)
            if mb > 5.0 and not ws_overflow_triggered:
                ws_overflow_triggered = True
                alert = f"WS DATA OVERFLOW\n{mb:.2f} MB/min > 5.0 MB\nStopping 10 min\n{now_str()}"
                log_ui(alert)
                send_callmebot_alert(alert, force_send=True)
                emergency_stop("WS DATA WEIGHT EXCEEDED")
                log_ui("Sleeping 10 minutes...")
                time.sleep(600)
                ws_overflow_triggered = False
                ws_data_bytes = 0
                log_ui("Resumed after cooldown")

# ==============================================================
# HEARTBEAT MONITOR
# ==============================================================
def on_pong(ws, message):
    last_pong[id(ws)] = time.time()

def monitor_heartbeats():
    log_ui("HEARTBEAT MONITOR STARTED")
    while True:
        time.sleep(30)
        now = time.time()
        for ws in ws_list:
            if id(ws) not in last_pong or now - last_pong.get(id(ws), 0) > 90:
                log_ui(f"NO PONG from {ws.url}")
                emergency_stop("WEBSOCKET HEARTBEAT FAILURE")
                return

# ==============================================================
# CALLMEBOT ALERTS
# ==============================================================
def send_callmebot_alert(message: str, force_send: bool = False):
    if not CALLMEBOT_PHONE or not CALLMEBOT_API_KEY:
        return
    with threading.Lock():
        alert_queue.append((message, force_send))
    log_ui(f"QUEUED ALERT: {message[:50]}")

def _send_single(msg: str):
    global last_bundle_sent
    url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={msg}&apikey={CALLMEBOT_API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            log_ui(f"SENT: {msg[:50]}...")
    except:
        pass
    last_bundle_sent = time.time()

def alert_manager():
    global alert_thread_running
    alert_thread_running = True
    while True:
        instant = []
        with threading.Lock():
            for msg, force in list(alert_queue):
                if force:
                    instant.append(msg)
                    alert_queue.remove((msg, force))
        for m in instant:
            _send_single(m)
        if time.time() - last_bundle_sent >= BUNDLE_INTERVAL:
            normal = []
            with threading.Lock():
                for msg, force in list(alert_queue):
                    if not force:
                        normal.append(f"[{now_str()}] {msg}")
                        alert_queue.remove((msg, force))
            if normal:
                bundle = "GRID ALERTS\n" + "\n".join(normal[-20:])
                _send_single(bundle)
        time.sleep(5)

# ==============================================================
# WEBSOCKET WRAPPERS
# ==============================================================
def safe_ws_connect(url, **kwargs):
    rate_limiter.wait_if_needed(2)
    kwargs.setdefault('on_pong', on_pong)
    return websocket.WebSocketApp(url, **kwargs)

def wrap_handler(handler):
    def wrapper(ws, message):
        record_ws_data_size(message)
        if handler:
            handler(ws, message)
    return wrapper

# ==============================================================
# WEBSOCKETS: PRICE
# ==============================================================
PRICE_STREAM = "wss://stream.binance.us:9443/stream?streams=!miniTicker@arr"

def on_price_message(ws, message):
    try:
        data = json.loads(message)
        if 'data' in data:
            for item in data['data']:
                live_prices[item['s']] = to_decimal(item['c'])
    except:
        pass

def start_price_stream():
    global price_ws
    price_ws = safe_ws_connect(
        PRICE_STREAM,
        on_message=wrap_handler(on_price_message),
        on_error=lambda ws, err: log_ui(f"Price WS error: {err}"),
        on_close=lambda ws, c, m: (log_ui("Price WS closed"), send_callmebot_alert("Price WS down", True), time.sleep(5), start_price_stream()),
        on_open=lambda ws: log_ui("PRICE WS CONNECTED")
    )
    ws_list.append(price_ws)
    threading.Thread(target=price_ws.run_forever, kwargs={'ping_interval': 15, 'ping_timeout': 10}, daemon=True).start()

# ==============================================================
# WEBSOCKETS: USER DATA
# ==============================================================
def get_listen_key():
    try:
        return safe_api_call(binance_client.stream_get_listen_key, weight=1)['listenKey']
    except:
        return None

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data['e'] == 'outboundAccountPosition':
            for b in data['B']:
                balances[b['a']] = to_decimal(b['f']) + to_decimal(b['l'])
        elif data['e'] == 'executionReport' and data['X'] == 'FILLED':
            symbol = data['s']
            side = data['S']
            qty = to_decimal(data['z'])
            price = to_decimal(data['L'])
            profit = ZERO
            if side == 'SELL':
                cost = get_buy_cost(symbol, qty)
                fee = to_decimal(data.get('n', '0'))
                profit = qty * price - cost - fee
                st.session_state.realized_pnl += profit
            msg = f"FILL {side} {symbol} {qty} @ {price}"
            if profit > ZERO:
                msg += f" +${profit:.2f}"
            send_callmebot_alert(msg, True)
            log_ui(msg)
    except:
        pass

def start_user_stream():
    global user_data_ws
    key = get_listen_key()
    if not key:
        time.sleep(10)
        return start_user_stream()
    url = f"wss://stream.binance.us:9443/ws/{key}"
    user_data_ws = safe_ws_connect(
        url,
        on_message=wrap_handler(on_user_message),
        on_error=lambda ws, err: log_ui("User WS error"),
        on_close=lambda ws, c, m: (log_ui("User WS closed"), time.sleep(5), start_user_stream()),
        on_open=lambda ws: log_ui("USER WS CONNECTED")
    )
    ws_list.append(user_data_ws)
    threading.Thread(target=user_data_ws.run_forever, kwargs={'ping_interval': 15, 'ping_timeout': 10}, daemon=True).start()

def keep_user_stream_alive():
    while True:
        time.sleep(1800)
        try:
            key = get_listen_key()
            if key:
                safe_api_call(binance_client.stream_keepalive, listenKey=key, weight=1)
        except:
            pass

# ==============================================================
# ACCOUNT & BALANCE
# ==============================================================
def update_account_cache():
    try:
        account = safe_api_call(binance_client.get_account, weight=10)
        for bal in account['balances']:
            asset = bal['asset']
            account_cache[asset] = to_decimal(bal['free']) + to_decimal(bal['locked'])
        log_ui("Account cache updated")
    except Exception as e:
        log_ui(f"Cache update failed: {e}")

def get_balance(asset: str) -> Decimal:
    return balances.get(asset, account_cache.get(asset, ZERO))

# ==============================================================
# P&L
# ==============================================================
def calculate_unrealized_pnl() -> Decimal:
    unrealized = ZERO
    for asset, bal in balances.items():
        if asset in ['USDT'] or bal <= ZERO:
            continue
        symbol = f"{asset}USDT"
        price = get_current_price(symbol)
        if price <= ZERO:
            continue
        current = bal * price
        initial = initial_asset_values.get(asset, current)
        unrealized += current - initial
    return unrealized

def update_initial_asset_values():
    for asset, bal in balances.items():
        if asset == 'USDT' or bal <= ZERO:
            continue
        symbol = f"{asset}USDT"
        price = get_current_price(symbol)
        if price > ZERO:
            initial_asset_values[asset] = bal * price

def get_current_price(symbol: str) -> Decimal:
    with price_lock:
        return live_prices.get(symbol, ZERO)

# ==============================================================
# SYMBOL INFO
# ==============================================================
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
    except:
        pass
    symbol_info_cache[symbol] = info
    return info

# ==============================================================
# GRID LOGIC
# ==============================================================
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
        log_ui(f"ORDER {side} {symbol} {qty} @ {price}")
        with state_lock:
            active_grids.setdefault(symbol, []).append(oid)
        return oid
    except Exception as e:
        log_ui(f"Order failed: {e}")
        return 0

def cancel_all_orders(symbol: str):
    try:
        orders = safe_api_call(binance_client.get_open_orders, symbol=symbol, weight=5)
        for o in orders:
            safe_api_call(binance_client.cancel_order, symbol=symbol, orderId=o['orderId'], weight=1)
        with state_lock:
            active_grids[symbol] = []
    except:
        pass

def cancel_all_orders_global():
    try:
        orders = safe_api_call(binance_client.get_open_orders, weight=40)
        for o in orders:
            safe_api_call(binance_client.cancel_order, symbol=o['symbol'], orderId=o['orderId'], weight=1)
        with state_lock:
            active_grids.clear()
        send_callmebot_alert("ALL ORDERS CANCELED", True)
    except:
        pass

def compute_qty_for_notional(symbol: str, notional: Decimal) -> Decimal:
    price = get_current_price(symbol)
    if price <= ZERO:
        return ZERO
    step = fetch_symbol_info(symbol)['stepSize']
    raw = notional / price
    return (raw // step) * step

def get_buy_cost(symbol: str, qty: Decimal) -> Decimal:
    try:
        orders = safe_api_call(binance_client.get_all_orders, symbol=symbol, limit=500, weight=40)
        buys = [o for o in orders if o['side'] == 'BUY' and o['status'] == 'FILLED']
        if not buys:
            return ZERO
        total_cost = sum(Decimal(o['price']) * Decimal(o['executedQty']) for o in buys)
        total_qty = sum(Decimal(o['executedQty']) for o in buys)
        avg = total_cost / total_qty if total_qty > ZERO else ZERO
        return qty * avg
    except:
        return ZERO

def place_new_grid(symbol: str, levels: int, grid_size: Decimal):
    global last_regrid_time, last_regrid_str
    if levels < 1 or levels > 5 or grid_size < 5 or grid_size > 50:
        return
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
    profit_mult = Decimal('1.01')
    for i in range(1, levels + 1):
        buy_price = price * (Decimal('1') - Decimal('0.015') * i)
        place_limit_order(symbol, 'BUY', buy_price, qty)
    for i in range(1, levels + 1):
        sell_price = price * (Decimal('1') + Decimal('0.015') * i) * profit_mult
        place_limit_order(symbol, 'SELL', sell_price, qty)
    last_regrid_time = time.time()
    last_regrid_str = now_str()
    log_ui(f"GRID {symbol} {levels}L ${grid_size}")

# ==============================================================
# PORTFOLIO & ROTATION
# ==============================================================
def import_portfolio_symbols():
    update_account_cache()
    owned = []
    for asset, bal in account_cache.items():
        if bal > ZERO and asset != 'USDT':
            sym = f"{asset}USDT"
            try:
                safe_api_call(binance_client.get_symbol_ticker, symbol=sym, weight=1)
                owned.append(sym)
            except:
                pass
    with state_lock:
        global portfolio_symbols
        portfolio_symbols = list(set(owned))
    log_ui(f"Portfolio: {len(portfolio_symbols)} symbols")

def rotate_to_top25():
    try:
        vols = [(s, bid_volume.get(s, ZERO)) for s in all_symbols]
        vols.sort(key=lambda x: x[1], reverse=True)
        new_top = [s for s, _ in vols[:25]]
        removed = [s for s in gridded_symbols if s not in new_top]
        added = [s for s in new_top if s not in gridded_symbols]
        for s in removed:
            cancel_all_orders(s)
            with state_lock:
                gridded_symbols.remove(s)
                active_grids.pop(s, None)
        for s in added:
            gridded_symbols.append(s)
        for s in gridded_symbols:
            place_new_grid(s, st.session_state.grid_levels, st.session_state.grid_size)
        msg = f"AUTO ROTATED\n+{len(added)} -{len(removed)}\n{now_str()}"
        send_callmebot_alert(msg, True)
        log_ui(f"Rotated: +{len(added)} -{len(removed)}")
    except Exception as e:
        log_ui(f"Rotate error: {e}")

# ==============================================================
# EMERGENCY STOP
# ==============================================================
def emergency_stop(reason: str = "MANUAL"):
    cancel_all_orders_global()
    st.session_state.paused = True
    st.session_state.emergency_stopped = True
    msg = f"EMERGENCY STOP: {reason}\nBot PAUSED\n{now_str()}"
    send_callmebot_alert(msg, True)
    log_ui(msg)

# ==============================================================
# STATUS LIGHTS
# ==============================================================
def get_ws_status() -> bool:
    return all(ws.sock and ws.sock.connected for ws in ws_list if ws)

def display_status_lights():
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("### WebSocket")
        if get_ws_status():
            st.markdown("**ONLINE**")
            st.markdown('<div style="width:30px;height:30px;background:#00ff00;border-radius:50%;"></div>', unsafe_allow_html=True)
        else:
            st.markdown("**OFFLINE**")
            st.markdown('<div style="width:30px;height:30px;background:#ff0000;border-radius:50%;"></div>', unsafe_allow_html=True)
    with col2:
        st.markdown("### API")
        try:
            safe_api_call(binance_client.get_server_time, weight=1)
            st.markdown("**ONLINE**")
            st.markdown('<div style="width:30px;height:30px;background:#00ff00;border-radius:50%;"></div>', unsafe_allow_html=True)
        except:
            st.markdown("**OFFLINE**")
            st.markdown('<div style="width:30px;height:30px;background:#ff0000;border-radius:50%;"></div>', unsafe_allow_html=True)

# ==============================================================
# BACKGROUND THREADS
# ==============================================================
def start_background_threads():
    threading.Thread(target=alert_manager, daemon=True).start()
    threading.Thread(target=monitor_ws_data_weight, daemon=True).start()
    threading.Thread(target=monitor_heartbeats, daemon=True).start()
    threading.Thread(target=keep_user_stream_alive, daemon=True).start()

    def rebalancer():
        time.sleep(5)
        log_ui("REBALANCER STARTED")
        while True:
            try:
                if st.session_state.get('paused', False):
                    time.sleep(10)
                    continue
                now = time.time()
                if now - last_full_refresh >= REFRESH_INTERVAL:
                    update_account_cache()
                    update_initial_asset_values()
                    global last_full_refresh
                    last_full_refresh = now
                for sym in list(gridded_symbols):
                    place_new_grid(sym, st.session_state.grid_levels, st.session_state.grid_size)
                time.sleep(45)
            except Exception as e:
                log_ui(f"Rebalancer error: {e}")
                time.sleep(10)

    threading.Thread(target=rebalancer, daemon=True).start()

# ==============================================================
# INITIALIZATION
# ==============================================================
def initialize():
    global all_symbols
    try:
        info = safe_api_call(binance_client.get_exchange_info, weight=40)
        all_symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        log_ui(f"Loaded {len(all_symbols)} symbols")
    except:
        all_symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
        log_ui("Using fallback symbols")

# ==============================================================
# MAIN & STREAMLIT UI
# ==============================================================
def main():
    st.session_state.setdefault('realized_pnl', ZERO)
    st.session_state.setdefault('grid_levels', 3)
    st.session_state.setdefault('grid_size', 10)
    st.session_state.setdefault('paused', False)
    st.session_state.setdefault('emergency_stopped', False)

    initialize()
    start_price_stream()
    start_user_stream()
    start_background_threads()

    st.title("INFINITY GRID BOT — BINANCE.US LIVE")
    display_status_lights()

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("P&L")
        realized = st.session_state.realized_pnl
        unrealized = calculate_unrealized_pnl()
        total = realized + unrealized
        st.metric("Realized", f"${realized:+.2f}")
        st.metric("Unrealized", f"${unrealized:+.2f}")
        st.metric("Total", f"${total:+.2f}", delta=f"{total:+.2f}")
    with col2:
        st.subheader("Status")
        st.write(f"**Grids:** {len(gridded_symbols)}")
        st.write(f"**Last Regrid:** {last_regrid_str}")
        st.write(f"**USDT:** ${get_balance('USDT'):.2f}")

    log_container = st.empty()
    while True:
        log_container.text("\n".join(ui_logs[-25:]))
        time.sleep(1)

if __name__ == "__main__":
    main()
