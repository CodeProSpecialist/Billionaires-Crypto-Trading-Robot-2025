#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, time, threading, logging, json, signal, re, urllib.parse, requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
import pytz
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

# ------------------ USER CONFIG ------------------
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

if not API_KEY or not API_SECRET:
    print("FATAL: Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

# Ask user which mode
print("=== INFINITY GRID BOT v7.2 ===")
print("Do you want this bot to:")
print("1) Only trade coins already in your portfolio")
print("2) Scan top USDT pairs dynamically for new buys (max 5% per coin)")

choice = input("Enter 1 or 2: ").strip()
if choice not in {'1','2'}:
    print("Invalid choice. Exiting.")
    sys.exit(1)

TRADE_MODE = 'portfolio_only' if choice == '1' else 'dynamic_scan'
print(f"Selected trade mode: {TRADE_MODE}")

# ------------------ CONSTANTS ------------------
MIN_USDT_RESERVE = Decimal('10.0')
MIN_USDT_TO_BUY = Decimal('8.0')
DEFAULT_GRID_SIZE_USDT = Decimal('8.0')
MIN_SELL_VALUE_USDT = Decimal('5.0')
MAX_GRIDS_PER_SIDE = 12
MIN_GRIDS_PER_SIDE = 1
REGRID_INTERVAL = 8
DASHBOARD_REFRESH = 20
PNL_REGRID_THRESHOLD = Decimal('6.0')
FEE_RATE = Decimal('0.001')
TREND_THRESHOLD = Decimal('0.02')
VP_UPDATE_INTERVAL = 300
DEFAULT_GRID_INTERVAL_PCT = Decimal('0.012')
STOP_LOSS_PCT = Decimal('-0.05')
PME_INTERVAL = 180
PME_MIN_SCORE_THRESHOLD = Decimal('1.2')
MAX_PERCENT_PER_COIN = Decimal('0.05')  # 5% max per coin
MIN_PORTFOLIO_COINS = 15
RSI_BUY = Decimal('70')
RSI_SELL = Decimal('58')

HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800

LOG_FILE = "infinity_grid_bot.log"
SHUTDOWN_EVENT = threading.Event()

ZERO = Decimal('0')
ONE = Decimal('1')
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
RESET = "\033[0m"

STRATEGY_COLORS = {'trend': MAGENTA,'mean_reversion': CYAN,'volume_anchored': BLUE}
STRATEGY_LABELS = {'trend': 'TREND','mean_reversion': 'MEAN-REV','volume_anchored': 'VOL-ANCHOR'}

CST_TZ = pytz.timezone('America/Chicago')

# ------------------ LOGGING ------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    from logging.handlers import TimedRotatingFileHandler
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=14)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

# ------------------ GLOBAL STATE ------------------
valid_symbols_dict = {}
symbol_info_cache = {}
active_grid_symbols = {}
live_prices = {}
price_lock = threading.Lock()
balances = {'USDT': ZERO}
balance_lock = threading.Lock()
ws_connected = False

# ------------------ ALERTS ------------------
alert_queue = []
alert_queue_lock = threading.Lock()
last_alert_sent = 0
ALERT_COOLDOWN = 300  # 5 minutes

def now_cst(): 
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")

def send_alert(message, subject="Trading Bot Alert"):
    global last_alert_sent
    current_time = time.time()
    with alert_queue_lock:
        alert_queue.append(f"[{now_cst()}] {subject}: {message}")
        if current_time - last_alert_sent < ALERT_COOLDOWN and len(alert_queue) < 50: 
            return
        full_message = "\n".join(alert_queue[-50:])
        alert_queue.clear()
    if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE:
        logger.error("Missing CALLMEBOT_API_KEY or CALLMEBOT_PHONE")
        return
    encoded = urllib.parse.quote_plus(full_message)
    url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={encoded}&apikey={CALLMEBOT_API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            last_alert_sent = current_time
            logger.info(f"BUNDLED WhatsApp sent ({len(full_message.splitlines())} lines)")
        else:
            logger.error(f"WhatsApp failed: {response.text}")
    except Exception as e:
        logger.error(f"WhatsApp error: {e}")

# ------------------ HELPERS ------------------
def safe_decimal(value, default=ZERO) -> Decimal:
    try: 
        return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except: 
        return default

# ------------------ DATABASE ------------------
DB_URL = "sqlite:///binance_trades.db"
engine = create_engine(DB_URL, echo=False, future=True, pool_pre_ping=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20,8), nullable=False)
    avg_entry_price = Column(Numeric(20,8), nullable=False)

class PendingOrder(Base):
    __tablename__ = "pending_orders"
    id = Column(Integer, primary_key=True)
    binance_order_id = Column(String(64), unique=True, nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20,8), nullable=False)
    quantity = Column(Numeric(20,8), nullable=False)

class TradeRecord(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20,8), nullable=False)
    quantity = Column(Numeric(20,8), nullable=False)
    fee = Column(Numeric(20,8), nullable=False, default=0)
    timestamp = Column(DateTime, default=func.now())

if not os.path.exists("binance_trades.db"):
    Base.metadata.create_all(engine)

class SafeDBManager:
    def __enter__(self):
        try:
            self.session = SessionFactory()
            return self.session
        except Exception:
            return None
    def __exit__(self, exc_type, exc_val, exc_tb):
        if not hasattr(self,'session'): return
        if exc_type: self.session.rollback()
        else:
            try: 
                self.session.commit()
            except IntegrityError: 
                self.session.rollback()
        self.session.close()

# ------------------ INDICATORS ------------------
import numpy as np

def calculate_rsi(prices, period=14):
    """
    Calculate RSI for a list of prices.
    Returns last RSI value as Decimal.
    """
    if len(prices) < period + 1:
        return None
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period-1) + gains[i]) / period
        avg_loss = (avg_loss * (period-1) + losses[i]) / period

    if avg_loss == 0:
        return Decimal('100')
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return Decimal(str(round(rsi, 2)))


def is_bullish_14day(candles):
    """
    Check if the last 14 daily candles show bullish trend.
    `candles` is a list of dicts: [{'open': , 'close': }, ...]
    """
    if len(candles) < 14:
        return False
    closes = [safe_decimal(c['close']) for c in candles[-14:]]
    opens = [safe_decimal(c['open']) for c in candles[-14:]]
    bullish_days = sum([1 for o,c in zip(opens,closes) if c > o])
    return bullish_days >= 8  # more than half of days bullish


def is_bullish_180day(candles, use_monthly=True):
    """
    Check if last 180-day trend is bullish.
    If `use_monthly=True`, use 1-month candles (approx. 6 candles for 180 days)
    `candles` is a list of dicts: [{'open': , 'close': }, ...]
    """
    if use_monthly:
        if len(candles) < 6:
            return False
        # sum open/close for 6 months
        opens = [safe_decimal(c['open']) for c in candles[-6:]]
        closes = [safe_decimal(c['close']) for c in candles[-6:]]
        bullish_months = sum([1 for o,c in zip(opens,closes) if c > o])
        return bullish_months >= 4  # majority months bullish
    else:
        if len(candles) < 180:
            return False
        opens = [safe_decimal(c['open']) for c in candles[-180:]]
        closes = [safe_decimal(c['close']) for c in candles[-180:]]
        bullish_days = sum([1 for o,c in zip(opens,closes) if c > o])
        return bullish_days >= 90  # majority of days bullish

# ------------------ BINANCE API HELPERS ------------------

def fetch_account_balances(bot: BinanceTradingBot):
    """
    Fetch account balances and update global `balances`.
    """
    global balances
    try:
        acct = bot.client.get_account()
        with balance_lock:
            for b in acct['balances']:
                asset = b['asset']
                free = safe_decimal(b['free'])
                balances[asset] = free
    except Exception as e:
        logger.error(f"Failed to fetch balances: {e}")


def fetch_symbol_ticker(bot: BinanceTradingBot, symbol: str):
    """
    Fetch last price for a symbol.
    """
    try:
        ticker = bot.client.get_symbol_ticker(symbol=symbol)
        price = safe_decimal(ticker.get('price', 0))
        with price_lock:
            live_prices[symbol] = price
        return price
    except Exception as e:
        logger.warning(f"Failed to fetch ticker {symbol}: {e}")
        return None


def fetch_klines(bot: BinanceTradingBot, symbol: str, interval: str, limit: int = 500):
    """
    Fetch historical candles.
    interval: '1d', '1h', '1m', '1M' (monthly)
    Returns list of dicts with open/close/high/low/volume
    """
    try:
        raw = bot.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        candles = []
        for r in raw:
            candles.append({
                'open': safe_decimal(r[1]),
                'high': safe_decimal(r[2]),
                'low': safe_decimal(r[3]),
                'close': safe_decimal(r[4]),
                'volume': safe_decimal(r[5]),
                'timestamp': r[0]
            })
        return candles
    except Exception as e:
        logger.warning(f"Failed to fetch klines {symbol} interval {interval}: {e}")
        return []


def fetch_daily_and_monthly_trends(bot: BinanceTradingBot, symbol: str):
    """
    Fetch daily candles for 14-day bullish check
    Fetch monthly candles for 180-day (6-month) bullish check
    Returns tuple (daily_candles, monthly_candles)
    """
    daily_candles = fetch_klines(bot, symbol, '1d', limit=30)  # last 30 days
    monthly_candles = fetch_klines(bot, symbol, '1M', limit=6)  # last 6 months
    return daily_candles, monthly_candles


# ------------------ DYNAMIC SYMBOL SCAN ------------------

def get_top_usdt_symbols(bot: BinanceTradingBot, top_n=20):
    """
    Scan all USDT trading pairs by 24h volume and return top N symbols
    """
    try:
        info = bot.client.get_ticker_24hr()
        usdt_pairs = [s for s in info if s['symbol'].endswith('USDT') and s['symbol'] not in {'BUSDUSDT','USDCUSDT'}]
        usdt_pairs_sorted = sorted(usdt_pairs, key=lambda x: float(x['quoteVolume']), reverse=True)
        top_symbols = [s['symbol'] for s in usdt_pairs_sorted[:top_n]]
        return top_symbols
    except Exception as e:
        logger.error(f"Failed to scan top USDT symbols: {e}")
        return []

# ------------------ BULLISH TREND CHECKS ------------------

def is_bullish_trend(candles: List[dict], min_pct=Decimal('0.0')):
    """
    Determines if a series of candles shows a bullish trend.
    Simple check: closing price today > closing price N days ago
    min_pct: minimum % gain over the period
    """
    if not candles or len(candles) < 2:
        return False
    start_price = candles[0]['close']
    end_price = candles[-1]['close']
    if start_price <= 0:
        return False
    pct_change = (end_price - start_price) / start_price
    return pct_change >= min_pct


def has_valid_buy_signal(symbol: str):
    """
    Checks if the symbol meets conditions to buy:
    - RSI >= 70
    - 14-day bullish trend
    - 6-month bullish trend
    """
    with kline_lock:
        daily_candles, monthly_candles = fetch_daily_and_monthly_trends(bot, symbol)

    # RSI check using last 14 daily closes
    daily_closes = [c['close'] for c in daily_candles[-RSI_PERIOD:]]
    rsi = calculate_rsi(daily_closes)
    if rsi < 70:
        return False

    # 14-day bullish
    if not is_bullish_trend(daily_candles[-14:], min_pct=Decimal('0.0')):
        return False

    # 180-day bullish -> approximate with last 6 monthly candles
    if not is_bullish_trend(monthly_candles, min_pct=Decimal('0.0')):
        return False

    return True


# ------------------ GRID MANAGEMENT ------------------

def update_active_symbols(bot: BinanceTradingBot, portfolio_only=False, max_per_coin=Decimal('0.05')):
    """
    Scan portfolio or top symbols dynamically to maintain active grids.
    """
    try:
        # Determine candidate symbols
        if portfolio_only:
            candidates = list(valid_symbols_dict.keys())
        else:
            candidates = get_top_usdt_symbols(bot, top_n=20)

        # Remove coins that have dropped below 58 RSI
        for symbol in list(active_grid_symbols.keys()):
            with kline_lock:
                daily_candles, _ = fetch_daily_and_monthly_trends(bot, symbol)
                closes = [c['close'] for c in daily_candles[-RSI_PERIOD:]]
                rsi = calculate_rsi(closes)
            if rsi < 58:
                logger.info(f"{symbol} RSI dropped below 58, removing from active grid")
                g = active_grid_symbols.pop(symbol, None)
                if g:
                    for oid in g['buy_orders'] + g['sell_orders']:
                        bot.cancel_order_safe(symbol, oid)

        # Place new grids for candidates not already active
        for symbol in candidates:
            if symbol in active_grid_symbols:
                continue

            if not has_valid_buy_signal(symbol):
                continue

            # Enforce 5% max per coin if dynamic scanning mode
            usdt_free = bot.get_balance()
            max_alloc = usdt_free * max_per_coin
            grid_size = min(DEFAULT_GRID_SIZE_USDT, max_alloc)

            if grid_size < MIN_USDT_TO_BUY:
                continue

            current_price = live_prices.get(symbol)
            if not current_price or current_price <= 0:
                current_price = fetch_symbol_ticker(bot, symbol)
                if not current_price or current_price <= 0:
                    continue

            regrid_symbol_with_strategy(bot, symbol, strategy='trend')
            time.sleep(0.2)  # small delay to respect API rate limits

    except Exception as e:
        logger.error(f"Error updating active symbols: {e}")

# ------------------ MAIN LOOP & SCHEDULER ------------------

def fetch_and_update_prices(bot: BinanceTradingBot):
    """
    Updates live prices for all active symbols.
    """
    try:
        for symbol in list(valid_symbols_dict.keys()):
            try:
                ticker = bot.client.get_symbol_ticker(symbol=symbol)
                price = safe_decimal(ticker['price'])
                if price > 0:
                    with price_lock:
                        live_prices[symbol] = price
            except Exception as e:
                logger.debug(f"Price update failed for {symbol}: {e}")
            time.sleep(0.05)  # small delay to avoid hitting rate limits
    except Exception as e:
        logger.error(f"Error fetching prices: {e}")


def main_loop(bot: BinanceTradingBot, portfolio_only=False):
    """
    Main loop for running the Infinity Grid Bot.
    """
    last_grid_update = 0
    last_dashboard_update = 0
    grid_interval = 600  # 10 minutes
    dashboard_interval = DASHBOARD_REFRESH  # seconds

    # Start PME in a separate thread
    threading.Thread(target=profit_monitoring_engine, daemon=True).start()

    # Start user stream keepalive
    threading.Thread(target=keepalive_user_stream, daemon=True).start()

    logger.info("Bot main loop started...")

    while not SHUTDOWN_EVENT.is_set():
        current_time = time.time()

        # Periodically fetch and update live prices
        fetch_and_update_prices(bot)

        # Update grids every 10 minutes
        if current_time - last_grid_update > grid_interval:
            last_grid_update = current_time
            update_active_symbols(bot, portfolio_only=portfolio_only)
        
        # Refresh dashboard periodically
        if current_time - last_dashboard_update > dashboard_interval:
            last_dashboard_update = current_time
            print_dashboard(bot)

        # Check stop-loss
        if check_stop_loss():
            logger.critical("Stop-loss triggered. Exiting main loop.")
            break

        time.sleep(1)


# ------------------ INITIAL STARTUP ------------------

def startup_bot():
    """
    Initialize bot, ask portfolio mode, start streams, sync positions.
    """
    print("Do you want this bot to only trade coins already in your portfolio (P),")
    print("or should it scan top USDT pairs dynamically for new buys (D) while respecting your 5% max per coin rule?")
    choice = input("Enter P for portfolio only, D for dynamic scan [P/D]: ").strip().upper()
    portfolio_only = choice == 'P'

    global rest_client, bot
    bot = BinanceTradingBot()
    rest_client = bot.client

    # Initial REST sync
    initial_sync_from_rest(bot)

    # Start user stream and market websockets
    start_user_stream()
    start_market_websocket()

    # Start main loop
    main_loop(bot, portfolio_only=portfolio_only)

# ------------------ UTILITY FUNCTIONS ------------------

def fetch_daily_and_monthly_trends(bot: BinanceTradingBot, symbol: str):
    """
    Fetches daily klines for 14-day trend check and monthly klines for 180-day trend check.
    Returns a tuple: (14_day_bullish, 180_day_bullish)
    """
    try:
        # 14-day daily trend
        daily_klines = bot.client.get_klines(symbol=symbol, interval='1d', limit=15)
        daily_closes = [safe_decimal(k[4]) for k in daily_klines]  # Close prices
        bullish_14 = all(daily_closes[i] > daily_closes[i - 1] for i in range(1, len(daily_closes)))

        # 180-day 1-month interval trend
        monthly_klines = bot.client.get_klines(symbol=symbol, interval='1M', limit=7)
        monthly_closes = [safe_decimal(k[4]) for k in monthly_klines]
        bullish_180 = all(monthly_closes[i] > monthly_closes[i - 1] for i in range(1, len(monthly_closes)))

        return bullish_14, bullish_180
    except Exception as e:
        logger.debug(f"Trend fetch failed for {symbol}: {e}")
        return False, False


def get_top_usdt_symbols(bot: BinanceTradingBot, top_n=50):
    """
    Returns a list of top USDT symbols by 24h volume.
    """
    try:
        tickers = bot.client.get_ticker_24hr()
        symbols = [
            t['symbol'] for t in tickers
            if t['symbol'].endswith('USDT') and t['symbol'] not in {'USDCUSDT', 'BUSDUSDT'}
        ]
        # Sort by quoteVolume descending
        symbols_sorted = sorted(
            symbols,
            key=lambda s: safe_decimal(next(t['quoteVolume'] for t in tickers if t['symbol'] == s)),
            reverse=True
        )
        return symbols_sorted[:top_n]
    except Exception as e:
        logger.error(f"Top symbols fetch failed: {e}")
        return []


def update_active_symbols(bot: BinanceTradingBot, portfolio_only=False):
    """
    Updates buy/sell grids for all active symbols or top USDT symbols.
    Ensures no coin exceeds 5% of portfolio allocation.
    """
    try:
        usdt_total = bot.get_balance()
        portfolio_symbols = list(valid_symbols_dict.keys()) if portfolio_only else get_top_usdt_symbols(bot)

        for symbol in portfolio_symbols:
            price = live_prices.get(symbol, ZERO)
            if price <= ZERO:
                continue

            # Calculate max allowable allocation per coin
            max_allocation = usdt_total * Decimal('0.05')
            base_asset = symbol.replace('USDT', '')
            asset_balance = bot.get_asset_balance(base_asset)
            current_value = asset_balance * price

            if current_value >= max_allocation:
                logger.debug(f"{symbol}: Skipping, exceeds 5% max allocation")
                continue

            # Fetch 14-day and 180-day bullish trends
            bullish_14, bullish_180 = fetch_daily_and_monthly_trends(bot, symbol)

            if not bullish_14:
                logger.debug(f"{symbol}: Skipping, 14-day trend not bullish")
                continue

            # Only grid if bullish 14-day trend or if already holding (to respect portfolio coins)
            regrid_symbol_with_strategy(bot, symbol, strategy='trend' if bullish_180 else 'volume_anchored')

    except Exception as e:
        logger.error(f"Update active symbols failed: {e}")


def format_decimal(value: Decimal, precision=8) -> str:
    """
    Helper to format Decimal for printing or logging.
    """
    return f"{value:.{precision}f}"

# ------------------ ENTRY POINT & STARTUP ------------------

if __name__ == "__main__":
    import os
    import sys

    # Load Binance API keys from environment variables
    API_KEY = os.getenv('BINANCE_API_KEY')
    API_SECRET = os.getenv('BINANCE_API_SECRET')

    if not API_KEY or not API_SECRET:
        print("FATAL: Set BINANCE_API_KEY and BINANCE_API_SECRET")
        sys.exit(1)

    # Initialize bot instance
    bot = BinanceTradingBot()

    # Ask user for portfolio or dynamic scan mode
    print("\nDo you want this bot to only trade coins already in your portfolio,")
    print("or should it scan top USDT pairs dynamically for new buys while respecting your 5% max per coin rule?")
    print("Enter 'P' for portfolio-only mode, or 'D' for dynamic mode (default: P): ", end='')
    user_input = input().strip().upper()
    portfolio_only_mode = True if user_input != 'D' else False

    mode_text = "Portfolio-only mode" if portfolio_only_mode else "Dynamic scan mode"
    logger.info(f"Bot starting in {mode_text}")

    # Initial REST sync of balances and positions
    try:
        initial_sync_from_rest(bot)
        logger.info("Initial REST sync completed.")
    except Exception as e:
        logger.error(f"Initial REST sync failed: {e}")
        sys.exit(1)

    # Start WebSocket streams
    start_market_websocket()
    start_user_stream()

    # Start keepalive for user stream
    threading.Thread(target=keepalive_user_stream, daemon=True).start()

    # Start PME (Profit Monitoring Engine) loop
    threading.Thread(target=profit_monitoring_engine, daemon=True).start()

    # Start 10-minute update loop for active symbols
    def periodic_symbol_update():
        while not SHUTDOWN_EVENT.is_set():
            update_active_symbols(bot, portfolio_only=portfolio_only_mode)
            time.sleep(600)  # 10 minutes

    threading.Thread(target=periodic_symbol_update, daemon=True).start()

    # Start dashboard refresh loop
    def dashboard_loop():
        while not SHUTDOWN_EVENT.is_set():
            print_dashboard(bot)
            time.sleep(DASHBOARD_REFRESH)

    threading.Thread(target=dashboard_loop, daemon=True).start()

    # Signal handler will gracefully shutdown threads
    try:
        while not SHUTDOWN_EVENT.is_set():
            time.sleep(1)
            if check_stop_loss():
                logger.critical("Emergency STOP-LOSS triggered. Shutting down bot.")
                SHUTDOWN_EVENT.set()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Shutting down...")
        SHUTDOWN_EVENT.set()

    # Wait briefly to allow threads to clean up
    time.sleep(2)
    logger.info("Bot has shut down gracefully.")








