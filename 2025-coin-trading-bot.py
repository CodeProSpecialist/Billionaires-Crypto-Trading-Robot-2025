#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BINANCE.US LIVE TRADING BOT – FULL SQLALCHEMY + DEBUG LOG + DASHBOARD
"""

import os
import sys
import time
import logging
import numpy as np
import talib
import traceback
import requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from collections import deque
import threading
import pytz
from logging.handlers import TimedRotatingFileHandler

# Binance
from binance.client import Client
from binance.exceptions import BinanceAPIException

# SQLAlchemy
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, func
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
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
DEPTH_IMBALANCE_THRESHOLD = 2.0
POLL_INTERVAL = 1.0
STALL_THRESHOLD_SECONDS = 15 * 60
RAPID_DROP_THRESHOLD = 0.01
RAPID_DROP_WINDOW = 5.0

# === LOGGING ================================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Main log (INFO+)
main_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7)
main_handler.setLevel(logging.INFO)
main_formatter = logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s')
main_handler.setFormatter(main_formatter)

# Debug log (DEBUG+)
debug_handler = TimedRotatingFileHandler(DEBUG_LOG_FILE, when="midnight", interval=1, backupCount=14)
debug_handler.setLevel(logging.DEBUG)
debug_formatter = logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s')
debug_handler.setFormatter(debug_formatter)

# Console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(asctime)s %(levelname)s:%(message)s')
console_handler.setFormatter(console_formatter)

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
            logger.debug(f"DB rollback: {exc_val}")
        else:
            try:
                self.session.commit()
            except SQLAlchemyError as e:
                self.session.rollback()
                logger.error(f"DB commit failed: {e}")
        self.session.close()

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

# === SAFE MATH ==============================================================
def safe_float(value, default=0.0) -> float:
    try: return float(value) if value is not None and np.isfinite(float(value)) else default
    except: return default

def to_decimal(value) -> Decimal:
    try: return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except: return Decimal('0')

# === RATE MANAGER ===========================================================
class RateManager:
    def __init__(self, client):
        self.client = client
        self.limits = {'REQUEST_WEIGHT': {'MINUTE_1': 1200}, 'ORDERS': {'SECOND_10': 50, 'DAY_1': 160000}}
        self.current = {'request_weight': 0, 'orders_10s': 0, 'orders_1d': 0}
        self.last_update = 0

    def update_current(self):
        if not hasattr(self.client, 'response') or not self.client.response: return
        hdr = self.client.response.headers
        if 'x-mbx-used-weight-1m' in hdr:
            self.current['request_weight'] = int(hdr['x-mbx-used-weight-1m'])
        if 'x-mbx-order-count-10s' in hdr:
            self.current['orders_10s'] = int(hdr['x-mbx-order-count-10s'])
        if 'x-mbx-order-count-1d' in hdr:
            self.current['orders_1d'] = int(hdr['x-mbx-order-count-1d'])

    def wait_if_needed(self, typ='REQUEST_WEIGHT'):
        self.update_current()
        if typ == 'REQUEST_WEIGHT' and self.current['request_weight'] >= 1100:
            time.sleep(10)
        if typ == 'ORDERS' and self.current['orders_10s'] >= 45:
            time.sleep(5)

# === BOT CLASS ==============================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.client.API_URL = 'https://api.binance.us/api'
        self.rate_manager = RateManager(self.client)
        self.api_lock = threading.Lock()
        self.state_lock = threading.Lock()
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

    def fetch_and_validate_usdt_pairs(self) -> Dict[str, dict]:
        global valid_symbols_dict
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                info = self.client.get_exchange_info()
                self.rate_manager.update_current()
            raw = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
            valid = {}
            for sym in raw:
                try:
                    with self.api_lock:
                        self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                        ticker = self.client.get_ticker(symbol=sym)
                        self.rate_manager.update_current()
                    price = safe_float(ticker.get('lastPrice'))
                    vol = safe_float(ticker.get('quoteVolume'))
                    low = safe_float(ticker.get('lowPrice'))
                    if MIN_PRICE <= price <= MAX_PRICE and vol >= MIN_24H_VOLUME_USDT and low > 0:
                        valid[sym] = {'price': price, 'volume': vol, 'low_24h': low}
                except: continue
            with self.state_lock:
                valid_symbols_dict = valid
            logger.info(f"Valid symbols: {len(valid)}")
            return valid
        except Exception as e:
            logger.error(f"Symbol fetch error: {e}")
            return {}

    def get_order_book_analysis(self, symbol: str) -> dict:
        now = time.time()
        with self.state_lock:
            cached = order_book_cache.get(symbol)
            if cached and now - cached.get('ts', 0) < 1.0:
                return cached
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                depth = self.client.get_order_book(symbol=symbol, limit=ORDER_BOOK_LIMIT)
                self.rate_manager.update_current()
            bids = depth.get('bids', [])[:ORDER_BOOK_LIMIT]
            asks = depth.get('asks', [])[:ORDER_BOOK_LIMIT]
            top5_bids = bids[:5]
            top5_asks = asks[:5]
            top5_bid_vol = sum(Decimal(b[1]) for b in top5_bids)
            top5_ask_vol = sum(Decimal(a[1]) for a in top5_asks)
            top5_total = top5_bid_vol + top5_ask_vol or Decimal('1')

            cum_bid_vol = sum(Decimal(b[1]) for b in bids)
            cum_ask_vol = sum(Decimal(a[1]) for a in asks)
            bid_weighted = sum(Decimal(b[0]) * Decimal(b[1]) for b in bids)
            ask_weighted = sum(Decimal(a[0]) * Decimal(a[1]) for a in asks)
            bid_vwap = (bid_weighted / cum_bid_vol) if cum_bid_vol else Decimal('0')
            ask_vwap = (ask_weighted / cum_ask_vol) if cum_ask_vol else Decimal('0')
            mid_price = (bid_vwap + ask_vwap) / 2 if (bid_vwap and ask_vwap) else Decimal('0')

            imbalance_ratio = float(cum_bid_vol / cum_ask_vol) if cum_ask_vol else 999.0
            depth_skew = ('strong_bid' if imbalance_ratio >= DEPTH_IMBALANCE_THRESHOLD else
                          'strong_ask' if (1.0 / imbalance_ratio) >= DEPTH_IMBALANCE_THRESHOLD else 'balanced')
            weighted_pressure = float((bid_vwap - ask_vwap) / mid_price) if mid_price else 0.0

            result = {
                'pct_bid': float(top5_bid_vol / top5_total * 100),
                'pct_ask': float(top5_ask_vol / top5_total * 100),
                'best_bid': Decimal(bids[0][0]) if bids else Decimal('0'),
                'best_ask': Decimal(asks[0][0]) if asks else Decimal('0'),
                'imbalance_ratio': imbalance_ratio,
                'depth_skew': depth_skew,
                'weighted_pressure': weighted_pressure,
                'mid_price': float(mid_price),
                'ts': now
            }
            with self.state_lock:
                order_book_cache[symbol] = result
            return result
        except Exception as e:
            logger.debug(f"Order book error {symbol}: {e}")
            return {'best_bid': Decimal('0'), 'best_ask': Decimal('0'), 'pct_ask': 50.0}

    def get_rsi_and_trend(self, symbol) -> Tuple[Optional[float], str, Optional[float]]:
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                klines = self.client.get_klines(symbol=symbol, interval='1m', limit=100)
                self.rate_manager.update_current()
            closes = np.array([safe_float(k[4]) for k in klines[-100:]])
            rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)[-1]
            if not np.isfinite(rsi): return None, "unknown", None
            trend = "bullish" if closes[-1] > closes[-10] else "bearish"
            return float(rsi), trend, float(closes[-1])
        except Exception as e:
            logger.debug(f"RSI error {symbol}: {e}")
            return None, "unknown", None

    def get_24h_price_stats(self, symbol):
        with self.state_lock:
            hist = price_history.get(symbol, deque())
            if not hist: return None, None, None
            prices = [p[1] for p in hist]
            return min(prices), max(prices), sum(prices) / len(prices)

    def get_balance(self, asset='USDT') -> float:
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                bal = self.client.get_asset_balance(asset=asset)
                self.rate_manager.update_current()
            return safe_float(bal['free'])
        except Exception as e:
            logger.error(f"Balance error: {e}")
            return 0.0

    def get_trade_fees(self, symbol):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                fee = self.client.get_trade_fee(symbol=symbol)
                self.rate_manager.update_current()
            return safe_float(fee[0]['makerCommission']), safe_float(fee[0]['takerCommission'])
        except: return 0.001, 0.001

    def place_market_buy(self, symbol: str, usdt_amount: float):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.order_market_buy(symbol=symbol, quoteOrderQty=usdt_amount)
                self.rate_manager.update_current()
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='buy',
                                      price=Decimal('0'), quantity=Decimal(str(usdt_amount))))
            logger.info(f"MARKET BUY {symbol} ${usdt_amount:.2f}")
            send_whatsapp_alert(f"BUY {symbol} ${usdt_amount:.2f}")
            return order
        except Exception as e:
            logger.error(f"BUY FAILED {symbol}: {e}")
            return None

    def place_market_sell(self, symbol: str, qty: float):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.order_market_sell(symbol=symbol, quantity=qty)
                self.rate_manager.update_current()
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='sell',
                                      price=Decimal('0'), quantity=Decimal(str(qty))))
            logger.info(f"MARKET SELL {symbol} {qty:.6f}")
            send_whatsapp_alert(f"SELL {symbol} {qty:.6f}")
            return order
        except Exception as e:
            logger.error(f"SELL FAILED {symbol}: {e}")
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
                            self.record_trade(sess, po.symbol, po.side, fill_price, Decimal(o['executedQty']), po.binance_order_id, po)
                            sess.delete(po)
                            logger.info(f"{po.side.upper()} FILLED: {po.symbol} @ {fill_price}")
                    except Exception as e:
                        logger.debug(f"Order check error: {e}")
        except Exception as e:
            logger.error(f"Process filled orders error: {e}")

    def record_trade(self, sess, symbol, side, price, qty, binance_id, pending):
        trade = Trade(symbol=symbol, side=side, price=price, quantity=qty, binance_order_id=binance_id, pending_order=pending)
        sess.add(trade)
        pos = sess.query(Position).filter_by(symbol=symbol).one_or_none()
        if side == "buy":
            if not pos:
                maker, _ = self.get_trade_fees(symbol)
                pos = Position(symbol=symbol, quantity=qty, avg_entry_price=price, buy_fee_rate=maker)
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

# === SCANNERS ===============================================================
def buy_scanner(bot):
    while True:
        try:
            for sym in list(valid_symbols_dict.keys()):
                ob = bot.get_order_book_analysis(sym)
                rsi, trend, _ = bot.get_rsi_and_trend(sym)
                bid = ob['best_bid']
                if bid <= 0: continue
                custom_low, _, _ = bot.get_24h_price_stats(sym)
                if (rsi and rsi <= RSI_OVERSOLD and trend == 'bullish' and
                    custom_low and bid <= Decimal(str(custom_low)) * Decimal('1.01') and
                    ob['pct_ask'] >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100):
                    with bot.state_lock:
                        if sym not in trailing_buy_active:
                            trailing_buy_active[sym] = {'start_time': time.time()}
                            usdt = bot.get_balance() * RISK_PER_TRADE
                            if usdt > MIN_BALANCE:
                                bot.place_market_buy(sym, usdt)
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.critical(f"Buy scanner error: {e}", exc_info=True)
            time.sleep(10)

def sell_scanner(bot):
    while True:
        try:
            for sym in list(valid_symbols_dict.keys()):
                with DBManager() as sess:
                    pos = sess.query(Position).filter_by(symbol=sym).one_or_none()
                    if not pos: continue
                ob = bot.get_order_book_analysis(sym)
                ask = ob['best_ask']
                if ask <= 0: continue
                entry = Decimal(str(pos.avg_entry_price))
                maker, taker = bot.get_trade_fees(sym)
                net = (ask - entry) / entry - Decimal(str(maker)) - Decimal(str(taker))
                if net >= PROFIT_TARGET_NET:
                    with bot.state_lock:
                        if sym not in trailing_sell_active:
                            trailing_sell_active[sym] = {}
                            bot.place_market_sell(sym, float(pos.quantity))
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.critical(f"Sell scanner error: {e}", exc_info=True)
            time.sleep(10)

# === DASHBOARD ==============================================================
def print_professional_dashboard(bot):
    GREEN = "\033[32m"
    RED = "\033[31m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    DIVIDER = "=" * 120

    os.system('cls' if os.name == 'nt' else 'clear')
    print(DIVIDER)
    print(f"{BOLD}{'COIN TRADING BOT':^120}{RESET}")
    print(DIVIDER)

    now_str = datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    usdt_free = bot.get_balance('USDT')
    tbuy_cnt = len(trailing_buy_active)
    tsel_cnt = len(trailing_sell_active)

    print(f"Current Time: {now_str}")
    print(f"Available USDT: ${usdt_free:,.6f}")
    print(f"Active Trailing Orders: {tbuy_cnt} buys, {tsel_cnt} sells")
    print(DIVIDER)

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
        status = ("Trailing Sell" if sym in trailing_sell_active else "Trailing Buy" if sym in trailing_buy_active else "24/7 Monitoring")
        pnl_color = GREEN if net > 0 else RED
        pct_color = GREEN if pnl_pct > 0 else RED
        print(f"{sym:<10} {qty:>12.6f} {entry:>12.6f} {cur:>12.6f} {rsi_str} {pct_color}{pnl_pct:>7.2f}%{RESET} {pnl_color}{net:>9.2f}{RESET} {status:<30}")

    for _ in range(len(positions_list), 15):
        print(" " * 120)

    total_pnl_color = GREEN if total_pnl > 0 else RED
    print(DIVIDER)
    print(f"TOTAL UNREALIZED PROFIT & LOSS: {total_pnl_color}${float(total_pnl):>12,.2f}{RESET}")
    print(DIVIDER)

    print(f"{BOLD}{'MARKET OVERVIEW':^120}{RESET}")
    valid_cnt = len(valid_symbols_dict)
    print(f"Number of Valid Symbols: {valid_cnt}")
    print(f"Price Range: ${MIN_PRICE} to ${MAX_PRICE}")

    watch_items = []
    for sym in valid_symbols_dict:
        ob = bot.get_order_book_analysis(sym)
        rsi, trend, _ = bot.get_rsi_and_trend(sym)
        bid = ob['best_bid']
        if not bid: continue
        custom_low, _, _ = bot.get_24h_price_stats(sym)
        strong_buy = (ob['depth_skew'] == 'strong_ask' and ob['imbalance_ratio'] <= 0.5)
        if (rsi and rsi <= RSI_OVERSOLD and trend == 'bullish' and custom_low and bid <= Decimal(str(custom_low)) * Decimal('1.01') and strong_buy):
            coin = sym.replace('USDT', '')
            watch_items.append(f"{coin}({rsi:.0f})")
    watch_str = " | ".join(watch_items[:18]) if watch_items else "No active buy signals"
    print(f"Coin Buy List: {watch_str}")
    print(DIVIDER)

# === ALERTS =================================================================
def send_whatsapp_alert(message: str):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(message)}&apikey={CALLMEBOT_API_KEY}"
            requests.get(url, timeout=5)
        except Exception as e:
            logger.debug(f"WhatsApp failed: {e}")

# === MAIN ===================================================================
def main():
    if not API_KEY or not API_SECRET:
        logger.critical("API keys missing.")
        sys.exit(1)

    bot = BinanceTradingBot()
    if not bot.fetch_and_validate_usdt_pairs():
        logger.critical("No symbols loaded.")
        sys.exit(1)

    threading.Thread(target=buy_scanner, args=(bot,), daemon=True).start()
    threading.Thread(target=sell_scanner, args=(bot,), daemon=True).start()

    print_professional_dashboard(bot)
    last_full = time.time()
    FULL_INTERVAL = 45.0

    while True:
        try:
            bot.check_and_process_filled_orders()
            now = time.time()
            if now - last_full >= FULL_INTERVAL:
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


