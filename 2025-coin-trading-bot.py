#!/usr/bin/env python3
"""
Websockets + REST Hybrid Bot for Binance.US – v7 STABLE
- REST: Symbols, Positions, Orders
- WebSocket: Price, Order Book, RSI
- LIVE TRADING
- NO RESTART SPAM
- Central Time (CST/CDT)
"""

import os
import time
import logging
import signal
import sys
import json
import threading
import websocket
import requests
import numpy as np
import talib
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException
from datetime import datetime
import pytz
from decimal import Decimal, ROUND_DOWN
from collections import deque, defaultdict
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, func
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import SQLAlchemyError

# ============================= CONFIG =============================
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE   = os.getenv('CALLMEBOT_PHONE')
LOG_FILE = "binance_us_stable_bot.log"

MIN_PRICE = Decimal('0.01')
MAX_PRICE = Decimal('1000.0')
MIN_24H_VOLUME_USDT = 100_000

RSI_PERIOD = 14
PROFIT_TARGET_NET = Decimal('0.008')
RISK_PER_TRADE    = Decimal('0.10')
MIN_BALANCE       = Decimal('2.0')
ORDERBOOK_SELL_PRESSURE_THRESHOLD = Decimal('0.60')
ORDERBOOK_BUY_PRESSURE_SPIKE      = Decimal('0.65')
ORDERBOOK_BUY_PRESSURE_DROP       = Decimal('0.55')
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
BUY_COOLDOWN_SECONDS = 15 * 60

API_KEY    = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
if not API_KEY or not API_SECRET:
    raise SystemExit("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET")

MAX_KLINE_SYMBOLS = 30
WS_BASE = "wss://stream.binance.us:9443"
SUBSCRIBE_DELAY = 0.25
DASHBOARD_REFRESH = 30
SYMBOL_CACHE_FILE = "symbol_cache_v7.json"
SYMBOL_CACHE_TTL = 60 * 60

# ============================= LOGGING =============================
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    fh = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=7)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)

# ============================= TIMEZONE =============================
CST = pytz.timezone('America/Chicago')

# ============================= GLOBALS =============================
price_cache = {}
book_cache  = {}
klines_cache = defaultdict(lambda: deque(maxlen=100))
rsi_cache   = {}
low_24h_cache = {}
last_price_update = {}
buy_pressure_hist = defaultdict(lambda: deque(maxlen=5))
sell_pressure_hist = defaultdict(lambda: deque(maxlen=5))
buy_cooldown = {}
positions = {}
dyn_buy_active = set()
dyn_sell_active = set()

ws_market = None
top_symbols = []

client = Client(API_KEY, API_SECRET, tld='us')

_balance_cache = {'value': Decimal('0'), 'ts': 0}
_balance_lock = threading.Lock()
_step_size_cache = {}
_tick_size_cache = {}

# ============================= SQLALCHEMY =============================
DB_URL = "sqlite:///binance_us_v7.db"
engine = create_engine(DB_URL, echo=False, future=True)
Base = declarative_base()
Session = sessionmaker(bind=engine, expire_on_commit=False)

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20, 8), nullable=False)
    avg_entry_price = Column(Numeric(20, 8), nullable=False)
    buy_fee_rate = Column(Numeric(10, 6), nullable=False, default=0.001)

Base.metadata.create_all(engine)

class DB:
    def __enter__(self): self.s = Session(); return self.s
    def __exit__(self, t, v, tb):
        if t: self.s.rollback()
        else:
            try: self.s.commit()
            except SQLAlchemyError: self.s.rollback()
        self.s.close()

# ============================= SIGNAL =============================
def shutdown(*_):
    logger.info("Shutting down...")
    stop_ws()
    sys.exit(0)
signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ============================= UTILS =============================
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S %Z")

def send_whatsapp(m):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(m)}&apikey={CALLMEBOT_API_KEY}",
                timeout=5
            )
        except: pass

# ============================= REST API: SYMBOLS =============================
def fetch_usdt_pairs():
    global top_symbols
    logger.info("Fetching USDT pairs via REST API...")
    try:
        info = client.get_exchange_info()
        usdt_pairs = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        tickers = client.get_ticker()
        valid = []
        for t in tickers:
            sym = t['symbol']
            if sym not in usdt_pairs: continue
            price = float(t['lastPrice'])
            vol = float(t['quoteVolume'])
            low = float(t['lowPrice'])
            if MIN_PRICE <= Decimal(str(price)) <= MAX_PRICE and vol >= MIN_24H_VOLUME_USDT:
                valid.append((sym, vol, low))
        top_symbols = [s[0] for s in sorted(valid, key=lambda x: x[1], reverse=True)[:MAX_KLINE_SYMBOLS]]
        with open(SYMBOL_CACHE_FILE, 'w') as f:
            json.dump({'ts': time.time(), 'symbols': top_symbols}, f)
        logger.info(f"Fetched {len(top_symbols)} valid /USDT pairs")
    except Exception as e:
        logger.critical(f"Failed to fetch symbols: {e}")
        sys.exit(1)

# ============================= REST API: POSITIONS =============================
def get_balance():
    with _balance_lock:
        if time.time() - _balance_cache['ts'] < 30:
            return _balance_cache['value']
    try:
        acc = client.get_account()
        free = next((Decimal(b['free']) for b in acc['balances'] if b['asset'] == 'USDT'), Decimal('0'))
        with _balance_lock:
            _balance_cache.update({'value': free, 'ts': time.time()})
        return free
    except Exception as e:
        logger.error(f"get_balance error: {e}")
        return Decimal('0')

def load_positions_from_rest():
    global positions
    positions.clear()
    try:
        acc = client.get_account()
        with DB() as s:
            s.query(Position).delete()
            for bal in acc['balances']:
                asset = bal['asset']
                qty = Decimal(bal['free'])
                if qty <= 0 or asset in {'USDT', 'USDC'}: continue
                symbol = f"{asset}USDT"
                try:
                    ticker = client.get_symbol_ticker(symbol=symbol)
                    price = Decimal(ticker['price'])
                    if price <= 0: continue
                    maker, _ = get_trade_fees(symbol)
                    pos = Position(symbol=symbol, quantity=qty, avg_entry_price=price, buy_fee_rate=maker)
                    s.add(pos)
                    positions[symbol] = {'qty': float(qty), 'entry': float(price)}
                    logger.info(f"Loaded position: {symbol} {qty} @ ${price}")
                except Exception as e:
                    logger.debug(f"Skip {symbol}: {e}")
    except Exception as e:
        logger.error(f"load_positions error: {e}")

def get_trade_fees(symbol):
    try:
        fee = client.get_trade_fee(symbol=symbol)
        return float(fee[0]['makerCommission']), float(fee[0]['takerCommission'])
    except:
        return 0.001, 0.001

# ============================= LIVE ORDER EXECUTION =============================
def place_market_buy(symbol, quote_qty):
    try:
        order = client.order_market_buy(symbol=symbol, quoteOrderQty=float(quote_qty))
        logger.info(f"MARKET BUY: {symbol} ${quote_qty}")
        send_whatsapp(f"BUY {symbol} ${quote_qty}")
        return order
    except Exception as e:
        logger.error(f"Buy failed: {e}")
        return None

def place_market_sell(symbol, qty):
    try:
        order = client.order_market_sell(symbol=symbol, quantity=float(qty))
        logger.info(f"MARKET SELL: {symbol} {qty}")
        send_whatsapp(f"SELL {symbol} {qty}")
        return order
    except Exception as e:
        logger.error(f"Sell failed: {e}")
        return None

# ============================= WEBSOCKET =============================
def on_message(ws, msg):
    try:
        data = json.loads(msg)
        stream = data.get('stream')
        payload = data.get('data', data)

        if stream == '!miniTicker@arr':
            for t in payload:
                sym = t['s']
                if sym not in top_symbols: continue
                price = float(t['c'])
                price_cache[sym] = price
                low_24h_cache[sym] = min(low_24h_cache.get(sym, price), float(t['l']))

        elif stream == '!bookTicker':
            sym = payload['s']
            if sym not in top_symbols: return
            bid = Decimal(payload['b'])
            ask = Decimal(payload['a'])
            tot = bid + ask
            pct_bid = float(bid/tot*100) if tot else 50.0
            pct_ask = float(ask/tot*100) if tot else 50.0
            book_cache[sym] = {
                'best_bid': bid, 'best_ask': ask,
                'pct_bid': pct_bid, 'pct_ask': pct_ask,
                'ts': time.time()
            }
            buy_pressure_hist[sym].append(pct_bid)

        elif stream and '@kline_1m' in stream:
            k = payload['k']
            if not k['x']: return
            sym = payload['s']
            if sym not in top_symbols: return
            close = float(k['c'])
            klines_cache[sym].append(close)
            if len(klines_cache[sym]) >= RSI_PERIOD and int(time.time()) % 60 == 0:
                closes = np.array(list(klines_cache[sym]))
                rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)[-1]
                if np.isfinite(rsi):
                    rsi_cache[sym] = float(rsi)

    except Exception as e:
        logger.error(f"WS error: {e}", exc_info=True)

def on_error(ws, err):
    logger.warning(f"WS error: {err}")
    time.sleep(5)
    start_websockets()

def on_close(ws, code, msg):
    logger.warning(f"WS closed: {code}")
    time.sleep(5)
    start_websockets()

def on_open_market(ws):
    logger.info("WebSocket connected")
    def sub():
        streams = ["!miniTicker@arr", "!bookTicker"]
        for s in top_symbols:
            streams.append(f"{s.lower()}@kline_1m")
        for s in streams:
            ws.send(json.dumps({"method": "SUBSCRIBE", "params": [s], "id": int(time.time()*1000)}))
            time.sleep(SUBSCRIBE_DELAY)
    threading.Thread(target=sub, daemon=True).start()

def start_websockets():
    global ws_market
    stop_ws()
    url = f"{WS_BASE}/stream?streams=!miniTicker@arr/!bookTicker"
    ws_market = websocket.WebSocketApp(url, on_open=on_open_market, on_message=on_message, on_error=on_error, on_close=on_close)
    threading.Thread(target=ws_market.run_forever, kwargs={'ping_interval': 20}, daemon=True).start()
    logger.info("WebSocket LIVE")

def stop_ws():
    global ws_market
    if ws_market: ws_market.close()
    time.sleep(1)

# ============================= TRADING LOGIC =============================
def check_buy_signals():
    usdt = get_balance()
    if usdt < MIN_BALANCE: return
    alloc = min(usdt * RISK_PER_TRADE, usdt - MIN_BALANCE)

    for sym in top_symbols:
        if sym in dyn_buy_active or sym in positions: continue
        if time.time() - buy_cooldown.get(sym, 0) < BUY_COOLDOWN_SECONDS: continue

        ob = book_cache.get(sym, {})
        rsi = rsi_cache.get(sym)
        low = low_24h_cache.get(sym)
        if not (ob and rsi and low): continue

        if (rsi <= RSI_OVERSOLD and
            ob['pct_ask'] >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
            ob['best_bid'] <= Decimal(str(low)) * Decimal('1.01')):

            order = place_market_buy(sym, alloc)
            if order:
                buy_cooldown[sym] = time.time()
                dyn_buy_active.add(sym)
                with DB() as s:
                    qty = Decimal(order['executedQty'])
                    price = Decimal(order['cummulativeQuoteQty']) / qty
                    s.add(Position(symbol=sym, quantity=qty, avg_entry_price=price, buy_fee_rate=0.001))

def check_sell_signals():
    with DB() as s:
        for pos in s.query(Position).all():
            sym = pos.symbol
            if sym in dyn_sell_active: continue
            entry = Decimal(str(pos.avg_entry_price))
            ob = book_cache.get(sym, {})
            if not ob: continue
            ask = ob['best_ask']
            rsi = rsi_cache.get(sym)
            maker, taker = get_trade_fees(sym)
            net_return = (ask - entry) / entry - Decimal(str(maker + taker))

            if (net_return >= PROFIT_TARGET_NET and rsi >= RSI_OVERBOUGHT):
                hist = buy_pressure_hist[sym]
                if len(hist) >= 3 and max(hist) >= ORDERBOOK_BUY_PRESSURE_SPIKE * 100 and hist[-1] <= ORDERBOOK_BUY_PRESSURE_DROP * 100:
                    order = place_market_sell(sym, pos.quantity)
                    if order:
                        dyn_sell_active.add(sym)
                        s.delete(pos)

# ============================= PORTFOLIO VALUE =============================
def calculate_portfolio_value():
    total = get_balance()
    with DB() as s:
        for pos in s.query(Position).all():
            sym = pos.symbol
            qty = float(pos.quantity)
            ob = book_cache.get(sym, {})
            price = float(ob.get('best_bid') or ob.get('best_ask', 0))
            total += Decimal(str(qty * price))
    return float(total), {}

# ============================= DASHBOARD =============================
def print_professional_dashboard():
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        now = now_cst()
        usdt_free = get_balance()
        total_portfolio, _ = calculate_portfolio_value()

        NAVY = "\033[48;5;17m"
        YELLOW = "\033[38;5;226m"
        GREEN = "\033[38;5;82m"
        RED = "\033[38;5;196m"
        WHITE = "\033[38;5;255m"
        RESET = "\033[0m"
        BOLD = "\033[1m"

        print(f"{NAVY}{'=' * 120}{RESET}")
        print(f"{NAVY}{BOLD}{WHITE}{' Binance.US Live Trading Bot v7 ':^120}{RESET}")
        print(f"{NAVY}{'=' * 120}{RESET}\n")

        print(f"{NAVY}{YELLOW}{'Time (CST/CDT)':<20} {WHITE}{now}{RESET}")
        print(f"{NAVY}{YELLOW}{'Available USDT':<20} {GREEN}${usdt_free:,.6f}{RESET}")
        print(f"{NAVY}{YELLOW}{'Total Portfolio':<20} {GREEN}${total_portfolio:,.6f}{RESET}")
        print(f"{NAVY}{YELLOW}{'Active Symbols':<20} {len(top_symbols):>3}{RESET}")
        print(f"{NAVY}{YELLOW}{'Trailing Buys':<20} {len(dyn_buy_active):>3}{RESET}")
        print(f"{NAVY}{YELLOW}{'Trailing Sells':<20} {len(dyn_sell_active):>3}{RESET}")
        print(f"{NAVY}{YELLOW}{'-' * 120}{RESET}\n")

        with DB() as s:
            db_positions = s.query(Position).all()

        if db_positions:
            print(f"{NAVY}{BOLD}{YELLOW}{' OWNED POSITIONS ':^120}{RESET}")
            print(f"{NAVY}{YELLOW}{'-' * 120}{RESET}")
            print(f"{NAVY}{YELLOW}{'SYMBOL':<10} {'QTY':>14} {'ENTRY':>12} {'CURRENT':>12} {'RSI':>6} {'P&L %':>8}{RESET}")
            print(f"{NAVY}{YELLOW}{'-' * 120}{RESET}")

            total_unrealized = Decimal('0')
            for pos in db_positions:
                sym = pos.symbol
                qty = float(pos.quantity)
                entry = float(pos.avg_entry_price)
                ob = book_cache.get(sym, {})
                cur = float(ob.get('best_bid') or ob.get('best_ask', 0))
                rsi = rsi_cache.get(sym, 0)
                rsi_str = f"{rsi:5.1f}" if rsi else "N/A"

                maker, taker = get_trade_fees(sym)
                gross = (cur - entry) * qty
                fee_cost = (maker + taker) * cur * qty
                net_profit = gross - fee_cost
                total_unrealized += Decimal(str(net_profit))

                pnl_pct = ((cur - entry) / entry - (maker + taker)) * 100
                color = GREEN if net_profit > 0 else RED
                print(f"{NAVY}{YELLOW}{sym:<10} {qty:>14.6f} {entry:>12.6f} {cur:>12.6f} {rsi_str:>6} {color}{pnl_pct:>7.2f}%{RESET}")

            print(f"{NAVY}{YELLOW}{'-' * 120}{RESET}")
            pnl_color = GREEN if total_unrealized > 0 else RED
            print(f"{NAVY}{YELLOW}{'TOTAL UNREALIZED P&L':<50} {pnl_color}${float(total_unrealized):>12,.2f}{RESET}\n")
        else:
            print(f"{NAVY}{YELLOW} No open positions.{RESET}\n")

        print(f"{NAVY}{'=' * 120}{RESET}")

    except Exception as e:
        logger.error(f"Dashboard error: {e}", exc_info=True)

# ============================= MAIN =============================
def main():
    # === ONE-TIME SETUP ===
    fetch_usdt_pairs()
    if not top_symbols:
        logger.critical("No symbols loaded")
        return

    load_positions_from_rest()
    logger.info(f"Loaded {len(positions)} positions from REST")

    if not start_websockets():
        return

    # === MAIN LOOP ===
    last_dash = 0
    while True:
        try:
            now = time.time()

            check_buy_signals()
            check_sell_signals()

            if now - last_dash >= DASHBOARD_REFRESH:
                print_professional_dashboard()
                last_dash = now

            time.sleep(1)
        except Exception as e:
            logger.critical(f"Main loop error: {e}", exc_info=True)
            time.sleep(10)

# ============================= RUN ONCE =============================
if __name__ == "__main__":
    main()  # No restart loop
