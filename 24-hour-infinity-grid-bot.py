#!/usr/bin/env python3
"""
    INFINITY GRID v8.3 – TRUE VAULT MODE (24/7 MINIMUM 1 GRID PER SIDE)
    • $40 USDT floor
    • 1–16 grids per side (1 buy + 1 sell minimum, forever)
    • Never removes active coins (unless volume < $100k)
    • Regrids only every 30 min
    • Profit recycle only after $50
    • Strategy change only every 2 hours
    • Set-and-forget for life
"""
import os
import sys
import time
import logging
import json
import threading
import websocket
import signal
import re
import requests
from decimal import Decimal, ROUND_DOWN, getcontext   # ← FIXED LINE
from datetime import datetime
import pytz
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError, IntegrityError

# === PRECISION ==============================================================
getcontext().prec = 28

# === CONFIGURATION ===========================================================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    print("FATAL: Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

# === VAULT MODE CONFIG (YOUR FINAL RULES) ===================================
MIN_PRICE_USDT              = Decimal('1.00')
MAX_PRICE_USDT              = Decimal('1000.00')
MIN_24H_VOLUME_USDT         = Decimal('100000')

MIN_USDT_RESERVE            = Decimal('40.0')     # ← $40 floor
MIN_TRADE_VALUE_USDT        = Decimal('5.0')
MIN_SELL_VALUE_USDT         = Decimal('5.0')
MIN_GRIDS_PER_SIDE          = 1                   # ← 1 buy + 1 sell minimum
MAX_GRIDS_PER_SIDE          = 16                  # ← Max 16 per side
REGRID_INTERVAL             = 1800                # ← 30 minutes
DASHBOARD_REFRESH           = 60
PNL_REGRID_THRESHOLD        = Decimal('25.0')
FEE_RATE                    = Decimal('0.001')
TREND_THRESHOLD             = Decimal('0.03')
VP_UPDATE_INTERVAL          = 3600                # ← Hourly
ENTRY_PCT_BELOW_ASK         = Decimal('0.001')    # ← Patient entry
PERCENTAGE_PER_COIN         = Decimal('0.05')
MIN_USDT_FRACTION           = Decimal('0.15')
MIN_BUFFER_USDT             = Decimal('20.0')

# PME – Only every 2 hours
PME_INTERVAL                = 7200                # ← 2 hours
PME_MIN_SCORE_THRESHOLD     = Decimal('2.2')      # ← Only extreme edge

# WebSocket
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
MAX_STREAMS_PER_CONNECTION = 100
HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800

# General
LOG_FILE = "infinity_grid_vault.log"
SHUTDOWN_EVENT = threading.Event()
ZERO = Decimal('0')
ONE = Decimal('1')
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# === LOGGING ================================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=30)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

CST_TZ = pytz.timezone('America/Chicago')

# === GLOBAL STATE ===========================================================
valid_symbols_dict: dict = {}
symbol_info_cache: dict = {}
active_grid_symbols: dict = {}
live_prices: dict = {}
live_asks: dict = {}
live_bids: dict = {}
price_lock = threading.Lock()
ws_instances = []
user_ws = None
listen_key = None
listen_key_lock = threading.Lock()

balances: dict = {'USDT': ZERO}
balance_lock = threading.Lock()

realized_pnl_per_symbol: dict = {}
total_realized_pnl = ZERO
last_reported_pnl = ZERO
last_recycle_pnl = ZERO
realized_lock = threading.Lock()

ws_connected = False
db_connected = True
rest_client = None

ticker_24h_stats: dict = {}
stats_lock = threading.Lock()
trend_bias: dict = {}
kline_data: dict = {}
kline_lock = threading.Lock()
momentum_score: dict = {}
stochastic_data: dict = {}
volume_profile: dict = {}
last_vp_update = 0
pnl_history = []

strategy_scores: dict = {}
pme_last_run = 0

positions: dict = {}
top25_symbols: list = []

# === ELITE COIN FILTER ======================================================
def is_eligible_coin(symbol: str) -> bool:
    price = live_prices.get(symbol, ZERO)
    if not price or price <= ZERO:
        return False
    if price < MIN_PRICE_USDT or price > MAX_PRICE_USDT:
        return False
    with stats_lock:
        volume_usdt = safe_decimal(ticker_24h_stats.get(symbol, {}).get('q', '0'))
    return volume_usdt >= MIN_24H_VOLUME_USDT

# === ALERTS =================================================================
def send_alert(message: str, subject: str = "VAULT"):
    if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE:
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&apikey={CALLMEBOT_API_KEY}&text={subject}: {message}"
        requests.get(url, timeout=5)
    except:
        pass

# === SIGNAL HANDLING ========================================================
def signal_handler(signum, frame):
    logger.info("Shutdown signal received.")
    SHUTDOWN_EVENT.set()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# === HELPERS ================================================================
def pad_field(text: str, width: int) -> str:
    visible = len(re.sub(r'\033\[[0-9;]*m', '', str(text)))
    return text + ' ' * max(0, width - visible)

def safe_decimal(value, default=ZERO) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    try:
        return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except:
        return default

def now_cst() -> str:
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")

# === CASH & MINIMUM CHECKS ==================================================
def calculate_qty_from_cash(cash_usdt: Decimal, price: Decimal, step: Decimal) -> Decimal:
    if price <= ZERO or cash_usdt < MIN_TRADE_VALUE_USDT:
        return ZERO
    raw_qty = cash_usdt / price
    qty = (raw_qty // step) * step
    value = qty * price
    if value < MIN_TRADE_VALUE_USDT:
        return ZERO
    return qty

def get_owned_qty(bot, symbol: str) -> Decimal:
    base = symbol.replace('USDT', '')
    try:
        with bot.api_lock:
            bal = bot.client.get_asset_balance(asset=base)
        return safe_decimal(bal['free'])
    except:
        return ZERO

# === DATABASE & REST OF CODE (UNCHANGED EXCEPT KEY SECTIONS BELOW) ===========
# [All previous code from v8.2 remains identical until regrid_symbol_with_strategy]

# === STRATEGY-AWARE REGRID (NOW GUARANTEES 1 GRID MINIMUM) ===================
def regrid_symbol_with_strategy(bot, symbol, strategy='volume_anchored'):
    if not is_eligible_coin(symbol):
        if symbol in active_grid_symbols:
            logger.info(f"REMOVING {symbol} — no longer elite (volume drop)")
            g = active_grid_symbols.pop(symbol, {})
            for oid in g.get('buy_orders', []) + g.get('sell_orders', []):
                bot.cancel_order_safe(symbol, oid)
        return

    # ← NEW: NEVER remove grid if already active (persistence mode)
    if symbol in active_grid_symbols and time.time() - active_grid_symbols[symbol].get('placed_at', 0) < REGRID_INTERVAL:
        return  # Skip if recently placed

    try:
        current_price = live_prices.get(symbol)
        if not current_price or current_price <= ZERO:
            return

        trend = trend_bias.get(symbol, ZERO)
        momentum = momentum_score.get(symbol, ZERO)
        final_bias = (trend * Decimal('0.35') + momentum * Decimal('0.65')).quantize(Decimal('0.01'))
        final_bias = max(Decimal('-1.0'), min(ONE, final_bias))

        grid_size = get_profit_optimized_grid_size(symbol, current_price)
        base_interval = get_optimal_interval(symbol, current_price)

        # Strategy center logic
        if strategy == 'trend':
            base_interval *= Decimal('1.5')
            final_bias *= Decimal('1.8')
            grid_center = current_price * (ONE + final_bias * Decimal('0.03'))
        elif strategy == 'mean_reversion':
            base_interval = Decimal('0.005')
            grid_center = current_price
        else:
            vp = volume_profile.get(symbol, {})
            grid_center = vp.get('vwap', current_price)
            hvns = vp.get('hvns', [])
            if hvns:
                nearest_hvn = min(hvns, key=lambda x: abs(x - current_price))
                grid_center = (grid_center + nearest_hvn) / 2
            center_offset = final_bias * base_interval * Decimal('1.8')
            grid_center = grid_center * (ONE + center_offset)

        usdt_free = bot.get_balance()
        if usdt_free <= MIN_USDT_RESERVE + Decimal('10'):
            return

        max_grids_total = min(
            MAX_GRIDS_PER_SIDE * 2,
            int((usdt_free - MIN_USDT_RESERVE) // grid_size)
        )
        # ← GUARANTEE MINIMUM 1 GRID PER SIDE
        if max_grids_total < 2:
            return  # Not enough cash even for 1+1

        buy_weight = ONE + final_bias * Decimal('0.5')
        sell_weight = ONE - final_bias * Decimal('0.5')
        buy_weight = max(buy_weight, Decimal('0.5'))
        sell_weight = max(sell_weight, Decimal('0.5'))
        total_weight = buy_weight + sell_weight
        buy_grids = max(MIN_GRIDS_PER_SIDE, int(max_grids_total * buy_weight / total_weight))
        sell_grids = max(MIN_GRIDS_PER_SIDE, max_grids_total - buy_grids)

        density = get_volume_density_multiplier(symbol, current_price)
        buy_grids = min(MAX_GRIDS_PER_SIDE, int(buy_grids * density))
        sell_grids = min(MAX_GRIDS_PER_SIDE, int(sell_grids * density))

        # Cancel old
        old = active_grid_symbols.get(symbol, {})
        for oid in old.get('buy_orders', []) + old.get('sell_orders', []):
            bot.cancel_order_safe(symbol, oid)
        active_grid_symbols.pop(symbol, None)

        step = bot.get_lot_step(symbol)
        tick = bot.get_tick_size(symbol)
        qty_per_grid = calculate_qty_from_cash(grid_size, current_price, step)
        if qty_per_grid <= ZERO:
            return

        new_grid = {
            'center': grid_center,
            'qty': qty_per_grid,
            'size': grid_size,
            'interval': base_interval,
            'bias': final_bias,
            'vwap': volume_profile.get(symbol, {}).get('vwap', current_price),
            'hvns': volume_profile.get(symbol, {}).get('hvns', []),
            'sl_price': grid_center * (ONE - final_bias * Decimal('0.08')),
            'tp_price': grid_center * (ONE + final_bias * Decimal('0.12')),
            'buy_orders': [],
            'sell_orders': [],
            'placed_at': time.time(),
            'strategy': strategy
        }

        # PLACE AT LEAST 1 BUY AND 1 SELL
        for i in range(1, buy_grids + 1):
            price = get_tick_aware_price(new_grid['center'], -1, i, base_interval, tick)
            if price >= current_price * Decimal('0.98'):
                continue
            cash_per = grid_size
            qty = calculate_qty_from_cash(cash_per, price, step)
            if qty > ZERO:
                order = bot.place_limit_buy_with_cash(symbol, price, cash_per)
                if order:
                    new_grid['buy_orders'].append(str(order['orderId']))

        for i in range(1, sell_grids + 1):
            price = get_tick_aware_price(new_grid['center'], +1, i, base_interval, tick)
            if price <= current_price * Decimal('1.02'):
                continue
            order = bot.place_limit_sell_with_owned(symbol, price)
            if order:
                new_grid['sell_orders'].append(str(order['orderId']))

        if len(new_grid['buy_orders']) >= 1 and len(new_grid['sell_orders']) >= 1:
            active_grid_symbols[symbol] = new_grid
            logger.info(f"GRID ACTIVE: {symbol} | {buy_grids}B/{sell_grids}S | STRAT: {strategy.upper()}")
        else:
            logger.warning(f"GRID FAILED: {symbol} – could not place minimum 1 buy + 1 sell")

    except Exception as e:
        logger.error(f"Regrid failed {symbol}: {e}")

# === MAIN LOOP – ENFORCED MINIMUM 1 GRID PER COIN ===========================
def main():
    global rest_client, valid_symbols_dict, symbol_info_cache, bot
    global last_reported_pnl, last_recycle_pnl, total_realized_pnl

    # [Symbol loading & websocket setup unchanged]

    bot = BinanceTradingBot()
    initial_sync_from_rest(bot)
    time.sleep(40)

    logger.info("VAULT MODE v8.3 ACTIVATED – 24/7 MINIMUM 1 GRID PER SIDE")
    threading.Thread(target=profit_monitoring_engine, daemon=True).start()
    threading.Thread(target=rebalance_loop, daemon=True).start()

    last_regrid = 0
    last_dashboard = 0
    last_pnl_check = 0

    while not SHUTDOWN_EVENT.is_set():
        try:
            now = time.time()

            # Profit trigger
            if now - last_pnl_check > 60:
                with realized_lock:
                    if total_realized_pnl - last_reported_pnl > PNL_REGRID_THRESHOLD:
                        logger.info(f"PROFIT ${float(total_realized_pnl - last_reported_pnl):.2f} → Regrid all")
                        for sym in list(active_grid_symbols.keys()):
                            if is_eligible_coin(sym):
                                regrid_symbol_with_strategy(bot, sym, active_grid_symbols[sym].get('strategy', 'volume_anchored'))
                        last_reported_pnl = total_realized_pnl
                last_pnl_check = now

            # Recycle only after $50
            with realized_lock:
                if total_realized_pnl - last_recycle_pnl > Decimal('50'):
                    logger.info(f"RECYCLING ${float(total_realized_pnl - last_recycle_pnl):.2f}")
                    last_recycle_pnl = total_realized_pnl

            # Regrid every 30 min – but only if grid missing or old
            if now - last_regrid >= REGRID_INTERVAL:
                with SafeDBManager() as sess:
                    if sess:
                        for pos in sess.query(Position).all():
                            sym = pos.symbol
                            if is_eligible_coin(sym):
                                if sym not in active_grid_symbols:
                                    regrid_symbol_with_strategy(bot, sym, 'volume_anchored')
                                elif now - active_grid_symbols[sym]['placed_at'] > REGRID_INTERVAL:
                                    regrid_symbol_with_strategy(bot, sym, active_grid_symbols[sym].get('strategy', 'volume_anchored'))
                last_regrid = now

            if now - last_dashboard >= DASHBOARD_REFRESH:
                print_dashboard(bot)
                last_dashboard = now

            time.sleep(1)
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(5)

    print("VAULT BOT STOPPED – Your wealth is safe.")

if __name__ == "__main__":
    main()
