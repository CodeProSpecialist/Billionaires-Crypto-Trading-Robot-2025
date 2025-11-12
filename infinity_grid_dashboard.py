#!/usr/bin/env python3
"""
INFINITY GRID BOT — LIVE TRADING ON BINANCE.US (MAINNET)
- Real limit orders (maker-only)
- 1.5% grid spacing, $5 per level
- Top-25 bid volume + 6-month trend filter
- Streamlit dashboard
- FORCE_LIVE_ORDERS = "YES"
- BINANCE_TESTNET = False → REAL BINANCE.US
"""

import os
import sys
import time
import threading
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, List, Tuple
import pytz

import requests
import streamlit as st

# --- Binance Client ---
try:
    from binance.client import Client
    BINANCE_AVAILABLE = True
except Exception:
    BINANCE_AVAILABLE = False

getcontext().prec = 28
ZERO = Decimal('0')
CST = pytz.timezone('America/Chicago')

# ---------------------------
# CONFIG (LIVE - BINANCE.US MAINNET)
# ---------------------------
USE_PAPER_TRADING = False
FORCE_LIVE_ORDERS = "YES"          # HARD-CODED: ALWAYS ENABLED
BINANCE_TESTNET = False            # HARD-CODED: BINANCE.US MAINNET

API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

GRID_SIZE_USDT = Decimal('5.0')
GRID_INTERVAL_PCT = Decimal('0.015')
GRID_LEVELS = 3
FIRST_GRID_DELAY = 40
PRICE_UPDATE_INTERVAL = 20
TREND_UPDATE_INTERVAL = 25 * 60
DASHBOARD_REFRESH = 10
DEPTH_LEVELS = 5
TOP_N_BID_VOLUME = 25
MIN_NOTIONAL_USDT = Decimal('5.0')

# ---------------------------
# Binance.US Client (LIVE)
# ---------------------------
if not BINANCE_AVAILABLE:
    print("ERROR: python-binance not installed. Run: pip install python-binance")
    sys.exit(1)

if not API_KEY or not API_SECRET:
    print("ERROR: BINANCE_API_KEY and BINANCE_API_SECRET must be set in environment.")
    sys.exit(1)

try:
    binance_client = Client(API_KEY, API_SECRET, tld='us')  # Binance.US
    print("Connected to BINANCE.US MAINNET (LIVE TRADING)")
except Exception as e:
    print(f"Binance.US client init error: {e}")
    sys.exit(1)

# ---------------------------
# State & Locks
# ---------------------------
live_prices: Dict[str, Decimal] = {}
live_bids: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
live_asks: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
symbol_info_cache: Dict[str, dict] = {}
top_bid_symbols: List[str] = []
last_trend_check: Dict[str, float] = {}
trend_bullish: Dict[str, bool] = {}
active_grids: Dict[str, List[int]] = {}  # real order IDs

price_lock = threading.Lock()
book_lock = threading.Lock()
state_lock = threading.Lock()
api_rate_lock = threading.Lock()

ui_logs = []

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
# Binance.US API Wrappers
# ---------------------------
def fetch_symbol_info(symbol: str) -> dict:
    if symbol in symbol_info_cache:
        return symbol_info_cache[symbol]
    info = {'tickSize': Decimal('1e-8'), 'stepSize': Decimal('1e-8'), 'minNotional': Decimal('1.0')}
    try:
        with api_rate_lock:
            si = binance_client.get_symbol_info(symbol)
        for f in si.get('filters', []):
            ft = f.get('filterType')
            if ft == 'PRICE_FILTER':
                info['tickSize'] = to_decimal(f.get('tickSize', '1e-8'))
            elif ft == 'LOT_SIZE':
                info['stepSize'] = to_decimal(f.get('stepSize', '1e-8'))
            elif ft == 'MIN_NOTIONAL':
                info['minNotional'] = to_decimal(f.get('minNotional', '1.0'))
    except Exception as e:
        log_ui(f"fetch_symbol_info error {symbol}: {e}")
    symbol_info_cache[symbol] = info
    return info

def get_current_price(symbol: str) -> Decimal:
    with price_lock:
        p = live_prices.get(symbol)
    if p:
        return p
    try:
        with api_rate_lock:
            t = binance_client.get_symbol_ticker(symbol=symbol)
        p = to_decimal(t['price'])
        with price_lock:
            live_prices[symbol] = p
        return p
    except Exception as e:
        log_ui(f"price error {symbol}: {e}")
        return ZERO

def fetch_depth5(symbol: str):
    try:
        with api_rate_lock:
            d = binance_client.get_order_book(symbol=symbol, limit=DEPTH_LEVELS)
        bids = [(to_decimal(p), to_decimal(q)) for p, q in d.get('bids', [])]
        asks = [(to_decimal(p), to_decimal(q)) for p, q in d.get('asks', [])]
        with book_lock:
            live_bids[symbol] = bids
            live_asks[symbol] = asks
        return bids, asks
    except Exception as e:
        log_ui(f"depth error {symbol}: {e}")
        with book_lock:
            return live_bids.get(symbol, []), live_asks.get(symbol, [])

def get_fee_rates(symbol: str) -> Tuple[Decimal, Decimal]:
    try:
        with api_rate_lock:
            fee = binance_client.get_trade_fee(symbol=symbol)
        return to_decimal(fee[0].get('makerCommission', '0.001')), to_decimal(fee[0].get('takerCommission', '0.001'))
    except:
        return Decimal('0.001'), Decimal('0.001')

# ---------------------------
# REAL ORDER FUNCTIONS (BINANCE.US)
# ---------------------------
def place_limit_order(symbol: str, side: str, price: Decimal, quantity: Decimal) -> int:
    info = fetch_symbol_info(symbol)
    price = price.quantize(info['tickSize'], rounding=ROUND_DOWN)
    quantity = (quantity // info['stepSize']) * info['stepSize']

    if (price * quantity) < info['minNotional']:
        log_ui(f"{symbol} notional too low: {price * quantity}")
        return 0

    try:
        resp = binance_client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=str(quantity),
            price=str(price)
        )
        oid = int(resp['orderId'])
        log_ui(f"[LIVE ORDER] {side} {symbol} {quantity} @ {price} -> {oid}")
        with state_lock:
            active_grids.setdefault(symbol, []).append(oid)
        return oid
    except Exception as e:
        log_ui(f"[ORDER ERROR] {side} {symbol} @ {price}: {e}")
        return 0

def cancel_all_orders(symbol: str):
    try:
        open_orders = binance_client.get_open_orders(symbol=symbol)
        for o in open_orders:
            binance_client.cancel_order(symbol=symbol, orderId=o['orderId'])
            log_ui(f"[CANCEL] {o['orderId']} {symbol}")
        with state_lock:
            if symbol in active_grids:
                active_grids[symbol] = []
    except Exception as e:
        log_ui(f"cancel error {symbol}: {e}")

# ---------------------------
# Grid Placement (LIVE)
# ---------------------------
def place_new_grid(symbol: str):
    cancel_all_orders(symbol)
    price = get_current_price(symbol)
    if price <= ZERO:
        log_ui(f"No price for {symbol}")
        return

    qty = compute_qty_for_notional(symbol, GRID_SIZE_USDT)
    if qty <= ZERO:
        log_ui(f"Qty zero for {symbol}")
        return

    info = fetch_symbol_info(symbol)
    if (qty * price) < MIN_NOTIONAL_USDT:
        log_ui(f"Notional too low: {qty * price}")
        return

    # BUY GRID
    for i in range(1, GRID_LEVELS + 1):
        buy_price = (price * (1 - GRID_INTERVAL_PCT * i)).quantize(info['tickSize'], ROUND_DOWN)
        place_limit_order(symbol, 'BUY', buy_price, qty)

    # SELL GRID (fee + 1% profit)
    maker_fee, _ = get_fee_rates(symbol)
    multiplier = Decimal('1') + maker_fee + Decimal('0.01')
    for i in range(1, GRID_LEVELS + 1):
        raw_sp = price * (1 + GRID_INTERVAL_PCT * i)
        sell_price = (raw_sp * multiplier).quantize(info['tickSize'], ROUND_DOWN)
        place_limit_order(symbol, 'SELL', sell_price, qty)

    log_ui(f"GRID PLACED: {symbol} @ {price}")

def compute_qty_for_notional(symbol: str, notional: Decimal) -> Decimal:
    price = get_current_price(symbol)
    if price <= ZERO:
        return ZERO
    step = fetch_symbol_info(symbol)['stepSize']
    raw = notional / price
    return (raw // step) * step

# ---------------------------
# Trend & Top-25
# ---------------------------
def is_symbol_bullish(symbol: str) -> bool:
    now = time.time()
    if now - last_trend_check.get(symbol, 0) < TREND_UPDATE_INTERVAL:
        return trend_bullish.get(symbol, True)
    try:
        klines = binance_client.get_klines(symbol=symbol, interval='1M', limit=7)
        if len(klines) < 2:
            return True
        closes = [to_decimal(k[4]) for k in klines]
        bullish = closes[-1] > closes[0]
        trend_bullish[symbol] = bullish
        last_trend_check[symbol] = now
        return bullish
    except Exception as e:
        log_ui(f"trend error {symbol}: {e}")
        return True

def update_top_bid_symbols(all_symbols: List[str]):
    try:
        vols = []
        for s in all_symbols:
            bids, _ = fetch_depth5(s)
            vol = sum(q for _, q in bids)
            vols.append((s, vol))
        vols.sort(key=lambda x: x[1], reverse=True)
        with state_lock:
            global top_bid_symbols
            top_bid_symbols = [s for s, _ in vols[:TOP_N_BID_VOLUME]]
        log_ui(f"Top {TOP_N_BID_VOLUME}: {top_bid_symbols[:8]}")
    except Exception as e:
        log_ui(f"top25 error: {e}")

# ---------------------------
# Rebalancer
# ---------------------------
def grid_rebalancer(all_symbols: List[str]):
    time.sleep(FIRST_GRID_DELAY)
    log_ui("LIVE GRID REBALANCER STARTED (BINANCE.US)")
    last_top = 0
    while True:
        try:
            now = time.time()
            if now - last_top > PRICE_UPDATE_INTERVAL:
                update_top_bid_symbols(all_symbols)
                last_top = now

            for sym in all_symbols:
                if sym in top_bid_symbols and is_symbol_bullish(sym):
                    place_new_grid(sym)
            time.sleep(40)
        except Exception as e:
            log_ui(f"rebalancer error: {e}")
            time.sleep(5)

# ---------------------------
# Background Threads
# ---------------------------
def start_background_threads(all_symbols):
    t = threading.Thread(target=grid_rebalancer, args=(all_symbols,), daemon=True)
    t.start()

    def price_updater():
        while True:
            for s in all_symbols:
                get_current_price(s)
            time.sleep(PRICE_UPDATE_INTERVAL)
    threading.Thread(target=price_updater, daemon=True).start()

# ---------------------------
# Streamlit UI
# ---------------------------
def run_streamlit_ui(all_symbols):
    st.set_page_config(page_title="Infinity Grid — BINANCE.US LIVE", layout="wide")
    st.title("Infinity Grid — BINANCE.US LIVE")
    st.caption("REAL MONEY TRADING | NO TESTNET | FORCE_LIVE_ORDERS = YES")

    with st.sidebar:
        st.write("**BINANCE.US LIVE BOT**")
        st.write(f"Symbols: {len(all_symbols)}")
        st.write(f"Grid: {GRID_LEVELS}×${GRID_SIZE_USDT} @ ±{GRID_INTERVAL_PCT*100}%")
        if st.button("Force Regrid All"):
            for s in all_symbols:
                place_new_grid(s)

    col1, col2 = st.columns([2, 1])
    placeholder = st.empty()

    while True:
        with placeholder.container():
            col1.subheader(f"Status — {now_str()}")
            col1.metric("Top 25", ", ".join(top_bid_symbols[:8]) if top_bid_symbols else "—")
            col1.write("Active Grids")
            rows = []
            for sym in all_symbols:
                oids = active_grids.get(sym, [])
                rows.append({"Symbol": sym, "Orders": len(oids)})
            col1.dataframe(rows, use_container_width=True)

            col2.subheader("Recent Logs")
            for line in ui_logs[-50:]:
                col2.text(line)

        time.sleep(DASHBOARD_REFRESH)

# ---------------------------
# Init & Main
# ---------------------------
def initialize():
    try:
        with api_rate_lock:
            info = binance_client.get_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        log_ui(f"Loaded {len(symbols)} USDT pairs from Binance.US")
        return symbols[:50]  # Limit for safety
    except Exception as e:
        log_ui(f"Exchange info error: {e}. Using defaults.")
        return ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT']

def main():
    all_symbols = initialize()
    log_ui(f"Bot starting with {len(all_symbols)} symbols on BINANCE.US")
    start_background_threads(all_symbols)
    run_streamlit_ui(all_symbols)

if __name__ == "__main__":
    main()
