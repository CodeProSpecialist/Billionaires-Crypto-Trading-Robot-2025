#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — BINANCE.US + Top Coin Fetching
Full automated trading with Tkinter dashboard
"""

import os, sys, time, threading, json, requests
from datetime import datetime, timedelta
from decimal import Decimal, getcontext, ROUND_DOWN
import tkinter as tk
from tkinter import font as tkfont, messagebox
import pytz, websocket
from binance.client import Client
from binance.exceptions import BinanceAPIException

# -------------------- DECIMAL CONTEXT --------------------
getcontext().prec = 28
ZERO = Decimal('0')
ONE  = Decimal('1')
SAFETY_BUFFER = Decimal('0.95')
CASH_USDT_PER_GRID_ORDER = Decimal('8.0')  # Reserve $8 per grid order
GRID_BUY_PCT  = Decimal('0.01')   # 1% below
MIN_SELL_PCT  = Decimal('0.018')  # minimum 1.8%
TARGET_PROFIT_PCT = Decimal('0.018')  # net profit target after fees

CONSERVATIVE_USE_TAKER = True
FEE_CACHE_TTL = 60*30  # 30 minutes

EXCLUDED_COINS = {"USD","USDT","BTC","BCH","ETH","SOL"}

CST = pytz.timezone("America/Chicago")

# -------------------- BINANCE CLIENT --------------------
api_key    = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
if not api_key or not api_secret:
    messagebox.showerror("API Error", "Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)
client = Client(api_key, api_secret, tld='us')

# -------------------- GLOBAL STATE --------------------
symbol_info = {}        # symbol -> info dict
account_balances = {}   # asset -> Decimal balance
active_grid_orders = {} # symbol -> [orderId,...]
_fee_cache = {}         # symbol -> {'maker': Decimal,'taker':Decimal,'ts':epoch}
live_prices = {}        # symbol -> Decimal
running = False

# -------------------- TKINTER GUI --------------------
root = tk.Tk()
root.title("INFINITY GRID BOT 2025")
root.geometry("1024x768")
root.configure(bg="#111111")

title_font = tkfont.Font(size=18, weight="bold")
button_font = tkfont.Font(size=14, weight="bold")
label_font = tkfont.Font(size=12)

# Scrollable frame utility
def create_scrollable_frame(parent, bg="#222222"):
    canvas = tk.Canvas(parent, borderwidth=0, background=bg)
    frame = tk.Frame(canvas, background=bg)
    scrollbar = tk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    canvas.create_window((0,0), window=frame, anchor="nw")
    
    def on_frame_configure(event):
        canvas.configure(scrollregion=canvas.bbox("all"))
    frame.bind("<Configure>", on_frame_configure)
    return frame, canvas

# -------------------- GUI LAYOUT --------------------
tk.Label(root, text="INFINITY GRID BOT 2025", font=title_font, fg="white", bg="#111111").pack(pady=10)

# Stats Frame
stats_outer_frame = tk.Frame(root, bg="#111111", height=150)
stats_outer_frame.pack(fill="x", padx=10, pady=5)
stats_scroll_frame, stats_canvas = create_scrollable_frame(stats_outer_frame, bg="#111111")

usdt_label = tk.Label(stats_scroll_frame, text="USDT Balance: 0.00", fg="lime", bg="#111111", font=label_font, anchor="w")
usdt_label.pack(fill="x", pady=2)
coins_label = tk.Label(stats_scroll_frame, text="Active Coins: 0", fg="lime", bg="#111111", font=label_font, anchor="w")
coins_label.pack(fill="x", pady=2)
top_coins_label = tk.Label(stats_scroll_frame, text="Top Coins: Loading...", fg="lime", bg="#111111", font=label_font, anchor="w", justify="left", wraplength=1000)
top_coins_label.pack(fill="x", pady=2)

# Terminal Frame
terminal_outer_frame = tk.Frame(root, bg="#111111")
terminal_outer_frame.pack(fill="both", expand=True, padx=10, pady=5)
terminal_scroll_frame, terminal_canvas = create_scrollable_frame(terminal_outer_frame, bg="#000000")
terminal_text = tk.Text(terminal_scroll_frame, wrap=tk.WORD, bg="black", fg="lime", font=("Courier",12))
terminal_text.pack(fill="both", expand=True)

def terminal_insert(msg):
    terminal_text.insert(tk.END, f"{msg}\n")
    terminal_text.see(tk.END)

# Buttons
button_frame = tk.Frame(root, bg="#111111")
button_frame.pack(pady=10)
status_label = tk.Label(button_frame, text="Status: Stopped", fg="red", bg="#111111", font=label_font)
status_label.pack(side="left", padx=5)


# -------------------- UTILS --------------------
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def _safe_decimal(val, fallback='0'):
    try:
        return Decimal(str(val))
    except:
        return Decimal(fallback)

# -------------------- BINANCE SYMBOL INFO --------------------
def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()
        for s in info.get('symbols', []):
            if s.get('quoteAsset') != 'USDT' or s.get('status') != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s.get('filters', [])}
            step = _safe_decimal(filters.get('LOT_SIZE', {}).get('stepSize','0'))
            tick = _safe_decimal(filters.get('PRICE_FILTER', {}).get('tickSize','0'))
            min_qty = _safe_decimal(filters.get('LOT_SIZE', {}).get('minQty','0'))
            min_notional = _safe_decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional','10'))
            if step == ZERO or tick == ZERO: continue
            symbol_info[s['symbol']] = {
                'stepSize': step,
                'tickSize': tick,
                'minQty': min_qty,
                'minNotional': min_notional
            }
        terminal_insert(f"[{now_cst()}] Loaded {len(symbol_info)} USDT trading symbols")
    except Exception as e:
        terminal_insert(f"[{now_cst()}] ERROR loading symbol info: {e}")

# -------------------- ACCOUNT BALANCES --------------------
def update_balances():
    global account_balances
    try:
        info = client.get_account()
        account_balances = {a['asset']: _safe_decimal(a['free'])
                            for a in info['balances'] if _safe_decimal(a['free']) > ZERO}
        usdt_label.config(text=f"USDT Balance: {account_balances.get('USDT', ZERO):.2f}")
    except Exception as e:
        terminal_insert(f"[{now_cst()}] ERROR fetching balances: {e}")

# -------------------- FEE FETCHING --------------------
def fetch_trade_fees_for_symbol(symbol):
    now_ts = int(time.time())
    cached = _fee_cache.get(symbol)
    if cached and now_ts - cached['ts'] < FEE_CACHE_TTL:
        return {'maker': cached['maker'], 'taker': cached['taker']}
    try:
        resp = client.get_trade_fee(symbol=symbol)
        if isinstance(resp, list) and resp:
            maker = Decimal(resp[0].get('makerCommission', '0'))
            taker = Decimal(resp[0].get('takerCommission', '0'))
        else:
            maker = taker = Decimal('0.001')
        _fee_cache[symbol] = {'maker': maker, 'taker': taker, 'ts': now_ts}
        return {'maker': maker, 'taker': taker}
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Fee fetch error {symbol}: {e}")
        return {'maker': Decimal('0.0'), 'taker': Decimal('0.001')}

def compute_required_sell_pct(symbol, target=TARGET_PROFIT_PCT):
    fees = fetch_trade_fees_for_symbol(symbol)
    buy_fee = fees['taker'] if CONSERVATIVE_USE_TAKER else fees['maker']
    sell_fee = fees['taker'] if CONSERVATIVE_USE_TAKER else fees['maker']
    try:
        req_multiplier = (ONE + target) * (ONE + buy_fee) / (ONE - sell_fee)
        req_pct = req_multiplier - ONE
        if req_pct < MIN_SELL_PCT:
            req_pct = MIN_SELL_PCT
        return req_pct
    except:
        return MIN_SELL_PCT

# -------------------- COIN FETCHING --------------------
coingecko_cache = {'top_coins': [], 'ts': datetime.min}

def fetch_top_coins():
    """Fetch top 25 coins by market cap, volume, 24h and 5-day % gain from CoinGecko + Coinbase"""
    now_time = datetime.utcnow()
    if (now_time - coingecko_cache['ts']).total_seconds() < 180:  # 3 minutes cache
        return coingecko_cache['top_coins']
    try:
        # CoinGecko
        cg_url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {"vs_currency":"usd","order":"market_cap_desc","per_page":100,"page":1,"sparkline":"false","price_change_percentage":"24h,5d,14d"}
        resp = requests.get(cg_url, params=params, timeout=10)
        coins = resp.json()
        # Sort by combined metric: top market cap + volume + 24h gain + 5d gain
        coins_sorted = sorted(coins, key=lambda x: (
            _safe_decimal(x.get('market_cap',0)),
            _safe_decimal(x.get('total_volume',0)),
            _safe_decimal(x.get('price_change_percentage_24h',0)),
            _safe_decimal(x.get('price_change_percentage_5d',0))
        ), reverse=True)
        top_coins = [c['symbol'].upper() for c in coins_sorted if c['symbol'].upper() not in EXCLUDED_COINS][:25]
        coingecko_cache['top_coins'] = top_coins
        coingecko_cache['ts'] = now_time
        top_coins_label.config(text="Top Coins: " + ", ".join(top_coins))
        terminal_insert(f"[{now_cst()}] Fetched top coins: {top_coins}")
        return top_coins
    except Exception as e:
        terminal_insert(f"[{now_cst()}] ERROR fetching top coins: {e}")
        return coingecko_cache.get('top_coins', [])

# -------------------- HELPER FUNCTIONS --------------------
def round_down(val: Decimal, step: Decimal) -> Decimal:
    return (val // step) * step if step > ZERO else val

def format_decimal(d: Decimal) -> str:
    s = f"{d:.8f}"
    return s.rstrip('0').rstrip('.') if '.' in s else s


# -------------------- ORDER PLACEMENT --------------------
def place_limit_order(symbol, side, price, qty, track=True):
    info = symbol_info.get(symbol)
    if not info: 
        terminal_insert(f"[{now_cst()}] ERROR: No symbol info for {symbol}")
        return None

    price = round_down(Decimal(price), info['tickSize'])
    qty   = round_down(Decimal(qty), info['stepSize'])

    if qty <= info['minQty']:
        terminal_insert(f"[{now_cst()}] ERROR: QTY {qty} too small for {symbol}")
        return None
    notional = price * qty
    if notional < info['minNotional']:
        terminal_insert(f"[{now_cst()}] ERROR: NOTIONAL {notional} < minNotional for {symbol}")
        return None

    if side == 'BUY' and notional > account_balances.get('USDT', ZERO):
        terminal_insert(f"[{now_cst()}] ERROR: Not enough USDT to buy {symbol}")
        return None
    if side == 'SELL':
        base = symbol.replace('USDT','')
        if qty > account_balances.get(base, ZERO):
            terminal_insert(f"[{now_cst()}] ERROR: Not enough {base} to sell {symbol}")
            return None

    try:
        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            price=format_decimal(price),
            quantity=format_decimal(qty)
        )
        terminal_insert(f"[{now_cst()}] ORDER PLACED: {side} {symbol} {qty}@{price}")
        if track: active_grid_orders.setdefault(symbol, []).append(order['orderId'])
        return order
    except BinanceAPIException as e:
        terminal_insert(f"[{now_cst()}] API ERROR placing {side} {symbol}: {e}")
    except Exception as e:
        terminal_insert(f"[{now_cst()}] ERROR placing {side} {symbol}: {e}")
    return None

# -------------------- GRID LOGIC --------------------
def cancel_symbol_orders(symbol):
    try:
        orders = client.get_open_orders(symbol=symbol)
        for o in orders:
            client.cancel_order(symbol=o['symbol'], orderId=o['orderId'])
        active_grid_orders[symbol] = []
        terminal_insert(f"[{now_cst()}] CANCELED {len(orders)} orders for {symbol}")
    except Exception as e:
        terminal_insert(f"[{now_cst()}] ERROR canceling orders {symbol}: {e}")

def place_single_grid(symbol, side):
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        cur_price = Decimal(ticker['price'])
    except Exception as e:
        terminal_insert(f"[{now_cst()}] ERROR fetching price for {symbol}: {e}")
        return

    info = symbol_info.get(symbol)
    if not info: return

    if side == 'BUY':
        price = cur_price * (ONE - GRID_BUY_PCT)
    else:
        req_pct = compute_required_sell_pct(symbol)
        price = cur_price * (ONE + req_pct)

    price = round_down(price, info['tickSize'])
    qty = CASH_USDT_PER_GRID_ORDER / price
    qty = round_down(qty, info['stepSize'])
    if qty <= ZERO: return

    place_limit_order(symbol, side, price, qty, track=True)

def regrid_on_fill(symbol):
    cancel_symbol_orders(symbol)
    place_single_grid(symbol, 'BUY')
    place_single_grid(symbol, 'SELL')

# -------------------- INITIAL GRID --------------------
def place_initial_grids():
    owned_symbols = [asset+'USDT' for asset in account_balances.keys() if asset != 'USDT' and asset+'USDT' in symbol_info]
    terminal_insert(f"[{now_cst()}] Placing initial grids for {len(owned_symbols)} symbols: {owned_symbols}")
    for sym in owned_symbols:
        cancel_symbol_orders(sym)
        place_single_grid(sym, 'BUY')
        place_single_grid(sym, 'SELL')

# -------------------- AUTOMATED BUY --------------------
def auto_buy_top_coins():
    usdt_available = account_balances.get('USDT', ZERO) * Decimal('0.33')  # 33% reserved
    top_coins = fetch_top_coins()
    for sym in top_coins:
        symbol = sym+'USDT'
        if symbol not in symbol_info: continue
        price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
        if price <= ZERO or price > usdt_available: continue
        qty = round_down(usdt_available / price, symbol_info[symbol]['stepSize'])
        if qty <= ZERO: continue
        place_limit_order(symbol, 'BUY', price, qty)


def grid_cycle():
    """
    Main loop that continuously updates balances and places grids for owned coins.
    Runs in a separate thread from the Tkinter mainloop.
    """
    while running:
        try:
            # 1. Update balances (USDT + owned coins)
            update_balances()

            # 2. Cancel old grid orders for owned coins
            for asset in list(account_balances.keys()):
                if asset == "USDT" or account_balances[asset] <= ZERO:
                    continue
                symbol = f"{asset}USDT"
                if symbol in symbol_info:
                    cancel_symbol_orders(symbol)

            # 3. Place grids on owned positions
            owned_assets = [a for a in account_balances.keys() if a != "USDT" and account_balances[a] > ZERO]
            terminal_insert(f"[{now_cst()}] Placing initial grids for owned coins: {owned_assets}")
            for asset in owned_assets:
                symbol = f"{asset}USDT"
                place_single_grid(symbol, 'BUY')
                place_single_grid(symbol, 'SELL')

            # 4. Wait before next update (obeys rate limit / reduces API weight)
            time.sleep(180)  # 3 minutes

        except Exception as e:
            terminal_insert(f"[{now_cst()}] GRID CYCLE ERROR: {e}")
            time.sleep(15)


# -------------------- TKINTER GUI & TERMINAL --------------------
root = tk.Tk()
root.title("INFINITY GRID BOT 2025")
root.geometry("1024x768")  # large window
root.resizable(True, True)

# -------------------- TERMINAL FRAME --------------------
terminal_frame = tk.Frame(root)
terminal_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

scrollbar = tk.Scrollbar(terminal_frame)
scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

terminal_text = tk.Text(terminal_frame, yscrollcommand=scrollbar.set, wrap=tk.NONE)
terminal_text.pack(fill=tk.BOTH, expand=True)
scrollbar.config(command=terminal_text.yview)

def terminal_insert(msg):
    terminal_text.insert(tk.END, msg + '\n')
    terminal_text.see(tk.END)

# -------------------- STATS FRAME --------------------
stats_frame = tk.Frame(root)
stats_frame.pack(fill=tk.X, padx=10, pady=5)

status_label = tk.Label(stats_frame, text="Status: Stopped", fg="red", font=("Helvetica", 12))
status_label.pack(side=tk.LEFT, padx=5)

usdt_label = tk.Label(stats_frame, text="USDT Balance: 0.00", fg="blue", font=("Helvetica", 12))
usdt_label.pack(side=tk.LEFT, padx=20)

def update_stats_labels():
    usdt_balance = account_balances.get('USDT', ZERO)
    usdt_label.config(text=f"USDT Balance: {usdt_balance:.8f}")
    status_label.config(text="Status: Running" if running else "Status: Stopped",
                        fg="green" if running else "red")
    root.after(5000, update_stats_labels)

update_stats_labels()

# -------------------- CONTROL BUTTONS --------------------
control_frame = tk.Frame(root)
control_frame.pack(fill=tk.X, padx=10, pady=5)

def start_trading():
    global running
    if not running:
        running = True
        terminal_insert(f"[{now_cst()}] GRID BOT STARTED")
        threading.Thread(target=grid_cycle, daemon=True).start()
        threading.Thread(target=auto_buy_top_coins, daemon=True).start()

def stop_trading():
    global running
    running = False
    for sym in list(active_grid_orders.keys()): cancel_symbol_orders(sym)
    terminal_insert(f"[{now_cst()}] GRID BOT STOPPED")

tk.Button(control_frame, text="Start", command=start_trading, bg="green", fg="white", font=("Helvetica", 14)).pack(side=tk.LEFT, padx=10)
tk.Button(control_frame, text="Stop",  command=stop_trading,  bg="red",   fg="white", font=("Helvetica", 14)).pack(side=tk.LEFT, padx=10)

# -------------------- WEBSOCKET HANDLERS --------------------
def on_user_message(ws, message):
    data = json.loads(message)
    if data.get('e') != 'executionReport' or data.get('X') != 'FILLED': return
    symbol = data.get('s')
    side   = data.get('S')
    qty    = Decimal(data.get('q', '0'))
    price  = Decimal(data.get('p', '0'))
    terminal_insert(f"[{now_cst()}] FILLED {side} {symbol} {qty}@{price}")
    threading.Thread(target=regrid_on_fill, args=(symbol,), daemon=True).start()

class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, **kwargs):
        super().__init__(url, **kwargs)
        self._stop = False
    def run_forever(self, **kwargs):
        while not self._stop:
            try: super().run_forever(ping_interval=25, **kwargs)
            except Exception as e: terminal_insert(f"[{now_cst()}] WS CRASH: {e}")
            time.sleep(5)
    def stop(self):
        self._stop = True
        self.close()

def start_user_data_stream():
    global listen_key
    res = client.stream_get_listen_key()
    listen_key = res.get('listenKey') if isinstance(res, dict) else str(res)
    url = f"wss://stream.binance.us:9443/ws/{listen_key}"
    ws = HeartbeatWebSocket(url, on_message=on_user_message)
    threading.Thread(target=ws.run_forever, daemon=True).start()
    terminal_insert(f"[{now_cst()}] User Data Stream Started")

def start_price_ws(symbols):
    streams = [f"{s.lower()}@ticker" for s in symbols[:500]]
    url = f"wss://stream.binance.us:9443/stream?streams={'/'.join(streams)}"
    ws = HeartbeatWebSocket(url, on_message=lambda ws,msg: update_live_prices(msg))
    threading.Thread(target=ws.run_forever, daemon=True).start()
    terminal_insert(f"[{now_cst()}] Price WS Started for {len(symbols)} symbols")

def update_live_prices(message):
    data = json.loads(message)
    payload = data.get('data', {})
    sym = payload.get('s','').upper()
    price = Decimal(payload.get('c', '0'))
    if price > ZERO:
        with price_lock:
            live_prices[sym] = price

# -------------------- MAIN LOOP --------------------
if __name__ == "__main__":
    api_key    = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')
    if not api_key or not api_secret:
        messagebox.showerror("API Error", "Set BINANCE_API_KEY and BINANCE_API_SECRET")
        sys.exit(1)
    client = Client(api_key, api_secret, tld='us')

    load_symbol_info()      # Load symbol filters / tick sizes
    update_balances()       # Load account balances
    start_user_data_stream()
    start_price_ws(list(symbol_info.keys()))

    terminal_insert(f"[{now_cst()}] GRID BOT READY")

    root.mainloop()
