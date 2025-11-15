#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — BINANCE.US
Places 1% BUY & 1.8% SELL grid orders for all owned coins
Tkinter GUI | WebSocket + REST | Rate-Limited | IP Ban Safe
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
MAX_POSITION_PCT = Decimal('0.05')
MIN_TRADE_VALUE = Decimal('5.0')
ENTRY_PCT_BELOW_ASK = Decimal('0.001')
TOP_N_VOLUME = 25
MAX_BUY_COINS = 2
DEPTH_LEVELS = 5
HEARTBEAT_INTERVAL = 25
MAX_STREAMS_PER_CONNECTION = 100
RECONNECT_BASE_DELAY = 5
MAX_RECONNECT_DELAY = 300

# === INFINITY GRID SETTINGS (COMPLETED) ===
CASH_USDT_PER_GRID_ORDER = Decimal('5.00')   # $5 per grid level
BUY_GRIDS_PER_POSITION = 1                   # Auto-scaled
SELL_GRIDS_PER_POSITION = 1                  # Auto-scaled
GRID_BUY_PCT = Decimal('0.01')               # 1.0% below current price
GRID_SELL_PCT = Decimal('0.018')             # 1.8% above current price
REGRID_INTERVAL = 1800                       # 30 minutes
PROFIT_PER_GRID_INCREASE = Decimal('150')    # +1 grid per $150 profit

# === RATE LIMITING ===
API_WEIGHT_MAX = 1200
API_WEIGHT_CURRENT = 0
API_WEIGHT_LOCK = threading.Lock()
REQUEST_DELAY = 0.1
CURRENT_DELAY = REQUEST_DELAY
BACKOFF_MULTIPLIER = 2
MAX_DELAY = 30.0

# USER CHOICE
USE_PAPER_TRADING = False

CST = pytz.timezone('America/Chicago')
LOG_FILE = os.path.expanduser("~/platinum_crypto_dashboard.log")
os.makedirs("logs", exist_ok=True)

# Filters
DISALLOWED_COINS = {
    'BTCUSDT', 'BCHUSDT', 'ETHUSDT', 'ETCUSDT',
    'USDTUSDT', 'USDCUSDT', 'USDUSDT'
}
MIN_PRICE = Decimal('15')
MAX_PRICE = Decimal('1000')
MIN_VOLUME = Decimal('150000')

# Global state
client = None
symbol_info = {}
account_balances = {}
live_prices = {}
live_bids = {}
live_asks = {}
top_volume_symbols = []
active_positions = {}
price_lock = threading.Lock()
book_lock = threading.Lock()
ws_instances = []
ws_threads = []
user_ws = None
listen_key = None
listen_key_lock = threading.Lock()
last_rebalance_str = "Never"
initial_total_usdt = ZERO
running = False

# ----------------------------------------------------------------------
# ========================= LOGGING =========================
# ----------------------------------------------------------------------
def setup_logging():
    logger = logging.getLogger("infinity_grid_bot")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = TimedRotatingFileHandler(LOG_FILE, when='midnight', backupCount=14)
        fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
        grid_fh = logging.FileHandler(
            f"logs/infinity_grid_{datetime.now().strftime('%Y%m%d')}.log")
        grid_fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
        logger.addHandler(fh)
        logger.addHandler(ch)
        logger.addHandler(grid_fh)
    return logger

logger = setup_logging()
log_info = logger.info
log_error = logger.error

# ----------------------------------------------------------------------
# ========================= RATE LIMITING =========================
# ----------------------------------------------------------------------
def update_api_weight(headers):
    global API_WEIGHT_CURRENT
    try:
        used = int(headers.get('x-mbx-used-weight-1m', 0))
        with API_WEIGHT_LOCK:
            API_WEIGHT_CURRENT = max(API_WEIGHT_CURRENT, used)
    except:
        pass

def wait_for_rate_limit():
    global CURRENT_DELAY
    with API_WEIGHT_LOCK:
        if API_WEIGHT_CURRENT > API_WEIGHT_MAX * 0.8:
            log_info(f"High API weight {API_WEIGHT_CURRENT}, slowing down...")
            CURRENT_DELAY = min(MAX_DELAY, CURRENT_DELAY * BACKOFF_MULTIPLIER)
        else:
            CURRENT_DELAY = max(REQUEST_DELAY, CURRENT_DELAY / 1.5)
    time.sleep(CURRENT_DELAY)

def handle_rate_limit_error(e):
    global CURRENT_DELAY
    code = getattr(e, 'code', 0)
    msg = str(getattr(e, 'message', ''))
    if code in (429, 418, -1003):
        ban_sec = 60
        if 'retry after' in msg.lower():
            try:
                ban_sec = int([x for x in msg.split() if x.isdigit()][-1])
            except:
                pass
        log_error(f"RATE LIMITED! Banned for {ban_sec}s. Backing off...")
        CURRENT_DELAY = min(MAX_DELAY, CURRENT_DELAY * BACKOFF_MULTIPLIER)
        time.sleep(ban_sec + 5)
    elif code >= 500:
        log_error(f"Server error {code}, retrying...")
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
            requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={m}&apikey={key}",
                timeout=5
            )
        except Exception:
            pass

def _safe_decimal(v, fallback='0'):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(fallback)

# ----------------------------------------------------------------------
# ========================= BALANCE & SYMBOL INFO =========================
# ----------------------------------------------------------------------
def update_balances():
    global account_balances
    try:
        wait_for_rate_limit()
        info = client.get_account()
        account_balances = {
            a['asset']: Decimal(a['free']) for a in info['balances'] if Decimal(a['free']) > ZERO
        }
        log_info(f"USDT free: {account_balances.get('USDT', ZERO):.2f}")
    except BinanceAPIException as e:
        handle_rate_limit_error(e)
    except Exception as e:
        log_error(f"Balance update error: {e}")

def get_total_portfolio_value():
    total = account_balances.get('USDT', ZERO)
    for asset, qty in account_balances.items():
        if asset == 'USDT':
            continue
        sym = f"{asset}USDT"
        price = live_prices.get(sym)
        if price:
            total += qty * price
    return total

def load_symbol_info():
    global symbol_info
    try:
        wait_for_rate_limit()
        info = client.get_exchange_info()
        for s in info.get('symbols', []):
            if s.get('quoteAsset') != 'USDT' or s.get('status') != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s.get('filters', [])}
            stepSize = _safe_decimal(filters.get('LOT_SIZE', {}).get('stepSize', '0'))
            tickSize = _safe_decimal(filters.get('PRICE_FILTER', {}).get('tickSize', '0'))
            minQty = _safe_decimal(filters.get('LOT_SIZE', {}).get('minQty', '0'))
            minNotional = _safe_decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            if stepSize == ZERO or tickSize == ZERO:
                continue
            symbol_info[s['symbol']] = {
                'stepSize': stepSize,
                'tickSize': tickSize,
                'minQty': minQty,
                'minNotional': minNotional
            }
        log_info(f"Loaded {len(symbol_info)} symbols")
    except BinanceAPIException as e:
        handle_rate_limit_error(e)
    except Exception as e:
        log_error(f"Symbol info error: {e}")

# ----------------------------------------------------------------------
# ========================= SAFE ORDER =========================
# ----------------------------------------------------------------------
def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    try:
        if step <= 0:
            return value
        return value.quantize(step, rounding=ROUND_DOWN)
    except Exception:
        try:
            multiples = (value // step)
            return multiples * step
        except Exception:
            return value

def format_decimal_for_order(d: Decimal) -> str:
    s = format(d.normalize(), 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s

def place_limit_order(symbol, side, price, quantity):
    if USE_PAPER_TRADING:
        log_info(f"[PAPER] {side} {symbol} {quantity} @ {price}")
        return {'status': 'TEST'}

    info = symbol_info.get(symbol)
    if not info:
        log_error(f"No symbol info for {symbol}")
        return None

    price = round_down_to_step(Decimal(price), info['tickSize'])
    quantity = round_down_to_step(Decimal(quantity), info['stepSize'])

    if quantity <= info['minQty']:
        log_error(f"Qty too small {symbol} {quantity} <= {info['minQty']}")
        return None
    notional = price * quantity
    if notional < info['minNotional']:
        log_error(f"Notional too low {symbol}: {notional} < {info['minNotional']}")
        return None

    if side == 'BUY':
        needed = notional * SAFETY_BUFFER
        if needed > account_balances.get('USDT', ZERO):
            log_error(f"Not enough USDT for BUY {symbol}")
            return None
    else:
        base = symbol.replace('USDT', '')
        if quantity > account_balances.get(base, ZERO) * SAFETY_BUFFER:
            log_error(f"Not enough {base} for SELL")
            return None

    try:
        wait_for_rate_limit()
        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=format_decimal_for_order(quantity),
            price=format_decimal_for_order(price)
        )
        log_info(f"PLACED {side} {symbol} {quantity} @ {price}")
        send_whatsapp(f"{side} {symbol} {quantity}@{price}")
        return order
    except BinanceAPIException as e:
        handle_rate_limit_error(e)
        return None
    except Exception as e:
        log_error(f"ORDER ERROR {symbol}: {e}")
        return None

# ----------------------------------------------------------------------
# ========================= INFINITY GRID CORE =========================
# ----------------------------------------------------------------------
def get_owned_assets():
    owned = []
    for asset, free in account_balances.items():
        if asset == 'USDT' or free <= Decimal('0.0001'):
            continue
        symbol = asset + 'USDT'
        try:
            wait_for_rate_limit()
            price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
            if free * price >= Decimal('1'):
                owned.append(asset)
        except BinanceAPIException as e:
            handle_rate_limit_error(e)
        except Exception as e:
            log_error(f"Failed to get price for {symbol}: {e}")
    return owned

def cancel_all_pending_orders():
    try:
        wait_for_rate_limit()
        open_orders = client.get_open_orders()
        log_info(f"Cancelling {len(open_orders)} pending orders...")
        for o in open_orders:
            try:
                wait_for_rate_limit()
                client.cancel_order(symbol=o['symbol'], orderId=o['orderId'])
                log_info(f"Cancelled {o['orderId']} {o['symbol']}")
            except BinanceAPIException as e:
                handle_rate_limit_error(e)
            except Exception as e:
                log_error(f"Cancel error {o['orderId']}: {e}")
    except BinanceAPIException as e:
        handle_rate_limit_error(e)
    except Exception as e:
        log_error(f"Failed to fetch open orders: {e}")

def place_grid_orders(asset):
    symbol = asset + 'USDT'
    try:
        wait_for_rate_limit()
        cur_price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
        log_info(f"Placing grid for {symbol} @ {cur_price}")
    except BinanceAPIException as e:
        handle_rate_limit_error(e)
        return
    except Exception as e:
        log_error(f"Cannot get price for {symbol}: {e}")
        return

    info = symbol_info.get(symbol, {})
    qty_step = info.get('stepSize', Decimal('1'))
    price_step = info.get('tickSize', Decimal('0.01'))
    free_asset = account_balances.get(asset, ZERO)

    # === PLACE BUY GRIDS: 1% BELOW ===
    for i in range(1, BUY_GRIDS_PER_POSITION + 1):
        try:
            buy_price = cur_price * (ONE - GRID_BUY_PCT * Decimal(i))
            buy_price = round_down_to_step(buy_price, price_step)
            qty = CASH_USDT_PER_GRID_ORDER / buy_price
            qty = round_down_to_step(qty, qty_step)
            if qty > ZERO:
                place_limit_order(symbol, 'BUY', buy_price, qty)
        except Exception as e:
            log_error(f"Buy grid error {symbol} level {i}: {e}")

    # === PLACE SELL GRIDS: 1.8% ABOVE ===
    for i in range(1, SELL_GRIDS_PER_POSITION + 1):
        try:
            sell_price = cur_price * (ONE + GRID_SELL_PCT * Decimal(i))
            sell_price = round_down_to_step(sell_price, price_step)
            qty = CASH_USDT_PER_GRID_ORDER / sell_price
            qty = round_down_to_step(qty, qty_step)
            if qty > ZERO and qty <= free_asset:
                place_limit_order(symbol, 'SELL', sell_price, qty)
            elif qty > free_asset:
                log_error(f"Insufficient {asset} for sell grid level {i}")
        except Exception as e:
            log_error(f"Sell grid error {symbol} level {i}: {e}")

def grid_cycle():
    global initial_total_usdt, BUY_GRIDS_PER_POSITION, SELL_GRIDS_PER_POSITION
    while running:
        try:
            update_balances()
            initial_total_usdt = get_total_portfolio_value()
            if initial_total_usdt == 0:
                log_error("Failed to get initial balance. Retrying in 30s...")
                time.sleep(30)
                continue

            cancel_all_pending_orders()
            owned = get_owned_assets()
            log_info(f"Placing INFINITY GRID for {len(owned)} assets")
            for asset in owned:
                place_grid_orders(asset)

            last_regrid = time.time()
            log_info(f"Grid placed. Portfolio: ${initial_total_usdt:.2f}")

            while running:
                time.sleep(60)
                current_total = get_total_portfolio_value()
                if current_total == 0:
                    continue

                # Auto-scale grid with profit
                increase = int((current_total - initial_total_usdt) / PROFIT_PER_GRID_INCREASE)
                new_grids = max(1, 1 + increase)
                if new_grids != BUY_GRIDS_PER_POSITION:
                    log_info(f"Profit ${current_total - initial_total_usdt:.2f} to grids to {new_grids}")
                    BUY_GRIDS_PER_POSITION = new_grids
                    SELL_GRIDS_PER_POSITION = new_grids

                # Re-grid every 30 minutes
                if time.time() - last_regrid > REGRID_INTERVAL:
                    log_info("30-minute re-grid")
                    cancel_all_pending_orders()
                    owned = get_owned_assets()
                    for asset in owned:
                        place_grid_orders(asset)
                    last_regrid = time.time()

        except Exception as e:
            log_error(f"GRID CYCLE CRASH: {e}. Restarting in 15s...")
            time.sleep(15)

# ----------------------------------------------------------------------
# ========================= WEBSOCKET =========================
# ----------------------------------------------------------------------
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, **kwargs):
        super().__init__(url, **kwargs)
        self.last_pong = time.time()
        self.hb_thread = None
        self.reconnect = 0
        self._stop = False

    def on_open(self, ws):
        log_info(f"WS OPEN {ws.url.split('?')[0]}")
        self.last_pong = time.time()
        self.reconnect = 0
        if not self.hb_thread or not self.hb_thread.is_alive():
            self.hb_thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.hb_thread.start()

    def on_pong(self, *args):
        self.last_pong = time.time()

    def _heartbeat(self):
        while not self._stop and getattr(self, 'sock', None) and getattr(self.sock, 'connected', False):
            if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 10:
                log_error("No pong to closing WS")
                self.close()
                break
            try:
                self.send("ping", opcode=websocket.ABNF.OPCODE_PING)
            except Exception:
                pass
            time.sleep(HEARTBEAT_INTERVAL)

    def run_forever(self, **kwargs):
        self._stop = False
        while not self._stop:
            try:
                super().run_forever(ping_interval=None, ping_timeout=None, **kwargs)
            except Exception as e:
                log_error(f"WS crash: {e}")
            self.reconnect += 1
            delay = min(MAX_RECONNECT_DELAY, RECONNECT_BASE_DELAY * (2 ** (self.reconnect - 1)))
            log_info(f"WS Reconnect in {delay}s")
            time.sleep(delay)

    def stop(self):
        self._stop = True
        try:
            self.close()
        except Exception:
            pass

def on_market_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get('stream', '')
        payload = data.get('data', {})
        if not payload:
            return
        sym = stream.split('@')[0].upper()
        if stream.endswith('@ticker'):
            price = _safe_decimal(payload.get('c', '0'))
            if price > ZERO:
                with price_lock:
                    live_prices[sym] = price
    except Exception as e:
        log_error(f"Market WS parse error: {e}")

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data.get('e') != 'executionReport':
            return
        ev = data
        sym = ev.get('s')
        side = ev.get('S')
        status = ev.get('X')
        price = _safe_decimal(ev.get('p', '0'))
        qty = _safe_decimal(ev.get('q', '0'))
        if status in ('FILLED', 'PARTIALLY_FILLED'):
            log_info(f"FILL {side} {sym} @ {price} | {qty}")
            send_whatsapp(f"{side} {sym} {status} @ {price}")
    except Exception as e:
        log_error(f"User WS error: {e}")

def on_ws_error(ws, err):
    log_error(f"WS error: {err}")

def on_ws_close(ws, code, msg):
    log_info(f"WS closed to {code}: {msg}")

def start_market_websockets(symbols):
    global ws_instances, ws_threads
    ticker = [f"{s.lower()}@ticker" for s in symbols]
    streams = ticker
    chunks = [streams[i:i + MAX_STREAMS_PER_CONNECTION] for i in range(0, len(streams), MAX_STREAMS_PER_CONNECTION)]

    for chunk in chunks:
        url = f"wss://stream.binance.us:9443/stream?streams={'/'.join(chunk)}"
        ws = HeartbeatWebSocket(
            url,
            on_message=on_market_message,
            on_error=on_ws_error,
            on_close=on_ws_close,
            on_open=lambda ws: log_info("Market WS open")
        )
        ws_instances.append(ws)
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        time.sleep(0.5)

def start_user_stream():
    global user_ws, listen_key
    try:
        wait_for_rate_limit()
        with listen_key_lock:
            listen_key = client.stream_get_listen_key()
        url = f"wss://stream.binance.us:9443/ws/{listen_key}"
        user_ws = HeartbeatWebSocket(
            url,
            on_message=on_user_message,
            on_error=on_ws_error,
            on_close=on_ws_close,
            on_open=lambda ws: log_info("User WS open")
        )
        t = threading.Thread(target=user_ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        log_info("User stream started")
    except BinanceAPIException as e:
        handle_rate_limit_error(e)
    except Exception as e:
        log_error(f"User stream failed: {e}")

def keepalive_user_stream():
    while running:
        time.sleep(1800)
        try:
            wait_for_rate_limit()
            with listen_key_lock:
                if listen_key:
                    client.stream_keepalive(listen_key)
        except BinanceAPIException as e:
            handle_rate_limit_error(e)
        except Exception:
            pass

# ----------------------------------------------------------------------
# ========================= INITIALIZE & MAIN LOOP =========================
# ----------------------------------------------------------------------
def initialise_bot():
    try:
        log_info("=== INITIALISING INFINITY GRID BOT ===")
        load_symbol_info()
        update_balances()

        all_syms = list(symbol_info.keys())
        start_market_websockets(all_syms[:1000] if len(all_syms) > 1000 else all_syms)
        start_user_stream()
        threading.Thread(target=keepalive_user_stream, daemon=True).start()

        deadline = time.time() + 15
        while time.time() < deadline:
            if live_prices:
                break
            time.sleep(0.5)

        log_info("INFINITY GRID BOT READY")
        send_whatsapp("INFINITY GRID BOT STARTED")
    except Exception as e:
        log_error(f"Initialise error: {e}")

# ----------------------------------------------------------------------
# ========================= TKINTER GUI =========================
# ----------------------------------------------------------------------
def start_trading():
    global running
    if not running:
        running = True
        log_info("START button pressed – launching bot")
        threading.Thread(target=initialise_bot, daemon=True).start()
        threading.Thread(target=grid_cycle, daemon=True).start()
        status_label.config(text="Status: Running", fg="green")
    else:
        log_info("Bot already running")

def stop_trading():
    global running
    log_info("STOP button pressed – shutting down")
    running = False
    cancel_all_pending_orders()
    for ws in ws_instances:
        ws.stop()
    status_label.config(text="Status: Stopped", fg="red")
    log_info("Bot stopped")

# GUI
root = tk.Tk()
root.title("INFINITY GRID BOT 2025")
root.geometry("460x180")
root.resizable(False, False)

title_font = tkfont.Font(family="Helvetica", size=14, weight="bold")
btn_font = tkfont.Font(family="Helvetica", size=12, weight="bold")

tk.Label(root, text="INFINITY GRID BOT 2025", font=title_font).pack(pady=10)

start_btn = tk.Button(root, text="Start", command=start_trading,
                      bg="green", fg="white", font=btn_font, width=18)
start_btn.pack(pady=8)

stop_btn = tk.Button(root, text="Stop", command=stop_trading,
                     bg="red", fg="white", font=btn_font, width=18)
stop_btn.pack(pady=5)

status_label = tk.Label(root, text="Status: Stopped", fg="red", font=("Helvetica", 11))
status_label.pack(pady=5)

def update_status():
    if running:
        status_label.config(text="Status: Running", fg="green")
    else:
        status_label.config(text="Status: Stopped", fg="red")
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
