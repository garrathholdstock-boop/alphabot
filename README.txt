╔══════════════════════════════════════════════════════════════╗
║           AlphaBot — Railway Deployment Guide               ║
╚══════════════════════════════════════════════════════════════╝

You have 3 files:
  • bot.py            — the trading bot
  • requirements.txt  — Python packages
  • README.txt        — this file

════════════════════════════════════════
STEP 1 — Get your Gmail App Password
════════════════════════════════════════

Gmail won't let bots log in with your normal password.
You need a special "App Password":

1. Go to myaccount.google.com
2. Click "Security" on the left
3. Under "How you sign in to Google" click "2-Step Verification"
   (you must have this turned on — if not, turn it on first)
4. Scroll to the bottom and click "App passwords"
5. Under "Select app" choose "Mail"
6. Under "Select device" choose "Other" and type "AlphaBot"
7. Click Generate
8. Copy the 16-character password shown (e.g. abcd efgh ijkl mnop)
9. Save it — you'll need it in Step 3

════════════════════════════════════════
STEP 2 — Create a GitHub repository
════════════════════════════════════════

1. Go to github.com and sign in (or sign up free)
2. Click the "+" button top right → "New repository"
3. Name it: alphabot
4. Leave it Public
5. Click "Create repository"
6. On the next page click "uploading an existing file"
7. Drag and drop BOTH files:
     • bot.py
     • requirements.txt
8. Click "Commit changes"

Your code is now on GitHub.

════════════════════════════════════════
STEP 3 — Deploy on Railway
════════════════════════════════════════

1. Go to railway.app
2. Click "Start a New Project"
3. Click "Deploy from GitHub repo"
4. Connect your GitHub account if asked
5. Select your "alphabot" repository
6. Railway will detect it's a Python project automatically
7. Click "Deploy" — it will try to start (and fail for now, that's ok)

════════════════════════════════════════
STEP 4 — Add your secret keys to Railway
════════════════════════════════════════

The bot needs your Alpaca keys and Gmail password.
These are stored as "Environment Variables" — they're secret
and never visible in your code.

In Railway:
1. Click on your alphabot project
2. Click "Variables" in the left menu
3. Add these one by one (click "New Variable" for each):

   Variable Name     │ Value
   ──────────────────┼──────────────────────────────
   ALPACA_KEY        │ your Alpaca API Key ID
   ALPACA_SECRET     │ your Alpaca Secret Key
   GMAIL_USER        │ garrathholdstock@gmail.com
   GMAIL_PASS        │ your 16-char Gmail App Password
   IS_LIVE           │ false   ← keep this false until ready!

4. Click "Deploy" after adding all variables

════════════════════════════════════════
STEP 5 — Check it's working
════════════════════════════════════════

1. In Railway click "Deployments" then click the latest deployment
2. Click "View Logs"
3. You should see something like:

   AlphaBot starting up
   Mode:    PAPER trading
   Stocks:  100 symbols
   Crypto:  23 pairs
   Connected — Portfolio: $100,000.00
   Cycle 1 | 2025-xx-xx xx:xx:xx
   [STOCKS] Scanning 100 symbols...
   [CRYPTO] Scanning 23 pairs...

If you see this — the bot is running! 🎉

════════════════════════════════════════
STEP 6 — Going live with real money
════════════════════════════════════════

When you're ready (after weeks of paper trading):

1. In Railway → Variables
2. Change ALPACA_KEY and ALPACA_SECRET to your LIVE keys
   (from Alpaca → Live Trading account → API Keys)
3. Change IS_LIVE to: true
4. Redeploy

⚠️  WARNING: This will trade with real money immediately.
Only do this after consistent paper trading results.

════════════════════════════════════════
COSTS
════════════════════════════════════════

• Railway:  ~$5/month (usage-based, very cheap for a simple bot)
• GitHub:   Free
• Gmail:    Free

════════════════════════════════════════
ADJUSTING SETTINGS
════════════════════════════════════════

All safety settings are at the top of bot.py (lines 30-38):

  MAX_DAILY_LOSS      = 50.0    ← shut off if you lose this much/day
  STOP_LOSS_PCT       = 2.0     ← sell if position drops this %
  MAX_POSITIONS       = 3       ← max stocks held at once
  MAX_TRADE_VALUE     = 500.0   ← max $ per single trade
  MAX_DAILY_SPEND     = 5000.0  ← max total buying per day
  DAILY_PROFIT_TARGET = 2000.0  ← stop trading once up this much

To change them:
1. Edit bot.py on your computer
2. Go to your GitHub repo
3. Click bot.py → click the pencil icon to edit
4. Make your changes → click "Commit changes"
5. Railway automatically redeploys within a minute

════════════════════════════════════════
DAILY EMAIL
════════════════════════════════════════

Every weekday at 5pm ET (New York time) you will receive
an email at garrathholdstock@gmail.com with:

  • Daily P&L for stocks and crypto
  • Number of trades, wins, losses, win rate
  • Full trade log
  • Open positions

════════════════════════════════════════
TROUBLESHOOTING
════════════════════════════════════════

Bot not connecting to Alpaca?
→ Check ALPACA_KEY and ALPACA_SECRET in Railway Variables
→ Make sure you're using Paper keys (start with PK...)

No email arriving?
→ Check GMAIL_PASS is the App Password (16 chars, no spaces)
→ Make sure 2-Step Verification is on in your Google account

Bot says "market closed" all the time?
→ This is correct outside 9:30am-4pm ET Monday-Friday
→ Crypto cycles will still run 24/7

Redeployment not working?
→ In Railway click "Deployments" → "Redeploy"

Need help?
→ Check Railway logs first — most errors are explained there
