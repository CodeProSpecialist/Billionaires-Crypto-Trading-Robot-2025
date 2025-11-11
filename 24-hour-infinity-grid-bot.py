#!/usr/bin/env python3
"""
INFINITY GRID BOT – 24/7 PRICE FOLLOWING (BINANCE.US)
Fully WebSocket-driven with database, alerts, and infinity grid logic.
"""

import os, sys, time, threading, logging, requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from collections import deque
import pytz
import numpy as np
import talib
from binance import Client, ThreadedWebsocketManager
from binance.exceptions import BinanceAPIException
from sqlalchemy import create_engine, Column, Integer, String, Numeric
from sqlalchemy.orm import declarative_base, sessionmaker

# === CONFIG ================================================================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

if not API_KEY or not API_SECRET:
    print("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)

GRID_SIZE_USDT = Decimal('5.0')
GRID_INTERVAL_PCT = Decimal('0.015')
MIN_BUFFER_USDT = Decimal('8.0')
MAX_GRIDS_PER_SIDE = 8
MIN_GRIDS_PER_SIDE = 2
MIN_GRIDS_FALLBACK = 1
REBALANCE_THRESHOLD_PCT = Decimal('0.0075')
POLL_INTERVAL = 45.0
LOG_FILE = "infinity_grid_bot.log"
CST_TZ = pytz.timezone('America/Chicago')

# === LOGGING ===============================================================
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s:%(message)s'))
    logger.addHandler(fh)
    logger.addHandler(ch)

# === GLOBALS ===============================================================
valid_symbols_dict = {}
active_grid_symbols = {}
first_dashboard_run = True
live_order_books = {}
live_prices = {}
klines_cache = {}

# === DATABASE ==============================================================
DB_URL = "sqlite:///binance_trades.db"
engine = create_engine(DB_URL, echo=False, future=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    quantity = Column(Numeric(20,8), nullable=False)
    avg_entry_price = Column(Numeric(20,8), nullable=False)
    buy_fee_rate = Column(Numeric(10,6), nullable=False, default=0.001)

class PendingOrder(Base):
    __tablename__ = "pending_orders"
    id = Column(Integer, primary_key=True)
    binance_order_id = Column(String(64), unique=True, nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(20,8), nullable=False)
    quantity = Column(Numeric(20,8), nullable=False)

if not os.path.exists("binance_trades.db"):
    Base.metadata.create_all(engine)

class DBManager:
    def __enter__(self):
        self.session = SessionFactory()
        return self.session
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type: self.session.rollback()
        else:
            try: self.session.commit()
            except: self.session.rollback()
        self.session.close()

# === HELPERS ===============================================================
def to_decimal(v):
    try: return Decimal(str(v)).quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    except: return Decimal('0')

def buy_notional_ok(price, qty): return price*qty >= Decimal('1.25')
def sell_notional_ok(price, qty): return price*qty >= Decimal('3.25')
def now_cst(): return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S")
def send_whatsapp_alert(msg):
    if CALLMEBOT_API_KEY and CALLMEBOT_PHONE:
        try: requests.get(f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(msg)}&apikey={CALLMEBOT_API_KEY}", timeout=5)
        except: pass

def retry_custom(func):
    def wrapper(*args, **kwargs):
        for i in range(5):
            try: return func(*args, **kwargs)
            except BinanceAPIException as e:
                delay=2**i
                logger.warning(f"Retry {i+1}/5 for {func.__name__}: {e}")
                time.sleep(delay)
        return None
    return wrapper

# === WEBSOCKET HANDLERS ===================================================
def handle_depth_update(msg):
    symbol = msg['s']
    bids = msg.get('b',[]); asks = msg.get('a',[])
    if not bids or not asks: return
    live_order_books[symbol] = {'best_bid':to_decimal(bids[0][0]),
                                'best_ask':to_decimal(asks[0][0]),
                                'raw_bids':[(to_decimal(p),to_decimal(q)) for p,q in bids],
                                'raw_asks':[(to_decimal(p),to_decimal(q)) for p,q in asks],
                                'ts': time.time()}

def handle_ticker(msg): live_prices[msg['s']] = to_decimal(msg['c'])
def handle_kline(msg):
    symbol = msg['s']; k = msg['k']
    if symbol not in klines_cache: klines_cache[symbol] = deque(maxlen=15)
    klines_cache[symbol].append(float(k['c']))

# === BINANCE.US BOT ========================================================
class BinanceTradingBot:
    def __init__(self):
        self.client = Client(API_KEY, API_SECRET, tld='us')
        self.api_lock = threading.Lock()
        self.state_lock = threading.Lock()
        global valid_symbols_dict
        info=self.client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset']=='USDT' and s['status']=='TRADING': valid_symbols_dict[s['symbol']]={'volume':1e6}
        self.twm=ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
        self.twm.start()
        for sym in valid_symbols_dict.keys():
            self.twm.start_depth_socket(callback=handle_depth_update,symbol=sym)
            self.twm.start_symbol_ticker_socket(callback=handle_ticker,symbol=sym)
            self.twm.start_kline_socket(callback=handle_kline,symbol=sym,interval='1m')
        self.sync_positions_from_binance()

    @retry_custom
    def get_order_book_analysis(self,symbol,force_refresh=False):
        ob = live_order_books.get(symbol)
        if ob and not force_refresh: return ob
        with self.api_lock:
            depth=self.client.get_order_book(symbol=symbol,limit=50)
        bids=depth.get('bids',[]); asks=depth.get('asks',[])
        result={'best_bid':to_decimal(bids[0][0]) if bids else Decimal('0'),
                'best_ask':to_decimal(asks[0][0]) if asks else Decimal('0'),
                'raw_bids':[(to_decimal(p),to_decimal(q)) for p,q in bids[:50]],
                'raw_asks':[(to_decimal(p),to_decimal(q)) for p,q in asks[:50]],
                'ts':time.time()}
        live_order_books[symbol] = result
        return result

    @retry_custom
    def get_price_usdt(self,asset):
        if asset=='USDT': return Decimal('1')
        sym = asset+'USDT'
        price = live_prices.get(sym)
        if price: return price
        with self.api_lock:
            ticker=self.client.get_symbol_ticker(symbol=sym)
        price = to_decimal(ticker['price'])
        live_prices[sym] = price
        return price

    @retry_custom
    def get_atr(self,symbol):
        closes=klines_cache.get(symbol)
        if closes and len(closes)>=14:
            arr=np.array(closes)
            atr=talib.ATR(arr,arr,arr,timeperiod=14)[-1]
            return float(atr)
        with self.api_lock:
            klines=self.client.get_klines(symbol=symbol,interval='1m',limit=15)
        highs=np.array([float(k[2]) for k in klines])
        lows=np.array([float(k[3]) for k in klines])
        closes=np.array([float(k[4]) for k in klines])
        atr=talib.ATR(highs,lows,closes,timeperiod=14)[-1]
        return float(atr)

    def get_balance(self):
        with self.api_lock: acct=self.client.get_account()
        for b in acct['balances']:
            if b['asset']=='USDT': return to_decimal(b['free'])
        return Decimal('0')

    def get_asset_balance(self,asset):
        with self.api_lock: acct=self.client.get_account()
        for b in acct['balances']:
            if b['asset']==asset: return to_decimal(b['free'])
        return Decimal('0')

    def sync_positions_from_binance(self):
        with self.api_lock: acct=self.client.get_account()
        with DBManager() as sess:
            sess.query(Position).delete()
            for b in acct['balances']:
                asset=b['asset']; qty=to_decimal(b['free'])
                if qty<=0 or asset in {'USDT','USDC'}: continue
                sym=asset+'USDT'; price=self.get_price_usdt(asset)
                if price<=0: continue
                sess.add(Position(symbol=sym,quantity=qty,avg_entry_price=price,buy_fee_rate=Decimal('0.001')))
            sess.commit()

    @retry_custom
    def place_limit_buy_with_tracking(self,symbol,price,qty):
        with self.api_lock: order=self.client.order_limit_buy(symbol=symbol,quantity=qty,price=price)
        with DBManager() as sess:
            sess.add(PendingOrder(binance_order_id=str(order['orderId']),symbol=symbol,side='buy',price=to_decimal(price),quantity=to_decimal(qty)))
        return order

    @retry_custom
    def place_limit_sell_with_tracking(self,symbol,price,qty):
        with self.api_lock: order=self.client.order_limit_sell(symbol=symbol,quantity=qty,price=price)
        with DBManager() as sess:
            sess.add(PendingOrder(binance_order_id=str(order['orderId']),symbol=symbol,side='sell',price=to_decimal(price),quantity=to_decimal(qty)))
        return order

    def cancel_order_safe(self,symbol,order_id):
        try:
            with self.api_lock: self.client.cancel_order(symbol=symbol,orderId=order_id)
        except: pass

    def check_and_process_filled_orders(self):
        with DBManager() as sess:
            for po in sess.query(PendingOrder).all():
                try:
                    with self.api_lock: o=self.client.get_order(symbol=po.symbol,orderId=int(po.binance_order_id))
                    if o['status']=='FILLED': 
                        sess.delete(po)
                        send_whatsapp_alert(f"{po.side.upper()} {po.symbol} @ {o['price']}")
                except: pass

# === INFINITY GRID FUNCTIONS ===================================================
def calculate_optimal_grids(bot):
    usdt_free = bot.get_balance() - MIN_BUFFER_USDT
    if usdt_free <= 0: return {}
    with DBManager() as sess:
        owned = sess.query(Position).all()
    if not owned: return {}
    scores=[]
    for pos in owned:
        sym=pos.symbol
        ob=bot.get_order_book_analysis(sym)
        cur_price=(ob['best_bid']+ob['best_ask'])/2
        if cur_price<=0: continue
        entry=Decimal(pos.avg_entry_price)
        qty=Decimal(pos.quantity)
        unrealized_pnl=(cur_price-entry)*qty
        atr=bot.get_atr(sym)
        volatility_score=atr/float(cur_price) if atr>0 else 0
        volume_score=min(valid_symbols_dict.get(sym,{}).get('volume',0)/1e6,5.0)
        pnl_score=max(float(unrealized_pnl)/10.0,-2.0)
        total_score=volatility_score*2.0+volume_score+max(pnl_score,0)
        scores.append((sym,total_score,float(qty),float(cur_price)))
    if not scores: return {}
    total_score=sum(s[1] for s in scores) or 1
    allocations={s[0]:(s[1]/total_score)*float(usdt_free) for s in scores}
    result={}
    for sym,alloc in allocations.items():
        max_possible=int(alloc//float(GRID_SIZE_USDT))
        levels=min(MAX_GRIDS_PER_SIDE,max(MIN_GRIDS_FALLBACK,max_possible//2))
        levels=max(MIN_GRIDS_PER_SIDE if alloc>=40 else MIN_GRIDS_FALLBACK,levels)
        result[sym]=(levels,levels)
    return result

def rebalance_infinity_grid(bot,symbol):
    ob=bot.get_order_book_analysis(symbol,force_refresh=True)
    current_price=(ob['best_bid']+ob['best_ask'])/2
    if current_price<=0: return
    grid=active_grid_symbols.get(symbol,{})
    old_center=Decimal(str(grid.get('center',current_price)))
    price_move=abs(current_price-old_center)/old_center if old_center>0 else 1
    if symbol not in active_grid_symbols or price_move>=REBALANCE_THRESHOLD_PCT:
        for oid in grid.get('buy_orders',[])+grid.get('sell_orders',[]):
            bot.cancel_order_safe(symbol,oid)
        active_grid_symbols.pop(symbol,None)
        buy_levels,sell_levels=calculate_optimal_grids(bot).get(symbol,(0,0))
        if buy_levels==0: return
        info=bot.client.get_symbol_info(symbol)
        lot=next(f for f in info['filters'] if f['filterType']=='LOT_SIZE')
        step=Decimal(lot['stepSize'])
        qty_per_grid=(GRID_SIZE_USDT/current_price)//step*step
        if qty_per_grid<=0: return
        new_grid={'center':current_price,'qty':float(qty_per_grid),'buy_orders':[],'sell_orders':[],'last_price':float(current_price),'placed_at':time.time()}
        tick=Decimal(next(f['tickSize'] for f in info['filters'] if f['filterType']=='PRICE_FILTER'))
        for i in range(1,buy_levels+1):
            price=(current_price*(1-GRID_INTERVAL_PCT*i)//tick)*tick
            if buy_notional_ok(price,qty_per_grid):
                order=bot.place_limit_buy_with_tracking(symbol,str(price),float(qty_per_grid))
                if order: new_grid['buy_orders'].append(str(order['orderId']))
        base_asset=symbol.replace('USDT','')
        free=bot.get_asset_balance(base_asset)
        for i in range(1,sell_levels+1):
            price=(current_price*(1+GRID_INTERVAL_PCT*i)//tick)*tick
            if free>=qty_per_grid and sell_notional_ok(price,qty_per_grid):
                order=bot.place_limit_sell_with_tracking(symbol,str(price),float(qty_per_grid))
                if order: new_grid['sell_orders'].append(str(order['orderId']))
                free-=qty_per_grid
        if new_grid['buy_orders'] or new_grid['sell_orders']: active_grid_symbols[symbol]=new_grid
        logger.info(f"INFINITY REGRID: {symbol} | Center: ${float(current_price):.6f} | {buy_levels}B/{sell_levels}S")

def print_dashboard(bot):
    global first_dashboard_run
    os.system('cls' if os.name=='nt' else 'clear')
    print("="*120)
    print(f"{'INFINITY GRID BOT – 24/7 PRICE FOLLOWING':^120}")
    print("="*120)
    print(f"Time: {now_cst()} | USDT: ${float(bot.get_balance()):,.2f}")
    with DBManager() as sess: print(f"Positions: {sess.query(Position).count()} | Active Grids: {len(active_grid_symbols)}")
    print("\nGRID STATUS (Center Price | B/S Count)")
    with DBManager() as sess:
        for pos in sess.query(Position).all():
            sym=pos.symbol
            grid=active_grid_symbols.get(sym,{})
            center=grid.get('center',0)
            b=len(grid.get('buy_orders',[]))
            s=len(grid.get('sell_orders',[]))
            print(f"{sym:<10} | Center: ${float(center):>10.6f} | Grid: {b}B/{s}S")
    first_dashboard_run=False
    print("="*120)

def cancel_all_grids(bot):
    for sym,state in list(active_grid_symbols.items()):
        for oid in state.get('buy_orders',[])+state.get('sell_orders',[]): bot.cancel_order_safe(sym,oid)
    active_grid_symbols.clear()

# === MAIN LOOP =============================================================
def main():
    bot=BinanceTradingBot()
    last_update=0; last_dashboard=0
    while True:
        try:
            bot.check_and_process_filled_orders()
            now_time=time.time()
            if now_time-last_update>=POLL_INTERVAL:
                with DBManager() as sess:
                    for pos in sess.query(Position).all():
                        if pos.symbol in valid_symbols_dict:
                            rebalance_infinity_grid(bot,pos.symbol)
                last_update=now_time
            if now_time-last_dashboard>=60:
                print_dashboard(bot)
                last_dashboard=now_time
            time.sleep(1)
        except KeyboardInterrupt:
            cancel_all_grids(bot)
            print("\nInfinity Grid stopped.")
            break
        except Exception as e:
            logger.critical(f"Critical error: {e}")
            time.sleep(10)

if __name__=="__main__":
    main()
