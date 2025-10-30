#!/usr/bin/env python3
"""
Binance.US Dynamic Trailing Bot – FINAL VERSION
- Flash Dip → Market Buy
- 15-Min Price Stall → Market Sell
- No 60-min timeout
- Full Dashboard + Alerts
"""

import os
import time
import logging
import signal
import sys
import numpy as np
import threading
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
from tenacity import retry, stop_after_attempt, wait_exponential
import talib
from datetime import datetime
import pytz
import requests
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, Optional, Tuple
from collections import deque

# === SQLALCHEMY ==============================================================
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, func
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import SQLAlchemyError

# === CONFIGURATION ===========================================================
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')
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
ORDER_BOOK_LIMIT = 20
POLL_INTERVAL = 1.0
STALL_THRESHOLD_SECONDS = 15 * 60  # 15 minutes
RAPID_DROP_THRESHOLD = 0.01  # 1.0%
RAPID_DROP_WINDOW = 5.0      # seconds

# API Keys
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

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
active_threads: Dict[str, threading.Thread] = {}
dynamic_buy_threads: Dict[str, threading.Thread] = {}
dynamic_sell_threads: Dict[str, threading.Thread] = {}
last_price_cache: Dict[str, Tuple[float, float]] = {}  # (price, timestamp)
thread_lock = threading.Lock()
dashboard_lock = threading.Lock()

# === Buy cooldown & order cancellation tracking ===
buy_cooldown: Dict[str, float] = {}
cancel_timer_threads: Dict[str, threading.Thread] = {}
cancel_timer_lock = threading.Lock()

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
            except SQLAlchemyError:
                self.session.rollback()
        self.session.close()

# === SIGNAL HANDLER =========================================================
def signal_handler(signum, frame):
    logger.info("Shutting down all threads...")
    with thread_lock:
        for symbol, thread in active_threads.items():
            thread.running = False
    with cancel_timer_lock:
        for t in cancel_timer_threads.values():
            t.running = False
    for t in list(dynamic_buy_threads.values()) + list(dynamic_sell_threads.values()):
        t.running = False
    sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)

# === SAFE MATH ==============================================================
def safe_float(value, default=0.0) -> float:
    try:
        return float(value) if value is not None and np.isfinite(float(value)) else default
    except:
        return default

def is_valid_float(value) -> bool:
    return value is not None and np.isfinite(value) and value > 0

def to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except:
        return ZERO

# === FETCH SYMBOLS ==========================================================
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def fetch_and_validate_usdt_pairs(client) -> Dict[str, dict]:
    global valid_symbols_dict
    try:
        info = client.get_exchange_info()
        raw_symbols = [
            s['symbol'] for s in info['symbols']
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING' and s['symbol'].endswith('USDT')
        ]

        EXCLUDED_PAIRS = {'USDCUSDT', 'USDTUSDT'}
        raw_symbols = [s for s in raw_symbols if s not in EXCLUDED_PAIRS]

        valid = {}
        for symbol in raw_symbols:
            try:
                ticker = client.get_ticker(symbol=symbol)
                price = safe_float(ticker.get('lastPrice'))
                volume = safe_float(ticker.get('quoteVolume'))
                low_24h = safe_float(ticker.get('lowPrice'))

                if not (MIN_PRICE <= price <= MAX_PRICE and volume >= MIN_24H_VOLUME_USDT and is_valid_float(low_24h)):
                    continue

                valid[symbol] = {
                    'price': price,
                    'volume': volume,
                    'low_24h': low_24h
                }
            except Exception as e:
                logger.debug(f"Failed to validate {symbol}: {e}")
                continue

        valid_symbols_dict = valid
        logger.info(f"Valid symbols: {len(valid)}")
        return valid
    except Exception as e:
        logger.error(f"Symbol fetch failed: {e}")
        return {}

# === ORDER BOOK =============================================================
def get_order_book_analysis(client, symbol: str) -> dict:
    now = time.time()
    cache = order_book_cache.get(symbol, {})
    if cache and now - cache.get('ts', 0) < 1:
        return cache

    try:
        depth = client.get_order_book(symbol=symbol, limit=ORDER_BOOK_LIMIT)
        bids = depth.get('bids', [])[:5]
        asks = depth.get('asks', [])[:5]

        bid_vol = sum(Decimal(b[1]) for b in bids)
        ask_vol = sum(Decimal(a[1]) for a in asks)
        total = bid_vol + ask_vol or Decimal('1')

        result = {
            'pct_bid': float(bid_vol / total * 100),
            'pct_ask': float(ask_vol / total * 100),
            'best_bid': Decimal(bids[0][0]) if bids else ZERO,
            'best_ask': Decimal(asks[0][0]) if asks else ZERO,
            'ts': now
        }
        order_book_cache[symbol] = result
        return result
    except:
        return {'pct_bid': 50.0, 'pct_ask': 50.0, 'best_bid': ZERO, 'best_ask': ZERO}

# === TICK SIZE HELPER =======================================================
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_tick_size(client, symbol):
    try:
        info = client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                return Decimal(f['tickSize'])
        return Decimal('0.00000001')
    except:
        return Decimal('0.00000001')

# === METRICS ================================================================
def get_rsi_and_trend(client, symbol) -> Tuple[Optional[float], str, Optional[float]]:
    try:
        klines = client.get_klines(symbol=symbol, interval='1m', limit=100)
        closes = np.array([safe_float(k[4]) for k in klines[-100:]])
        if len(closes) < RSI_PERIOD: return None, "unknown", None

        rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)[-1]
        if not np.isfinite(rsi): return None, "unknown", None

        upper, middle, _ = talib.BBANDS(closes, timeperiod=BB_PERIOD, nbdevup=BB_DEV, nbdevdn=BB_DEV)
        macd, signal_line, _ = talib.MACD(closes, fastperiod=MACD_FAST, slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)

        trend = ("bullish" if closes[-1] > middle[-1] and macd[-1] > signal_line[-1] else
                 "bearish" if closes[-1] < middle[-1] and macd[-1] < signal_line[-1] else "sideways")

        low_24h = valid_symbols_dict.get(symbol, {}).get('low_24h')
        return float(rsi), trend, float(low_24h) if low_24h else None
    except:
        return None, "unknown", None

# === 15-MIN BUY COOLDOWN ====================================================
def can_place_buy_order(symbol: str) -> bool:
    now = time.time()
    last_buy = buy_cooldown.get(symbol, 0)
    return now - last_buy >= 15 * 60

def record_buy_placed(symbol: str):
    buy_cooldown[symbol] = time.time()

# === 60-MIN ORDER CANCELLATION TIMER ========================================
def start_order_cancellation_timer(client, order_id: str, symbol: str):
    def _cancel_after_delay():
        time.sleep(60 * 60)
        if not getattr(_cancel_after_delay, "running", True):
            return
        try:
            client.cancel_order(symbol=symbol, orderId=int(order_id))
            logger.info(f"CANCELLED unfilled order {order_id} for {symbol} after 60 min")
            send_whatsapp_alert(f"CANCELLED {symbol} order {order_id} (60 min timeout)")
        except Exception as e:
            logger.warning(f"Failed to cancel order {order_id}: {e}")
        finally:
            with cancel_timer_lock:
                cancel_timer_threads.pop(order_id, None)

    thread = threading.Thread(target=_cancel_after_delay, daemon=True)
    thread.running = True
    with cancel_timer_lock:
        cancel_timer_threads[order_id] = thread
    thread.start()

# === DYNAMIC TRAILING BUY THREAD ============================================
class DynamicBuyThread(threading.Thread):
    def __init__(self, client, symbol, bot):
        super().__init__(daemon=True)
        self.client = client
        self.symbol = symbol
        self.bot = bot
        self.running = True
        self.lowest_price = Decimal('inf')
        self.last_buy_order_id = None
        self.start_time = time.time()

    def run(self):
        logger.info(f"Started DYNAMIC BUY MONITOR for {self.symbol}")
        send_whatsapp_alert(f"TRAILING BUY ACTIVE: {self.symbol}")

        while self.running:
            try:
                ob = get_order_book_analysis(self.client, self.symbol)
                best_bid = ob['best_bid']
                if best_bid <= ZERO:
                    time.sleep(1)
                    continue

                current_price = float(best_bid)
                now = time.time()

                # RAPID DROP → MARKET BUY
                if self.symbol not in last_price_cache:
                    last_price_cache[self.symbol] = (current_price, now)
                else:
                    last_price, last_time = last_price_cache[self.symbol]
                    if now - last_time < RAPID_DROP_WINDOW:
                        drop_pct = (last_price - current_price) / last_price
                        if drop_pct >= RAPID_DROP_THRESHOLD:
                            logger.warning(f"FLASH DIP: {self.symbol} -{drop_pct:.2%} in {now-last_time:.1f}s")
                            send_whatsapp_alert(f"FLASH DIP {self.symbol}: -{drop_pct:.2%} → MARKET BUY")
                            self.place_buy_at_price(None, force_market=True)
                            break
                    last_price_cache[self.symbol] = (current_price, now)

                if best_bid < self.lowest_price:
                    self.lowest_price = best_bid

                rsi, trend, low_24h = get_rsi_and_trend(self.client, self.symbol)
                if rsi is None or rsi > RSI_OVERSOLD:
                    time.sleep(1)
                    continue

                history = sell_pressure_history.get(self.symbol, deque(maxlen=5))
                history.append(ob['pct_ask'])
                sell_pressure_history[self.symbol] = history

                if low_24h and best_bid > Decimal(str(low_24h)) * Decimal('1.02'):
                    time.sleep(1)
                    continue

                if len(history) >= 3:
                    peak = max(history)
                    current = history[-1]
                    if (peak >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
                        current <= peak * 0.9):
                        self.place_buy_at_price(best_bid)
                        break

                if best_bid > self.lowest_price * Decimal('1.003'):
                    self.place_buy_at_price(best_bid)
                    break

            except Exception as e:
                logger.debug(f"Dynamic buy error [{self.symbol}]: {e}")

            time.sleep(1)

        if self.running:
            self.cancel_order()

        with thread_lock:
            dynamic_buy_threads.pop(self.symbol, None)

    def place_buy_at_price(self, price: Optional[Decimal], force_market: bool = False):
        try:
            if self.last_buy_order_id and not force_market:
                try:
                    self.client.cancel_order(symbol=self.symbol, orderId=int(self.last_buy_order_id))
                except:
                    pass

            balance = get_balance(self.client)
            if balance <= MIN_BALANCE: return
            available = Decimal(str(balance - MIN_BALANCE))
            alloc = min(available * Decimal(str(RISK_PER_TRADE)), available)

            if force_market or price is None:
                order = self.client.order_market_buy(symbol=self.symbol, quoteOrderQty=float(alloc))
                fill_price = Decimal(order['fills'][0]['price']) if order['fills'] else Decimal(str(current_price))
                logger.info(f"MARKET BUY EXECUTED: {self.symbol} @ {fill_price}")
                send_whatsapp_alert(f"MARKET BUY {self.symbol} @ {fill_price:.6f} (flash dip)")
            else:
                qty = alloc / price
                adjusted, error = validate_and_adjust_order(self.client, self.symbol, 'BUY', ORDER_TYPE_LIMIT, qty, price)
                if not adjusted or error: return
                order = self.bot.place_limit_buy_with_tracking(self.symbol, str(adjusted['price']), float(adjusted['quantity']))
                if order:
                    self.last_buy_order_id = str(order['orderId'])
                    start_order_cancellation_timer(self.client, self.last_buy_order_id, self.symbol)
                fill_price = Decimal(adjusted['price'])

            if order:
                record_buy_placed(self.symbol)
                send_whatsapp_alert(f"BUY EXECUTED {self.symbol} @ {fill_price:.6f} (dynamic dip)")
                logger.info(f"DYNAMIC BUY: {self.symbol} @ {fill_price}")
        except Exception as e:
            logger.error(f"Dynamic buy failed: {e}")
        finally:
            self.running = False

    def cancel_order(self):
        if self.last_buy_order_id:
            try:
                self.client.cancel_order(symbol=self.symbol, orderId=int(self.last_buy_order_id))
            except:
                pass
        self.running = False

# === DYNAMIC TRAILING SELL THREAD ===========================================
class DynamicSellThread(threading.Thread):
    def __init__(self, client, symbol, bot, position):
        super().__init__(daemon=True)
        self.client = client
        self.symbol = symbol
        self.bot = bot
        self.position = position
        self.entry_price = Decimal(str(position.avg_entry_price))
        self.qty = position.quantity
        self.running = True
        self.peak_price = self.entry_price
        self.last_sell_order_id = None
        self.start_time = time.time()
        self.price_peaks_history = {}  # symbol → list of (timestamp, price)

    def run(self):
        logger.info(f"Started DYNAMIC SELL MONITOR for {self.symbol}")
        send_whatsapp_alert(f"TRAILING SELL ACTIVE: {self.symbol} @ {self.entry_price:.6f}")

        while self.running:
            try:
                ob = get_order_book_analysis(self.client, self.symbol)
                best_bid = ob['best_bid']
                if best_bid <= ZERO:
                    time.sleep(1)
                    continue

                current_price = best_bid

                if current_price > self.peak_price:
                    self.peak_price = current_price

                maker_fee, taker_fee = get_trade_fees(self.client, self.symbol)
                total_fee_rate = Decimal(str(maker_fee)) + Decimal(str(taker_fee))
                net_return = (current_price - self.entry_price) / self.entry_price - total_fee_rate

                if net_return < PROFIT_TARGET_NET:
                    time.sleep(1)
                    continue

                rsi, _, _ = get_rsi_and_trend(self.client, self.symbol)
                if rsi is None or rsi < RSI_OVERBOUGHT:
                    time.sleep(1)
                    continue

                history = buy_pressure_history.get(self.symbol, deque(maxlen=5))
                history.append(ob['pct_bid'])
                buy_pressure_history[self.symbol] = history

                if len(history) >= 3:
                    peak = max(history)
                    current = history[-1]
                    if peak >= ORDERBOOK_BUY_PRESSURE_SPIKE * 100 and current <= ORDERBOOK_BUY_PRESSURE_DROP * 100:
                        self.place_sell_at_price(best_bid)
                        break

                if best_bid < self.peak_price * Decimal('0.995'):
                    self.place_sell_at_price(best_bid)
                    break

                if self.detect_price_stall(current_price):
                    logger.info(f"STALLED 15 MIN at {current_price:.6f} → MARKET SELL")
                    send_whatsapp_alert(f"STALLED 15 MIN {self.symbol} @ {current_price:.6f} → MARKET SELL")
                    self.place_sell_at_price(None, force_market=True)
                    break

            except Exception as e:
                logger.debug(f"Dynamic sell error [{self.symbol}]: {e}")

            time.sleep(1)

        with thread_lock:
            dynamic_sell_threads.pop(self.symbol, None)
            self.price_peaks_history.pop(self.symbol, None)

    def detect_price_stall(self, current_price: Decimal, stall_threshold: int = STALL_THRESHOLD_SECONDS) -> bool:
        now = time.time()
        symbol = self.symbol

        if symbol not in self.price_peaks_history:
            self.price_peaks_history[symbol] = []

        if not self.price_peaks_history[symbol] or current_price > self.price_peaks_history[symbol][-1][1]:
            self.price_peaks_history[symbol].append((now, current_price))

        cutoff = now - stall_threshold
        self.price_peaks_history[symbol] = [
            (t, p) for t, p in self.price_peaks_history[symbol] if t > cutoff
        ]

        if self.price_peaks_history[symbol]:
            last_peak_time = self.price_peaks_history[symbol][-1][0]
            return now - last_peak_time >= stall_threshold
        return False

    def place_sell_at_price(self, price: Optional[Decimal], force_market: bool = False):
        try:
            if self.last_sell_order_id and not force_market:
                try:
                    self.client.cancel_order(symbol=self.symbol, orderId=int(self.last_sell_order_id))
                except:
                    pass

            if force_market or price is None:
                order = self.client.order_market_sell(symbol=self.symbol, quantity=float(self.qty))
                fill_price = Decimal(order['fills'][0]['price']) if order['fills'] else self.peak_price
                logger.info(f"MARKET SELL (STALL) {self.symbol} @ {fill_price}")
                send_whatsapp_alert(f"MARKET SELL {self.symbol} @ {fill_price:.6f} (15-min stall)")
            else:
                tick_size = get_tick_size(self.client, self.symbol)
                price = ((price // tick_size) + 1) * tick_size
                adjusted, _ = validate_and_adjust_order(self.client, self.symbol, 'SELL', ORDER_TYPE_LIMIT, self.qty, price)
                if not adjusted: return
                order = self.bot.place_limit_sell_with_tracking(self.symbol, str(adjusted['price']), float(adjusted['quantity']))
                if order:
                    self.last_sell_order_id = str(order['orderId'])
                    start_order_cancellation_timer(self.client, self.last_sell_order_id, self.symbol)
                fill_price = Decimal(adjusted['price'])

            if order:
                send_whatsapp_alert(f"SELL EXECUTED {self.symbol} @ {fill_price:.6f}")
                logger.info(f"DYNAMIC SELL: {self.symbol} @ {fill_price}")
        except Exception as e:
            logger.error(f"Dynamic sell failed: {e}")
        finally:
            self.running = False

# === COIN MONITOR THREAD ====================================================
class CoinMonitorThread(threading.Thread):
    def __init__(self, client, symbol, bot):
        super().__init__(daemon=True)
        self.client = client
        self.symbol = symbol
        self.bot = bot
        self.running = True

    def run(self):
        logger.info(f"Started 24/7 monitor thread for {self.symbol}")
        while self.running:
            try:
                ob = get_order_book_analysis(self.client, self.symbol)
                rsi, trend, low_24h = get_rsi_and_trend(self.client, self.symbol)

                buy_price = ob['best_bid']
                sell_price = ob['best_ask']

                if buy_price <= ZERO or sell_price <= ZERO:
                    time.sleep(POLL_INTERVAL)
                    continue

                # DYNAMIC BUY
                with DBManager() as sess:
                    if not sess.query(Position).filter_by(symbol=self.symbol).first():
                        if (rsi is not None and rsi <= RSI_OVERSOLD and
                            trend == 'bullish' and
                            low_24h and buy_price <= Decimal(str(low_24h)) * Decimal('1.01') and
                            ob['pct_ask'] >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
                            self.symbol not in dynamic_buy_threads):
                            if not can_place_buy_order(self.symbol):
                                time.sleep(POLL_INTERVAL)
                                continue
                            thread = DynamicBuyThread(self.client, self.symbol, self.bot)
                            with thread_lock:
                                dynamic_buy_threads[self.symbol] = thread
                            thread.start()
                            time.sleep(3)

                # DYNAMIC SELL
                with DBManager() as sess:
                    pos = sess.query(Position).filter_by(symbol=self.symbol).one_or_none()
                    if pos and self.symbol not in dynamic_sell_threads:
                        entry = Decimal(str(pos.avg_entry_price))
                        maker_fee, taker_fee = get_trade_fees(self.client, self.symbol)
                        total_fee_rate = Decimal(str(maker_fee)) + Decimal(str(taker_fee))
                        gross_return = (sell_price - entry) / entry
                        net_return = gross_return - total_fee_rate

                        if net_return >= PROFIT_TARGET_NET and rsi is not None and rsi >= RSI_OVERBOUGHT:
                            history = buy_pressure_history.get(self.symbol, deque(maxlen=5))
                            history.append(ob['pct_bid'])
                            buy_pressure_history[self.symbol] = history
                            if len(history) >= 3:
                                peak = max(history)
                                if (peak >= ORDERBOOK_BUY_PRESSURE_SPIKE * 100 and
                                    history[-1] <= ORDERBOOK_BUY_PRESSURE_DROP * 100):
                                    thread = DynamicSellThread(self.client, self.symbol, self.bot, pos)
                                    with thread_lock:
                                        dynamic_sell_threads[self.symbol] = thread
                                    thread.start()
                                    time.sleep(3)

            except Exception as e:
                logger.debug(f"Thread error [{self.symbol}]: {e}")

            time.sleep(POLL_INTERVAL)

# === BOT CLASS ==============================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        with DBManager() as sess:
            import_owned_assets_to_db(self.client, sess)
            sess.commit()
        self.load_state_from_db()

    def load_state_from_db(self):
        global positions
        with DBManager() as sess:
            for p in sess.query(Position).all():
                positions[p.symbol] = {
                    'qty': float(p.quantity),
                    'entry_price': float(p.avg_entry_price),
                    'buy_fee': float(p.buy_fee_rate)
                }

    def place_limit_buy_with_tracking(self, symbol, price: str, qty: float):
        try:
            order = self.client.order_limit_buy(symbol=symbol, quantity=qty, price=price)
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='buy',
                                    price=Decimal(price), quantity=Decimal(str(qty))))
            return order
        except: return None

    def place_limit_sell_with_tracking(self, symbol, price: str, qty: float):
        try:
            order = self.client.order_limit_sell(symbol=symbol, quantity=qty, price=price)
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='sell',
                                    price=Decimal(price), quantity=Decimal(str(qty))))
            return order
        except: return None

    def check_and_process_filled_orders(self):
        try:
            with DBManager() as sess:
                for pending in sess.query(PendingOrder).all():
                    try:
                        order = self.client.get_order(symbol=pending.symbol, orderId=int(pending.binance_order_id))
                        if order['status'] == 'FILLED':
                            fill_price = Decimal(order['cummulativeQuoteQty']) / Decimal(order['executedQty'])
                            self.record_trade(sess, pending.symbol, pending.side, fill_price, Decimal(order['executedQty']),
                                              pending.binance_order_id, pending)
                            sess.delete(pending)
                            action = "BUY" if pending.side == 'buy' else "SELL"
                            logger.info(f"{action} FILLED: {pending.symbol} @ {fill_price}")
                            with cancel_timer_lock:
                                t = cancel_timer_threads.pop(pending.binance_order_id, None)
                                if t: t.running = False
                            if pending.side == 'buy':
                                with thread_lock:
                                    t = dynamic_buy_threads.pop(pending.symbol, None)
                                    if t: t.running = False
                            else:
                                with thread_lock:
                                    t = dynamic_sell_threads.pop(pending.symbol, None)
                                    if t: t.running = False
                    except: pass
        except: pass

    def record_trade(self, sess, symbol, side, price, qty, binance_order_id, pending_order):
        trade = Trade(symbol=symbol, side=side, price=price, quantity=qty,
                      binance_order_id=binance_order_id, pending_order=pending_order)
        sess.add(trade)
        pos = sess.query(Position).filter_by(symbol=symbol).one_or_none()
        if side == "buy":
            if not pos:
                maker_fee, _ = get_trade_fees(self.client, symbol)
                pos = Position(symbol=symbol, quantity=qty, avg_entry_price=price, buy_fee_rate=maker_fee)
                sess.add(pos)
            else:
                total_cost = pos.quantity * pos.avg_entry_price + qty * price
                pos.quantity += qty
                pos.avg_entry_price = total_cost / pos.quantity
        else:
            if pos:
                pos.quantity -= qty
                if pos.quantity <= 0:
                    sess.delete(pos)
                    positions.pop(symbol, None)

# === HELPER FUNCTIONS =======================================================
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_balance(client, asset='USDT') -> float:
    try:
        for bal in client.get_account()['balances']:
            if bal['asset'] == asset:
                return safe_float(bal['free'])
        return 0.0
    except Exception as e:
        logger.warning(f"get_balance failed: {e}")
        return 0.0

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_trade_fees(client, symbol):
    try:
        fee = client.get_trade_fee(symbol=symbol)
        return safe_float(fee[0]['makerCommission']), safe_float(fee[0]['takerCommission'])
    except:
        return 0.001, 0.001

def validate_and_adjust_order(client, symbol, side, order_type, quantity, price):
    try:
        info = client.get_symbol_info(symbol)
        lot = next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        price_f = next(f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER')

        step_qty = Decimal(lot['stepSize'])
        tick = Decimal(price_f['tickSize'])

        qty = (quantity // step_qty) * step_qty
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
            logger.debug(f"WhatsApp alert failed: {e}")

def import_owned_assets_to_db(client, sess):
    try:
        account = client.get_account()
        for bal in account['balances']:
            asset = bal['asset']
            free_str = bal['free']
            qty = safe_float(free_str)
            if qty <= 0 or asset in {'USDT', 'USDC'}:
                continue

            symbol = f"{asset}USDT"
            if sess.query(Position).filter_by(symbol=symbol).first():
                continue

            price = get_price_usdt(client, asset)
            if price <= ZERO:
                logger.warning(f"Could not get price for {asset}, skipping import")
                continue

            maker_fee, _ = get_trade_fees(client, symbol)
            pos = Position(
                symbol=symbol,
                quantity=Decimal(str(qty)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN),
                avg_entry_price=price,
                buy_fee_rate=Decimal(str(maker_fee))
            )
            sess.add(pos)
            logger.info(f"Imported existing position {symbol}: {qty} @ {price}")
    except Exception as e:
        logger.error(f"import_owned_assets_to_db failed: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_price_usdt(client, asset: str) -> Decimal:
    if asset == 'USDT': return Decimal('1')
    symbol = asset + 'USDT'
    try:
        return Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
    except:
        return ZERO

def set_terminal_background_and_title():
    try:
        print("\033]0;TRADING BOT – LIVE\007", end='')
        print("\033[48;5;17m", end='')
        print("\033[2J\033[H", end='')
    except:
        pass

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def calculate_total_portfolio_value(client):
    try:
        account = client.get_account()
        total = Decimal('0')
        values = {}
        for b in account['balances']:
            qty = Decimal(str(safe_float(b['free'])))
            if qty <= 0: continue
            if b['asset'] == 'USDT':
                total += qty
                values['USDT'] = float(qty)
            else:
                price = get_price_usdt(client, b['asset'])
                if price > 0:
                    val = qty * price
                    total += val
                    values[b['asset']] = float(val)
        return float(total), values
    except Exception as e:
        logger.warning(f"Portfolio value error: {e}")
        return 0.0, {}

def now_cst():
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

# === PROFESSIONAL DASHBOARD =================================================
def print_professional_dashboard(client):
    try:
        with dashboard_lock:
            set_terminal_background_and_title()
            os.system('cls' if os.name == 'nt' else 'clear')
            now = now_cst()
            usdt_free = get_balance(client, 'USDT')
            total_portfolio, _ = calculate_total_portfolio_value(client)

            NAVY = "\033[48;5;17m"
            YELLOW = "\033[38;5;226m"
            GREEN = "\033[38;5;82m"
            RED = "\033[38;5;196m"
            RESET = "\033[0m"
            BOLD = "\033[1m"

            print(f"{NAVY}{'='*120}{RESET}")
            print(f"{NAVY}{YELLOW}{'TRADING BOT – LIVE DASHBOARD ':^120}{RESET}")
            print(f"{NAVY}{'='*120}{RESET}\n")

            print(f"{NAVY}{YELLOW}{'Time (CST)':<20} {now}{RESET}")
            print(f"{NAVY}{YELLOW}{'Available USDT':<20} ${usdt_free:,.6f}{RESET}")
            print(f"{NAVY}{YELLOW}{'Portfolio Value':<20} ${total_portfolio:,.6f}{RESET}")
            print(f"{NAVY}{YELLOW}{'Active Threads':<20} {len(active_threads)}{RESET}")
            print(f"{NAVY}{YELLOW}{'Trailing Buys':<20} {len(dynamic_buy_threads)}{RESET}")
            print(f"{NAVY}{YELLOW}{'Trailing Sells':<20} {len(dynamic_sell_threads)}{RESET}")
            print(f"{NAVY}{YELLOW}{'-'*120}{RESET}\n")

            with DBManager() as sess:
                db_positions = sess.query(Position).all()

            if db_positions:
                print(f"{NAVY}{BOLD}{YELLOW}{'POSITIONS IN DATABASE':^120}{RESET}")
                print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
                print(f"{NAVY}{YELLOW}{'SYMBOL':<10} {'QTY':>12} {'ENTRY':>12} {'CURRENT':>12} {'RSI':>6} {'P&L%':>8} {'PROFIT':>10} {'STATUS':<25}{RESET}")
                print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
                total_pnl = Decimal('0')
                for pos in db_positions:
                    symbol = pos.symbol
                    qty = float(pos.quantity)
                    entry = float(pos.avg_entry_price)
                    ob = get_order_book_analysis(client, symbol)
                    cur_price = float(ob['best_bid'] or ob['best_ask'])
                    rsi, _, _ = get_rsi_and_trend(client, symbol)
                    rsi_str = f"{rsi:5.1f}" if rsi else "N/A"

                    maker, taker = get_trade_fees(client, symbol)
                    gross = (cur_price - entry) * qty
                    fee_cost = (maker + taker) * cur_price * qty
                    net_profit = gross - fee_cost
                    pnl_pct = ((cur_price - entry) / entry - (maker + taker)) * 100
                    total_pnl += Decimal(str(net_profit))

                    status = ("Market Sell (15-min Stall)" if symbol in dynamic_sell_threads and getattr(dynamic_sell_threads.get(symbol), "stalled", False)
                              else "Trailing Sell Active" if symbol in dynamic_sell_threads
                              else "Trailing Buy Active" if symbol in dynamic_buy_threads
                              else "24/7 Monitoring For Sell Profit")
                    color = GREEN if net_profit > 0 else RED
                    print(f"{NAVY}{YELLOW}{symbol:<10} {qty:>12.6f} {entry:>12.6f} {cur_price:>12.6f} {rsi_str} {color}{pnl_pct:>7.2f}%{RESET}{NAVY}{YELLOW} {color}{net_profit:>10.2f}{RESET}{NAVY}{YELLOW} {status:<25}{RESET}")

                print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
                pnl_color = GREEN if total_pnl > 0 else RED
                print(f"{NAVY}{YELLOW}{'TOTAL UNREALIZED P&L':<50} {pnl_color}${float(total_pnl):>12,.2f}{RESET}\n")
            else:
                print(f"{NAVY}{YELLOW} No active positions.{RESET}\n")

            print(f"{NAVY}{'='*120}{RESET}\n")

    except Exception as e:
        logger.error(f"Dashboard error: {e}")

# === MAIN ===================================================================
def main():
    if not API_KEY or not API_SECRET:
        logger.critical("API keys missing.")
        sys.exit(1)

    client = Client(API_KEY, API_SECRET, tld='us')
    bot = BinanceTradingBot()

    if not fetch_and_validate_usdt_pairs(client):
        sys.exit(1)

    with thread_lock:
        for symbol in valid_symbols_dict.keys():
            if symbol not in active_threads:
                thread = CoinMonitorThread(client, symbol, bot)
                active_threads[symbol] = thread
                thread.start()

    logger.info("All threads running. Dashboard active.")
    last_dashboard = 0
    while True:
        try:
            bot.check_and_process_filled_orders()
            now = time.time()
            if now - last_dashboard >= 30:
                print_professional_dashboard(client)
                last_dashboard = now
            time.sleep(1)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.critical(f"Main loop error: {e}", exc_info=True)
            time.sleep(10)

if __name__ == "__main__":
    main()
