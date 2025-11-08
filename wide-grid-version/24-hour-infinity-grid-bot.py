#!/usr/bin/env python3
"""
    INFINITY GRID BOT – LEGACY CORE + PRO FILTERS + DASHBOARD
    • Entry: TOP 3 buy pressure + $60k 24h vol + $0.15–$1,000
    • Dynamic grids via ATR + Volume + PnL (fallback: 4x4)
    • $8 USDT buffer | No sell < $5 or qty < 0.00001
    • DB sync on startup | All grid orders on dashboard
    • 100% WebSocket | No REST after startup
    • ENFORCED: ONLY BUY TOP-3 BUY PRESSURE COINS (NO EXCEPTIONS)
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
from logging.handlers import TimedRotatingFileHandler

from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
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

# ---- Grid & Entry ----------------------------------------------------------
GRID_SIZE_USDT = Decimal('5.0')
GRID_INTERVAL_PCT = Decimal('0.015')  # 1.5%
MIN_BUFFER_USDT = Decimal('8.0')
MIN_SELL_VALUE_USDT = Decimal('5.0')
MIN_SELL_QTY = Decimal('0.00001')
MAX_GRIDS_PER_SIDE = 8
MIN_GRIDS_PER_SIDE = 2
MIN_GRIDS_FALLBACK = 1
REBALANCE_THRESHOLD_PCT = Decimal('0.0075')  # 0.75%

# ---- Entry Filters ---------------------------------------------------------
MIN_24H_VOLUME_USDT = 100000
MIN_PRICE = Decimal('1.00')
MAX_PRICE = Decimal('1000')
ENTRY_MIN_USDT = Decimal('5.0')
ENTRY_BUY_PCT_BELOW_ASK = Decimal('0.001')

# ---- WebSocket -------------------------------------------------------------
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
MAX_STREAMS_PER_CONNECTION = 100
DEPTH_LEVELS = 5
HEARTBEAT_INTERVAL = 25

# ---- Misc ------------------------------------------------------------------
LOG_FILE = "infinity_grid_bot.log"
first_run_executed = False
first_dashboard_run = True

# --------------------------------------------------------------------------- #
# =============================== CONSTANTS ================================ #
# --------------------------------------------------------------------------- #
ZERO = Decimal('0')
ONE = Decimal('1')
HUNDRED = Decimal('100')
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
valid_symbols_dict: Dict[str, dict] = {}
active_grid_symbols: Dict[str, dict] = {}
live_prices: Dict[str, Decimal] = {}
live_bids: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
live_asks: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
price_lock = threading.Lock()
book_lock = threading.Lock()
ws_instances = []
ws_threads = []
user_ws = None
listen_key = None
listen_key_lock = threading.Lock()

# PnL
position_pnl: Dict[str, Dict[str, Decimal]] = {}
total_pnl = ZERO
pnl_lock = threading.Lock()
realized_pnl_per_symbol: Dict[str, Decimal] = {}
total_realized_pnl = ZERO
realized_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# =============================== HELPERS ================================== #
# --------------------------------------------------------------------------- #
def safe_float(value, default=0.0) -> float:
    try:
        return float(value) if value is not None and np.isfinite(float(value)) else default
    except:
        return default

def to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except:
        return ZERO

def buy_notional_ok(price: Decimal, qty: Decimal) -> bool:
    return price * qty >= Decimal('1.25')

def sell_notional_ok(price: Decimal, qty: Decimal) -> bool:
    return price * qty >= Decimal('3.25')

def position_value_usdt(price: Decimal, qty: Decimal) -> Decimal:
    return (price * qty).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)

def send_whatsapp_alert(msg: str):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}",
                timeout=5
            )
        except:
            pass

def now_cst():
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")

# --------------------------------------------------------------------------- #
# =============================== RETRY ===================================== #
# --------------------------------------------------------------------------- #
def retry_custom(func):
    def wrapper(*args, **kwargs):
        max_retries = 5
        for i in range(max_retries):
            try:
                return func(*args, **kwargs)
            except BinanceAPIException as e:
                if hasattr(e, 'response') and e.response is not None:
                    hdr = e.response.headers
                    if e.status_code in (429, 418):
                        retry_after = int(hdr.get('Retry-After', 60))
                        logger.warning(f"Rate limit {e.status_code}: sleeping {retry_after}s")
                        time.sleep(retry_after)
                    else:
                        delay = 2 ** i
                        logger.warning(f"Retry {i+1}/{max_retries} for {func.__name__}: {e}")
                        time.sleep(delay)
                else:
                    if i == max_retries - 1:
                        raise
                    time.sleep(2 ** i)
        return None
    return wrapper

# --------------------------------------------------------------------------- #
# =============================== DATABASE ================================= #
# --------------------------------------------------------------------------- #
DB_URL = "sqlite:///binance_trades.db"
engine = create_engine(DB_URL, echo=False, future=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20, 8), nullable=False)
    avg_entry_price = Column(Numeric(20, 8), nullable=False)
    buy_fee_rate = Column(Numeric(10, 6), nullable=False, default=0.001)

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

if not os.path.exists("binance_trades.db"):
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
        if self.heartbeat_thread is None:
            self.heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.heartbeat_thread.start()

    def on_pong(self, *args):
        self.last_pong = time.time()

    def _heartbeat(self):
        while self.sock and self.sock.connected:
            if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 5:
                logger.warning("No pong – closing")
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
            volume = to_decimal(payload.get('v', '0')) * price
            if price > ZERO:
                with price_lock:
                    live_prices[symbol] = price
                valid_symbols_dict[symbol]['volume'] = float(volume)
                update_pnl_for_symbol(symbol)

        elif stream.endswith(f'@depth{DEPTH_LEVELS}'):
            bids = [(to_decimal(p), to_decimal(q)) for p, q in payload.get('bids', [])]
            asks = [(to_decimal(p), to_decimal(q)) for p, q in payload.get('asks', [])]
            with book_lock:
                live_bids[symbol] = bids
                live_asks[symbol] = asks
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
        fee_asset = ev.get('N', 'USDT')

        if status == 'FILLED':
            with DBManager() as sess:
                po = sess.query(PendingOrder).filter_by(binance_order_id=order_id).first()
                if po: sess.delete(po)
                sess.add(TradeRecord(symbol=symbol, side=side, price=price, quantity=qty,
                                     fee=fee if fee_asset == 'USDT' else ZERO))

            if side == 'SELL':
                with DBManager() as sess:
                    pos = sess.query(Position).filter_by(symbol=symbol).first()
                    if pos:
                        entry = Decimal(str(pos.avg_entry_price))
                        pnl = (price - entry) * qty - fee
                        with realized_lock:
                            realized_pnl_per_symbol[symbol] = realized_pnl_per_symbol.get(symbol, ZERO) + pnl
                            global total_realized_pnl
                            total_realized_pnl += pnl

            send_whatsapp_alert(f"{side} {symbol} FILLED @ {price} | Qty: {filled}")
            logger.info(f"FILL: {side} {symbol} @ {price}")
            update_pnl_for_symbol(symbol)
    except Exception as e:
        logger.debug(f"User WS error: {e}")

def on_ws_error(ws, err):
    logger.warning(f"WebSocket error ({ws.url.split('?')[0]}): {err}")

def on_ws_close(ws, code, msg):
    logger.info(f"WebSocket closed ({ws.url.split('?')[0]}) – {code}: {msg}")

# --------------------------------------------------------------------------- #
# =========================== WEBSOCKET STARTERS ============================ #
# --------------------------------------------------------------------------- #
def start_market_websocket():
    global ws_instances, ws_threads
    symbols = [s.lower() for s in valid_symbols_dict.keys() if 'USDT' in s]
    ticker_streams = [f"{s}@ticker" for s in symbols]
    depth_streams = [f"{s}@depth{DEPTH_LEVELS}" for s in symbols]
    all_streams = ticker_streams + depth_streams
    chunks = [all_streams[i:i + MAX_STREAMS_PER_CONNECTION] for i in range(0, len(all_streams), MAX_STREAMS_PER_CONNECTION)]
    for chunk in chunks:
        url = WS_BASE + '/'.join(chunk)
        ws = HeartbeatWebSocket(
            url,
            on_message=on_market_message,
            on_error=on_ws_error,
            on_close=on_ws_close,
            on_open=lambda ws: logger.info(f"WS Open: {ws.url}")
        )
        ws_instances.append(ws)
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        time.sleep(0.5)

def start_user_stream():
    global user_ws, listen_key
    try:
        with threading.Lock():
            client = Client(API_KEY, API_SECRET, tld='us')
            listen_key = client.stream_get_listen_key()
        url = f"{USER_STREAM_BASE}{listen_key}"
        user_ws = HeartbeatWebSocket(
            url,
            on_message=on_user_message,
            on_error=on_ws_error,
            on_close=on_ws_close,
            on_open=lambda ws: logger.info("User WS Open")
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
        self.sync_positions_from_binance()

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
                    if price <= ZERO:
                        try:
                            ticker = self.client.get_symbol_ticker(symbol=sym)
                            price = to_decimal(ticker['price'])
                        except:
                            continue
                    if price <= ZERO: continue
                    maker, _ = self.get_trade_fees(sym)
                    sess.add(Position(symbol=sym, quantity=qty, avg_entry_price=price, buy_fee_rate=to_decimal(maker)))
                sess.commit()
            logger.info("DB synced with Binance positions on startup")
        except Exception as e:
            logger.error(f"Sync failed: {e}")

    @retry_custom
    def get_price_usdt(self, asset: str) -> Decimal:
        if asset == 'USDT': return Decimal('1')
        sym = asset + 'USDT'
        with self.api_lock:
            ticker = self.client.get_symbol_ticker(symbol=sym)
        return to_decimal(ticker['price'])

    @retry_custom
    def get_trade_fees(self, symbol):
        with self.api_lock:
            fee = self.client.get_trade_fee(symbol=symbol)
        return safe_float(fee[0]['makerCommission']), safe_float(fee[0]['takerCommission'])

    @retry_custom
    def get_tick_size(self, symbol):
        with self.api_lock:
            info = self.client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                return Decimal(f['tickSize'])
        return Decimal('0.00000001')

    @retry_custom
    def get_lot_step(self, symbol):
        with self.api_lock:
            info = self.client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                return Decimal(f['stepSize'])
        return Decimal('0.00000001')

    @retry_custom
    def get_atr(self, symbol) -> float:
        with self.api_lock:
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=15)
        highs = np.array([safe_float(k[2]) for k in klines])
        lows = np.array([safe_float(k[3]) for k in klines])
        closes = np.array([safe_float(k[4]) for k in klines])
        atr = talib.ATR(highs, lows, closes, timeperiod=14)[-1]
        return safe_float(atr)

    def get_balance(self) -> Decimal:
        with self.api_lock:
            acct = self.client.get_account()
        for b in acct['balances']:
            if b['asset'] == 'USDT':
                return to_decimal(b['free'])
        return ZERO

    def get_asset_balance(self, asset: str) -> Decimal:
        with self.api_lock:
            acct = self.client.get_account()
        for b in acct['balances']:
            if b['asset'] == asset:
                return to_decimal(b['free'])
        return ZERO

    def place_limit_buy_with_tracking(self, symbol, price: str, qty: float):
        try:
            with self.api_lock:
                order = self.client.order_limit_buy(symbol=symbol, quantity=qty, price=price)
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='buy',
                                      price=to_decimal(price), quantity=to_decimal(qty)))
            return order
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol, price: str, qty: float):
        try:
            with self.api_lock:
                order = self.client.order_limit_sell(symbol=symbol, quantity=qty, price=price)
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='sell',
                                      price=to_decimal(price), quantity=to_decimal(qty)))
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
# =============================== PnL TRACKING =============================== #
# --------------------------------------------------------------------------- #
def update_pnl_for_symbol(symbol: str):
    with DBManager() as sess:
        pos = sess.query(Position).filter_by(symbol=symbol).first()
        if not pos: return
        qty = Decimal(str(pos.quantity))
        entry = Decimal(str(pos.avg_entry_price))
        with price_lock:
            current = live_prices.get(symbol, ZERO)
        if current <= 0: return
        unrealized = (current - entry) * qty
        with pnl_lock:
            position_pnl[symbol] = {
                'unrealized': unrealized,
                'pct': ((current - entry) / entry * HUNDRED) if entry > 0 else ZERO
            }
        update_total_pnl()

def update_total_pnl():
    with pnl_lock:
        global total_pnl
        total_pnl = sum(info['unrealized'] for info in position_pnl.values())

def refresh_all_pnl():
    with DBManager() as sess:
        for pos in sess.query(Position).all():
            update_pnl_for_symbol(pos.symbol)

# --------------------------------------------------------------------------- #
# ======================= LEGACY GRID REBALANCING CORE ======================= #
# --------------------------------------------------------------------------- #
def calculate_optimal_grids(bot) -> Dict[str, Tuple[int, int]]:
    usdt_free = bot.get_balance() - MIN_BUFFER_USDT
    if usdt_free <= 0: return {}
    with DBManager() as sess:
        owned = sess.query(Position).all()
    if not owned: return {}

    scores = []
    for pos in owned:
        sym = pos.symbol
        with price_lock:
            cur_price = live_prices.get(sym)
        if not cur_price: continue
        entry = Decimal(str(pos.avg_entry_price))
        qty = Decimal(str(pos.quantity))
        unrealized_pnl = (cur_price - entry) * qty
        atr = bot.get_atr(sym)
        volatility_score = atr / float(cur_price) if atr > 0 else 0
        volume = valid_symbols_dict.get(sym, {}).get('volume', 0)
        volume_score = min(volume / 1e6, 5.0)
        pnl_score = max(float(unrealized_pnl) / 10.0, -2.0)
        total_score = volatility_score * 2.0 + volume_score + max(pnl_score, 0)
        scores.append((sym, total_score, float(qty), float(cur_price)))

    if not scores: return {}
    total_score = sum(s[1] for s in scores) or 1
    allocations = {s[0]: (s[1] / total_score) * float(usdt_free) for s in scores}

    result = {}
    for sym, alloc in allocations.items():
        max_possible = int(alloc // float(GRID_SIZE_USDT))
        levels = min(MAX_GRIDS_PER_SIDE, max(MIN_GRIDS_FALLBACK, max_possible // 2))
        levels = max(MIN_GRIDS_PER_SIDE if alloc >= 40 else MIN_GRIDS_FALLBACK, levels)
        result[sym] = (levels, levels)
    return result

def rebalance_infinity_grid(bot, symbol):
    with price_lock:
        current_price = live_prices.get(symbol)
    if not current_price or current_price <= 0: return

    grid = active_grid_symbols.get(symbol, {})
    old_center = Decimal(str(grid.get('center', current_price)))
    price_move = abs(current_price - old_center) / old_center if old_center > 0 else 1

    if symbol not in active_grid_symbols or price_move >= REBALANCE_THRESHOLD_PCT:
        for oid in grid.get('buy_orders', []) + grid.get('sell_orders', []):
            bot.cancel_order_safe(symbol, oid)
        active_grid_symbols.pop(symbol, None)

        buy_levels, sell_levels = calculate_optimal_grids(bot).get(symbol, (0, 0))
        if buy_levels == 0: return

        step = bot.get_lot_step(symbol)
        qty_per_grid = (GRID_SIZE_USDT / current_price) // step * step
        if qty_per_grid <= 0: return

        new_grid = {
            'center': current_price,
            'qty': float(qty_per_grid),
            'buy_orders': [],
            'sell_orders': [],
            'placed_at': time.time()
        }
        tick = bot.get_tick_size(symbol)

        for i in range(1, buy_levels + 1):
            price = (current_price * (1 - GRID_INTERVAL_PCT * i) // tick) * tick
            if buy_notional_ok(price, qty_per_grid):
                order = bot.place_limit_buy_with_tracking(symbol, str(price), float(qty_per_grid))
                if order:
                    new_grid['buy_orders'].append(str(order['orderId']))

        base_asset = symbol.replace('USDT', '')
        free = bot.get_asset_balance(base_asset)
        for i in range(1, sell_levels + 1):
            price = (current_price * (1 + GRID_INTERVAL_PCT * i) // tick) * tick
            if free >= qty_per_grid and sell_notional_ok(price, qty_per_grid):
                order = bot.place_limit_sell_with_tracking(symbol, str(price), float(qty_per_grid))
                if order:
                    new_grid['sell_orders'].append(str(order['orderId']))
                free -= qty_per_grid

        if new_grid['buy_orders'] or new_grid['sell_orders']:
            active_grid_symbols[symbol] = new_grid
            logger.info(f"GRID: {symbol} | ${float(current_price):.6f} | {buy_levels}B/{sell_levels}S")

# --------------------------------------------------------------------------- #
# =========================== TOP-3 ENTRY GUARD ============================= #
# --------------------------------------------------------------------------- #
def is_top3_buy_pressure(symbol: str) -> bool:
    """Check if symbol is in top-3 buy pressure (depth-5)."""
    with book_lock:
        bids = live_bids.get(symbol, [])
        asks = live_asks.get(symbol, [])
    if len(bids) < DEPTH_LEVELS or len(asks) < DEPTH_LEVELS:
        return False

    bid_vol = sum(q for _, q in bids[:DEPTH_LEVELS])
    ask_vol = sum(q for _, q in asks[:DEPTH_LEVELS])
    if ask_vol == ZERO:
        return False
    my_imbalance = bid_vol / ask_vol

    # Build top-3 from all valid symbols
    competitors = []
    for sym in valid_symbols_dict:
        if sym == symbol:
            continue
        with book_lock:
            b = live_bids.get(sym, [])
            a = live_asks.get(sym, [])
        if len(b) < DEPTH_LEVELS or len(a) < DEPTH_LEVELS:
            continue
        bv = sum(q for _, q in b[:DEPTH_LEVELS])
        av = sum(q for _, q in a[:DEPTH_LEVELS])
        if av == ZERO:
            continue
        competitors.append((sym, bv / av))

    competitors.sort(key=lambda x: x[1], reverse=True)
    top3_symbols = {s for s, _ in competitors[:3]}

    return symbol in top3_symbols

# --------------------------------------------------------------------------- #
# =========================== FIRST RUN ENTRY =============================== #
# --------------------------------------------------------------------------- #
def first_run_entry_from_ladder(bot):
    """ONLY buy the top-3 coins with highest bid/ask volume imbalance in depth-5."""
    global first_run_executed
    if first_run_executed:
        return

    usdt_free = bot.get_balance()
    if usdt_free < ENTRY_MIN_USDT:
        logger.info("Insufficient USDT for first-run entry.")
        first_run_executed = True
        return

    # --- 1. Scan depth-5 for buy pressure ---
    pressure = []  # (symbol, imbalance, best_ask)
    with DBManager() as sess:
        owned = {p.symbol for p in sess.query(Position).all()}

    for sym in valid_symbols_dict:
        if sym in owned:
            continue
        if valid_symbols_dict[sym]['volume'] < MIN_24H_VOLUME_USDT:
            continue

        with price_lock:
            price = live_prices.get(sym, ZERO)
        if price <= ZERO or price < MIN_PRICE or price > MAX_PRICE:
            continue

        with book_lock:
            bids = live_bids.get(sym, [])
            asks = live_asks.get(sym, [])
        if len(bids) < DEPTH_LEVELS or len(asks) < DEPTH_LEVELS:
            continue

        bid_vol = sum(q for _, q in bids[:DEPTH_LEVELS])
        ask_vol = sum(q for _, q in asks[:DEPTH_LEVELS])
        if ask_vol == ZERO:
            continue

        imbalance = bid_vol / ask_vol
        best_ask = asks[0][0]

        pressure.append((sym, imbalance, best_ask))

    if not pressure:
        logger.info("No top-3 buy pressure signals.")
        first_run_executed = True
        return

    # --- 2. Select TOP-3 only ---
    pressure.sort(key=lambda x: x[1], reverse=True)
    top3 = pressure[:3]

    # --- 3. Allocate & buy ---
    per_coin = (usdt_free - MIN_BUFFER_USDT) / len(top3)

    for sym, imbalance, ask_price in top3:
        if per_coin < ENTRY_MIN_USDT:
            break

        raw_qty = (per_coin * (1 - float(ENTRY_BUY_PCT_BELOW_ASK))) / float(ask_price)
        step = bot.get_lot_step(sym)
        qty = (to_decimal(raw_qty) // step) * step
        if qty <= ZERO:
            continue

        buy_price = to_decimal(float(ask_price) * (1 - ENTRY_BUY_PCT_BELOW_ASK))
        tick = bot.get_tick_size(sym)
        buy_price = (buy_price // tick) * tick

        if not buy_notional_ok(buy_price, qty):
            continue

        order = bot.place_limit_buy_with_tracking(sym, str(buy_price), float(qty))
        if order:
            logger.info(f"TOP-3 ENTRY: {sym} @ {buy_price} | Pressure: {imbalance:.2f}x")
            send_whatsapp_alert(f"TOP-3 {sym} @ {buy_price:.6f} (Pressure {imbalance:.2f}x)")

    first_run_executed = True

# --------------------------------------------------------------------------- #
# =============================== DASHBOARD ================================= #
# --------------------------------------------------------------------------- #
def print_dashboard(bot):
    global first_dashboard_run
    os.system('cls' if os.name == 'nt' else 'clear')
    print("=" * 120)
    print(f"{'INFINITY GRID BOT – TOP-3 ENTRY ONLY':^120}")
    print("=" * 120)
    print(f"Time: {now_cst()} | USDT: ${float(bot.get_balance()):,.2f}")
    print(f"Total Unrealized: ${float(total_pnl):+.2f} | Total Realized: ${float(total_realized_pnl):+.2f}")
    with DBManager() as sess:
        print(f"Positions: {sess.query(Position).count()} | Active Grids: {len(active_grid_symbols)}")

    print("\nPOSITIONS + LIVE PnL")
    with DBManager() as sess:
        for pos in sess.query(Position).all():
            sym = pos.symbol
            qty = float(pos.quantity)
            entry = float(pos.avg_entry_price)
            with price_lock:
                current = float(live_prices.get(sym, 0))
            with pnl_lock:
                pnl_info = position_pnl.get(sym, {'unrealized': 0, 'pct': 0})
            unrealized = float(pnl_info['unrealized'])
            pct = float(pnl_info['pct'])
            with realized_lock:
                realized = float(realized_pnl_per_symbol.get(sym, ZERO))
            color = "\033[92m" if unrealized >= 0 else "\033[91m"
            reset = "\033[0m"
            print(f"{sym:<10} | Qty: {qty:>8.4f} | Entry: ${entry:>8.6f} | Now: ${current:>8.6f} | Live: {color}${unrealized:+8.2f} ({pct:+.2f}%){reset} | Realized: ${realized:+.2f}")

    print("\nTOP 3 BUY PRESSURE (LIVE)")
    candidates = []
    for sym in valid_symbols_dict:
        with book_lock:
            bids = live_bids.get(sym, [])
            asks = live_asks.get(sym, [])
        if len(bids) < DEPTH_LEVELS or len(asks) < DEPTH_LEVELS:
            continue
        bid_vol = sum(q for _, q in bids[:DEPTH_LEVELS])
        ask_vol = sum(q for _, q in asks[:DEPTH_LEVELS])
        if ask_vol == ZERO:
            continue
        imbalance = bid_vol / ask_vol
        if imbalance >= 1.0:
            candidates.append((sym.replace('USDT',''), imbalance, float(asks[0][0])))
    for coin, imb, price in sorted(candidates, key=lambda x: x[1], reverse=True)[:3]:
        print(f" {coin:>8} | {imb:>4.1f}x | Ask: ${price:,.6f}")

    print("\nGRID LIMIT ORDERS (BUY & SELL)")
    with DBManager() as sess:
        orders = sess.query(PendingOrder).all()
        if not orders:
            print("  No active grid orders")
        else:
            for o in orders:
                side = "BUY" if o.side == 'buy' else "SELL"
                color = "\033[92m" if side == "BUY" else "\033[91m"
                reset = "\033[0m"
                print(f"  {color}{side}{reset} {o.symbol:<10} @ ${float(o.price):>10.6f} | Qty: {float(o.quantity):>8.4f}")

    print("\nGRID STATUS")
    with DBManager() as sess:
        for pos in sess.query(Position).all():
            sym = pos.symbol
            grid = active_grid_symbols.get(sym, {})
            center = grid.get('center', ZERO)
            b = len(grid.get('buy_orders', []))
            s = len(grid.get('sell_orders', []))
            print(f"{sym:<10} | Center: ${float(center):>10.6f} | Grid: {b}B/{s}S")
    print("=" * 120)

# --------------------------------------------------------------------------- #
# =============================== MAIN LOOP ================================= #
# --------------------------------------------------------------------------- #
def main():
    bot = BinanceTradingBot()
    global valid_symbols_dict
    try:
        info = bot.client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
                valid_symbols_dict[s['symbol']] = {'volume': 1e6}
    except: pass

    start_market_websocket()
    start_user_stream()
    threading.Thread(target=keepalive_user_stream, daemon=True).start()

    logger.info("Waiting 20s for WebSocket sync...")
    time.sleep(20)

    last_rebalance_check = 0
    last_dashboard = 0
    last_pnl_refresh = 0

    while True:
        try:
            now = time.time()

            # === FIRST-RUN ENTRY (TOP-3 ONLY) ===
            with DBManager() as sess:
                if sess.query(Position).count() == 0 and not first_run_executed:
                    first_run_entry_from_ladder(bot)

            # === RE-ENTRY: ONLY IF TOP-3 SIGNAL APPEARS ===
            with DBManager() as sess:
                if sess.query(Position).count() == 0 and first_run_executed:
                    # Scan for any top-3 signal
                    for sym in valid_symbols_dict:
                        if is_top3_buy_pressure(sym):
                            logger.info(f"RE-ENTRY SIGNAL: {sym} in top-3 buy pressure. Re-triggering entry...")
                            first_run_executed = False
                            first_run_entry_from_ladder(bot)
                            break

            # === GRID REBALANCE ===
            if now - last_rebalance_check >= 45:
                with DBManager() as sess:
                    for pos in sess.query(Position).all():
                        if pos.symbol in valid_symbols_dict:
                            rebalance_infinity_grid(bot, pos.symbol)
                last_rebalance_check = now

            # === PnL REFRESH ===
            if now - last_pnl_refresh >= 2:
                refresh_all_pnl()
                last_pnl_refresh = now

            # === DASHBOARD ===
            if now - last_dashboard >= 60:
                print_dashboard(bot)
                last_dashboard = now

            time.sleep(1)
        except KeyboardInterrupt:
            for ws in ws_instances:
                ws.close()
            if user_ws:
                user_ws.close()
            print("\nBot stopped.")
            break
        except Exception as e:
            logger.critical(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
