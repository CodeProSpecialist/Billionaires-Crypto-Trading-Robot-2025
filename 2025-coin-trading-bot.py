#!/usr/bin/env python3
"""
Binance.US Dynamic Trailing Bot – WEBSOCKET + FULL PRO DASHBOARD
- Flash Dip → Market Buy
- 15-Min Stall → Market Sell
- WebSocket-Driven (NO REST polling)
- Binance.US ONLY (tld='us')
"""

import os
import time
import logging
import signal
import sys
import numpy as np
import threading
import json
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance import ThreadedWebsocketManager
from binance.exceptions import BinanceAPIException
import talib
from datetime import datetime
import pytz
import requests
from decimal import Decimal, ROUND_DOWN
from typing import Dict
from collections import deque, defaultdict
import queue

# === CONFIGURATION (BINANCE.US) =============================================
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')
MAX_PRICE = 1000.0
MIN_PRICE = 0.01
MIN_24H_VOLUME_USDT = 100000
LOG_FILE = "crypto_trading_bot.log"
RSI_PERIOD = 14
BB_PERIOD = 20
BB_DEV = 2
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
PROFIT_TARGET_NET = Decimal('0.008')  # 0.8%
RISK_PER_TRADE = Decimal('0.10')
MIN_BALANCE = 2.0

# Strategy
ORDERBOOK_SELL_PRESSURE_THRESHOLD = 0.60
ORDERBOOK_BUY_PRESSURE_SPIKE = 0.65
ORDERBOOK_BUY_PRESSURE_DROP = 0.55
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
STALL_THRESHOLD_SECONDS = 15 * 60
RAPID_DROP_THRESHOLD = 0.01
RAPID_DROP_WINDOW = 5.0

# BINANCE.US API
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

# WebSocket
MAX_KLINE_SYMBOLS = 30
KLINE_UPDATE_INTERVAL = 60

# Dashboard
DASHBOARD_REFRESH_INTERVAL = 20
CRASH_RESTART_DELAY = 4 * 60 + 30

# === CONSTANTS ==============================================================
ZERO = Decimal('0')
CST_TZ = pytz.timezone('America/Chicago')

# === LOGGING ================================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

# === GLOBAL STATE ===========================================================
price_cache: Dict[str, float] = {}
book_cache: Dict[str, Dict] = {}
klines_cache: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
rsi_cache: Dict[str, float] = {}
low_24h_cache: Dict[str, float] = {}
last_price_update: Dict[str, float] = {}
buy_pressure_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=5))
sell_pressure_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=5))

positions: Dict[str, dict] = {}
dynamic_buy_active: set = set()
dynamic_sell_active: set = set()
twm = None

api_queue = queue.Queue()
api_worker_thread = None
dashboard_lock = threading.Lock()

# === SQLALCHEMY =============================================================
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError

DB_URL = "sqlite:///binance_us_trades.db"
engine = create_engine(DB_URL, echo=False, future=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20, 8), nullable=False)
    avg_entry_price = Column(Numeric(20, 8), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())

Base.metadata.create_all(engine)

class DBManager:
    def __enter__(self):
        self.session = SessionFactory()
        return self.session
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.session.rollback()
        else:
            try: self.session.commit()
            except SQLAlchemyError: self.session.rollback()
        self.session.close()

# === SIGNAL HANDLER =========================================================
def signal_handler(signum, frame):
    logger.info("Shutting down gracefully...")
    if twm: twm.stop()
    sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# === API WORKER (BINANCE.US) ================================================
def api_worker():
    client = Client(API_KEY, API_SECRET, tld='us')  # BINANCE.US
    while True:
        try:
            func, args, kwargs, future = api_queue.get()
            result = func(client, *args, **kwargs)
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
        finally:
            time.sleep(3)
        api_queue.task_done()

def rate_limited_api_call(func, *args, **kwargs):
    future = threading.Future()
    api_queue.put((func, args, kwargs, future))
    return future.result()

# === WEBSOCKETS (BINANCE.US) ================================================
def start_websockets():
    global twm
    twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET, tld='us')  # BINANCE.US
    twm.start()

    twm.start_multiplex_socket(
        callback=on_multiplex_message,
        streams=['!miniTicker@arr', '!bookTicker']
    )

    for sym in top_symbols:
        twm.start_kline_socket(callback=on_kline, symbol=sym, interval='1m')

    twm.start_user_socket(callback=handle_user_data)

def on_multiplex_message(msg):
    if 'stream' not in msg or 'data' not in msg: return
    stream = msg['stream']
    data = msg['data']

    if stream == '!miniTicker@arr':
        for t in data:
            symbol = t['s']
            price_cache[symbol] = float(t['c'])
            last_price_update[symbol] = time.time()
            low_24h_cache[symbol] = min(low_24h_cache.get(symbol, float(t['c'])), float(t['l']))

    elif stream == '!bookTicker':
        symbol = data['s']
        bid = Decimal(data['b'])
        ask = Decimal(data['a'])
        total = bid + ask
        pct_bid = float(bid / total * 100) if total > 0 else 50.0
        pct_ask = float(ask / total * 100) if total > 0 else 50.0

        book_cache[symbol] = {
            'best_bid': bid, 'best_ask': ask,
            'pct_bid': pct_bid, 'pct_ask': pct_ask,
            'ts': time.time()
        }
        buy_pressure_history[symbol].append(pct_bid)
        sell_pressure_history[symbol].append(pct_ask)

def on_kline(msg):
    k = msg['k']
    if not k['x']: return
    symbol = msg['s']
    close = float(k['c'])
    klines_cache[symbol].append(close)

    if len(klines_cache[symbol]) >= RSI_PERIOD and int(time.time()) % KLINE_UPDATE_INTERVAL == 0:
        closes = np.array(list(klines_cache[symbol]))
        rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)[-1]
        if np.isfinite(rsi):
            rsi_cache[symbol] = float(rsi)

def handle_user_data(msg):
    if msg.get('e') == 'executionReport':
        order = msg
        symbol = order['s']
        side = order['S']
        status = order['X']
        qty = Decimal(order['q'])
        price = Decimal(order['L']) if order['L'] else ZERO

        if status == 'FILLED':
            if side == 'BUY':
                record_buy(symbol, float(price), float(qty))
                send_whatsapp(f"FILLED BUY {symbol} @ {price:.6f}")
                dynamic_buy_active.discard(symbol)
            elif side == 'SELL':
                record_sell(symbol, float(price), float(qty))
                send_whatsapp(f"FILLED SELL {symbol} @ {price:.6f}")
                dynamic_sell_active.discard(symbol)

# === UTILS ==================================================================
def to_decimal(v): return Decimal(str(v)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
def now_cst(): return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
def send_whatsapp(msg):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}"
            requests.get(url, timeout=5)
        except: pass

def get_balance():
    try:
        acc = rate_limited_api_call(lambda c: c.get_account())
        for b in acc['balances']:
            if b['asset'] == 'USDT':
                return float(b['free'])
        return 0.0
    except: return 0.0

# === STRATEGY LOGIC =========================================================
def check_buy_signal(symbol):
    if (symbol in dynamic_buy_active or symbol in positions or
        time.time() - buy_cooldown.get(symbol, 0) < 15 * 60):
        return

    book = book_cache.get(symbol, {})
    if not book or book['best_bid'] <= 0: return
    bid = book['best_bid']
    rsi = rsi_cache.get(symbol)
    low_24h = low_24h_cache.get(symbol)

    if (rsi is not None and rsi <= RSI_OVERSOLD and
        book['pct_ask'] >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
        low_24h and bid <= Decimal(str(low_24h)) * Decimal('1.01')):
        start_dynamic_buy(symbol, bid)

def check_sell_signal(symbol):
    if symbol not in positions or symbol in dynamic_sell_active: return
    pos = positions[symbol]
    entry = Decimal(str(pos['entry_price']))
    qty = Decimal(str(pos['qty']))
    book = book_cache.get(symbol, {})
    if not book: return
    ask = book['best_ask']
    rsi = rsi_cache.get(symbol)

    net_return = (ask - entry) / entry - Decimal('0.002')
    if net_return >= PROFIT_TARGET_NET and rsi is not None and rsi >= RSI_OVERBOUGHT:
        history = buy_pressure_history[symbol]
        if len(history) >= 3:
            peak = max(history)
            if peak >= ORDERBOOK_BUY_PRESSURE_SPIKE * 100 and history[-1] <= ORDERBOOK_BUY_PRESSURE_DROP * 100:
                start_dynamic_sell(symbol, entry, qty)

# === DYNAMIC BUY/SELL =======================================================
buy_cooldown: Dict[str, float] = {}

def start_dynamic_buy(symbol, trigger_price):
    if symbol in dynamic_buy_active: return
    dynamic_buy_active.add(symbol)
    threading.Thread(target=run_dynamic_buy, args=(symbol, trigger_price), daemon=True).start()

def run_dynamic_buy(symbol, trigger_price):
    lowest = trigger_price
    force_market = False

    while symbol in dynamic_buy_active:
        book = book_cache.get(symbol, {})
        if not book: time.sleep(0.5); continue
        price = book['best_bid']

        now = time.time()
        if symbol in last_price_update and now - last_price_update[symbol] < RAPID_DROP_WINDOW:
            drop = (price_cache.get(symbol, price) - price) / price
            if drop >= RAPID_DROP_THRESHOLD:
                force_market = True
                break

        if price < lowest: lowest = price
        if price > lowest * Decimal('1.003'): break

        history = sell_pressure_history[symbol]
        if len(history) >= 3:
            peak = max(history)
            if peak >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and history[-1] <= peak * 0.9:
                break

        time.sleep(0.5)

    try:
        balance = get_balance()
        if balance <= MIN_BALANCE: return
        alloc = min(Decimal(str(balance - MIN_BALANCE)) * RISK_PER_TRADE, Decimal(str(balance - MIN_BALANCE)))

        if force_market:
            order = rate_limited_api_call(lambda c: c.order_market_buy(symbol=symbol, quoteOrderQty=float(alloc)))
            fill_price = to_decimal(order['fills'][0]['price'])
            send_whatsapp(f"FLASH DIP BUY {symbol} @ {fill_price:.6f}")
        else:
            qty = alloc / price
            order = rate_limited_api_call(lambda c: c.order_limit_buy(symbol=symbol, quantity=float(qty), price=str(price)))
            send_whatsapp(f"LIMIT BUY {symbol} @ {price:.6f}")
        buy_cooldown[symbol] = time.time()
    except Exception as e:
        logger.error(f"Buy failed: {e}")
    finally:
        dynamic_buy_active.discard(symbol)

def start_dynamic_sell(symbol, entry, qty):
    if symbol in dynamic_sell_active: return
    dynamic_sell_active.add(symbol)
    threading.Thread(target=run_dynamic_sell, args=(symbol, entry, qty), daemon=True).start()

def run_dynamic_sell(symbol, entry, qty):
    peak = entry
    while symbol in dynamic_sell_active:
        book = book_cache.get(symbol, {})
        if not book: time.sleep(0.5); continue
        price = book['best_bid']
        if price > peak: peak = price

        if price >= entry * Decimal('1.005'):
            stall_timer[symbol] = time.time()
        if symbol in stall_timer and time.time() - stall_timer[symbol] >= STALL_THRESHOLD_SECONDS:
            execute_market_sell(symbol, qty)
            break

        if price < peak * Decimal('0.995'):
            execute_limit_sell(symbol, price, qty)
            break

        time.sleep(0.5)

    dynamic_sell_active.discard(symbol)

stall_timer: Dict[str, float] = {}

def execute_market_sell(symbol, qty):
    try:
        order = rate_limited_api_call(lambda c: c.order_market_sell(symbol=symbol, quantity=float(qty)))
        fill_price = to_decimal(order['fills'][0]['price'])
        send_whatsapp(f"STALL SELL {symbol} @ {fill_price:.6f}")
    except Exception as e:
        logger.error(f"Market sell failed: {e}")

def execute_limit_sell(symbol, price, qty):
    try:
        order = rate_limited_api_call(lambda c: c.order_limit_sell(symbol=symbol, quantity=float(qty), price=str(price)))
        send_whatsapp(f"LIMIT SELL {symbol} @ {price:.6f}")
    except Exception as e:
        logger.error(f"Limit sell failed: {e}")

# === DB HELPERS =============================================================
def record_buy(symbol, price, qty):
    with DBManager() as sess:
        pos = sess.query(Position).filter_by(symbol=symbol).first()
        if not pos:
            sess.add(Position(symbol=symbol, quantity=to_decimal(qty), avg_entry_price=to_decimal(price)))
        else:
            total = pos.quantity * pos.avg_entry_price + to_decimal(qty) * to_decimal(price)
            pos.quantity += to_decimal(qty)
            pos.avg_entry_price = total / pos.quantity
        positions[symbol] = {'entry_price': price, 'qty': qty}

def record_sell(symbol, price, qty):
    with DBManager() as sess:
        pos = sess.query(Position).filter_by(symbol=symbol).first()
        if pos:
            pos.quantity -= to_decimal(qty)
            if pos.quantity <= 0:
                sess.delete(pos)
                positions.pop(symbol, None)

# === DASHBOARD ==============================================================
def set_terminal_background_and_title():
    try:
        print("\033]0;BINANCE.US BOT – LIVE\007", end='')
        print("\033[48;5;17m", end='')
        print("\033[2J\033[H", end='')
    except: pass

def print_professional_dashboard():
    try:
        with dashboard_lock:
            set_terminal_background_and_title()
            os.system('cls' if os.name == 'nt' else 'clear')
            now = now_cst()
            usdt_free = get_balance()
            total_portfolio, _ = calculate_total_portfolio_value()

            NAVY = "\033[48;5;17m"
            YELLOW = "\033[38;5;226m"
            GREEN = "\033[38;5;82m"
            RED = "\033[38;5;196m"
            RESET = "\033[0m"
            BOLD = "\033[1m"

            print(f"{NAVY}{'='*120}{RESET}")
            print(f"{NAVY}{YELLOW}{'BINANCE.US TRADING BOT – LIVE':^120}{RESET}")
            print(f"{NAVY}{'='*120}{RESET}\n")

            print(f"{NAVY}{YELLOW}{'Time (CST)':<20} {now}{RESET}")
            print(f"{NAVY}{YELLOW}{'Available USDT':<20} ${usdt_free:,.6f}{RESET}")
            print(f"{NAVY}{YELLOW}{'Portfolio Value':<20} ${total_portfolio:,.6f}{RESET}")
            print(f"{NAVY}{YELLOW}{'Active Threads':<20} {len(dynamic_buy_active) + len(dynamic_sell_active)}{RESET}")
            print(f"{NAVY}{YELLOW}{'Trailing Buys':<20} {len(dynamic_buy_active)}{RESET}")
            print(f"{NAVY}{YELLOW}{'Trailing Sells':<20} {len(dynamic_sell_active)}{RESET}")
            print(f"{NAVY}{YELLOW}{'Rate Limit':<20} 1 call / 3 sec{RESET}")
            print(f"{NAVY}{YELLOW}{'-'*120}{RESET}\n")

            with DBManager() as sess:
                db_positions = sess.query(Position).all()

            if db_positions:
                print(f"{NAVY}{BOLD}{YELLOW}{'POSITIONS (BINANCE.US)':^120}{RESET}")
                print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
                print(f"{NAVY}{YELLOW}{'SYMBOL':<10} {'QTY':>12} {'ENTRY':>12} {'CURRENT':>12} {'RSI':>6} {'P&L%':>8} {'PROFIT':>10} {'STATUS':<25}{RESET}")
                print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
                total_pnl = ZERO
                for pos in db_positions:
                    symbol = pos.symbol
                    qty = float(pos.quantity)
                    entry = float(pos.avg_entry_price)
                    ob = book_cache.get(symbol, {})
                    cur_price = float(ob.get('best_bid') or ob.get('best_ask', 0))
                    rsi = rsi_cache.get(symbol)
                    rsi_str = f"{rsi:5.1f}" if rsi else "N/A"

                    gross = (cur_price - entry) * qty
                    fee_cost = 0.002 * cur_price * qty
                    net_profit = gross - fee_cost
                    pnl_pct = ((cur_price - entry) / entry - 0.002) * 100
                    total_pnl += Decimal(str(net_profit))

                    status = ("Trailing Sell Active" if symbol in dynamic_sell_active
                              else "Trailing Buy Active" if symbol in dynamic_buy_active
                              else "24/7 Monitoring")
                    color = GREEN if net_profit > 0 else RED
                    print(f"{NAVY}{YELLOW}{symbol:<10} {qty:>12.6f} {entry:>12.6f} {cur_price:>12.6f} {rsi_str} {color}{pnl_pct:>7.2f}%{RESET}{NAVY}{YELLOW} {color}{net_profit:>10.2f}{RESET}{NAVY}{YELLOW} {status:<25}{RESET}")

                print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
                pnl_color = GREEN if total_pnl > 0 else RED
                print(f"{NAVY}{YELLOW}{'TOTAL UNREALIZED P&L':<50} {pnl_color}${float(total_pnl):>12,.2f}{RESET}\n")
            else:
                print(f"{NAVY}{YELLOW} No active positions.{RESET}\n")

            print(f"{NAVY}{BOLD}{YELLOW}{'MARKET UNIVERSE (BINANCE.US)':^120}{RESET}")
            print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
            print(f"{NAVY}{YELLOW}{'VALID SYMBOLS':<20} {len(price_cache)}{RESET}")
            print(f"{NAVY}{YELLOW}{'PRICE RANGE':<20} ${MIN_PRICE} → ${MAX_PRICE}{RESET}")
            print(f"{NAVY}{YELLOW}{'-'*120}{RESET}\n")

            # ... (BUY/SELL WATCHLISTS same as before) ...

            print(f"{NAVY}{'='*120}{RESET}\n")

    except Exception as e:
        logger.error(f"Dashboard error: {e}")

def calculate_total_portfolio_value():
    try:
        total = ZERO
        for symbol, pos in positions.items():
            qty = to_decimal(pos['qty'])
            price = to_decimal(book_cache.get(symbol, {}).get('best_bid', 0))
            if price > 0:
                total += qty * price
        return float(total + Decimal(str(get_balance()))), {}
    except:
        return 0.0, {}

# === MAIN ===================================================================
top_symbols = []

def main():
    global twm, top_symbols

    if not API_KEY or not API_SECRET:
        logger.critical("API keys missing")
        return

    api_worker_thread = threading.Thread(target=api_worker, daemon=True)
    api_worker_thread.start()

    client = Client(API_KEY, API_SECRET, tld='us')  # BINANCE.US
    info = client.get_exchange_info()
    symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    tickers = client.get_ticker()
    valid = [(t['symbol'], float(t['quoteVolume'])) for t in tickers
             if t['symbol'] in symbols and float(t['quoteVolume']) >= MIN_24H_VOLUME_USDT]
    top_symbols = [s[0] for s in sorted(valid, key=lambda x: x[1], reverse=True)[:MAX_KLINE_SYMBOLS]]

    start_websockets()

    with DBManager() as sess:
        for p in sess.query(Position).all():
            positions[p.symbol] = {'entry_price': float(p.avg_entry_price), 'qty': float(p.quantity)}

    logger.info(f"Binance.US WebSocket bot started with {len(top_symbols)} symbols")
    last_dash = 0
    while True:
        try:
            now = time.time()
            for sym in top_symbols:
                if sym in price_cache:
                    check_buy_signal(sym)
                    check_sell_signal(sym)

            if now - last_dash >= DASHBOARD_REFRESH_INTERVAL:
                print_professional_dashboard()
                last_dash = now

            time.sleep(0.5)
        except Exception as e:
            logger.critical(f"Main loop error: {e}", exc_info=True)
            time.sleep(10)

if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            logger.critical(f"Bot crashed: {e}", exc_info=True)
            time.sleep(CRASH_RESTART_DELAY)
