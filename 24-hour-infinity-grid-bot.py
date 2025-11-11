#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Infinity Grid Bot - Binance.US (Complete)
Rules:
 - Rotate top USDT pairs by top bid volume every 15 minutes
 - Maintain ≥15 coins
 - No coin > 5% of total portfolio
 - Only buy coins with RSI(14) > 70 and 14-day bullish trend
 - Only sell for profit including maker/taker fees
"""

import os, sys, time, threading, logging, requests
from decimal import Decimal, getcontext, ROUND_DOWN
from logging.handlers import TimedRotatingFileHandler
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from binance.spot import Spot
from binance.websocket.spot.websocket_client import SpotWebsocketClient
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

# === SETTINGS ===============================================================
getcontext().prec = 28
def q(d, exp="0.0001"):
    return d.quantize(Decimal(exp), rounding=ROUND_DOWN)

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
if not API_KEY or not API_SECRET:
    print("FATAL: Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

GRID_SIZE_USDT = Decimal("5.0")
NET_PROFIT_PCT = Decimal("0.018")
MAKER_FEE = Decimal("0.0002")
TAKER_FEE = Decimal("0.0004")
MAX_POSITION_PCT = Decimal("0.05")
MIN_COINS = 15
MIN_BID_VOLUME = Decimal("100000")
ROTATE_INTERVAL = 15 * 60
DB_FILE = "infinity_grid_us.db"
LOG_FILE = "infinity_grid_us.log"
MIN_SELL_MULT = (Decimal("1") + NET_PROFIT_PCT + MAKER_FEE + TAKER_FEE)
EXCLUDE_SYMBOLS = {"BUSDUSDT"}
BASE_URL = "https://api.binance.us"

# === LOGGING ================================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("grid_us")
handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=30)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s:%(message)s"))
logger.addHandler(handler)

# === DATABASE ===============================================================
Base = declarative_base()
engine = create_engine(f"sqlite:///{DB_FILE}", echo=False, future=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True)
    quantity = Column(Numeric(20,8))
    avg_entry_price = Column(Numeric(20,8))
Base.metadata.create_all(engine)

class DB:
    def __enter__(self): self.s = SessionFactory(); return self.s
    def __exit__(self, t, v, tb):
        if t: self.s.rollback()
        else: self.s.commit()
        self.s.close()

# === CLIENTS ================================================================
client = Spot(api_key=API_KEY, api_secret=API_SECRET, base_url=BASE_URL)
ws = SpotWebsocketClient(stream_url="wss://stream.binance.us:9443/ws")
ws.start()

price_cache = {}
active_symbols = set()
cache_lock = threading.Lock()

# === UTILITIES ==============================================================
def get_balance_usdt():
    try:
        for a in client.user_asset():
            if a["asset"] == "USDT":
                return Decimal(a["free"])
    except Exception as e:
        logger.error(f"get_balance_usdt: {e}")
    return Decimal("0")

def total_portfolio_usdt():
    total = Decimal("0")
    try:
        for a in client.user_asset():
            if a["asset"] == "USDT":
                total += Decimal(a["free"]) + Decimal(a["locked"])
            else:
                sym = a["asset"] + "USDT"
                price = price_cache.get(sym)
                if price:
                    total += (Decimal(a["free"]) + Decimal(a["locked"])) * price
    except Exception as e:
        logger.error(f"portfolio error: {e}")
    return total

# === RSI + TREND FILTERS ====================================================
def rsi14_from_klines(symbol):
    """Compute RSI(14) and trend slope over last 14 days"""
    try:
        data = client.klines(symbol, "1d", limit=20)
        closes = [float(x[4]) for x in data]
        if len(closes) < 15:
            return 0, False
        df = pd.DataFrame(closes, columns=["close"])
        df["diff"] = df["close"].diff()
        df["gain"] = df["diff"].clip(lower=0)
        df["loss"] = -df["diff"].clip(upper=0)
        avg_gain = df["gain"].rolling(14, min_periods=14).mean()
        avg_loss = df["loss"].rolling(14, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        last_rsi = rsi.iloc[-1]
        # bullish if slope of close prices > 0 over last 14 days
        x = np.arange(14)
        y = np.array(closes[-14:])
        slope = np.polyfit(x, y, 1)[0]
        bullish = slope > 0
        return last_rsi, bullish
    except Exception as e:
        logger.error(f"RSI calc {symbol}: {e}")
        return 0, False

# === PRICE FEED HANDLER =====================================================
def handle_price(msg):
    if "s" in msg and "c" in msg:
        with cache_lock:
            price_cache[msg["s"]] = Decimal(msg["c"])

# === STRATEGY ===============================================================
def select_top_liquid_symbols():
    tickers = client.ticker_price()
    ranked = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT") or s in EXCLUDE_SYMBOLS:
            continue
        try:
            depth = client.depth(s)
            bid_val = Decimal(depth["bids"][0][0]) * Decimal(depth["bids"][0][1])
            if bid_val >= MIN_BID_VOLUME:
                ranked.append((s, bid_val))
        except Exception:
            continue
    ranked.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, _ in ranked[:max(MIN_COINS, 15)]]
    filtered = []
    for sym in top:
        rsi, bullish = rsi14_from_klines(sym)
        if rsi > 70 and bullish:
            filtered.append(sym)
    logger.info(f"Top qualified coins: {filtered}")
    return filtered[:MIN_COINS]

def place_buy(symbol, price, usdt_alloc):
    qty = q(usdt_alloc / price)
    try:
        order = client.new_order(symbol=symbol, side="BUY", type="LIMIT",
                                 timeInForce="GTC", price=str(price), quantity=str(qty))
        logger.info(f"BUY placed {symbol} qty={qty} price={price}")
        return order
    except Exception as e:
        logger.error(f"buy error {symbol}: {e}")
        return None

def place_sell(symbol, qty, price):
    try:
        order = client.new_order(symbol=symbol, side="SELL", type="LIMIT",
                                 timeInForce="GTC", price=str(price), quantity=str(q(qty)))
        logger.info(f"SELL placed {symbol} qty={qty} price={price}")
        return order
    except Exception as e:
        logger.error(f"sell error {symbol}: {e}")
        return None

# === GRID ENGINE ============================================================
def grid_loop():
    global active_symbols
    while True:
        try:
            total_val = total_portfolio_usdt()
            usdt_bal = get_balance_usdt()
            logger.info(f"Portfolio: {total_val:.2f} USDT | Free: {usdt_bal:.2f}")

            # rotate every 15min
            if not active_symbols or (time.time() % ROTATE_INTERVAL < POLL_INTERVAL):
                active_symbols = set(select_top_liquid_symbols())
                for s in active_symbols:
                    ws.ticker(symbol=s, id=int(time.time()), callback=handle_price)
                logger.info(f"Tracking {len(active_symbols)} symbols")

            with DB() as db:
                for sym in active_symbols:
                    p = price_cache.get(sym)
                    if not p: continue

                    pos = db.query(Position).filter_by(symbol=sym).first()
                    if not pos:
                        # new buy if under 5% cap and enough balance
                        if usdt_bal < GRID_SIZE_USDT: continue
                        if (GRID_SIZE_USDT / total_val) > MAX_POSITION_PCT: continue
                        buy_price = p
                        o = place_buy(sym, buy_price, GRID_SIZE_USDT)
                        if o:
                            db.add(Position(symbol=sym, quantity=Decimal(o["origQty"]),
                                            avg_entry_price=Decimal(o["price"])))
                            usdt_bal -= GRID_SIZE_USDT
                    else:
                        sell_target = pos.avg_entry_price * MIN_SELL_MULT
                        if p >= sell_target:
                            o = place_sell(sym, pos.quantity, p)
                            if o:
                                db.delete(pos)
            time.sleep(45)
        except Exception as e:
            logger.error(f"grid loop: {e}")
            time.sleep(10)

# === MAIN ================================================================
if __name__ == "__main__":
    logger.info("Starting Infinity Grid Bot (Binance.US)")
    t = threading.Thread(target=grid_loop, daemon=True)
    t.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping bot...")
        ws.stop()
        sys.exit(0)
