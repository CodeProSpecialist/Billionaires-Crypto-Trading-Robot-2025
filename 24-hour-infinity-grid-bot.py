#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
    INFINITY GRID BOT v7.2 – FULLY FIXED + WEBSOCKETS + PME + DASHBOARD + DYNAMIC SCANNING + RSI/TREND FILTERS + ORDER BOOK SCAN
    • Trades portfolio OR dynamic top 20 USDT pairs by buy bid volume (vol >100k, price $1-$1000)
    • Strategy switching: Trend / Mean-Reversion / Volume-Anchored via PME
    • Real-time dashboard with strategy labels
    • Bundled WhatsApp alerts
    • 5% max per coin | 20% cash reserve (1/5 unspent) | RSI + trend filters
"""

import os
import sys
import time
import json
import threading
import logging
import websocket
import signal
import re
import requests
import urllib.parse
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, List, Optional
import pytz
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError, IntegrityError

getcontext().prec = 28

# === CONFIG ===
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    print("FATAL: Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

# === CONSTANTS ===
MIN_USDT_TO_BUY = Decimal('8.0')
DEFAULT_GRID_SIZE_USDT = Decimal('8.0')
MAX_GRIDS_PER_SIDE = 12
REGRID_INTERVAL = 600
DASHBOARD_REFRESH = 20
VP_UPDATE_INTERVAL = 300
STOP_LOSS_PCT = Decimal('-0.05')
PME_INTERVAL = 180
PME_MIN_SCORE_THRESHOLD = Decimal('1.2')
MAX_PERCENT_PER_COIN = Decimal('0.05')        # <<< FIX >>> 5% max per coin
RESERVE_FRACTION = Decimal('0.20')           # <<< FIX >>> Keep 1/5 (20%) unspent
RSI_BUY = Decimal('70')
RSI_SELL = Decimal('58')
RSI_PERIOD = 14
DEFAULT_GRID_INTERVAL_PCT = Decimal('0.012')

HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800
MAX_STREAMS_PER_CONNECTION = 100

WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"

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

STRATEGY_COLORS = {'trend': MAGENTA, 'mean_reversion': CYAN, 'volume_anchored': BLUE}
STRATEGY_LABELS = {'trend': 'TREND', 'mean_reversion': 'MEAN-REV', 'volume_anchored': 'VOL-ANCHOR'}

CST_TZ = pytz.timezone('America/Chicago')

# === LOGGING ===
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=14)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

# === GLOBAL STATE ===
valid_symbols_dict: Dict[str, dict] = {}
symbol_info_cache: Dict[str, dict] = {}
active_grid_symbols: Dict[str, dict] = {}
live_prices: Dict[str, Decimal] = {}
price_lock = threading.Lock()
balances: Dict[str, Decimal] = {'USDT': ZERO}
balance_lock = threading.Lock()
kline_lock = threading.Lock()
ws_instances: List[websocket.WebSocketApp] = []
user_ws: Optional[websocket.WebSocketApp] = None
listen_key: Optional[str] = None
listen_key_lock = threading.Lock()
ws_connected = False

alert_queue = []
alert_queue_lock = threading.Lock()
last_alert_sent = 0
ALERT_COOLDOWN = 300

realized_pnl_per_symbol: Dict[str, Decimal] = {}
total_realized_pnl = ZERO
peak_pnl = ZERO
realized_lock = threading.Lock()

ticker_24h_stats: Dict[str, dict] = {}
stats_lock = threading.Lock()
trend_bias: Dict[str, Decimal] = {}
kline_data: Dict[str, List[dict]] = {}
volume_profile: Dict[str, dict] = {}
last_vp_update = 0
strategy_scores: Dict[str, dict] = {}
pme_last_run = 0

# === ALERTS ===
def now_cst() -> str:
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

# === HELPERS ===
def safe_decimal(value, default=ZERO) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except:
        return default

def pad_field(text: str, width: int) -> str:
    visible = len(re.sub(r'\033\[[0-9;]*m', '', str(text)))
    return text + ' ' * max(0, width - visible)

def meets_min_notional(symbol: str, price: Decimal, qty: Decimal) -> bool:
    info = symbol_info_cache.get(symbol, {})
    step = info.get('stepSize', ZERO)
    min_notional = info.get('minNotional', Decimal('5.0'))
    if qty < step:
        return False
    if (qty * price) < min_notional:
        return False
    return True

# === DATABASE ===
DB_URL = "sqlite:///binance_trades.db"
engine = create_engine(DB_URL, echo=False, future=True, pool_pre_ping=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20, 8), nullable=False)
    avg_entry_price = Column(Numeric(20, 8), nullable=False)

class PendingOrder(Base):
    __tablename__ = "pending_orders"
    id = Column(Integer, primary_key=True)
    binance_order_id = Column(String(64), unique=True, nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    quantity = Column(Numeric(20, 8), nullable=False)

class TradeRecord(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    quantity = Column(Numeric(20, 8), nullable=False)
    fee = Column(Numeric(20, 8), nullable=False, default=0)
    timestamp = Column(DateTime, default=func.now())

if not os.path.exists("binance_trades.db"):
    Base.metadata.create_all(engine)

class SafeDBManager:
    def __enter__(self):
        try:
            self.session = SessionFactory()
            return self.session
        except:
            return None
    def __exit__(self, exc_type, exc_val, exc_tb):
        if not hasattr(self, 'session'): return
        if exc_type: self.session.rollback()
        else:
            try: self.session.commit()
            except IntegrityError: self.session.rollback()
        self.session.close()

# === [WEBSOCKET, INDICATORS, PME, etc. unchanged] ===

# === GRID & STRATEGY ===
def regrid_symbol_with_strategy(bot, symbol, strategy='volume_anchored'):
    if symbol not in valid_symbols_dict: return
    price = live_prices.get(symbol, ZERO)
    if price <= ZERO: return

    final_bias = trend_bias.get(symbol, ZERO)
    base_interval = DEFAULT_GRID_INTERVAL_PCT

    if strategy == 'trend':
        base_interval *= Decimal('1.5')
        center = price * (ONE + final_bias * Decimal('0.03'))
    elif strategy == 'mean_reversion':
        base_interval = Decimal('0.005')
        center = price
    else:
        vp = volume_profile.get(symbol, {})
        center = vp.get('vwap', price)
        center = center * (ONE + final_bias * base_interval)

    total_usdt = bot.get_balance()
    dynamic_reserve = total_usdt * RESERVE_FRACTION
    if total_usdt < dynamic_reserve + MIN_USDT_TO_BUY:
        return

    usdt_free = total_usdt - dynamic_reserve
    max_alloc = usdt_free * MAX_PERCENT_PER_COIN
    if max_alloc < MIN_USDT_TO_BUY:
        return

    grid_size = min(DEFAULT_GRID_SIZE_USDT, max_alloc)
    max_grids = int(usdt_free // grid_size)
    if max_grids < 2: return

    buy_grids = sell_grids = max_grids // 2
    step = bot.get_lot_step(symbol)
    tick = bot.get_tick_size(symbol)
    qty = (grid_size / price) // step * step
    if qty <= ZERO: return

    old = active_grid_symbols.get(symbol, {})
    for oid in old.get('buy_orders', []) + old.get('sell_orders', []):
        bot.cancel_order_safe(symbol, oid)
    active_grid_symbols.pop(symbol, None)

    new_grid = {
        'center': center, 'qty': qty, 'size': grid_size,
        'buy_orders': [], 'sell_orders': [], 'strategy': strategy
    }

    for i in range(1, buy_grids + 1):
        p = (center * (ONE - base_interval * i)).quantize(tick)
        if p < price * Decimal('0.98'):
            order = bot.place_limit_buy_with_tracking(symbol, p, qty)
            if order:
                new_grid['buy_orders'].append(str(order['orderId']))

    base_asset = symbol.replace('USDT', '')
    asset_free = bot.get_asset_balance(base_asset)
    for i in range(1, sell_grids + 1):
        p = (center * (ONE + base_interval * i)).quantize(tick)
        if p > price * Decimal('1.02') and asset_free >= qty:
            if meets_min_notional(symbol, p, qty):
                order = bot.place_limit_sell_with_tracking(symbol, p, qty)
                if order:
                    new_grid['sell_orders'].append(str(order['orderId']))
                    asset_free -= qty

    if new_grid['buy_orders'] or new_grid['sell_orders']:
        active_grid_symbols[symbol] = new_grid
        color = STRATEGY_COLORS.get(strategy, GREEN)
        label = STRATEGY_LABELS.get(strategy, strategy.upper())
        send_alert(f"{symbol} | ${float(grid_size):.1f} | {len(new_grid['buy_orders'])}B/{len(new_grid['sell_orders'])}S | {color}{label}{RESET}", subject="GRID")

# === UPDATE ACTIVE SYMBOLS ===
def update_active_symbols(bot, portfolio_only):
    total_usdt = bot.get_balance()
    dynamic_reserve = total_usdt * RESERVE_FRACTION
    usdt_free = total_usdt - dynamic_reserve

    candidates = list(valid_symbols_dict.keys()) if portfolio_only else get_top_buy_volume_symbols(bot)
    for sym in candidates:
        if sym in active_grid_symbols: continue
        if not has_valid_buy_signal(bot, sym): continue
        max_alloc = usdt_free * MAX_PERCENT_PER_COIN
        if max_alloc < MIN_USDT_TO_BUY: continue
        regrid_symbol_with_strategy(bot, sym, 'trend')

    for sym in list(active_grid_symbols.keys()):
        closes = [k['close'] for k in kline_data.get(sym, []) if k['interval'] == '1m'][-RSI_PERIOD:]
        rsi = calculate_rsi(closes)
        if rsi < RSI_SELL:
            logger.info(f"{sym} RSI < {RSI_SELL}, removing grid")
            g = active_grid_symbols.pop(sym, None)
            if g:
                for oid in g['buy_orders'] + g['sell_orders']:
                    bot.cancel_order_safe(sym, oid)

# === DASHBOARD ===
def print_dashboard(bot):
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        total_usdt = bot.get_balance()
        reserve = total_usdt * RESERVE_FRACTION
        line = "=" * 120
        print(f"{YELLOW}{line}{RESET}")
        print(pad_field(f"{GREEN}INFINITY GRID BOT v7.2{RESET} | November 11, 2025 11:49 AM CST – US | WS: {'ON' if ws_connected else 'OFF'}", 120))
        print(f"{YELLOW}{line}{RESET}")
        print(pad_field(f"USDT: ${float(total_usdt):,.2f} | RESERVED: ${float(reserve):,.2f} | GRIDS: {len(active_grid_symbols)} | PNL: ${float(total_realized_pnl):+.2f}", 120))
        if active_grid_symbols:
            print(f"\n{YELLOW}ACTIVE GRIDS{RESET}")
            print(pad_field(f"{'SYM':<12} {'PRICE':>10} {'B/S':>6} {'STRATEGY':<12}", 120))
            for sym, g in active_grid_symbols.items():
                p = live_prices.get(sym, ZERO)
                strat = g.get('strategy', 'unknown')
                color = STRATEGY_COLORS.get(strat, GREEN)
                label = STRATEGY_LABELS.get(strat, strat.upper())
                print(pad_field(f"{sym:<12} {float(p):>10.4f} {len(g['buy_orders'])}/{len(g['sell_orders']):>2} {color}{label}{RESET}", 120))
        print(f"{YELLOW}{line}{RESET}")
    except Exception as e:
        logger.error(f"Dashboard error: {e}")


# === STOP LOSS ===
def check_stop_loss():
    with realized_lock:
        drawdown = (total_realized_pnl - peak_pnl) / peak_pnl if peak_pnl > ZERO else ZERO
        if drawdown <= STOP_LOSS_PCT:
            send_alert(f"STOP-LOSS: {float(drawdown*100):.2f}%", subject="EMERGENCY")
            return True
    return False

# === INITIAL SYNC ===
def initial_sync_from_rest(bot):
    load_portfolio_symbols(bot)
    with SafeDBManager() as sess:
        if sess:
            sess.query(Position).delete()
            for sym in valid_symbols_dict:
                base = sym.replace('USDT', '')
                qty = bot.get_asset_balance(base)
                if qty > ZERO:
                    price = live_prices.get(sym, ZERO)
                    if price <= ZERO:
                        try:
                            price = safe_decimal(bot.client.get_symbol_ticker(symbol=sym)['price'])
                        except: price = ZERO
                    if price > ZERO:
                        sess.add(Position(symbol=sym, quantity=qty, avg_entry_price=price))

# === MAIN ===
def main():
    global bot
    bot = BinanceTradingBot()

    print("=== INFINITY GRID BOT v7.2 ===")
    print("Would you like to:")
    print("1) Use coins in your portfolio for infinity grid")
    print("2) Buy coins from top 20 by buying bid volume")
    choice = input("Enter 1 or 2: ").strip()
    portfolio_only = choice == '1'

    load_portfolio_symbols(bot)
    if not valid_symbols_dict and portfolio_only:
        logger.critical("No portfolio symbols.")
        sys.exit(1)

    start_market_websocket()
    start_user_stream()
    threading.Thread(target=keepalive_user_stream, daemon=True).start()

    try:
        initial_sync_from_rest(bot)
    except Exception as e:
        logger.critical(f"Sync failed: {e}")
        sys.exit(1)

    send_alert("Bot started – WebSockets ON", subject="ONLINE")

    threading.Thread(target=profit_monitoring_engine, daemon=True).start()

    last_regrid = 0
    last_dashboard = 0
    last_symbol_update = 0
    while not SHUTDOWN_EVENT.is_set():
        now = time.time()
        if now - last_symbol_update >= 300:
            update_active_symbols(bot, portfolio_only)
            last_symbol_update = now
        if check_stop_loss():
            logger.critical("Stop-loss triggered")
            SHUTDOWN_EVENT.set()
        if now - last_regrid >= REGRID_INTERVAL:
            for sym in list(active_grid_symbols.keys()):
                regrid_symbol_with_strategy(bot, sym, active_grid_symbols[sym]['strategy'])
            last_regrid = now
        if now - last_dashboard >= DASHBOARD_REFRESH:
            print_dashboard(bot)
            last_dashboard = now
        time.sleep(1)

    for ws in ws_instances + ([user_ws] if user_ws else []):
        ws.close()
    logger.info("Bot stopped.")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda s, f: SHUTDOWN_EVENT.set())
    signal.signal(signal.SIGTERM, lambda s, f: SHUTDOWN_EVENT.set())
    main()
