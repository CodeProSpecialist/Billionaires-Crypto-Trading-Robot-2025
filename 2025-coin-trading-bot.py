#!/usr/bin/env python3
"""
Binance.US Trading Bot – FULL VERSION (Order-Book First Strategy)
- Imports ALL owned assets (any pair) on startup
- Primary: Follow order book to bottom/top before buying/selling
- Secondary: ATR + Bollinger Bands as soft filters (never override book)
- Full original strategy: RSI, MACD, MFI, BB, Stochastic, Candlesticks, Momentum
- SQLAlchemy persistence + graceful shutdown
- WhatsApp alerts + dashboard
"""

import os
import time
import logging
import signal
import sys
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException, BinanceOrderException
from tenacity import retry, stop_after_attempt, wait_exponential
import talib
import numpy as np
from datetime import datetime, timedelta
import pytz
import requests
from decimal import Decimal
from typing import Optional, Dict, Any
from collections import deque  # <-- NEW: for order-book tracking

# === SQLALCHEMY ===
from sqlalchemy import (
    create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import SQLAlchemyError

# === CONFIGURATION ===
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')
MAX_PRICE = 1000.00
MIN_PRICE = 1.00
LOOP_INTERVAL = 60
LOG_FILE = "crypto_trading_bot.log"
VOLUME_THRESHOLD = 15000
RSI_PERIOD = 14
BB_PERIOD = 20
BB_DEV = 2
ATR_PERIOD = 14
SMA_PERIOD = 50
EMA_PERIOD = 21
HISTORY_DAYS = 60
KLINE_INTERVAL = '1h'
PROFIT_TARGET = 0.008  # 0.8%
RISK_PER_TRADE = 0.10
MIN_BALANCE = 2.0
ORDER_TIMEOUT = 300

# === DEBUG ===
DEBUG_SHOW_API_COIN_DATA_FETCHING = False

# API Keys
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Timezone
CST_TZ = pytz.timezone('America/Chicago')

# === DATABASE ===
DB_URL = "sqlite:///binance_trades.db"
engine = create_engine(DB_URL, echo=False, future=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

# === MODELS ===
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

# === DB MANAGER ===
class DBManager:
    def __init__(self):
        self.session = None

    def __enter__(self):
        self.session = SessionFactory()
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.session.rollback()
        else:
            try:
                self.session.commit()
            except SQLAlchemyError as e:
                logger.error(f"DB commit failed: {e}")
                self.session.rollback()
        self.session.close()

def signal_handler(signum, frame):
    logger.info("Ctrl+C received. Shutting down gracefully...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ----------------------------------------------------------------------
#  ORDER-BOOK FOLLOW-THE-PRICE ENGINE
# ----------------------------------------------------------------------
DEPTH_HISTORY = 6          # ~6 seconds of data
DEPTH_WAIT    = 1.0        # seconds between pulls
MIN_MOVE_PCT  = 0.0003     # 0.03% → consider "still moving"
MAX_ITER      = 45         # safety net (~45 s)

def _best_price(depth: dict, side: str) -> Decimal:
    """Best bid for BUY, best ask for SELL."""
    if side == 'buy' and depth.get('bids'):
        return Decimal(depth['bids'][0][0])
    if side == 'sell' and depth.get('asks'):
        return Decimal(depth['asks'][0][0])
    raise ValueError("Empty depth")

def follow_price_to_extreme(client, symbol: str, side: str) -> Decimal:
    """
    Pull depth repeatedly.
    Returns the price once it stops moving *in the expected direction*.
    """
    history = deque(maxlen=DEPTH_HISTORY)
    direction = 1 if side == 'buy' else -1   # buy: look for lower, sell: higher

    for i in range(MAX_ITER):
        depth = client.get_order_book(symbol=symbol, limit=5)
        price = _best_price(depth, side)
        history.append(price)

        if len(history) < DEPTH_HISTORY:
            time.sleep(DEPTH_WAIT)
            continue

        # % change over the whole window
        delta_pct = float((history[-1] - history[0]) / history[0])
        moved_against = direction * delta_pct < -MIN_MOVE_PCT
        flat = abs(delta_pct) < MIN_MOVE_PCT

        if moved_against:
            logger.info(f"{side.upper()} reversal → {symbol} @ {price}")
            return price
        if flat:
            logger.info(f"{side.upper()} flat → {symbol} @ {price}")
            return price

        time.sleep(DEPTH_WAIT)

    logger.warning(f"{side.upper()} timeout → using last price {price}")
    return price

# === BOT CLASS ===
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.load_state_from_db()

    # === IMPORT ALL OWNED ASSETS ON STARTUP ===
    def load_state_from_db(self):
        """Restore DB + import every owned coin (any pair) as a tracked Position."""
        with DBManager() as sess:
            db_positions = {p.symbol: p for p in sess.query(Position).all()}
            logger.info(f"DB contains {len(db_positions)} tracked positions")

            try:
                account = self.client.get_account()
            except Exception as e:
                logger.error(f"Failed to fetch account: {e}")
                return

            imported = 0
            for bal in account['balances']:
                asset = bal['asset']
                free = Decimal(bal['free'])
                if free <= 0 or asset == 'USDT':
                    continue

                symbol = asset + 'USDT'
                if symbol in db_positions:
                    continue

                price_usdt = get_price_usdt(self.client, asset)
                if price_usdt <= 0:
                    logger.warning(f"Skipping {asset} – no USDT price")
                    continue

                maker_fee, _ = get_trade_fees(self.client, symbol)

                pos = Position(
                    symbol=symbol,
                    quantity=free,
                    avg_entry_price=price_usdt,
                    buy_fee_rate=maker_fee
                )
                sess.add(pos)
                imported += 1

            if imported:
                logger.info(f"Imported {imported} new owned assets as tracked positions")
                sess.commit()
            else:
                logger.info("No new assets to import")

    # === DB HELPERS ===
    def get_position(self, sess, symbol: str) -> Optional[Position]:
        return sess.query(Position).filter_by(symbol=symbol).one_or_none()

    def get_pending_order(self, sess, binance_order_id: str) -> Optional[PendingOrder]:
        return sess.query(PendingOrder).filter_by(binance_order_id=binance_order_id).one_or_none()

    def record_trade(self, sess, symbol, side, price, qty, binance_order_id, pending_order=None):
        trade = Trade(
            symbol=symbol,
            side=side,
            price=price,
            quantity=qty,
            binance_order_id=binance_order_id,
            pending_order=pending_order
        )
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

    def add_pending_order(self, sess, binance_order_id, symbol, side, price, qty):
        order = PendingOrder(
            binance_order_id=binance_order_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=qty
        )
        sess.add(order)
        return order

    def remove_pending_order(self, sess, binance_order_id):
        sess.query(PendingOrder).filter_by(binance_order_id=binance_order_id).delete()

    # === ORDER TRACKING ===
    def place_limit_buy_with_tracking(self, symbol, price, qty):
        try:
            order = self.client.order_limit_buy(symbol=symbol, quantity=qty, price=price)
            order_id = str(order['orderId'])
            with DBManager() as sess:
                self.add_pending_order(sess, order_id, symbol, 'buy', price, qty)
            logger.info(f"BUY LIMIT {symbol} @ {price} qty {qty} (ID: {order_id})")
            return order
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol, price, qty):
        try:
            order = self.client.order_limit_sell(symbol=symbol, quantity=qty, price=price)
            order_id = str(order['orderId'])
            with DBManager() as sess:
                self.add_pending_order(sess, order_id, symbol, 'sell', price, qty)
            logger.info(f"SELL LIMIT {symbol} @ {price} qty {qty} (ID: {order_id})")
            return order
        except Exception as e:
            logger.error(f"Sell failed: {e}")
            return None

    def check_and_process_filled_orders(self):
        with DBManager() as sess:
            pending_orders = sess.query(PendingOrder).all()
            for pending in pending_orders:
                try:
                    order = self.client.get_order(symbol=pending.symbol, orderId=int(pending.binance_order_id))
                    if order['status'] == 'FILLED':
                        executed_qty = Decimal(order['executedQty'])
                        cumm_quote = Decimal(order['cummulativeQuoteQty'])
                        fill_price = cumm_quote / executed_qty if executed_qty > 0 else pending.price

                        self.record_trade(
                            sess=sess,
                            symbol=pending.symbol,
                            side=pending.side,
                            price=fill_price,
                            qty=executed_qty,
                            binance_order_id=pending.binance_order_id,
                            pending_order=pending
                        )

                        self.remove_pending_order(sess, pending.binance_order_id)

                        action = "BUY" if pending.side == 'buy' else "SELL"
                        logger.info(f"{action} FILLED: {pending.symbol} @ {fill_price} qty {executed_qty}")
                        send_whatsapp_alert(f"{action} {pending.symbol} @ {fill_price:.6f}")

                except Exception as e:
                    logger.debug(f"Order check failed {pending.binance_order_id}: {e}")

    # === DASHBOARD ===
    def print_status_dashboard(self):
        with DBManager() as sess:
            positions = sess.query(Position).all()
            usdt_free = get_balance(self.client, 'USDT')
            total_portfolio, _ = calculate_total_portfolio_value(self.client)

            print("\n" + "="*100)
            print(f" PROFESSIONAL TRADING DASHBOARD - {now_cst()} ")
            print("="*100)
            print(f"Available Cash (USDT):         ${usdt_free:,.6f}")
            print(f"Total Portfolio Value:         ${total_portfolio:,.6f}")
            print(f"Active Tracked Positions:      {len(positions)}")
            print("-" * 100)

            if positions:
                print(f"{'SYMBOL':<10} {'QTY':>12} {'ENTRY':>12} {'CURRENT':>12} {'P&L %':>8} {'PROFIT $':>10}")
                print("-" * 100)
                total_unrealized = 0
                for pos in positions:
                    current = fetch_current_data(self.client, pos.symbol)
                    cur_price = Decimal(str(current['price'])) if current else pos.avg_entry_price
                    gross_pnl = (cur_price - pos.avg_entry_price) * pos.quantity
                    fee_cost = gross_pnl * (pos.buy_fee_rate + Decimal('0.001'))
                    net_pnl = gross_pnl - fee_cost
                    pnl_pct = float(net_pnl / (pos.avg_entry_price * pos.quantity) * 100)
                    total_unrealized += float(net_pnl)
                    print(f"{pos.symbol:<10} {pos.quantity:>12.6f} {pos.avg_entry_price:>12.6f} {cur_price:>12.6f} {pnl_pct:>7.2f}% {net_pnl:>10.2f}")
                print("-" * 100)
                print(f"Total Unrealized P&L:          ${total_unrealized:,.2f}")
            else:
                print(" No active positions.")
            print("="*100 + "\n")

    # === MAIN LOOP ===
    def run(self):
        logger.info("Bot starting...")
        symbols = get_all_usdt_symbols(self.client)

        try:
            while True:
                self.check_and_process_filled_orders()

                # Buy new signals
                for symbol in symbols:
                    if not any(p.symbol == symbol for p in self.get_all_positions()):
                        if check_buy_signal(self.client, symbol):
                            execute_buy(self.client, symbol, self)

                # Sell all positions
                with DBManager() as sess:
                    for pos in sess.query(Position).all():
                        if check_sell_signal(self.client, pos.symbol, pos):
                            execute_sell(self.client, pos.symbol, pos, self)

                self.print_status_dashboard()
                time.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
        finally:
            logger.info("Bot stopped gracefully.")

    def get_all_positions(self):
        with DBManager() as sess:
            return sess.query(Position).all()

# === FULL ORIGINAL HELPERS & STRATEGY ===
def now_cst():
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_price_usdt(client, asset: str) -> Decimal:
    if asset == 'USDT': return Decimal('1')
    symbol = asset + 'USDT'
    try:
        return Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
    except Exception:
        for bridge in ('BTC', 'ETH'):
            try:
                p1 = Decimal(client.get_symbol_ticker(symbol=asset + bridge)['price'])
                p2 = Decimal(client.get_symbol_ticker(symbol=bridge + 'USDT')['price'])
                return p1 * p2
            except Exception:
                continue
    logger.warning(f"No USDT price for {asset}")
    return Decimal('0')

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_all_nonzero_balances(client):
    try:
        account = client.get_account()
        return {b['asset']: float(b['free']) for b in account['balances'] if float(b['free']) > 0}
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return {}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def calculate_total_portfolio_value(client):
    balances = get_all_nonzero_balances(client)
    total_usdt = 0.0
    asset_values = {}
    for asset, qty in balances.items():
        if asset == 'USDT':
            total_usdt += qty
            asset_values[asset] = qty
            continue
        price = float(get_price_usdt(client, asset))
        value_usdt = qty * price
        total_usdt += value_usdt
        asset_values[asset] = value_usdt
    return total_usdt, asset_values

def print_coin_scanner(client, symbols):
    if not DEBUG_SHOW_API_COIN_DATA_FETCHING: return
    logger.debug("\n" + "-" * 120)
    logger.debug(f" LIVE COIN SCANNER - {now_cst()} - SCANNING {len(symbols)} PAIRS")
    logger.debug("-" * 120)
    logger.debug(f"{'SYMBOL':<10} {'PRICE':>12} {'RSI':>6} {'MACD':>8} {'BB_POS':>6} {'VOL_24H':>12} {'BUY?'}")
    logger.debug("-" * 120)
    for symbol in symbols:
        try:
            current = fetch_current_data(client, symbol)
            if not current: continue
            price = current['price']
            volume = current['volume_24h']
            if price < MIN_PRICE or price > MAX_PRICE or volume < VOLUME_THRESHOLD: continue
            metrics = get_historical_metrics(client, symbol)
            if not metrics or any(v is None for v in [metrics['rsi'], metrics['macd'], metrics['bb_upper'], metrics['bb_lower']]): continue
            rsi = metrics['rsi']
            macd = metrics['macd'] - metrics['signal']
            bb_pos = (price - metrics['bb_lower']) / (metrics['bb_upper'] - metrics['bb_lower']) * 100 if metrics['bb_upper'] != metrics['bb_lower'] else 50
            buy_signal = "YES" if check_buy_signal(client, symbol) else ""
            macd_str = f"{macd:+.4f}"
            bb_str = f"{bb_pos:5.1f}%"
            logger.debug(f"{symbol:<10} {price:>12.6f} {rsi:>5.1f} {macd_str:>8} {bb_str:>6} {volume:>12,.0f} {buy_signal}")
        except Exception as e:
            logger.debug(f"Scanner skip {symbol}: {e}")
    logger.debug("-" * 120 + "\n")

def calculate_rsi(prices, period=RSI_PERIOD):
    if len(prices) < period + 1: return None
    delta = np.diff(prices)
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_symbol_filters(client, symbol):
    try:
        info = client.get_exchange_info()
        symbol_info = next(s for s in info['symbols'] if s['symbol'] == symbol)
        filters = {}
        for f in symbol_info['filters']:
            ftype = f['filterType']
            params = {}
            if ftype == 'LOT_SIZE':
                params = {k: float(f.get(k)) for k in ['minQty', 'maxQty', 'stepSize']}
            elif ftype == 'MIN_NOTIONAL':
                params = {'minNotional': float(f.get('minNotional'))}
            elif ftype == 'PRICE_FILTER':
                params = {k: float(f.get(k)) for k in ['minPrice', 'maxPrice', 'tickSize']}
            filters[ftype] = params
        return filters
    except Exception as e:
        logger.error(f"Failed to get filters for {symbol}: {e}")
        return {}

def round_to_step_size(value, step_size):
    if value == 0 or step_size == 0: return 0
    return round(value / step_size) * step_size

def round_to_tick_size(value, tick_size):
    if value == 0 or tick_size == 0: return 0
    return round(value / tick_size) * tick_size

def validate_and_adjust_order(client, symbol, side, order_type, quantity, price=None, current_price=None):
    filters = get_symbol_filters(client, symbol)
    adjusted_qty = quantity
    adjusted_price = price
    lot_filter = filters.get('LOT_SIZE', {})
    if lot_filter:
        step_size = lot_filter.get('stepSize', 0)
        adjusted_qty = round_to_step_size(adjusted_qty, step_size)
        min_qty = lot_filter.get('minQty', 0)
        max_qty = lot_filter.get('maxQty', float('inf'))
        if adjusted_qty < min_qty: adjusted_qty = min_qty
        if adjusted_qty > max_qty: adjusted_qty = max_qty
        if adjusted_qty < min_qty: return None, f"Qty below minQty: {adjusted_qty}"
    if adjusted_price is not None:
        price_filter = filters.get('PRICE_FILTER', {})
        if price_filter:
            tick_size = price_filter.get('tickSize', 0)
            adjusted_price = round_to_tick_size(adjusted_price, tick_size)
            min_price = price_filter.get('minPrice', 0)
            max_price = price_filter.get('maxPrice', float('inf'))
            if adjusted_price < min_price: adjusted_price = min_price
            if adjusted_price > max_price: adjusted_price = max_price
            if adjusted_price < min_price: return None, f"Price below minPrice: {adjusted_price}"
    min_notional = filters.get('MIN_NOTIONAL', {}).get('minNotional', 0)
    if min_notional > 0:
        effective_price = adjusted_price or current_price
        if effective_price:
            notional = adjusted_qty * effective_price
            if notional < min_notional:
                needed_qty = min_notional / effective_price
                adjusted_qty = max(adjusted_qty, needed_qty)
                if lot_filter:
                    adjusted_qty = round_to_step_size(adjusted_qty, lot_filter.get('stepSize', 0))
                if adjusted_qty * effective_price < min_notional:
                    return None, f"Cannot meet MIN_NOTIONAL: {adjusted_qty * effective_price}"
    return {'quantity': adjusted_qty, 'price': adjusted_price}, None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_momentum_status(client, symbol):
    try:
        klines = client.get_klines(symbol=symbol, interval='1d', limit=10)
        if len(klines) < 3: return "sideways"
        opens = np.array([float(k[1]) for k in klines])
        highs = np.array([float(k[2]) for k in klines])
        lows = np.array([float(k[3]) for k in klines])
        closes = np.array([float(k[4]) for k in klines])
        bullish_score = sum([
            np.any(talib.CDLHAMMER(opens, highs, lows, closes) > 0),
            np.any(talib.CDLENGULFING(opens, highs, lows, closes) > 0),
            np.any(talib.CDLMORNINGSTAR(opens, highs, lows, closes) > 0)
        ])
        bearish_score = sum([
            np.any(talib.CDLSHOOTINGSTAR(opens, highs, lows, closes) > 0),
            np.any(talib.CDLENGULFING(opens, highs, lows, closes) < 0),
            np.any(talib.CDLEVENINGSTAR(opens, highs, lows, closes) > 0)
        ])
        return "bullish" if bullish_score > bearish_score else "bearish" if bearish_score > bullish_score else "sideways"
    except Exception as e:
        logger.error(f"Momentum failed {symbol}: {e}")
        return "sideways"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_volume_24h(client, symbol):
    try:
        ticker = client.get_ticker(symbol=symbol)
        return float(ticker.get('quoteVolume', 0))
    except Exception as e:
        logger.error(f"Volume failed {symbol}: {e}")
        return 0

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_trade_fees(client, symbol):
    try:
        fee_info = client.get_trade_fee(symbol=symbol)
        return float(fee_info[0]['makerCommission']), float(fee_info[0]['takerCommission'])
    except Exception:
        return 0.001, 0.001

def send_whatsapp_alert(message: str):
    if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE: return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(message)}&apikey={CALLMEBOT_API_KEY}"
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            logger.info(f"WhatsApp: {message[:50]}...")
    except Exception as e:
        logger.error(f"Alert failed: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_balance(client, asset='USDT'):
    try:
        for bal in client.get_account()['balances']:
            if bal['asset'] == asset:
                return float(bal['free'])
        return 0.0
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return 0.0

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_historical_metrics(client, symbol):
    try:
        end_time = datetime.now(CST_TZ)
        start_time = end_time - timedelta(days=HISTORY_DAYS)
        start_ms = int(start_time.timestamp() * 1000)
        klines = client.get_historical_klines(symbol, KLINE_INTERVAL, start_ms)
        if len(klines) < max(24, BB_PERIOD, ATR_PERIOD, SMA_PERIOD): return None
        opens = np.array([float(k[1]) for k in klines])
        highs = np.array([float(k[2]) for k in klines])
        lows = np.array([float(k[3]) for k in klines])
        closes = np.array([float(k[4]) for k in klines])
        volumes = np.array([float(k[5]) for k in klines])
        fifteen_low = np.min(lows)
        avg_volume = np.mean(volumes)
        rsi = calculate_rsi(closes)
        roc_5d = (closes[-1] - closes[-120]) / closes[-120] * 100 if len(closes) > 120 else 0
        max_dd = ((closes - np.maximum.accumulate(closes)) / np.maximum.accumulate(closes) * 100).min()
        macd, signal, hist = talib.MACD(closes, fastperiod=12, slowperiod=26, signalperiod=9)
        macd_val = macd[-1] if len(macd) > 0 else 0
        signal_val = signal[-1] if len(signal) > 0 else 0
        hist_val = hist[-1] if len(hist) > 0 else 0
        mfi = talib.MFI(highs, lows, closes, volumes, timeperiod=14)[-1] if len(highs) >= 14 else None
        upper, middle, lower = talib.BBANDS(closes, timeperiod=BB_PERIOD, nbdevup=BB_DEV, nbdevdn=BB_DEV)
        bb_upper = upper[-1] if len(upper) > 0 else None
        bb_middle = middle[-1] if len(middle) > 0 else None
        bb_lower = lower[-1] if len(lower) > 0 else None
        slowk, slowd = talib.STOCH(highs, lows, closes, fastk_period=5, slowk_period=3, slowd_period=3)
        stoch_k = slowk[-1] if len(slowk) > 0 else None
        stoch_d = slowd[-1] if len(slowd) > 0 else None
        atr = talib.ATR(highs, lows, closes, timeperiod=ATR_PERIOD)[-1] if len(highs) >= ATR_PERIOD else None
        sma = talib.SMA(closes, timeperiod=SMA_PERIOD)[-1] if len(closes) >= SMA_PERIOD else None
        ema = talib.EMA(closes, timeperiod=EMA_PERIOD)[-1] if len(closes) >= EMA_PERIOD else None
        return {
            'fifteen_low': fifteen_low, 'avg_volume': avg_volume, 'rsi': rsi, 'roc_5d': roc_5d,
            'max_dd': max_dd, 'current_price': closes[-1], 'macd': macd_val, 'signal': signal_val,
            'hist': hist_val, 'mfi': mfi, 'bb_upper': bb_upper, 'bb_middle': bb_middle,
            'bb_lower': bb_lower, 'stoch_k': stoch_k, 'stoch_d': stoch_d, 'atr': atr,
            'sma': sma, 'ema': ema, 'opens': opens, 'highs': highs, 'lows': lows, 'closes': closes
        }
    except Exception as e:
        logger.error(f"History failed {symbol}: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def fetch_current_data(client, symbol):
    try:
        ticker = client.get_ticker(symbol=symbol)
        return {
            'price': float(ticker['lastPrice']),
            'volume_24h': float(ticker['quoteVolume']),
            'price_change_pct': float(ticker['priceChangePercent'])
        }
    except Exception as e:
        logger.error(f"Current data failed {symbol}: {e}")
        return None

def is_bullish_candlestick_pattern(metrics):
    if not metrics or len(metrics['opens']) < 3: return False
    opens = metrics['opens'][-3:]
    highs = metrics['highs'][-3:]
    lows = metrics['lows'][-3:]
    closes = metrics['closes'][-3:]
    patterns = [
        talib.CDLHAMMER(opens, highs, lows, closes)[-1] > 0,
        talib.CDLINVERTEDHAMMER(opens, highs, lows, closes)[-1] > 0,
        talib.CDLENGULFING(opens, highs, lows, closes)[-1] > 0,
        talib.CDLMORNINGSTAR(opens, highs, lows, closes)[-1] > 0,
        talib.CDL3WHITESOLDIERS(opens, highs, lows, closes)[-1] > 0,
    ]
    return any(patterns)

def is_bearish_candlestick_pattern(metrics):
    if not metrics or len(metrics['opens']) < 3: return False
    opens = metrics['opens'][-3:]
    highs = metrics['highs'][-3:]
    lows = metrics['lows'][-3:]
    closes = metrics['closes'][-3:]
    patterns = [
        talib.CDLSHOOTINGSTAR(opens, highs, lows, closes)[-1] > 0,
        talib.CDLHANGINGMAN(opens, highs, lows, closes)[-1] > 0,
        talib.CDLENGULFING(opens, highs, lows, closes)[-1] < 0,
    ]
    return any(patterns)

def check_buy_signal(client, symbol):
    with DBManager() as sess:
        if sess.query(Position).filter_by(symbol=symbol).first():
            return False
    metrics = get_historical_metrics(client, symbol)
    if not metrics or any(v is None for v in [metrics['rsi'], metrics['mfi'], metrics['bb_lower'], metrics['stoch_k'], metrics['stoch_d'], metrics['atr'], metrics['sma'], metrics['ema']]):
        return False
    current = fetch_current_data(client, symbol)
    if not current: return False
    if not (MIN_PRICE <= current['price'] <= MAX_PRICE) or current['volume_24h'] < VOLUME_THRESHOLD: return False
    if metrics['rsi'] <= 50 or metrics['mfi'] > 70 or metrics['macd'] <= metrics['signal'] or metrics['hist'] <= 0: return False
    if get_momentum_status(client, symbol) != 'bullish': return False
    if current['price'] > metrics['bb_lower'] * 1.005 or metrics['stoch_k'] > 30 or metrics['ema'] <= metrics['sma']: return False
    if not is_bullish_candlestick_pattern(metrics): return False
    return True

def execute_buy(client, symbol, bot):
    if not check_buy_signal(client, symbol):
        return

    balance = get_balance(client)
    if balance <= MIN_BALANCE:
        send_whatsapp_alert("Low balance, skipping buy")
        return

    try:
        book_price = follow_price_to_extreme(client, symbol, side='buy')
    except Exception as e:
        logger.error(f"Depth follow failed (buy {symbol}): {e}")
        return

    metrics = get_historical_metrics(client, symbol)
    if not metrics or metrics['atr'] is None or metrics['bb_lower'] is None:
        logger.warning(f"Missing metrics for {symbol}, using raw book price")
        buy_price = book_price
    else:
        bb_lower = Decimal(str(metrics['bb_lower']))
        atr = Decimal(str(metrics['atr']))
        if book_price > bb_lower + atr * Decimal('1.5'):
            logger.info(f"{symbol} book price too far above BB lower, skipping")
            return
        buy_price = book_price * (Decimal('1') - Decimal('0.0001'))

    alloc = min((balance - MIN_BALANCE) * RISK_PER_TRADE, balance - MIN_BALANCE)
    qty = alloc / buy_price

    adjusted, error = validate_and_adjust_order(
        client, symbol, 'BUY', ORDER_TYPE_LIMIT, qty, buy_price, float(book_price)
    )
    if error or not adjusted:
        logger.warning(f"Buy validation failed {symbol}: {error}")
        return

    bot.place_limit_buy_with_tracking(symbol, adjusted['price'], adjusted['quantity'])
    logger.info(f"BUY {symbol} @ {adjusted['price']} qty {adjusted['quantity']} (book-guided)")
    send_whatsapp_alert(f"BUY {symbol} @ {adjusted['price']:.6f}")

def check_sell_signal(client, symbol, position):
    current = fetch_current_data(client, symbol)
    if not current: return False, None, None
    current_price = Decimal(str(current['price']))
    maker_fee, taker_fee = get_trade_fees(client, symbol)
    buy_fee = position.buy_fee_rate

    profit_pct = float(
        (current_price - position.avg_entry_price) / position.avg_entry_price
        - (buy_fee + Decimal(str(taker_fee)))
    )
    if profit_pct < PROFIT_TARGET:
        return False, None, None

    metrics = get_historical_metrics(client, symbol)
    if not metrics: return False, None, None

    if (is_bearish_candlestick_pattern(metrics) or
        current_price >= Decimal(str(metrics['bb_upper'])) * Decimal('0.995')):
        return True, 'market', taker_fee

    return True, 'limit', maker_fee

def execute_sell(client, symbol, position, bot):
    current = fetch_current_data(client, symbol)
    if not current:
        return
    cur_price = Decimal(str(current['price']))
    maker_fee, taker_fee = get_trade_fees(client, symbol)
    profit_pct = float(
        (cur_price - position.avg_entry_price) / position.avg_entry_price
        - (position.buy_fee_rate + Decimal(str(taker_fee)))
    )
    if profit_pct < PROFIT_TARGET:
        return

    try:
        book_price = follow_price_to_extreme(client, symbol, side='sell')
    except Exception as e:
        logger.error(f"Depth follow failed (sell {symbol}): {e}")
        return

    metrics = get_historical_metrics(client, symbol)
    if metrics and metrics['bb_upper'] is not None:
        bb_upper = Decimal(str(metrics['bb_upper']))
        atr = Decimal(str(metrics['atr'])) if metrics['atr'] else Decimal('0')
        if book_price < bb_upper - atr * Decimal('1.5'):
            logger.info(f"{symbol} book price too far below BB upper, checking for market sell")
            if (is_bearish_candlestick_pattern(metrics) or
                cur_price >= bb_upper * Decimal('0.995')):
                order = place_market_sell(client, symbol, position.quantity)
                if order:
                    fills = order.get('fills', [])
                    total_qty = sum(Decimal(f['qty']) for f in fills)
                    total_val = sum(Decimal(f['price']) * Decimal(f['qty']) for f in fills)
                    exit_price = total_val / total_qty if total_qty > 0 else position.avg_entry_price
                    profit = (exit_price - position.avg_entry_price) * position.quantity
                    with DBManager() as sess:
                        bot.record_trade(sess, symbol, 'sell', exit_price, position.quantity, str(order['orderId']))
                        sess.delete(position)
                    send_whatsapp_alert(f"MARKET SOLD {symbol} @ {exit_price:.6f} Profit ${profit:,.2f}")
                return
            else:
                return

    sell_price = book_price * (Decimal('1') + Decimal('0.0001'))

    adjusted, error = validate_and_adjust_order(
        client, symbol, 'SELL', ORDER_TYPE_LIMIT, position.quantity, sell_price, float(cur_price)
    )
    if error or not adjusted:
        logger.warning(f"Sell validation failed {symbol}: {error}")
        return

    bot.place_limit_sell_with_tracking(symbol, adjusted['price'], adjusted['quantity'])
    logger.info(f"SELL {symbol} @ {adjusted['price']} qty {adjusted['quantity']} (book-guided)")
    send_whatsapp_alert(f"SELL {symbol} @ {adjusted['price']:.6f}")

def get_all_usdt_symbols(client):
    try:
        info = client.get_exchange_info()
        return [s['symbol'] for s in info['symbols'] if s['symbol'].endswith('USDT') and s['status'] == 'TRADING']
    except Exception as e:
        logger.error(f"Symbols fetch failed: {e}")
        return []

def place_market_sell(client, symbol, qty):
    adjusted, error = validate_and_adjust_order(client, symbol, 'SELL', ORDER_TYPE_MARKET, qty)
    if error: logger.error(f"MARKET SELL failed {symbol}: {error}"); return None
    try:
        order = client.order_market_sell(symbol=symbol, quantity=adjusted['quantity'])
        logger.info(f"Placed MARKET SELL {symbol} qty {adjusted['quantity']}")
        return order
    except BinanceOrderException as e:
        logger.error(f"MARKET SELL failed {symbol}: {e}")
        return None

# === MAIN ===
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        logger.error("API keys missing")
        exit(1)

    bot = BinanceTradingBot()
    bot.run()
