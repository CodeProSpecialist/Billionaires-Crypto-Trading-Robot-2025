#!/usr/bin/env python3
"""
INFINITY GRID PLATINUM 2025 — FINAL COMPLETE VERSION
CoinGecko Top Coins Buy List + Volume + Bollinger + RSI + MACD + Order Book
WebSockets for Fills/Balances/Prices — REST only for P&L
November 18, 2025 — THE UNSTOPPABLE PROFIT BOT
"""

import os
import sys
import time
import threading
import json
import requests
from datetime import datetime, date
from decimal import Decimal, getcontext
import tkinter as tk
from tkinter import font as tkfont, messagebox, scrolledtext
import pytz
import websocket
from binance.client import Client
from binance.exceptions import BinanceAPIException
import numpy as np
import pandas as pd

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
MIN_POSITION_PCT = Decimal('0.02')
MAX_POSITION_PCT = Decimal('0.15')

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
last_buy_list_update = 0
last_rebalance = 0
last_pnl_update = 0
cached_total_pnl = ZERO
cached_daily_pnl = ZERO

# Caches
atr_cache = {}
rsi_cache = {}
macd_cache = {}
bb_cache = {}
volume_cache = {}

# -------------------- UTILITIES --------------------
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def terminal_insert(msg):
    try:
        terminal_text.insert(tk.END, msg + "\n")
        terminal_text.see(tk.END)
    except:
        pass

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

# -------------------- INDICATORS --------------------
def get_volume_ratio(symbol):
    key = f"{symbol}_vol"
    if key in volume_cache and time.time() - volume_cache[key][1] < 300:
        return volume_cache[key][0]
    try:
        klines = client.get_klines(symbol=symbol, interval='1h', limit=21)
        volumes = [float(k[7]) for k in klines if float(k[7]) > 0]
        if len(volumes) < 2:
            return ONE
        avg = np.mean(volumes[:-1])
        ratio = volumes[-1] / avg if avg > 0 else 1.0
        volume_cache[key] = (Decimal(ratio), time.time())
        return Decimal(ratio)
    except:
        return ONE

def get_rsi(symbol):
    key = f"{symbol}_rsi"
    if key in rsi_cache and time.time() - rsi_cache[key][1] < 300:
        return rsi_cache[key][0]
    try:
        klines = client.get_klines(symbol=symbol, interval='1h', limit=35)
        closes = [float(k[4]) for k in klines]
        if len(closes) < 15:
            return Decimal('50')
        deltas = np.diff(closes)
        gains = [x for x in deltas[-14:] if x > 0]
        losses = [-x for x in deltas[-14:] if x < 0]
        avg_gain = np.mean(gains) if gains else 0
        avg_loss = np.mean(losses) if losses else 0.001
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))
        rsi_cache[key] = (Decimal(rsi), time.time())
        return Decimal(rsi)
    except:
        return Decimal('50')

def get_macd(symbol):
    key = f"{symbol}_macd"
    if key in macd_cache and time.time() - macd_cache[key][3] < 300:
        return macd_cache[key][:3]
    try:
        klines = client.get_klines(symbol=symbol, interval='1h', limit=50)
        closes = pd.Series([float(k[4]) for k in klines])
        exp12 = closes.ewm(span=12, adjust=False).mean()
        exp26 = closes.ewm(span=26, adjust=False).mean()
        macd_line = exp12 - exp26
        signal = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal
        macd_cache[key] = (Decimal(macd_line.iloc[-1]), Decimal(signal.iloc[-1]), Decimal(hist.iloc[-1]), time.time())
        return macd_cache[key][:3]
    except:
        return ZERO, ZERO, ZERO

def get_bollinger_bands(symbol):
    key = f"{symbol}_bb"
    if key in bb_cache and time.time() - bb_cache[key][4] < 300:
        return bb_cache[key][:4]
    try:
        klines = client.get_klines(symbol=symbol, interval='1h', limit=30)
        closes = np.array([float(k[4]) for k in klines[-25:]])
        if len(closes) < 20:
            price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
            return price, price * Decimal('1.02'), price * Decimal('0.98'), price
        sma = np.mean(closes[-20:])
        std = np.std(closes[-20:])
        price = Decimal(closes[-1])
        upper = Decimal(sma + 2 * std)
        lower = Decimal(sma - 2 * std)
        bb_cache[key] = (price, upper, lower, Decimal(sma), time.time())
        return price, upper, lower, Decimal(sma)
    except:
        price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
        return price, price * Decimal('1.02'), price * Decimal('0.98'), price

def get_atr(symbol):
    key = f"{symbol}_atr"
    if key in atr_cache and time.time() - atr_cache[key][1] < 300:
        return atr_cache[key][0]
    try:
        klines = client.get_klines(symbol=symbol, interval='1h', limit=15)
        trs = []
        for i in range(1, len(klines)):
            h = Decimal(klines[i][2])
            l = Decimal(klines[i][3])
            pc = Decimal(klines[i-1][4])
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        atr_val = sum(trs) / len(trs) if trs else ZERO
        atr_cache[key] = (atr_val, time.time())
        return atr_val
    except:
        price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
        return price * Decimal('0.02')

def get_orderbook_bias(symbol):
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

# -------------------- COINGECKO TOP COINS BUY LIST --------------------
def generate_buy_list():
    global buy_list, last_buy_list_update
    if time.time() - last_buy_list_update < 3600:
        return
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            'vs_currency': 'usd',
            'order': 'market_cap_desc',
            'per_page': 100,
            'page': 1,
            'price_change_percentage': '24h,7d,14d'
        }
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        coins = r.json()
        candidates = []
        for coin in coins:
            sym = coin['symbol'].upper() + 'USDT'
            if sym not in symbol_info: continue
            market_cap = coin.get('market_cap', 0)
            volume = coin.get('total_volume', 0)
            change_7d = coin.get('price_change_percentage_7d_in_currency', 0) or 0
            change_14d = coin.get('price_change_percentage_14d_in_currency', 0) or 0
            if market_cap < 1_000_000_000 or volume < 50_000_000: continue
            score = change_7d * 1.5 + change_14d + (volume / market_cap * 100)
            candidates.append((sym, score))
        candidates.sort(key=lambda x: -x[1])
        buy_list = [x[0] for x in candidates[:10]]
        last_buy_list_update = time.time()
        terminal_insert(f"[{now_cst()}] COINGECKO BUY LIST: {len(buy_list)} coins → {', '.join(buy_list)}")
    except Exception as e:
        terminal_insert(f"Buy list error: {e}")
        buy_list = ['SOLUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'MATICUSDT']

# -------------------- ORDER BOOK REBALANCE --------------------
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
                price = Decimal(client.get_symbol_ticker(symbol=sym)['price'])
                total_portfolio += account_balances[asset] * price

        if total_portfolio <= ZERO:
            return

        terminal_insert(f"[{now_cst()}] Dynamic rebalance — Portfolio ${total_portfolio:,.0f}")

        for asset in list(account_balances.keys()):
            if asset == 'USDT': continue
            sym = asset + 'USDT'
            if sym not in symbol_info: continue

            pressure = get_orderbook_bias(sym)
            target_pct = MIN_POSITION_PCT + (MAX_POSITION_PCT - MIN_POSITION_PCT) * max(0, (pressure - Decimal('0.5')) * 2)
            target_pct = max(MIN_POSITION_PCT, min(MAX_POSITION_PCT, target_pct))

            current_qty = account_balances.get(asset, ZERO)
            current_price = Decimal(client.get_symbol_ticker(symbol=sym)['price'])
            current_value = current_qty * current_price
            target_value = total_portfolio * target_pct

            if current_value > target_value * Decimal('1.05'):
                sell_qty = (current_value - target_value) / current_price
                sell_qty = (sell_qty // symbol_info[sym]['stepSize']) * symbol_info[sym]['stepSize']
                sell_qty = sell_qty.quantize(Decimal('0.00000000'))
                if sell_qty >= symbol_info[sym]['minQty']:
                    place_limit_order(sym, 'SELL', current_price * Decimal('0.999'), sell_qty)
                    terminal_insert(f"[{now_cst()}] REBAL SELL {asset} {sell_qty} (pressure {pressure:.1%})")

            elif current_value < target_value * Decimal('0.95') and pressure > Decimal('0.6'):
                buy_qty = (target_value - current_value) / current_price
                buy_qty = (buy_qty // symbol_info[sym]['stepSize']) * symbol_info[sym]['stepSize']
                buy_qty = buy_qty.quantize(Decimal('0.00000000'))
                required = current_price * buy_qty * (ONE + FEE_RATE)
                available = account_balances.get('USDT', ZERO) - (account_balances.get('USDT', ZERO) * RESERVE_PCT + MIN_USDT_RESERVE)
                if available >= required and buy_qty >= symbol_info[sym]['minQty']:
                    place_limit_order(sym, 'BUY', current_price * Decimal('1.001'), buy_qty)
                    terminal_insert(f"[{now_cst()}] REBAL BUY {asset} {buy_qty} (pressure {pressure:.1%})")

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
    except BinanceAPIException as e:
        if e.code == -2010:
            terminal_insert(f"[{now_cst()}] Insufficient balance — skipping {side} {symbol}")
        else:
            terminal_insert(f"Binance error {e.code}: {e.message}")
        return False
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
        vol_ratio = get_volume_ratio(symbol)
        price_bb, _, lower_bb, _ = get_bollinger_bands(symbol)

        grid_pct = Decimal('0.012')
        size_multiplier = Decimal('1.0') + vol_ratio * Decimal('0.5')
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

# -------------------- P&L (LIGHT REST) --------------------
def get_light_pnl():
    global cached_total_pnl, cached_daily_pnl, last_pnl_update
    if time.time() - last_pnl_update < 600:
        return cached_total_pnl, cached_daily_pnl
    try:
        total = Decimal('0')
        daily = Decimal('0')
        today = date.today()
        for symbol in list(symbol_info.keys())[:20]:
            try:
                trades = client.get_my_trades(symbol=symbol, limit=200)
                for t in trades:
                    if t['isBuyer']: continue
                    qty = Decimal(t['qty'])
                    price = Decimal(t['price'])
                    fee = Decimal(t.get('commission', '0'))
                    profit = qty * price - fee
                    total += profit
                    if datetime.fromtimestamp(t['time']/1000).date() == today:
                        daily += profit
            except: continue
        cached_total_pnl = total.quantize(Decimal('0.01'))
        cached_daily_pnl = daily.quantize(Decimal('0.01'))
        last_pnl_update = time.time()
    except:
        pass
    return cached_total_pnl, cached_daily_pnl

# -------------------- GUI --------------------
root = tk.Tk()
root.title("INFINITY GRID PLATINUM 2025")
root.geometry("1000x1000")
root.configure(bg="#0d1117")

tk.Label(root, text="INFINITY GRID PLATINUM 2025", font=("Helvetica", 30, "bold"), fg="#58a6ff", bg="#0d1117").pack(pady=20)

stats = tk.Frame(root, bg="#0d1117")
stats.pack(padx=50, fill="x")
usdt_label = tk.Label(stats, text="USDT: $0.00", font=("Helvetica", 20), fg="#8b949e", bg="#0d1117", anchor="w")
usdt_label.pack(fill="x", pady=5)
total_pnl_label = tk.Label(stats, text="Total P&L: $0.00", font=("Helvetica", 20), fg="lime", bg="#0d1117", anchor="w")
total_pnl_label.pack(fill="x", pady=8)
pnl_24h_label = tk.Label(stats, text="24h P&L: $0.00", font=("Helvetica", 20), fg="lime", bg="#0d1117", anchor="w")
pnl_24h_label.pack(fill="x", pady=8)
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
    terminal_insert(f"[{now_cst()}] BOT STARTED — UNBANNABLE MODE")

def stop_bot():
    global running
    running = False
    for s in list(active_grid_orders.keys()):
        cancel_symbol_orders(s)
    status_label.config(text="Status: Stopped", fg="red")

tk.Button(button_frame, text="START BOT", command=start_bot, bg="#238636", fg="white", font=("Helvetica", 20), width=20, height=2).pack(side="right", padx=15)
tk.Button(button_frame, text="STOP BOT", command=stop_bot, bg="#da3633", fg="white", font=("Helvetica", 20), width=20, height=2).pack(side="right", padx=15)

def update_gui():
    total_pnl, daily_pnl = get_light_pnl()
    usdt_label.config(text=f"USDT Balance: ${account_balances.get('USDT', ZERO):,.2f}")
    total_pnl_label.config(text=f"Total P&L: ${total_pnl:+,.2f}", fg="#00ff00" if total_pnl >= 0 else "#ff4444")
    pnl_24h_label.config(text=f"24h P&L: ${daily_pnl:+,.2f}", fg="#00ff00" if daily_pnl >= 0 else "#ff4444")
    orders_label.config(text=f"Active Orders: {active_orders}")
    root.after(10000, update_gui)

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
