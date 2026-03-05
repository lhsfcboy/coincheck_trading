# Coincheck Trading 项目重建提示词（可直接投喂代码模型）

你是资深 Python 工程师。请从零搭建一个可运行的 Coincheck 现货做市机器人项目。  
要求是仅凭这份提示词即可构建出与当前项目架构一致的代码与文件。

## 1. 目标与边界

1. 语言与运行环境：Python 3.12，Linux（Ubuntu），systemd 管理常驻进程。  
2. 产出两个并列目录：`coincheck_trading` 与 `crypto_common`。  
3. `coincheck_trading` 通过可编辑依赖 `-e ../crypto_common` 引用公共库。  
4. 交易逻辑只实现 BTC 现货限价买卖循环，不做杠杆逻辑。  
5. 不写伪代码，必须给出完整可运行源码。

## 2. 目录结构（必须创建）

```text
coincheck_trading/
  .vscode/
    settings.json
  .envrc
  Makefile
  Prompt.txt
  README.md
  coincheck_trading.service
  requirements.txt
  main.py
  trading.py

crypto_common/
  setup.py
  crypto_common/
    __init__.py
    env.py
    trading_utils.py
    datatime/
      __init__.py
      formatting.py
    monitoring/
      __init__.py
      context.py
      logger.py
      email.py
    exchange_coincheck/
      __init__.py
      config.py
      coincheck_api.py
```

## 3. coincheck_trading 项目要求

### 3.1 `requirements.txt`

内容必须是：

```txt
requests
python-dotenv
-e ../crypto_common
```

### 3.2 `.envrc`

内容必须是：

```bash
source .venv/bin/activate
```

### 3.3 `coincheck_trading.service`

必须包含以下关键字段：

1. `WorkingDirectory=/home/ubuntu/coincheck_trading`
2. `ExecStart=/home/ubuntu/coincheck_trading/.venv/bin/python main.py`
3. `Restart=always`
4. `RestartSec=10`
5. `EnvironmentFile=/home/ubuntu/crypto_common/.env`

### 3.4 `Makefile`

必须提供这些 target（可保持中文提示输出）：

1. `bounce`：`systemctl restart coincheck_trading` 后 `journalctl -u coincheck_trading -f`
2. `stop`：`systemctl stop coincheck_trading`，输出 `is-active`，再输出最近 30 行 journal
3. `install`：`python3 -m pip install -r requirements.txt`
4. `clean`：仅停止 nohup 启动的 `main.py` 进程  
   并保留日志清理命令为注释状态（不要实际截断 `trading.log`）
5. `log`：`tail -f trading.log`
6. `nohup`：后台启动 `main.py` 并写入 `trading.log`

### 3.5 `main.py`

1. 从 `trading` 导入 `CoincheckTrader`。  
2. 从 `crypto_common.exchange_coincheck` 导入 `config`。  
3. 用 `crypto_common.monitoring.context.setup_logger_from_config` 初始化 logger。  
4. 定义并传入以下策略/运行常量（名称必须一致）：

```python
LOG_PREFIX = "[coincheck]"
TRADE_AMOUNT_JPY_TARGET = 10_000
BUY_QTY_DECIMAL_PLACES = 5
BUY_PRICE_OFFSET_JPY = 1
SELL_SPREAD = 3000
SPREAD_THRESHOLD_JPY = 1
BUY_ORDER_TIMEOUT_SECONDS = 3 * 60
SELL_ORDER_TIMEOUT_SECONDS = 3 * 60 * 60
ORDER_STATUS_POLL_INTERVAL_SECONDS = 10
ORDER_STATUS_RETRY_SLEEP_SECONDS = 10
MARKET_DATA_RETRY_SLEEP_SECONDS = 10
BUY_ORDER_RETRY_SLEEP_SECONDS = 10
SELL_ORDER_RETRY_SLEEP_SECONDS = 10
CRITICAL_ERROR_SLEEP_SECONDS = 10
COOLDOWN_SECONDS = 3 * 60
API_TIMEOUT_SECONDS = 10
BALANCE_THRESHOLD_RATIO = 1.1
BALANCE_SLEEP_ON_FAIL = True
BUY_TIME_IN_FORCE = "post_only"
SELL_TIME_IN_FORCE = "post_only"
```

5. `main()` 中实例化 `CoincheckTrader`，把以上参数全部透传。  
6. 捕获 `KeyboardInterrupt` 输出用户停止日志并退出 0。  
7. 捕获其他异常输出 `critical` 并退出 1。

### 3.6 `.vscode/settings.json`

为避免 Pylance 误报导入失败，内容必须包含：

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "python.analysis.extraPaths": [
    "${workspaceFolder}/../crypto_common"
  ]
}
```

### 3.7 `trading.py`

实现 `CoincheckTrader`，结构与行为必须包含以下要点：

1. 初始化要求：
- 接收 `main.py` 传入的全部参数并做基础类型归一化。  
- 创建 `CoincheckApi` 实例，使用 `config.COINCHECK_API_KEY` / `config.COINCHECK_API_SECRET`。  
- 使用 `PrefixedLogger` 包装日志前缀。  
- 保存 `symbol`、`min_qty`、超时参数、重试参数、余额参数。  
- 创建 `BalanceMonitor(self.logger, config, self.log_prefix)`。

2. 维护窗口判断（必须）：
- 增加 `_is_in_maintenance_window()`：JST（UTC+9）每周六 09:00-11:10 返回 `True`。  
- 增加 `_sleep_if_maintenance_window()`：若在窗口内，日志提示并 `sleep(sell_cooldown_seconds)`，若该值为 0 则 sleep 1 秒。  
- 在 `run()` 主循环入口、卖单重试循环、订单监控循环都调用该判断，命中时跳过业务逻辑继续下一轮。

3. 买单阶段要求：
- 读取 orderbook best bid/ask。  
- `candidate_price = best_bid + buy_price_offset_jpy`。  
- 若 `spread > spread_threshold` 用 `candidate_price`，否则用 `best_bid`。  
- 买量通过 `calculate_buy_quantity(target_jpy, price, decimal_places, min_qty)` 计算。  
- 下 BUY 前检查当前未成交 SELL 订单，若 `active_sell_price - sell_spread < target_buy_price`，则不下 BUY，并 `sleep(sell_cooldown_seconds)`。  
- 查询 JPY 可用余额，不足时通过 `BalanceMonitor.check_and_alert` 处理。  
- 下 BUY 限价单（`timeInForce=buy_time_in_force`）。  
- 监控成交超时后，若 `_should_extend_buy_timeout()` 条件满足则继续等待，否则撤单并回到买单阶段。  
- BUY 监控超时默认不发邮件。

4. 卖单阶段要求：
- 卖价 `sell_target_price = final_buy_price + sell_spread`。  
- 卖量为买单实成交量。  
- 下 SELL 限价单（`timeInForce=sell_time_in_force`）。  
- 下单失败按 `sell_order_retry_sleep_seconds` 重试。  
- 卖单监控超时发邮件提醒，但不撤单，直接回到新一轮 BUY。  
- 卖单完全成交后记录利润，并 `sleep(sell_cooldown_seconds)` 再进入下一轮。

5. 订单监控要求：
- `_wait_for_fill(order_id, phase_name, timeout_seconds, ...)` 每隔轮询间隔查询订单状态。  
- 处理 `"EXECUTED"`, `"CANCELED"`, `"EXPIRED"`。  
- 超时时按参数决定是否发送邮件。  
- 查询异常按重试间隔 sleep。

6. 容错要求：
- 任意主循环异常记录 `Critical Loop Error`。  
- 异常后按 `critical_error_sleep_seconds` sleep 再继续。

## 4. crypto_common 公共库要求

### 4.1 `setup.py`

包名 `crypto_common`，版本 `0.1.0`，`find_packages()`，依赖：

1. `requests`
2. `python-dotenv`

### 4.2 `crypto_common/env.py`

实现 `load_env()`，加载优先级必须为：

1. `CRYPTO_COMMON_ENV` 指向的路径  
2. 默认路径 `/home/ubuntu/crypto_common/.env`（通过 `Path(__file__).resolve().parents[1] / ".env"` 计算）  
3. 当前工作目录 `.env`

### 4.3 `crypto_common/exchange_coincheck/config.py`

1. `load_env()` 后读取以下环境变量：  
`COINCHECK_API_KEY`, `COINCHECK_API_SECRET`, `EMAIL_USER`, `EMAIL_PASSWORD`, `EMAIL_TO`, `EMAIL_HOST`, `EMAIL_PORT`。

2. 必须定义以下常量：
- `PUBLIC_API_URL = "https://coincheck.com"`
- `PRIVATE_API_URL = "https://coincheck.com"`
- `SYMBOL = "btc_jpy"`
- `MIN_QUANTITY = 0.001`
- `MAINTENANCE_WEEKDAY_JST = None`
- `MAINTENANCE_START_JST = (9, 0)`
- `MAINTENANCE_END_JST = (11, 10)`

### 4.4 `crypto_common/exchange_coincheck/coincheck_api.py`

1. 定义 `CoincheckBusinessError`，从响应里解析 `message_code` 与 `message_string`。  
2. `CoincheckApi` 必须支持以下方法：  
`get_ticker(symbol)`、`get_orderbooks(symbol)`、`place_order(symbol, side, price, size, execution_type="LIMIT", time_in_force="post_only")`、`get_orders(order_id)`、`get_active_orders(symbol=None)`、`get_assets()`、`cancel_order(order_id)`。

3. 私有请求需做 HMAC-SHA256 签名。  
4. HTTP 非 200 返回 `None`。  
5. `success == false` 抛 `CoincheckBusinessError`。  
6. 网络异常记录日志并返回 `None`。

### 4.5 monitoring 模块

1. `logger.py`：`setup_logger(name, log_file)` 输出到控制台、文件，并提供 `PrefixedLogger` 自动补前缀。  
2. `email.py`：`send_email(...)` 使用 SMTP STARTTLS 发送纯文本邮件；无账号密码时跳过并警告。  
3. `context.py`：实现 `setup_logger_from_config(config, name, log_file)` 与 `send_email_from_config(subject, body, config)`。

### 4.6 其他公共工具

1. `datatime/formatting.py` 提供 `format_duration(seconds) -> HH:MM:SS`。  
2. `trading_utils.py` 需包含：  
`calculate_buy_quantity(target_jpy, price, decimal_places=4, min_qty=None)`（向上取整）与 `BalanceMonitor`（余额不足告警、24 小时邮件冷却、`sleep_on_fail=True` 时 sleep 3 小时后返回 `False`）。

### 4.7 包导出约束

1. `crypto_common/exchange_coincheck/__init__.py` 必须导出：
`from .coincheck_api import CoincheckApi, CoincheckBusinessError`  
并设置：`__all__ = ["CoincheckApi", "CoincheckBusinessError"]`。
2. 其他 `__init__.py` 可为空文件，但必须存在。

## 5. 关键行为约束（必须满足）

1. 买单超时默认撤单；卖单超时默认不撤单。  
2. 维护窗口内机器人不做任何交易动作，只按冷却秒数循环 sleep。  
3. 所有日志走统一 logger，日志文件名为 `trading.log`。  
4. `coincheck_trading` 必须可通过 `systemd` 或 `nohup` 启动。  
5. 代码风格务实，不引入无关框架。

## 6. 交付标准

1. 运行 `python3 -m py_compile main.py trading.py` 通过。  
2. `pip install -r requirements.txt` 后可导入 `crypto_common.exchange_coincheck`。  
3. `make bounce`、`make stop`、`make nohup`、`make clean` 可执行。  
4. 逻辑与本提示词描述一致，不省略核心函数与异常分支。
