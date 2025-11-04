#!/usr/bin/env python3
"""
    Dynamic Trailing Bot – MULTI-THREADED (Rate-Limit & Depth Aware)
- 1 extra thread for buy scanning
- 1 extra thread for sell scanning
- 1 thread for all trailing buys
- 1 thread for all trailing sells
- Full 50-level order book depth analysis (VWAP, imbalance, skew, pressure)
- Thread-safe, rate-limit aware, professional dashboard
- Integrated candlestick pattern detection for enhanced trend analysis
- Price history tracking for custom 24h high/low/avg calculations
- ATR + Order-Book Hybrid Trailing Stops (Sell) & Entries (Buy)
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

MAX_PRICE = 1000.00
MIN_PRICE = 0.01
MIN_24H_VOLUME_USDT = 100000
LOG_FILE = "crypto_trading_bot.log"
RSI_PERIOD = 14
BB_PERIOD = 20
BB_DEV = 2
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
PROFIT_TARGET_NET = Decimal('0.008')  # 0.8%
RISK_PER_TRADE = 0.10
MIN_BALANCE = 2.0

# Strategy
ORDERBOOK_SELL_PRESSURE_THRESHOLD = 0.60
ORDERBOOK_BUY_PRESSURE_SPIKE = 0.65
ORDERBOOK_BUY_PRESSURE_DROP = 0.55
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
ORDER_BOOK_LIMIT = 50
ORDER_BOOK_LEVELS = 50
DEPTH_IMBALANCE_THRESHOLD = 2.0
POLL_INTERVAL = 1.0
STALL_THRESHOLD_SECONDS = 15 * 60  # 15 minutes
RAPID_DROP_THRESHOLD = 0.01  # 1.0%
RAPID_DROP_WINDOW = 5.0      # seconds
ATR_TRAIL_MULTIPLIER_BUY = 1.0  # 1.0 × ATR min trail

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
price_history: Dict[str, deque] = {}  # symbol: deque of (timestamp, close_price) for 24h tracking

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

# Create DB if not exists
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
        if exc_type is not None:
            self.session.rollback()
        else:
            try: self.session.commit()
            except SQLAlchemyError: self.session.rollback()
        self.session.close()

# === RETRY DECORATOR (handles 429/418) ======================================
def retry_custom(func):
    def wrapper(*args, **kwargs):
        max_retries = 5
        base_delay = 2.0
        for i in range(max_retries):
            try:
                return func(*args, **kwargs)
            except BinanceAPIException as e:
                if e.status_code in (429, 418):
                    retry_after = int(e.response.headers.get('Retry-After', 60))
                    logger.warning(f"Rate limit {e.status_code}: sleeping {retry_after}s")
                    time.sleep(retry_after)
                else:
                    if i == max_retries - 1: raise
                    delay = base_delay * (2 ** i)
                    logger.warning(f"Retry {i+1}/{max_retries} for {func.__name__}: {e}")
                    time.sleep(delay)
            except Exception as e:
                if i == max_retries - 1: raise
                delay = base_delay * (2 ** i)
                logger.warning(f"Retry {i+1}/{max_retries} for {func.__name__}: {e}")
                time.sleep(delay)
    return wrapper

# === RATE MANAGER ===========================================================
class RateManager:
    def __init__(self, client):
        self.client = client
        self.limits = self._fetch_limits()
        self.current = {'request_weight': 0, 'orders_10s': 0, 'orders_1d': 0}
        self.last_update = 0

    @retry_custom
    def _fetch_limits(self):
        info = self.client.get_exchange_info()
        limits = {}
        for rl in info.get('rateLimits', []):
            key = rl['rateLimitType']
            interval = f"{rl['interval']}_{rl['intervalNum']}"
            limits.setdefault(key, {})[interval] = rl['limit']
        return limits

    def update_current(self):
        if getattr(self.client, 'response', None) is None: return
        hdr = self.client.response.headers
        updated = False
        if 'x-mbx-used-weight-1m' in hdr:
            self.current['request_weight'] = int(hdr['x-mbx-used-weight-1m'])
            updated = True
        if 'x-mbx-order-count-10s' in hdr:
            self.current['orders_10s'] = int(hdr['x-mbx-order-count-10s'])
            updated = True
        if 'x-mbx-order-count-1d' in hdr:
            self.current['orders_1d'] = int(hdr['x-mbx-order-count-1d'])
            updated = True
        if updated:
            self.last_update = time.time()

    def is_close(self, typ, margin=0.95):
        if typ == 'REQUEST_WEIGHT':
            lim = self.limits.get('REQUEST_WEIGHT', {}).get('MINUTE_1', 6000)
            return self.current['request_weight'] >= lim * margin
        if typ == 'ORDERS':
            lim10 = self.limits.get('ORDERS', {}).get('SECOND_10', 50)
            lim1d = self.limits.get('ORDERS', {}).get('DAY_1', 160000)
            return (self.current['orders_10s'] >= lim10 * margin or
                    self.current['orders_1d'] >= lim1d * margin)
        return False

    def wait_if_needed(self, typ='REQUEST_WEIGHT'):
        self.update_current()
        if self.is_close(typ):
            wait = self._calc_wait(typ)
            logger.debug(f"Close to {typ} limit – sleeping {wait:.1f}s")
            time.sleep(wait)

    def _calc_wait(self, typ):
        now = datetime.now()
        if typ == 'REQUEST_WEIGHT':
            return max(60 - now.second + 0.1, 1.0)
        return max(10 - (now.second % 10) + 0.1, 1.0)

# === BOT CLASS ==============================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.rate_manager = RateManager(self.client)
        self.api_lock = threading.Lock()
        self.state_lock = threading.Lock()
        with DBManager() as sess:
            self.import_owned_assets_to_db(sess)
            sess.commit()
        self.load_state_from_db()

    def load_state_from_db(self):
        with DBManager() as sess:
            for p in sess.query(Position).all():
                with self.state_lock:
                    positions[p.symbol] = {
                        'qty': float(p.quantity),
                        'entry_price': float(p.avg_entry_price),
                        'buy_fee': float(p.buy_fee_rate)
                    }

    @retry_custom
    def fetch_and_validate_usdt_pairs(self) -> Dict[str, dict]:
        global valid_symbols_dict
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            info = self.client.get_exchange_info()
            self.rate_manager.update_current()
        raw = [
            s['symbol'] for s in info['symbols']
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING' and s['symbol'].endswith('USDT')
        ]
        raw = [s for s in raw if s not in {'USDCUSDT', 'USDTUSDT'}]

        valid = {}
        for sym in raw:
            try:
                with self.api_lock:
                    self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                    ticker = self.client.get_ticker(symbol=sym)
                    self.rate_manager.update_current()
                price = safe_float(ticker.get('lastPrice'))
                vol   = safe_float(ticker.get('quoteVolume'))
                low   = safe_float(ticker.get('lowPrice'))
                if MIN_PRICE <= price <= MAX_PRICE and vol >= MIN_24H_VOLUME_USDT and low > 0:
                    valid[sym] = {'price': price, 'volume': vol, 'low_24h': low}
            except Exception:
                continue

        with self.state_lock:
            valid_symbols_dict = valid
        logger.info(f"Valid symbols fetched: {len(valid)}")
        return valid

    @retry_custom
    def get_order_book_analysis(self, symbol: str) -> dict:
        now = time.time()
        with self.state_lock:
            cached = order_book_cache.get(symbol)
            if cached and now - cached.get('ts', 0) < 1.0:
                return cached

        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            depth = self.client.get_order_book(symbol=symbol, limit=ORDER_BOOK_LEVELS)
            self.rate_manager.update_current()

        bids = depth.get('bids', [])[:ORDER_BOOK_LEVELS]
        asks = depth.get('asks', [])[:ORDER_BOOK_LEVELS]

        top5_bids = bids[:5]
        top5_asks = asks[:5]
        top5_bid_vol = sum(Decimal(b[1]) for b in top5_bids)
        top5_ask_vol = sum(Decimal(a[1]) for a in top5_asks)
        top5_total = top5_bid_vol + top5_ask_vol or Decimal('1')

        cum_bid_vol = Decimal('0')
        cum_ask_vol = Decimal('0')
        bid_weighted = Decimal('0')
        ask_weighted = Decimal('0')

        for price_str, qty_str in bids:
            price = Decimal(price_str)
            qty   = Decimal(qty_str)
            cum_bid_vol   += qty
            bid_weighted  += price * qty

        for price_str, qty_str in asks:
            price = Decimal(price_str)
            qty   = Decimal(qty_str)
            cum_ask_vol   += qty
            ask_weighted  += price * qty

        bid_vwap = (bid_weighted / cum_bid_vol) if cum_bid_vol else ZERO
        ask_vwap = (ask_weighted / cum_ask_vol) if cum_ask_vol else ZERO
        mid_price = (bid_vwap + ask_vwap) / Decimal('2') if (bid_vwap and ask_vwap) else ZERO

        imbalance_ratio = float(cum_bid_vol / cum_ask_vol) if cum_ask_vol else 999.0
        depth_skew = (
            'strong_bid' if imbalance_ratio >= DEPTH_IMBALANCE_THRESHOLD else
            'strong_ask' if (1.0 / imbalance_ratio) >= DEPTH_IMBALANCE_THRESHOLD else
            'balanced'
        )

        weighted_pressure = float((bid_vwap - ask_vwap) / mid_price) if mid_price else 0.0

        result = {
            'pct_bid':      float(top5_bid_vol / top5_total * 100),
            'pct_ask':      float(top5_ask_vol / top5_total * 100),
            'best_bid':     Decimal(bids[0][0]) if bids else ZERO,
            'best_ask':     Decimal(asks[0][0]) if asks else ZERO,
            'cum_bid_vol':  float(cum_bid_vol),
            'cum_ask_vol':  float(cum_ask_vol),
            'bid_vwap':     float(bid_vwap),
            'ask_vwap':     float(ask_vwap),
            'imbalance_ratio': imbalance_ratio,
            'depth_skew':   depth_skew,
            'weighted_pressure': weighted_pressure,
            'mid_price':    float(mid_price),
            'ts':           now,
            'raw_bids':     [(Decimal(p), Decimal(q)) for p, q in bids],
            'raw_asks':     [(Decimal(p), Decimal(q)) for p, q in asks]
        }

        with self.state_lock:
            order_book_cache[symbol] = result
        return result

    @retry_custom
    def get_tick_size(self, symbol):
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            info = self.client.get_symbol_info(symbol)
            self.rate_manager.update_current()
        for f in info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                return Decimal(f['tickSize'])
        return Decimal('0.00000001')

    @retry_custom
    def get_rsi_and_trend(self, symbol) -> Tuple[Optional[float], str, Optional[float]]:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=100)
            self.rate_manager.update_current()
        opens = np.array([safe_float(k[1]) for k in klines[-100:]])
        highs = np.array([safe_float(k[2]) for k in klines[-100:]])
        lows = np.array([safe_float(k[3]) for k in klines[-100:]])
        closes = np.array([safe_float(k[4]) for k in klines[-100:]])

        # Update price history
        now = time.time()
        with self.state_lock:
            if symbol not in price_history:
                price_history[symbol] = deque(maxlen=1440)
            price_history[symbol].append((now, closes[-1]))
            while price_history[symbol] and now - price_history[symbol][0][0] > 86400:
                price_history[symbol].popleft()

        if len(closes) < RSI_PERIOD:
            return None, "unknown", None
        rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)[-1]
        if not np.isfinite(rsi):
            return None, "unknown", None
        upper, middle, lower = talib.BBANDS(closes, timeperiod=BB_PERIOD, nbdevup=BB_DEV, nbdevdn=BB_DEV)
        macd, sig, _ = talib.MACD(closes, fastperiod=MACD_FAST, slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)

        # Candlestick pattern detection
        pattern_score = 0
        pattern_score += talib.CDLHAMMER(opens, highs, lows, closes)[-1]
        pattern_score += talib.CDLENGULFING(opens, highs, lows, closes)[-1] if talib.CDLENGULFING(opens, highs, lows, closes)[-1] > 0 else 0
        pattern_score += talib.CDLMORNINGSTAR(opens, highs, lows, closes)[-1]
        pattern_score -= abs(talib.CDLSHOOTINGSTAR(opens, highs, lows, closes)[-1])
        pattern_score -= abs(talib.CDLENGULFING(opens, highs, lows, closes)[-1]) if talib.CDLENGULFING(opens, highs, lows, closes)[-1] < 0 else 0
        pattern_score -= abs(talib.CDLEVENINGSTAR(opens, highs, lows, closes)[-1])
        if talib.CDLDOJI(opens, highs, lows, closes)[-1] != 0:
            pattern_score = 0

        trend_base = ("bullish" if closes[-1] > middle[-1] and macd[-1] > sig[-1]
                      else "bearish" if closes[-1] < middle[-1] and macd[-1] < sig[-1] else "sideways")
        trend = "bullish" if pattern_score > 0 else "bearish" if pattern_score < 0 else trend_base

        with self.state_lock:
            low_24h = valid_symbols_dict.get(symbol, {}).get('low_24h')
        return float(rsi), trend, float(low_24h) if low_24h else None

    def get_24h_price_stats(self, symbol):
        with self.state_lock:
            hist = price_history.get(symbol, deque())
            if not hist:
                return None, None, None
            prices = [p[1] for p in hist]
            return min(prices), max(prices), sum(prices) / len(prices)

    @retry_custom
    def get_balance(self, asset='USDT') -> float:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            account = self.client.get_account()
            self.rate_manager.update_current()
        for bal in account['balances']:
            if bal['asset'] == asset:
                return safe_float(bal['free'])
        return 0.0

    @retry_custom
    def get_trade_fees(self, symbol):
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            fee = self.client.get_trade_fee(symbol=symbol)
            self.rate_manager.update_current()
        return safe_float(fee[0]['makerCommission']), safe_float(fee[0]['takerCommission'])

    @retry_custom
    def get_price_usdt(self, asset: str) -> Decimal:
        if asset == 'USDT':
            return Decimal('1')
        sym = asset + 'USDT'
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                ticker = self.client.get_symbol_ticker(symbol=sym)
                self.rate_manager.update_current()
            return Decimal(ticker['price'])
        except:
            return ZERO

    def calculate_total_portfolio_value(self):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                account = self.client.get_account()
                self.rate_manager.update_current()
            total = Decimal('0')
            values = {}
            for b in account['balances']:
                qty = Decimal(str(safe_float(b['free'])))
                if qty <= 0: continue
                if b['asset'] == 'USDT':
                    total += qty
                    values['USDT'] = float(qty)
                else:
                    price = self.get_price_usdt(b['asset'])
                    if price > 0:
                        val = qty * price
                        total += val
                        values[b['asset']] = float(val)
            return float(total), values
        except Exception as e:
            logger.warning(f"Portfolio value error: {e}")
            return 0.0, {}

    def import_owned_assets_to_db(self, sess):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                acct = self.client.get_account()
                self.rate_manager.update_current()
            for bal in acct['balances']:
                asset = bal['asset']
                qty = safe_float(bal['free'])
                if qty <= 0 or asset in {'USDT', 'USDC'}: continue
                sym = f"{asset}USDT"
                if sess.query(Position).filter_by(symbol=sym).first(): continue
                price = self.get_price_usdt(asset)
                if price <= ZERO: continue
                maker, _ = self.get_trade_fees(sym)
                pos = Position(symbol=sym,
                               quantity=Decimal(str(qty)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN),
                               avg_entry_price=price,
                               buy_fee_rate=Decimal(str(maker)))
                sess.add(pos)
                logger.info(f"Imported {sym}: {qty} @ {price}")
        except Exception as e:
            logger.error(f"Import failed: {e}")

    def place_limit_buy_with_tracking(self, symbol, price: str, qty: float):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.order_limit_buy(symbol=symbol, quantity=qty, price=price)
                self.rate_manager.update_current()
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']),
                                      symbol=symbol, side='buy',
                                      price=Decimal(price), quantity=Decimal(str(qty))))
            return order
        except Exception as e:
            logger.error(f"Buy order failed: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol, price: str, qty: float):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.order_limit_sell(symbol=symbol, quantity=qty, price=price)
                self.rate_manager.update_current()
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']),
                                      symbol=symbol, side='sell',
                                      price=Decimal(price), quantity=Decimal(str(qty))))
            return order
        except Exception as e:
            logger.error(f"Sell order failed: {e}")
            return None

    def check_and_process_filled_orders(self):
        try:
            with DBManager() as sess:
                for po in sess.query(PendingOrder).all():
                    try:
                        with self.api_lock:
                            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                            o = self.client.get_order(symbol=po.symbol, orderId=int(po.binance_order_id))
                            self.rate_manager.update_current()
                        if o['status'] == 'FILLED':
                            fill_price = Decimal(o['cummulativeQuoteQty']) / Decimal(o['executedQty'])
                            self.record_trade(sess, po.symbol, po.side, fill_price,
                                              Decimal(o['executedQty']), po.binance_order_id, po)
                            sess.delete(po)
                            action = "BUY" if po.side == 'buy' else "SELL"
                            logger.info(f"{action} FILLED: {po.symbol} @ {fill_price}")
                            with self.state_lock:
                                if po.side == 'buy' and po.symbol in trailing_buy_active:
                                    del trailing_buy_active[po.symbol]
                                if po.side == 'sell' and po.symbol in trailing_sell_active:
                                    del trailing_sell_active[po.symbol]
                    except Exception as e:
                        logger.debug(f"Order check error: {e}")
        except Exception as e:
            logger.error(f"Process filled orders error: {e}")

    def record_trade(self, sess, symbol, side, price, qty, binance_id, pending):
        trade = Trade(symbol=symbol, side=side, price=price, quantity=qty,
                      binance_order_id=binance_id, pending_order=pending)
        sess.add(trade)
        pos = sess.query(Position).filter_by(symbol=symbol).one_or_none()
        if side == "buy":
            if not pos:
                maker, _ = self.get_trade_fees(symbol)
                pos = Position(symbol=symbol, quantity=qty, avg_entry_price=price,
                               buy_fee_rate=maker)
                sess.add(pos)
            else:
                total = pos.quantity * pos.avg_entry_price + qty * price
                pos.quantity += qty
                pos.avg_entry_price = total / pos.quantity
        else:
            if pos:
                pos.quantity -= qty
                if pos.quantity <= 0:
                    sess.delete(pos)
                    with self.state_lock:
                        positions.pop(symbol, None)

    def can_place_buy_order(self, symbol: str) -> bool:
        with self.state_lock:
            return time.time() - buy_cooldown.get(symbol, 0) >= 15 * 60

    def record_buy_placed(self, symbol: str):
        with self.state_lock:
            buy_cooldown[symbol] = time.time()

    def is_trailing_buy_active(self, symbol):
        with self.state_lock:
            return symbol in trailing_buy_active

    def is_trailing_sell_active(self, symbol):
        with self.state_lock:
            return symbol in trailing_sell_active

    def get_trailing_buy_actives(self):
        with self.state_lock:
            return list(trailing_buy_active.keys())

    def get_trailing_sell_actives(self):
        with self.state_lock:
            return list(trailing_sell_active.keys())

    def start_trailing_buy(self, symbol):
        with self.state_lock:
            if symbol in trailing_buy_active: return
            trailing_buy_active[symbol] = {
                'lowest_price': Decimal('inf'),
                'last_buy_order_id': None,
                'start_time': time.time()
            }
        logger.info(f"Trailing BUY started for {symbol}")
        send_whatsapp_alert(f"TRAILING BUY ACTIVE: {symbol}")

    @retry_custom
    def get_ATR(self, symbol: str, period: int = 14) -> float:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=period + 1)
            self.rate_manager.update_current()
        highs = np.array([safe_float(k[2]) for k in klines])
        lows = np.array([safe_float(k[3]) for k in klines])
        closes = np.array([safe_float(k[4]) for k in klines])
        if len(highs) < period:
            return 0.0
        atr = talib.ATR(highs, lows, closes, timeperiod=period)[-1]
        return safe_float(atr)

    def get_dynamic_stop(self, symbol: str, current_high_price: Decimal, ob: dict) -> Decimal:
        strong_bids = [(p, q) for p, q in ob['raw_bids'] if p < current_high_price]
        if not strong_bids:
            return current_high_price * Decimal('0.995')
        wall_price, _ = max(strong_bids, key=lambda x: x[1])
        stop_price = wall_price * Decimal('0.999')
        atr_value = self.get_ATR(symbol)
        if atr_value > 0:
            atr_decimal = to_decimal(atr_value)
            min_trail_dist = (atr_decimal / current_high_price) * Decimal('1.0')
            min_stop = current_high_price * (Decimal('1') - min_trail_dist)
            if stop_price > min_stop:
                stop_price = min_stop
        return stop_price

    def get_dynamic_entry(self, symbol: str, current_low_price: Decimal, ob: dict) -> Decimal:
        strong_asks = [(p, q) for p, q in ob['raw_asks'] if p > current_low_price]
        if not strong_asks:
            return current_low_price * Decimal('1.005')
        wall_price, _ = max(strong_asks, key=lambda x: x[1])
        entry_price = wall_price * Decimal('1.001')
        atr_value = self.get_ATR(symbol)
        if atr_value > 0:
            atr_decimal = to_decimal(atr_value)
            min_trail_dist = (atr_decimal / current_low_price) * Decimal(str(ATR_TRAIL_MULTIPLIER_BUY))
            min_entry = current_low_price * (Decimal('1') + min_trail_dist)
            if entry_price < min_entry:
                entry_price = min_entry
        return entry_price

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

            if is_fast_move(float(ask), lp, lt, direction='up', threshold_pct=0.5):
                self.place_buy_at_price(symbol, None, force_market=True)
                with self.state_lock:
                    trailing_buy_active.pop(symbol, None)
                return

            if is_fast_move(float(ask), lp, lt, direction='down', threshold_pct=1.5):
                self.place_buy_at_price(symbol, None, force_market=True)
                with self.state_lock:
                    trailing_buy_active.pop(symbol, None)
                return

            with self.state_lock:
                if ask < state['lowest_price']:
                    state['lowest_price'] = ask
                trailing_buy_active[symbol] = state

            rsi, trend, low24 = self.get_rsi_and_trend(symbol)
            if rsi is None or rsi > RSI_OVERSOLD or trend != 'bullish':
                return

            with self.state_lock:
                if symbol not in sell_pressure_history:
                    sell_pressure_history[symbol] = deque(maxlen=5)
                sell_pressure_history[symbol].append(ob['pct_ask'])
                hist = list(sell_pressure_history[symbol])

            custom_low, _, _ = self.get_24h_price_stats(symbol)
            if custom_low and ask > Decimal(str(custom_low)) * Decimal('1.02'):
                return

            dynamic_entry = self.get_dynamic_entry(symbol, state['lowest_price'], ob)
            if ask >= dynamic_entry:
                logger.info(f"DYNAMIC ENTRY HIT: {symbol} @ {ask:.6f} (above wall @ {dynamic_entry:.6f})")
                send_whatsapp_alert(f"DYNAMIC ENTRY {symbol} @ {ask:.6f}")
                self.place_buy_at_price(symbol, ask)
                with self.state_lock:
                    if symbol in trailing_buy_active:
                        del trailing_buy_active[symbol]
                return

            if len(hist) >= 3:
                peak = max(hist)
                cur = hist[-1]
                if peak >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and cur <= peak * 0.9:
                    self.place_buy_at_price(symbol, ask)
                    with self.state_lock:
                        if symbol in trailing_buy_active:
                            del trailing_buy_active[symbol]
                    return

            if ask > state['lowest_price'] * Decimal('1.003'):
                self.place_buy_at_price(symbol, ask)
                with self.state_lock:
                    if symbol in trailing_buy_active:
                        del trailing_buy_active[symbol]
                return

        except Exception as e:
            logger.debug(f"Trailing buy error [{symbol}]: {e}")

    def place_buy_at_price(self, symbol, price: Optional[Decimal], force_market=False):
        with self.state_lock:
            state = trailing_buy_active.get(symbol, {})
        try:
            if state.get('last_buy_order_id') and not force_market:
                try:
                    with self.api_lock:
                        self.rate_manager.wait_if_needed('ORDERS')
                        self.client.cancel_order(symbol=symbol, orderId=int(state['last_buy_order_id']))
                        self.rate_manager.update_current()
                except: pass

            bal = self.get_balance()
            if bal <= MIN_BALANCE: return
            alloc = min(Decimal(str(bal - MIN_BALANCE)) * Decimal(str(RISK_PER_TRADE)),
                        Decimal(str(bal - MIN_BALANCE)))

            if force_market or price is None:
                with self.api_lock:
                    self.rate_manager.wait_if_needed('ORDERS')
                    order = self.client.order_market_buy(symbol=symbol, quoteOrderQty=float(alloc))
                    self.rate_manager.update_current()
                fill = Decimal(order['fills'][0]['price']) if order['fills'] else Decimal('0')
                logger.info(f"MARKET BUY {symbol} @ {fill}")
                send_whatsapp_alert(f"MARKET BUY {symbol} @ {fill:.6f} (flash dip)")
            else:
                qty = alloc / price
                adj, err = validate_and_adjust_order(self, symbol, 'BUY', ORDER_TYPE_LIMIT, qty, price)
                if not adj or err: return
                order = self.place_limit_buy_with_tracking(symbol, str(adj['price']), adj['quantity'])
                if order:
                    with self.state_lock:
                        state['last_buy_order_id'] = str(order['orderId'])
                        trailing_buy_active[symbol] = state
                fill = Decimal(adj['price'])

            if order:
                self.record_buy_placed(symbol)
                send_whatsapp_alert(f"BUY EXECUTED {symbol} @ {fill:.6f}")
                logger.info(f"DYNAMIC BUY {symbol} @ {fill}")

        except Exception as e:
            logger.error(f"Buy failed: {e}")

    def start_trailing_sell(self, symbol, pos):
        with self.state_lock:
            if symbol in trailing_sell_active: return
            entry = Decimal(str(pos.avg_entry_price))
            trailing_sell_active[symbol] = {
                'entry_price': entry,
                'qty': pos.quantity,
                'peak_price': entry,
                'last_sell_order_id': None,
                'price_peaks_history': [],
                'start_time': time.time()
            }
        logger.info(f"Trailing SELL started for {symbol} @ {entry:.6f}")
        send_whatsapp_alert(f"TRAILING SELL ACTIVE: {symbol} @ {entry:.6f}")

    def process_trailing_sell(self, symbol):
        with self.state_lock:
            if symbol not in trailing_sell_active: return
            state = trailing_sell_active[symbol]
        try:
            ob = self.get_order_book_analysis(symbol)
            bid = ob['best_bid']
            if bid <= ZERO: return

            with self.state_lock:
                if bid > state['peak_price']:
                    state['peak_price'] = bid
                trailing_sell_active[symbol] = state

            if is_fast_move(float(bid), float(state['peak_price']), state['start_time'], direction='down', threshold_pct=0.5):
                self.place_sell_at_price(symbol, None, force_market=True)
                with self.state_lock:
                    trailing_sell_active.pop(symbol, None)
                return

            maker, taker = self.get_trade_fees(symbol)
            net = (bid - state['entry_price']) / state['entry_price'] - Decimal(str(maker)) - Decimal(str(taker))
            if net < PROFIT_TARGET_NET:
                return

            rsi, trend, _ = self.get_rsi_and_trend(symbol)
            if rsi is None or rsi < RSI_OVERBOUGHT or trend != 'bearish':
                return

            dynamic_stop = self.get_dynamic_stop(symbol, state['peak_price'], ob)
            if bid < dynamic_stop:
                logger.info(f"DYNAMIC STOP HIT: {symbol} @ {bid:.6f} (below wall @ {dynamic_stop:.6f})")
                send_whatsapp_alert(f"DYNAMIC STOP HIT {symbol} @ {bid:.6f}")
                self.place_sell_at_price(symbol, bid)
                with self.state_lock:
                    if symbol in trailing_sell_active:
                        del trailing_sell_active[symbol]
                return

            with self.state_lock:
                if symbol not in buy_pressure_history:
                    buy_pressure_history[symbol] = deque(maxlen=5)
                buy_pressure_history[symbol].append(ob['pct_bid'])
                hist = list(buy_pressure_history[symbol])

            if len(hist) >= 3:
                peak = max(hist)
                cur = hist[-1]
                if peak >= ORDERBOOK_BUY_PRESSURE_SPIKE * 100 and cur <= ORDERBOOK_BUY_PRESSURE_DROP * 100:
                    self.place_sell_at_price(symbol, bid)
                    with self.state_lock:
                        if symbol in trailing_sell_active:
                            del trailing_sell_active[symbol]
                    return

            if self._detect_stall(symbol, bid):
                logger.info(f"STALLED 15 min {symbol} @ {bid:.6f} → MARKET SELL")
                send_whatsapp_alert(f"STALLED 15 MIN {symbol} @ {bid:.6f} → MARKET SELL")
                self.place_sell_at_price(symbol, None, force_market=True)
                with self.state_lock:
                    if symbol in trailing_sell_active:
                        del trailing_sell_active[symbol]
                return

        except Exception as e:
            logger.debug(f"Trailing sell error [{symbol}]: {e}")

    def _detect_stall(self, symbol, cur_price):
        with self.state_lock:
            state = trailing_sell_active.get(symbol, {})
            hist = state.get('price_peaks_history', [])
        now = time.time()
        if not hist or cur_price > hist[-1][1]:
            hist.append((now, cur_price))
        cutoff = now - STALL_THRESHOLD_SECONDS
        hist = [(t, p) for t, p in hist if t > cutoff]
        with self.state_lock:
            state['price_peaks_history'] = hist
            trailing_sell_active[symbol] = state
        return bool(hist) and now - hist[-1][0] >= STALL_THRESHOLD_SECONDS

    def place_sell_at_price(self, symbol, price: Optional[Decimal], force_market=False):
        with self.state_lock:
            state = trailing_sell_active.get(symbol, {})
        try:
            if state.get('last_sell_order_id') and not force_market:
                try:
                    with self.api_lock:
                        self.rate_manager.wait_if_needed('ORDERS')
                        self.client.cancel_order(symbol=symbol, orderId=int(state['last_sell_order_id']))
                        self.rate_manager.update_current()
                except: pass

            qty = state['qty']
            if force_market or price is None:
                with self.api_lock:
                    self.rate_manager.wait_if_needed('ORDERS')
                    order = self.client.order_market_sell(symbol=symbol, quantity=float(qty))
                    self.rate_manager.update_current()
                fill = Decimal(order['fills'][0]['price']) if order['fills'] else state['peak_price']
                logger.info(f"MARKET SELL {symbol} @ {fill}")
                send_whatsapp_alert(f"MARKET SELL {symbol} @ {fill:.6f} (stall)")
            else:
                tick = self.get_tick_size(symbol)
                price = ((price // tick) + 1) * tick
                adj, _ = validate_and_adjust_order(self, symbol, 'SELL', ORDER_TYPE_LIMIT, qty, price)
                if not adj: return
                order = self.place_limit_sell_with_tracking(symbol, str(adj['price']), adj['quantity'])
                if order:
                    with self.state_lock:
                        state['last_sell_order_id'] = str(order['orderId'])
                        trailing_sell_active[symbol] = state
                fill = Decimal(adj['price'])

            if order:
                send_whatsapp_alert(f"SELL EXECUTED {symbol} @ {fill:.6f}")
                logger.info(f"DYNAMIC SELL {symbol} @ {fill}")

        except Exception as e:
            logger.error(f"Sell failed: {e}")

# === HELPER FUNCTIONS =======================================================
def is_fast_move(current_price, last_price, last_time, direction='up', threshold_pct=0.5, window_sec=30):
    now = time.time()
    if now - last_time > window_sec: return False
    change = (current_price - last_price) / last_price * 100
    if direction == 'up' and change >= threshold_pct: return True
    if direction == 'down' and change <= -threshold_pct: return True
    return False

def validate_and_adjust_order(bot, symbol, side, order_type, quantity, price):
    try:
        with bot.api_lock:
            bot.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            info = bot.client.get_symbol_info(symbol)
            bot.rate_manager.update_current()
        lot = next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        price_f = next(f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER')
        step = Decimal(lot['stepSize'])
        tick = Decimal(price_f['tickSize'])
        qty = (quantity // step) * step
        price = (price // tick) * tick
        return {'quantity': float(qty), 'price': float(price)}, None
    except Exception as e:
        logger.error(f"Filter error {symbol}: {e}")
        return None, "Filter error"

def send_whatsapp_alert(message: str):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(message)}&apikey={CALLMEBOT_API_KEY}"
            requests.get(url, timeout=5)
        except Exception as e:
            logger.debug(f"WhatsApp failed: {e}")

def now_cst():
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


# === DASHBOARD ==============================================================
def print_professional_dashboard(bot):
    try:
        GREEN  = "\033[32m"
        RED    = "\033[31m"
        YELLOW = "\033[33m"
        BOLD   = "\033[1m"
        RESET  = "\033[0m"
        DIVIDER = "=" * 120

        os.system('cls' if os.name == 'nt' else 'clear')

        # === HEADER ===
        print(DIVIDER)
        print(f"{BOLD}{'SMART COIN TRADING BOT':^120}{RESET}")
        print(DIVIDER)

        now_str = now_cst()
        usdt_free = bot.get_balance('USDT')
        total_port, _ = bot.calculate_total_portfolio_value()
        tbuy_cnt = len(trailing_buy_active)
        tsel_cnt = len(trailing_sell_active)

        print(f"Current Time: {now_str}")
        print(f"Available USDT: ${usdt_free:,.6f}")
        print(f"Total Portfolio Value: ${total_port:,.6f}")
        print(f"Active Trailing Orders: {tbuy_cnt} buys, {tsel_cnt} sells")

        # === DEPTH IMBALANCE BARS (Top, 50-char wide, 1 blank line under each) ===
        print(f"\n{BOLD}DEPTH IMBALANCE BARS (Top 10 by Volume){RESET}")

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

            bid_blocks = max(0, min(BAR_WIDTH, int(pct_bid / 2)))
            ask_blocks = max(0, min(BAR_WIDTH, int(pct_ask / 2)))

            bid_bar = GREEN + "█" * bid_blocks + RESET
            ask_bar = RED + "█" * ask_blocks + RESET
            neutral = "░" * (BAR_WIDTH - bid_blocks - ask_blocks)
            bar = bid_bar + neutral + ask_bar[::-1]
            bar = (bar + "░" * BAR_WIDTH)[:BAR_WIDTH]

            bias = ("strong bid wall" if pct_bid > 60 else
                    "strong ask pressure" if pct_bid < 40 else
                    "balanced")

            coin = sym.replace("USDT", "")
            print(f"{coin:<9} |{bar}|  {pct_bid:>3.0f}% bid / {pct_ask:>3.0f}% ask")
            print(f"{'':<9}   {bias:<20}")
            print()

        print(DIVIDER)

        # === CURRENT POSITIONS ===
        print(f"{BOLD}{'CURRENT POSITIONS':^120}{RESET}")
        print(f"{'SYMBOL':<10} {'QUANTITY':>12} {'ENTRY PRICE':>12} {'CURRENT PRICE':>12} {'RSI':>6} {'P&L %':>8} {'PROFIT':>10} {'STATUS':<30}")

        with DBManager() as sess:
            positions_list = sess.query(Position).all()[:15]

        total_pnl = Decimal('0')
        for pos in positions_list:
            sym = pos.symbol
            qty = float(pos.quantity)
            entry = float(pos.avg_entry_price)
            ob = bot.get_order_book_analysis(sym)
            cur = float(ob['best_bid'] or ob['best_ask'])
            rsi, _, _ = bot.get_rsi_and_trend(sym)
            rsi_str = f"{rsi:5.1f}" if rsi else " N/A "

            maker, taker = bot.get_trade_fees(sym)
            gross = (cur - entry) * qty
            fee = (maker + taker) * cur * qty
            net = gross - fee
            pnl_pct = ((cur - entry) / entry - (maker + taker)) * 100
            total_pnl += Decimal(str(net))

            # === FIXED BLOCK ===
            status = "Held"
            if sym in trailing_sell_active:
                state = trailing_sell_active[sym]
                peak = state['peak_price']
                ob_data = bot.get_order_book_analysis(sym)
                stop = bot.get_dynamic_stop(sym, peak, ob_data)
                status = f"Trailing Sell (Stop: {float(stop):.6f})"
            elif sym in trailing_buy_active:
                state = trailing_buy_active[sym]
                low = state['lowest_price']  # ← FIXED: was stateLowest_price']
                ob_data = bot.get_order_book_analysis(sym)
                entry = bot.get_dynamic_entry(sym, low, ob_data)
                status = f"Trailing Buy (Entry: {float(entry):.6f})"
            else:
                status = "24/7 Monitoring"

            pnl_color = GREEN if net > 0 else RED
            pct_color = GREEN if pnl_pct > 0 else RED

            print(f"{sym:<10} {qty:>12.6f} {entry:>12.6f} {cur:>12.6f} "
                  f"{rsi_str} {pct_color}{pnl_pct:>7.2f}%{RESET} {pnl_color}{net:>9.2f}{RESET} {status:<30}")

        for _ in range(len(positions_list), 15):
            print(" " * 120)

        total_pnl_color = GREEN if total_pnl > 0 else RED
        print(DIVIDER)
        print(f"TOTAL UNREALIZED PROFIT & LOSS: {total_pnl_color}${float(total_pnl):>12,.2f}{RESET}")
        print(DIVIDER)

        # === MARKET OVERVIEW ===
        print(f"{BOLD}{'MARKET OVERVIEW':^120}{RESET}")

        valid_cnt = len(valid_symbols_dict)
        avg_vol = sum(s['volume'] for s in valid_symbols_dict.values()) if valid_symbols_dict else 0

        print(f"Number of Valid Symbols: {valid_cnt}")
        print(f"Average 24h Volume: ${avg_vol:,.0f}")
        print(f"Price Range: ${MIN_PRICE} to ${MAX_PRICE}")

        # === Coin Buy List ===
        watch_items = []
        for sym in valid_symbols_dict:
            ob = bot.get_order_book_analysis(sym)
            rsi, trend, _ = bot.get_rsi_and_trend(sym)
            bid = ob['best_bid']
            if not bid: continue
            custom_low, _, _ = bot.get_24h_price_stats(sym)
            strong_buy = (ob['depth_skew'] == 'strong_ask' and
                          ob['imbalance_ratio'] <= 0.5 and
                          ob['weighted_pressure'] < -0.002)
            if (rsi and rsi <= RSI_OVERSOLD and trend == 'bullish' and
                custom_low and bid <= Decimal(str(custom_low)) * Decimal('1.01') and strong_buy):
                coin = sym.replace('USDT', '')
                watch_items.append(f"{coin}({rsi:.0f})")

        watch_str = " | ".join(watch_items[:18]) if watch_items else "No active buy signals"
        if len(watch_str) > 100: watch_str = watch_str[:97] + "..."
        print(f"\nCoin Buy List: {watch_str}")

        print(DIVIDER)

    except Exception as e:
        logger.error(f"Dashboard print failed: {e}")


# === THREADS ================================================================
def buy_scanner(bot):
    while True:
        try:
            for sym in list(valid_symbols_dict.keys()):
                ob = bot.get_order_book_analysis(sym)
                rsi, trend, low24 = bot.get_rsi_and_trend(sym)
                bid = ob['best_bid']
                ask = ob['best_ask']
                if bid <= ZERO or ask <= ZERO: continue
                with DBManager() as sess:
                    if not sess.query(Position).filter_by(symbol=sym).first():
                        custom_low, _, _ = bot.get_24h_price_stats(sym)
                        if (rsi is not None and rsi <= RSI_OVERSOLD and
                            trend == 'bullish' and
                            custom_low and bid <= Decimal(str(custom_low)) * Decimal('1.01') and
                            ob['pct_ask'] >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
                            not bot.is_trailing_buy_active(sym)):
                            if bot.can_place_buy_order(sym):
                                bot.start_trailing_buy(sym)
                time.sleep(POLL_INTERVAL / max(1, len(valid_symbols_dict)))
        except Exception as e:
            logger.critical(f"Buy scanner error: {e}", exc_info=True)
            time.sleep(10)

def sell_scanner(bot):
    while True:
        try:
            for sym in list(valid_symbols_dict.keys()):
                ob = bot.get_order_book_analysis(sym)
                rsi, trend, low24 = bot.get_rsi_and_trend(sym)
                bid = ob['best_bid']
                ask = ob['best_ask']
                if bid <= ZERO or ask <= ZERO: continue
                with DBManager() as sess:
                    pos = sess.query(Position).filter_by(symbol=sym).one_or_none()
                    if pos and not bot.is_trailing_sell_active(sym):
                        entry = Decimal(str(pos.avg_entry_price))
                        maker, taker = bot.get_trade_fees(sym)
                        net = (ask - entry) / entry - Decimal(str(maker)) - Decimal(str(taker))
                        if net >= PROFIT_TARGET_NET and rsi is not None and rsi >= RSI_OVERBOUGHT and trend == 'bearish':
                            with bot.state_lock:
                                if sym not in buy_pressure_history:
                                    buy_pressure_history[sym] = deque(maxlen=5)
                                buy_pressure_history[sym].append(ob['pct_bid'])
                                hist = list(buy_pressure_history[sym])
                            if len(hist) >= 3 and max(hist) >= ORDERBOOK_BUY_PRESSURE_SPIKE * 100 and hist[-1] <= ORDERBOOK_BUY_PRESSURE_DROP * 100:
                                bot.start_trailing_sell(sym, pos)
                time.sleep(POLL_INTERVAL / max(1, len(valid_symbols_dict)))
        except Exception as e:
            logger.critical(f"Sell scanner error: {e}", exc_info=True)
            time.sleep(10)

def trailing_buy_processor(bot):
    while True:
        try:
            actives = bot.get_trailing_buy_actives()
            for sym in actives:
                bot.process_trailing_buy(sym)
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.critical(f"Trailing buy processor error: {e}", exc_info=True)
            time.sleep(10)

def trailing_sell_processor(bot):
    while True:
        try:
            actives = bot.get_trailing_sell_actives()
            for sym in actives:
                bot.process_trailing_sell(sym)
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.critical(f"Trailing sell processor error: {e}", exc_info=True)
            time.sleep(10)

# === MAIN ===================================================================
def main():
    if not API_KEY or not API_SECRET:
        logger.critical("API keys missing.")
        sys.exit(1)

    bot = BinanceTradingBot()

    for attempt in range(2):
        if bot.fetch_and_validate_usdt_pairs():
            break
        logger.warning("No symbols fetched – retrying in 5s...")
        time.sleep(5)
    else:
        logger.critical("Failed to fetch any valid symbols.")
        sys.exit(1)

    with DBManager() as sess:
        bot.import_owned_assets_to_db(sess)
        sess.commit()

    threading.Thread(target=buy_scanner, args=(bot,), daemon=True).start()
    threading.Thread(target=sell_scanner, args=(bot,), daemon=True).start()
    threading.Thread(target=trailing_buy_processor, args=(bot,), daemon=True).start()
    threading.Thread(target=trailing_sell_processor, args=(bot,), daemon=True).start()

    print_professional_dashboard(bot)
    logger.info("Multi-threaded bot started.")
    last_full = time.time()
    FULL_INTERVAL = 45.0

    while True:
        try:
            bot.check_and_process_filled_orders()
            now = time.time()
            if now - last_full >= FULL_INTERVAL:
                os.system('cls' if os.name == 'nt' else 'clear')
                print_professional_dashboard(bot)
                last_full = now
            time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nShutting down...")
            break
        except Exception as e:
            logger.critical(f"Main loop error: {e}", exc_info=True)
            time.sleep(10)

if __name__ == "__main__":
    main()
