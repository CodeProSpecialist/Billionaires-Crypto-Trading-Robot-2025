#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — BINANCE.US + P&L Tracker
Real-time Profit & Loss (Realized + Unrealized)
"""

import os, sys, time, threading, json, requests
from datetime import datetime, date
from decimal import Decimal, getcontext
import tkinter as tk
from tkinter import font as tkfont, messagebox
import pytz, websocket
from binance.client import Client
from binance.exceptions import BinanceAPIException

# -------------------- CONFIG --------------------
getcontext().prec = 28
ZERO = Decimal('0')
ONE  = Decimal('1')
SAFETY_BUFFER = Decimal('0.95')
CASH_USDT_PER_GRID_ORDER = Decimal('8.0')
GRID_BUY_PCT  = Decimal('0.01')
MIN_SELL_PCT  = Decimal('0.018')
TARGET_PROFIT_PCT = Decimal('0.018')
CONSERVATIVE_USE_TAKER = True
FEE_CACHE_TTL = 60*30
EXCLUDED_COINS = {"USD","USDT","BTC","BCH","ETH","SOL"}
CST = pytz.timezone("America/Chicago")

# P&L Persistence
PNL_FILE = "grid_pnl.json"

# -------------------- BINANCE CLIENT --------------------
api_key    = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
if not api_key or not api_secret:
    messagebox.showerror("API Error", "Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)
client = Client(api_key, api_secret, tld='us')

# -------------------- GLOBAL STATE --------------------
symbol_info = {}
account_balances = {}
active_grid_orders = {}
_fee_cache = {}
live_prices = {}
running = False
price_lock = threading.Lock()

# -------------------- P&L TRACKING --------------------
pnl_data = {
    'total_realized': ZERO,
    'daily_realized': ZERO,
    'last_reset_date': str(date.today()),
    'cost_basis': {},  # symbol -> (qty, cost_usdt)
    'unrealized': ZERO
}

def load_pnl():
    global pnl_data
    if os.path.exists(PNL_FILE):
        try:
            with open(PNL_FILE, 'r') as f:
                data = json.load(f)
                pnl_data['total_realized'] = Decimal(data.get('total_realized', '0'))
                pnl_data['daily_realized'] = Decimal(data.get('daily_realized', '0'))
                pnl_data['last_reset_date'] = data.get('last_reset_date', str(date.today()))
                pnl_data['cost_basis'] = {
                    k: (Decimal(v[0]), Decimal(v[1])) for k, v in data.get('cost_basis', {}).items()
                }
        except Exception as e:
            terminal_insert(f"[{now_cst()}] P&L load error: {e}")

def save_pnl():
    try:
        data = {
            'total_realized': str(pnl_data['total_realized']),
            'daily_realized': str(pnl_data['daily_realized']),
            'last_reset_date': pnl_data['last_reset_date'],
            'cost_basis': {k: [str(q), str(c)] for k, (q, c) in pnl_data['cost_basis'].items()}
        }
        with open(PNL_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        terminal_insert(f"[{now_cst()}] P&L save error: {e}")

def reset_daily_pnl():
    today = str(date.today())
    if pnl_data['last_reset_date'] != today:
        pnl_data['daily_realized'] = ZERO
        pnl_data['last_reset_date'] = today
        save_pnl()
        terminal_insert(f"[{now_cst()}] Daily P&L reset")

def update_unrealized_pnl():
    unrealized = ZERO
    for asset, (qty, cost) in pnl_data['cost_basis'].items():
        symbol = f"{asset}USDT"
        if symbol not in live_prices or qty <= ZERO:
            continue
        market_value = qty * live_prices[symbol]
        unrealized += market_value - cost
    pnl_data['unrealized'] = unrealized
    return unrealized

def record_fill(symbol, side, qty, price, fee_usdt):
    base = symbol.replace('USDT', '')
    qty = Decimal(qty)
    price = Decimal(price)
    fee_usdt = Decimal(fee_usdt)
    notional = qty * price

    if side == 'BUY':
        if base not in pnl_data['cost_basis']:
            pnl_data['cost_basis'][base] = (ZERO, ZERO)
        old_qty, old_cost = pnl_data['cost_basis'][base]
        new_qty = old_qty + qty
        new_cost = old_cost + notional + fee_usdt
        pnl_data['cost_basis'][base] = (new_qty, new_cost)
    elif side == 'SELL':
        if base not in pnl_data['cost_basis']:
            return
        old_qty, old_cost = pnl_data['cost_basis'][base]
        if qty >= old_qty:
            # Full sell
            realized = (notional - fee_usdt) - old_cost
            pnl_data['total_realized'] += realized
            pnl_data['daily_realized'] += realized
            del pnl_data['cost_basis'][base]
        else:
            # Partial sell
            avg_cost_per_unit = old_cost / old_qty
            cost_sold = avg_cost_per_unit * qty
            realized = (notional - fee_usdt) - cost_sold
            pnl_data['total_realized'] += realized
            pnl_data['daily_realized'] += realized
            pnl_data['cost_basis'][base] = (old_qty - qty, old_cost - cost_sold)
    save_pnl()

# -------------------- GUI --------------------
root = tk.Tk()
root.title("INFINITY GRID BOT 2025")
root.geometry("800x600")
root.configure(bg="#000000")
root.resizable(False, False)

title_font = tkfont.Font(family="Helvetica", size=16, weight="bold")
label_font = tkfont.Font(family="Helvetica", size=11)
btn_font   = tkfont.Font(family="Helvetica", size=12, weight="bold")
term_font  = tkfont.Font(family="Courier", size=14)

# Stats Bar
stats_frame = tk.Frame(root, bg="#000000", height=80)
stats_frame.pack(fill="x", padx=8, pady=(8, 4))
stats_frame.pack_propagate(False)

# USDT
tk.Label(stats_frame, text="USDT:", fg="lime", bg="#000000", font=label_font).pack(side="left", padx=5)
usdt_label = tk.Label(stats_frame, text="0.00", fg="cyan", bg="#000000", font=label_font, width=12, anchor="w")
usdt_label.pack(side="left", padx=5)

# Active Coins
tk.Label(stats_frame, text="Active:", fg="lime", bg="#000000", font=label_font).pack(side="left", padx=(20,5))
active_coins_label = tk.Label(stats_frame, text="0", fg="cyan", bg="#000000", font=label_font, width=4, anchor="w")
active_coins_label.pack(side="left")

# P&L Labels
tk.Label(stats_frame, text="P&L:", fg="lime", bg="#000000", font=label_font).pack(side="left", padx=(30,5))
pnl_total_label = tk.Label(stats_frame, text="$0.00", fg="white", bg="#000000", font=label_font, width=10, anchor="w")
pnl_total_label.pack(side="left")
pnl_daily_label = tk.Label(stats_frame, text="(Today: $0.00)", fg="gray", bg="#000000", font=label_font, anchor="w")
pnl_daily_label.pack(side="left", padx=5)

# Status
status_label = tk.Label(stats_frame, text="Status: Stopped", fg="red", bg="#000000", font=label_font)
status_label.pack(side="right", padx=10)

# Top Coins
top_coins_frame = tk.Frame(stats_frame, bg="#000000")
top_coins_frame.pack(fill="x", pady=2)
tk.Label(top_coins_frame, text="Top Coins:", fg="lime", bg="#000000", font=label_font).pack(side="left")
top_coins_label = tk.Label(top_coins_frame, text="Loading...", fg="yellow", bg="#000000", font=label_font, anchor="w", justify="left")
top_coins_label.pack(side="left", fill="x", expand=True)

# Terminal
term_outer = tk.Frame(root, bg="#000000")
term_outer.pack(fill="both", expand=True, padx=8, pady=4)

term_canvas = tk.Canvas(term_outer, bg="black", highlightthickness=0)
term_scrollbar = tk.Scrollbar(term_outer, orient="vertical", command=term_canvas.yview)
term_frame = tk.Frame(term_canvas, bg="black")

term_canvas.configure(yscrollcommand=term_scrollbar.set)
term_scrollbar.pack(side="right", fill="y")
term_canvas.pack(side="left", fill="both", expand=True)
term_canvas.create_window((0,0), window=term_frame, anchor="nw")

term_text = tk.Text(term_frame, bg="black", fg="lime", font=term_font, wrap="word", spacing1=2, spacing3=2)
term_text.pack(fill="both", expand=True)

def on_frame_configure(event):
    term_canvas.configure(scrollregion=term_canvas.bbox("all"))
term_frame.bind("<Configure>", on_frame_configure)

def terminal_insert(msg):
    term_text.insert(tk.END, f"{msg}\n")
    term_text.see(tk.END)

# Buttons
btn_frame = tk.Frame(root, bg="#000000")
btn_frame.pack(fill="x", padx=8, pady=(4, 8))
tk.Button(btn_frame, text="START", command=lambda: start_trading(), bg="#00aa00", fg="white", font=btn_font, width=12).pack(side="left", padx=10)
tk.Button(btn_frame, text="STOP",  command=lambda: stop_trading(),  bg="#aa0000", fg="white", font=btn_font, width=12).pack(side="left", padx=10)

# -------------------- UTILS --------------------
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def _safe_decimal(val, fallback='0'):
    try: return Decimal(str(val))
    except: return Decimal(fallback)

def round_down(val, step):
    return (val // step) * step if step > ZERO else val

def format_decimal(d):
    s = f"{d:.8f}"
    return s.rstrip('0').rstrip('.') if '.' in s else s

# -------------------- SYMBOL INFO --------------------
def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] != 'USDT' or s['status'] != 'TRADING': continue
            f = {x['filterType']: x for x in s['filters']}
            step = _safe_decimal(f.get('LOT_SIZE', {}).get('stepSize', '0'))
            tick = _safe_decimal(f.get('PRICE_FILTER', {}).get('tickSize', '0'))
            min_qty = _safe_decimal(f.get('LOT_SIZE', {}).get('minQty', '0'))
            min_not = _safe_decimal(f.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            if step == ZERO or tick == ZERO: continue
            symbol_info[s['symbol']] = {
                'stepSize': step, 'tickSize': tick,
                'minQty': min_qty, 'minNotional': min_not
            }
        terminal_insert(f"[{now_cst()}] Loaded {len(symbol_info)} symbols")
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Symbol load error: {e}")

# -------------------- BALANCES & STATS --------------------
def update_balances():
    global account_balances
    try:
        info = client.get_account()
        account_balances = {a['asset']: _safe_decimal(a['free']) for a in info['balances'] if _safe_decimal(a['free']) > ZERO}
        update_stats_labels()
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Balance error: {e}")

def update_stats_labels():
    usdt = account_balances.get('USDT', ZERO)
    usdt_label.config(text=f"{usdt:.2f}")

    active = len([a for a in account_balances if a != 'USDT' and account_balances[a] > ZERO])
    active_coins_label.config(text=str(active))

    # Update P&L
    reset_daily_pnl()
    unrealized = update_unrealized_pnl()
    total_pnl = pnl_data['total_realized'] + unrealized
    daily_pnl = pnl_data['daily_realized'] + unrealized

    # Color coding
    def color(val):
        return "lime" if val > 0 else "red" if val < 0 else "white"

    pnl_total_label.config(text=f"${total_pnl:.2f}", fg=color(total_pnl))
    pnl_daily_label.config(text=f"(Today: ${daily_pnl:.2f})", fg=color(daily_pnl))

    status_label.config(text="Status: Running" if running else "Status: Stopped",
                        fg="lime" if running else "red")

    root.after(3000, update_stats_labels)  # Faster refresh for P&L

# -------------------- FEES --------------------
def get_fee_for_side(symbol, side):
    fees = fetch_trade_fees_for_symbol(symbol)
    return fees['taker'] if CONSERVATIVE_USE_TAKER else fees['maker']

def fetch_trade_fees_for_symbol(symbol):
    now = int(time.time())
    if symbol in _fee_cache and now - _fee_cache[symbol]['ts'] < FEE_CACHE_TTL:
        return _fee_cache[symbol]
    try:
        resp = client.get_trade_fee(symbol=symbol)
        maker = Decimal(resp[0]['makerCommission']) if resp else Decimal('0.001')
        taker = Decimal(resp[0]['takerCommission']) if resp else Decimal('0.001')
        _fee_cache[symbol] = {'maker': maker, 'taker': taker, 'ts': now}
        return _fee_cache[symbol]
    except: return {'maker': Decimal('0.001'), 'taker': Decimal('0.001')}

# -------------------- TOP COINS --------------------
def fetch_top_coins():
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 100, "page": 1}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        coins = [c['symbol'].upper() for c in r.json() if c['symbol'].upper() not in EXCLUDED_COINS]
        top_coins_label.config(text=", ".join(coins[:25]))
        return coins[:25]
    except:
        top_coins_label.config(text="Error")
        return []

# -------------------- ORDER PLACEMENT --------------------
def place_limit_order(symbol, side, price, qty, track=True):
    info = symbol_info.get(symbol)
    if not info: return None
    price = round_down(Decimal(price), info['tickSize'])
    qty = round_down(Decimal(qty), info['stepSize'])
    if qty <= info['minQty']: return None
    notional = price * qty
    if notional < info['minNotional']: return None

    if side == 'BUY':
        need = notional * SAFETY_BUFFER
        have = account_balances.get('USDT', ZERO)
        if need > have:
            terminal_insert(f"[{now_cst()}] NOT ENOUGH USDT: Need {need:.2f}")
            return None
    elif side == 'SELL':
        base = symbol.replace('USDT', '')
        have = account_balances.get(base, ZERO)
        if qty > have:
            terminal_insert(f"[{now_cst()}] NOT ENOUGH {base}")
            return None

    try:
        order = client.create_order(
            symbol=symbol, side=side, type='LIMIT',
            timeInForce='GTC', price=format_decimal(price),
            quantity=format_decimal(qty)
        )
        terminal_insert(f"[{now_cst()}] {side} {symbol} {qty} @ {price}")
        if track:
            active_grid_orders.setdefault(symbol, []).append(order['orderId'])
        return order
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Order error: {e}")
        return None

# -------------------- GRID LOGIC --------------------
def cancel_symbol_orders(symbol):
    try:
        for o in client.get_open_orders(symbol=symbol):
            client.cancel_order(symbol=symbol, orderId=o['orderId'])
        active_grid_orders[symbol] = []
    except: pass

def place_single_grid(symbol, side):
    try:
        price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
    except: return
    info = symbol_info.get(symbol)
    if not info: return
    p = price * (ONE - GRID_BUY_PCT) if side == 'BUY' else price * (ONE + compute_required_sell_pct(symbol))
    p = round_down(p, info['tickSize'])
    q = round_down(CASH_USDT_PER_GRID_ORDER / p, info['stepSize'])
    if q > ZERO:
        place_limit_order(symbol, side, p, q)

def regrid_on_fill(symbol):
    cancel_symbol_orders(symbol)
    place_single_grid(symbol, 'BUY')
    place_single_grid(symbol, 'SELL')

def compute_required_sell_pct(symbol):
    fees = fetch_trade_fees_for_symbol(symbol)
    buy_fee = fees['taker'] if CONSERVATIVE_USE_TAKER else fees['maker']
    sell_fee = fees['taker'] if CONSERVATIVE_USE_TAKER else fees['maker']
    try:
        pct = (ONE + TARGET_PROFIT_PCT) * (ONE + buy_fee) / (ONE - sell_fee) - ONE
        return max(pct, MIN_SELL_PCT)
    except:
        return MIN_SELL_PCT

# -------------------- WEBSOCKET FILL HANDLER --------------------
def on_user_message(ws, msg):
    try:
        d = json.loads(msg)
        if d.get('e') != 'executionReport' or d.get('X') != 'FILLED':
            return
        symbol = d['s']
        side = d['S']
        qty = d['q']
        price = d['p']
        fee = d.get('n', '0')  # Fee in USDT
        fee_asset = d.get('N', 'USDT')
        fee_usdt = Decimal(fee) if fee_asset == 'USDT' else Decimal(fee) * Decimal(price)

        terminal_insert(f"[{now_cst()}] FILLED {side} {symbol} {qty}@{price} (Fee: {fee_usdt:.4f} USDT)")
        record_fill(symbol, side, qty, price, fee_usdt)
        threading.Thread(target=regrid_on_fill, args=(symbol,), daemon=True).start()
    except Exception as e:
        terminal_insert(f"[{now_cst()}] WS Error: {e}")

# -------------------- THREADS --------------------
def auto_buy_top_coins():
    while running:
        time.sleep(3600)
        try:
            update_balances()
            usdt = account_balances.get('USDT', ZERO) * Decimal('0.3')
            if usdt < 50: continue
            coins = fetch_top_coins()
            per_coin = usdt / len(coins)
            for c in coins:
                sym = c + 'USDT'
                if sym not in symbol_info: continue
                p = Decimal(client.get_symbol_ticker(symbol=sym)['price'])
                q = round_down(per_coin / p, symbol_info[sym]['stepSize'])
                if q > ZERO:
                    place_limit_order(sym, 'BUY', p, q, track=False)
        except: pass

def grid_cycle():
    while running:
        try:
            update_balances()
            for asset in [a for a in account_balances if a != 'USDT' and account_balances[a] > ZERO]:
                sym = f"{asset}USDT"
                if sym in symbol_info:
                    cancel_symbol_orders(sym)
                    place_single_grid(sym, 'BUY')
                    place_single_grid(sym, 'SELL')
            time.sleep(180)
        except Exception as e:
            terminal_insert(f"[{now_cst()}] Cycle error: {e}")
            time.sleep(15)

# -------------------- CONTROL --------------------
def start_trading():
    global running
    if not running:
        running = True
        threading.Thread(target=grid_cycle, daemon=True).start()
        threading.Thread(target=auto_buy_top_coins, daemon=True).start()
        terminal_insert(f"[{now_cst()}] BOT STARTED")

def stop_trading():
    global running
    running = False
    for s in list(active_grid_orders): cancel_symbol_orders(s)
    terminal_insert(f"[{now_cst()}] BOT STOPPED")

# -------------------- WEBSOCKETS --------------------
class WS(websocket.WebSocketApp):
    def __init__(self, url, on_msg):
        super().__init__(url, on_message=on_msg)
        self._stop = False
    def run_forever(self, **kw):
        while not self._stop:
            try: super().run_forever(ping_interval=25, **kw)
            except: time.sleep(5)
    def stop(self): self._stop = True; self.close()

def start_user_stream():
    try:
        key = client.stream_get_listen_key()['listenKey']
        url = f"wss://stream.binance.us:9443/ws/{key}"
        WS(url, on_user_message).run_forever()
    except: pass

def start_price_ws():
    streams = [f"{s.lower()}@ticker" for s in list(symbol_info.keys())[:500]]
    if streams:
        def on_price(ws, m):
            try:
                data = json.loads(m)['data']
                sym = data['s']
                price = Decimal(data['c'])
                if price > ZERO:
                    with price_lock:
                        live_prices[sym] = price
            except: pass
        url = f"wss://stream.binance.us:9443/stream?streams={'/'.join(streams)}"
        WS(url, on_price).run_forever()

# -------------------- MAIN --------------------
if __name__ == "__main__":
    load_pnl()
    load_symbol_info()
    update_balances()
    fetch_top_coins()
    threading.Thread(target=start_user_stream, daemon=True).start()
    threading.Thread(target=start_price_ws, daemon=True).start()

    terminal_insert(f"[{now_cst()}] INFINITY GRID BOT 2025 READY")
    update_stats_labels()

    root.mainloop()
