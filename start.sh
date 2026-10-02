#!/bin/bash
# 在容器内启动挂机（禁沙箱 + 脱离会话）
# 启动前强制清理所有旧实例，避免多实例并发导致兑换不结算。
set -u   # 未定义变量即报错；不用 set -e（清理步骤失败应继续往下走）
cd "$APP_DIR" || exit 1

# ── 1. 杀掉所有旧实例（python 主程序 + xvfb-run 包装进程）──
pkill -9 -f "venv/bin/python.*neoheberg.py" 2>/dev/null
sleep 1
pkill -9 -f "xvfb-run.*neoheberg.py" 2>/dev/null
sleep 1
pkill -9 -f "neoheberg.py" 2>/dev/null
sleep 1
# ── 2. 清理残留 Xvfb（僵尸显示会占用内存且可能抢占 display）──
pkill -9 -f "Xvfb :" 2>/dev/null
pkill -f "ms-playwright.*chrome" 2>/dev/null   # Playwright Chromium 子进程
pkill -f firefox 2>/dev/null                   # 兼容历史 Firefox 版本残留
sleep 1

# ── 3. 验证杀干净了；还有残留就等一会儿再杀一次 ──
_LEFT=$(pgrep -f "neoheberg.py" | wc -l)
if [ "$_LEFT" -gt 0 ]; then
    echo "[warn] 仍有 $_LEFT 个残留进程，二次清理..."
    sleep 2
    pkill -9 -f "neoheberg.py" 2>/dev/null
    pkill -9 -f "Xvfb :" 2>/dev/null
    sleep 1
fi
echo "[info] 清理完成，残留实例数: $(pgrep -f 'neoheberg.py' | wc -l)"

# ── 4. 启动单实例 ──
set -a
. ./env
set +a

# 按日期切分日志，保留历史（原先每次启动都 rm -f，历史收益记录会丢）
LOG_FILE="neoheberg-$(date +%F).log"
# 兼容旧习惯：软链到固定名，便于 tail -f neoheberg.log
ln -sf "$LOG_FILE" neoheberg.log 2>/dev/null || true

setsid nohup xvfb-run -a -s "-screen 0 1024x768x24" ./venv/bin/python ./neoheberg.py \
    </dev/null >>"$LOG_FILE" 2>&1 &

echo "launched pid=$!"
sleep 8

echo "[info] 当前实例列表（应只 1 组）:"
ps -eo pid,ppid,cmd | grep "[n]eoheberg.py" || echo NOT_STARTED
