import time
import math
from datetime import datetime, timedelta, timezone
import json
from crypto_common.exchange_coincheck import config
from crypto_common.exchange_coincheck import CoincheckApi, CoincheckBusinessError
from crypto_common.monitoring.context import send_email_from_config
from crypto_common.monitoring.logger import PrefixedLogger
from crypto_common.datatime.formatting import format_duration
from crypto_common.trading_utils import calculate_buy_quantity, BalanceMonitor

DEFAULT_LOG_PREFIX = "[coincheck]"
POST_ONLY_TIME_IN_FORCE = "post_only"  # Coincheck API does not expose SOK/FAS style values.


class CoincheckTrader:
    def __init__(
        self,
        trade_amount_jpy_target,
        target_cycle_profit,
        logger,
        sell_cooldown_seconds,
        sell_timeout_seconds,
        buy_timeout_seconds,
        default_wait_seconds,
        balance_threshold_ratio,
        log_prefix=None,
    ):
        logger_prefix = getattr(logger, "_prefix", None) if isinstance(logger, PrefixedLogger) else None
        if log_prefix is not None:
            self.log_prefix = str(log_prefix)
        elif isinstance(logger_prefix, str) and logger_prefix.strip():
            self.log_prefix = logger_prefix
        else:
            self.log_prefix = DEFAULT_LOG_PREFIX

        self.logger = logger if isinstance(logger, PrefixedLogger) else PrefixedLogger(logger, self.log_prefix)

        # Unified wait/timeout setting
        wait_sec = max(1, int(default_wait_seconds))
        self.api_timeout_seconds = wait_sec
        self.order_status_poll_interval_seconds = wait_sec
        self.order_status_retry_sleep_seconds = wait_sec
        self.market_data_retry_sleep_seconds = wait_sec
        self.buy_order_retry_sleep_seconds = wait_sec
        self.sell_order_retry_sleep_seconds = wait_sec
        self.critical_error_sleep_seconds = wait_sec

        self.api = CoincheckApi(
            api_key=config.COINCHECK_API_KEY,
            secret_key=config.COINCHECK_API_SECRET,
            public_url=config.PUBLIC_API_URL,
            private_url=config.PRIVATE_API_URL,
            timeout=self.api_timeout_seconds,
            logger=self.logger,
        )
        self.symbol = config.SYMBOL
        self.min_qty = config.MIN_QUANTITY
        self.buy_qty_decimal_places = config.QTY_DECIMAL_PLACES
        self.trade_amount_jpy_target = trade_amount_jpy_target
        self.target_cycle_profit = float(target_cycle_profit)
        self.sell_cooldown_seconds = max(0, int(sell_cooldown_seconds))
        self.sell_timeout_seconds = max(0, int(sell_timeout_seconds))
        self.buy_timeout_seconds = max(0, int(buy_timeout_seconds))

        self.balance_threshold_ratio = float(balance_threshold_ratio)
        self.balance_monitor = BalanceMonitor(self.logger, config, self.log_prefix)

    def _fetch_order_snapshot(self, order_id, context):
        try:
            return self.api.get_orders(order_id)
        except CoincheckBusinessError as e:
            self.logger.warning(f"{context} failed for order {order_id}: {e}")
        except Exception as e:
            self.logger.warning(f"{context} failed for order {order_id}: {e}")
        return None

    def _send_email_alert(self, subject, body, high_priority=False):
        decorated_subject = f"[HIGH] {subject}" if high_priority else subject
        email_subject = self._format_email_subject(decorated_subject)
        try:
            send_email_from_config(email_subject, body, config)
        except Exception as e:
            self.logger.error(f"Failed to send alert email: {e}")

    def _is_in_maintenance_window(self):
        now_jst = datetime.now(timezone(timedelta(hours=9)))
        if now_jst.weekday() != config.MAINTENANCE_WEEKDAY_JST:
            return False

        start_hour, start_minute = config.MAINTENANCE_START_JST
        end_hour, end_minute = config.MAINTENANCE_END_JST
        now_minutes = now_jst.hour * 60 + now_jst.minute
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute
        return start_minutes <= now_minutes < end_minutes

    def _sleep_if_maintenance_window(self):
        if not self._is_in_maintenance_window():
            return False

        start_hour, start_minute = config.MAINTENANCE_START_JST
        end_hour, end_minute = config.MAINTENANCE_END_JST
        sleep_seconds = self.sell_cooldown_seconds if self.sell_cooldown_seconds > 0 else 1
        self.logger.info(
            f"Detected Coincheck maintenance window (JST Sat "
            f"{start_hour:02d}:{start_minute:02d}-{end_hour:02d}:{end_minute:02d}). "
            f"Sleeping for {sleep_seconds} seconds."
        )
        time.sleep(sleep_seconds)
        return True

    def _get_available_buying_power(self):
        try:
            buying_power = self.api.get_available_margin_amount()
        except Exception as e:
            self.logger.warning(
                f"Error checking margin availableAmount: {e}. Proceeding without balance check."
            )
            return None

        if buying_power is None:
            self.logger.warning("Margin data unavailable or parse failed. Proceeding without balance check.")
            return None

        return buying_power

    def _format_email_subject(self, subject):
        prefix = self.log_prefix
        cleaned = subject.strip() if subject else ""
        if cleaned.lower().startswith(prefix):
            return cleaned
        if cleaned:
            return f"{prefix} {cleaned}"
        return prefix

    def _send_business_error_alert(self, context, error_exception):
        error_msg = str(error_exception)
        full_msg = f"{context}: {error_msg}\n\n"

        request_info = getattr(error_exception, "request_info", None)
        if request_info:
            full_msg += f"--- Request Info ---\n{json.dumps(request_info, indent=2, ensure_ascii=False)}\n\n"

        response = getattr(error_exception, "response", None)
        if response:
            full_msg += f"--- Response Info ---\n{json.dumps(response, indent=2, ensure_ascii=False)}\n\n"

        self._send_email_alert(f"API Business Error - {context}", full_msg)

    def _get_best_prices(self):
        try:
            return self.api.get_best_bid_ask(self.symbol)
        except Exception as e:
            self.logger.error(f"Failed to fetch best bid/ask: {e}")
            return None, None

    def _calc_buy_qty(self, price):
        """
        Qty = (target_jpy / price) -> round up to configured decimal places.
        """
        return calculate_buy_quantity(
            self.trade_amount_jpy_target,
            price,
            decimal_places=self.buy_qty_decimal_places,
            min_qty=self.min_qty,
        )

    def _calculate_dynamic_spread(self, qty):
        """
        Calculates the spread required to achieve the target cycle profit for the given quantity.
        Spread = Target Profit / Quantity
        Result is rounded up to ensure minimum profit (or at least rounded to integer).
        """
        if qty <= 0:
            self.logger.warning(f"[_calculate_dynamic_spread] Received invalid quantity: {qty}. Returning 0.")
            return 0

        # Calculate raw spread needed to get the target profit
        raw_spread = self.target_cycle_profit / qty

        # Let's use math.ceil to guarantee at least the target profit.
        spread = int(math.ceil(raw_spread))

        self.logger.info(
            f"TargetProfit={self.target_cycle_profit} / Qty={qty}, RawSpread={raw_spread:.2f}. "
            f"Rounding UP to {spread}"
        )

        return spread

    def _get_active_order_prices(self):
        """
        Returns (active_buy_prices, active_sell_prices) as lists of float.
        Returns (None, None) when active order query cannot be trusted.
        """
        try:
            return self.api.get_active_order_prices(symbol=self.symbol)
        except CoincheckBusinessError as e:
            msg = f"[BUY ] Failed to fetch active orders: {e}"
            self.logger.warning(msg)
            self._send_business_error_alert("GetActiveOrders", e)
            return None, None

    def _has_conflicting_active_sell(self, target_buy_price, planned_buy_qty, active_sell_prices=None):
        """
        Validates if placing a buy at `target_buy_price` is safe relative to existing shell orders.
        Rule: The distance between the lowest active SELL price and the current `target_buy_price`
        must be GREATER than 2 * projected_spread.

        This ensures we don't buy too close to existing positions, enforcing a wider grid
        when averaging down or re-entering.

        Returns True if conflict exists (do not buy).
        """
        if active_sell_prices is None:
            _, active_sell_prices = self._get_active_order_prices()
        if active_sell_prices is None:
            # Fail-safe: if we cannot verify, don't place BUY now.
            self.logger.warning(
                f"[BUY ] Active SELL check unavailable. "
                f"TargetBUY={target_buy_price}. Skipping BUY for safety."
            )
            return True

        if not active_sell_prices:
            self.logger.info(
                f"[BUY ] Active SELL check passed: no active SELL orders. TargetBUY={target_buy_price}"
            )
            return False

        # Calculate our projected spread
        projected_spread = self._calculate_dynamic_spread(planned_buy_qty)
        min_active_sell_price = min(active_sell_prices)

        # Calculate distance and requirement
        price_distance = min_active_sell_price - target_buy_price
        required_distance = 2 * projected_spread

        self.logger.info(
            f"Active SELL conflict check: MinActiveSell={min_active_sell_price}, TargetBuy={target_buy_price}"
        )

        self.logger.info(
            f"Distance={price_distance}, RequiredDistance={required_distance} (2 * Spread {projected_spread})"
        )
        # Conflict if distance is NOT greater than required (i.e. <=)
        if price_distance <= required_distance:
             self.logger.info(
                f"[BUY ] Skipping BUY due to insufficient price distance. "
                f"Distance ({price_distance}) <= Required ({required_distance}). "
            )
             self.logger.info(
                f"Calculated Sell would be too close to existing orders."
            )

             return True

        self.logger.info("Active SELL check passed: sufficient distance found.")
        return False

    def _has_conflicting_active_buy(self, target_buy_price, active_buy_prices=None):
        """
        Rule: when active BUY orders exist, a new BUY price must not be below them.
        Returns True if conflict exists (do not buy).
        """
        if active_buy_prices is None:
            active_buy_prices, _ = self._get_active_order_prices()

        if active_buy_prices is None:
            self.logger.warning(
                f"[BUY ] Active BUY check unavailable. "
                f"TargetBUY={target_buy_price}. Skipping BUY for safety."
            )
            return True

        if not active_buy_prices:
            self.logger.info(
                f"[BUY ] Active BUY check passed: no active BUY orders. TargetBUY={target_buy_price}"
            )
            return False

        max_active_buy_price = max(active_buy_prices)
        self.logger.info(
            f"Active BUY conflict check: MaxActiveBuy={max_active_buy_price}, TargetBuy={target_buy_price}"
        )

        if target_buy_price < max_active_buy_price:
            self.logger.info(
                f"[BUY ] Skipping BUY because target price is below an existing active BUY. "
                f"TargetBuy={target_buy_price}, MaxActiveBuy={max_active_buy_price}"
            )
            return True

        self.logger.info("Active BUY check passed: target is not below existing BUY orders.")
        return False

    def _should_extend_buy_timeout(self, target_buy_price):
        best_bid, _ = self._get_best_prices()
        if best_bid is not None and target_buy_price == best_bid:
            self.logger.info(
                f"[BUY ] Timeout but price condition met (MyPrice: {target_buy_price}, BestBid: {best_bid}). Continuing wait..."
            )
            return True
        self.logger.info(
            f"[BUY ] Timeout and price condition failed (MyPrice: {target_buy_price}, BestBid: {best_bid})."
        )
        return False

    def _wait_for_fill(
        self,
        order_id,
        phase_name,
        timeout_seconds,
        email_subject=None,
        email_body=None,
        notify_on_timeout=True,
        should_extend_timeout=None,
    ):
        """
        Monitors an order until filled or timeout.
        Returns: (success_bool, executed_qty)
        """
        start_time = time.time()
        self.logger.info(f"[{phase_name}] Monitoring Order ID: {order_id}")

        while True:
            if self._sleep_if_maintenance_window():
                continue

            # Check Timeout
            if time.time() - start_time > timeout_seconds:
                self.logger.info(f"[{phase_name}] Order {order_id} timed out (> {timeout_seconds}s).")

                if should_extend_timeout and should_extend_timeout():
                    time.sleep(self.order_status_poll_interval_seconds)
                    continue

                if notify_on_timeout:
                    subj = email_subject if email_subject else f"{phase_name} Order Timeout"
                    body = email_body if email_body else f"{phase_name} Order ID {order_id} not filled in {timeout_seconds}s."
                    send_email_from_config(self._format_email_subject(subj), body, config)
                return False, 0.0

            # Check Status
            try:
                resp = self.api.get_orders(order_id)
            except CoincheckBusinessError as e:
                msg = f"[{phase_name}] Error checking order {order_id}: {e}. Retrying..."
                self.logger.warning(msg)
                self._send_business_error_alert(f"{phase_name.strip()} CheckStatus", e)
                time.sleep(self.order_status_retry_sleep_seconds)
                continue

            order_data = self.api.extract_first_order(resp)
            if not order_data:
                self.logger.warning(f"[{phase_name}] Could not fetch order {order_id}. Retrying...")
                time.sleep(self.order_status_retry_sleep_seconds)
                continue

            status = str(order_data.get('status') or "").upper()
            executed_size = self.api.extract_executed_size(resp)

            # Statuses: "ORDERED", "MODIFIED", "CANCELED", "EXECUTED", "EXPIRED"
            if status == "EXECUTED":
                self.logger.info(f"[{phase_name}] Order {order_id} FILLED.")
                return True, executed_size

            elif status in ["CANCELED", "EXPIRED"]:
                self.logger.warning(f"[{phase_name}] Order {order_id} was {status}.")
                return False, executed_size

            current_age = time.time() - start_time
            age_str = format_duration(current_age)
            order_side = str(order_data.get("side") or "N/A").upper()
            order_side_fixed = f"{order_side:>4}" if len(order_side) <= 4 else order_side
            self.logger.info(
                f"[{phase_name}] Order {order_id} status: {status}. Side: {order_side_fixed}, "
                f"Price: {order_data.get('price')}, Age: {age_str}. Waiting"
            )
            time.sleep(self.order_status_poll_interval_seconds)

    def _run_quick_sell_after_partial_buy_cancel(self, buy_order_id, executed_size, executed_price):
        if executed_size <= 0:
            return False

        _, best_ask = self._get_best_prices()
        target_candidates = []
        if best_ask is not None:
            target_candidates.append(int(math.ceil(best_ask)))
        if executed_price is not None:
            target_candidates.append(int(math.ceil(executed_price + 1)))

        if not target_candidates:
            self.logger.error(
                f"[QSELL] Cannot determine quick-sell price for BUY {buy_order_id}. "
                f"ExecutedSize={executed_size}, ExecutedPrice={executed_price}, BestAsk={best_ask}"
            )
            return False

        quick_sell_price = max(target_candidates)
        self.logger.warning(
            f"[QSELL] BUY {buy_order_id} partially filled then canceled. "
            f"ExecutedSize={executed_size}, ExecutedPrice={executed_price}, BestAsk={best_ask}. "
            f"Placing quick SELL at {quick_sell_price}."
        )

        try:
            quick_sell_order_id = self.api.place_order(
                symbol=self.symbol,
                side="SELL",
                price=int(quick_sell_price),
                size=executed_size,
                time_in_force=POST_ONLY_TIME_IN_FORCE,
            )
        except CoincheckBusinessError as e:
            msg = f"[QSELL] Failed to place quick SELL for BUY {buy_order_id}: {e}"
            self.logger.error(msg)
            self._send_business_error_alert("QuickSellAfterPartialBuyCancel", e)
            return False

        if not quick_sell_order_id:
            self.logger.error(f"[QSELL] Empty response while placing quick SELL for BUY {buy_order_id}.")
            return False

        s_success, _ = self._wait_for_fill(
            quick_sell_order_id,
            "QSELL",
            timeout_seconds=self.sell_timeout_seconds,
            notify_on_timeout=False,
        )

        if s_success:
            self.logger.info(f"[QSELL] Quick SELL filled for BUY {buy_order_id}.")

            if executed_price is not None:
                quick_profit = (quick_sell_price - executed_price) * executed_size
                self.logger.info(f"[QSELL] Realized quick-sell P/L: {quick_profit:.2f} JPY")
            return True

        self.logger.info(
            f"[QSELL] Quick SELL {quick_sell_order_id} not filled within "
            f"{format_duration(self.sell_timeout_seconds)}. Keeping it on book."
        )
        return True

    def _run_buy_phase(self):
        while True:
            self.logger.info("=== Starting BUY Phase ===")

            if self._sleep_if_maintenance_window():
                continue

            # 1. Get Market Data
            best_bid, best_ask = self._get_best_prices()
            if not best_bid or not best_ask:
                self.logger.warning("Failed to get market data. Retrying...")
                time.sleep(self.market_data_retry_sleep_seconds)
                continue

            # 2. Calc Price
            target_buy_price = best_bid

            # 3. Calc Qty
            buy_qty = self._calc_buy_qty(target_buy_price)
            self.logger.info(f"Preparing BUY: Price={target_buy_price}, Qty={buy_qty}")

            active_buy_prices, active_sell_prices = self._get_active_order_prices()
            if active_buy_prices is None or active_sell_prices is None:
                if self.sell_cooldown_seconds:
                    self.logger.info(
                        f"[BUY ] Active-order checks unavailable. Cooling down for {self.sell_cooldown_seconds} seconds."
                    )
                    time.sleep(self.sell_cooldown_seconds)
                continue

            # Check existing active SELL orders before placing BUY.
            if self._has_conflicting_active_sell(
                target_buy_price,
                buy_qty,
                active_sell_prices=active_sell_prices,
            ):
                if self.sell_cooldown_seconds:
                    self.logger.info(
                        f"[BUY ] Conflict detected. Cooling down for {self.sell_cooldown_seconds} seconds."
                    )
                    time.sleep(self.sell_cooldown_seconds)
                continue

            if self._has_conflicting_active_buy(
                target_buy_price,
                active_buy_prices=active_buy_prices,
            ):
                if self.sell_cooldown_seconds:
                    self.logger.info(
                        f"[BUY ] Conflict detected. Cooling down for {self.sell_cooldown_seconds} seconds."
                    )
                    time.sleep(self.sell_cooldown_seconds)
                continue


            # Check available buying power from account margin.
            buying_power = self._get_available_buying_power()
            if buying_power is not None:
                estimated_cost = target_buy_price * buy_qty
                self.logger.info(
                    f"Available Buying Power: {buying_power}, "
                    f"Estimated Cost: {estimated_cost}"
                )
                if not self.balance_monitor.check_and_alert(
                    buying_power,
                    estimated_cost,
                    threshold_ratio=self.balance_threshold_ratio,
                    sleep_on_fail=True,
                ):
                    # After sleep, retry BUY phase from scratch.
                    continue

            # 4. Place Order
            try:
                order_resp = self.api.place_order(
                    symbol=self.symbol,
                    side="BUY",
                    price=int(target_buy_price),
                    size=buy_qty,
                    time_in_force=POST_ONLY_TIME_IN_FORCE,
                )
            except CoincheckBusinessError as e:
                msg = f"Buy Order Failed with Business Error: {e}"
                self.logger.error(msg)
                self._send_business_error_alert("BuyOrderPlace", e)
                time.sleep(self.buy_order_retry_sleep_seconds)
                continue

            if not order_resp:
                self.logger.error("Buy order placement failed. Retrying BUY phase...")
                time.sleep(self.buy_order_retry_sleep_seconds)
                continue

            buy_order_id = order_resp

            # 5. Monitor (configured timeout, conditional wait on best bid)
            success, filled_size = self._wait_for_fill(
                buy_order_id,
                "BUY ",
                timeout_seconds=self.buy_timeout_seconds,
                notify_on_timeout=False,
                should_extend_timeout=lambda: self._should_extend_buy_timeout(target_buy_price),
            )

            if not success:
                # Timeout or canceled/expired -> try to cancel remainder, then resolve final state.
                cancel_failed = False
                try:
                    self.api.cancel_order(buy_order_id)
                    self.logger.info(f"Buy order {buy_order_id} canceled successfully after timeout.")
                except CoincheckBusinessError as e:
                    # ERR-5122 often means it was already EXECUTED/CANCELED just before cancel.
                    cancel_failed = True
                    self.logger.warning(f"Failed to cancel buy order {buy_order_id}: {e}")
                except Exception as e:
                    cancel_failed = True
                    self.logger.warning(f"Unexpected error when canceling buy order {buy_order_id}: {e}")

                final_status = ""
                final_executed_size = filled_size
                final_executed_price = target_buy_price

                final_snapshot = self._fetch_order_snapshot(
                    buy_order_id,
                    "Fetch BUY snapshot after cancel attempt",
                )
                if final_snapshot:
                    final_status = str(self.api.extract_order_status(final_snapshot) or "").upper()
                    final_executed_size = self.api.extract_executed_size(final_snapshot)
                    extracted_price = self.api.extract_executed_price(final_snapshot)
                    if extracted_price is not None:
                        final_executed_price = extracted_price

                if final_status == "EXECUTED":
                    if cancel_failed:
                        self.logger.info(
                            f"[Race Condition Detected] Order {buy_order_id} failed to cancel because it is EXECUTED. "
                            "Proceeding to SELL phase."
                        )
                    filled_size = final_executed_size
                elif (
                    final_executed_size > 0
                    and (
                        final_status in ("CANCELED", "EXPIRED")
                        or (not final_status and filled_size > 0)
                    )
                ):
                    self.logger.warning(
                        f"[BUY ] Order {buy_order_id} partially filled after cancel. "
                        f"Status={final_status or 'UNKNOWN'}, ExecutedSize={final_executed_size}, "
                        f"ExecutedPrice={final_executed_price}"
                    )
                    quick_sell_started = self._run_quick_sell_after_partial_buy_cancel(
                        buy_order_id,
                        final_executed_size,
                        final_executed_price,
                    )
                    if quick_sell_started:
                        # Quick-sell path is handled independently; start next BUY cycle.
                        continue

                if final_status != "EXECUTED":
                    self.logger.info(
                        f"Buy order canceled or verified not filled (Timeout {format_duration(self.buy_timeout_seconds)}). "
                        "Restarting BUY phase."
                    )
                    continue

            # Use the actual limit price as basis since strategy is maker-only.
            final_buy_price = target_buy_price
            self.logger.info(f"BUY Complete. Price: {final_buy_price}, Qty: {filled_size}")
            return buy_order_id, final_buy_price, filled_size

    def _run_sell_phase(self, buy_order_id, final_buy_price, filled_size):
        self.logger.info("=== Starting SELL Phase ===")

        # Calculate dynamic spread based on actual filled size.
        sell_spread = self._calculate_dynamic_spread(filled_size)
        sell_target_price = final_buy_price + sell_spread
        sell_qty = filled_size
        self.logger.info(
            f"Calculated Dynamic Spread: {sell_spread} for Qty: {filled_size} "
            f"(Target Profit: {self.target_cycle_profit})"
        )

        while True:
            if self._sleep_if_maintenance_window():
                continue

            self.logger.info(f"Placing SELL: Price={sell_target_price}, Qty={sell_qty}")

            try:
                sell_resp = self.api.place_order(
                    symbol=self.symbol,
                    side="SELL",
                    price=int(sell_target_price),
                    size=sell_qty,
                    time_in_force=POST_ONLY_TIME_IN_FORCE,
                )
            except CoincheckBusinessError as e:
                msg = f"Sell Order Failed with Business Error: {e}"
                self.logger.error(msg)
                self._send_business_error_alert("SellOrderPlace", e)
                time.sleep(self.sell_order_retry_sleep_seconds)
                continue

            if not sell_resp:
                self.logger.warning(
                    f"Sell order placement failed. Waiting "
                    f"{format_duration(self.sell_order_retry_sleep_seconds)}..."
                )
                time.sleep(self.sell_order_retry_sleep_seconds)
                continue

            sell_order_id = sell_resp

            # Monitor sell order.
            s_success, _ = self._wait_for_fill(
                sell_order_id,
                "SELL",
                timeout_seconds=self.sell_timeout_seconds,
                notify_on_timeout=False,
            )

            if s_success:
                self.logger.info("SELL Complete. Cycle Finished.")
                profit = (sell_target_price - final_buy_price) * sell_qty
                self.logger.info(f"Cycle Profit: {profit:.2f} JPY")
                self.logger.info("Proceeding immediately to next BUY cycle.")
                return

            # Timeout: keep order on book and continue with a new BUY cycle.
            msg = (
                f"Sell Order Timeout: {sell_order_id} - Not filled in "
                f"{format_duration(self.sell_timeout_seconds)}. "
                "Will keep it on order book and start next buy."
            )
            self.logger.info(msg)
            return

    def run(self):
        self.logger.info(f"=============================")
        self.logger.info(f"=============================")
        self.logger.info(f"Starting Coincheck Trading Bot... [Target Cycle Profit: {self.target_cycle_profit} JPY]")

        while True:
            try:
                if self._sleep_if_maintenance_window():
                    continue

                buy_order_id, final_buy_price, filled_size = self._run_buy_phase()
                self._run_sell_phase(buy_order_id, final_buy_price, filled_size)

            except Exception as e:
                self.logger.error(f"Critical Loop Error: {e}")
                time.sleep(self.critical_error_sleep_seconds)
