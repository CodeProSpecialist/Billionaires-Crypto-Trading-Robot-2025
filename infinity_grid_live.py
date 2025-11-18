#!/usr/bin/env python3
"""
INFINITY GRID PLATINUM 2025 — 12-MINUTE ROBOT
• Accurate Realized + Unrealized P&L from Binance trade history
• Buy & Sell grids with full lot-size and cash safety
• CoinGecko Top 25 + live order book pressure
• 12-minute hyper cycle • GUI 800x900 • Live updates
"""

import os
import sys
import time
import threading
import json
import requests
import logging
from datetime import datetime
from decimal import Decimal, getcontext, ROUND_DOWN
import tkinter as tk
from tkinter import font as tkfont, scrolledtext, messagebox
import pytz
import websocket
from binance.client import Client

# ==================== CONFIG ====================
getcontext().prec = 28
ZERO = Decimal('0')
ONE = Decimal('1')
BASE_CASH_PER_LEVEL = Decimal('9.0')
GOLDEN_RATIO = Decimal('1.618034')
RESERVE_PCT = Decimal('0.30')
MIN_USDT_RESERVE = Decimal('15')

MAX_POSITION_STRONG_BUY = Decimal('0.085')  # 8.5%
MAX_POSITION_NEUTRAL   = Decimal('0.065')  # 6.5%
MAX_POSITION_SELL      = Decimal('0.04')   # 4.0%

BLACKLIST = {'BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA', 'DOGE', 'TRUMP'}
CST = pytz.timezone("America/Chicago")
REST_COOLDOWN = 1.6
last_rest_call = 0.0

def rest_throttle():
    global last_rest_call
    now = time.time()
    elapsed = now - last_rest_call
    if elapsed < REST_COOLDOWN:
        time.sleep(REST_COOLDOWN - elapsed)
    last_rest_call = time.time()

# ==================== LOGGING & WHATSAPP ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%m-%d %H:%M:%S')
log = logging.getLogger()

CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

def send_whatsapp(msg: str):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get("https://api.callmebot.com/whatsapp.php", params={
                "phone": CALLMEBOT_PHONE, "text": f"[GRID] {msg}", "apikey": CALLMEBOT_API_KEY}, timeout=10)
        except:
            pass

# ==================== BINANCE CLIENT ====================
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
if not api_key or not api_secret:
    messagebox.showerror("FATAL", "Set BINANCE_API_KEY and BINANCE_API_SECRET environment variables!")
    sys.exit(1)

client = Client(api_key, api_secret, tld='us')

# ==================== GLOBALS ====================
symbol_info = {}
account_balances = {'USDT': ZERO}
active_grid_orders = {}
running = False
buy_list = []
active_orders = 0

live_prices = {}
live_bids = {}
live_asks = {}
price_lock = threading.Lock()
book_lock = threading.Lock()

valid_symbols = set()
coingecko_top25 = []
last_coingecko_update = 0

# P&L from real trade history
realized_pnl = ZERO
cost_basis = {}  # asset -> (quantity, total_cost_usdt)

# ==================== P&L FROM ORDER HISTORY ====================
def update_pnl_from_history():
    global realized_pnl, cost_basis
    try:
        realized_pnl = ZERO
        cost_basis.clear()

        rest_throttle()
        info = client.get_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']

        for sym in symbols:
            base = sym.replace('USDT', '')
            if base in BLACKLIST:
                continue

            try:
                rest_throttle()
                trades = client.get_my_trades(symbol=sym, limit=1000)
                qty_held = ZERO
                total_cost = ZERO

                for t in trades:
                    q = Decimal(t['qty'])
                    price = Decimal(t['price'])
                    notional = q * price
                    is_buyer = t['isBuyer']
                    fee = Decimal(t.get('commission', '0'))
                    fee_asset = t.get('commissionAsset', 'USDT')
                    fee_usdt = fee * price if fee_asset != 'USDT' else fee

                    if is_buyer:
                        qty_held += q
                        total_cost += notional + fee_usdt
                    else:
                        if qty_held > ZERO:
                            avg_cost = total_cost / qty_held
                            cost_sold = avg_cost * q
                            profit = (notional - fee_usdt) - cost_sold
                            realized_pnl += profit
                            qty_held -= q
                            total_cost -= cost_sold
                            if qty_held < ZERO:
                                qty_held = ZERO
                                total_cost = ZERO

                if qty_held > ZERO:
                    cost_basis[base] = (qty_held, total_cost)
            except:
                continue
    except Exception as e:
        term(f"P&L sync failed: {e}")

def calculate_total_pnl():
    update_pnl_from_history()
    unrealized = ZERO
    for asset, (qty, cost) in cost_basis.items():
        price = live_prices.get(f"{asset}USDT")
        if price and qty > ZERO:
            unrealized += qty * price - cost
    total = realized_pnl + unrealized
    return total.quantize(Decimal('0.01'))

# ==================== COINGECKO TOP 25 ====================
def update_coingecko_top25():
    global coingecko_top25, last_coingecko_update
    if time.time() - last_coingecko_update < 3600:
        return
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc", "per_page": 50}, timeout=12)
        data = r.json()
        top = []
        for coin in data:
            sym = coin['symbol'].upper() + 'USDT'
            if sym in valid_symbols and coin['symbol'].upper() not in BLACKLIST:
                top.append(sym)
                if len(top) >= 25:
                    break
        coingecko_top25 = top
        last_coingecko_update = time.time()
        term(f"CoinGecko Top 25 → {len(coingecko_top25)} coins")
    except:
        term("CoinGecko offline — using order book only")

# ==================== WEBSOCKETS ====================
class HeartbeatWebSocket(websocket.WebSocketApp):
    def on_open(self, ws):
        pass
    def run_forever(self, **kwargs):
        while True:
            try:
                super().run_forever(ping_interval=20, **kwargs)
            except:
                time.sleep(5)

def on_market_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get("stream", "")
        payload = data.get("data", {})
        if not payload:
            return
        sym = stream.split("@")[0].upper()
        if stream.endswith("@ticker"):
            p = Decimal(payload.get("c", "0"))
            if p > ZERO:
                with price_lock:
                    live_prices[sym] = p
        elif stream.endswith("@depth20"):
            bids = [(Decimal(p), Decimal(q)) for p, q in payload.get("bids", [])]
            asks = [(Decimal(p), Decimal(q)) for p, q in payload.get("asks", [])]
            if bids and asks:
                with book_lock:
                    live_bids[sym] = bids
                    live_asks[sym] = asks
    except:
        pass

def start_market_websocket():
    streams = [f"{s.lower()}@ticker/{s.lower()}@depth20" for s in valid_symbols]
    for i in range(0, len(streams), 90):
        chunk = streams[i:i+90]
        url = f"wss://stream.binance.us:9443/stream?streams={'/'.join(chunk)}"
        ws = HeartbeatWebSocket(url, on_message=on_market_message)
        threading.Thread(target=ws.run_forever, daemon=True).start()
        time.sleep(0.4)

# ==================== GUI 800x900 ====================
root = tk.Tk()
root.title("INFINITY GRID PLATINUM 2025 — 12-MINUTE ROBOT")
root.geometry("800x900")
root.configure(bg="#0d1117")
root.resizable(False, False)

title_font = tkfont.Font(family="Helvetica", size=26, weight="bold")
big_font = tkfont.Font(family="Helvetica", size=18, weight="bold")
term_font = tkfont.Font(family="Consolas", size=10)

tk.Label(root, text="INFINITY GRID", font=title_font, fg="#58a6ff", bg="#0d1117").pack(pady=12)
tk.Label(root, text="PLATINUM 2025", font=title_font, fg="#00ff00", bg="#0d1117").pack(pady=2)
tk.Label(root, text="12-MINUTE ROBOT • REAL P&L • BUY/SELL GRID", fg="#ffaa00", bg="#0d1117", font=("Helvetica", 13, "bold")).pack(pady=8)

stats = tk.Frame(root, bg="#0d1117")
stats.pack(padx=40, fill="x", pady=10)

usdt_label = tk.Label(stats, text="USDT: $0.00", font=big_font, fg="#ffffff", bg="#0d1117", anchor="w")
usdt_label.pack(fill="x", pady=6)
pnl_label = tk.Label(stats, text="Total P&L: $0.00", font=big_font, fg="#00ff00", bg="#0d1117", anchor="w")
pnl_label.pack(fill="x", pady=6)
orders_label = tk.Label(stats, text="Active Orders: 0", font=big_font, fg="#39d353", bg="#0d1117", anchor="w")
orders_label.pack(fill="x", pady=6)
status_label = tk.Label(stats, text="Status: STOPPED", font=big_font, fg="#ff4444", bg="#0d1117", anchor="w")
status_label.pack(fill="x", pady=6)

terminal_frame = tk.Frame(root, bg="black")
terminal_frame.pack(fill="both", expand=True, padx=40, pady=10)
terminal_text = scrolledtext.ScrolledText(terminal_frame, bg="#000000", fg="#39d353", font=term_font, insertbackground="white")
terminal_text.pack(fill="both", expand=True)

button_frame = tk.Frame(root, bg="#0d1117")
button_frame.pack(pady=20)
tk.Button(button_frame, text="START ROBOT", command=lambda: start_bot(), bg="#238636", fg="white", font=("Helvetica", 16, "bold"), width=14, height=2).pack(side="left", padx=40)
tk.Button(button_frame, text="STOP ROBOT", command=lambda: stop_bot(), bg="#da3633", fg="white", font=("Helvetica", 16, "bold"), width=14, height=2).pack(side="left", padx=40)

def now():
    return datetime.now(CST).strftime("%H:%M:%S")

def term(msg):
    line = f"[{now()}] {msg}\n"
    print(line.strip())
    try:
        terminal_text.insert(tk.END, line)
        terminal_text.see(tk.END)
    except:
        pass

# ==================== CORE FUNCTIONS ====================
def update_usdt_balance():
    try:
        rest_throttle()
        for b in client.get_account()['balances']:
            if b['asset'] == 'USDT':
                account_balances['USDT'] = Decimal(b['free'])
                break
    except:
        pass

def place_limit_order(symbol, side, price, qty):
    global active_orders
    info = symbol_info[symbol]
    try:
        price = (price // info['tickSize']) * info['tickSize']
        qty = (qty // info['stepSize']) * info['stepSize']
        if qty < info['minQty'] or price * qty < info['minNotional']:
            return False
        rest_throttle()
        o = client.create_order(symbol=symbol, side=side, type='LIMIT', timeInForce='GTC',
                                price=str(price), quantity=str(qty))
        active_grid_orders.setdefault(symbol, []).append(o['orderId'])
        active_orders += 1
        term(f"{side} {qty:.6f} {symbol.replace('USDT','')} @ {price}")
        return True
    except Exception as e:
        term(f"Order failed: {e}")
        return False

def cancel_all_orders_for_symbol(symbol):
    global active_orders
    try:
        rest_throttle()
        orders = client.get_open_orders(symbol=symbol)
        for o in orders:
            rest_throttle()
            client.cancel_order(symbol=symbol, orderId=o['orderId'])
            active_orders -= 1
        active_grid_orders[symbol] = []
    except:
        pass

def load_symbol_info():
    global symbol_info, valid_symbols
    for s in client.get_exchange_info()['symbols']:
        if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
            base = s['baseAsset']
            if base in BLACKLIST:
                continue
            filters = {f['filterType']: f for f in s['filters']}
            lot = filters.get('LOT_SIZE', {})
            price_f = filters.get('PRICE_FILTER', {})
            step = Decimal(lot.get('stepSize', '1').rstrip('0').rstrip('.') or '1')
            tick = Decimal(price_f.get('tickSize', '1').rstrip('0').rstrip('.') or '1')
            if step == ZERO or tick == ZERO:
                continue
            symbol_info[s['symbol']] = {
                'stepSize': step,
                'tickSize': tick,
                'minQty': Decimal(lot.get('minQty', '0')),
                'minNotional': Decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '5'))
            }
            valid_symbols.add(s['symbol'])
    term(f"Loaded {len(valid_symbols)} USDT pairs")

def get_pressure_ratio(symbol):
    with book_lock:
        bid_vol = sum(q for _, q in live_bids.get(symbol, []))
        ask_vol = sum(q for _, q in live_asks.get(symbol, []))
    return 99.0 if ask_vol <= ZERO else bid_vol / ask_vol

def generate_hybrid_buy_list():
    global buy_list
    update_coingecko_top25()
    candidates = []
    for sym in coingecko_top25:
        if sym in live_prices and get_pressure_ratio(sym) > 1.6:
            candidates.append((sym, get_pressure_ratio(sym)))
    candidates.sort(key=lambda x: -x[1])
    buy_list = [x[0] for x in candidates[:20]]
    if len(buy_list) < 12:
        for sym in valid_symbols:
            if sym not in coingecko_top25 and get_pressure_ratio(sym) > 2.5:
                buy_list.append(sym)
                if len(buy_list) >= 20:
                    break
    term(f"ROBOT BUY LIST → {len(buy_list)} coins")

def place_buy_grid(symbol):
    price = live_prices.get(symbol)
    if not price or price <= ZERO:
        return
    info = symbol_info[symbol]
    update_usdt_balance()
    available = account_balances['USDT'] * (ONE - RESERVE_PCT)
    if available < Decimal('60'):
        return

    ratio = get_pressure_ratio(symbol)
    max_pct = MAX_POSITION_STRONG_BUY if ratio > 2.3 else MAX_POSITION_NEUTRAL if ratio > 1.4 else MAX_POSITION_SELL
    total_value = account_balances['USDT'] + sum(qty * live_prices.get(f"{a}USDT", ZERO) for a, (qty,_) in cost_basis.items())
    target = total_value * max_pct

    asset = symbol.replace('USDT', '')
    current = cost_basis.get(asset, (ZERO, ZERO))[0] * price

    if current >= target * Decimal('0.95'):
        return

    to_spend = min(available * Decimal('0.8'), target - current)
    if to_spend < Decimal('30'):
        return

    levels = 18
    grid_step = Decimal('0.017')
    spent = ZERO
    for i in range(1, levels + 1):
        if spent >= to_spend * Decimal('0.9'):
            break
        bp = price * (ONE - grid_step) ** i
        bp = (bp // info['tickSize']) * info['tickSize']
        qty = (BASE_CASH_PER_LEVEL * (GOLDEN_RATIO ** (i-1))) / bp
        qty = (qty // info['stepSize']) * info['stepSize']
        cost = bp * qty
        if cost < Decimal('6') or cost > to_spend - spent:
            break
        if place_limit_order(symbol, 'BUY', bp, qty):
            spent += cost

def place_sell_grid(symbol):
    price = live_prices.get(symbol)
    if not price or price <= ZERO:
        return
    asset = symbol.replace('USDT', '')
    if asset not in cost_basis:
        return
    qty_held, avg_cost = cost_basis[asset]
    if qty_held <= ZERO:
        return

    info = symbol_info[symbol]
    qty_held = (qty_held // info['stepSize']) * info['stepSize']
    if qty_held < info['minQty']:
        return

    ratio = get_pressure_ratio(symbol)
    max_pct = MAX_POSITION_STRONG_BUY if ratio > 2.3 else MAX_POSITION_NEUTRAL if ratio > 1.4 else MAX_POSITION_SELL
    total_value = account_balances['USDT'] + sum(q * live_prices.get(f"{a}USDT", ZERO) for a, (q,_) in cost_basis.items())
    target = total_value * max_pct
    current_value = qty_held * price

    if current_value <= target:
        return

    excess = current_value - target
    sell_qty = (excess / price).quantize(info['stepSize'], ROUND_DOWN)
    sell_qty = min(sell_qty, qty_held)
    if sell_qty < info['minQty']:
        return

    levels = 12
    grid_step_up = Decimal('0.016')
    remaining = sell_qty
    for i in range(1, levels + 1):
        if remaining <= ZERO:
            break
        sp = price * (ONE + grid_step_up) ** i
        sp = (sp // info['tickSize']) * info['tickSize']
        this_qty = min(remaining, BASE_CASH_PER_LEVEL * 3 / sp)
        this_qty = (this_qty // info['stepSize']) * info['stepSize']
        if this_qty >= info['minQty']:
            place_limit_order(symbol, 'SELL', sp, this_qty)
            remaining -= this_qty

def grid_cycle():
    while running:
        start = time.time()
        generate_hybrid_buy_list()
        if buy_list:
            term(f"12-MINUTE ROBOT CYCLE START — {len(buy_list)} coins")
            send_whatsapp(f"ROBOT CYCLE START — {len(buy_list)} coins")
            for sym in buy_list:
                if not running:
                    break
                term(f"Processing {sym.replace('USDT','')} | Pressure {get_pressure_ratio(sym):.2f}x")
                cancel_all_orders_for_symbol(sym)
                time.sleep(1.3)
                place_buy_grid(sym)
                place_sell_grid(sym)
                time.sleep(1.8)
            term("12-MINUTE ROBOT CYCLE COMPLETE")
            send_whatsapp("ROBOT CYCLE DONE — Next in 12 min")
        elapsed = time.time() - start
        sleep_for = max(0, 12*60 - elapsed)
        for _ in range(int(sleep_for / 7) + 1):
            if not running:
                break
            time.sleep(7)

# ==================== LIVE GUI UPDATE ====================
def live_gui_update():
    if root.winfo_exists():
        update_usdt_balance()
        total_pnl = calculate_total_pnl()
        usdt_label.config(text=f"USDT: ${account_balances['USDT']:,.2f}")
        pnl_label.config(text=f"Total P&L: ${total_pnl:+,.2f}",
                         fg="#00ff00" if total_pnl >= 0 else "#ff4444")
        orders_label.config(text=f"Active Orders: {active_orders}")
        status_label.config(text="Status: RUNNING", fg="#00ff00") if running else status_label.config(text="Status: STOPPED", fg="#ff4444")
        root.after(5000, live_gui_update)

def start_bot():
    global running
    if running:
        return
    running = True
    threading.Thread(target=grid_cycle, daemon=True).start()
    term("12-MINUTE ROBOT ACTIVATED — PROFIT MODE ON")
    send_whatsapp("Infinity Grid 12-MINUTE ROBOT STARTED")

def stop_bot():
    global running, active_orders
    running = False
    for sym in list(active_grid_orders.keys()):
        cancel_all_orders_for_symbol(sym)
    active_orders = 0
    term("ROBOT STOPPED — All orders canceled")

# ==================== MAIN ====================
if __name__ == "__main__":
    term("INFINITY GRID PLATINUM 2025 — 12-MINUTE ROBOT STARTING...")
    load_symbol_info()
    start_market_websocket()
    time.sleep(20)
    update_coingecko_top25()
    live_gui_update()
    term("ROBOT READY — Press START ROBOT to begin")
    root.mainloop()
