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

TRADE_AMOUNT_JPY_TARGET    = 10_000  # 买入目标金额（日元）
TARGET_CYCLE_PROFIT        = 8.1     # 目标单次循环利润（JPY，动态价差计算基准）
# 以上两个数值也实际上定义了每次盈利所需要的价格变化区间 价格需要变化3.1/10000   0.031%
# 完成盈利循环需要的价差 RawSpread=3263.16.

DEFAULT_WAIT_SECONDS       = 10      # 默认等待时间（秒）：用于API超时、轮询间隔、重试等待等
COOLDOWN_SECONDS           = 60      # 通用冷却时间（秒）
BUY_ORDER_TIMEOUT_SECONDS  = 60      # 买单超时（秒）
SELL_ORDER_TIMEOUT_SECONDS = 30      # 卖单超时（秒）

BALANCE_THRESHOLD_RATIO    = 1.1     # 余额阈值倍率（估算成本 * 倍率）

def main():
    try:
        trader = CoincheckTrader(
            logger                  = logger,

            trade_amount_jpy_target = TRADE_AMOUNT_JPY_TARGET,
            target_cycle_profit     = TARGET_CYCLE_PROFIT,

            default_wait_seconds    = DEFAULT_WAIT_SECONDS,
            sell_cooldown_seconds   = COOLDOWN_SECONDS,
            buy_timeout_seconds     = BUY_ORDER_TIMEOUT_SECONDS,
            sell_timeout_seconds    = SELL_ORDER_TIMEOUT_SECONDS,

            balance_threshold_ratio = BALANCE_THRESHOLD_RATIO,
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
