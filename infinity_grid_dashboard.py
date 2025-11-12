#!/usr/bin/env python3
"""
Infinity Grid Live Streamlit Bot (single file)

- Default: REAL trading enabled if BINANCE_API_KEY & BINANCE_API_SECRET are present
- Limit-only resting orders (buys below current / sells above current)
- Regrid on price move (configurable), regrid on fills (user stream)
- Pending orders persisted in SQLite and removed on FILLED
- Streamlit dashboard: status, active grids, logs, pending orders
- Safety: enforces tickSize and stepSize, minimum notional checks
"""

import os
import sys
import time
import json
import threading
import logging
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime
import pytz
import streamlit as st
from logging.handlers import TimedRotatingFileHandler
from sqlalchemy import create_engine, Column, Integer, String, Numeric
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError

# Attempt to import python-binance; if missing, Streamlit will show error on startup
try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException
    BINANCE_AVAILABLE = True
except Exception:
    BINANCE_AVAILABLE = False

# Decimal precision
getcontext().prec = 28

# -------------------- CONFIG -------------------- #
# Grid & Streams
GRID_SIZE_USDT = Decimal(os.getenv('GRID_SIZE_USDT', '5.0'))
GRID_INTERVAL = Decimal(os.getenv('GRID_INTERVAL', '0.015'))  # 1.5%
MAX_GRIDS_PER_SIDE = int(os.getenv('MAX_GRIDS_PER_SIDE', '3'))
REGRID_PRICE_MOVE_PCT = Decimal(os.getenv('REGRID_PRICE_MOVE_PCT', '0.005'))  # 0.5%
INITIAL_GRID_DELAY = int(os.getenv('INITIAL_GRID_DELAY', '20'))
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
KEEPALIVE_INTERVAL = 1800
HEARTBEAT_INTERVAL = 25

# Minimum checks
MIN_NOTIONAL_USDT = Decimal(os.getenv('MIN_NOTIONAL_USDT', '10.0'))

# Logging
LOG_FILE = os.getenv('LOG_FILE', 'grid_live.log')

# -------------------- GLOBALS -------------------- #
ZERO = Decimal('0')
ONE = Decimal('1')
CST_TZ = pytz.timezone('America/Chicago')
SHUTDOWN_EVENT = threading.Event()

# Locks
price_lock = threading.Lock()
balance_lock = threading.Lock()
listen_key_lock = threading.Lock()
grid_lock = threading.Lock()

# Runtime state
ws_connected = False
rest_client = None
listen_key = None
user_ws = None
ws_instances = []

live_prices = {}
last_regrid_price = {}
balances = {}
valid_symbols_dict = {}
symbol_info_cache = {}
active_grid_symbols = {}

# Logging init
logger = logging.getLogger("infinity_grid_live")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=7)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
    logger.addHandler(ch)

# UI in-memory log buffer (Streamlit)
from collections import deque
ui_logs = deque(maxlen=1000)

def now_cst():
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")

def ui_log(msg, level="INFO"):
    s = f"{now_cst()} {level} - {msg}"
    ui_logs.appendleft(s)
    logger.info(s)

def safe_decimal(v, default=ZERO):
    try:
        return Decimal(str(v)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except Exception:
        return default

# -------------------- DATABASE -------------------- #
DB_URL = "sqlite:///trades_live.db"
engine = create_engine(DB_URL, echo=False, future=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class PendingOrder(Base):
    __tablename__ = "pending_orders"
    id = Column(Integer, primary_key=True)
    binance_order_id = Column(String(64), unique=True)
    symbol = Column(String(20))
    side = Column(String(4))
    price = Column(Numeric(20, 8))
    quantity = Column(Numeric(20, 8))

Base.metadata.create_all(engine)

class SafeDB:
    def __enter__(self):
        self.s = SessionFactory()
        return self.s
    def __exit__(self, *args):
        exc = args[0]
        if exc:
            self.s.rollback()
        else:
            try:
                self.s.commit()
            except IntegrityError:
                self.s.rollback()
        self.s.close()

# -------------------- WEBSOCKET APP -------------------- #
import websocket

class WS(websocket.WebSocketApp):
    def __init__(self, url, on_message_cb, user=False):
        super().__init__(url, on_open=self._on_open, on_message=on_message_cb,
                         on_error=self._on_error, on_close=self._on_close, on_pong=self._on_pong)
        self.user = user
        self._last_pong = time.time()

    def _on_open(self, ws):
        global ws_connected
        ws_connected = True
        ui_log(f"WS {'USER' if self.user else 'MARKET'} CONNECTED")

    def _on_error(self, ws, err):
        ui_log(f"WS ERROR: {err}", "WARNING")

    def _on_close(self, ws, *args):
        global ws_connected
        ws_connected = False
        ui_log("WS CLOSED", "WARNING")

    def _on_pong(self, ws, *args):
        self._last_pong = time.time()

    def run(self):
        while not SHUTDOWN_EVENT.is_set():
            try:
                super().run_forever(ping_interval=HEARTBEAT_INTERVAL, ping_timeout=10)
            except Exception as e:
                ui_log(f"WS run error: {e}", "ERROR")
            time.sleep(2)

# -------------------- BOT -------------------- #
class Bot:
    def __init__(self, api_key, api_secret):
        global rest_client
        if not BINANCE_AVAILABLE:
            ui_log("python-binance not available. Install python-binance to trade.", "ERROR")
            raise RuntimeError("python-binance not available")
        self.c = Client(api_key, api_secret, tld='us')
        rest_client = self.c

    def get_tick(self, s): return symbol_info_cache.get(s, {}).get('tickSize', Decimal('1e-8'))
    def get_step(self, s): return symbol_info_cache.get(s, {}).get('stepSize', Decimal('1e-8'))

    def balance(self, asset='USDT'):
        with balance_lock:
            return balances.get(asset, safe_decimal('0'))

    def _store_pending(self, order, sym, side, p, q):
        try:
            with SafeDB() as s:
                s.add(PendingOrder(binance_order_id=str(order['orderId']), symbol=sym, side=side, price=p, quantity=q))
        except Exception as e:
            ui_log(f"DB store pending failed: {e}", "WARNING")

    def place_limit_buy(self, sym, p, q):
        try:
            o = self.c.order_limit_buy(symbol=sym, quantity=str(q), price=str(p))
            self._store_pending(o, sym, 'BUY', p, q)
            ui_log(f"BUY placed {sym} @ {p} x {q}")
            return o
        except BinanceAPIException as e:
            ui_log(f"BUY failed {sym}: {e}", "ERROR")
            return None
        except Exception as e:
            ui_log(f"BUY error {sym}: {e}", "ERROR")
            return None

    def place_limit_sell(self, sym, p, q):
        try:
            o = self.c.order_limit_sell(symbol=sym, quantity=str(q), price=str(p))
            self._store_pending(o, sym, 'SELL', p, q)
            ui_log(f"SELL placed {sym} @ {p} x {q}")
            return o
        except BinanceAPIException as e:
            ui_log(f"SELL failed {sym}: {e}", "ERROR")
            return None
        except Exception as e:
            ui_log(f"SELL error {sym}: {e}", "ERROR")
            return None

    def cancel_order(self, sym, oid):
        try:
            self.c.cancel_order(symbol=sym, orderId=oid)
            ui_log(f"CANCELLED {sym} #{oid}")
        except BinanceAPIException as e:
            ui_log(f"CANCEL failed {sym} #{oid}: {e}", "WARNING")
        except Exception:
            pass

# -------------------- GRID LOGIC -------------------- #
def should_regrid_on_price(sym, current_price):
    last = last_regrid_price.get(sym)
    if not last: return True
    move = (current_price - last).copy_abs() / last
    return move >= REGRID_PRICE_MOVE_PCT

def cancel_grid(sym):
    with grid_lock:
        grid = active_grid_symbols.get(sym, {})
        for oid in grid.get('buy', []) + grid.get('sell', []):
            try:
                bot.cancel_order(sym, oid)
            except Exception:
                pass
        active_grid_symbols.pop(sym, None)

def place_new_grid(sym, center_price):
    try:
        cancel_grid(sym)
        time.sleep(0.2)
        step = bot.get_step(sym) or Decimal('1e-8')
        raw_qty = (GRID_SIZE_USDT / center_price)
        steps = (raw_qty // step)
        qty = (steps * step).quantize(step, rounding=ROUND_DOWN)
        if qty <= ZERO or (center_price * qty) < MIN_NOTIONAL_USDT:
            ui_log(f"{sym}: qty too small or notional below {MIN_NOTIONAL_USDT} USDT -> qty={qty}, notional={center_price*qty}", "WARNING")
            return
        tick = bot.get_tick(sym) or Decimal('1e-8')
        buy_oids = []
        sell_oids = []
        for i in range(1, MAX_GRIDS_PER_SIDE + 1):
            raw_buy = center_price * (ONE - GRID_INTERVAL * Decimal(i))
            buy_p = (raw_buy // tick) * tick
            buy_p = buy_p.quantize(tick, rounding=ROUND_DOWN)
            if buy_p <= ZERO: continue
            # buys placed below center price only
            if buy_p < center_price:
                o = bot.place_limit_buy(sym, buy_p, qty)
                if o: buy_oids.append(str(o['orderId']))
            raw_sell = center_price * (ONE + GRID_INTERVAL * Decimal(i))
            sell_p = (raw_sell // tick) * tick
            sell_p = sell_p.quantize(tick, rounding=ROUND_DOWN)
            if sell_p <= ZERO: continue
            # check asset balance before placing sell
            asset = sym.replace('USDT', '')
            if bot.balance(asset) >= qty:
                o = bot.place_limit_sell(sym, sell_p, qty)
                if o: sell_oids.append(str(o['orderId']))
        if buy_oids or sell_oids:
            with grid_lock:
                active_grid_symbols[sym] = {
                    'center': center_price,
                    'qty': qty,
                    'buy': buy_oids,
                    'sell': sell_oids,
                    'at': time.time()
                }
            last_regrid_price[sym] = center_price
            ui_log(f"GRID {sym} @ {float(center_price):.6f} | {len(buy_oids)}B/{len(sell_oids)}S")
        else:
            ui_log(f"{sym}: no orders placed (insufficient balance or filters)", "INFO")
    except Exception as e:
        ui_log(f"place_new_grid exception {sym}: {e}", "ERROR")

def regrid_on_fill(sym):
    price = live_prices.get(sym)
    if price:
        threading.Thread(target=place_new_grid, args=(sym, price), daemon=True).start()

# -------------------- WEBSOCKET HANDLERS -------------------- #
def on_market(ws, msg):
    try:
        d = json.loads(msg)
        s = d.get('stream', '').split('@')[0].upper()
        p = d.get('data', {})
        price = safe_decimal(p.get('c', '0'))
        if price > ZERO:
            with price_lock:
                live_prices[s] = price
            if s in valid_symbols_dict and should_regrid_on_price(s, price):
                threading.Thread(target=place_new_grid, args=(s, price), daemon=True).start()
    except Exception:
        pass

def on_user(ws, msg):
    try:
        d = json.loads(msg)
        if d.get('e') != 'executionReport': return
        if d.get('X') != 'FILLED': return
        sym = d.get('s', '')
        side = d.get('S', '')
        oid = str(d.get('i', ''))
        price = safe_decimal(d.get('p', '0'))
        qty = safe_decimal(d.get('q', '0'))
        # remove pending
        try:
            with SafeDB() as sess:
                po = sess.query(PendingOrder).filter_by(binance_order_id=oid).first()
                if po: sess.delete(po)
        except Exception:
            pass
        ui_log(f"FILL {side} {sym} @ {price} x {qty}")
        if sym in valid_symbols_dict:
            threading.Thread(target=regrid_on_fill, args=(sym,), daemon=True).start()
    except Exception as e:
        ui_log(f"User WS error: {e}", "ERROR")

# -------------------- STREAMS START -------------------- #
def start_streams():
    global listen_key, user_ws
    symbols = [s.lower() for s in valid_symbols_dict.keys()]
    if not symbols:
        ui_log("No symbols to stream", "WARNING")
        return
    streams = [f"{s}@ticker" for s in symbols]
    for i in range(0, len(streams), 100):
        chunk = streams[i:i+100]
        url = WS_BASE + '/'.join(chunk)
        ws = WS(url, on_market)
        ws_instances.append(ws)
        threading.Thread(target=ws.run, daemon=True).start()
        time.sleep(0.1)
    # user stream listenKey
    try:
        with listen_key_lock:
            lk = rest_client.stream_get_listen_key()
            if isinstance(lk, dict):
                listen_key = lk.get('listenKey')
            else:
                listen_key = lk
        if not listen_key:
            raise RuntimeError("No listen key")
        url = f"{USER_STREAM_BASE}{listen_key}"
        user_ws = WS(url, on_user, user=True)
        threading.Thread(target=user_ws.run, daemon=True).start()
    except Exception as e:
        ui_log(f"User stream failed: {e}", "ERROR")
    threading.Thread(target=keepalive, daemon=True).start()

def keepalive():
    while not SHUTDOWN_EVENT.is_set():
        time.sleep(KEEPALIVE_INTERVAL)
        try:
            with listen_key_lock:
                if listen_key:
                    rest_client.stream_keepalive(listen_key)
        except Exception:
            pass

# -------------------- INITIAL GRID COUNTDOWN -------------------- #
def initial_grid_in_seconds():
    ph = st.empty()
    with ph.container():
        st.info(f"Placing first grid in **{INITIAL_GRID_DELAY}** seconds...")
        for i in range(INITIAL_GRID_DELAY, 0, -1):
            st.write(f"Starting in: **{i}**")
            time.sleep(1)
        st.write("Placing initial grid...")
    ph.empty()
    for sym in list(valid_symbols_dict.keys()):
        price = live_prices.get(sym)
        if not price:
            try:
                t = rest_client.get_symbol_ticker(symbol=sym)
                price = safe_decimal(t['price'])
                live_prices[sym] = price
            except Exception:
                ui_log(f"{sym}: cannot fetch price for initial grid", "WARNING")
                continue
        threading.Thread(target=place_new_grid, args=(sym, price), daemon=True).start()

# -------------------- STREAMLIT DASHBOARD -------------------- #
st.set_page_config(page_title="Infinity Grid Live", layout="wide")
st.title("Infinity Grid – Live (Limit-only)")

# Real trading default: enable if keys present
ENV_API_KEY = os.getenv('BINANCE_API_KEY')
ENV_API_SECRET = os.getenv('BINANCE_API_SECRET')
real_trading_default = bool(ENV_API_KEY and ENV_API_SECRET and BINANCE_AVAILABLE)

col1, col2 = st.columns([1,2])
with col1:
    st.write(f"Keys present: {'Yes' if ENV_API_KEY and ENV_API_SECRET else 'No'}")
    use_real = st.checkbox("ENABLE LIVE TRADING (uses your env keys)", value=real_trading_default)
    st.write("**WARNING:** Live trading will place real orders if enabled and keys are valid.")
with col2:
    st.write("Grid configuration")
    st.number_input("Grid size (USDT)", value=float(GRID_SIZE_USDT), key="grid_size_usdt")
    st.number_input("Grid interval (fraction)", value=float(GRID_INTERVAL), key="grid_interval")
    st.number_input("Max grids per side", value=MAX_GRIDS_PER_SIDE, key="max_grids")
    st.number_input("Regrid price move pct (fraction)", value=float(REGRID_PRICE_MOVE_PCT), key="regrid_pct")

# apply potential UI config edits
try:
    GRID_SIZE_USDT = safe_decimal(st.session_state.get("grid_size_usdt", float(GRID_SIZE_USDT)))
    GRID_INTERVAL = safe_decimal(st.session_state.get("grid_interval", float(GRID_INTERVAL)))
    MAX_GRIDS_PER_SIDE = int(st.session_state.get("max_grids", MAX_GRIDS_PER_SIDE))
    REGRID_PRICE_MOVE_PCT = safe_decimal(st.session_state.get("regrid_pct", float(REGRID_PRICE_MOVE_PCT)))
except Exception:
    pass

# Instantiate bot (live) if requested
bot = None
if use_real:
    if not (ENV_API_KEY and ENV_API_SECRET):
        st.error("BINANCE_API_KEY and BINANCE_API_SECRET must be set in environment to enable live trading.")
        st.stop()
    if not BINANCE_AVAILABLE:
        st.error("python-binance library not available; install python-binance.")
        st.stop()
    try:
        bot = Bot(ENV_API_KEY, ENV_API_SECRET)
        st.success("Connected to Binance.US (live mode).")
    except Exception as e:
        st.error(f"Failed to connect to Binance: {e}")
        st.stop()
else:
    st.warning("Live trading disabled — exiting. (This app is intended to run live by default.)")
    st.stop()

# Discover tradable symbols from account balances (non-zero non-stablecoin)
try:
    acct = bot.c.get_account()
    with balance_lock:
        for b in acct.get('balances', []):
            balances[b['asset']] = safe_decimal(b['free'])
    for b in acct.get('balances', []):
        asset = b['asset']
        amt = safe_decimal(b['free'])
        if amt <= ZERO or asset in {'USDT', 'USDC'}:
            continue
        sym = f"{asset}USDT"
        try:
            info = bot.c.get_symbol_info(sym)
        except Exception:
            info = None
        if info and info.get('status') == 'TRADING':
            valid_symbols_dict[sym] = {}
            tick = step = Decimal('1e-8')
            for f in info.get('filters', []):
                if f.get('filterType') == 'PRICE_FILTER':
                    tick = safe_decimal(f.get('tickSize', tick))
                if f.get('filterType') == 'LOT_SIZE':
                    step = safe_decimal(f.get('stepSize', step))
            symbol_info_cache[sym] = {'tickSize': tick, 'stepSize': step}
except Exception as e:
    st.error(f"Error loading account/symbols: {e}")
    st.stop()

if not valid_symbols_dict:
    st.warning("No tradable non-stablecoin balances found. The bot derives symbols from non-zero balances.")
    st.stop()

st.success(f"Loaded {len(valid_symbols_dict)} symbols: {', '.join(valid_symbols_dict.keys())}")

# Start market & user streams
start_streams()

# Initial grid countdown & place
initial_grid_in_seconds()

# UI panels
status_ph = st.empty()
grids_ph = st.empty()
logs_ph = st.empty()
pending_ph = st.empty()

try:
    while True:
        # Status
        with status_ph.container():
            st.markdown("### Status")
            c1, c2, c3 = st.columns(3)
            c1.metric("API", "OK")
            c2.metric("WS", "ON" if ws_connected else "OFF")
            c3.write(f"Time: {now_cst()}")

        # Active grids
        with grids_ph.container():
            st.markdown("### Active Grids")
            with grid_lock:
                rows = []
                for s, g in active_grid_symbols.items():
                    rows.append({
                        "Symbol": s,
                        "Center": f"${float(g['center']):.6f}",
                        "Qty": f"{float(g['qty']):.8f}",
                        "Age(s)": int(time.time() - g['at']),
                        "B/S": f"{len(g['buy'])}/{len(g['sell'])}"
                    })
            st.dataframe(rows or [{"Symbol": "—"}], use_container_width=True)

        # Pending orders from DB (live)
        with pending_ph.container():
            st.markdown("### Pending Orders (DB)")
            with SafeDB() as sess:
                pending = sess.query(PendingOrder).all()
                table = [{"OrderID": p.binance_order_id, "Symbol": p.symbol, "Side": p.side, "Price": float(p.price), "Qty": float(p.quantity)} for p in pending]
            st.dataframe(table or [{"OrderID": "—"}], use_container_width=True)

        # Logs
        with logs_ph.container():
            st.markdown("### Logs (recent)")
            for line in list(ui_logs)[:200]:
                st.text(line)

        time.sleep(2)
except KeyboardInterrupt:
    SHUTDOWN_EVENT.set()
    ui_log("KeyboardInterrupt: shutting down")
except Exception as e:
    ui_log(f"Main loop error: {e}", "ERROR")
    SHUTDOWN_EVENT.set()
finally:
    try:
        cancel_all = True
        if cancel_all:
            # cancel all placed grids on shutdown
            with grid_lock:
                for sym, grid in list(active_grid_symbols.items()):
                    for oid in grid.get('buy', []) + grid.get('sell', []):
                        try:
                            bot.cancel_order(sym, oid)
                        except Exception:
                            pass
    except Exception:
        pass
