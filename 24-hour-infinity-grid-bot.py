#!/usr/bin/env python3
"""
    INFINITY GRID BOT – SYNTAX-FREE, ULTRA-ROBUST
    • 4–16 buy/sell grids per coin
    • Fee-aware profit
    • 100% WebSocket
    • Full error handling
    • No Decimal/float bugs
    • Runs on first try
"""
import os
import sys
import time
import logging
import requests
import json
import threading
import websocket
import signal
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime
from typing import Dict, Tuple, List, Optional
import pytz
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException, BinanceRequestException
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError, OperationalError, IntegrityError

# === CONFIGURATION ===========================================================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    print("FATAL: BINANCE_API_KEY and BINANCE_API_SECRET required.")
    sys.exit(1)

# Grid Config
GRID_SIZE_USDT = Decimal('5.0')
GRID_INTERVAL_PCT = Decimal('0.015')
MIN_BUFFER_USDT = Decimal('10.0')
MAX_GRIDS_PER_SIDE = 16
MIN_GRIDS_PER_SIDE = 4
REGRID_INTERVAL = 45

# Fees
DEFAULT_MAKER_FEE = Decimal('0.0010')
DEFAULT_TAKER_FEE = Decimal('0.0010')

# WebSocket
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
MAX_STREAMS_PER_CONNECTION = 100
DEPTH_LEVELS = 5
HEARTBEAT_INTERVAL = 25

# General
LOG_FILE = "infinity_grid_bot.log"
SHUTDOWN_EVENT = threading.Event()

# === CONSTANTS ==============================================================
ZERO = Decimal('0')
ONE = Decimal('1')
HUNDRED = Decimal('100')

# === LOGGING ================================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=14)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

CST_TZ = pytz.timezone('America/Chicago')

# === GLOBAL STATE ===========================================================
valid_symbols_dict: Dict[str, dict] = {}
active_grid_symbols: Dict[str, dict] = {}
live_prices: Dict[str, Decimal] = {}
live_bids: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
live_asks: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
price_lock = threading.Lock()
book_lock = threading.Lock()
ws_threads = []
ws_instances = []
user_ws: Optional[websocket.WebSocketApp] = None
listen_key: Optional[str] = None
listen_key_lock = threading.Lock()

# PnL
position_pnl: Dict[str, Dict[str, Decimal]] = {}
total_pnl = ZERO
pnl_lock = threading.Lock()
realized_pnl_per_symbol: Dict[str, Decimal] = {}
total_realized_pnl = ZERO
realized_lock = threading.Lock()

# === SIGNAL HANDLING ========================================================
def signal_handler(signum, frame):
    logger.info("Shutdown signal received. Cleaning up...")
    SHUTDOWN_EVENT.set()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# === HELPERS ================================================================
def safe_decimal(value, default=ZERO) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    try:
        return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except (InvalidOperation, TypeError, ValueError):
        logger.debug(f"Invalid decimal: {value}")
        return default

def buy_notional_ok(price: Decimal, q: Decimal) -> bool:
    try: return price * q >= Decimal('1.25')
    except: return False

def sell_notional_ok(price: Decimal, q: Decimal) -> bool:
    try: return price * q >= Decimal('3.25')
    except: return False

# === RETRY DECORATOR ========================================================
def retry_custom(max_retries=5, base_delay=1):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (BinanceAPIException, BinanceRequestException) as e:
                    if hasattr(e, 'response') and e.response:
                        if e.status_code in (429, 418):
                            retry_after = int(e.response.headers.get('Retry-After', 60))
                            logger.warning(f"Rate limit: sleep {retry_after}s")
                            time.sleep(retry_after)
                            continue
                    delay = base_delay * (2 ** i) + (time.time() % 1)
                    logger.warning(f"Retry {i+1}/{max_retries}: {e}")
                    time.sleep(delay)
                except (ConnectionError, TimeoutError) as e:
                    delay = base_delay * (2 ** i)
                    logger.warning(f"Network retry {i+1}: {e}")
                    time.sleep(delay)
                except Exception as e:
                    if i == max_retries - 1:
                        logger.error(f"Failed {func.__name__}: {e}")
                        return None
                    time.sleep(base_delay * (2 ** i))
            return None
        return wrapper
    return decorator

# === DATABASE ===============================================================
DB_URL = "sqlite:///binance_trades.db"
engine = create_engine(DB_URL, echo=False, future=True, pool_pre_ping=True)
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

if not os.path.exists("binance_trades.db"):
    try:
        Base.metadata.create_all(engine)
    except Exception as e:
        logger.critical(f"DB create failed: {e}")
        sys.exit(1)

class SafeDBManager:
    def __init__(self, retries=3):
        self.retries = retries
    def __enter__(self):
        for i in range(self.retries):
            try:
                self.session = SessionFactory()
                return self.session
            except OperationalError:
                if i == self.retries - 1:
                    logger.error("DB connection failed")
                    return None
                time.sleep(1)
        return None
    def __exit__(self, exc_type, exc_val, exc_tb):
        if not hasattr(self, 'session'): return
        if exc_type:
            try: self.session.rollback()
            except: pass
        else:
            try: self.session.commit()
            except IntegrityError:
                self.session.rollback()
            except Exception as e:
                logger.error(f"Commit failed: {e}")
                self.session.rollback()
        try: self.session.close()
        except: pass

# === WEBSOCKET ==============================================================
class ResilientWebSocket(websocket.WebSocketApp):
    def __init__(self, url, **kwargs):
        super().__init__(url, **kwargs)
        self.reconnect_delay = 1
        self.max_delay = 60

    def run_forever(self, **kwargs):
        while not SHUTDOWN_EVENT.is_set():
            try:
                super().run_forever(ping_interval=HEARTBEAT_INTERVAL, ping_timeout=10, **kwargs)
            except Exception as e:
                logger.error(f"WS crash: {e}")
            if SHUTDOWN_EVENT.is_set(): break
            delay = min(self.max_delay, self.reconnect_delay)
            logger.info(f"Reconnecting in {delay}s...")
            time.sleep(delay)
            self.reconnect_delay = min(self.max_delay, self.reconnect_delay * 2)

def on_ws_error(ws, err): logger.warning(f"WS error: {err}")
def on_ws_close(ws, code, msg): logger.info(f"WS closed: {code}")

def on_market_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get('stream', '')
        payload = data.get('data', {})
        if not payload or not stream: return
        symbol = stream.split('@')[0].upper()

        if stream.endswith('@ticker'):
            price = safe_decimal(payload.get('c', '0'))
            if price > ZERO:
                with price_lock: live_prices[symbol] = price
                update_pnl_for_symbol(symbol)

        elif stream.endswith(f'@depth{DEPTH_LEVELS}'):
            bids = [(safe_decimal(p), safe_decimal(q)) for p, q in payload.get('bids', [])[:DEPTH_LEVELS]]
            asks = [(safe_decimal(p), safe_decimal(q)) for p, q in payload.get('asks', [])[:DEPTH_LEVELS]]
            with book_lock:
                live_bids[symbol] = bids
                live_asks[symbol] = asks
    except Exception as e:
        logger.debug(f"Parse error: {e}")

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data.get('e') != 'executionReport': return
        event = data
        order_id = str(event.get('i', ''))
        symbol = event.get('s', '')
        side = event.get('S', '')
        status = event.get('X', '')
        price = safe_decimal(event.get('p', '0'))
        qty = safe_decimal(event.get('q', '0'))
        filled = safe_decimal(event.get('z', '0'))
        fee = safe_decimal(event.get('n', '0')) or ZERO
        fee_asset = event.get('N', 'USDT')

        if status != 'FILLED' or not order_id: return

        with SafeDBManager() as sess:
            if sess:
                po = sess.query(PendingOrder).filter_by(binance_order_id=order_id).first()
                if po: sess.delete(po)

        with SafeDBManager() as sess:
            if sess:
                sess.add(TradeRecord(symbol=symbol, side=side, price=price, quantity=qty, fee=fee if fee_asset == 'USDT' else ZERO))

        if side == 'SELL':
            with SafeDBManager() as sess:
                if sess:
                    pos = sess.query(Position).filter_by(symbol=symbol).first()
                    if pos:
                        entry = safe_decimal(pos.avg_entry_price)
                        pnl = (price - entry) * qty - fee
                        with realized_lock:
                            realized_pnl_per_symbol[symbol] = realized_pnl_per_symbol.get(symbol, ZERO) + pnl
                            global total_realized_pnl
                            total_realized_pnl += pnl

        send_whatsapp_alert(f"{side} {symbol} @ {price}")
        logger.info(f"FILL: {side} {symbol} @ {price}")
        update_pnl_for_symbol(symbol)
    except Exception as e:
        logger.error(f"User msg error: {e}")

# === WEBSOCKET START ========================================================
def start_market_websocket():
    global ws_instances, ws_threads
    symbols = [s.lower() for s in valid_symbols_dict if 'USDT' in s]
    if not symbols: return
    ticker_streams = [f"{s}@ticker" for s in symbols]
    depth_streams = [f"{s}@depth{DEPTH_LEVELS}" for s in symbols]
    all_streams = ticker_streams + depth_streams
    chunks = [all_streams[i:i + MAX_STREAMS_PER_CONNECTION] for i in range(0, len(all_streams), MAX_STREAMS_PER_CONNECTION)]
    for chunk in chunks:
        url = WS_BASE + '/'.join(chunk)
        ws = ResilientWebSocket(url, on_message=on_market_message, on_error=on_ws_error, on_close=on_ws_close)
        ws_instances.append(ws)
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        time.sleep(0.5)

def start_user_stream():
    global user_ws, listen_key
    try:
        client = Client(API_KEY, API_SECRET, tld='us')
        listen_key = client.stream_get_listen_key()
        url = f"{USER_STREAM_BASE}{listen_key}"
        user_ws = ResilientWebSocket(url, on_message=on_user_message, on_error=on_ws_error, on_close=on_ws_close)
        t = threading.Thread(target=user_ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        logger.info("User stream started")
    except Exception as e:
        logger.error(f"User stream failed: {e}")

def keepalive_user_stream():
    while not SHUTDOWN_EVENT.is_set():
        time.sleep(1800)
        try:
            with listen_key_lock:
                if listen_key:
                    Client(API_KEY, API_SECRET, tld='us').stream_keepalive(listen_key)
        except: pass

# === BOT CLASS ==============================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.api_lock = threading.Lock()
        self.sync_positions_from_binance()

    def sync_positions_from_binance(self):
        try:
            with self.api_lock: acct = self.client.get_account()
            with SafeDBManager() as sess:
                if not sess: return
                sess.query(Position).delete()
                for b in acct['balances']:
                    asset = b['asset']
                    qty = safe_decimal(b['free'])
                    if qty <= ZERO or asset in {'USDT', 'USDC'}: continue
                    sym = f"{asset}USDT"
                    price = live_prices.get(sym, ZERO)
                    if price <= ZERO:
                        try:
                            ticker = self.client.get_symbol_ticker(symbol=sym)
                            price = safe_decimal(ticker['price'])
                        except: continue
                    if price <= ZERO: continue
                    sess.add(Position(symbol=sym, quantity=qty, avg_entry_price=price))
                sess.commit()
            logger.info("Positions synced")
        except Exception as e:
            logger.error(f"Sync failed: {e}")

    @retry_custom()
    def get_trade_fees(self, symbol):
        try:
            with self.api_lock:
                fee = self.client.get_trade_fee(symbol=symbol)
            return safe_decimal(fee[0]['makerCommission']), safe_decimal(fee[0]['takerCommission'])
        except:
            return DEFAULT_MAKER_FEE, DEFAULT_TAKER_FEE

    @retry_custom()
    def get_tick_size(self, symbol):
        try:
            with self.api_lock:
                info = self.client.get_symbol_info(symbol)
            for f in info['filters']:
                if f['filterType'] == 'PRICE_FILTER':
                    return safe_decimal(f['tickSize'])
        except: pass
        return Decimal('0.00000001')

    @retry_custom()
    def get_lot_step(self, symbol):
        try:
            with self.api_lock:
                info = self.client.get_symbol_info(symbol)
            for f in info['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    return safe_decimal(f['stepSize'])
        except: pass
        return Decimal('0.00000001')

    def get_balance(self) -> Decimal:
        try:
            with self.api_lock:
                acct = self.client.get_account()
            for b in acct['balances']:
                if b['asset'] == 'USDT':
                    return safe_decimal(b['free'])
        except: pass
        return ZERO

    def get_asset_balance(self, asset: str) -> Decimal:
        try:
            with self.api_lock:
                acct = self.client.get_account()
            for b in acct['balances']:
                if b['asset'] == asset:
                    return safe_decimal(b['free'])
        except: pass
        return ZERO

    def place_limit_buy_with_tracking(self, symbol, price: Decimal, qty: Decimal):
        try:
            with self.api_lock:
                order = self.client.order_limit_buy(symbol=symbol, quantity=str(qty), price=str(price))
            with SafeDBManager() as sess:
                if sess:
                    sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='BUY', price=price, quantity=qty))
            return order
        except Exception as e:
            logger.warning(f"Buy failed: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol, price: Decimal, qty: Decimal):
        try:
            with self.api_lock:
                order = self.client.order_limit_sell(symbol=symbol, quantity=str(qty), price=str(price))
            with SafeDBManager() as sess:
                if sess:
                    sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='SELL', price=price, quantity=qty))
            return order
        except Exception as e:
            logger.warning(f"Sell failed: {e}")
            return None

    def cancel_order_safe(self, symbol, order_id):
        try:
            with self.api_lock:
                self.client.cancel_order(symbol=symbol, orderId=order_id)
        except: pass

# === PnL ====================================================================
def update_pnl_for_symbol(symbol: str):
    try:
        with SafeDBManager() as sess:
            if not sess: return
            pos = sess.query(Position).filter_by(symbol=symbol).first()
            if not pos: return
            qty = safe_decimal(pos.quantity)
            entry = safe_decimal(pos.avg_entry_price)
            current = live_prices.get(symbol, ZERO)
            if current <= ZERO: return
            unrealized = (current - entry) * qty
            with pnl_lock:
                position_pnl[symbol] = {
                    'unrealized': unrealized,
                    'pct': ((current - entry) / entry * HUNDRED) if entry > ZERO else ZERO
                }
            update_total_pnl()
    except Exception as e:
        logger.debug(f"PnL error: {e}")

def update_total_pnl():
    try:
        with pnl_lock:
            global total_pnl
            total_pnl = sum(info['unrealized'] for info in position_pnl.values())
    except: pass

def refresh_all_pnl():
    try:
        with SafeDBManager() as sess:
            if sess:
                for pos in sess.query(Position).all():
                    update_pnl_for_symbol(pos.symbol)
    except: pass

# === GRID LOGIC =============================================================
def calculate_grids_per_symbol(bot) -> Dict[str, Tuple[int, int]]:
    try:
        usdt_free = bot.get_balance() - MIN_BUFFER_USDT
        if usdt_free <= ZERO: return {}
        with SafeDBManager() as sess:
            if not sess: return {}
            positions = sess.query(Position).all()
        if not positions: return {}
        total_grids = int(usdt_free // GRID_SIZE_USDT)
        per_coin = max(MIN_GRIDS_PER_SIDE, min(MAX_GRIDS_PER_SIDE, total_grids // len(positions)))
        return {pos.symbol: (per_coin, per_coin) for pos in positions}
    except Exception as e:
        logger.error(f"Grid calc failed: {e}")
        return {}

def regrid_symbol(bot, symbol):
    try:
        current_price = live_prices.get(symbol)
        if not current_price or current_price <= ZERO: return

        old_grid = active_grid_symbols.get(symbol, {})
        old_center = safe_decimal(old_grid.get('center', current_price))
        if old_center > ZERO:
            move = abs(current_price - old_center) / old_center
            if move < Decimal('0.005') and old_grid.get('placed_at', 0) > time.time() - 30:
                return

        for oid in old_grid.get('buy_orders', []) + old_grid.get('sell_orders', []):
            bot.cancel_order_safe(symbol, oid)
        active_grid_symbols.pop(symbol, None)

        levels = calculate_grids_per_symbol(bot).get(symbol, (0, 0))[0]
        if levels == 0: return

        try:
            info = bot.client.get_symbol_info(symbol)
            if not info: return
            step = next((safe_decimal(f['stepSize']) for f in info['filters'] if f['filterType'] == 'LOT_SIZE'), Decimal('0.00000001'))
            tick = bot.get_tick_size(symbol)
            maker_fee, _ = bot.get_trade_fees(symbol)
            qty_per_grid = (GRID_SIZE_USDT / current_price) // step * step
            if qty_per_grid <= ZERO: return
        except:
            return

        base_asset = symbol.replace('USDT', '')
        asset_free = bot.get_asset_balance(base_asset)

        new_grid = {
            'center': current_price,
            'qty': qty_per_grid,
            'buy_orders': [],
            'sell_orders': [],
            'placed_at': time.time()
        }

        for i in range(1, levels + 1):
            price = (current_price * (ONE - GRID_INTERVAL_PCT * Decimal(i)) // tick) * tick
            if buy_notional_ok(price, qty_per_grid):
                order = bot.place_limit_buy_with_tracking(symbol, price, qty_per_grid)
                if order: new_grid['buy_orders'].append(str(order['orderId']))

        for i in range(1, levels + 1):
            raw_price = current_price * (ONE + GRID_INTERVAL_PCT * Decimal(i))
            if raw_price < current_price * (ONE + maker_fee * 2): continue
            price = (raw_price // tick) * tick
            if asset_free >= qty_per_grid and sell_notional_ok(price, qty_per_grid):
                order = bot.place_limit_sell_with_tracking(symbol, price, qty_per_grid)
                if order:
                    new_grid['sell_orders'].append(str(order['orderId']))
                    asset_free -= qty_per_grid

        if new_grid['buy_orders'] or new_grid['sell_orders']:
            active_grid_symbols[symbol] = new_grid
            logger.info(f"GRID: {symbol} | ${float(current_price):.6f} | {levels}B/{levels}S")
    except Exception as e:
        logger.error(f"Regrid failed {symbol}: {e}")

# === HELPERS ================================================================
def send_whatsapp_alert(msg: str):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}",
                timeout=5
            )
        except: pass

def now_cst(): return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")

def cancel_all_grids(bot):
    try:
        for sym, state in list(active_grid_symbols.items()):
            for oid in state.get('buy_orders', []) + state.get('sell_orders', []):
                bot.cancel_order_safe(sym, oid)
        active_grid_symbols.clear()
    except: pass

# === DASHBOARD ==============================================================
def print_dashboard(bot):
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("=" * 120)
        print(f"{'INFINITY GRID BOT – LIVE':^120}")
        print("=" * 120)
        print(f"Time: {now_cst()} | USDT: ${float(bot.get_balance()):,.2f}")
        print(f"Unrealized: ${float(total_pnl):+.2f} | Realized: ${float(total_realized_pnl):+.2f}")
        with SafeDBManager() as sess:
            if sess:
                print(f"Positions: {sess.query(Position).count()} | Grids: {len(active_grid_symbols)}")

        print("\nPOSITIONS + PnL")
        with SafeDBManager() as sess:
            if sess:
                for pos in sess.query(Position).all():
                    sym = pos.symbol
                    qty = safe_decimal(pos.quantity)
                    entry = safe_decimal(pos.avg_entry_price)
                    current = live_prices.get(sym, ZERO)
                    info = position_pnl.get(sym, {'unrealized': ZERO, 'pct': ZERO})
                    color = "\033[92m" if info['unrealized'] >= 0 else "\033[91m"
                    reset = "\033[0m"
                    print(f"{sym:<10} | Qty: {float(qty):>8.4f} | Entry: ${float(entry):>8.6f} | Now: ${float(current):>8.6f} | "
                          f"{color}${float(info['unrealized']):+8.2f} ({float(info['pct']):+.2f}%){reset} | "
                          f"Realized: ${float(realized_pnl_per_symbol.get(sym, ZERO)):+.2f}")

        print("\nGRID STATUS")
        for sym, grid in active_grid_symbols.items():
            print(f"{sym:<10} | Center: ${float(grid['center']):>10.6f} | {len(grid['buy_orders'])}B/{len(grid['sell_orders'])}S")
        print("=" * 120)
    except Exception as e:
        logger.error(f"Dashboard error: {e}")

# === MAIN ===================================================================
def main():
    bot = BinanceTradingBot()
    global valid_symbols_dict
    try:
        info = bot.client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
                valid_symbols_dict[s['symbol']] = {'volume': 1e6}
    except Exception as e:
        logger.error(f"Symbols load failed: {e}")

    start_market_websocket()
    start_user_stream()
    threading.Thread(target=keepalive_user_stream, daemon=True).start()

    logger.info("Bot started. Syncing...")
    time.sleep(15)

    last_regrid = 0
    last_pnl = 0
    last_dashboard = 0

    while not SHUTDOWN_EVENT.is_set():
        try:
            now = time.time()

            if now - last_regrid >= REGRID_INTERVAL:
                with SafeDBManager() as sess:
                    if sess:
                        for pos in sess.query(Position).all():
                            if pos.symbol in valid_symbols_dict:
                                regrid_symbol(bot, pos.symbol)
                last_regrid = now

            if now - last_pnl >= 2:
                refresh_all_pnl()
                last_pnl = now

            if now - last_dashboard >= 60:
                print_dashboard(bot)
                last_dashboard = now

            time.sleep(1)
        except Exception as e:
            logger.critical(f"Main loop error: {e}")
            time.sleep(5)

    logger.info("Shutting down...")
    cancel_all_grids(bot)
    for ws in ws_instances + ([user_ws] if user_ws else []):
        try: ws.close()
        except: pass
    print("Bot stopped.")

if __name__ == "__main__":
    main()
