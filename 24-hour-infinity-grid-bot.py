#!/usr/bin/env python3
"""
INFINITY GRID + PME + DASHBOARD (Streamlit)
• Real-time WebSocket & API status
• Auto-starts grid within 30s
• Live PnL, strategy, positions
"""
import os
import sys
import time
import json
import signal
import logging
import threading
import websocket
import numpy as np
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from typing import Dict, List, Tuple
import pytz
import streamlit as st
from logging.handlers import TimedRotatingFileHandler
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError

# --------------------------------------------------------------------------- #
# ====================== REQUIRED IMPORTS (AS YOU SPECIFIED) =============== #
# --------------------------------------------------------------------------- #
from binance.client import Client
from binance.exceptions import BinanceAPIException

# --------------------------------------------------------------------------- #
# =============================== CONFIG ==================================== #
# --------------------------------------------------------------------------- #
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    st.error("Set BINANCE_API_KEY and BINANCE_API_SECRET")
    st.stop()

# Grid & PME
GRID_SIZE_USDT = Decimal('5.0')
MIN_USDT_RESERVE = Decimal('10.0')
MIN_USDT_TO_BUY = Decimal('15.0')
MAX_GRIDS_PER_SIDE = 8
MIN_GRIDS_PER_SIDE = 2
REBALANCE_THRESHOLD_PCT = Decimal('0.0075')
TREND_THRESHOLD = Decimal('0.02')

# WebSocket
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
MAX_STREAMS_PER_CONNECTION = 100
HEARTBEAT_INTERVAL = 25
KEEPALIVE_INTERVAL = 1800

# --------------------------------------------------------------------------- #
# =============================== GLOBALS =================================== #
# --------------------------------------------------------------------------- #
ZERO = Decimal('0')
ONE = Decimal('1')
CST_TZ = pytz.timezone('America/Chicago')
SHUTDOWN_EVENT = threading.Event()

# Locks & State
price_lock = threading.Lock()
kline_lock = threading.Lock()
stats_lock = threading.Lock()
balance_lock = threading.Lock()
realized_lock = threading.Lock()
listen_key_lock = threading.Lock()

# Streamlit placeholders
ph_status = None
ph_grid = None
ph_pnl = None
ph_positions = None

# Global state
ws_connected = False
api_connected = True
rest_client = None
listen_key = None
user_ws = None
ws_instances: List[websocket.WebSocketApp] = []

live_prices: Dict[str, Decimal] = {}
ticker_24h_stats: Dict[str, dict] = {}
trend_bias: Dict[str, Decimal] = {}
kline_data: Dict[str, List[dict]] = {}
momentum_score: Dict[str, Decimal] = {}
volume_profile: Dict[str, dict] = {}
last_vp_update = 0
total_realized_pnl = ZERO
peak_pnl = ZERO
realized_pnl_per_symbol: Dict[str, Decimal] = {}
balances: Dict[str, Decimal] = {}
valid_symbols_dict: Dict[str, dict] = {}
symbol_info_cache: Dict[str, dict] = {}
active_grid_symbols: Dict[str, dict] = {}

# --------------------------------------------------------------------------- #
# =============================== LOGGING =================================== #
# --------------------------------------------------------------------------- #
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
fh = TimedRotatingFileHandler("grid_dashboard.log", when="midnight", backupCount=7)
fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
logger.addHandler(fh)

# --------------------------------------------------------------------------- #
# =============================== HELPERS =================================== #
# --------------------------------------------------------------------------- #
def safe_decimal(value, default=ZERO) -> Decimal:
    try: return Decimal(str(value)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except: return default

def now_cst() -> str:
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")

# --------------------------------------------------------------------------- #
# =============================== DATABASE ================================== #
# --------------------------------------------------------------------------- #
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

if not os.path.exists("binance_trades.db"):
    Base.metadata.create_all(engine)

class SafeDBManager:
    def __enter__(self):
        try:
            self.session = SessionFactory()
            return self.session
        except: return None
    def __exit__(self, exc_type, exc_val, exc_tb):
        if not hasattr(self, 'session'): return
        if exc_type: self.session.rollback()
        else:
            try: self.session.commit()
            except IntegrityError: self.session.rollback()
        self.session.close()

# --------------------------------------------------------------------------- #
# =============================== WEBSOCKET ================================= #
# --------------------------------------------------------------------------- #
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, on_message_cb, is_user_stream=False):
        super().__init__(url, on_open=self.on_open, on_message=on_message_cb,
                         on_error=self.on_error, on_close=self.on_close, on_pong=self.on_pong)
        self.is_user_stream = is_user_stream
        self.last_pong = time.time()
        self.heartbeat_thread = None

    def on_open(self, ws):
        global ws_connected
        ws_connected = True
        logger.info(f"WS CONNECTED: {'User' if self.is_user_stream else 'Market'}")
        self.last_pong = time.time()
        if not self.heartbeat_thread:
            self.heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.heartbeat_thread.start()

    def on_error(self, ws, error): logger.warning(f"WS ERROR: {error}")
    def on_close(self, ws, *args):
        global ws_connected
        ws_connected = False
        logger.warning(f"WS CLOSED")
    def on_pong(self, ws, *args): self.last_pong = time.time()

    def _heartbeat(self):
        while self.sock and self.sock.connected and not SHUTDOWN_EVENT.is_set():
            if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 10:
                self.close(); break
            try: self.send("ping", opcode=websocket.ABNF.OPCODE_PING)
            except: break
            time.sleep(HEARTBEAT_INTERVAL)

    def run_forever(self):
        while not SHUTDOWN_EVENT.is_set():
            try: super().run_forever(ping_interval=None)
            except Exception as e: logger.error(f"WS CRASH: {e}")
            if SHUTDOWN_EVENT.is_set(): break
            time.sleep(2)

# --------------------------------------------------------------------------- #
# =============================== BOT CLASS ================================= #
# --------------------------------------------------------------------------- #
class BinanceTradingBot:
    def __init__(self):
        global rest_client
        self.client = Client(API_KEY, API_SECRET, tld='us')
        rest_client = self.client
        self.api_lock = threading.Lock()

    def get_tick_size(self, symbol): return symbol_info_cache.get(symbol, {}).get('tickSize', Decimal('0.00000001'))
    def get_lot_step(self, symbol): return symbol_info_cache.get(symbol, {}).get('stepSize', Decimal('0.00000001'))

    def get_balance(self) -> Decimal:
        with balance_lock: return balances.get('USDT', ZERO)

    def get_asset_balance(self, asset: str) -> Decimal:
        with balance_lock: return balances.get(asset, ZERO)

    def place_limit_buy_with_tracking(self, symbol, price: Decimal, qty: Decimal):
        try:
            order = self.client.order_limit_buy(symbol=symbol, quantity=str(qty), price=str(price))
            with SafeDBManager() as sess:
                if sess:
                    sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='BUY', price=price, quantity=qty))
            return order
        except Exception as e: logger.warning(f"Buy failed: {e}"); return None

    def place_limit_sell_with_tracking(self, symbol, price: Decimal, qty: Decimal):
        try:
            order = self.client.order_limit_sell(symbol=symbol, quantity=str(qty), price=str(price))
            with SafeDBManager() as sess:
                if sess:
                    sess.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=symbol, side='SELL', price=price, quantity=qty))
            return order
        except Exception as e: logger.warning(f"Sell failed: {e}"); return None

    def cancel_order_safe(self, symbol, order_id):
        try: self.client.cancel_order(symbol=symbol, orderId=order_id)
        except: pass

# --------------------------------------------------------------------------- #
# =============================== WS HANDLERS =============================== #
# --------------------------------------------------------------------------- #
def on_market_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get('stream', '')
        payload = data.get('data', {})
        if not payload or not stream: return
        symbol = stream.split('@')[0].upper()

        if stream.endswith('@ticker'):
            price = safe_decimal(payload.get('c', '0'))
            if price > ZERO:
                with price_lock: live_prices[symbol] = price
            with stats_lock:
                ticker_24h_stats[symbol] = {
                    'h': payload.get('h'), 'l': payload.get('l'),
                    'P': payload.get('P'), 'v': payload.get('q')
                }
            pct = safe_decimal(payload.get('P', '0')) / 100
            bias = ONE if pct >= TREND_THRESHOLD else Decimal('-1.0') if pct <= -TREND_THRESHOLD else ZERO
            trend_bias[symbol] = bias

    except Exception as e: logger.debug(f"Market parse error: {e}")

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data.get('e') != 'executionReport' or data.get('X') != 'FILLED': return
        order_id = str(data.get('i', ''))
        symbol = data.get('s', '')
        side = data.get('S', '')
        price = safe_decimal(data.get('p', '0'))
        qty = safe_decimal(data.get('q', '0'))
        fee = safe_decimal(data.get('n', '0')) or ZERO

        with SafeDBManager() as sess:
            if sess:
                po = sess.query(PendingOrder).filter_by(binance_order_id=order_id).first()
                if po: sess.delete(po)

        if side == 'SELL':
            with SafeDBManager() as sess:
                if sess:
                    pos = sess.query(Position).filter_by(symbol=symbol).first()
                    if pos:
                        entry = safe_decimal(pos.avg_entry_price)
                        pnl = (price - entry) * qty - fee
                        with realized_lock:
                            global total_realized_pnl, peak_pnl
                            total_realized_pnl += pnl
                            peak_pnl = max(peak_pnl, total_realized_pnl)

    except Exception as e: logger.error(f"User WS error: {e}")

# --------------------------------------------------------------------------- #
# =============================== GRID SETUP ================================ #
# --------------------------------------------------------------------------- #
def setup_grids(bot):
    time.sleep(2)  # Wait for data
    usdt = bot.get_balance()
    if usdt < MIN_USDT_RESERVE + MIN_USDT_TO_BUY:
        st.warning(f"USDT ${float(usdt):.2f} < Reserve")
        return

    for sym in list(valid_symbols_dict.keys()):
        price = live_prices.get(sym)
        if not price: continue
        grid_size = GRID_SIZE_USDT
        qty = (grid_size / price) // bot.get_lot_step(sym) * bot.get_lot_step(sym)
        if qty <= ZERO: continue

        # Simple 3x3 grid
        for i in range(1, 4):
            buy_price = (price * (1 - 0.015 * i)).quantize(bot.get_tick_size(sym))
            sell_price = (price * (1 + 0.015 * i)).quantize(bot.get_tick_size(sym))
            bot.place_limit_buy_with_tracking(sym, buy_price, qty)
            asset = sym.replace('USDT', '')
            if bot.get_asset_balance(asset) >= qty:
                bot.place_limit_sell_with_tracking(sym, sell_price, qty)

        active_grid_symbols[sym] = {'center': price, 'qty': qty, 'placed_at': time.time()}
        logger.info(f"GRID STARTED: {sym} @ ${float(price):.6f}")

# --------------------------------------------------------------------------- #
# =============================== STREAMS =================================== #
# --------------------------------------------------------------------------- #
def start_streams():
    global listen_key, user_ws
    # Market
    symbols = [s.lower() for s in valid_symbols_dict]
    if symbols:
        streams = [f"{s}@ticker" for s in symbols]
        chunks = [streams[i:i+MAX_STREAMS_PER_CONNECTION] for i in range(0, len(streams), MAX_STREAMS_PER_CONNECTION)]
        for chunk in chunks:
            url = WS_BASE + '/'.join(chunk)
            ws = HeartbeatWebSocket(url, on_market_message)
            ws_instances.append(ws)
            threading.Thread(target=ws.run_forever, daemon=True).start()

    # User
    try:
        listen_key = rest_client.stream_get_listen_key()
        url = f"{USER_STREAM_BASE}{listen_key}"
        user_ws = HeartbeatWebSocket(url, on_user_message, is_user_stream=True)
        threading.Thread(target=user_ws.run_forever, daemon=True).start()
    except: pass

    threading.Thread(target=keepalive, daemon=True).start()

def keepalive():
    while not SHUTDOWN_EVENT.is_set():
        time.sleep(KEEPALIVE_INTERVAL)
        try:
            with listen_key_lock:
                if listen_key and rest_client:
                    rest_client.stream_keepalive(listen_key)
        except: pass

# --------------------------------------------------------------------------- #
# =============================== DASHBOARD ================================= #
# --------------------------------------------------------------------------- #
def main_dashboard():
    global ph_status, ph_grid, ph_pnl, ph_positions
    st.set_page_config(page_title="Infinity Grid Bot", layout="wide")
    st.title("Infinity Grid + PME Bot")

    # Status
    col1, col2, col3 = st.columns(3)
    ph_status = st.empty()
    ph_api = col1.empty()
    ph_ws_market = col2.empty()
    ph_ws_user = col3.empty()

    # PnL
    ph_pnl = st.empty()

    # Positions
    ph_positions = st.empty()

    # Grid
    ph_grid = st.empty()

    # Load symbols
    bot = BinanceTradingBot()
    try:
        acct = bot.client.get_account()
        with balance_lock:
            for b in acct['balances']:
                balances[b['asset']] = safe_decimal(b['free'])
        for b in acct['balances']:
            asset = b['asset']
            qty = safe_decimal(b['free'])
            if qty <= ZERO or asset in {'USDT', 'USDC'}: continue
            sym = f"{asset}USDT"
            info = bot.client.get_symbol_info(sym)
            if info and info['status'] == 'TRADING':
                valid_symbols_dict[sym] = {}
                tick = step = Decimal('0.00000001')
                for f in info['filters']:
                    if f['filterType'] == 'PRICE_FILTER': tick = safe_decimal(f['tickSize'])
                    if f['filterType'] == 'LOT_SIZE': step = safe_decimal(f['stepSize'])
                symbol_info_cache[sym] = {'tickSize': tick, 'stepSize': step}
    except Exception as e:
        st.error(f"API Error: {e}")
        api_connected = False

    if valid_symbols_dict:
        start_streams()
        st.success(f"Loaded {len(valid_symbols_dict)} symbols")

        # Auto-start grid in 30s
        with st.spinner("Starting grid in 30 seconds..."):
            for i in range(30, 0, -1):
                st.write(f"Grid setup in {i}s...")
                time.sleep(1)
            setup_grids(bot)

    # Live update loop
    while True:
        with ph_status.container():
            st.markdown("### Status")
            col1, col2, col3 = st.columns(3)
            col1.metric("REST API", "Connected" if api_connected else "Disconnected", delta=None)
            col2.metric("Market WS", "Connected" if ws_connected else "Disconnected")
            col3.metric("User WS", "Connected" if user_ws and user_ws.sock and user_ws.sock.connected else "Disconnected")

        with ph_pnl.container():
            st.markdown("### PnL")
            st.metric("Total Realized", f"${float(total_realized_pnl):.2f}", delta=f"{float(total_realized_pnl - peak_pnl):+.2f}")

        with ph_positions.container():
            st.markdown("### Positions")
            rows = []
            with SafeDBManager() as sess:
                if sess:
                    for pos in sess.query(Position).all():
                        price = live_prices.get(pos.symbol, ZERO)
                        unreal = (price - safe_decimal(pos.avg_entry_price)) * safe_decimal(pos.quantity)
                        rows.append({
                            "Symbol": pos.symbol,
                            "Qty": f"{float(pos.quantity):.6f}",
                            "Entry": f"${float(pos.avg_entry_price):.6f}",
                            "Price": f"${float(price):.6f}",
                            "Unrealized": f"${float(unreal):.2f}"
                        })
            if rows: st.dataframe(rows, use_container_width=True)
            else: st.write("No positions")

        with ph_grid.container():
            st.markdown("### Active Grids")
            rows = []
            for sym, g in active_grid_symbols.items():
                rows.append({
                    "Symbol": sym,
                    "Center": f"${float(g['center']):.6f}",
                    "Buy Orders": len(g.get('buy_orders', [])),
                    "Sell Orders": len(g.get('sell_orders', [])),
                    "Age": f"{int(time.time() - g['placed_at'])}s"
                })
            if rows: st.dataframe(rows, use_container_width=True)
            else: st.write("No active grids")

        time.sleep(2)

if __name__ == "__main__":
    main_dashboard()
