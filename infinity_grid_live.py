#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — PROFESSIONAL DASHBOARD EDITION
- Real-time P&L, Positions, Market Overview, Buy Signals
- SQLite DB via SQLAlchemy (auto-import holdings, track P&L, active grids, positions)
- Rebalance every 15 minutes based on bid volume in profit direction
- Only sell at 1% profit + maker/taker fees
- Enhanced dashboard: buttons, sliders, metrics, tables
"""

import streamlit as st
import os
import time
import threading
import json
import requests
import websocket
from decimal import Decimal, getcontext
from datetime import datetime, timedelta
from binance.client import Client
from binance.exceptions import BinanceAPIException
import logging
from logging.handlers import TimedRotatingFileHandler
import sys
import pandas as pd
import numpy as np

# ========================= SQLALCHEMY SETUP =========================
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.exc import SQLAlchemyError

Base = declarative_base()
engine = create_engine(f"sqlite:///{os.path.expanduser('~/infinity_grid_bot.db')}", echo=False)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))

# Global session factory for convenience
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class Trade(Base):
    __tablename__ = 'trades'
    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)  # BUY / SELL
    price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    order_id = Column(String, unique=True)

class Position(Base):
    __tablename__ = 'positions'
    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, unique=True)
    avg_buy_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    invested_usdt = Column(Float, nullable=False)
    last_updated = Column(DateTime, default=datetime.utcnow)
    active_grid = Column(Boolean, default=True)

# Create tables
Base.metadata.create_all(engine)

# ========================= CONFIG =========================
getcontext().prec = 28
ZERO = Decimal('0')
SAFETY_BUFFER = Decimal('0.95')
PROFIT_TARGET = Decimal('1.01')  # 1% profit
MAKER_FEE = Decimal('0.0010')    # 0.1%
TAKER_FEE = Decimal('0.0010')    # 0.1%
REBALANCE_INTERVAL = 15 * 60     # 15 minutes

LOG_FILE = os.path.expanduser("~/infinity_grid_bot.log")

def setup_logging():
    logger = logging.getLogger("infinity_bot")
    logger.setLevel(logging.INFO)
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, 'a').close()
    file_handler = TimedRotatingFileHandler(LOG_FILE, when='midnight', backupCount=14)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

logger = setup_logging()

# ========================= GLOBALS =========================
client = None
all_symbols = []
gridded_symbols = []
bid_volume = {}
symbol_info = {}
account_balances = {}
state_lock = threading.Lock()
last_regrid_str = "Never"
last_rebalance = datetime.min

# ========================= UTILS =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    line = f"{now_str()} - {msg}"
    print(line)
    logger.info(msg)
    if 'logs' in st.session_state:
        st.session_state.logs.append(line)
        if len(st.session_state.logs) > 1000:
            st.session_state.logs = st.session_state.logs[-1000:]

def send_whatsapp(msg):
    phone = os.getenv('CALLMEBOT_PHONE')
    key = os.getenv('CALLMEBOT_API_KEY')
    if phone and key:
        try:
            msg = requests.utils.quote(msg)
            requests.get(f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={msg}&apikey={key}", timeout=5)
        except:
            pass

# ========================= DB HELPERS =========================
def get_session():
    return SessionLocal()

def import_existing_positions():
    """Auto-import current Binance holdings into DB"""
    db = get_session()
    try:
        info = client.get_account()['balances']
        for bal in info:
            asset = bal['asset']
            free = Decimal(bal['free'])
            if free <= ZERO or asset == 'USDT':
                continue
            symbol = asset + 'USDT'
            if symbol not in symbol_info:
                continue
            # Get average price from trade history (last 1000 trades)
            trades = client.get_my_trades(symbol=symbol, limit=1000)
            buys = [t for t in trades if t['isBuyer']]
            if not buys:
                continue
            total_qty = sum(Decimal(t['qty']) for t in buys)
            total_cost = sum(Decimal(t['qty']) * Decimal(t['price']) for t in buys)
            if total_qty > ZERO:
                avg_price = float(total_cost / total_qty)
                pos = db.query(Position).filter_by(symbol=symbol).first()
                if not pos:
                    pos = Position(
                        symbol=symbol,
                        avg_buy_price=avg_price,
                        quantity=float(free),
                        invested_usdt=float(free * Decimal(str(avg_price))),
                        active_grid=False
                    )
                    db.add(pos)
                else:
                    pos.quantity = float(free)
                    pos.invested_usdt = float(free * Decimal(str(avg_price)))
                log(f"Imported {symbol}: {free} @ ${avg_price:.4f}")
        db.commit()
    except Exception as e:
        db.rollback()
        log(f"Import failed: {e}")
    finally:
        db.close()

def record_trade(symbol, side, price, quantity, order_id):
    db = get_session()
    try:
        trade = Trade(
            symbol=symbol, side=side, price=price, quantity=quantity, order_id=order_id
        )
        db.add(trade)
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        log(f"DB record failed: {e}")
    finally:
        db.close()

def update_position_from_fills(symbol, fills):
    """Update position after order fill"""
    db = get_session()
    try:
        pos = db.query(Position).filter_by(symbol=symbol).first()
        if not pos:
            return
        for fill in fills:
            qty = Decimal(str(fill['qty']))
            price = Decimal(str(fill['price']))
            if fill['isBuyer']:
                new_invested = pos.invested_usdt + float(qty * price)
                new_qty = pos.quantity + float(qty)
                if new_qty > 0:
                    pos.avg_buy_price = new_invested / new_qty
                    pos.quantity = new_qty
                    pos.invested_usdt = new_invested
            else:
                pos.quantity -= float(qty)
                if pos.quantity <= 0:
                    db.delete(pos)
            pos.last_updated = datetime.utcnow()
        db.commit()
    except Exception as e:
        db.rollback()
        log(f"Position update failed: {e}")
    finally:
        db.close()

def get_positions():
    db = get_session()
    try:
        return db.query(Position).filter(Position.quantity > 0).all()
    finally:
        db.close()

def get_position(symbol):
    db = get_session()
    try:
        return db.query(Position).filter_by(symbol=symbol).first()
    finally:
        db.close()

# ========================= BALANCE & INFO =========================
def update_balances():
    global account_balances
    try:
        info = client.get_account()['balances']
        account_balances = {
            a['asset']: Decimal(a['free']) for a in info if Decimal(a['free']) > ZERO
        }
        log(f"Updated balances: USDT={account_balances.get('USDT', ZERO):.2f}")
    except Exception as e:
        log(f"Balance update failed: {e}")

def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()['symbols']
        for s in info:
            if s['quoteAsset'] != 'USDT' or s['status'] != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s['filters']}
            symbol_info[s['symbol']] = {
                'stepSize': Decimal(filters['LOT_SIZE']['stepSize']),
                'tickSize': Decimal(filters['PRICE_FILTER']['tickSize']),
                'minQty': Decimal(filters['LOT_SIZE']['minQty']),
                'minNotional': Decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            }
        log(f"Loaded info for {len(symbol_info)} symbols")
    except Exception as e:
        log(f"Symbol info error: {e}")

# ========================= SAFE ORDER FUNCTION =========================
def round_step(value, step):
    return (value // step) * step

def place_limit_order(symbol, side, price, quantity):
    info = symbol_info.get(symbol)
    if not info:
        log(f"No symbol info for {symbol}")
        return False

    price = round_step(price, info['tickSize'])
    quantity = round_step(quantity, info['stepSize'])

    if quantity < info['minQty']:
        log(f"Qty too small {symbol}: {quantity} < {info['minQty']}")
        return False

    notional = price * quantity
    if notional < Decimal(info['minNotional']):
        log(f"Notional too low {symbol}: {notional} < {info['minNotional']}")
        return False

    # CASH CHECK FOR BUY
    if side == 'BUY':
        needed = notional * SAFETY_BUFFER
        usdt_free = account_balances.get('USDT', ZERO)
        if needed > usdt_free:
            log(f"NOT ENOUGH USDT for {symbol} BUY: need {needed:.2f}, have {usdt_free:.2f}")
            return False

    # QTY CHECK FOR SELL
    if side == 'SELL':
        base = symbol.replace('USDT', '')
        coin_free = account_balances.get(base, ZERO)
        if quantity > coin_free * SAFETY_BUFFER:
            log(f"NOT ENOUGH {base} for SELL: need {quantity}, have {coin_free}")
            return False

        # PROFIT CHECK: Only sell at 1% + fees
        pos = get_position(symbol)
        if pos and price < pos.avg_buy_price * float(PROFIT_TARGET * (1 + TAKER_FEE)):
            target = pos.avg_buy_price * float(PROFIT_TARGET * (1 + TAKER_FEE))
            log(f"PROFIT TOO LOW {symbol}: {price} < {target:.4f}")
            return False

    try:
        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=f"{quantity:.8f}".rstrip('0').rstrip('.'),
            price=f"{price:.8f}".rstrip('0').rstrip('.')
        )
        order_id = order['orderId']
        log(f"PLACED {side} {symbol} {quantity} @ {price} | ID:{order_id}")
        send_whatsapp(f"{side} {symbol} {quantity}@{price}")
        record_trade(symbol, side, float(price), float(quantity), str(order_id))
        return True
    except BinanceAPIException as e:
        log(f"ORDER FAILED {symbol} {side}: {e.message} (Code: {e.code})")
        return False
    except Exception as e:
        log(f"ORDER ERROR {symbol}: {e}")
        return False

# ========================= GRID & REBALANCE =========================
def place_grid(symbol):
    price = bid_volume.get(symbol, ZERO)
    if price <= ZERO:
        log(f"No price for {symbol}")
        return

    qty_usdt = Decimal(str(st.session_state.grid_size))
    raw_qty = qty_usdt / price
    info = symbol_info.get(symbol)
    if not info:
        return

    qty = round_step(raw_qty, info['stepSize'])
    if qty < info['minQty']:
        log(f"Qty too small for {symbol}")
        return

    total_buy_cost = qty * price * st.session_state.grid_levels * SAFETY_BUFFER
    if total_buy_cost > account_balances.get('USDT', ZERO):
        log(f"SKIPPING {symbol}: Not enough USDT for {st.session_state.grid_levels} levels")
        return

    buys = sells = 0
    for i in range(1, st.session_state.grid_levels + 1):
        buy_price = price * (1 - Decimal('0.015') * i)
        sell_price = price * (1 + Decimal('0.015') * i) * PROFIT_TARGET * (1 + MAKER_FEE)

        if place_limit_order(symbol, 'BUY', buy_price, qty):
            buys += 1
        if place_limit_order(symbol, 'SELL', sell_price, qty):
            sells += 1

    global last_regrid_str
    last_regrid_str = now_str()
    log(f"GRID {symbol} | B:{buys} S:{sells} | ${qty_usdt} per level")

def rebalance_grids():
    global last_rebalance
    if datetime.now() - last_rebalance < timedelta(seconds=REBALANCE_INTERVAL):
        return
    last_rebalance = datetime.now()

    update_balances()
    if not bid_volume:
        log("No price data, skipping rebalance")
        return

    # Only consider symbols with positive P&L direction
    top = []
    for sym, vol in bid_volume.items():
        pos = get_position(sym)
        current_price = bid_volume.get(sym, ZERO)
        if not current_price:
            continue
        if pos and current_price > pos.avg_buy_price * float(PROFIT_TARGET):
            top.append((sym, vol))
        elif not pos:
            top.append((sym, vol))

    top = sorted(top, key=lambda x: x[1], reverse=True)[:25]
    targets = [s for s, _ in top][:st.session_state.target_grid_count]

    to_add = [s for s in targets if s not in gridded_symbols]
    to_remove = [s for s in gridded_symbols if s not in targets]

    for s in to_remove:
        try:
            client.cancel_open_orders(symbol=s)
            log(f"Canceled {s}")
        except: pass
        gridded_symbols.remove(s)

    for s in to_add:
        if s not in symbol_info:
            continue
        gridded_symbols.append(s)
        place_grid(s)

    log(f"REBALANCED +{len(to_add)} -{len(to_remove)} → {len(gridded_symbols)} GRIDS")
    send_whatsapp(f"REBALANCE: {len(to_add)} new grids")

# ========================= WEBSOCKET =========================
def start_websockets():
    streams = "/".join([f"{s.lower()}@ticker" for s in all_symbols[:100]])
    url = f"wss://stream.binance.us:9443/stream?streams={streams}"

    def run():
        ws = websocket.WebSocketApp(url, on_message=lambda ws, msg: on_msg(msg))
        while True:
            try:
                ws.run_forever(ping_interval=25)
                time.sleep(5)
            except:
                time.sleep(5)

    threading.Thread(target=run, daemon=True).start()

def on_msg(msg):
    try:
        data = json.loads(msg)
        if 'data' not in data: return
        payload = data['data']
        sym = data['stream'].split('@')[0].upper()
        if payload.get('c'):
            with state_lock:
                bid_volume[sym] = Decimal(str(payload['c']))
    except: pass

# ========================= DASHBOARD METRICS =========================
def get_pnl_summary():
    total_invested = 0
    total_value = 0
    positions = get_positions()
    for pos in positions:
        current_price = bid_volume.get(pos.symbol, ZERO)
        if current_price:
            total_invested += pos.invested_usdt
            total_value += pos.quantity * float(current_price)
    pnl = total_value - total_invested if total_invested > 0 else 0
    pnl_pct = (pnl / total_invested * 100) if total_invested > 0 else 0
    return {
        'positions': len(positions),
        'invested': total_invested,
        'value': total_value,
        'pnl': pnl,
        'pnl_pct': pnl_pct
    }

# ========================= MAIN =========================
def main():
    for k, v in {
        'bot_running': False, 'grid_size': 50.0, 'grid_levels': 5,
        'target_grid_count': 10, 'auto_rotate': True, 'rotation_interval': 300,
        'logs': [], 'shutdown': False, 'show_pnl': True, 'show_positions': True
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

    st.set_page_config(page_title="Pro Grid Bot", layout="wide")
    st.title("∞ INFINITY GRID BOT — PROFESSIONAL DASHBOARD")

    if not os.getenv('BINANCE_API_KEY'):
        st.error("Set BINANCE_API_KEY and BINANCE_API_SECRET")
        st.stop()

    global client
    client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'), tld='us')

    # Sidebar Controls
    with st.sidebar:
        st.header("Controls")
        if not st.session_state.bot_running:
            if st.button("START BOT", type="primary", use_container_width=True):
                with st.spinner("Initializing..."):
                    info = client.get_exchange_info()['symbols']
                    global all_symbols
                    all_symbols = [s['symbol'] for s in info if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
                    load_symbol_info()
                    update_balances()
                    import_existing_positions()
                    start_websockets()
                    time.sleep(3)
                    rebalance_grids()
                    st.session_state.bot_running = True
                    log("BOT STARTED — PROFESSIONAL MODE")
                    send_whatsapp("PRO GRID BOT STARTED")
                st.rerun()
        else:
            col1, col2 = st.columns(2)
            with col1:
                if st.button("REBALANCE", use_container_width=True):
                    rebalance_grids()
            with col2:
                if st.button("ROTATE", use_container_width=True):
                    rebalance_grids()  # Use rebalance

            st.session_state.grid_size = st.slider("Grid Size ($)", 10.0, 500.0, st.session_state.grid_size, 5.0)
            st.session_state.grid_levels = st.slider("Grid Levels", 1, 15, st.session_state.grid_levels)
            st.session_state.target_grid_count = st.slider("Max Grids", 1, 30, st.session_state.target_grid_count)

            st.markdown("---")
            usdt = account_balances.get('USDT', ZERO)
            st.metric("USDT Free", f"${usdt:.2f}")
            st.metric("Active Grids", len(gridded_symbols))
            st.metric("Last Rebalance", last_regrid_str)

    if st.session_state.bot_running:
        update_balances()
        pnl = get_pnl_summary()

        # Top Metrics
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.metric("Total P&L", f"${pnl['pnl']:+.2f}", f"{pnl['pnl_pct']:+.2f}%")
        with col2:
            st.metric("Invested", f"${pnl['invested']:.2f}")
        with col3:
            st.metric("Current Value", f"${pnl['value']:.2f}")
        with col4:
            st.metric("Positions", pnl['positions'])
        with col5:
            st.metric("Buy Signals", len([s for s in bid_volume if s not in gridded_symbols][:5]))

        # Tabs
        tab1, tab2, tab3, tab4 = st.tabs(["Positions", "Market Heatmap", "Active Grids", "Logs"])

        with tab1:
            positions = get_positions()
            if positions:
                data = []
                for pos in positions:
                    cur_price = bid_volume.get(pos.symbol, ZERO)
                    unrealized = (cur_price * Decimal(str(pos.quantity))) - Decimal(str(pos.invested_usdt))
                    unrealized_pct = (unrealized / Decimal(str(pos.invested_usdt)) * 100) if pos.invested_usdt > 0 else 0
                    data.append({
                        'Symbol': pos.symbol,
                        'Qty': f"{pos.quantity:.4f}",
                        'Avg Buy': f"${pos.avg_buy_price:.4f}",
                        'Current': f"${float(cur_price):.4f}" if cur_price else "—",
                        'Unrealized': f"${float(unrealized):+.2f}",
                        '%': f"{float(unrealized_pct):+.2f}%"
                    })
                df = pd.DataFrame(data)
                st.dataframe(df, use_container_width=True)
            else:
                st.info("No active positions")

        with tab2:
            top_vol = sorted(bid_volume.items(), key=lambda x: x[1], reverse=True)[:20]
            if top_vol:
                df = pd.DataFrame(top_vol, columns=['Symbol', 'Bid Volume'])
                df['Signal'] = df['Symbol'].apply(lambda x: 'BUY' if x not in gridded_symbols else 'GRID')
                st.bar_chart(df.set_index('Symbol')['Bid Volume'])
                st.dataframe(df, use_container_width=True)
            else:
                st.info("Waiting for market data...")

        with tab3:
            st.write("**Active Grids:** " + " | ".join(gridded_symbols[:20]))
            if st.button("Cancel All & Restart"):
                for s in gridded_symbols[:]:
                    try:
                        client.cancel_open_orders(symbol=s)
                    except: pass
                gridded_symbols.clear()
                rebalance_grids()

        with tab4:
            for line in st.session_state.logs[-50:]:
                st.code(line)

        st.success("PROFESSIONAL DASHBOARD — P&L, POSITIONS, SIGNALS, REBALANCING")

        # Auto-rebalance thread
        if st.session_state.get('auto_rotate', True):
            threading.Thread(target=rebalance_grids, daemon=True).start()

    else:
        st.info("Click **START BOT** in sidebar to begin")

if __name__ == "__main__":
    main()
