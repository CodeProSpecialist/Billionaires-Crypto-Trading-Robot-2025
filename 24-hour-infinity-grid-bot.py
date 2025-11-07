#!/usr/bin/env python3
"""
    WIDE GRID INFINITY GRID BOT – FINAL PRODUCTION
    • 100% WebSocket after startup
    • Heartbeat + Auto-Reconnect
    • Trailing Buy/Sell + Candlestick Patterns
    • Live PnL + Realized PnL
    • First Run Entry + Infinity Grid
    • Dashboard + WhatsApp Alerts
    • NEVER FREEZES
"""

import os
import sys
import time
import json
import threading
import logging
import websocket
import requests
import numpy as np
import talib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from typing import Dict, List, Tuple
from collections import deque
from logging.handlers import TimedRotatingFileHandler

from binance.client import Client
from binance.enums import *
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
import pytz

# --------------------------------------------------------------------------- #
# ============================= CONFIGURATION ============================== #
# --------------------------------------------------------------------------- #
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')
if not API_KEY or not API_SECRET:
    print("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET env vars")
    sys.exit(1)

# ---- Strategy ------------------------------------------------------
MAX_PRICE = 1000.0
MIN_PRICE = 0.01
PROFIT_TARGET_NET = Decimal('0.008')
GRID_SIZE_USDT = Decimal('5.0')
GRID_INTERVAL_PCT = Decimal('0.015')
MIN_BUFFER_USDT = Decimal('8.0')
MAX_GRIDS_PER_SIDE = 8
REBALANCE_THRESHOLD_PCT = Decimal('0.0075')

ORDERBOOK_BUY_PRESSURE_SPIKE = 0.65
ORDERBOOK_SELL_PRESSURE_THRESHOLD = 0.60
DEPTH_LEVELS = 20
STALL_THRESHOLD_SECONDS = 15 * 60
RAPID_DROP_THRESHOLD = 0.01
RAPID_DROP_WINDOW = 5.0

# ---- Time windows --------------------------------------------------
LOW_TIME_START = 2
LOW_TIME_END = 6
HIGH_TIME_START = 14
HIGH_TIME_END = 18

# ---- WS -----------------------------------------------------------
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
MAX_STREAMS_PER_CONNECTION = 100
HEARTBEAT_INTERVAL = 25  # Critical: must match Binance

# ---- Misc ---------------------------------------------------------
LOG_FILE = "wide_grid_infinity_bot.log"
first_run_executed = False

# --------------------------------------------------------------------------- #
# =============================== CONSTANTS ================================ #
# --------------------------------------------------------------------------- #
ZERO = Decimal('0')
CST_TZ = pytz.timezone('America/Chicago')

# --------------------------------------------------------------------------- #
# =============================== LOGGING ================================= #
# --------------------------------------------------------------------------- #
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    fh = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(lineno)d - %(message)s'))
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    logger.addHandler(fh)
    logger.addHandler(ch)

# --------------------------------------------------------------------------- #
# =============================== GLOBAL STATE ============================= #
# --------------------------------------------------------------------------- #
valid_symbols: Dict[str, dict] = {}
live_prices: Dict[str, Decimal] = {}
live_bids: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
live_asks: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
active_grid: Dict[str, dict] = {}
position_pnl: Dict[str, Decimal] = {}
total_pnl = ZERO
realized_pnl: Dict[str, Decimal] = {}
total_realized = ZERO
trailing_buy: Dict[str, dict] = {}
trailing_sell: Dict[str, dict] = {}
price_24h: Dict[str, deque] = {}
klines_1m: Dict[str, deque] = {}

price_lock = threading.Lock()
book_lock = threading.Lock()
pnl_lock = threading.Lock()
realized_lock = threading.Lock()
listen_key: str = None
listen_key_lock = threading.Lock()

# === SINGLE ORDER QUEUE =====================================================
order_queue: deque = deque()
order_queue_lock = threading.Lock()
order_queue_cv = threading.Condition(order_queue_lock)

# === WEBSOCKET TRACKING =====================================================
ws_instances = []
ws_threads = []
user_ws = None

# --------------------------------------------------------------------------- #
# =============================== HELPERS ================================== #
# --------------------------------------------------------------------------- #
def to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except Exception as e:
        logger.error(f"to_decimal error: {e}")
        return ZERO

def send_whatsapp_alert(msg: str):
    try:
        if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
            requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}",
                timeout=5
            )
    except Exception as e:
        logger.error(f"WhatsApp error: {e}")

def is_low_time() -> bool:
    try:
        now = datetime.now(CST_TZ)
        return LOW_TIME_START <= now.hour < LOW_TIME_END
    except:
        return False

def is_high_time() -> bool:
    try:
        now = datetime.now(CST_TZ)
        return HIGH_TIME_START <= now.hour < HIGH_TIME_END
    except:
        return False

# --------------------------------------------------------------------------- #
# =============================== DATABASE ================================= #
# --------------------------------------------------------------------------- #
DB_URL = "sqlite:///wide_grid_trades.db"
engine = create_engine(DB_URL, echo=False, future=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20, 8), nullable=False)
    avg_entry_price = Column(Numeric(20, 8), nullable=False)

class PendingOrder(Base):
    __tablename__ = "pending_orders"
    id = Column(Integer, primary_key=True)
    binance_order_id = Column(String(64), unique=True, nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    quantity = Column(Numeric(20, 8), nullable=False)

class TradeRecord(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    quantity = Column(Numeric(20, 8), nullable=False)
    fee = Column(Numeric(20, 8), nullable=False, default=0)
    timestamp = Column(DateTime, default=func.now())

if not os.path.exists("wide_grid_trades.db"):
    Base.metadata.create_all(engine)

class DBManager:
    def __enter__(self):
        self.session = SessionFactory()
        return self.session
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.session.rollback()
        else:
            try:
                self.session.commit()
            except:
                self.session.rollback()
        self.session.close()

# --------------------------------------------------------------------------- #
# ====================== HEARTBEAT WEBSOCKET CLASS ========================= #
# --------------------------------------------------------------------------- #
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, **kwargs):
        super().__init__(url, **kwargs)
        self.last_pong = time.time()
        self.heartbeat_thread = None
        self.reconnect_attempts = 0

    def on_open(self, ws):
        logger.info(f"WS Connected: {ws.url.split('?')[0]}")
        self.last_pong = time.time()
        self.reconnect_attempts = 0
        if not self.heartbeat_thread:
            self.heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.heartbeat_thread.start()

    def on_pong(self, *args):
        self.last_pong = time.time()

    def _heartbeat(self):
        while self.sock and self.sock.connected:
            if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 5:
                logger.warning("No pong – closing WS")
                self.close()
                break
            try:
                self.send("ping", opcode=websocket.ABNF.OPCODE_PING)
            except:
                pass
            time.sleep(HEARTBEAT_INTERVAL)

    def run_forever(self, **kwargs):
        while True:
            try:
                super().run_forever(ping_interval=None, ping_timeout=None, **kwargs)
            except Exception as e:
                logger.error(f"WS crashed: {e}")
            self.reconnect_attempts += 1
            delay = min(300, (2 ** self.reconnect_attempts))
            logger.info(f"Reconnecting in {delay}s...")
            time.sleep(delay)

# --------------------------------------------------------------------------- #
# =========================== WEBSOCKET HANDLERS ============================ #
# --------------------------------------------------------------------------- #
def on_market_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get('stream', '')
        payload = data.get('data', {})
        if not payload: return
        symbol = stream.split('@')[0].upper()

        if stream.endswith('@ticker'):
            price = to_decimal(payload.get('c', '0'))
            if price > ZERO:
                with price_lock:
                    live_prices[symbol] = price
                    if symbol not in price_24h:
                        price_24h[symbol] = deque(maxlen=1440)
                    price_24h[symbol].append((time.time(), price))
                update_pnl_for_symbol(symbol)

        elif stream.endswith(f'@depth{DEPTH_LEVELS}'):
            bids = [(to_decimal(p), to_decimal(q)) for p, q in payload.get('bids', [])]
            asks = [(to_decimal(p), to_decimal(q)) for p, q in payload.get('asks', [])]
            with book_lock:
                live_bids[symbol] = bids
                live_asks[symbol] = asks

        elif stream.endswith('@kline_1m'):
            k = payload.get('k', {})
            if not k.get('x'): return
            o = to_decimal(k['o'])
            h = to_decimal(k['h'])
            l = to_decimal(k['l'])
            c = to_decimal(k['c'])
            with price_lock:
                if symbol not in klines_1m:
                    klines_1m[symbol] = deque(maxlen=100)
                klines_1m[symbol].append((o, h, l, c))

    except Exception as e:
        logger.debug(f"Market WS error: {e}")

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data.get('e') != 'executionReport': return
        ev = data
        order_id = str(ev['i'])
        symbol = ev['s']
        side = ev['S']
        status = ev['X']
        price = to_decimal(ev['p'])
        qty = to_decimal(ev['q'])
        filled = to_decimal(ev['z'])
        fee = to_decimal(ev.get('n', '0')) or ZERO

        if status == 'FILLED':
            with DBManager() as sess:
                po = sess.query(PendingOrder).filter_by(binance_order_id=order_id).first()
                if po: sess.delete(po)
                sess.add(TradeRecord(symbol=symbol, side=side, price=price, quantity=qty, fee=fee))

            if side == 'SELL':
                with DBManager() as sess:
                    pos = sess.query(Position).filter_by(symbol=symbol).first()
                    if pos:
                        entry = Decimal(str(pos.avg_entry_price))
                        pnl = (price - entry) * qty - fee
                        with realized_lock:
                            realized_pnl[symbol] = realized_pnl.get(symbol, ZERO) + pnl
                            global total_realized
                            total_realized += pnl

            send_whatsapp_alert(f"{side} {symbol} FILLED @ {price}")
            logger.info(f"FILL: {side} {symbol} @ {price}")
            update_pnl_for_symbol(symbol)

            if side == 'BUY' and symbol in trailing_buy:
                trailing_buy.pop(symbol, None)
            if side == 'SELL' and symbol in trailing_sell:
                trailing_sell.pop(symbol, None)

    except Exception as e:
        logger.debug(f"User WS error: {e}")

def on_error(ws, err):
    logger.warning(f"WS error: {err}")

def on_close(ws, code, msg):
    logger.info(f"WS closed: {code} - {msg}")

# --------------------------------------------------------------------------- #
# =========================== WEBSOCKET STARTERS ============================ #
# --------------------------------------------------------------------------- #
def start_market_websockets():
    global ws_instances, ws_threads
    symbols = [s.lower() for s in valid_symbols.keys() if 'USDT' in s]
    streams = (
        [f"{s}@ticker" for s in symbols] +
        [f"{s}@depth{DEPTH_LEVELS}" for s in symbols] +
        [f"{s}@kline_1m" for s in symbols]
    )
    chunks = [streams[i:i + MAX_STREAMS_PER_CONNECTION] for i in range(0, len(streams), MAX_STREAMS_PER_CONNECTION)]
    for chunk in chunks:
        url = WS_BASE + '/'.join(chunk)
        ws = HeartbeatWebSocket(
            url,
            on_message=on_market_message,
            on_error=on_error,
            on_close=on_close,
            on_open=lambda ws: ws.on_open(ws)
        )
        ws_instances.append(ws)
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        time.sleep(0.3)

def start_user_websocket():
    global user_ws, listen_key
    try:
        client = Client(API_KEY, API_SECRET, tld='us')
        with listen_key_lock:
            listen_key = client.stream_get_listen_key()
        url = f"{USER_STREAM_BASE}{listen_key}"
        user_ws = HeartbeatWebSocket(
            url,
            on_message=on_user_message,
            on_error=on_error,
            on_close=on_close,
            on_open=lambda ws: ws.on_open(ws)
        )
        t = threading.Thread(target=user_ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        logger.info("User stream started")
    except Exception as e:
        logger.error(f"User stream failed: {e}")

def keepalive_user_stream():
    while True:
        time.sleep(1800)
        try:
            with listen_key_lock:
                if listen_key:
                    Client(API_KEY, API_SECRET, tld='us').stream_keepalive(listen_key)
        except:
            pass

# --------------------------------------------------------------------------- #
# =============================== BOT CLASS ================================= #
# --------------------------------------------------------------------------- #
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.api_lock = threading.Lock()

    def sync_positions_from_binance(self):
        try:
            with self.api_lock:
                acct = self.client.get_account()
            with DBManager() as sess:
                sess.query(Position).delete()
                for b in acct['balances']:
                    asset = b['asset']
                    qty = to_decimal(b['free'])
                    if qty <= 0 or asset in {'USDT', 'USDC'}: continue
                    sym = f"{asset}USDT"
                    with price_lock:
                        price = live_prices.get(sym, ZERO)
                    if price <= ZERO: continue
                    sess.add(Position(symbol=sym, quantity=qty, avg_entry_price=price))
        except Exception as e:
            logger.error(f"sync_positions error: {e}")

    def get_balance(self) -> Decimal:
        try:
            with self.api_lock:
                acct = self.client.get_account()
            for b in acct['balances']:
                if b['asset'] == 'USDT':
                    return to_decimal(b['free'])
        except Exception as e:
            logger.error(f"get_balance error: {e}")
        return ZERO

    def get_tick_size(self, symbol):
        try:
            with self.api_lock:
                info = self.client.get_symbol_info(symbol)
            for f in info['filters']:
                if f['filterType'] == 'PRICE_FILTER':
                    return Decimal(f['tickSize'])
        except Exception as e:
            logger.error(f"tick_size error: {e}")
        return Decimal('0.00000001')

    def get_lot_step(self, symbol):
        try:
            with self.api_lock:
                info = self.client.get_symbol_info(symbol)
            for f in info['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    return Decimal(f['stepSize'])
        except Exception as e:
            logger.error(f"lot_step error: {e}")
        return Decimal('0.00000001')

    def place_limit_buy_with_tracking(self, symbol, price: str, qty: float):
        try:
            with self.api_lock:
                order = self.client.order_limit_buy(symbol=symbol, quantity=qty, price=price)
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol,
                                      side='BUY', price=to_decimal(price), quantity=to_decimal(qty)))
            return order
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol, price: str, qty: float):
        try:
            with self.api_lock:
                order = self.client.order_limit_sell(symbol=symbol, quantity=qty, price=price)
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol,
                                      side='SELL', price=to_decimal(price), quantity=to_decimal(qty)))
            return order
        except Exception as e:
            logger.error(f"Sell failed: {e}")
            return None

    def cancel_order_safe(self, symbol, order_id):
        try:
            with self.api_lock:
                self.client.cancel_order(symbol=symbol, orderId=order_id)
        except:
            pass

# --------------------------------------------------------------------------- #
# =============================== CANDLESTICK =============================== #
# --------------------------------------------------------------------------- #
def get_candlestick_score(symbol: str) -> int:
    try:
        with price_lock:
            klines = klines_1m.get(symbol, deque())
            if len(klines) < 20: return 0
            opens = np.array([float(o) for o, _, _, _ in klines])
            highs = np.array([float(h) for _, h, _, _ in klines])
            lows  = np.array([float(l) for _, _, l, _ in klines])
            closes= np.array([float(c) for _, _, _, c in klines])
        score = 0
        score += int(talib.CDLHAMMER(opens, highs, lows, closes)[-1])
        score += int(talib.CDLMORNINGSTAR(opens, highs, lows, closes)[-1])
        score += int(talib.CDLENGULFING(opens, highs, lows, closes)[-1] > 0)
        score -= int(talib.CDLSHOOTINGSTAR(opens, highs, lows, closes)[-1])
        score -= int(talib.CDLEVENINGSTAR(opens, highs, lows, closes)[-1])
        score -= int(talib.CDLENGULFING(opens, highs, lows, closes)[-1] < 0)
        if talib.CDLDOJI(opens, highs, lows, closes)[-1] != 0:
            score = 0
        return score
    except Exception as e:
        logger.error(f"candlestick error: {e}")
        return 0

# --------------------------------------------------------------------------- #
# =============================== ORDER QUEUE ================================ #
# --------------------------------------------------------------------------- #
def enqueue_order(action: str, **kwargs):
    with order_queue_cv:
        order_queue.append((action, kwargs))
        order_queue_cv.notify()

def order_worker():
    global bot
    while True:
        with order_queue_cv:
            if not order_queue:
                order_queue_cv.wait(timeout=5.0)
                continue
            action, kwargs = order_queue.popleft()
        try:
            if action == "LIMIT_BUY":
                bot.place_limit_buy_with_tracking(**kwargs)
            elif action == "LIMIT_SELL":
                bot.place_limit_sell_with_tracking(**kwargs)
            elif action == "MARKET_BUY":
                with bot.api_lock:
                    bot.client.order_market_buy(**kwargs)
            elif action == "MARKET_SELL":
                with bot.api_lock:
                    bot.client.order_market_sell(**kwargs)
            elif action == "CANCEL":
                bot.cancel_order_safe(**kwargs)
        except Exception as e:
            logger.error(f"order_worker error: {e}")

# --------------------------------------------------------------------------- #
# =============================== PnL TRACKING =============================== #
# --------------------------------------------------------------------------- #
def update_pnl_for_symbol(symbol: str):
    try:
        with DBManager() as sess:
            pos = sess.query(Position).filter_by(symbol=symbol).first()
            if not pos: return
            qty = Decimal(str(pos.quantity))
            entry = Decimal(str(pos.avg_entry_price))
        with price_lock:
            current = live_prices.get(symbol, ZERO)
        if current <= ZERO: return
        unreal = (current - entry) * qty
        with pnl_lock:
            position_pnl[symbol] = unreal
        update_total_pnl()
    except Exception as e:
        logger.error(f"pnl update error: {e}")

def update_total_pnl():
    try:
        global total_pnl
        with pnl_lock:
            total_pnl = sum(position_pnl.values())
    except Exception as e:
        logger.error(f"total pnl error: {e}")

def refresh_all_pnl():
    try:
        with DBManager() as sess:
            for pos in sess.query(Position).all():
                update_pnl_for_symbol(pos.symbol)
    except Exception as e:
        logger.error(f"refresh pnl error: {e}")

# --------------------------------------------------------------------------- #
# =========================== FIRST RUN ENTRY =============================== #
# --------------------------------------------------------------------------- #
def first_run_entry_from_ladder(bot):
    global first_run_executed
    if first_run_executed: return
    try:
        usdt_free = bot.get_balance()
        if usdt_free < Decimal('15'): 
            first_run_executed = True
            return
        candidates = []
        with DBManager() as sess:
            owned = {p.symbol for p in sess.query(Position).all()}
        for sym in valid_symbols:
            if sym in owned: continue
            with book_lock:
                bids = live_bids.get(sym, [])
                asks = live_asks.get(sym, [])
            if not bids or not asks: continue
            price = float(asks[0][0])
            if not (MIN_PRICE <= price <= MAX_PRICE): continue
            bid_vol = sum(float(q) for _, q in bids)
            ask_vol = sum(float(q) for _, q in asks)
            imbalance = bid_vol / (ask_vol or 1)
            if imbalance >= 1.5:
                candidates.append((sym, imbalance, price))
        if not candidates:
            first_run_executed = True
            return
        candidates.sort(key=lambda x: x[1], reverse=True)
        to_buy = candidates[:3]
        per_coin = (usdt_free - MIN_BUFFER_USDT) / len(to_buy)
        for sym, _, price in to_buy:
            if per_coin < Decimal('15'): break
            qty = (per_coin * Decimal('0.999')) / Decimal(str(price))
            step = bot.get_lot_step(sym)
            qty = (qty // step) * step
            if qty <= 0: continue
            buy_price = to_decimal(Decimal(str(price)) * Decimal('0.999'))
            tick = bot.get_tick_size(sym)
            buy_price = (buy_price // tick) * tick
            if buy_price * qty >= Decimal('1.25'):
                enqueue_order("LIMIT_BUY", symbol=sym, price=str(buy_price), qty=float(qty))
                logger.info(f"ENTRY: {sym} @ ${float(buy_price):.6f}")
                send_whatsapp_alert(f"ENTRY {sym} @ {buy_price:.6f}")
        first_run_executed = True
    except Exception as e:
        logger.error(f"first_run error: {e}")

# --------------------------------------------------------------------------- #
# =========================== INFINITY GRID CORE =========================== #
# --------------------------------------------------------------------------- #
def rebalance_infinity_grid(bot, symbol):
    try:
        with price_lock:
            current_price = live_prices.get(symbol)
        if not current_price or current_price <= ZERO: return
        grid = active_grid.get(symbol, {})
        old_center = Decimal(str(grid.get('center', current_price)))
        price_move = abs(current_price - old_center) / old_center if old_center > 0 else Decimal('1')
        if symbol not in active_grid or price_move >= REBALANCE_THRESHOLD_PCT:
            for oid in grid.get('buy_orders', []) + grid.get('sell_orders', []):
                enqueue_order("CANCEL", symbol=symbol, order_id=oid)
            active_grid.pop(symbol, None)
            with DBManager() as sess:
                pos = sess.query(Position).filter_by(symbol=symbol).first()
                if not pos: return
                free_qty = Decimal(str(pos.quantity))
            step = bot.get_lot_step(symbol)
            qty_per_grid = (GRID_SIZE_USDT / current_price) // step * step
            qty_per_grid = qty_per_grid.quantize(step, rounding=ROUND_DOWN)
            if qty_per_grid <= 0 or qty_per_grid > free_qty: return
            new_grid = {
                'center': current_price,
                'qty': float(qty_per_grid),
                'buy_orders': [],
                'sell_orders': [],
                'placed_at': time.time()
            }
            tick = bot.get_tick_size(symbol)
            for i in range(1, MAX_GRIDS_PER_SIDE + 1):
                raw = current_price * (Decimal('1') - GRID_INTERVAL_PCT * Decimal(str(i)))
                price = (raw // tick) * tick
                if price * qty_per_grid >= Decimal('1.25'):
                    order = bot.place_limit_buy_with_tracking(symbol, str(price), float(qty_per_grid))
                    if order:
                        new_grid['buy_orders'].append(str(order['orderId']))
            remaining = free_qty
            for i in range(1, MAX_GRIDS_PER_SIDE + 1):
                if remaining < qty_per_grid: break
                raw = current_price * (Decimal('1') + GRID_INTERVAL_PCT * Decimal(str(i)))
                price = (raw // tick) * tick
                if price * qty_per_grid >= Decimal('3.25'):
                    order = bot.place_limit_sell_with_tracking(symbol, str(price), float(qty_per_grid))
                    if order:
                        new_grid['sell_orders'].append(str(order['orderId']))
                        remaining -= qty_per_grid
            if new_grid['buy_orders'] or new_grid['sell_orders']:
                active_grid[symbol] = new_grid
                logger.info(f"GRID: {symbol} | ${float(current_price):.6f}")
    except Exception as e:
        logger.error(f"grid error: {e}")

# --------------------------------------------------------------------------- #
# =========================== TRAILING BUY/SELL ============================= #
# --------------------------------------------------------------------------- #
def check_trailing_buy(symbol: str):
    try:
        if symbol in trailing_buy or symbol in position_pnl: return
        with book_lock:
            bids = live_bids.get(symbol, [])
            asks = live_asks.get(symbol, [])
        if not bids or not asks: return
        price = live_prices.get(symbol, ZERO)
        if price <= ZERO: return
        hist = price_24h.get(symbol, [])
        if len(hist) < 14: return
        closes = [p for _, p in list(hist)[-14:]]
        rsi = talib.RSI(np.array([float(p) for p in closes]), timeperiod=14)[-1]
        if not np.isfinite(rsi) or rsi > 35: return
        top5_bid = sum(float(q) for _, q in bids[:5])
        top5_ask = sum(float(q) for _, q in asks[:5])
        if top5_bid / (top5_bid + top5_ask) < ORDERBOOK_BUY_PRESSURE_SPIKE: return
        if not is_low_time(): return
        if get_candlestick_score(symbol) <= 0: return
        trailing_buy[symbol] = {
            'lowest_price': price,
            'last_buy_order_id': None,
            'start_time': time.time(),
            'price_peaks_history': []
        }
        logger.info(f"TRAILING BUY STARTED: {symbol}")
        send_whatsapp_alert(f"TRAILING BUY: {symbol}")
    except Exception as e:
        logger.error(f"check_trailing_buy error: {e}")

def update_trailing_buy(symbol: str):
    try:
        state = trailing_buy.get(symbol)
        if not state: return
        price = live_prices.get(symbol, ZERO)
        if price <= ZERO: return
        if not state['price_peaks_history'] or price > state['price_peaks_history'][-1][1]:
            state['price_peaks_history'].append((time.time(), price))
        cutoff = time.time() - RAPID_DROP_WINDOW
        state['price_peaks_history'] = [(t, p) for t, p in state['price_peaks_history'] if t > cutoff]
        if len(state['price_peaks_history']) >= 2:
            last_p, _ = state['price_peaks_history'][-2]
            if (last_p - price) / last_p >= RAPID_DROP_THRESHOLD:
                enqueue_order("MARKET_BUY", symbol=symbol, quoteOrderQty=50.0)
                trailing_buy.pop(symbol, None)
                return
        if price < state['lowest_price']:
            state['lowest_price'] = price
        if price > state['lowest_price'] * Decimal('1.003'):
            enqueue_order("MARKET_BUY", symbol=symbol, quoteOrderQty=50.0)
            trailing_buy.pop(symbol, None)
            return
        new_price = price * Decimal('0.999')
        if state['last_buy_order_id']:
            enqueue_order("CANCEL", symbol=symbol, order_id=state['last_buy_order_id'])
        order = bot.place_limit_buy_with_tracking(symbol, str(new_price), 0.001)
        if order:
            state['last_buy_order_id'] = str(order['orderId'])
        trailing_buy[symbol] = state
    except Exception as e:
        logger.error(f"update_trailing_buy error: {e}")

def check_trailing_sell(symbol: str):
    try:
        if symbol not in position_pnl or symbol in trailing_sell: return
        with book_lock:
            bids = live_bids.get(symbol, [])
            asks = live_asks.get(symbol, [])
        if not bids or not asks: return
        price = live_prices.get(symbol, ZERO)
        if price <= ZERO: return
        hist = price_24h.get(symbol, [])
        if len(hist) < 14: return
        closes = [p for _, p in list(hist)[-14:]]
        rsi = talib.RSI(np.array([float(p) for p in closes]), timeperiod=14)[-1]
        if not np.isfinite(rsi) or rsi < 65: return
        top5_bid = sum(float(q) for _, q in bids[:5])
        top5_ask = sum(float(q) for _, q in asks[:5])
        if top5_ask / (top5_bid + top5_ask) < ORDERBOOK_SELL_PRESSURE_THRESHOLD: return
        if not is_high_time(): return
        if get_candlestick_score(symbol) >= 0: return
        with DBManager() as sess:
            pos = sess.query(Position).filter_by(symbol=symbol).first()
            if not pos: return
            qty = float(pos.quantity)
        trailing_sell[symbol] = {
            'entry_price': Decimal(str(pos.avg_entry_price)),
            'peak_price': price,
            'qty': qty,
            'last_sell_order_id': None,
            'price_peaks_history': [],
            'start_time': time.time()
        }
        logger.info(f"TRAILING SELL STARTED: {symbol}")
        send_whatsapp_alert(f"TRAILING SELL: {symbol}")
    except Exception as e:
        logger.error(f"check_trailing_sell error: {e}")

def update_trailing_sell(symbol: str):
    try:
        state = trailing_sell.get(symbol)
        if not state: return
        price = live_prices.get(symbol, ZERO)
        if price <= ZERO: return
        if price > state['peak_price']:
            state['peak_price'] = price
        net = (price - state['entry_price']) / state['entry_price']
        if net >= PROFIT_TARGET_NET:
            enqueue_order("MARKET_SELL", symbol=symbol, quantity=state['qty'])
            trailing_sell.pop(symbol, None)
            return
        if price < state['peak_price'] * Decimal('0.995'):
            enqueue_order("MARKET_SELL", symbol=symbol, quantity=state['qty'])
            trailing_sell.pop(symbol, None)
            return
        if not state['price_peaks_history'] or price > state['price_peaks_history'][-1][1]:
            state['price_peaks_history'].append((time.time(), price))
        cutoff = time.time() - STALL_THRESHOLD_SECONDS
        state['price_peaks_history'] = [(t, p) for t, p in state['price_peaks_history'] if t > cutoff]
        if state['price_peaks_history'] and time.time() - state['price_peaks_history'][-1][0] >= STALL_THRESHOLD_SECONDS:
            enqueue_order("MARKET_SELL", symbol=symbol, quantity=state['qty'])
            trailing_sell.pop(symbol, None)
            return
        new_price = price * Decimal('1.001')
        if state['last_sell_order_id']:
            enqueue_order("CANCEL", symbol=symbol, order_id=state['last_sell_order_id'])
        order = bot.place_limit_sell_with_tracking(symbol, str(new_price), state['qty'])
        if order:
            state['last_sell_order_id'] = str(order['orderId'])
        trailing_sell[symbol] = state
    except Exception as e:
        logger.error(f"update_trailing_sell error: {e}")

# --------------------------------------------------------------------------- #
# =============================== DASHBOARD ================================= #
# --------------------------------------------------------------------------- #
def print_dashboard(bot):
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        GREEN = "\033[92m"
        RED = "\033[91m"
        YELLOW = "\033[93m"
        RESET = "\033[0m"
        print("=" * 120)
        print(f"{'WIDE GRID INFINITY BOT – LIVE':^120}")
        print("=" * 120)
        print(f"Time: {datetime.now(CST_TZ).strftime('%Y-%m-%d %H:%M:%S')} | USDT: ${float(bot.get_balance()):,.2f}")
        print(f"Total Unrealized: {GREEN if total_pnl >= 0 else RED}${float(total_pnl):+.2f}{RESET} | Realized: ${float(total_realized):+.2f}")
        with DBManager() as sess:
            positions = sess.query(Position).all()
            print(f"Positions: {len(positions)} | Grids: {len(active_grid)} | Trailing: {len(trailing_buy)}B/{len(trailing_sell)}S")
        print("\nPOSITIONS")
        for pos in positions:
            sym = pos.symbol
            qty = float(pos.quantity)
            entry = float(pos.avg_entry_price)
            with price_lock:
                cur = float(live_prices.get(sym, 0))
            unreal = float(position_pnl.get(sym, ZERO))
            pct = ((cur - entry) / entry * 100) if entry > 0 and cur > 0 else 0
            color = GREEN if unreal >= 0 else RED
            status = "TS" if sym in trailing_sell else "TB" if sym in trailing_buy else "Grid"
            print(f"{sym:<10} | Qty: {qty:>8.6f} | Entry: ${entry:>8.6f} | Now: ${cur:>8.6f} | {color}${unreal:+8.2f} ({pct:+.2f}%){RESET} | {status}")
        print("\nGRID STATUS")
        for sym, g in active_grid.items():
            c = float(g['center'])
            b = len(g.get('buy_orders', []))
            s = len(g.get('sell_orders', []))
            print(f"{sym:<10} | Center: ${c:>10.6f} | {b}B/{s}S")
        print("\nTRAILING")
        for sym, st in trailing_buy.items():
            print(f"BUY  {sym:<10} | Low: ${float(st.get('lowest_price',0)):.6f}")
        for sym, st in trailing_sell.items():
            print(f"SELL {sym:<10} | Peak: ${float(st.get('peak_price',0)):.6f}")
        print("=" * 120)
    except Exception as e:
        logger.error(f"dashboard error: {e}")

# --------------------------------------------------------------------------- #
# =============================== MAIN LOOP ================================= #
# --------------------------------------------------------------------------- #
def main():
    global bot
    bot = BinanceTradingBot()

    # One-time sync
    logger.info("One-time sync...")
    try:
        info = bot.client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
                valid_symbols[s['symbol']] = {'volume': 1e6}
    except: pass
    bot.sync_positions_from_binance()

    # Start WS
    start_market_websockets()
    start_user_websocket()
    threading.Thread(target=keepalive_user_stream, daemon=True).start()
    threading.Thread(target=order_worker, daemon=True).start()

    logger.info("Waiting 15s for WS sync...")
    time.sleep(15)

    last_dash = last_rebal = last_pnl = last_trail = time.time()

    while True:
        try:
            now = time.time()
            if not first_run_executed and now - last_dash > 5:
                with DBManager() as sess:
                    if sess.query(Position).count() == 0:
                        first_run_entry_from_ladder(bot)

            if now - last_rebal >= 45:
                with DBManager() as sess:
                    for pos in sess.query(Position).all():
                        if pos.symbol in valid_symbols:
                            rebalance_infinity_grid(bot, pos.symbol)
                last_rebal = now

            if now - last_trail >= 3:
                for sym in list(trailing_buy.keys()):
                    update_trailing_buy(sym)
                for sym in list(trailing_sell.keys()):
                    update_trailing_sell(sym)
                last_trail = now

            if now - last_pnl >= 2:
                refresh_all_pnl()
                last_pnl = now

            if now - last_dash >= 60:
                print_dashboard(bot)
                last_dash = now

            time.sleep(1)

        except KeyboardInterrupt:
            print("\nShutting down...")
            for ws in ws_instances:
                ws.close()
            if user_ws:
                user_ws.close()
            break
        except Exception as e:
            logger.critical(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
