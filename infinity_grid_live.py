#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — BINANCE.US
Places 1% BUY & 1.8% SELL grid orders
ORDERS PLACED BEFORE RATE LIMIT CHECK
"""
import tkinter as tk
from tkinter import font as tkfont, messagebox
import threading
import time
import json
import os
import sys
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
import pytz
import requests
import websocket
from decimal import Decimal, getcontext, ROUND_DOWN, InvalidOperation
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ----------------------------------------------------------------------
# ========================= CONFIG & GLOBALS =========================
# ----------------------------------------------------------------------
getcontext().prec = 28
ZERO = Decimal('0')
ONE = Decimal('1')
SAFETY_BUFFER = Decimal('0.95')

# GRID SETTINGS
CASH_USDT_PER_GRID_ORDER = Decimal('5.00')
BUY_GRIDS_PER_POSITION = 1
SELL_GRIDS_PER_POSITION = 1
GRID_BUY_PCT = Decimal('0.01')      # 1% below
GRID_SELL_PCT = Decimal('0.018')   # 1.8% above
REGRID_INTERVAL = 1800             # 30 min
PROFIT_PER_GRID_INCREASE = Decimal('150')

# RATE LIMITING (UPDATED AFTER API CALLS)
API_WEIGHT_MAX = 1200
API_WEIGHT_CURRENT = 0
API_WEIGHT_LOCK = threading.Lock()
BASE_DELAY = 0.1
CURRENT_DELAY = BASE_DELAY
BACKOFF_MULTIPLIER = 2
MAX_DELAY = 30.0

USE_PAPER_TRADING = False

CST = pytz.timezone('America/Chicago')
LOG_FILE = os.path.expanduser("~/infinity_grid.log")
os.makedirs("logs", exist_ok=True)

client = None
symbol_info = {}
account_balances = {}
live_prices = {}
price_lock = threading.Lock()
ws_instances = []
ws_threads = []
user_ws = None
listen_key = None
listen_key_lock = threading.Lock()
initial_total_usdt = ZERO
running = False

# ----------------------------------------------------------------------
# ========================= LOGGING =========================
# ----------------------------------------------------------------------
def setup_logging():
    logger = logging.getLogger("infinity_grid")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = TimedRotatingFileHandler(LOG_FILE, when='midnight', backupCount=14)
        fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
        debug_fh = logging.FileHandler(f"logs/debug_{datetime.now().strftime('%Y%m%d')}.log")
        debug_fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
        logger.addHandler(fh)
        logger.addHandler(ch)
        logger.addHandler(debug_fh)
    return logger

logger = setup_logging()
log_info = logger.info
log_error = logger.error
log_debug = logger.debug

# ----------------------------------------------------------------------
# ========================= RATE LIMITING (POST-API) =========================
# ----------------------------------------------------------------------
def update_weight_from_response(response):
    """Update weight from any API response (dict or object)"""
    try:
        headers = getattr(response, 'headers', {}) or response.get('headers', {})
        used = int(headers.get('x-mbx-used-weight-1m', 0))
        with API_WEIGHT_LOCK:
            global API_WEIGHT_CURRENT
            API_WEIGHT_CURRENT = max(API_WEIGHT_CURRENT, used)
        log_debug(f"Weight updated: {API_WEIGHT_CURRENT}")
    except Exception as e:
        log_debug(f"Weight parse failed: {e}")

def apply_delay_and_backoff():
    """Apply delay *after* API call, adjust based on weight"""
    global CURRENT_DELAY
    with API_WEIGHT_LOCK:
        if API_WEIGHT_CURRENT > API_WEIGHT_MAX * 0.8:
            CURRENT_DELAY = min(MAX_DELAY, CURRENT_DELAY * BACKOFF_MULTIPLIER)
            log_info(f"High weight {API_WEIGHT_CURRENT} → delay {CURRENT_DELAY}s")
        else:
            CURRENT_DELAY = max(BASE_DELAY, CURRENT_DELAY / 1.5)
    time.sleep(CURRENT_DELAY)

def handle_api_error(e):
    global CURRENT_DELAY
    code = getattr(e, 'code', 0)
    msg = str(getattr(e, 'message', ''))
    log_error(f"API ERROR {code}: {msg}")
    if code in (429, 418, -1003):
        ban = 60
        if 'retry after' in msg.lower():
            try:
                ban = int([x for x in msg.split() if x.isdigit()][-1])
            except:
                pass
        CURRENT_DELAY = min(MAX_DELAY, CURRENT_DELAY * BACKOFF_MULTIPLIER)
        time.sleep(ban + 5)
    elif code >= 500:
        time.sleep(10)

# ----------------------------------------------------------------------
# ========================= UTILS =========================
# ----------------------------------------------------------------------
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def send_whatsapp(msg):
    phone = os.getenv('CALLMEBOT_PHONE')
    key = os.getenv('CALLMEBOT_API_KEY')
    if phone and key:
        try:
            m = requests.utils.quote(msg)
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={m}&apikey={key}", timeout=5)
        except:
            pass

def _safe_decimal(v, fallback='0'):
    try:
        return Decimal(str(v))
    except:
        return Decimal(fallback)

# ----------------------------------------------------------------------
# ========================= BALANCE & SYMBOL INFO =========================
# ----------------------------------------------------------------------
def update_balances():
    global account_balances
    try:
        info = client.get_account()
        update_weight_from_response(info)
        account_balances = {
            a['asset']: _safe_decimal(a['free'])
            for a in info['balances'] if _safe_decimal(a['free']) > ZERO
        }
        log_info(f"USDT: {account_balances.get('USDT', ZERO):.2f}")
        apply_delay_and_backoff()
    except BinanceAPIException as e:
        handle_api_error(e)
    except Exception as e:
        log_error(f"Balance error: {e}")

def get_total_portfolio_value():
    total = account_balances.get('USDT', ZERO)
    for asset, qty in account_balances.items():
        if asset == 'USDT': continue
        sym = f"{asset}USDT"
        price = live_prices.get(sym)
        if price:
            total += qty * price
    return total

def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()
        update_weight_from_response(info)
        for s in info.get('symbols', []):
            if s.get('quoteAsset') != 'USDT' or s.get('status') != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s.get('filters', [])}
            step = _safe_decimal(filters.get('LOT_SIZE', {}).get('stepSize', '0'))
            tick = _safe_decimal(filters.get('PRICE_FILTER', {}).get('tickSize', '0'))
            min_qty = _safe_decimal(filters.get('LOT_SIZE', {}).get('minQty', '0'))
            min_notional = _safe_decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            if step == ZERO or tick == ZERO:
                continue
            symbol_info[s['symbol']] = {
                'stepSize': step,
                'tickSize': tick,
                'minQty': min_qty,
                'minNotional': min_notional
            }
        log_info(f"Loaded {len(symbol_info)} symbols")
        apply_delay_and_backoff()
    except BinanceAPIException as e:
        handle_api_error(e)
    except Exception as e:
        log_error(f"Symbol info error: {e}")

# ----------------------------------------------------------------------
# ========================= SAFE ORDER (NO PRE-CHECK) =========================
# ----------------------------------------------------------------------
def round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value // step) * step

def format_decimal(d: Decimal) -> str:
    s = f"{d:f}"
    return s.rstrip('0').rstrip('.') if '.' in s else s

def place_limit_order(symbol, side, price, quantity):
    if USE_PAPER_TRADING:
        log_info(f"[PAPER] {side} {symbol} {quantity} @ {price}")
        return {'status': 'TEST'}

    info = symbol_info.get(symbol)
    if not info:
        log_error(f"NO SYMBOL INFO: {symbol}")
        return None

    price = round_down(Decimal(price), info['tickSize'])
    qty = round_down(Decimal(quantity), info['stepSize'])

    if qty <= info['minQty']:
        log_error(f"QTY TOO SMALL: {qty} <= {info['minQty']}")
        return None
    notional = price * qty
    if notional < info['minNotional']:
        log_error(f"NOTIONAL TOO LOW: {notional} < {info['minNotional']}")
        return None

    if side == 'BUY':
        needed = notional * SAFETY_BUFFER
        if needed > account_balances.get('USDT', ZERO):
            log_error(f"NOT ENOUGH USDT: {needed} > {account_balances.get('USDT', ZERO)}")
            return None
    else:
        base = symbol.replace('USDT', '')
        if qty > account_balances.get(base, ZERO) * SAFETY_BUFFER:
            log_error(f"NOT ENOUGH {base}: {qty} > {account_balances.get(base, ZERO)}")
            return None

    try:
        # PLACE ORDER FIRST
        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=format_decimal(qty),
            price=format_decimal(price)
        )
        # THEN UPDATE WEIGHT AND DELAY
        update_weight_from_response(order)
        log_info(f"PLACED {side} {symbol} {qty} @ {price}")
        send_whatsapp(f"{side} {symbol} {qty}@{price}")
        apply_delay_and_backoff()
        return order
    except BinanceAPIException as e:
        handle_api_error(e)
        return None
    except Exception as e:
        log_error(f"ORDER FAILED {symbol}: {e}")
        return None

# ----------------------------------------------------------------------
# ========================= GRID CORE =========================
# ----------------------------------------------------------------------
def get_owned_assets():
    owned = []
    for asset, free in account_balances.items():
        if asset == 'USDT' or free <= Decimal('0.0001'):
            continue
        sym = f"{asset}USDT"
        if sym not in symbol_info:
            continue
        try:
            ticker = client.get_symbol_ticker(symbol=sym)
            update_weight_from_response(ticker)
            price = Decimal(ticker['price'])
            if free * price >= Decimal('1'):
                owned.append(asset)
            apply_delay_and_backoff()
        except Exception as e:
            log_error(f"Price fetch failed {sym}: {e}")
    return owned

def cancel_all_pending_orders():
    try:
        orders = client.get_open_orders()
        update_weight_from_response(orders)
        log_info(f"Cancelling {len(orders)} orders...")
        for o in orders:
            try:
                client.cancel_order(symbol=o['symbol'], orderId=o['orderId'])
                apply_delay_and_backoff()
            except Exception as e:
                log_error(f"Cancel failed {o['orderId']}: {e}")
        apply_delay_and_backoff()
    except Exception as e:
        log_error(f"Open orders fetch failed: {e}")

def place_grid_orders(asset):
    symbol = f"{asset}USDT"
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        update_weight_from_response(ticker)
        cur_price = Decimal(ticker['price'])
        log_info(f"GRID: {symbol} @ {cur_price}")
        apply_delay_and_backoff()
    except Exception as e:
        log_error(f"Price fetch failed {symbol}: {e}")
        return

    info = symbol_info.get(symbol)
    if not info:
        log_error(f"NO INFO for {symbol}")
        return

    qty_step = info['stepSize']
    price_step = info['tickSize']
    free_asset = account_balances.get(asset, ZERO)

    # === BUY GRIDS: 1% BELOW ===
    for i in range(1, BUY_GRIDS_PER_POSITION + 1):
        try:
            buy_price = cur_price * (ONE - GRID_BUY_PCT * Decimal(i))
            buy_price = round_down(buy_price, price_step)
            qty = CASH_USDT_PER_GRID_ORDER / buy_price
            qty = round_down(qty, qty_step)
            if qty > ZERO:
                log_debug(f"BUY GRID: {symbol} {qty} @ {buy_price}")
                place_limit_order(symbol, 'BUY', buy_price, qty)
        except Exception as e:
            log_error(f"BUY GRID ERROR {i}: {e}")

    # === SELL GRIDS: 1.8% ABOVE ===
    for i in range(1, SELL_GRIDS_PER_POSITION + 1):
        try:
            sell_price = cur_price * (ONE + GRID_SELL_PCT * Decimal(i))
            sell_price = round_down(sell_price, price_step)
            qty = CASH_USDT_PER_GRID_ORDER / sell_price
            qty = round_down(qty, qty_step)
            if qty > ZERO and qty <= free_asset:
                log_debug(f"SELL GRID: {symbol} {qty} @ {sell_price}")
                place_limit_order(symbol, 'SELL', sell_price, qty)
            elif qty > free_asset:
                log_debug(f"SKIP SELL: only {free_asset} {asset}")
        except Exception as e:
            log_error(f"SELL GRID ERROR {i}: {e}")

def grid_cycle():
    global initial_total_usdt, BUY_GRIDS_PER_POSITION, SELL_GRIDS_PER_POSITION
    while running:
        try:
            update_balances()
            initial_total_usdt = get_total_portfolio_value()
            if initial_total_usdt == 0:
                log_error("No balance. Retrying...")
                time.sleep(30)
                continue

            cancel_all_pending_orders()
            owned = get_owned_assets()
            log_info(f"Placing grid for {len(owned)} coins: {owned}")
            for asset in owned:
                place_grid_orders(asset)

            last_regrid = time.time()
            log_info(f"GRID ACTIVE. Portfolio: ${initial_total_usdt:.2f}")

            while running:
                time.sleep(60)
                current = get_total_portfolio_value()
                profit = current - initial_total_usdt
                new_grids = max(1, 1 + int(profit / PROFIT_PER_GRID_INCREASE))
                if new_grids != BUY_GRIDS_PER_POSITION:
                    log_info(f"PROFIT ${profit:.2f} → GRIDS: {new_grids}")
                    BUY_GRIDS_PER_POSITION = new_grids
                    SELL_GRIDS_PER_POSITION = new_grids

                if time.time() - last_regrid > REGRID_INTERVAL:
                    log_info("RE-GRIDDING...")
                    cancel_all_pending_orders()
                    owned = get_owned_assets()
                    for asset in owned:
                        place_grid_orders(asset)
                    last_regrid = time.time()

        except Exception as e:
            log_error(f"GRID CRASH: {e}. Restarting...")
            time.sleep(15)

# ----------------------------------------------------------------------
# ========================= WEBSOCKET =========================
# ----------------------------------------------------------------------
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, **kwargs):
        super().__init__(url, **kwargs)
        self._stop = False

    def on_open(self, ws):
        log_info(f"WS OPEN: {ws.url.split('?')[0]}")

    def run_forever(self, **kwargs):
        while not self._stop:
            try:
                super().run_forever(ping_interval=25, **kwargs)
            except Exception as e:
                log_error(f"WS CRASH: {e}")
            time.sleep(5)

    def stop(self):
        self._stop = True
        self.close()

def on_price_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get('stream', '')
        payload = data.get('data', {})
        sym = stream.split('@')[0].upper()
        if stream.endswith('@ticker') and payload:
            price = _safe_decimal(payload.get('c', '0'))
            if price > ZERO:
                with price_lock:
                    live_prices[sym] = price
    except Exception as e:
        log_debug(f"Price WS parse: {e}")

def start_price_ws(symbols):
    global ws_instances
    streams = [f"{s.lower()}@ticker" for s in symbols[:500]]
    url = f"wss://stream.binance.us:9443/stream?streams={'/'.join(streams)}"
    ws = HeartbeatWebSocket(url, on_message=on_price_message)
    ws_instances.append(ws)
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    ws_threads.append(t)

# ----------------------------------------------------------------------
# ========================= INIT & GUI =========================
# ----------------------------------------------------------------------
def initialise_bot():
    try:
        log_info("INITIALISING GRID BOT...")
        load_symbol_info()
        update_balances()
        start_price_ws(list(symbol_info.keys()))
        log_info("GRID BOT READY")
        send_whatsapp("GRID BOT STARTED")
    except Exception as e:
        log_error(f"Init failed: {e}")

def start_trading():
    global running
    if not running:
        running = True
        log_info("START PRESSED")
        threading.Thread(target=initialise_bot, daemon=True).start()
        threading.Thread(target=grid_cycle, daemon=True).start()
        status_label.config(text="Status: Running", fg="green")

def stop_trading():
    global running
    log_info("STOP PRESSED")
    running = False
    cancel_all_pending_orders()
    for ws in ws_instances:
        ws.stop()
    status_label.config(text="Status: Stopped", fg="red")

# GUI
root = tk.Tk()
root.title("INFINITY GRID BOT 2025")
root.geometry("460x180")
root.resizable(False, False)

tk.Label(root, text="INFINITY GRID BOT", font=tkfont.Font(size=14, weight="bold")).pack(pady=10)
tk.Button(root, text="Start", command=start_trading, bg="green", fg="white", font=tkfont.Font(size=12, weight="bold"), width=18).pack(pady=8)
tk.Button(root, text="Stop", command=stop_trading, bg="red", fg="white", font=tkfont.Font(size=12, weight="bold"), width=18).pack(pady=5)
status_label = tk.Label(root, text="Status: Stopped", fg="red")
status_label.pack(pady=5)

def update_status():
    status_label.config(text="Status: Running" if running else "Status: Stopped", fg="green" if running else "red")
    root.after(1000, update_status)
update_status()

# ----------------------------------------------------------------------
# ========================= ENTRY POINT =========================
# ----------------------------------------------------------------------
if __name__ == "__main__":
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')
    if not api_key or not api_secret:
        messagebox.showerror("API Error", "Set BINANCE_API_KEY and BINANCE_API_SECRET")
        sys.exit(1)
    client = Client(api_key, api_secret, tld='us')
    root.mainloop()
