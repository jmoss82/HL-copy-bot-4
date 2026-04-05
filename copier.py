"""
Trade Copier

Executes mirrored trades on your HyperLiquid account using the official SDK.
Handles position sizing, leverage, price slippage, and safety guards.

This module targets STANDARD HyperLiquid perps (BTC, ETH, etc.),
not XYZ HIP-3 pairs.
"""
import time
from typing import Dict, Optional
from collections import deque
from dataclasses import dataclass
from loguru import logger

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from config import CopyBotConfig
from tracker import PositionChange


@dataclass
class TradeResult:
    """Outcome of a single copy-trade execution."""
    success: bool
    coin: str
    side: str           # "BUY" or "SELL"
    requested_size: float
    filled_size: float = 0.0
    avg_price: float = 0.0
    order_id: Optional[int] = None
    error: str = ""


class TradeCopier:
    """
    Mirrors detected position changes onto your HyperLiquid account.

    Uses the standard HyperLiquid SDK (Exchange / Info) for order placement
    and account queries.  All orders are IOC (immediate-or-cancel) limits
    priced aggressively through the spread so they behave like market orders.
    """

    def __init__(self, config: CopyBotConfig):
        self.config = config

        # SDK clients - initialised in setup()
        self._account: Optional[Account] = None
        self.info: Optional[Info] = None
        self.exchange: Optional[Exchange] = None

        # Resolved address used for info queries
        self.query_address: str = ""

        # Metadata cache
        self._sz_decimals: Dict[str, int] = {}

        # Rate-limiting / daily trade counter
        self._trade_timestamps: deque = deque(maxlen=config.max_daily_trades)

        # Cached equity (refreshed periodically, not every cycle)
        self._our_equity: float = 0.0
        self._equity_ts: float = 0.0

        # Cached account state and mids to reduce API pressure
        self._positions_cache: Dict[str, float] = {}
        self._positions_ts: float = 0.0
        self._mids_cache: Dict[str, float] = {}
        self._mids_ts: float = 0.0

    # -- Lifecycle --------------------------------------------------

    def setup(self) -> None:
        """Initialise SDK clients, load metadata, set leverage."""
        self._account = Account.from_key(self.config.private_key)

        if self._account.address.lower() != self.config.wallet_address.lower():
            raise ValueError(
                f"Private key does not match wallet address. "
                f"Expected {self.config.wallet_address}, got {self._account.address}"
            )

        base_url = constants.MAINNET_API_URL
        self.info = self._build_info_with_retry(base_url)

        # Agent-wallet: if account_address differs from the signer, pass it
        acct = self.config.account_address
        if acct and acct.lower() != self._account.address.lower():
            self.exchange = Exchange(self._account, base_url, account_address=acct)
            self.query_address = acct
        else:
            self.exchange = Exchange(self._account, base_url)
            self.query_address = self._account.address

        logger.info(f"SDK initialised  | signer={self._account.address}")
        logger.info(f"Trading account  | address={self.query_address}")

        # Load universe metadata (size decimals and tick sizes per coin)
        meta = self.info.meta()
        for asset in meta.get("universe", []):
            name = asset["name"]
            self._sz_decimals[name] = asset.get("szDecimals", 5)

        logger.info(f"Loaded metadata for {len(self._sz_decimals)} perps")

        # Set leverage for each coin we plan to copy
        for coin in self.config.coins_to_copy:
            if coin == "*":
                continue
            self._set_leverage(coin)

    # -- Account queries --------------------------------------------

    def get_our_equity(self, force: bool = False) -> float:
        """Account equity, cached for 60 s unless *force*."""
        if not force and (time.time() - self._equity_ts) < 60:
            return self._our_equity
        try:
            state = self.info.user_state(self.query_address)
            self._our_equity = float(
                state.get("marginSummary", {}).get("accountValue", 0)
            )
            self._equity_ts = time.time()
        except Exception as e:
            logger.error(f"Failed to fetch our equity: {e}")
        return self._our_equity

    def get_our_positions(self, force: bool = False) -> Dict[str, float]:
        """Return our current positions as {coin: signed_size}."""
        if not force and (time.time() - self._positions_ts) < 2:
            return dict(self._positions_cache)
        try:
            state = self.info.user_state(self.query_address)
            positions: Dict[str, float] = {}
            for entry in state.get("assetPositions", []):
                pos = entry.get("position", {})
                coin = pos.get("coin", "")
                size = float(pos.get("szi", 0))
                if abs(size) > 1e-10:
                    positions[coin] = size
            self._positions_cache = positions
            self._positions_ts = time.time()
            return dict(positions)
        except Exception as e:
            logger.error(f"Failed to fetch our positions: {e}")
            return dict(self._positions_cache)

    def get_mid_price(self, coin: str) -> float:
        """Current mid-market price for *coin*."""
        if (time.time() - self._mids_ts) >= 1:
            try:
                mids = self.info.all_mids()
                self._mids_cache = {k: float(v) for k, v in mids.items()}
                self._mids_ts = time.time()
            except Exception as e:
                logger.error(f"Failed to refresh mid prices: {e}")
        if coin in self._mids_cache:
            return self._mids_cache[coin]
        try:
            mids = self.info.all_mids()
            px = float(mids.get(coin, 0))
            if px > 0:
                self._mids_cache[coin] = px
                self._mids_ts = time.time()
            return px
        except Exception as e:
            logger.error(f"Failed to get mid price for {coin}: {e}")
            return 0.0

    # -- Scaling ----------------------------------------------------

    def target_position_to_desired_size(
        self,
        coin: str,
        target_size: float,
        target_equity: float,
    ) -> float:
        """
        Convert target's absolute position into our desired absolute position.

        Returns signed size (positive long, negative short).
        """
        if abs(target_size) < 1e-10:
            return 0.0

        if self.config.scaling_mode == "proportional":
            our_eq = self.get_our_equity()
            ratio = (our_eq / target_equity) if target_equity > 0 else 0
            desired = target_size * ratio
        elif self.config.scaling_mode == "fixed_ratio":
            desired = target_size * self.config.fixed_ratio
        elif self.config.scaling_mode == "fixed_size":
            desired = self.config.fixed_size * (1.0 if target_size > 0 else -1.0)
        elif self.config.scaling_mode == "fixed_notional":
            mid = self.get_mid_price(coin)
            if mid <= 0:
                logger.error(f"No price data for {coin}, cannot compute desired size")
                return 0.0
            desired = (self.config.fixed_notional_usd / mid) * (
                1.0 if target_size > 0 else -1.0
            )
        else:
            logger.error(f"Unknown scaling mode: {self.config.scaling_mode}")
            return 0.0

        return desired

    def scale_delta(
        self,
        change: PositionChange,
        target_equity: float,
    ) -> float:
        """
        Convert the target's raw delta into the size we should trade.

        Returns a signed float (positive = buy, negative = sell).
        """
        raw = change.delta

        if self.config.scaling_mode == "proportional":
            our_eq = self.get_our_equity()
            ratio = (our_eq / target_equity) if target_equity > 0 else 0
            scaled = raw * ratio
        elif self.config.scaling_mode == "fixed_ratio":
            scaled = raw * self.config.fixed_ratio
        elif self.config.scaling_mode == "fixed_size":
            scaled = self.config.fixed_size * (1.0 if raw > 0 else -1.0)
        elif self.config.scaling_mode == "fixed_notional":
            mid = self.get_mid_price(change.coin)
            if mid <= 0:
                logger.error(f"No price data for {change.coin}, cannot scale")
                return 0.0
            scaled = (self.config.fixed_notional_usd / mid) * (1.0 if raw > 0 else -1.0)
        else:
            logger.error(f"Unknown scaling mode: {self.config.scaling_mode}")
            return 0.0

        # Optional per-trade notional guard
        mid = self.get_mid_price(change.coin)
        if (
            mid > 0
            and self.config.max_trade_usd > 0
            and abs(scaled) * mid > self.config.max_trade_usd
        ):
            capped = self.config.max_trade_usd / mid
            logger.warning(
                f"Per-trade cap hit: {abs(scaled):.6f} {change.coin} "
                f"(${abs(scaled) * mid:,.0f}) capped to {capped:.6f} "
                f"(${self.config.max_trade_usd:,.0f})"
            )
            scaled = capped * (1.0 if scaled > 0 else -1.0)

        return scaled

    # -- Execution --------------------------------------------------

    def execute(
        self,
        coin: str,
        size_delta: float,
        dry_run: bool = True,
    ) -> Optional[TradeResult]:
        """
        Place an IOC limit order to mirror a position change.

        Args:
            coin:       e.g. "BTC"
            size_delta: signed size to trade (positive = buy, negative = sell)
            dry_run:    if True, log but don't send the order

        Returns:
            TradeResult, or None if the trade was filtered out.
        """
        if abs(size_delta) < 1e-10:
            return None

        is_buy = size_delta > 0
        abs_size = abs(size_delta)
        side = "BUY" if is_buy else "SELL"

        # -- Round to valid size increment --------------------------
        decimals = self._sz_decimals.get(coin, 5)
        abs_size = round(abs_size, decimals)
        if abs_size == 0:
            logger.debug(f"Size rounded to zero for {coin}, skipping")
            return None

        # -- Min trade size check -----------------------------------
        mid = self.get_mid_price(coin)
        if mid <= 0:
            logger.error(f"No price data for {coin}, cannot execute")
            return TradeResult(False, coin, side, abs_size, error="no price data")

        # Enforce max-position cap on resulting exposure.
        signed_delta = abs_size if is_buy else -abs_size
        current_size = self.get_our_positions().get(coin, 0.0)
        max_abs_pos = self.config.max_position_usd / mid if self.config.max_position_usd > 0 else float("inf")
        proposed_size = current_size + signed_delta
        clipped_size = max(-max_abs_pos, min(max_abs_pos, proposed_size))
        signed_delta = clipped_size - current_size
        if abs(signed_delta) < 1e-10:
            logger.warning(
                f"Position cap blocks trade: {coin} current={current_size:+.6f}, "
                f"requested_delta={proposed_size - current_size:+.6f}, "
                f"max_abs={max_abs_pos:.6f}"
            )
            return None
        if abs((proposed_size - current_size) - signed_delta) > 1e-10:
            logger.warning(
                f"Position cap clipped trade: {coin} "
                f"{proposed_size - current_size:+.6f} -> {signed_delta:+.6f}"
            )

        is_buy = signed_delta > 0
        side = "BUY" if is_buy else "SELL"
        abs_size = round(abs(signed_delta), decimals)
        if abs_size == 0:
            logger.debug(f"Clipped size rounded to zero for {coin}, skipping")
            return None

        notional = abs_size * mid
        if notional < self.config.min_trade_size_usd:
            logger.debug(
                f"Trade too small: {abs_size} {coin} = ${notional:.2f} "
                f"(min ${self.config.min_trade_size_usd})"
            )
            return None

        # -- Daily trade limit --------------------------------------
        now = time.time()
        day_ago = now - 86400
        while self._trade_timestamps and self._trade_timestamps[0] < day_ago:
            self._trade_timestamps.popleft()
        if len(self._trade_timestamps) >= self.config.max_daily_trades:
            logger.critical("Daily trade limit reached - refusing to execute")
            return TradeResult(False, coin, side, abs_size, error="daily limit")

        # -- Calculate aggressive IOC price -------------------------
        limit_px = self._slippage_ioc_price(coin, is_buy, mid)

        # -- Dry-run shortcut ---------------------------------------
        if dry_run:
            logger.info(
                f"[DRY RUN] {side} {abs_size} {coin} @ ~${self._fmt_price(limit_px)} "
                f"(mid=${self._fmt_price(mid)}, notional=${notional:,.0f})"
            )
            return TradeResult(True, coin, side, abs_size, abs_size, mid)

        # -- Live execution -----------------------------------------
        logger.warning(
            f"EXECUTING: {side} {abs_size} {coin} @ ${self._fmt_price(limit_px)} "
            f"(mid=${self._fmt_price(mid)}, slippage={self.config.slippage_bps}bps)"
        )

        try:
            result = self.exchange.order(
                coin, is_buy, abs_size, limit_px,
                {"limit": {"tif": "Ioc"}},
                reduce_only=False,
            )

            # Parse SDK response
            if result and result.get("status") == "ok":
                statuses = (
                    result.get("response", {})
                    .get("data", {})
                    .get("statuses", [])
                )
                if statuses:
                    st = statuses[0]
                    if "filled" in st:
                        fill = st["filled"]
                        avg = float(fill.get("avgPx", 0))
                        tsz = float(fill.get("totalSz", 0))
                        oid = fill.get("oid", 0)
                        self._trade_timestamps.append(now)
                        self._positions_ts = 0.0
                        logger.success(
                            f"FILLED: {side} {tsz} {coin} @ ${self._fmt_price(avg)} "
                            f"(oid={oid})"
                        )
                        return TradeResult(True, coin, side, abs_size, tsz, avg, oid)

                    if "resting" in st:
                        oid = st["resting"].get("oid", 0)
                        self._trade_timestamps.append(now)
                        self._positions_ts = 0.0
                        logger.warning(
                            f"Order resting (unexpected for IOC): oid={oid}"
                        )
                        return TradeResult(True, coin, side, abs_size, 0, 0, oid)

                    if "error" in st:
                        err = st["error"]
                        logger.error(f"Order rejected: {err}")
                        return TradeResult(False, coin, side, abs_size, error=err)

            logger.error(f"Unexpected order response: {result}")
            return TradeResult(False, coin, side, abs_size, error=str(result))

        except Exception as e:
            logger.error(f"Execution exception: {e}")
            return TradeResult(False, coin, side, abs_size, error=str(e))

    # -- Internal helpers -------------------------------------------

    def _slippage_ioc_price(self, coin: str, is_buy: bool, mid: float) -> float:
        """
        Price an IOC order using HyperLiquid's perp rounding constraints:
        5 significant figures and <= (6 - szDecimals) decimals.
        """
        slip = self.config.slippage_bps / 10_000
        px = mid * (1 + slip) if is_buy else mid * (1 - slip)
        px = float(f"{px:.5g}")
        max_decimals = max(0, 6 - int(self._sz_decimals.get(coin, 5)))
        return round(px, max_decimals)

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return "429" in text or "too many requests" in text

    def _build_info_with_retry(self, base_url: str) -> Info:
        """Retry Info client init to survive transient 429 responses."""
        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                return Info(base_url, skip_ws=True)
            except Exception as e:
                last_exc = e
                if not self._is_rate_limit_error(e) or attempt == 3:
                    break
                logger.warning(
                    f"Info init rate-limited (attempt {attempt}/3). "
                    f"Retrying in {delay:.1f}s"
                )
                time.sleep(delay)
                delay = min(delay * 2, 4.0)
        if last_exc:
            raise last_exc
        raise RuntimeError("Info init failed without an exception")

    @staticmethod
    def _fmt_price(price: float) -> str:
        """Render prices with enough precision for sub-$1 perps."""
        if price >= 100:
            return f"{price:,.1f}"
        if price >= 1:
            return f"{price:,.3f}"
        if price >= 0.1:
            return f"{price:,.4f}"
        if price >= 0.01:
            return f"{price:,.5f}"
        return f"{price:,.6f}"

    def _set_leverage(self, coin: str) -> None:
        """Set leverage for a coin. Failures are non-fatal."""
        try:
            self.exchange.update_leverage(
                self.config.leverage, coin, is_cross=self.config.is_cross,
            )
            mode = "cross" if self.config.is_cross else "isolated"
            logger.info(f"Leverage set: {coin} {self.config.leverage}x ({mode})")
        except Exception as e:
            logger.warning(f"Could not set leverage for {coin}: {e} (may already be set)")
