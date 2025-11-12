#!/usr/bin/env python3
import streamlit as st
import os
import time
import threading
import logging
import json
import websocket
import requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func, text
from sqlalchemy.orm import declarative_base, sessionmaker
import pandas as pd
import numpy as np
import pytz

# ========================= CONFIG =========================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    st.error("Set BINANCE_API_KEY and BINANCE_API_SECRET")
    st.stop()

# Trading settings
PROFIT_TARGET = Decimal('0.008')  # 0.8%
GRID_STEP = Decimal('0.003')      # 0.3%
MAX_POSITIONS = 15
MIN_ORDER_USDT = Decimal('10')

# WebSocket
WS_BASE = "wss://stream.binance.us:9443/stream?streams="
USER_STREAM_BASE = "wss://stream.binance.us:9443/ws/"
HEARTBEAT_INTERVAL = 25

# Timezone
CST = pytz.timezone('America/Chicago')

# ========================= GLOBALS (Thread-Safe) =========================
class GlobalState:
    emergency_stopped = False
    last_full_refresh = 0.0
    positions = {}
    prices = {}
    orderbooks = {}
    active_orders = {}
    pnl_today = Decimal('0')
    trades_today = 0
    listen_key = None

state = GlobalState()
locks = {
    'prices': threading.Lock(),
    'orderbooks': threading.Lock(),
    'positions': threading.Lock(),
    'orders': threading.Lock(),
}

# ========================= LOGGING =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========================= DATABASE =========================
engine = create_engine("sqlite:///binance_trades.db", future=True)
Base = declarative_base()

class Trade(Base):
    __tablename__ = 'trades'
    id = Column(Integer, primary_key=True)
    symbol = Column(String)
    side = Column(String)
    price = Column(Numeric(20,8))
    qty = Column(Numeric(20,8))
    time = Column(DateTime, default=func.now())

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ========================= HELPERS =========================
def send_alert(msg):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}",
                timeout=5
            )
        except:
            pass

def now_str():
    return datetime.now(CST).strftime("%m-%d %H:%M:%S")

# ========================= WEBSOCKET HEARTBEAT =========================
class HeartbeatWS(websocket.WebSocketApp):
    def __init__(self, url, on_message=None, on_error=None, on_close=None, on_open=None):
        super().__init__(url, on_message=on_message, on_error=on_error, on_close=on_close, on_open=on_open)
        self.last_pong = time.time()

    def on_pong(self, *args):
        self.last_pong = time.time()

    def run_forever(self, *args, **kwargs):
        while True:
            try:
                super().run_forever(ping_interval=HEARTBEAT_INTERVAL, ping_timeout=10, *args, **kwargs)
            except:
                pass
            time.sleep(10)

# ========================= WEBSOCKET HANDLERS =========================
def on_price_update(ws, msg):
    data = json.loads(msg)
    for item in data.get('data', []):
        if 's' not in item: continue
        sym = item['s']
        price = Decimal(item['c'])
        with locks['prices']:
            state.prices[sym] = price

def on_orderbook_update(ws, msg):
    data = json.loads(msg)
    stream = data.get('stream', '')
    payload = data.get('data', {})
    if not payload: return
    sym = stream.split('@')[0].upper()
    bids = [(Decimal(p), Decimal(q)) for p, q in payload.get('b', [])[:5]]
    asks = [(Decimal(p), Decimal(q)) for p, q in payload.get('a', [])[:5]]
    with locks['orderbooks']:
        state.orderbooks[sym] = {'bids': bids, 'asks': asks}

def on_user_update(ws, msg):
    data = json.loads(msg)
    if data.get('e') != 'executionReport': return
    e = data
    sym = e['s']
    side = e['S']
    status = e['X']
    price = Decimal(e['p'])
    qty = Decimal(e['q'])
    if status in ('FILLED', 'PARTIALLY_FILLED'):
        with Session() as s:
            s.add(Trade(symbol=sym, side=side, price=price, qty=qty))
            s.commit()
        profit = (price - state.positions.get(sym, {}).get('avg_price', price)) * qty if side == 'SELL' else 0
        if side == 'SELL':
            state.pnl_today += profit
            state.trades_today += 1
        send_alert(f"{side} {sym} FILLED @ {price}")

# ========================= LISTENKEY MANAGEMENT =========================
def start_user_stream():
    client = Client(API_KEY, API_SECRET, tld='us')
    try:
        state.listen_key = client.stream_get_listen_key()
        url = f"{USER_STREAM_BASE}{state.listen_key}"
        ws = HeartbeatWS(url, on_message=on_user_update, on_error=lambda w,e: logger.error(f"User WS: {e}"))
        threading.Thread(target=ws.run_forever, daemon=True).start()
        logger.info("User stream started")
    except Exception as e:
        logger.error(f"ListenKey failed: {e}")

def keepalive_listenkey():
    client = Client(API_KEY, API_SECRET, tld='us')
    while True:
        time.sleep(1800)
        if state.listen_key:
            try:
                client.stream_keepalive(state.listen_key)
            except:
                pass

# ========================= TRADING LOGIC =========================
client = Client(API_KEY, API_SECRET, tld='us')

def get_balance(asset='USDT'):
    try:
        return Decimal(client.get_asset_balance(asset)['free'])
    except:
        return Decimal('0')

def place_order(symbol, side, qty, price=None):
    try:
        if price:
            order = client.order_limit_buy(symbol=symbol, quantity=str(qty), price=str(price)) if side == 'BUY' else \
                    client.order_limit_sell(symbol=symbol, quantity=str(qty), price=str(price))
        else:
            order = client.order_market_buy(symbol=symbol, quantity=str(qty)) if side == 'BUY' else \
                    client.order_market_sell(symbol=symbol, quantity=str(qty))
        with locks['orders']:
            state.active_orders[order['orderId']] = order
        return order
    except Exception as e:
        logger.error(f"Order failed {side} {symbol}: {e}")
        return None

def background_worker():
    while not state.emergency_stopped:
        try:
            # Refresh prices & books
            if time.time() - state.last_full_refresh > 60:
                with locks['prices']:
                    state.prices.clear()
                with locks['orderbooks']:
                    state.orderbooks.clear()
                state.last_full_refresh = time.time()

            # Load holdings
            acct = client.get_account()
            with locks['positions']:
                state.positions.clear()
                for b in acct['balances']:
                    if Decimal(b['free']) <= 0: continue
                    asset = b['asset']
                    if asset == 'USDT': continue
                    sym = f"{asset}USDT"
                    if sym in state.prices:
                        state.positions[sym] = {
                            'qty': Decimal(b['free']),
                            'avg_price': Decimal(b.get('avgPrice', state.prices[sym]))
                        }

            # Grid logic per symbol
            for sym in list(state.prices.keys()):
                if sym not in state.orderbooks: continue
                book = state.orderbooks[sym]
                price = state.prices[sym]
                bid, ask = book['bids'][0][0], book['asks'][0][0]

                pos = state.positions.get(sym, {'qty': Decimal('0')})

                # Sell signal
                if pos['qty'] > 0:
                    target_price = pos['avg_price'] * (1 + PROFIT_TARGET)
                    if price >= target_price:
                        qty = pos['qty']
                        place_order(sym, 'SELL', qty, ask + Decimal('0.0001'))

                # Buy signal (dip)
                if pos['qty'] == 0:
                    if price <= ask * (1 - GRID_STEP):
                        usdt = get_balance()
                        if usdt > MIN_ORDER_USDT * 2:
                            qty = (MIN_ORDER_USDT / price).quantize(Decimal('0.000001'), ROUND_DOWN)
                            place_order(sym, 'BUY', qty, bid)

            time.sleep(1)
        except Exception as e:
            logger.error(f"Worker error: {e}")
            time.sleep(5)

# ========================= STREAMLIT UI =========================
st.set_page_config(page_title="Infinity Grid Bot 2025", layout="wide")
st.title("INFINITY GRID BOT 2025 - Binance.US")

if st.button("START BOT", type="primary"):
    if not any(t.is_alive() for t in threading.enumerate() if t.name == "Worker"):
        threading.Thread(target=background_worker, name="Worker", daemon=True).start()
        threading.Thread(target=keepalive_listenkey, daemon=True).start()
        start_user_stream()
        st.success("Bot started!")

if st.button("EMERGENCY STOP & CANCEL ALL"):
    state.emergency_stopped = True
    try:
        client.cancel_all_orders()
        st.error("ALL ORDERS CANCELED")
        send_alert("EMERGENCY STOP - ALL ORDERS CANCELED")
    except:
        pass

# Live dashboard
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("P&L Today", f"${float(state.pnl_today):.2f}")
with col2:
    st.metric("Trades Today", state.trades_today)
with col3:
    st.metric("USDT Balance", f"${float(get_balance()):.2f}")
with col4:
    st.metric("Positions", len(state.positions))

# Positions table
if state.positions:
    df = []
    for sym, pos in state.positions.items():
        current = state.prices.get(sym, Decimal('0'))
        pnl = (current - pos['avg_price']) * pos['qty'] if current else 0
        df.append({
            'Symbol': sym,
            'Qty': float(pos['qty']),
            'Avg Price': float(pos['avg_price']),
            'Current': float(current),
            'P&L': float(pnl),
            '%': f"{(pnl / (pos['avg_price'] * pos['qty']) * 100):.2f}%" if pnl != 0 else "0%"
        })
    st.dataframe(pd.DataFrame(df), use_container_width=True)

# Start WebSocket streams
if "ws_started" not in st.session_state:
    symbols = [s['symbol'] for s in client.get_exchange_info()['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    streams = '/'.join([f"{s.lower()}@ticker" for s in symbols[:100]])  # limit
    ws_price = HeartbeatWS(WS_BASE + streams, on_message=on_price_update)
    ws_book = HeartbeatWS(WS_BASE + streams.replace('ticker', 'depth5'), on_message=on_orderbook_update)
    threading.Thread(target=ws_price.run_forever, daemon=True).start()
    threading.Thread(target=ws_book.run_forever, daemon=True).start()
    st.session_state.ws_started = True

st.info("Bot running 24/7 | Updated: " + now_str())
