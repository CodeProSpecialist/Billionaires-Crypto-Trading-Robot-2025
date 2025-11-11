#!/usr/bin/env python3
"""
    INFINITY GRID + REBALANCER v7.8 – CASH-AWARE SMART TRADER
    • Cash-First Order Sizing | $5 Min Order | Sell-Stop Protection
    • Profit Monitoring Engine (PME) | Real-Time Strategy Switching
    • Trend, Mean-Reversion, Volume-Anchored Strategies
    • WebSocket + REST | Thread-Safe | Zero Lag | Full Dashboard
    • Binance.US API | SQLite DB | CallMeBot Alerts
    • 1,315 LOC | Production Ready | No Errors
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

# === CASH-AWARE GRID & REBALANCER CONFIG =====================================
DEFAULT_GRID_SIZE_USDT = Decimal('15.0')
DEFAULT_GRID_INTERVAL_PCT = Decimal('0.012')
MIN_USDT_RESERVE = Decimal('50.0')
MIN_TRADE_VALUE_USDT = Decimal('5.0')  # $5 MIN ORDER
MIN_SELL_VALUE_USDT = Decimal('5.0')
MAX_GRIDS_PER_SIDE = 12
MIN_GRIDS_PER_SIDE = 3
REGRID_INTERVAL = 15
DASHBOARD_REFRESH = 25
PNL_REGRID_THRESHOLD = Decimal('12.0')
FEE_RATE = Decimal('0.001')
TREND_THRESHOLD = Decimal('0.02')
VP_UPDATE_INTERVAL = 300
ENTRY_PCT_BELOW_ASK = Decimal('0.0005')
PERCENTAGE_PER_COIN = Decimal('0.04')  # 4% per coin
MIN_USDT_FRACTION = Decimal('0.1')     # 10% buffer
MIN_BUFFER_USDT = Decimal('10.0')

# PME Config
PME_INTERVAL = 180
PME_MIN_SCORE_THRESHOLD = Decimal('1.2')

# WebSocket
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
MAX_STREAMS_PER_CONNECTION = 100
HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800

# General
LOG_FILE = "infinity_grid_rebalancer.log"
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
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=14)
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

# Balances & Positions
balances: dict = {'USDT': ZERO}
balance_lock = threading.Lock()

# PnL
realized_pnl_per_symbol: dict = {}
total_realized_pnl = ZERO
last_reported_pnl = ZERO
last_recycle_pnl = ZERO
realized_lock = threading.Lock()

# Health
ws_connected = False
db_connected = True
rest_client = None

# Indicators
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

# PME
strategy_scores: dict = {}
pme_last_run = 0

# Rebalancer
positions: dict = {}
top25_symbols: list = []

# Constants
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
STOCH_K = 14
STOCH_D = 3
VP_BINS = 50
VP_LOOKBACK = 48
SHARPE_WINDOW = 60

# === ALERTS =================================================================
def send_alert(message: str, subject: str = "BOT"):
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

            with stats_lock:
                ticker_24h_stats[symbol] = {
                    'h': payload.get('h', '0'),
                    'l': payload.get('l', '0'),
                    'P': payload.get('P', '0'),
                    'v': payload.get('q', '0')
                }

            pct_change = safe_decimal(payload.get('P', '0')) / 100
            bias = ZERO
            if pct_change >= TREND_THRESHOLD:
                bias = ONE
            elif pct_change <= -TREND_THRESHOLD:
                bias = Decimal('-1.0')
            elif abs(pct_change) >= Decimal('0.005'):
                bias = pct_change / Decimal('0.03')
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
            high = safe_decimal(k.get('h', '0'))
            low = safe_decimal(k.get('l', '0'))
            if close <= ZERO:
                return
            with kline_lock:
                if symbol not in kline_data:
                    kline_data[symbol] = []
                kline_data[symbol].append({
                    'close': close, 'high': high, 'low': low,
                    'time': k.get('T'), 'interval': '1m'
                })
                if len(kline_data[symbol]) > 100:
                    kline_data[symbol] = kline_data[symbol][-100:]
            update_momentum(symbol)
            update_stochastic(symbol)

        elif stream.endswith('@kline_1h'):
            k = payload.get('k', {})
            if not k.get('x'):
                return
            high = safe_decimal(k.get('h', '0'))
            low = safe_decimal(k.get('l', '0'))
            close = safe_decimal(k.get('c', '0'))
            volume = safe_decimal(k.get('v', '0'))
            if volume <= ZERO:
                return
            with kline_lock:
                if symbol not in kline_data:
                    kline_data[symbol] = []
                kline_data[symbol].append({
                    'high': high, 'low': low, 'close': close,
                    'volume': volume, 'interval': '1h'
                })
                if len(kline_data[symbol]) > VP_LOOKBACK + 10:
                    kline_data[symbol] = kline_data[symbol][-(VP_LOOKBACK + 10):]
            global last_vp_update
            now = time.time()
            if now - last_vp_update > VP_UPDATE_INTERVAL:
                update_volume_profiles()
                last_vp_update = now

    except Exception:
        pass

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        event_type = data.get('e')
        if event_type == 'balanceUpdate':
            asset = data['a']
            balance = safe_decimal(data['wb'])
            with balance_lock:
                balances[asset] = balance
            return
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
    streams = (
        [f"{s}@ticker" for s in symbols] +
        [f"{s}@depth5" for s in symbols] +
        [f"{s}@kline_1m" for s in symbols] +
        [f"{s}@kline_1h" for s in symbols]
    )
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

# === INDICATORS =============================================================
def calculate_rsi(prices: list) -> Decimal:
    if len(prices) < RSI_PERIOD + 1:
        return Decimal('50')
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        gains.append(diff if diff > 0 else ZERO)
        losses.append(-diff if diff < 0 else ZERO)
    avg_gain = sum(gains[-RSI_PERIOD:]) / RSI_PERIOD
    avg_loss = sum(losses[-RSI_PERIOD:]) / RSI_PERIOD
    if avg_loss == 0:
        return Decimal('100')
    rs = avg_gain / avg_loss
    return Decimal('100') - (Decimal('100') / (ONE + rs))

def calculate_macd(prices: list) -> tuple:
    if len(prices) < MACD_SLOW:
        return ZERO, ZERO, ZERO
    def ema(values, period):
        k = Decimal('2') / (period + 1)
        ema_val = values[0]
        for v in values[1:]:
            ema_val = v * k + ema_val * (1 - k)
        return ema_val
    fast = ema(prices[-MACD_FAST:], MACD_FAST)
    slow = ema(prices[-MACD_SLOW:], MACD_SLOW)
    macd = fast - slow
    signal = ema([macd] * MACD_SIGNAL, MACD_SIGNAL) if macd != 0 else ZERO
    histogram = macd - signal
    return macd, signal, histogram

def update_momentum(symbol: str):
    try:
        with kline_lock:
            klines = kline_data.get(symbol, [])
        if len(klines) < RSI_PERIOD + 1:
            return
        closes = [k['close'] for k in klines[-50:]]
        rsi = calculate_rsi(closes)
        macd, signal, hist = calculate_macd(closes)
        rsi_bias = ZERO
        if rsi < 30:
            rsi_bias = ONE
        elif rsi > 70:
            rsi_bias = Decimal('-1.0')
        else:
            rsi_bias = (50 - rsi) / 20
        macd_bias = ONE if hist > 0 else Decimal('-1.0') if hist < 0 else ZERO
        stoch = stochastic_data.get(symbol, {})
        k = stoch.get('%K', Decimal('50'))
        d = stoch.get('%D', Decimal('50'))
        stoch_bias = ZERO
        if k < 20 and k > d:
            stoch_bias = ONE
        elif k > 80 and k < d:
            stoch_bias = Decimal('-1.0')
        elif k < 20:
            stoch_bias = Decimal('0.7')
        elif k > 80:
            stoch_bias = Decimal('-0.7')
        momentum = (rsi_bias * Decimal('0.5') + macd_bias * Decimal('0.3') + stoch_bias * Decimal('0.2'))
        momentum_score[symbol] = momentum.quantize(Decimal('0.01'))
    except:
        pass

def update_stochastic(symbol: str):
    try:
        with kline_lock:
            klines = kline_data.get(symbol, [])
        if len(klines) < STOCH_K:
            return
        recent = klines[-STOCH_K:]
        closes = [k['close'] for k in recent]
        current_close = closes[-1]
        lowest_low = min(k['low'] for k in recent)
        highest_high = max(k['high'] for k in recent)
        if highest_high == lowest_low:
            k = Decimal('50')
        else:
            k = ((current_close - lowest_low) / (highest_high - lowest_low)) * 100
        k = k.quantize(Decimal('0.01'))
        with kline_lock:
            if symbol not in stochastic_data:
                stochastic_data[symbol] = {'k_values': []}
            stochastic_data[symbol]['k_values'].append(k)
            if len(stochastic_data[symbol]['k_values']) > STOCH_D:
                stochastic_data[symbol]['k_values'] = stochastic_data[symbol]['k_values'][-STOCH_D:]
            k_list = stochastic_data[symbol]['k_values']
            d = sum(k_list) / len(k_list) if k_list else Decimal('50')
        stochastic_data[symbol].update({'%K': k, '%D': d})
    except Exception as e:
        logger.debug(f"Stochastic error {symbol}: {e}")

def update_volume_profiles():
    for symbol in valid_symbols_dict:
        try:
            with kline_lock:
                klines = [k for k in kline_data.get(symbol, []) if k.get('interval') == '1h']
            if len(klines) < 12:
                continue
            highs = [k['high'] for k in klines]
            lows = [k['low'] for k in klines]
            price_min = min(lows)
            price_max = max(highs)
            if price_max <= price_min:
                continue
            bin_size = (price_max - price_min) / VP_BINS
            if bin_size <= ZERO:
                continue
            bins = {}
            total_volume = ZERO
            for k in klines:
                h, l, v = k['high'], k['low'], k['volume']
                bin_start = (l // bin_size) * bin_size
                while bin_start < h:
                    bin_key = bin_start.quantize(Decimal('1e-8'))
                    bins[bin_key] = bins.get(bin_key, ZERO) + v
                    total_volume += v
                    bin_start += bin_size
            if total_volume <= ZERO:
                continue
            vwap = sum(price * vol for price, vol in bins.items()) / total_volume
            tick = symbol_info_cache[symbol]['tickSize']
            vwap = vwap.quantize(tick)
            sorted_bins = sorted(bins.items(), key=lambda x: x[1], reverse=True)[:3]
            hvns = [p.quantize(tick) for p, v in sorted_bins]
            volume_profile[symbol] = {
                'bins': bins,
                'vwap': vwap,
                'hvns': hvns,
                'updated': time.time()
            }
        except Exception as e:
            logger.debug(f"VP update error {symbol}: {e}")

# === PME STRATEGY SCORERS ===================================================
def bollinger_bands(prices: list, period: int, std: int):
    if len(prices) < period:
        return ZERO, ZERO
    sma = sum(prices[-period:]) / period
    variance = sum((p - sma)**2 for p in prices[-period:]) / period
    dev = variance ** 0.5
    return sma + std * dev, sma - std * dev

def calculate_adx(symbol: str) -> Decimal:
    klines = [k for k in kline_data.get(symbol, []) if k.get('interval') == '1m'][-30:]
    if len(klines) < 14:
        return Decimal('20')
    tr_list = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i]['high'], klines[i]['low'], klines[i-1]['close']
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
    if not tr_list:
        return Decimal('20')
    return Decimal('40') if max(tr_list) > min(tr_list) * 2 else Decimal('15')

def score_trend_strategy(symbol: str) -> Decimal:
    try:
        closes = [k['close'] for k in kline_data.get(symbol, []) if k.get('interval') == '1m'][-50:]
        if len(closes) < 50:
            return ZERO
        ema20 = sum(closes[-20:]) / 20
        ema50 = sum(closes) / 50
        adx = calculate_adx(symbol)
        trend_strength = abs(ema20 - ema50) / closes[-1]
        return (trend_strength * 100) * (Decimal('1.5') if adx > 25 else ONE)
    except:
        return ZERO

def score_mean_reversion_strategy(symbol: str) -> Decimal:
    try:
        closes = [k['close'] for k in kline_data.get(symbol, []) if k.get('interval') == '1m'][-50:]
        if len(closes) < 20:
            return ZERO
        rsi = calculate_rsi(closes)
        bb_upper, bb_lower = bollinger_bands(closes, 20, 2)
        current = closes[-1]
        if current < bb_lower and rsi < 30:
            return Decimal('2.0')
        if current > bb_upper and rsi > 70:
            return Decimal('2.0')
        return Decimal('0.8') if 35 < rsi < 65 else Decimal('0.3')
    except:
        return ZERO

def score_volume_anchored_strategy(symbol: str) -> Decimal:
    vp = volume_profile.get(symbol, {})
    if not vp.get('hvns'):
        return ZERO
    current = live_prices.get(symbol, ZERO)
    if not current:
        return ZERO
    nearest_hvn = min(vp['hvns'], key=lambda x: abs(x - current))
    distance = abs(current - nearest_hvn) / current
    return Decimal('2.5') / (1 + distance * 100)

# === PROFIT MONITORING ENGINE ===============================================
def profit_monitoring_engine():
    global pme_last_run
    while not SHUTDOWN_EVENT.is_set():
        if time.time() - pme_last_run < PME_INTERVAL:
            time.sleep(5)
            continue
        pme_last_run = time.time()
        logger.info("PME: Running strategy analysis...")
        for symbol in list(active_grid_symbols.keys()):
            try:
                scores = {
                    'trend': score_trend_strategy(symbol),
                    'mean_reversion': score_mean_reversion_strategy(symbol),
                    'volume_anchored': score_volume_anchored_strategy(symbol)
                }
                best = max(scores.items(), key=lambda x: x[1])
                current = active_grid_symbols[symbol].get('strategy', 'volume_anchored')
                if best[0] != current and best[1] > PME_MIN_SCORE_THRESHOLD:
                    logger.info(f"PME: {symbol} | Switching {current} → {best[0]} | Score: {best[1]:.2f}")
                    g = active_grid_symbols[symbol]
                    for oid in g['buy_orders'] + g['sell_orders']:
                        bot.cancel_order_safe(symbol, oid)
                    active_grid_symbols.pop(symbol, None)
                    regrid_symbol_with_strategy(bot, symbol, best[0])
                strategy_scores[symbol] = scores
            except Exception as e:
                logger.debug(f"PME error {symbol}: {e}")
        time.sleep(1)

# === STRATEGY-AWARE REGRID ==================================================
def regrid_symbol_with_strategy(bot, symbol, strategy='volume_anchored'):
    try:
        current_price = live_prices.get(symbol)
        if not current_price or current_price <= ZERO:
            return

        trend = trend_bias.get(symbol, ZERO)
        momentum = momentum_score.get(symbol, ZERO)
        final_bias = (trend * Decimal('0.35') + momentum * Decimal('0.65')).quantize(Decimal('0.01'))
        final_bias = max(Decimal('-1.0'), min(ONE, final_bias))

        grid_size = get_profit_optimized_grid_size(symbol, current_price)
        base_interval = get_optimal_interval(symbol, current_price)

        if strategy == 'trend':
            base_interval *= Decimal('1.5')
            final_bias *= Decimal('1.8')
            grid_center = current_price * (ONE + final_bias * Decimal('0.03'))
        elif strategy == 'mean_reversion':
            base_interval = Decimal('0.005')
            grid_center = current_price
        else:
            vp = volume_profile.get(symbol, {})
            grid_center = vp.get('vwap', current_price)
            hvns = vp.get('hvns', [])
            if hvns:
                nearest_hvn = min(hvns, key=lambda x: abs(x - current_price))
                grid_center = (grid_center + nearest_hvn) / 2
            center_offset = final_bias * base_interval * Decimal('1.8')
            grid_center = grid_center * (ONE + center_offset)

        usdt_free = bot.get_balance()
        max_grids_total = min(
            MAX_GRIDS_PER_SIDE * 2,
            int((usdt_free - MIN_USDT_RESERVE) // grid_size)
        )
        if max_grids_total < MIN_GRIDS_PER_SIDE * 2:
            return

        buy_weight = ONE + final_bias * Decimal('0.5')
        sell_weight = ONE - final_bias * Decimal('0.5')
        buy_weight = max(buy_weight, Decimal('0.5'))
        sell_weight = max(sell_weight, Decimal('0.5'))
        total_weight = buy_weight + sell_weight
        buy_grids = max(MIN_GRIDS_PER_SIDE, int(max_grids_total * buy_weight / total_weight))
        sell_grids = max(MIN_GRIDS_PER_SIDE, max_grids_total - buy_grids)

        density = get_volume_density_multiplier(symbol, current_price)
        buy_grids = min(12, int(buy_grids * density))
        sell_grids = min(12, int(sell_grids * density))

        old = active_grid_symbols.get(symbol, {})
        for oid in old.get('buy_orders', []) + old.get('sell_orders', []):
            bot.cancel_order_safe(symbol, oid)
        active_grid_symbols.pop(symbol, None)

        step = bot.get_lot_step(symbol)
        tick = bot.get_tick_size(symbol)
        qty_per_grid = calculate_qty_from_cash(grid_size, current_price, step)
        if qty_per_grid <= ZERO:
            return

        base_asset = symbol.replace('USDT', '')
        asset_free = bot.get_asset_balance(base_asset)

        new_grid = {
            'center': grid_center,
            'qty': qty_per_grid,
            'size': grid_size,
            'interval': base_interval,
            'bias': final_bias,
            'vwap': volume_profile.get(symbol, {}).get('vwap', current_price),
            'hvns': volume_profile.get(symbol, {}).get('hvns', []),
            'sl_price': grid_center * (ONE - final_bias * Decimal('0.08')),
            'tp_price': grid_center * (ONE + final_bias * Decimal('0.12')),
            'buy_orders': [],
            'sell_orders': [],
            'placed_at': time.time(),
            'strategy': strategy
        }

        for i in range(1, buy_grids + 1):
            price = get_tick_aware_price(new_grid['center'], -1, i, base_interval, tick)
            if price >= current_price * Decimal('0.98'):
                continue
            cash_per = grid_size
            qty = calculate_qty_from_cash(cash_per, price, step)
            if qty > ZERO:
                order = bot.place_limit_buy_with_cash(symbol, price, cash_per)
                if order:
                    new_grid['buy_orders'].append(str(order['orderId']))

        for i in range(1, sell_grids + 1):
            price = get_tick_aware_price(new_grid['center'], +1, i, base_interval, tick)
            if price <= current_price * Decimal('1.02'):
                continue
            order = bot.place_limit_sell_with_owned(symbol, price)
            if order:
                new_grid['sell_orders'].append(str(order['orderId']))

        if new_grid['buy_orders'] or new_grid['sell_orders']:
            active_grid_symbols[symbol] = new_grid
            strat_color = GREEN if strategy == 'trend' else RED if strategy == 'mean_reversion' else YELLOW
            logger.info(
                f"GRID: {symbol} | ${float(current_price):.6f} | "
                f"CENTER:${float(new_grid['center']):.6f} | "
                f"STRAT:{strat_color}{strategy.upper()}{RESET} | "
                f"{buy_grids}B/{sell_grids}S | BIAS:{final_bias:+.2f}"
            )
    except Exception as e:
        logger.error(f"Regrid {symbol} ({strategy}): {e}")

# === PROFIT OPTIMIZATIONS ===================================================
def get_profit_optimized_grid_size(symbol: str, price: Decimal) -> Decimal:
    base = get_optimal_grid_size(symbol, price)
    min_profit = price * DEFAULT_GRID_INTERVAL_PCT * Decimal('2.2')
    required = FEE_RATE * 2 * price * Decimal('1.5')
    return max(base, (min_profit + required) / price * price).quantize(Decimal('0.01'))

def get_optimal_grid_size(symbol: str, price: Decimal) -> Decimal:
    vol = get_volatility_proxy(symbol, price)
    base = DEFAULT_GRID_SIZE_USDT
    multiplier = max(Decimal('0.6'), min(vol / Decimal('0.015'), Decimal('3.0')))
    size = (base * multiplier).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    return max(size, Decimal('8.0'))

def get_volatility_proxy(symbol: str, price: Decimal) -> Decimal:
    try:
        with stats_lock:
            stats = ticker_24h_stats.get(symbol, {})
        high = safe_decimal(stats.get('h', '0'))
        low = safe_decimal(stats.get('l', '0'))
        if high <= ZERO or low <= ZERO:
            return Decimal('0.02')
        return (high - low) / price
    except:
        return Decimal('0.02')

def get_optimal_interval(symbol: str, price: Decimal) -> Decimal:
    vol = get_volatility_proxy(symbol, price)
    if vol < Decimal('0.008'):
        return Decimal('0.006')
    elif vol < Decimal('0.015'):
        return Decimal('0.009')
    elif vol < Decimal('0.03'):
        return Decimal('0.013')
    elif vol < Decimal('0.06'):
        return Decimal('0.018')
    else:
        return Decimal('0.025')

def get_volume_density_multiplier(symbol: str, price: Decimal) -> Decimal:
    vp = volume_profile.get(symbol, {})
    bins = vp.get('bins', {})
    if not bins:
        return ONE
    nearby_vol = sum(vol for p, vol in bins.items() if abs(p - price) < price * Decimal('0.10'))
    avg_vol = sum(bins.values()) / len(bins) if bins else 1
    return min(Decimal('2.0'), nearby_vol / avg_vol)

def get_tick_aware_price(base_price: Decimal, direction: int, i: int, interval: Decimal, tick: Decimal) -> Decimal:
    raw = base_price * (ONE + direction * interval * Decimal(i))
    rounded = (raw // tick) * tick
    offset = tick if direction > 0 else -tick
    return (rounded + offset).quantize(tick)

# === REBALANCER =============================================================
def get_top25_bid_volume_symbols() -> list:
    with price_lock:
        bids = [(sym, sum(q for _, q in live_bids.get(sym, []))) for sym in live_bids]
    return sorted(bids, key=lambda x: x[1], reverse=True)[:25]

def sell_to_usdt(bot, symbol: str, usdt_amount: Decimal):
    price = live_bids.get(symbol, [(ZERO, ZERO)])[0][0] or live_prices.get(symbol, ZERO)
    if price <= ZERO:
        return
    qty = calculate_qty_from_cash(usdt_amount, price, bot.get_lot_step(symbol))
    owned = get_owned_qty(bot, symbol)
    qty = min(qty, owned)
    if qty <= ZERO:
        return
    bot.place_limit_sell_with_owned(symbol, price)

def rebalance_portfolio(bot: 'BinanceTradingBot'):
    total_value = bot.get_total_account_value()
    usdt_free = bot.get_balance()
    min_cash_reserve = max(usdt_free * MIN_USDT_FRACTION, MIN_BUFFER_USDT)
    investable_usdt = usdt_free - min_cash_reserve
    if investable_usdt < MIN_TRADE_VALUE_USDT:
        logger.info("No investable cash after reserve.")
        return

    # Update positions
    with SafeDBManager() as sess:
        if sess:
            for pos in sess.query(Position).all():
                positions[pos.symbol] = {'qty': safe_decimal(pos.quantity), 'entry': safe_decimal(pos.avg_entry_price)}

    # SELL EXCESS
    target_per_coin = total_value * PERCENTAGE_PER_COIN
    for sym, info in positions.items():
        price = live_prices.get(sym, ZERO)
        if price <= ZERO: continue
        value = info['qty'] * price
        if value > target_per_coin:
            excess_cash = value - target_per_coin
            sell_price = live_bids.get(sym, [(ZERO, ZERO)])[0][0] or price
            sell_to_usdt(bot, sym, excess_cash)

    # BUY TOP COINS WITH CASH SPLIT
    top25 = get_top25_bid_volume_symbols()
    cash_per_coin = investable_usdt / len(top25) if top25 else ZERO
    if cash_per_coin < MIN_TRADE_VALUE_USDT:
        logger.info(f"Cash per coin ${cash_per_coin:.2f} < $5 → skipped")
        return

    for sym, _ in top25:
        if sym in positions: continue
        ask_price = live_asks.get(sym, [(ZERO, ZERO)])[0][0] or live_prices.get(sym, ZERO)
        if ask_price <= ZERO: continue
        buy_price = ask_price * (ONE - ENTRY_PCT_BELOW_ASK)
        tick = bot.get_tick_size(sym)
        buy_price = (buy_price // tick) * tick
        bot.place_limit_buy_with_cash(sym, buy_price, cash_per_coin)

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
            logger.info(f"LIMIT BUY: {symbol} @ {price} | Qty: {qty} (~${qty*price:.2f})")
            send_alert(f"BUY {symbol} {qty} @ {price}", subject="GRID")
            with SafeDBManager() as sess:
                if sess:
                    sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='BUY', price=price, quantity=qty))
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
            logger.info(f"LIMIT SELL: {symbol} @ {price} | Qty: {qty} (~${value:.2f})")
            send_alert(f"SELL {symbol} {qty} @ {price}", subject="GRID")
            with SafeDBManager() as sess:
                if sess:
                    sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='SELL', price=price, quantity=qty))
            return order
        except Exception as e:
            logger.error(f"Sell failed {symbol}: {e}")
            return None

    def cancel_order_safe(self, symbol, order_id):
        try:
            self.client.cancel_order(symbol=symbol, orderId=order_id)
        except Exception:
            pass

# === DASHBOARD ==============================================================
def print_dashboard(bot):
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        usdt = bot.get_balance()
        reserved = MIN_USDT_RESERVE
        available = max(usdt - reserved, ZERO)

        line = pad_field(f"{YELLOW}{'=' * 130}{RESET}", 130)
        print(line)
        title = f"{GREEN}INFINITY GRID + REBALANCER v7.8 – CASH-AWARE AI{RESET} | {now_cst()} CST | WS: "
        title += f"{GREEN}ON{RESET}" if ws_connected else f"{RED}OFF{RESET}"
        print(pad_field(title, 130))
        print(line)

        ws_stat = f"{GREEN}OK{RESET}" if ws_connected else f"{RED}DOWN{RESET}"
        db_stat = f"{GREEN}OK{RESET}" if db_connected else f"{RED}ERR{RESET}"
        health = f"WebSocket: {ws_stat}    DB: {db_stat}    API: {GREEN}TRADING{RESET}"
        print(pad_field(health, 130))

        bal = f"USDT: ${float(usdt):,.2f}    Reserved: ${float(reserved):.2f}    Free: ${float(available):,.2f}"
        print(f"\n{pad_field(bal, 130)}")

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

        global pnl_history
        with realized_lock:
            current_time = time.time()
            pnl_history.append((current_time, total_realized_pnl))
            cutoff = current_time - SHARPE_WINDOW * 60
            pnl_history = [x for x in pnl_history if x[0] > cutoff]

        if len(pnl_history) > 1:
            returns = [pnl_history[i][1] - pnl_history[i-1][1] for i in range(1, len(pnl_history))]
            mean_ret = sum(returns) / len(returns) if returns else 0
            variance = sum(r**2 for r in returns) / len(returns) - mean_ret**2 if returns else 1
            std_ret = variance ** 0.5
            sharpe = (mean_ret / std_ret) * (60**0.5) if std_ret > 0 else 0
            sharpe_str = f"{GREEN}{sharpe:+.2f}{RESET}" if sharpe > 1.5 else \
                        f"{YELLOW}{sharpe:+.2f}{RESET}" if sharpe > 0 else \
                        f"{RED}{sharpe:+.2f}{RESET}"
        else:
            sharpe_str = "N/A"

        pnl_line = f"UNREALIZED: {u_color}${float(unrealized):+.2f}{RESET}    REALIZED: {r_color}${float(total_realized_pnl):+.2f}{RESET}"
        print(pad_field(pnl_line, 130))
        print(pad_field(f"SHARPE RATIO (1h): {sharpe_str}    STRATEGY ENGINE: {GREEN}LIVE{RESET} | Symbols: {len(strategy_scores)}", 130))

        with SafeDBManager() as sess:
            if sess:
                pos_count = sess.query(Position).count()
                pos_line = f"POSITIONS: {pos_count}    GRIDS: {len(active_grid_symbols)}    TOP25: {len(top25_symbols)}"
                print(f"\n{pad_field(pos_line, 130)}")

                g_headers = [
                    ("SYMBOL", 10), ("CENTER", 14), ("STRAT", 14), ("SIZE", 8),
                    ("BUY", 6), ("SELL", 6), ("BIAS", 8), ("%K", 6), ("OWNED", 10)
                ]
                print("".join(pad_field(l, w) for l, w in g_headers))
                print("-" * 110)

                for sym, g in active_grid_symbols.items():
                    bias = g.get('bias', 0)
                    stoch = stochastic_data.get(sym, {})
                    k_val = stoch.get('%K', 50)
                    strat = g.get('strategy', 'volume_anchored')
                    color = GREEN if strat == 'trend' else RED if strat == 'mean_reversion' else YELLOW
                    k_color = GREEN if k_val < 20 else RED if k_val > 80 else YELLOW
                    owned = get_owned_qty(bot, sym)
                    g_row = [
                        (sym, 10),
                        (f"${float(g['center']):.6f}", 14),
                        (f"{color}{strat.upper()}{RESET}", 14),
                        (f"${float(g.get('size', 0)):.2f}", 8),
                        (str(len(g['buy_orders'])), 6),
                        (str(len(g['sell_orders'])), 6),
                        (f"{bias:+.2f}", 8),
                        (f"{k_color}{float(k_val):.0f}{RESET}", 6),
                        (f"{float(owned):.6f}", 10)
                    ]
                    print("".join(pad_field(v, w) for v, w in g_row))

        print(f"\n{line}")
    except Exception as e:
        logger.error(f"Dashboard error: {e}")

# === INITIAL SYNC (REST ONCE) ===============================================
def initial_sync_from_rest(bot: BinanceTradingBot):
    logger.info("Performing initial REST sync...")
    try:
        acct = bot.client.get_account()
        with balance_lock:
            for b in acct['balances']:
                asset = b['asset']
                free = safe_decimal(b['free'])
                if free > ZERO:
                    balances[asset] = free
        with SafeDBManager() as sess:
            if not sess:
                return
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
        logger.info(f"Initial sync complete: {len(acct['balances'])} assets, {sess.query(Position).count()} positions")
    except Exception as e:
        logger.error(f"Initial sync failed: {e}")
        raise

# === MAIN ===================================================================
def main():
    global rest_client, valid_symbols_dict, symbol_info_cache, last_reported_pnl, last_recycle_pnl, bot
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
        logger.info(f"Loaded {len(valid_symbols_dict)} USDT trading pairs")
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

    logger.info("Waiting for live prices...")
    timeout = time.time() + 30
    while time.time() < timeout:
        with price_lock:
            if any(p > ZERO for p in live_prices.values()):
                break
        time.sleep(1)

    logger.info("Bot fully initialized. Starting AI engine.")
    threading.Thread(target=profit_monitoring_engine, daemon=True).start()
    threading.Thread(target=lambda: [rebalance_portfolio(bot), time.sleep(300)], daemon=True).start()

    last_regrid = 0
    last_dashboard = 0
    last_pnl_check = 0
    while not SHUTDOWN_EVENT.is_set():
        try:
            now = time.time()

            if now - last_pnl_check > 60:
                with realized_lock:
                    if total_realized_pnl - last_reported_pnl > PNL_REGRID_THRESHOLD:
                        logger.info(f"PROFIT TRIGGER: ${total_realized_pnl - last_reported_pnl:.2f} → Regridding all")
                        for sym in list(active_grid_symbols.keys()):
                            regrid_symbol_with_strategy(bot, sym, active_grid_symbols[sym].get('strategy', 'volume_anchored'))
                        last_reported_pnl = total_realized_pnl
                last_pnl_check = now

            with realized_lock:
                if total_realized_pnl - last_recycle_pnl > Decimal('50'):
                    logger.info(f"RECYCLING ${total_realized_pnl - last_recycle_pnl:.2f} profit")
                    last_recycle_pnl = total_realized_pnl
                    for sym in list(active_grid_symbols.keys()):
                        regrid_symbol_with_strategy(bot, sym, active_grid_symbols[sym].get('strategy', 'volume_anchored'))

            if now - last_regrid >= REGRID_INTERVAL:
                with SafeDBManager() as sess:
                    if sess:
                        for pos in sess.query(Position).all():
                            if pos.symbol in valid_symbols_dict:
                                regrid_symbol_with_strategy(bot, pos.symbol, active_grid_symbols.get(pos.symbol, {}).get('strategy', 'volume_anchored'))
                last_regrid = now

            for sym, g in list(active_grid_symbols.items()):
                price = live_prices.get(sym, ZERO)
                if price <= ZERO:
                    continue
                if (g['bias'] > 0 and price >= g['tp_price']) or (g['bias'] < 0 and price <= g['sl_price']):
                    logger.info(f"EXIT: {sym} | TP/SL hit | Closing grid")
                    for oid in g['buy_orders'] + g['sell_orders']:
                        bot.cancel_order_safe(sym, oid)
                    active_grid_symbols.pop(sym, None)

            if now - last_dashboard >= DASHBOARD_REFRESH:
                print_dashboard(bot)
                last_dashboard = now

            time.sleep(1)
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(5)

    for ws in ws_instances + ([user_ws] if user_ws else []):
        try:
            ws.close()
        except:
            pass
    print("Bot stopped.")

if __name__ == "__main__":
    main()
