#!/usr/bin/env python3
"""
INFINITY GRID PLATINUM 2025 — ULTIMATE FINAL PERFECTION + TA-LIB (November 21, 2025)
★ Legacy backbone 100% preserved
★ All indicators replaced with real TA-Lib on 500 daily klines
★ RSI, MACD + crossover, MFI, SMA200, candlestick patterns, volume spike, bullish momentum
★ NEW: Large safe fallback list if CoinGecko momentum list ever drops below 25 coins
"""

import os
import sys
import time
import json
import random
import requests
import threading
import numpy as np
import talib
from datetime import datetime
from decimal import Decimal, getcontext, ROUND_DOWN

import pytz
import websocket
from binance.client import Client
from binance.exceptions import BinanceAPIException

import tkinter as tk
from tkinter import font as tkfont, messagebox, scrolledtext

# -------------------- PRECISION & CONSTANTS --------------------
getcontext().prec = 28
ZERO = Decimal('0')
ONE = Decimal('1')

maker_fee = Decimal('0.0010')
taker_fee = Decimal('0.0020')
last_fee_update = 0
FEE_UPDATE_INTERVAL = 21600

BASE_CASH_PER_LEVEL = Decimal('8.0')
GOLDEN_RATIO = Decimal('1.618034')
SELL_GROWTH_OPTIMAL = Decimal('1.309')
RESERVE_PCT = Decimal('0.33')
MIN_USDT_RESERVE = Decimal('8')
CST = pytz.timezone("America/Chicago")

EVENING_EXIT_START = "17:30"
TRADING_START_HOUR = 3
TRADING_END_HOUR = 18
EXIT_RETRY_INTERVAL = 300
exit_in_progress = False
last_exit_attempt = 0

last_buy_alert = 0
last_sell_alert = 0
ALERT_COOLDOWN = 600

running = True

# -------------------- ENVIRONMENT --------------------
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

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
active_orders = 0
price_cache = {}
buy_list = []
last_buy_list_update = 0
daily_klines_cache = {}      # symbol -> dict with np arrays + talib outputs
last_kline_update = {}       # symbol -> timestamp
placing_order_for = set()
last_whale_check = {}
WHALE_CHECK_INTERVAL = 60

BLACKLISTED_BASE_ASSETS = {
    'BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'BCH', 'LTC', 'DOGE', 'PEPE', 'SHIB',
    'USDT', 'USDC', 'DAI', 'TUSD', 'FDUSD', 'BUSD', 'USDP', 'GUSD',
    'WBTC', 'WETH', 'STETH', 'CBETH', 'RETH'
}

# -------------------- TOTAL BALANCE & WHATSAPP --------------------
def get_total_balance_str() -> str:
    update_balances()
    total = account_balances.get('USDT', ZERO)
    for asset, qty in account_balances.items():
        if asset == 'USDT' or qty <= ZERO: continue
        sym = asset + 'USDT'
        price = price_cache.get(sym)
        if not price:
            try:
                price = Decimal(client.get_symbol_ticker(symbol=sym)['price'])
            except: continue
        total += qty * price
    return f" | Total: ${total:,.2f}"

def send_whatsapp(message: str):
    global last_buy_alert, last_sell_alert
    if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE: return

    now = time.time()
    is_buy = any(x in message.upper() for x in ["BUY", "GRID BUY"])
    is_sell = any(x in message.upper() for x in ["SELL", "EXIT", "DUMP", "GRID SELL"])

    if (is_buy and now - last_buy_alert < ALERT_COOLDOWN) or \
       (is_sell and now - last_sell_alert < ALERT_COOLDOWN):
        return

    try:
        full_message = f"{message}{get_total_balance_str()}"
        url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(full_message)}&apikey={CALLMEBOT_API_KEY}"
        requests.get(url, timeout=10)
        if is_buy: last_buy_alert = now
        if is_sell: last_sell_alert = now
        terminal_insert(f"[{now_cst()}] WhatsApp → {full_message}")
    except: pass

# -------------------- UTILITIES --------------------
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def terminal_insert(msg):
    try:
        terminal_text.insert(tk.END, msg + "\n")
        terminal_text.see(tk.END)
    except: pass

def floor_to_step(value: Decimal, step_size: Decimal) -> Decimal:
    if step_size <= ZERO: return value
    return (value // step_size) * step_size

def update_balances():
    global account_balances
    try:
        info = client.get_account()
        new_bal = {}
        for a in info['balances']:
            asset = a['asset']
            total = Decimal(a['free']) + Decimal(a['locked'])
            if total > ZERO:
                new_bal[asset] = total
        account_balances = new_bal
    except: pass

def update_fees():
    global maker_fee, taker_fee, last_fee_update
    if time.time() - last_fee_update < FEE_UPDATE_INTERVAL: return
    try:
        info = client.get_account()
        maker_fee = Decimal(info['makerCommission']) / 10000
        taker_fee = Decimal(info['takerCommission']) / 10000
        last_fee_update = time.time()
    except: pass

def get_available_usdt_after_reserve() -> Decimal:
    update_balances()
    update_fees()
    reserved = account_balances.get('USDT', ZERO) * RESERVE_PCT + MIN_USDT_RESERVE
    available = account_balances.get('USDT', ZERO) - reserved
    return max(available, ZERO)

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

# -------------------- TA-LIB INDICATORS ON 500 DAILY KLINES --------------------
def get_daily_ohlcv(symbol, limit=500):
    now = time.time()
    if symbol in daily_klines_cache and symbol in last_kline_update:
        if now - last_kline_update[symbol] < 3600:
            return daily_klines_cache[symbol]

    try:
        klines = client.get_klines(symbol=symbol, interval='1d', limit=limit)
        if len(klines) < 220:
            return None

        o = np.array([float(k[1]) for k in klines], dtype=float)
        h = np.array([float(k[2]) for k in klines], dtype=float)
        l = np.array([float(k[3]) for k in klines], dtype=float)
        c = np.array([float(k[4]) for k in klines], dtype=float)
        v = np.array([float(k[5]) for k in klines], dtype=float)

        rsi = talib.RSI(c, timeperiod=14)
        macd, signal, hist = talib.MACD(c, fastperiod=12, slowperiod=26, signalperiod=9)
        mfi = talib.MFI(h, l, c, v, timeperiod=14)
        sma200 = talib.SMA(c, timeperiod=200)
        atr = talib.ATR(h, l, c, timeperiod=14)
        hammer = talib.CDLHAMMER(o, h, l, c)
        engulfing = talib.CDLENGULFING(o, h, l, c)
        morningstar = talib.CDLMORNINGSTAR(o, h, l, c)

        data = {
            'open': o, 'high': h, 'low': l, 'close': c, 'volume': v,
            'rsi': rsi, 'macd': macd, 'signal': signal, 'hist': hist,
            'mfi': mfi, 'sma200': sma200, 'atr': atr,
            'hammer': hammer, 'engulfing': engulfing, 'morningstar': morningstar
        }
        daily_klines_cache[symbol] = data
        last_kline_update[symbol] = now
        return data
    except Exception as e:
        terminal_insert(f"TA-Lib data error {symbol}: {e}")
        return None

def get_indicators(symbol):
    data = get_daily_ohlcv(symbol)
    if not data:
        return None

    i = -1
    p = -2

    return {
        'price': Decimal(str(data['close'][i])),
        'rsi': Decimal(str(data['rsi'][i])) if not np.isnan(data['rsi'][i]) else Decimal('50'),
        'mfi': Decimal(str(data['mfi'][i])) if not np.isnan(data['mfi'][i]) else Decimal('50'),
        'sma200': Decimal(str(data['sma200'][i])) if not np.isnan(data['sma200'][i]) else ZERO,
        'macd_line': Decimal(str(data['macd'][i])) if not np.isnan(data['macd'][i]) else ZERO,
        'signal_line': Decimal(str(data['signal'][i])) if not np.isnan(data['signal'][i]) else ZERO,
        'histogram': Decimal(str(data['hist'][i])) if not np.isnan(data['hist'][i]) else ZERO,
        'macd_bullish': data['macd'][i] > data['signal'][i] and data['hist'][i] > ZERO,
        'macd_cross_bull': data['macd'][i] > data['signal'][i] and data['macd'][p] <= data['signal'][p],
        'above_sma200': data['close'][i] > data['sma200'][i],
        'volume_spike': data['volume'][i] > np.mean(data['volume'][-20:]) * 2,
        'bullish_pattern': data['hammer'][i] == 100 or data['engulfing'][i] == 100 or data['morningstar'][i] == 100
    }

# Override legacy functions to use TA-Lib
def get_rsi(symbol):
    ind = get_indicators(symbol)
    return ind['rsi'] if ind else Decimal('50')

def get_macd(symbol):
    ind = get_indicators(symbol)
    if not ind: return None, None, None
    return ind['macd_line'], ind['signal_line'], ind['histogram']

def get_mfi(symbol):
    ind = get_indicators(symbol)
    return ind['mfi'] if ind else Decimal('50')

# -------------------- WHALE WALL PROTECTION --------------------
def get_buy_pressure_and_slippage(symbol):
    try:
        book = client.get_order_book(symbol=symbol, limit=50)
        bids = book['bids']
        asks = book['asks']
        bid_vol = sum(Decimal(b[1]) for b in bids)
        ask_vol = sum(Decimal(a[1]) for a in asks)
        total = bid_vol + ask_vol
        buy_pressure = (bid_vol / total) if total > ZERO else Decimal('0.5')
        target_usdt = Decimal('10000')
        accumulated = ZERO
        for price_str, qty_str in bids:
            price = Decimal(price_str)
            qty = Decimal(qty_str)
            cost = price * qty
            if accumulated + cost >= target_usdt:
                slippage_price = price
                break
            accumulated += cost
        else:
            slippage_price = Decimal(bids[-1][0])
        current_price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
        slippage_pct = (current_price - slippage_price) / current_price * 100
        return buy_pressure.quantize(Decimal('0.0001')), slippage_pct.quantize(Decimal('0.01'))
    except:
        return Decimal('0.5'), Decimal('10')

def is_whale_wall_danger(symbol) -> bool:
    now = time.time()
    if symbol in last_whale_check:
        last_time, danger = last_whale_check[symbol]
        if now - last_time < WHALE_CHECK_INTERVAL:
            return danger
    pressure, slippage = get_buy_pressure_and_slippage(symbol)
    danger = (
        pressure < Decimal('0.40') or
        slippage > Decimal('1.2') or
        (pressure < Decimal('0.55') and slippage > Decimal('0.8'))
    )
    last_whale_check[symbol] = (now, danger)
    return danger

# -------------------- TRADING HOURS & EXIT --------------------
def is_trading_allowed():
    if exit_in_progress: return True
    now = datetime.now(CST)
    return TRADING_START_HOUR <= now.hour < TRADING_END_HOUR

def is_evening_exit_time():
    return datetime.now(CST).strftime("%H:%M") >= EVENING_EXIT_START

def is_portfolio_fully_in_usdt():
    update_balances()
    for asset, qty in account_balances.items():
        if asset != 'USDT' and qty > ZERO and asset + 'USDT' in symbol_info:
            return False
    return True

# -------------------- WEBSOCKETS --------------------
def start_user_stream():
    def keep_listenkey_alive():
        while running:
            time.sleep(1800)
            try: client.stream_get_listen_key()
            except: pass

    def run_user_stream():
        while running:
            try:
                key = client.stream_get_listen_key()['listenKey']
                url = f"wss://stream.binance.us:9443/ws/{key}"
                ws = websocket.WebSocketApp(url, on_message=on_user_message)
                ws.run_forever(ping_interval=1800)
            except: time.sleep(5)

    def run_price_stream():
        while running:
            try:
                ws = websocket.WebSocketApp("wss://stream.binance.us:9443/stream?streams=!ticker@arr", on_message=on_user_message)
                ws.run_forever(ping_interval=20)
            except: time.sleep(5)

    threading.Thread(target=keep_listenkey_alive, daemon=True).start()
    threading.Thread(target=run_user_stream, daemon=True).start()
    threading.Thread(target=run_price_stream, daemon=True).start()

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
        elif e == 'executionReport' and data.get('X') in ('FILLED', 'PARTIALLY_FILLED'):
            symbol = data['s']
            side = data['S']
            qty = Decimal(data['q'])
            price = Decimal(data['p'])
            base = symbol.replace('USDT', '')
            terminal_insert(f"[{now_cst()}] FILLED {side} {base} {qty:.6f} @ ${price:.8f}{get_total_balance_str()}")
            send_whatsapp(f"{'BUY' if side=='BUY' else 'SELL'} {base} {qty:.4f} @ ${price:.6f}")
            placing_order_for.discard(symbol)
        elif e == '24hrTicker':
            symbol = data['s']
            if symbol in symbol_info:
                price_cache[symbol] = Decimal(data['c'])
    except: pass

# -------------------- ORDER PLACEMENT --------------------
def place_limit_order(symbol, side, price, qty, is_exit=False):
    global active_orders
    if symbol in placing_order_for: return False
    placing_order_for.add(symbol)
    try:
        info = symbol_info[symbol]
        price_dec = (Decimal(price) // info['tickSize']) * info['tickSize']
        qty_dec = floor_to_step(Decimal(qty), info['stepSize']).quantize(Decimal('0.00000000'), ROUND_DOWN)
        if qty_dec < info['minQty'] or price_dec * qty_dec < info['minNotional']:
            placing_order_for.discard(symbol)
            return False
        update_fees()
        update_balances()
        if side == 'BUY':
            cost = price_dec * qty_dec * (ONE + taker_fee)
            if get_available_usdt_after_reserve() < cost:
                placing_order_for.discard(symbol)
                return False
        else:
            base = symbol.replace('USDT', '')
            if qty_dec > account_balances.get(base, ZERO):
                placing_order_for.discard(symbol)
                return False
        order = client.create_order(symbol=symbol, side=side, type='LIMIT', timeInForce='GTC',
                                    price=str(price_dec), quantity=str(qty_dec))
        base = symbol.replace('USDT', '')
        msg = f"GRID {side} {base} {qty_dec} @ ${price_dec:.8f}"
        terminal_insert(f"[{now_cst()}] SUCCESS {msg}")
        send_whatsapp(f"∞ {msg}")
        if not exit_in_progress:
            active_grid_orders.setdefault(symbol, []).append(order['orderId'])
        active_orders += 1
        return True
    except Exception as e:
        terminal_insert(f"Order error {symbol}: {e}")
        placing_order_for.discard(symbol)
        return False

def cancel_symbol_orders(symbol):
    global active_orders
    try:
        for o in client.get_open_orders(symbol=symbol):
            client.cancel_order(symbol=symbol, orderId=o['orderId'])
            active_orders -= 1
        if symbol in active_grid_orders:
            active_grid_orders[symbol] = []
    except: pass


## -------------------- MOMENTUM BUY LIST --------------------
# Large proven high-liquidity fallback list (activated only when <25 dynamic coins)
FALLBACK_BUY_LIST = [
    'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT', 'MATICUSDT', 'UNIUSDT', 'AAVEUSDT',
    'NEARUSDT', 'INJUSDT', 'APTUSDT', 'SUIUSDT', 'OPUSDT', 'ARBUSDT', 'HBARUSDT',
    'VETUSDT', 'ATOMUSDT', 'ALGOUSDT', 'FILUSDT', 'ICPUSDT', 'RUNEUSDT', 'GRTUSDT',
    'MANAUSDT', 'SANDUSDT', 'AXSUSDT', 'EGLDUSDT', 'FTMUSDT', 'THETAUSDT', 'XTZUSDT',
    'CHZUSDT', 'APEUSDT', 'GALAUSDT', 'CFXUSDT', 'RNDRUSDT', 'STXUSDT', 'FLOWUSDT',
    'IMXUSDT', 'MINAUSDT', 'KAVAUSDT', 'ROSEUSDT', 'LRCUSDT', 'ZILUSDT', 'ENJUSDT',
    'BATUSDT', '1INCHUSDT', 'SNXUSDT', 'COMPUSDT', 'YFIUSDT', 'MKRUSDT', 'CRVUSDT',
    'BALUSDT', 'SUSHIUSDT', 'KSMUSDT', 'WAVESUSDT', 'DASHUSDT', 'ZECUSDT', 'ONTUSDT',
    'IOSTUSDT', 'REEFUSDT', 'TRBUSDT', 'EOSUSDT', 'QTUMUSDT', 'ICXUSDT', 'BTGUSDT',
    'ZENUSDT', 'ONEUSDT', 'KLAYUSDT', 'HNTUSDT', 'ARUSDT', 'CELOUSDT', 'ANKRUSDT',
    'SKLUSDT', 'DENTUSDT', 'CVCUSDT', 'HOTUSDT', 'BLZUSDT', 'OGNUSDT', 'FLUXUSDT',
    'AUDIOUSDT', 'BANDUSDT', 'CELRUSDT', 'CHRUSDT', 'COTIUSDT', 'DOCKUSDT', 'LINAUSDT'
]

def generate_buy_list():
    global buy_list, last_buy_list_update
    if exit_in_progress or not is_trading_allowed(): 
        return
    if time.time() - last_buy_list_update < 1800:
        return

    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            'vs_currency': 'usd',
            'order': 'market_cap_desc',
            'per_page': 250,
            'page': 1,
            'price_change_percentage': '24h,7d'
        }
        coins = requests.get(url, params=params, timeout=25).json()

        candidates = []
        for coin in coins:
            base = coin['symbol'].upper()
            if base in BLACKLISTED_BASE_ASSETS: 
                continue
            sym = base + 'USDT'
            if sym not in symbol_info: 
                continue

            raw_volume = coin.get('total_volume') or 0
            raw_change_24h = coin.get('price_change_percentage_24h') or 0
            raw_market_cap = coin.get('market_cap') or 0

            volume_24h   = Decimal(str(raw_volume))
            change_24h   = Decimal(str(raw_change_24h))
            market_cap   = Decimal(str(raw_market_cap))

            if volume_24h < Decimal('40000000'): 
                continue
            if change_24h < Decimal('3'):  
                continue
            if market_cap < Decimal('500000000'): 
                continue

            score = float(volume_24h / Decimal('1000000')) * (1 + float(change_24h) / 100)
            pretty_name = f"{base} +{change_24h:.2f}% ${volume_24h/Decimal('1000000'):.0f}M vol"
            candidates.append((sym, score, pretty_name))

        candidates.sort(key=lambda x: -x[1])
        dynamic_list = [x[0] for x in candidates[:80]]

        # ←←← NEW SAFETY LOGIC ←←←
        if len(dynamic_list) < 25:
            terminal_insert(f"[{now_cst()}] ⚠️ Only {len(dynamic_list)} dynamic coins found — activating LARGE FALLBACK LIST ({len(FALLBACK_BUY_LIST)} coins)")
            send_whatsapp(f"⚠️ Low momentum — switched to safe fallback list ({len(FALLBACK_BUY_LIST)} coins)")
            buy_list = FALLBACK_BUY_LIST[:]
        else:
            buy_list = dynamic_list

        # ←←← END NEW LOGIC ←←←
        
        last_buy_list_update = time.time()
        names = [x[2].split()[0] for x in candidates[:15]]
        source = "FALLBACK" if len(dynamic_list) < 25 else "CoinGecko"
        terminal_insert(f"[{now_cst()}] 🚀 Buy list refreshed ({source}) — {len(buy_list)} coins: {', '.join(names[:15])}")
        send_whatsapp(f"🚀 New rocket list ({source}, {len(buy_list)} coins): {', '.join(names[:15])}")

    except Exception as e:
        terminal_insert(f"Buy list error — using SAFE FALLBACK list: {e}")
        send_whatsapp("🚨 CoinGecko failed — activated safe fallback list")
        buy_list = FALLBACK_BUY_LIST[:]
        last_buy_list_update = time.time()

# -------------------- GRID USING TA-LIB --------------------
def place_platinum_grid(symbol):
    if exit_in_progress or not is_trading_allowed(): return
    if is_whale_wall_danger(symbol):
        terminal_insert(f"[{now_cst()}] 🐳 Whale wall detected on {symbol} — skipping grid")
        return

    ind = get_indicators(symbol)
    if not ind: return

    price = ind['price']
    info = symbol_info[symbol]
    grid_pct = Decimal('0.012')
    cash = BASE_CASH_PER_LEVEL * Decimal('1.5')

    rsi_ok = Decimal('60') <= ind['rsi'] <= Decimal('74')
    mfi_ok = Decimal('50') < ind['mfi'] < Decimal('82')
    macd_ok = ind['macd_bullish']
    pressure, _ = get_buy_pressure_and_slippage(symbol)
    momentum_ok = ind['above_sma200'] and ind['volume_spike']
    pattern_ok = ind['macd_cross_bull'] or ind['bullish_pattern']

    if not (rsi_ok and mfi_ok and macd_ok and pressure > Decimal('0.58') and momentum_ok and pattern_ok):
        return

    for i in range(1, 9):
        buy_price = price * ((ONE - grid_pct) ** i)
        buy_price = (buy_price // info['tickSize']) * info['tickSize']
        qty = cash * (GOLDEN_RATIO ** (i-1)) / buy_price
        qty = floor_to_step(qty, info['stepSize']).quantize(Decimal('0.00000000'))
        required = buy_price * qty * (ONE + taker_fee)
        if get_available_usdt_after_reserve() >= required:
            place_limit_order(symbol, 'BUY', buy_price, qty)

    owned = account_balances.get(symbol.replace('USDT', ''), ZERO)
    for i in range(1, 9):
        sell_price = price * ((ONE + grid_pct) ** i)
        sell_price = (sell_price // info['tickSize']) * info['tickSize']
        qty = cash * (SELL_GROWTH_OPTIMAL ** (i-1)) / sell_price
        qty = floor_to_step(qty, info['stepSize']).quantize(Decimal('0.00000000'))
        if qty <= owned:
            place_limit_order(symbol, 'SELL', sell_price, qty)
            owned -= qty

def regrid_symbol(symbol):
    if exit_in_progress: return
    cancel_symbol_orders(symbol)
    place_platinum_grid(symbol)

def dynamic_rebalance():
    if exit_in_progress or not is_trading_allowed(): 
        return

    try:
        update_balances()
        total_portfolio = Decimal('0')
        for asset, qty in account_balances.items():
            if asset == 'USDT':
                total_portfolio += qty
                continue
            sym = asset + 'USDT'
            if sym not in symbol_info: 
                continue
            price = price_cache.get(sym) or Decimal(client.get_symbol_ticker(symbol=sym)['price'])
            total_portfolio += qty * price
        if total_portfolio <= ZERO: 
            return

        terminal_insert(f"[{now_cst()}] Rebalance — Portfolio ${total_portfolio:,.0f}")

        for asset in list(account_balances.keys()):
            if asset == 'USDT': 
                continue
            sym = asset + 'USDT'
            if sym not in symbol_info: 
                continue

            ind = get_indicators(sym)
            if not ind: continue

            current_price = price_cache.get(sym) or Decimal(client.get_symbol_ticker(symbol=sym)['price'])
            current_qty = account_balances.get(asset, ZERO)
            current_value = current_qty * current_price

            pressure, _ = get_buy_pressure_and_slippage(sym)

            if (pressure >= Decimal('0.70') and ind['rsi'] < Decimal('75') and 
                ind['mfi'] > Decimal('62') and ind['macd_bullish'] and ind['above_sma200']):
                target_pct = Decimal('0.20')
            elif pressure >= Decimal('0.60') and ind['rsi'] < Decimal('72') and ind['macd_bullish']:
                target_pct = Decimal('0.12')
            elif pressure >= Decimal('0.50') and ind['macd_bullish']:
                target_pct = Decimal('0.06')
            else:
                target_pct = Decimal('0.02')

            target_value = total_portfolio * target_pct

            if current_value > target_value * Decimal('1.12'):
                sell_qty = floor_to_step((current_value - target_value) / current_price, symbol_info[sym]['stepSize'])
                if sell_qty >= symbol_info[sym]['minQty']:
                    place_limit_order(sym, 'SELL', current_price * Decimal('0.999'), sell_qty)

            elif current_value < target_value * Decimal('0.88'):
                buy_qty = floor_to_step((target_value - current_value) / current_price, symbol_info[sym]['stepSize'])
                required = current_price * buy_qty * (ONE + taker_fee)
                if get_available_usdt_after_reserve() >= required and buy_qty >= symbol_info[sym]['minQty']:
                    place_limit_order(sym, 'BUY', current_price * Decimal('1.001'), buy_qty)

    except Exception as e:
        terminal_insert(f"Rebalance error (ignored): {e}")

# -------------------- EVENING EXIT & SCANNER --------------------
def aggressive_evening_exit():
    global exit_in_progress, last_exit_attempt

    if not is_evening_exit_time() and not exit_in_progress: return

    if is_portfolio_fully_in_usdt():
        if exit_in_progress:
            terminal_insert(f"[{now_cst()}] FULL EXIT COMPLETE — 100% USDT — NIGHT MODE")
            send_whatsapp("EXIT COMPLETE — 100% USDT — Safe until 3 AM")
            exit_in_progress = False
        return

    if time.time() - last_exit_attempt < EXIT_RETRY_INTERVAL: return
    last_exit_attempt = time.time()

    if not exit_in_progress:
        exit_in_progress = True
        send_whatsapp("EVENING EXIT STARTED — Dumping all assets")

    update_balances()
    for asset, qty in list(account_balances.items()):
        if asset == 'USDT' or qty <= ZERO: continue
        sym = asset + 'USDT'
        if sym not in symbol_info: continue
        try:
            price = Decimal(client.get_symbol_ticker(symbol=sym)['price'])
            bid = Decimal(client.get_order_book(symbol=sym, limit=5)['bids'][0][0])
            sell_price = (bid * Decimal('0.998')).quantize(symbol_info[sym]['tickSize'], ROUND_DOWN)
            sell_qty = floor_to_step(qty, symbol_info[sym]['stepSize']).quantize(Decimal('0.00000000'))
            if sell_qty >= symbol_info[sym]['minQty']:
                place_limit_order(sym, 'SELL', sell_price, sell_qty, is_exit=True)
        except: pass

def scan_and_grid_healthy_coins():
    if not buy_list:
        return

    candidates = buy_list[:]
    random.shuffle(candidates)

    added = 0
    for sym in candidates:
        if added >= 12:
            break
        if sym in placing_order_for:
            continue
        if sym in active_grid_orders and active_grid_orders[sym]:
            continue

        if is_whale_wall_danger(sym):
            continue

        ind = get_indicators(sym)
        if not ind: continue

        pressure, _ = get_buy_pressure_and_slippage(sym)

        if (Decimal('60') <= ind['rsi'] <= Decimal('74') and
            Decimal('50') < ind['mfi'] < Decimal('82') and
            ind['macd_bullish'] and
            pressure > Decimal('0.58') and
            ind['above_sma200'] and ind['volume_spike'] and
            (ind['macd_cross_bull'] or ind['bullish_pattern'])):

            terminal_insert(f"[{now_cst()}] 🚀 Healthy rocket found → placing grid on {sym}")
            regrid_symbol(sym)
            added += 1

    if added == 0:
        terminal_insert(f"[{now_cst()}] No healthy coins right now — waiting for whale walls to clear...")

# -------------------- MAIN LOOP --------------------
def main_loop():
    if not running: return

    aggressive_evening_exit()

    if not exit_in_progress and is_trading_allowed():
        generate_buy_list()
        scan_and_grid_healthy_coins()
        dynamic_rebalance()

    root.after(15000, main_loop)

# -------------------- GUI --------------------
root = tk.Tk()
root.title("INFINITY GRID PLATINUM 2025 — FINAL PERFECTION + TA-LIB")
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
    if running: return
    running = True
    status_label.config(text="Status: RUNNING", fg="#00ff00")
    terminal_insert(f"[{now_cst()}] BOT STARTED — FINAL PERFECTION + TA-LIB ENGAGED")
    send_whatsapp("INFINITY GRID PLATINUM 2025 — FINAL PERFECTION STARTED")
    root.after(100, main_loop)

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

# -------------------- STARTUP --------------------
if __name__ == "__main__":
    load_symbol_info()
    update_fees()
    start_user_stream()
    generate_buy_list()
    terminal_insert(f"[{now_cst()}] INFINITY GRID PLATINUM 2025 — FINAL PERFECTION + TA-LIB LOADED")
    update_gui()
    root.mainloop()
