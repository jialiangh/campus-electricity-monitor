#!/bin/sh
# 安装电量监控的定时任务。幂等：重复执行只会替换自己的条目，
# 服务器上原有的其它 crontab 条目原样保留。
set -e
APP="${APP:-/opt/electricity-monitor}"
PY=/usr/bin/python3
command -v flock >/dev/null || { echo "缺少 flock"; exit 1; }
TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "$APP" | grep -v "^PYTHONIOENCODING" > "$TMP" || true
cat >> "$TMP" <<EOF
PYTHONIOENCODING=utf-8
0 * * * * flock -n $APP/.lock $PY $APP/electricity_monitor.py sample >> $APP/monitor.log 2>&1
5 0 * * 1 flock -n $APP/.lock $PY $APP/electricity_monitor.py report >> $APP/monitor.log 2>&1
EOF
crontab "$TMP"
rm -f "$TMP"
echo "=== 当前 crontab ==="
crontab -l
