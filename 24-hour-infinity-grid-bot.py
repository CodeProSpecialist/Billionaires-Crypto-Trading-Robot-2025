#!/usr/bin/env python3
"""
    INFINITY GRID BOT – LIVE DASHBOARD + HEARTBEAT (Nov 10, 2025)
    • $8 reserve | $5 min sell
    • WebSocket heartbeat
    • Full stats dashboard
    • 100% Binance.US
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
from binance.exceptions import BinanceAPIException, BinanceRequestException
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError, IntegrityError

# === CONFIGURATION ===========================================================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    print("FATAL: Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

# Grid Config
GRID_SIZE_USDT = Decimal('5.0')
GRID_INTERVAL_PCT = Decimal('0.015')
MIN_USDT_RESERVE = Decimal('8.0')
MIN_SELL_VALUE_USDT = Decimal('5.0')
MAX_GRIDS_PER_SIDE = 16
MIN_GRIDS_PER_SIDE = 4
REGRID_INTERVAL = 45
DASHBOARD_REFRESH = 60

# Fees
DEFAULT_MAKER_FEE = Decimal('0.0010')

# WebSocket
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
MAX_STREAMS_PER_CONNECTION = 100
HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800

# General
LOG_FILE = "infinity_grid_bot.log"
SHUTDOWN_EVENT = threading.Event()

# === CONSTANTS ==============================================================
ZERO = Decimal('0')
ONE = Decimal('1')
HUNDRED = Decimal('100')
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# === LOGGING ================================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=14)
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
price_lock = threading.Lock()
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

# Health
ws_connected = False
db_connected = True
api_connected = True

# === SIGNAL HANDLING ========================================================
def signal_handler(signum, frame):
    logger.info("Shutdown signal received.")
    SHUTDOWN_EVENT.set()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# === HELPERS ================================================================
def safe_decimal(value, default=ZERO) -> Decimal:
    if isinstance(value, Decimal): return value.quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    try: return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except: return default

def now_cst(): return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")

# === CASH & MINIMUM CHECKS ==================================================
def buy_notional_ok(bot, price: Decimal, qty: Decimal) -> bool:
    try:
        cost = price * qty
        usdt_free = bot.get_balance()
        return (usdt_free >= MIN_USDT_RESERVE + cost) and (cost >= Decimal('1.25'))
    except: return False

def sell_notional_ok(price: Decimal, qty: Decimal) -> bool:
    try:
        value = price * qty
        return value >= MIN_SELL_VALUE_USDT and value >= Decimal('3.25')
    except: return False

# === RETRY DECORATOR ========================================================
def retry_custom(max_retries=5):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for i in range(max_retries):
                try: return func(*args, **kwargs)
                except (BinanceAPIException, BinanceRequestException) as e:
                    if hasattr(e, 'response') and e.response and e.status_code in (429, 418):
                        retry_after = int(e.response.headers.get('Retry-After', 60))
                        time.sleep(retry_after)
                        continue
                    time.sleep(2 ** i)
                except Exception:
                    if i == max_retries - 1: return None
                    time.sleep(2 ** i)
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
    Base.metadata.create_all(engine)

class SafeDBManager:
    def __enter__(self):
        global db_connected
        try:
            self.session = SessionFactory()
            db_connected = True
            return self.session
        except:
            db_connected = False
            return None
    def __exit__(self, exc_type, exc_val, exc_tb):
        if not hasattr(self, 'session'): return
        if exc_type: self.session.rollback()
        else:
            try: self.session.commit()
            except IntegrityError: self.session.rollback()
        self.session.close()

# === HEARTBEAT WEBSOCKET ====================================================
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, **kwargs):
        super().__init__(url, **kwargs)
        self.last_pong = time.time()
        self.heartbeat_thread = None
        self.reconnect_delay = 1
        self.max_delay = 60

    def on_open(self, ws):
        global ws_connected
        ws_connected = True
        logger.info(f"WS Connected: {ws.url.split('?')[0]}")
        self.last_pong = time.time()
        if not self.heartbeat_thread:
            self.heartbeat_thread = threading.Thread(target=self._send_heartbeat, daemon=True)
            self.heartbeat_thread.start()

    def on_pong(self, ws, *args):
        self.last_pong = time.time()

    def _send_heartbeat(self):
        while self.sock and self.sock.connected and not SHUTDOWN_EVENT.is_set():
            if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 10:
                logger.warning("No pong – reconnecting...")
                self.close()
                break
            try: self.send("ping", opcode=websocket.ABNF.OPCODE_PING)
            except: break
            time.sleep(HEARTBEAT_INTERVAL)

    def run_forever(self, **kwargs):
        while not SHUTDOWN_EVENT.is_set():
            try:
                super().run_forever(ping_interval=None, ping_timeout=None, **kwargs)
            except: pass
            global ws_connected
            ws_connected = False
            if SHUTDOWN_EVENT.is_set(): break
            delay = min(self.max_delay, self.reconnect_delay)
            logger.info(f"Reconnecting in {delay}s...")
            time.sleep(delay)
            self.reconnect_delay = min(self.max_delay, self.reconnect_delay * 2)

# === WEBSOCKET CALLBACKS ====================================================
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
    except: pass

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

        logger.info(f"FILL: {side} {symbol} @ {price}")
    except: pass

# === WEBSOCKET START ========================================================
def start_market_websocket():
    global ws_instances
    symbols = [s.lower() for s in valid_symbols_dict if 'USDT' in s]
    if not symbols: return
    streams = [f"{s}@ticker" for s in symbols]
    chunks = [streams[i:i + MAX_STREAMS_PER_CONNECTION] for i in range(0, len(streams), MAX_STREAMS_PER_CONNECTION)]
    for chunk in chunks:
        url = WS_BASE + '/'.join(chunk)
        ws = HeartbeatWebSocket(url, on_message=on_market_message, on_open=lambda ws: ws.on_open(ws))
        ws_instances.append(ws)
        threading.Thread(target=ws.run_forever, daemon=True).start()
        time.sleep(0.5)

def start_user_stream():
    global user_ws, listen_key
    try:
        client = Client(API_KEY, API_SECRET, tld='us')
        listen_key = client.stream_get_listen_key()
        url = f"{USER_STREAM_BASE}{listen_key}"
        user_ws = HeartbeatWebSocket(url, on_message=on_user_message, on_open=lambda ws: ws.on_open(ws))
        threading.Thread(target=user_ws.run_forever, daemon=True).start()
        logger.info("User stream started")
    except Exception as e:
        logger.error(f"User stream failed: {e}")

def keepalive_user_stream():
    while not SHUTDOWN_EVENT.is_set():
        time.sleep(KEEPALIVE_INTERVAL)
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
        global api_connected
        try:
            acct = self.client.get_account()
            api_connected = True
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
        except: api_connected = False

    @retry_custom()
    def get_tick_size(self, symbol):
        info = self.client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                return safe_decimal(f['tickSize'])
        return Decimal('0.00000001')

    @retry_custom()
    def get_lot_step(self, symbol):
        info = self.client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                return safe_decimal(f['stepSize'])
        return Decimal('0.00000001')

    def get_balance(self) -> Decimal:
        try:
            acct = self.client.get_account()
            for b in acct['balances']:
                if b['asset'] == 'USDT':
                    return safe_decimal(b['free'])
        except: pass
        return ZERO

    def get_asset_balance(self, asset: str) -> Decimal:
        try:
            acct = self.client.get_account()
            for b in acct['balances']:
                if b['asset'] == asset:
                    return safe_decimal(b['free'])
        except: pass
        return ZERO

    def place_limit_buy_with_tracking(self, symbol, price: Decimal, qty: Decimal):
        try:
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
            self.client.cancel_order(symbol=symbol, orderId=order_id)
        except: pass

# === GRID LOGIC =============================================================
def calculate_grids_per_symbol(bot) -> Dict[str, Tuple[int, int]]:
    usdt_free = bot.get_balance()
    if usdt_free <= MIN_USDT_RESERVE: return {}
    with SafeDBManager() as sess:
        if not sess: return {}
        positions = sess.query(Position).all()
    if not positions: return {}
    available = usdt_free - MIN_USDT_RESERVE
    total_grids = int(available // GRID_SIZE_USDT)
    per_coin = max(MIN_GRIDS_PER_SIDE, min(MAX_GRIDS_PER_SIDE, total_grids // len(positions)))
    return {pos.symbol: (per_coin, per_coin) for pos in positions}

def regrid_symbol(bot, symbol):
    try:
        current_price = live_prices.get(symbol)
        if not current_price or current_price <= ZERO: return
        old = active_grid_symbols.get(symbol, {})
        for oid in old.get('buy_orders', []) + old.get('sell_orders', []):
            bot.cancel_order_safe(symbol, oid)
        active_grid_symbols.pop(symbol, None)
        levels = calculate_grids_per_symbol(bot).get(symbol, (0, 0))[0]
        if levels == 0: return
        step = bot.get_lot_step(symbol)
        tick = bot.get_tick_size(symbol)
        qty_per_grid = (GRID_SIZE_USDT / current_price) // step * step
        if qty_per_grid <= ZERO: return
        base_asset = symbol.replace('USDT', '')
        asset_free = bot.get_asset_balance(base_asset)
        new_grid = {'center': current_price, 'qty': qty_per_grid, 'buy_orders': [], 'sell_orders': [], 'placed_at': time.time()}
        for i in range(1, levels + 1):
            price = (current_price * (ONE - GRID_INTERVAL_PCT * Decimal(i)) // tick) * tick
            if buy_notional_ok(bot, price, qty_per_grid):
                order = bot.place_limit_buy_with_tracking(symbol, price, qty_per_grid)
                if order: new_grid['buy_orders'].append(str(order['orderId']))
        for i in range(1, levels + 1):
            price = (current_price * (ONE + GRID_INTERVAL_PCT * Decimal(i)) // tick) * tick
            if asset_free >= qty_per_grid and sell_notional_ok(price, qty_per_grid):
                order = bot.place_limit_sell_with_tracking(symbol, price, qty_per_grid)
                if order:
                    new_grid['sell_orders'].append(str(order['orderId']))
                    asset_free -= qty_per_grid
        if new_grid['buy_orders'] or new_grid['sell_orders']:
            active_grid_symbols[symbol] = new_grid
            logger.info(f"GRID: {symbol} | ${float(current_price):.6f} | {len(new_grid['buy_orders'])}B/{len(new_grid['sell_orders'])}S")
    except Exception as e:
        logger.error(f"Regrid {symbol}: {e}")

# === LIVE DASHBOARD =========================================================
def print_dashboard(bot):
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        usdt = bot.get_balance()
        reserved = MIN_USDT_RESERVE
        available = usdt - reserved if usdt > reserved else ZERO

        print(f"{YELLOW}{'='*100}{RESET}")
        print(f"{GREEN}INFINITY GRID BOT – LIVE DASHBOARD{RESET} | {now_cst()} CST")
        print(f"{YELLOW}{'='*100}{RESET}")

        # Health
        status = f"{GREEN}OK{RESET}" if ws_connected else f"{RED}DOWN{RESET}"
        db_status = f"{GREEN}OK{RESET}" if db_connected else f"{RED}ERR{RESET}"
        api_status = f"{GREEN}OK{RESET}" if api_connected else f"{RED}ERR{RESET}"
        print(f"WebSocket: {status} | DB: {db_status} | API: {api_status}")

        # Balance
        print(f"\nUSDT BALANCE: ${float(usdt):,.2f} | Reserved: ${float(reserved):.2f} | Available: ${float(available):,.2f}")

        # PnL
        unrealized = sum(p['unrealized'] for p in position_pnl.values())
        color = GREEN if unrealized >= 0 else RED
        print(f"TOTAL UNREALIZED: {color}${float(unrealized):+.2f}{RESET}")
        color = GREEN if total_realized_pnl >= 0 else RED
        print(f"TOTAL REALIZED: {color}${float(total_realized_pnl):+.2f}{RESET}")

        # Positions
        with SafeDBManager() as sess:
            if sess:
                pos_count = sess.query(Position).count()
                print(f"\nPOSITIONS: {pos_count} | GRIDS ACTIVE: {len(active_grid_symbols)}")

                print(f"\n{'SYMBOL':<10} {'QTY':>10} {'ENTRY':>10} {'NOW':>10} {'PNL $':>10} {'%':>8} {'REALIZED':>10}")
                print("-" * 80)
                for pos in sess.query(Position).all():
                    sym = pos.symbol
                    qty = safe_decimal(pos.quantity)
                    entry = safe_decimal(pos.avg_entry_price)
                    current = live_prices.get(sym, ZERO)
                    unreal = (current - entry) * qty if current > ZERO else ZERO
                    pct = ((current - entry) / entry * HUNDRED) if entry > ZERO and current > ZERO else ZERO
                    color = GREEN if unreal >= 0 else RED
                    realized = realized_pnl_per_symbol.get(sym, ZERO)
                    print(f"{sym:<10} {float(qty):>10.4f} ${float(entry):>9.6f} ${float(current):>9.6f} "
                          f"{color}${float(unreal):+9.2f}{RESET} {color}{float(pct):+7.2f}%{RESET} ${float(realized):+9.2f}")

        # Grids
        if active_grid_symbols:
            print(f"\nGRID STATUS")
            print(f"{'SYMBOL':<10} {'CENTER':>12} {'BUY':>6} {'SELL':>6}")
            print("-" * 40)
            for sym, g in active_grid_symbols.items():
                print(f"{sym:<10} ${float(g['center']):>11.6f} {len(g['buy_orders']):>6} {len(g['sell_orders']):>6}")

        print(f"\n{YELLOW}{'='*100}{RESET}")
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
                valid_symbols_dict[s['symbol']] = {}
    except: pass

    start_market_websocket()
    start_user_stream()
    threading.Thread(target=keepalive_user_stream, daemon=True).start()
    time.sleep(15)

    last_regrid = 0
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

            if now - last_dashboard >= DASHBOARD_REFRESH:
                print_dashboard(bot)
                last_dashboard = now

            time.sleep(1)
        except: time.sleep(5)

    for ws in ws_instances + ([user_ws] if user_ws else []):
        try: ws.close()
        except: pass
    print("Bot stopped.")

if __name__ == "__main__":
    main()
