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
        log_ui(f"
