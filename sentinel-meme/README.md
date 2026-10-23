> **Deployment update:** Follow [DEPLOYMENT-CHECKPOINTS.md](DEPLOYMENT-CHECKPOINTS.md) for Render + Windows + iPhone. Live execution is disabled in this release; older live instructions below do not enable it.

# SENTINEL Telegram v0.2

**Phone-operated Solana auto-trading prototype. Not deployed. Not audited. No real-money execution or Telegram delivery tested with your accounts.**

Start with **PHONE-SETUP.md**. The cloud worker runs continuously; Telegram is the private control panel. Your iPhone does not run the trading process.

## Added in this version

- Owner-only Telegram pairing and commands for status, positions, realized P&L, public wallet address, watchlist/developer mappings, pause, resume and exits.
- Durable notifications for filled buys/sells, failed transactions and operational problems; paper alerts are explicitly labeled. Outgoing messages are queued separately from trading.
- Finalized Solana wallet log subscriptions with separate transaction fetching. Stream interruption blocks entries for a full signal window after recovery; exits remain independent of the stream.
- Short-hold example configuration: five-minute time exit, 50 entry-attempt daily maximum, re-entry cooldown and fresh buyer evidence, cost filter and consecutive-loss pause.
- Encrypted on-disk wallet, persistent SQLite ledger, Docker image instructions and a Render worker blueprint.
- Worker pauses entries on every restart and when Telegram communication has not succeeded for 60 seconds. This protects entry control; protective exits may still run during Telegram outages.

`/start` shows help. It does NOT start live trading. `/resume` and exit requests require a short-lived confirmation. `/halt` stops ALL new submissions, including exits. All commands are accepted only in the paired owner's private chat. There is no key export or arbitrary withdrawal command in Telegram.

## What remains unproven or incomplete

This is not a promise of 20–50 trades/day or profit. The build can attempt up to its configured cap when evidence and limits allow; no activity level, latency, execution success or profitable strategy has been measured. The sample strategy is watched-wallet accumulation, not trained AI. It does not learn or rewrite itself.

Only supported SOL-funded Jupiter swaps are recognized. It does not discover every buyer, prove wallet independence, infer developers, comprehensively inspect liquidity locks, or guarantee rug detection. Unknown developers are rejected by default until you supply verified mappings. That can strongly limit the number of eligible trades. Token-2022 and unsupported routes remain excluded.

The worker still uses RPC/API calls for validation and execution. The three-second monitor delay is a delay after work, not an end-to-end execution guarantee. Long API checks and finality can delay exits. Stops are best-effort attempts and may fail or fill far beyond a target.

Jupiter transaction construction and your RPC remain trusted dependencies. Simulations, program filters and spending checks are not formal verification. A compromised server/signing key can bypass application controls. Wallet encryption protects the stored file, not a running process or compromised hosting account.

## Validation

See `test-results-v0.2.txt`: automated core, signing-fixture, Telegram authorization, replay, queue, stream-state and vault tests. Tests never submit funded trades or contact Telegram. The previous real Jupiter quote validation succeeded; DEX Screener returned HTTP 403 from this environment. See `network-check.json`. Provider compatibility, live messages, the Docker build on the actual host, and funded execution still require deployment testing.

Run locally for engineering checks:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s . -p 'test_*.py' -v
```

## Files

- `cloud.py`: cloud process, vault, streaming strategy adapter, loss pause.
- `telegram_control.py`: pairing, commands, outbox and trade-alert collection.
- `stream.py`: wallet subscriptions and transaction ingestion.
- `bot.py`: quote/token checks, signed execution, ledger and exits.
- `config.scalp.example.json`: starting cloud configuration (paper only).
- `config.example.json`: original CLI configuration retained for tests.
- `render.yaml`, `Dockerfile`: deployable cloud configuration.
- `wallet_recovery.py`: owner-only interactive hosting-shell recovery; never a start/build command.

The ZIP contains a flat `sentinel-meme` folder so its contents can be selected and uploaded through a phone browser. Do not upload the ZIP itself as a source repository; GitHub does not unpack it for deployment.
