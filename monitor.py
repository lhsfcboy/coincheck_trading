#!/usr/bin/env python3

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

try:
    from crypto_common.exchange_coincheck import config
    from crypto_common.exchange_coincheck.coincheck_api import CoincheckApi, CoincheckBusinessError
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root / "crypto_common"))
    from crypto_common.exchange_coincheck import config
    from crypto_common.exchange_coincheck.coincheck_api import CoincheckApi, CoincheckBusinessError


KEY_WIDTH = 20
VALUE_WIDTH = 14
ACTIVE_ORDER_PAGE_SIZE = 100
MAX_ACTIVE_ORDER_PAGES = 30
MAX_INTERVAL_DIGITS = 6
INLINE_INTERVAL_WIDTH = MAX_INTERVAL_DIGITS + 1  # e.g. 99_9999
MAX_ORDER_AGE_SECONDS = (99 * 60 * 60) + (59 * 60) + 59
DEFAULT_SYMBOL = config.SYMBOL
DEFAULT_REFRESH_SECONDS = 5.0
DEFAULT_SELL_DEPTH = 20
DEFAULT_BUY_DEPTH = 5
DEFAULT_TIMEOUT_SECONDS = 10.0


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(round(value))
        text = str(value).strip()
        if not text:
            return None
        return int(round(float(text)))
    except (TypeError, ValueError):
        return None


def _format_grouped(value: int | None) -> str:
    if value is None:
        return "N/A"

    sign = "-" if value < 0 else ""
    digits = str(abs(int(value)))
    groups: list[str] = []
    while digits:
        groups.append(digits[-4:])
        digits = digits[:-4]
    return sign + "_".join(reversed(groups))


def _format_interval(value: int | None) -> str:
    if value is None:
        return "(N/A)"
    return f"({_format_grouped(value)})"


def _format_line(key: str, value: str) -> str:
    return f"{key:<{KEY_WIDTH}}{value:>{VALUE_WIDTH}}"


def _format_price_with_inline_interval(price: int | None, interval: int | None) -> str:
    price_text = _format_grouped(price)
    interval_text = _format_grouped(interval)
    return f"{price_text:>{VALUE_WIDTH}} ({interval_text:>{INLINE_INTERVAL_WIDTH}})"


def _parse_order_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_order_age(order_time: datetime | None, now_utc: datetime | None = None) -> str:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    if order_time is None:
        age_seconds = 0
    else:
        if order_time.tzinfo is None:
            order_time_utc = order_time.replace(tzinfo=timezone.utc)
        else:
            order_time_utc = order_time.astimezone(timezone.utc)
        age_seconds = int((now_utc - order_time_utc).total_seconds())
        if age_seconds < 0:
            age_seconds = 0

    age_seconds = min(age_seconds, MAX_ORDER_AGE_SECONDS)
    hours, remainder = divmod(age_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _jst_now_string() -> str:
    if ZoneInfo is not None:
        now = datetime.now(ZoneInfo("Asia/Tokyo"))
    else:
        now = datetime.now(timezone(timedelta(hours=9)))
    return now.strftime("%Y-%m-%d_%H:%M:%S")


class CoincheckMonitor:
    def __init__(
        self,
        api: CoincheckApi,
        symbol: str,
        sell_depth: int,
        buy_depth: int,
        refresh_seconds: float,
        clear_screen: bool,
    ) -> None:
        self.api = api
        self.symbol = symbol
        self.sell_depth = sell_depth
        self.buy_depth = buy_depth
        self.refresh_seconds = refresh_seconds
        self.clear_screen = clear_screen
        self._stopped = False

    def stop(self, *_: Any) -> None:
        self._stopped = True

    def _fetch_latest_price(self) -> int | None:
        ticker = self.api.get_ticker(self.symbol)
        if isinstance(ticker, list) and ticker:
            return _parse_int(ticker[0].get("last"))
        if isinstance(ticker, dict):
            return _parse_int(ticker.get("last"))
        return None

    def _fetch_buy_power(self) -> int | None:
        return _parse_int(self.api.get_available_margin_amount())

    def _fetch_board_best_prices(self) -> tuple[int | None, int | None]:
        best_bid_raw, best_ask_raw = self.api.get_best_bid_ask(self.symbol)
        best_ask = _parse_int(best_ask_raw)
        best_bid = _parse_int(best_bid_raw)
        return best_ask, best_bid

    def _fetch_all_active_orders(self) -> list[dict[str, Any]]:
        all_orders = self.api.get_active_orders_paginated(
            symbol=self.symbol,
            count=ACTIVE_ORDER_PAGE_SIZE,
            max_pages=MAX_ACTIVE_ORDER_PAGES,
        )
        if all_orders is None:
            raise RuntimeError("activeOrders request failed")
        return all_orders

    def _collect_snapshot(self) -> tuple[dict[str, Any], list[str]]:
        errors: list[str] = []
        snapshot: dict[str, Any] = {
            "latest_refresh_jst": _jst_now_string(),
            "latest_price": None,
            "buy_power": None,
            "board_best_ask": None,
            "board_best_bid": None,
            "sell_orders": [],
            "buy_orders": [],
        }

        try:
            snapshot["latest_price"] = self._fetch_latest_price()
        except Exception as exc:
            errors.append(f"latest_price: {exc}")

        try:
            snapshot["buy_power"] = self._fetch_buy_power()
        except Exception as exc:
            errors.append(f"buy_power: {exc}")

        try:
            board_best_ask, board_best_bid = self._fetch_board_best_prices()
            snapshot["board_best_ask"] = board_best_ask
            snapshot["board_best_bid"] = board_best_bid
        except Exception as exc:
            errors.append(f"orderbooks: {exc}")

        try:
            active_orders = self._fetch_all_active_orders()
            sell_orders: list[tuple[int, datetime | None]] = []
            buy_orders: list[tuple[int, datetime | None]] = []
            for order in active_orders:
                side = str(order.get("side") or "").upper()
                price = _parse_int(order.get("price"))
                if price is None:
                    continue
                order_time = _parse_order_timestamp(order.get("timestamp"))
                if side == "SELL":
                    sell_orders.append((price, order_time))
                elif side == "BUY":
                    buy_orders.append((price, order_time))
            sell_orders.sort(key=lambda item: item[0])
            buy_orders.sort(key=lambda item: item[0], reverse=True)
            snapshot["sell_orders"] = sell_orders[: self.sell_depth]
            snapshot["buy_orders"] = buy_orders[: self.buy_depth]
        except CoincheckBusinessError as exc:
            errors.append(f"activeOrders business error: {exc}")
        except Exception as exc:
            errors.append(f"activeOrders: {exc}")

        return snapshot, errors

    def _render(self, snapshot: dict[str, Any], errors: list[str]) -> str:
        lines: list[str] = []

        # lines.append(_format_line("latest_price:", _format_grouped(snapshot["latest_price"])))
        # lines.append("")

        board_best_ask = snapshot["board_best_ask"]
        board_best_bid = snapshot["board_best_bid"]
        sell_orders: list[tuple[int, datetime | None]] = snapshot["sell_orders"]
        buy_orders: list[tuple[int, datetime | None]] = snapshot["buy_orders"]
        now_utc = datetime.now(timezone.utc)

        if sell_orders:
            for rank in range(len(sell_orders), 0, -1):
                price, order_time = sell_orders[rank - 1]
                if rank > 1:
                    next_closer_price = sell_orders[rank - 2][0]
                    interval = price - next_closer_price
                else:
                    interval = price - board_best_ask if board_best_ask is not None else None
                lines.append(
                    _format_line(
                        f"sell_price{rank}:",
                        f"{_format_price_with_inline_interval(price, interval)} {_format_order_age(order_time, now_utc)}",
                    )
                )
        else:
            lines.append(_format_line("sell_price1:", "N/A"))

        lines.append(_format_line("board_best_ask:", _format_grouped(board_best_ask)))
        board_interval = (
            board_best_ask - board_best_bid
            if board_best_ask is not None and board_best_bid is not None
            else None
        )
        lines.append(_format_line("spread:", _format_interval(board_interval)))
        lines.append(_format_line("board_best_bid:", _format_grouped(board_best_bid)))

        if buy_orders:
            for rank, (price, order_time) in enumerate(buy_orders, start=1):
                if rank == 1:
                    interval = board_best_bid - price if board_best_bid is not None else None
                else:
                    interval = buy_orders[rank - 2][0] - price
                lines.append(
                    _format_line(
                        f"buy_price{rank}:",
                        f"{_format_price_with_inline_interval(price, interval)} {_format_order_age(order_time, now_utc)}",
                    )
                )
        else:
            lines.append(_format_line("buy_price1:", "N/A"))

        # lines.append("")
        if sell_orders and buy_orders:
            locked_range = sell_orders[0][0] - buy_orders[0][0]
            lines.append(_format_line("locked_range:", _format_grouped(locked_range)))
        else:
            lines.append(_format_line("locked_range:", "N/A"))
        lines.append(_format_line("buy_power:", _format_grouped(snapshot["buy_power"])))

        lines.append(_format_line("[COINCHECK]latest_refresh_JST: ", snapshot["latest_refresh_jst"]))

        if errors:
            lines.append("")
            lines.append("errors:")
            for message in errors:
                lines.append(f"- {message}")

        return "\n".join(lines)

    def run(self, once: bool = False) -> int:
        while not self._stopped:
            started_at = time.monotonic()
            snapshot, errors = self._collect_snapshot()
            rendered = self._render(snapshot, errors)

            if self.clear_screen:
                sys.stdout.write("\033[2J\033[H")
            print(rendered, flush=True)
            if not self.clear_screen:
                print("", flush=True)

            if once:
                return 0

            elapsed = time.monotonic() - started_at
            remain = max(0.0, self.refresh_seconds - elapsed)
            until = time.monotonic() + remain
            while not self._stopped and time.monotonic() < until:
                time.sleep(min(0.2, until - time.monotonic()))

        return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coincheck BTC monitor")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help=f"default: {DEFAULT_SYMBOL}")
    parser.add_argument(
        "--refresh-seconds",
        type=_positive_float,
        default=DEFAULT_REFRESH_SECONDS,
        help=f"refresh interval seconds (default: {DEFAULT_REFRESH_SECONDS})",
    )
    parser.add_argument(
        "--sell-depth",
        type=_positive_int,
        default=DEFAULT_SELL_DEPTH,
        help=f"closest sell orders to display (default: {DEFAULT_SELL_DEPTH})",
    )
    parser.add_argument(
        "--buy-depth",
        type=_positive_int,
        default=DEFAULT_BUY_DEPTH,
        help=f"closest buy orders to display (default: {DEFAULT_BUY_DEPTH})",
    )
    parser.add_argument(
        "--no-clear-screen",
        action="store_true",
        help="disable clear screen on each refresh",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run only one refresh cycle",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not config.COINCHECK_API_KEY or not config.COINCHECK_API_SECRET:
        print(
            "Missing Coincheck credentials. Please set COINCHECK_API_KEY and COINCHECK_API_SECRET.",
            file=sys.stderr,
        )
        return 1

    api = CoincheckApi(
        api_key=config.COINCHECK_API_KEY,
        secret_key=config.COINCHECK_API_SECRET,
        public_url=config.PUBLIC_API_URL,
        private_url=config.PRIVATE_API_URL,
        timeout=args.timeout,
    )
    monitor = CoincheckMonitor(
        api=api,
        symbol=args.symbol,
        sell_depth=args.sell_depth,
        buy_depth=args.buy_depth,
        refresh_seconds=args.refresh_seconds,
        clear_screen=not args.no_clear_screen,
    )

    signal.signal(signal.SIGINT, monitor.stop)
    signal.signal(signal.SIGTERM, monitor.stop)
    return monitor.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
