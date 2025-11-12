#!/usr/bin/env python3
"""
INFINITY GRID BOT — LIVE TRADING ON BINANCE.US
- P&L Tracking (Realized + Unrealized)
- Grid Levels Slider (1–5)
- NUMBER_OF_SYMBOLS_TO_GRID Slider (1–50)
- Portfolio Import + Priority
- CALLMEBOT WhatsApp Alerts (grid updates)
- $20/level, no PRICE_FILTER
- Real-time Streamlit Dashboard
"""

import os
import sys
import time
import threading
import requests
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, List, Tuple
import pytz

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
# CONFIG (LIVE - BINANCE.US)
# ---------------------------
USE_PAPER_TRADING = False
FORCE_LIVE_ORDERS = "YES"
BINANCE_TESTNET = False

API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')  # e.g., '+1234567890'
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')  # From bot activation

GRID_SIZE_USDT = Decimal('20.0')
GRID_INTERVAL_PCT = Decimal('0.015')
FIRST_GRID_DELAY = 2
PRICE_UPDATE_INTERVAL = 20
TREND_UPDATE_INTERVAL = 25 * 60
DASHBOARD_REFRESH = 10
DEPTH_LEVELS = 5
TOP_N_BID_VOLUME = 25
MIN_NOTIONAL_USDT = Decimal('10.0')

# ---------------------------
# Binance.US Client + Balance Check
# ---------------------------
if not BINANCE_AVAILABLE:
    st.error("Install python-binance: `pip install python-binance`")
    st.stop()

if not API_KEY or not API_SECRET:
    st.error("Set BINANCE_API_KEY and BINANCE_API_SECRET")
    st.stop()

try:
    binance_client = Client(API_KEY, API_SECRET, tld='us')
    st.success("Connected to BINANCE.US MAINNET")
except Exception as e:
    st.error(f"Client error: {e}")
    st.stop()

# --- BALANCE CHECK ---
try:
    account = binance_client.get_account()
    usdt_bal = next((a for a in account['balances'] if a['asset'] == 'USDT'), None)
    if usdt_bal:
        free = Decimal(usdt_bal['free'])
        locked = Decimal(usdt_bal['locked'])
        st.metric("USDT Balance", f"{free + locked:.2f}", f"{free:.2f} free")
        if free < Decimal('60'):
            st.warning("Low balance. Need $60+ for 11 symbols.")
except Exception as e:
    st.warning(f"Balance check failed: {e}")

# ---------------------------
# State & Locks
# ---------------------------
live_prices: Dict[str, Decimal] = {}
live_bids: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
live_asks: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
symbol_info_cache: Dict[str, dict] = {}
top_bid_symbols: List[str] = []
gridded_symbols: List[str] = []
portfolio_symbols: List[str] = []
last_trend_check: Dict[str, float] = {}
trend_bullish: Dict[str, bool] = {}
active_grids: Dict[str, List[int]] = {}

# P&L TRACKING
realized_pnl: Dict[str, Decimal] = {}  # symbol -> realized profit
unrealized_pnl: Dict[str, Decimal] = {}  # symbol -> current open P&L
initial_balance: Decimal = ZERO

price_lock = threading.Lock()
book_lock = threading.Lock()
state_lock = threading.Lock()
api_rate_lock = threading.Lock()

ui_logs = []

# ---------------------------
# CALLMEBOT ALERTS
# ---------------------------
def send_callmebot_alert(message: str):
    """Send WhatsApp alert via CallMeBot API"""
    if not CALLMEBOT_PHONE or not CALLMEBOT_API_KEY:
        log_ui("CALLMEBOT not configured - skipping alert")
        return

    url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={message}&apikey={CALLMEBOT_API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            log_ui(f"Alert sent: {message[:50]}...")
        else:
            log_ui(f"Alert failed: {response.status_code}")
    except Exception as e:
        log_ui(f"Alert error: {e}")

def alert_grid_update(gridded_symbols: List[str], grid_levels: int):
    """Alert current grid: symbols + levels per coin"""
    if not gridded_symbols:
        return

    msg = f"🚀 Grid Update: {len(gridded_symbols)} symbols active\n"
    msg += f"Levels per coin: {grid_levels}\n\n"
    msg += "Grids:\n"
    for i, sym in enumerate(gridded_symbols[:10], 1):  # First 10
        msg += f"{i}. {sym} ({grid_levels} levels)\n"
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
# API Wrappers
# ---------------------------
def fetch_symbol_info(symbol: str) -> dict:
    if symbol in symbol_info_cache:
        return symbol_info_cache[symbol]
    info = {'tickSize': Decimal('1e-8'), 'stepSize': Decimal('1e-8'), 'minNotional': Decimal('10.0')}
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
                info['minNotional'] = to_decimal(f.get('minNotional', '10.0'))
    except Exception as e:
        log_ui(f"info error {symbol}: {e}")
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
    except:
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
    except:
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
# P&L TRACKING
# ---------------------------
def update_pnl():
    """Update realized & unrealized P&L"""
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
                    price = live_prices[symbol]
                    value = (free + locked) * price
                    current_usdt += value
        # Realized P&L = current - initial
        if initial_balance == ZERO:
            initial_balance = current_usdt
            log_ui(f"Initial balance set: ${initial_balance:.2f}")
        realized = current_usdt - initial_balance
        st.session_state.realized_pnl = realized
    except Exception as e:
        log_ui(f"PnL update failed: {e}")

# ---------------------------
# FIXED: place_limit_order
# ---------------------------
def place_limit_order(symbol: str, side: str, raw_price: Decimal, raw_qty: Decimal) -> int:
    info = fetch_symbol_info(symbol)
    price = (raw_price // info['tickSize']) * info['tickSize']
    qty = (raw_qty // info['stepSize']) * info['stepSize']
    notional = price * qty
    if notional < info['minNotional']:
        log_ui(f"SKIP {side} {symbol}: notional {notional} < min {info['minNotional']}")
        return 0
    if price <= 0 or qty <= 0:
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
def place_new_grid(symbol: str, levels: int):
    cancel_all_orders(symbol)
    price = get_current_price(symbol)
    if price <= ZERO:
        return
    qty = compute_qty_for_notional(symbol, GRID_SIZE_USDT)
    if qty <= ZERO:
        return

    info = fetch_symbol_info(symbol)
    if (qty * price) < MIN_NOTIONAL_USDT:
        return

    # BUY GRID
    for i in range(1, levels + 1):
        raw_buy = price * (1 - GRID_INTERVAL_PCT * i)
        place_limit_order(symbol, 'BUY', raw_buy, qty)

    # SELL GRID
    maker_fee, _ = get_fee_rates(symbol)
    multiplier = Decimal('1') + maker_fee + Decimal('0.01')
    for i in range(1, levels + 1):
        raw_sell = price * (1 + GRID_INTERVAL_PCT * i) * multiplier
        place_limit_order(symbol, 'SELL', raw_sell, qty)

    log_ui(f"GRID ACTIVE: {symbol} @ {price} ({levels} levels)")

def compute_qty_for_notional(symbol: str, notional: Decimal) -> Decimal:
    price = get_current_price(symbol)
    if price <= ZERO:
        return ZERO
    step = fetch_symbol_info(symbol)['stepSize']
    raw = notional / price
    return (raw // step) * step

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
                        log_ui(f"Portfolio: {asset} (no USDT pair)")
        with state_lock:
            global portfolio_symbols
            portfolio_symbols = list(set(owned))
        log_ui(f"Imported {len(portfolio_symbols)} owned symbols")
        return portfolio_symbols
    except Exception as e:
        log_ui(f"Portfolio import failed: {e}")
        return []

# ---------------------------
# Trend & Top Symbols
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
    except:
        return True

def update_top_bid_symbols(all_symbols: List[str], target_grid_count: int):
    global top_bid_symbols, gridded_symbols
    try:
        vols = []
        for s in all_symbols:
            bids, _ = fetch_depth5(s)
            vol = sum(q for _, q in bids)
            vols.append((s, vol))
        vols.sort(key=lambda x: x[1], reverse=True)
        with state_lock:
            top_bid_symbols = [s for s, _ in vols[:TOP_N_BID_VOLUME]]
            remaining = [s for s in top_bid_symbols if s not in portfolio_symbols]
            gridded_symbols = portfolio_symbols + remaining[:target_grid_count - len(portfolio_symbols)]
            if len(gridded_symbols) > target_grid_count:
                gridded_symbols = gridded_symbols[:target_grid_count]
        log_ui(f"Gridding updated to {len(gridded_symbols)} symbols")
        # Send alert on update
        alert_grid_update(gridded_symbols, st.session_state.grid_levels if 'grid_levels' in st.session_state else 1)
    except Exception as e:
        log_ui(f"top update error: {e}")

# ---------------------------
# Rebalancer
# ---------------------------
def grid_rebalancer(all_symbols: List[str], target_grid_count: int, grid_levels: int):
    time.sleep(FIRST_GRID_DELAY)
    log_ui(f"GRID REBALANCER STARTED — {target_grid_count} symbols, {grid_levels} levels")
    last_top = 0
    while True:
        try:
            now = time.time()
            if now - last_top > PRICE_UPDATE_INTERVAL:
                update_top_bid_symbols(all_symbols, target_grid_count)
                last_top = now

            for sym in gridded_symbols:
                if is_symbol_bullish(sym):
                    place_new_grid(sym, grid_levels)
            time.sleep(40)
        except Exception as e:
            log_ui(f"rebalancer error: {e}")
            time.sleep(5)

# ---------------------------
# Background Threads
# ---------------------------
def start_background_threads(all_symbols, target_grid_count, grid_levels):
    threading.Thread(target=grid_rebalancer, args=(all_symbols, target_grid_count, grid_levels), daemon=True).start()

    def price_updater():
        while True:
            for s in all_symbols:
                get_current_price(s)
            time.sleep(PRICE_UPDATE_INTERVAL)
    threading.Thread(target=price_updater, daemon=True).start()

    def pnl_updater():
        while True:
            update_pnl()
            time.sleep(30)
    threading.Thread(target=pnl_updater, daemon=True).start()

# ---------------------------
# Streamlit UI (FULLY INTERACTIVE)
# ---------------------------
def run_streamlit_ui(all_symbols):
    st.set_page_config(page_title="Infinity Grid — BINANCE.US LIVE", layout="wide")
    st.title("Infinity Grid — BINANCE.US LIVE")
    st.caption("P&L | Grid Levels | Portfolio | WhatsApp Alerts")

    # --- SIDEBAR CONTROLS ---
    with st.sidebar:
        st.header("Bot Settings")
        target_grid_count = st.slider(
            "NUMBER_OF_SYMBOLS_TO_GRID",
            min_value=1, max_value=50, value=11, step=1,
            help="Total symbols to grid (portfolio first)"
        )
        grid_levels = st.slider(
            "GRID_LEVELS (per side)",
            min_value=1, max_value=5, value=1, step=1,
            help="Buy + Sell levels per symbol"
        )
        st.write(f"**Target Symbols:** {target_grid_count}")
        st.write(f"**Grid Levels:** {grid_levels}")
        if CALLMEBOT_PHONE and CALLMEBOT_API_KEY:
            st.success("✅ CALLMEBOT Alerts Enabled")
        else:
            st.warning("⚠️ Set CALLMEBOT_PHONE & CALLMEBOT_API_KEY for alerts")
        if st.button("Force Regrid All"):
            for s in gridded_symbols:
                place_new_grid(s, grid_levels)
            alert_grid_update(gridded_symbols, grid_levels)
            st.success("Regrid triggered + Alert sent!")

    # --- MAIN DASHBOARD ---
    col1, col2 = st.columns([2, 1])
    placeholder = st.empty()

    # Initialize session state
    if 'initialized' not in st.session_state:
        st.session_state.initialized = True
        st.session_state.target_grid_count = target_grid_count
        st.session_state.grid_levels = grid_levels
        st.session_state.realized_pnl = Decimal('0')
        import_portfolio_symbols()
        start_background_threads(all_symbols, target_grid_count, grid_levels)

    # Update on slider change
    if (st.session_state.target_grid_count != target_grid_count or
        st.session_state.grid_levels != grid_levels):
        st.session_state.target_grid_count = target_grid_count
        st.session_state.grid_levels = grid_levels
        update_top_bid_symbols(all_symbols, target_grid_count)
        log_ui(f"Settings updated: {target_grid_count} symbols, {grid_levels} levels")

    while True:
        with placeholder.container():
            col1.subheader(f"Status — {now_str()}")
            col1.metric("Target Symbols", target_grid_count)
            col1.metric("Grid Levels", grid_levels)
            col1.metric("Portfolio", len(portfolio_symbols))
            col1.metric("Gridded", len(gridded_symbols))

            # P&L
            realized = st.session_state.realized_pnl
            col1.metric("Realized P&L", f"${realized:+.2f}", 
                       delta=f"{realized:+.2f}", delta_color="normal")

            # Portfolio Table
            if portfolio_symbols:
                col1.write("**Imported Portfolio**")
                port_data = []
                for sym in portfolio_symbols:
                    orders = len(active_grids.get(sym, []))
                    port_data.append({"Symbol": sym, "Orders": orders})
                col1.dataframe(port_data, use_container_width=True)

            # Grids Table
            col1.write("**Active Grids**")
            grid_data = []
            for sym in gridded_symbols:
                orders = len(active_grids.get(sym, []))
                grid_data.append({
                    "Symbol": sym,
                    "Orders": orders,
                    "Levels": grid_levels,
                    "In Portfolio": "Yes" if sym in portfolio_symbols else "No"
                })
            col1.dataframe(grid_data, use_container_width=True)

            col2.subheader("Recent Logs")
            for line in ui_logs[-30:]:
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
        log_ui(f"Found {len(symbols)} USDT pairs")
        return symbols
    except Exception as e:
        log_ui(f"Using fallback: {e}")
        return ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'ADAUSDT']

def main():
    all_symbols = initialize()
    run_streamlit_ui(all_symbols)

if __name__ == "__main__":
    main()
