.PHONY: monitor bounce stop clean log ps

PY := /home/ubuntu/coincheck_trading/.venv/bin/python

ps:
	@echo "=== Systemd 服务状态 ==="
	@systemctl is-active coincheck_trading || true
	@echo ""
	@echo "=== Python 进程列表 ==="
	@pgrep -af "/home/ubuntu/coincheck_trading/.venv/bin/python" | grep -vE "language-server|pgrep|vscode|extension" || echo "无运行中的进程"

bounce:
	@echo "正在重启 coincheck_trading 服务..."
	@sudo systemctl restart coincheck_trading
	@echo "服务已重启，正在打开日志..."
# 	@echo "按 Ctrl+C 退出日志查看 (服务会继续在后台运行)"
# 	@sudo journalctl -u coincheck_trading -n 10 --no-pager
	@$(MAKE) log

stop:
	@echo "正在停止 coincheck_trading 服务..."
	@sudo systemctl stop coincheck_trading
	@echo "当前服务状态: $$(sudo systemctl is-active coincheck_trading || true)"
	@$(MAKE) ps


clean:
	@echo "停止所有 coincheck_trading 程序..."
	@# 尝试停止系统服务
	@sudo systemctl stop coincheck_trading || true
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

