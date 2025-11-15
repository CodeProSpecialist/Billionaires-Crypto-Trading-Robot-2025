#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — BINANCE.US
RE-GRIDS ONLY ON ORDER FILLS (NO TIMER, NO PROFIT SCALING)

Updated: adds dynamic fee querying and dynamic sell % to guarantee net profit >= TARGET_PROFIT_PCT
"""
import tkinter as tk
from tkinter import font as tkfont, messagebox
import threading, time, json, os, sys, logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
import pytz, requests, websocket
from decimal import Decimal, getcontext, ROUND_DOWN
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ----------------------------------------------------------------------
# ========================= CONFIG & GLOBALS =========================
# ----------------------------------------------------------------------
getcontext().prec = 28
ZERO = Decimal('0')
ONE  = Decimal('1')
SAFETY_BUFFER = Decimal('0.95')

CASH_USDT_PER_GRID_ORDER = Decimal('5.00')
GRID_BUY_PCT  = Decimal('0.01')   # 1% below (unchanged)
MIN_SELL_PCT  = Decimal('0.018')  # minimum 1.8% above (keeps old behavior)

# New: target net profit after ALL fees (relative to cost including buy-fee)
TARGET_PROFIT_PCT = Decimal('0.018')  # 1.8% target net profit
# If True, assume worst-case taker fees for both buy & sell when computing required sell %
CONSERVATIVE_USE_TAKER = True

# Fee caching (seconds)
FEE_CACHE_TTL = 60 * 30  # 30 minutes

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
listen_key = None
listen_key_lock = threading.Lock()
running = False

# Track our grid orders: {symbol: [orderId, ...]}
active_grid_orders = {}

# Fee cache: {symbol: {'maker': Decimal, 'taker': Decimal, 'ts': epoch}}
_fee_cache = {}

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
        logger.addHandler(fh); logger.addHandler(ch); logger.addHandler(debug_fh)
    return logger

logger = setup_logging()
log_info  = logger.info
log_error = logger.error
log_debug = logger.debug

# ----------------------------------------------------------------------
# ========================= RATE LIMITING =========================
# ----------------------------------------------------------------------
def update_weight_from_response(resp):
    try:
        if isinstance(resp, (list, tuple)): return
        headers = getattr(resp, 'headers', {}) or resp.get('headers', {})
        used = int(headers.get('x-mbx-used-weight-1m', 0))
        with API_WEIGHT_LOCK:
            global API_WEIGHT_CURRENT
            API_WEIGHT_CURRENT = max(API_WEIGHT_CURRENT, used)
        log_debug(f"Weight updated: {API_WEIGHT_CURRENT}")
    except Exception as e:
        log_debug(f"Weight parse failed: {e}")

def apply_delay_and_backoff():
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
    msg  = str(getattr(e, 'message', ''))
    log_error(f"API ERROR {code}: {msg}")
    if code in (429, 418, -1003):
        ban = 60
        if 'retry after' in msg.lower():
            try: ban = int([x for x in msg.split() if x.isdigit()][-1])
            except: pass
        CURRENT_DELAY = min(MAX_DELAY, CURRENT_DELAY * BACKOFF_MULTIPLIER)
        time.sleep(ban + 5)
    elif code >= 500:
        time.sleep(10)

# ----------------------------------------------------------------------
# ========================= UTILS =========================
# ----------------------------------------------------------------------
def now_cst(): return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def send_whatsapp(msg):
    phone = os.getenv('CALLMEBOT_PHONE')
    key   = os.getenv('CALLMEBOT_API_KEY')
    if phone and key:
        try:
            m = requests.utils.quote(msg)
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={m}&apikey={key}", timeout=5)
        except: pass

def _safe_decimal(v, fallback='0'):
    try: return Decimal(str(v))
    except: return Decimal(fallback)

# ----------------------------------------------------------------------
# ========================= BALANCE & SYMBOL INFO =========================
# ----------------------------------------------------------------------
def update_balances():
    global account_balances
    try:
        info = client.get_account()
        update_weight_from_response(info)
        account_balances = {a['asset']: _safe_decimal(a['free'])
                           for a in info['balances'] if _safe_decimal(a['free']) > ZERO}
        log_info(f"USDT: {account_balances.get('USDT', ZERO):.2f}")
        apply_delay_and_backoff()
    except BinanceAPIException as e: handle_api_error(e)
    except Exception as e: log_error(f"Balance error: {e}")

def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()
        update_weight_from_response(info)
        for s in info.get('symbols', []):
            if s.get('quoteAsset') != 'USDT' or s.get('status') != 'TRADING': continue
            filters = {f['filterType']: f for f in s.get('filters', [])}
            step = _safe_decimal(filters.get('LOT_SIZE', {}).get('stepSize', '0'))
            tick = _safe_decimal(filters.get('PRICE_FILTER', {}).get('tickSize', '0'))
            min_qty = _safe_decimal(filters.get('LOT_SIZE', {}).get('minQty', '0'))
            min_notional = _safe_decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            if step == ZERO or tick == ZERO: continue
            symbol_info[s['symbol']] = {
                'stepSize': step, 'tickSize': tick,
                'minQty': min_qty, 'minNotional': min_notional
            }
        log_info(f"Loaded {len(symbol_info)} symbols")
        apply_delay_and_backoff()
    except BinanceAPIException as e: handle_api_error(e)
    except Exception as e: log_error(f"Symbol info error: {e}")

# ----------------------------------------------------------------------
# ========================= TRADE FEE FETCHING & CALC =========================
# ----------------------------------------------------------------------
def _parse_fee_response_item(item):
    """Accepts various shapes returned by API / wrapper and returns (maker, taker) as Decimals (fractions)."""
    try:
        # python-binance often returns 'makerCommission' (string or number) and 'takerCommission'
        maker = item.get('makerCommission') or item.get('maker') or item.get('makerCommissionRate') or item.get('makerCommissionRateStr')
        taker = item.get('takerCommission') or item.get('taker') or item.get('takerCommissionRate') or item.get('takerCommissionRateStr')
        # Some endpoints return strings like '0.001' or integers like '10' meaning 0.001? try to coerce
        m = Decimal(str(maker)) if maker is not None and maker != '' else None
        t = Decimal(str(taker)) if taker is not None and taker != '' else None
        # If values look like basis points (e.g., 10 means 0.001? unlikely) — we won't attempt magical conversions.
        if m is None: m = Decimal('0')
        if t is None: t = Decimal('0')
        return (m, t)
    except Exception as e:
        log_debug(f"Fee parse error: {e}")
        return (Decimal('0'), Decimal('0'))

def fetch_trade_fees_for_symbol(symbol):
    """
    Return dict {'maker': Decimal(...), 'taker': Decimal(...)}
    Caches results for FEE_CACHE_TTL seconds.
    """
    now_ts = int(time.time())
    cached = _fee_cache.get(symbol)
    if cached and now_ts - cached['ts'] < FEE_CACHE_TTL:
        return {'maker': cached['maker'], 'taker': cached['taker']}

    try:
        # Primary: use python-binance wrapper method
        resp = client.get_trade_fee(symbol=symbol)
        update_weight_from_response(resp)
        # client.get_trade_fee usually returns a list like [{'symbol':'ETHUSDT', 'makerCommission':'0','takerCommission':'0.001'}]
        if isinstance(resp, list) and resp:
            maker, taker = _parse_fee_response_item(resp[0])
        elif isinstance(resp, dict):
            # sometimes might return dict
            maker, taker = _parse_fee_response_item(resp)
        else:
            maker, taker = (Decimal('0'), Decimal('0'))
        apply_delay_and_backoff()
    except Exception as e:
        log_error(f"Failed to fetch trade fee via client.get_trade_fee for {symbol}: {e}")
        # fallback defaults (conservative small fees)
        maker, taker = (Decimal('0.000'), Decimal('0.001'))
    # Normalize: ensure maker/taker are fractional (0.001 means 0.1%)
    # Some APIs return percentages as '0' or '0.001' already — we trust direct parse.
    # Save to cache
    _fee_cache[symbol] = {'maker': maker, 'taker': taker, 'ts': now_ts}
    log_info(f"Fees {symbol}: maker={maker} taker={taker}")
    return {'maker': maker, 'taker': taker}

def compute_required_sell_pct(symbol, target=TARGET_PROFIT_PCT, conservative=CONSERVATIVE_USE_TAKER):
    """
    Compute required sell % (fraction, e.g. 0.02 == 2%) to achieve net profit >= target
    after accounting for fees on buy and sell.
    Formula:
      required_multiplier = (1 + target) * (1 + buy_fee) / (1 - sell_fee)
      required_pct = required_multiplier - 1
    buy_fee and sell_fee chosen as maker/taker depending on conservative flag.
    """
    fees = fetch_trade_fees_for_symbol(symbol)
    if conservative:
        buy_fee = fees.get('taker', Decimal('0'))
        sell_fee = fees.get('taker', Decimal('0'))
    else:
        buy_fee = fees.get('maker', Decimal('0'))
        sell_fee = fees.get('maker', Decimal('0'))

    # Safety: ensure fees are decimals >= 0 and < 1
    if buy_fee < ZERO: buy_fee = ZERO
    if sell_fee < ZERO: sell_fee = ZERO
    if buy_fee >= ONE: buy_fee = buy_fee / Decimal('100')  # try to correct percent->frac mistakes
    if sell_fee >= ONE: sell_fee = sell_fee / Decimal('100')

    # required multiplier
    try:
        required_multiplier = (ONE + target) * (ONE + buy_fee) / (ONE - sell_fee)
        required_pct = required_multiplier - ONE
    except Exception as e:
        log_error(f"Error computing required sell pct for {symbol}: {e}")
        required_pct = MIN_SELL_PCT

    # If result is NaN or negative, fallback
    if required_pct.is_nan() or required_pct < ZERO:
        required_pct = MIN_SELL_PCT

    log_debug(f"Computed required sell pct for {symbol}: {required_pct:.6f} (buy_fee={buy_fee}, sell_fee={sell_fee}, conservative={conservative})")
    # Ensure at least MIN_SELL_PCT
    if required_pct < MIN_SELL_PCT:
        log_info(f"Required sell pct {required_pct:.6%} lower than MIN {MIN_SELL_PCT:.6%} — using MIN")
        return MIN_SELL_PCT
    return required_pct

# ----------------------------------------------------------------------
# ========================= ORDER PLACEMENT =========================
# ----------------------------------------------------------------------
def round_down(val: Decimal, step: Decimal) -> Decimal:
    return (val // step) * step if step > ZERO else val

def format_decimal(d: Decimal) -> str:
    s = f"{d:f}"
    return s.rstrip('0').rstrip('.') if '.' in s else s

def place_limit_order(symbol, side, price, qty, track=True):
    if USE_PAPER_TRADING:
        log_info(f"[PAPER] {side} {symbol} {qty} @ {price}")
        order = {'orderId': f"paper_{int(time.time()*1000)}", 'symbol': symbol}
        if track: active_grid_orders.setdefault(symbol, []).append(order['orderId'])
        return order

    info = symbol_info.get(symbol)
    if not info:
        log_error(f"NO SYMBOL INFO: {symbol}")
        return None

    price = round_down(Decimal(price), info['tickSize'])
    qty   = round_down(Decimal(qty),   info['stepSize'])

    if qty <= info['minQty']:
        log_error(f"QTY TOO SMALL {qty} <= {info['minQty']}")
        return None
    notional = price * qty
    if notional < info['minNotional']:
        log_error(f"NOTIONAL TOO LOW {notional} < {info['minNotional']}")
        return None

    if side == 'BUY':
        needed = notional * SAFETY_BUFFER
        if needed > account_balances.get('USDT', ZERO):
            log_error(f"NOT ENOUGH USDT {needed} > {account_balances.get('USDT', ZERO)}")
            return None
    else:
        base = symbol.replace('USDT', '')
        if qty > account_balances.get(base, ZERO) * SAFETY_BUFFER:
            log_error(f"NOT ENOUGH {base} {qty} > {account_balances.get(base, ZERO)}")
            return None

    try:
        order = client.create_order(
            symbol=symbol, side=side, type='LIMIT',
            timeInForce='GTC',
            quantity=format_decimal(qty),
            price=format_decimal(price)
        )
        update_weight_from_response(order)
        log_info(f"PLACED {side} {symbol} {qty} @ {price}")
        send_whatsapp(f"{side} {symbol} {qty}@{price}")
        apply_delay_and_backoff()
        if track:
            active_grid_orders.setdefault(symbol, []).append(order['orderId'])
        return order
    except BinanceAPIException as e:
        handle_api_error(e)
        return None
    except Exception as e:
        log_error(f"ORDER FAILED {symbol}: {e}")
        return None

# ----------------------------------------------------------------------
# ========================= GRID HELPERS =========================
# ----------------------------------------------------------------------
def cancel_symbol_orders(symbol):
    try:
        open_orders = client.get_open_orders(symbol=symbol)
        update_weight_from_response(open_orders)
        canceled = 0
        for o in open_orders:
            try:
                client.cancel_order(symbol=o['symbol'], orderId=o['orderId'])
                apply_delay_and_backoff()
                canceled += 1
            except: pass
        if canceled: log_info(f"Canceled {canceled} orders for {symbol}")
        active_grid_orders[symbol] = []
    except Exception as e:
        log_error(f"Cancel failed {symbol}: {e}")

def place_single_grid(symbol, side):
    try:
        cur = client.get_symbol_ticker(symbol=symbol)
        update_weight_from_response(cur)
        cur_price = Decimal(cur['price'])
        apply_delay_and_backoff()
    except Exception as e:
        log_error(f"Price fetch failed {symbol}: {e}")
        return

    info = symbol_info.get(symbol)
    if not info: return

    if side == 'BUY':
        price = cur_price * (ONE - GRID_BUY_PCT)
    else:
        # compute dynamic sell pct based on fees to guarantee target net profit
        required_sell_pct = compute_required_sell_pct(symbol, target=TARGET_PROFIT_PCT, conservative=CONSERVATIVE_USE_TAKER)
        price = cur_price * (ONE + required_sell_pct)

    price = round_down(price, info['tickSize'])
    qty   = CASH_USDT_PER_GRID_ORDER / price
    qty   = round_down(qty, info['stepSize'])
    if qty <= ZERO: return

    if side == 'SELL':
        base = symbol.replace('USDT', '')
        if qty > account_balances.get(base, ZERO):
            log_debug(f"Insufficient {base} for sell grid")
            return

    order = place_limit_order(symbol, side, price, qty, track=True)
    if order:
        log_info(f"NEW GRID {side} {symbol} {qty} @ {price}")

def regrid_on_fill(symbol):
    log_info(f"FILL DETECTED → REGRIDDING {symbol}")
    cancel_symbol_orders(symbol)
    place_single_grid(symbol, 'BUY')
    place_single_grid(symbol, 'SELL')

def place_initial_grids():
    owned = []
    for asset, free in account_balances.items():
        if asset == 'USDT' or free <= Decimal('0.0001'): continue
        sym = f"{asset}USDT"
        if sym not in symbol_info: continue
        try:
            ticker = client.get_symbol_ticker(symbol=sym)
            update_weight_from_response(ticker)
            price = Decimal(ticker['price'])
            if free * price >= Decimal('1'):
                owned.append(asset)
            apply_delay_and_backoff()
        except Exception as e:
            log_error(f"Price fetch failed {sym}: {e}")

    log_info(f"Placing initial grid for {len(owned)} coins: {owned}")
    for asset in owned:
        symbol = f"{asset}USDT"
        cancel_symbol_orders(symbol)
        place_single_grid(symbol, 'BUY')
        place_single_grid(symbol, 'SELL')

# ----------------------------------------------------------------------
# ========================= USER DATA STREAM (FILL DETECTION) =========================
# ----------------------------------------------------------------------
def keep_alive_listen_key():
    global listen_key
    while running:
        with listen_key_lock:
            if listen_key:
                try: client.stream_keepalive(listen_key)
                except: pass
        time.sleep(1800)

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data.get('e') != 'executionReport': return
        status = data.get('X')
        if status != 'FILLED': return

        symbol = data.get('s')
        side   = data.get('S')
        qty    = _safe_decimal(data.get('q', '0'))
        price  = _safe_decimal(data.get('p', '0'))

        if qty <= ZERO: return

        log_info(f"FILLED {side} {symbol} {qty} @ {price}")
        send_whatsapp(f"FILLED {side} {symbol} {qty}@{price}")

        # REGRID IMMEDIATELY
        threading.Thread(target=regrid_on_fill, args=(symbol,), daemon=True).start()
    except Exception as e:
        log_debug(f"User WS parse error: {e}")

def start_user_data_stream():
    global listen_key
    try:
        res = client.stream_get_listen_key()
        update_weight_from_response(res)
        key = res.get('listenKey') if isinstance(res, dict) else str(res)
        if not key: raise ValueError("No listenKey")
        listen_key = key
        url = f"wss://stream.binance.us:9443/ws/{listen_key}"
        ws = HeartbeatWebSocket(url, on_message=on_user_message)
        ws_instances.append(ws)
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        threading.Thread(target=keep_alive_listen_key, daemon=True).start()
        log_info("User Data Stream STARTED")
    except Exception as e:
        log_error(f"User stream failed: {e}")

# ----------------------------------------------------------------------
# ========================= PRICE WEBSOCKET =========================
# ----------------------------------------------------------------------
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, **kwargs):
        super().__init__(url, **kwargs)
        self._stop = False
    def on_open(self, ws): log_info(f"WS OPEN: {ws.url.split('?')[0]}")
    def run_forever(self, **kwargs):
        while not self._stop:
            try: super().run_forever(ping_interval=25, **kwargs)
            except Exception as e: log_error(f"WS CRASH: {e}")
            time.sleep(5)
    def stop(self): self._stop = True; self.close()

def on_price_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get('stream', '')
        payload = data.get('data', {})
        sym = stream.split('@')[0].upper()
        if stream.endswith('@ticker') and payload:
            price = _safe_decimal(payload.get('c', '0'))
            if price > ZERO:
                with price_lock: live_prices[sym] = price
    except Exception as e: log_debug(f"Price WS parse: {e}")

def start_price_ws(symbols):
    streams = [f"{s.lower()}@ticker" for s in symbols[:500]]
    url = f"wss://stream.binance.us:9443/stream?streams={'/'.join(streams)}"
    ws = HeartbeatWebSocket(url, on_message=on_price_message)
    ws_instances.append(ws)
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    ws_threads.append(t)

# ----------------------------------------------------------------------
# ========================= MAIN CYCLE (ONLY INITIAL + FILLS) =========================
# ----------------------------------------------------------------------
def grid_cycle():
    while running:
        try:
            update_balances()
            if not account_balances.get('USDT', ZERO) > ZERO:
                log_error("No USDT balance. Waiting...")
                time.sleep(30)
                continue

            place_initial_grids()
            log_info("INITIAL GRID PLACED. WAITING FOR FILLS...")

            # No loop, no timer — just sleep and let fills trigger regrids
            while running:
                time.sleep(60)
                update_balances()  # Keep balances fresh

        except Exception as e:
            log_error(f"GRID CRASH: {e}. Restarting in 15s...")
            time.sleep(15)

# ----------------------------------------------------------------------
# ========================= INIT & GUI =========================
# ----------------------------------------------------------------------
def initialise_bot():
    try:
        log_info("INITIALISING GRID BOT...")
        load_symbol_info()
        update_balances()
        start_price_ws(list(symbol_info.keys()))
        start_user_data_stream()
        log_info("GRID BOT READY")
        send_whatsapp("GRID BOT STARTED (FILL-ONLY MODE)")
    except Exception as e: log_error(f"Init failed: {e}")

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
    for sym in list(active_grid_orders.keys()): cancel_symbol_orders(sym)
    for ws in ws_instances: ws.stop()
    status_label.config(text="Status: Stopped", fg="red")

# ---- GUI ----
root = tk.Tk()
root.title("INFINITY GRID BOT 2025")
root.geometry("460x180"); root.resizable(False, False)
tk.Label(root, text="INFINITY GRID BOT", font=tkfont.Font(size=14, weight="bold")).pack(pady=10)
tk.Button(root, text="Start", command=start_trading, bg="green", fg="white",
          font=tkfont.Font(size=12, weight="bold"), width=18).pack(pady=8)
tk.Button(root, text="Stop",  command=stop_trading, bg="red",   fg="white",
          font=tkfont.Font(size=12, weight="bold"), width=18).pack(pady=5)
status_label = tk.Label(root, text="Status: Stopped", fg="red")
status_label.pack(pady=5)

def update_status():
    status_label.config(text="Status: Running" if running else "Status: Stopped",
                        fg="green" if running else "red")
    root.after(1000, update_status)
update_status()

# ----------------------------------------------------------------------
# ========================= ENTRY POINT =========================
# ----------------------------------------------------------------------
if __name__ == "__main__":
    api_key    = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')
    if not api_key or not api_secret:
        messagebox.showerror("API Error", "Set BINANCE_API_KEY and BINANCE_API_SECRET")
        sys.exit(1)
    client = Client(api_key, api_secret, tld='us')
    root.mainloop()
