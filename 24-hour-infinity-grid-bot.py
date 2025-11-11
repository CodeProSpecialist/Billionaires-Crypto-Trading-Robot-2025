#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, time, threading, logging, requests, pytz
from decimal import Decimal, getcontext
from logging.handlers import TimedRotatingFileHandler
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
from binance.client import Client
from binance.streams import ThreadedWebsocketManager

# === PRECISION ==============================================================
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
NET_PROFIT_PCT = Decimal('0.018')
FEE_PCT = Decimal('0.001')
GRID_INTERVAL_PCT = (NET_PROFIT_PCT + 2 * FEE_PCT) / (Decimal('1') - FEE_PCT)
MIN_BUFFER_USDT = Decimal('8.0')
MAX_GRIDS_PER_SIDE = 32
REBALANCE_THRESHOLD_PCT = Decimal('0.0075')
MAX_POSITION_PCT = Decimal('0.05')
POLL_INTERVAL = 45.0
LOG_FILE = "infinity_grid_bot.log"

MIN_PRICE = Decimal('1.00')
MAX_PRICE = Decimal('1000.00')
MIN_BID_VOLUME = Decimal('100000')
EXCLUDE_SYMBOLS = {'BTCUSDT','BCHUSDT','ETHUSDT'}

# === LOGGING ================================================================
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=30)
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(funcName)s:%(lineno)d - %(message)s'))
logger.addHandler(handler)

# === DATABASE ===============================================================
DB_URL = "sqlite:///binance_trades.db"
engine = create_engine(DB_URL, echo=False, future=True, pool_pre_ping=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20,8), nullable=False)
    avg_entry_price = Column(Numeric(20,8), nullable=False)

class PendingOrder(Base):
    __tablename__ = "pending_orders"
    id = Column(Integer, primary_key=True)
    binance_order_id = Column(String(64), unique=True, nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20,8), nullable=False)
    quantity = Column(Numeric(20,8), nullable=False)

class TradeRecord(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20,8), nullable=False)
    quantity = Column(Numeric(20,8), nullable=False)
    fee = Column(Numeric(20,8), nullable=False, default=0)
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

# === BINANCE CLIENT ========================================================
client = Client(API_KEY, API_SECRET)
twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
twm.start()

# === GLOBAL STATE ===========================================================
price_cache = {}
orderbook_cache = {}
cache_lock = threading.Lock()
active_symbols = set()
last_alert_time = 0

# === UTILITY FUNCTIONS =====================================================

def send_whatsapp_alert(msg: str, interval=300):
    global last_alert_time
    now = time.time()
    if now - last_alert_time < interval:
        return
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try:
            requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}",
                timeout=5
            )
            last_alert_time = now
            logger.info(f"Sent WhatsApp alert: {msg}")
        except Exception as e:
            logger.error(f"Error sending WhatsApp alert: {e}")

def get_balance():
    try:
        info = client.get_asset_balance('USDT')
        return Decimal(info['free'])
    except Exception as e:
        logger.error(f"Error getting USDT balance: {e}")
        return Decimal('0')

def get_position(symbol):
    with DBManager() as session:
        pos = session.query(Position).filter(Position.symbol==symbol).first()
        if pos:
            return Decimal(pos.quantity), Decimal(pos.avg_entry_price)
        return Decimal('0'), Decimal('0')

def place_limit_order(symbol, side, price, quantity):
    try:
        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=float(quantity),
            price=str(price)
        )
        with DBManager() as session:
            po = PendingOrder(
                binance_order_id=str(order['orderId']),
                symbol=symbol,
                side=side,
                price=price,
                quantity=quantity
            )
            session.add(po)
        logger.info(f"Placed {side} order: {symbol} {quantity} @ {price}")
    except Exception as e:
        logger.error(f"Error placing order {symbol}: {e}")

def scale_grids(symbol):
    total_balance = get_balance()
    grids = min(MAX_GRIDS_PER_SIDE, max(1, int(total_balance // GRID_SIZE_USDT)))
    return grids

def calculate_grid_prices(price, grids):
    buy_prices = [price * (1 - GRID_INTERVAL_PCT * (i+1)) for i in range(grids)]
    sell_prices = [price * (1 + GRID_INTERVAL_PCT * (i+1)) for i in range(grids)]
    return buy_prices, sell_prices

def rebalance_grids(symbol, price, pending_orders):
    threshold = REBALANCE_THRESHOLD_PCT
    for order in pending_orders:
        order_price = Decimal(order.price)
        try:
            if order.side == 'BUY' and order_price < price * (1 - threshold):
                client.cancel_order(symbol=symbol, orderId=order.binance_order_id)
            elif order.side == 'SELL' and order_price > price * (1 + threshold):
                client.cancel_order(symbol=symbol, orderId=order.binance_order_id)
        except Exception as e:
            logger.error(f"Error cancelling order {order.symbol}: {e}")

# === GRID LOOP ==============================================================
def grid_loop(symbol):
    while True:
        try:
            price = price_cache.get(symbol)
            if not price:
                time.sleep(1)
                continue

            grids = scale_grids(symbol)
            buy_prices, sell_prices = calculate_grid_prices(price, grids)

            with DBManager() as session:
                pending_orders = session.query(PendingOrder).filter(PendingOrder.symbol==symbol).all()

            rebalance_grids(symbol, price, pending_orders)

            usdt_balance = get_balance()

            for bp in buy_prices:
                if any(Decimal(po.price)==bp and po.side=='BUY' for po in pending_orders):
                    continue
                if usdt_balance > GRID_SIZE_USDT + MIN_BUFFER_USDT:
                    qty = (GRID_SIZE_USDT / bp).quantize(Decimal('0.0001'))
                    place_limit_order(symbol, 'BUY', bp, qty)
                    usdt_balance -= GRID_SIZE_USDT

            pos_qty, _ = get_position(symbol)
            if pos_qty * price >= GRID_SIZE_USDT:
                for sp in sell_prices:
                    if any(Decimal(po.price)==sp and po.side=='SELL' for po in pending_orders):
                        continue
                    sell_qty = min((GRID_SIZE_USDT / sp).quantize(Decimal('0.0001')), pos_qty)
                    if sell_qty * sp >= GRID_SIZE_USDT:
                        place_limit_order(symbol, 'SELL', sp, sell_qty)

            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.error(f"Error in grid loop for {symbol}: {e}")
            time.sleep(5)

# === WEBSOCKET HANDLER =====================================================
def handle_trade(msg):
    try:
        if msg['e'] != 'executionReport':
            return
        data = msg
        symbol = data['s']
        side = data['S']
        qty = Decimal(data['l'])
        price = Decimal(data['L'])
        order_id = data['i']
        fee = Decimal(data.get('n', '0'))

        with DBManager() as session:
            pending = session.query(PendingOrder).filter(PendingOrder.binance_order_id==str(order_id)).first()
            if pending:
                session.delete(pending)

            pos = session.query(Position).filter(Position.symbol==symbol).first()
            if not pos:
                pos = Position(symbol=symbol, quantity=qty if side=='BUY' else -qty, avg_entry_price=price)
                session.add(pos)
            else:
                if side=='BUY':
                    total_cost = pos.avg_entry_price * pos.quantity + price * qty
                    pos.quantity += qty
                    pos.avg_entry_price = total_cost / pos.quantity
                else:
                    pos.quantity -= qty
                    if pos.quantity <= 0:
                        pos.quantity = 0
                        pos.avg_entry_price = 0

            trade = TradeRecord(symbol=symbol, side=side, price=price, quantity=qty, fee=fee)
            session.add(trade)

        send_whatsapp_alert(f"{symbol} {side} {qty}@{price}")
    except Exception as e:
        logger.error(f"Error processing trade: {e}")

# === START USER STREAM =====================================================
listen_key = client.stream_get_listen_key()['listenKey']
twm.start_user_socket(callback=handle_trade)

# === PRICE WEBSOCKET =======================================================
def handle_price(msg):
    try:
        symbol = msg['s']
        price = Decimal(msg['c'])
        price_cache[symbol] = price
    except Exception as e:
        logger.error(f"Error updating price cache: {e}")

# === ORDERBOOK MONITOR =====================================================
def update_orderbook(symbol):
    while True:
        try:
            depth = client.get_order_book(symbol=symbol, limit=5)
            with cache_lock:
                orderbook_cache[symbol] = depth
            time.sleep(5)
        except Exception as e:
            logger.error(f"Error updating orderbook for {symbol}: {e}")
            time.sleep(5)

def select_top_coins():
    symbols = client.get_all_tickers()
    bids_volumes = []
    for s in symbols:
        sym = s['symbol']
        if sym in EXCLUDE_SYMBOLS or not sym.endswith('USDT'):
            continue
        try:
            depth = client.get_order_book(symbol=sym, limit=5)
            top_bid = Decimal(depth['bids'][0][1])
            if top_bid * Decimal(depth['bids'][0][0]) >= MIN_BID_VOLUME:
                bids_volumes.append((sym, top_bid))
        except:
            continue
    bids_volumes.sort(key=lambda x: x[1], reverse=True)
    top_symbols = [x[0] for x in bids_volumes[:10]]
    return top_symbols

# === PROFIT MONITOR ========================================================
def profit_monitor_loop():
    time.sleep(120)
    while True:
        total_pnl = Decimal('0')
        with DBManager() as session:
            positions = session.query(Position).all()
            for pos in positions:
                current_price = price_cache.get(pos.symbol)
                if current_price:
                    total_pnl += (current_price - pos.avg_entry_price) * pos.quantity
        send_whatsapp_alert(f"Total unrealized PnL: {total_pnl}")
        time.sleep(3600)

# === TOP COIN ROTATION =====================================================
def rotate_top_coins_loop():
    while True:
        try:
            top_symbols = select_top_coins()
            logger.info(f"Rotating top symbols: {top_symbols}")

            # Remove symbols no longer in top list
            for sym in list(active_symbols):
                if sym not in top_symbols:
                    active_symbols.remove(sym)
                    logger.info(f"Stopped trading {sym}")

            # Add new top symbols
            for sym in top_symbols:
                if sym not in active_symbols:
                    active_symbols.add(sym)
                    twm.start_symbol_ticker_socket(symbol=sym, callback=handle_price)
                    threading.Thread(target=grid_loop, args=(sym,), daemon=True).start()
                    threading.Thread(target=update_orderbook, args=(sym,), daemon=True).start()
                    logger.info(f"Started trading {sym}")
        except Exception as e:
            logger.error(f"Error rotating top coins: {e}")
        time.sleep(1200)  # 20 minutes

# === START TRADING =========================================================
def start_bot():
    initial_top = select_top_coins()
    logger.info(f"Selected symbols for trading: {initial_top}")
    for sym in initial_top:
        active_symbols.add(sym)
        twm.start_symbol_ticker_socket(symbol=sym, callback=handle_price)
        threading.Thread(target=grid_loop, args=(sym,), daemon=True).start()
        threading.Thread(target=update_orderbook, args=(sym,), daemon=True).start()

    threading.Thread(target=profit_monitor_loop, daemon=True).start()
    threading.Thread(target=rotate_top_coins_loop, daemon=True).start()

logger.info("Infinity Grid Bot starting...")
start_bot()

# Keep main thread alive
while True:
    time.sleep(60)
