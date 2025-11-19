#!/usr/bin/env python3
"""
INFINITY GRID PLATINUM 2025 — FINAL AGGRESSIVE EXIT EDITION
★ Starts full exit at 17:30 CST
★ Limit sells 0.2% below best bid
★ Retries every 5 minutes until 100% in USDT
★ Only then sleeps safely overnight (no trading 18:00–03:00)
★ Every function included — zero missing
November 19, 2025
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
from binance.exceptions import BinanceAPIException

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

# EXIT & TRADING HOURS
EVENING_EXIT_START = "17:30"      # Begin exit
TRADING_START_HOUR = 3            # Resume trading
TRADING_END_HOUR = 18             # No new buys after this
EXIT_RETRY_INTERVAL = 300        # 5 minutes
exit_in_progress = False
last_exit_attempt = 0
exit_sell_orders = {}  # symbol -> list of active exit order IDs

# RSI Settings
RSI_PERIOD = 14
RSI_OVERBOUGHT = Decimal('70')
RSI_OVERSOLD = Decimal('35')

# -------------------- CALLMEBOT --------------------
last_buy_alert = 0
last_sell_alert = 0
ALERT_COOLDOWN = 3600

CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')
if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE:
    messagebox.showwarning("CallMeBot", "CALLMEBOT_API_KEY or CALLMEBOT_PHONE not set — WhatsApp alerts DISABLED")
else:
    print("✓ CallMeBot WhatsApp alerts ENABLED")

api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
if not api_key or not api_secret:
    messagebox.showerror("Error", "BINANCE_API_KEY and BINANCE_API_SECRET required!")
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
kline_cache = {}

BLACKLISTED_BASE_ASSETS = {
    'BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'BCH', 'LTC', 'DOGE', 'PEPE', 'SHIB',
    'USDT', 'USDC', 'DAI', 'TUSD', 'FDUSD', 'BUSD', 'USDP', 'GUSD',
    'WBTC', 'WETH', 'STETH', 'CBETH', 'RETH'
}

# -------------------- UTILITIES --------------------
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def terminal_insert(msg):
    try:
        terminal_text.insert(tk.END, msg + "\n")
        terminal_text.see(tk.END)
    except:
        pass

def send_whatsapp(message):
    global last_buy_alert, last_sell_alert
    if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE:
        return
    now = time.time()
    is_buy = "BUY" in message.upper()
    is_sell = any(x in message.upper() for x in ["SELL", "EXIT", "DUMP"])
    if (is_buy and now - last_buy_alert < ALERT_COOLDOWN) or (is_sell and now - last_sell_alert < ALERT_COOLDOWN):
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(message)}&apikey={CALLMEBOT_API_KEY}"
        requests.get(url, timeout=10)
        if is_buy: last_buy_alert = now
        if is_sell: last_sell_alert = now
    except:
        pass

# -------------------- TRADING HOURS & EXIT LOGIC --------------------
def is_trading_allowed():
    if exit_in_progress:
        return True  # Allow exit sells
    now = datetime.now(CST)
    return TRADING_START_HOUR <= now.hour < TRADING_END_HOUR

def is_evening_exit_time():
    now = datetime.now(CST)
    return now.strftime("%H:%M") >= EVENING_EXIT_START

def is_portfolio_fully_in_usdt():
    update_balances()
    for asset, qty in account_balances.items():
        if asset != 'USDT' and qty > ZERO:
            if asset + 'USDT' in symbol_info:
                return False
    return True

# -------------------- AGGRESSIVE EVENING EXIT --------------------
def aggressive_evening_exit():
    global exit_in_progress, last_exit_attempt

    if not is_evening_exit_time() and not exit_in_progress:
        return

    if is_portfolio_fully_in_usdt():
        if exit_in_progress:
            terminal_insert(f"[{now_cst()}] ✓ FULL EXIT COMPLETE — 100% IN USDT — NIGHT SLEEP MODE")
            send_whatsapp("EXIT COMPLETE — 100% USDT — Safe overnight until 3 AM")
            exit_in_progress = False
            exit_sell_orders.clear()
        return

    if time.time() - last_exit_attempt < EXIT_RETRY_INTERVAL:
        return
    last_exit_attempt = time.time()

    if not exit_in_progress:
        exit_in_progress = True
        terminal_insert(f"[{now_cst()}] EVENING EXIT ACTIVATED — Dumping all assets to USDT")
        send_whatsapp("EVENING EXIT STARTED (17:30+) — Aggressive limit sells every 5min until 100% USDT")

    update_balances()
    sold_this_wave = 0

    for asset, qty in list(account_balances.items()):
        if asset == 'USDT' or qty <= ZERO:
            continue
        sym = asset + 'USDT'
        if sym not in symbol_info:
            continue

        try:
            ticker = client.get_symbol_ticker(symbol=sym)
            current_price = Decimal(ticker['price'])
            book = client.get_order_book(symbol=sym, limit=5)
            best_bid = Decimal(book['bids'][0][0]) if book['bids'] else current_price * Decimal('0.998')
            aggressive_price = (best_bid * Decimal('0.998')).quantize(symbol_info[sym]['tickSize'], rounding='ROUND_DOWN')

            info = symbol_info[sym]
            sell_qty = (qty // info['stepSize']) * info['stepSize']
            sell_qty = sell_qty.quantize(Decimal('0.00000000'))
            if sell_qty < info['minQty']:
                continue

            # Cancel previous exit orders
            if sym in exit_sell_orders:
                for oid in exit_sell_orders[sym]:
                    try: client.cancel_order(symbol=sym, orderId=oid)
                    except: pass
                exit_sell_orders[sym] = []

            # Place new tight limit sell
            order = client.create_order(
                symbol=sym,
                side='SELL',
                type='LIMIT',
                timeInForce='GTC',
                price=str(aggressive_price),
                quantity=str(sell_qty)
            )
            exit_sell_orders[sym] = [order['orderId']]
            sold_this_wave += 1
            terminal_insert(f"[{now_cst()}] EXIT SELL {asset} {sell_qty} @ ${aggressive_price} (bid ${best_bid})")

        except Exception as e:
            terminal_insert(f"Exit error {asset}: {e}")

    if sold_this_wave > 0:
        send_whatsapp(f"EXIT WAVE — {sold_this_wave} assets selling @ bid-0.2% — retry in 5min")

# -------------------- BALANCES & SYMBOL INFO --------------------
def update_balances():
    global account_balances
    try:
        info = client.get_account()
        new_balances = {}
        for a in info['balances']:
            asset = a['asset']
            total = Decimal(a['free']) + Decimal(a['locked'])
            if total > ZERO:
                new_balances[asset] = total
        account_balances = new_balances
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Balance update failed: {e}")

def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] != 'USDT' or s['status'] != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s['filters']}
            step = Decimal(filters.get('LOT_SIZE', {}).get('stepSize', '0'))
            tick = Decimal(filters.get('PRICE_FILTER', {}).get('tickSize', '0'))
            if step == ZERO or tick == ZERO:
                continue
            symbol_info[s['symbol']] = {
                'stepSize': step,
                'tickSize': tick,
                'minQty': Decimal(filters.get('LOT_SIZE', {}).get('minQty', '0')),
                'minNotional': Decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            }
        terminal_insert(f"[{now_cst()}] Loaded {len(symbol_info)} USDT pairs")
    except Exception as e:
        terminal_insert(f"Symbol load error: {e}")

# -------------------- RSI & ORDER BOOK --------------------
def get_rsi(symbol, period=14):
    try:
        if symbol not in kline_cache or len(kline_cache[symbol]) < period + 1:
            klines = client.get_klines(symbol=symbol, interval='1m', limit=period + 1)
            closes = [Decimal(k[4]) for k in klines]
            kline_cache[symbol] = closes
        else:
            latest = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
            kline_cache[symbol] = (kline_cache[symbol][-(period):] + [latest])[-period-1:]

        closes = kline_cache[symbol]
        if len(closes) < period + 1:
            return Decimal('50')

        gains = losses = ZERO
        for i in range(1, len(closes)):
            change = closes[i] - closes[i-1]
            if change > 0:
                gains += change
            else:
                losses -= change

        if losses == ZERO: return Decimal('100')
        if gains == ZERO: return ZERO

        rs = (gains / period) / (losses / period)
        rsi = Decimal('100') - (Decimal('100') / (ONE + rs))
        return rsi.quantize(Decimal('0.01'))
    except:
        return Decimal('50')

def get_buy_pressure(symbol):
    try:
        book = client.get_order_book(symbol=symbol, limit=500)
        bids = sum(Decimal(b[1]) * Decimal(b[0]) for b in book['bids'][:20])
        asks = sum(Decimal(a[1]) * Decimal(a[0]) for a in book['asks'][:20])
        total = bids + asks
        if total == ZERO:
            return Decimal('0.5')
        return (bids / total).quantize(Decimal('0.0001'))
    except:
        return Decimal('0.5')

# -------------------- REBALANCE --------------------
def dynamic_rebalance():
    if exit_in_progress or not is_trading_allowed():
        return
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

        terminal_insert(f"[{now_cst()}] Rebalance — Portfolio ${total_portfolio:,.0f}")

        for asset in list(account_balances.keys()):
            if asset == 'USDT': continue
            sym = asset + 'USDT'
            if sym not in symbol_info: continue

            rsi = get_rsi(sym)
            pressure = get_buy_pressure(sym)
            current_qty = account_balances.get(asset, ZERO)
            current_price = price_cache.get(sym, Decimal(client.get_symbol_ticker(symbol=sym)['price']))
            current_value = current_qty * current_price

            if rsi >= RSI_OVERBOUGHT and pressure >= Decimal('0.65'):
                target_pct = Decimal('0.15')
                reason = f"RSI≥70 + Strong Bids"
            elif rsi <= RSI_OVERSOLD and pressure <= Decimal('0.35'):
                target_pct = Decimal('0.04')
                reason = f"RSI≤35 + Weak Bids"
            else:
                target_pct = Decimal('0.05')
                reason = "Neutral"

            target_value = total_portfolio * target_pct

            if current_value > target_value * Decimal('1.05'):
                sell_qty = ((current_value - target_value) / current_price)
                sell_qty = (sell_qty // symbol_info[sym]['stepSize']) * symbol_info[sym]['stepSize']
                sell_qty = sell_qty.quantize(Decimal('0.00000000'))
                if sell_qty >= symbol_info[sym]['minQty']:
                    place_limit_order(sym, 'SELL', current_price * Decimal('0.999'), sell_qty)
                    msg = f"REBAL SELL {asset} → {target_pct:.0%} [{reason}]"
                    terminal_insert(f"[{now_cst()}] {msg}")
                    send_whatsapp(f"↓ {msg}")

            elif current_value < target_value * Decimal('0.95'):
                buy_qty = ((target_value - current_value) / current_price)
                buy_qty = (buy_qty // symbol_info[sym]['stepSize']) * symbol_info[sym]['stepSize']
                buy_qty = buy_qty.quantize(Decimal('0.00000000'))
                required = current_price * buy_qty * (ONE + FEE_RATE)
                available = account_balances.get('USDT', ZERO) - (account_balances.get('USDT', ZERO) * RESERVE_PCT + MIN_USDT_RESERVE)
                if available >= required and buy_qty >= symbol_info[sym]['minQty']:
                    place_limit_order(sym, 'BUY', current_price * Decimal('1.001'), buy_qty)
                    msg = f"REBAL BUY {asset} → {target_pct:.0%} [{reason}]"
                    terminal_insert(f"[{now_cst()}] {msg}")
                    send_whatsapp(f"↑ {msg}")
    except Exception as e:
        terminal_insert(f"Rebalance error: {e}")

# -------------------- GRID ORDERS --------------------
def place_limit_order(symbol, side, price, qty):
    if side == 'BUY' and (exit_in_progress or not is_trading_allowed()):
        return False

    global active_orders
    info = symbol_info.get(symbol)
    if not info:
        return False

    try:
        price = (Decimal(price) // info['tickSize']) * info['tickSize']
        qty = (qty // info['stepSize']) * info['stepSize']
        qty = qty.quantize(Decimal('0.00000000'))
        if qty < info['minQty']:
            return False
        notional = price * qty
        if notional < info['minNotional']:
            return False

        if side == 'BUY':
            required = notional * (ONE + FEE_RATE)
            available = account_balances.get('USDT', ZERO) - (account_balances.get('USDT', ZERO) * RESERVE_PCT + MIN_USDT_RESERVE)
            if available < required:
                return False

        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            price=str(price),
            quantity=str(qty)
        )
        if not exit_in_progress:
            active_grid_orders.setdefault(symbol, []).append(order['orderId'])
        active_orders += 1

        base = symbol.replace('USDT', '')
        msg = f"GRID {side} {base} {qty} @ ${price:.8f}"
        terminal_insert(f"[{now_cst()}] SUCCESS {msg}")
        send_whatsapp(f"∞ {msg}")
        return True
    except BinanceAPIException as e:
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
        if not exit_in_progress:
            active_grid_orders[symbol] = []
    except:
        pass

def place_platinum_grid(symbol):
    if exit_in_progress or not is_trading_allowed():
        return
    try:
        price = price_cache.get(symbol, Decimal(client.get_symbol_ticker(symbol=symbol)['price']))
        info = symbol_info[symbol]
        grid_pct = Decimal('0.012')
        cash = BASE_CASH_PER_LEVEL * Decimal('1.5')
        reserve = account_balances.get('USDT', ZERO) * RESERVE_PCT + MIN_USDT_RESERVE

        for i in range(1, 9):
            buy_price = price * ((ONE - grid_pct) ** i)
            buy_price = (buy_price // info['tickSize']) * info['tickSize']
            qty = cash * (GOLDEN_RATIO ** (i-1)) / buy_price
            qty = (qty // info['stepSize']) * info['stepSize']
            qty = qty.quantize(Decimal('0.00000000'))
            required = buy_price * qty * (ONE + FEE_RATE)
            if account_balances.get('USDT', ZERO) - reserve >= required:
                place_limit_order(symbol, 'BUY', buy_price, qty)

        owned = account_balances.get(symbol.replace('USDT', ''), ZERO)
        for i in range(1, 9):
            sell_price = price * ((ONE + grid_pct) ** i)
            sell_price = (sell_price // info['tickSize']) * info['tickSize']
            qty = cash * (SELL_GROWTH_OPTIMAL ** (i-1)) / sell_price
            qty = (qty // info['stepSize']) * info['stepSize']
            qty = qty.quantize(Decimal('0.00000000'))
            if qty <= owned:
                place_limit_order(symbol, 'SELL', sell_price, qty)
                owned -= qty
    except:
        pass

def regrid_symbol(symbol):
    if exit_in_progress:
        return
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
            fill_msg = f"FILLED {side} {base} {qty:.6f} @ ${price:.8f}"
            terminal_insert(f"[{now_cst()}] {fill_msg}")
            send_whatsapp(f"{'🟢' if side=='BUY' else '🔴'} {base} {qty:.4f} @ ${price:.6f}")
            if not exit_in_progress:
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
    global buy_list, last_buy_list_update
    if exit_in_progress or not is_trading_allowed():
        return
    if time.time() - last_buy_list_update < 3600:
        return

    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            'vs_currency': 'usd',
            'order': 'market_cap_desc',
            'per_page': 100,
            'page': 1,
            'sparkline': False,
            'price_change_percentage': '7d,14d'
        }
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        coins = r.json()

        candidates = []
        for coin in coins:
            base = coin['symbol'].upper()
            if base in BLACKLISTED_BASE_ASSETS:
                continue
            sym = base + 'USDT'
            if sym not in symbol_info:
                continue

            market_cap = coin.get('market_cap', 0)
            volume = coin.get('total_volume', 0)
            change_7d = coin.get('price_change_percentage_7d_in_currency', 0) or 0
            change_14d = coin.get('price_change_percentage_14d_in_currency', 0) or 0

            if market_cap < 800_000_000 or volume < 40_000_000:
                continue
            if change_7d < 6 or change_14d < 12:
                continue

            score = change_7d * 1.5 + change_14d + (volume / max(market_cap, 1) * 100)
            candidates.append((sym, score, coin['name']))

        candidates.sort(key=lambda x: -x[1])
        buy_list = [x[0] for x in candidates[:10]]
        last_buy_list_update = time.time()
        terminal_insert(f"[{now_cst()}] Buy list updated — {len(buy_list)} coins")
        send_whatsapp(f"New trending list — Top {len(buy_list)} coins")

    except Exception as e:
        terminal_insert(f"Buy list error: {e}")
        buy_list = ['ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'MATICUSDT', 'LINKUSDT', 'UNIUSDT', 'AAVEUSDT', 'CRVUSDT', 'COMPUSDT', 'MKRUSDT']

# -------------------- GUI --------------------
root = tk.Tk()
root.title("INFINITY GRID PLATINUM 2025")
root.geometry("800x900")
root.resizable(False, False)
root.configure(bg="#0d1117")

screen_w = root.winfo_screenwidth()
screen_h = root.winfo_screenheight()
x = (screen_w - 800) // 2
y = (screen_h - 900) // 2
root.geometry(f"800x900+{x}+{y}")

title_font = tkfont.Font(family="Helvetica", size=30, weight="bold")
big_font = tkfont.Font(family="Helvetica", size=18, weight="bold")
term_font = tkfont.Font(family="Consolas", size=11)

tk.Label(root, text="INFINITY GRID", font=title_font, fg="#58a6ff", bg="#0d1117").pack(pady=10)
tk.Label(root, text="PLATINUM 2025", font=title_font, fg="#ffffff", bg="#0d1117").pack(pady=5)

stats = tk.Frame(root, bg="#0d1117")
stats.pack(padx=40, fill="x", pady=20)
usdt_label = tk.Label(stats, text="USDT Balance: $0.00", font=big_font, fg="#8b949e", bg="#0d1117", anchor="w")
usdt_label.pack(fill="x", pady=5)
orders_label = tk.Label(stats, text="Active Orders: 0", font=big_font, fg="#39d353", bg="#0d1117", anchor="w")
orders_label.pack(fill="x", pady=5)

terminal_frame = tk.Frame(root, bg="black")
terminal_frame.pack(fill="both", expand=True, padx=40, pady=10)
terminal_text = scrolledtext.ScrolledText(terminal_frame, bg="black", fg="#39d353", font=term_font)
terminal_text.pack(fill="both", expand=True)

button_frame = tk.Frame(root, bg="#0d1117")
button_frame.pack(pady=30)
status_label = tk.Label(button_frame, text="Status: Stopped", font=big_font, fg="red", bg="#0d1117")
status_label.pack(side="left", padx=60)

def start_bot():
    global running
    running = True
    threading.Thread(target=grid_cycle, daemon=True).start()
    status_label.config(text="Status: RUNNING", fg="#00ff00")
    terminal_insert(f"[{now_cst()}] BOT STARTED — Exit at 17:30 → 100% USDT → Safe sleep")
    send_whatsapp("INFINITY GRID PLATINUM 2025 STARTED\n• Full exit from 17:30 CST\n• Retries every 5min until 100% USDT")

def stop_bot():
    global running
    running = False
    for s in list(active_grid_orders.keys()):
        cancel_symbol_orders(s)
    status_label.config(text="Status: Stopped", fg="red")
    send_whatsapp("INFINITY GRID STOPPED")

tk.Button(button_frame, text="START BOT", command=start_bot, bg="#238636", fg="white", font=big_font, width=20, height=2).pack(side="right", padx=15)
tk.Button(button_frame, text="STOP BOT", command=stop_bot, bg="#da3633", fg="white", font=big_font, width=20, height=2).pack(side="right", padx=15)

def update_gui():
    usdt_label.config(text=f"USDT Balance: ${account_balances.get('USDT', ZERO):,.2f}")
    orders_label.config(text=f"Active Orders: {active_orders}")
    root.after(5000, update_gui)

# -------------------- MAIN CYCLE --------------------
def grid_cycle():
    while running:
        aggressive_evening_exit()

        if exit_in_progress or not is_trading_allowed():
            time.sleep(60)
            continue

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
    terminal_insert(f"[{now_cst()}] INFINITY GRID PLATINUM 2025 — FULLY COMPLETE & READY")
    update_gui()
    root.mainloop()
