#!/usr/bin/env python

from trading import CoincheckTrader

# pyright: ignore[reportMissingImports]
from crypto_common.exchange_coincheck import config
from crypto_common.monitoring.context import setup_logger_from_config
from crypto_common.monitoring.logger import PrefixedLogger
import sys

base_logger = setup_logger_from_config(
    config=config,
    name="coincheck_trader",
    log_file="trading.log",
)
logger = PrefixedLogger(base_logger, "[coincheck]")  # log前缀

BASE_ORDER_SIZE            = 0.001   # 基础买入数量（BTC）
BASE_CYCLE_PROFIT          = 8.1     # 基础单次循环利润（JPY，动态价差计算基准）
# 以 0.001 BTC 下单时，完成盈利循环所需理论最小价差约为 8100 JPY。

DEFAULT_WAIT_SECONDS       = 10      # 默认等待时间（秒）：用于API超时、轮询间隔、重试等待等
COOLDOWN_SECONDS           = 60      # 通用冷却时间（秒）
BUY_ORDER_TIMEOUT_SECONDS  = 60      # 买单超时（秒）
SELL_ORDER_TIMEOUT_SECONDS = 30      # 卖单超时（秒）
RECENT_ORDER_COUNT_WINDOW  = 60 * 60 # 近期开仓 SELL 统计窗口（秒）
RECENT_ORDER_THRESHOLD     = 4       # 近期开仓 SELL 阈值（达到阈值则暂停新 BUY）

BALANCE_THRESHOLD_RATIO    = 1.1     # 余额阈值倍率（估算成本 * 倍率）

def main():
    try:
        trader = CoincheckTrader(
            logger                  = logger,

            base_order_size         = BASE_ORDER_SIZE,
            target_cycle_profit     = BASE_CYCLE_PROFIT,

            default_wait_seconds    = DEFAULT_WAIT_SECONDS,
            sell_cooldown_seconds   = COOLDOWN_SECONDS,
            buy_timeout_seconds     = BUY_ORDER_TIMEOUT_SECONDS,
            sell_timeout_seconds    = SELL_ORDER_TIMEOUT_SECONDS,

            balance_threshold_ratio = BALANCE_THRESHOLD_RATIO,
            recent_order_count_window_seconds = RECENT_ORDER_COUNT_WINDOW,
            recent_order_threshold = RECENT_ORDER_THRESHOLD,
        )
        trader.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Bot crashed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
