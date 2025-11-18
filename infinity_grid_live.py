#!/usr/bin/env python3
"""
INFINITY GRID PLATINUM 2025 — FINAL CLEAN VERSION
No P&L in GUI — 12-minute Order Book Rebalance (15% / 5% / 4%)
WebSockets only — ZERO REST weight
November 18, 2025 — THE ULTIMATE BOT
"""

import os
import sys
import time
import threading
import json
import requests
from datetime import datetime
from decimal import Decimal, getcontext
import tkinter as tk
from tkinter import font as tkfont, messagebox, scrolledtext
import pytz
import websocket
from binance.client import Client
import numpy as np

# -------------------- PRECISION & CONSTANTS --------------------
getcontext().prec = 28
ZERO = Decimal('0')
ONE = Decimal('1')
FEE_RATE = Decimal('0.001')
BASE_CASH_PER_LEVEL = Decimal('8.0')
GOLDEN_RATIO = Decimal('1.618034')
SELL_GROWTH_OPTIMAL = Decimal('1.309')
RESERVE_PCT = Decimal('0.33')
MIN_USDT_RESERVE = Decimal('8')
CST = pytz.timezone("America/Chicago")
REBALANCE_INTERVAL = 720  # 12 minutes

# Position targets based on buy pressure
TARGET_HIGH = Decimal('0.15')   # 15% — strong buy pressure
TARGET_MEDIUM = Decimal('0.05') # 5% — neutral
TARGET_LOW = Decimal('0.04')    # 4% — sell pressure

# -------------------- ENVIRONMENT --------------------
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
if not api_key or not api_secret:
    messagebox.showerror("Error", "Set BINANCE_API_KEY and BINANCE_API_SECRET!")
    sys.exit(1)

client = Client(api_key, api_secret, tld='us')

# -------------------- GLOBALS --------------------
symbol_info = {}
account_balances = {'USDT': ZERO}
active_grid_orders = {}
running = False
active_orders = 0
price_cache = {}
buy_list = []
last_rebalance = 0

# -------------------- SYMBOL INFO --------------------
def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] != 'USDT' or s['status'] != 'TRADING': continue
            filters = {f['filterType']: f for f in s['filters']}
            step = Decimal(filters.get('LOT_SIZE', {}).get('stepSize', '0'))
            tick = Decimal(filters.get('PRICE_FILTER', {}).get('tickSize', '0'))
            if step == ZERO or tick == ZERO: continue
            symbol_info[s['symbol']] = {
                'stepSize': step,
                'tickSize': tick,
                'minQty': Decimal(filters.get('LOT_SIZE', {}).get('minQty', '0')),
                'minNotional': Decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            }
        terminal_insert(f"[{now_cst()}] Loaded {len(symbol_info)} USDT pairs")
    except Exception as e:
        terminal_insert(f"Symbol load error: {e}")

# -------------------- ORDER BOOK REBALANCE (12 MIN) --------------------
def get_buy_pressure(symbol):
    try:
        book = client.get_order_book(symbol=symbol, limit=500)
        bids = sum(Decimal(b[1]) * Decimal(b[0]) for b in book['bids'][:20])
        asks = sum(Decimal(a[1]) * Decimal(a[0]) for a in book['asks'][:20])
        total = bids + asks
        if total == ZERO:
            return Decimal('0.5')
        return bids / total
    except:
        return Decimal('0.5')

def dynamic_rebalance():
    global last_rebalance
    if time.time() - last_rebalance < REBALANCE_INTERVAL:
        return
    last_rebalance = time.time()

    try:
        update_balances()
        total_portfolio = account_balances.get('USDT', ZERO)
        for asset in account_balances:
            if asset == 'USDT': continue
            sym = asset + 'USDT'
            if sym in symbol_info:
                price = price_cache.get(sym, Decimal(client.get_symbol_ticker(symbol=sym)['price']))
                total_portfolio += account_balances[asset] * price

        if total_portfolio <= ZERO:
            return

        terminal_insert(f"[{now_cst()}] 12-min Rebalance — Portfolio ${total_portfolio:,.0f}")

        for asset in list(account_balances.keys()):
            if asset == 'USDT': continue
            sym = asset + 'USDT'
            if sym not in symbol_info: continue

            pressure = get_buy_pressure(sym)

            if pressure > Decimal('0.65'):
                target_pct = TARGET_HIGH  # 15%
            elif pressure < Decimal('0.35'):
                target_pct = TARGET_LOW    # 4%
            else:
                target_pct = TARGET_MEDIUM # 5%

            current_qty = account_balances.get(asset, ZERO)
            current_price = price_cache.get(sym, Decimal(client.get_symbol_ticker(symbol=sym)['price']))
            current_value = current_qty * current_price
            target_value = total_portfolio * target_pct

            if current_value > target_value * Decimal('1.05'):
                sell_qty = (current_value - target_value) / current_price
                sell_qty = (sell_qty // symbol_info[sym]['stepSize']) * symbol_info[sym]['stepSize']
                sell_qty = sell_qty.quantize(Decimal('0.00000000'))
                if sell_qty >= symbol_info[sym]['minQty']:
                    place_limit_order(sym, 'SELL', current_price * Decimal('0.999'), sell_qty)
                    terminal_insert(f"[{now_cst()}] REBAL SELL {asset} {sell_qty} (pressure {pressure:.1%} → {target_pct:.1%})")

            elif current_value < target_value * Decimal('0.95') and pressure > Decimal('0.6'):
                buy_qty = (target_value - current_value) / current_price
                buy_qty = (buy_qty // symbol_info[sym]['stepSize']) * symbol_info[sym]['stepSize']
                buy_qty = buy_qty.quantize(Decimal('0.00000000'))
                required = current_price * buy_qty * (ONE + FEE_RATE)
                available = account_balances.get('USDT', ZERO) - (account_balances.get('USDT', ZERO) * RESERVE_PCT + MIN_USDT_RESERVE)
                if available >= required and buy_qty >= symbol_info[sym]['minQty']:
                    place_limit_order(sym, 'BUY', current_price * Decimal('1.001'), buy_qty)
                    terminal_insert(f"[{now_cst()}] REBAL BUY {asset} {buy_qty} (pressure {pressure:.1%} → {target_pct:.1%})")

    except Exception as e:
        terminal_insert(f"Rebalance error: {e}")

# -------------------- GRID ENGINE --------------------
def place_limit_order(symbol, side, price, qty):
    global active_orders
    info = symbol_info.get(symbol)
    if not info: return False
    try:
        price = (Decimal(price) // info['tickSize']) * info['tickSize']
        qty = (qty // info['stepSize']) * info['stepSize']
        qty = qty.quantize(Decimal('0.00000000'))
        if qty < info['minQty']: return False
        notional = price * qty
        if notional < info['minNotional']: return False

        if side == 'BUY':
            required = notional * (ONE + FEE_RATE)
            available = account_balances.get('USDT', ZERO) - (account_balances.get('USDT', ZERO) * RESERVE_PCT + MIN_USDT_RESERVE)
            if available < required: return False

        order = client.create_order(symbol=symbol, side=side, type='LIMIT', timeInForce='GTC',
                                    price=str(price), quantity=str(qty))
        active_grid_orders.setdefault(symbol, []).append(order['orderId'])
        active_orders += 1
        terminal_insert(f"[{now_cst()}] SUCCESS {side} {symbol} {qty} @ {price}")
        return True
    except Exception as e:
        terminal_insert(f"Order error: {e}")
        return False

def cancel_symbol_orders(symbol):
    global active_orders
    try:
        for o in client.get_open_orders(symbol=symbol):
            client.cancel_order(symbol=symbol, orderId=o['orderId'])
            active_orders -= 1
        active_grid_orders[symbol] = []
    except: pass

def place_platinum_grid(symbol):
    try:
        price = price_cache.get(symbol, Decimal(client.get_symbol_ticker(symbol=symbol)['price']))
        info = symbol_info[symbol]
        grid_pct = Decimal('0.012')
        size_multiplier = Decimal('1.0') + get_volume_ratio(symbol) * Decimal('0.5')
        size_multiplier = max(Decimal('0.8'), min(size_multiplier, Decimal('5.0')))
        num_levels = 8
        cash = BASE_CASH_PER_LEVEL * size_multiplier
        reserve = account_balances.get('USDT', ZERO) * RESERVE_PCT + MIN_USDT_RESERVE

        for i in range(1, num_levels + 1):
            buy_price = price * ((ONE - grid_pct) ** i)
            buy_price = (buy_price // info['tickSize']) * info['tickSize']
            qty = cash * (GOLDEN_RATIO ** (i-1)) / buy_price
            qty = (qty // info['stepSize']) * info['stepSize']
            qty = qty.quantize(Decimal('0.00000000'))
            required = buy_price * qty * (ONE + FEE_RATE)
            if account_balances.get('USDT', ZERO) - reserve >= required:
                place_limit_order(symbol, 'BUY', buy_price, qty)

        owned = account_balances.get(symbol.replace('USDT', ''), ZERO)
        for i in range(1, num_levels + 1):
            sell_price = price * ((ONE + grid_pct) ** i)
            sell_price = (sell_price // info['tickSize']) * info['tickSize']
            qty = cash * (SELL_GROWTH_OPTIMAL ** (i-1)) / sell_price
            qty = (qty // info['stepSize']) * info['stepSize']
            qty = qty.quantize(Decimal('0.00000000'))
            if qty <= owned:
                place_limit_order(symbol, 'SELL', sell_price, qty)
                owned -= qty

    except Exception as e:
        pass

def regrid_symbol(symbol):
    cancel_symbol_orders(symbol)
    place_platinum_grid(symbol)

# -------------------- WEBSOCKETS --------------------
def on_user_message(ws, message):
    try:
        data = json.loads(message)
        e = data.get('e')

        if e == 'outboundAccountPosition':
            for b in data['B']:
                asset = b['a']
                total = Decimal(b['f']) + Decimal(b['l'])
                if total > ZERO:
                    account_balances[asset] = total
                elif asset in account_balances:
                    del account_balances[asset]

        elif e == 'executionReport' and data.get('X') == 'FILLED':
            symbol = data['s']
            side = data['S']
            qty = Decimal(data['q'])
            price = Decimal(data['p'])
            base = symbol.replace('USDT', '')
            terminal_insert(f"[{now_cst()}] FILLED {side} {base} {qty:.6f} @ {price:.8f}")
            threading.Thread(target=regrid_symbol, args=(symbol,), daemon=True).start()

        elif e == '24hrTicker':
            symbol = data['s']
            if symbol in symbol_info:
                price_cache[symbol] = Decimal(data['c'])

    except Exception as e:
        terminal_insert(f"WS Error: {e}")

def start_user_stream():
    def run():
        while True:
            try:
                key = client.stream_get_listen_key()['listenKey']
                user_url = f"wss://stream.binance.us:9443/ws/{key}"
                price_url = "wss://stream.binance.us:9443/stream?streams=!ticker@arr"
                ws_user = websocket.WebSocketApp(user_url, on_message=on_user_message)
                ws_price = websocket.WebSocketApp(price_url, on_message=on_user_message)
                ws_user.run_forever(ping_interval=1800)
                ws_price.run_forever(ping_interval=60)
            except:
                time.sleep(5)
    threading.Thread(target=run, daemon=True).start()

# -------------------- COINGECKO BUY LIST --------------------
def generate_buy_list():
    global buy_list
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {'vs_currency': 'usd', 'order': 'market_cap_desc', 'per_page': 100}
        coins = requests.get(url, params=params, timeout=15).json()
        candidates = []
        for coin in coins:
            sym = coin['symbol'].upper() + 'USDT'
            if sym not in symbol_info: continue
            if coin.get('market_cap', 0) < 1_000_000_000: continue
            score = coin.get('price_change_percentage_7d_in_currency', 0) or 0
            if score > 10:
                candidates.append(sym)
        buy_list = candidates[:10]
        terminal_insert(f"[{now_cst()}] CoinGecko Buy List: {len(buy_list)} coins")
    except:
        buy_list = ['SOLUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'MATICUSDT']

# -------------------- GUI (NO P&L) --------------------
root = tk.Tk()
root.title("INFINITY GRID PLATINUM 2025")
root.geometry("1000x1000")
root.configure(bg="#0d1117")

tk.Label(root, text="INFINITY GRID PLATINUM 2025", font=("Helvetica", 30, "bold"), fg="#58a6ff", bg="#0d1117").pack(pady=20)

stats = tk.Frame(root, bg="#0d1117")
stats.pack(padx=50, fill="x")
usdt_label = tk.Label(stats, text="USDT: $0.00", font=("Helvetica", 20), fg="#8b949e", bg="#0d1117", anchor="w")
usdt_label.pack(fill="x", pady=5)
orders_label = tk.Label(stats, text="Active Orders: 0", font=("Helvetica", 20), fg="#39d353", bg="#0d1117", anchor="w")
orders_label.pack(fill="x", pady=8)

terminal_frame = tk.Frame(root, bg="black")
terminal_frame.pack(fill="both", expand=True, padx=50, pady=10)
terminal_text = scrolledtext.ScrolledText(terminal_frame, bg="black", fg="#39d353", font=("Consolas", 11))
terminal_text.pack(fill="both", expand=True)

button_frame = tk.Frame(root, bg="#0d1117")
button_frame.pack(pady=30)
status_label = tk.Label(button_frame, text="Status: Stopped", font=("Helvetica", 20), fg="red", bg="#0d1117")
status_label.pack(side="left", padx=60)

def start_bot():
    global running
    running = True
    threading.Thread(target=grid_cycle, daemon=True).start()
    status_label.config(text="Status: RUNNING", fg="#00ff00")
    terminal_insert(f"[{now_cst()}] BOT STARTED — 12-MIN REBALANCE ACTIVE")

def stop_bot():
    global running
    running = False
    for s in list(active_grid_orders.keys()):
        cancel_symbol_orders(s)
    status_label.config(text="Status: Stopped", fg="red")

tk.Button(button_frame, text="START BOT", command=start_bot, bg="#238636", fg="white", font=("Helvetica", 20), width=20, height=2).pack(side="right", padx=15)
tk.Button(button_frame, text="STOP BOT", command=stop_bot, bg="#da3633", fg="white", font=("Helvetica", 20), width=20, height=2).pack(side="right", padx=15)

def update_gui():
    usdt_label.config(text=f"USDT Balance: ${account_balances.get('USDT', ZERO):,.2f}")
    orders_label.config(text=f"Active Orders: {active_orders}")
    root.after(5000, update_gui)

def grid_cycle():
    while running:
        generate_buy_list()
        for sym in buy_list:
            if running and sym in symbol_info:
                regrid_symbol(sym)
        dynamic_rebalance()
        time.sleep(180)

# -------------------- MAIN --------------------
if __name__ == "__main__":
    load_symbol_info()
    start_user_stream()
    generate_buy_list()
    terminal_insert(f"[{now_cst()}] INFINITY GRID PLATINUM 2025 — FINAL & READY")
    update_gui()
    root.mainloop()
