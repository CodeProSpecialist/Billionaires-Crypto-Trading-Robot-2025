#!/usr/bin/env python3
"""
BINANCE.US LIVE TRADING BOT – MACD + 6MO MOMENTUM + PROFESSIONAL DASHBOARD
- Real market orders
- SQLAlchemy DB
- MACD + RSI + Order Book + 6mo Trend
- Exact dashboard format
- Green/red P&L
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
PROFIT_TARGET_NET = Decimal('0.008')
RISK_PER_TRADE = 0.10
MIN_BALANCE = 5.0
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
ORDERBOOK_SELL_PRESSURE_THRESHOLD = 0.60
ORDER_BOOK_LIMIT = 20
POLL_INTERVAL = 2.0

# MACD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# 6-MONTH MOMENTUM
MOMENTUM_LOOKBACK_DAYS = 180
MIN_MOMENTUM_GAIN = 0.25

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
macd_cache: Dict[str, Tuple[float, float, float]] = {}  # macd, signal, hist
momentum_cache: Dict[str, bool] = {}
trailing_buy_active: Dict[str, dict] = {}
trailing_sell_active: Dict[str, dict] = {}
last_fill_check = 0

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
            return safe_float(bal['free'])
        except: return 0.0

    def calculate_total_portfolio_value(self):
        total = Decimal('0')
        usdt = self.get_balance('USDT')
        with DBManager() as sess:
            for pos in sess.query(Position).all():
                ob = self.get_order_book_analysis(pos.symbol)
                price = ob['best_bid'] or ob['best_ask']
                total += to_decimal(price) * pos.quantity
        return float(total + usdt), float(usdt)

    def place_market_buy(self, symbol, usdt_amount):
        if usdt_amount < MIN_BALANCE: return False
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
        try:
            with self.api_lock:
                self.rate_manager.wait()
                order = self.client.order_market_sell(symbol=symbol, quantity=float(qty))
                self.rate_manager.update()
            logger.info(f"SELL {symbol} {qty:.6f}")
            send_whatsapp_alert(f"SELL {symbol} {qty:.6f}")
            return order
        except Exception as e:
            logger.error(f"SELL FAILED {symbol}: {e}")
            return None

    def check_fills_and_update_db(self):
        global last_fill_check
        if time.time() - last_fill_check < 5: return
        last_fill_check = time.time()
        try:
            with self.api_lock:
                self.rate_manager.wait()
                orders = self.client.get_all_orders(limit=100)
                self.rate_manager.update()
            with DBManager() as sess:
                for o in orders:
                    if o['status'] != 'FILLED' or o['side'] not in ('BUY', 'SELL'): continue
                    sym = o['symbol']
                    price = to_decimal(o['cummulativeQuoteQty']) / to_decimal(o['executedQty'])
                    qty = to_decimal(o['executedQty'])
                    if sess.query(Trade).filter_by(binance_order_id=str(o['orderId'])).first(): continue
                    sess.add(Trade(symbol=sym, side=o['side'], price=price, quantity=qty, binance_order_id=str(o['orderId'])))
                    pos = sess.query(Position).filter_by(symbol=sym).one_or_none()
                    if o['side'] == 'BUY':
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
        except Exception as e:
            logger.debug(f"Fill check error: {e}")

# === SCANNERS ===============================================================
def buy_scanner(bot):
    while True:
        try:
            usdt = bot.get_balance('USDT')
            if usdt < MIN_BALANCE * 2: time.sleep(10); continue
            for pos in bot.get_db_positions():
                sym = pos.symbol
                ob = bot.get_order_book_analysis(sym)
                rsi, trend, low_24h = bot.get_rsi_and_trend(sym)
                macd_val, signal, hist = bot.get_macd(sym)
                is_bullish_6mo = momentum_cache.get(sym, False)

                if (rsi and rsi <= RSI_OVERSOLD and
                    ob['pct_ask'] >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
                    low_24h and ob['best_bid'] <= Decimal(str(low_24h)) * Decimal('1.01') and
                    macd_val > signal and hist > 0 and  # MACD bullish
                    is_bullish_6mo and
                    sym not in trailing_buy_active):
                    with bot.state_lock:
                        trailing_buy_active[sym] = {}
                    amount = usdt * RISK_PER_TRADE
                    bot.place_market_buy(sym, amount)
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.critical(f"Buy scanner error: {e}", exc_info=True)
            time.sleep(10)

def sell_scanner(bot):
    while True:
        try:
            for pos in bot.get_db_positions():
                sym = pos.symbol
                ob = bot.get_order_book_analysis(sym)
                ask = ob['best_ask']
                if ask <= 0: continue
                entry = to_decimal(pos.avg_entry_price)
                net = (ask - entry) / entry
                rsi, _, _ = bot.get_rsi_and_trend(sym)
                if net >= PROFIT_TARGET_NET and rsi and rsi >= RSI_OVERBOUGHT and sym not in trailing_sell_active:
                    with bot.state_lock:
                        trailing_sell_active[sym] = {}
                    bot.place_market_sell(sym, float(pos.quantity))
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.critical(f"Sell scanner error: {e}", exc_info=True)
            time.sleep(10)

# === PROFESSIONAL DASHBOARD =================================================
def print_professional_dashboard(client, bot):
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        now = now_cst()
        usdt_free = bot.get_balance('USDT')
        total_portfolio, _ = bot.calculate_total_portfolio_value()

        GREEN = "\033[32m"
        RED = "\033[31m"
        BOLD = "\033[1m"
        RESET = "\033[0m"

        print(f"{'='*120}")
        print(f"{BOLD}{'TRADING BOT – LIVE DASHBOARD ':^120}{RESET}")
        print(f"{'='*120}\n")

        print(f"{'Time (CST)':<20} {now}")
        print(f"{'Available USDT':<20} ${usdt_free:,.6f}")
        print(f"{'Portfolio Value':<20} ${total_portfolio:,.6f}")
        print(f"{'Trailing Buys':<20} {len(trailing_buy_active)}")
        print(f"{'Trailing Sells':<20} {len(trailing_sell_active)}")
        print(f"{'-'*120}\n")

        with DBManager() as sess:
            db_positions = sess.query(Position).all()

        if db_positions:
            print(f"{BOLD}{'POSITIONS IN DATABASE':^120}{RESET}")
            print(f"{'-'*120}")
            print(f"{'SYMBOL':<10} {'QTY':>12} {'ENTRY':>12} {'CURRENT':>12} {'RSI':>6} {'P&L%':>8} {'PROFIT':>10} {'STATUS':<25}")
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
                net_profit = gross - fee_cost
                pnl_pct = ((cur_price - entry) / entry - (maker + taker)) * 100
                total_pnl += Decimal(str(net_profit))

                status = ("Trailing Sell Active" if symbol in trailing_sell_active
                          else "Trailing Buy Active" if symbol in trailing_buy_active
                          else "24/7 Monitoring")
                color = GREEN if net_profit > 0 else RED
                print(f"{symbol:<10} {qty:>12.6f} {entry:>12.6f} {cur_price:>12.6f} {rsi_str} {color}{pnl_pct:>7.2f}%{RESET} {color}{net_profit:>10.2f}{RESET} {status:<25}")

            print(f"{'-'*120}")
            pnl_color = GREEN if total_pnl > 0 else RED
            print(f"{'TOTAL UNREALIZED P&L':<50} {pnl_color}${float(total_pnl):>12,.2f}{RESET}\n")
        else:
            print(f" No active positions.\n")

        print(f"{BOLD}{'MARKET UNIVERSE':^120}{RESET}")
        print(f"{'-'*120}")
        print(f"{'VALID SYMBOLS':<20} {len(valid_symbols_dict)}")
        print(f"{'AVG 24H VOLUME':<20} ${sum(s.get('volume',0) for s in valid_symbols_dict.values()):,.0f}")
        print(f"{'PRICE RANGE':<20} ${MIN_PRICE} → ${MAX_PRICE}")
        print(f"{'-'*120}\n")

        print(f"{BOLD}{'BUY WATCHLIST (RSI ≤ 35 + SELL PRESSURE)':^120}{RESET}")
        print(f"{'-'*120}")
        watchlist = []
        for symbol in valid_symbols_dict.keys():
            ob = bot.get_order_book_analysis(symbol)
            rsi, trend, low_24h = bot.get_rsi_and_trend(symbol)
            if (rsi is not None and rsi <= RSI_OVERSOLD and
                ob['pct_ask'] >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
                low_24h and ob['best_bid'] <= Decimal(str(low_24h)) * Decimal('1.01')):
                watchlist.append((symbol, rsi, ob['pct_ask'], ob['best_bid']))

        if watchlist:
            print(f"{'SYMBOL':<10} {'RSI':>6} {'SELL %':>8} {'PRICE':>12}")
            print(f"{'-'*40}")
            for sym, rsi_val, sell_pct, price in sorted(watchlist, key=lambda x: x[1])[:10]:
                print(f"{sym:<10} {rsi_val:>6.1f} {sell_pct:>7.1f}% ${price:>11.6f}")
        else:
            print(f" No strong dip signals.")
        print(f"{'-'*120}\n")

        print(f"{BOLD}{'SELL WATCHLIST (PROFIT + RSI ≥ 65)':^120}{RESET}")
        print(f"{'-'*120}")
        sell_watch = []
        with DBManager() as sess:
            for pos in sess.query(Position).all():
                symbol = pos.symbol
                entry = Decimal(str(pos.avg_entry_price))
                ob = bot.get_order_book_analysis(symbol)
                sell_price = ob['best_ask']
                rsi, _, _ = bot.get_rsi_and_trend(symbol)
                maker, taker = bot.get_trade_fees(symbol)
                net_return = (sell_price - entry) / entry - Decimal(str(maker)) - Decimal(str(taker))
                if net_return >= PROFIT_TARGET_NET and rsi is not None and rsi >= RSI_OVERBOUGHT:
                    sell_watch.append((symbol, float(net_return * 100), rsi))

        if sell_watch:
            print(f"{'SYMBOL':<10} {'NET %':>8} {'RSI':>6}")
            print(f"{'-'*30}")
            for sym, ret_pct, rsi_val in sorted(sell_watch, key=lambda x: x[1], reverse=True)[:10]:
                print(f"{sym:<10} {GREEN}{ret_pct:>7.2f}%{RESET} {rsi_val:>6.1f}")
        else:
            print(f" No profitable sell signals.")
        print(f"{'-'*120}\n")

        print(f"{'='*120}\n")

    except Exception as e:
        logger.error(f"Dashboard error: {e}")

# === ALERTS =================================================================
def send_whatsapp_alert(msg):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}", timeout=5)
        except: pass

# === MAIN ===================================================================
def main():
    if not API_KEY or not API_SECRET:
        logger.critical("API keys missing")
        sys.exit(1)
    bot = BinanceTradingBot()
    threading.Thread(target=buy_scanner, args=(bot,), daemon=True).start()
    threading.Thread(target=sell_scanner, args=(bot,), daemon=True).start()
    last_dash = time.time()
    while True:
        bot.check_fills_and_update_db()
        if time.time() - last_dash >= 30:
            print_professional_dashboard(bot.client, bot)
            last_dash = time.time()
        time.sleep(1)

if __name__ == "__main__":
    main()
