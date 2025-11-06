#!/usr/bin/env python3
"""
    INFINITY GRID BOT – TRUE 24/7 PRICE FOLLOWING (FIXED: response.headers)
    • Follows price UP and DOWN forever
    • Regrids every 45s on 0.75% move
    • Cancels stale orders, places new grid
    • Smart allocation: volatility + PnL weighted
    • Full ladder: first run only
    • Fixed: response.get('headers') → response.headers
"""
import os
import sys
import time
import logging
import numpy as np
import talib
import requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List
from collections import deque
import threading
import pytz
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, func
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import SQLAlchemyError

# === CONFIGURATION ===========================================================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')
if not API_KEY or not API_SECRET:
    print("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET env vars")
    sys.exit(1)

# Grid Config
GRID_SIZE_USDT = Decimal('5.0')
GRID_INTERVAL_PCT = Decimal('0.015')  # 1.5% spacing
MIN_BUFFER_USDT = Decimal('8.0')
MAX_GRIDS_PER_SIDE = 8
MIN_GRIDS_PER_SIDE = 2
MIN_GRIDS_FALLBACK = 1
REBALANCE_THRESHOLD_PCT = Decimal('0.0075')  # 0.75% move

# General
MAX_PRICE = 1000.00
MIN_PRICE = 0.15
MIN_24H_VOLUME_USDT = 60000
LOG_FILE = "infinity_grid_bot.log"
POLL_INTERVAL = 45.0  # 45 seconds

# === CONSTANTS ==============================================================
ZERO = Decimal('0')
HUNDRED = Decimal('100')

# === LOGGING ================================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    file_handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

CST_TZ = pytz.timezone('America/Chicago')

# === GLOBAL STATE ===========================================================
valid_symbols_dict: Dict[str, dict] = {}
order_book_cache: Dict[str, dict] = {}
active_grid_symbols: Dict[str, dict] = {}
first_dashboard_run = True
last_ob_fetch: Dict[str, float] = {}

# === MATH & HELPERS =========================================================
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

def buy_notional_ok(price: Decimal, qty: Decimal) -> bool:
    return price * qty >= Decimal('1.25')

def sell_notional_ok(price: Decimal, qty: Decimal) -> bool:
    return price * qty >= Decimal('3.25')

# === RETRY DECORATOR ========================================================
def retry_custom(func):
    def wrapper(*args, **kwargs):
        max_retries = 5
        for i in range(max_retries):
            try:
                return func(*args, **kwargs)
            except BinanceAPIException as e:
                if hasattr(e, 'response') and e.response is not None:
                    hdr = e.response.headers
                    if e.status_code in (429, 418):
                        retry_after = int(hdr.get('Retry-After', 60))
                        logger.warning(f"Rate limit {e.status_code}: sleeping {retry_after}s")
                        time.sleep(retry_after)
                    else:
                        delay = 2 ** i
                        logger.warning(f"Retry {i+1}/{max_retries} for {func.__name__}: {e}")
                        time.sleep(delay)
                else:
                    if i == max_retries - 1:
                        raise
                    time.sleep(2 ** i)
        return None
    return wrapper

# === DATABASE ===============================================================
DB_URL = "sqlite:///binance_trades.db"
engine = create_engine(DB_URL, echo=False, future=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20, 8), nullable=False)
    avg_entry_price = Column(Numeric(20, 8), nullable=False)
    buy_fee_rate = Column(Numeric(10, 6), nullable=False, default=0.001)

class PendingOrder(Base):
    __tablename__ = "pending_orders"
    id = Column(Integer, primary_key=True)
    binance_order_id = Column(String(64), unique=True, nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    quantity = Column(Numeric(20, 8), nullable=False)

if not os.path.exists("binance_trades.db"):
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
            except:
                self.session.rollback()
        self.session.close()

# === RATE MANAGER ===========================================================
class RateManager:
    def __init__(self, client):
        self.client = client
    def wait_if_needed(self, typ='REQUEST_WEIGHT'):
        if not hasattr(self.client, 'response') or self.client.response is None:
            return
        hdr = self.client.response.headers
        if typ == 'REQUEST_WEIGHT' and 'x-mbx-used-weight-1m' in hdr:
            if int(hdr['x-mbx-used-weight-1m']) > 5700:
                time.sleep(1.1)
        if typ == 'ORDERS' and 'x-mbx-order-count-10s' in hdr:
            if int(hdr['x-mbx-order-count-10s']) > 45:
                time.sleep(1.0)

# === BOT CLASS ==============================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.rate_manager = RateManager(self.client)
        self.api_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.sync_positions_from_binance()

    def sync_positions_from_binance(self):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                acct = self.client.get_account()
            with DBManager() as sess:
                sess.query(Position).delete()
                for b in acct['balances']:
                    asset = b['asset']
                    qty = to_decimal(b['free'])
                    if qty <= 0 or asset in {'USDT', 'USDC'}: continue
                    sym = f"{asset}USDT"
                    price = self.get_price_usdt(asset)
                    if price <= ZERO: continue
                    maker, _ = self.get_trade_fees(sym)
                    sess.add(Position(symbol=sym, quantity=qty, avg_entry_price=price, buy_fee_rate=to_decimal(maker)))
                sess.commit()
        except Exception as e:
            logger.error(f"Sync failed: {e}")

    @retry_custom
    def get_price_usdt(self, asset: str) -> Decimal:
        if asset == 'USDT': return Decimal('1')
        sym = asset + 'USDT'
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            ticker = self.client.get_symbol_ticker(symbol=sym)
        return to_decimal(ticker['price'])

    @retry_custom
    def get_trade_fees(self, symbol):
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            fee = self.client.get_trade_fee(symbol=symbol)
        return safe_float(fee[0]['makerCommission']), safe_float(fee[0]['takerCommission'])

    @retry_custom
    def get_order_book_analysis(self, symbol: str, force_refresh=False) -> dict:
        now = time.time()
        with self.state_lock:
            if not force_refresh and symbol in order_book_cache and now - order_book_cache[symbol].get('ts', 0) < 900:
                return order_book_cache[symbol]
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            depth = self.client.get_order_book(symbol=symbol, limit=50)
        bids = depth.get('bids', [])[:50]
        asks = depth.get('asks', [])[:50]
        result = {
            'best_bid': Decimal(bids[0][0]) if bids else ZERO,
            'best_ask': Decimal(asks[0][0]) if asks else ZERO,
            'raw_bids': [(Decimal(p), Decimal(q)) for p, q in bids],
            'raw_asks': [(Decimal(p), Decimal(q)) for p, q in asks],
            'ts': now
        }
        with self.state_lock:
            order_book_cache[symbol] = result
            last_ob_fetch[symbol] = now
        return result

    @retry_custom
    def get_tick_size(self, symbol):
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            info = self.client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                return Decimal(f['tickSize'])
        return Decimal('0.00000001')

    @retry_custom
    def get_atr(self, symbol) -> float:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            klines = self.client.get_klines(symbol=symbol, interval='1m', limit=15)
        highs = np.array([safe_float(k[2]) for k in klines])
        lows = np.array([safe_float(k[3]) for k in klines])
        closes = np.array([safe_float(k[4]) for k in klines])
        atr = talib.ATR(highs, lows, closes, timeperiod=14)[-1]
        return safe_float(atr)

    def get_balance(self) -> Decimal:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            acct = self.client.get_account()
        for b in acct['balances']:
            if b['asset'] == 'USDT':
                return to_decimal(b['free'])
        return ZERO

    def get_asset_balance(self, asset: str) -> Decimal:
        with self.api_lock:
            self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
            acct = self.client.get_account()
        for b in acct['balances']:
            if b['asset'] == asset:
                return to_decimal(b['free'])
        return ZERO

    def place_limit_buy_with_tracking(self, symbol, price: str, qty: float):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.order_limit_buy(symbol=symbol, quantity=qty, price=price)
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='buy',
                                      price=to_decimal(price), quantity=to_decimal(qty)))
            return order
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol, price: str, qty: float):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                order = self.client.order_limit_sell(symbol=symbol, quantity=qty, price=price)
            with DBManager() as sess:
                sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='sell',
                                      price=to_decimal(price), quantity=to_decimal(qty)))
            return order
        except Exception as e:
            logger.error(f"Sell failed: {e}")
            return None

    def cancel_order_safe(self, symbol, order_id):
        try:
            with self.api_lock:
                self.rate_manager.wait_if_needed('ORDERS')
                self.client.cancel_order(symbol=symbol, orderId=order_id)
        except:
            pass

    def check_and_process_filled_orders(self):
        with DBManager() as sess:
            for po in sess.query(PendingOrder).all():
                try:
                    with self.api_lock:
                        self.rate_manager.wait_if_needed('REQUEST_WEIGHT')
                        o = self.client.get_order(symbol=po.symbol, orderId=int(po.binance_order_id))
                    if o['status'] == 'FILLED':
                        sess.delete(po)
                        send_whatsapp_alert(f"{po.side.upper()} {po.symbol} @ {o['price']}")
                except:
                    pass

# === INFINITY GRID CORE =====================================================
def calculate_optimal_grids(bot) -> Dict[str, Tuple[int, int]]:
    usdt_free = bot.get_balance() - MIN_BUFFER_USDT
    if usdt_free <= 0: return {}
    with DBManager() as sess:
        owned = sess.query(Position).all()
    if not owned: return {}

    scores = []
    for pos in owned:
        sym = pos.symbol
        ob = bot.get_order_book_analysis(sym)
        cur_price = (ob['best_bid'] + ob['best_ask']) / 2
        if cur_price <= 0: continue
        entry = Decimal(str(pos.avg_entry_price))
        qty = Decimal(str(pos.quantity))
        unrealized_pnl = (cur_price - entry) * qty
        atr = bot.get_atr(sym)
        volatility_score = atr / float(cur_price) if atr > 0 else 0
        volume = valid_symbols_dict.get(sym, {}).get('volume', 0)
        volume_score = min(volume / 1e6, 5.0)
        pnl_score = max(float(unrealized_pnl) / 10.0, -2.0)
        total_score = volatility_score * 2.0 + volume_score + max(pnl_score, 0)
        scores.append((sym, total_score, float(qty), float(cur_price)))

    if not scores: return {}
    total_score = sum(s[1] for s in scores) or 1
    allocations = {s[0]: (s[1] / total_score) * float(usdt_free) for s in scores}

    result = {}
    for sym, alloc in allocations.items():
        max_possible = int(alloc // float(GRID_SIZE_USDT))
        levels = min(MAX_GRIDS_PER_SIDE, max(MIN_GRIDS_FALLBACK, max_possible // 2))
        levels = max(MIN_GRIDS_PER_SIDE if alloc >= 40 else MIN_GRIDS_FALLBACK, levels)
        result[sym] = (levels, levels)
    return result

def rebalance_infinity_grid(bot, symbol):
    ob = bot.get_order_book_analysis(symbol, force_refresh=True)
    current_price = (ob['best_bid'] + ob['best_ask']) / 2
    if current_price <= 0: return

    grid = active_grid_symbols.get(symbol, {})
    old_center = Decimal(str(grid.get('center', current_price)))
    price_move = abs(current_price - old_center) / old_center if old_center > 0 else 1

    if symbol not in active_grid_symbols or price_move >= REBALANCE_THRESHOLD_PCT:
        for oid in grid.get('buy_orders', []) + grid.get('sell_orders', []):
            bot.cancel_order_safe(symbol, oid)
        active_grid_symbols.pop(symbol, None)

        buy_levels, sell_levels = calculate_optimal_grids(bot).get(symbol, (0, 0))
        if buy_levels == 0: return

        info = bot.client.get_symbol_info(symbol)
        lot = next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        step = Decimal(lot['stepSize'])
        qty_per_grid = (GRID_SIZE_USDT / current_price) // step * step
        if qty_per_grid <= 0: return

        new_grid = {
            'center': current_price,
            'qty': float(qty_per_grid),
            'buy_orders': [],
            'sell_orders': [],
            'last_price': float(current_price),
            'placed_at': time.time()
        }
        tick = bot.get_tick_size(symbol)

        for i in range(1, buy_levels + 1):
            price = (current_price * (1 - GRID_INTERVAL_PCT * i) // tick) * tick
            if buy_notional_ok(price, qty_per_grid):
                order = bot.place_limit_buy_with_tracking(symbol, str(price), float(qty_per_grid))
                if order:
                    new_grid['buy_orders'].append(str(order['orderId']))

        base_asset = symbol.replace('USDT', '')
        free = bot.get_asset_balance(base_asset)
        for i in range(1, sell_levels + 1):
            price = (current_price * (1 + GRID_INTERVAL_PCT * i) // tick) * tick
            if free >= qty_per_grid and sell_notional_ok(price, qty_per_grid):
                order = bot.place_limit_sell_with_tracking(symbol, str(price), float(qty_per_grid))
                if order:
                    new_grid['sell_orders'].append(str(order['orderId']))
                free -= qty_per_grid

        if new_grid['buy_orders'] or new_grid['sell_orders']:
            active_grid_symbols[symbol] = new_grid
            logger.info(f"INFINITY REGRID: {symbol} | Center: ${float(current_price):.6f} | {buy_levels}B/{sell_levels}S")

# === HELPERS ================================================================
def send_whatsapp_alert(msg: str):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}", timeout=5)
        except:
            pass

def now_cst():
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")

def cancel_all_grids(bot):
    for sym, state in list(active_grid_symbols.items()):
        for oid in state.get('buy_orders', []) + state.get('sell_orders', []):
            bot.cancel_order_safe(sym, oid)
    active_grid_symbols.clear()

# === DASHBOARD ==============================================================
def print_dashboard(bot):
    global first_dashboard_run
    os.system('cls' if os.name == 'nt' else 'clear')
    print("=" * 120)
    print(f"{'INFINITY GRID BOT – 24/7 PRICE FOLLOWING':^120}")
    print("=" * 120)
    print(f"Time: {now_cst()} | USDT: ${float(bot.get_balance()):,.2f}")
    with DBManager() as sess:
        print(f"Positions: {sess.query(Position).count()} | Active Grids: {len(active_grid_symbols)}")

    print("\nTOP 3 VOLATILE + PnL POTENTIAL")
    scores = []
    for sym in valid_symbols_dict:
        ob = bot.get_order_book_analysis(sym)
        cur = (ob['best_bid'] + ob['best_ask']) / 2
        if cur <= 0: continue
        with DBManager() as sess:
            pos = sess.query(Position).filter_by(symbol=sym).first()
        if not pos: continue
        atr = bot.get_atr(sym)
        vol_score = atr / float(cur)
        pnl = (cur - Decimal(str(pos.avg_entry_price))) * Decimal(str(pos.quantity))
        scores.append((sym, vol_score, float(pnl)))
    for sym, vol, pnl in sorted(scores, key=lambda x: x[1] + max(x[2]/10,0), reverse=True)[:3]:
        coin = sym.replace('USDT','')
        print(f" {coin:>8} | Vol: {vol:.4f} | PnL: ${pnl:,.2f}")
        if first_dashboard_run:
            ob = bot.get_order_book_analysis(sym, force_refresh=True)
            print("  BIDS           | ASKS")
            for i in range(5):
                bp = float(ob['raw_bids'][i][0]) if i < len(ob['raw_bids']) else 0
                bq = float(ob['raw_bids'][i][1]) if i < len(ob['raw_bids']) else 0
                ap = float(ob['raw_asks'][i][0]) if i < len(ob['raw_asks']) else 0
                aq = float(ob['raw_asks'][i][1]) if i < len(ob['raw_asks']) else 0
                print(f"  {bp:>10.6f}x{bq:>6.1f} | {ap:<10.6f}x{aq:<6.1f}")
    if first_dashboard_run:
        print("\nFull ladder shown once.")
        first_dashboard_run = False

    print("\nGRID STATUS (Center Price | B/S Count)")
    with DBManager() as sess:
        for pos in sess.query(Position).all():
            sym = pos.symbol
            grid = active_grid_symbols.get(sym, {})
            center = grid.get('center', 0)
            b = len(grid.get('buy_orders', []))
            s = len(grid.get('sell_orders', []))
            print(f"{sym:<10} | Center: ${float(center):>10.6f} | Grid: {b}B/{s}S")
    print("=" * 120)

# === MAIN ===================================================================
def main():
    bot = BinanceTradingBot()
    global valid_symbols_dict
    try:
        info = bot.client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
                valid_symbols_dict[s['symbol']] = {'volume': 1e6}
    except: pass

    last_update = 0
    last_dashboard = 0

    while True:
        try:
            bot.check_and_process_filled_orders()
            now = time.time()

            if now - last_update >= POLL_INTERVAL:
                with DBManager() as sess:
                    for pos in sess.query(Position).all():
                        if pos.symbol in valid_symbols_dict:
                            rebalance_infinity_grid(bot, pos.symbol)
                last_update = now

            if now - last_dashboard >= 60:
                print_dashboard(bot)
                last_dashboard = now

            time.sleep(1)
        except KeyboardInterrupt:
            cancel_all_grids(bot)
            print("\nInfinity grid stopped.")
            break
        except Exception as e:
            logger.critical(f"Critical error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
