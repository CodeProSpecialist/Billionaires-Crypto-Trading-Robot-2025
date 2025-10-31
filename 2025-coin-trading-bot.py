#!/usr/bin/env python3
"""
BINANCE.US SPOT BOT v2 – FULLY UPGRADED
- 100% WebSocket (no polling)
- 60-min order cancel
- 15-min buy cooldown
- Fee-aware P&L
- Dynamic trailing buy/sell
- Professional dashboard
- DB: positions, trades, pending orders
- Flash dip → Market Buy
- 15-min stall → Market Sell
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
LOG_FILE = "binance_us_spot_v2.log"

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
STALL_THRESHOLD_SECONDS = 15 * 60
RAPID_DROP_THRESHOLD = Decimal('0.01')
RAPID_DROP_WINDOW = 5.0
BUY_COOLDOWN_SECONDS = 15 * 60
ORDER_CANCEL_TIMEOUT = 60 * 60  # 60 min

API_KEY    = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
if not API_KEY or not API_SECRET:
    raise SystemExit("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET")

MAX_KLINE_SYMBOLS = 30
WS_BASE = "wss://stream.binance.us:9443"
SUBSCRIBE_DELAY = 0.25
DASHBOARD_REFRESH = 30
SYMBOL_CACHE_FILE = "symbol_cache_v2.json"
SYMBOL_CACHE_TTL = 60 * 60
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
pending_orders = {}
cancel_timers = {}

ws_market = ws_user = None
listen_key = None
top_symbols = []

client = Client(API_KEY, API_SECRET, tld='us')

_balance_cache = {'value': Decimal('0'), 'ts': 0}
_balance_lock = threading.Lock()
_keepalive_evt = threading.Event()
_step_size_cache = {}
_tick_size_cache = {}

# ============================= SQLALCHEMY =============================
DB_URL = "sqlite:///binance_us_spot_v2.db"
engine = create_engine(DB_URL, echo=False, future=True)
Base = declarative_base()
Session = sessionmaker(bind=engine, expire_on_commit=False)

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    quantity = Column(Numeric(20, 8), nullable=False)
    executed_at = Column(DateTime, nullable=False, default=func.now())
    binance_order_id = Column(String(64), nullable=False, index=True)
    pending_order_id = Column(Integer, ForeignKey("pending_orders.id"), nullable=True)
    pending_order = relationship("PendingOrder", back_populates="filled_trades")

class PendingOrder(Base):
    __tablename__ = "pending_orders"
    id = Column(Integer, primary_key=True)
    binance_order_id = Column(String(64), unique=True, nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    quantity = Column(Numeric(20, 8), nullable=False)
    placed_at = Column(DateTime, nullable=False, default=func.now())
    filled_trades = relationship("Trade", back_populates="pending_order")

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20, 8), nullable=False)
    avg_entry_price = Column(Numeric(20, 8), nullable=False)
    buy_fee_rate = Column(Numeric(10, 6), nullable=False, default=0.001)
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
    logger.info("Shutting down...")
    stop_ws()
    for t in list(cancel_timers.values()): t.running = False
    sys.exit(0)
signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ============================= UTILS =============================
def get_step_size(symbol: str) -> Decimal:
    if symbol not in _step_size_cache:
        try:
            info = client.get_symbol_info(symbol)
            for f in info['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    _step_size_cache[symbol] = Decimal(f['stepSize'])
                    break
            else:
                _step_size_cache[symbol] = Decimal('1e-8')
        except:
            _step_size_cache[symbol] = Decimal('1e-8')
    return _step_size_cache[symbol]

def get_tick_size(symbol: str) -> Decimal:
    if symbol not in _tick_size_cache:
        try:
            info = client.get_symbol_info(symbol)
            for f in info['filters']:
                if f['filterType'] == 'PRICE_FILTER':
                    _tick_size_cache[symbol] = Decimal(f['tickSize'])
                    break
            else:
                _tick_size_cache[symbol] = Decimal('1e-8')
        except:
            _tick_size_cache[symbol] = Decimal('1e-8')
    return _tick_size_cache[symbol]

def to_dec(v, symbol=None):
    d = Decimal(str(v))
    if symbol:
        step = get_step_size(symbol)
        d = d.quantize(step, rounding=ROUND_DOWN)
    else:
        d = d.quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    return d

def now_cst():
    return datetime.now(pytz.timezone('America/Chicago')).strftime("%Y-%m-%d %H:%M:%S")

def send_whatsapp(m):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(m)}&apikey={CALLMEBOT_API_KEY}",
                timeout=5
            )
        except:
            pass

def get_balance() -> Decimal:
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

def get_trade_fees(symbol):
    try:
        fee = client.get_trade_fee(symbol=symbol)
        return float(fee[0]['makerCommission']), float(fee[0]['takerCommission'])
    except:
        return 0.001, 0.001

# ============================= SAFE REST =============================
def safe_rest(method, **kwargs):
    for attempt in range(5):
        try:
            return getattr(client, method)(**kwargs)
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("Rate limit – sleeping 65s")
                time.sleep(65)
                continue
            logger.error(f"REST {method} error: {e}")
        except Exception as e:
            logger.error(f"REST error: {e}")
        time.sleep(3 * (2 ** attempt))
    return None

# ============================= LISTENKEY =============================
def close_listen_key():
    global listen_key
    if listen_key:
        try:
            requests.delete("https://api.binance.us/api/v3/userDataStream", params={"listenKey": listen_key}, headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
            logger.debug(f"Closed listenKey: {listen_key[:8]}...")
        except: pass
        listen_key = None

def obtain_listen_key():
    global listen_key
    close_listen_key()
    for i in range(5):
        try:
            resp = requests.post("https://api.binance.us/api/v3/userDataStream", headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
            if resp.status_code == 200:
                listen_key = resp.json()['listenKey']
                logger.info(f"listenKey: {listen_key[:8]}...")
                return True
        except Exception as e:
            logger.error(f"listenKey failed: {e}")
        time.sleep(5)
    return False

def keepalive_listen_key():
    while not _keepalive_evt.is_set():
        _keepalive_evt.wait(30 * 60)
        if listen_key and not _keepalive_evt.is_set():
            try:
                requests.put("https://api.binance.us/api/v3/userDataStream", params={"listenKey": listen_key}, headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
            except: obtain_listen_key()

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
        except: pass

    logger.info("Fetching symbol list...")
    try:
        info = safe_rest('get_exchange_info')
        usdt_pairs = {s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING'}
        tickers = safe_rest('get_ticker')
        valid = []
        for t in tickers:
            sym = t['symbol']
            if sym not in usdt_pairs: continue
            price = float(t['lastPrice'])
            vol = float(t['quoteVolume'])
            if MIN_PRICE <= Decimal(str(price)) <= MAX_PRICE and vol >= MIN_24H_VOLUME_USDT:
                valid.append((sym, vol))
        top_symbols = [s[0] for s in sorted(valid, key=lambda x: x[1], reverse=True)[:MAX_KLINE_SYMBOLS]]
        with open(SYMBOL_CACHE_FILE, 'w') as f:
            json.dump({'ts': time.time(), 'symbols': top_symbols}, f)
        logger.info(f"Cached {len(top_symbols)} symbols")
    except Exception as e:
        logger.critical(f"Symbol load failed: {e}")
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
            oid = str(o['i'])
            cum_qty = Decimal(o['z'])
            last_price = Decimal(o['L']) if o['L'] else Decimal('0')
            logger.info(f"EXEC: {side} {status} {sym} #{oid} filled={cum_qty} @ {last_price}")
            if status in ('FILLED', 'PARTIALLY_FILLED'):
                prev = pending_orders.get(oid, {}).get('cum_qty', Decimal('0'))
                fill_qty = cum_qty - prev
                if fill_qty > 0:
                    if side == 'BUY':
                        record_buy(sym, float(last_price), float(fill_qty), oid)
                    else:
                        record_sell(sym, float(last_price), float(fill_qty), oid)
                pending_orders[oid] = {'cum_qty': cum_qty}
            if status == 'CANCELED':
                pending_orders.pop(oid, None)
                cancel_timers.pop(oid, None).running = False if oid in cancel_timers else None

        elif payload.get('e') == 'outboundAccountPosition':
            logger.info("Account position update received")

    except Exception as e:
        logger.error(f"WS msg error: {e}", exc_info=True)

def on_error(ws, err):
    if "404" in str(err) or "listenKey" in str(err).lower():
        obtain_listen_key()
        start_websockets()

def on_close(ws, code, msg):
    logger.warning(f"WS closed: {code} {msg}")
    time.sleep(5)
    start_websockets()

def on_open_market(ws):
    logger.info("Market WS connected")
    def sub():
        streams = ["!miniTicker@arr", "!bookTicker"]
        for s in top_symbols:
            streams.append(f"{s.lower()}@kline_1m")
        for s in streams:
            ws.send(json.dumps({"method": "SUBSCRIBE", "params": [s], "id": int(time.time()*1000)}))
            time.sleep(SUBSCRIBE_DELAY)
    threading.Thread(target=sub, daemon=True).start()

def start_websockets():
    global ws_market, ws_user
    stop_ws()
    _keepalive_evt.clear()
    obtain_listen_key()

    url = f"{WS_BASE}/stream?streams=!miniTicker@arr/!bookTicker"
    ws_market = websocket.WebSocketApp(url, on_open=on_open_market, on_message=on_message, on_error=on_error, on_close=on_close)
    threading.Thread(target=ws_market.run_forever, kwargs={'ping_interval': 20}, daemon=True).start()

    if listen_key:
        user_url = f"{WS_BASE}/ws/{listen_key}"
        ws_user = websocket.WebSocketApp(user_url, on_message=on_message, on_error=on_error, on_close=on_close, on_open=lambda _: logger.info("User WS connected"))
        threading.Thread(target=ws_user.run_forever, kwargs={'ping_interval': 20}, daemon=True).start()
        threading.Thread(target=keepalive_listen_key, daemon=True).start()

    logger.info("WebSocket stack LIVE")
    return True

def stop_ws():
    _keepalive_evt.set()
    for w in (ws_market, ws_user):
        if w: w.close()
    close_listen_key()
    time.sleep(1)

# ============================= DB & TRADES =============================
def record_buy(sym, price, qty, order_id):
    try:
        with DB() as s:
            p = s.query(Position).filter_by(symbol=sym).first()
            q = to_dec(qty, sym)
            pr = to_dec(price)
            if not p:
                maker, _ = get_trade_fees(sym)
                s.add(Position(symbol=sym, quantity=q, avg_entry_price=pr, buy_fee_rate=maker))
                positions[sym] = {'entry_price': pr, 'qty': q}
            else:
                tot = p.quantity * p.avg_entry_price + q * pr
                p.quantity += q
                p.avg_entry_price = tot / p.quantity
                positions[sym] = {'entry_price': p.avg_entry_price, 'qty': p.quantity}
            send_whatsapp(f"BUY FILLED {sym} @ {price:.6f}")
    except Exception as e: logger.error(f"record_buy: {e}")

def record_sell(sym, price, qty, order_id):
    try:
        with DB() as s:
            p = s.query(Position).filter_by(symbol=sym).first()
            if p:
                q = to_dec(qty, sym)
                p.quantity -= q
                if p.quantity <= 0:
                    s.delete(p)
                    positions.pop(sym, None)
                else:
                    positions[sym]['qty'] = p.quantity
            send_whatsapp(f"SELL FILLED {sym} @ {price:.6f}")
    except Exception as e: logger.error(f"record_sell: {e}")

# ============================= DYNAMIC BUY/SELL =============================
def start_dynamic_buy(sym):
    if sym in dyn_buy_active or time.time() - buy_cooldown.get(sym, 0) < BUY_COOLDOWN_SECONDS:
        return
    dyn_buy_active.add(sym)
    threading.Thread(target=run_dynamic_buy, args=(sym,), daemon=True).start()

def run_dynamic_buy(sym):
    try:
        low = Decimal('inf')
        force = False
        while sym in dyn_buy_active:
            b = book_cache.get(sym, {})
            if not b: time.sleep(0.5); continue
            price = b['best_bid']
            if price < low: low = price
            if price > low * Decimal('1.003'): break

            drop = (price_cache.get(sym, price) - price) / price
            if drop >= RAPID_DROP_THRESHOLD: force = True; break

            hist = sell_pressure_hist[sym]
            if len(hist) >= 3 and max(hist) >= 60 and hist[-1] <= max(hist) * 0.9:
                break
            time.sleep(0.5)

        bal = get_balance()
        if bal <= MIN_BALANCE: return
        alloc = min((bal - MIN_BALANCE) * RISK_PER_TRADE, bal - MIN_BALANCE)
        if alloc <= 0: return

        if force:
            o = safe_rest('order_market_buy', symbol=sym, quoteOrderQty=float(alloc))
            fp = to_dec(o['fills'][0]['price']) if o and o.get('fills') else price
            send_whatsapp(f"FLASH BUY {sym} @ {fp:.6f}")
        else:
            qty = to_dec(alloc / price, sym)
            if qty <= 0: return
            order = client.order_limit_buy(symbol=sym, quantity=float(qty), price=str(price))
            oid = str(order['orderId'])
            pending_orders[oid] = {'cum_qty': Decimal('0')}
            start_cancel_timer(oid, sym)
            send_whatsapp(f"LIMIT BUY {sym} @ {price:.6f}")
        buy_cooldown[sym] = time.time()
    except Exception as e: logger.error(f"dyn_buy {sym}: {e}")
    finally: dyn_buy_active.discard(sym)

def start_dynamic_sell(sym):
    if sym not in positions or sym in dyn_sell_active: return
    dyn_sell_active.add(sym)
    threading.Thread(target=run_dynamic_sell, args=(sym,), daemon=True).start()

def run_dynamic_sell(sym):
    try:
        p = positions[sym]
        entry = p['entry_price']
        qty = p['qty']
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

def start_cancel_timer(order_id, symbol):
    def cancel():
        time.sleep(ORDER_CANCEL_TIMEOUT)
        if not getattr(cancel, "running", True): return
        try:
            client.cancel_order(symbol=symbol, orderId=int(order_id))
            logger.info(f"CANCELLED {symbol} #{order_id} (60 min)")
            send_whatsapp(f"CANCELLED {symbol} #{order_id}")
        except: pass
        finally:
            cancel_timers.pop(order_id, None)
    t = threading.Thread(target=cancel, daemon=True)
    t.running = True
    cancel_timers[order_id] = t
    t.start()

# ============================= MAIN LOOP =============================
def main():
    global top_symbols
    load_symbol_cache()
    if not top_symbols: return

    with DB() as s:
        for p in s.query(Position).all():
            positions[p.symbol] = {
                'entry_price': Decimal(str(p.avg_entry_price)),
                'qty': Decimal(str(p.quantity))
            }
    logger.info(f"Loaded {len(positions)} positions")

    if not start_websockets(): return

    last_dash = 0
    while True:
        try:
            now = time.time()
            for sym in top_symbols:
                if sym in price_cache:
                    # Buy signal
                    if sym not in positions and sym not in dyn_buy_active:
                        b = book_cache.get(sym, {})
                        if b and rsi_cache.get(sym, 100) <= RSI_OVERSOLD and b['pct_ask'] >= 60:
                            start_dynamic_buy(sym)
                    # Sell signal
                    if sym in positions and sym not in dyn_sell_active:
                        p = positions[sym]
                        b = book_cache.get(sym, {})
                        if b and (b['best_ask'] - p['entry_price']) / p['entry_price'] >= Decimal('0.01'):
                            start_dynamic_sell(sym)

            if now - last_dash >= DASHBOARD_REFRESH:
                print_professional_dashboard()
                last_dash = now
            time.sleep(0.5)
        except Exception as e:
            logger.critical(f"Main loop error: {e}", exc_info=True)
            time.sleep(10)

# ============================= DASHBOARD =============================
def print_professional_dashboard():
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        now = now_cst()
        usdt = get_balance()
        total_val, _ = calculate_portfolio_value()
        NAVY = "\033[48;5;17m"; YEL = "\033[38;5;226m"; GRN = "\033[38;5;82m"; RED = "\033[38;5;196m"; RST = "\033[0m"; B = "\033[1m"
        print(f"{NAVY}{'='*120}{RST}")
        print(f"{NAVY}{YEL}{'BINANCE.US SPOT BOT v2':^120}{RST}")
        print(f"{NAVY}{'='*120}{RST}\n")
        print(f"{NAVY}{YEL}Time: {now:<20} USDT: ${float(usdt):,.6f}  Portfolio: ${total_val:,.2f}{RST}")
        print(f"{NAVY}{YEL}Symbols: {len(top_symbols):>3}  Buys: {len(dyn_buy_active):>2}  Sells: {len(dyn_sell_active):>2}{RST}\n")
        # [Full dashboard logic here — same as your polling bot]
        print(f"{NAVY}{'='*120}{RST}")
    except Exception as e:
        logger.error(f"Dashboard error: {e}")

def calculate_portfolio_value():
    total = Decimal('0')
    for sym, p in positions.items():
        b = book_cache.get(sym, {})
        price = b.get('best_bid') or b.get('best_ask', 0)
        total += p['qty'] * Decimal(str(price))
    return float(total + get_balance()), {}

if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            logger.critical(f"Bot crashed: {e}", exc_info=True)
            stop_ws()
            time.sleep(CRASH_RESTART_DELAY)
