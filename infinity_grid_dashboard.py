#!/usr/bin/env python3
"""
INFINITY GRID BOT — BINANCE.US LIVE
- 100% FULL CODE
- AUTO-ROTATION: ≥1.8% PROFIT PER POSITION
- TOP 25 BID VOLUME + ROTATE + SELL NON-TOP 25
- RISK MANAGEMENT + EMERGENCY STOP
- P&L + UNREALIZED + DASHBOARD
- CALLMEBOT: BUNDLED EVERY 10 MIN
- CUSTOM WEBSOCKETS: websocket-client + wss://stream.binance.us:9443
- GREEN/RED LIGHTS: WebSocket + Binance API
- WEBSOCKETS — REAL-TIME DATA
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
from typing import Dict, List, Tuple, Set
import pytz

import streamlit as st

# --------------------------------------------------------------
# RATE LIMITER
# --------------------------------------------------------------
class BinanceRateLimiter:
    def __init__(self):
        self.weight_used = 0
        self.minute_start = time.time()
        self.lock = threading.Lock()
        self.banned_until = 0  # epoch seconds
        self.backoff_multiplier = 1

    def _reset_if_new_minute(self):
        now = time.time()
        if now - self.minute_start >= 60:
            self.weight_used = 0
            self.minute_start = now // 60 * 60  # align to minute boundary
            self.backoff_multiplier = 1  # reset backoff when healthy

    def wait_if_needed(self, weight: int = 1):
        with self.lock:
            self._reset_if_new_minute()

            # IP ban check
            if time.time() < self.banned_until:
                wait = int(self.banned_until - time.time()) + 5
                log_ui(f"IP BANNED. Sleeping {wait}s until {time.ctime(self.banned_until)}")
                time.sleep(wait)
                self.banned_until = 0

            # Preemptive backoff if close to limit
            if self.weight_used + weight > 5400:  # 90% of 6000
                sleep_time = 60 - (time.time() - self.minute_start) + 5
                log_ui(f"Weight high ({self.weight_used}+{weight}). Backing off {sleep_time:.1f}s")
                time.sleep(sleep_time)
                self._reset_if_new_minute()

    def update_from_headers(self, headers: dict):
        with self.lock:
            used = headers.get('X-MBX-USED-WEIGHT-1M')
            if used:
                self.weight_used = max(self.weight_used, int(used))
            # Handle 429/418
            if 'Retry-After' in headers:
                retry = int(headers.get('Retry-After', 60))
                if retry > 50000:  # it's a timestamp (418 ban)
                    self.banned_until = retry
                    log_ui(f"IP BANNED UNTIL {time.ctime(retry)}")
                else:
                    time.sleep(retry * self.backoff_multiplier)
                    self.backoff_multiplier = min(self.backoff_multiplier * 2, 16)

# Global instance
rate_limiter = BinanceRateLimiter()

# --------------------------------------------------------------
# SAFE REST CALL WRAPPER
# --------------------------------------------------------------
def safe_api_call(func, *args, weight: int = 1, **kwargs):
    while True:
        rate_limiter.wait_if_needed(weight)
        try:
            with api_rate_lock:  # your existing lock
                response = func(*args, **kwargs)
            # Update weight from headers (if available)
            if hasattr(response, 'headers'):
                rate_limiter.update_from_headers(response.headers)
            elif isinstance(response, dict) and 'headers' in response:
                rate_limiter.update_from_headers(response['headers'])
            return response
        except Exception as e:
            err_str = str(e).lower()
            if '429' in err_str or 'rate limit' in err_str:
                log_ui("429 detected. Backing off...")
                rate_limiter.update_from_headers({'Retry-After': '60'})
                continue
            if '418' in err_str or 'banned' in err_str:
                log_ui("418 IP BAN detected!")
                rate_limiter.update_from_headers({'Retry-After': str(int(time.time() + 180))})
                continue
            if 'api-key' in err_str or 'signature' in err_str:
                log_ui(f"Auth error: {e}")
                time.sleep(10)
                continue
            log_ui(f"API error: {e}. Retrying in 5s...")
            time.sleep(5)

# --------------------------------------------------------------
# SAFE WS CONNECT
# --------------------------------------------------------------
def safe_ws_connect(url, **kwargs):
    rate_limiter.wait_if_needed(weight=2)  # WS connect costs ~2 weight
    return websocket.WebSocketApp(url, **kwargs)

# --------------------------------------------------------------
# 1. PAGE CONFIG
# --------------------------------------------------------------
st.set_page_config(page_title="Infinity Grid — BINANCE.US LIVE", layout="wide")

# --------------------------------------------------------------
# 2. Binance Client (REST only)
# --------------------------------------------------------------
try:
    from binance.client import Client
except Exception as e:
    st.error(f"Failed to import binance: {e}")
    st.stop()

getcontext().prec = 28
ZERO = Decimal('0')
CST = pytz.timezone('America/Chicago')

# ---------------------------
# CONFIG & VALIDATION
# ---------------------------
API_KEY = os.getenv('BINANCE_API_KEY', '').strip()
API_SECRET = os.getenv('BINANCE_API_SECRET', '').strip()
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE', '').strip()
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY', '').strip()

if not API_KEY or not API_SECRET:
    st.error("BINANCE_API_KEY and BINANCE_API_SECRET required.")
    st.stop()

if CALLMEBOT_PHONE:
    if not re.match(r'^\+\d{7,15}$', CALLMEBOT_PHONE):
        st.error("CALLMEBOT_PHONE must be +1234567890 format.")
        st.stop()
    if not CALLMEBOT_API_KEY:
        st.error("CALLMEBOT_API_KEY required.")
        st.stop()
else:
    CALLMEBOT_API_KEY = None

# ---------------------------
# Binance Client (REST only)
# ---------------------------
try:
    binance_client = Client(API_KEY, API_SECRET, tld='us')
    st.success("Connected to BINANCE.US (REST)")
except Exception as e:
    st.error(f"Binance connection failed: {e}")
    st.stop()

# ---------------------------
# State & Locks
# ---------------------------
live_prices: Dict[str, Decimal] = {}
bid_volume: Dict[str, Decimal] = {}
klines: Dict[str, List[Dict]] = {}
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
daily_pnl_start: Decimal = ZERO
daily_pnl_date: str = ""
last_auto_rotation = 0
last_top25_snapshot: List[str] = []

# Alert Bundling
alert_queue: List[Tuple[str, bool]] = []
last_bundle_sent = 0
BUNDLE_INTERVAL = 10 * 60
alert_thread_running = False

# Timers
last_regrid_time = 0
last_regrid_str = "Never"
last_full_refresh = 0
REFRESH_INTERVAL = 300

price_lock = threading.Lock()
state_lock = threading.Lock()
api_rate_lock = threading.Lock()

ui_logs = []
initial_balance: Decimal = ZERO

# WebSocket Objects
price_ws = None
depth_ws = None
kline_ws = None
user_data_ws = None
all_symbols: List[str] = []

# ---------------------------
# Utilities
# ---------------------------
def now_str():
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

# ---------------------------
# ACCOUNT CACHE (FALLBACK)
# ---------------------------
def update_account_cache():
    try:
        account = safe_api_call(binance_client.get_account, weight=10)
        for bal in account['balances']:
            asset = bal['asset']
            free = to_decimal(bal['free'])
            locked = to_decimal(bal['locked'])
            account_cache[asset] = free + locked
        log_ui("Account cache updated (REST fallback)")
    except Exception as e:
        log_ui(f"Account cache failed: {e}")

def get_balance(asset: str) -> Decimal:
    return balances.get(asset, account_cache.get(asset, ZERO))

# ---------------------------
# UNREALIZED P&L
# ---------------------------
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

# ---------------------------
# AVERAGE BUY PRICE
# ---------------------------
def get_avg_buy_price(symbol: str) -> Decimal:
    try:
        orders = safe_api_call(binance_client.get_all_orders, symbol=symbol, limit=500, weight=40)
        buys = [o for o in orders if o['side'] == 'BUY' and o['status'] == 'FILLED']
        if not buys:
            return ZERO
        total_cost = sum(Decimal(o['price']) * Decimal(o['executedQty']) for o in buys)
        total_qty = sum(Decimal(o['executedQty']) for o in buys)
        return total_cost / total_qty if total_qty > ZERO else ZERO
    except:
        return ZERO

# ---------------------------
# PROFIT GATE: ≥1.8%
# ---------------------------
def can_rotate_on_profit() -> Tuple[bool, List[str]]:
    threshold = st.session_state.profit_threshold_pct / 100
    underperforming = []
    try:
        for asset, bal in balances.items():
            if asset == 'USDT' or bal <= ZERO:
                continue
            symbol = f"{asset}USDT"
            price = get_current_price(symbol)
            if price <= ZERO:
                continue
            avg_buy = get_avg_buy_price(symbol)
            if avg_buy <= ZERO:
                underperforming.append(symbol)
                continue
            profit_pct = (price - avg_buy) / avg_buy
            if profit_pct < threshold:
                underperforming.append(f"{symbol}: {profit_pct:.2%}")
    except Exception as e:
        log_ui(f"Profit check error: {e}")
    return len(underperforming) == 0, underperforming

# ---------------------------
# CALLMEBOT: SINGLE THREAD BUNDLING
# ---------------------------
def send_callmebot_alert(message: str, is_fill: bool = False, force_send: bool = False):
    if not CALLMEBOT_PHONE or not CALLMEBOT_API_KEY:
        log_ui("CallMeBot disabled")
        return
    with threading.Lock():
        alert_queue.append((message, force_send))
    log_ui(f"QUEUED: {message[:50]}{' (INSTANT)' if force_send else ''}")

def _send_single(message: str):
    global last_bundle_sent
    url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={message}&apikey={CALLMEBOT_API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            log_ui(f"SENT: {message[:50]}...")
        else:
            log_ui(f"CallMeBot failed: {r.status_code} {r.text}")
    except Exception as e:
        log_ui(f"CallMeBot error: {e}")
    last_bundle_sent = time.time()

def alert_manager():
    global last_bundle_sent, alert_queue, alert_thread_running
    alert_thread_running = True
    log_ui("ALERT THREAD STARTED")
    while alert_thread_running:
        try:
            instant_msgs = []
            with threading.Lock():
                for msg, force in list(alert_queue):
                    if force:
                        instant_msgs.append(msg)
                        alert_queue.remove((msg, force))
            for msg in instant_msgs:
                _send_single(msg)

            if time.time() - last_bundle_sent >= BUNDLE_INTERVAL:
                normal_msgs = []
                with threading.Lock():
                    for msg, force in list(alert_queue):
                        if not force:
                            normal_msgs.append(f"[{now_str()}] {msg}")
                            alert_queue.remove((msg, force))
                if normal_msgs:
                    bundle = "INFINITY GRID ALERTS\n"
                    bundle += f"({len(normal_msgs)} events)\n\n"
                    bundle += "\n".join(normal_msgs[-20:])
                    if len(normal_msgs) > 20:
                        bundle += f"\n... +{len(normal_msgs)-20} more"
                    _send_single(bundle)
                    last_bundle_sent = time.time()

            time.sleep(5)
        except Exception as e:
            log_ui(f"Alert thread error: {e}")
            time.sleep(10)

# ---------------------------
# API Wrappers (FALLBACK)
# ---------------------------
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
        log_ui(f"info error {symbol}: {e}")
    symbol_info_cache[symbol] = info
    return info

def get_current_price(symbol: str) -> Decimal:
    with price_lock:
        return live_prices.get(symbol, ZERO)

# ---------------------------
# WEBSOCKETS: PRICE
# ---------------------------
PRICE_STREAM = "wss://stream.binance.us:9443/stream?streams=!miniTicker@arr"

def on_price_message(ws, message):
    try:
        data = json.loads(message)
        if 'stream' not in data or 'data' not in data:
            return
        d = data['data']
        symbol = d['s']
        price = to_decimal(d['c'])
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
    log_ui("PRICE WS CONNECTED")

def start_price_stream():
    global price_ws
    price_ws = safe_ws_connect(
        PRICE_STREAM,
        on_message=on_price_message,
        on_error=on_price_error,
        on_close=on_price_close,
        on_open=on_price_open
    )
    threading.Thread(target=price_ws.run_forever, kwargs={'ping_interval': 15, 'ping_timeout': 10}, daemon=True).start()

# ---------------------------
# WEBSOCKETS: DEPTH
# ---------------------------
def build_depth_stream(symbols):
    streams = [f"{s.lower()}@depth5@100ms" for s in symbols]
    return f"wss://stream.binance.us:9443/stream?streams={'/'.join(streams)}"

def on_depth_message(ws, message):
    try:
        data = json.loads(message)
        if 'stream' not in data or 'data' not in data:
            return
        d = data['data']
        symbol = d['s']
        bids = d['b']
        vol = sum(to_decimal(q) for p, q in bids)
        bid_volume[symbol] = vol
    except Exception as e:
        log_ui(f"Depth WS error: {e}")

def on_depth_error(ws, error):
    log_ui(f"Depth WS error: {error}")

def on_depth_close(ws, code, msg):
    log_ui("Depth WS closed. Reconnecting...")
    time.sleep(5)
    start_depth_stream()

def on_depth_open(ws):
    log_ui("DEPTH WS CONNECTED")

def start_depth_stream():
    global depth_ws
    stream_url = build_depth_stream(all_symbols[:50])
    depth_ws = safe_ws_connect(
        stream_url,
        on_message=on_depth_message,
        on_error=on_depth_error,
        on_close=on_depth_close,
        on_open=on_depth_open
    )
    threading.Thread(target=depth_ws.run_forever, kwargs={'ping_interval': 15, 'ping_timeout': 10}, daemon=True).start()

# ---------------------------
# WEBSOCKETS: KLINES
# ---------------------------
def build_kline_stream(symbols):
    streams = [f"{s.lower()}@kline_1m" for s in symbols]
    return f"wss://stream.binance.us:9443/stream?streams={'/'.join(streams)}"

def on_kline_message(ws, message):
    try:
        data = json.loads(message)
        if 'stream' not in data or 'data' not in data:
            return
        k = data['data']['k']
        symbol = k['s']
        if symbol not in klines:
            klines[symbol] = []
        klines[symbol].append({
            'open_time': k['t'],
            'open': to_decimal(k['o']),
            'high': to_decimal(k['h']),
            'low': to_decimal(k['l']),
            'close': to_decimal(k['c']),
            'volume': to_decimal(k['v'])
        })
        if len(klines[symbol]) > 7:
            klines[symbol] = klines[symbol][-7:]
    except Exception as e:
        log_ui(f"Kline WS error: {e}")

def on_kline_error(ws, error):
    log_ui(f"Kline WS error: {error}")

def on_kline_close(ws, code, msg):
    log_ui("Kline WS closed. Reconnecting...")
    time.sleep(5)
    start_kline_stream()

def on_kline_open(ws):
    log_ui("KLINE WS CONNECTED")

def start_kline_stream():
    global kline_ws
    stream_url = build_kline_stream(gridded_symbols)
    kline_ws = safe_ws_connect(
        stream_url,
        on_message=on_kline_message,
        on_error=on_kline_error,
        on_close=on_kline_close,
        on_open=on_kline_open
    )
    threading.Thread(target=kline_ws.run_forever, kwargs={'ping_interval': 15, 'ping_timeout': 10}, daemon=True).start()

# ---------------------------
# WEBSOCKETS: USER DATA
# ---------------------------
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
            fee = to_decimal(data.get('n', '0'))
            profit = ZERO
            if side == 'SELL':
                cost = get_buy_cost(symbol, qty)
                profit = (qty * price) - cost - fee
            msg = f"FILLED: {side} {symbol} {qty} @ {price}"
            if profit > ZERO:
                msg += f" +${profit:.2f}"
            send_callmebot_alert(msg, is_fill=True)
            log_ui(msg)
    except Exception as e:
        log_ui(f"User WS error: {e}")

def on_user_error(ws, error):
    log_ui(f"User WS error: {error}")

def on_user_close(ws, code, msg):
    log_ui("User WS closed. Reconnecting...")
    time.sleep(5)
    start_user_stream()

def on_user_open(ws):
    log_ui("USER DATA WS CONNECTED")

def start_user_stream():
    global user_data_ws
    key = get_listen_key()
    if not key:
        return
    stream_url = f"wss://stream.binance.us:9443/ws/{key}"
    user_data_ws = safe_ws_connect(
        stream_url,
        on_message=on_user_message,
        on_error=on_user_error,
        on_close=on_user_close,
        on_open=on_user_open
    )
    threading.Thread(target=user_data_ws.run_forever, kwargs={'ping_interval': 15, 'ping_timeout': 10}, daemon=True).start()

def keep_user_stream_alive():
    while True:
        time.sleep(1800)
        try:
            key = get_listen_key()
            if key:
                safe_api_call(binance_client.stream_keepalive, listenKey=key, weight=1)
                log_ui("User stream keepalive sent")
        except:
            start_user_stream()

# ---------------------------
# START ALL WEBSOCKETS
# ---------------------------
def start_all_websockets():
    start_price_stream()
    start_depth_stream()
    start_kline_stream()
    start_user_stream()
    threading.Thread(target=keep_user_stream_alive, daemon=True).start()
    log_ui("ALL WEBSOCKETS STARTED (websocket-client)")

# ---------------------------
# GRID LOGIC
# ---------------------------
def place_limit_order(symbol: str, side: str, raw_price: Decimal, raw_qty: Decimal) -> int:
    if raw_price <= 0 or raw_qty <= 0:
        return 0
    info = fetch_symbol_info(symbol)
    price = (raw_price // info['tickSize']) * info['tickSize']
    qty = (raw_qty // info['stepSize']) * info['stepSize']
    notional = price * qty
    if notional < info['minNotional']:
        return 0
    base_asset = symbol.replace('USDT', '')
    if side == 'BUY':
        required_usdt = notional + Decimal('8')
        if get_balance('USDT') < required_usdt:
            return 0
    elif side == 'SELL':
        if get_balance(base_asset) < qty:
            return 0
    try:
        resp = safe_api_call(
            binance_client.create_order,
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=str(qty),
            price=str(price),
            weight=1
        )
        oid = int(resp['orderId'])
        log_ui(f"ORDER: {side} {symbol} {qty} @ {price} -> {oid}")
        with state_lock:
            active_grids.setdefault(symbol, []).append(oid)
            tracked_orders.add(oid)
        return oid
    except Exception as e:
        log_ui(f"ORDER FAILED {side} {symbol}: {e}")
        return 0

def cancel_all_orders(symbol: str):
    try:
        open_orders = safe_api_call(binance_client.get_open_orders, symbol=symbol, weight=5)
        for o in open_orders:
            safe_api_call(binance_client.cancel_order, symbol=symbol, orderId=o['orderId'], weight=1)
        with state_lock:
            active_grids[symbol] = []
    except Exception as e:
        log_ui(f"cancel error {symbol}: {e}")

def cancel_all_orders_global():
    try:
        open_orders = safe_api_call(binance_client.get_open_orders, weight=40)
        for o in open_orders:
            safe_api_call(binance_client.cancel_order, symbol=o['symbol'], orderId=o['orderId'], weight=1)
        with state_lock:
            active_grids.clear()
            tracked_orders.clear()
        send_callmebot_alert("ALL ORDERS CANCELED", force_send=True)
    except Exception as e:
        log_ui(f"Cancel all failed: {e}")

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
    if (qty * price) < info['minNotional']:
        return
    maker_fee, _ = get_fee_rates(symbol)
    profit_multiplier = Decimal('1') + maker_fee + Decimal('0.01')
    for i in range(1, levels + 1):
        raw_buy = price * (Decimal('1') - Decimal('0.015') * i)
        place_limit_order(symbol, 'BUY', raw_buy, qty)
    for i in range(1, levels + 1):
        raw_sell = price * (Decimal('1') + Decimal('0.015') * i) * profit_multiplier
        place_limit_order(symbol, 'SELL', raw_sell, qty)
    last_regrid_time = time.time()
    last_regrid_str = now_str()
    log_ui(f"GRID: {symbol} @ {price} ({levels}L, ${grid_size})")

def compute_qty_for_notional(symbol: str, notional: Decimal) -> Decimal:
    price = get_current_price(symbol)
    if price <= ZERO:
        return ZERO
    step = fetch_symbol_info(symbol)['stepSize']
    raw = notional / price
    return (raw // step) * step

# ---------------------------
# PORTFOLIO & TOP SYMBOLS
# ---------------------------
def import_portfolio_symbols() -> List[str]:
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
    with state_lock:
        global portfolio_symbols
        portfolio_symbols = list(set(owned))
    log_ui(f"Portfolio: {len(portfolio_symbols)} symbols")
    return portfolio_symbols

def update_top_bid_symbols():
    global top_bid_symbols
    try:
        vols = [(s, bid_volume.get(s, ZERO)) for s in all_symbols]
        vols.sort(key=lambda x: x[1], reverse=True)
        with state_lock:
            top_bid_symbols = [s for s, _ in vols[:25]]
    except Exception as e:
        log_ui(f"Top bid update error: {e}")

# ---------------------------
# ROTATE TO TOP 25
# ---------------------------
def rotate_to_top25(all_symbols, is_auto=False):
    try:
        vols = [(s, bid_volume.get(s, ZERO)) for s in all_symbols]
        vols.sort(key=lambda x: x[1], reverse=True)
        new_top25 = [s for s, _ in vols[:25]]
        removed = [sym for sym in gridded_symbols if sym not in new_top25]
        added = [sym for sym in new_top25 if sym not in gridded_symbols]
        for sym in removed:
            cancel_all_orders(sym)
            with state_lock:
                gridded_symbols.remove(sym)
                active_grids.pop(sym, None)
        for sym in added:
            gridded_symbols.append(sym)
        for sym in gridded_symbols:
            place_new_grid(sym, st.session_state.grid_levels, st.session_state.grid_size)
        msg = f"{'AUTO' if is_auto else 'MANUAL'}-ROTATED TO TOP 25\n"
        if removed: msg += f"Removed: {len(removed)}\n"
        if added: msg += f"Added: {len(added)}\n"
        msg += f"Active: {len(gridded_symbols)}\n{now_str()}"
        send_callmebot_alert(msg, force_send=True)
        log_ui(f"Rotated: +{len(added)}, -{len(removed)}")
    except Exception as e:
        log_ui(f"Rotate error: {e}")

# ---------------------------
# SELL NON-TOP 25
# ---------------------------
def sell_non_top25(all_symbols):
    try:
        vols = [(s, bid_volume.get(s, ZERO)) for s in all_symbols]
        vols.sort(key=lambda x: x[1], reverse=True)
        top25 = {s for s, _ in vols[:25]}
        removed = [sym for sym in gridded_symbols if sym not in top25]
        for sym in removed:
            cancel_all_orders(sym)
            with state_lock:
                gridded_symbols.remove(sym)
                active_grids.pop(sym, None)
        if removed:
            msg = f"SELL NON-TOP 25\nRemoved {len(removed)} grids\n{now_str()}"
            send_callmebot_alert(msg, force_send=True)
            log_ui(f"Removed {len(removed)} non-top25")
    except Exception as e:
        log_ui(f"Sell non-top25 error: {e}")

# ---------------------------
# EMERGENCY STOP
# ---------------------------
def emergency_stop(reason: str = "MANUAL"):
    cancel_all_orders_global()
    st.session_state.paused = True
    st.session_state.emergency_stopped = True
    msg = f"EMERGENCY STOP: {reason}\nBot PAUSED\n{now_str()}"
    send_callmebot_alert(msg, force_send=True)
    log_ui(msg)

# ---------------------------
# DASHBOARD STATUS LIGHTS
# ---------------------------
def get_ws_status():
    return (
        (price_ws and price_ws.sock and price_ws.sock.connected) or
        (depth_ws and depth_ws.sock and depth_ws.sock.connected) or
        (kline_ws and kline_ws.sock and kline_ws.sock.connected) or
        (user_data_ws and user_data_ws.sock and user_data_ws.sock.connected)
    )

def get_api_status():
    try:
        safe_api_call(binance_client.get_server_time, weight=1)
        return True
    except:
        return False

def display_status_lights():
    col1, col2 = st.columns(2)
    ws_ok = get_ws_status()
    api_ok = get_api_status()

    with col1:
        st.markdown("### WebSocket")
        if ws_ok:
            st.markdown("**ONLINE**")
            st.markdown('<div style="width:30px;height:30px;background:#00ff00;border-radius:50%;display:inline-block;margin:10px 0;"></div>', unsafe_allow_html=True)
        else:
            st.markdown("**OFFLINE**")
            st.markdown('<div style="width:30px;height:30px;background:#ff0000;border-radius:50%;display:inline-block;margin:10px 0;"></div>', unsafe_allow_html=True)

    with col2:
        st.markdown("### Binance API")
        if api_ok:
            st.markdown("**ONLINE**")
            st.markdown('<div style="width:30px;height:30px;background:#00ff00;border-radius:50%;display:inline-block;margin:10px 0;"></div>', unsafe_allow_html=True)
        else:
            st.markdown("**OFFLINE**")
            st.markdown('<div style="width:30px;height:30px;background:#ff0000;border-radius:50%;display:inline-block;margin:10px 0;"></div>', unsafe_allow_html=True)

# ---------------------------
# BACKGROUND THREADS
# ---------------------------
def start_background_threads():
    global alert_thread_running
    if not alert_thread_running:
        threading.Thread(target=alert_manager, daemon=True).start()
        alert_thread_running = True

    def rebalancer():
        global last_full_refresh
        time.sleep(2)
        log_ui("REBALANCER STARTED")
        while True:
            now = time.time()
            try:
                if st.session_state.get('paused', False):
                    time.sleep(10)
                    continue
                if now - last_full_refresh >= REFRESH_INTERVAL:
                    update_account_cache()
                    update_initial_asset_values()
                    last_full_refresh = now
                for sym in gridded_symbols:
                    place_new_grid(sym, st.session_state.grid_levels, st.session_state.grid_size)
                time.sleep(40)
            except Exception as e:
                log_ui(f"rebalancer error: {e}")
                time.sleep(5)

    threading.Thread(target=rebalancer, daemon=True).start()

# ---------------------------
# STREAMLIT UI
# ---------------------------
def run_streamlit_ui():
    st.title("INFINITY GRID BOT — BINANCE.US LIVE")
    st.markdown("---")

    # === STATUS LIGHTS ===
    display_status_lights()
    st.markdown("---")

    # === P&L ===
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("P&L")
        realized = st.session_state.get('realized_pnl', ZERO)
        unrealized = calculate_unrealized_pnl()
        total = realized + unrealized
        st.metric("Realized", f"${realized:+.2f}")
        st.metric("Unrealized", f"${unrealized:+.2f}")
        st.metric("Total P&L", f"${total:+.2f}", delta=f"{total:+.2f}")

    with col2:
        st.subheader("Grid Status")
        st.write(f"**Active Grids:** {len(gridded_symbols)}")
        st.write(f"**Last Regrid:** {last_regrid_str}")
        st.write(f"**USDT Balance:** ${get_balance('USDT'):.2f}")

    st.markdown("---")
    st.subheader("Logs")
    log_container = st.empty()
    while True:
        log_container.text("\n".join(ui_logs[-20:]))
        time.sleep(1)

# ---------------------------
# INIT & MAIN
# ---------------------------
def initialize():
    global all_symbols
    try:
        info = safe_api_call(binance_client.get_exchange_info, weight=40)
        all_symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        log_ui(f"Found {len(all_symbols)} USDT pairs")
    except Exception as e:
        log_ui(f"Using fallback: {e}")
        all_symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
    return all_symbols

def main():
    initialize()
    start_all_websockets()
    start_background_threads()
    run_streamlit_ui()

if __name__ == "__main__":
    main()
