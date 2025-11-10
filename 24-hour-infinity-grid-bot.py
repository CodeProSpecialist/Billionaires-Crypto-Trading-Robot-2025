#!/usr/bin/env python3
"""
    INFINITY GRID BOT – DYNAMIC COLOR-AWARE DASHBOARD (Nov 10, 2025)
    • +4 visible spaces between columns
    • ANSI color codes ignored in width calc
    • Perfect alignment always
    • DATA: WebSocket only | TRADING: REST only
"""
import os
import sys
import time
import logging
import json
import threading
import websocket
import signal
import re
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from typing import Dict, Tuple, List, Optional
import pytz
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError, IntegrityError

# === CONFIGURATION ===========================================================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

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
symbol_info_cache: Dict[str, dict] = {}
active_grid_symbols: Dict[str, dict] = {}
live_prices: Dict[str, Decimal] = {}
price_lock = threading.Lock()
ws_instances = []
user_ws: Optional[websocket.WebSocketApp] = None
listen_key: Optional[str] = None
listen_key_lock = threading.Lock()

# Balances & Positions
balances: Dict[str, Decimal] = {'USDT': ZERO}
balance_lock = threading.Lock()

# PnL
realized_pnl_per_symbol: Dict[str, Decimal] = {}
total_realized_pnl = ZERO
realized_lock = threading.Lock()

# Health
ws_connected = False
db_connected = True
rest_client = None

# === SIGNAL HANDLING ========================================================
def signal_handler(signum, frame):
    logger.info("Shutdown signal received.")
    SHUTDOWN_EVENT.set()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# === DYNAMIC PADDING ========================================================
def pad_field(text: str, width: int) -> str:
    visible = len(re.sub(r'\033\[[0-9;]*m', '', str(text)))
    return text + ' ' * max(0, width - visible)

# === HELPERS ================================================================
def safe_decimal(value, default=ZERO) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    try:
        return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except:
        return default

def now_cst() -> str:
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")

# === CASH & MINIMUM CHECKS ==================================================
def buy_notional_ok(price: Decimal, qty: Decimal) -> bool:
    with balance_lock:
        usdt_free = balances.get('USDT', ZERO)
    cost = price * qty
    return (usdt_free >= MIN_USDT_RESERVE + cost) and (cost >= Decimal('1.25'))

def sell_notional_ok(price: Decimal, qty: Decimal) -> bool:
    value = price * qty
    return value >= MIN_SELL_VALUE_USDT and value >= Decimal('3.25')

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
        except Exception:
            db_connected = False
            return None
    def __exit__(self, exc_type, exc_val, exc_tb):
        if not hasattr(self, 'session'):
            return
        if exc_type:
            self.session.rollback()
        else:
            try:
                self.session.commit()
            except IntegrityError:
                self.session.rollback()
        self.session.close()

# === HEARTBEAT WEBSOCKET ====================================================
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, on_message_cb, is_user_stream=False):
        super().__init__(
            url,
            on_open=self.on_open,
            on_message=on_message_cb,
            on_error=self.on_error,
            on_close=self.on_close,
            on_pong=self.on_pong
        )
        self.is_user_stream = is_user_stream
        self.last_pong = time.time()
        self.heartbeat_thread = None
        self.reconnect_delay = 1
        self.max_delay = 60

    def on_open(self, ws):
        global ws_connected
        ws_connected = True
        logger.info(f"WEBSOCKET CONNECTED: {ws.url.split('?')[0]}")
        self.last_pong = time.time()
        if not self.heartbeat_thread:
            self.heartbeat_thread = threading.Thread(target=self._send_heartbeat, daemon=True)
            self.heartbeat_thread.start()

    def on_error(self, ws, error):
        logger.warning(f"WS ERROR: {error}")

    def on_close(self, ws, *args):
        global ws_connected
        ws_connected = False
        logger.warning(f"WS CLOSED: {ws.url.split('?')[0]}")

    def on_pong(self, ws, *args):
        self.last_pong = time.time()

    def _send_heartbeat(self):
        while self.sock and self.sock.connected and not SHUTDOWN_EVENT.is_set():
            if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 10:
                logger.warning("No pong – reconnecting...")
                self.close()
                break
            try:
                self.send("ping", opcode=websocket.ABNF.OPCODE_PING)
            except:
                break
            time.sleep(HEARTBEAT_INTERVAL)

    def run_forever(self):
        while not SHUTDOWN_EVENT.is_set():
            try:
                super().run_forever(ping_interval=None, ping_timeout=None)
            except Exception as e:
                logger.error(f"WS CRASH: {e}")
            if SHUTDOWN_EVENT.is_set():
                break
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
        if not payload or not stream:
            return
        symbol = stream.split('@')[0].upper()
        if stream.endswith('@ticker'):
            price = safe_decimal(payload.get('c', '0'))
            if price > ZERO:
                with price_lock:
                    live_prices[symbol] = price
    except Exception:
        pass

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        event_type = data.get('e')

        # Balance Update
        if event_type == 'balanceUpdate':
            asset = data['a']
            balance = safe_decimal(data['wb'])
            with balance_lock:
                balances[asset] = balance
            return

        # Account Position Update
        if event_type == 'outboundAccountPosition':
            for b in data['B']:
                asset = b['a']
                free = safe_decimal(b['f'])
                if asset == 'USDT' or free <= ZERO:
                    continue
                symbol = f"{asset}USDT"
                if symbol not in valid_symbols_dict:
                    continue
                with balance_lock:
                    balances[asset] = free
                with SafeDBManager() as sess:
                    if sess:
                        pos = sess.query(Position).filter_by(symbol=symbol).first()
                        current_price = live_prices.get(symbol, ZERO)
                        entry = current_price if current_price > ZERO else (safe_decimal(pos.avg_entry_price) if pos else ZERO)
                        if pos:
                            pos.quantity = free
                            if entry > ZERO:
                                pos.avg_entry_price = entry
                        else:
                            sess.add(Position(symbol=symbol, quantity=free, avg_entry_price=entry))
            return

        # Execution Report
        if event_type != 'executionReport':
            return
        event = data
        order_id = str(event.get('i', ''))
        symbol = event.get('s', '')
        side = event.get('S', '')
        status = event.get('X', '')
        price = safe_decimal(event.get('p', '0'))
        qty = safe_decimal(event.get('q', '0'))
        fee = safe_decimal(event.get('n', '0')) or ZERO
        fee_asset = event.get('N', 'USDT')

        if status == 'FILLED' and order_id:
            with SafeDBManager() as sess:
                if sess:
                    po = sess.query(PendingOrder).filter_by(binance_order_id=order_id).first()
                    if po:
                        sess.delete(po)

            with SafeDBManager() as sess:
                if sess:
                    sess.add(TradeRecord(symbol=symbol, side=side, price=price, quantity=qty,
                                       fee=fee if fee_asset == 'USDT' else ZERO))

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

    except Exception as e:
        logger.error(f"User WS error: {e}")

# === WEBSOCKET START ========================================================
def start_market_websocket():
    global ws_instances
    symbols = [s.lower() for s in valid_symbols_dict if 'USDT' in s]
    if not symbols:
        logger.warning("No USDT symbols found")
        return
    streams = [f"{s}@ticker" for s in symbols]
    chunks = [streams[i:i + MAX_STREAMS_PER_CONNECTION] for i in range(0, len(streams), MAX_STREAMS_PER_CONNECTION)]
    for chunk in chunks:
        url = WS_BASE + '/'.join(chunk)
        ws = HeartbeatWebSocket(url, on_message_cb=on_market_message)
        ws_instances.append(ws)
        threading.Thread(target=ws.run_forever, daemon=True).start()
        time.sleep(0.5)

def start_user_stream():
    global user_ws, listen_key, rest_client
    try:
        listen_key = rest_client.stream_get_listen_key()
        url = f"{USER_STREAM_BASE}{listen_key}"
        user_ws = HeartbeatWebSocket(url, on_message_cb=on_user_message, is_user_stream=True)
        threading.Thread(target=user_ws.run_forever, daemon=True).start()
        logger.info("User stream started")
    except Exception as e:
        logger.error(f"User stream failed: {e}")

def keepalive_user_stream():
    global rest_client
    while not SHUTDOWN_EVENT.is_set():
        time.sleep(KEEPALIVE_INTERVAL)
        try:
            with listen_key_lock:
                if listen_key and rest_client:
                    rest_client.stream_keepalive(listen_key)
        except Exception:
            pass

# === BOT CLASS ==============================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.api_lock = threading.Lock()

    def get_tick_size(self, symbol):
        return symbol_info_cache.get(symbol, {}).get('tickSize', Decimal('0.00000001'))

    def get_lot_step(self, symbol):
        return symbol_info_cache.get(symbol, {}).get('stepSize', Decimal('0.00000001'))

    def get_balance(self) -> Decimal:
        with balance_lock:
            return balances.get('USDT', ZERO)

    def get_asset_balance(self, asset: str) -> Decimal:
        with balance_lock:
            return balances.get(asset, ZERO)

    def place_limit_buy_with_tracking(self, symbol, price: Decimal, qty: Decimal):
        try:
            order = self.client.order_limit_buy(
                symbol=symbol,
                quantity=str(qty),
                price=str(price)
            )
            with SafeDBManager() as sess:
                if sess:
                    sess.add(PendingOrder(
                        binance_order_id=str(order['orderId']),
                        symbol=symbol,
                        side='BUY',
                        price=price,
                        quantity=qty
                    ))
            return order
        except Exception as e:
            logger.warning(f"Buy failed: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol, price: Decimal, qty: Decimal):
        try:
            order = self.client.order_limit_sell(
                symbol=symbol,
                quantity=str(qty),
                price=str(price)
            )
            with SafeDBManager() as sess:
                if sess:
                    sess.add(PendingOrder(
                        binance_order_id=str(order['orderId']),
                        symbol=symbol,
                        side='SELL',
                        price=price,
                        quantity=qty
                    ))
            return order
        except Exception as e:
            logger.warning(f"Sell failed: {e}")
            return None

    def cancel_order_safe(self, symbol, order_id):
        try:
            self.client.cancel_order(symbol=symbol, orderId=order_id)
        except Exception:
            pass

# === GRID LOGIC =============================================================
def calculate_grids_per_symbol(bot) -> Dict[str, Tuple[int, int]]:
    usdt_free = bot.get_balance()
    if usdt_free <= MIN_USDT_RESERVE:
        return {}
    with SafeDBManager() as sess:
        if not sess:
            return {}
        positions = sess.query(Position).all()
    if not positions:
        return {}
    available = usdt_free - MIN_USDT_RESERVE
    total_grids = int(available // GRID_SIZE_USDT)
    per_coin = max(MIN_GRIDS_PER_SIDE, min(MAX_GRIDS_PER_SIDE, total_grids // len(positions)))
    return {pos.symbol: (per_coin, per_coin) for pos in positions}

def regrid_symbol(bot, symbol):
    try:
        current_price = live_prices.get(symbol)
        if not current_price or current_price <= ZERO:
            return
        old = active_grid_symbols.get(symbol, {})
        for oid in old.get('buy_orders', []) + old.get('sell_orders', []):
            bot.cancel_order_safe(symbol, oid)
        active_grid_symbols.pop(symbol, None)

        levels = calculate_grids_per_symbol(bot).get(symbol, (0, 0))[0]
        if levels == 0:
            return

        step = bot.get_lot_step(symbol)
        tick = bot.get_tick_size(symbol)
        qty_per_grid = (GRID_SIZE_USDT / current_price) // step * step
        if qty_per_grid <= ZERO:
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
            price = (current_price * (ONE - GRID_INTERVAL_PCT * Decimal(i))) // tick * tick
            if buy_notional_ok(price, qty_per_grid):
                order = bot.place_limit_buy_with_tracking(symbol, price, qty_per_grid)
                if order:
                    new_grid['buy_orders'].append(str(order['orderId']))

        for i in range(1, levels + 1):
            price = (current_price * (ONE + GRID_INTERVAL_PCT * Decimal(i))) // tick * tick
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

# === DASHBOARD ==============================================================
def print_dashboard(bot):
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        usdt = bot.get_balance()
        reserved = MIN_USDT_RESERVE
        available = max(usdt - reserved, ZERO)

        line = pad_field(f"{YELLOW}{'=' * 120}{RESET}", 120)
        print(line)
        title = f"{GREEN}INFINITY GRID BOT – LIVE{RESET} | {now_cst()} CST | WS: "
        title += f"{GREEN}ON{RESET}" if ws_connected else f"{RED}OFF{RESET}"
        print(pad_field(title, 120))
        print(line)

        ws_stat = f"{GREEN}OK{RESET}" if ws_connected else f"{RED}DOWN{RESET}"
        db_stat = f"{GREEN}OK{RESET}" if db_connected else f"{RED}ERR{RESET}"
        health = f"WebSocket: {ws_stat}    DB: {db_stat}    API: {GREEN}TRADING{RESET}"
        print(pad_field(health, 120))

        bal = f"USDT: ${float(usdt):,.2f}    Reserved: ${float(reserved):.2f}    Free: ${float(available):,.2f}"
        print(f"\n{pad_field(bal, 120)}")

        # Unrealized PnL
        unrealized = ZERO
        with SafeDBManager() as sess:
            if sess:
                for pos in sess.query(Position).all():
                    qty = safe_decimal(pos.quantity)
                    entry = safe_decimal(pos.avg_entry_price)
                    current = live_prices.get(pos.symbol, ZERO)
                    if current > ZERO:
                        unrealized += (current - entry) * qty
        u_color = GREEN if unrealized >= 0 else RED
        r_color = GREEN if total_realized_pnl >= 0 else RED
        pnl_line = f"UNREALIZED: {u_color}${float(unrealized):+.2f}{RESET}    REALIZED: {r_color}${float(total_realized_pnl):+.2f}{RESET}"
        print(pad_field(pnl_line, 120))

        # Positions
        with SafeDBManager() as sess:
            if sess:
                pos_count = sess.query(Position).count()
                pos_line = f"POSITIONS: {pos_count}    GRIDS: {len(active_grid_symbols)}"
                print(f"\n{pad_field(pos_line, 120)}")

                headers = [("SYMBOL", 10), ("QTY", 14), ("ENTRY", 14), ("NOW", 14), ("PNL", 14), ("REALIZED", 14)]
                print("".join(pad_field(l, w) for l, w in headers))
                print("-" * 120)

                for pos in sess.query(Position).all():
                    sym = pos.symbol
                    qty = safe_decimal(pos.quantity)
                    entry = safe_decimal(pos.avg_entry_price)
                    current = live_prices.get(sym, ZERO)
                    unreal = (current - entry) * qty if current > ZERO else ZERO
                    realized = realized_pnl_per_symbol.get(sym, ZERO)
                    color = GREEN if unreal >= 0 else RED

                    row = [
                        (sym, 10),
                        (f"{float(qty):.8f}".rstrip('0').rstrip('.'), 14),
                        (f"${float(entry):.6f}", 14),
                        (f"${float(current):.6f}", 14),
                        (f"{color}${float(unreal):+.2f}{RESET}", 14),
                        (f"${float(realized):+.2f}", 14)
                    ]
                    print("".join(pad_field(v, w) for v, w in row))

        # Grid Status
        if active_grid_symbols:
            print(f"\n{pad_field('GRID STATUS', 120)}")
            g_headers = [("SYMBOL", 10), ("CENTER", 16), ("BUY", 10), ("SELL", 10)]
            print("".join(pad_field(l, w) for l, w in g_headers))
            print("-" * 60)
            for sym, g in active_grid_symbols.items():
                g_row = [
                    (sym, 10),
                    (f"${float(g['center']):.6f}", 16),
                    (str(len(g['buy_orders'])), 10),
                    (str(len(g['sell_orders'])), 10)
                ]
                print("".join(pad_field(v, w) for v, w in g_row))

        print(f"\n{line}")
    except Exception as e:
        logger.error(f"Dashboard error: {e}")

# === MAIN ===================================================================
def main():
    global rest_client, valid_symbols_dict, symbol_info_cache
    rest_client = Client(API_KEY, API_SECRET, tld='us')

    # Load symbols and filters once via REST
    try:
        info = rest_client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
                sym = s['symbol']
                valid_symbols_dict[sym] = {}
                tick = step = Decimal('0.00000001')
                for f in s['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        tick = safe_decimal(f['tickSize'])
                    if f['filterType'] == 'LOT_SIZE':
                        step = safe_decimal(f['stepSize'])
                symbol_info_cache[sym] = {'tickSize': tick, 'stepSize': step}
    except Exception as e:
        logger.error(f"Failed to load symbols: {e}")
        sys.exit(1)

    start_market_websocket()
    start_user_stream()
    threading.Thread(target=keepalive_user_stream, daemon=True).start()

    bot = BinanceTradingBot()
    time.sleep(10)

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
        except Exception:
            time.sleep(5)

    for ws in ws_instances + ([user_ws] if user_ws else []):
        try:
            ws.close()
        except:
            pass
    print("Bot stopped.")

if __name__ == "__main__":
    main()
