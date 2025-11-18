# Check back here weekly for the newest program version. 
 The newest update was on 11-18-2025. 

# INFINITY GRID PLATINUM 2025 – Full Feature Overview

This is a fully automated Binance.US spot trading bot (Python/Tkinter GUI) that runs an **infinite grid strategy** on a dynamically selected basket of altcoins, combined with **order-book-based dynamic rebalancing**.

## Core Strategy Components

| Component                  | Description                                                                 |
|----------------------------|-----------------------------------------------------------------------------|
| **Grid Type**              | Asymmetric Infinite Grid (Buy grid below price, Sell grid above price)     |
| **Position Sizing**        | Golden Ratio (φ ≈ 1.618) progression on buys, optimized 1.309× on sells    |
| **Base Investment per Level** | $12 USD equivalent per grid level (`BASE_CASH_PER_LEVEL × 1.5`)           |
| **Grid Depth**             | 8 buy levels + 8 sell levels per coin                                      |
| **Grid Spacing**           | Fixed-percentage: 1.2% between each level (compounding downward/upward)    |
| **Re-grid on Fill**        | Instantly cancels all open orders for that symbol and places a fresh grid centered on the new current price |

## Coin Selection (CoinGecko Integration)

- Updates **every hour** (or on bot start)
- Pulls top 100 coins by market cap from CoinGecko API (`vs_currency=usd`)
- Strict filtering:
  - Must have a valid `/USDT` trading pair on Binance.US
  - Market cap ≥ $800 M
  - 24h volume ≥ $40 M
  - 7-day price change ≥ +6%
  - 14-day price change ≥ +12%
  - Scoring formula: `1.5×7d% + 14d% + (volume / market_cap × 100)`
- **Hard blacklist** (never traded, even if pair exists):  
  `BTC`, `ETH`, `SOL`, `XRP`, `BNB`, `BCH` + all stablecoins + wrapped tokens (`WBTC`, `WETH`, etc.)
- Selects **top 10 scoring** coins → final `buy_list`
- Fallback safe list if API fails:  
  `ADAUSDT`, `AVAXUSDT`, `DOTUSDT`, `MATICUSDT`, `LINKUSDT`, `UNIUSDT`, `AAVEUSDT`, `CRVUSDT`, `COMPUSDT`, `MKRUSDT`

## Grid Order Placement Details

| Side | Levels | Price Formula                            | Quantity Progression                     | Notes                          |
|------|--------|------------------------------------------|------------------------------------------|--------------------------------|
| Buy  | 8      | `current_price × (1 - 0.012)^n`          | `cash × 1.618^(n-1) / price`             | Larger size deeper in dip      |
| Sell | 8      | `current_price × (1 + 0.012)^n`          | `cash × 1.309^(n-1) / price`             | Smaller size higher up         |

- All prices/quantities rounded to exchange tickSize/stepSize
- Minimum notional and quantity filters respected
- Limit orders placed slightly aggressive for faster fills:
  - Buys: `calculated_price × 1.001`
  - Sells: `calculated_price × 0.999`

## Rebalancing Engine (Order-Book Pressure Based)

- Runs **every 12 minutes** (`REBALANCE_INTERVAL = 720` seconds)
- Calculates total portfolio value in USD (all holdings × current price + USDT)
- For every held altcoin:
  1. Fetches top 20 bid/ask levels → computes **buy pressure ratio** (bids value / total depth value)
     - > 65% bids → **High conviction** → target **15%** of portfolio
     - < 35% bids → **Low conviction** → target **4%** of portfolio
     - Otherwise → target **5%** of portfolio
  2. If current value > 105% of target → place limit sell to reduce
  3. If current value < 95% of target **and** buy pressure > 60% → place limit buy (only if enough free USDT after reserves)

## Fee & Reserve Handling

- Assumes 0.1% maker/taker fee (`FEE_RATE = 0.001`)
- Every buy adds fee buffer: `required_usdt = price × qty × 1.001`
- **Reserve system**: Always keeps **33% of USDT balance + minimum $8** untouched → never goes all-in

## Safety & Risk Management

- Full blacklist enforcement
- Strict market-cap, volume, and momentum filters
- Reserve system prevents liquidation-style drawdowns
- Instant re-grid on every fill → true infinite grid behavior
- All open orders canceled on STOP or per-symbol regrid

## GUI & Monitoring

- Fixed 800×900 dark-theme window
- Real-time scrolling terminal log
- Live USDT balance and active order counter
- Large START / STOP buttons
- Runs exclusively on **Binance.US** (`tld='us'`)

## Summary

The bot combines three powerful mechanisms:

1. **Infinite grid profit** from volatility on 10 high-conviction altcoins  
2. **Momentum filtering** via CoinGecko (only trades coins already outperforming)  
3. **Dynamic allocation** based on real-time order-book sentiment (not just price)

Result: A completely hands-off, 24/7 grid bot that automatically concentrates capital into the strongest mid/large-cap altcoins while continuously harvesting grid profits.
---

## Setup

### Environment Variables
```bash
export BINANCE_API_KEY="your_api_key"
export BINANCE_API_SECRET="your_api_secret"
export CALLMEBOT_PHONE="your_whatsapp_number"  # optional for alerts
export CALLMEBOT_API_KEY="your_callmebot_key" # optional for alerts

 
 
**Not affiliated with Binance.US, coingecko.com, or CallMeBot.** **Profits are not guaranteed; you risk losing all or part of your investment.** Always test in simulation mode (e.g., Binance.US testnet) before live trading and proceed at your own risk!

### Legal Disclaimers

**Important Notice: This information is provided as of October 28, 2025, and cryptocurrency markets, regulations, and technologies evolve rapidly. Always verify the latest information and consult professionals before using this bot.**

- **No Affiliation or Endorsement**: This trading bot is not affiliated with, endorsed by, or sponsored by Binance.US, coingecko.com, or CallMeBot. All interactions with these platforms are at the user's sole discretion and risk, subject to their respective terms of service.
- **Not Financial, Investment, or Legal Advice**: This bot and its description are for informational and educational purposes only. They do not constitute financial, investment, tax, legal, or professional advice. The author is not a financial advisor, broker, or registered investment advisor. All trading decisions are your sole responsibility. Seek independent advice from qualified professionals to assess suitability for your circumstances.
- **High Risk of Loss**: Cryptocurrency trading involves substantial risks, including the potential for **complete or partial loss of your investment**. Prices are highly volatile, subject to rapid and unpredictable changes due to market sentiment, regulatory news, technological issues, or external factors. Past performance does not guarantee future results. You could lose more than your initial investment.
- **No Guarantee of Profits**: **Profits are not guaranteed**. There is no assurance that this bot will generate profits or avoid losses. Trading strategies, including mean-reversion with momentum, oscillator, and trend filters, may fail in certain market conditions (e.g., high volatility, low liquidity, or black swan events). The 0.8% profit target is a goal, not a guarantee, and losses may occur due to fees, slippage, or unfavorable price movements.
- **Legal and Regulatory Compliance**: Cryptocurrency trading is subject to U.S. laws, including oversight by the SEC (for securities-like assets) and CFTC (for commodities). As of October 2025, the CLARITY Act and FIT21 provide clearer jurisdiction, but users must ensure compliance with AML/KYC requirements under the Bank Secrecy Act (BSA), sanctions screening, and state licensing (e.g., New York's BitLicense). Do not use this bot if prohibited in your jurisdiction. On October 17, 2025, the NFA eliminated certain disclosure guidance for digital assets but still requires disclosing material risks. Users are responsible for tax reporting (e.g., IRS Form 1099-DA for gains/losses) and any violations could result in penalties.
- **No Liability**: The author disclaims all liability for any direct, indirect, consequential, or special losses arising from using this bot, including trading losses, data inaccuracies, third-party service failures (e.g., Binance.US API, CallMeBot), or security breaches. You are responsible for securing API keys and accounts. The bot interacts with third-party services; any downtime, errors, or changes in their terms are beyond control.
- **Software Risks and Warranty Disclaimer**: The bot is provided "as is" without warranties of merchantability, fitness for purpose, or non-infringement. It may contain bugs, and users assume risks from technical failures, incorrect configurations, or unauthorized access. Test thoroughly in a simulation environment before live trading.
- **User Responsibilities**: You must comply with Binance.US, coingecko.com, and CallMeBot terms, maintain account security, and monitor trades. Stop using the bot if it violates any laws or platform rules. This bot does not provide custody services; all assets remain under your control on Binance.US.

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
# Environment variables for This Crypto Trading Robot

export BINANCE_API_KEY="your_binance_api_key_here"
export BINANCE_API_SECRET="your_binance_api_secret_here"
export CALLMEBOT_API_KEY="your_callmebot_api_key_here"
export CALLMEBOT_PHONE="your_phone_number_here"  # E.g., +1234567890
```

Run Program: 

```bash
conda activate 
python3 infinity_grid_live.py
```

**Note**: Ensure the bot script is saved as `infinity_grid_live.py`.  

This setup is for educational use; live trading carries significant risks, including the potential to **lose all or part of your investment**, as outlined in the disclaimers. 

To set up CallMeBot for WhatsApp, visit https://www.callmebot.com/ and follow the instructions to get your API key and configure the service. Then, use `curl` or Python to send messages via the API.

This setup is for educational use; live trading carries significant risks, including the potential to **lose all or part of your investment**, as outlined in the disclaimers. 

The bot is not affiliated with or endorsed by Binance.US, coingecko.com, or CallMeBot. Always test in a simulation environment (e.g., Binance.US testnet) before deploying with real funds.
