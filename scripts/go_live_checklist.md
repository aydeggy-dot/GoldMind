# GoldMind — go-live checklist

Run through this before the first live cent is risked. Tick each box; do
not skip. Steps 1–4 are one-time; step 5 is the operational regime.

---

## 1. VPS provisioning (one-time)

- [ ] Windows VPS provisioned, joined to broker-region timezone
- [ ] MetaTrader 5 terminal installed, logged in to the target account
- [ ] In MT5: **Tools → Options → Expert Advisors → Allow algorithmic
      trading** is ON; automated trading permitted on the account
- [ ] MT5 **auto-update DISABLED** (mid-update crashes during trades
      are the single biggest Windows-VPS failure mode)
- [ ] Python 3.10+ installed and on PATH
- [ ] `git clone` / copy the repo to `C:\goldmind`
- [ ] `python -m venv .venv` + `.venv\Scripts\pip install -r requirements.txt`

## 2. Configuration

- [ ] `config\config.yaml` copied from `config\config.example.yaml`
- [ ] `config\credentials.yaml` copied from `config\credentials.example.yaml`
      — values filled in: MT5 account/password/server/terminal_path,
      Telegram bot_token/chat_id
- [ ] `config\credentials.yaml` is gitignored (verify: it is NOT in
      `git status`)
- [ ] Telegram bot created via @BotFather, chat_id obtained by
      messaging the bot then fetching /getUpdates
- [ ] Risk caps reviewed: `risk.risk_per_trade_pct`, `risk.max_lot_size`,
      `circuit_breakers.*`, `sanity_check.max_lot_hard_ceiling`

## 3. Pre-flight

- [ ] `python scripts\preflight.py` exits 0 (all checks PASS)
- [ ] `python -m pytest tests\` is green end-to-end

## 4. VPS install

- [ ] Run `scripts\install.bat` as Administrator:
    - Registry keys set (screen lock, auto-reboot, screensaver)
    - NTP configured + forced resync
    - Scheduled Tasks created: `GoldMind Bot`, `GoldMind Watchdog`,
      `GoldMind NTP Sync`
- [ ] Manually start `GoldMind Bot` from Task Scheduler once;
      Telegram startup message received
- [ ] `/status` via Telegram returns healthy output

## 5. Two-week paper-demo soak

Run the bot on a **demo** MT5 account for **≥ 2 consecutive weeks**
before any live capital. Daily checks:

- [ ] Bot online 24h of the last 24h (check `/uptime` + log rotation)
- [ ] No unhandled exceptions in `logs\goldmind.log`
- [ ] Daily report received at configured time
- [ ] Signals recorded in DB (`signals` table row count > 0 per day
      during active sessions)
- [ ] Circuit breakers never false-triggered
- [ ] Clock drift stays below `max_clock_drift_seconds`

Rolling weekly checks:

- [ ] Win rate, profit factor, avg R:R all meet thresholds in
      `config.health.*`
- [ ] Walk-forward sweep on collected data still passes
      (`backtesting.walk_forward` criteria)

## 6. Go-live

- [ ] Fund the live MT5 account (USD or broker's quote currency —
      **never deposit in USDT expecting USD**; the broker will convert
      at an unfavorable rate)
- [ ] Temporarily set `risk.max_lot_size` to `0.01` in `config.yaml`
- [ ] Re-run `scripts\preflight.py` on the live account
- [ ] Restart `GoldMind Bot` task
- [ ] Watch the first three trades live; verify SL + TP placed
      server-side (visible in the MT5 GUI)

## 7. Post-go-live scale-up

- [ ] After 30 consecutive live trades with the configured KPIs intact,
      consider scaling `risk.max_lot_size` per `compounding.*` rules
- [ ] Never scale up during a drawdown
- [ ] Document every parameter change with a version bump in
      `strategy_version` (audit trail via `trades.strategy_version`)

## 8. Incident playbook

- Bot not responding to `/status` within 2 min:
  `/kill` via Telegram (confirms), then SSH/RDP to VPS and check logs.
- MT5 disconnected: engine auto-reconnects; if 5 attempts fail, engine
  auto-pauses and alerts. Do NOT manually place trades on the account
  while paused — restart the bot first.
- Suspected broker spec change alert: verify in MT5 GUI; update
  `gold_specs` if real, and investigate swap rollover impact.
