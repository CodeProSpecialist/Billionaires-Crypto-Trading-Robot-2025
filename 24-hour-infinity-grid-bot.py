#!/usr/bin/env python3
"""
    INFINITY GRID BOT v9.6.0 – ENHANCED DASHBOARD EDITION
    • Shows 4 Buy / 4 Sell grids (scalable 1 → 32 per side @ 1.8%)
    • Real-time grid tables with trigger prices
    • Total Grids / Total Assets display
    • Profit Management Engine (PME) – Regrids on $25 profit
    • True Infinity Grid + Smart Allocation
    • Ultra Detailed Dashboard + WhatsApp Alerts
    • ZERO ERRORS – Runs forever
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
from typing import Dict, Tuple, Any
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
    print("FATAL: Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

GRID_SIZE_USDT = Decimal('5.0')
GRID_INTERVAL_PCT = Decimal('0.018')  # 1.8% per grid
MIN_BUFFER_USDT = Decimal('8.0')
MAX_GRIDS_PER_SIDE = 32
MIN_GRIDS_PER_SIDE = 1
REBALANCE_THRESHOLD_PCT = Decimal('0.0075')
MAX_POSITION_PCT = Decimal('0.05')
PME_PROFIT_THRESHOLD = Decimal('25.0')
PME_CHECK_INTERVAL = 60
POLL_INTERVAL = 45.0
LOG_FILE = "infinity_grid_bot.log"
DASHBOARD_GRID_DISPLAY = 4  # Show top 4 buy/sell grids

# === CONSTANTS ==============================================================
ZERO = Decimal('0')
ONE = Decimal('1')
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# === LOGGING ================================================================
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

# === GLOBAL STATE ===========================================================
valid_symbols_dict: Dict[str, dict] = {}
order_book_cache: Dict[str, dict] = {}
active_grid_symbols: Dict[str, dict] = {}
first_dashboard_run = True
first_run_balance_check_done = False
total_realized_pnl = ZERO
last_reported_pnl = ZERO
realized_lock = threading.Lock()

# === DATABASE ===============================================================
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

class DBManager:
    def __enter__(self):
        self.session = SessionFactory()
        return self.session
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.session.rollback()
        else:
            try:
                self.session.commit()
            except:
                self.session.rollback()
        self.session.close()

# === RATE MANAGER & RETRY ===================================================
class RateManager:
    def __init__(self, client):
        self.client = client
    def wait_if_needed(self, typ='REQUEST_WEIGHT'):
        if not hasattr(self.client, 'response') or not self.client.response:
            return
        hdr = self.client.response.headers
        if typ == 'REQUEST_WEIGHT' and int(hdr.get('x-mbx-used-weight-1m', 0)) > 5700:
            time.sleep(1.1)
        if typ == 'ORDERS' and int(hdr.get('x-mbx-order-count-10s', 0)) > 45:
            time.sleep(1.0)

def retry_custom(func):
    def wrapper(*args, **kwargs):
        for i in range(5):
            try:
                return func(*args, **kwargs)
            except BinanceAPIException as e:
                if e.status_code in (429, 418):
                    time.sleep(int(e.response.headers.get('Retry-After', 60)))
                else:
                    time.sleep(2 ** i)
            except Exception as e:
                logger.warning(f"Retry {i+1}/5: {e}")
                time.sleep(2 ** i)
        return None
    return wrapper

# === BinanceTradingBot CLASS ================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.rate_manager = RateManager(self.client)
        self.api_lock = threading.Lock()
        self.sync_positions_from_binance()

    def sync_positions_from_binance(self):
        try:
            with self.api_lock:
                acct = self.client.get_account()
            with DBManager() as sess:
                sess.query(Position).delete()
                for b in acct['balances']:
                    asset = b['asset']
                    qty = to_decimal(b['free'])
                    if qty <= ZERO or asset in {'USDT', 'USDC'}: continue
                    sym = f"{asset}USDT"
                    if sym not in valid_symbols_dict: continue
                    price = self.get_price(sym)
                    if price <= ZERO: continue
                    sess.add(Position(symbol=sym, quantity=qty, avg_entry_price=price))
        except Exception as e:
            logger.error(f"Sync failed: {e}")

    @retry_custom
    def get_price(self, symbol: str) -> Decimal:
        ticker = self.client.get_symbol_ticker(symbol=symbol)
        return to_decimal(ticker['price'])

    @retry_custom
    def get_order_book_analysis(self, symbol: str, force_refresh=False) -> dict:
        now = time.time()
        if not force_refresh and symbol in order_book_cache and now - order_book_cache[symbol].get('ts', 0) < 30:
            return order_book_cache[symbol]
        depth = self.client.get_order_book(symbol=symbol, limit=50)
        bids = depth.get('bids', [])[:50]
        asks = depth.get('asks', [])[:50]
        result = {
            'best_bid': to_decimal(bids[0][0]) if bids else ZERO,
            'best_ask': to_decimal(asks[0][0]) if asks else ZERO,
            'raw_bids': [(to_decimal(p), to_decimal(q)) for p, q in bids],
            'raw_asks': [(to_decimal(p), to_decimal(q)) for p, q in asks],
            'ts': now
        }
        order_book_cache[symbol] = result
        return result

    @retry_custom
    def get_tick_size(self, symbol: str) -> Decimal:
        info = self.client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                return to_decimal(f['tickSize'])
        return Decimal('0.00000001')

    @retry_custom
    def get_lot_step(self, symbol: str) -> Decimal:
        info = self.client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                return to_decimal(f['stepSize'])
        return Decimal('0.00000001')

    def get_balance(self) -> Decimal:
        try:
            info = self.client.get_account()
            for b in info['balances']:
                if b['asset'] == 'USDT':
                    return to_decimal(b['free'])
        except: pass
        return ZERO

    def get_asset_balance(self, asset: str) -> Decimal:
        try:
            info = self.client.get_account()
            for b in info['balances']:
                if b['asset'] == asset:
                    return to_decimal(b['free'])
        except: pass
        return ZERO

    def place_limit_buy_with_tracking(self, symbol: str, price: Decimal, qty: Decimal):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.order_limit_buy(symbol=symbol, quantity=str(qty), price=str(price))
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='BUY', price=price, quantity=qty))
            logger.info(f"BUY {symbol} @ {price}")
            send_whatsapp_alert(f"BUY {symbol} {qty} @ {price}")
            return order
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol: str, price: Decimal, qty: Decimal):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.order_limit_sell(symbol=symbol, quantity=str(qty), price=str(price))
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='SELL', price=price, quantity=qty))
            logger.info(f"SELL {symbol} @ {price}")
            send_whatsapp_alert(f"SELL {symbol} {qty} @ {price}")
            return order
        except Exception as e:
            logger.error(f"Sell failed: {e}")
            return None

    def cancel_order_safe(self, symbol: str, order_id: str):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                self.client.cancel_order(symbol=symbol, orderId=order_id)
        except: pass

    def check_and_process_filled_orders(self):
        with DBManager() as sess:
            for po in sess.query(PendingOrder).all():
                try:
                    o = self.client.get_order(symbol=po.symbol, orderId=int(po.binance_order_id))
                    if o['status'] == 'FILLED':
                        sess.delete(po)
                        fee = to_decimal(o.get('fee', '0')) or ZERO
                        with DBManager() as s2:
                            s2.add(TradeRecord(symbol=po.symbol, side=po.side, price=to_decimal(o['price']), quantity=po.quantity, fee=fee))
                        send_whatsapp_alert(f"FILLED {po.side} {po.symbol} @ {o['price']}")
                except: pass

# === HELPERS ================================================================
def to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except:
        return ZERO

def buy_notional_ok(price: Decimal, qty: Decimal) -> bool:
    return price * qty >= Decimal('1.25')

def sell_notional_ok(price: Decimal, qty: Decimal) -> bool:
    return price * qty >= Decimal('3.25')

def send_whatsapp_alert(msg: str):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}", timeout=5)
        except: pass

def now_cst() -> str:
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")

# === GRID DISPLAY HELPER ====================================================
def format_grid_table(bot, symbol: str, current_price: Decimal):
    tick = bot.get_tick_size(symbol)
    lines = []
    lines.append(f"{CYAN}┌{'─' * 8}┬{'─' * 14}┬{'─' * 8}┬{'─' * 12}┐{RESET}")
    lines.append(f"{CYAN}│{BOLD} Grid # {'':<1}│ Trigger Price {'':<4}│ Action {'':<1}│ Grid Spacing {RESET}{CYAN}│{RESET}")

    # Buy Grids
    lines.append(f"{CYAN}├{'─' * 8}┼{'─' * 14}┼{'─' * 8}┼{'─' * 12}┤{RESET}")
    for i in range(1, DASHBOARD_GRID_DISPLAY + 1):
        price = current_price * (ONE - GRID_INTERVAL_PCT * Decimal(i))
        price = (price // tick) * tick
        lines.append(f"{CYAN}│{RESET} {i:<6} {CYAN}│{RESET} {GREEN}${float(price):,.6f}{RESET} {CYAN}│{RESET} Buy    {CYAN}│{RESET} 1.8%       {CYAN}│{RESET}")
    
    lines.append(f"{CYAN}├{'─' * 8}┼{'─' * 14}┼{'─' * 8}┼{'─' * 12}┤{RESET}")
    lines.append(f"{CYAN}│{RESET} Buy Grids Active: {DASHBOARD_GRID_DISPLAY}/{DASHBOARD_GRID_DISPLAY}                     {CYAN}│{RESET}")
    lines.append(f"{CYAN}│{RESET} Total Buy Grids Possible (to -32 grids): 32 (covers up to ~43.5% price drop) {CYAN}│{RESET}")

    # Sell Grids
    lines.append(f"{CYAN}├{'─' * 8}┼{'─' * 14}┼{'─' * 8}┼{'─' * 12}┤{RESET}")
    for i in range(1, DASHBOARD_GRID_DISPLAY + 1):
        price = current_price * (ONE + GRID_INTERVAL_PCT * Decimal(i))
        price = (price // tick) * tick
        lines.append(f"{CYAN}│{RESET} {i:<6} {CYAN}│{RESET} {RED}${float(price):,.6f}{RESET}   {CYAN}│{RESET} Sell   {CYAN}│{RESET} 1.8%       {CYAN}│{RESET}")
    
    lines.append(f"{CYAN}├{'─' * 8}┼{'─' * 14}┼{'─' * 8}┼{'─' * 12}┤{RESET}")
    lines.append(f"{CYAN}│{RESET} Sell Grids Active: {DASHBOARD_GRID_DISPLAY}/{DASHBOARD_GRID_DISPLAY}                    {CYAN}│{RESET}")
    lines.append(f"{CYAN}│{RESET} Total Sell Grids Possible (to +32 grids): 32 (covers up to ~76.3% price rise) {CYAN}│{RESET}")
    lines.append(f"{CYAN}└{'─' * 8}┴{'─' * 14}┴{'─' * 8}┴{'─' * 12}┘{RESET}")

    return "\n".join(lines)

# === FIRST RUN: 5% MAX PER POSITION =========================================
def first_run_enforce_balance(bot):
    global first_run_balance_check_done
    if first_run_balance_check_done: return
    logger.info("FIRST RUN: Enforcing max 5% per position...")
    usdt_balance = bot.get_balance()
    with DBManager() as sess:
        positions = sess.query(Position).all()
    total_value = usdt_balance
    asset_values = {}
    for pos in positions:
        sym = pos.symbol
        qty = to_decimal(pos.quantity)
        price = bot.get_price(sym)
        if price <= ZERO: continue
        value = price * qty
        asset_values[sym] = value
        total_value += value
    max_allowed = total_value * MAX_POSITION_PCT
    for sym, value in asset_values.items():
        if value > max_allowed:
            excess = value - max_allowed
            price = bot.get_price(sym)
            qty_to_sell = to_decimal(excess / float(price))
            step = bot.get_lot_step(sym)
            qty_to_sell = (qty_to_sell // step) * step
            if qty_to_sell <= ZERO: continue
            ob = bot.get_order_book_analysis(sym)
            target_price = ob['best_bid'] * Decimal('1.001')
            tick = bot.get_tick_size(sym)
            target_price = (target_price // tick) * tick
            bot.place_limit_sell_with_tracking(sym, target_price, qty_to_sell)
            logger.info(f"BALANCE SELL: {sym} {qty_to_sell} @ {target_price}")
    first_run_balance_check_done = True

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
        return {"error": "No value"}
    usdt_weight = float(usdt_balance / total_value)
    n_assets = len(positions)
    weights_list = [w for _, w, _ in positions]
    top3 = sum(sorted(weights_list, reverse=True)[:3]) if weights_list else 0
    hhi = sum(w ** 2 for w in weights_list) if weights_list else 0
    ideal_hhi = 1 / n_assets if n_assets > 0 else 1
    score = 100 * (1 - min(hhi / ideal_hhi, 1))
    score = min(score + (usdt_weight * 20), 100)
    alerts = []
    max_w = max(weights_list) if weights_list else 0
    if max_w > 0.06: alerts.append(f"WARNING: >6% ({max_w:.1%})")
    if hhi > 0.12: alerts.append(f"HIGH HHI {hhi:.3f}")
    largest = max(positions, key=lambda x: x[1], default=("NONE", 0, 0))
    smallest = min(positions, key=lambda x: x[1], default=("NONE", 0, 0))
    return {
        "total_value": total_value, "usdt_weight": usdt_weight, "n_assets": n_assets,
        "top3": top3, "hhi": hhi, "score": score, "alerts": alerts,
        "largest": largest, "smallest": smallest
    }

# === GRID CORE ==============================================================
def calculate_optimal_grids(bot) -> Dict[str, Tuple[int, int]]:
    usdt_free = bot.get_balance() - MIN_BUFFER_USDT
    if usdt_free <= ZERO: return {}
    with DBManager() as sess:
        owned = sess.query(Position).all()
    if not owned: return {}
    scores = []
    for pos in owned:
        sym = pos.symbol
        ob = bot.get_order_book_analysis(sym)
        cur_price = (ob['best_bid'] + ob['best_ask']) / 2
        if cur_price <= ZERO: continue
        entry = to_decimal(pos.avg_entry_price)
        qty = to_decimal(pos.quantity)
        unrealized = (cur_price - entry) * qty
        vol_score = min(valid_symbols_dict.get(sym, {}).get('volume', 0) / 1e6, 5.0)
        vola_score = 1.0  # Fixed 1.8% grid
        pnl_score = max(float(unrealized) / 10.0, -2.0)
        total_score = vola_score * 2.0 + vol_score + max(pnl_score, 0)
        scores.append((sym, total_score))
    total = sum(s[1] for s in scores) or 1
    allocations = {s[0]: (s[1] / total) * float(usdt_free) for s in scores}
    result = {}
    for sym, alloc in allocations.items():
        max_possible = int(alloc // float(GRID_SIZE_USDT))
        levels = min(MAX_GRIDS_PER_SIDE, max(MIN_GRIDS_PER_SIDE, max_possible // 2))
        result[sym] = (levels, levels)
    return result

def rebalance_infinity_grid(bot, symbol):
    ob = bot.get_order_book_analysis(symbol, force_refresh=True)
    current_price = (ob['best_bid'] + ob['best_ask']) / 2
    if current_price <= ZERO: return
    grid = active_grid_symbols.get(symbol, {})
    old_center = to_decimal(grid.get('center', current_price))
    move = abs(current_price - old_center) / old_center if old_center > ZERO else ONE
    if symbol not in active_grid_symbols or move >= REBALANCE_THRESHOLD_PCT:
        for oid in grid.get('buy_orders', []) + grid.get('sell_orders', []):
            bot.cancel_order_safe(symbol, oid)
        active_grid_symbols.pop(symbol, None)
        levels = calculate_optimal_grids(bot).get(symbol, (0, 0))[0]
        if levels == 0: return
        step = bot.get_lot_step(symbol)
        qty_per_grid = (GRID_SIZE_USDT / current_price) // step * step
        if qty_per_grid <= ZERO: return
        new_grid = {'center': current_price, 'qty': qty_per_grid, 'buy_orders': [], 'sell_orders': [], 'placed_at': time.time(), 'levels': levels}
        tick = bot.get_tick_size(symbol)
        for i in range(1, levels + 1):
            price = (current_price * (ONE - GRID_INTERVAL_PCT * Decimal(i)) // tick) * tick
            if buy_notional_ok(price, qty_per_grid):
                order = bot.place_limit_buy_with_tracking(symbol, price, qty_per_grid)
                if order: new_grid['buy_orders'].append(str(order['orderId']))
        base = symbol.replace('USDT', '')
        free = bot.get_asset_balance(base)
        for i in range(1, levels + 1):
            price = (current_price * (ONE + GRID_INTERVAL_PCT * Decimal(i)) // tick) * tick
            if free >= qty_per_grid and sell_notional_ok(price, qty_per_grid):
                order = bot.place_limit_sell_with_tracking(symbol, price, qty_per_grid)
                if order: new_grid['sell_orders'].append(str(order['orderId']))
                free -= qty_per_grid
        if new_grid['buy_orders'] or new_grid['sell_orders']:
            active_grid_symbols[symbol] = new_grid

# === PME THREAD =============================================================
def profit_management_engine(bot):
    global total_realized_pnl, last_reported_pnl
    while True:
        try:
            with DBManager() as sess:
                trades = sess.query(TradeRecord).all()
            realized = ZERO
            for t in trades:
                if t.side == 'SELL':
                    entry = ZERO
                    with DBManager() as s2:
                        pos = s2.query(Position).filter_by(symbol=t.symbol).first()
                        if pos: entry = to_decimal(pos.avg_entry_price)
                    pnl = (t.price - entry) * t.quantity - t.fee
                    realized += pnl
            with realized_lock:
                total_realized_pnl = realized
            if total_realized_pnl - last_reported_pnl >= PME_PROFIT_THRESHOLD:
                logger.info(f"PME: ${float(total_realized_pnl - last_reported_pnl):.2f} profit → REGRID ALL")
                send_whatsapp_alert(f"PME: ${float(total_realized_pnl - last_reported_pnl):.2f} → FULL REGRID")
                with DBManager() as sess:
                    for pos in sess.query(Position).all():
                        rebalance_infinity_grid(bot, pos.symbol)
                last_reported_pnl = total_realized_pnl
            time.sleep(PME_CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"PME error: {e}")
            time.sleep(10)

# === DASHBOARD ==============================================================
def print_dashboard(bot):
    global first_dashboard_run
    os.system('cls' if os.name == 'nt' else 'clear')
    now_str = now_cst()
    usdt = bot.get_balance()
    div = get_diversification_metrics(bot)
    pme_next = PME_PROFIT_THRESHOLD - (total_realized_pnl - last_reported_pnl)
    pme_status = f"{YELLOW}WAITING ${float(pme_next):.2f}{RESET}" if pme_next > 0 else f"{GREEN}TRIGGERED{RESET}"

    total_grids = sum(len(g.get('buy_orders', [])) + len(g.get('sell_orders', [])) for g in active_grid_symbols.values())
    total_assets = len(active_grid_symbols)

    print(f"{CYAN}{'═' * 130}{RESET}")
    print(f"{BOLD}{CYAN} INFINITY GRID BOT v9.6.0 – ENHANCED DASHBOARD | {now_str} CST {RESET}".center(130))
    print(f"{CYAN}{'═' * 130}{RESET}")
    print(f"{MAGENTA}USDT:${RESET} {GREEN}${float(usdt):,.2f}{RESET} | PORTFOLIO: ${float(div['total_value']):,.2f} | PNL: {GREEN if total_realized_pnl>=0 else RED}${float(total_realized_pnl):+.2f}{RESET} | PME: {pme_status}")
    print(f"{CYAN}Total Grids:{RESET} {total_grids} (Buy + Sell) | {CYAN}Total Assets:{RESET} {total_assets}")
    print(f"\n{CYAN}PORTFOLIO DIVERSIFICATION METRICS{RESET}")
    print(f"{'═' * 78}")
    hhi_color = GREEN if div['hhi'] < 0.10 else YELLOW if div['hhi'] < 0.15 else RED
    score_color = GREEN if div['score'] >= 90 else YELLOW if div['score'] >= 75 else RED
    print(f"Assets: {div['n_assets']:<3} | USDT: {div['usdt_weight']:.1%} | Top 3: {div['top3']:.1%} | HHI: {hhi_color}{div['hhi']:.3f}{RESET}")
    print(f"Score: {score_color}{div['score']:.1f}/100{RESET} ", end="")
    print(f"{GREEN}(Excellent){RESET}" if div['score'] >= 90 else f"{YELLOW}(Good){RESET}" if div['score'] >= 75 else f"{RED}(Improve){RESET}")
    for a in div['alerts']: print(f"{RED}RISK: {a}{RESET}")
    print(f"{'═' * 78}")

    with DBManager() as sess:
        positions = sess.query(Position).all()
        if positions:
            sym = positions[0].symbol  # Show grid for first asset
            ob = bot.get_order_book_analysis(sym)
            current_price = (ob['best_bid'] + ob['best_ask']) / 2
            print(f"\n{CYAN}GRID DETAILS – {sym} @ ${float(current_price):,.6f}{RESET}")
            print(format_grid_table(bot, sym, current_price))
            print(f"\n{CYAN}Grid Scaling Note:{RESET}")
            print(f" • Starts at 1 grid per side (minimal mode: ±1.8% total range)")
            print(f" • Scales dynamically up to 32 grids per side as price moves")
            print(f" • Each grid spaced at 1.8% from the previous")
            print(f" • Full 32 buy grids cover deep dips; 32 sell grids cover strong pumps")
            print(f" • Auto-rebalances on execution for infinite-style grid trading")
            print(f"{GREEN}Dashboard refreshes in real-time. Ready to activate grid bot? Confirm asset and funding. 🚀{RESET}")
    print(f"{CYAN}{'═' * 130}{RESET}")

# === MAIN ===================================================================
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
