.PHONY: monitor bounce stop install-service enable disable clean log ps

PY := /home/ubuntu/coincheck_trading/.venv/bin/python
SERVICE := coincheck_trading
UNIT := $(SERVICE).service
UNIT_FILE := /home/ubuntu/coincheck_trading/$(UNIT)
SYSTEMD_UNIT := /etc/systemd/system/$(UNIT)

ps:
	@echo "=== Systemd 服务状态 ==="
	@systemctl is-active $(UNIT) || true
	@echo ""
	@echo "=== Python 进程列表 ==="
	@pgrep -af "/home/ubuntu/coincheck_trading/.venv/bin/python" | grep -vE "language-server|pgrep|vscode|extension" || echo "无运行中的进程"

install-service:
	@test -f "$(UNIT_FILE)" || (echo "缺少 systemd unit 文件: $(UNIT_FILE)"; exit 1)
	@echo "正在安装 $(UNIT) 到 systemd..."
	@sudo ln -sfn "$(UNIT_FILE)" "$(SYSTEMD_UNIT)"
	@sudo systemctl daemon-reload
	@echo "当前 unit 映射: $$(readlink -f "$(SYSTEMD_UNIT)" 2>/dev/null || echo '未安装')"

bounce: install-service
	@echo "正在重启 $(SERVICE) 服务..."
	@sudo systemctl restart $(UNIT)
	@echo "服务已重启，正在打开日志..."
# 	@echo "按 Ctrl+C 退出日志查看 (服务会继续在后台运行)"
# 	@sudo journalctl -u $(UNIT) -n 10 --no-pager
	@$(MAKE) log

stop:
	@echo "正在停止 $(SERVICE) 服务..."
	@sudo systemctl stop $(UNIT)
	@echo "当前服务状态: $$(sudo systemctl is-active $(UNIT) || true)"
	@$(MAKE) ps

enable: install-service
	@echo "正在启用 $(SERVICE) 服务开机自启..."
	@sudo systemctl enable $(UNIT)
	@echo "当前开机自启状态: $$(sudo systemctl is-enabled $(UNIT) || true)"
	@$(MAKE) ps

disable: install-service
	@echo "正在禁用 $(SERVICE) 服务开机自启..."
	@sudo systemctl disable $(UNIT)
	@echo "当前开机自启状态: $$(sudo systemctl is-enabled $(UNIT) || true)"
	@$(MAKE) ps


clean:
	@echo "停止所有 $(SERVICE) 程序..."
	@# 尝试停止系统服务
	@sudo systemctl stop $(UNIT) || true
	@# 停止相关 Python 进程
	@# 停止相关 Python 进程 (排除当前 Shell 进程 $$)
	@pids=$$(pgrep -f "/home/ubuntu/coincheck_trading/.venv/bin/python" | grep -v "^$$$$" || true); \
	if [ -n "$$pids" ]; then \
		echo "发现进程: $$pids"; \
		kill $$pids 2>/dev/null || true; \
		echo "已发送停止信号"; \
	else \
		echo "未发现其他运行中的进程"; \
	fi
	@$(MAKE) ps

log:
	@test -f trading.log || touch trading.log
	@tail -f trading.log


monitor:
	@$(PY) monitor.py
