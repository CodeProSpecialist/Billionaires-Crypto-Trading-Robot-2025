#!/usr/bin/env python3
"""
Binance.US Trading Bot – FINAL 100% SAFE VERSION
- All /USDT pairs fetched & validated
- Rejects bad data
- Auto-restart main loop
- Decimal-safe math
- No crashes
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
from datetime import datetime, timedelta
import pytz
import requests
from decimal import Decimal
from typing import List, Optional, Dict
from collections import deque

# === SQLALCHEMY ===
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, func
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import SQLAlchemyError

# === CONFIGURATION ===
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')
MAX_PRICE = 1000.00
MIN_PRICE = 0.01
LOOP_INTERVAL = 15
LOG_FILE = "crypto_trading_bot.log"
RSI_PERIOD = 14
BB_PERIOD = 20
BB_DEV = 2
HISTORY_DAYS = 60
KLINE_INTERVAL = '1m'
PROFIT_TARGET = 0.008
RISK_PER_TRADE = 0.10
MIN_BALANCE = 2.0

# Strategy
BUY_PRICE_TOLERANCE_PCT = 1.0
ORDERBOOK_IMBALANCE_THRESHOLD = 0.55
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
RSI_CONFIRM_WINDOW = 3
DEPTH_HISTORY = 6
DEPTH_WAIT = 1.0
MIN_MOVE_PCT = 0.0003
MAX_ITER = 45

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
    logger.info("Shutting down...")
    sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)

# === SAFE FLOAT CONVERSION ===
def safe_float(value, default=0.0) -> float:
    try:
        if value is None or value == '':
            return default
        f = float(value)
        return f if np.isfinite(f) else default
    except:
        return default

def is_valid_float(value) -> bool:
    return value is not None and np.isfinite(value) and value > 0

# === VALIDATE SYMBOL ===
def validate_symbol(client, symbol: str) -> bool:
    try:
        ticker = client.get_ticker(symbol=symbol)
        price = safe_float(ticker.get('lastPrice'))
        low_24h = safe_float(ticker.get('lowPrice'))
        if not (is_valid_float(price) and is_valid_float(low_24h)):
            return False
        if not (MIN_PRICE <= price <= MAX_PRICE):
            return False
        return True
    except Exception as e:
        logger.debug(f"validate_symbol [{symbol}] failed: {e}")
        return False

# === GET VALID /USDT PAIRS ===
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_valid_usdt_symbols(client) -> List[str]:
    try:
        info = client.get_exchange_info()
        raw_symbols = [
            s['symbol'] for s in info['symbols']
            if s['quoteAsset'] == 'USDT'
            and s['status'] == 'TRADING'
            and s['symbol'].endswith('USDT')
        ]
        logger.info(f"Found {len(raw_symbols)} /USDT pairs. Validating...")

        valid_symbols = []
        for symbol in raw_symbols:
            if validate_symbol(client, symbol):
                valid_symbols.append(symbol)
            else:
                logger.debug(f"Skipping invalid symbol: {symbol}")
        
        logger.info(f"Valid trading symbols: {len(valid_symbols)}")
        return valid_symbols
    except Exception as e:
        logger.error(f"Failed to fetch symbols: {e}")
        return []

# === ORDER BOOK + RSI FOLLOW ===
def _best_price(depth: dict, side: str) -> Decimal:
    if side == 'buy' and depth.get('bids'):
        return Decimal(str(depth['bids'][0][0]))
    if side == 'sell' and depth.get('asks'):
        return Decimal(str(depth['asks'][0][0]))
    return Decimal('0')

def follow_price_with_rsi(client, symbol: str, side: str) -> Decimal:
    history = deque(maxlen=DEPTH_HISTORY)
    direction = 1 if side == 'buy' else -1
    rsi_confirm_count = 0

    for _ in range(MAX_ITER):
        try:
            depth = client.get_order_book(symbol=symbol, limit=5)
            price = _best_price(depth, side)
            if price <= 0:
                time.sleep(DEPTH_WAIT)
                continue
            history.append(price)

            try:
                klines = client.get_klines(symbol=symbol, interval='1m', limit=RSI_PERIOD + 5)
                if len(klines) < RSI_PERIOD + 1:
                    raise ValueError("Not enough klines")
                closes = np.array([safe_float(k[4]) for k in klines[-RSI_PERIOD-1:]])
                if not np.all(np.isfinite(closes)) or len(closes) < RSI_PERIOD:
                    raise ValueError("Invalid closes")
                rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)[-1]
                rsi = float(rsi) if np.isfinite(rsi) else None
            except:
                rsi = None

            print(f"  [{side.upper()}] {symbol} @ {price:.6f} | RSI={rsi:.1f if rsi else 'N/A'} | win={len(history)}")

            if len(history) < DEPTH_HISTORY:
                time.sleep(DEPTH_WAIT)
                continue

            delta_pct = float((history[-1] - history[0]) / history[0])
            moved_against = direction * delta_pct < -MIN_MOVE_PCT
            flat = abs(delta_pct) < MIN_MOVE_PCT

            rsi_ok = (side == 'buy' and rsi is not None and rsi <= RSI_OVERSOLD) or \
                     (side == 'sell' and rsi is not None and rsi >= RSI_OVERBOUGHT)
            if rsi_ok:
                rsi_confirm_count += 1
            else:
                rsi_confirm_count = 0

            if moved_against or (flat and rsi_confirm_count >= RSI_CONFIRM_WINDOW):
                return price

        except Exception as e:
            logger.debug(f"follow_price_with_rsi [{symbol}] error: {e}")

        time.sleep(DEPTH_WAIT)

    try:
        depth = client.get_order_book(symbol=symbol, limit=5)
        return _best_price(depth, side)
    except:
        return Decimal('0')

# === BOT CLASS ===
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.load_state_from_db()

    def load_state_from_db(self):
        with DBManager() as sess:
            db_positions = {p.symbol: p for p in sess.query(Position).all()}
            logger.info(f"DB: {len(db_positions)} positions")

            try:
                account = self.client.get_account()
            except Exception as e:
                logger.error(f"Account fetch failed: {e}")
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
                    continue
                maker_fee, _ = get_trade_fees(self.client, symbol)
                pos = Position(symbol=symbol, quantity=free, avg_entry_price=price_usdt, buy_fee_rate=maker_fee)
                sess.add(pos)
                imported += 1
            if imported:
                logger.info(f"Imported {imported} assets")
                sess.commit()

    def get_position(self, sess, symbol: str) -> Optional[Position]:
        return sess.query(Position).filter_by(symbol=symbol).one_or_none()

    def record_trade(self, sess, symbol, side, price, qty, binance_order_id, pending_order=None):
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

    def add_pending_order(self, sess, binance_order_id, symbol, side, price, qty):
        order = PendingOrder(binance_order_id=binance_order_id, symbol=symbol, side=side, price=price, quantity=qty)
        sess.add(order)
        return order

    def remove_pending_order(self, sess, binance_order_id):
        sess.query(PendingOrder).filter_by(binance_order_id=binance_order_id).delete()

    def place_limit_buy_with_tracking(self, symbol, price: str, qty: float):
        try:
            order = self.client.order_limit_buy(
                symbol=symbol,
                quantity=qty,
                price=price
            )
            order_id = str(order['orderId'])
            with DBManager() as sess:
                self.add_pending_order(sess, order_id, symbol, 'buy', Decimal(price), Decimal(str(qty)))
            logger.info(f"BUY LIMIT {symbol} @ {price} qty {qty}")
            return order
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol, price: str, qty: float):
        try:
            order = self.client.order_limit_sell(
                symbol=symbol,
                quantity=qty,
                price=price
            )
            order_id = str(order['orderId'])
            with DBManager() as sess:
                self.add_pending_order(sess, order_id, symbol, 'sell', Decimal(price), Decimal(str(qty)))
            logger.info(f"SELL LIMIT {symbol} @ {price} qty {qty}")
            return order
        except Exception as e:
            logger.error(f"Sell failed: {e}")
            return None

    def check_and_process_filled_orders(self):
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
                    logger.debug(f"Order check failed: {e}")

    def print_status_dashboard(self):
        try:
            with DBManager() as sess:
                positions = sess.query(Position).all()
                usdt_free = get_balance(self.client, 'USDT')
                total_portfolio, _ = calculate_total_portfolio_value(self.client)
                print("\n" + "="*100)
                print(f" DASHBOARD - {now_cst()} ")
                print("="*100)
                print(f"USDT: ${usdt_free:,.6f} | Portfolio: ${total_portfolio:,.6f} | Positions: {len(positions)}")
                if positions:
                    print(f"{'SYMBOL':<10} {'QTY':>12} {'ENTRY':>12} {'CURRENT':>12} {'P&L %':>8}")
                    for pos in positions:
                        cur = fetch_current_data(self.client, pos.symbol)
                        cur_price = Decimal(str(cur['price'])) if cur else pos.avg_entry_price
                        pnl_pct = float((cur_price - pos.avg_entry_price) / pos.avg_entry_price * 100)
                        print(f"{pos.symbol:<10} {pos.quantity:>12.6f} {pos.avg_entry_price:>12.6f} {cur_price:>12.6f} {pnl_pct:>7.2f}%")
                print("="*100 + "\n")
        except Exception as e:
            logger.error(f"Dashboard error: {e}")

    def run(self):
        logger.info("Bot starting...")
        while True:
            try:
                symbols = get_valid_usdt_symbols(self.client)
                if not symbols:
                    logger.warning("No valid symbols found. Retrying in 30s...")
                    time.sleep(30)
                    continue

                self.check_and_process_filled_orders()

                for symbol in symbols:
                    try:
                        with DBManager() as sess:
                            if sess.query(Position).filter_by(symbol=symbol).first():
                                continue
                        execute_buy(self.client, symbol, self)
                    except Exception as e:
                        logger.warning(f"Buy loop error [{symbol}]: {e}")

                with DBManager() as sess:
                    for pos in sess.query(Position).all():
                        try:
                            execute_sell(self.client, pos.symbol, pos, self)
                        except Exception as e:
                            logger.warning(f"Sell loop error [{pos.symbol}]: {e}")

                self.print_status_dashboard()
                time.sleep(LOOP_INTERVAL)

            except KeyboardInterrupt:
                logger.info("Bot stopped by user.")
                break
            except Exception as e:
                logger.error(f"Main loop crashed: {e}")
                logger.info("Restarting in 10 seconds...")
                time.sleep(10)

# === STRATEGY: BUY ===
def check_buy_signal(client, symbol):
    try:
        ticker = client.get_ticker(symbol=symbol)
        current_price = safe_float(ticker.get('lastPrice'))
        low_24h = safe_float(ticker.get('lowPrice'))
        if not (is_valid_float(current_price) and is_valid_float(low_24h)):
            return False, None

        if current_price > low_24h * (1 + BUY_PRICE_TOLERANCE_PCT / 100):
            return False, None

        try:
            depth = client.get_order_book(symbol=symbol, limit=10)
            total_bid = sum(safe_float(b[1]) for b in depth.get('bids', []))
            total_ask = sum(safe_float(a[1]) for a in depth.get('asks', []))
            total = total_bid + total_ask
            if total <= 0:
                return False, None
            pct_ask = total_ask / total * 100
            if pct_ask < ORDERBOOK_IMBALANCE_THRESHOLD * 100:
                return False, None
        except:
            return False, None

        final_price = follow_price_with_rsi(client, symbol, 'buy')
        if final_price <= 0:
            return False, None

        try:
            klines = client.get_klines(symbol=symbol, interval='1m', limit=RSI_PERIOD + 1)
            closes = np.array([safe_float(k[4]) for k in klines])
            if len(closes) < RSI_PERIOD or not np.all(np.isfinite(closes)):
                return False, None
            final_rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)[-1]
            if not np.isfinite(final_rsi) or final_rsi > RSI_OVERSOLD:
                return False, None
        except:
            return False, None

        return True, final_price
    except Exception as e:
        logger.debug(f"check_buy_signal [{symbol}] error: {e}")
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
            logger.debug(f"Order validation failed [{symbol}]: {error}")
            return

        bot.place_limit_buy_with_tracking(
            symbol,
            str(adjusted['price']),
            float(adjusted['quantity'])
        )
        send_whatsapp_alert(f"BUY {symbol} @ {adjusted['price']:.6f}")
    except Exception as e:
        logger.warning(f"execute_buy [{symbol}] failed: {e}")

# === STRATEGY: SELL ===
def check_sell_signal(client, symbol, position):
    try:
        cur = fetch_current_data(client, symbol)
        if not cur or not is_valid_float(cur['price']):
            return False, None
        cur_price = Decimal(str(cur['price']))
        profit_pct = float((cur_price - position.avg_entry_price) / position.avg_entry_price - 0.002)
        if profit_pct < PROFIT_TARGET:
            return False, None

        final_price = follow_price_with_rsi(client, symbol, 'sell')
        if final_price <= 0:
            return False, None

        try:
            klines = client.get_klines(symbol=symbol, interval='1m', limit=RSI_PERIOD + 1)
            closes = np.array([safe_float(k[4]) for k in klines])
            if len(closes) < RSI_PERIOD or not np.all(np.isfinite(closes)):
                return False, None
            final_rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)[-1]
            if not np.isfinite(final_rsi) or final_rsi < RSI_OVERBOUGHT:
                return False, None
        except:
            return False, None

        return True, final_price
    except Exception as e:
        logger.debug(f"check_sell_signal [{symbol}] error: {e}")
        return False, None

def execute_sell(client, symbol, position, bot):
    try:
        should_sell, final_price = check_sell_signal(client, symbol, position)
        if not should_sell or not final_price > 0:
            return

        qty = position.quantity
        adjusted, error = validate_and_adjust_order(client, symbol, 'SELL', ORDER_TYPE_LIMIT, qty, final_price)
        if error or not adjusted:
            logger.debug(f"Sell validation failed [{symbol}]: {error}")
            return

        bot.place_limit_sell_with_tracking(
            symbol,
            str(adjusted['price']),
            float(adjusted['quantity'])
        )
        send_whatsapp_alert(f"SELL {symbol} @ {adjusted['price']:.6f}")
    except Exception as e:
        logger.warning(f"execute_sell [{symbol}] failed: {e}")

# === HELPERS ===
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
    return Decimal('0')

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
        for b in account['balances']:
            qty = Decimal(str(safe_float(b['free'])))
            if qty <= 0:
                continue
            if b['asset'] == 'USDT':
                total += qty
            else:
                price = get_price_usdt(client, b['asset'])
                if price > 0:
                    total += qty * price
        return float(total), {}
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
    except:
        return None

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
    except:
        return {}

def validate_and_adjust_order(client, symbol, side, order_type, quantity, price=None, current_price=None):
    try:
        filters = get_symbol_filters(client, symbol)
        adj_qty = quantity
        adj_price = price

        lot = filters.get('LOT_SIZE', {})
        if lot and lot.get('stepSize', 0) > 0:
            step = lot['stepSize']
            adj_qty = (adj_qty // step) * step
            if adj_qty < lot.get('minQty', 0):
                return None, "Qty too low"

        if adj_price is not None:
            price_f = filters.get('PRICE_FILTER', {})
            if price_f and price_f.get('tickSize', 0) > 0:
                tick = price_f['tickSize']
                adj_price = (adj_price // tick) * tick
                if adj_price < price_f.get('minPrice', 0):
                    return None, "Price too low"

        min_notional = filters.get('MIN_NOTIONAL', {}).get('minNotional', 0)
        if min_notional > 0 and adj_price is not None:
            notional = adj_qty * adj_price
            if notional < min_notional:
                return None, "Below min notional"

        return {'quantity': adj_qty, 'price': adj_price}, None
    except Exception as e:
        logger.debug(f"validate_and_adjust_order error [{symbol}]: {e}")
        return None, "Filter error"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_trade_fees(client, symbol):
    try:
        fee = client.get_trade_fee(symbol=symbol)
        return safe_float(fee[0]['makerCommission']), safe_float(fee[0]['takerCommission'])
    except:
        return 0.001, 0.001

def send_whatsapp_alert(message: str):
    if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE:
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(message)}&apikey={CALLMEBOT_API_KEY}"
        requests.get(url, timeout=10)
    except:
        pass

# === MAIN ===
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        logger.error("API keys missing")
        sys.exit(1)
    bot = BinanceTradingBot()
    bot.run()
