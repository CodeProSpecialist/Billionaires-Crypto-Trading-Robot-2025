#!/usr/bin/env python3
"""
    INFINITY GRID BOT v9.2 – ULTRA DETAILED DASHBOARD
    • First run: Top 3 bid-pressure entries
    • True infinity grid + smart allocation
    • PME: Regrids all on $25 profit
    • THE MOST BEAUTIFUL DASHBOARD YOU'VE EVER SEEN
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
    print("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

GRID_SIZE_USDT = Decimal('5.0')
GRID_INTERVAL_PCT = Decimal('0.015')
MIN_BUFFER_USDT = Decimal('8.0')
MAX_GRIDS_PER_SIDE = 8
MIN_GRIDS_PER_SIDE = 2
MIN_GRIDS_FALLBACK = 1
REBALANCE_THRESHOLD_PCT = Decimal('0.0075')

MAX_PRICE = 1000.00
MIN_PRICE = 0.15
MIN_24H_VOLUME_USDT = 60000
ENTRY_BUY_PCT_BELOW_ASK = Decimal('0.001')
ENTRY_MIN_USDT = Decimal('15.0')

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

# === LOGGING ================================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    fh = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=7)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    logger.addHandler(fh)
    logger.addHandler(ch)

CST_TZ = pytz.timezone('America/Chicago')

# === GLOBAL STATE ===========================================================
valid_symbols_dict: Dict[str, dict] = {}
order_book_cache: Dict[str, dict] = {}
active_grid_symbols: Dict[str, dict] = {}
first_dashboard_run = True
first_run_executed = False

total_realized_pnl = ZERO
last_reported_pnl = ZERO
realized_lock = threading.Lock()

# === DATABASE & BOT CLASS (same as v9.1) =====================================
# [All previous classes: DBManager, RateManager, BinanceTradingBot, etc.]
# ... (unchanged from v9.1 – only dashboard changed)

# === ULTRA DETAILED DASHBOARD ===============================================
def print_dashboard(bot):
    global first_dashboard_run, total_realized_pnl, last_reported_pnl
    os.system('cls' if os.name == 'nt' else 'clear')
    
    now_str = datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")
    usdt = bot.get_balance()
    floor = Decimal('40.0')
    free_usdt = usdt - floor
    
    print(f"{CYAN}{'=' * 130}{RESET}")
    print(f"{CYAN} INFINITY GRID BOT v9.2 – ULTRA DASHBOARD | {now_str} CST {RESET}".center(130))
    print(f"{CYAN}{'=' * 130}{RESET}")
    
    # Header Stats
    pnl_color = GREEN if total_realized_pnl >= 0 else RED
    pme_next = PME_PROFIT_THRESHOLD - (total_realized_pnl - last_reported_pnl)
    pme_status = f"{YELLOW}WAITING ${float(pme_next):.2f}{RESET}" if pme_next > 0 else f"{GREEN}TRIGGERED!{RESET}"
    
    print(f"{MAGENTA}USDT BALANCE:${RESET} {GREEN}${float(usdt):,.2f}{RESET} | "
          f"{MAGENTA}FLOOR:${RESET} ${float(floor):.2f} | "
          f"{MAGENTA}FREE:${RESET} {GREEN}${float(free_usdt):,.2f}{RESET} | "
          f"{MAGENTA}REALIZED PNL:{RESET} {pnl_color}${float(total_realized_pnl):+.2f}{RESET}")
    print(f"{MAGENTA}PME STATUS:{RESET} {pme_status} | "
          f"{MAGENTA}GRIDS:{RESET} {len(active_grid_symbols)} | "
          f"{MAGENTA}POSITIONS:{RESET} {len([p for p in bot.client.get_account()['balances'] if float(p['free'])>0]) - 1}")
    print(f"{CYAN}{'=' * 130}{RESET}")

    # Top 3 Entry Candidates
    print(f"\n{CYAN}TOP 3 BID PRESSURE (ENTRY CANDIDATES){RESET}")
    candidates = []
    for sym in valid_symbols_dict:
        if valid_symbols_dict[sym]['volume'] < MIN_24H_VOLUME_USDT: continue
        ob = bot.get_order_book_analysis(sym)
        price = float(ob['best_ask'])
        if not (MIN_PRICE <= price <= MAX_PRICE): continue
        bid_vol = sum(float(q) for _, q in ob['raw_bids'][:5])
        ask_vol = sum(float(q) for _, q in ob['raw_asks'][:5])
        imbalance = bid_vol / (ask_vol or 1)
        if imbalance >= 1.3:
            candidates.append((sym.replace('USDT',''), imbalance, price, bid_vol, ask_vol))
    
    if candidates:
        for coin, imb, price, bvol, avol in sorted(candidates, key=lambda x: x[1], reverse=True)[:3]:
            color = GREEN if imb >= 2.0 else YELLOW if imb >= 1.5 else RESET
            print(f" {coin:>8} | {color}{imb:>5.2f}x{RESET} | Ask: ${price:>8.6f} | "
                  f"BidVol: {bvol:>6.1f} | AskVol: {avol:>6.1f}")
            if first_dashboard_run:
                ob = bot.get_order_book_analysis(f"{coin}USDT", force_refresh=True)
                print(f"   {'BIDS':>12}           | {'ASKS':<12}")
                for i in range(5):
                    bp = float(ob['raw_bids'][i][0]) if i < len(ob['raw_bids']) else 0
                    bq = float(ob['raw_bids'][i][1]) if i < len(ob['raw_bids']) else 0
                    ap = float(ob['raw_asks'][i][0]) if i < len(ob['raw_asks']) else 0
                    aq = float(ob['raw_asks'][i][1]) if i < len(ob['raw_asks']) else 0
                    print(f"   {bp:>10.6f} x {bq:>8.1f} | {ap:<10.6f} x {aq:<8.1f}")
        if first_dashboard_run:
            print(f"\n{YELLOW}Full ladder shown once. Next updates: summary only.{RESET}")
            first_dashboard_run = False
    else:
        print(" No strong entry signals right now.")

    # Active Grids + PnL
    print(f"\n{CYAN}ACTIVE GRIDS & REAL-TIME PnL{RESET}")
    print(f"{'SYMBOL':<10} {'PRICE':>10} {'CHANGE':>8} {'GRID':>10} {'EFF%':>6} {'UNREAL':>10} {'REALIZED':>10}")
    print("-" * 76)
    
    total_unreal = ZERO
    with DBManager() as sess:
        for pos in sess.query(Position).all():
            sym = pos.symbol
            ob = bot.get_order_book_analysis(sym)
            price = (ob['best_bid'] + ob['best_ask']) / 2
            if price <= ZERO: continue
            
            entry = to_decimal(pos.avg_entry_price)
            qty = to_decimal(pos.quantity)
            unreal = (price - entry) * qty
            total_unreal += unreal
            
            grid = active_grid_symbols.get(sym, {})
            placed = len(grid.get('buy_orders', [])) + len(grid.get('sell_orders', []))
            filled = 0
            with DBManager() as s2:
                filled = s2.query(TradeRecord).filter(TradeRecord.symbol == sym).count()
            efficiency = (filled / max(placed, 1)) * 100 if placed > 0 else 0
            
            change = ((price / entry) - 1) * 100 if entry > ZERO else ZERO
            change_color = GREEN if change >= 0 else RED
            
            unreal_color = GREEN if unreal >= 0 else RED
            print(f"{sym:<10} "
                  f"{GREEN}${float(price):>9.6f}{RESET} "
                  f"{change_color}{float(change):+6.2f}%{RESET} "
                  f"{placed:>5}→{filled:<4} "
                  f"{efficiency:>5.1f}% "
                  f"{unreal_color}${float(unreal):+8.2f}{RESET} "
                  f"{GREEN}${float(total_realized_pnl):+8.2f}{RESET}")
    
    total_value = usdt + total_unreal
    print(f"{'-' * 76}")
    print(f"{MAGENTA}TOTAL UNREALIZED:{RESET} {GREEN if total_unreal>=0 else RED}${float(total_unreal):+.2f}{RESET} | "
          f"{MAGENTA}PORTFOLIO VALUE:{RESET} ${float(total_value):,.2f}")
    print(f"{CYAN}{'=' * 130}{RESET}")

# === MAIN (unchanged except dashboard call) ================================
def main():
    global valid_symbols_dict
    bot = BinanceTradingBot()

    try:
        tickers = bot.client.get_ticker()
        ticker_dict = {t['symbol']: float(t['quoteVolume']) for t in tickers}
        info = bot.client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
                valid_symbols_dict[s['symbol']] = {'volume': ticker_dict.get(s['symbol'], 0)}
        logger.info(f"Loaded {len(valid_symbols_dict)} pairs")
    except Exception as e:
        logger.critical(f"Failed to load symbols: {e}")
        sys.exit(1)

    threading.Thread(target=profit_management_engine, args=(bot,), daemon=True).start()

    last_update = 0
    last_dashboard = 0

    while True:
        try:
            bot.check_and_process_filled_orders()
            now = time.time()

            with DBManager() as sess:
                if sess.query(Position).count() == 0 and not first_run_executed:
                    first_run_entry_from_ladder(bot)

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
            cancel_all_grids(bot)
            print(f"\n{GREEN}Bot stopped gracefully. See you in the lambo.{RESET}")
            break
        except Exception as e:
            logger.critical(f"Main error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
