#!/usr/bin/env python3
"""
INFINITY GRID BOT — LIVE TRADING ON BINANCE.US
- 15-MIN ALERT: Grids, Realized, UNREALIZED, Total P&L, Last Regrid, USDT
- ORDER FILL NOTIFICATIONS (WhatsApp)
- 1% PROFIT PER ORDER (after fees)
- USDT + $8 BUFFER BEFORE BUY
- ASSET BALANCE CHECK BEFORE SELL
- CALLMEBOT: THROTTLED
"""

import os
import time
import threading
import requests
import re
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, List, Tuple, Set
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

if not API_KEY or not API_SECRET:
    st.error("BINANCE_API_KEY and BINANCE_API_SECRET are required.")
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
# Binance Client
# ---------------------------
try:
    binance_client = Client(API_KEY, API_SECRET, tld='us')
    st.success("Connected to BINANCE.US MAINNET")
except Exception as e:
    st.error(f"Binance connection failed: {e}")
    st.stop()

# ---------------------------
# State & Locks
# ---------------------------
live_prices: Dict[str, Decimal] = {}
symbol_info_cache: Dict[str, dict] = {}
top_bid_symbols: List[str] = []
gridded_symbols: List[str] = []
portfolio_symbols: List[str] = []
active_grids: Dict[str, List[int]] = {}
account_cache: Dict[str, Decimal] = {}
tracked_orders: Set[int] = set()

# Alert & Regrid Timers
last_fill_alert_time = 0
last_status_alert_time = 0
last_regrid_time = 0
last_regrid_str = "Never"

MIN_FILL_ALERT_INTERVAL = 2 * 60
MIN_STATUS_INTERVAL = 15 * 60

price_lock = threading.Lock()
state_lock = threading.Lock()
api_rate_lock = threading.Lock()

ui_logs = []
initial_balance: Decimal = ZERO
initial_asset_values: Dict[str, Decimal] = {}  # asset -> initial value in USDT

# ---------------------------
# CALLMEBOT: THROTTLED ALERTS
# ---------------------------
def send_callmebot_alert(message: str, is_fill: bool = False):
    global last_fill_alert_time, last_status_alert_time
    now = time.time()
    if is_fill:
        if now - last_fill_alert_time < MIN_FILL_ALERT_INTERVAL:
            return
        last_fill_alert_time = now
    else:
        if now - last_status_alert_time < MIN_STATUS_INTERVAL:
            return
        last_status_alert_time = now

    if not CALLMEBOT_PHONE or not CALLMEBOT_API_KEY:
        return
    url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={message}&apikey={CALLMEBOT_API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            log_ui(f"Alert sent: {message[:50]}...")
    except Exception as e:
        log_ui(f"Alert error: {e}")

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
# ACCOUNT & BALANCE
# ---------------------------
def update_account_cache():
    try:
        with api_rate_lock:
            account = binance_client.get_account()
        for bal in account['balances']:
            asset = bal['asset']
            free = to_decimal(bal['free'])
            locked = to_decimal(bal['locked'])
            account_cache[asset] = free + locked
    except Exception as e:
        log_ui(f"Account cache failed: {e}")

def get_balance(asset: str) -> Decimal:
    return account_cache.get(asset, ZERO)

# ---------------------------
# UNREALIZED P&L CALCULATION
# ---------------------------
def calculate_unrealized_pnl() -> Decimal:
    unrealized = ZERO
    try:
        for asset, bal in account_cache.items():
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
        for asset, bal in account_cache.items():
            if asset == 'USDT' or bal <= ZERO:
                continue
            symbol = f"{asset}USDT"
            price = get_current_price(symbol)
            if price > ZERO:
                initial_asset_values[asset] = bal * price
    except Exception as e:
        log_ui(f"Initial values update error: {e}")

# ---------------------------
# ORDER FILL DETECTION & ALERT
# ---------------------------
def check_filled_orders():
    global tracked_orders
    try:
        with api_rate_lock:
            open_orders = binance_client.get_open_orders()
        current_ids = {int(o['orderId']) for o in open_orders}
        filled = tracked_orders - current_ids
        for order_id in filled:
            try:
                with api_rate_lock:
                    order = binance_client.get_order(orderId=order_id)
                symbol = order['symbol']
                side = order['side']
                qty = to_decimal(order['executedQty'])
                price = to_decimal(order['price'])
                notional = qty * price
                fee = to_decimal(order.get('commission', '0'))
                base = symbol.replace('USDT', '')
                profit = ZERO
                if side == 'SELL':
                    cost = get_buy_cost(symbol, qty)
                    profit = notional - cost - fee
                msg = f"FILLED: {side} {symbol} {qty} @ {price}"
                if profit > ZERO:
                    msg += f" → +${profit:.2f}"
                send_callmebot_alert(msg, is_fill=True)
                log_ui(msg)
            except Exception as e:
                log_ui(f"Fill check error {order_id}: {e}")
        tracked_orders = current_ids
    except Exception as e:
        log_ui(f"Fill monitor error: {e}")

def get_buy_cost(symbol: str, qty: Decimal) -> Decimal:
    try:
        orders = binance_client.get_all_orders(symbol=symbol, limit=100)
        buys = [o for o in orders if o['side'] == 'BUY' and o['status'] == 'FILLED']
        if not buys:
            return qty * get_current_price(symbol)
        total_cost = sum(to_decimal(o['executedQty']) * to_decimal(o['price']) for o in buys)
        total_qty = sum(to_decimal(o['executedQty']) for o in buys)
        return (total_cost / total_qty) * qty if total_qty > ZERO else ZERO
    except:
        return qty * get_current_price(symbol)

# ---------------------------
# GRID STATUS ALERT (Every 15 Min)
# ---------------------------
def send_grid_status_alert():
    global last_regrid_str
    grid_count = len(gridded_symbols)
    usdt = get_balance('USDT')
    realized = st.session_state.get('realized_pnl', ZERO)
    unrealized = calculate_unrealized_pnl()
    total_pnl = realized + unrealized

    msg = f"Grid Status\n"
    msg += f"{grid_count} active grids\n"
    msg += f"Realized: ${realized:+.2f}\n"
    msg += f"Unrealized: ${unrealized:+.2f}\n"
    msg += f"Total P&L: ${total_pnl:+.2f}\n"
    msg += f"USDT: ${usdt:.2f}\n"
    msg += f"Last Regrid: {last_regrid_str}\n"
    msg += f"{now_str()}"
    send_callmebot_alert(msg, is_fill=False)

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
    except:
        return ZERO

def get_fee_rates(symbol: str) -> Tuple[Decimal, Decimal]:
    try:
        with api_rate_lock:
            fee = binance_client.get_trade_fee(symbol=symbol)
        maker = to_decimal(fee[0].get('makerCommission', '0.001'))
        taker = to_decimal(fee[0].get('takerCommission', '0.001'))
        return maker, taker
    except:
        return Decimal('0.001'), Decimal('0.001')

# ---------------------------
# ORDER PLACEMENT: 1% PROFIT + BALANCE CHECK
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
        return 0

    # BALANCE CHECK
    base_asset = symbol.replace('USDT', '')
    if side == 'BUY':
        required_usdt = notional + Decimal('8')
        if get_balance('USDT') < required_usdt:
            log_ui(f"INSUFFICIENT USDT: need {required_usdt}, have {get_balance('USDT')}")
            return 0
    elif side == 'SELL':
        if get_balance(base_asset) < qty:
            log_ui(f"INSUFFICIENT {base_asset}: need {qty}, have {get_balance(base_asset)}")
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
            tracked_orders.add(oid)
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
# GRID: 1% PROFIT AFTER FEES
# ---------------------------
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

    now = time.time()
    last_regrid_time = now
    last_regrid_str = now_str()
    log_ui(f"GRID ACTIVE: {symbol} @ {price} ({levels}L, ${grid_size})")

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
                binance_client.get_symbol_ticker(symbol=symbol)
                owned.append(symbol)
            except:
                pass
    with state_lock:
        global portfolio_symbols
        portfolio_symbols = list(set(owned))
    log_ui(f"Imported {len(portfolio_symbols)} owned symbols")
    return portfolio_symbols

def update_top_bid_symbols(all_symbols: List[str], target_grid_count: int):
    if target_grid_count < 1 or target_grid_count > 50:
        return
    global top_bid_symbols, gridded_symbols, last_regrid_time, last_regrid_str
    try:
        vols = []
        for s in all_symbols:
            try:
                bids = binance_client.get_order_book(symbol=s, limit=5).get('bids', [])
                vol = sum(to_decimal(q) for _, q in bids)
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
        last_regrid_time = time.time()
        last_regrid_str = now_str()
    except Exception as e:
        log_ui(f"top update error: {e}")

# ---------------------------
# REBALANCER + STATUS ALERT
# ---------------------------
def grid_rebalancer(all_symbols: List[str], target_grid_count: int, grid_levels: int, grid_size: Decimal):
    time.sleep(2)
    log_ui(f"REBALANCER STARTED")
    last_top = 0
    last_status = 0
    while True:
        try:
            update_account_cache()
            check_filled_orders()
            now = time.time()

            # Update initial values on first run
            if not initial_asset_values:
                update_initial_asset_values()

            # Send status every 15 min
            if now - last_status >= MIN_STATUS_INTERVAL:
                send_grid_status_alert()
                last_status = now

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
# BACKGROUND THREADS
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
        update_account_cache()
        current_usdt = get_balance('USDT')
        for asset, bal in account_cache.items():
            if asset != 'USDT' and bal > ZERO:
                symbol = f"{asset}USDT"
                if symbol in live_prices:
                    current_usdt += bal * live_prices[symbol]
        if initial_balance == ZERO:
            initial_balance = current_usdt
        st.session_state.realized_pnl = current_usdt - initial_balance
        st.session_state.unrealized_pnl = calculate_unrealized_pnl()
        st.session_state.total_pnl = st.session_state.realized_pnl + st.session_state.unrealized_pnl
    except Exception as e:
        log_ui(f"PnL error: {e}")

# ---------------------------
# STREAMLIT UI
# ---------------------------
def run_streamlit_ui(all_symbols):
    st.title("Infinity Grid — BINANCE.US LIVE")
    st.caption("15-Min Full P&L | Fill Alerts | 1% Profit")

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
        st.write(f"**Last Regrid:** {last_regrid_str}")

        if st.button("Force Regrid"):
            update_top_bid_symbols(all_symbols, target_grid_count)
            for s in gridded_symbols:
                place_new_grid(s, grid_levels, Decimal(grid_size))
            st.success("Regrid!")

    col1, col2 = st.columns([2, 1])
    placeholder = st.empty()

    if 'initialized' not in st.session_state:
        st.session_state.initialized = True
        st.session_state.realized_pnl = Decimal('0')
        st.session_state.unrealized_pnl = Decimal('0')
        st.session_state.total_pnl = Decimal('0')
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
            col1.metric("USDT", f"{get_balance('USDT'):.2f}")

            realized = st.session_state.realized_pnl
            unrealized = st.session_state.unrealized_pnl
            total = st.session_state.total_pnl

            col1.metric("Realized P&L", f"${realized:+.2f}")
            col1.metric("Unrealized P&L", f"${unrealized:+.2f}", delta=f"{unrealized:+.2f}")
            col1.metric("Total P&L", f"${total:+.2f}", delta=f"{total:+.2f}")

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
# INIT & MAIN
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
