#!/usr/bin/env python3
"""
SMART TRADING BOT for Binance.US – SPOT TRADING
- Live spot trading with market/limit orders
- Order book analysis + RSI/MFI indicators
- Price/volume alerts
- Trailing buy/sell
- ASCII dashboard (no Rich)
- Full logging + WhatsApp alerts (optional)
"""

import os
import sys
import time
import logging
import numpy as np
import talib
import requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List
from collections import deque
import threading
import pytz
from logging.handlers import TimedRotatingFileHandler
from queue import Queue

from binance.client import Client
from binance.exceptions import BinanceAPIException

from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError

# === CONFIGURATION ===========================================================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    print("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET env vars")
    sys.exit(1)

# === HARD-CODED MINIMUMS ====================================================
SELL_ORDER_MINIMUM_USDT_COIN_VALUE = 5.00
BUY_ORDER_MINIMUM_USDT = 5.00
MIN_PROFIT_USDT = Decimal('0.25')
PROFIT_TARGET_NET = Decimal('0.010')
RISK_PER_TRADE = 0.10

# === INDICATOR THRESHOLDS ===================================================
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
MFI_OVERSOLD = 25
MFI_OVERBOUGHT = 75
VOLUME_SURGE_MULTIPLIER = 2.5
SHORT_TREND_WINDOW = 15

# === ORDER BOOK & TRAILING ==================================================
ORDER_BOOK_DEPTH = 50
ORDER_BOOK_IMBALANCE_BUY = 0.65
ORDER_BOOK_IMBALANCE_SELL = 0.35
TRAILING_BUY_STEP_PCT = 0.002
TRAILING_SELL_STEP_PCT = 0.002
CANCEL_AFTER_HOURS = 2.0
CANCEL_CHECK_INTERVAL = 300
POLL_INTERVAL = 2.0

# === TRAILING ACTIVATION ====================================================
MIN_DIP_FOR_TRAIL_BUY = 0.015
MIN_PROFIT_FOR_TRAIL_SELL = 0.012
MIN_RISE_FOR_TRAIL_SELL = 0.02

# === TECHNICAL INDICATORS ===================================================
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MOMENTUM_LOOKBACK_DAYS = 180
MIN_MOMENTUM_GAIN = 0.25

# === LIMIT → MARKET FALLBACK ================================================
FALLBACK_TO_MARKET_AFTER = 300
MAX_PRICE_DRIFT_PCT = 0.01
FAST_RISE_FOR_MARKET_SELL = 0.003

# === PRICE ALERTS ===========================================================
PRICE_ALERT_PCT = 0.005        # 0.5% change in 1 min
PRICE_ALERT_COOLDOWN = 30      # seconds

# === VOLUME SPIKE ALERTS ====================================================
VOLUME_SPIKE_MULTIPLIER = 3.0
VOLUME_SPIKE_COOLDOWN = 60

# === LOGGING ================================================================
LOG_FILE = "crypto_trading_bot.log"
DEBUG_LOG_FILE = "crypto_trading_bot_debug.log"

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

main_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7)
main_handler.setLevel(logging.INFO)
main_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))

debug_handler = TimedRotatingFileHandler(DEBUG_LOG_FILE, when="midnight", interval=1, backupCount=14)
debug_handler.setLevel(logging.DEBUG)
debug_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))

logger.addHandler(main_handler)
logger.addHandler(debug_handler)
logger.addHandler(console_handler)

# === REALTIME LOG QUEUE HANDLER ============================================
log_queue = Queue()

class QueueHandler(logging.Handler):
    def emit(self, record):
        try:
            log_queue.put(self.format(record))
        except Exception:
            pass

queue_handler = QueueHandler()
queue_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))

logger.addHandler(queue_handler)

CST_TZ = pytz.timezone('America/Chicago')

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

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20, 8), nullable=False)
    avg_entry_price = Column(Numeric(20, 8), nullable=False)
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

# === GLOBAL STATE ===========================================================
order_book_cache: Dict[str, dict] = {}
price_history_1m: Dict[str, deque] = {}
price_history_6mo: Dict[str, Tuple[float, float]] = {}
macd_cache: Dict[str, Tuple[float, float, float]] = {}
momentum_cache: Dict[str, bool] = {}
trailing_buy_active: Dict[str, dict] = {}
trailing_sell_active: Dict[str, dict] = {}
symbol_info_cache: Dict[str, dict] = {}
price_1h_extremes: Dict[str, Dict[str, float]] = {}
last_fill_check = 0
last_trade_timestamp = 0
last_cancel_check = 0

# === PRICE & VOLUME ALERT STATE ===
price_alert_cache: Dict[str, Dict] = {}
price_alert_flash: Optional[Tuple[str, str, float]] = None
volume_alert_cache: Dict[str, Dict] = {}
volume_alert_flash: Optional[Tuple[str, float]] = None

# === HELPERS ================================================================
def safe_float(v, default=0.0): 
    return float(v) if v and np.isfinite(float(v)) else default

def to_decimal(v): 
    return Decimal(str(v)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN) if v else Decimal('0')

def now_cst(): 
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

def format_volume(vol_usdt: float) -> str:
    """
    Returns volume with B/M/K suffix, max 12 characters:
        1.23B  (6 chars)
        12.3M  (6 chars)
        1.23K  (6 chars)
        500    (3 chars)
    Perfect for:  f"{vol_str:>12}"
    """
    vol = abs(vol_usdt)

    if vol >= 1_000_000_000:  # Billions
        return f"{vol/1_000_000_000:.2f}B"   # 1.23B → 6 chars
    if vol >= 1_000_000:      # Millions
        return f"{vol/1_000_000:.2f}M"      # 12.3M → 6 chars
    if vol >= 1_000:          # Thousands
        return f"{vol/1_000:.2f}K"          # 1.23K → 6 chars

    # < 1,000 → show in hundreds
    hundreds = int(round(vol / 100.0))
    return f"{hundreds * 100}"              # 500 → 3 chars

# === RATE MANAGER ===========================================================
class RateManager:
    def __init__(self, client):
        self.client = client
        self.current = {'weight': 0, 'orders': 0}

    def update(self):
        if not hasattr(self.client, 'response') or not self.client.response: 
            return
        hdr = self.client.response.headers
        self.current['weight'] = int(hdr.get('x-mbx-used-weight-1m', 0))
        self.current['orders'] = int(hdr.get('x-mbx-order-count-10s', 0))

    def wait(self):
        self.update()
        if self.current['weight'] > 1100: 
            time.sleep(10)
        if self.current['orders'] > 45: 
            time.sleep(5)

# === 1-HOUR HIGH/LOW CACHE ==================================================
def get_1h_high_low(bot, symbol) -> Tuple[float, float]:
    now = time.time()
    with bot.state_lock:
        cached = price_1h_extremes.get(symbol)
        if cached and now - cached['ts'] < 60:
            return cached['high'], cached['low']
    try:
        with bot.api_lock:
            bot.rate_manager.wait()
            klines = bot.client.get_klines(symbol=symbol, interval='1m', limit=60)
            bot.rate_manager.update()
        highs = [safe_float(k[2]) for k in klines]
        lows  = [safe_float(k[3]) for k in klines]
        high, low = max(highs), min(lows)
        with bot.state_lock:
            price_1h_extremes[symbol] = {'high': high, 'low': low, 'ts': now}
        return high, low
    except:
        return 0.0, 0.0

def price_change_last_n_sec(bot, symbol: str, seconds: int = 30) -> float:
    try:
        with bot.api_lock:
            bot.rate_manager.wait()
            klines = bot.client.get_klines(symbol=symbol, interval='1s', limit=seconds + 1)
            bot.rate_manager.update()
        if len(klines) < 2:
            return 0.0
        old = safe_float(klines[0][4])
        new = safe_float(klines[-1][4])
        return (new - old) / old if old > 0 else 0.0
    except Exception:
        return 0.0

# === PRICE ALERT ============================================================
def trigger_price_alert(symbol: str, old_price: float, new_price: float, bot):
    if old_price <= 0 or new_price <= 0:
        return

    pct_change = abs(new_price - old_price) / old_price
    if pct_change < PRICE_ALERT_PCT:
        return

    now = time.time()
    cache = price_alert_cache.get(symbol, {})
    last_ts = cache.get('last_alert_ts', 0)
    last_dir = cache.get('direction')

    direction = "UP" if new_price > old_price else "DOWN"
    if now - last_ts < PRICE_ALERT_COOLDOWN and last_dir == direction:
        return

    price_alert_cache[symbol] = {
        'last_price': new_price,
        'last_alert_ts': now,
        'direction': direction
    }

    print("\a")  # Bell sound
    global price_alert_flash
    price_alert_flash = (symbol, direction, pct_change)
    threading.Timer(1.0, lambda: globals().update(price_alert_flash=None)).start()

    send_whatsapp_alert(f"{direction} {symbol} {pct_change:+.2%} in 1 min!")
    logger.warning(f"PRICE ALERT: {symbol} {direction} {pct_change:+.2%}")

# === VOLUME ALERT ===========================================================
def trigger_volume_alert(symbol: str, current_vol: float, avg_vol: float, bot):
    if current_vol <= 0 or avg_vol <= 0:
        return

    ratio = current_vol / avg_vol
    if ratio < VOLUME_SPIKE_MULTIPLIER:
        return

    now = time.time()
    cache = volume_alert_cache.get(symbol, {})
    last_ts = cache.get('last_alert_ts', 0)

    if now - last_ts < VOLUME_SPIKE_COOLDOWN:
        return

    volume_alert_cache[symbol]['last_alert_ts'] = now

    print("\a")  # Bell sound
    global volume_alert_flash
    volume_alert_flash = (symbol, ratio)
    threading.Timer(1.5, lambda: globals().update(volume_alert_flash=None)).start()

    send_whatsapp_alert(f"VOLUME SPIKE {symbol} {ratio:.1f}× avg!")
    logger.warning(f"VOLUME ALERT: {symbol} {ratio:.1f}× volume spike")

# === BOT CLASS ==============================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.rate_manager = RateManager(self.client)
        self.api_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.sync_positions_from_binance()
        self.warmup_data()

    def sync_positions_from_binance(self):
        logger.info("Syncing positions...")
        try:
            with self.api_lock:
                self.rate_manager.wait()
                account = self.client.get_account()
                self.rate_manager.update()
            with DBManager() as sess:
                for bal in account['balances']:
                    asset = bal['asset']
                    qty = safe_float(bal['free'])
                    if qty <= 0 or asset == 'USDT': 
                        continue
                    sym = f"{asset}USDT"
                    if not self.is_valid_symbol(sym): 
                        continue
                    with self.api_lock:
                        self.rate_manager.wait()
                        ticker = self.client.get_symbol_ticker(symbol=sym)
                        self.rate_manager.update()
                    price = to_decimal(ticker['price'])
                    pos = sess.query(Position).filter_by(symbol=sym).one_or_none()
                    if not pos:
                        pos = Position(symbol=sym, quantity=to_decimal(qty), avg_entry_price=price)
                        sess.add(pos)
                    else:
                        pos.quantity = to_decimal(qty)
                        pos.avg_entry_price = price
                sess.commit()
        except Exception as e:
            logger.error(f"Sync failed: {e}")

    def is_valid_symbol(self, sym):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                info = self.client.get_exchange_info()
                self.rate_manager.update()
            return any(s['symbol'] == sym and s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT' for s in info['symbols'])
        except: 
            return False

    def get_valid_symbols(self):
        now = time.time()
        with self.state_lock:
            if hasattr(self, '_valid_cache') and now - self._valid_cache[1] < 300:
                return self._valid_cache[0]
        try:
            with self.api_lock:
                self.rate_manager.wait()
                info = self.client.get_exchange_info()
                self.rate_manager.update()
            syms = [s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT']
            with self.state_lock:
                self._valid_cache = (syms, now)
            return syms
        except: 
            return []

    def warmup_data(self):
        logger.info("Warming up data...")
        symbols = [p.symbol for p in self.get_db_positions()]
        for sym in symbols:
            self.warmup_24h(sym)
            self.warmup_macd(sym)
            self.warmup_6mo(sym)
        logger.info("Data ready.")

    def warmup_24h(self, sym):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                klines = self.client.get_klines(symbol=sym, interval='1m', limit=1440)
                self.rate_manager.update()
            with self.state_lock:
                if sym not in price_history_1m:
                    price_history_1m[sym] = deque(maxlen=1440)
                for k in klines:
                    price_history_1m[sym].append((float(k[0])/1000, safe_float(k[4])))
        except: 
            pass

    def warmup_macd(self, sym):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                klines = self.client.get_klines(symbol=sym, interval='1h', limit=100)
                self.rate_manager.update()
            closes = np.array([safe_float(k[4]) for k in klines])
            macd, signal, hist = talib.MACD(closes, fastperiod=MACD_FAST, slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)
            if len(macd) > 0:
                with self.state_lock:
                    macd_cache[sym] = (float(macd[-1]), float(signal[-1]), float(hist[-1]))
        except: 
            pass

    def warmup_6mo(self, sym):
        try:
            end_time = int(time.time() * 1000)
            start_time = end_time - (MOMENTUM_LOOKBACK_DAYS * 24 * 60 * 60 * 1000)
            with self.api_lock:
                self.rate_manager.wait()
                klines = self.client.get_klines(symbol=sym, interval='1d', startTime=start_time, endTime=end_time, limit=2)
                self.rate_manager.update()
            if len(klines) >= 2:
                old = safe_float(klines[0][4])
                cur = safe_float(klines[-1][4])
                if old > 0:
                    gain = (cur - old) / old
                    with self.state_lock:
                        price_history_6mo[sym] = (old, cur)
                        momentum_cache[sym] = gain >= MIN_MOMENTUM_GAIN
        except: 
            pass

    def get_db_positions(self):
        with DBManager() as sess:
            return sess.query(Position).all()

    def get_order_book_analysis(self, symbol, depth=ORDER_BOOK_DEPTH):
        now = time.time()
        with self.state_lock:
            cached = order_book_cache.get(symbol)
            if cached and now - cached['ts'] < 1.0:
                return cached

        try:
            with self.api_lock:
                self.rate_manager.wait()
                depth_data = self.client.get_order_book(symbol=symbol, limit=depth)
                self.rate_manager.update()

            bids = depth_data['bids']
            asks = depth_data['asks']

            bid_vol = sum(Decimal(b[1]) for b in bids)
            ask_vol = sum(Decimal(a[1]) for a in asks)
            total_vol = bid_vol + ask_vol
            ask_pct = float(ask_vol / total_vol) if total_vol > 0 else 0.5

            bid_wall = max(bids[:10], key=lambda x: Decimal(x[1]))[0] if bids else '0'
            ask_wall = min(asks[:10], key=lambda x: Decimal(x[1]))[0] if asks else '0'

            result = {
                'best_bid': to_decimal(bids[0][0]) if bids else Decimal('0'),
                'best_ask': to_decimal(asks[0][0]) if asks else Decimal('0'),
                'bid_vol': float(bid_vol),
                'ask_vol': float(ask_vol),
                'ask_pct': ask_pct,
                'bid_wall_price': to_decimal(bid_wall),
                'ask_wall_price': to_decimal(ask_wall),
                'imbalance': (
                    'buy_pressure' if ask_pct < ORDER_BOOK_IMBALANCE_SELL else
                    'sell_pressure' if ask_pct > ORDER_BOOK_IMBALANCE_BUY else
                    'balanced'
                ),
                'raw_bids': bids,
                'raw_asks': asks,
                'ts': now
            }
            with self.state_lock:
                order_book_cache[symbol] = result
            return result
        except Exception as e:
            logger.error(f"Order book failed {symbol}: {e}")
            return {'best_bid':0, 'best_ask':0, 'ask_pct':0.5, 'imbalance':'unknown', 'raw_bids':[], 'raw_asks':[], 'ts':now}

    def get_rsi_and_trend(self, symbol):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                klines = self.client.get_klines(symbol=symbol, interval='1m', limit=100)
                self.rate_manager.update()
            closes = np.array([safe_float(k[4]) for k in klines])
            rsi = talib.RSI(closes, timeperiod=14)
            rsi_val = float(rsi[-1]) if len(rsi) > 0 and np.isfinite(rsi[-1]) else None
            trend = "bullish" if closes[-1] > closes[-10] else "bearish"
            low_24h = min(closes[-1440:]) if len(closes) >= 1440 else closes[-1]
            return rsi_val, trend, float(low_24h)
        except: 
            return None, "unknown", None

    def get_macd(self, symbol):
        with self.state_lock:
            cached = macd_cache.get(symbol)
            if cached: 
                return cached
        try:
            with self.api_lock:
                self.rate_manager.wait()
                klines = self.client.get_klines(symbol=symbol, interval='1h', limit=100)
                self.rate_manager.update()
            closes = np.array([safe_float(k[4]) for k in klines])
            macd, signal, hist = talib.MACD(closes, fastperiod=MACD_FAST, slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)
            if len(macd) > 0:
                result = (float(macd[-1]), float(signal[-1]), float(hist[-1]))
                with self.state_lock: 
                    macd_cache[symbol] = result
                return result
        except: 
            pass
        return 0.0, 0.0, 0.0

    def get_mfi(self, symbol, period=14, length=100):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                klines = self.client.get_klines(symbol=symbol, interval='1m', limit=length)
                self.rate_manager.update()
            typical = [(float(k[2]) + float(k[3]) + float(k[4])) / 3 for k in klines]
            volume = [float(k[5]) for k in klines]
            money_flow = [typical[i] * volume[i] for i in range(len(typical))]
            pos = sum(mf for i, mf in enumerate(money_flow[1:]) if typical[i+1] > typical[i] and i >= length-period-1)
            neg = sum(mf for i, mf in enumerate(money_flow[1:]) if typical[i+1] < typical[i] and i >= length-period-1)
            if neg == 0: 
                return 100.0
            mfr = pos / neg
            return 100 - (100 / (1 + mfr))
        except: 
            return None

    def get_volume_surge(self, symbol):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                klines = self.client.get_klines(symbol=symbol, interval='1m', limit=21)
                self.rate_manager.update()
            volumes = [float(k[5]) for k in klines]
            return volumes[-1] > np.mean(volumes[:-1]) * VOLUME_SURGE_MULTIPLIER
        except: 
            return False

    def get_short_term_trend(self, symbol, window=SHORT_TREND_WINDOW):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                klines = self.client.get_klines(symbol=symbol, interval='1m', limit=window+1)
                self.rate_manager.update()
            prices = [float(k[4]) for k in klines]
            return "bullish" if prices[-1] > prices[0] else "bearish"
        except: 
            return "unknown"

    def get_trade_fees(self, symbol):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                fee = self.client.get_trade_fee(symbol=symbol)
                self.rate_manager.update()
            return safe_float(fee[0]['makerCommission']), safe_float(fee[0]['takerCommission'])
        except: 
            return 0.001, 0.001

    def get_balance(self, asset='USDT'):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                bal = self.client.get_asset_balance(asset=asset)
                self.rate_manager.update()
            return to_decimal(bal['free'])
        except: 
            return Decimal('0')

    def calculate_total_portfolio_value(self):
        total = Decimal('0')
        usdt = self.get_balance('USDT')
        with DBManager() as sess:
            for pos in sess.query(Position).all():
                ob = self.get_order_book_analysis(pos.symbol)
                price = to_decimal(ob['best_bid'] or ob['best_ask'])
                total += price * pos.quantity
        return total + usdt, usdt

    def place_market_buy(self, symbol, usdt_amount):
        if usdt_amount < BUY_ORDER_MINIMUM_USDT:
            logger.info(f"BUY SKIPPED {symbol}: Only ${usdt_amount:.2f} < ${BUY_ORDER_MINIMUM_USDT}")
            return None
        try:
            with self.api_lock:
                self.rate_manager.wait()
                order = self.client.order_market_buy(symbol=symbol, quoteOrderQty=float(usdt_amount))
                self.rate_manager.update()
            logger.info(f"BUY {symbol} ${usdt_amount:.2f}")
            send_whatsapp_alert(f"BUY {symbol} ${usdt_amount:.2f}")
            return order
        except Exception as e:
            logger.error(f"BUY FAILED {symbol}: {e}")
            return None

    def place_market_sell(self, symbol, qty):
        ob = self.get_order_book_analysis(symbol)
        price = to_decimal(ob['best_ask'] or ob['best_bid'])
        if price <= 0: 
            return None

        value_usdt = price * qty
        if value_usdt < SELL_ORDER_MINIMUM_USDT_COIN_VALUE:
            logger.info(f"SELL SKIPPED {symbol}: Only ${value_usdt:.2f} < ${SELL_ORDER_MINIMUM_USDT_COIN_VALUE}")
            return None
        if qty < 1:
            logger.info(f"SELL SKIPPED {symbol}: Qty {qty:.6f} < 1")
            return None

        with DBManager() as sess:
            pos = sess.query(Position).filter_by(symbol=symbol).first()
            if not pos: 
                return None
            entry = to_decimal(pos.avg_entry_price)
            gross_profit = (price - entry) * qty
            maker, taker = self.get_trade_fees(symbol)
            fee_cost = (maker + taker) * price * qty
            net_profit = gross_profit - fee_cost
            if net_profit < MIN_PROFIT_USDT:
                logger.info(f"SELL SKIPPED {symbol}: Net profit ${net_profit:.2f} < ${MIN_PROFIT_USDT}")
                return None

        try:
            with self.api_lock:
                self.rate_manager.wait()
                order = self.client.order_market_sell(symbol=symbol, quantity=float(qty))
                self.rate_manager.update()
            logger.info(f"SELL {symbol} {qty:.6f} @ {price} (Net ${net_profit:.2f})")
            send_whatsapp_alert(f"SELL {symbol} {qty:.6f} @ {price}")
            return order
        except Exception as e:
            logger.error(f"SELL FAILED {symbol}: {e}")
            return None

    def check_fills_and_update_db(self):
        global last_fill_check, last_trade_timestamp
        if time.time() - last_fill_check < 5: 
            return
        last_fill_check = time.time()

        try:
            with self.api_lock:
                self.rate_manager.wait()
                open_orders = self.client.get_open_orders()
                self.rate_manager.update()

            with DBManager() as sess:
                symbols = [p.symbol for p in sess.query(Position.symbol).distinct()]

            for sym in symbols:
                try:
                    with self.api_lock:
                        self.rate_manager.wait()
                        trades = self.client.get_my_trades(symbol=sym, limit=100)
                        self.rate_manager.update()
                    for t in trades:
                        ts = t['time']
                        if ts <= last_trade_timestamp: 
                            continue
                        order_id = str(t['orderId'])
                        if sess.query(Trade).filter_by(binance_order_id=order_id).first(): 
                            continue

                        price = to_decimal(t['price'])
                        qty = to_decimal(t['qty'])
                        side = 'BUY' if t['isBuyer'] else 'SELL'

                        sess.add(Trade(symbol=sym, side=side, price=price, quantity=qty, binance_order_id=order_id))
                        pos = sess.query(Position).filter_by(symbol=sym).one_or_none()
                        if side == 'BUY':
                            if not pos:
                                pos = Position(symbol=sym, quantity=qty, avg_entry_price=price)
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
                        last_trade_timestamp = max(last_trade_timestamp, ts)
                        logger.info(f"FILLED {side} {sym} {qty} @ {price}")
                except Exception as e:
                    logger.debug(f"Trade check {sym}: {e}")
        except Exception as e:
            logger.debug(f"Fill check error: {e}")

    def cancel_old_orders(self):
        global last_cancel_check
        if time.time() - last_cancel_check < CANCEL_CHECK_INTERVAL: 
            return
        last_cancel_check = time.time()

        cutoff = int((time.time() - CANCEL_AFTER_HOURS * 3600) * 1000)
        logger.info(f"Checking for orders older than {CANCEL_AFTER_HOURS}h...")

        try:
            with self.api_lock:
                self.rate_manager.wait()
                open_orders = self.client.get_open_orders()
                self.rate_manager.update()

            canceled = 0
            for order in open_orders:
                if order['time'] >= cutoff: 
                    continue
                sym = order['symbol']
                order_id = order['orderId']
                try:
                    with self.api_lock:
                        self.rate_manager.wait()
                        self.client.cancel_order(symbol=sym, orderId=order_id)
                        self.rate_manager.update()
                    logger.warning(f"CANCELED OLD ORDER: {sym} ID:{order_id}")
                    send_whatsapp_alert(f"CANCELED {sym} ID:{order_id} (>2h)")
                    canceled += 1
                    with self.state_lock:
                        if sym in trailing_buy_active: 
                            del trailing_buy_active[sym]
                        if sym in trailing_sell_active: 
                            del trailing_sell_active[sym]
                except Exception as e:
                    logger.error(f"Cancel failed {sym} {order_id}: {e}")
            if canceled:
                logger.info(f"Canceled {canceled} old order(s).")
        except Exception as e:
            logger.error(f"Cancel check error: {e}")

    def check_unfilled_limit_orders(self):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                open_orders = self.client.get_open_orders()
                self.rate_manager.update()
            now_ms = int(time.time() * 1000)

            for order in open_orders:
                sym = order['symbol']
                order_id = order['orderId']
                order_type = order['type']
                side = order['side']
                order_time = order['time']
                qty = Decimal(order['origQty'])
                filled = Decimal(order['executedQty'])
                remaining = qty - filled

                if order_type != 'LIMIT' or filled > 0:
                    continue

                age_sec = (now_ms - order_time) / 1000
                if age_sec < FALLBACK_TO_MARKET_AFTER:
                    continue

                try:
                    with self.api_lock:
                        self.rate_manager.wait()
                        self.client.cancel_order(symbol=sym, orderId=order_id)
                        self.rate_manager.update()
                except Exception as e:
                    logger.error(f"Cancel stale {sym} {order_id}: {e}")
                    continue

                logger.warning(f"[{sym}] Stale LIMIT {side} ID:{order_id} ({age_sec:.0f}s) → cancelled")

                limit_price = Decimal(order['price'])
                ob = self.get_order_book_analysis(sym)
                last_price = to_decimal(ob['best_bid'] if side == 'BUY' else ob['best_ask'])
                if last_price <= 0:
                    logger.warning(f"[{sym}] No valid market price → skip fallback")
                    continue

                drift = abs((last_price - limit_price) / limit_price)
                if drift > MAX_PRICE_DRIFT_PCT:
                    logger.info(f"[{sym}] Price drift {drift:.2%} > {MAX_PRICE_DRIFT_PCT:.0%} → skip market")
                    continue

                if side == 'SELL':
                    rise = price_change_last_n_sec(self, sym, seconds=30)
                    if rise > FAST_RISE_FOR_MARKET_SELL:
                        logger.info(f"[{sym}] Fast rise {rise:.2%} → immediate market SELL")
                        self.place_market_sell(sym, float(remaining))
                        continue

                if remaining <= 0:
                    continue

                market_side = 'BUY' if side == 'BUY' else 'SELL'
                try:
                    with self.api_lock:
                        self.rate_manager.wait()
                        market_order = self.client.create_order(
                            symbol=sym,
                            side=market_side,
                            type='MARKET',
                            quantity=float(remaining)
                        )
                        self.rate_manager.update()

                    logger.info(f"[{sym}] MARKET {market_side} {remaining} (limit → market fallback)")
                    send_whatsapp_alert(f"{sym}: LIMIT stale → MARKET {market_side} {remaining}")

                except Exception as e:
                    logger.error(f"Market fallback failed {sym} {market_side}: {e}")

        except Exception as e:
            logger.error(f"check_unfilled_limit_orders error: {e}")

def get_symbol_info(bot, symbol):
    with bot.state_lock:
        if symbol in symbol_info_cache:
            return symbol_info_cache[symbol]
    try:
        with bot.api_lock:
            bot.rate_manager.wait()
            info = bot.client.get_symbol_info(symbol)
            bot.rate_manager.update()
        with bot.state_lock:
            symbol_info_cache[symbol] = info
        return info
    except Exception as e:
        logger.error(f"Failed to get symbol info {symbol}: {e}")
        return None

def round_price(price: Decimal, symbol: str, bot) -> Decimal:
    info = get_symbol_info(bot, symbol)
    if not info: 
        return price
    try:
        tick_size = Decimal(info['filters'][1]['tickSize'])
        return (price // tick_size) * tick_size
    except: 
        return price

def round_quantity(qty: Decimal, symbol: str, bot) -> Decimal:
    info = get_symbol_info(bot, symbol)
    if not info: 
        return qty
    try:
        step_size = Decimal(info['filters'][2]['stepSize'])
        return (qty // step_size) * step_size
    except: 
        return qty

def smart_buy_scanner(bot):
    while True:
        try:
            usdt = float(bot.get_balance('USDT'))
            if usdt < BUY_ORDER_MINIMUM_USDT * 3:
                time.sleep(10)
                continue

            for sym in bot.get_valid_symbols():
                if sym in trailing_buy_active or any(p.symbol == sym for p in bot.get_db_positions()):
                    continue

                ob = bot.get_order_book_analysis(sym)
                if ob['best_bid'] <= 0: 
                    continue

                current = to_decimal(ob['best_bid'])
                rsi, _, _ = bot.get_rsi_and_trend(sym)
                mfi = bot.get_mfi(sym)
                volume_surge = bot.get_volume_surge(sym)
                trend = bot.get_short_term_trend(sym)
                macd, signal, _ = bot.get_macd(sym)

                high_1h_float, _ = get_1h_high_low(bot, sym)
                high_1h = to_decimal(high_1h_float)
                if high_1h <= 0: 
                    continue
                dip_pct = float((high_1h - current) / high_1h)

                conditions = [
                    rsi and rsi <= RSI_OVERSOLD,
                    mfi and mfi <= MFI_OVERSOLD,
                    ob['imbalance'] == 'sell_pressure',
                    volume_surge,
                    trend == "bearish",
                    macd < signal,
                    dip_pct >= MIN_DIP_FOR_TRAIL_BUY
                ]

                if all(conditions):
                    with bot.state_lock:
                        trailing_buy_active[sym] = {'last_price': current}
                    logger.info(f"BUY SIGNAL {sym} | RSI:{rsi:.1f} MFI:{mfi:.1f} Dip:{dip_pct:.1%}")
                    send_whatsapp_alert(f"BUY {sym} @ {current}")

            time.sleep(5)
        except Exception as e:
            logger.error(f"Smart buy error: {e}", exc_info=True)
            time.sleep(10)

def smart_sell_scanner(bot):
    while True:
        try:
            for pos in bot.get_db_positions():
                sym = pos.symbol
                if sym in trailing_sell_active: 
                    continue

                ob = bot.get_order_book_analysis(sym)
                if ob['best_ask'] <= 0: 
                    continue

                entry = to_decimal(pos.avg_entry_price)
                current = to_decimal(ob['best_ask'])
                if entry <= 0: 
                    continue
                profit_pct = float((current - entry) / entry)

                rsi, _, _ = bot.get_rsi_and_trend(sym)
                mfi = bot.get_mfi(sym)
                volume_surge = bot.get_volume_surge(sym)
                trend = bot.get_short_term_trend(sym)
                macd, signal, _ = bot.get_macd(sym)

                _, low_1h_float = get_1h_high_low(bot, sym)
                low_1h = to_decimal(low_1h_float)
                if low_1h <= 0: 
                    continue
                rise_pct = float((current - low_1h) / low_1h)

                conditions = [
                    rsi and rsi >= RSI_OVERBOUGHT,
                    mfi and mfi >= MFI_OVERBOUGHT,
                    ob['imbalance'] == 'buy_pressure',
                    volume_surge,
                    trend == "bullish",
                    macd > signal,
                    profit_pct >= MIN_PROFIT_FOR_TRAIL_SELL,
                    rise_pct >= MIN_RISE_FOR_TRAIL_SELL
                ]

                if all(conditions):
                    with bot.state_lock:
                        trailing_sell_active[sym] = {'last_price': current}
                    logger.info(f"SELL SIGNAL {sym} | +{profit_pct:.2%} RSI:{rsi:.1f} MFI:{mfi:.1f}")
                    send_whatsapp_alert(f"SELL {sym} @ {current}")

            time.sleep(5)
        except Exception as e:
            logger.error(f"Smart sell error: {e}", exc_info=True)
            time.sleep(10)

def trailing_buy_scanner(bot):
    while True:
        try:
            for sym, data in list(trailing_buy_active.items()):
                ob = bot.get_order_book_analysis(sym)
                best_bid = ob['best_bid']
                if best_bid <= 0: 
                    continue

                last_price = Decimal(str(data.get('last_price', best_bid)))
                target_price = best_bid * Decimal(str(1 - TRAILING_BUY_STEP_PCT))

                if target_price >= last_price: 
                    continue

                old_id = data.get('order_id')
                if old_id:
                    try:
                        with bot.api_lock:
                            bot.rate_manager.wait()
                            bot.client.cancel_order(symbol=sym, orderId=old_id)
                            bot.rate_manager.update()
                    except: 
                        pass

                usdt = float(bot.get_balance('USDT')) * RISK_PER_TRADE
                if usdt < BUY_ORDER_MINIMUM_USDT: 
                    continue

                target_price_rounded = round_price(target_price, sym, bot)
                if target_price_rounded <= 0: 
                    continue

                raw_qty = Decimal(str(usdt)) / target_price_rounded
                qty = round_quantity(raw_qty, sym, bot)
                if qty <= 0: 
                    continue

                try:
                    with bot.api_lock:
                        bot.rate_manager.wait()
                        order = bot.client.order_limit_buy(
                            symbol=sym,
                            quantity=float(qty),
                            price=float(target_price_rounded)
                        )
                        bot.rate_manager.update()
                    with bot.state_lock:
                        trailing_buy_active[sym] = {
                            'order_id': order['orderId'],
                            'last_price': target_price_rounded
                        }
                    logger.info(f"TRAILING BUY {sym} {qty} @ {target_price_rounded}")
                except BinanceAPIException as e:
                    if e.code == -1013:
                        logger.warning(f"BUY PRICE_FILTER {sym}, skipping")
                    else:
                        logger.error(f"Trailing buy failed {sym}: {e}")
                except Exception as e:
                    logger.error(f"Trailing buy failed {sym}: {e}")

            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.critical(f"Trailing buy crash: {e}", exc_info=True)
            time.sleep(10)

def trailing_sell_scanner(bot):
    while True:
        try:
            for sym, data in list(trailing_sell_active.items()):
                ob = bot.get_order_book_analysis(sym)
                best_ask = ob['best_ask']
                if best_ask <= 0: 
                    continue

                last_price = Decimal(str(data.get('last_price', best_ask)))
                target_price = best_ask * Decimal(str(1 + TRAILING_SELL_STEP_PCT))

                if target_price <= last_price: 
                    continue

                old_id = data.get('order_id')
                if old_id:
                    try:
                        with bot.api_lock:
                            bot.rate_manager.wait()
                            bot.client.cancel_order(symbol=sym, orderId=old_id)
                            bot.rate_manager.update()
                    except: 
                        pass

                with DBManager() as sess:
                    pos = sess.query(Position).filter_by(symbol=sym).first()
                    if not pos or pos.quantity <= 0:
                        with bot.state_lock:
                            if sym in trailing_sell_active: 
                                del trailing_sell_active[sym]
                        continue
                    raw_qty = Decimal(str(pos.quantity))

                qty = round_quantity(raw_qty, sym, bot)
                if qty <= 0 or qty < 1:
                    continue

                value = best_ask * qty
                if value < SELL_ORDER_MINIMUM_USDT_COIN_VALUE:
                    continue

                target_price_rounded = round_price(target_price, sym, bot)
                if target_price_rounded <= 0: 
                    continue

                try:
                    with bot.api_lock:
                        bot.rate_manager.wait()
                        order = bot.client.order_limit_sell(
                            symbol=sym,
                            quantity=float(qty),
                            price=float(target_price_rounded)
                        )
                        bot.rate_manager.update()
                    with bot.state_lock:
                        trailing_sell_active[sym] = {
                            'order_id': order['orderId'],
                            'last_price': target_price_rounded
                        }
                    logger.info(f"TRAILING SELL {sym} {qty} @ {target_price_rounded}")
                except BinanceAPIException as e:
                    if e.code == -1013:
                        logger.warning(f"SELL PRICE_FILTER {sym}, using market")
                        bot.place_market_sell(sym, float(qty))
                        with bot.state_lock:
                            if sym in trailing_sell_active: 
                                del trailing_sell_active[sym]
                    else:
                        logger.error(f"Trailing sell failed {sym}: {e}")
                except Exception as e:
                    logger.error(f"Trailing sell failed {sym}: {e}")

            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.critical(f"Trailing sell crash: {e}", exc_info=True)
            time.sleep(10)

def send_whatsapp_alert(msg):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}", timeout=5)
        except: 
            pass

# === ASCII DASHBOARD ========================================================
def update_ascii_dashboard(bot):
    global price_alert_flash, volume_alert_flash

    os.system('clear' if os.name == 'posix' else 'cls')

    # Header
    print("═" * 80)
    print(" SMART COIN TRADING BOT ".center(80))
    print("═" * 80)
    print()

    now = now_cst()
    usdt_free = float(bot.get_balance('USDT'))
    total_portfolio, _ = bot.calculate_total_portfolio_value()
    total_portfolio = float(total_portfolio)

    print(f"Time (CST): {now}")
    print(f"Available USDT: ${usdt_free:,.6f}")
    print(f"Portfolio Value: ${total_portfolio:,.6f}")
    print(f"Trailing Buys: {len(trailing_buy_active)} | Trailing Sells: {len(trailing_sell_active)}")
    print()

    # Alert Bars
    if price_alert_flash:
        sym, direction, pct = price_alert_flash
        color = "🟢" if direction == "UP" else "🔴"
        print(f" {color} {direction} {sym} {pct:+.2%} ".center(80, "█"))
    else:
        print(" " * 80)

    if volume_alert_flash:
        sym, ratio = volume_alert_flash
        print(f" 📈 VOLUME {sym} {ratio:.1f}× ".center(80, "█"))
    else:
        print(" " * 80)
    print()

    # Positions Table
    print("─" * 160)
    print(f"{'SYMBOL':<10} {'QTY':>12} {'ENTRY':>12} {'CURRENT':>12} {'RSI':>6} {'MFI':>6} {'P&L%':>10} {'PROFIT $':>12} {'VOLUME':>12} {'STATUS':<25}")
    print("─" * 160)

    total_pnl = 0.0
    with DBManager() as sess:
        db_positions = sess.query(Position).all()

    for pos in db_positions:
        sym = pos.symbol
        qty = float(pos.quantity)
        entry = float(pos.avg_entry_price)

        ob = bot.get_order_book_analysis(sym)
        cur_price = float(ob['best_bid'] or ob['best_ask'])
        rsi, _, _ = bot.get_rsi_and_trend(sym)
        mfi = bot.get_mfi(sym)

        # Volume
        try:
            klines = bot.client.get_klines(symbol=sym, interval='1m', limit=21)
            volumes = [float(k[5]) for k in klines]
            current_vol = volumes[-1]
        except:
            current_vol = 0.0

        # P&L
        maker, taker = bot.get_trade_fees(sym)
        gross = (cur_price - entry) * qty
        fee_cost = (maker + taker) * cur_price * qty
        net_profit = gross - fee_cost
        pnl_pct = ((cur_price - entry) / entry - (maker + taker)) * 100 if entry > 0 else 0
        total_pnl += net_profit

        vol_str = format_volume(current_vol)
        pnl_style = "🟢" if net_profit > 0 else "🔴"

        status = (
            "Trailing Sell Active" if sym in trailing_sell_active else
            "Trailing Buy Active" if sym in trailing_buy_active else
            "Monitoring"
        )

        print(f"{sym:<10} {qty:>12.6f} {entry:>12.6f} {cur_price:>12.6f} {rsi:>6.1f} {mfi:>6.1f} {pnl_pct:>10.2f}% {pnl_style} {net_profit:>12.2f} {vol_str:>12} {status:<25}")

        # Dynamic Order Book Panel
        if sym in trailing_buy_active or sym in trailing_sell_active:
            thread = "BUY THREAD" if sym in trailing_buy_active else "SELL THREAD"
            panel_lines = _make_orderbook_panel_ascii(sym, bot, thread)
            for line in panel_lines:
                print(line)
            print()

    # Total P&L
    pnl_style = "🟢" if total_pnl > 0 else "🔴"
    print("─" * 160)
    print(f"{'TOTAL NET P&L':<10} {'':>12} {'':>12} {'':>12} {'':>6} {'':>6} {'':>10} {'':>12} {pnl_style} {total_pnl:>12.2f} {'':>12} {'':<25}")
    print()

    # Log Panel
    print(" LOGS (Last 15 lines) ".center(80, "─"))
    log_lines = []
    while not log_queue.empty() and len(log_lines) < 15:
        try:
            log_lines.append(log_queue.get_nowait())
        except:
            break
    for line in log_lines[-15:]:
        print(f" {line.rstrip()}")
    print()

def _make_orderbook_panel_ascii(symbol: str, bot, thread_type: str) -> List[str]:
    ob = bot.get_order_book_analysis(symbol)
    raw_bids = ob.get('raw_bids', [])[:5]
    raw_asks = ob.get('raw_asks', [])[:5]
    ask_pct = ob['ask_pct']
    bid_pct = 1.0 - ask_pct

    lines = []

    # Title
    lines.append(f" {symbol} Order Book ({thread_type}) ".center(78, "─"))

    # Bar
    bar_len = 28
    buy_bar = "█" * int(bar_len * bid_pct)
    sell_bar = "█" * int(bar_len * ask_pct)
    lines.append(f" BUY {buy_bar:<28} {bid_pct:>6.1%} | {ask_pct:<6.1%} {sell_bar:>28} SELL")

    # Ladder Header
    lines.append("  PRICE      QTY          VOL BAR          VOL BAR      QTY       PRICE")
    lines.append("─" * 78)

    max_vol = max(
        max(Decimal(b[1]) for b in raw_bids) if raw_bids else Decimal('0'),
        max(Decimal(a[1]) for a in raw_asks) if raw_asks else Decimal('0')
    ) or Decimal('1')

    for i in range(5):
        b_price = b_qty = b_bar = ""
        a_price = a_qty = a_bar = ""

        if i < len(raw_bids):
            b_price = f"{float(raw_bids[i][0]):.6f}"
            b_qty = f"{float(raw_bids[i][1]):.6f}"
            b_bar = "█" * int(28 * Decimal(raw_bids[i][1]) / max_vol)

        if i < len(raw_asks):
            a_price = f"{float(raw_asks[i][0]):.6f}"
            a_qty = f"{float(raw_asks[i][1]):.6f}"
            a_bar = "█" * int(28 * Decimal(raw_asks[i][1]) / max_vol)

        lines.append(f" {b_price:>10} {b_qty:>8} {b_bar:<28} {a_bar:>28} {a_qty:>8} {a_price:<10}")

    lines.append("─" * 78)
    return lines

# === MAIN ===================================================================
def main():
    bot = BinanceTradingBot()

    threading.Thread(target=smart_buy_scanner, args=(bot,), daemon=True).start()
    threading.Thread(target=smart_sell_scanner, args=(bot,), daemon=True).start()
    threading.Thread(target=trailing_buy_scanner, args=(bot,), daemon=True).start()
    threading.Thread(target=trailing_sell_scanner, args=(bot,), daemon=True).start()

    last_dash = time.time()

    while True:
        bot.check_fills_and_update_db()
        bot.cancel_old_orders()
        bot.check_unfilled_limit_orders()

        if time.time() - last_dash >= 8:
            update_ascii_dashboard(bot)
            last_dash = time.time()

        time.sleep(1)

if __name__ == "__main__":
    main()
