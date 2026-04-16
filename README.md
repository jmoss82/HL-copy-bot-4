# HyperLiquid Copy Trading Bot

This folder contains one of the live HyperLiquid copy-trading bots in this repository.

Like the other `copy-bot*` folders, it is a standalone bot instance with its own deployment and environment variables. The engine is shared, but the copied wallet, coin universe, and limits are configured independently.

As of April 16, 2026, this bot is being moved from the main account onto HyperLiquid sub-account `SA2`.

## Important Context

This README is meant to describe structure and runtime behavior. It should not be treated as a guaranteed snapshot of the exact live production settings.

For this bot, the source of truth is:

1. the code in this folder
2. the environment variables currently configured for this deployment

That is especially important for values like `COPY_TARGET_ADDRESS`, `COPY_COINS`, leverage, notional caps, and dry-run status.

Current rollout status:

- Bot 4 is being reconfigured for sub-account `SA2`
- a dedicated API wallet is being used as the signer
- the initial rollout target is `BTC` only
- the first deployment pass is intended to run in dry-run mode until signer/account routing and equity are confirmed

## Runtime Flow

1. Poll the copied wallet via HyperLiquid's public `/info` API.
2. Filter to the coins configured for this bot.
3. Compare the latest positions with the previous snapshot.
4. Translate target movement into the bot's own desired size.
5. Place mirrored IOC orders through the HyperLiquid SDK.

When `COPY_RECONCILE_MODE=lifecycle`, the bot anchors a copy ratio when a target trade opens and then mirrors the rest of that lifecycle instead of only targeting a final net position.

## Key Files

| File | Purpose |
|---|---|
| `bot.py` | Main process, startup sync, polling loop, reconciliation, heartbeat logging |
| `config.py` | Environment-variable loading, defaults, and validation |
| `tracker.py` | Polls the copied wallet and detects position changes |
| `copier.py` | Queries account state, computes sizes, sets leverage, and places orders |
| `.env.example` | Local configuration template |
| `requirements.txt` | Python dependencies |

## Configuration

Railway variables are the usual production source of truth. For local runs, copy `.env.example` to `.env` and fill in the values you want to use.

The main variables are:

| Variable | Meaning |
|---|---|
| `HL_WALLET_ADDRESS` | Signer wallet address |
| `HL_PRIVATE_KEY` | Private key for signing |
| `HL_ACCOUNT_ADDRESS` | Trading account or agent wallet |
| `COPY_TARGET_ADDRESS` | Wallet being copied |
| `COPY_COINS` | Coins this bot may copy |
| `COPY_SCALING_MODE` | How copied trades are sized |
| `COPY_RECONCILE_MODE` | `state`, `delta`, or `lifecycle` |
| `COPY_SYNC_STARTUP` | Whether to join existing positions on startup |
| `COPY_DRY_RUN` | Simulated vs live execution |

`.env.example` is a template and may not always match the exact live deployment settings.

For sub-account deployments:

- `HL_WALLET_ADDRESS` must be the address that matches `HL_PRIVATE_KEY`
- `HL_ACCOUNT_ADDRESS` should be the sub-account or agent-wallet address you want this bot to trade on
- if `HL_ACCOUNT_ADDRESS` is left blank, the bot trades on the signer wallet directly

For the current Bot 4 deployment, the signer is an API wallet and `HL_ACCOUNT_ADDRESS` points to `SA2`.

Do not include the `HL:` prefix that HyperLiquid sometimes shows in the UI when copying a sub-account address. Railway should store the raw `0x...` address.

One important implementation detail for sub-accounts: HyperLiquid can expose capital partly in perp state and partly in spot balances while still using that capital for trading. Bot 4's equity calculation was updated in April 2026 so startup equity reflects combined sub-account value rather than just posted perp margin.

## Reconcile Modes

- `state`: aim for a desired net position each cycle
- `delta`: trade only the detected net change since the last snapshot
- `lifecycle`: anchor a copy ratio on open, then mirror the full trade lifecycle

## Startup Behavior

If the target already has an open position when the bot starts, the bot can keep that coin locked until the next clean entry instead of jumping in mid-trade.

If `COPY_SYNC_STARTUP=true`, the bot is allowed to enter those already-open target positions immediately. That is most useful for controlled recovery after a restart or crash.

## Risk Guards

- `COPY_MAX_TRADE_USD` caps a single order's notional
- `COPY_MAX_POSITION_USD` caps resulting exposure
- `COPY_MIN_TRADE_USD` filters out trades below the exchange minimum
- `COPY_MAX_DAILY_TRADES` limits unexpected bursts of trading

## Running

Local entry point:

```bash
python bot.py
```

Production entry point:

```bash
python bot.py
```
