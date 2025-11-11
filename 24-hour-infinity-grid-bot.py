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

