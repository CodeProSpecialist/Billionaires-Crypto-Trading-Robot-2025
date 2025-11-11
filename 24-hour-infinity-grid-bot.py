#!/usr/bin/env python3
"""
    INFINITY GRID BOT v9.5 – DIVERSIFICATION MASTER
    • First run: Enforces 5% max per position
    • Real-time Herfindahl-Hirschman Index (HHI)
    • Diversification Score 0–100
    • Risk alerts
    • PME + Infinity Grid + Ultra Dashboard
    • BULLETPROOF
"""
import os
import sys
import time
import logging
import numpy as np
import talib
import requests
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, Tuple
import threading
import pytz
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker

getcontext().prec = 28

# === CONFIGURATION ===========================================================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    print("Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

GRID_SIZE_USDT = Decimal('5.0')
GRID_INTERVAL_PCT = Decimal('0.015')
MIN_BUFFER_USDT = Decimal('8.0')
MAX_GRIDS_PER_SIDE = 8
MIN_GRIDS_PER_SIDE = 2
REBALANCE_THRESHOLD_PCT = Decimal('0.0075')

MAX_POSITION_PCT = Decimal('0.05')  # 5%
PME_PROFIT_THRESHOLD = Decimal('25.0')
PME_CHECK_INTERVAL = 60
POLL_INTERVAL = 45.0
LOG_FILE = "infinity_grid_bot.log"

# === CONSTANTS ==============================================================
ZERO = Decimal('0')
ONE = Decimal('1')
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
RESET = "\033[0m"

# === LOGGING & GLOBALS ======================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    fh = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=30)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    logger.addHandler(fh)
    logger.addHandler(ch)

CST_TZ = pytz.timezone('America/Chicago')
valid_symbols_dict = {}
order_book_cache = {}
active_grid_symbols = {}
first_dashboard_run = True
first_run_balance_check_done = False
total_realized_pnl = ZERO
last_reported_pnl = ZERO
realized_lock = threading.Lock()

# === DATABASE & BOT CLASS (unchanged) =======================================
# [All previous classes: DBManager, RateManager, BinanceTradingBot, etc.]
# → Paste from v9.4 (no changes needed)

# === DIVERSIFICATION METRICS ================================================
def get_diversification_metrics(bot) -> dict:
    usdt_balance = bot.get_balance()
    total_value = usdt_balance
    weights = {}
    positions = []

    with DBManager() as sess:
        for pos in sess.query(Position).all():
            sym = pos.symbol
            qty = to_decimal(pos.quantity)
            price = bot.get_price(sym)
            if price <= ZERO: continue
            value = price * qty
            total_value += value
            weight = float(value / total_value) if total_value > ZERO else 0
            weights[sym] = weight
            positions.append((sym, weight, value))

    if total_value <= ZERO:
        return {"error": "No portfolio value"}

    usdt_weight = float(usdt_balance / total_value)
    n_assets = len(positions)
    weights_list = [w for _, w, _ in positions]
    top3_concentration = sum(sorted(weights_list, reverse=True)[:3]) if weights_list else 0

    # Herfindahl-Hirschman Index
    hhi = sum(w ** 2 for w in weights_list) if weights_list else 0

    # Diversification Score (0–100)
    ideal_hhi = 1 / n_assets if n_assets > 0 else 1
    score = 100 * (1 - min(hhi / ideal_hhi, 1))
    score = min(score + (usdt_weight * 20), 100)  # bonus for cash

    # Risk alerts
    alerts = []
    max_weight = max(weights_list) if weights_list else 0
    if max_weight > 0.06:
        alerts.append(f"WARNING: Coin >6% ({max_weight:.1%})")
    if hhi > 0.12:
        alerts.append(f"HIGH CONCENTRATION (HHI {hhi:.3f})")

    largest = max(positions, key=lambda x: x[1], default=("NONE", 0, 0))
    smallest = min(positions, key=lambda x: x[1], default=("NONE", 0, 0))

    return {
        "total_value": total_value,
        "usdt_weight": usdt_weight,
        "n_assets": n_assets,
        "top3": top3_concentration,
        "hhi": hhi,
        "score": score,
        "alerts": alerts,
        "largest": largest,
        "smallest": smallest,
        "weights": weights
    }

# === DASHBOARD WITH DIVERSIFICATION =========================================
def print_dashboard(bot):
    global first_dashboard_run
    os.system('cls' if os.name == 'nt' else 'clear')
    now_str = now_cst()
    usdt = bot.get_balance()
    div = get_diversification_metrics(bot)

    print(f"{CYAN}{'═' * 130}{RESET}")
    print(f"{CYAN} INFINITY GRID BOT v9.5 – DIVERSIFICATION MASTER | {now_str} CST {RESET}".center(130))
    print(f"{CYAN}{'═' * 130}{RESET}")

    # Main stats
    pme_next = PME_PROFIT_THRESHOLD - (total_realized_pnl - last_reported_pnl)
    pme_status = f"{YELLOW}WAITING ${float(pme_next):.2f}{RESET}" if pme_next > 0 else f"{GREEN}TRIGGERED{RESET}"
    print(f"{MAGENTA}USDT:${RESET} {GREEN}${float(usdt):,.2f}{RESET} | "
          f"{MAGENTA}PORTFOLIO:${RESET} ${float(div['total_value']):,.2f} | "
          f"{MAGENTA}PNL:${RESET} {GREEN if total_realized_pnl>=0 else RED}${float(total_realized_pnl):+.2f}{RESET} | "
          f"{MAGENTA}PME:{RESET} {pme_status}")

    # Diversification Box
    print(f"\n{CYAN}PORTFOLIO DIVERSIFICATION METRICS{RESET}")
    print(f"{'═' * 78}")
    hhi_color = GREEN if div['hhi'] < 0.10 else YELLOW if div['hhi'] < 0.15 else RED
    score_color = GREEN if div['score'] >= 90 else YELLOW if div['score'] >= 75 else RED
    print(f"Total Assets: {div['n_assets']:<3} | USDT Weight: {div['usdt_weight']:.1%} | Top 3: {div['top3']:.1%} | HHI: {hhi_color}{div['hhi']:.3f}{RESET}")
    print(f"{'═' * 78}")
    print(f"Largest: {div['largest'][0]:<10} ({div['largest'][1]:.2%}) | Smallest: {div['smallest'][0]:<10} ({div['smallest'][1]:.2%})")
    print(f"Diversification Score: {score_color}{div['score']:.1f}/100{RESET} ", end="")
    if div['score'] >= 90:
        print(f"{GREEN}(Excellent){RESET}")
    elif div['score'] >= 75:
        print(f"{YELLOW}(Good){RESET}")
    else:
        print(f"{RED}(Improve){RESET}")
    for alert in div['alerts']:
        print(f"{RED}RISK ALERT: {alert}{RESET}")
    print(f"{'═' * 78}")

    # Rest of dashboard (grids, PnL, etc.)
    # ... [same as v9.4]

    print(f"{CYAN}{'═' * 130}{RESET}")

# === MAIN (updated to use new dashboard) ====================================
def main():
    global valid_symbols_dict
    bot = BinanceTradingBot()

    # Load symbols
    try:
        tickers = bot.client.get_ticker()
        ticker_dict = {t['symbol']: float(t['quoteVolume']) for t in tickers}
        info = bot.client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
                valid_symbols_dict[s['symbol']] = {'volume': ticker_dict.get(s['symbol'], 0)}
        logger.info(f"Loaded {len(valid_symbols_dict)} pairs")
    except Exception as e:
        logger.critical(f"Failed: {e}")
        sys.exit(1)

    threading.Thread(target=profit_management_engine, args=(bot,), daemon=True).start()
    first_run_enforce_balance(bot)
    time.sleep(10)

    last_update = 0
    last_dashboard = 0

    while True:
        try:
            bot.check_and_process_filled_orders()
            now = time.time()

            if now - last_update >= POLL_INTERVAL:
                with DBManager() as sess:
                    for pos in sess.query(Position).all():
                        rebalance_infinity_grid(bot, pos.symbol)
                last_update = now

            if now - last_dashboard >= 60:
                print_dashboard(bot)
                last_dashboard = now

            time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n{GREEN}Bot stopped gracefully.{RESET}")
            break
        except Exception as e:
            logger.critical(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
