# -*- coding: utf-8 -*-
"""
SMART COIN TRADING ROBOT 2025 - BINANCE.US EDITION
INFINITY GRID + TRAILING HYBRID STRATEGY
FULLY AUTONOMOUS, 24/7 OPERATION

Exchange: Binance.US (Spot Trading Only - No Futures/Margin)
"""

import os
import sys
import time
import json
import logging
import threading
import math
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext
from typing import Dict, List, Tuple, Optional, Any, Set

import pandas as pd
import numpy as np
from colorama import init, Fore, Style
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException

# === CONFIGURATION ===
getcontext().prec = 28

# Binance.US API
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

if not API_KEY or not API_SECRET:
    print("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET env vars")
    sys.exit(1)

# Trading Parameters
GRID_SIZE_USDT = 5.0
MAX_SCALED_COINS = 5
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
TRAILING_STOP_PCT = 0.015
TRAILING_BUY_PCT = 0.02
MAX_POSITIONS = 10
POSITION_SIZE_USDT = 25.0
FEE_MAKER = 0.0010
FEE_TAKER = 0.0010

# Time & Intervals
TIMEZONE_CST = timezone(timedelta(hours=-6))
INTERVAL_5M = "5m"
INTERVAL_1H = "1h"
LOOKBACK_CANDLES = 100

# File Paths
DB_PATH = "trading_bot_us.db"
LOG_PATH = "trading_bot_us.log"
CONFIG_PATH = "config_us.json"

# Global State
valid_symbols_dict: Dict[str, Dict] = {}
active_grid_symbols: Dict[str, Dict] = {}
trailing_buy_active: Dict[str, Dict] = {}
trailing_sell_active: Dict[str, Dict] = {}
MIN_PRICE = Decimal('0.0001')
MAX_PRICE = Decimal('10000.0')

# Initialize colorama
init(autoreset=True)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# === DATABASE ===
Base = declarative_base()

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    quantity = Column(Float, nullable=False)
    avg_entry_price = Column(Float, nullable=False)
    side = Column(String(10), nullable=False)
    leverage = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    status = Column(String(20), default="open")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    order_id = Column(String(50), unique=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)
    type = Column(String(20), nullable=False)
    quantity = Column(Float, nullable=False)
    price = Column(Float)
    status = Column(String(20), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

engine = create_engine(f"sqlite:///{DB_PATH}")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

class DBManager:
    def __init__(self):
        self.session = Session()
    def __enter__(self):
        return self.session
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.session.commit()
        else:
            self.session.rollback()
        self.session.close()

# === UTILITIES ===
def now_cst() -> str:
    return datetime.now(TIMEZONE_CST).strftime("%Y-%m-%d %H:%M:%S CST")

def load_config() -> Dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    return {}

def save_config(config: Dict):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

# === BINANCE.US CLIENT ===
def get_binance_client() -> Client:
    try:
        client = Client(API_KEY, API_SECRET, tld='us')
        account = client.get_account()
        logger.info(f"Connected to Binance.US. Account: {account.get('accountType', 'N/A')}")
        return client
    except Exception as e:
        logger.error(f"Client init failed: {e}")
        raise

# === TRADING BOT CORE ===
class SmartTradingBot:
    def __init__(self):
        self.client: Client = get_binance_client()
        self.valid_symbols: Set[str] = set()
        self.valid_symbols_dict: Dict[str, Dict] = {}
        self.active_grid_symbols: Dict[str, Dict] = {}
        self.trailing_buy_active: Dict[str, Dict] = {}
        self.trailing_sell_active: Dict[str, Dict] = {}
        self.load_state()
        self.refresh_symbols()

    def load_state(self):
        config = load_config()
        self.active_grid_symbols = config.get("active_grid_symbols", {})
        self.trailing_buy_active = config.get("trailing_buy_active", {})
        self.trailing_sell_active = config.get("trailing_sell_active", {})

    def save_state(self):
        config = {
            "active_grid_symbols": self.active_grid_symbols,
            "trailing_buy_active": self.trailing_buy_active,
            "trailing_sell_active": self.trailing_sell_active
        }
        save_config(config)

    # === SYMBOL & PRECISION ===
    def refresh_symbols(self):
        try:
            exchange_info = self.client.get_exchange_info()
            symbols = []
            for s in exchange_info['symbols']:
                if (s['quoteAsset'] != 'USDT' or
                    s['status'] != 'TRADING' or
                    s['baseAsset'] in ['UST', 'BUSD', 'LUNA']):
                    continue

                # Find PRICE_FILTER and LOT_SIZE
                price_filter = next((f for f in s['filters'] if f['filterType'] == 'PRICE_FILTER'), {})
                lot_size = next((f for f in s['filters'] if f['filterType'] == 'LOT_SIZE'), {})

                if not price_filter or not lot_size:
                    continue

                tick_size = float(price_filter.get('tickSize', 0))
                min_qty = float(lot_size.get('minQty', 0))
                step_size = float(lot_size.get('stepSize', 0))

                if tick_size <= 0 or min_qty <= 0 or step_size <= 0:
                    continue

                symbol = s['symbol']
                symbols.append(symbol)
                self.valid_symbols_dict[symbol] = {
                    'min_qty': min_qty,
                    'step_size': step_size,
                    'tick_size': tick_size,
                    'min_notional': float(price_filter.get('minNotional', 0))
                }

            self.valid_symbols = set(symbols)
            logger.info(f"Loaded {len(self.valid_symbols)} valid USDT pairs from Binance.US")
        except Exception as e:
            logger.error(f"Symbol refresh failed: {e}")

    def round_price(self, price: float, symbol: str) -> float:
        info = self.valid_symbols_dict.get(symbol, {})
        tick_size = info.get('tick_size', 1e-8)
        if tick_size == 0:
            return round(price, 8)
        precision = int(round(-math.log10(tick_size)))
        return round(price, precision)

    def round_qty(self, qty: float, symbol: str) -> float:
        info = self.valid_symbols_dict.get(symbol, {})
        step_size = info.get('step_size', 1e-8)
        if step_size == 0:
            return round(qty, 8)
        precision = int(round(-math.log10(step_size)))
        return round(qty, precision)

    # === MARKET DATA ===
    def get_server_time(self) -> int:
        try:
            return self.client.get_server_time()['serverTime']
        except:
            return int(time.time() * 1000)

    def get_ohlcv(self, symbol: str, interval: str = INTERVAL_5M, limit: int = 100) -> Optional[pd.DataFrame]:
        try:
            klines = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
            if not klines:
                return None
            df = pd.DataFrame(klines, columns=[
                'open_time', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)
            return df
        except Exception as e:
            logger.warning(f"OHLCV fetch failed for {symbol}: {e}")
            return None

    def get_24h_price_stats(self, symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        try:
            ticker = self.client.get_ticker(symbol=symbol)
            return (
                float(ticker['lowPrice']),
                float(ticker['highPrice']),
                float(ticker['priceChangePercent'])
            )
        except:
            return None, None, None

    def get_order_book(self, symbol: str, limit: int = 100) -> Dict:
        try:
            return self.client.get_order_book(symbol=symbol, limit=limit)
        except:
            return {}

    def get_order_book_analysis(self, symbol: str) -> Dict:
        ob = self.get_order_book(symbol)
        bids = [(float(p), float(q)) for p, q in ob.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in ob.get("asks", [])]
        if not bids or not asks:
            return {
                'best_bid': 0.0, 'best_ask': 0.0, 'mid_price': 0.0,
                'pct_bid': 50.0, 'raw_bids': [], 'raw_asks': [],
                'depth_skew': 'balanced', 'imbalance_ratio': 1.0,
                'weighted_pressure': 0.0
            }
        bid_vol = sum(q for _, q in bids[:20])
        ask_vol = sum(q for _, q in asks[:20])
        total = bid_vol + ask_vol
        pct_bid = (bid_vol / total * 100) if total > 0 else 50.0
        mid = (bids[0][0] + asks[0][0]) / 2

        bid_pressure = sum(q * (1 / (1 + abs(p - mid))) for p, q in bids[:10])
        ask_pressure = sum(q * (1 / (1 + abs(p - mid))) for p, q in asks[:10])
        weighted_pressure = (bid_pressure - ask_pressure) / (bid_pressure + ask_pressure) if (bid_pressure + ask_pressure) > 0 else 0.0

        skew = "strong_bid" if pct_bid > 65 else "strong_ask" if pct_bid < 35 else "balanced"
        imbalance = min(bid_vol, ask_vol) / max(bid_vol, ask_vol) if max(bid_vol, ask_vol) > 0 else 1.0

        return {
            'best_bid': float(bids[0][0]), 'best_ask': float(asks[0][0]), 'mid_price': float(mid),
            'pct_bid': pct_bid, 'raw_bids': bids, 'raw_asks': asks,
            'depth_skew': skew, 'imbalance_ratio': imbalance,
            'weighted_pressure': weighted_pressure
        }

    # === INDICATORS ===
    def get_rsi_and_trend(self, symbol: str) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        df = self.get_ohlcv(symbol, limit=50)
        if df is None or len(df) < 14:
            return None, None, None
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs)).iloc[-1]
        trend = "bullish" if df['close'].iloc[-1] > df['close'].iloc[-10] else "bearish"
        return float(rsi), trend, float(df['close'].iloc[-1])

    def get_macd_bullish_with_histogram(self, symbol: str) -> Tuple[bool, Optional[float], int, Optional[float]]:
        try:
            df = self.get_ohlcv(symbol, interval='5m', limit=100)
            if df is None or len(df) < 50:
                return False, None, 0, None

            exp1 = df['close'].ewm(span=12).mean()
            exp2 = df['close'].ewm(span=26).mean()
            macd = exp1 - exp2
            signal = macd.ewm(span=9).mean()
            hist = macd - signal

            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss.replace(0, 1e-10)
            rsi = 100 - (100 / (1 + rs)).iloc[-1]

            hist_series = hist.iloc[-20:]
            lookback = 0
            for i in range(len(hist_series)-1, -1, -1):
                if hist_series.iloc[i] <= 0:
                    break
                lookback += 1

            momentum_ok = (
                hist.iloc[-1] > 0 and
                hist.iloc[-1] > hist.iloc[-2] and
                macd.iloc[-1] > signal.iloc[-1] and
                lookback <= 6
            )

            return momentum_ok, float(rsi), lookback, float(hist.iloc[-1])
        except Exception as e:
            logger.warning(f"MACD analysis failed for {symbol}: {e}")
            return False, None, 0, None

    # === BALANCE & PORTFOLIO ===
    def get_balance(self, asset: str = 'USDT') -> Decimal:
        try:
            account = self.client.get_account()
            for bal in account['balances']:
                if bal['asset'] == asset and float(bal['free']) > 0:
                    return Decimal(bal['free'])
            return Decimal('0')
        except Exception as e:
            logger.warning(f"Balance fetch failed: {e}")
            return Decimal('0')

    def get_trade_fees(self, symbol: str) -> Tuple[float, float]:
        return FEE_MAKER, FEE_TAKER

    def calculate_total_portfolio_value(self) -> Tuple[Decimal, Dict]:
        total = self.get_balance('USDT')
        positions_value = Decimal('0')
        details = {}
        with DBManager() as sess:
            positions = sess.query(Position).filter_by(status='open').all()
            for pos in positions:
                ob = self.get_order_book_analysis(pos.symbol)
                price = Decimal(str(ob['best_bid'] or ob['best_ask'] or 0))
                value = Decimal(str(pos.quantity)) * price
                positions_value += value
                details[pos.symbol] = float(value)
        total += positions_value
        return total, details

    # === ORDER MANAGEMENT ===
    def place_market_buy(self, symbol: str, amount_usdt: float) -> Optional[str]:
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            price = float(ticker['price'])
            quantity = amount_usdt / price
            quantity = self.round_qty(quantity, symbol)

            min_notional = self.valid_symbols_dict.get(symbol, {}).get('min_notional', 10.0)
            if quantity * price < min_notional:
                logger.warning(f"Order too small for {symbol}: {quantity * price:.2f} < {min_notional}")
                return None

            order = self.client.order_market_buy(symbol=symbol, quantity=quantity)
            order_id = str(order['orderId'])
            logger.info(f"Market BUY {symbol}: {quantity:.6f} @ ~${price:.4f} (ID: {order_id})")

            with DBManager() as sess:
                pos = Position(
                    symbol=symbol,
                    quantity=quantity,
                    avg_entry_price=price,
                    side='long'
                )
                sess.add(pos)
            return order_id
        except Exception as e:
            logger.error(f"Buy order failed for {symbol}: {e}")
            return None

    def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Optional[str]:
        try:
            price = self.round_price(price, symbol)
            order = self.client.order_limit_sell(symbol=symbol, quantity=quantity, price=price)
            order_id = str(order['orderId'])
            logger.info(f"Limit SELL {symbol}: {quantity:.6f} @ ${price:.4f} (ID: {order_id})")
            return order_id
        except Exception as e:
            logger.error(f"Sell order failed for {symbol}: {e}")
            return None

    def place_limit_buy(self, symbol: str, quantity: float, price: float) -> Optional[str]:
        try:
            price = self.round_price(price, symbol)
            quantity = self.round_qty(quantity, symbol)
            order = self.client.order_limit_buy(symbol=symbol, quantity=quantity, price=price)
            order_id = str(order['orderId'])
            logger.info(f"Limit BUY {symbol}: {quantity:.6f} @ ${price:.4f} (ID: {order_id})")
            return order_id
        except Exception as e:
            logger.error(f"Buy order failed for {symbol}: {e}")
            return None

    def place_market_sell(self, symbol: str, quantity: float) -> Optional[str]:
        try:
            quantity = self.round_qty(quantity, symbol)
            order = self.client.order_market_sell(symbol=symbol, quantity=quantity)
            order_id = str(order['orderId'])
            logger.info(f"Market SELL {symbol}: {quantity:.6f} (ID: {order_id})")
            with DBManager() as sess:
                pos = sess.query(Position).filter_by(symbol=symbol, status='open').first()
                if pos:
                    pos.status = 'closed'
            return order_id
        except Exception as e:
            logger.error(f"Sell failed: {e}")
            return None

    def cancel_order(self, symbol: str, order_id: int) -> bool:
        try:
            self.client.cancel_order(symbol=symbol, orderId=order_id)
            logger.info(f"Cancelled order {order_id} for {symbol}")
            return True
        except:
            return False

    # === STRATEGY LOGIC ===
    def get_top_priority_coins(self, limit: int = MAX_SCALED_COINS) -> List[str]:
        priorities = []
        for sym in list(self.valid_symbols)[:50]:
            ob = self.get_order_book_analysis(sym)
            if ob['best_bid'] == 0:
                continue
            rsi, trend, _ = self.get_rsi_and_trend(sym)
            momentum_ok, _, lookback, _ = self.get_macd_bullish_with_histogram(sym)
            low, _, pct_change = self.get_24h_price_stats(sym)

            score = 0
            if rsi and rsi <= RSI_OVERSOLD and trend == 'bullish':
                score += 30
            if momentum_ok and lookback <= 4:
                score += 40
            if ob['weighted_pressure'] < -0.001:
                score += 20
            if pct_change and pct_change > -5:
                score += 10

            if score > 50:
                priorities.append((sym, score))

        priorities.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in priorities[:limit]]

    def place_scaled_grids(self):
        top_coins = self.get_top_priority_coins()
        balance = float(self.get_balance('USDT'))
        levels = min(8, int(balance // 25))

        for sym in top_coins:
            if sym in self.active_grid_symbols or balance < 50:
                continue

            ob = self.get_order_book_analysis(sym)
            current_price = ob['mid_price']
            if current_price < 0.0001 or current_price > 10000:
                continue

            grid = {
                'symbol': sym,
                'base_price': current_price,
                'levels': levels,
                'grid_pct': 0.01,
                'buy_orders': [],
                'sell_orders': []
            }

            for i in range(1, levels + 1):
                buy_price = current_price * (1 - i * grid['grid_pct'])
                qty = GRID_SIZE_USDT / buy_price
                qty = self.round_qty(qty, sym)
                order_id = self.place_limit_buy(sym, qty, buy_price)
                if order_id:
                    grid['buy_orders'].append({'price': buy_price, 'qty': qty, 'id': order_id})

            self.active_grid_symbols[sym] = grid
            balance -= levels * GRID_SIZE_USDT

        self.save_state()

    def manage_trailing_stops(self):
        for sym, state in list(trailing_sell_active.items()):
            ob = self.get_order_book_analysis(sym)
            current = ob['best_bid']
            peak = float(state['peak_price'])
            trail_stop = peak * (1 - TRAILING_STOP_PCT)
            if current <= trail_stop:
                with DBManager() as sess:
                    pos = sess.query(Position).filter_by(symbol=sym, status='open').first()
                    if pos:
                        self.place_market_sell(sym, float(pos.quantity))
                        pos.status = 'closed'
                        del trailing_sell_active[sym]
        self.save_state()

    
    # ==============================================================
    # === DASHBOARD =================================================
    # ==============================================================

    def print_professional_dashboard(self) -> None:
        """Print live dashboard – refreshed every few seconds."""
        try:
            os.system('cls' if os.name == 'nt' else 'clear')

            class Color:
                GREEN   = Fore.GREEN + Style.BRIGHT
                RED     = Fore.RED + Style.BRIGHT
                YELLOW  = Fore.YELLOW + Style.BRIGHT
                CYAN    = Fore.CYAN + Style.BRIGHT
                MAGENTA = Fore.MAGENTA + Style.BRIGHT
                WHITE   = Fore.WHITE + Style.BRIGHT
                GRAY    = Fore.LIGHTBLACK_EX          # Fixed
                RESET   = Style.RESET_ALL
                BOLD    = Style.BRIGHT
                DIM     = Style.DIM

            DIVIDER = "=" * 120
            print(Color.CYAN + DIVIDER)
            print(f"{Color.BOLD}{Color.WHITE}{'INFINITY GRID + TRAILING HYBRID BOT - BINANCE.US':^120}{Color.RESET}")
            print(Color.CYAN + DIVIDER)

            now_str = now_cst()
            usdt_free = self.get_balance('USDT')
            total_port, _ = self.calculate_total_portfolio_value()
            tbuy_cnt = len(trailing_buy_active)
            tsel_cnt = len(trailing_sell_active)
            grid_count = sum(len(s['buy_orders']) for s in active_grid_symbols.values())

            print(f"{Color.GRAY}Time (CST):{Color.RESET} {Color.YELLOW}{now_str}{Color.RESET}")
            print(f"{Color.GRAY}Free USDT:{Color.RESET}  ${Color.GREEN}{float(usdt_free):,.2f}{Color.RESET}")
            print(f"{Color.GRAY}Portfolio:{Color.RESET}  ${Color.CYAN}{float(total_port):,.2f}{Color.RESET}")
            print(f"{Color.GRAY}Trailing: {Color.MAGENTA}{tbuy_cnt}{Color.RESET} buys, {Color.MAGENTA}{tsel_cnt}{Color.RESET} sells")
            print(f"{Color.GRAY}Grid Orders:{Color.RESET} {Color.YELLOW}{grid_count}{Color.RESET} × $5")

            balance = float(usdt_free)
            mode, _ = self._determine_strategy_mode(balance, grid_count, tbuy_cnt + tsel_cnt)
            mode_color = Color.GREEN if "GRID" in mode else Color.YELLOW if tbuy_cnt + tsel_cnt > 0 else Color.RED
            print(f"\n{Color.BOLD}Strategy Mode:{Color.RESET} {mode_color}{mode}{Color.RESET}")

            # Depth Imbalance Bars
            print(f"\n{Color.BOLD}DEPTH IMBALANCE (Top 10 by Volume){Color.RESET}")
            self._print_depth_imbalance_bars()

            # Order Book Ladder
            print(Color.CYAN + DIVIDER)
            print(f"{Color.BOLD}ORDER BOOK LADDER (Top 3 Active){Color.RESET}")
            self._print_order_book_ladders()

            # Positions & P&L
            print(Color.CYAN + DIVIDER)
            print(f"{Color.BOLD}{'CURRENT POSITIONS & P&L':^120}{Color.RESET}")
            total_pnl = self._print_positions_table()

            # Market Overview
            print(Color.CYAN + DIVIDER)
            print(f"{Color.BOLD}{'MARKET OVERVIEW':^120}{Color.RESET}")
            self._print_market_overview_and_signals()

            print(Color.CYAN + DIVIDER)
            total_pnl_color = Color.GREEN if total_pnl > 0 else Color.RED
            print(f"{Color.BOLD}TOTAL UNREALIZED P&L: {total_pnl_color}${float(total_pnl):,.2f}{Color.RESET}")
            print(Color.CYAN + DIVIDER)

        except Exception as e:
            logger.error(f"Dashboard error: {e}")
            print(f"{Fore.RED}DASHBOARD ERROR: {e}{Style.RESET_ALL}")

    def _determine_strategy_mode(self, balance: float, grid_count: int, trailing_count: int) -> Tuple[str, int]:
        if balance >= 80:
            levels = min(8, int((balance - 20) // 5))
            return f"INFINITY GRID ({levels} levels)", levels
        elif balance >= 60:
            levels = min(6, max(4, int((balance - 20) // 5)))
            return f"INFINITY GRID ({levels} levels)", levels
        elif balance >= 40:
            return "INFINITY GRID (3 levels)", 3
        else:
            return "TRAILING MOMENTUM", 0

    def _print_depth_imbalance_bars(self) -> None:
        sorted_symbols = sorted(self.valid_symbols_dict.keys())[:10]
        BAR_WIDTH = 50
        for sym in sorted_symbols:
            ob = self.get_order_book_analysis(sym)
            pct_bid = ob['pct_bid']
            pct_ask = 100 - pct_bid
            bid_blocks = int(pct_bid / 2)
            ask_blocks = int(pct_ask / 2)
            neutral = BAR_WIDTH - bid_blocks - ask_blocks
            bar = (Fore.GREEN + "█" * bid_blocks +
                   Fore.LIGHTBLACK_EX + "░" * max(0, neutral) +
                   Fore.RED + "█" * ask_blocks + Style.RESET_ALL)
            bias = ("strong bid wall" if pct_bid > 60 else
                    "strong ask pressure" if pct_bid < 40 else "balanced")
            coin = sym.replace("USDT", "")
            print(f"{Fore.CYAN}{coin:<8}{Style.RESET_ALL} |{bar}| {Fore.GREEN}{pct_bid:>3.0f}%{Style.RESET_ALL} / {Fore.RED}{pct_ask:>3.0f}%{Style.RESET_ALL}")
            print(f"{'':<10} {Style.DIM}{bias}{Style.RESET_ALL}")
            print()

    def _print_order_book_ladders(self) -> None:
        ladder_symbols = list(active_grid_symbols.keys())[:3] or list(self.valid_symbols)[:3]
        for sym in ladder_symbols:
            ob = self.get_order_book_analysis(sym)
            bids = ob.get('raw_bids', [])[:5]
            asks = ob.get('raw_asks', [])[:5]
            mid = ob['mid_price']
            coin = sym.replace("USDT", "")
            print(f"  {Fore.MAGENTA}{coin:>8}{Style.RESET_ALL} | Mid: ${Fore.YELLOW}{mid:,.4f}{Style.RESET_ALL}")
            print(f"    {Fore.GREEN}BIDS{' ' * 22}|{' ' * 27}ASKS{Style.RESET_ALL}")
            print(f"    {'-' * 27} | {'-' * 27}")
            for i in range(5):
                bid_price = float(bids[i][0]) if i < len(bids) else 0.0
                bid_qty = float(bids[i][1]) if i < len(bids) else 0.0
                ask_price = float(asks[i][0]) if i < len(asks) else 0.0
                ask_qty = float(asks[i][1]) if i < len(asks) else 0.0
                print(f"    {Fore.GREEN}{bid_price:>10.4f} x {bid_qty:>7.2f}{Style.RESET_ALL} | "
                      f"{Fore.RED}{ask_price:<10.4f} x {ask_qty:>7.2f}{Style.RESET_ALL}")
            print()

    def _print_positions_table(self) -> Decimal:
        print(f"{'SYM':<8} {'QTY':>10} {'ENTRY':>12} {'CUR':>12} {'RSI':>6} {'P&L%':>8} {'PROFIT':>10} {'STATUS'}")
        print("-" * 120)

        with DBManager() as sess:
            positions_list = sess.query(Position).filter_by(status='open').all()[:15]

        total_pnl = Decimal('0')
        for pos in positions_list:
            sym = pos.symbol
            qty = float(pos.quantity)
            entry = float(pos.avg_entry_price)
            ob = self.get_order_book_analysis(sym)
            cur = float(ob['best_bid'] or ob['best_ask'] or 0)
            rsi, _, _ = self.get_rsi_and_trend(sym)
            rsi_str = f"{rsi:5.1f}" if rsi else "N/A "

            maker, taker = self.get_trade_fees(sym)
            gross = (cur - entry) * qty
            fee = (maker + taker) * cur * qty
            net = gross - fee
            total_pnl += Decimal(str(net))

            pnl_pct = ((cur - entry) / entry - (maker + taker)) * 100 if entry > 0 else 0.0

            status = "Spot Hold"
            if sym in trailing_sell_active:
                state = trailing_sell_active[sym]
                peak = float(state['peak_price'])
                stop = peak * (1 - TRAILING_STOP_PCT)
                status = f"Trail Sell @ {stop:.4f}"

            pnl_color = Fore.GREEN if net > 0 else Fore.RED
            pct_color = Fore.GREEN if pnl_pct > 0 else Fore.RED

            print(f"{Fore.CYAN}{sym:<8}{Style.RESET_ALL} "
                  f"{qty:>10.6f} {entry:>12.4f} {cur:>12.4f} "
                  f"{Fore.YELLOW}{rsi_str}{Style.RESET_ALL} "
                  f"{pct_color}{pnl_pct:>7.2f}%{Style.RESET_ALL} "
                  f"{pnl_color}{net:>9.2f}{Style.RESET_ALL} "
                  f"{Style.DIM}{status}{Style.RESET_ALL}")

        for _ in range(len(positions_list), 15):
            print(" " * 120)

        return total_pnl

    def _print_market_overview_and_signals(self) -> None:
        valid_cnt = len(self.valid_symbols)
        avg_vol = 1000000.0  # Replace with real calc later

        print(f"Valid USDT Pairs: {Fore.CYAN}{valid_cnt}{Style.RESET_ALL}")
        print(f"Avg 24h Volume: ${Fore.YELLOW}{avg_vol:,.0f}{Style.RESET_ALL}")
        print(f"Price Range: ${Fore.LIGHTBLACK_EX}{MIN_PRICE}{Style.RESET_ALL} → ${Fore.LIGHTBLACK_EX}{MAX_PRICE}{Style.RESET_ALL}")

        watch_items = []
        for sym in list(self.valid_symbols)[:20]:
            ob = self.get_order_book_analysis(sym)
            rsi, trend, _ = self.get_rsi_and_trend(sym)
            if not rsi: continue
            low, _, _ = self.get_24h_price_stats(sym)
            if not low: continue
            strong_buy = (ob['depth_skew'] == 'strong_ask' and
                          ob['imbalance_ratio'] <= 0.5 and
                          ob['weighted_pressure'] < -0.002)

            if (rsi <= RSI_OVERSOLD and trend == 'bullish' and
                ob['best_bid'] <= low * 1.01 and strong_buy):
                coin = sym.replace('USDT', '')
                watch_items.append(f"{coin}({rsi:.0f})")

        signal_str = " | ".join(watch_items[:10]) if watch_items else "No strong buy signals"
        if len(signal_str) > 80:
            signal_str = signal_str[:77] + "..."

        print(f"\n{Fore.GREEN}{Style.BRIGHT}Smart Buy Signals:{Style.RESET_ALL} {Fore.GREEN}{signal_str}{Style.RESET_ALL}")


    # ==============================================================
    # === MAIN LOOP =================================================
    # ==============================================================

    def run(self):
        """Main trading loop + dashboard thread."""
        logger.info("Starting Binance.US Trading Bot...")
        dashboard_thread = threading.Thread(target=self.dashboard_loop, daemon=True)
        dashboard_thread.start()

        while True:
            try:
                self.manage_trailing_stops()
                self.place_scaled_grids()

                # Refresh symbols hourly
                if int(time.time()) % 3600 < 5:
                    self.refresh_symbols()

                time.sleep(30)
            except KeyboardInterrupt:
                logger.info("Shutting down gracefully...")
                self.save_state()
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                time.sleep(60)

    def dashboard_loop(self):
        """Background thread: refresh dashboard."""
        while True:
            try:
                self.print_professional_dashboard()
                time.sleep(3)
            except Exception as e:
                logger.error(f"Dashboard loop error: {e}")
                time.sleep(5)


    # ==============================================================
    # === ORDER MANAGEMENT ==========================================
    # ==============================================================

    def round_price(self, price: float, symbol: str) -> float:
        info = self.valid_symbols_dict.get(symbol, {})
        tick_size = info.get('tick_size', 1e-8)
        if tick_size == 0:
            return round(price, 8)
        precision = int(round(-math.log10(tick_size)))
        return round(price, precision)

    def round_qty(self, qty: float, symbol: str) -> float:
        info = self.valid_symbols_dict.get(symbol, {})
        step_size = info.get('step_size', 1e-8)
        if step_size == 0:
            return round(qty, 8)
        precision = int(round(-math.log10(step_size)))
        return round(qty, precision)

    def place_market_buy(self, symbol: str, amount_usdt: float) -> Optional[str]:
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            price = float(ticker['price'])
            quantity = amount_usdt / price
            quantity = self.round_qty(quantity, symbol)

            min_notional = self.valid_symbols_dict.get(symbol, {}).get('min_notional', 10.0)
            if quantity * price < min_notional:
                logger.warning(f"Order too small: {quantity * price:.2f} < {min_notional}")
                return None

            order = self.client.order_market_buy(symbol=symbol, quantity=quantity)
            order_id = str(order['orderId'])
            logger.info(f"BUY {symbol}: {quantity:.6f} @ ${price:.4f}")

            with DBManager() as sess:
                sess.add(Position(
                    symbol=symbol,
                    quantity=quantity,
                    avg_entry_price=price,
                    side='long'
                ))
            return order_id
        except Exception as e:
            logger.error(f"Buy failed: {e}")
            return None

    def place_limit_buy(self, symbol: str, quantity: float, price: float) -> Optional[str]:
        try:
            price = self.round_price(price, symbol)
            quantity = self.round_qty(quantity, symbol)
            order = self.client.order_limit_buy(symbol=symbol, quantity=quantity, price=price)
            order_id = str(order['orderId'])
            logger.info(f"Limit BUY {symbol}: {quantity:.6f} @ ${price:.4f}")
            return order_id
        except Exception as e:
            logger.error(f"Limit buy failed: {e}")
            return None

    def place_market_sell(self, symbol: str, quantity: float) -> Optional[str]:
        try:
            quantity = self.round_qty(quantity, symbol)
            order = self.client.order_market_sell(symbol=symbol, quantity=quantity)
            order_id = str(order['orderId'])
            logger.info(f"SELL {symbol}: {quantity:.6f}")
            with DBManager() as sess:
                pos = sess.query(Position).filter_by(symbol=symbol, status='open').first()
                if pos:
                    pos.status = 'closed'
            return order_id
        except Exception as e:
            logger.error(f"Sell failed: {e}")
            return None

    # ==============================================================
    # === STRATEGY LOGIC ============================================
    # ==============================================================

    def get_top_priority_coins(self, limit: int = MAX_SCALED_COINS) -> List[str]:
        priorities = []
        for sym in list(self.valid_symbols)[:50]:
            ob = self.get_order_book_analysis(sym)
            if ob['best_bid'] == 0: continue
            rsi, trend, _ = self.get_rsi_and_trend(sym)
            momentum_ok, _, lookback, _ = self.get_macd_bullish_with_histogram(sym)
            low, _, pct_change = self.get_24h_price_stats(sym)

            score = 0
            if rsi and rsi <= RSI_OVERSOLD and trend == 'bullish':
                score += 30
            if momentum_ok and lookback <= 4:
                score += 40
            if ob['weighted_pressure'] < -0.001:
                score += 20
            if pct_change and pct_change > -5:
                score += 10

            if score > 50:
                priorities.append((sym, score))

        priorities.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in priorities[:limit]]

    def place_scaled_grids(self):
        top_coins = self.get_top_priority_coins()
        balance = float(self.get_balance('USDT'))
        levels = min(8, int(balance // 25))

        for sym in top_coins:
            if sym in self.active_grid_symbols or balance < 50:
                continue

            ob = self.get_order_book_analysis(sym)
            current_price = ob['mid_price']
            if current_price < 0.0001 or current_price > 10000:
                continue

            grid = {
                'symbol': sym,
                'base_price': current_price,
                'levels': levels,
                'grid_pct': 0.01,
                'buy_orders': [],
                'sell_orders': []
            }

            for i in range(1, levels + 1):
                buy_price = current_price * (1 - i * grid['grid_pct'])
                qty = GRID_SIZE_USDT / buy_price
                qty = self.round_qty(qty, sym)
                order_id = self.place_limit_buy(sym, qty, buy_price)
                if order_id:
                    grid['buy_orders'].append({'price': buy_price, 'qty': qty, 'id': order_id})

            self.active_grid_symbols[sym] = grid
            balance -= levels * GRID_SIZE_USDT

        self.save_state()

    def manage_trailing_stops(self):
        for sym, state in list(trailing_sell_active.items()):
            ob = self.get_order_book_analysis(sym)
            current = ob['best_bid']
            peak = float(state['peak_price'])
            trail_stop = peak * (1 - TRAILING_STOP_PCT)
            if current <= trail_stop:
                with DBManager() as sess:
                    pos = sess.query(Position).filter_by(symbol=sym, status='open').first()
                    if pos:
                        self.place_market_sell(sym, float(pos.quantity))
                        pos.status = 'closed'
                        del trailing_sell_active[sym]
        self.save_state()


# ==============================================================
# === MAIN ======================================================
# ==============================================================

if __name__ == "__main__":
    bot = SmartTradingBot()
    bot.run()
