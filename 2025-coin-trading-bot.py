#!/usr/bin/env python3
"""
Binance.US Trading Bot – FINAL VERSION
BUY: low RSI + 24h low + sell pressure → dip buy
SELL: 0.8% profit (excl fees) + RSI high + buy-pressure spike → drop → sell
"""

import os
import time
import logging
import signal
import sys
import numpy as np
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
from tenacity import retry, stop_after_attempt, wait_exponential
import talib
from datetime import datetime
import pytz
import requests
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Dict, Any, Optional, Tuple
from collections import deque

# === SQLALCHEMY ==============================================================
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, func
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import SQLAlchemyError

# === CONFIGURATION ===========================================================
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')
MAX_PRICE = 2000.00
MIN_PRICE = 0.000001
MIN_24H_VOLUME_USDT = 10000
LOOP_INTERVAL = 15
LOG_FILE = "crypto_trading_bot.log"
RSI_PERIOD = 14
BB_PERIOD = 20
BB_DEV = 2
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
PROFIT_TARGET_EXCL_FEES = 0.008          # 0.8 % net profit *excluding* fees
RISK_PER_TRADE = 0.10
MIN_BALANCE = 2.0

# Strategy
BUY_PRICE_TOLERANCE_PCT = 1.0
ORDERBOOK_SELL_PRESSURE_THRESHOLD = 0.60   # 60 %+ ask = sell pressure
ORDERBOOK_BUY_PRESSURE_SPIKE = 0.65        # spike ≥ 65 %
ORDERBOOK_BUY_PRESSURE_DROP = 0.55         # then drop ≤ 55 %
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
DEPTH_HISTORY = 6
DEPTH_WAIT = 1.0
MIN_MOVE_PCT = 0.0003
MAX_ITER = 45
ORDER_BOOK_LIMIT = 20

# API Keys
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

# === CONSTANTS ==============================================================
HUNDRED = Decimal('100')
FIFTY = Decimal('50')
POINT_TWO = Decimal('0.2')
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

# === GLOBAL CACHE ============================================================
valid_symbols_dict: Dict[str, dict] = {}
order_book_cache: Dict[str, dict] = {}
positions: Dict[str, dict] = {}
buy_pressure_history: Dict[str, deque] = {}   # for sell-signal pressure tracking

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
            logger.error(f"DB session error: {exc_val}")
            self.session.rollback()
        else:
            try:
                self.session.commit()
            except SQLAlchemyError as e:
                logger.error(f"DB commit failed: {e}")
                self.session.rollback()
        try:
            self.session.close()
        except:
            pass

# === SIGNAL HANDLER =========================================================
def signal_handler(signum, frame):
    logger.info("Shutdown signal received. Exiting gracefully...")
    sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)

# === SAFE MATH ==============================================================
def safe_float(value, default=0.0) -> float:
    try:
        if value is None or value == '':
            return default
        f = float(value)
        return f if np.isfinite(f) else default
    except Exception as e:
        logger.debug(f"safe_float conversion failed: {value} -> {e}")
        return default

def is_valid_float(value) -> bool:
    return value is not None and np.isfinite(value) and value > 0

def to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except (InvalidOperation, TypeError, ValueError) as e:
        logger.debug(f"to_decimal failed: {value} -> {e}")
        return ZERO

# === FETCH & VALIDATE SYMBOLS – ONLY CHANGE =================================
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def fetch_and_validate_usdt_pairs(client) -> Dict[str, dict]:
    """
    Fetches all /USDT pairs and **STRICTLY filters** by:
    - MIN_PRICE <= price <= MAX_PRICE
    - 24h volume >= MIN_24H_VOLUME_USDT
    - Only TRADING status
    """
    global valid_symbols_dict
    try:
        logger.info("Fetching exchange info and validating /USDT pairs...")
        info = client.get_exchange_info()
        raw_symbols = [
            s['symbol'] for s in info['symbols']
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING' and s['symbol'].endswith('USDT')
        ]
        logger.info(f"Found {len(raw_symbols)} /USDT pairs. Applying strict filters...")

        valid = {}
        for symbol in raw_symbols:
            try:
                ticker = client.get_ticker(symbol=symbol)
                price = safe_float(ticker.get('lastPrice'))
                volume = safe_float(ticker.get('quoteVolume'))
                low_24h = safe_float(ticker.get('lowPrice'))
                high_24h = safe_float(ticker.get('highPrice'))

                # === STRICT FILTERS USING CONFIG ===
                if price < MIN_PRICE:
                    logger.debug(f"{symbol} price {price} < MIN_PRICE {MIN_PRICE}")
                    continue
                if price > MAX_PRICE:
                    logger.debug(f"{symbol} price {price} > MAX_PRICE {MAX_PRICE}")
                    continue
                if volume < MIN_24H_VOLUME_USDT:
                    logger.debug(f"{symbol} volume {volume:,.0f} < MIN_24H_VOLUME_USDT {MIN_24H_VOLUME_USDT}")
                    continue
                if not all(is_valid_float(x) for x in [price, low_24h, high_24h]):
                    logger.debug(f"{symbol} invalid ticker data")
                    continue

                valid[symbol] = {
                    'price': price,
                    'volume': volume,
                    'low_24h': low_24h,
                    'high_24h': high_24h
                }
                logger.debug(f"{symbol} PASSED: price={price}, vol={volume:,.0f}")

            except Exception as e:
                logger.debug(f"Validation failed for {symbol}: {e}")

        logger.info(f"FINAL VALID SYMBOLS AFTER FILTERING: {len(valid)}")
        valid_symbols_dict = valid
        return valid

    except Exception as e:
        logger.error(f"Failed to fetch symbols: {e}")
        return {}

# === ORDER BOOK ANALYSIS (Decimal-safe) =====================================
def get_order_book_analysis(client, symbol: str) -> dict:
    global order_book_cache
    now = time.time()
    cache = order_book_cache.get(symbol, {})
    if cache and now - cache.get('ts', 0) < 2:
        return cache

    try:
        depth = client.get_order_book(symbol=symbol, limit=ORDER_BOOK_LIMIT)
        bids = depth.get('bids', [])[:5]
        asks = depth.get('asks', [])[:5]

        bid_vol = sum(Decimal(b[1]) for b in bids)
        ask_vol = sum(Decimal(a[1]) for a in asks)
        total = bid_vol + ask_vol
        total = total if total > 0 else Decimal('1')

        pct_bid = float(bid_vol / total * HUNDRED)
        pct_ask = 100.0 - pct_bid

        result = {
            'pct_bid': pct_bid,
            'pct_ask': pct_ask,
            'best_bid': Decimal(bids[0][0]) if bids else ZERO,
            'best_ask': Decimal(asks[0][0]) if asks else ZERO,
            'ts': now
        }
        order_book_cache[symbol] = result
        return result
    except Exception as e:
        logger.error(f"Order book fetch failed for {symbol}: {e}")
        return {'pct_bid': 50.0, 'pct_ask': 50.0, 'best_bid': ZERO, 'best_ask': ZERO, 'ts': now}

# === PERFORMANCE INDICATORS =================================================
def get_historical_metrics(client, symbol) -> dict:
    try:
        klines = client.get_klines(symbol=symbol, interval='1m', limit=200)
        if len(klines) < 100:
            return {}

        closes = np.array([safe_float(k[4]) for k in klines[-100:]])
        if not np.all(np.isfinite(closes)):
            return {}

        rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)
        upper, middle, lower = talib.BBANDS(closes, timeperiod=BB_PERIOD, nbdevup=BB_DEV, nbdevdn=BB_DEV)
        macd, signal_line, _ = talib.MACD(closes, fastperiod=MACD_FAST, slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)

        current_price = closes[-1]
        trend = ("bullish" if current_price > middle[-1] and macd[-1] > signal_line[-1] else
                 "bearish" if current_price < middle[-1] and macd[-1] < signal_line[-1] else "sideways")

        low_24h = valid_symbols_dict.get(symbol, {}).get('low_24h')

        return {
            'rsi': rsi[-1] if len(rsi) > 0 and np.isfinite(rsi[-1]) else None,
            'trend': trend,
            'low_24h': low_24h
        }
    except Exception as e:
        logger.error(f"Metrics error {symbol}: {e}")
        return {}

# === DASHBOARD ==============================================================
def print_status_dashboard(client):
    try:
        usdt_free = get_balance(client, 'USDT')
        total_portfolio_usdt, asset_usdt_values = calculate_total_portfolio_value(client)

        with DBManager() as sess:
            db_positions = sess.query(Position).all()

        total_unrealized = Decimal('0')

        print("\n" + "="*100)
        print(f" PROFESSIONAL TRADING DASHBOARD - {now_cst()} ")
        print("="*100)
        print(f"Available Cash (USDT):         ${usdt_free:,.6f}")
        print(f"Total Portfolio Value:         ${total_portfolio_usdt:,.6f}")
        print(f"Active Tracked Positions:      {len(db_positions)}")
        print("-" * 100)

        owned_coins = get_all_nonzero_balances(client)
        if owned_coins:
            print(f"{'ASSET':<8} {'QTY':>12} {'≈ USDT':>12}")
            print("-" * 40)
            for asset, qty in owned_coins.items():
                usdt_val = asset_usdt_values.get(asset, 0.0)
                print(f"{asset:<8} {qty:>12.8f} {usdt_val:>12.2f}")
            print("-" * 40)

        if not db_positions:
            print(" No tracked positions.")
            print("="*100 + "\n")
            return

        print(f"{'SYMBOL':<10} {'QTY':>10} {'ENTRY':>12} {'CURRENT':>12} {'RSI':>6} {'P&L %':>8} {'PROFIT $':>10} {'SELL PRICE':>12} {'AGE':>12}")
        print("-" * 100)

        for pos in db_positions:
            symbol = pos.symbol
            qty = float(pos.quantity)
            entry_price = float(pos.avg_entry_price)
            buy_fee = float(pos.buy_fee_rate)

            cur_data = fetch_current_data(client, symbol)
            cur_price = cur_data['price'] if cur_data else entry_price

            metrics = get_historical_metrics(client, symbol)
            rsi = metrics.get('rsi')
            rsi_disp = f"{rsi:5.1f}" if rsi is not None else " N/A "

            maker_fee, taker_fee = get_trade_fees(client, symbol)
            total_fees = buy_fee + taker_fee

            gross = (cur_price - entry_price) * qty
            fee_cost = total_fees * cur_price * qty
            net_profit = gross - fee_cost
            profit_pct = ((cur_price - entry_price) / entry_price - total_fees) * 100

            total_unrealized += Decimal(str(net_profit))

            target_sell = entry_price * (1 + PROFIT_TARGET_EXCL_FEES + buy_fee + taker_fee)
            age = datetime.now(CST_TZ) - pos.updated_at.replace(tzinfo=CST_TZ)
            age_str = str(age).split('.')[0]

            print(f"{symbol:<10} {qty:>10.6f} {entry_price:>12.6f} {cur_price:>12.6f} "
                  f"{rsi_disp} {profit_pct:>7.2f}% {net_profit:>10.2f} {target_sell:>12.6f} {age_str:>12}")

        print("-" * 100)
        print(f"{'TOTAL UNREALIZED P&L':<30} ${float(total_unrealized):>12,.2f}")
        print(f"Bot Running... Next update in {LOOP_INTERVAL} seconds.\n")
        print("="*100 + "\n")

    except Exception as e:
        logger.error(f"Dashboard rendering failed: {e}", exc_info=True)

# === IMPORT OWNED ASSETS AT STARTUP =========================================
def import_owned_assets_to_db(client, sess):
    try:
        account = client.get_account()
        logger.info("Importing owned assets from Binance account...")
        imported = 0
        for bal in account['balances']:
            asset = bal['asset']
            qty_str = bal['free']
            qty = safe_float(qty_str)
            if qty <= 0 or asset == 'USDT':
                continue

            price_usdt = get_price_usdt(client, asset)
            if price_usdt <= 0:
                logger.warning(f"Could not get price for {asset}, skipping.")
                continue

            symbol = f"{asset}USDT"
            existing = sess.query(Position).filter_by(symbol=symbol).one_or_none()
            if existing:
                logger.debug(f"Position already exists for {symbol}, skipping import.")
                continue

            maker_fee, _ = get_trade_fees(client, symbol)
            pos = Position(
                symbol=symbol,
                quantity=Decimal(str(qty)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN),
                avg_entry_price=price_usdt,
                buy_fee_rate=maker_fee
            )
            sess.add(pos)
            imported += 1
            logger.info(f"Imported {asset}: {qty:.8f} @ {price_usdt} → {symbol}")

        logger.info(f"Imported {imported} owned assets into Position table.")
    except Exception as e:
        logger.error(f"Failed to import owned assets: {e}", exc_info=True)

# === BOT CLASS ==============================================================
class BinanceTradingBot:
    def __init__(self):
        try:
            self.client = Client(API_KEY, API_SECRET, tld='us')
            logger.info("Binance.US client initialized.")
            self.load_state_from_db()

            with DBManager() as sess:
                import_owned_assets_to_db(self.client, sess)

        except Exception as e:
            logger.critical(f"Failed to initialize bot: {e}")
            sys.exit(1)

    def load_state_from_db(self):
        global positions
        try:
            with DBManager() as sess:
                db_positions = sess.query(Position).all()
                for p in db_positions:
                    positions[p.symbol] = {
                        'qty': float(p.quantity),
                        'entry_price': float(p.avg_entry_price),
                        'entry_time': p.updated_at.replace(tzinfo=CST_TZ),
                        'buy_fee': float(p.buy_fee_rate)
                    }
                logger.info(f"Loaded {len(positions)} positions from DB.")
        except Exception as e:
            logger.error(f"Failed to load positions from DB: {e}")

    def get_position(self, sess, symbol: str) -> Optional[Position]:
        try:
            return sess.query(Position).filter_by(symbol=symbol).one_or_none()
        except Exception as e:
            logger.error(f"DB query failed for {symbol}: {e}")
            return None

    def record_trade(self, sess, symbol, side, price, qty, binance_order_id, pending_order=None):
        try:
            trade = Trade(symbol=symbol, side=side, price=price, quantity=qty,
                          binance_order_id=binance_order_id, pending_order=pending_order)
            sess.add(trade)
            pos = self.get_position(sess, symbol)
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
        except Exception as e:
            logger.error(f"Failed to record trade: {e}")

    def add_pending_order(self, sess, binance_order_id, symbol, side, price, qty):
        try:
            order = PendingOrder(binance_order_id=binance_order_id, symbol=symbol, side=side, price=price, quantity=qty)
            sess.add(order)
            return order
        except Exception as e:
            logger.error(f"Failed to add pending order: {e}")
            return None

    def remove_pending_order(self, sess, binance_order_id):
        try:
            sess.query(PendingOrder).filter_by(binance_order_id=binance_order_id).delete()
        except Exception as e:
            logger.error(f"Failed to remove pending order {binance_order_id}: {e}")

    def place_limit_buy_with_tracking(self, symbol, price: str, qty: float):
        try:
            order = self.client.order_limit_buy(symbol=symbol, quantity=qty, price=price)
            order_id = str(order['orderId'])
            with DBManager() as sess:
                self.add_pending_order(sess, order_id, symbol, 'buy', Decimal(price), Decimal(str(qty)))
            logger.info(f"BUY LIMIT {symbol} @ {price} qty {qty}")
            return order
        except Exception as e:
            logger.error(f"Buy order failed: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol, price: str, qty: float):
        try:
            order = self.client.order_limit_sell(symbol=symbol, quantity=qty, price=price)
            order_id = str(order['orderId'])
            with DBManager() as sess:
                self.add_pending_order(sess, order_id, symbol, 'sell', Decimal(price), Decimal(str(qty)))
            logger.info(f"SELL LIMIT {symbol} @ {price} qty {qty}")
            return order
        except Exception as e:
            logger.error(f"Sell order failed: {e}")
            return None

    def check_and_process_filled_orders(self):
        try:
            with DBManager() as sess:
                for pending in sess.query(PendingOrder).all():
                    try:
                        order = self.client.get_order(symbol=pending.symbol, orderId=int(pending.binance_order_id))
                        if order['status'] == 'FILLED':
                            executed_qty = Decimal(order['executedQty'])
                            cumm_quote = Decimal(order['cummulativeQuoteQty'])
                            fill_price = cumm_quote / executed_qty if executed_qty > 0 else pending.price
                            self.record_trade(sess, pending.symbol, pending.side, fill_price, executed_qty,
                                              pending.binance_order_id, pending)
                            self.remove_pending_order(sess, pending.binance_order_id)
                            action = "BUY" if pending.side == 'buy' else "SELL"
                            logger.info(f"{action} FILLED: {pending.symbol} @ {fill_price}")
                            send_whatsapp_alert(f"{action} {pending.symbol} @ {fill_price:.6f}")
                    except Exception as e:
                        logger.debug(f"Order check failed for {pending.binance_order_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to process filled orders: {e}")

# === BUY SIGNAL =============================================================
def check_buy_signal(client, symbol):
    try:
        final_price = follow_price_with_rsi(client, symbol, side='buy')
        if final_price <= 0:
            return False, None

        metrics = get_historical_metrics(client, symbol)
        rsi = metrics.get('rsi')
        trend = metrics.get('trend')
        low_24h = metrics.get('low_24h')

        if rsi is None or rsi > RSI_OVERSOLD:
            return False, None
        if trend != 'bullish':
            return False, None
        if low_24h and final_price > Decimal(str(low_24h)) * Decimal('1.01'):
            return False, None

        ob = get_order_book_analysis(client, symbol)
        if ob['pct_ask'] < ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100:
            return False, None

        logger.info(f"BUY SIGNAL: {symbol} @ {final_price} | RSI={rsi:.1f} | Trend={trend.upper()}")
        return True, final_price

    except Exception as e:
        logger.error(f"check_buy_signal error [{symbol}]: {e}")
        return False, None

def execute_buy(client, symbol, bot):
    try:
        should_buy, final_price = check_buy_signal(client, symbol)
        if not should_buy or not final_price > 0:
            return

        balance = get_balance(client)
        if balance <= MIN_BALANCE:
            return

        balance_d = Decimal(str(balance))
        min_bal_d = Decimal(str(MIN_BALANCE))
        risk_d = Decimal(str(RISK_PER_TRADE))
        available = balance_d - min_bal_d
        if available <= 0:
            return

        alloc = min(available * risk_d, available)
        qty = alloc / final_price

        adjusted, error = validate_and_adjust_order(client, symbol, 'BUY', ORDER_TYPE_LIMIT, qty, final_price)
        if error or not adjusted:
            return

        bot.place_limit_buy_with_tracking(symbol, str(adjusted['price']), float(adjusted['quantity']))
        send_whatsapp_alert(f"BUY {symbol} @ {adjusted['price']:.6f}")
    except Exception as e:
        logger.error(f"execute_buy failed [{symbol}]: {e}", exc_info=True)

# === SELL SIGNAL ============================================================
def check_sell_signal(client, symbol, position):
    try:
        cur_data = fetch_current_data(client, symbol)
        if not cur_data or not is_valid_float(cur_data['price']):
            return False, None
        cur_price = Decimal(str(cur_data['price']))
        entry_price = Decimal(str(position.avg_entry_price))

        profit_excl_fees = (cur_price - entry_price) / entry_price
        if profit_excl_fees < Decimal(str(PROFIT_TARGET_EXCL_FEES)):
            return False, None

        final_price = follow_price_with_rsi(client, symbol, side='sell')
        if final_price <= 0:
            return False, None

        metrics = get_historical_metrics(client, symbol)
        rsi = metrics.get('rsi')
        if rsi is None or rsi < RSI_OVERBOUGHT:
            return False, None

        history = buy_pressure_history.get(symbol, deque(maxlen=5))
        ob = get_order_book_analysis(client, symbol)
        history.append(ob['pct_bid'])
        buy_pressure_history[symbol] = history

        if len(history) >= 3:
            peak = max(history)
            current = history[-1]
            if peak >= ORDERBOOK_BUY_PRESSURE_SPIKE * 100 and current <= ORDERBOOK_BUY_PRESSURE_DROP * 100:
                net_pct = float((final_price - entry_price) / entry_price - Decimal(str(get_trade_fees(client, symbol)[0] + get_trade_fees(client, symbol)[1]))) * 100
                logger.info(f"SELL SIGNAL: {symbol} @ {final_price} | RSI={rsi:.1f} | Net P&L: {net_pct:+.3f}%")
                return True, final_price

        return False, None

    except Exception as e:
        logger.error(f"check_sell_signal error [{symbol}]: {e}")
        return False, None

def execute_sell(client, symbol, position, bot):
    try:
        should_sell, final_price = check_sell_signal(client, symbol, position)
        if not should_sell or not final_price > 0:
            return

        qty = position.quantity
        adjusted, error = validate_and_adjust_order(client, symbol, 'SELL', ORDER_TYPE_LIMIT, qty, final_price)
        if error or not adjusted:
            return

        bot.place_limit_sell_with_tracking(symbol, str(adjusted['price']), float(adjusted['quantity']))
        send_whatsapp_alert(f"SELL {symbol} @ {adjusted['price']:.6f}")
    except Exception as e:
        logger.error(f"execute_sell failed [{symbol}]: {e}", exc_info=True)

# === HELPERS ================================================================
def now_cst():
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_price_usdt(client, asset: str) -> Decimal:
    if asset == 'USDT':
        return Decimal('1')
    symbol = asset + 'USDT'
    try:
        return Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
    except:
        for bridge in ('BTC', 'ETH'):
            try:
                p1 = Decimal(client.get_symbol_ticker(symbol=asset + bridge)['price'])
                p2 = Decimal(client.get_symbol_ticker(symbol=bridge + 'USDT')['price'])
                return p1 * p2
            except:
                continue
    return ZERO

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_balance(client, asset='USDT') -> float:
    try:
        for bal in client.get_account()['balances']:
            if bal['asset'] == asset:
                return safe_float(bal['free'])
        return 0.0
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return 0.0

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def calculate_total_portfolio_value(client):
    try:
        account = client.get_account()
        total = Decimal('0')
        asset_values = {}
        for b in account['balances']:
            qty = Decimal(str(safe_float(b['free'])))
            if qty <= 0:
                continue
            if b['asset'] == 'USDT':
                total += qty
                asset_values['USDT'] = float(qty)
            else:
                price = get_price_usdt(client, b['asset'])
                if price > 0:
                    val = qty * price
                    total += val
                    asset_values[b['asset']] = float(val)
        return float(total), asset_values
    except Exception as e:
        logger.error(f"Portfolio calc error: {e}")
        return 0.0, {}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def fetch_current_data(client, symbol):
    try:
        ticker = client.get_ticker(symbol=symbol)
        price = safe_float(ticker['lastPrice'])
        if not is_valid_float(price):
            return None
        return {'price': price}
    except Exception as e:
        logger.debug(f"fetch_current_data failed for {symbol}: {e}")
        return None

def get_all_nonzero_balances(client):
    try:
        account = client.get_account()
        return {b['asset']: Decimal(b['free']) for b in account['balances'] if safe_float(b['free']) > 0 and b['asset'] != 'USDT'}
    except Exception as e:
        logger.error(f"get_all_nonzero_balances error: {e}")
        return {}

def get_symbol_filters(client, symbol):
    try:
        info = client.get_exchange_info()
        s = next(x for x in info['symbols'] if x['symbol'] == symbol)
        filters = {}
        for f in s['filters']:
            ft = f['filterType']
            if ft in ['LOT_SIZE', 'PRICE_FILTER', 'MIN_NOTIONAL']:
                filters[ft] = {k: safe_float(f.get(k)) for k in f.keys() if k != 'filterType'}
        return filters
    except Exception as e:
        logger.error(f"get_symbol_filters error for {symbol}: {e}")
        return {}

def validate_and_adjust_order(client, symbol, side, order_type, quantity, price=None, current_price=None):
    try:
        filters = get_symbol_filters(client, symbol)
        adj_qty = Decimal(str(quantity))
        adj_price = Decimal(str(price)) if price else None

        lot = filters.get('LOT_SIZE', {})
        if lot and lot.get('stepSize', 0) > 0:
            step = Decimal(str(lot['stepSize']))
            adj_qty = (adj_qty // step) * step
            if adj_qty < lot.get('minQty', 0):
                return None, "Qty too low"

        if adj_price is not None:
            price_f = filters.get('PRICE_FILTER', {})
            if price_f and price_f.get('tickSize', 0) > 0:
                tick = Decimal(str(price_f['tickSize']))
                adj_price = (adj_price // tick) * tick
                if adj_price < price_f.get('minPrice', 0):
                    return None, "Price too low"

        min_notional = filters.get('MIN_NOTIONAL', {}).get('minNotional', 0)
        if min_notional > 0 and adj_price is not None:
            notional = adj_qty * adj_price
            if notional < min_notional:
                return None, "Below min notional"

        return {'quantity': float(adj_qty), 'price': float(adj_price) if adj_price else None}, None
    except Exception as e:
        logger.error(f"validate_and_adjust_order error [{symbol}]: {e}")
        return None, "Filter error"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_trade_fees(client, symbol):
    try:
        fee = client.get_trade_fee(symbol=symbol)
        return safe_float(fee[0]['makerCommission']), safe_float(fee[0]['takerCommission'])
    except Exception as e:
        logger.warning(f"Using default fees for {symbol}: {e}")
        return 0.001, 0.001

def send_whatsapp_alert(message: str):
    if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE:
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(message)}&apikey={CALLMEBOT_API_KEY}"
        requests.get(url, timeout=10)
        logger.debug(f"WhatsApp alert sent: {message}")
    except Exception as e:
        logger.error(f"WhatsApp alert failed: {e}")

def _best_price(depth: dict, side: str) -> Decimal:
    if side == 'buy' and depth.get('bids'):
        return Decimal(str(depth['bids'][0][0]))
    if side == 'sell' and depth.get('asks'):
        return Decimal(str(depth['asks'][0][0]))
    return ZERO

def follow_price_with_rsi(client, symbol: str, side: str) -> Decimal:
    history = deque(maxlen=DEPTH_HISTORY)
    direction = 1 if side == 'buy' else -1

    for i in range(MAX_ITER):
        try:
            depth = client.get_order_book(symbol=symbol, limit=5)
            price = _best_price(depth, side)
            if price <= 0:
                time.sleep(DEPTH_WAIT)
                continue
            history.append(price)

            if len(history) < DEPTH_HISTORY:
                time.sleep(DEPTH_WAIT)
                continue

            delta_pct = float((history[-1] - history[0]) / history[0])
            moved_against = direction * delta_pct < -MIN_MOVE_PCT
            flat = abs(delta_pct) < MIN_MOVE_PCT

            if moved_against or flat:
                return price

        except Exception as e:
            logger.debug(f"follow_price_with_rsi [{symbol}] iter {i}: {e}")

        time.sleep(DEPTH_WAIT)

    try:
        depth = client.get_order_book(symbol=symbol, limit=5)
        return _best_price(depth, side)
    except Exception as e:
        logger.error(f"Final price fetch failed: {e}")
        return ZERO

# === MAIN LOOP ==============================================================
def main():
    if not API_KEY or not API_SECRET:
        logger.critical("API keys missing. Set BINANCE_API_KEY and BINANCE_API_SECRET.")
        sys.exit(1)

    client = Client(API_KEY, API_SECRET, tld='us')
    bot = BinanceTradingBot()

    if not fetch_and_validate_usdt_pairs(client):
        logger.critical("No valid symbols found. Exiting.")
        sys.exit(1)

    logger.info("Bot started. Entering main loop...")
    while True:
        try:
            bot.check_and_process_filled_orders()

            for symbol in list(valid_symbols_dict.keys()):
                try:
                    with DBManager() as sess:
                        if sess.query(Position).filter_by(symbol=symbol).first():
                            continue
                    execute_buy(client, symbol, bot)
                except Exception as e:
                    logger.warning(f"Buy loop error [{symbol}]: {e}")

            with DBManager() as sess:
                for pos in sess.query(Position).all():
                    try:
                        execute_sell(client, pos.symbol, pos, bot)
                    except Exception as e:
                        logger.warning(f"Sell loop error [{pos.symbol}]: {e}")

            print_status_dashboard(client)
            time.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break
        except Exception as e:
            logger.critical(f"Main loop crashed: {e}", exc_info=True)
            time.sleep(10)

if __name__ == "__main__":
    main()
