#!/usr/bin/env python3
"""
BINANCE.US SPOT-ONLY TRAILING BOT – FINAL VERSION
- USDT pairs only
- Price: $0.01 – $1000
- 24h volume ≥ $50,000
- 100% WebSocket, symbol cache, safe listenKey
- Flash Dip → Market Buy
- 15-Min Stall → Market Sell
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
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError

# ============================= CONFIG =============================
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE   = os.getenv('CALLMEBOT_PHONE')
LOG_FILE = "binance_us_spot.log"

MIN_PRICE = Decimal('0.01')
MAX_PRICE = Decimal('1000.0')
MIN_24H_VOLUME_USDT = 50_000

RSI_PERIOD = 14
PROFIT_TARGET_NET = Decimal('0.008')
RISK_PER_TRADE    = Decimal('0.10')
MIN_BALANCE       = 2.0
ORDERBOOK_SELL_PRESSURE_THRESHOLD = 0.60
ORDERBOOK_BUY_PRESSURE_SPIKE      = 0.65
ORDERBOOK_BUY_PRESSURE_DROP       = 0.55
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
STALL_THRESHOLD_SECONDS = 15 * 60
RAPID_DROP_THRESHOLD = 0.01
RAPID_DROP_WINDOW = 5.0

API_KEY    = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
if not API_KEY or not API_SECRET:
    raise SystemExit("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET")

MAX_KLINE_SYMBOLS = 30
WS_BASE = "wss://stream.binance.us:9443"
SUBSCRIBE_DELAY = 0.25
DASHBOARD_REFRESH = 20
SYMBOL_CACHE_FILE = "symbol_cache.json"
SYMBOL_CACHE_TTL = 60 * 60  # 1 hour
CRASH_RESTART_DELAY = 4 * 60 + 30

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
stall_timer = {}

positions = {}
dyn_buy_active = set()
dyn_sell_active = set()

ws_market = ws_user = None
listen_key = None
top_symbols = []
client = None

# ============================= SQLALCHEMY =============================
DB_URL = "sqlite:///binance_us_spot.db"
engine = create_engine(DB_URL, echo=False, future=True)
Base = declarative_base()
Session = sessionmaker(bind=engine, expire_on_commit=False)

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20,8), nullable=False)
    avg_entry_price = Column(Numeric(20,8), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

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
    logger.info("Shutting down gracefully...")
    stop_ws()
    sys.exit(0)
signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ============================= SAFE REST =============================
def safe_rest(method, **kwargs):
    for attempt in range(5):
        try:
            return getattr(client, method)(**kwargs)
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("Rate limit hit – sleeping 65s")
                time.sleep(65)
                continue
            logger.error(f"REST {method} error (attempt {attempt+1}): {e}")
        except Exception as e:
            logger.error(f"Unexpected REST error (attempt {attempt+1}): {e}")
        time.sleep(3 * (2 ** attempt))
    logger.error(f"REST {method} failed after 5 attempts")
    return None

# ============================= LISTENKEY =============================
def obtain_listen_key():
    global listen_key, client
    if client is None:
        client = Client(API_KEY, API_SECRET, tld='us')
    for i in range(5):
        try:
            resp = safe_rest('stream_get_listen_key')
            if resp and 'listenKey' in resp:
                listen_key = resp['listenKey']
                logger.info(f"listenKey obtained: {listen_key[:8]}...")
                return True
        except Exception as e:
            logger.error(f"listenKey attempt {i+1} failed: {e}")
        time.sleep(5)
    logger.warning("Continuing without user stream (listenKey failed)")
    return False

def keepalive_listen_key():
    while True:
        time.sleep(30 * 60)
        if listen_key:
            try:
                client.stream_keepalive(listen_key)
                logger.debug("listenKey keep-alive sent")
            except Exception as e:
                logger.warning(f"keepalive failed: {e}")

# ============================= SYMBOL CACHE =============================
def load_symbol_cache():
    global top_symbols
    if os.path.exists(SYMBOL_CACHE_FILE):
        try:
            with open(SYMBOL_CACHE_FILE) as f:
                data = json.load(f)
                if time.time() - data.get('ts', 0) < SYMBOL_CACHE_TTL:
                    top_symbols = data['symbols']
                    logger.info(f"Loaded {len(top_symbols)} symbols from cache")
                    return
        except Exception as e:
            logger.warning(f"Cache read error: {e}")

    logger.info("Fetching fresh symbol list from Binance.US...")
    try:
        info = safe_rest('get_exchange_info')
        if not info: raise Exception("No exchange info")
        usdt_pairs = {s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING'}

        tickers = safe_rest('get_ticker')
        if not tickers: raise Exception("No ticker data")
        valid = []
        for t in tickers:
            sym = t['symbol']
            if sym not in usdt_pairs: continue
            try:
                price = float(t['lastPrice'])
                vol = float(t['quoteVolume'])
            except: continue
            if MIN_PRICE <= Decimal(str(price)) <= MAX_PRICE and vol >= MIN_24H_VOLUME_USDT:
                valid.append((sym, vol))
        top_symbols = [s[0] for s in sorted(valid, key=lambda x: x[1], reverse=True)[:MAX_KLINE_SYMBOLS]]
        with open(SYMBOL_CACHE_FILE, 'w') as f:
            json.dump({'ts': time.time(), 'symbols': top_symbols}, f)
        logger.info(f"Fresh list cached: {len(top_symbols)} symbols")
    except Exception as e:
        logger.critical(f"Failed to load symbols: {e}")
        sys.exit(1)

# ============================= WEBSOCKET =============================
def on_message(ws, msg):
    try:
        if not msg.strip(): return
        data = json.loads(msg)
        stream = data.get('stream')
        payload = data.get('data', data)

        if stream == '!miniTicker@arr':
            for t in payload:
                sym = t['s']
                if sym not in top_symbols: continue
                price = float(t['c'])
                price_cache[sym] = price
                last_price_update[sym] = time.time()
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
            sell_pressure_hist[sym].append(pct_ask)

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

        elif payload.get('e') == 'executionReport':
            o = payload
            sym = o['s']
            side = o['S']
            status = o['X']
            qty = Decimal(o['q'])
            price = Decimal(o['L']) if o['L'] else Decimal('0')
            if status == 'FILLED':
                if side == 'BUY':
                    record_buy(sym, float(price), float(qty))
                    send_whatsapp(f"FILLED BUY {sym} @ {price:.6f}")
                    dyn_buy_active.discard(sym)
                elif side == 'SELL':
                    record_sell(sym, float(price), float(qty))
                    send_whatsapp(f"FILLED SELL {sym} @ {price:.6f}")
                    dyn_sell_active.discard(sym)

        elif payload == {}:
            ws.send(json.dumps({}))
    except Exception as e:
        logger.error(f"WS message error: {e}", exc_info=True)

def on_error(ws, err): logger.error(f"WS error: {err}")
def on_close(ws, code, msg):
    logger.warning(f"WS closed ({code}): {msg}")
    time.sleep(5)
    start_websockets()

def on_open_market(ws):
    logger.info("Market WS connected – subscribing...")
    def sub():
        streams = ["!miniTicker@arr", "!bookTicker"]
        for s in top_symbols:
            streams.append(f"{s.lower()}@kline_1m")
        for s in streams:
            ws.send(json.dumps({"method": "SUBSCRIBE", "params": [s], "id": int(time.time()*1000)}))
            time.sleep(SUBSCRIBE_DELAY)
        logger.info(f"Subscribed to {len(streams)} streams")
    threading.Thread(target=sub, daemon=True).start()

def start_websockets():
    global ws_market, ws_user, listen_key, client
    stop_ws()

    if client is None:
        client = Client(API_KEY, API_SECRET, tld='us')

    have_key = obtain_listen_key()

    try:
        url = f"{WS_BASE}/stream?streams=!miniTicker@arr/!bookTicker"
        global ws_market
        ws_market = websocket.WebSocketApp(url, on_open=on_open_market, on_message=on_message, on_error=on_error, on_close=on_close)
        threading.Thread(target=ws_market.run_forever, kwargs={'ping_interval': 20}, daemon=True).start()
    except Exception as e:
        logger.error(f"Market WS init failed: {e}")
        return False

    if have_key and listen_key:
        try:
            user_url = f"{WS_BASE}/ws/{listen_key}"
            global ws_user
            ws_user = websocket.WebSocketApp(user_url, on_message=on_message, on_error=on_error, on_close=on_close,
                                             on_open=lambda _: logger.info("User WS connected"))
            threading.Thread(target=ws_user.run_forever, kwargs={'ping_interval': 20}, daemon=True).start()
            threading.Thread(target=keepalive_listen_key, daemon=True).start()
        except Exception as e:
            logger.error(f"User WS init failed: {e}")

    logger.info("WebSocket stack LIVE")
    return True

def stop_ws():
    for w in (ws_market, ws_user):
        if w: w.close()

# ============================= UTILS =============================
def to_dec(v): return Decimal(str(v)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
def now_cst(): return datetime.now(pytz.timezone('America/Chicago')).strftime("%Y-%m-%d %H:%M:%S")
def send_whatsapp(m):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(m)}&apikey={CALLMEBOT_API_KEY}", timeout=5)
        except: pass

def get_balance():
    acc = safe_rest('get_account')
    if not acc: return 0.0
    for b in acc.get('balances', []):
        if b['asset'] == 'USDT':
            return float(b['free'])
    return 0.0

# ============================= STRATEGY =============================
def check_buy_signal(sym):
    try:
        if sym in dyn_buy_active or sym in positions or time.time() - buy_cooldown.get(sym, 0) < 15 * 60:
            return
        b = book_cache.get(sym, {})
        if not b or b['best_bid'] <= 0: return
        bid = b['best_bid']
        rsi = rsi_cache.get(sym)
        low = low_24h_cache.get(sym)
        if (rsi is not None and rsi <= RSI_OVERSOLD and
            b['pct_ask'] >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
            low and bid <= Decimal(str(low)) * Decimal('1.01')):
            start_dynamic_buy(sym, bid)
    except Exception as e: logger.error(f"buy_signal {sym}: {e}")

def check_sell_signal(sym):
    try:
        if sym not in positions or sym in dyn_sell_active: return
        p = positions[sym]
        entry = Decimal(str(p['entry_price']))
        qty = Decimal(str(p['qty']))
        b = book_cache.get(sym, {})
        if not b: return
        ask = b['best_ask']
        rsi = rsi_cache.get(sym)
        net = (ask - entry) / entry - Decimal('0.002')
        if net >= PROFIT_TARGET_NET and rsi is not None and rsi >= RSI_OVERBOUGHT:
            hist = buy_pressure_hist[sym]
            if len(hist) >= 3:
                peak = max(hist)
                if peak >= ORDERBOOK_BUY_PRESSURE_SPIKE * 100 and hist[-1] <= ORDERBOOK_BUY_PRESSURE_DROP * 100:
                    start_dynamic_sell(sym, entry, qty)
    except Exception as e: logger.error(f"sell_signal {sym}: {e}")

# ============================= DYNAMIC BUY/SELL =============================
def start_dynamic_buy(sym, trig):
    if sym in dyn_buy_active: return
    dyn_buy_active.add(sym)
    threading.Thread(target=run_dynamic_buy, args=(sym, trig), daemon=True).start()

def run_dynamic_buy(sym, trig):
    try:
        low = trig
        force = False
        while sym in dyn_buy_active:
            b = book_cache.get(sym, {})
            if not b: time.sleep(0.5); continue
            price = b['best_bid']
            if sym in last_price_update and time.time() - last_price_update[sym] < RAPID_DROP_WINDOW:
                drop = (price_cache.get(sym, price) - price) / price
                if drop >= RAPID_DROP_THRESHOLD: force = True; break
            if price < low: low = price
            if price > low * Decimal('1.003'): break
            hist = sell_pressure_hist[sym]
            if len(hist) >= 3 and max(hist) >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and hist[-1] <= max(hist) * 0.9:
                break
            time.sleep(0.5)

        bal = get_balance()
        if bal <= MIN_BALANCE: return
        alloc = min(Decimal(str(bal - MIN_BALANCE)) * RISK_PER_TRADE, Decimal(str(bal - MIN_BALANCE)))
        if force:
            o = safe_rest('order_market_buy', symbol=sym, quoteOrderQty=float(alloc))
            fp = to_dec(o['fills'][0]['price']) if o and o.get('fills') else Decimal('0')
            send_whatsapp(f"FLASH BUY {sym} @ {fp:.6f}")
        else:
            qty = alloc / price
            safe_rest('order_limit_buy', symbol=sym, quantity=float(qty), price=str(price))
            send_whatsapp(f"LIMIT BUY {sym} @ {price:.6f}")
        buy_cooldown[sym] = time.time()
    except Exception as e: logger.error(f"dyn_buy {sym}: {e}")
    finally: dyn_buy_active.discard(sym)

def start_dynamic_sell(sym, entry, qty):
    if sym in dyn_sell_active: return
    dyn_sell_active.add(sym)
    threading.Thread(target=run_dynamic_sell, args=(sym, entry, qty), daemon=True).start()

def run_dynamic_sell(sym, entry, qty):
    try:
        peak = entry
        while sym in dyn_sell_active:
            b = book_cache.get(sym, {})
            if not b: time.sleep(0.5); continue
            price = b['best_bid']
            if price > peak: peak = price
            if price >= entry * Decimal('1.005'): stall_timer[sym] = time.time()
            if sym in stall_timer and time.time() - stall_timer[sym] >= STALL_THRESHOLD_SECONDS:
                safe_rest('order_market_sell', symbol=sym, quantity=float(qty))
                send_whatsapp(f"STALL SELL {sym}")
                break
            if price < peak * Decimal('0.995'):
                safe_rest('order_limit_sell', symbol=sym, quantity=float(qty), price=str(price))
                send_whatsapp(f"LIMIT SELL {sym} @ {price:.6f}")
                break
            time.sleep(0.5)
    except Exception as e: logger.error(f"dyn_sell {sym}: {e}")
    finally: dyn_sell_active.discard(sym)

# ============================= DB =============================
def record_buy(sym, price, qty):
    try:
        with DB() as s:
            p = s.query(Position).filter_by(symbol=sym).first()
            q = to_dec(qty); pr = to_dec(price)
            if not p:
                s.add(Position(symbol=sym, quantity=q, avg_entry_price=pr))
            else:
                tot = p.quantity * p.avg_entry_price + q * pr
                p.quantity += q
                p.avg_entry_price = tot / p.quantity
            positions[sym] = {'entry_price': price, 'qty': qty}
    except Exception as e: logger.error(f"record_buy: {e}")

def record_sell(sym, price, qty):
    try:
        with DB() as s:
            p = s.query(Position).filter_by(symbol=sym).first()
            if p:
                p.quantity -= to_dec(qty)
                if p.quantity <= 0:
                    s.delete(p)
                    positions.pop(sym, None)
    except Exception as e: logger.error(f"record_sell: {e}")

# ============================= DASHBOARD =============================
def print_dashboard():
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        now = now_cst()
        usdt = get_balance()
        NAVY = "\033[48;5;17m"
        YEL  = "\033[38;5;226m"
        GRN  = "\033[38;5;82m"
        RED  = "\033[38;5;196m"
        RST  = "\033[0m"
        B    = "\033[1m"

        print(f"{NAVY}{'='*120}{RST}")
        print(f"{NAVY}{YEL}{'BINANCE.US SPOT BOT – LIVE':^120}{RST}")
        print(f"{NAVY}{'='*120}{RST}\n")
        print(f"{NAVY}{YEL}Time (CST): {now:<20} USDT: ${usdt:,.6f}{RST}")
        print(f"{NAVY}{YEL}Symbols: {len(top_symbols):>3}  Buy: {len(dyn_buy_active):>2}  Sell: {len(dyn_sell_active):>2}{RST}")
        print(f"{NAVY}{YEL}{'-'*120}{RST}\n")

        if positions:
            print(f"{NAVY}{B}{YEL}{'POSITIONS':^120}{RST}")
            print(f"{NAVY}{YEL}{'SYM':<10} {'QTY':>12} {'ENTRY':>12} {'CURR':>12} {'RSI':>6} {'P&L%':>8}{RST}")
            print(f"{NAVY}{YEL}{'-'*120}{RST}")
            for sym, p in positions.items():
                b = book_cache.get(sym, {})
                cur = float(b.get('best_bid') or b.get('best_ask', 0))
                rsi = rsi_cache.get(sym, 0)
                pnl = ((cur - p['entry_price']) / p['entry_price'] - 0.002) * 100
                color = GRN if pnl > 0 else RED
                print(f"{NAVY}{YEL}{sym:<10} {p['qty']:>12.6f} {p['entry_price']:>12.6f} {cur:>12.6f} {rsi:>6.1f} {color}{pnl:>7.2f}%{RST}")
            print(f"{NAVY}{YEL}{'-'*120}{RST}")
        else:
            print(f"{NAVY}{YEL}No open positions.{RST}\n")
        print(f"{NAVY}{'='*120}{RST}")
    except Exception as e:
        logger.error(f"Dashboard error: {e}")

# ============================= MAIN =============================
def main():
    global top_symbols
    load_symbol_cache()
    if not top_symbols:
        logger.critical("No valid symbols loaded")
        return

    try:
        with DB() as s:
            for p in s.query(Position).all():
                positions[p.symbol] = {'entry_price': float(p.avg_entry_price), 'qty': float(p.quantity)}
    except Exception as e:
        logger.error(f"DB load error: {e}")

    if not start_websockets():
        return

    logger.info("Bot fully operational – trading live")
    last_dash = 0
    while True:
        try:
            now = time.time()
            for sym in top_symbols:
                if sym in price_cache:
                    check_buy_signal(sym)
                    check_sell_signal(sym)
            if now - last_dash >= DASHBOARD_REFRESH:
                print_dashboard()
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
            stop_ws()
            time.sleep(CRASH_RESTART_DELAY)
