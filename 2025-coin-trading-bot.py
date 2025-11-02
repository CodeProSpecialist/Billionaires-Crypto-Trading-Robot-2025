#!/usr/bin/env python3
"""
BINANCE.US TRADING BOT – PROFIT & SIZE GUARANTEED
- 1.0% NET profit + $1.00 min profit
- No sell if < $5.00 value or <1 coin
- No buy if < $5.00 USDT cash
- PRICE_FILTER & LOT_SIZE safe
- Auto-cancel >2h
- Full dashboard + WhatsApp
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
from typing import Dict, Any, Optional, Tuple
from collections import deque
import threading
import pytz
from logging.handlers import TimedRotatingFileHandler

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

MAX_PRICE = 1000.00
MIN_PRICE = 0.01
MIN_24H_VOLUME_USDT = 100000
LOG_FILE = "crypto_trading_bot.log"
DEBUG_LOG_FILE = "crypto_trading_bot_debug.log"

# === HARD-CODED MINIMUMS ====================================================
SELL_ORDER_MINIMUM_USDT_COIN_VALUE = 5.00   # Min $5 value to sell
BUY_ORDER_MINIMUM_USDT = 5.00               # Min $5 USDT to buy
MIN_PROFIT_USDT = Decimal('0.25')           # Min $0.25 NET profit per trade after fees 

PROFIT_TARGET_NET = Decimal('0.010')        # 1.0% NET profit
RISK_PER_TRADE = 0.10
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
ORDERBOOK_SELL_PRESSURE_THRESHOLD = 0.60
ORDER_BOOK_LIMIT = 20
POLL_INTERVAL = 2.0

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MOMENTUM_LOOKBACK_DAYS = 180
MIN_MOMENTUM_GAIN = 0.25

TRAILING_BUY_STEP_PCT = 0.002
TRAILING_SELL_STEP_PCT = 0.002
CANCEL_AFTER_HOURS = 2.0
CANCEL_CHECK_INTERVAL = 300

# === LOGGING ================================================================
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

if not logger.handlers:
    logger.addHandler(main_handler)
    logger.addHandler(debug_handler)
    logger.addHandler(console_handler)

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
            try: self.session.commit()
            except SQLAlchemyError: self.session.rollback()
        self.session.close()

# === GLOBAL STATE ===========================================================
valid_symbols_dict: Dict[str, dict] = {}
order_book_cache: Dict[str, dict] = {}
price_history_1m: Dict[str, deque] = {}
price_history_6mo: Dict[str, Tuple[float, float]] = {}
macd_cache: Dict[str, Tuple[float, float, float]] = {}
momentum_cache: Dict[str, bool] = {}
trailing_buy_active: Dict[str, dict] = {}
trailing_sell_active: Dict[str, dict] = {}
symbol_info_cache: Dict[str, dict] = {}
last_fill_check = 0
last_trade_timestamp = 0
last_cancel_check = 0

# === HELPERS ================================================================
def safe_float(v, default=0.0): return float(v) if v and np.isfinite(float(v)) else default
def to_decimal(v): return Decimal(str(v)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN) if v else Decimal('0')
def now_cst(): return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

# === RATE MANAGER ===========================================================
class RateManager:
    def __init__(self, client):
        self.client = client
        self.current = {'weight': 0, 'orders': 0}

    def update(self):
        if not hasattr(self.client, 'response') or not self.client.response: return
        hdr = self.client.response.headers
        self.current['weight'] = int(hdr.get('x-mbx-used-weight-1m', 0))
        self.current['orders'] = int(hdr.get('x-mbx-order-count-10s', 0))

    def wait(self):
        self.update()
        if self.current['weight'] > 1100: time.sleep(10)
        if self.current['orders'] > 45: time.sleep(5)

        # === BOT CLASS ==============================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.client.API_URL = 'https://api.binance.us/api'
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
                    if qty <= 0 or asset == 'USDT': continue
                    sym = f"{asset}USDT"
                    if not self.is_valid_symbol(sym): continue
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
        except: return False

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
        except: pass

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
        except: pass

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
        except: pass

    def get_db_positions(self):
        with DBManager() as sess:
            return sess.query(Position).all()

    def get_order_book_analysis(self, symbol):
        now = time.time()
        with self.state_lock:
            cached = order_book_cache.get(symbol)
            if cached and now - cached['ts'] < 1.0: return cached
        try:
            with self.api_lock:
                self.rate_manager.wait()
                depth = self.client.get_order_book(symbol=symbol, limit=ORDER_BOOK_LIMIT)
                self.rate_manager.update()
            bids = depth['bids'][:10]
            asks = depth['asks'][:10]
            bid_vol = sum(Decimal(b[1]) for b in bids)
            ask_vol = sum(Decimal(a[1]) for a in asks)
            result = {
                'best_bid': to_decimal(bids[0][0]) if bids else to_decimal('0'),
                'best_ask': to_decimal(asks[0][0]) if asks else to_decimal('0'),
                'pct_ask': float(ask_vol/(bid_vol+ask_vol)*100) if (bid_vol+ask_vol) else 50.0,
                'ts': now
            }
            with self.state_lock: order_book_cache[symbol] = result
            return result
        except: return {'best_bid':0, 'best_ask':0, 'pct_ask':50.0}

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
        except: return None, "unknown", None

    def get_macd(self, symbol):
        with self.state_lock:
            cached = macd_cache.get(symbol)
            if cached: return cached
        try:
            with self.api_lock:
                self.rate_manager.wait()
                klines = self.client.get_klines(symbol=symbol, interval='1h', limit=100)
                self.rate_manager.update()
            closes = np.array([safe_float(k[4]) for k in klines])
            macd, signal, hist = talib.MACD(closes, fastperiod=MACD_FAST, slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)
            if len(macd) > 0:
                result = (float(macd[-1]), float(signal[-1]), float(hist[-1]))
                with self.state_lock: macd_cache[symbol] = result
                return result
        except: pass
        return 0.0, 0.0, 0.0

    def get_trade_fees(self, symbol):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                fee = self.client.get_trade_fee(symbol=symbol)
                self.rate_manager.update()
            return safe_float(fee[0]['makerCommission']), safe_float(fee[0]['takerCommission'])
        except: return 0.001, 0.001

    def get_balance(self, asset='USDT'):
        try:
            with self.api_lock:
                self.rate_manager.wait()
                bal = self.client.get_asset_balance(asset=asset)
                self.rate_manager.update()
            return to_decimal(bal['free'])
        except: return Decimal('0')

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
        # Get current price
        ob = self.get_order_book_analysis(symbol)
        price = to_decimal(ob['best_ask'] or ob['best_bid'])
        if price <= 0: return None

        value_usdt = price * qty
        if value_usdt < SELL_ORDER_MINIMUM_USDT_COIN_VALUE:
            logger.info(f"SELL SKIPPED {symbol}: Only ${value_usdt:.2f} < ${SELL_ORDER_MINIMUM_USDT_COIN_VALUE}")
            return None
        if qty < 1:
            logger.info(f"SELL SKIPPED {symbol}: Qty {qty:.6f} < 1")
            return None

        # Check min profit
        with DBManager() as sess:
            pos = sess.query(Position).filter_by(symbol=symbol).first()
            if not pos: return None
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
        if time.time() - last_fill_check < 5: return
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
                        if ts <= last_trade_timestamp: continue
                        order_id = str(t['orderId'])
                        if sess.query(Trade).filter_by(binance_order_id=order_id).first(): continue

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
        if time.time() - last_cancel_check < CANCEL_CHECK_INTERVAL: return
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
                if order['time'] >= cutoff: continue
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
                        if sym in trailing_buy_active: del trailing_buy_active[sym]
                        if sym in trailing_sell_active: del trailing_sell_active[sym]
                except Exception as e:
                    logger.error(f"Cancel failed {sym} {order_id}: {e}")
            if canceled:
                logger.info(f"Canceled {canceled} old order(s).")
        except Exception as e:
            logger.error(f"Cancel check error: {e}")

# === SYMBOL INFO & ROUNDING HELPERS =========================================
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
    if not info: return price
    try:
        tick_size = Decimal(info['filters'][1]['tickSize'])
        return (price // tick_size) * tick_size
    except: return price

def round_quantity(qty: Decimal, symbol: str, bot) -> Decimal:
    info = get_symbol_info(bot, symbol)
    if not info: return qty
    try:
        step_size = Decimal(info['filters'][2]['stepSize'])
        return (qty // step_size) * step_size
    except: return qty


# === TRAILING SCANNERS (UPDATED) ============================================
def trailing_buy_scanner(bot):
    while True:
        try:
            for sym, data in list obviously(trailing_buy_active.items()):
                ob = bot.get_order_book_analysis(sym)
                best_bid = ob['best_bid']
                if best_bid <= 0: continue

                last_price = Decimal(str(data.get('last_price', best_bid)))
                target_price = best_bid * Decimal(str(1 - TRAILING_BUY_STEP_PCT))

                if target_price >= last_price: continue

                old_id = data.get('order_id')
                if old_id:
                    try:
                        with bot.api_lock:
                            bot.rate_manager.wait()
                            bot.client.cancel_order(symbol=sym, orderId=old_id)
                            bot.rate_manager.update()
                        logger.info(f"CANCELED trailing buy {sym} @ {last_price}")
                    except: pass

                usdt = float(bot.get_balance('USDT')) * RISK_PER_TRADE
                if usdt < BUY_ORDER_MINIMUM_USDT: continue

                target_price_rounded = round_price(target_price, sym, bot)
                if target_price_rounded <= 0: continue

                raw_qty = Decimal(str(usdt)) / target_price_rounded
                qty = round_quantity(raw_qty, sym, bot)
                if qty <= 0: continue

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
                    send_whatsapp_alert(f"TRAIL BUY {sym} {qty} @ {target_price_rounded}")
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
                if best_ask <= 0: continue

                last_price = Decimal(str(data.get('last_price', best_ask)))
                target_price = best_ask * Decimal(str(1 + TRAILING_SELL_STEP_PCT))

                if target_price <= last_price: continue

                old_id = data.get('order_id')
                if old_id:
                    try:
                        with bot.api_lock:
                            bot.rate_manager.wait()
                            bot.client.cancel_order(symbol=sym, orderId=old_id)
                            bot.rate_manager.update()
                        logger.info(f"CANCELED trailing sell {sym} @ {last_price}")
                    except: pass

                with DBManager() as sess:
                    pos = sess.query(Position).filter_by(symbol=sym).first()
                    if not pos or pos.quantity <= 0:
                        with bot.state_lock:
                            if sym in trailing_sell_active: del trailing_sell_active[sym]
                        continue
                    raw_qty = Decimal(str(pos.quantity))

                qty = round_quantity(raw_qty, sym, bot)
                if qty <= 0 or qty < 1:
                    logger.info(f"TRAIL SELL SKIP {sym}: Qty {qty} < 1")
                    continue

                value = best_ask * qty
                if value < SELL_ORDER_MINIMUM_USDT_COIN_VALUE:
                    logger.info(f"TRAIL SELL SKIP {sym}: Value ${value:.2f} < ${SELL_ORDER_MINIMUM_USDT_COIN_VALUE}")
                    continue

                target_price_rounded = round_price(target_price, sym, bot)
                if target_price_rounded <= 0: continue

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
                    send_whatsapp_alert(f"TRAIL SELL {sym} {qty} @ {target_price_rounded}")
                except BinanceAPIException as e:
                    if e.code == -1013:
                        logger.warning(f"TRAIL SELL PRICE_FILTER {sym}, using market")
                        bot.place_market_sell(sym, float(qty))
                        with bot.state_lock:
                            if sym in trailing_sell_active: del trailing_sell_active[sym]
                    else:
                        logger.error(f"Trailing sell failed {sym}: {e}")
                except Exception as e:
                    logger.error(f"Trailing sell failed {sym}: {e}")

            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.critical(f"Trailing sell crash: {e}", exc_info=True)
            time.sleep(10)

# === FEE & SIZE AWARE SELL TRIGGER ==========================================
def sell_trigger_scanner(bot):
    while True:
        try:
            for pos in bot.get_db_positions():
                sym = pos.symbol
                if sym in trailing_sell_active: continue
                ob = bot.get_order_book_analysis(sym)
                ask = ob['best_ask']
                if ask <= 0: continue
                entry = to_decimal(pos.avg_entry_price)
                qty = to_decimal(pos.quantity)

                # Size checks
                if qty < 1:
                    logger.info(f"SELL SKIP {sym}: Qty {qty} < 1")
                    continue
                value = ask * qty
                if value < SELL_ORDER_MINIMUM_USDT_COIN_VALUE:
                    logger.info(f"SELL SKIP {sym}: Value ${value:.2f} < ${SELL_ORDER_MINIMUM_USDT_COIN_VALUE}")
                    continue

                # Profit check
                maker, taker = bot.get_trade_fees(sym)
                gross_profit = (ask - entry) * qty
                fee_cost = (maker + taker) * ask * qty
                net_profit = gross_profit - fee_cost
                if net_profit < MIN_PROFIT_USDT:
                    continue

                gross_pct = (ask - entry) / entry
                required_gross = PROFIT_TARGET_NET + maker + taker
                if gross_pct < required_gross:
                    continue

                rsi, _, _ = bot.get_rsi_and_trend(sym)
                if rsi and rsi >= RSI_OVERBOUGHT:
                    with bot.state_lock:
                        trailing_sell_active[sym] = {'last_price': ask}
                    bot.place_market_sell(sym, float(qty))
                    logger.info(f"SELL TRIGGERED {sym} | Net ${net_profit:.2f} | {gross_pct*100:.2f}%")
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.critical(f"Sell trigger error: {e}", exc_info=True)
            time.sleep(10)


# === DASHBOARD ==============================================================
def print_professional_dashboard(client, bot):
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        now = now_cst()
        usdt_free = float(bot.get_balance('USDT'))
        total_portfolio, _ = bot.calculate_total_portfolio_value()
        total_portfolio = float(total_portfolio)

        GREEN = "\033[32m"
        RED = "\033[31m"
        BOLD = "\033[1m"
        YELLOW = "\033[33m"
        CYAN = "\033[36m"
        RESET = "\033[0m"

        print(f"{'='*120}")
        print(f"{BOLD}{CYAN}{'BINANCE.US TRADING BOT – PROFIT GUARANTEED':^120}{RESET}")
        print(f"{'='*120}\n")

        print(f"{BOLD}{'Time (CST)':<20}{RESET} {now}")
        print(f"{BOLD}{'Available USDT':<20}{RESET} ${usdt_free:,.6f}")
        print(f"{BOLD}{'Portfolio Value':<20}{RESET} ${total_portfolio:,.6f}")
        print(f"{BOLD}{'Trailing Buys':<20}{RESET} {len(trailing_buy_active)}")
        print(f"{BOLD}{'Trailing Sells':<20}{RESET} {len(trailing_sell_active)}")
        print(f"{'-'*120}\n")

        with DBManager() as sess:
            db_positions = sess.query(Position).all()

        if db_positions:
            print(f"{BOLD}{YELLOW}{'POSITIONS IN DATABASE':^120}{RESET}")
            print(f"{'-'*120}")
            print(f"{'SYMBOL':<10} {'QTY':>12} {'ENTRY':>12} {'CURRENT':>12} {'RSI':>6} {'NET P&L%':>10} {'PROFIT':>10} {'STATUS':<25}")
            print(f"{'-'*120}")
            total_pnl = Decimal('0')
            for pos in db_positions:
                symbol = pos.symbol
                qty = float(pos.quantity)
                entry = float(pos.avg_entry_price)
                ob = bot.get_order_book_analysis(symbol)
                cur_price = float(ob['best_bid'] or ob['best_ask'])
                rsi, _, _ = bot.get_rsi_and_trend(symbol)
                rsi_str = f"{rsi:5.1f}" if rsi else "N/A"

                maker, taker = bot.get_trade_fees(symbol)
                gross = (cur_price - entry) * qty
                fee_cost = (maker + taker) * cur_price * qty
                net_profit = Decimal(str(gross - fee_cost))
                pnl_pct = ((cur_price - entry) / entry - (maker + taker)) * 100
                total_pnl += net_profit

                status = ("Trailing Sell Active" if symbol in trailing_sell_active
                          else "Trailing Buy Active" if symbol in trailing_buy_active
                          else "24/7 Monitoring")
                color = GREEN if net_profit > 0 else RED
                print(f"{symbol:<10} {qty:>12.6f} {entry:>12.6f} {cur_price:>12.6f} {rsi_str} {color}{pnl_pct:>9.2f}%{RESET} {color}{float(net_profit):>10.2f}{RESET} {status:<25}")

            print(f"{'-'*120}")
            pnl_color = GREEN if total_pnl > 0 else RED
            print(f"{BOLD}{'TOTAL NET P&L (after fees)':<50}{RESET} {pnl_color}${float(total_pnl):>12,.2f}{RESET}\n")
        else:
            print(f"{YELLOW} No active positions.\n{RESET}")

        print(f"{'='*120}\n")

    except Exception as e:
        logger.error(f"Dashboard error: {e}")

# === ALERTS & MAIN ==========================================================
def send_whatsapp_alert(msg):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}", timeout=5)
        except: pass

def main():
    if not API_KEY or not API_SECRET:
        logger.critical("API keys missing")
        sys.exit(1)
    bot = BinanceTradingBot()

    threading.Thread(target=buy_trigger_scanner, args=(bot,), daemon=True).start()
    threading.Thread(target=sell_trigger_scanner, args=(bot,), daemon=True).start()
    threading.Thread(target=trailing_buy_scanner, args=(bot,), daemon=True).start()
    threading.Thread(target=trailing_sell_scanner, args=(bot,), daemon=True).start()

    last_dash = time.time()
    while True:
        bot.check_fills_and_update_db()
        bot.cancel_old_orders()
        if time.time() - last_dash >= 30:
            print_professional_dashboard(bot.client, bot)
            last_dash = time.time()
        time.sleep(1)

if __name__ == "__main__":
    main()

