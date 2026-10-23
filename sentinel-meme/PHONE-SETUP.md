# Phone setup — Telegram + persistent cloud worker

You can perform setup in Telegram and your phone browser. No PC is required by this route. The accounts, hosting purchase, credentials and initial pairing still require your actions; this package has not created them for you.

## 1. Create your Telegram bot

Open the official **https://t.me/BotFather**. Send `/newbot`, choose a display name, and choose an available username ending in `bot`. Keep the token private. Put it only into your hosting provider's secret field later, not in a public repository, this chat, or a message to the trading bot.

Your new bot will not answer until the server is deployed. Sending `/start` before deployment is harmless.

## 2. Upload the source using your phone

1. Save this ZIP in the iPhone Files app and tap it to extract.
2. In Safari, sign in at https://github.com and create a **private** repository called `sentinel-telegram`. An empty repository can be initialized with a README to expose the file controls.
3. Use **Add file → Upload files → choose your files**. In Files, open the extracted `sentinel-meme` folder and select its files. Upload its contents into the repository root, not a nested folder. Use Safari's desktop-site option if GitHub hides the controls. Browser file selection differs by iOS version.
4. Commit. Check that `render.yaml`, `Dockerfile`, `cloud.py`, `bot.py`, `telegram_control.py`, `stream.py`, the two example config JSON files, requirements and the two root `test_*.py` files appear at the repository root.

No secrets or generated wallets are included in this delivery. The flat folder avoids needing to upload a nested directory. The `render.yaml` file must be visible in the repo, not trapped inside a ZIP.

## 3. Create provider credentials

Use a Solana mainnet RPC provider with HTTP and WebSocket endpoints, historical parsed transaction access, account simulation and token-largest-account support. You need both the `https://...` RPC URL and the `wss://...` subscription URL supplied by that provider; do not guess one from the other.

Get your own Jupiter API key from the official developer portal linked at https://developers.jup.ag/docs/swap. Provider plans and credits may cost money. The code throttles HTTP requests and requires supported data; it will block trades when a provider fails rather than invent data.

## 4. Deploy the worker from your phone

1. Open https://dashboard.render.com and connect your private GitHub repository.
2. Choose **New → Blueprint**, select that repository, and use its `render.yaml`.
3. Review the proposed paid background worker and persistent disk before approving the purchase/deployment. No service or charges have been created by this delivery. The selected worker is `starter`; check the price shown by Render. Persistent disks require a paid service.
4. Supply these requested secrets:

| Hosting field | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token given by BotFather |
| `SOLANA_RPC_URL` | Your mainnet HTTPS RPC endpoint |
| `SOLANA_WS_URL` | Your mainnet WSS endpoint |
| `JUPITER_API_KEY` | Your Jupiter key |

The blueprint generates `PAIRING_CODE` and `WALLET_VAULT_KEY`. Keep both private and back up the vault key in your password manager. Do not rotate the vault key without migrating the encrypted wallet.

The default is `BOT_MODE=paper`, `ENABLE_LIVE_TRADING=NO`, and `EXPECTED_WALLET=UNSET`. Leave those unchanged for initial deployment.

5. Wait for the image build and tests to complete. A failed test aborts the build. Open the service's Environment page to reveal and copy `PAIRING_CODE`.
6. In your bot's **private Telegram chat**, send `/pair YOUR_PAIRING_CODE`. Pairing binds the numeric Telegram account ID, not a changeable username. The bot tries to delete that pairing message. After success, other accounts cannot claim ownership using the same code.
7. Send `/status`. Until the watchlist is configured, the worker can correctly report that setup is blocked.

Keep only ONE service instance. Persistent disk `/var/data` holds the encrypted wallet, ledger, owner ID, command state and notification queue. Do not remove or replace it during redeployments. Manual deployments pause entries on restart. Render disk deployments have a brief interruption; no bot can monitor exits while its process is stopped.

## 5. Configure the watchlist in Telegram

Use real **public** addresses:

```
/watch FIRST_WALLET_ADDRESS
/watch SECOND_WALLET_ADDRESS
/watch THIRD_WALLET_ADDRESS
/watches
/dev TOKEN_MINT_ADDRESS VERIFIED_DEVELOPER_WALLET_ADDRESS
/settings
```

The example words above are placeholders, not addresses. There must be at least three watched wallets; the build caps the list at twenty. Developer mappings are explicit user-supplied attribution. The bot does not independently verify identity or discover developers. Unknown developers block entry by default.

Wait at least one signal window (two minutes by default) after the stream connects or reconnects. Then send `/resume` and the requested `/confirm CODE`. You are enabling **paper** entries, not depositing or spending real funds.

## 6. Control and notifications

| Command | Behavior |
|---|---|
| `/status` | Mode, pause flags, heartbeat, positions and unresolved orders |
| `/positions` | Managed positions and time held |
| `/pnl` | UTC-day realized results; open P&L excluded |
| `/wallet` | Public address of the dedicated encrypted wallet |
| `/pause` | Stop new buys; continue exits |
| `/resume` | Confirm to permit entries subject to all checks |
| `/close MINT` | Confirm to request a full exit of one managed position |
| `/exit_all` | Confirm to pause entries and request exits of all positions |
| `/clear_exit` | Clear exit-all only after flat and no pending orders |
| `/halt` | Stop ALL new submissions, including protective exits |
| `/unhalt` | Clear halt; entries stay paused |

A request is not a filled trade. Pending transactions and quote/simulation checks can block exits. Already submitted transactions cannot be cancelled by these commands.

The bot sends a notification for each recorded fill and important failure. Sell notifications include actual SOL cash-flow P&L after recorded fees/costs, not an invented percentage or wallet-profit leaderboard. Paper alerts always say PAPER. Rent left in an account is treated conservatively as a cost; later manual rent recovery is outside the ledger.

Notifications use a durable retry queue. In an ambiguous Telegram timeout, a message may be delivered more than once; fill IDs identify duplicates. Delivery is not guaranteed while Telegram, your connectivity or the cloud provider is unavailable. If the bot cannot successfully communicate with Telegram for 60 seconds, new entries stop but exits remain active. Enable Telegram/iOS notifications for the bot chat and unmute it to receive phone alerts.

## 7. Read the limits before live use

These are **engineering example settings**, not a deposit recommendation, calibrated strategy, or promise of profitability:

- Buy size: **0.05 SOL**.
- At most **50 entry attempts per UTC day**, not 50 forced completed trades.
- Lifetime cumulative buy-cost budget: **5 SOL**. Profits and restarting do not reset it.
- Open cost cap: **0.12 SOL**, two positions maximum.
- Daily worst-case risk budget: **0.15 SOL**: realized losses + all open principal + next entry/cost allowance.
- Native fee/rent allowance per order: **0.003 SOL**; reserve: **0.03 SOL**.
- Time exit: **five minutes**; stop/profit/trailing example thresholds: **8% / 15% / 6%**.
- A conservative round-trip cost filter skips entries when the allowance plus quote losses consumes the profit target.
- Three consecutive realized losing outcomes pause new entries; you must review and explicitly resume.
- Re-entry needs a ten-minute cooldown and new buyer evidence after the prior exit.

The cap permits frequency; the signal, liquidity, developer checks, budgets and cost filter determine whether any trade is allowed. Twenty to fifty trades per day has NOT been demonstrated. Many small scalps will correctly be rejected as uneconomical. No minimum activity quota or automatic limit increases exist.

## 8. Wallet backup and live enablement

The server creates a dedicated wallet and encrypts it with `WALLET_VAULT_KEY`. Nothing is funded automatically. `/wallet` reveals only the public address. Protect your hosting account: the running server necessarily has signing access.

**Before funding, establish and verify a recovery method.** Back up both the persistent disk (especially `wallet.enc`) and the vault key separately. Losing either can lose access to the wallet. In your own private Render browser shell, you may run `python wallet_recovery.py` and follow its interactive prompt to recover the base58 private key for a compatible wallet. Never run that script as a build/start command, copy its output to logs, or send its output through Telegram/ChatGPT. If the shell is unusable on your phone, do not fund until you have a working backup/recovery route.

This version has no Telegram withdrawal command. Use the recovered dedicated wallet in a compatible wallet app after pausing the bot and resolving all positions/pending orders. Manual wallet activity is not reconciled by the strategy ledger; never trade simultaneously from another app.

Before enabling live mode:

1. Observe paper operation, validate the actual provider integration and ensure both test alerts and positions behave as expected. Review the source and the lack of independent security audit.
2. `/pause`, then `/exit_all` and confirm. Wait until `/positions` is empty and no transaction is pending. Mode changes refuse to abandon managed positions.
3. Copy the public address from `/wallet` into Render's `EXPECTED_WALLET`. Review all limits and wallet recovery.
4. In Render's environment, change `BOT_MODE` to `live` and `ENABLE_LIVE_TRADING` to `YES`, save and deploy. No entry is enabled by this action alone: every startup creates PAUSE.
5. Check `/status` says LIVE, the same public address remains, and provider checks work. Fund only the dedicated public address with an amount whose complete loss you can tolerate, retaining the configured reserve.
6. `/resume` and confirm the displayed mode and limits. Future eligible buys and exits are automatic; you do not approve each trade.

A permanently ambiguous/dropped submission currently requires technical investigation. Do not delete its pending row or replace the database to get around the block. Risk settings are fingerprinted into each ledger. Changes require a deliberate migration after positions/pending orders are resolved; the Telegram interface deliberately cannot raise your limits.

## Implementation status

Automated tests cover authorization, duplicate command handling, notification persistence, wallet encryption, stream health gates and signing fixtures. They are not an audit, a strategy backtest, or proof that live transactions will execute. No funded execution, authenticated Telegram delivery, Render deployment or mobile browser walkthrough has been performed in this delivery. These require your accounts.

## Official references

- Telegram bot creation: https://core.telegram.org/bots/tutorial
- Telegram API: https://core.telegram.org/bots/api
- Browser source uploads: https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository
- Render blueprints: https://render.com/docs/blueprint-spec
- Persistent disks: https://render.com/docs/disks
- Solana logs subscriptions: https://solana.com/docs/rpc/websocket/logssubscribe
- Jupiter: https://developers.jup.ag/docs/swap

References checked 2026-10-23. Provider UI and pricing can change; follow the actual account screens and review charges before deploying.
