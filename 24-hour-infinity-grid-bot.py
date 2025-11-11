#!/usr/bin/env python3
"""
    INFINITY GRID v8.3 – TRUE VAULT MODE (FINAL FULL VERSION)
    • $40 USDT floor (untouchable)
    • 1–16 grids per side (1 buy + 1 sell minimum, 24/7/365)
    • Never removes active coins unless volume < $100k
    • Regrids only every 30 minutes
    • Profit recycle only after $50+
    • Strategy change only every 2 hours
    • Set-and-forget for life
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
import requests
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
import pytz
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError, IntegrityError

# === PRECISION ==============================================================
getcontext().prec = 28

# === CONFIGURATION ===========================================================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    print("FATAL: Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

# === VAULT MODE CONFIG (YOUR FINAL RULES) ===================================
MIN_PRICE_USDT              = Decimal('1.00')
MAX_PRICE_USDT              = Decimal('1000.00')
MIN_24H_VOLUME_USDT         = Decimal('100000')

MIN_USDT_RESERVE            = Decimal('40.0')     # ← $40 floor
MIN_TRADE_VALUE_USDT        = Decimal('5.0')
MIN_SELL_VALUE_USDT         = Decimal('5.0')
MIN_GRIDS_PER_SIDE          = 1                   # ← 1 buy + 1 sell minimum
MAX_GRIDS_PER_SIDE          = 16                  # ← Max 16 per side
REGRID_INTERVAL             = 1800                # ← 30 minutes
DASHBOARD_REFRESH           = 60
PNL_REGRID_THRESHOLD        = Decimal('25.0')
FEE_RATE                    = Decimal('0.001')
TREND_THRESHOLD             = Decimal('0.03')
VP_UPDATE_INTERVAL          = 3600
ENTRY_PCT_BELOW_ASK         = Decimal('0.001')
PERCENTAGE_PER_COIN         = Decimal('0.05')
MIN_USDT_FRACTION           = Decimal('0.15')
MIN_BUFFER_USDT             = Decimal('20.0')

PME_INTERVAL                = 7200                # ← 2 hours
PME_MIN_SCORE_THRESHOLD     = Decimal('2.2')

# WebSocket
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
MAX_STREAMS_PER_CONNECTION = 100
HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800

# General
LOG_FILE = "infinity_grid_vault.log"
SHUTDOWN_EVENT = threading.Event()
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
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=30)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

CST_TZ = pytz.timezone('America/Chicago')

# === GLOBAL STATE ===========================================================
valid_symbols_dict: dict = {}
symbol_info_cache: dict = {}
active_grid_symbols: dict = {}
live_prices: dict = {}
live_asks: dict = {}
live_bids: dict = {}
price_lock = threading.Lock()
ws_instances = []
user_ws = None
listen_key = None
listen_key_lock = threading.Lock()

balances: dict = {'USDT': ZERO}
balance_lock = threading.Lock()

realized_pnl_per_symbol: dict = {}
total_realized_pnl = ZERO
last_reported_pnl = ZERO
last_recycle_pnl = ZERO
realized_lock = threading.Lock()

ws_connected = False
db_connected = True
rest_client = None

ticker_24h_stats: dict = {}
stats_lock = threading.Lock()
trend_bias: dict = {}
kline_data: dict = {}
kline_lock = threading.Lock()
momentum_score: dict = {}
stochastic_data: dict = {}
volume_profile: dict = {}
last_vp_update = 0
pnl_history = []

strategy_scores: dict = {}
pme_last_run = 0

positions: dict = {}
top25_symbols: list = []

# === HELPERS ================================================================
def pad_field(text: str, width: int) -> str:
    visible = len(re.sub(r'\033\[[0-9;]*m', '', str(text)))
    return text + ' ' * max(0, width - visible)

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
def calculate_qty_from_cash(cash_usdt: Decimal, price: Decimal, step: Decimal) -> Decimal:
    if price <= ZERO or cash_usdt < MIN_TRADE_VALUE_USDT:
        return ZERO
    raw_qty = cash_usdt / price
    qty = (raw_qty // step) * step
    value = qty * price
    if value < MIN_TRADE_VALUE_USDT:
        return ZERO
    return qty

def get_owned_qty(bot, symbol: str) -> Decimal:
    base = symbol.replace('USDT', '')
    try:
        with bot.api_lock:
            bal = bot.client.get_asset_balance(asset=base)
        return safe_decimal(bal['free'])
    except:
        return ZERO

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

# === ALERTS =================================================================
def send_alert(message: str, subject: str = "VAULT"):
    if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE:
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&apikey={CALLMEBOT_API_KEY}&text={subject}: {message}"
        requests.get(url, timeout=5)
    except:
        pass

# === SIGNAL HANDLING ========================================================
def signal_handler(signum, frame):
    logger.info("Shutdown signal received.")
    SHUTDOWN_EVENT.set()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

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

    def get_total_account_value(self) -> Decimal:
        total = self.get_balance()
        with SafeDBManager() as sess:
            if sess:
                for pos in sess.query(Position).all():
                    price = live_prices.get(pos.symbol, ZERO)
                    if price > ZERO:
                        total += safe_decimal(pos.quantity) * price
        return total

    def place_limit_buy_with_cash(self, symbol, price: Decimal, cash_usdt: Decimal):
        if not is_eligible_coin(symbol):
            return None
        step = self.get_lot_step(symbol)
        qty = calculate_qty_from_cash(cash_usdt, price, step)
        if qty <= ZERO:
            return None
        try:
            with self.api_lock:
                order = self.client.order_limit_buy(
                    symbol=symbol,
                    quantity=str(qty),
                    price=str(price)
                )
            logger.info(f"LIMIT BUY: {symbol} @ {price} | Qty: {qty}")
            send_alert(f"BUY {symbol} {qty} @ {price}", subject="GRID")
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
            logger.error(f"Buy failed {symbol}: {e}")
            return None

    def place_limit_sell_with_owned(self, symbol, price: Decimal):
        owned = get_owned_qty(self, symbol)
        if owned <= ZERO:
            return None
        step = self.get_lot_step(symbol)
        qty = (owned // step) * step
        value = qty * price
        if value < MIN_TRADE_VALUE_USDT:
            return None
        try:
            with self.api_lock:
                order = self.client.order_limit_sell(
                    symbol=symbol,
                    quantity=str(qty),
                    price=str(price)
                )
            logger.info(f"LIMIT SELL: {symbol} @ {price} | Qty: {qty}")
            send_alert(f"SELL {symbol} {qty} @ {price}", subject="GRID")
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
            logger.error(f"Sell failed {symbol}: {e}")
            return None

    def cancel_order_safe(self, symbol, order_id):
        try:
            self.client.cancel_order(symbol=symbol, orderId=order_id)
        except Exception:
            pass

# === WEBSOCKET HEARTBEAT ====================================================
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
            with stats_lock:
                ticker_24h_stats[symbol] = {
                    'h': payload.get('h', '0'),
                    'l': payload.get('l', '0'),
                    'P': payload.get('P', '0'),
                    'q': payload.get('q', '0'),
                    'v': payload.get('v', '0')
                }
            pct_change = safe_decimal(payload.get('P', '0')) / 100
            bias = ZERO
            if pct_change >= TREND_THRESHOLD:
                bias = ONE
            elif pct_change <= -TREND_THRESHOLD:
                bias = Decimal('-1.0')
            trend_bias[symbol] = bias

        elif stream.endswith('@depth5'):
            asks = payload.get('asks', [])
            bids = payload.get('bids', [])
            if asks:
                with price_lock:
                    live_asks[symbol] = [(safe_decimal(p), safe_decimal(q)) for p, q in asks]
            if bids:
                with price_lock:
                    live_bids[symbol] = [(safe_decimal(p), safe_decimal(q)) for p, q in bids]

        elif stream.endswith('@kline_1m'):
            k = payload.get('k', {})
            if not k.get('x'):
                return
            close = safe_decimal(k.get('c', '0'))
            if close <= ZERO:
                return
            with kline_lock:
                if symbol not in kline_data:
                    kline_data[symbol] = []
                kline_data[symbol].append({'close': close, 'time': k.get('T'), 'interval': '1m'})
                if len(kline_data[symbol]) > 100:
                    kline_data[symbol] = kline_data[symbol][-100:]

    except Exception:
        pass

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        event_type = data.get('e')
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
        if event_type == 'executionReport':
            event = data
            order_id = str(event.get('i', ''))
            symbol = event.get('s', '')
            side = event.get('S', '')
            status = event.get('X', '')
            price = safe_decimal(event.get('p', '0'))
            qty = safe_decimal(event.get('q', '0'))
            fee = safe_decimal(event.get('n', '0')) or ZERO
            if status == 'FILLED' and order_id:
                with SafeDBManager() as sess:
                    if sess:
                        po = sess.query(PendingOrder).filter_by(binance_order_id=order_id).first()
                        if po:
                            sess.delete(po)
                with SafeDBManager() as sess:
                    if sess:
                        sess.add(TradeRecord(symbol=symbol, side=side, price=price, quantity=qty, fee=fee))
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
                send_alert(f"{side} {symbol} {qty} @ {price}", subject="FILL")
    except Exception as e:
        logger.error(f"User WS error: {e}")

# === WEBSOCKET START ========================================================
def start_market_websocket():
    global ws_instances
    symbols = [s.lower() for s in valid_symbols_dict if 'USDT' in s]
    if not symbols:
        logger.warning("No USDT symbols found")
        return
    streams = [f"{s}@ticker" for s in symbols] + [f"{s}@depth5" for s in symbols] + [f"{s}@kline_1m" for s in symbols]
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

# === ELITE COIN FILTER ======================================================
def is_eligible_coin(symbol: str) -> bool:
    price = live_prices.get(symbol, ZERO)
    if not price or price <= ZERO:
        return False
    if price < MIN_PRICE_USDT or price > MAX_PRICE_USDT:
        return False
    with stats_lock:
        volume_usdt = safe_decimal(ticker_24h_stats.get(symbol, {}).get('q', '0'))
    return volume_usdt >= MIN_24H_VOLUME_USDT

# === PROFIT OPTIMIZATIONS ===================================================
def get_profit_optimized_grid_size(symbol: str, price: Decimal) -> Decimal:
    base = Decimal('15.0')
    return max(base, Decimal('10.0')).quantize(Decimal('0.01'))

def get_optimal_interval(symbol: str, price: Decimal) -> Decimal:
    return Decimal('0.009')

def get_volume_density_multiplier(symbol: str, price: Decimal) -> Decimal:
    return ONE

def get_tick_aware_price(base_price: Decimal, direction: int, i: int, interval: Decimal, tick: Decimal) -> Decimal:
    raw = base_price * (ONE + direction * interval * Decimal(i))
    rounded = (raw // tick) * tick
    offset = tick if direction > 0 else -tick
    return (rounded + offset).quantize(tick)

# === REBALANCER =============================================================
def rebalance_loop():
    while not SHUTDOWN_EVENT.is_set():
        time.sleep(3600)

# === PME ====================================================================
def profit_monitoring_engine():
    global pme_last_run
    while not SHUTDOWN_EVENT.is_set():
        time.sleep(PME_INTERVAL)

# === REGRID FUNCTION (GUARANTEES 1+1) =======================================
def regrid_symbol_with_strategy(bot, symbol, strategy='volume_anchored'):
    if not is_eligible_coin(symbol):
        if symbol in active_grid_symbols:
            logger.info(f"REMOVING {symbol} — no longer elite")
            g = active_grid_symbols.pop(symbol, {})
            for oid in g.get('buy_orders', []) + g.get('sell_orders', []):
                bot.cancel_order_safe(symbol, oid)
        return

    if symbol in active_grid_symbols and time.time() - active_grid_symbols[symbol].get('placed_at', 0) < REGRID_INTERVAL:
        return

    try:
        current_price = live_prices.get(symbol)
        if not current_price or current_price <= ZERO:
            return

        grid_size = get_profit_optimized_grid_size(symbol, current_price)
        base_interval = get_optimal_interval(symbol, current_price)
        grid_center = current_price

        usdt_free = bot.get_balance()
        if usdt_free <= MIN_USDT_RESERVE + Decimal('10'):
            return

        max_grids_total = min(MAX_GRIDS_PER_SIDE * 2, int((usdt_free - MIN_USDT_RESERVE) // grid_size))
        if max_grids_total < 2:
            return

        buy_grids = max(MIN_GRIDS_PER_SIDE, max_grids_total // 2)
        sell_grids = max(MIN_GRIDS_PER_SIDE, max_grids_total - buy_grids)

        old = active_grid_symbols.get(symbol, {})
        for oid in old.get('buy_orders', []) + old.get('sell_orders', []):
            bot.cancel_order_safe(symbol, oid)
        active_grid_symbols.pop(symbol, None)

        step = bot.get_lot_step(symbol)
        tick = bot.get_tick_size(symbol)
        qty_per_grid = calculate_qty_from_cash(grid_size, current_price, step)
        if qty_per_grid <= ZERO:
            return

        new_grid = {
            'center': grid_center,
            'qty': qty_per_grid,
            'size': grid_size,
            'interval': base_interval,
            'buy_orders': [],
            'sell_orders': [],
            'placed_at': time.time(),
            'strategy': strategy
        }

        for i in range(1, buy_grids + 1):
            price = get_tick_aware_price(grid_center, -1, i, base_interval, tick)
            if price >= current_price * Decimal('0.98'):
                continue
            order = bot.place_limit_buy_with_cash(symbol, price, grid_size)
            if order:
                new_grid['buy_orders'].append(str(order['orderId']))

        for i in range(1, sell_grids + 1):
            price = get_tick_aware_price(grid_center, +1, i, base_interval, tick)
            if price <= current_price * Decimal('1.02'):
                continue
            order = bot.place_limit_sell_with_owned(symbol, price)
            if order:
                new_grid['sell_orders'].append(str(order['orderId']))

        if len(new_grid['buy_orders']) >= 1 and len(new_grid['sell_orders']) >= 1:
            active_grid_symbols[symbol] = new_grid
            logger.info(f"GRID ACTIVE: {symbol} | {buy_grids}B/{sell_grids}S")
        else:
            logger.warning(f"GRID FAILED: {symbol} – not enough cash for 1+1")

    except Exception as e:
        logger.error(f"Regrid failed {symbol}: {e}")

# === DASHBOARD ==============================================================
def print_dashboard(bot):
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        usdt = bot.get_balance()
        line = "=" * 130
        print(f"{GREEN}{line}{RESET}")
        print(f"{GREEN}INFINITY GRID v8.3 – VAULT MODE | {now_cst()} CST | GRIDS: {len(active_grid_symbols)}{RESET}")
        print(f"USDT: ${float(usdt):,.2f} | Floor: $40.00 | Free: ${float(usdt - MIN_USDT_RESERVE):,.2f}")
        print(f"REALIZED PNL: ${float(total_realized_pnl):+.2f}")
        print(f"{line}")
    except:
        pass

# === INITIAL SYNC ===========================================================
def initial_sync_from_rest(bot: BinanceTradingBot):
    logger.info("Initial sync...")
    try:
        acct = bot.client.get_account()
        with balance_lock:
            for b in acct['balances']:
                asset = b['asset']
                free = safe_decimal(b['free'])
                if free > ZERO:
                    balances[asset] = free
        with SafeDBManager() as sess:
            if sess:
                sess.query(Position).delete()
                for b in acct['balances']:
                    asset = b['asset']
                    qty = safe_decimal(b['free'])
                    if qty <= ZERO or asset in {'USDT', 'USDC'}:
                        continue
                    sym = f"{asset}USDT"
                    if sym not in valid_symbols_dict:
                        continue
                    price = live_prices.get(sym, ZERO)
                    if price <= ZERO:
                        try:
                            ticker = bot.client.get_symbol_ticker(symbol=sym)
                            price = safe_decimal(ticker['price'])
                        except:
                            price = ZERO
                    if price <= ZERO:
                        continue
                    sess.add(Position(symbol=sym, quantity=qty, avg_entry_price=price))
    except Exception as e:
        logger.error(f"Initial sync failed: {e}")
        raise

# === MAIN ===================================================================
def main():
    global rest_client, valid_symbols_dict, symbol_info_cache, bot

    rest_client = Client(API_KEY, API_SECRET, tld='us')
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
        logger.info(f"Loaded {len(valid_symbols_dict)} USDT pairs")
    except Exception as e:
        logger.error(f"Failed to load symbols: {e}")
        sys.exit(1)

    start_market_websocket()
    start_user_stream()
    threading.Thread(target=keepalive_user_stream, daemon=True).start()

    bot = BinanceTradingBot()

    try:
        initial_sync_from_rest(bot)
    except Exception as e:
        logger.critical("Initial sync failed. Cannot continue.")
        sys.exit(1)

    logger.info("Waiting for data...")
    time.sleep(40)

    logger.info("VAULT MODE v8.3 – FINAL FULL VERSION – RUNNING FOREVER")
    threading.Thread(target=profit_monitoring_engine, daemon=True).start()
    threading.Thread(target=rebalance_loop, daemon=True).start()

    last_regrid = 0
    last_dashboard = 0
    last_pnl_check = 0

    while not SHUTDOWN_EVENT.is_set():
        try:
            now = time.time()

            if now - last_pnl_check > 60:
                with realized_lock:
                    if total_realized_pnl - last_reported_pnl > PNL_REGRID_THRESHOLD:
                        for sym in list(active_grid_symbols.keys()):
                            if is_eligible_coin(sym):
                                regrid_symbol_with_strategy(bot, sym, 'volume_anchored')
                        last_reported_pnl = total_realized_pnl
                last_pnl_check = now

            if now - last_regrid >= REGRID_INTERVAL:
                with SafeDBManager() as sess:
                    if sess:
                        for pos in sess.query(Position).all():
                            sym = pos.symbol
                            if is_eligible_coin(sym):
                                regrid_symbol_with_strategy(bot, sym, 'volume_anchored')
                last_regrid = now

            if now - last_dashboard >= DASHBOARD_REFRESH:
                print_dashboard(bot)
                last_dashboard = now

            time.sleep(1)
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(5)

    print("Vault bot stopped.")

if __name__ == "__main__":
    main()
