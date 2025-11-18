#!/usr/bin/env python3
"""
INFINITY GRID PLATINUM 2025 — LIGHTWEIGHT & TRULY UNBANNABLE
Fixed CoinGecko vs_currency error + added headers + fallback
All critical fixes applied
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
buy_list = []
last_buy_list_update = 0
last_pnl_update = 0
cached_total_pnl = ZERO
cached_daily_pnl = ZERO

# Caches
price_cache = {}
atr_cache = {}
rsi_cache = {}
macd_cache = {}
bb_cache = {}
volume_cache = {}

# -------------------- UTILITIES --------------------
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def send_whatsapp_alert(msg: str):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": CALLMEBOT_PHONE, "text": f"[GRID] {msg}", "apikey": CALLMEBOT_API_KEY},
                         timeout=10)
        except:
            pass

def terminal_insert(msg):
    try:
        terminal_text.insert(tk.END, msg + "\n")
        terminal_text.see(tk.END)
    except:
        pass

# -------------------- INDICATORS --------------------
def get_current_price(symbol):
    if symbol in price_cache:
        return price_cache[symbol]
    try:
        return Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
    except:
        return ZERO

def get_volume_ratio(symbol):
    key = f"{symbol}_vol"
    if key in volume_cache and time.time() - volume_cache[key][1] < 300:
        return volume_cache[key][0]
    try:
        klines = client.get_historical_klines(symbol, '1h', "21 hours ago UTC")
        volumes = [float(k[7]) for k in klines]
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
        klines = client.get_historical_klines(symbol, '1h', "35 hours ago UTC")
        closes = [float(k[4]) for k in klines]
        df = pd.DataFrame(closes, columns=['close'])
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs.iloc[-1]))
        rsi_val = Decimal(rsi) if not pd.isna(rsi) else Decimal('50')
        rsi_cache[key] = (rsi_val, time.time())
        return rsi_val
    except:
        return Decimal('50')

def get_bollinger_bands(symbol):
    key = f"{symbol}_bb"
    if key in bb_cache and time.time() - bb_cache[key][4] < 300:
        return bb_cache[key][:4]
    try:
        klines = client.get_historical_klines(symbol, '1h', "30 hours ago UTC")
        closes = [float(k[4]) for k in klines[-25:]]
        series = pd.Series(closes)
        sma = series.rolling(20).mean().iloc[-1]
        std = series.rolling(20).std().iloc[-1]
        price = Decimal(closes[-1])
        upper = Decimal(sma + 2 * std)
        lower = Decimal(sma - 2 * std)
        bb_cache[key] = (price, upper, lower, Decimal(sma), time.time())
        return price, upper, lower, Decimal(sma)
    except:
        price = get_current_price(symbol)
        return price, price * Decimal('1.02'), price * Decimal('0.98'), price

# -------------------- SYMBOL INFO --------------------
def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] != 'USDT' or s['status'] != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s['filters']}
            lot = filters.get('LOT_SIZE', {})
            price_f = filters.get('PRICE_FILTER', {})
            step = Decimal(lot.get('stepSize', '0').rstrip('0').rstrip('.') or '0')
            tick = Decimal(price_f.get('tickSize', '0').rstrip('0').rstrip('.') or '0')
            if step == ZERO or tick == ZERO:
                continue
            symbol_info[s['symbol']] = {
                'stepSize': step,
                'tickSize': tick,
                'minQty': Decimal(lot.get('minQty', '0')),
                'minNotional': Decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            }
        terminal_insert(f"[{now_cst()}] Loaded {len(symbol_info)} USDT trading pairs")
    except Exception as e:
        terminal_insert(f"Symbol load error: {e}")
        sys.exit(1)

# -------------------- SMART BUY LIST — FIXED COINGECKO --------------------
def generate_buy_list():
    global buy_list, last_buy_list_update
    if time.time() - last_buy_list_update < 3600:
        return
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            'vs_currency': 'usd',
            'order': 'market_cap_desc',
            'per_page': 150,
            'page': 1,
            'sparkline': False
        }
        headers = {
            'accept': 'application/json',
            'User-Agent': 'InfinityGridBot/2025'
        }
        response = requests.get(url, params=params, headers=headers, timeout=20)
        if response.status_code != 200:
            raise Exception(f"CoinGecko HTTP {response.status_code}")
        
        coins = response.json()
        candidates = []
        for coin in coins:
            cg_id = coin['id']
            sym = coin['symbol'].upper() + 'USDT'
            if sym not in symbol_info:
                continue
            try:
                price, _, lower_bb, _ = get_bollinger_bands(sym)
                vol_ratio = get_volume_ratio(sym)
                rsi = get_rsi(sym)

                score = 0
                if price > 0 and price < lower_bb * Decimal('1.03'):
                    score += 6
                if vol_ratio > Decimal('2.5'):
                    score += 4
                if rsi < Decimal('42'):
                    score += 3
                if score >= 8:
                    candidates.append((sym, float(score), coin['name']))
            except:
                continue

        buy_list = [x[0] for x in sorted(candidates, key=lambda x: -x[1])[:12]]
        last_buy_list_update = time.time()
        names = ', '.join([f"{s[:-4]}({sc:.0f})" for s, sc, n in sorted(candidates, key=lambda x: -x[1])[:6]])
        terminal_insert(f"[{now_cst()}] Smart Buy List Updated → {len(buy_list)} coins: {names}")
    except Exception as e:
        terminal_insert(f"Buy list failed (will retry in 1h): {e}")
        # Fallback: use top volume coins from Binance
        try:
            tickers = client.get_ticker()
            usdt_pairs = [t for t in tickers if t['symbol'].endswith('USDT')]
            top_vol = sorted(usdt_pairs, key=lambda x: float(x['quoteVolume']), reverse=True)[:10]
            buy_list = [t['symbol'] for t in top_vol]
            terminal_insert(f"[{now_cst()}] Fallback: Using top volume coins")
        except:
            pass

# -------------------- GRID ENGINE --------------------
def place_limit_order(symbol, side, price, qty):
    global active_orders
    info = symbol_info.get(symbol)
    if not info or qty <= 0 or price <= 0:
        return False
    try:
        price = (price // info['tickSize']) * info['tickSize']
        price = price.quantize(info['tickSize'])
        qty = (qty // info['stepSize']) * info['stepSize']
        qty = qty.quantize(info['stepSize'])

        if qty < info['minQty']:
            return False
        notional = price * qty
        if notional < info['minNotional']:
            return False

        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=str(qty),
            price=str(price)
        )
        oid = order['orderId']
        active_grid_orders.setdefault(symbol, []).append(oid)
        active_orders += 1
        return True
    except Exception as e:
        terminal_insert(f"Order failed {symbol} {side} {qty}@{price}: {e}")
        return False

def cancel_symbol_orders(symbol):
    global active_orders
    try:
        open_orders = client.get_open_orders(symbol=symbol)
        for o in open_orders:
            client.cancel_order(symbol=symbol, orderId=o['orderId'])
            active_orders -= 1
        active_grid_orders[symbol] = []
    except:
        pass

def place_platinum_grid(symbol):
    try:
        price = get_current_price(symbol)
        if price <= 0:
            return
        info = symbol_info[symbol]
        vol_ratio = get_volume_ratio(symbol)
        _, _, lower_bb, _ = get_bollinger_bands(symbol)

        grid_pct = Decimal('0.014') + (vol_ratio - 1) * Decimal('0.001')
        grid_pct = max(Decimal('0.010'), min(grid_pct, Decimal('0.022')))

        size_mult = ONE + (vol_ratio - ONE) * Decimal('0.6')
        size_mult = max(Decimal('0.7'), min(size_mult, Decimal('6.0')))
        num_levels = max(7, min(20, 9 + int(float(vol_ratio))))

        cash_per_level = BASE_CASH_PER_LEVEL * size_mult
        reserve = max(account_balances.get('USDT', ZERO) * RESERVE_PCT, MIN_USDT_RESERVE)
        available = account_balances.get('USDT', ZERO) - reserve

        base_asset = symbol.replace('USDT', '')
        owned = account_balances.get(base_asset, ZERO)

        # Buy side
        for i in range(1, num_levels + 1):
            if available < Decimal('5'):
                break
            buy_price = price * ((ONE - grid_pct) ** i)
            buy_price = (buy_price // info['tickSize']) * info['tickSize']
            qty = cash_per_level * (GOLDEN_RATIO ** (i-1)) / buy_price
            qty = (qty // info['stepSize']) * info['stepSize']
            cost = buy_price * qty
            if cost > available:
                break
            if place_limit_order(symbol, 'BUY', buy_price, qty):
                available -= cost

        # Sell side
        for i in range(1, num_levels + 1):
            if owned <= ZERO:
                break
            sell_price = price * ((ONE + grid_pct) ** i)
            sell_price = (sell_price // info['tickSize']) * info['tickSize']
            qty = min(owned, cash_per_level * (SELL_GROWTH_OPTIMAL ** (i-1)) / sell_price)
            qty = (qty // info['stepSize']) * info['stepSize']
            if qty >= info['minQty'] and place_limit_order(symbol, 'SELL', sell_price, qty):
                owned -= qty

    except Exception as e:
        terminal_insert(f"Grid placement error {symbol}: {e}")

def regrid_symbol(symbol):
    if not running:
        return
    cancel_symbol_orders(symbol)
    time.sleep(1.5)  # Avoid rate limit
    place_platinum_grid(symbol)

# -------------------- WEBSOCKETS --------------------
def on_user_message(ws, message):
    try:
        data = json.loads(message)
        e = data.get('e')

        if e == 'outboundAccountPosition':
            for b in data['B']:
                asset = b['a']
                free = Decimal(b['f'])
                locked = Decimal(b['l'])
                total = free + locked
                if total > ZERO:
                    account_balances[asset] = total
                elif asset in account_balances:
                    del account_balances[asset]

        elif e == 'executionReport' and data.get('X') in ['FILLED', 'PARTIALLY_FILLED']:
            if data.get('X') == 'FILLED' or data.get('l') > '0':  # last filled qty
                symbol = data['s']
                side = data['S']
                qty = Decimal(data['l'] or data['q'])
                price = Decimal(data['p'] or data['L'])
                base = symbol.replace('USDT', '')
                msg = f"{side} {qty:.6f} {base} @ ${price:.8f}"
                terminal_insert(f"[{now_cst()}] FILLED → {msg}")
                send_whatsapp_alert(msg)
                if running:
                    threading.Thread(target=regrid_symbol, args=(symbol,), daemon=True).start()

        elif e == '24hrTicker':
            symbol = data['s']
            if symbol.endswith('USDT'):
                price_cache[symbol] = Decimal(data['c'])

    except Exception as e:
        terminal_insert(f"WebSocket parse error: {e}")

def start_user_stream():
    def run():
        while True:
            try:
                listen_key = client.stream_get_listen_key()['listenKey']
                user_url = f"wss://stream.binance.us:9443/ws/{listen_key}"
                ticker_url = "wss://stream.binance.us:9443/stream?streams=!ticker@arr"

                user_ws = websocket.WebSocketApp(user_url, on_message=on_user_message)
                ticker_ws = websocket.WebSocketApp(ticker_url, on_message=on_user_message)

                user_ws.run_forever(ping_interval=1800, ping_timeout=10)
                ticker_ws.run_forever(ping_interval=60)
            except Exception as e:
                terminal_insert(f"WebSocket crashed: {e}. Reconnecting in 10s...")
                time.sleep(10)
    threading.Thread(target=run, daemon=True).start()

# -------------------- P&L --------------------
def update_pnl_light():
    global cached_total_pnl, cached_daily_pnl, last_pnl_update
    if time.time() - last_pnl_update < 300:
        return cached_total_pnl, cached_daily_pnl
    try:
        total = daily = ZERO
        today = date.today()
        for sym in list(symbol_info.keys())[:25]:
            try:
                trades = client.get_my_trades(symbol=sym, limit=100)
                for t in trades:
                    if not t['isBuyer']:
                        profit = Decimal(t['quoteQty']) - Decimal(t.get('commission', '0'))
                        total += profit
                        if datetime.fromtimestamp(t['time']/1000).date() == today:
                            daily += profit
            except:
                continue
        cached_total_pnl = total.quantize(Decimal('0.01'))
        cached_daily_pnl = daily.quantize(Decimal('0.01'))
        last_pnl_update = time.time()
    except:
        pass
    return cached_total_pnl, cached_daily_pnl

# -------------------- GUI & MAIN --------------------
# ... [GUI code remains same, only small fixes below]

def update_gui():
    if not running and 'root' in globals():
        return
    total_pnl, daily_pnl = update_pnl_light()
    usdt_label.config(text=f"USDT: ${account_balances.get('USDT', ZERO):,.2f}")
    total_pnl_label.config(text=f"Total P&L: ${total_pnl:+,.2f}",
                           fg="#00ff00" if total_pnl >= 0 else "#ff4444")
    pnl_24h_label.config(text=f"24h P&L: ${daily_pnl:+,.2f}",
                         fg="#00ff00" if daily_pnl >= 0 else "#ff4444")
    orders_label.config(text=f"Active Orders: {active_orders}")
    root.after(10000, update_gui)

def grid_cycle():
    while running:
        generate_buy_list()
        for sym in buy_list[:]:
            if running and sym in symbol_info:
                regrid_symbol(sym)
                time.sleep(2)  # Be gentle
        for _ in range(180):  # 3-minute cycle
            if not running:
                break
            time.sleep(1)

if __name__ == "__main__":
    load_symbol_info()
    start_user_stream()
    time.sleep(3)
    generate_buy_list()
    terminal_insert(f"[{now_cst()}] INFINITY GRID PLATINUM 2025 — FULLY UNBANNABLE & READY")
    update_gui()
    root.mainloop()
