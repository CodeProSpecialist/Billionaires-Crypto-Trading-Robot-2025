#!/usr/bin/env python3
"""
Binance.US Dynamic Trailing Bot – MODIFIED VERSION
- Removed all threading to slow down for Binance API
- Disabled custom rate limiting; only react to API error codes (e.g., 429)
- Added RateManager to store and monitor rate limit data from headers, proactively wait if close to limit to keep as fast as possible
- Converted to single-threaded loop with states for trailing buy/sell
- Removed circuit breaker, startup grace, custom limiter
- Adjusted retry to handle 429 with Retry-After
"""

import os
import time
import logging
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
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
last_price_cache: Dict[str, Tuple[float, float]] = {}
trailing_buy_active: Dict[str, dict] = {}
trailing_sell_active: Dict[str, dict] = {}
buy_cooldown: Dict[str, float] = {}

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

# === CUSTOM RETRY DECORATOR WITH RATE LIMIT HANDLING ========================
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
                    logger.warning(f"Rate limit hit ({e.status_code}): {e.message} - sleeping {retry_after}s")
                    time.sleep(retry_after)
                else:
                    if i == max_retries - 1:
                        raise
                    delay = base_delay * (2 ** i)
                    logger.warning(f"Retry {i+1}/{max_retries} for {func.__name__}: {e} → sleep {delay}s")
                    time.sleep(delay)
            except Exception as e:
                if i == max_retries - 1:
                    raise
                delay = base_delay * (2 ** i)
                logger.warning(f"Retry {i+1}/{max_retries} for {func.__name__}: {e} → sleep {delay}s")
                time.sleep(delay)
    return wrapper

# === NEW FUNCTION: RATE MANAGER TO STORE AND MANAGE RATE LIMITS =============
class RateManager:
    def __init__(self, client):
        self.client = client
        self.limits = self.get_limits()
        self.current = {
            'request_weight': 0,
            'orders_10s': 0,
            'orders_1d': 0
        }
        self.last_update = 0

    @retry_custom
    def get_limits(self):
        info = self.client.get_exchange_info()
        limits = {}
        for rl in info['rateLimits']:
            key = rl['rateLimitType']
            interval_key = f"{rl['interval']}_{rl['intervalNum']}"
            limits.setdefault(key, {})[interval_key] = rl['limit']
        return limits

    def update_current(self):
        if self.client.response is None:
            return
        headers = self.client.response.headers
        updated = False
        if 'x-mbx-used-weight-1m' in headers:
            self.current['request_weight'] = int(headers['x-mbx-used-weight-1m'])
            updated = True
        if 'x-mbx-order-count-10s' in headers:
            self.current['orders_10s'] = int(headers['x-mbx-order-count-10s'])
            updated = True
        if 'x-mbx-order-count-1d' in headers:
            self.current['orders_1d'] = int(headers['x-mbx-order-count-1d'])
            updated = True
        if updated:
            self.last_update = time.time()

    def is_close_to_limit(self, type_, margin=0.95):
        if type_ == 'REQUEST_WEIGHT':
            limit = self.limits.get('REQUEST_WEIGHT', {}).get('MINUTE_1', 6000)
            used = self.current['request_weight']
            return used >= limit * margin
        elif type_ == 'ORDERS':
            limit_10s = self.limits.get('ORDERS', {}).get('SECOND_10', 50)
            used_10s = self.current['orders_10s']
            if used_10s >= limit_10s * margin:
                return True
            limit_1d = self.limits.get('ORDERS', {}).get('DAY_1', 160000)
            used_1d = self.current['orders_1d']
            return used_1d >= limit_1d * margin
        return False

    def calculate_wait_time(self, type_):
        now = datetime.now()
        if type_ == 'REQUEST_WEIGHT':
            seconds_to_next = 60 - now.second
            return max(seconds_to_next + 0.1, 1.0)
        elif type_ == 'ORDERS':
            seconds = now.second % 10
            to_next = 10 - seconds if seconds > 0 else 10
            return max(to_next + 0.1, 1.0)
        return 1.0

# === FETCH SYMBOLS ==========================================================
@retry_custom
def fetch_and_validate_usdt_pairs(client) -> Dict[str, dict]:
    global valid_symbols_dict
    info = client.get_exchange_info()
    raw_symbols = [
        s['symbol'] for s in info['symbols']
        if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING' and s['symbol'].endswith('USDT')
    ]
    raw_symbols = [s for s in raw_symbols if s not in {'USDCUSDT', 'USDTUSDT'}]

    valid = {}
    for symbol in raw_symbols:
        try:
            ticker = client.get_ticker(symbol=symbol)
            price = safe_float(ticker.get('lastPrice'))
            volume = safe_float(ticker.get('quoteVolume'))
            low_24h = safe_float(ticker.get('lowPrice'))
            if MIN_PRICE <= price <= MAX_PRICE and volume >= MIN_24H_VOLUME_USDT and low_24h > 0:
                valid[symbol] = {'price': price, 'volume': volume, 'low_24h': low_24h}
        except: continue

    valid_symbols_dict = valid
    logger.info(f"Valid symbols: {len(valid)}")
    return valid

# === ORDER BOOK =============================================================
@retry_custom
def get_order_book_analysis(client, symbol: str) -> dict:
    now = time.time()
    cache = order_book_cache.get(symbol, {})
    if cache and now - cache.get('ts', 0) < 1:
        return cache
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

# === TICK SIZE ==============================================================
@retry_custom
def get_tick_size(client, symbol):
    info = client.get_symbol_info(symbol)
    for f in info['filters']:
        if f['filterType'] == 'PRICE_FILTER':
            return Decimal(f['tickSize'])
    return Decimal('0.00000001')

# === METRICS ================================================================
@retry_custom
def get_rsi_and_trend(client, symbol) -> Tuple[Optional[float], str, Optional[float]]:
    klines = client.get_klines(symbol=symbol, interval='1m', limit=100)
    closes = np.array([safe_float(k[4]) for k in klines[-100:]])
    if len(closes) < RSI_PERIOD: return None, "unknown", None
    rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)[-1]
    if not np.isfinite(rsi): return None, "unknown", None
    upper, middle, _ = talib.BBANDS(closes, timeperiod=BB_PERIOD, nbdevup=BB_DEV, nbdevdn=BB_DEV)
    macd, signal_line, _ = talib.MACD(closes, fastperiod=MACD_FAST, slowperiod=MACD_SLOW, signalperiod=MACD_SIGNAL)
    trend = ("bullish" if closes[-1] > middle[-1] and macd[-1] > signal_line[-1]
             else "bearish" if closes[-1] < middle[-1] and macd[-1] < signal_line[-1] else "sideways")
    low_24h = valid_symbols_dict.get(symbol, {}).get('low_24h')
    return float(rsi), trend, float(low_24h) if low_24h else None

# === BUY COOLDOWN ===========================================================
def can_place_buy_order(symbol: str) -> bool:
    now = time.time()
    last_buy = buy_cooldown.get(symbol, 0)
    return now - last_buy >= 15 * 60

def record_buy_placed(symbol: str):
    buy_cooldown[symbol] = time.time()

# === HELPER FUNCTIONS =======================================================
@retry_custom
def get_balance(client, asset='USDT') -> float:
    for bal in client.get_account()['balances']:
        if bal['asset'] == asset:
            return safe_float(bal['free'])
    return 0.0

@retry_custom
def get_trade_fees(client, symbol):
    fee = client.get_trade_fee(symbol=symbol)
    return safe_float(fee[0]['makerCommission']), safe_float(fee[0]['takerCommission'])

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

@retry_custom
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

@retry_custom
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
def print_professional_dashboard(client, bot):
    try:
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
        print(f"{NAVY}{YELLOW}{'Trailing Buys':<20} {len(trailing_buy_active)}{RESET}")
        print(f"{NAVY}{YELLOW}{'Trailing Sells':<20} {len(trailing_sell_active)}{RESET}")
        print(f"{NAVY}{YELLOW}{'-'*120}{RESET}\n")

        # POSITIONS
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

                status = ("Trailing Sell Active" if symbol in trailing_sell_active
                          else "Trailing Buy Active" if symbol in trailing_buy_active
                          else "24/7 Monitoring")
                color = GREEN if net_profit > 0 else RED
                print(f"{NAVY}{YELLOW}{symbol:<10} {qty:>12.6f} {entry:>12.6f} {cur_price:>12.6f} {rsi_str} {color}{pnl_pct:>7.2f}%{RESET}{NAVY}{YELLOW} {color}{net_profit:>10.2f}{RESET}{NAVY}{YELLOW} {status:<25}{RESET}")

            print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
            pnl_color = GREEN if total_pnl > 0 else RED
            print(f"{NAVY}{YELLOW}{'TOTAL UNREALIZED P&L':<50} {pnl_color}${float(total_pnl):>12,.2f}{RESET}\n")
        else:
            print(f"{NAVY}{YELLOW} No active positions.{RESET}\n")

        # UNIVERSE SUMMARY
        print(f"{NAVY}{BOLD}{YELLOW}{'MARKET UNIVERSE':^120}{RESET}")
        print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
        print(f"{NAVY}{YELLOW}{'VALID SYMBOLS':<20} {len(valid_symbols_dict)}{RESET}")
        print(f"{NAVY}{YELLOW}{'AVG 24H VOLUME':<20} ${sum(s['volume'] for s in valid_symbols_dict.values()):,.0f}{RESET}")
        print(f"{NAVY}{YELLOW}{'PRICE RANGE':<20} ${MIN_PRICE} → ${MAX_PRICE}{RESET}")
        print(f"{NAVY}{YELLOW}{'-'*120}{RESET}\n")

        # BUY WATCHLIST
        print(f"{NAVY}{BOLD}{YELLOW}{'BUY WATCHLIST (RSI ≤ 35 + SELL PRESSURE)':^120}{RESET}")
        print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
        watchlist = []
        for symbol in valid_symbols_dict.keys():
            ob = get_order_book_analysis(client, symbol)
            rsi, trend, low_24h = get_rsi_and_trend(client, symbol)
            if (rsi is not None and rsi <= RSI_OVERSOLD and
                ob['pct_ask'] >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
                low_24h and ob['best_bid'] <= Decimal(str(low_24h)) * Decimal('1.01')):
                watchlist.append((symbol, rsi, ob['pct_ask'], ob['best_bid']))

        if watchlist:
            print(f"{NAVY}{YELLOW}{'SYMBOL':<10} {'RSI':>6} {'SELL %':>8} {'PRICE':>12}{RESET}")
            print(f"{NAVY}{YELLOW}{'-'*40}{RESET}")
            for sym, rsi_val, sell_pct, price in sorted(watchlist, key=lambda x: x[1])[:10]:
                print(f"{NAVY}{YELLOW}{sym:<10} {rsi_val:>6.1f} {sell_pct:>7.1f}% ${price:>11.6f}{RESET}")
        else:
            print(f"{NAVY}{YELLOW} No strong dip signals.{RESET}")
        print(f"{NAVY}{YELLOW}{'-'*120}{RESET}\n")

        # SELL WATCHLIST
        print(f"{NAVY}{BOLD}{YELLOW}{'SELL WATCHLIST (PROFIT + RSI ≥ 65)':^120}{RESET}")
        print(f"{NAVY}{YELLOW}{'-'*120}{RESET}")
        sell_watch = []
        with DBManager() as sess:
            for pos in sess.query(Position).all():
                symbol = pos.symbol
                entry = Decimal(str(pos.avg_entry_price))
                ob = get_order_book_analysis(client, symbol)
                sell_price = ob['best_ask']
                rsi, _, _ = get_rsi_and_trend(client, symbol)
                maker, taker = get_trade_fees(client, symbol)
                net_return = (sell_price - entry) / entry - Decimal(str(maker)) - Decimal(str(taker))
                if net_return >= PROFIT_TARGET_NET and rsi is not None and rsi >= RSI_OVERBOUGHT:
                    sell_watch.append((symbol, float(net_return * 100), rsi))

        if sell_watch:
            print(f"{NAVY}{YELLOW}{'SYMBOL':<10} {'NET %':>8} {'RSI':>6}{RESET}")
            print(f"{NAVY}{YELLOW}{'-'*30}{RESET}")
            for sym, ret_pct, rsi_val in sorted(sell_watch, key=lambda x: x[1], reverse=True)[:10]:
                print(f"{NAVY}{YELLOW}{sym:<10} {GREEN}{ret_pct:>7.2f}%{RESET} {rsi_val:>6.1f}{RESET}")
        else:
            print(f"{NAVY}{YELLOW} No profitable sell signals.{RESET}")
        print(f"{NAVY}{YELLOW}{'-'*120}{RESET}\n")

        print(f"{NAVY}{'='*120}{RESET}\n")

    except Exception as e:
        logger.error(f"Dashboard error: {e}")

# === BOT CLASS ==============================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.rate_manager = RateManager(self.client)
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
                            if pending.side == 'buy':
                                if pending.symbol in trailing_buy_active:
                                    del trailing_buy_active[pending.symbol]
                            else:
                                if pending.symbol in trailing_sell_active:
                                    del trailing_sell_active[pending.symbol]
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

    def process_trailing_buy(self, symbol):
        state = trailing_buy_active[symbol]
        try:
            ob = get_order_book_analysis(self.client, symbol)
            best_bid = ob['best_bid']
            if best_bid <= ZERO:
                return

            current_price = float(best_bid)
            now = time.time()

            # RAPID DROP → MARKET BUY
            if symbol not in last_price_cache:
                last_price_cache[symbol] = (current_price, now)
            else:
                last_price, last_time = last_price_cache[symbol]
                if now - last_time < RAPID_DROP_WINDOW:
                    drop_pct = (last_price - current_price) / last_price
                    if drop_pct >= RAPID_DROP_THRESHOLD:
                        logger.warning(f"FLASH DIP: {symbol} -{drop_pct:.2%} in {now-last_time:.1f}s")
                        send_whatsapp_alert(f"FLASH DIP {symbol}: -{drop_pct:.2%} → MARKET BUY")
                        self.place_buy_at_price(symbol, None, force_market=True)
                        del trailing_buy_active[symbol]
                        return
                last_price_cache[symbol] = (current_price, now)

            if best_bid < state['lowest_price']:
                state['lowest_price'] = best_bid

            rsi, trend, low_24h = get_rsi_and_trend(self.client, symbol)
            if rsi is None or rsi > RSI_OVERSOLD:
                return

            history = sell_pressure_history.get(symbol, deque(maxlen=5))
            history.append(ob['pct_ask'])
            sell_pressure_history[symbol] = history

            if low_24h and best_bid > Decimal(str(low_24h)) * Decimal('1.02'):
                return

            if len(history) >= 3:
                peak = max(history)
                current = history[-1]
                if (peak >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
                    current <= peak * 0.9):
                    self.place_buy_at_price(symbol, best_bid)
                    del trailing_buy_active[symbol]
                    return

            if best_bid > state['lowest_price'] * Decimal('1.003'):
                self.place_buy_at_price(symbol, best_bid)
                del trailing_buy_active[symbol]
                return

        except Exception as e:
            logger.debug(f"Trailing buy error [{symbol}]: {e}")

    def place_buy_at_price(self, symbol: str, price: Optional[Decimal], force_market: bool = False):
        state = trailing_buy_active.get(symbol, {})
        try:
            if state.get('last_buy_order_id') and not force_market:
                try:
                    self.client.cancel_order(symbol=symbol, orderId=int(state['last_buy_order_id']))
                except:
                    pass

            balance = get_balance(self.client)
            if balance <= MIN_BALANCE: return
            available = Decimal(str(balance - MIN_BALANCE))
            alloc = min(available * Decimal(str(RISK_PER_TRADE)), available)

            if force_market or price is None:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.order_market_buy(symbol=symbol, quoteOrderQty=float(alloc))
                self.rate_manager.update_current()
                fill_price = Decimal(order['fills'][0]['price']) if order['fills'] else Decimal(str(current_price))
                logger.info(f"MARKET BUY EXECUTED: {symbol} @ {fill_price}")
                send_whatsapp_alert(f"MARKET BUY {symbol} @ {fill_price:.6f} (flash dip)")
            else:
                qty = alloc / price
                adjusted, error = validate_and_adjust_order(self.client, symbol, 'BUY', ORDER_TYPE_LIMIT, qty, price)
                if not adjusted or error: return
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.place_limit_buy_with_tracking(symbol, str(adjusted['price']), adjusted['quantity'])
                self.rate_manager.update_current()
                if order:
                    state['last_buy_order_id'] = str(order['orderId'])
                fill_price = Decimal(adjusted['price'])

            if order:
                record_buy_placed(symbol)
                send_whatsapp_alert(f"BUY EXECUTED {symbol} @ {fill_price:.6f} (dynamic dip)")
                logger.info(f"DYNAMIC BUY: {symbol} @ {fill_price}")
        except Exception as e:
            logger.error(f"Dynamic buy failed: {e}")

    def process_trailing_sell(self, symbol):
        state = trailing_sell_active[symbol]
        try:
            ob = get_order_book_analysis(self.client, symbol)
            best_bid = ob['best_bid']
            if best_bid <= ZERO:
                return

            current_price = best_bid

            if current_price > state['peak_price']:
                state['peak_price'] = current_price

            maker_fee, taker_fee = get_trade_fees(self.client, symbol)
            total_fee_rate = Decimal(str(maker_fee)) + Decimal(str(taker_fee))
            net_return = (current_price - state['entry_price']) / state['entry_price'] - total_fee_rate

            if net_return < PROFIT_TARGET_NET:
                return

            rsi, _, _ = get_rsi_and_trend(self.client, symbol)
            if rsi is None or rsi < RSI_OVERBOUGHT:
                return

            history = buy_pressure_history.get(symbol, deque(maxlen=5))
            history.append(ob['pct_bid'])
            buy_pressure_history[symbol] = history

            if len(history) >= 3:
                peak = max(history)
                current = history[-1]
                if peak >= ORDERBOOK_BUY_PRESSURE_SPIKE * 100 and current <= ORDERBOOK_BUY_PRESSURE_DROP * 100:
                    self.place_sell_at_price(symbol, best_bid)
                    del trailing_sell_active[symbol]
                    return

            if best_bid < state['peak_price'] * Decimal('0.995'):
                self.place_sell_at_price(symbol, best_bid)
                del trailing_sell_active[symbol]
                return

            if self.detect_price_stall(symbol, current_price):
                logger.info(f"STALLED 15 MIN at {current_price:.6f} → MARKET SELL")
                send_whatsapp_alert(f"STALLED 15 MIN {symbol} @ {current_price:.6f} → MARKET SELL")
                self.place_sell_at_price(symbol, None, force_market=True)
                del trailing_sell_active[symbol]
                return

        except Exception as e:
            logger.debug(f"Trailing sell error [{symbol}]: {e}")

    def detect_price_stall(self, symbol: str, current_price: Decimal, stall_threshold: int = STALL_THRESHOLD_SECONDS) -> bool:
        state = trailing_sell_active[symbol]
        now = time.time()

        if 'price_peaks_history' not in state:
            state['price_peaks_history'] = []

        if not state['price_peaks_history'] or current_price > state['price_peaks_history'][-1][1]:
            state['price_peaks_history'].append((now, current_price))

        cutoff = now - stall_threshold
        state['price_peaks_history'] = [
            (t, p) for t, p in state['price_peaks_history'] if t > cutoff
        ]

        if state['price_peaks_history']:
            last_peak_time = state['price_peaks_history'][-1][0]
            return now - last_peak_time >= stall_threshold
        return False

    def place_sell_at_price(self, symbol: str, price: Optional[Decimal], force_market: bool = False):
        state = trailing_sell_active.get(symbol, {})
        try:
            if state.get('last_sell_order_id') and not force_market:
                try:
                    self.client.cancel_order(symbol=symbol, orderId=int(state['last_sell_order_id']))
                except:
                    pass

            qty = state['qty']
            if force_market or price is None:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.order_market_sell(symbol=symbol, quantity=float(qty))
                self.rate_manager.update_current()
                fill_price = Decimal(order['fills'][0]['price']) if order['fills'] else state['peak_price']
                logger.info(f"MARKET SELL (STALL) {symbol} @ {fill_price}")
                send_whatsapp_alert(f"MARKET SELL {symbol} @ {fill_price:.6f} (15-min stall)")
            else:
                tick_size = get_tick_size(self.client, symbol)
                price = ((price // tick_size) + 1) * tick_size
                adjusted, _ = validate_and_adjust_order(self.client, symbol, 'SELL', ORDER_TYPE_LIMIT, qty, price)
                if not adjusted: return
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.place_limit_sell_with_tracking(symbol, str(adjusted['price']), adjusted['quantity'])
                self.rate_manager.update_current()
                if order:
                    state['last_sell_order_id'] = str(order['orderId'])
                fill_price = Decimal(adjusted['price'])

            if order:
                send_whatsapp_alert(f"SELL EXECUTED {symbol} @ {fill_price:.6f}")
                logger.info(f"DYNAMIC SELL: {symbol} @ {fill_price}")
        except Exception as e:
            logger.error(f"Dynamic sell failed: {e}")

    def wait_if_needed(self, type_='REQUEST_WEIGHT'):
        self.rate_manager.update_current()  # Ensure latest before check
        if self.rate_manager.is_close_to_limit(type_):
            wait_time = self.rate_manager.calculate_wait_time(type_)
            logger.debug(f"Close to {type_} limit - sleeping {wait_time:.1f}s")
            time.sleep(wait_time)

# === MAIN ===================================================================
def main():
    if not API_KEY or not API_SECRET:
        logger.critical("API keys missing.")
        sys.exit(1)

    bot = BinanceTradingBot()

    if not fetch_and_validate_usdt_pairs(bot.client):
        sys.exit(1)

    with DBManager() as sess:
        import_owned_assets_to_db(bot.client, sess)
        sess.commit()

    print_professional_dashboard(bot.client, bot)
    time.sleep(2)

    logger.info("Single-threaded loop starting. Dashboard active.")
    last_dashboard = 0
    while True:
        try:
            for symbol in list(valid_symbols_dict.keys()):
                bot.wait_if_needed('REQUEST_WEIGHT')
                ob = get_order_book_analysis(bot.client, symbol)
                bot.rate_manager.update_current()
                rsi, trend, low_24h = get_rsi_and_trend(bot.client, symbol)
                bot.rate_manager.update_current()

                buy_price = ob['best_bid']
                sell_price = ob['best_ask']

                if buy_price <= ZERO or sell_price <= ZERO:
                    continue

                # DYNAMIC BUY START/UPDATE
                with DBManager() as sess:
                    if not sess.query(Position).filter_by(symbol=symbol).first():
                        if (rsi is not None and rsi <= RSI_OVERSOLD and
                            trend == 'bullish' and
                            low_24h and buy_price <= Decimal(str(low_24h)) * Decimal('1.01') and
                            ob['pct_ask'] >= ORDERBOOK_SELL_PRESSURE_THRESHOLD * 100 and
                            symbol not in trailing_buy_active):
                            if not can_place_buy_order(symbol):
                                continue
                            trailing_buy_active[symbol] = {
                                'lowest_price': Decimal('inf'),
                                'start_time': time.time(),
                                'last_buy_order_id': None
                            }
                            logger.info(f"Started TRAILING BUY for {symbol}")
                            send_whatsapp_alert(f"TRAILING BUY ACTIVE: {symbol}")

                if symbol in trailing_buy_active:
                    bot.process_trailing_buy(symbol)

                # DYNAMIC SELL START/UPDATE
                with DBManager() as sess:
                    pos = sess.query(Position).filter_by(symbol=symbol).one_or_none()
                    if pos and symbol not in trailing_sell_active:
                        entry = Decimal(str(pos.avg_entry_price))
                        maker_fee, taker_fee = get_trade_fees(bot.client, symbol)
                        total_fee_rate = Decimal(str(maker_fee)) + Decimal(str(taker_fee))
                        gross_return = (sell_price - entry) / entry
                        net_return = gross_return - total_fee_rate

                        if net_return >= PROFIT_TARGET_NET and rsi is not None and rsi >= RSI_OVERBOUGHT:
                            history = buy_pressure_history.get(symbol, deque(maxlen=5))
                            history.append(ob['pct_bid'])
                            buy_pressure_history[symbol] = history
                            if len(history) >= 3:
                                peak = max(history)
                                if (peak >= ORDERBOOK_BUY_PRESSURE_SPIKE * 100 and
                                    history[-1] <= ORDERBOOK_BUY_PRESSURE_DROP * 100):
                                    trailing_sell_active[symbol] = {
                                        'entry_price': entry,
                                        'qty': pos.quantity,
                                        'peak_price': entry,
                                        'start_time': time.time(),
                                        'last_sell_order_id': None,
                                        'price_peaks_history': []
                                    }
                                    logger.info(f"Started TRAILING SELL for {symbol}")
                                    send_whatsapp_alert(f"TRAILING SELL ACTIVE: {symbol} @ {entry:.6f}")

                if symbol in trailing_sell_active:
                    bot.process_trailing_sell(symbol)

            bot.check_and_process_filled_orders()

            now = time.time()
            if now - last_dashboard >= 30:
                print_professional_dashboard(bot.client, bot)
                last_dashboard = now

            time.sleep(POLL_INTERVAL / max(1, len(valid_symbols_dict)))  # Adjust sleep based on num symbols

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.critical(f"Main loop error: {e}", exc_info=True)
            time.sleep(10)

if __name__ == "__main__":
    main()
