# 🚀 24 HOUR INFINITY GRID BOT 
      for Binance.US Edition 🚀
**Skyrocketing Wealth Creation with Precision Automation**  

*****
Update to the newest version 
for the best working 
trading robot python code. 
Newest Python code 
updates were on 11-8-2025. 

*****

( Note: delete the file named 
binance_trades.db when restarting the program if there might be any configuration changes. 
This also fixes any program startup errors. )


> **Advanced Crypto Trading Automation for Binance.US – Fully Threaded, Real-Time Order Book Intelligence, and Institutional-Grade Risk Controls**

---

## Features at a Glance

| Feature | Description |
|--------|-------------|

| **Professional Dashboard** | Real-time P&L, positions, market overview, and buy signals |
| **SQLite Trade & Position DB** | auto-import of existing holdings |
| **WhatsApp Alerts** | Instant execution & event notifications via CallMeBot |
| **Dynamic Trailing Logic** | Buy on dips, sell on peaks — with stall detection & flash move triggers |

---

## Why This Bot Stands Out

```text
Most bots trade blindly. This one *watches the market like a hawk*.
	•	Order Book Pressure Spikes → Detects panic selling or FOMO buying
	•	Rapid Price Moves (1.5% in <5s) → Instant market entry/exit
	
	•	0.8% Net Profit Target → Disciplined exits after fees


Safety & Compliance
	•	Binance.US Only (tld=‘us’)
	•	No leverage, no futures
	•	Rate limits respected (weight + order count)
	•	All trades logged to SQLite
	•	No external dependencies beyond Binance

Get It Now
Ready-to-run. No config files. Just set API keys and go.

Built for traders who demand precision. Powered by real-time depth, not hype.


```bash
# 1. Install

pip3 install python-binance rich sqlalchemy pytz numpy TA-Lib requests

# 2. Set env
export BINANCE_API_KEY="your_key"
export BINANCE_API_SECRET="your_secret"
export CALLMEBOT_API_KEY="123456"
export CALLMEBOT_PHONE="15551234567"

# 3. Run
python3 2025-coin-trading-bot.py
```
 
**NO SUBSCRIPTION • NO CLOUD • FULL CONTROL**

---

**BILLIONAIRES CRYPTO TRADING ROBOT 2025**  
*Built for speed. Built for profit. Built for you.*

--- 

**Tagline:**  
> **"Trade like a billionaire."**

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
2. **Spawn 1 thread for buy orders and 1 thread for sell orders. ** → each runs forever  
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

Unleash the power of automated crypto trading with the SMART COIN TRADING BOT, a cutting-edge bot crafted for Binance.US. Target 0.8% net profits per trade on USDT pairs using advanced mean-reversion with comprehensive momentum, oscillator, and trend filters. **Not affiliated with Binance.US or CallMeBot.** **Profits are not guaranteed; you risk losing all or part of your investment.** Always test in simulation mode (e.g., Binance.US testnet) before live trading and proceed at your own risk!

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
********
Step-by-Step Guide to Create API Keys

Log In to Your Binance.US Account:

Go to www.binance.us and sign in with your credentials.


Access API Management:

Hover over your profile icon or email address in the upper right corner of the dashboard.
From the dropdown menu, select API Management (or Settings > API Management).


Create a New API Key:

In the API Management section, enter a descriptive name for your key (e.g., "Trading Bot Key").
Click Create API (or Create).
Complete 2FA verification (e.g., Google Authenticator or SMS code).
Confirm via email verification from Binance.US.


Copy Your Keys:

Your API Key and Secret Key will be displayed once—copy and save them securely (e.g., in a password manager).
The Secret Key will be hidden forever after you leave the page. If lost, delete the key and create a new one.


Configure Permissions (API Restrictions):

Defaults to Read-Only—ideal for data access (e.g., portfolio tracking).
For trading bots: Enable Spot Trading if needed (allows buying/selling), but NEVER enable Withdrawals unless absolutely required, as it risks fund theft.
IP Restrictions: Add trusted IP addresses (e.g., your server's IP) to limit access. Leave unrestricted only if necessary.
Click Save or Confirm to apply.
********

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
python3 24-hour-infinity-grid-bot.py
```

**Note**: Ensure the bot script is saved as `2025-coin-trading-bot.py`. Monitor logs in `crypto_trading_bot.log` for issues and the console dashboard for status. This setup is for educational use; live trading carries significant risks, including the potential to **lose all or part of your investment**, as outlined in the disclaimers. 

To set up CallMeBot for WhatsApp, visit https://www.callmebot.com/ and follow the instructions to get your API key and configure the service. Then, use `curl` or Python to send messages via the API.

This setup is for educational use; live trading carries significant risks, including the potential to **lose all or part of your investment**, as outlined in the disclaimers. 

The bot is not affiliated with or endorsed by Binance.US or CallMeBot. Always test in a simulation environment (e.g., Binance.US testnet) before deploying with real funds.
