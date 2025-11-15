# Infinity Grid Bot 2025 — Binance.US Automated Trading Bot

## Overview
**Infinity Grid Bot 2025** is a fully automated cryptocurrency trading bot for **Binance.US**.  
It combines **dynamic grid trading**, **fee-aware limit orders**, and **top market coin selection** to automate buying and selling while maintaining a target net profit.  

The bot features:  

- Automatic fetching of top coins by **market cap**, **trading volume**, and **recent price gains** (14-day and 5-day performance) from **CoinGecko** and **Coinbase**.
- Avoids **stablecoins**, **Bitcoin**, **Ethereum**, **Solana**, and other excluded coins.
- Fully automated **limit buy/sell orders** with **fee calculation** to guarantee net profit ≥ 1.8%.
- **Grid rebalancing** triggered immediately upon order fills.
- **USDT reserve management**: 33% reserved for future buys.
- **Customizable Tkinter GUI** with **scrollable live terminal** and **status monitoring**.

---

## Features

### Grid Trading
- Places limit **buy/sell grids** on owned positions.
- Automatically **cancels old grid orders** before creating new ones.
- Dynamic **sell percentage** calculation based on **maker/taker fees**.

### Top Coin Selection
- Fetches top 25 coins from **CoinGecko** and **Coinbase**:
  - Sorted by **market cap**
  - Sorted by **24h volume**
  - Sorted by **price growth over last 5 and 14 days**
- Filters to exclude stablecoins, BTC, ETH, BNB, SOL, etc.

### Risk Management
- Maintains **33% of USDT** for additional purchases.
- Calculates required sell price to ensure net profit after fees ≥ 1.8%.
- Applies **safety buffer** to avoid overspending USDT.

### User Interface
- Large **Tkinter GUI** (800x800)
- **Scrollable terminal** for live logs
- Status labels updated every second
- Start/Stop buttons for bot control

### Automation
- Continuous background **grid cycle every 3 minutes** to minimize API usage.
- Auto regrid on **order fills** using Binance.US **user data streams**.
- Live **price WebSocket** updates.

---

## Setup

### Environment Variables
```bash
export BINANCE_API_KEY="your_api_key"
export BINANCE_API_SECRET="your_api_secret"
export CALLMEBOT_PHONE="your_whatsapp_number"  # optional for alerts
export CALLMEBOT_API_KEY="your_callmebot_key" # optional for alerts

 
 
**Not affiliated with Binance.US or CallMeBot.** **Profits are not guaranteed; you risk losing all or part of your investment.** Always test in simulation mode (e.g., Binance.US testnet) before live trading and proceed at your own risk!

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


5. **Verify Installation**:  
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
