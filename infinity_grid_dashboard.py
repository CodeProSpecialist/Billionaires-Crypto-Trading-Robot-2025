#!/usr/bin/env python3
"""
INFINITY GRID BOT — LIVE TRADING ON BINANCE.US
- FULL INPUT VALIDATION
- P&L, Grid Size ($5–$50), Levels (1–5), Symbols (1–50)
- Portfolio Priority
- CALLMEBOT WhatsApp Alerts
- No PRICE_FILTER, Safe Orders
"""

import os
import sys
import time
import threading
import requests
import re
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, List, Tuple
import pytz

import streamlit as st

# --------------------------------------------------------------
# 1. SET PAGE CONFIG FIRST
# --------------------------------------------------------------
st.set_page_config(page_title="Infinity Grid — BINANCE.US LIVE", layout="wide")

# --------------------------------------------------------------
# 2. Binance Client
# --------------------------------------------------------------
try:
    from binance.client import Client
    BINANCE_AVAILABLE = True
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

# Validate API Keys
if not API_KEY or not API_SECRET:
    st.error("BINANCE_API_KEY and BINANCE_API_SECRET are required.")
    st.stop()

# Validate CallMeBot
if CALLMEBOT_PHONE:
    if not re.match(r'^\+\d{7,15}$', CALLMEBOT_PHONE):
        st.error("CALLMEBOT_PHONE must be in international format: +1234567890")
        st.stop()
    if not CALLMEBOT_API_KEY:
        st.error("CALLMEBOT_API_KEY is required if CALLMEBOT_PHONE is set.")
        st.stop()
else:
    CALLMEBOT_API_KEY = None

# ---------------------------
# Binance Client
# ---------------------------
try:
    binance_client = Client(API_KEY, API_SECRET, tld='us')
    st.success("Connected to BINANCE.US MAINNET")
except Exception as e:
    st.error(f"Binance connection failed: {e}")
    st.stop()

# ---------------------------
# Balance & Validation
# ---------------------------
try:
    account = binance_client.get_account()
    usdt_bal = next((a for a in account['balances'] if a['asset'] == 'USDT'), None)
    if usdt_bal:
        free = Decimal(usdt_bal['free'])
        locked = Decimal(usdt_bal['locked'])
        total = free + locked
        st.metric("USDT Balance", f"{total:.2f}", f"{free:.2f} free")
        if total < Decimal('50'):
            st.warning("Low balance. Need $50+ for safe gridding.")
except Exception as e:
    st.warning(f"Balance check failed: {e}")

# ---------------------------
# State & Locks
# ---------------------------
live_prices: Dict[str, Decimal] = {}
symbol_info_cache: Dict[str, dict] = {}
top_bid_symbols: List[str] = []
gridded_symbols: List[str] = []
portfolio_symbols: List[str] = []
active_grids: Dict[str, List[int]] = {}

price_lock = threading.Lock()
state_lock = threading.Lock()
api_rate_lock = threading.Lock()

ui_logs = []
initial_balance: Decimal = ZERO

# ---------------------------
# CALLMEBOT ALERTS (VALIDATED)
# ---------------------------
def send_callmebot_alert(message: str):
    if not CALLMEBOT_PHONE or not CALLMEBOT_API_KEY:
        return
    url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={message}&apikey={CALLMEBOT_API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            log_ui(f"Alert sent: {message[:50]}...")
        else:
            log_ui(f"Alert failed: {r.status_code}")
    except Exception as e:
        log_ui(f"Alert error: {e}")

def alert_grid_update(gridded_symbols: List[str], grid_levels: int, grid_size: Decimal):
    if not gridded_symbols:
        return
    msg = f"Grid Update:\n"
    msg += f"{len(gridded_symbols)} symbols | {grid_levels} levels | ${grid_size}/level\n\n"
    msg += "Active:\n"
    for i, sym in enumerate(gridded_symbols[:10], 1):
        msg += f"{i}. {sym}\n"
    if len(gridded_symbols) > 10:
        msg += f"... +{len(gridded_symbols)-10} more"
    send_callmebot_alert(msg)

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
# API Wrappers (Validated)
# ---------------------------
def fetch_symbol_info(symbol: str) -> dict:
    if symbol in symbol_info_cache:
        return symbol_info_cache[symbol]
    info = {'tickSize': Decimal('1e-8'), 'stepSize': Decimal('1e-8'), 'minNotional': Decimal('10.0')}
    try:
        with api_rate_lock:
            si = binance_client.get_symbol_info(symbol)
        if not si:
            log_ui(f"Symbol {symbol} not found")
            symbol_info_cache[symbol] = info
            return info
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
        p = live_prices.get(symbol)
    if p and p > ZERO:
        return p
    try:
        with api_rate_lock:
            t = binance_client.get_symbol_ticker(symbol=symbol)
        p = to_decimal(t['price'])
        if p > ZERO:
            with price_lock:
                live_prices[symbol] = p
            return p
    except Exception as e:
        log_ui(f"Price fetch failed {symbol}: {e}")
    return ZERO

# ---------------------------
# ORDER PLACEMENT (FULLY VALIDATED)
# ---------------------------
def place_limit_order(symbol: str, side: str, raw_price: Decimal, raw_qty: Decimal) -> int:
    if raw_price <= 0 or raw_qty <= 0:
        return 0

    info = fetch_symbol_info(symbol)
    price = (raw_price // info['tickSize']) * info['tickSize']
    qty = (raw_qty // info['stepSize']) * info['stepSize']

    notional = price * qty
    if notional < info['minNotional']:
        log_ui(f"SKIP {side} {symbol}: notional {notional} < min {info['minNotional']}")
        return 0
    if price <= 0 or qty <= 0:
        log_ui(f"SKIP {side} {symbol}: invalid after rounding")
        return 0

    try:
        resp = binance_client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=str(qty),
            price=str(price)
        )
        oid = int(resp['orderId'])
        log_ui(f"LIVE ORDER: {side} {symbol} {qty} @ {price} -> {oid}")
        with state_lock:
            active_grids.setdefault(symbol, []).append(oid)
        return oid
    except Exception as e:
        log_ui(f"ORDER FAILED {side} {symbol}: {e}")
        return 0

def cancel_all_orders(symbol: str):
    try:
        open_orders = binance_client.get_open_orders(symbol=symbol)
        for o in open_orders:
            binance_client.cancel_order(symbol=symbol, orderId=o['orderId'])
            log_ui(f"CANCEL {o['orderId']} {symbol}")
        with state_lock:
            active_grids[symbol] = []
    except Exception as e:
        log_ui(f"cancel error {symbol}: {e}")

# ---------------------------
# Grid Logic
# ---------------------------
def place_new_grid(symbol: str, levels: int, grid_size: Decimal):
    if levels < 1 or levels > 5 or grid_size < 5 or grid_size > 50:
        log_ui(f"Invalid grid params: levels={levels}, size=${grid_size}")
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

    for i in range(1, levels + 1):
        raw_buy = price * (1 - Decimal('0.015') * i)
        place_limit_order(symbol, 'BUY', raw_buy, qty)

    maker_fee, _ = get_fee_rates(symbol)
    multiplier = Decimal('1') + maker_fee + Decimal('0.01')
    for i in range(1, levels + 1):
        raw_sell = price * (1 + Decimal('0.015') * i) * multiplier
        place_limit_order(symbol, 'SELL', raw_sell, qty)

    log_ui(f"GRID ACTIVE: {symbol} @ {price} ({levels}L, ${grid_size})")

def compute_qty_for_notional(symbol: str, notional: Decimal) -> Decimal:
    price = get_current_price(symbol)
    if price <= ZERO:
        return ZERO
    step = fetch_symbol_info(symbol)['stepSize']
    raw = notional / price
    return (raw // step) * step

def get_fee_rates(symbol: str) -> Tuple[Decimal, Decimal]:
    try:
        with api_rate_lock:
            fee = binance_client.get_trade_fee(symbol=symbol)
        return to_decimal(fee[0].get('makerCommission', '0.001')), to_decimal(fee[0].get('takerCommission', '0.001'))
    except:
        return Decimal('0.001'), Decimal('0.001')

# ---------------------------
# PORTFOLIO IMPORT
# ---------------------------
def import_portfolio_symbols() -> List[str]:
    try:
        account = binance_client.get_account()
        balances = account['balances']
        owned = []
        for bal in balances:
            asset = bal['asset']
            free = Decimal(bal['free'])
            locked = Decimal(bal['locked'])
            if free > ZERO or locked > ZERO:
                if asset == 'USDT':
                    log_ui(f"Portfolio: USDT {free + locked}")
                else:
                    symbol = f"{asset}USDT"
                    try:
                        binance_client.get_symbol_ticker(symbol=symbol)
                        owned.append(symbol)
                        log_ui(f"Portfolio: {symbol} {free + locked}")
                    except:
                        pass
        with state_lock:
            global portfolio_symbols
            portfolio_symbols = list(set(owned))
        log_ui(f"Imported {len(portfolio_symbols)} owned symbols")
        return portfolio_symbols
    except Exception as e:
        log_ui(f"Portfolio import failed: {e}")
        return []

# ---------------------------
# Top Symbols & Update
# ---------------------------
def update_top_bid_symbols(all_symbols: List[str], target_grid_count: int):
    if target_grid_count < 1 or target_grid_count > 50:
        return
    global top_bid_symbols, gridded_symbols
    try:
        vols = []
        for s in all_symbols:
            try:
                bids, _ = binance_client.get_order_book(symbol=s, limit=5)
                vol = sum(to_decimal(q) for _, q in bids.get('bids', []))
                vols.append((s, vol))
            except:
                continue
        vols.sort(key=lambda x: x[1], reverse=True)
        with state_lock:
            top_bid_symbols = [s for s, _ in vols[:25]]
            remaining = [s for s in top_bid_symbols if s not in portfolio_symbols]
            gridded_symbols = portfolio_symbols + remaining[:target_grid_count - len(portfolio_symbols)]
            if len(gridded_symbols) > target_grid_count:
                gridded_symbols = gridded_symbols[:target_grid_count]
        log_ui(f"Gridding {len(gridded_symbols)} symbols")
        grid_size = st.session_state.get('grid_size', Decimal('20'))
        grid_levels = st.session_state.get('grid_levels', 1)
        alert_grid_update(gridded_symbols, grid_levels, grid_size)
    except Exception as e:
        log_ui(f"top update error: {e}")

# ---------------------------
# Rebalancer
# ---------------------------
def grid_rebalancer(all_symbols: List[str], target_grid_count: int, grid_levels: int, grid_size: Decimal):
    time.sleep(2)
    log_ui(f"REBALANCER STARTED: {target_grid_count}S, {grid_levels}L, ${grid_size}")
    last_top = 0
    while True:
        try:
            now = time.time()
            if now - last_top > 20:
                update_top_bid_symbols(all_symbols, target_grid_count)
                last_top = now
            for sym in gridded_symbols:
                place_new_grid(sym, grid_levels, grid_size)
            time.sleep(40)
        except Exception as e:
            log_ui(f"rebalancer error: {e}")
            time.sleep(5)

# ---------------------------
# Background Threads
# ---------------------------
def start_background_threads(all_symbols, target_grid_count, grid_levels, grid_size):
    threading.Thread(target=grid_rebalancer, args=(all_symbols, target_grid_count, grid_levels, grid_size), daemon=True).start()

    def price_updater():
        while True:
            for s in all_symbols:
                get_current_price(s)
            time.sleep(20)
    threading.Thread(target=price_updater, daemon=True).start()

    def pnl_updater():
        while True:
            update_pnl()
            time.sleep(30)
    threading.Thread(target=pnl_updater, daemon=True).start()

def update_pnl():
    global initial_balance
    try:
        account = binance_client.get_account()
        current_usdt = Decimal('0')
        for bal in account['balances']:
            asset = bal['asset']
            free = Decimal(bal['free'])
            locked = Decimal(bal['locked'])
            if asset == 'USDT':
                current_usdt += free + locked
            else:
                symbol = f"{asset}USDT"
                if symbol in live_prices:
                    current_usdt += (free + locked) * live_prices[symbol]
        if initial_balance == ZERO:
            initial_balance = current_usdt
        st.session_state.realized_pnl = current_usdt - initial_balance
    except Exception as e:
        log_ui(f"PnL error: {e}")

# ---------------------------
# Streamlit UI (VALIDATED)
# ---------------------------
def run_streamlit_ui(all_symbols):
    st.title("Infinity Grid — BINANCE.US LIVE")
    st.caption("Validated | P&L | Grid Size | Levels | Symbols | Alerts")

    with st.sidebar:
        st.header("Settings")

        grid_size = st.slider("Money per Grid Level ($)", 5, 50, 20, 5)
        st.session_state.grid_size = Decimal(grid_size)

        grid_levels = st.slider("GRID_LEVELS (per side)", 1, 5, 1, 1)
        st.session_state.grid_levels = grid_levels

        target_grid_count = st.slider("NUMBER_OF_SYMBOLS_TO_GRID", 1, 50, 11, 1)
        st.session_state.target_grid_count = target_grid_count

        st.write(f"**Grid Size:** ${grid_size}")
        st.write(f"**Levels:** {grid_levels}")
        st.write(f"**Symbols:** {target_grid_count}")

        if CALLMEBOT_PHONE:
            st.success("CALLMEBOT Enabled")
        else:
            st.info("Set CALLMEBOT_PHONE for alerts")

        if st.button("Force Regrid"):
            for s in gridded_symbols:
                place_new_grid(s, grid_levels, Decimal(grid_size))
            alert_grid_update(gridded_symbols, grid_levels, Decimal(grid_size))
            st.success("Regrid + Alert!")

    col1, col2 = st.columns([2, 1])
    placeholder = st.empty()

    if 'initialized' not in st.session_state:
        st.session_state.initialized = True
        st.session_state.realized_pnl = Decimal('0')
        import_portfolio_symbols()
        start_background_threads(all_symbols, target_grid_count, grid_levels, Decimal(grid_size))

    if (st.session_state.get('prev_size') != Decimal(grid_size) or
        st.session_state.get('prev_levels') != grid_levels or
        st.session_state.get('prev_symbols') != target_grid_count):
        st.session_state.prev_size = Decimal(grid_size)
        st.session_state.prev_levels = grid_levels
        st.session_state.prev_symbols = target_grid_count
        update_top_bid_symbols(all_symbols, target_grid_count)

    while True:
        with placeholder.container():
            col1.subheader(f"Status — {now_str()}")
            col1.metric("Grid Size", f"${grid_size}")
            col1.metric("Levels", grid_levels)
            col1.metric("Symbols", target_grid_count)
            col1.metric("Gridded", len(gridded_symbols))

            realized = st.session_state.realized_pnl
            col1.metric("P&L", f"${realized:+.2f}", delta=f"{realized:+.2f}")

            if portfolio_symbols:
                col1.write("**Portfolio**")
                col1.dataframe([{"Symbol": s} for s in portfolio_symbols], use_container_width=True)

            col1.write("**Active Grids**")
            col1.dataframe([
                {"Symbol": s, "Orders": len(active_grids.get(s, [])), "Size": f"${grid_size}"}
                for s in gridded_symbols
            ], use_container_width=True)

            col2.subheader("Logs")
            for line in ui_logs[-30:]:
                col2.text(line)

        time.sleep(10)

# ---------------------------
# Init & Main
# ---------------------------
def initialize():
    try:
        with api_rate_lock:
            info = binance_client.get_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        log_ui(f"Found {len(symbols)} USDT pairs")
        return symbols
    except Exception as e:
        log_ui(f"Using fallback: {e}")
        return ['BTCUSDT', 'ETHUSDT']

def main():
    all_symbols = initialize()
    run_streamlit_ui(all_symbols)

if __name__ == "__main__":
    main()
