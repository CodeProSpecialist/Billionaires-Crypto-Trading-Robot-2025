# 🚀 Billionaires Crypto Trading Robot 2025: Binance.US Edition 🚀
**Skyrocketing Wealth Creation with Precision Automation**  

*****
Update to the newest version 
for the best working 
trading robot python code. 
Newest Python code updates were 
completed on October 30, 2025. 
*****

( Note: delete the file named 
binance_trades.db when restarting the program if there might be any configuration changes. 
This also fixes any program startup errors. )

**Binance.US Trading Bot – Professional Version**  
*1 thread per coin • 24/7 real-time monitoring • Navy blue + yellow + green/red P&L dashboard*

---

### **Core Strategy Summary (Buy & Sell Logic)**

- **BUY Signal** → **Strong Dip Entry**  
  - **RSI ≤ 35** *(oversold)*  
  - **Trend = Bullish** *(price above Bollinger middle + MACD > signal)*  
  - **Price near 24h low** *(≤ 1.01 × 24h low)*  
  - **≥60% sell pressure** *(order book ask volume ≥60% of top 5 levels)*

- **SELL Signal** → **Take Profit + Momentum Reversal**  
  - **≥0.8% NET profit** *(after maker + taker fees)*  
  - **RSI ≥ 65** *(overbought)*  
  - **Buy pressure spike → drop**:  
    - Peak buy pressure ≥65%  
    - Current buy pressure drops to ≤55%  
    → *Confirms fading momentum after profit*

---

### **Architecture & Execution Model**

- **One dedicated thread per USDT pair**  
  → Parallel, independent 24/7 monitoring  
  → Polls every **1 second**

- **Real-time data sources**  
  - 1-minute klines (last 100) → RSI, MACD, Bollinger Bands  
  - Order book depth (top 5 bid/ask) → buy/sell pressure  
  - 24h ticker stats → volume, low/high filtering

- **Symbol filtering (on startup & retry)**  
  - Only **USDT pairs** in `TRADING` status  
  - Price: **$0.01 – $1,000**  
  - 24h volume: **≥ $100,000 USDT**  
  → Ensures liquidity & tradeability

---

### **Risk & Position Management**

- **Risk per trade**: **10% of available USDT**  
  - `available = free USDT – $2.00 (buffer)`  
  - Position size = `min(10% of available, full available)`

- **Position tracking via SQLAlchemy + SQLite**  
  - Tables: `trades`, `pending_orders`, `positions`  
  - Tracks: entry price, quantity, fees, fill time, order IDs  
  - Supports **partial fills** & **average cost recalculation**

- **Fee-aware profit calculation**  
  - Uses actual **maker/taker rates** per symbol  
  - Net P&L = Gross – (maker + taker fees)

---

### **Order Execution & Validation**

- **Limit orders only** → precise entry/exit  
- **Tick size & lot size compliance**  
  - Auto-adjusts price & quantity to exchange rules  
  - Rounds down to valid step/tick

- **Order lifecycle tracking**  
  1. Place limit order → save to `pending_orders`  
  2. Poll Binance → detect `FILLED`  
  3. Record fill → update `trades` + `positions`  
  4. Delete pending order

- **WhatsApp alerts via CallMeBot**  
  - On every **BUY** and **SELL** execution  
  - Format: `BUY BTCUSDT @ 62345.12`

---

### **Technical Indicators (TA-Lib)**

| Indicator | Settings | Purpose |
|--------|----------|-------|
| **RSI** | 14-period | Oversold (≤35) / Overbought (≥65) |
| **Bollinger Bands** | 20-period, 2σ | Middle band for trend context |
| **MACD** | 12, 26, 9 | Confirm bullish/bearish momentum |

---

### **Order Book Pressure Logic**

- Analyzes **top 5 bid/ask levels**  
- **Sell Pressure** = % of ask volume → triggers buy on panic  
- **Buy Pressure History** (deque, last 5 polls):  
  - Detects **spike (≥65%) → drop (≤55%)** → sell signal

---

### **Professional Live Dashboard (Terminal UI)**

- **Navy blue background** (`\033[48;5;17m`)  
- **Bright yellow headers** (`\033[38;5;226m`)  
- **Green/Red P&L** based on net profit  
- Updates **every 15 seconds**

#### Dashboard Fields:
| Field | Description |
|------|-------------|
| Time (CST) | Current time in Chicago timezone |
| Available USDT | Free balance (minus $2 buffer) |
| Portfolio Value | Total value of all assets in USDT |
| Active Threads | # of coin monitor threads |
| Active Positions | # of open trades |
| Per Position | Symbol, Qty, Entry, Current, RSI, **P&L%**, **Profit $**, Age, Signal |

---

### **Safety & Reliability Features**

- **Graceful shutdown** on `Ctrl+C` → stops all threads  
- **Retry logic** (`tenacity`) on API failures (3 attempts, exponential backoff)  
- **Logging**  
  - Rotating daily logs (`crypto_trading_bot.log`, 7-day retention)  
  - Console + file, with function/line info  
- **Error resilience**  
  - All critical sections in `try/except`  
  - DB transactions with rollback  
  - Thread isolation → one coin crash ≠ bot crash

---

### **Database Persistence**

- **SQLite** (`binance_trades.db`)  
- Survives restarts:  
  - Open positions reloaded  
  - Pending orders rechecked  
  - Trade history preserved

---

### **Setup Requirements**

```bash
# Environment Variables
export BINANCE_API_KEY="..."
export BINANCE_API_SECRET="..."
export CALLMEBOT_API_KEY="..."   # optional
export CALLMEBOT_PHONE="..."     # optional
```

```bash
pip install python-binance sqlalchemy talib tenacity requests numpy
```

---

### **Summary: How It Works (Step-by-Step)**

1. **Start** → Load API keys, init DB, fetch valid USDT pairs  
2. **Spawn 1 thread per coin** → each runs forever  
3. **Every 1 second**:  
   - Pull klines → compute RSI, MACD, BB, trend  
   - Pull order book → compute bid/ask pressure  
   - Check 24h low & volume  
4. **If BUY signal** → place limit buy @ best bid (adjusted)  
5. **If SELL signal** → place limit sell @ next tick above ask  
6. **Main loop (every 15s)**:  
   - Check filled orders → update DB  
   - Print **live dashboard** with P&L  
7. **Repeat 24/7** → fully automated

---

**Professional. Fast. Precise. Fee-Aware. Visual.**  
*Built for serious traders who want automation without compromise.*

Unleash the power of automated crypto trading with the Billionaires Crypto Trading Robot 2025, a cutting-edge bot crafted for Binance.US. Target 0.8% net profits per trade on USDT pairs using advanced mean-reversion with comprehensive momentum, oscillator, and trend filters. **Not affiliated with Binance.US or CallMeBot.** **Profits are not guaranteed; you risk losing all or part of your investment.** Always test in simulation mode (e.g., Binance.US testnet) before live trading and proceed at your own risk!

### Legal Disclaimers

**Important Notice: This information is provided as of October 28, 2025, and cryptocurrency markets, regulations, and technologies evolve rapidly. Always verify the latest information and consult professionals before using this bot.**

- **No Affiliation or Endorsement**: This trading bot is not affiliated with, endorsed by, or sponsored by Binance.US or CallMeBot. All interactions with these platforms are at the user's sole discretion and risk, subject to their respective terms of service.
- **Not Financial, Investment, or Legal Advice**: This bot and its description are for informational and educational purposes only. They do not constitute financial, investment, tax, legal, or professional advice. The author is not a financial advisor, broker, or registered investment advisor. All trading decisions are your sole responsibility. Seek independent advice from qualified professionals to assess suitability for your circumstances.
- **High Risk of Loss**: Cryptocurrency trading involves substantial risks, including the potential for **complete or partial loss of your investment**. Prices are highly volatile, subject to rapid and unpredictable changes due to market sentiment, regulatory news, technological issues, or external factors. Past performance does not guarantee future results. You could lose more than your initial investment.
- **No Guarantee of Profits**: **Profits are not guaranteed**. There is no assurance that this bot will generate profits or avoid losses. Trading strategies, including mean-reversion with momentum, oscillator, and trend filters, may fail in certain market conditions (e.g., high volatility, low liquidity, or black swan events). The 0.8% profit target is a goal, not a guarantee, and losses may occur due to fees, slippage, or unfavorable price movements.
- **Legal and Regulatory Compliance**: Cryptocurrency trading is subject to U.S. laws, including oversight by the SEC (for securities-like assets) and CFTC (for commodities). As of October 2025, the CLARITY Act and FIT21 provide clearer jurisdiction, but users must ensure compliance with AML/KYC requirements under the Bank Secrecy Act (BSA), sanctions screening, and state licensing (e.g., New York's BitLicense). Do not use this bot if prohibited in your jurisdiction. On October 17, 2025, the NFA eliminated certain disclosure guidance for digital assets but still requires disclosing material risks. Users are responsible for tax reporting (e.g., IRS Form 1099-DA for gains/losses) and any violations could result in penalties.
- **No Liability**: The author disclaims all liability for any direct, indirect, consequential, or special losses arising from using this bot, including trading losses, data inaccuracies, third-party service failures (e.g., Binance.US API, CallMeBot), or security breaches. You are responsible for securing API keys and accounts. The bot interacts with third-party services; any downtime, errors, or changes in their terms are beyond control.
- **Software Risks and Warranty Disclaimer**: The bot is provided "as is" without warranties of merchantability, fitness for purpose, or non-infringement. It may contain bugs, and users assume risks from technical failures, incorrect configurations, or unauthorized access. Test thoroughly in a simulation environment before live trading.
- **User Responsibilities**: You must comply with Binance.US and CallMeBot terms, maintain account security, and monitor trades. Stop using the bot if it violates any laws or platform rules. This bot does not provide custody services; all assets remain under your control on Binance.US.

By using this bot, you acknowledge these risks and agree to indemnify the author against any claims.


### Installation in Ubuntu 24.04 Linux (Anaconda Python Environment Preferred)

For optimal isolation and management of dependencies, use Anaconda. This setup ensures a controlled Python environment, avoiding conflicts with system Python.

#### Step-by-Step Anaconda Installation
1. **Download the Anaconda Installer**:  
   Open a terminal and download the latest Linux installer (as of October 2025, check for updates at https://www.anaconda.com/download):  
   ```bash
   wget https://repo.anaconda.com/archive/Anaconda3-2024.10-1-Linux-x86_64.sh
   ```

2. **Run the Installer**:  
   Execute the script:  
   ```bash
   bash Anaconda3-2024.10-1-Linux-x86_64.sh
   ```  
   - Press `Enter` to review the license.  
   - Scroll with `Space` and type `yes` to accept.  
   - Confirm the default path (e.g., `/home/yourusername/anaconda3`) by pressing `Enter`.  
   - Type `yes` to initialize Conda (this adds setup to `.bashrc`).

3. **Reload Shell Configuration**:  
   Apply changes:  
   ```bash
   source ~/.bashrc
   ```  
   Your terminal should show `(base)` indicating the base environment is active.

4. **Create a New Conda Environment**:  
   Create an isolated environment for the bot (e.g., with Python 3.11):  
   ```bash
   conda create -n trading_bot python=3.11
   ```  
   Activate it:  
   ```bash
   conda activate trading_bot
   ```  
   (To deactivate later: `conda deactivate`.)

5. **Install Dependencies**:  
   Inside the activated environment:  
   ```bash
   pip install python-binance ta-lib numpy pandas sqlalchemy requests tenacity
   ```  
   - Note: If `ta-lib` fails, install the system library first:  
     ### Simplified TA-Lib Installation (Version 0.6.4) on Ubuntu 24.04

To install the latest TA-Lib (v0.6.4) on Ubuntu 24.04:

1. **Install Build Tools**:
   ```bash
   sudo apt update
   sudo apt install -y build-essential wget autoconf automake libtool pkg-config python3-dev python3-pip
   ```

2. **Download and Build TA-Lib**:
   ```bash
   wget https://github.com/TA-Lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz
   tar -xzf ta-lib-0.6.4-src.tar.gz
   cd ta-lib-0.6.4
   ./autogen.sh
   ./configure --prefix=/usr
   make -j$(nproc)
   sudo make install
   sudo ldconfig
   ```

3. **Install Python TA-Lib (v0.6.8)**:
   ```bash
   pip3 install TA-Lib==0.6.8
   ```

4. **Verify Installation**:
   ```bash
   pkg-config --modversion ta-lib  # Should show "0.6.4"
   python3 -c "import talib; print(talib.__version__)"  # Should show "0.6.8"
   ```

### Notes
- If `pip3 install` fails, try:
  ```bash
  TA_INCLUDE_PATH=/usr/include TA_LIBRARY_PATH=/usr/lib pip3 install TA-Lib==0.6.8
  ```
- If errors occur during `./configure` or `make`, ensure all dependencies are installed or run `autoreconf -fiv` before `./configure`.

Let me know if you hit any issues!

6. **Verify Installation**:  
   Run `conda list` to check packages. Test Python:  
   ```bash
   python -c "print('Hello, Anaconda!')"
   ```

#### What to Add to `.bashrc` in Ubuntu Linux
The Anaconda installer automatically adds Conda initialization to `~/.bashrc` (e.g., export PATH and conda setup block). For the bot's environment variables (used via `os.getenv`), add these exports at the end of `~/.bashrc` (replace placeholders with your actual values):

```bash
# Environment variables for Billionaires Crypto Trading Robot
export BINANCE_API_KEY="your_binance_api_key_here"
export BINANCE_API_SECRET="your_binance_api_secret_here"
export CALLMEBOT_API_KEY="your_callmebot_api_key_here"
export CALLMEBOT_PHONE="your_phone_number_here"  # E.g., +1234567890
```

Run Program: 

```bash
conda activate 
python3 2025-coin-trading-bot.py
```

**Note**: Ensure the bot script is saved as `2025-coin-trading-bot.py`. Monitor logs in `crypto_trading_bot.log` for issues and the console dashboard for status. This setup is for educational use; live trading carries significant risks, including the potential to **lose all or part of your investment**, as outlined in the disclaimers. 

To set up CallMeBot for WhatsApp, visit https://www.callmebot.com/ and follow the instructions to get your API key and configure the service. Then, use `curl` or Python to send messages via the API.

This setup is for educational use; live trading carries significant risks, including the potential to **lose all or part of your investment**, as outlined in the disclaimers. 

The bot is not affiliated with or endorsed by Binance.US or CallMeBot. Always test in a simulation environment (e.g., Binance.US testnet) before deploying with real funds.
