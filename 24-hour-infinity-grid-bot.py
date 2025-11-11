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






