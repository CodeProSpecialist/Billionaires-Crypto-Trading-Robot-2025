#!/usr/bin/env python3
"""
PLATINUM CRYPTO TRADER DASHBOARD 2025 — BINANCE.US EDITION
Complete patched script — REAL trading (Binance.US). Use with caution.
"""
import streamlit as st
import os
import time
import threading
import json
import requests
import websocket
from decimal import Decimal, getcontext, ROUND_DOWN, InvalidOperation
from datetime import datetime
import pytz
from binance.client import Client
from binance.exceptions import BinanceAPIException
import logging
from logging.handlers import TimedRotatingFileHandler
import sys

# optional run context import (Streamlit versions vary)
try:
    from streamlit.runtime.scriptrunner import get_script_run_ctx
except Exception:
    def get_script_run_ctx():
        return None

# ========================= CONFIG =========================
getcontext().prec = 28
ZERO = Decimal('0')
ONE = Decimal('1')
SAFETY_BUFFER = Decimal('0.95')
# global max position % (bot uses symbol_info & session slider to adapt)
MAX_POSITION_PCT = Decimal('0.05')
MIN_TRADE_VALUE = Decimal('5.0')
ENTRY_PCT_BELOW_ASK = Decimal('0.001')
TOP_N_VOLUME = 25
MAX_BUY_COINS = 2
DEPTH_LEVELS = 5
HEARTBEAT_INTERVAL = 25
MAX_STREAMS_PER_CONNECTION = 100
RECONNECT_BASE_DELAY = 5
MAX_RECONNECT_DELAY = 300

# USER CHOICE: REAL trading
USE_PAPER_TRADING = False  # <-- USER CHOSE REAL (B). If you want paper, set True.

CST = pytz.timezone('America/Chicago')
TRADING_START_HOUR = 4
TRADING_END_HOUR = 16

LOG_FILE = os.path.expanduser("~/platinum_crypto_dashboard.log")

# ========================= LOGGING =========================
def setup_logging():
    logger = logging.getLogger("platinum_bot")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = TimedRotatingFileHandler(LOG_FILE, when='midnight', backupCount=14)
        fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger

logger = setup_logging()

# ========================= GLOBALS =========================
client = None
symbol_info = {}
account_balances = {}
live_prices = {}
live_bids = {}
live_asks = {}
top_volume_symbols = []
active_positions = {}
price_lock = threading.Lock()
book_lock = threading.Lock()
ws_instances = []
ws_threads = []
user_ws = None
listen_key = None
listen_key_lock = threading.Lock()
last_rebalance_str = "Never"

# -----------------------------------------------------------------
# Session-state defaults (kept module-level as fallback)
# -----------------------------------------------------------------
DEFAULTS = {
    'logs': [], 'shutdown': False, 'auto_rebalance': True,
    'rebalance_interval': 7200, 'min_trade_value': 5.0,
    'max_position_pct': 5.0, 'entry_pct_below_ask': 0.1,
    'bot_running': False, 'init_status': ''
}

# ========================= UTILS =========================
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    """Log to file/console and append to Streamlit session logs if available."""
    line = f"{now_cst()} - {msg}"
    print(line)
    logger.info(msg)
    ctx = get_script_run_ctx()
    try:
        if ctx is not None:
            if 'logs' not in st.session_state:
                st.session_state['logs'] = []
            st.session_state.logs.append(line)
            if len(st.session_state.logs) > 500:
                st.session_state.logs = st.session_state.logs[-500:]
    except Exception:
        pass

def send_whatsapp(msg):
    phone = os.getenv('CALLMEBOT_PHONE')
    key = os.getenv('CALLMEBOT_API_KEY')
    if phone and key:
        try:
            m = requests.utils.quote(msg)
            requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={m}&apikey={key}",
                timeout=5
            )
        except Exception:
            pass

def is_trading_hours():
    h = datetime.now(CST).hour
    return TRADING_START_HOUR <= h < TRADING_END_HOUR

# ========================= BALANCE & INFO =========================
def update_balances():
    """Fetch account balances and store free balances > 0."""
    global account_balances
    try:
        info = client.get_account()['balances']
        account_balances = {
            a['asset']: Decimal(a['free']) for a in info if Decimal(a['free']) > ZERO
        }
        log(f"USDT free: {account_balances.get('USDT', ZERO):.2f}")
    except Exception as e:
        log(f"Balance update error: {e}")

def get_total_portfolio_value():
    """Return portfolio value in USDT (cash + assets priced in live_prices)."""
    total = account_balances.get('USDT', ZERO)
    for asset, qty in account_balances.items():
        if asset == 'USDT':
            continue
        sym = f"{asset}USDT"
        price = live_prices.get(sym)
        if price:
            total += qty * price
    return total

def _safe_decimal(v, fallback='0'):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(fallback)

def load_symbol_info():
    """Load exchange info and parse step/tick/minQty/minNotional for USDT pairs."""
    global symbol_info
    try:
        info = client.get_exchange_info().get('symbols', [])
        for s in info:
            if s.get('quoteAsset') != 'USDT' or s.get('status') != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s.get('filters', [])}
            stepSize = _safe_decimal(filters.get('LOT_SIZE', {}).get('stepSize', '0'))
            tickSize = _safe_decimal(filters.get('PRICE_FILTER', {}).get('tickSize', '0'))
            minQty = _safe_decimal(filters.get('LOT_SIZE', {}).get('minQty', '0'))
            minNotional = _safe_decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            if stepSize == ZERO or tickSize == ZERO:
                continue
            symbol_info[s['symbol']] = {
                'stepSize': stepSize,
                'tickSize': tickSize,
                'minQty': minQty,
                'minNotional': minNotional
            }
        log(f"Loaded {len(symbol_info)} symbols")
    except Exception as e:
        log(f"Symbol info error: {e}")

# ========================= SAFE ORDER =========================
def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    """
    Round DOWN 'value' to nearest multiple of 'step'.
    Uses Decimal.quantize when possible; otherwise falls back to floor multiplication.
    """
    try:
        if step <= 0:
            return value
        return value.quantize(step, rounding=ROUND_DOWN)
    except Exception:
        try:
            multiples = (value // step)
            return multiples * step
        except Exception:
            return value

def format_decimal_for_order(d: Decimal) -> str:
    """Format a Decimal for order API: remove extraneous trailing zeros."""
    s = format(d.normalize(), 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s

def _mock_place_limit_order(symbol, side, price, quantity):
    """Mock order used for paper trading."""
    order = {
        'symbol': symbol, 'side': side, 'price': format_decimal_for_order(price),
        'quantity': format_decimal_for_order(quantity), 'status': 'TEST'
    }
    log(f"[PAPER] PLACED {side} {symbol} {quantity} @ {price}")
    return order

def place_limit_order(symbol, side, price, quantity):
    """Place a limit order with safety checks and step/tick rounding."""
    if USE_PAPER_TRADING:
        return _mock_place_limit_order(symbol, side, price, quantity)

    info = symbol_info.get(symbol)
    if not info:
        log(f"No info for {symbol}")
        return None

    price = round_down_to_step(Decimal(price), info['tickSize'])
    quantity = round_down_to_step(Decimal(quantity), info['stepSize'])

    if quantity <= info['minQty']:
        log(f"Qty too small {symbol} qty({quantity}) <= minQty({info['minQty']})")
        return None
    notional = price * quantity
    if notional < info['minNotional']:
        log(f"Notional too low for {symbol}: {notional} < {info['minNotional']}")
        return None

    if side == 'BUY':
        needed = notional * SAFETY_BUFFER
        if needed > account_balances.get('USDT', ZERO):
            log(f"Not enough USDT for BUY {symbol} needed:{needed} have:{account_balances.get('USDT', ZERO)}")
            return None
    else:
        base = symbol.replace('USDT', '')
        if quantity > account_balances.get(base, ZERO) * SAFETY_BUFFER:
            log(f"Not enough {base} for SELL")
            return None

    try:
        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=format_decimal_for_order(quantity),
            price=format_decimal_for_order(price)
        )
        log(f"PLACED {side} {symbol} {quantity} @ {price}")
        send_whatsapp(f"{side} {symbol} {quantity}@{price}")
        return order
    except BinanceAPIException as e:
        msg = getattr(e, 'message', str(e))
        log(f"ORDER FAILED {symbol} {side}: {msg}")
        return None
    except Exception as e:
        log(f"ORDER ERROR {symbol}: {e}")
        return None

# ========================= HEARTBEAT WS =========================
class HeartbeatWebSocket(websocket.WebSocketApp):
    """WebSocketApp subclass that tracks last pong and runs a heartbeat thread."""
    def __init__(self, url, **kwargs):
        super().__init__(url, **kwargs)
        self.last_pong = time.time()
        self.hb_thread = None
        self.reconnect = 0
        self._stop = False

    def on_open(self, ws):
        try:
            log(f"WS OPEN {ws.url.split('?')[0]}")
            self.last_pong = time.time()
            self.reconnect = 0
            if not self.hb_thread or not self.hb_thread.is_alive():
                self.hb_thread = threading.Thread(target=self._heartbeat, daemon=True)
                self.hb_thread.start()
        except Exception as e:
            log(f"on_open error: {e}")

    def on_pong(self, *args):
        self.last_pong = time.time()

    def _heartbeat(self):
        """Send PING frames periodically and monitor pong."""
        try:
            while not self._stop and getattr(self, 'sock', None) and getattr(self.sock, 'connected', False):
                if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 5:
                    log("No pong → closing WS")
                    try:
                        self.close()
                    except Exception:
                        pass
                    break
                try:
                    self.send("ping", opcode=websocket.ABNF.OPCODE_PING)
                except Exception:
                    pass
                time.sleep(HEARTBEAT_INTERVAL)
        except Exception as e:
            log(f"Heartbeat thread error: {e}")

    def run_forever(self, **kwargs):
        """Wrap run_forever with exponential backoff reconnects."""
        self._stop = False
        while not self._stop:
            try:
                super().run_forever(ping_interval=None, ping_timeout=None, **kwargs)
            except Exception as e:
                log(f"WS crash: {e}")
            self.reconnect += 1
            delay = min(MAX_RECONNECT_DELAY, RECONNECT_BASE_DELAY * (2 ** (self.reconnect - 1)))
            log(f"Reconnect in {delay}s")
            time.sleep(delay)

    def stop(self):
        self._stop = True
        try:
            self.close()
        except Exception:
            pass

# ========================= WS HANDLERS =========================
def on_market_message(ws, message):
    """Parse combined stream messages from Binance.US stream endpoints."""
    try:
        data = json.loads(message)
        stream = data.get('stream', '')
        payload = data.get('data', {})
        if not payload:
            return
        sym = stream.split('@')[0].upper()

        if stream.endswith('@ticker'):
            price = _safe_decimal(payload.get('c', '0'))
            if price > ZERO:
                with price_lock:
                    live_prices[sym] = price
        elif stream.endswith(f'@depth{DEPTH_LEVELS}'):
            bids = [( _safe_decimal(p), _safe_decimal(q)) for p, q in payload.get('bids', [])[:DEPTH_LEVELS]]
            asks = [( _safe_decimal(p), _safe_decimal(q)) for p, q in payload.get('asks', [])[:DEPTH_LEVELS]]
            with book_lock:
                live_bids[sym] = bids
                live_asks[sym] = asks
    except Exception as e:
        log(f"Market WS parse error: {e}")

def on_user_message(ws, message):
    """Handle user execution reports (orders/fills)."""
    try:
        data = json.loads(message)
        if data.get('e') != 'executionReport':
            return
        ev = data
        sym = ev.get('s')
        side = ev.get('S')
        status = ev.get('X')
        price = _safe_decimal(ev.get('p', '0'))
        qty = _safe_decimal(ev.get('q', '0'))
        if status in ('FILLED', 'PARTIALLY_FILLED'):
            log(f"FILL {side} {sym} @ {price} | {qty}")
            send_whatsapp(f"{side} {sym} {status} @ {price}")
    except Exception as e:
        log(f"User WS error: {e}")

def on_ws_error(ws, err):
    try:
        log(f"WS error ({getattr(ws, 'url', 'unknown')}): {err}")
    except Exception:
        log(f"WS error: {err}")

def on_ws_close(ws, code, msg):
    try:
        log(f"WS closed ({getattr(ws, 'url', 'unknown')}) – {code}: {msg}")
    except Exception:
        log(f"WS closed – {code}: {msg}")

# ========================= WS STARTERS =========================
def start_market_websockets(symbols):
    """Start websocket connections for price and depth streams (chunked)."""
    global ws_instances, ws_threads
    ticker = [f"{s.lower()}@ticker" for s in symbols]
    depth = [f"{s.lower()}@depth{DEPTH_LEVELS}" for s in symbols]
    streams = ticker + depth
    chunks = [streams[i:i + MAX_STREAMS_PER_CONNECTION] for i in range(0, len(streams), MAX_STREAMS_PER_CONNECTION)]

    for chunk in chunks:
        url = f"wss://stream.binance.us:9443/stream?streams={'/'.join(chunk)}"
        ws = HeartbeatWebSocket(
            url,
            on_message=on_market_message,
            on_error=on_ws_error,
            on_close=on_ws_close,
            on_open=lambda ws: log("Market WS open")
        )
        ws_instances.append(ws)
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        time.sleep(0.4)

def start_user_stream():
    """Create a listen key and open a private user websocket for order updates."""
    global user_ws, listen_key
    try:
        with listen_key_lock:
            listen_key = client.stream_get_listen_key()
        url = f"wss://stream.binance.us:9443/ws/{listen_key}"
        user_ws = HeartbeatWebSocket(
            url,
            on_message=on_user_message,
            on_error=on_ws_error,
            on_close=on_ws_close,
            on_open=lambda ws: log("User WS open")
        )
        t = threading.Thread(target=user_ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        log("User stream started")
    except Exception as e:
        log(f"User stream failed: {e}")

def keepalive_user_stream():
    """Periodically ping Binance to keep listenKey alive."""
    while st.session_state.bot_running:
        time.sleep(1800)  # 30 minutes
        try:
            with listen_key_lock:
                if listen_key:
                    client.stream_keepalive(listen_key)
        except Exception:
            pass

# ========================= TRADING LOGIC =========================
def get_top_volume_symbols():
    """Calculate top symbols by 5-level bid volume and update global list."""
    global top_volume_symbols
    candidates = []
    with book_lock:
        for sym, bids in live_bids.items():
            if len(bids) < DEPTH_LEVELS:
                continue
            vol = sum((p * q) for p, q in bids[:DEPTH_LEVELS])
            try:
                volf = float(vol)
            except Exception:
                volf = 0.0
            if volf > 0:
                candidates.append((sym, volf))
    candidates.sort(key=lambda x: x[1], reverse=True)
    top_volume_symbols = [s for s, _ in candidates[:TOP_N_VOLUME]]
    log(f"Top {len(top_volume_symbols)} volume symbols refreshed")

def rebalance_portfolio():
    """Core rebalancing: sells excess and buys up to MAX_BUY_COINS from top_volume_symbols."""
    global last_rebalance_str
    try:
        if not is_trading_hours():
            log("Outside 4AM-4PM CST → no buys")
            return
        if not top_volume_symbols:
            log("No volume data yet")
            return

        update_balances()
        total = get_total_portfolio_value()
        # Use slider percentage if present
        try:
            pct = Decimal(str(st.session_state.get('max_position_pct', 5.0))) / Decimal('100')
        except Exception:
            pct = MAX_POSITION_PCT
        target = total * pct
        usdt_free = account_balances.get('USDT', ZERO)
        investable = usdt_free * SAFETY_BUFFER

        # compute active positions in USDT value
        active_positions.clear()
        for asset, qty in account_balances.items():
            if asset == 'USDT':
                continue
            sym = f"{asset}USDT"
            if sym in live_prices:
                active_positions[sym] = qty * live_prices[sym]

        # sell excess positions above target
        for sym, val in list(active_positions.items()):
            try:
                if val > target:
                    excess = val - target
                    price = live_prices.get(sym, ZERO)
                    if price <= ZERO:
                        continue
                    step = symbol_info.get(sym, {}).get('stepSize')
                    if not step:
                        continue
                    qty = (excess / price).quantize(step, rounding=ROUND_DOWN)
                    if qty > ZERO:
                        with book_lock:
                            bid = live_bids.get(sym, [(ZERO, ZERO)])[0][0]
                        if bid > ZERO:
                            place_limit_order(sym, 'SELL', bid, qty)
            except Exception as e:
                log(f"Error selling {sym}: {e}")

        # buy top targets (top 2 by default)
        buy_targets = [s for s in top_volume_symbols[:MAX_BUY_COINS] if s not in active_positions]
        buys = 0
        for sym in buy_targets:
            if buys >= MAX_BUY_COINS:
                break
            try:
                needed = min(target, investable)
                if needed < Decimal(st.session_state.get('min_trade_value', MIN_TRADE_VALUE)):
                    continue
                with book_lock:
                    ask = live_asks.get(sym, [(ZERO, ZERO)])[0][0]
                if ask <= ZERO:
                    continue
                buy_price = (ask * (ONE - ENTRY_PCT_BELOW_ASK))
                tick = symbol_info.get(sym, {}).get('tickSize')
                step = symbol_info.get(sym, {}).get('stepSize')
                if not tick or not step:
                    continue
                buy_price = round_down_to_step(buy_price, tick)
                qty = ((needed / buy_price)).quantize(step, rounding=ROUND_DOWN)
                order_val = buy_price * qty
                if order_val < Decimal(st.session_state.get('min_trade_value', MIN_TRADE_VALUE)) or order_val > investable:
                    continue
                order = place_limit_order(sym, 'BUY', buy_price, qty)
                if order:
                    buys += 1
                    investable -= order_val
            except Exception as e:
                log(f"Error buying {sym}: {e}")

        last_rebalance_str = now_cst()
        log(f"REBALANCE | Buys:{buys} | Targets:{buy_targets[:MAX_BUY_COINS]}")
    except Exception as e:
        log(f"Rebalance error: {e}")

# ========================= BACKGROUND INITIALISER =========================
def initialise_bot():
    """Runs in a background thread – sets bot_running when ready."""
    try:
        st.session_state.init_status = "Loading symbol info..."
        load_symbol_info()

        st.session_state.init_status = "Fetching balances..."
        update_balances()

        all_syms = list(symbol_info.keys())
        st.session_state.init_status = f"Starting {len(all_syms)} WS streams..."
        # avoid starting huge number of streams at once
        start_list = all_syms[:1000] if len(all_syms) > 1000 else all_syms
        start_market_websockets(start_list)

        st.session_state.init_status = "Starting user stream..."
        start_user_stream()
        threading.Thread(target=keepalive_user_stream, daemon=True).start()

        # Wait for *some* market data (max 15 s)
        st.session_state.init_status = "Waiting for price/book data..."
        deadline = time.time() + 15
        while time.time() < deadline:
            if live_prices or live_bids:
                break
            time.sleep(0.5)

        st.session_state.init_status = "Fetching first volume ranking..."
        get_top_volume_symbols()

        st.session_state.bot_running = True
        st.session_state.init_status = "READY"
        log("PLATINUM TRADER STARTED")
        send_whatsapp("PLATINUM TRADER STARTED")
    except Exception as e:
        log(f"Initialise bot error: {e}")
        st.session_state.init_status = f"ERROR: {e}"
        st.session_state.bot_running = False

# ========================= STREAMLIT UI =========================
def main():
    global client
    st.set_page_config(page_title="Platinum Crypto Trader", layout="wide")
    st.title("PLATINUM CRYPTO TRADER DASHBOARD")
    st.markdown("**4 AM – 4 PM CST | Top 25 volume → Top 2 buys | 5 % max per coin**")

    # --- ENSURE REQUIRED SESSION STATE KEYS EXIST (fix for AttributeError) ---
    required_keys = {
        'logs': [], 'shutdown': False, 'auto_rebalance': True,
        'rebalance_interval': 7200, 'min_trade_value': 5.0,
        'max_position_pct': 5.0, 'entry_pct_below_ask': 0.1,
        'bot_running': False, 'init_status': ''
    }
    for k, v in required_keys.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # -----------------------------------------------------------------
    # API key check
    # -----------------------------------------------------------------
    if not os.getenv('BINANCE_API_KEY') or not os.getenv('BINANCE_API_SECRET'):
        st.error("Set `BINANCE_API_KEY` and `BINANCE_API_SECRET` env vars")
        st.stop()

    # create client (Binance.US uses tld='us')
    try:
        client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'), tld='us')
    except Exception as e:
        st.error(f"Failed to create Binance client: {e}")
        st.stop()

    # -----------------------------------------------------------------
    # START / STATUS UI (non-blocking auto-refresh to show init progress)
    # -----------------------------------------------------------------
    if not st.session_state.bot_running:
        col_btn, col_status = st.columns([1, 3])
        with col_btn:
            # START button triggers background initialization
            if st.button("START TRADER (REAL)"):
                # ensure previous thread not running
                if not st.session_state.get('init_thread_running', False):
                    st.session_state.init_thread_running = True
                    st.session_state.init_status = "Initialising..."
                    t = threading.Thread(target=initialise_bot, daemon=True)
                    t.start()
        with col_status:
            st.write("**Status:**", st.session_state.init_status or "Press START")
            st.write("Note: This page auto-refreshes while initializing.")
        # show recent logs
        st.markdown("---")
        st.subheader("Log (live)")
        for line in st.session_state.logs[-20:]:
            st.code(line)

        # auto-refresh once per second while not running
        time.sleep(1)
        # trigger a re-run to reflect background thread progress
        try:
            st.experimental_rerun()
        except Exception:
            # if experimental_rerun not available, just return
            return

    # -----------------------------------------------------------------
    # BOT IS RUNNING → live dashboard
    # -----------------------------------------------------------------
    update_balances()
    total = get_total_portfolio_value()
    usdt = account_balances.get('USDT', ZERO)
    in_hours = is_trading_hours()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("USDT Free", f"${usdt:.2f}")
    c2.metric("Portfolio Value", f"${total:.2f}")
    c3.metric("Positions", len(active_positions))
    c4.metric("Trading Window", "YES" if in_hours else "NO")

    st.metric("Last Rebalance", last_rebalance_str)

    # top-volume preview
    st.write("**Top 10 volume coins:** " + " | ".join(top_volume_symbols[:10]))

    colA, colB = st.columns([1, 2])
    with colA:
        st.session_state.max_position_pct = st.slider(
            "Max % per Coin", 1.0, 10.0, float(st.session_state.max_position_pct)
        )
        st.session_state.min_trade_value = st.number_input(
            "Min Trade $", 1.0, 50.0, st.session_state.min_trade_value
        )
        if st.button("Rebalance Now") and in_hours:
            threading.Thread(target=rebalance_portfolio, daemon=True).start()

    with colB:
        st.write("**Live 5-level bid volume (top 5):**")
        for sym in top_volume_symbols[:5]:
            with book_lock:
                vol = sum((p * q) for p, q in live_bids.get(sym, []))
            try:
                vdisplay = f"${float(vol):,.0f}"
            except Exception:
                vdisplay = f"{vol}"
            st.write(f"**{sym}** – {vdisplay}")

    st.markdown("---")
    st.subheader("Log")
    for line in st.session_state.logs[-50:]:
        st.code(line)

    # -----------------------------------------------------------------
    # Auto-rebalance loop (non-blocking; triggers per interval)
    # -----------------------------------------------------------------
    if st.session_state.auto_rebalance and in_hours:
        if 'last_auto' not in st.session_state:
            st.session_state.last_auto = time.time()
        if time.time() - st.session_state.last_auto > st.session_state.rebalance_interval:
            threading.Thread(target=rebalance_portfolio, daemon=True).start()
            st.session_state.last_auto = time.time()
        # refresh top-volume
        get_top_volume_symbols()

    st.success("SAFE • SMART • PLATINUM-GRADE (REAL TRADING ENABLED)")

if __name__ == "__main__":
    main()
