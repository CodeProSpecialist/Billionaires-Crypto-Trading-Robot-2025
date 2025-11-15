#!/usr/bin/env python3
"""
Infinity Grid Bot 2025 — Binance.US
Fully automated end-to-end
"""
import os, sys, time, json, threading, logging, requests, pytz
from datetime import datetime
from decimal import Decimal, getcontext, ROUND_DOWN
from binance.client import Client
from binance.exceptions import BinanceAPIException
import websocket
import tkinter as tk
from tkinter import font as tkfont, messagebox

# ----------------------------------------------------------------------
# ========================= CONFIG & GLOBALS =========================
# ----------------------------------------------------------------------
getcontext().prec = 28
ZERO = Decimal('0')
ONE  = Decimal('1')
SAFETY_BUFFER = Decimal('0.95')
TARGET_PROFIT_PCT = Decimal('0.018')  # 1.8% net profit target
GRID_BUY_PCT = Decimal('0.01')        # 1% below current price
CASH_USDT_PER_GRID_ORDER = Decimal('8.00')  # USDT per buy order
REGRID_ON_FILL = True
EXCLUDED_COINS = {'BTC', 'ETH', 'SOL', 'USDT', 'USD', 'BCH', 'BNB'}  # avoid buying

# Fee cache: {symbol: {'maker': Decimal, 'taker': Decimal, 'ts': epoch}}
FEE_CACHE_TTL = 1800  # 30 min
_fee_cache = {}

CST = pytz.timezone('America/Chicago')
account_balances = {}
symbol_info = {}
live_prices = {}
price_lock = threading.Lock()
ws_instances = []
ws_threads = []
listen_key = None
listen_key_lock = threading.Lock()
running = False
active_grid_orders = {}  # {symbol: [orderId,...]}

# Logging
LOG_FILE = os.path.expanduser("~/infinity_grid.log")
os.makedirs("logs", exist_ok=True)
logger = logging.getLogger("infinity_grid")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
    logger.addHandler(fh); logger.addHandler(ch)

log_info = logger.info
log_error = logger.error
log_debug = logger.debug

# ----------------------------------------------------------------------
# ========================= UTILITY FUNCTIONS =========================
# ----------------------------------------------------------------------
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def _safe_decimal(val, fallback='0'):
    try:
        return Decimal(str(val))
    except:
        return Decimal(fallback)

def round_down(val: Decimal, step: Decimal) -> Decimal:
    return (val // step) * step if step > ZERO else val

def format_decimal(d: Decimal) -> str:
    s = f"{d:.8f}"
    return s.rstrip('0').rstrip('.') if '.' in s else s

def send_whatsapp(msg):
    phone = os.getenv('CALLMEBOT_PHONE')
    key   = os.getenv('CALLMEBOT_API_KEY')
    if phone and key:
        try:
            m = requests.utils.quote(msg)
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={m}&apikey={key}", timeout=5)
        except: pass


# ----------------------------------------------------------------------
# ========================= BINANCE CLIENT =========================
# ----------------------------------------------------------------------
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
if not api_key or not api_secret:
    messagebox.showerror("API Error", "Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

client = Client(api_key, api_secret, tld='us')

# ----------------------------------------------------------------------
# ========================= SYMBOL INFO & BALANCES ====================
# ----------------------------------------------------------------------
def load_symbol_info():
    """Load all USDT trading pairs and their filters from Binance.US"""
    global symbol_info
    try:
        info = client.get_exchange_info()
        for s in info.get('symbols', []):
            if s.get('quoteAsset') != 'USDT' or s.get('status') != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s.get('filters', [])}
            step = _safe_decimal(filters.get('LOT_SIZE', {}).get('stepSize', '0'))
            tick = _safe_decimal(filters.get('PRICE_FILTER', {}).get('tickSize', '0'))
            min_qty = _safe_decimal(filters.get('LOT_SIZE', {}).get('minQty', '0'))
            min_notional = _safe_decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            if step == ZERO or tick == ZERO: continue
            symbol_info[s['symbol']] = {
                'stepSize': step,
                'tickSize': tick,
                'minQty': min_qty,
                'minNotional': min_notional
            }
        log_info(f"Loaded {len(symbol_info)} symbols")
    except Exception as e:
        log_error(f"Error loading symbols: {e}")

def update_balances():
    """Update account balances for all assets"""
    global account_balances
    try:
        info = client.get_account()
        account_balances = {a['asset']: _safe_decimal(a['free'])
                            for a in info['balances'] if _safe_decimal(a['free']) > ZERO}
        log_info(f"USDT balance: {account_balances.get('USDT', ZERO):.2f}")
    except Exception as e:
        log_error(f"Balance fetch error: {e}")

# ----------------------------------------------------------------------
# ========================= FEES & PROFIT =========================
# ----------------------------------------------------------------------
def fetch_trade_fees(symbol):
    """Return dict {'maker': Decimal, 'taker': Decimal} with caching"""
    now_ts = int(time.time())
    cached = _fee_cache.get(symbol)
    if cached and now_ts - cached['ts'] < FEE_CACHE_TTL:
        return {'maker': cached['maker'], 'taker': cached['taker']}
    try:
        resp = client.get_trade_fee(symbol=symbol)
        if isinstance(resp, list) and resp:
            fee_data = resp[0]
            maker = _safe_decimal(fee_data.get('makerCommission', 0))
            taker = _safe_decimal(fee_data.get('takerCommission', 0))
        else:
            maker = taker = Decimal('0.001')
    except Exception as e:
        log_error(f"Fee fetch failed for {symbol}: {e}")
        maker = taker = Decimal('0.001')
    _fee_cache[symbol] = {'maker': maker, 'taker': taker, 'ts': now_ts}
    return {'maker': maker, 'taker': taker}

def compute_required_sell_pct(symbol, target=TARGET_PROFIT_PCT):
    """Compute required sell % to achieve target net profit after fees"""
    fees = fetch_trade_fees(symbol)
    buy_fee = fees['taker']
    sell_fee = fees['taker']
    required_multiplier = (ONE + target) * (ONE + buy_fee) / (ONE - sell_fee)
    required_pct = required_multiplier - ONE
    if required_pct < Decimal('0.018'):
        required_pct = Decimal('0.018')
    return required_pct

# ----------------------------------------------------------------------
# ========================= ORDER PLACEMENT =========================
# ----------------------------------------------------------------------
def place_limit_order(symbol, side, price, qty):
    """Place a limit order (live)"""
    info = symbol_info.get(symbol)
    if not info:
        log_error(f"No symbol info for {symbol}")
        return None
    price = round_down(Decimal(price), info['tickSize'])
    qty = round_down(Decimal(qty), info['stepSize'])
    if qty <= info['minQty']:
        log_error(f"Order qty too low {qty} <= {info['minQty']}")
        return None
    notional = price * qty
    if notional < info['minNotional']:
        log_error(f"Order notional too low {notional} < {info['minNotional']}")
        return None

    if side == 'BUY' and notional * SAFETY_BUFFER > account_balances.get('USDT', ZERO):
        log_error(f"Insufficient USDT for buy {notional}")
        return None
    elif side == 'SELL':
        base = symbol.replace('USDT', '')
        if qty > account_balances.get(base, ZERO) * SAFETY_BUFFER:
            log_error(f"Insufficient {base} for sell {qty}")
            return None

    try:
        order = client.create_order(
            symbol=symbol, side=side, type='LIMIT', timeInForce='GTC',
            quantity=format_decimal(qty), price=format_decimal(price)
        )
        log_info(f"Placed {side} {symbol} {qty} @ {price}")
        active_grid_orders.setdefault(symbol, []).append(order['orderId'])
        return order
    except BinanceAPIException as e:
        log_error(f"Binance API error {e}")
    except Exception as e:
        log_error(f"Order failed {symbol}: {e}")
    return None

# ----------------------------------------------------------------------
# ========================= CANCEL ORDERS =========================
# ----------------------------------------------------------------------
def cancel_symbol_orders(symbol):
    """Cancel all open orders for a symbol"""
    try:
        open_orders = client.get_open_orders(symbol=symbol)
        canceled = 0
        for o in open_orders:
            try:
                client.cancel_order(symbol=o['symbol'], orderId=o['orderId'])
                canceled += 1
            except: pass
        if canceled: log_info(f"Canceled {canceled} open orders for {symbol}")
        active_grid_orders[symbol] = []
    except Exception as e:
        log_error(f"Cancel failed {symbol}: {e}")


# ----------------------------------------------------------------------
# ========================= TOP COINS FETCHING =========================
# ----------------------------------------------------------------------
def fetch_top_coingecko_coins():
    """Fetch top coins by market cap, 24h volume, and 5/14-day gains"""
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 100,
            "page": 1,
            "sparkline": False,
            "price_change_percentage": "5d,14d"
        }
        resp = requests.get(url, params=params, timeout=10).json()
        coins = []
        for coin in resp:
            coins.append({
                "symbol": coin['symbol'].upper(),
                "market_cap": coin['market_cap'],
                "volume": coin['total_volume'],
                "pct_5d": coin.get('price_change_percentage_5d_in_currency', 0),
                "pct_14d": coin.get('price_change_percentage_14d_in_currency', 0)
            })
        log_info(f"Fetched {len(coins)} coins from CoinGecko")
        return coins
    except Exception as e:
        log_error(f"CoinGecko fetch failed: {e}")
        return []

def fetch_top_coinbase_coins():
    """Fetch top coins by volume from Coinbase"""
    try:
        url = "https://api.pro.coinbase.com/products"
        resp = requests.get(url, timeout=10).json()
        coins = [p['base_currency'].upper() for p in resp if p['quote_currency'] == 'USD']
        log_info(f"Fetched {len(coins)} coins from Coinbase")
        return coins
    except Exception as e:
        log_error(f"Coinbase fetch failed: {e}")
        return []

# ----------------------------------------------------------------------
# ========================= DETERMINE ELIGIBLE COINS ==================
# ----------------------------------------------------------------------
EXCLUDE_COINS = {'USDT', 'USD', 'BTC', 'ETH', 'BCH', 'BNB', 'SOL'}

def filter_top_coins():
    """Combine top lists, exclude stablecoins/major coins, and select top 25"""
    cg = fetch_top_coingecko_coins()
    cb = fetch_top_coinbase_coins()

    combined = {}
    for coin in cg:
        symbol = coin['symbol']
        if symbol in EXCLUDE_COINS: continue
        combined[symbol] = coin

    for symbol in cb:
        if symbol in EXCLUDE_COINS: continue
        if symbol not in combined:
            combined[symbol] = {"symbol": symbol, "market_cap": 0, "volume": 0, "pct_5d": 0, "pct_14d": 0}

    # Rank top by market_cap + volume + 5d/14d pct sum
    def rank_key(c): return (
        c.get('market_cap', 0),
        c.get('volume', 0),
        c.get('pct_5d', 0),
        c.get('pct_14d', 0)
    )
    sorted_coins = sorted(combined.values(), key=rank_key, reverse=True)
    top25 = sorted_coins[:25]

    # Save to file
    with open("coins-to-buy-today.txt", "w") as f:
        for c in top25:
            f.write(f"{c['symbol']}\n")
    log_info("Saved top 25 eligible coins to coins-to-buy-today.txt")
    return [c['symbol'] for c in top25]

# ----------------------------------------------------------------------
# ========================= GRID PLACEMENT =========================
# ----------------------------------------------------------------------
CASH_USDT_PER_GRID_ORDER = Decimal('8.00')  # reserve 33% buying power per order

def place_single_grid(symbol, side):
    """Place a single grid order, either BUY or SELL"""
    cancel_symbol_orders(symbol)  # cancel old grid orders first
    try:
        ticker = client.get_symbol_ticker(symbol=f"{symbol}USDT")
        price = Decimal(ticker['price'])
    except Exception as e:
        log_error(f"Price fetch failed for {symbol}: {e}")
        return

    info = symbol_info.get(f"{symbol}USDT")
    if not info: return

    if side == 'BUY':
        grid_price = price * (ONE - GRID_BUY_PCT)
        qty = CASH_USDT_PER_GRID_ORDER / grid_price
    else:  # SELL
        required_pct = compute_required_sell_pct(f"{symbol}USDT")
        grid_price = price * (ONE + required_pct)
        qty = account_balances.get(symbol, ZERO)

    grid_price = round_down(grid_price, info['tickSize'])
    qty = round_down(qty, info['stepSize'])
    if qty <= ZERO: return

    place_limit_order(f"{symbol}USDT", side, grid_price, qty)


# ----------------------------------------------------------------------
# ========================= LIVE DATA WEBSOCKETS ======================
# ----------------------------------------------------------------------
class HeartbeatWebSocket(websocket.WebSocketApp):
    """WebSocket wrapper with heartbeat and reconnect logic"""
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
    """Parse live price ticker message"""
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
    """Start WebSocket for live price updates"""
    streams = [f"{s.lower()}@ticker" for s in symbols[:500]]
    url = f"wss://stream.binance.us:9443/stream?streams={'/'.join(streams)}"
    ws = HeartbeatWebSocket(url, on_message=on_price_message)
    ws_instances.append(ws)
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    ws_threads.append(t)


# ----------------------------------------------------------------------
# ========================= USER DATA STREAM ===========================
# ----------------------------------------------------------------------
def on_user_message(ws, message):
    """Detect order fills and trigger regrid"""
    try:
        data = json.loads(message)
        if data.get('e') != 'executionReport': return
        if data.get('X') != 'FILLED': return

        symbol = data.get('s')
        side   = data.get('S')
        qty    = _safe_decimal(data.get('q', '0'))
        price  = _safe_decimal(data.get('p', '0'))
        if qty <= ZERO: return

        log_info(f"FILLED {side} {symbol} {qty} @ {price}")
        send_whatsapp(f"FILLED {side} {symbol} {qty}@{price}")

        threading.Thread(target=regrid_on_fill, args=(symbol,), daemon=True).start()
    except Exception as e:
        log_debug(f"User WS parse error: {e}")


def start_user_data_stream():
    """Start user data stream to detect fills"""
    global listen_key
    try:
        res = client.stream_get_listen_key()
        listen_key = res.get('listenKey') if isinstance(res, dict) else str(res)
        if not listen_key: raise ValueError("No listenKey")
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


def keep_alive_listen_key():
    """Send keepalive ping for user stream"""
    global listen_key
    while running:
        if listen_key:
            try:
                client.stream_keepalive(listen_key)
            except: pass
        time.sleep(1800)


def regrid_on_fill(symbol):
    """Cancel old orders and place new grid orders after a fill"""
    log_info(f"FILL DETECTED → REGRIDDING {symbol}")
    cancel_symbol_orders(symbol)
    place_single_grid(symbol.replace('USDT',''), 'BUY')
    place_single_grid(symbol.replace('USDT',''), 'SELL')


# ----------------------------------------------------------------------
# ========================= GUI SETUP (1024x768) ======================
# ----------------------------------------------------------------------
root = tk.Tk()
root.title("INFINITY GRID BOT 2025")
root.geometry("1024x768")
root.resizable(True, True)

tk.Label(root, text="INFINITY GRID BOT", font=tkfont.Font(size=24, weight="bold")).pack(pady=20)

status_label = tk.Label(root, text="Status: Stopped", font=tkfont.Font(size=18), fg="red")
status_label.pack(pady=10)

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
    running = False
    log_info("STOP PRESSED")
    for sym in list(active_grid_orders.keys()): cancel_symbol_orders(sym)
    for ws in ws_instances: ws.stop()
    status_label.config(text="Status: Stopped", fg="red")

tk.Button(root, text="Start", command=start_trading, bg="green", fg="white",
          font=tkfont.Font(size=16, weight="bold"), width=20).pack(pady=10)
tk.Button(root, text="Stop",  command=stop_trading, bg="red", fg="white",
          font=tkfont.Font(size=16, weight="bold"), width=20).pack(pady=5)

def update_status():
    status_label.config(text="Status: Running" if running else "Status: Stopped",
                        fg="green" if running else "red")
    root.after(1000, update_status)
update_status()


# ----------------------------------------------------------------------
# ========================= INITIALISATION & MAIN =====================
# ----------------------------------------------------------------------
def initialise_bot():
    """Load symbols, balances, and start websockets"""
    try:
        log_info("INITIALISING GRID BOT...")
        load_symbol_info()
        update_balances()
        start_price_ws(list(symbol_info.keys()))
        start_user_data_stream()
        log_info("GRID BOT READY")
        send_whatsapp("GRID BOT STARTED (FILL-ONLY MODE)")
    except Exception as e:
        log_error(f"Init failed: {e}")


def grid_cycle():
    """Main grid loop: place initial grids and update balances every 3 minutes"""
    while running:
        try:
            update_balances()
            usdt_balance = account_balances.get('USDT', ZERO)
            if usdt_balance <= ZERO:
                log_error("No USDT balance. Waiting 30s...")
                time.sleep(30)
                continue

            # Place initial grids for owned coins
            place_initial_grids()

            log_info("INITIAL GRID PLACED. Waiting for fills...")
            # Sleep 3 minutes before next balance + grid check
            sleep_time = 180
            while running and sleep_time > 0:
                time.sleep(1)
                sleep_time -= 1
                update_balances()  # keep balances fresh every 3 min

        except Exception as e:
            log_error(f"GRID CYCLE CRASH: {e}. Restarting in 15s...")
            time.sleep(15)


def start_main():
    """Start the Tkinter GUI and bot"""
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')
    if not api_key or not api_secret:
        messagebox.showerror("API Error", "Set BINANCE_API_KEY and BINANCE_API_SECRET")
        sys.exit(1)

    global client
    client = Client(api_key, api_secret, tld='us')

    log_info("Starting INFINITY GRID BOT GUI...")
    root.mainloop()


# ----------------------------------------------------------------------
# ========================= ENTRY POINT =================================
# ----------------------------------------------------------------------
if __name__ == "__main__":
    start_main()
