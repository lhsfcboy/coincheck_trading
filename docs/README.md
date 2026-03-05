# Coincheck Trading Bot

Coincheck 现货（`BTC`）自动做市机器人。项目由 `coincheck_trading`（策略与运行）+ `crypto_common`（交易 API、监控与通用工具）组成。

## 项目结构

```text
coincheck_trading/
  main.py
  trading.py
  Makefile
  coincheck_trading.service
  requirements.txt
  .envrc
  .vscode/settings.json

crypto_common/
  crypto_common/
    env.py
    exchange_coincheck/
      config.py
      coincheck_api.py
    monitoring/
      context.py
      logger.py
      email.py
    trading_utils.py
    datatime/formatting.py
```

## 配置与依赖

### 1. Python 环境

```bash
cd /home/ubuntu/coincheck_trading
python3 -m venv .venv
source .venv/bin/activate
make install
```

说明：`make install` 使用 `python3 -m pip`，请先激活 `.venv`，避免装到系统 Python。

### 2. 环境变量

配置入口：`crypto_common.exchange_coincheck.config`

`load_env()` 加载顺序：
1. `CRYPTO_COMMON_ENV` 指向的文件
2. `/home/ubuntu/crypto_common/.env`
3. 当前工作目录 `.env`

建议在 `/home/ubuntu/crypto_common/.env` 中配置：

```ini
COINCHECK_API_KEY=your_api_key
COINCHECK_API_SECRET=your_secret_key

# 可选：邮件通知
EMAIL_USER=your_email
EMAIL_PASSWORD=your_email_app_password
EMAIL_TO=you@example.com
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
```

## 当前策略行为（与代码一致）

### 关键常量（`main.py`）

- `TRADE_AMOUNT_JPY_TARGET = 10_000`
- `TARGET_CYCLE_PROFIT = 3.1`
- `DEFAULT_WAIT_SECONDS = 10`
- `COOLDOWN_SECONDS = 60`
- `BUY_ORDER_TIMEOUT_SECONDS = 60`
- `SELL_ORDER_TIMEOUT_SECONDS = 30`
- `BALANCE_THRESHOLD_RATIO = 1.1`
- `POST_ONLY_TIME_IN_FORCE = "post_only"`（定义于 `trading.py`）

### 交易流程（`trading.py`）

1. 主循环开始时先检查维护时间窗口。
2. BUY 阶段：读取盘口、计算买价和买量、检查 JPY 余额、下限价买单、轮询成交。
3. BUY 下单前：检查当前未成交 SELL 订单，若 `active_sell_price - SELL_SPREAD < target_buy_price`，则跳过本次 BUY，并冷却 `COOLDOWN_SECONDS`。
4. BUY 超时：在价格条件满足时可延长等待，否则撤单并重开 BUY。
5. SELL 阶段：以 `buy_price + SELL_SPREAD` 下限价卖单并轮询成交。
6. SELL 超时：发送邮件提醒，不撤单，直接开始下一轮 BUY。
7. SELL 成功：记录利润并冷却 `COOLDOWN_SECONDS` 后进入下一轮。

### 维护窗口（JST）

由 `crypto_common/exchange_coincheck/config.py` 定义：
- 当前默认关闭（`MAINTENANCE_WEEKDAY_JST = None`）

命中维护窗口时：
- 不进行任何交易动作
- 仅 sleep（默认 `COOLDOWN_SECONDS`）后继续检查

## 运行方式

### 1. systemd（推荐）

`coincheck_trading.service` 关键配置：
- `WorkingDirectory=/home/ubuntu/coincheck_trading`
- `ExecStart=/home/ubuntu/coincheck_trading/.venv/bin/python main.py`
- `EnvironmentFile=/home/ubuntu/crypto_common/.env`

首次部署：

```bash
sudo ln -sf /home/ubuntu/coincheck_trading/coincheck_trading.service /etc/systemd/system/coincheck_trading.service
sudo systemctl daemon-reload
sudo systemctl enable coincheck_trading
sudo systemctl start coincheck_trading
```

日常操作：

```bash
# 重启并实时跟随服务日志
make bounce

# 停止服务，并输出最后 30 行 journal
make stop
```

### 2. nohup（本地临时运行）

```bash
# 后台启动，输出到 trading.log
make nohup

# 跟随 trading.log
make log

# 停止 nohup 启动的 main.py 进程
make clean
```

说明：当前 `make clean` 只停止进程，不清空 `trading.log`。

## 日志与告警

- 应用日志文件：`/home/ubuntu/coincheck_trading/trading.log`
- systemd 日志：`journalctl -u coincheck_trading -f`
- 邮件通知场景：
  - SELL 超时
  - 余额不足（带冷却）

## VSCode 建议

项目已包含 `.vscode/settings.json`：
- `python.defaultInterpreterPath = ${workspaceFolder}/.venv/bin/python`
- `python.analysis.extraPaths` 包含 `../crypto_common`

若仍有导入报错，执行：
1. `Python: Select Interpreter` 选择 `coincheck_trading/.venv/bin/python`
2. `Developer: Reload Window`

## 常见问题

1. 报错 `Missing Coincheck API credentials`：检查 `/home/ubuntu/crypto_common/.env` 是否存在并包含 `COINCHECK_API_KEY/COINCHECK_API_SECRET`。
2. `make install` 后依赖仍不可用：确认执行前已 `source .venv/bin/activate`。
3. 服务启动失败：用 `sudo systemctl status coincheck_trading` + `journalctl -u coincheck_trading -n 100 --no-pager` 查看详细错误。
