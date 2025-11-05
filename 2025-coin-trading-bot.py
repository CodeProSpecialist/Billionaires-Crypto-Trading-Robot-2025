#!/usr/bin/env python3
"""
    BINANCE INFINITY GRID + TRAILING HYBRID BOT
    • $5 per grid • Top 10 by BUY ORDERS + 24H VOLUME
    • 4–16 grids when USDT ≥ $40
    • Grid orders NEVER cancel (infinity mode)
    • Fallback to trailing momentum if < $40
    • Full dashboard, DB, WhatsApp alerts, rate-limit safe
    • $5 MIN NOTIONAL | $10 BUFFER
    • RSI + MACD + Histogram + RSI Div + Volume Div
"""

import os
import sys
import time
import logging
import numpy as np
import talib
import requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List

from colorama import init, Fore, Style

from collections import deque
import threading
import pytz
from logging.handlers import TimedRotatingFileHandler

from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, func
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import SQLAlchemyError

# === CONFIGURATION ===========================================================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    print("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET env vars")
    sys.exit(1)

# Grid Config
GRID_SIZE_USDT = Decimal('5.0')
MIN_SELL_NOTIONAL_USDT = Decimal('5.0')
MIN_BUY_NOTIONAL_USDT = Decimal('5.0')
MIN_BUFFER_USDT = Decimal('10.0')
GRID_INTERVAL_PCT = Decimal('0.015')  # 1.5%
MAX_GRIDS_HIGH_BALANCE = 16
MAX_SCALED_COINS = 10

# Priority Weights
BUY_VOLUME_WEIGHT = 0.70
VOLUME_24H_WEIGHT = 0.30

# Filters
MIN_24H_VOLUME_USDT = 60000
RSI_PERIOD = 14
RSI_MOMENTUM_THRESHOLD = 50.0
RSI_MOMENTUM_LOOKBACK = 3
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MACD_MIN_LOOKBACK = 3
MACD_MAX_LOOKBACK = 8
MACD_HIST_EXPANSION_LOOKBACK = 3
MACD_HIST_MIN_THRESHOLD = 0.0
RSI_DIVERGENCE_LOOKBACK = 15
RSI_DIVERGENCE_THRESHOLD = 0.003
VOLUME_DIVERGENCE_LOOKBACK = 15
VOLUME_DIVERGENCE_THRESHOLD = 0.15
BEARISH_DIVERGENCE_LOOKBACK = 12
BEARISH_DIVERGENCE_THRESHOLD = 0.001

# General
MAX_PRICE = 1000.00
MIN_PRICE = 0.15
LOG_FILE = "crypto_trading_bot.log"
PROFIT_TARGET_NET = Decimal('0.008')
RISK_PER_TRADE = 0.10
MIN_BALANCE = Decimal('2.0')
ORDERBOOK_SELL_PRESSURE_THRESHOLD = 0.60
ORDERBOOK_BUY_PRESSURE_SPIKE = 0.65
ORDERBOOK_BUY_PRESSURE_DROP = 0.55
ORDER_BOOK_LEVELS = 50
DEPTH_IMBALANCE_THRESHOLD = 2.0
POLL_INTERVAL = 1.0
STALL_THRESHOLD_SECONDS = 15 * 60
RAPID_DROP_THRESHOLD = 0.01
RAPID_DROP_WINDOW = 5.0
ATR_TRAIL_MULTIPLIER_BUY = Decimal('1.0')
BB_PERIOD = 20
BB_DEV = 2
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# === CONSTANTS ==============================================================
HUNDRED = Decimal('100')
ZERO = Decimal('0')

# === LOGGING ================================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7)
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s')
    file_handler.setFormatter(file_formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s %(levelname)s:%(message)s')
    console_handler.setFormatter(console_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

CST_TZ = pytz.timezone('America/Chicago')

# === GLOBAL STATE ===========================================================
valid_symbols_dict: Dict[str, dict] = {}
order_book_cache: Dict[str, dict] = {}
positions: Dict[str, dict] = {}
buy_pressure_history: Dict[str, deque] = {}
sell_pressure_history: Dict[str, deque] = {}
last_price_cache: Dict[str, Tuple[float, float]] = {}
trailing_buy_active: Dict[str, dict] = {}
trailing_sell_active: Dict[str, dict] = {}
buy_cooldown: Dict[str, float] = {}
price_history: Dict[str, deque] = {}
active_grid_symbols: Dict[str, dict] = {}

# === SAFE MATH ==============================================================
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

# === NOTIONAL & BUFFER GUARDS ===============================================
def sell_notional_ok(symbol: str, price: Decimal, qty: Decimal) -> bool:
    if price <= ZERO or qty <= ZERO:
        return False
    return price * qty >= MIN_SELL_NOTIONAL_USDT

def buy_notional_ok(symbol: str, price: Decimal, qty: Decimal) -> bool:
    if price <= ZERO or qty <= ZERO:
        return False
    return price * qty >= MIN_BUY_NOTIONAL_USDT

def has_enough_usdt_for(bot: 'BinanceTradingBot', notional_usdt: Decimal) -> bool:
    free = bot.get_balance('USDT')
    required = notional_usdt + MIN_BUFFER_USDT
    return free >= required

# === DATABASE ===============================================================
DB_URL = "sqlite:///binance_trades.db"
engine = create_engine(DB_URL, echo=False, future=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

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
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())

db_path = "binance_trades.db"
if not os.path.exists(db_path):
    Base.metadata.create_all(engine)
    logger.info(f"Database '{db_path}' created.")
else:
    logger.info(f"Database '{db_path}' already exists.")

class DBManager:
    def __enter__(self):
        self.session = SessionFactory()
        return self.session
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            try:
                self.session.commit()
            except:
                self.session.rollback()
                raise
        else:
            self.session.rollback()
        self.session.close()

# === RATE LIMIT MANAGER =====================================================
class RateManager:
    def __init__(self):
        self.limits = {}
        self.current = {}
        self.last_reset = {}
        self._fetch_limits()

    def _fetch_limits(self):
        try:
            info = Client().get_exchange_info()
            for rate in info.get('rateLimits', []):
                self.limits[rate['rateLimitType']] = {
                    'interval': rate['interval'],
                    'limit': rate['limit']
                }
        except:
            self.limits = {
                'REQUEST_WEIGHT': {'interval': 'MINUTE', 'limit': 1200},
                'ORDERS': {'interval': 'SECOND', 'limit': 10},
                'RAW_REQUESTS': {'interval': 'MINUTE', 'limit': 6100}
            }

    def update_current(self, headers=None):
        if not headers:
            return
        for key in ['X-MBX-USED-WEIGHT-1M', 'X-MBX-ORDER-COUNT-10S']:
            if key in headers:
                self.current[key] = int(headers[key])

    def is_close(self, limit_type, threshold=0.9):
        limit = self.limits.get(limit_type, {})
        current = self.current.get(f'X-MBX-USED-WEIGHT-1M' if limit_type == 'REQUEST_WEIGHT' else 'X-MBX-ORDER-COUNT-10S', 0)
        return current >= limit.get('limit', 1000) * threshold

    def wait_if_needed(self, limit_type):
        if self.is_close(limit_type):
            wait = self._calc_wait(limit_type)
            logger.warning(f"Rate limit near: sleeping {wait}s")
            time.sleep(wait)

    def _calc_wait(self, limit_type):
        interval = self.limits[limit_type]['interval']
        if interval == 'SECOND':
            return 1.0
        elif interval == 'MINUTE':
            return 60.0
        return 1.0

# === RETRY DECORATOR ========================================================
def retry_custom(max_retries=3, delay=1, backoff=2):
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except BinanceAPIException as e:
                    if e.code in [-1003, -1021]:
                        time.sleep(delay * (backoff ** retries))
                        retries += 1
                    else:
                        raise
                except Exception as e:
                    logger.error(f"Retry error: {e}")
                    time.sleep(delay * (backoff ** retries))
                    retries += 1
            return func(*args, **kwargs)
        return wrapper
    return decorator

# === WHATSAPP ALERT =========================================================
def send_whatsapp_alert(message: str):
    if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE:
        return
    url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={message}&apikey={CALLMEBOT_API_KEY}"
    try:
        requests.get(url, timeout=5)
    except:
        pass

# === MAIN BOT CLASS =========================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET)
        self.rate_manager = RateManager()
        self.api_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.fetch_and_validate_usdt_pairs()
        self.load_state_from_db()

    # === VALIDATE ORDER =====================================================
    def validate_and_adjust_order(self, symbol: str, price: Decimal, qty: Decimal, side: str) -> Tuple[Decimal, Decimal]:
        filters = valid_symbols_dict[symbol]['filters']
        for f in filters:
            if f['filterType'] == 'LOT_SIZE':
                step = Decimal(str(f['stepSize']))
                min_qty = Decimal(str(f['minQty']))
                qty = max(min_qty, ((qty - min_qty) // step) * step + min_qty)
            elif f['filterType'] == 'MIN_NOTIONAL':
                min_notional = Decimal(str(f['minNotional']))
                if price * qty < min_notional:
                    qty = (min_notional / price).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
        return price, qty

    # === LOAD STATE FROM DB =================================================
    def load_state_from_db(self):
        with DBManager() as db:
            for pos in db.query(Position).all():
                positions[pos.symbol] = {
                    'qty': pos.quantity,
                    'entry': pos.avg_entry_price,
                    'fee': pos.buy_fee_rate
                }

    # === FETCH & VALIDATE USDT PAIRS ========================================
    def fetch_and_validate_usdt_pairs(self):
        global valid_symbols_dict
        try:
            exchange_info = self.client.get_exchange_info()
            tickers = self.client.get_ticker()
            ticker_dict = {t['symbol']: t for t in tickers}
            valid_symbols_dict.clear()
            for symbol_info in exchange_info['symbols']:
                symbol = symbol_info['symbol']
                if not symbol.endswith('USDT') or symbol_info['status'] != 'TRADING':
                    continue
                ticker = ticker_dict.get(symbol, {})
                price = safe_float(ticker.get('lastPrice', 0))
                low_24h = safe_float(ticker.get('lowPrice', 0))
                vol_24h = safe_float(ticker.get('quoteVolume', 0))
                if not (MIN_PRICE <= price <= MAX_PRICE and low_24h > 0 and vol_24h >= MIN_24H_VOLUME_USDT):
                    continue
                valid_symbols_dict[symbol] = {
                    'price': price,
                    'low_24h': low_24h,
                    'volume_24h': vol_24h,
                    'filters': symbol_info['filters']
                }
            logger.info(f"Validated {len(valid_symbols_dict)} USDT pairs")
        except Exception as e:
            logger.error(f"Validation error: {e}")

    # === ORDER BOOK ANALYSIS ================================================
    def get_order_book_analysis(self, symbol) -> dict:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            ob = self.client.get_order_book(symbol=symbol, limit=ORDER_BOOK_LEVELS)
            self.rate_manager.update_current()
        bids = [(float(b[0]), float(b[1])) for b in ob['bids'][:ORDER_BOOK_LEVELS]]
        asks = [(float(a[0]), float(a[1])) for a in ob['asks'][:ORDER_BOOK_LEVELS]]
        cum_bid = sum(qty for _, qty in bids)
        cum_ask = sum(qty for _, qty in asks)
        return {
            'best_bid': Decimal(str(bids[0][0])) if bids else ZERO,
            'best_ask': Decimal(str(asks[0][0])) if asks else ZERO,
            'cum_bid_vol': cum_bid,
            'cum_ask_vol': cum_ask,
            'pct_bid': cum_bid / (cum_bid + cum_ask + 1e-8) if (cum_bid + cum_ask) > 0 else 0.5,
            'pct_ask': cum_ask / (cum_bid + cum_ask + 1e-8) if (cum_bid + cum_ask) > 0 else 0.5
        }

    # === RSI + TREND ========================================================
    def get_rsi_and_trend(self, symbol) -> Tuple[Optional[float], str, Optional[float]]:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=100)
            self.rate_manager.update_current()
        closes = np.array([safe_float(k[4]) for k in klines[-100:]])
        if len(closes) < RSI_PERIOD:
            return None, "unknown", None
        rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)[-1]
        if not np.isfinite(rsi):
            return None, "unknown", None
        upper, middle, lower = talib.BBANDS(closes, timeperiod=BB_PERIOD, nbdevup=BB_DEV, nbdevdn=BB_DEV)
        macd, sig, _ = talib.MACD(closes, fastperiod=MACD_FAST, slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)
        trend = ("bullish" if closes[-1] > middle[-1] and macd[-1] > sig[-1]
                 else "bearish" if closes[-1] < middle[-1] and macd[-1] < sig[-1] else "sideways")
        low_24h = valid_symbols_dict.get(symbol, {}).get('low_24h')
        return float(rsi), trend, float(low_24h) if low_24h else None

    # === BALANCE & FEES =====================================================
    @retry_custom()
    def get_balance(self, asset: str) -> Decimal:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            info = self.client.get_account()
            self.rate_manager.update_current()
        for b in info['balances']:
            if b['asset'] == asset:
                return to_decimal(b['free'])
        return ZERO

    @retry_custom()
    def get_trade_fees(self, symbol: str) -> Tuple[float, float]:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            fees = self.client.get_trade_fee(symbol=symbol)
            self.rate_manager.update_current()
        maker = 0.001
        taker = 0.001
        for f in fees.get('tradeFee', []):
            if f['symbol'] == symbol:
                maker = float(f['maker'])
                taker = float(f['taker'])
                break
        return maker, taker

    # === ATR ================================================================
    def get_ATR(self, symbol: str, period: int = 14) -> float:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=period + 1)
            self.rate_manager.update_current()
        if len(klines) < period + 1:
            return 0.0
        highs = np.array([safe_float(k[2]) for k in klines])
        lows = np.array([safe_float(k[3]) for k in klines])
        closes = np.array([safe_float(k[4]) for k in klines])
        atr = talib.ATR(highs, lows, closes, timeperiod=period)
        return float(atr[-1]) if len(atr) > 0 and np.isfinite(atr[-1]) else 0.0

    # === RSI MOMENTUM =======================================================
    def get_rsi_momentum(self, symbol: str) -> Tuple[bool, Optional[float], Optional[List[float]]]:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=RSI_PERIOD + 3)
            self.rate_manager.update_current()
        if len(klines) < RSI_PERIOD + 3:
            return False, None, None
        closes = np.array([safe_float(k[4]) for k in klines])
        rsi_series = talib.RSI(closes, timeperiod=RSI_PERIOD)
        if len(rsi_series) < 3:
            return False, None, None
        rsi_vals = [float(rsi_series[-3]), float(rsi_series[-2]), float(rsi_series[-1])]
        current_rsi = rsi_vals[-1]
        momentum_up = (current_rsi > RSI_MOMENTUM_THRESHOLD and rsi_vals[-1] > rsi_vals[-2] > rsi_vals[-3])
        return momentum_up, current_rsi, rsi_vals

    # === MACD + HISTOGRAM ===================================================
    def get_macd_bullish_with_histogram(self, symbol: str) -> Tuple[bool, int, Optional[float]]:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=MACD_SLOW + 20)
            self.rate_manager.update_current()
        if len(klines) < MACD_SLOW + 20:
            return False, 0, None
        closes = np.array([safe_float(k[4]) for k in klines])
        macd, signal, hist = talib.MACD(closes, fastperiod=MACD_FAST, slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)
        if len(macd) < MACD_MAX_LOOKBACK or len(hist) < MACD_HIST_EXPANSION_LOOKBACK:
            return False, 0, None
        lookback = 5
        recent_macd = macd[-lookback:]
        recent_sig = signal[-lookback:]
        macd_above = all(m > s for m, s in zip(recent_macd, recent_sig))
        recent_hist = hist[-MACD_HIST_EXPANSION_LOOKBACK:]
        hist_positive = all(h > MACD_HIST_MIN_THRESHOLD for h in recent_hist)
        hist_expanding = all(recent_hist[i] < recent_hist[i+1] for i in range(len(recent_hist)-1))
        current_hist = float(hist[-1]) if len(hist) > 0 else None
        is_bullish = macd_above and hist_positive and hist_expanding
        return is_bullish, lookback, current_hist

    # === RSI BULLISH DIVERGENCE ============================================
    def detect_rsi_bullish_divergence(self, symbol: str) -> bool:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=RSI_DIVERGENCE_LOOKBACK + 5)
            self.rate_manager.update_current()
        if len(klines) < RSI_DIVERGENCE_LOOKBACK:
            return False
        closes = np.array([safe_float(k[4]) for k in klines])
        rsi_series = talib.RSI(closes, timeperiod=RSI_PERIOD)
        if len(rsi_series) < RSI_DIVERGENCE_LOOKBACK:
            return False
        recent_closes = closes[-RSI_DIVERGENCE_LOOKBACK:]
        recent_rsi = rsi_series[-RSI_DIVERGENCE_LOOKBACK:]
        price_lows = []
        for i in range(1, len(recent_closes) - 1):
            if recent_closes[i] < recent_closes[i-1] and recent_closes[i] < recent_closes[i+1]:
                price_lows.append((i, recent_closes[i]))
        rsi_lows = []
        for i in range(1, len(recent_rsi) - 1):
            if recent_rsi[i] < recent_rsi[i-1] and recent_rsi[i] < recent_rsi[i+1]:
                rsi_lows.append((i, recent_rsi[i]))
        if len(price_lows) < 2 or len(rsi_lows) < 2:
            return False
        last_price_low = price_lows[-1]
        prev_price_low = price_lows[-2]
        last_rsi_low = rsi_lows[-1]
        prev_rsi_low = rsi_lows[-2]
        price_lower = last_price_low[1] < prev_price_low[1] * (1 - RSI_DIVERGENCE_THRESHOLD)
        rsi_higher = last_rsi_low[1] > prev_rsi_low[1]
        time_aligned = abs(last_price_low[0] - last_rsi_low[0]) <= 3
        return price_lower and rsi_higher and time_aligned

    # === RSI BEARISH DIVERGENCE ============================================
    def detect_rsi_bearish_divergence(self, symbol: str) -> bool:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=RSI_DIVERGENCE_LOOKBACK + 5)
            self.rate_manager.update_current()
        if len(klines) < RSI_DIVERGENCE_LOOKBACK:
            return False
        closes = np.array([safe_float(k[4]) for k in klines])
        rsi_series = talib.RSI(closes, timeperiod=RSI_PERIOD)
        if len(rsi_series) < RSI_DIVERGENCE_LOOKBACK:
            return False
        recent_closes = closes[-RSI_DIVERGENCE_LOOKBACK:]
        recent_rsi = rsi_series[-RSI_DIVERGENCE_LOOKBACK:]
        price_highs = []
        for i in range(1, len(recent_closes) - 1):
            if recent_closes[i] > recent_closes[i-1] and recent_closes[i] > recent_closes[i+1]:
                price_highs.append((i, recent_closes[i]))
        rsi_highs = []
        for i in range(1, len(recent_rsi) - 1):
            if recent_rsi[i] > recent_rsi[i-1] and recent_rsi[i] > recent_rsi[i+1]:
                rsi_highs.append((i, recent_rsi[i]))
        if len(price_highs) < 2 or len(rsi_highs) < 2:
            return False
        last_price_high = price_highs[-1]
        prev_price_high = price_highs[-2]
        last_rsi_high = rsi_highs[-1]
        prev_rsi_high = rsi_highs[-2]
        price_higher = last_price_high[1] > prev_price_high[1] * (1 + RSI_DIVERGENCE_THRESHOLD)
        rsi_lower = last_rsi_high[1] < prev_rsi_high[1]
        time_aligned = abs(last_price_high[0] - last_rsi_high[0]) <= 3
        return price_higher and rsi_lower and time_aligned

    # === VOLUME BULLISH DIVERGENCE =========================================
    def detect_volume_bullish_divergence(self, symbol: str) -> bool:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=VOLUME_DIVERGENCE_LOOKBACK + 5)
            self.rate_manager.update_current()
        if len(klines) < VOLUME_DIVERGENCE_LOOKBACK:
            return False
        closes = np.array([safe_float(k[4]) for k in klines])
        volumes = np.array([safe_float(k[5]) for k in klines])
        recent_closes = closes[-VOLUME_DIVERGENCE_LOOKBACK:]
        recent_volumes = volumes[-VOLUME_DIVERGENCE_LOOKBACK:]
        price_lows = []
        for i in range(1, len(recent_closes) - 1):
            if recent_closes[i] < recent_closes[i-1] and recent_closes[i] < recent_closes[i+1]:
                price_lows.append((i, recent_closes[i]))
        vol_lows = []
        for i in range(1, len(recent_volumes) - 1):
            if recent_volumes[i] < recent_volumes[i-1] and recent_volumes[i] < recent_volumes[i+1]:
                vol_lows.append((i, recent_volumes[i]))
        if len(price_lows) < 2 or len(vol_lows) < 2:
            return False
        last_price_low = price_lows[-1]
        prev_price_low = price_lows[-2]
        last_vol_low = vol_lows[-1]
        prev_vol_low = vol_lows[-2]
        price_lower = last_price_low[1] < prev_price_low[1] * (1 - RSI_DIVERGENCE_THRESHOLD)
        vol_lower = last_vol_low[1] <= prev_vol_low[1] * (1 + VOLUME_DIVERGENCE_THRESHOLD)
        time_aligned = abs(last_price_low[0] - last_vol_low[0]) <= 3
        return price_lower and vol_lower and time_aligned

    # === VOLUME BEARISH DIVERGENCE =========================================
    def detect_volume_bearish_divergence(self, symbol: str) -> bool:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=VOLUME_DIVERGENCE_LOOKBACK + 5)
            self.rate_manager.update_current()
        if len(klines) < VOLUME_DIVERGENCE_LOOKBACK:
            return False
        closes = np.array([safe_float(k[4]) for k in klines])
        volumes = np.array([safe_float(k[5]) for k in klines])
        recent_closes = closes[-VOLUME_DIVERGENCE_LOOKBACK:]
        recent_volumes = volumes[-VOLUME_DIVERGENCE_LOOKBACK:]
        price_highs = []
        for i in range(1, len(recent_closes) - 1):
            if recent_closes[i] > recent_closes[i-1] and recent_closes[i] > recent_closes[i+1]:
                price_highs.append((i, recent_closes[i]))
        vol_highs = []
        for i in range(1, len(recent_volumes) - 1):
            if recent_volumes[i] > recent_volumes[i-1] and recent_volumes[i] > recent_volumes[i+1]:
                vol_highs.append((i, recent_volumes[i]))
        if len(price_highs) < 2 or len(vol_highs) < 2:
            return False
        last_price_high = price_highs[-1]
        prev_price_high = price_highs[-2]
        last_vol_high = vol_highs[-1]
        prev_vol_high = vol_highs[-2]
        price_higher = last_price_high[1] > prev_price_high[1] * (1 + RSI_DIVERGENCE_THRESHOLD)
        vol_lower = last_vol_high[1] < prev_vol_high[1] * (1 - VOLUME_DIVERGENCE_THRESHOLD)
        time_aligned = abs(last_price_high[0] - last_vol_high[0]) <= 3
        return price_higher and vol_lower and time_aligned

    # === MACD HISTOGRAM BEARISH DIVERGENCE ================================
    def detect_bearish_histogram_divergence(self, symbol: str) -> bool:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=BEARISH_DIVERGENCE_LOOKBACK + 5)
            self.rate_manager.update_current()
        if len(klines) < BEARISH_DIVERGENCE_LOOKBACK:
            return False
        closes = np.array([safe_float(k[4]) for k in klines])
        macd, signal, hist = talib.MACD(closes, fastperiod=MACD_FAST, slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)
        if len(hist) < BEARISH_DIVERGENCE_LOOKBACK:
            return False
        recent_closes = closes[-BEARISH_DIVERGENCE_LOOKBACK:]
        recent_hist = hist[-BEARISH_DIVERGENCE_LOOKBACK:]
        price_peaks = []
        for i in range(1, len(recent_closes) - 1):
            if recent_closes[i] > recent_closes[i-1] and recent_closes[i] > recent_closes[i+1]:
                price_peaks.append((i, recent_closes[i]))
        hist_peaks = []
        for i in range(1, len(recent_hist) - 1):
            if recent_hist[i] > recent_hist[i-1] and recent_hist[i] > recent_hist[i+1]:
                hist_peaks.append((i, recent_hist[i]))
        if len(price_peaks) < 2 or len(hist_peaks) < 2:
            return False
        last_price_peak = price_peaks[-1]
        prev_price_peak = price_peaks[-2]
        last_hist_peak = hist_peaks[-1]
        prev_hist_peak = hist_peaks[-2]
        price_higher = last_price_peak[1] > prev_price_peak[1] * (1 + BEARISH_DIVERGENCE_THRESHOLD)
        hist_lower = last_hist_peak[1] < prev_hist_peak[1]
        time_aligned = abs(last_price_peak[0] - last_hist_peak[0]) <= 3
        return price_higher and hist_lower and time_aligned

    # === PRIORITY RANKING ===================================================
    def get_top_priority_coins(self, limit: int = 10) -> List[Tuple[str, float]]:
        candidates = []
        max_bid_vol = max((valid_symbols_dict[s].get('cum_bid_vol', 0) for s in valid_symbols_dict), default=1)
        max_vol_24h = max((valid_symbols_dict[s].get('volume_24h', 0) for s in valid_symbols_dict), default=1)
        for sym in valid_symbols_dict.keys():
            ob = self.get_order_book_analysis(sym)
            bid_vol = ob['cum_bid_vol']
            vol_24h = valid_symbols_dict[sym].get('volume_24h', 0)
            momentum_ok, rsi, lookback, hist = self.get_macd_bullish_with_histogram(sym)
            rsi_div_bull = self.detect_rsi_bullish_divergence(sym)
            vol_div_bull = self.detect_volume_bullish_divergence(sym)
            if not (momentum_ok or rsi_div_bull or vol_div_bull):
                continue
            norm_bid = bid_vol / max_bid_vol if max_bid_vol > 0 else 0
            norm_vol = vol_24h / max_vol_24h if max_vol_24h > 0 else 0
            score = (BUY_VOLUME_WEIGHT * norm_bid) + (VOLUME_24H_WEIGHT * norm_vol)
            candidates.append((sym, score))
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[:limit]

    # === GRID LOGIC =========================================================
    def calculate_grid_count(self) -> int:
        free_usdt = self.get_balance('USDT')
        if free_usdt < Decimal('40.0'):
            return 0
        return min(MAX_GRIDS_HIGH_BALANCE, int(free_usdt // GRID_SIZE_USDT))

    def place_scaled_grids(self):
        grid_count = self.calculate_grid_count()
        if grid_count == 0:
            return
        top_coins = self.get_top_priority_coins(limit=MAX_SCALED_COINS)
        if not top_coins:
            return
        usdt_per_grid = GRID_SIZE_USDT
        total_cost_per_grid = usdt_per_grid * Decimal(str(grid_count))
        placed = 0
        for sym, score in top_coins:
            if sym in active_grid_symbols:
                continue
            ob = self.get_order_book_analysis(sym)
            mid = ob['best_ask']
            if not mid or mid <= ZERO:
                continue
            total_grid_cost = total_cost_per_grid
            if not has_enough_usdt_for(self, total_grid_cost):
                break
            self.place_infinity_grid(sym)
            active_grid_symbols[sym] = {'grid_count': grid_count, 'score': float(score)}
            placed += 1
            if placed >= MAX_SCALED_COINS:
                break

    def place_infinity_grid(self, symbol: str):
        ob = self.get_order_book_analysis(symbol)
        mid = ob['best_ask']
        if not mid or mid <= ZERO:
            return
        grid_count = active_grid_symbols[symbol]['grid_count']
        start_price = mid * (1 - GRID_INTERVAL_PCT * (grid_count - 1) / 2)
        qty = GRID_SIZE_USDT / start_price
        for i in range(grid_count):
            price = start_price * (1 - GRID_INTERVAL_PCT * i)
            if price <= ZERO:
                continue
            if not buy_notional_ok(symbol, price, qty):
                continue
            self.place_limit_buy_with_tracking(symbol, float(price), float(qty))

    def place_limit_buy_with_tracking(self, symbol: str, price: float, qty: float):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.create_order(
                    symbol=symbol,
                    side=SIDE_BUY,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    quantity=qty,
                    price=price
                )
                self.rate_manager.update_current()
            with DBManager() as db:
                pending = PendingOrder(
                    binance_order_id=order['orderId'],
                    symbol=symbol,
                    side='BUY',
                    price=price,
                    quantity=qty
                )
                db.add(pending)
                db.commit()
        except Exception as e:
            logger.error(f"Buy order failed: {e}")

    # === TRAILING BUY =======================================================
    def process_trailing_buy(self, symbol):
        with self.state_lock:
            if symbol not in trailing_buy_active:
                return
            state = trailing_buy_active[symbol]
        try:
            ob = self.get_order_book_analysis(symbol)
            ask = ob['best_ask']
            if ask <= ZERO:
                return
            if self.detect_volume_bullish_divergence(symbol):
                self.place_buy_at_price(symbol, None, force_market=True)
                with self.state_lock:
                    trailing_buy_active.pop(symbol, None)
                return
            if self.detect_rsi_bullish_divergence(symbol):
                self.place_buy_at_price(symbol, None, force_market=True)
                with self.state_lock:
                    trailing_buy_active.pop(symbol, None)
                return
            now = time.time()
            with self.state_lock:
                if symbol not in last_price_cache:
                    last_price_cache[symbol] = (float(ask), now)
                else:
                    lp, lt = last_price_cache[symbol]
                    if now - lt < RAPID_DROP_WINDOW:
                        drop = (lp - float(ask)) / lp
                        if drop >= RAPID_DROP_THRESHOLD:
                            self.place_buy_at_price(symbol, None, force_market=True)
                            if symbol in trailing_buy_active:
                                del trailing_buy_active[symbol]
                            return
                    last_price_cache[symbol] = (float(ask), now)
            if ask < state['lowest_price']:
                state['lowest_price'] = ask
            trailing_buy_active[symbol] = state
            rsi, trend, low24 = self.get_rsi_and_trend(symbol)
            if rsi is None or rsi > RSI_OVERSOLD or trend != 'bullish':
                return
            dynamic_entry = self.get_dynamic_entry(symbol, state['lowest_price'], ob)
            if ask >= dynamic_entry:
                self.place_buy_at_price(symbol, ask)
                with self.state_lock:
                    if symbol in trailing_buy_active:
                        del trailing_buy_active[symbol]
                return
        except Exception as e:
            logger.debug(f"Trailing buy error: {e}")

    def get_dynamic_entry(self, symbol: str, lowest_price: Decimal, ob: dict) -> Decimal:
        atr = self.get_ATR(symbol)
        return lowest_price + Decimal(str(atr)) * ATR_TRAIL_MULTIPLIER_BUY

    def place_buy_at_price(self, symbol: str, price: Optional[float], force_market: bool = False):
        ob = self.get_order_book_analysis(symbol)
        ask = ob['best_ask'] if price is None else Decimal(str(price))
        qty = GRID_SIZE_USDT / ask
        if not buy_notional_ok(symbol, ask, qty):
            return
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.create_order(
                    symbol=symbol,
                    side=SIDE_BUY,
                    type=ORDER_TYPE_MARKET if force_market else ORDER_TYPE_LIMIT,
                    quantity=float(qty),
                    price=price
                )
                self.rate_manager.update_current()
            send_whatsapp_alert(f"BUY {symbol} @ {ask:.6f}")
        except Exception as e:
            logger.error(f"Buy failed: {e}")

    # === TRAILING SELL ======================================================
    def process_trailing_sell(self, symbol):
        with self.state_lock:
            if symbol not in trailing_sell_active:
                return
            state = trailing_sell_active[symbol]
        try:
            ob = self.get_order_book_analysis(symbol)
            bid = ob['best_bid']
            if bid <= ZERO:
                return
            if bid > state['peak_price']:
                state['peak_price'] = bid
                trailing_sell_active[symbol] = state
            if self.detect_volume_bearish_divergence(symbol):
                self.place_sell_at_price(symbol, None, force_market=True)
                with self.state_lock:
                    trailing_sell_active.pop(symbol, None)
                return
            if self.detect_rsi_bearish_divergence(symbol):
                self.place_sell_at_price(symbol, None, force_market=True)
                with self.state_lock:
                    trailing_sell_active.pop(symbol, None)
                return
            if self.detect_bearish_histogram_divergence(symbol):
                self.place_sell_at_price(symbol, None, force_market=True)
                with self.state_lock:
                    trailing_sell_active.pop(symbol, None)
                return
            maker, taker = self.get_trade_fees(symbol)
            net = (bid - state['entry_price']) / state['entry_price'] - Decimal(str(maker)) - Decimal(str(taker))
            if net >= PROFIT_TARGET_NET:
                self.place_sell_at_price(symbol, bid)
                with self.state_lock:
                    if symbol in trailing_sell_active:
                        del trailing_sell_active[symbol]
                return
        except Exception as e:
            logger.debug(f"Trailing sell error: {e}")

    def place_sell_at_price(self, symbol: str, price: Optional[float], force_market: bool = False):
        pos = positions.get(symbol)
        if not pos:
            return
        qty = pos['qty']
        bid = price or self.get_order_book_analysis(symbol)['best_bid']
        if not sell_notional_ok(symbol, Decimal(str(bid)), qty):
            return
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.create_order(
                    symbol=symbol,
                    side=SIDE_SELL,
                    type=ORDER_TYPE_MARKET if force_market else ORDER_TYPE_LIMIT,
                    quantity=float(qty),
                    price=price
                )
                self.rate_manager.update_current()
            send_whatsapp_alert(f"SELL {symbol} @ {bid:.6f}")
            with self.state_lock:
                positions.pop(symbol, None)
        except Exception as e:
            logger.error(f"Sell failed: {e}")

    # === PROFESSIONAL DASHBOARD =====================================================

# Initialize colorama for cross-platform colored output
init(autoreset=True)

# ANSI Color & Style Constants
class Color:
    GREEN   = Fore.GREEN + Style.BRIGHT
    RED     = Fore.RED + Style.BRIGHT
    YELLOW  = Fore.YELLOW + Style.BRIGHT
    CYAN    = Fore.CYAN + Style.BRIGHT
    MAGENTA = Fore.MAGENTA + Style.BRIGHT
    WHITE   = Fore.WHITE + Style.BRIGHT
    GRAY    = Fore.LIGHTBLACK_EX
    RESET   = Style.RESET_ALL
    BOLD    = Style.BRIGHT
    DIM     = Style.DIM

DIVIDER = "=" * 120


def print_professional_dashboard(bot) -> None:
    """Prints a live, real-time professional trading dashboard."""
    try:
        os.system('cls' if os.name == 'nt' else 'clear')

        # === HEADER ===
        print(Color.CYAN + DIVIDER)
        print(f"{Color.BOLD}{Color.WHITE}{'INFINITY GRID + TRAILING HYBRID BOT':^120}{Color.RESET}")
        print(Color.CYAN + DIVIDER)

        # === CORE METRICS ===
        now_str = now_cst()
        usdt_free = bot.get_balance('USDT')
        total_port, _ = bot.calculate_total_portfolio_value()
        tbuy_cnt = len(trailing_buy_active)
        tsel_cnt = len(trailing_sell_active)
        grid_count = sum(len(s['buy_orders']) for s in active_grid_symbols.values())

        print(f"{Color.GRAY}Time (CST):{Color.RESET} {Color.YELLOW}{now_str}{Color.RESET}")
        print(f"{Color.GRAY}Free USDT:{Color.RESET}  ${Color.GREEN}{float(usdt_free):,.6f}{Color.RESET}")
        print(f"{Color.GRAY}Portfolio:{Color.RESET}  ${Color.CYAN}{total_port:,.6f}{Color.RESET}")
        print(f"{Color.GRAY}Trailing: {Color.MAGENTA}{tbuy_cnt}{Color.RESET} buys, {Color.MAGENTA}{tsel_cnt}{Color.RESET} sells")
        print(f"{Color.GRAY}Grid Orders:{Color.RESET} {Color.YELLOW}{grid_count}{Color.RESET} × $5")

        # === STRATEGY MODE ===
        balance = float(usdt_free)
        mode, grid_levels = _determine_strategy_mode(balance, grid_count, tbuy_cnt + tsel_cnt)
        mode_color = Color.GREEN if "GRID" in mode else Color.YELLOW if tbuy_cnt + tsel_cnt > 0 else Color.RED
        print(f"\n{Color.BOLD}Strategy Mode:{Color.RESET} {mode_color}{mode}{Color.RESET}")

        # === DEPTH IMBALANCE BARS ===
        print(f"\n{Color.BOLD}DEPTH IMBALANCE (Top 10 by 24h Volume){Color.RESET}")
        _print_depth_imbalance_bars(bot)

        # === ORDER BOOK LADDER ===
        print(Color.CYAN + DIVIDER)
        print(f"{Color.BOLD}ORDER BOOK LADDER (Top 3 Active){Color.RESET}")
        _print_order_book_ladders(bot)

        # === POSITIONS & P&L ===
        print(Color.CYAN + DIVIDER)
        print(f"{Color.BOLD}{'CURRENT POSITIONS & P&L':^120}{Color.RESET}")
        total_pnl = _print_positions_table(bot)

        # === MARKET OVERVIEW & SIGNALS ===
        print(Color.CYAN + DIVIDER)
        print(f"{Color.BOLD}{'MARKET OVERVIEW':^120}{Color.RESET}")
        _print_market_overview_and_signals(bot)

        print(Color.CYAN + DIVIDER)
        print(f"{Color.BOLD}TOTAL UNREALIZED P&L: {Color.GREEN if total_pnl > 0 else Color.RED}${float(total_pnl):,.2f}{Color.RESET}")
        print(Color.CYAN + DIVIDER)

    except Exception as e:
        print(f"{Color.RED}DASHBOARD ERROR: {e}{Color.RESET}")


def _determine_strategy_mode(balance: float, grid_count: int, trailing_count: int) -> Tuple[str, int]:
    """Determine current bot strategy mode based on balance and activity."""
    if balance >= 80:
        levels = min(16, int((balance - 20) // 5))
        return f"INFINITY GRID ({levels} levels)", levels
    elif balance >= 60:
        levels = min(10, max(6, int((balance - 20) // 5)))
        return f"INFINITY GRID ({levels} levels)", levels
    elif balance >= 40:
        return "INFINITY GRID (4 levels)", 4
    else:
        return "TRAILING MOMENTUM", 0


def _print_depth_imbalance_bars(bot) -> None:
    """Print top 10 symbols with depth imbalance bars."""
    sorted_symbols = sorted(
        valid_symbols_dict.items(),
        key=lambda x: x[1]['volume'],
        reverse=True
    )[:10]

    BAR_WIDTH = 50
    for sym, info in sorted_symbols:
        ob = bot.get_order_book_analysis(sym)
        pct_bid = ob['pct_bid']
        pct_ask = 100 - pct_bid

        bid_blocks = int(pct_bid / 2)
        ask_blocks = int(pct_ask / 2)
        neutral = BAR_WIDTH - bid_blocks - ask_blocks

        bar = (Color.GREEN + "█" * bid_blocks +
               Color.GRAY + "░" * max(0, neutral) +
               Color.RED + "█" * ask_blocks + Color.RESET)

        bias = ("strong bid wall" if pct_bid > 60 else
                "strong ask pressure" if pct_bid < 40 else
                "balanced")

        coin = sym.replace("USDT", "")
        print(f"{Color.CYAN}{coin:<8}{Color.RESET} |{bar}| {Color.GREEN}{pct_bid:>3.0f}%{Color.RESET} / {Color.RED}{pct_ask:>3.0f}%{Color.RESET}")
        print(f"{'':<10} {Color.DIM}{bias}{Color.RESET}")
        print()


def _print_order_book_ladders(bot) -> None:
    """Print order book ladder for top 3 active symbols."""
    ladder_symbols = list(active_grid_symbols.keys())[:3]
    if not ladder_symbols:
        ladder_symbols = [s for s, _ in sorted(valid_symbols_dict.items(), key=lambda x: x[1]['volume'], reverse=True)[:3]]

    for sym in ladder_symbols:
        ob = bot.get_order_book_analysis(sym)
        bids = ob.get('raw_bids', [])[:5]
        asks = ob.get('raw_asks', [])[:5]
        mid = ob['mid_price']

        coin = sym.replace("USDT", "")
        print(f"  {Color.MAGENTA}{coin:>8}{Color.RESET} | Mid: ${Color.YELLOW}{mid:,.6f}{Color.RESET}")
        print(f"    {Color.GREEN}BIDS{' ' * 22}|{' ' * 27}ASKS{Color.RESET}")
        print(f"    {'-' * 27} | {'-' * 27}")
        for i in range(5):
            bid_price = float(bids[i][0]) if i < len(bids) else 0.0
            bid_qty = float(bids[i][1]) if i < len(bids) else 0.0
            ask_price = float(asks[i][0]) if i < len(asks) else 0.0
            ask_qty = float(asks[i][1]) if i < len(asks) else 0.0
            print(f"    {Color.GREEN}{bid_price:>10.6f} x {bid_qty:>7.2f}{Color.RESET} | "
                  f"{Color.RED}{ask_price:<10.6f} x {ask_qty:>7.2f}{Color.RESET}")
        print()


def _print_positions_table(bot) -> Decimal:
    """Print positions table and return total P&L."""
    print(f"{'SYM':<8} {'QTY':>10} {'ENTRY':>12} {'CUR':>12} {'RSI':>6} {'P&L%':>8} {'PROFIT':>10} {'STATUS'}")
    print("-" * 120)

    with DBManager() as sess:
        positions_list = sess.query(Position).all()[:15]

    total_pnl = Decimal('0')
    for pos in positions_list:
        sym = pos.symbol
        qty = float(pos.quantity)
        entry = float(pos.avg_entry_price)
        ob = bot.get_order_book_analysis(sym)
        cur = float(ob['best_bid'] or ob['best_ask'] or 0)
        rsi, _, _ = bot.get_rsi_and_trend(sym)
        rsi_str = f"{rsi:5.1f}" if rsi else "N/A"

        maker, taker = bot.get_trade_fees(sym)
        gross = (cur - entry) * qty
        fee = (maker + taker) * cur * qty
        net = Decimal(str(gross - fee))
        total_pnl += net

        pnl_pct = ((cur - entry) / entry - (maker + taker)) * 100 if entry > 0 else 0.0

        status = "Monitoring"
        if sym in trailing_sell_active:
            state = trailing_sell_active[sym]
            peak = float(state['peak_price'])
            ob_data = bot.get_order_book_analysis(sym)
            stop = bot.get_dynamic_stop(sym, Decimal(str(peak)), ob_data)
            status = f"Trail Sell @ {float(stop):.6f}"
        elif sym in trailing_buy_active:
            state = trailing_buy_active[sym]
            low = float(state['lowest_price'])
            ob_data = bot.get_order_book_analysis(sym)
            entry_trail = bot.get_dynamic_entry(sym, Decimal(str(low)), ob_data)
            status = f"Trail Buy @ {float(entry_trail):.6f}"

        pnl_color = Color.GREEN if net > 0 else Color.RED
        pct_color = Color.GREEN if pnl_pct > 0 else Color.RED

        print(f"{Color.CYAN}{sym:<8}{Color.RESET} "
              f"{qty:>10.6f} {entry:>12.6f} {cur:>12.6f} "
              f"{Color.YELLOW}{rsi_str}{Color.RESET} "
              f"{pct_color}{pnl_pct:>7.2f}%{Color.RESET} "
              f"{pnl_color}{net:>9.2f}{Color.RESET} "
              f"{Color.DIM}{status}{Color.RESET}")

    # Fill empty rows
    for _ in range(len(positions_list), 15):
        print(" " * 120)

    return total_pnl


def _print_market_overview_and_signals(bot) -> None:
    """Print market stats and smart buy signals."""
    valid_cnt = len(valid_symbols_dict)
    avg_vol = sum(s['volume'] for s in valid_symbols_dict.values()) / valid_cnt if valid_cnt else 0

    print(f"Valid Symbols: {Color.CYAN}{valid_cnt}{Color.RESET}")
    print(f"Avg 24h Volume: ${Color.YELLOW}{avg_vol:,.0f}{Color.RESET}")
    print(f"Price Range: ${Color.GRAY}{MIN_PRICE}{Color.RESET} → ${Color.GRAY}{MAX_PRICE}{Color.RESET}")

    # Smart Buy Signals
    watch_items = []
    for sym in valid_symbols_dict:
        ob = bot.get_order_book_analysis(sym)
        rsi, trend, _ = bot.get_rsi_and_trend(sym)
        bid = ob.get('best_bid')
        if not bid or not rsi: continue

        custom_low, _, _ = bot.get_24h_price_stats(sym)
        if not custom_low: continue

        strong_buy = (ob['depth_skew'] == 'strong_ask' and
                      ob['imbalance_ratio'] <= 0.5 and
                      ob['weighted_pressure'] < -0.002)

        if (rsi <= RSI_OVERSOLD and trend == 'bullish' and
            bid <= Decimal(str(custom_low)) * Decimal('1.01') and strong_buy):
            coin = sym.replace('USDT', '')
            watch_items.append(f"{coin}({rsi:.0f})")

    signal_str = " | ".join(watch_items[:18]) if watch_items else "No strong buy signals"
    if len(signal_str) > 100:
        signal_str = signal_str[:97] + "..."

    print(f"\n{Color.BOLD}Smart Buy Signals:{Color.RESET} {Color.GREEN}{signal_str}{Color.RESET}")

    # === MAIN LOOP ==========================================================
def main():
    bot = BinanceTradingBot()
    last_grid_check = 0
    last_dashboard = 0
    while True:
        now = time.time()
        if now - last_grid_check > 60:
            bot.place_scaled_grids()
            last_grid_check = now
        if now - last_dashboard > 30:
            bot.print_professional_dashboard()
            last_dashboard = now
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
