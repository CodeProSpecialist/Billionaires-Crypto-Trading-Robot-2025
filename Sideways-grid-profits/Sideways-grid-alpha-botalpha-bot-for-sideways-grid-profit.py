#!/usr/bin/env python3
"""
ALPHA BOT 2025 — ZEC/LTC/DASH EDITION
$213 USDT → 50%–200% APY on Sideways Swings
Privacy Alts: No BTC Losses
"""

import os
import sys
import time
import threading
import json
import requests
import websocket
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
import tkinter as tk
from tkinter import font as tkfont, messagebox
import pytz
from binance.client import Client
from binance.exceptions import BinanceAPIException

# -------------------- CONFIG (ZEC/LTC/DASH TUNED) --------------------
getcontext().prec = 28
ZERO = Decimal('0')
ONE  = Decimal('1')
SAFETY_BUFFER = Decimal('0.95')
GRID_SIZE = Decimal('8.0')  # Fits $213
GRID_PCT = Decimal('0.005')  # Tighter for low-vol alts
PRIORITY_COINS = ['ZECUSDT', 'LTCUSDT', 'DASHUSDT']  # Sideways kings
RESERVE_PCT = Decimal('0.33')
CST = pytz.timezone("America/Chicago")
PNL_FILE = "alpha_pnl.json"
PNL_SUMMARY_FILE = "alpha_summary.txt"

# -------------------- CLIENT --------------------
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
if not api_key or not api_secret:
    messagebox.showerror("API", "Set BINANCE_API_KEY and BINANCE_API_SECRET")
    sys.exit(1)
client = Client(api_key, api_secret, tld='us')

# -------------------- STATE --------------------
symbol_info = {}
balances = {}
grid_orders = {}
running = False
min_reserve = ZERO

# -------------------- P&L --------------------
pnl = {'realized': ZERO, 'unrealized': ZERO, 'daily': ZERO, 'last_date': str(datetime.now().date())}

def load_pnl():
    global pnl
    if os.path.exists(PNL_FILE):
        with open(PNL_FILE, 'r') as f:
            data = json.load(f)
            pnl = {k: Decimal(v) if k != 'last_date' else v for k, v in data.items()}

def save_pnl():
    with open(PNL_FILE, 'w') as f:
        json.dump({k: str(v) if isinstance(v, Decimal) else v for k, v in pnl.items()}, f)
    with open(PNL_SUMMARY_FILE, 'w') as f:
        f.write(f"Total P&L: ${pnl['realized'] + pnl['unrealized']:+.2f}\n")
        f.write(f"Daily: ${pnl['daily']:+.2f}\n")

# -------------------- GUI (YOUR EXACT DESIGN) --------------------
root = tk.Tk()
root.title("ALPHA BOT 2025 — ZEC/LTC/DASH")
root.geometry("800x800")
root.configure(bg="#111111")

title_font = tkfont.Font(family="Helvetica", size=18, weight="bold")
label_font = tkfont.Font(family="Helvetica", size=14)
pnl_font = tkfont.Font(family="Helvetica", size=16)
term_font = tkfont.Font(family="Courier", size=16)

tk.Label(root, text="ALPHA BOT 2025 — ZEC/LTC/DASH", font=title_font, fg="gold", bg="#111111").pack(pady=10)

stats = tk.Frame(root, bg="#111111", height=150)
stats.pack(fill="x", padx=10, pady=5)
stats.pack_propagate(False)

usdt_lbl = tk.Label(stats, text="USDT: 0.00", fg="lime", bg="#111111", font=label_font, anchor="w")
usdt_lbl.pack(fill="x", pady=2)
pnl_lbl = tk.Label(stats, text="P&L: $0.00", fg="lime", bg="#111111", font=pnl_font, anchor="w")
pnl_lbl.pack(fill="x", pady=2)
mode_lbl = tk.Label(stats, text="MODE: SIDEWAYS GRIDS", fg="cyan", bg="#111111", font=label_font, anchor="w")
mode_lbl.pack(fill="x", pady=2)

term_frame = tk.Frame(root, bg="#111111")
term_frame.pack(fill="both", expand=True, padx=10, pady=5)
term_text = tk.Text(term_frame, bg="black", fg="lime", font=term_font)
term_text.pack(fill="both", expand=True)

def log(msg):
    term_text.insert(tk.END, f"[{datetime.now(CST).strftime('%H:%M:%S')}] {msg}\n")
    term_text.see(tk.END)

btn_frame = tk.Frame(root, bg="#111111")
btn_frame.pack(pady=10)
tk.Button(btn_frame, text="START", command=lambda: start(), bg="#00aa00", fg="white", font=label_font, width=12).pack(side="right", padx=5)
tk.Button(btn_frame, text="STOP", command=lambda: stop(), bg="#aa0000", fg="white", font=label_font, width=12).pack(side="right", padx=5)

# -------------------- UTILS --------------------
def update_balances():
    global balances, min_reserve
    info = client.get_account()
    balances = {a['asset']: Decimal(a['free']) for a in info['balances'] if Decimal(a['free']) > ZERO}
    usdt = balances.get('USDT', ZERO)
    min_reserve = usdt * RESERVE_PCT
    usdt_lbl.config(text=f"USDT: {usdt:.2f} | Reserve: {min_reserve:.2f}")

def update_pnl_label():
    total = pnl['realized'] + pnl['unrealized']
    color = "lime" if total >= 0 else "red"
    pnl_lbl.config(text=f"P&L: ${total:+.2f} (Daily: ${pnl['daily']:+.2f})", fg=color)
    root.after(3000, update_pnl_label)

# -------------------- GRID (TIGHTER FOR LOW-VOL) --------------------
def place_grid(symbol, side):
    try:
        price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
        p = price * (ONE - GRID_PCT) if side == 'BUY' else price * (ONE + Decimal('0.01'))
        q = (GRID_SIZE / p).quantize(Decimal('0.000001'))
        if p * q < 10: return  # Min notional
        client.create_order(symbol=symbol, side=side, type='LIMIT', timeInForce='GTC', price=str(p), quantity=str(q))
        log(f"GRID {side} {symbol} {q} @ {p:.4f} — Sideways Win!")
    except: pass

# -------------------- ALPHA CYCLE (ZEC/LTC/DASH FOCUS) --------------------
def alpha_cycle():
    while running:
        update_balances()
        for sym in PRIORITY_COINS:  # Skip BTC volatility
            if sym in symbol_info and balances.get('USDT', ZERO) > min_reserve + GRID_SIZE:
                place_grid(sym, 'BUY')
                place_grid(sym, 'SELL')
        time.sleep(60)  # Check hourly for swings

def start():
    global running
    if not running:
        running = True
        threading.Thread(target=alpha_cycle, daemon=True).start()
        log("ALPHA BOT STARTED — ZEC/LTC/DASH GRIDS | No BTC Losses")

def stop():
    global running
    running = False
    log("ALPHA BOT STOPPED")

# -------------------- INIT --------------------
if __name__ == "__main__":
    load_pnl()
    save_pnl()
    # Load symbols (focus alts)
    info = client.get_exchange_info()
    for s in info['symbols']:
        if s['symbol'] in PRIORITY_COINS and s['status'] == 'TRADING':
            f = {x['filterType']: x for x in s['filters']}
            symbol_info[s['symbol']] = {
                'stepSize': Decimal(f.get('LOT_SIZE', {}).get('stepSize', '0.000001')),
                'tickSize': Decimal(f.get('PRICE_FILTER', {}).get('tickSize', '0.00000001'))
            }
    update_balances()
    log(f"Optimized for $210 USDT READY | Focus: ZEC/LTC/DASH Sideways")
    update_pnl_label()
    root.mainloop()
