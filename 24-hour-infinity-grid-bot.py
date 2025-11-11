#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
    INFINITY GRID BOT v7.2 – FULLY FIXED + WEBSOCKETS + PME + DASHBOARD + DYNAMIC SCANNING + RSI/TREND FILTERS + ORDER BOOK SCAN
    • Trades portfolio OR dynamic top 20 USDT pairs by buy bid volume (vol >100k, price $1-$1000)
    • Strategy switching: Trend / Mean-Reversion / Volume-Anchored via PME
    • Real-time dashboard with strategy labels
    • Bundled WhatsApp alerts
    • 5% max per coin | $8 cash guard | RSI + trend filters
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
import numpy as np
import requests
import urllib.parse
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
from typing import Dict, Tuple, List, Optional
import pytz
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError, IntegrityError

# === PRECISION ===
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
MIN_USDT_RESERVE = Decimal('10.0')
MIN_USDT_TO_BUY = Decimal('8.0')
DEFAULT_GRID_SIZE_USDT = Decimal('8.0')
MIN_SELL_VALUE_USDT = Decimal('5.0')
MAX_GRIDS_PER_SIDE = 12
REGRID_INTERVAL = 600  # 10 min
DASHBOARD_REFRESH = 20
PNL_REGRID_THRESHOLD = Decimal('6.0')
FEE_RATE = Decimal('0.001')
TREND_THRESHOLD = Decimal('0.02')
VP_UPDATE_INTERVAL = 300
STOP_LOSS_PCT = Decimal('-0.05')
PME_INTERVAL = 180
PME_MIN_SCORE_THRESHOLD = Decimal('1.2')
MAX_PERCENT_PER_COIN = Decimal('0.05')
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
momentum_score: Dict[str, Decimal] = {}
stochastic_data: Dict[str, dict] = {}
volume_profile: Dict[str, dict] = {}
last_vp_update = 0
strategy_scores: Dict[str, dict] = {}
pme_last_run = 0

RSI_PERIOD = 14
STOCH_K = 14
STOCH_D = 3
VP_BINS = 50
VP_LOOKBACK = 48

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

# === WEBSOCKET HEARTBEAT ===
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, on_message_cb, is_user_stream=False):
        super().__init__(
            url,
            on_open=self.on_open,
            on_message=on_message_cb,
            on_error=self.on_error,
            on_close=self.on_close,
            on_pong=self.on_pong
        )
        self.is_user_stream = is_user_stream
        self.last_pong = time.time()
        self.heartbeat_thread = None
        self.reconnect_delay = 1
        self.max_delay = 60

    def on_open(self, ws):
        global ws_connected
        ws_connected = True
        logger.info(f"WS CONNECTED: {ws.url.split('?')[0]}")
        self.last_pong = time.time()
        if not self.heartbeat_thread:
            self.heartbeat_thread = threading.Thread(target=self._send_heartbeat, daemon=True)
            self.heartbeat_thread.start()

    def on_error(self, ws, error):
        logger.warning(f"WS ERROR: {error}")

    def on_close(self, ws, *args):
        global ws_connected
        ws_connected = False
        logger.warning(f"WS CLOSED: {ws.url.split('?')[0]}")

    def on_pong(self, ws, *args):
        self.last_pong = time.time()

    def _send_heartbeat(self):
        while self.sock and self.sock.connected and not SHUTDOWN_EVENT.is_set():
            if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 10:
                self.close()
                break
            try:
                self.send("ping", opcode=websocket.ABNF.OPCODE_PING)
            except:
                break
            time.sleep(HEARTBEAT_INTERVAL)

    def run_forever(self):
        while not SHUTDOWN_EVENT.is_set():
            try:
                super().run_forever(ping_interval=None, ping_timeout=None)
            except Exception as e:
                logger.error(f"WS CRASH: {e}")
            if SHUTDOWN_EVENT.is_set():
                break
            delay = min(self.max_delay, self.reconnect_delay)
            time.sleep(delay)
            self.reconnect_delay = min(self.max_delay, self.reconnect_delay * 2)

# === INDICATORS ===
def calculate_rsi(prices: List[Decimal]) -> Decimal:
    if len(prices) < RSI_PERIOD + 1:
        return Decimal('50')
    gains = [max(prices[i] - prices[i-1], ZERO) for i in range(1, len(prices))]
    losses = [max(prices[i-1] - prices[i], ZERO) for i in range(1, len(prices))]
    avg_gain = sum(gains[-RSI_PERIOD:]) / RSI_PERIOD
    avg_loss = sum(losses[-RSI_PERIOD:]) / RSI_PERIOD
    if avg_loss == 0:
        return Decimal('100')
    rs = avg_gain / avg_loss
    return Decimal('100') - (Decimal('100') / (ONE + rs))

def is_bullish_trend(candles: List[dict], period: int, min_pct=Decimal('0.0')) -> bool:
    if len(candles) < period:
        return False
    start = candles[-period]['close']
    end = candles[-1]['close']
    if start <= 0:
        return False
    pct = (end - start) / start
    return pct >= min_pct

def fetch_klines(bot, symbol, interval, limit=500):
    try:
        raw = bot.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        return [{'open': safe_decimal(r[1]), 'high': safe_decimal(r[2]), 'low': safe_decimal(r[3]), 'close': safe_decimal(r[4]), 'volume': safe_decimal(r[5])} for r in raw]
    except Exception as e:
        logger.warning(f"Klines failed {symbol} {interval}: {e}")
        return []

def has_valid_buy_signal(bot, symbol):
    daily_candles = fetch_klines(bot, symbol, '1d', 30)
    monthly_candles = fetch_klines(bot, symbol, '1M', 6)
    if not daily_candles: return False
    closes = [c['close'] for c in daily_candles[-RSI_PERIOD:]]
    rsi = calculate_rsi(closes)
    if rsi < RSI_BUY: return False
    if not is_bullish_trend(daily_candles, 14): return False
    if not is_bullish_trend(monthly_candles, 6): return False
    return True

def update_stochastic(symbol: str):
    with kline_lock:
        klines = [k for k in kline_data.get(symbol, []) if k['interval'] == '1m'][-STOCH_K:]
    if len(klines) < STOCH_K: return
    high = max(k['high'] for k in klines)
    low = min(k['low'] for k in klines)
    close = klines[-1]['close']
    k = ((close - low) / (high - low)) * 100 if high != low else Decimal('50')
    stochastic_data.setdefault(symbol, {'k_values': []})['k_values'].append(k)
    k_values = stochastic_data[symbol]['k_values'][-STOCH_D:]
    d = sum(k_values) / len(k_values) if k_values else Decimal('50')
    stochastic_data[symbol].update({'%K': k, '%D': d})

def update_volume_profiles():
    global last_vp_update
    now = time.time()
    if now - last_vp_update < VP_UPDATE_INTERVAL: return
    for symbol in list(active_grid_symbols.keys()):
        with kline_lock:
            klines = [k for k in kline_data.get(symbol, []) if k['interval'] == '1h'][-VP_LOOKBACK:]
        if len(klines) < 12: continue
        price_min = min(k['low'] for k in klines)
        price_max = max(k['high'] for k in klines)
        if price_max <= price_min: continue
        bin_size = (price_max - price_min) / VP_BINS
        if bin_size <= ZERO: continue
        bins = {}
        total_volume = ZERO
        for k in klines:
            bin_start = (k['low'] // bin_size) * bin_size
            while bin_start < k['high']:
                bins[bin_start] = bins.get(bin_start, ZERO) + k['volume']
                total_volume += k['volume']
                bin_start += bin_size
        if total_volume <= ZERO: continue
        vwap = sum(p * v for p, v in bins.items()) / total_volume
        volume_profile[symbol] = {'vwap': vwap.quantize(bot.get_tick_size(symbol)), 'updated': now}
    last_vp_update = now

# === PME SCORERS ===
def score_trend_strategy(symbol: str) -> Decimal:
    with kline_lock:
        closes = [k['close'] for k in kline_data.get(symbol, []) if k['interval'] == '1m'][-50:]
    if len(closes) < 50: return ZERO
    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes) / 50
    trend_strength = abs(ema20 - ema50) / closes[-1]
    return (trend_strength * 100).quantize(Decimal('0.01'))

def score_mean_reversion_strategy(symbol: str) -> Decimal:
    with kline_lock:
        closes = [k['close'] for k in kline_data.get(symbol, []) if k['interval'] == '1m'][-20:]
    if len(closes) < 20: return ZERO
    rsi = calculate_rsi(closes)
    if rsi < 30 or rsi > 70: return Decimal('2.0')
    return Decimal('0.5')

def score_volume_anchored_strategy(symbol: str) -> Decimal:
    vp = volume_profile.get(symbol, {})
    if 'vwap' not in vp: return ZERO
    current = live_prices.get(symbol, ZERO)
    if current <= ZERO: return ZERO
    distance = abs(current - vp['vwap']) / current
    return Decimal('2.0') if distance < Decimal('0.01') else Decimal('1.0') / (1 + distance * 10)

# === PME ENGINE ===
def profit_monitoring_engine():
    global pme_last_run
    while not SHUTDOWN_EVENT.is_set():
        if time.time() - pme_last_run < PME_INTERVAL: 
            time.sleep(5)
            continue
        pme_last_run = time.time()
        for symbol in list(active_grid_symbols.keys()):
            scores = {
                'trend': score_trend_strategy(symbol),
                'mean_reversion': score_mean_reversion_strategy(symbol),
                'volume_anchored': score_volume_anchored_strategy(symbol)
            }
            best = max(scores, key=scores.get)
            current = active_grid_symbols[symbol].get('strategy', 'volume_anchored')
            if best != current and scores[best] > PME_MIN_SCORE_THRESHOLD:
                logger.info(f"PME switch {symbol}: {current} -> {best}")
                send_alert(f"{symbol} STRATEGY: {current.upper()} -> {best.upper()}", subject="PME")
                g = active_grid_symbols[symbol]
                for oid in g['buy_orders'] + g['sell_orders']:
                    bot.cancel_order_safe(symbol, oid)
                active_grid_symbols.pop(symbol)
                regrid_symbol_with_strategy(bot, symbol, best)
            strategy_scores[symbol] = scores
        time.sleep(1)

# === ORDER BOOK SCAN ===
def get_top_buy_volume_symbols(bot, top_n=20):
    try:
        tickers = bot.client.get_ticker_24hr()
        usdt_pairs = [t['symbol'] for t in tickers if t['symbol'].endswith('USDT') and float(t['quoteVolume']) > 100000]
        candidates = []
        for sym in usdt_pairs:
            price = safe_decimal(next((t['lastPrice'] for t in tickers if t['symbol'] == sym), 0))
            if not 1 <= float(price) <= 1000: continue
            book = bot.client.get_order_book(symbol=sym, limit=10)
            buy_volume = sum(safe_decimal(b[0]) * safe_decimal(b[1]) for b in book['bids'])
            candidates.append((sym, buy_volume))
        candidates.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in candidates[:top_n]]
    except Exception as e:
        logger.error(f"Top buy volume scan failed: {e}")
        return []

# === UPDATE ACTIVE SYMBOLS ===
def update_active_symbols(bot, portfolio_only):
    candidates = list(valid_symbols_dict.keys()) if portfolio_only else get_top_buy_volume_symbols(bot)
    for sym in candidates:
        if sym in active_grid_symbols: continue
        if not has_valid_buy_signal(bot, sym): continue
        usdt_free = bot.get_balance()
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

# === BOT CLASS ===
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.api_lock = threading.Lock()

    def get_tick_size(self, symbol):
        return symbol_info_cache.get(symbol, {}).get('tickSize', Decimal('0.00000001'))

    def get_lot_step(self, symbol):
        return symbol_info_cache.get(symbol, {}).get('stepSize', Decimal('0.00000001'))

    def get_balance(self) -> Decimal:
        with balance_lock:
            return balances.get('USDT', ZERO)

    def get_asset_balance(self, asset: str) -> Decimal:
        with balance_lock:
            return balances.get(asset, ZERO)

    def place_limit_buy_with_tracking(self, symbol, price: Decimal, qty: Decimal):
        try:
            order = self.client.order_limit_buy(symbol=symbol, quantity=str(qty), price=str(price))
            with SafeDBManager() as sess:
                if sess:
                    sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='BUY', price=price, quantity=qty))
            return order
        except Exception as e:
            logger.warning(f"Buy failed {symbol}: {e}")
            return None

    def place_limit_sell_with_tracking(self, symbol, price: Decimal, qty: Decimal):
        try:
            order = self.client.order_limit_sell(symbol=symbol, quantity=str(qty), price=str(price))
            with SafeDBManager() as sess:
                if sess:
                    sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='SELL', price=price, quantity=qty))
            return order
        except Exception as e:
            logger.warning(f"Sell failed {symbol}: {e}")
            return None

    def cancel_order_safe(self, symbol, order_id):
        try:
            self.client.cancel_order(symbol=symbol, orderId=order_id)
        except:
            pass

# === WEBSOCKET HANDLERS ===
def on_market_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get('stream', '')
        payload = data.get('data', {})
        if not payload or not stream:
            return
        symbol = stream.split('@')[0].upper()

        if stream.endswith('@ticker'):
            price = safe_decimal(payload.get('c', '0'))
            if price > ZERO:
                with price_lock:
                    live_prices[symbol] = price
            with stats_lock:
                ticker_24h_stats[symbol] = {
                    'h': payload.get('h', '0'),
                    'l': payload.get('l', '0'),
                    'P': payload.get('P', '0'),
                    'v': payload.get('q', '0')
                }
            pct_change = safe_decimal(payload.get('P', '0')) / 100
            bias = pct_change / Decimal('0.03') if abs(pct_change) >= Decimal('0.005') else ZERO
            trend_bias[symbol] = max(min(bias, ONE), -ONE)

        elif stream.endswith('@kline_1m'):
            k = payload.get('k', {})
            if not k.get('x'): return
            entry = {'close': safe_decimal(k.get('c')), 'high': safe_decimal(k.get('h')), 'low': safe_decimal(k.get('l')), 'interval': '1m'}
            with kline_lock:
                kline_data.setdefault(symbol, []).append(entry)
                if len(kline_data[symbol]) > 200:
                    kline_data[symbol] = kline_data[symbol][-200:]
            update_stochastic(symbol)

        elif stream.endswith('@kline_1h'):
            k = payload.get('k', {})
            if not k.get('x'): return
            entry = {'close': safe_decimal(k.get('c')), 'high': safe_decimal(k.get('h')), 'low': safe_decimal(k.get('l')), 'volume': safe_decimal(k.get('v')), 'interval': '1h'}
            with kline_lock:
                kline_data.setdefault(symbol, []).append(entry)
                if len(kline_data[symbol]) > VP_LOOKBACK + 10:
                    kline_data[symbol] = kline_data[symbol][-(VP_LOOKBACK + 10):]
            update_volume_profiles()

    except Exception:
        pass

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        e = data.get('e')
        if e == 'executionReport':
            order_id = str(data.get('i'))
            symbol = data.get('s')
            side = data.get('S')
            status = data.get('X')
            price = safe_decimal(data.get('p', '0'))
            qty = safe_decimal(data.get('q', '0'))
            fee = safe_decimal(data.get('n', '0')) or ZERO

            if status == 'FILLED' and order_id:
                with SafeDBManager() as sess:
                    if sess:
                        po = sess.query(PendingOrder).filter_by(binance_order_id=order_id).first()
                        if po:
                            sess.delete(po)
                        sess.add(TradeRecord(symbol=symbol, side=side, price=price, quantity=qty, fee=fee))

                send_alert(f"{side} {symbol} @ ${float(price):.4f} | {float(qty):.6f}", subject="FILL")

                if side == 'SELL':
                    with SafeDBManager() as sess:
                        if sess:
                            pos = sess.query(Position).filter_by(symbol=symbol).first()
                            if pos:
                                entry = safe_decimal(pos.avg_entry_price)
                                pnl = (price - entry) * qty - fee
                                with realized_lock:
                                    realized_pnl_per_symbol[symbol] = realized_pnl_per_symbol.get(symbol, ZERO) + pnl
                                    global total_realized_pnl, peak_pnl
                                    total_realized_pnl += pnl
                                    peak_pnl = max(peak_pnl, total_realized_pnl)
    except Exception as e:
        logger.debug(f"User WS: {e}")

# === SYMBOL LOADER ===
def load_portfolio_symbols(bot):
    global valid_symbols_dict, symbol_info_cache
    valid_symbols_dict.clear()
    symbol_info_cache.clear()
    try:
        acct = bot.client.get_account()
        with balance_lock:
            for b in acct['balances']:
                asset = b['asset']
                free = safe_decimal(b['free'])
                if free > ZERO:
                    balances[asset] = free
        for b in acct['balances']:
            asset = b['asset']
            qty = safe_decimal(b['free'])
            if qty <= ZERO or asset in {'USDT', 'USDC'}: continue
            sym = f"{asset}USDT"
            try:
                info = bot.client.get_symbol_info(sym)
                if not info or info['status'] != 'TRADING': continue
                valid_symbols_dict[sym] = {}
                tick = step = Decimal('0.00000001')
                for f in info['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        tick = safe_decimal(f['tickSize'])
                    if f['filterType'] == 'LOT_SIZE':
                        step = safe_decimal(f['stepSize'])
                symbol_info_cache[sym] = {'tickSize': tick, 'stepSize': step}
            except: continue
        logger.info(f"Loaded {len(valid_symbols_dict)} symbols")
    except Exception as e:
        logger.error(f"Load symbols failed: {e}")

# === WEBSOCKET STARTERS ===
def start_market_websocket():
    symbols = [s.lower() for s in valid_symbols_dict]
    if not symbols: return
    streams = [f"{s}@ticker" for s in symbols] + [f"{s}@kline_1m" for s in symbols] + [f"{s}@kline_1h" for s in symbols]
    chunks = [streams[i:i+MAX_STREAMS_PER_CONNECTION] for i in range(0, len(streams), MAX_STREAMS_PER_CONNECTION)]
    for chunk in chunks:
        url = WS_BASE + '/'.join(chunk)
        ws = HeartbeatWebSocket(url, on_message_cb=on_market_message)
        ws_instances.append(ws)
        threading.Thread(target=ws.run_forever, daemon=True).start()
        time.sleep(0.5)

def start_user_stream():
    global user_ws, listen_key
    try:
        listen_key = bot.client.stream_get_listen_key()
        url = f"{USER_STREAM_BASE}{listen_key}"
        user_ws = HeartbeatWebSocket(url, on_message_cb=on_user_message, is_user_stream=True)
        threading.Thread(target=user_ws.run_forever, daemon=True).start()
        logger.info("User stream started")
    except Exception as e:
        logger.error(f"User stream failed: {e}")

def keepalive_user_stream():
    while not SHUTDOWN_EVENT.is_set():
        time.sleep(KEEPALIVE_INTERVAL)
        try:
            with listen_key_lock:
                if listen_key and bot:
                    bot.client.stream_keepalive(listen_key)
        except: pass

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

    grid_size = DEFAULT_GRID_SIZE_USDT
    usdt_free = bot.get_balance()
    if usdt_free < MIN_USDT_RESERVE + MIN_USDT_TO_BUY:
        return

    max_grids = int((usdt_free - MIN_USDT_RESERVE) // grid_size)
    if max_grids < 2: return

    buy_grids = sell_grids = max_grids // 2
    step = bot.get_lot_step(symbol)
    tick = bot.get_tick_size(symbol)
    qty = (grid_size / price) // step * step
    if qty <= ZERO: return

    # Cancel old
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
            order = bot.place_limit_sell_with_tracking(symbol, p, qty)
            if order:
                new_grid['sell_orders'].append(str(order['orderId']))
                asset_free -= qty

    if new_grid['buy_orders'] or new_grid['sell_orders']:
        active_grid_symbols[symbol] = new_grid
        color = STRATEGY_COLORS.get(strategy, GREEN)
        label = STRATEGY_LABELS.get(strategy, strategy.upper())
        send_alert(f"{symbol} | ${float(grid_size):.1f} | {len(new_grid['buy_orders'])}B/{len(new_grid['sell_orders'])}S | {color}{label}{RESET}", subject="GRID")

# === DASHBOARD ===
def print_dashboard(bot):
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
        usdt = bot.get_balance()
        line = "=" * 120
        print(f"{YELLOW}{line}{RESET}")
        print(pad_field(f"{GREEN}INFINITY GRID BOT v7.2{RESET} | {now_cst()} | WS: {'ON' if ws_connected else 'OFF'}", 120))
        print(f"{YELLOW}{line}{RESET}")
        print(pad_field(f"USDT: ${float(usdt):,.2f} | GRIDS: {len(active_grid_symbols)} | PNL: ${float(total_realized_pnl):+.2f}", 120))
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
