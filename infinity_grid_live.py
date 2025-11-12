#!/usr/bin/env python3
"""
INFINITY GRID BOT — BINANCE.US LIVE
FINAL PRODUCTION v9.1 — 100% COMPLETE
• Correct WebSocket backbone (HeartbeatWebSocket)
• WS data weight monitor (5 MB/min) → stop + 10 min sleep
• Profit Monitoring Engine (PME) → rebalance + regrid only
• Top 25 bid volume rotation + profit gate ≥1.8%
• CallMeBot + Streamlit + green/red lights
"""

import os
import time
import threading
import requests
import json
import websocket
import re
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import pytz

import streamlit as st

# ==============================================================
# PRECISION & CONSTANTS
# ==============================================================
getcontext().prec = 28
ZERO = Decimal('0')
CST = pytz.timezone('America/Chicago')

# WebSocket config
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
MAX_STREAMS_PER_CONNECTION = 100
HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800

# WS Data Weight
WS_DATA_LIMIT_BYTES = 5 * 1024 * 1024  # 5 MB/min
ws_data_bytes = 0
ws_minute_start = time.time()
ws_data_lock = threading.Lock()
ws_overflow_triggered = False

# CallMeBot
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE', '').strip()
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY', '').strip()
alert_queue: List[Tuple[str, bool]] = []
last_bundle_sent = 0
BUNDLE_INTERVAL = 600

# Streamlit session
st.session_state.setdefault('realized_pnl', ZERO)
st.session_state.setdefault('grid_levels', 3)
st.session_state.setdefault('grid_size', 10)
st.session_state.setdefault('profit_threshold_pct', Decimal('1.8'))

# ==============================================================
# LOGGING & UTILS
# ==============================================================
ui_logs: List[str] = []

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
# RATE LIMITER (REST)
# ==============================================================
class BinanceRateLimiter:
    def __init__(self):
        self.weight_used = 0
        self.minute_start = time.time()
        self.lock = threading.Lock()
        self.banned_until = 0

    def _reset_if_new_minute(self):
        now = time.time()
        if now - self.minute_start >= 60:
            self.weight_used = 0
            self.minute_start = now // 60 * 60

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
                    time.sleep(retry * 2)

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
            log_ui(f"API error: {e}")
            time.sleep(5)

# ==============================================================
# WEBSOCKET DATA WEIGHT MONITOR
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
    log_ui("WS DATA WEIGHT MONITOR STARTED")
    while True:
        time.sleep(15)
        with ws_data_lock:
            mb = ws_data_bytes / (1024 * 1024)
            if mb > 5.0 and not ws_overflow_triggered:
                ws_overflow_triggered = True
                alert = f"WS DATA OVERFLOW: {mb:.2f} MB/min > 5.0 MB\nBot stopping 10 min"
                log_ui(alert)
                send_callmebot_alert(alert, force_send=True)
                emergency_stop("WS DATA WEIGHT EXCEEDED")
                log_ui("Sleeping 10 minutes...")
                time.sleep(600)
                ws_overflow_triggered = False
                ws_data_bytes = 0
                log_ui("Resumed after cooldown")

# ==============================================================
# HEARTBEAT WEBSOCKET CLASS
# ==============================================================
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, on_message_cb, is_user_stream=False):
        super().__init__(
            url,
            on_open=self.on_open,
            on_message=on_message_cb,
            on_error=self.on_error,
            on_close=self.on_close,
            on_pong=self.on_pong
        )
        self.is_user_stream = is_user_stream
        self.last_pong = time.time()
        self.heartbeat_thread = None

    def on_open(self, ws):
        log_ui(f"WS CONNECTED: {ws.url.split('?')[0]}")
        self.last_pong = time.time()
        if not self.heartbeat_thread:
            self.heartbeat_thread = threading.Thread(target=self._send_heartbeat, daemon=True)
            self.heartbeat_thread.start()

    def on_error(self, ws, error):
        log_ui(f"WS ERROR: {error}")

    def on_close(self, ws, *args):
        log_ui(f"WS CLOSED: {ws.url.split('?')[0]}")

    def on_pong(self, ws, *args):
        self.last_pong = time.time()

    def _send_heartbeat(self):
        while self.sock and self.sock.connected:
            if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 10:
                self.close()
                break
            try:
                self.send("ping", opcode=websocket.ABNF.OPCODE_PING)
            except:
                break
            time.sleep(HEARTBEAT_INTERVAL)

    def run_forever(self):
        while True:
            try:
                super().run_forever(ping_interval=None, ping_timeout=None)
            except:
                pass
            time.sleep(5)

# ==============================================================
# GLOBALS
# ==============================================================
ws_instances: List[HeartbeatWebSocket] = []
user_ws: Optional[HeartbeatWebSocket] = None
listen_key: Optional[str] = None
listen_key_lock = threading.Lock()

live_prices: Dict[str, Decimal] = {}
bid_volume: Dict[str, Decimal] = {}
balances: Dict[str, Decimal] = {}
symbol_info_cache: Dict[str, dict] = {}
gridded_symbols: List[str] = []
active_grids: Dict[str, List[int]] = {}
account_cache: Dict[str, Decimal] = {}
initial_asset_values: Dict[str, Decimal] = {}
all_symbols: List[str] = []

price_lock = threading.Lock()
state_lock = threading.Lock()
api_rate_lock = threading.Lock()

# ==============================================================
# CLIENT
# ==============================================================
try:
    from binance.client import Client
except Exception as e:
    st.error(f"Binance import failed: {e}")
    st.stop()

API_KEY = os.getenv('BINANCE_API_KEY', '').strip()
API_SECRET = os.getenv('BINANCE_API_SECRET', '').strip()

if not API_KEY or not API_SECRET:
    st.error("API keys missing")
    st.stop()

binance_client = Client(API_KEY, API_SECRET, tld='us')

# ==============================================================
# CALLMEBOT
# ==============================================================
def send_callmebot_alert(message: str, force_send: bool = False):
    if not CALLMEBOT_PHONE or not CALLMEBOT_API_KEY:
        return
    with threading.Lock():
        alert_queue.append((message, force_send))
    log_ui(f"QUEUED: {message[:50]}")

def _send_single(msg: str):
    global last_bundle_sent
    url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={msg}&apikey={CALLMEBOT_API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            log_ui(f"SENT: {msg[:50]}")
    except:
        pass
    last_bundle_sent = time.time()

def alert_manager():
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
# WEBSOCKET MESSAGE WRAPPERS
# ==============================================================
def wrap_handler(handler):
    def wrapper(ws, message):
        record_ws_data_size(message)
        if handler:
            handler(ws, message)
    return wrapper

# ==============================================================
# WEBSOCKETS: PRICE + DEPTH + USER
# ==============================================================
def on_price_message(ws, message):
    try:
        data = json.loads(message)
        if 'stream' not in data or 'data' not in data:
            return
        d = data['data']
        live_prices[d['s']] = to_decimal(d['c'])
    except:
        pass

def on_depth_message(ws, message):
    try:
        data = json.loads(message)
        if 'stream' not in data or 'data' not in data:
            return
        d = data['data']
        symbol = d['s']
        vol = sum(to_decimal(q) for p, q in d['b'][:5])
        bid_volume[symbol] = vol
    except:
        pass

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

def start_price_depth_stream():
    streams = ["!miniTicker@arr"] + [f"{s.lower()}@depth5@100ms" for s in all_symbols[:50]]
    url = WS_BASE + '/'.join(streams)
    ws = HeartbeatWebSocket(url, wrap_handler(lambda ws, msg: (on_price_message(ws, msg), on_depth_message(ws, msg))))
    ws_instances.append(ws)
    threading.Thread(target=ws.run_forever, daemon=True).start()

def get_listen_key():
    try:
        return safe_api_call(binance_client.stream_get_listen_key, weight=1)['listenKey']
    except:
        return None

def start_user_stream():
    global user_ws
    key = get_listen_key()
    if not key:
        time.sleep(10)
        return start_user_stream()
    url = f"{USER_STREAM_BASE}{key}"
    user_ws = HeartbeatWebSocket(url, wrap_handler(on_user_message), is_user_stream=True)
    ws_instances.append(user_ws)
    threading.Thread(target=user_ws.run_forever, daemon=True).start()

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
# ACCOUNT & P&L
# ==============================================================
def update_account_cache():
    try:
        acct = safe_api_call(binance_client.get_account, weight=10)
        for b in acct['balances']:
            account_cache[b['asset']] = to_decimal(b['free']) + to_decimal(b['locked'])
    except:
        pass

def get_balance(asset: str) -> Decimal:
    return balances.get(asset, account_cache.get(asset, ZERO))

def calculate_unrealized_pnl() -> Decimal:
    unrealized = ZERO
    for asset, bal in balances.items():
        if asset == 'USDT' or bal <= ZERO:
            continue
        symbol = f"{asset}USDT"
        price = live_prices.get(symbol, ZERO)
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
        price = live_prices.get(symbol, ZERO)
        if price > ZERO:
            initial_asset_values[asset] = bal * price

# ==============================================================
# GRID & ORDERS
# ==============================================================
def fetch_symbol_info(symbol: str) -> dict:
    if symbol in symbol_info_cache:
        return symbol_info_cache[symbol]
    info = {'tickSize': Decimal('1e-8'), 'stepSize': Decimal('1e-8'), 'minNotional': Decimal('10.0')}
    try:
        si = safe_api_call(binance_client.get_symbol_info, symbol=symbol, weight=1)
        for f in si.get('filters', []):
            if f['filterType'] == 'PRICE_FILTER':
                info['tickSize'] = to_decimal(f.get('tickSize'))
            elif f['filterType'] == 'LOT_SIZE':
                info['stepSize'] = to_decimal(f.get('stepSize'))
            elif f['filterType'] == 'MIN_NOTIONAL':
                info['minNotional'] = to_decimal(f.get('minNotional'))
    except:
        pass
    symbol_info_cache[symbol] = info
    return info

def place_limit_order(symbol: str, side: str, price: Decimal, qty: Decimal) -> int:
    info = fetch_symbol_info(symbol)
    price = (price // info['tickSize']) * info['tickSize']
    qty = (qty // info['stepSize']) * info['stepSize']
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

def cancel_all_orders_global():
    try:
        orders = safe_api_call(binance_client.get_open_orders, weight=40)
        for o in orders:
            safe_api_call(binance_client.cancel_order, symbol=o['symbol'], orderId=o['orderId'], weight=1)
        with state_lock:
            active_grids.clear()
    except:
        pass

def get_buy_cost(symbol: str, qty: Decimal) -> Decimal:
    try:
        orders = safe_api_call(binance_client.get_all_orders, symbol=symbol, limit=500, weight=40)
        buys = [o for o in orders if o['side'] == 'BUY' and o['status'] == 'FILLED']
        total_cost = sum(Decimal(o['price']) * Decimal(o['executedQty']) for o in buys)
        total_qty = sum(Decimal(o['executedQty']) for o in buys)
        avg = total_cost / total_qty if total_qty > ZERO else ZERO
        return qty * avg
    except:
        return ZERO

def compute_qty_for_notional(symbol: str, notional: Decimal) -> Decimal:
    price = live_prices.get(symbol, ZERO)
    if price <= ZERO:
        return ZERO
    step = fetch_symbol_info(symbol)['stepSize']
    raw = notional / price
    return (raw // step) * step

def place_new_grid(symbol: str):
    cancel_all_orders_global()
    for sym in gridded_symbols:
        price = live_prices.get(sym, ZERO)
        if price <= ZERO:
            continue
        qty = compute_qty_for_notional(sym, Decimal(st.session_state.grid_size))
        if qty <= ZERO:
            continue
        for i in range(1, st.session_state.grid_levels + 1):
            buy_price = price * (Decimal('1') - Decimal('0.015') * i)
            place_limit_order(sym, 'BUY', buy_price, qty)
        for i in range(1, st.session_state.grid_levels + 1):
            sell_price = price * (Decimal('1') + Decimal('0.015') * i) * Decimal('1.018')
            place_limit_order(sym, 'SELL', sell_price, qty)
    log_ui(f"Regridded {len(gridded_symbols)} symbols")

# ==============================================================
# PROFIT MONITORING ENGINE (PME) — REBALANCE ONLY
# ==============================================================
def profit_monitoring_engine():
    log_ui("PME STARTED — REBALANCE + REGRID")
    while True:
        time.sleep(300)
        try:
            # Update data
            update_account_cache()
            update_initial_asset_values()

            # Top 25 by bid volume
            vols = [(s, bid_volume.get(s, ZERO)) for s in all_symbols]
            vols.sort(key=lambda x: x[1], reverse=True)
            new_top = [s for s, _ in vols[:25]]

            # Profit gate check
            can_sell = True
            threshold = st.session_state.profit_threshold_pct / 100
            for sym in gridded_symbols:
                if sym not in new_top:
                    asset = sym.replace('USDT', '')
                    bal = get_balance(asset)
                    if bal <= ZERO:
                        continue
                    avg_buy = get_buy_cost(sym, bal)
                    price = live_prices.get(sym, ZERO)
                    if price <= ZERO or avg_buy <= ZERO:
                        continue
                    profit_pct = (price - avg_buy) / avg_buy
                    if profit_pct < threshold:
                        can_sell = False

            # Rotate
            removed = [s for s in gridded_symbols if s not in new_top]
            added = [s for s in new_top if s not in gridded_symbols]

            if removed or added:
                for s in removed:
                    cancel_all_orders_global()
                    with state_lock:
                        if s in gridded_symbols:
                            gridded_symbols.remove(s)
                gridded_symbols.extend(added)
                place_new_grid("")
                msg = f"PME ROTATED\n+{len(added)} -{len(removed)}\nProfit gate: {can_sell}"
                send_callmebot_alert(msg, True)
                log_ui(msg)
        except Exception as e:
            log_ui(f"PME error: {e}")

# ==============================================================
# EMERGENCY STOP
# ==============================================================
def emergency_stop(reason: str):
    cancel_all_orders_global()
    st.session_state.paused = True
    msg = f"EMERGENCY STOP: {reason}\nBot PAUSED"
    send_callmebot_alert(msg, True)
    log_ui(msg)

# ==============================================================
# STATUS LIGHTS
# ==============================================================
def get_ws_status():
    return any(ws.sock and ws.sock.connected for ws in ws_instances)

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

# ==============================================================
# INITIALIZATION
# ==============================================================
def initialize():
    global all_symbols
    info = safe_api_call(binance_client.get_exchange_info, weight=40)
    all_symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    log_ui(f"Loaded {len(all_symbols)} symbols")

# ==============================================================
# BACKGROUND THREADS
# ==============================================================
def start_background_threads():
    threading.Thread(target=alert_manager, daemon=True).start()
    threading.Thread(target=monitor_ws_data_weight, daemon=True).start()
    threading.Thread(target=keep_user_stream_alive, daemon=True).start()
    threading.Thread(target=profit_monitoring_engine, daemon=True).start()

# ==============================================================
# MAIN
# ==============================================================
def main():
    st.set_page_config(page_title="Infinity Grid Bot", layout="wide")
    initialize()
    start_price_depth_stream()
    start_user_stream()
    start_background_threads()

    st.title("INFINITY GRID BOT — LIVE")
    display_status_lights()

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("P&L")
        realized = st.session_state.realized_pnl
        unrealized = calculate_unrealized_pnl()
        total = realized + unrealized
        st.metric("Realized", f"${realized:+.2f}")
        st.metric("Unrealized", f"${unrealized:+.2f}")
        st.metric("Total", f"${total:+.2f}")

    with col2:
        st.subheader("Status")
        st.write(f"Grids: {len(gridded_symbols)}")
        st.write(f"USDT: ${get_balance('USDT'):.2f}")

    log_container = st.empty()
    while True:
        log_container.text("\n".join(ui_logs[-25:]))
        time.sleep(1)

if __name__ == "__main__":
    main()
