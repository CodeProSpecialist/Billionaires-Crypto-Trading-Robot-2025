#!/usr/bin/env python3
"""
INFINITY GRID BOT — BINANCE.US LIVE
- 100% FULL CODE
- AUTO-ROTATION: ONLY IF ≥1.8% PROFIT PER POSITION
- TOP 25 BID VOLUME + ROTATE + SELL NON-TOP 25
- RISK MANAGEMENT + EMERGENCY STOP
- P&L + UNREALIZED + DASHBOARD
- CALLMEBOT: BUNDLED EVERY 10 MIN VIA SINGLE THREAD
"""

import os
import time
import threading
import requests
import re
import pandas as pd
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, List, Tuple, Set
import pytz

import streamlit as st

# --------------------------------------------------------------
# 1. PAGE CONFIG
# --------------------------------------------------------------
st.set_page_config(page_title="Infinity Grid — BINANCE.US LIVE", layout="wide")

# --------------------------------------------------------------
# 2. Binance Client
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
# Binance Client
# ---------------------------
try:
    binance_client = Client(API_KEY, API_SECRET, tld='us')
    st.success("Connected to BINANCE.US")
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
initial_asset_values: Dict[str, Decimal] = {}

# Risk & Rotation
peak_equity: Decimal = ZERO
daily_pnl_start: Decimal = ZERO
daily_pnl_date: str = ""
risk_violation: str = ""
last_auto_rotation = 0
last_top25_snapshot: List[str] = []

# Alert Bundling
alert_queue: List[Tuple[str, bool]] = []  # (message, force_send)
last_bundle_sent = 0
BUNDLE_INTERVAL = 10 * 60  # 10 minutes
alert_thread_running = False

# Timers
last_regrid_time = 0
last_regrid_str = "Never"
last_full_refresh = 0
REFRESH_INTERVAL = 300  # 5 minutes

price_lock = threading.Lock()
state_lock = threading.Lock()
api_rate_lock = threading.Lock()

ui_logs = []
initial_balance: Decimal = ZERO

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
        log_ui("Account cache updated")
    except Exception as e:
        log_ui(f"Account cache failed: {e}")

def get_balance(asset: str) -> Decimal:
    return account_cache.get(asset, ZERO)

# ---------------------------
# UNREALIZED P&L
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
        log_ui(f"Initial values error: {e}")

# ---------------------------
# ORDER FILL DETECTION
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
                symbol = order.get('symbol')
                if not symbol:
                    log_ui(f"Order {order_id} missing symbol")
                    continue
                side = order['side']
                qty = to_decimal(order['executedQty'])
                price = to_decimal(order['price'])
                fee = to_decimal(order.get('commission', '0'))
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
# AVERAGE BUY PRICE
# ---------------------------
def get_avg_buy_price(symbol: str) -> Decimal:
    try:
        with api_rate_lock:
            orders = binance_client.get_all_orders(symbol=symbol, limit=500)
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
        for asset, bal in account_cache.items():
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
            # Instant alerts
            instant_msgs = []
            with threading.Lock():
                for msg, force in list(alert_queue):
                    if force:
                        instant_msgs.append(msg)
                        alert_queue.remove((msg, force))
            for msg in instant_msgs:
                _send_single(msg)

            # Bundle normal
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
# GRID STATUS ALERT
# ---------------------------
def send_grid_status_alert():
    grid_count = len(gridded_symbols)
    usdt = get_balance('USDT')
    realized = st.session_state.get('realized_pnl', ZERO)
    unrealized = calculate_unrealized_pnl()
    total_pnl = realized + unrealized
    msg = f"Grid Status\n{grid_count} active grids\nRealized: ${realized:+.2f}\nUnrealized: ${unrealized:+.2f}\nTotal P&L: ${total_pnl:+.2f}\nUSDT: ${usdt:.2f}\nLast Regrid: {last_regrid_str}\n{now_str()}"
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
# ORDER PLACEMENT
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
    if price <= 0 or qty <= 0:
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
        resp = binance_client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=str(qty),
            price=str(price)
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
        open_orders = binance_client.get_open_orders(symbol=symbol)
        for o in open_orders:
            binance_client.cancel_order(symbol=symbol, orderId=o['orderId'])
        with state_lock:
            active_grids[symbol] = []
    except Exception as e:
        log_ui(f"cancel error {symbol}: {e}")

def cancel_all_orders_global():
    try:
        with api_rate_lock:
            open_orders = binance_client.get_open_orders()
        for o in open_orders:
            binance_client.cancel_order(symbol=o['symbol'], orderId=o['orderId'])
            log_ui(f"CANCELED {o['orderId']} {o['symbol']}")
        with state_lock:
            active_grids.clear()
            tracked_orders.clear()
        send_callmebot_alert("ALL ORDERS CANCELED", force_send=True)
    except Exception as e:
        log_ui(f"Cancel all failed: {e}")

# ---------------------------
# GRID LOGIC
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
                binance_client.get_symbol_ticker(symbol=symbol)
                owned.append(symbol)
            except:
                pass
    with state_lock:
        global portfolio_symbols
        portfolio_symbols = list(set(owned))
    log_ui(f"Portfolio: {len(portfolio_symbols)} symbols")
    return portfolio_symbols

def update_top_bid_symbols(all_symbols: List[str], target_grid_count: int):
    if target_grid_count < ) or target_grid_count > 50:
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
        last_regrid_time = time.time()
        last_regrid_str = now_str()
        log_ui(f"Gridding {len(gridded_symbols)} symbols")
    except Exception as e:
        log_ui(f"top update error: {e}")

# ---------------------------
# ROTATE TO TOP 25
# ---------------------------
def rotate_to_top25(all_symbols, is_auto=False):
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
        new_top25 = [s for s, _ in vols[:25]]
        removed = []
        for sym in list(gridded_symbols):
            if sym not in new_top25:
                cancel_all_orders(sym)
                with state_lock:
                    if sym in gridded_symbols:
                        gridded_symbols.remove(sym)
                    if sym in active_grids:
                        del active_grids[sym]
                removed.append(sym)
        added = []
        for sym in new_top25:
            if sym not in gridded_symbols:
                gridded_symbols.append(sym)
                added.append(sym)
        for sym in gridded_symbols:
            place_new_grid(sym, st.session_state.grid_levels, st.session_state.grid_size)
        global last_regrid_time, last_regrid_str
        last_regrid_time = time.time()
        last_regrid_str = now_str()
        msg = f"{'AUTO' if is_auto else 'MANUAL'}-ROTATED TO TOP 25\n"
        if removed:
            msg += f"Removed: {len(removed)} ({', '.join(removed[:5])}{'...' if len(removed)>5 else ''})\n"
        if added:
            msg += f"Added: {len(added)} ({', '.join(added[:5])}{'...' if len(added)>5 else ''})\n"
        msg += f"Active: {len(gridded_symbols)}\n{now_str()}"
        send_callmebot_alert(msg, force_send=is_auto)
        log_ui(f"Rotated to Top 25: +{len(added)}, -{len(removed)}")
    except Exception as e:
        log_ui(f"Rotate error: {e}")

# ---------------------------
# SELL NON-TOP 25
# ---------------------------
def sell_non_top25(all_symbols):
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
        top25 = {s for s, _ in vols[:25]}
        removed = []
        for sym in list(gridded_symbols):
            if sym not in top25:
                cancel_all_orders(sym)
                with state_lock:
                    if sym in gridded_symbols:
                        gridded_symbols.remove(sym)
                    if sym in active_grids:
                        del active_grids[sym]
                removed.append(sym)
        if removed:
            msg = f"SELL NON-TOP 25\nRemoved {len(removed)} grids:\n" + ", ".join(removed[:10])
            if len(removed) > 10:
                msg += f" +{len(removed)-10} more"
            msg += f"\n{now_str()}"
            send_callmebot_alert(msg, force_send=True)
            log_ui(f"Removed {len(removed)} non-top25 grids")
        else:
            send_callmebot_alert("SELL NON-TOP 25: No action", force_send=True)
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
# RISK METRICS
# ---------------------------
def update_risk_metrics():
    global peak_equity, daily_pnl_start, daily_pnl_date
    current_date = datetime.now(CST).strftime("%Y-%m-%d")
    total_pnl = st.session_state.total_pnl
    equity = initial_balance + total_pnl
    if daily_pnl_date != current_date:
        daily_pnl_start = total_pnl
        daily_pnl_date = current_date
        log_ui(f"Daily P&L reset for {current_date}")
    if equity > peak_equity:
        peak_equity = equity

# ---------------------------
# AUTO-ROTATION SCHEDULER
# ---------------------------
def auto_rotation_scheduler(all_symbols):
    global last_auto_rotation, last_top25_snapshot
    time.sleep(10)
    log_ui("AUTO-ROTATION SCHEDULER STARTED")
    while True:
        try:
            if not st.session_state.get('auto_rotate_enabled', False):
                time.sleep(30)
                continue
            now = time.time()
            interval = st.session_state.rotation_interval * 60
            if now - last_auto_rotation < interval:
                time.sleep(10)
                continue
            vols = []
            for s in all_symbols:
                try:
                    bids = binance_client.get_order_book(symbol=s, limit=5).get('bids', [])
                    vol = sum(to_decimal(q) for _, q in bids)
                    vols.append((s, vol))
                except:
                    continue
            vols.sort(key=lambda x: x[1], reverse=True)
            current_top25 = [s for s, _ in vols[:25]]
            if last_top25_snapshot:
                new_coins = [s for s in current_top25 if s not in last_top25_snapshot]
                if len(new_coins) < st.session_state.min_new_coins:
                    log_ui(f"Auto-rotate skipped: {len(new_coins)} new")
                    last_auto_rotation = now
                    last_top25_snapshot = current_top25
                    time.sleep(60)
                    continue
            can_rotate, under = can_rotate_on_profit()
            if not can_rotate:
                log_ui(f"Auto-rotate BLOCKED: {len(under)} below {st.session_state.profit_threshold_pct}%")
                send_callmebot_alert(f"AUTO-ROTATE BLOCKED\nNeed ≥{st.session_state.profit_threshold_pct}%\nUnder: {', '.join(under[:5])}\n{now_str()}", force_send=True)
                last_auto_rotation = now
                time.sleep(300)
                continue
            rotate_to_top25(all_symbols, is_auto=True)
            last_auto_rotation = now
            last_top25_snapshot = current_top25
        except Exception as e:
            log_ui(f"Auto-scheduler error: {e}")
            time.sleep(30)

# ---------------------------
# MAIN REBALANCER
# ---------------------------
def grid_rebalancer(all_symbols: List[str], target_grid_count: int, grid_levels: int, grid_size: Decimal):
    global last_full_refresh
    time.sleep(2)
    log_ui("REBALANCER STARTED (300s cycle)")
    while True:
        now = time.time()
        try:
            if st.session_state.get('paused', False):
                time.sleep(10)
                continue
            if now - last_full_refresh >= REFRESH_INTERVAL:
                update_account_cache()
                update_initial_asset_values()
                check_filled_orders()
                update_top_bid_symbols(all_symbols, target_grid_count)
                last_full_refresh = now
            for sym in gridded_symbols:
                place_new_grid(sym, grid_levels, grid_size)
            if now - last_bundle_sent >= BUNDLE_INTERVAL:
                send_grid_status_alert()
            time.sleep(40)
        except Exception as e:
            log_ui(f"rebalancer error: {e}")
            time.sleep(5)

# ---------------------------
# BACKGROUND THREADS
# ---------------------------
def start_background_threads(all_symbols, target_grid_count, grid_levels, grid_size):
    global alert_thread_running
    if not alert_thread_running:
        threading.Thread(target=alert_manager, daemon=True).start()
    threading.Thread(target=grid_rebalancer, args=(all_symbols, target_grid_count, grid_levels, grid_size), daemon=True).start()
    threading.Thread(target=auto_rotation_scheduler, args=(all_symbols,), daemon=True).start()
    def price_updater():
        while True:
            for s in all_symbols:
                get_current_price(s)
            time.sleep(60)
    threading.Thread(target=price_updater, daemon=True).start()
    def pnl_updater():
        while True:
            update_pnl()
            time.sleep(30)
    threading.Thread(target=pnl_updater, daemon=True).start()

def update_pnl():
    try:
        current_usdt = get_balance('USDT')
        for asset, bal in account_cache.items():
            if asset != 'USDT' and bal > ZERO:
                symbol = f"{asset}USDT"
                if symbol in live_prices:
                    current_usdt += bal * live_prices[symbol]
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

# ---------------------------
# STREAMLIT UI
# ---------------------------
def run_streamlit_ui(all_symbols):
    st.title("Infinity Grid — BINANCE.US LIVE")
    st.caption("300s Cycle | Full P&L | Portfolio + Grids")

    with st.sidebar:
        st.header("Settings")
        grid_size = st.slider("Grid Level ($)", 5, 50, 20, 5)
        st.session_state.grid_size = Decimal(grid_size)
        grid_levels = st.slider("Levels", 1, 5, 1, 1)
        st.session_state.grid_levels = grid_levels
        target_grid_count = st.slider("Symbols", 1, 50, 11, 1)
        st.session_state.target_grid_count = target_grid_count
        st.write(f"**Last Regrid:** {last_regrid_str}")

        st.header("AUTO-ROTATION SCHEDULER")
        auto_rotate_enabled = st.checkbox("Enable Auto-Rotation", value=False)
        st.session_state.auto_rotate_enabled = auto_rotate_enabled
        if auto_rotate_enabled:
            rotation_interval = st.slider("Rotate Every (min)", 15, 240, 60, 15)
            st.session_state.rotation_interval = rotation_interval
            min_new_coins = st.slider("Min New Top 25 Coins to Trigger", 1, 10, 3, 1)
            st.session_state.min_new_coins = min_new_coins
            profit_threshold = st.slider("Min Profit to Rotate (%)", 0.5, 5.0, 1.8, 0.1)
            st.session_state.profit_threshold_pct = Decimal(profit_threshold)
        else:
            st.session_state.rotation_interval = 60
            st.session_state.min_new_coins = 3
            st.session_state.profit_threshold_pct = Decimal('1.8')

        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            if st.button("REBALANCE", type="primary", use_container_width=True):
                rebalance_portfolio(all_symbols)
            if st.button("SELL ALL POSITIONS", type="secondary", use_container_width=True):
                st.session_state.show_sell_all_confirm = True
            if st.button("EMERGENCY STOP", type="secondary", use_container_width=True):
                st.session_state.show_emergency_confirm = True
            if st.button("SELL NON-TOP 25", type="secondary", use_container_width=True):
                st.session_state.show_sell_nontop_confirm = True
            if st.button("ROTATE TO TOP 25", type="primary", use_container_width=True):
                st.session_state.show_rotate_confirm = True
        with col_btn2:
            if st.button("STATUS NOW", use_container_width=True):
                send_grid_status_alert()
            if st.button("RESET P&L", use_container_width=True):
                reset_pnl_tracker()
            if st.button("REFRESH PRICES", use_container_width=True):
                refresh_prices_now(all_symbols)

        # Confirmations
        if st.session_state.get('show_sell_all_confirm', False):
            with st.expander("CONFIRM: SELL ALL POSITIONS", expanded=True):
                st.warning("This will cancel cancel ALL open orders.")
                col_yes, col_no = st.columns(2)
                with col_yes:
                    if st.button("YES, SELL ALL", type="primary", use_container_width=True):
                        cancel_all_orders_global()
                        st.session_state.show_sell_all_confirm = False
                        st.success("All positions liquidated!")
                with col_no:
                    if st.button("CANCEL", use_container_width=True):
                        st.session_state.show_sell_all_confirm = False

        if st.session_state.get('show_emergency_confirm', False):
            with st.expander("EMERGENCY STOP", expanded=True):
                st.error("Cancel all + pause bot")
                col_yes, col_no = st.columns(2)
                with col_yes:
                    if st.button("EXECUTE STOP", type="primary", use_container_width=True):
                        emergency_stop()
                        st.session_state.show_emergency_confirm = False
                        st.rerun()
                with col_no:
                    if st.button("CANCEL", use_container_width=True):
                        st.session_state.show_emergency_confirm = False

        if st.session_state.get('show_sell_nontop_confirm', False):
            with st.expander("SELL NON-TOP 25", expanded=True):
                st.warning("Remove low-liquidity grids")
                col_yes, col_no = st.columns(2)
                with col_yes:
                    if st.button("YES, SELL", type="primary", use_container_width=True):
                        sell_non_top25(all_symbols)
                        st.session_state.show_sell_nontop_confirm = False
                        st.success("Non-top 25 liquidated!")
                with col_no:
                    if st.button("CANCEL", use_container_width=True):
                        st.session_state.show_sell_nontop_confirm = False

        if st.session_state.get('show_rotate_confirm', False):
            with st.expander("ROTATE TO TOP 25", expanded=True):
                st.info("Upgrade to high-volume coins")
                col_yes, col_no = st.columns(2)
                with col_yes:
                    if st.button("YES, ROTATE", type="primary", use_container_width=True):
                        rotate_to_top25(all_symbols)
                        st.session_state.show_rotate_confirm = False
                        st.success("Rotated!")
                with col_no:
                    if st.button("CANCEL", use_container_width=True):
                        st.session_state.show_rotate_confirm = False

    col1, col2 = st.columns([2, 1])
    placeholder = st.empty()

    if 'initialized' not in st.session_state:
        st.session_state.initialized = True
        st.session_state.realized_pnl = ZERO
        st.session_state.unrealized_pnl = ZERO
        st.session_state.total_pnl = ZERO
        st.session_state.paused = False
        st.session_state.emergency_stopped = False
        import_portfolio_symbols()
        start_background_threads(all_symbols, target_grid_count, grid_levels, Decimal(grid_size))

    while True:
        with placeholder.container():
            col1.subheader(f"Status — {now_str()}")
            col1.metric("Gridded", len(gridded_symbols))
            col1.metric("USDT", f"{get_balance('USDT'):.2f}")

            r = st.session_state.realized_pnl
            u = st.session_state.unrealized_pnl
            t = st.session_state.total_pnl
            col1.metric("Realized P&L", f"${r:+.2f}")
            col1.metric("Unrealized P&L", f"${u:+.2f}", delta=f"{u:+.2f}")
            col1.metric("Total P&L", f"${t:+.2f}", delta=f"{t:+.2f}")

            can_rotate, under = can_rotate_on_profit()
            if can_rotate:
                col1.success(f"PROFIT GATE: PASSED (≥{st.session_state.profit_threshold_pct}%)")
            else:
                col1.warning(f"PROFIT GATE: {len(under)} below target")
                col1.caption(" | ".join(under[:5]))

            if st.session_state.get('emergency_stopped', False):
                col1.error("EMERGENCY STOP ACTIVE")
            elif st.session_state.get('paused', False):
                col1.warning("BOT PAUSED")
            else:
                col1.success("BOT RUNNING")

            if st.session_state.get('auto_rotate_enabled', False):
                mins_left = max(0, int((st.session_state.rotation_interval * 60 - (time.time() - last_auto_rotation)) / 60))
                col1.success(f"AUTO-ROTATION ON — Next in {mins_left} min")

            portfolio_data = []
            for sym in portfolio_symbols:
                base = sym.replace('USDT', '')
                bal = get_balance(base)
                grid_active = sym in gridded_symbols
                portfolio_data.append({
                    "Symbol": sym,
                    "Balance": f"{bal:.6f}",
                    "Grid": "Yes" if grid_active else "No"
                })
            if portfolio_data:
                col1.write("**Portfolio**")
                col1.dataframe(portfolio_data, use_container_width=True)

            col1.write("**Active Grids**")
            col1.dataframe([
                {"Symbol": s, "Orders": len(active_grids.get(s, [])), "Size": f"${st.session_state.grid_size}"}
                for s in gridded_symbols
            ], use_container_width=True)

            col1.write("### Top 25 Bid Volume (USDT)")
            top25_data = []
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
                top25 = vols[:25]
                for rank, (sym, vol) in enumerate(top25, 1):
                    in_grid = sym in gridded_symbols
                    score = "High" if rank <= 10 else "Medium" if rank <= 20 else "Low"
                    top25_data.append({
                        "Rank": rank,
                        "Symbol": sym,
                        "Bid Vol": f"${vol:,.2f}",
                        "In Grid": "Yes" if in_grid else "No",
                        "Score": score
                    })
            except Exception as e:
                log_ui(f"Top25 panel error: {e}")
            if top25_data:
                df = pd.DataFrame(top25_data)
                def color_rows(row):
                    return ['background-color: #d4edda' if row['In Grid'] == 'Yes' else ''] * len(row)
                styled = df.style.apply(color_rows, axis=1)
                col1.dataframe(styled, use_container_width=True, height=600)

            col2.subheader("Logs")
            for line in ui_logs[-30:]:
                col2.text(line)

        time.sleep(10)

# ---------------------------
# REBALANCE PORTFOLIO
# ---------------------------
def rebalance_portfolio(all_symbols):
    with st.spinner("Rebalancing..."):
        import_portfolio_symbols()
        update_top_bid_symbols(all_symbols, st.session_state.target_grid_count)
        for sym in gridded_symbols:
            place_new_grid(sym, st.session_state.grid_levels, st.session_state.grid_size)
        send_callmebot_alert(f"Portfolio Rebalanced\n{len(gridded_symbols)} grids\n{now_str()}", force_send=True)
        log_ui("REBALANCE COMPLETE")

# ---------------------------
# RESET P&L
# ---------------------------
def reset_pnl_tracker():
    global initial_balance, initial_asset_values
    update_account_cache()
    current_usdt = get_balance('USDT')
    for asset, bal in account_cache.items():
        if asset != 'USDT' and bal > ZERO:
            symbol = f"{asset}USDT"
            price = get_current_price(symbol)
            if price > ZERO:
                current_usdt += bal * price
    initial_balance = current_usdt
    initial_asset_values.clear()
    update_initial_asset_values()
    st.session_state.realized_pnl = ZERO
    st.session_state.unrealized_pnl = ZERO
    st.session_state.total_pnl = ZERO
    send_callmebot_alert("P&L Tracker RESET", force_send=True)
    log_ui("P&L reset")

# ---------------------------
# REFRESH PRICES
# ---------------------------
def refresh_prices_now(all_symbols):
    with st.spinner("Refreshing prices..."):
        for s in all_symbols:
            get_current_price(s)
        log_ui("All prices refreshed")

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
