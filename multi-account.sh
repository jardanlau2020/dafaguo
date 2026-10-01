#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MULTI_HOME=${DAFAGUO_MULTI_HOME:-"$HOME/.local/share/dafaguo-multi"}
ACCOUNTS_DIR="$MULTI_HOME/accounts"
SYSTEMD_DIR=${DAFAGUO_SYSTEMD_USER_DIR:-"$HOME/.config/systemd/user"}
# 解释器优先级：显式指定 > 脚本同目录的 venv > 系统 python3
# （venv 优先是因为 ruyipage 只装在 venv 里，用系统 python3 会报"缺少依赖"）
PYTHON_BIN=${DAFAGUO_PYTHON_BIN:-}
if [ -z "$PYTHON_BIN" ]; then
  for _cand in "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/.venv/bin/python"; do
    if [ -x "$_cand" ]; then PYTHON_BIN="$_cand"; break; fi
  done
fi
PYTHON_BIN=${PYTHON_BIN:-python3}
APP="$SCRIPT_DIR/neoheberg.py"

usage() {
  cat <<'EOF'
用法：
  multi-account.sh add <账号名> <HH:MM> <环境文件>
  multi-account.sh delete <账号名>
  multi-account.sh start [账号名...]      # 不给名字则启动全部
  multi-account.sh stop [账号名...]       # 不给名字则停止全部
  multi-account.sh restart [账号名...]     # 重启（不给名字则全部）
  multi-account.sh set-proxy <账号名> <代理地址>  # 设置账号代理：socks5://user:pass@host:port
  multi-account.sh set-proxy <账号名>               # 清除账号代理
  multi-account.sh set-schedule <账号名> <HH:MM>     # 修改每日自动启动时间，自动重建定时器
  multi-account.sh status [账号名]
  multi-account.sh list                    # 列出全部账号
  multi-account.sh watch [账号名...]        # 进程看护：进程意外死亡时自动拉起
  multi-account.sh install-watch-timers    # 配置每分钟进程看护 cron
  multi-account.sh remove-watch-timers     # 关闭进程看护 cron
  multi-account.sh install-timers
  multi-account.sh remove-timers
EOF
}

fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
valid_name() { [[ ${1:-} =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]; }
# 展示用代理地址：隐藏 userinfo 里的密码（保留用户名），避免密码出现在终端输出
mask_proxy() {
  local p=${1:-} proto rest userinfo hostpart user
  [[ $p == *"//"* && $p == *@* ]] || { printf '%s' "$p"; return; }
  proto=${p%%://*}
  rest=${p#*://}
  userinfo=${rest%%@*}
  hostpart=${rest#*@}
  user=${userinfo%%:*}
  if [[ -n "$user" ]]; then
    printf '%s://%s:****@%s' "$proto" "$user" "$hostpart"
  else
    printf '%s://****@%s' "$proto" "$hostpart"
  fi
}
valid_time() { [[ ${1:-} =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; }
# 站点每日计费周期的「当前日期」。
# 站点在每天 08:00（北京时间）重置额度，所以在重置点之前，
# 站点跑的还是「昨天」这一轮。若这里用 date +%F 会与 neoheberg.py 写出的
# 标记日期错位，导致看护误判/漏判。两端必须用同一套算法。
# 可用 DAFAGUO_SITE_RESET_HOUR 覆盖（需与 neoheberg.py 的 NH_SITE_RESET_HOUR 一致）。
site_day() {
  local h reset=${DAFAGUO_SITE_RESET_HOUR:-8}
  h=$(date +%-H)
  if (( h < reset )); then
    date -d 'yesterday' +%F
  else
    date +%F
  fi
}

account_dir() { printf '%s/%s' "$ACCOUNTS_DIR" "$1"; }
require_name() { valid_name "${1:-}" || fail '账号名只能包含字母、数字、下划线和连字符，且不能以符号开头'; }
require_account() { require_name "$1"; [[ -d "$(account_dir "$1")" ]] || fail "账号不存在：$1"; }
account_exists() { valid_name "${1:-}" && [[ -d "$(account_dir "$1")" ]]; }
all_account_names() {
  local dir
  [[ -d "$ACCOUNTS_DIR" ]] || return 0
  for dir in "$ACCOUNTS_DIR"/*; do
    [[ -d "$dir" ]] || continue
    printf '%s\n' "${dir##*/}"
  done
}
has_accounts() { [[ -d "$ACCOUNTS_DIR" ]] && compgen -G "$ACCOUNTS_DIR/*" >/dev/null; }
start_batch() {
  local name failed=0
  if (( $# > 0 )); then
    for name in "$@"; do
      if account_exists "$name"; then
        start_account "$name" || failed=1
      else
        printf '错误：账号不存在：%s（跳过）\n' "${name:-}" >&2
        failed=1
      fi
    done
  else
    has_accounts || { printf '尚未添加账号\n'; return 0; }
    for name in $(all_account_names); do
      start_account "$name" || failed=1
    done
  fi
  if (( failed )); then return 1; fi
  return 0
}

stop_batch() {
  local name failed=0
  if (( $# > 0 )); then
    for name in "$@"; do
      if account_exists "$name"; then
        stop_account "$name" || failed=1
      else
        printf '错误：账号不存在：%s（跳过）\n' "${name:-}" >&2
        failed=1
      fi
    done
  else
    has_accounts || { printf '尚未添加账号\n'; return 0; }
    for name in $(all_account_names); do
      stop_account "$name" || failed=1
    done
  fi
  if (( failed )); then return 1; fi
  return 0
}

restart_batch() {
  local name failed=0 targets=()
  if (( $# > 0 )); then
    for name in "$@"; do
      if account_exists "$name"; then
        targets+=("$name")
      else
        printf '错误：账号不存在：%s（跳过）\n' "${name:-}" >&2
        failed=1
      fi
    done
  else
    for name in $(all_account_names); do
      targets+=("$name")
    done
  fi
  if (( ${#targets[@]} == 0 )); then
    if (( $# > 0 )); then return 1; fi
    printf '尚未添加账号\n'
    return 0
  fi
  for name in "${targets[@]}"; do
    stop_account "$name" || failed=1
    start_account "$name" || failed=1
  done
  if (( failed )); then return 1; fi
  return 0
}

list_accounts() {
  local name any=0
  for name in $(all_account_names); do
    any=1
    printf '%s\n' "$name"
  done
  if (( ! any )); then printf '尚未添加账号\n'; fi
  return 0
}

# ═══════════ 进程看护：进程意外死亡时自动拉起 ═══════════
# 重启熔断：单账号每小时最多自动重启次数。超过则停止自动重启并告警，
# 防止进程反复崩溃时每分钟无限重启。
MAX_RESTARTS_PER_HOUR=${DAFAGUO_WATCH_MAX_RESTARTS:-5}
# 卡死检测阈值（分钟）：进程 PID 还活着，但日志长时间没有任何新增内容。
# 正常挂机每 1-2 分钟就会写一行「历劫归来！第 N 轮完成」，超过这个分钟数没动静
# 基本可以断定卡死（例如浏览器会话已死却在空转报错）。设 0 关闭该检测。
STUCK_MINUTES=${DAFAGUO_STUCK_MINUTES:-10}

# 日志清理：删除超过 LOG_RETENTION_DAYS 天的旧日志，防止磁盘被日志撑满。
LOG_RETENTION_DAYS=${DAFAGUO_LOG_RETENTION_DAYS:-7}
cleanup_old_logs() {
  local marker="$MULTI_HOME/.last-log-cleanup" today
  today=$(date +%F)
  # 每天最多执行一次，避免每分钟看护都去扫盘
  if [[ -f "$marker" && "$(cat "$marker")" == "$today" ]]; then
    return 0
  fi
  [[ -d "$ACCOUNTS_DIR" ]] && find "$ACCOUNTS_DIR" -type f -name '*.log' -mtime +"$LOG_RETENTION_DAYS" -delete 2>/dev/null || true
  find "$MULTI_HOME" -maxdepth 1 -type f -name '*.log' -mtime +"$LOG_RETENTION_DAYS" -delete 2>/dev/null || true
  printf '%s\n' "$today" > "$marker" 2>/dev/null || true
}

# 看护重启计数：每小时最多 MAX_RESTARTS_PER_HOUR 次。
# 用 "小时窗口起始 epoch:次数" 单行文件记录，避免频繁写盘。
restart_budget_ok() {
  local dir=$1 now window_start count file
  file="$dir/state/watch-restart"
  now=$(date +%s)
  window_start=$(( now - now % 3600 ))   # 当前整点小时窗口
  count=0
  if [[ -f "$file" ]]; then
    IFS=: read -r file_window file_count < "$file" || true
    if [[ "$file_window" == "$window_start" && "$file_count" =~ ^[0-9]+$ ]]; then
      count=$file_count
    fi
  fi
  mkdir -p "$dir/state"
  if (( count >= MAX_RESTARTS_PER_HOUR )); then
    return 1   # 超出预算，拒绝重启
  fi
  printf '%s:%s\n' "$window_start" "$(( count + 1 ))" > "$file"
  return 0
}

watch_account() {
  local name=${1:-} dir pid_file pid
  cleanup_old_logs
  require_account "$name"
  dir=$(account_dir "$name")
  pid_file="$dir/run.pid"
  # 未运行：不在这里补启动，交给每日定时器；仅当进程意外死亡时自恢复
  if [[ ! -f "$pid_file" ]]; then
    printf 'watch[%s]: 未运行（无 PID 文件），交由每日定时启动\n' "$name"
    return 0
  fi
  read -r pid < "$pid_file" || true
  if ! ([[ ${pid:-} =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null); then
    # 今日 100 条已刷满：neoheberg.py 是正常收工退出的，别把它当"意外死亡"拉起，
    # 否则每分钟空转一次 Firefox，并重复推送 TG 战报。
    local done_marker="$dir/state/profiles/done-$(site_day)"
    if [[ -f "$done_marker" ]]; then
      printf 'watch[%s]: 今日广告已刷满，正常收工，不再拉起\n' "$name"
      rm -f "$pid_file"
      return 0
    fi
    if ! restart_budget_ok "$dir"; then
      printf 'watch[%s]: 进程已死，但已达每小时自动重启上限(%d)，暂停自恢复，请手动检查\n' \
        "$name" "$MAX_RESTARTS_PER_HOUR"
      return 0
    fi
    printf 'watch[%s]: 进程已死 (PID %s)，自动重启\n' "$name" "${pid:-unknown}"
    rm -f "$pid_file"
    start_account "$name"
    return 0
  fi
  # ---- 卡死检测 ----
  # 走到这里说明进程 PID 活着。但「活着」不等于「在干活」：
  # 浏览器会话已经死掉、Python 只是在空转报错时，kill -0 依然成功，
  # 上面那条「进程已死」分支永远不会触发，脚本会一直假装正常。
  #
  # 判据不能用日志文件的 mtime：故障时脚本每 5 秒就写一行
  # "挂机循环抛出异常: WebSocket 连接未建立"，mtime 始终是最新的，
  # 那样检测永远不会触发（实测 09-30 空转 33 分钟、396 条错误，mtime 一直很新）。
  # 改为看「进度行」的时间戳 —— 只有真正干活才会有这些行。
  if (( STUCK_MINUTES > 0 )); then
    local latest_log prog_ts log_age
    latest_log=$(ls -t "$dir"/logs/*.log 2>/dev/null | head -1)
    if [[ -n "$latest_log" ]]; then
      # 取最后一条「有进展」的日志时间戳（轮次完成 / 挂机启动）
      prog_ts=$(grep -E "轮完成|挂机任务开始" "$latest_log" 2>/dev/null \
                | tail -1 | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}')
      if [[ -n "$prog_ts" ]]; then
        log_age=$(( ( $(date +%s) - $(date -d "$prog_ts" +%s 2>/dev/null || echo 0) ) / 60 ))
        if (( log_age >= STUCK_MINUTES )); then
          if ! restart_budget_ok "$dir"; then
            printf 'watch[%s]: 已 %d 分钟无进度（疑似卡死），但已达每小时重启上限(%d)，需人工检查\n' \
              "$name" "$log_age" "$MAX_RESTARTS_PER_HOUR"
            return 0
          fi
          printf 'watch[%s]: 进程存活但已 %d 分钟无进度，判定卡死，自动重启\n' "$name" "$log_age"
          stop_account "$name" >/dev/null 2>&1 || true
          rm -f "$pid_file"
          start_account "$name"
          return 0
        fi
      fi
    fi
  fi

  printf 'watch[%s]: 运行正常 (PID %s)\n' "$name" "$pid"
}

watch_batch() {
  local name found=0
  if (( $# > 0 )); then
    for name in "$@"; do
      account_exists "$name" || { printf '错误：账号不存在：%s（跳过）\n' "$name" >&2; found=1; continue; }
      recommended=1
      watch_account "$name"
    done
    return 0
  fi
  [[ -d "$ACCOUNTS_DIR" ]] || { printf '尚未添加账号\n'; return 0; }
  for dir in "$ACCOUNTS_DIR"/*; do
    [[ -d "$dir" ]] || continue
    found=1
    watch_account "${dir##*/}"
  done
  (( found )) || printf '尚未添加账号\n'
  return 0
}

# 配置每分钟看护 cron
install_watch_cron() {
  local tag="# DAFAGUO-V1-WATCH"
  local cronline="* * * * * DAFAGUO_MULTI_HOME=$MULTI_HOME bash \"$SCRIPT_DIR/multi-account.sh\" watch >> \"$MULTI_HOME/cron.log\" 2>&1 $tag"
  # 注意：crontab 为空时 grep -vF 无匹配行、返回 1，
  # 在 set -euo pipefail 下会中止子 shell 导致写入静默失败，必须 || true。
  ( crontab -l 2>/dev/null | grep -vF "$tag" || true; printf '%s\n' "$cronline" ) | crontab -
  if crontab -l | grep -qF "$tag"; then
    printf '已配置每分钟进程看护 cron (进程死亡自动拉起，每小时最多 %d 次)\n' "$MAX_RESTARTS_PER_HOUR"
  else
    printf 'cron 写入失败，可手动执行: %s watch\n' "$SCRIPT_DIR/multi-account.sh"
  fi
}

# 关闭看护 cron
remove_watch_cron() {
  local tag="# DAFAGUO-V1-WATCH"
  crontab -l 2>/dev/null | grep -vF "$tag" | crontab - || true
  printf '已关闭收益看护 cron\n'
}

reload_systemd() {
  if [[ $SYSTEMD_DIR == "$HOME/.config/systemd/user" ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
}

add_account() {
  local name=${1:-} schedule=${2:-} source_env=${3:-} dir
  require_name "$name"
  valid_time "$schedule" || fail '每日启动时间必须为 HH:MM（00:00 至 23:59）'
  [[ -f "$source_env" ]] || fail '环境文件不存在'
  dir=$(account_dir "$name")
  [[ ! -e "$dir" ]] || fail "账号已存在：$name"
  umask 077
  mkdir -p "$dir/logs" "$dir/firefox-profile" "$dir/state"
  cp -- "$source_env" "$dir/account.env"
  chmod 600 "$dir/account.env"
  printf '%s\n' "$schedule" > "$dir/schedule"
  chmod 600 "$dir/schedule"
  printf '已安全添加账号：%s（每日 %s）\n' "$name" "$schedule"
}

delete_account() {
  local name=${1:-} dir
  require_account "$name"
  stop_account "$name" >/dev/null 2>&1 || true
  rm -f "$SYSTEMD_DIR/dafaguo-$name.service" "$SYSTEMD_DIR/dafaguo-$name.timer"
  dir=$(account_dir "$name")
  rm -rf -- "$dir"
  reload_systemd
  printf '已删除账号：%s\n' "$name"
}

start_account() {
  local name=${1:-} dir pid_file log_file pid
  require_account "$name"
  dir=$(account_dir "$name")
  pid_file="$dir/run.pid"
  # 清理该账号 profile 上残留的 Firefox 进程：
  # 崩溃/被杀的旧实例会残留（占内存且占用 marionette 端口 2828），
  # 导致新实例 "Could not bind to port 2828" 后连接失败。
  pkill -9 -f "profile $dir/firefox-profile" 2>/dev/null || true
  sleep 1
  if [[ -f "$pid_file" ]]; then
    read -r pid < "$pid_file" || true
    if [[ ${pid:-} =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      printf '账号 %s 已在运行（PID %s）\n' "$name" "$pid"
      return 0
    fi
    rm -f "$pid_file"
  fi
  log_file="$dir/logs/$(date +%F).log"
  # 组装启动命令：无显示环境走 xvfb-run；setsid 脱离会话（SSH 断开不影响）
  local -a launch=()
  if command -v setsid >/dev/null 2>&1; then
    launch+=(setsid)
  fi
  if [[ -z "${DISPLAY:-}" ]] && command -v xvfb-run >/dev/null 2>&1; then
    launch+=(xvfb-run -a -s "-screen 0 1024x768x24")
  fi
  launch+=("$PYTHON_BIN" "$APP")
  (
    set -a
    source "$dir/account.env"
    set +a
    export BROWSER_WORK_DIR="$dir/state"
    export BROWSER_USER_DATA_DIR="$dir/firefox-profile"
    # LXC/容器内 Firefox 沙箱会导致调试端口不开，必须禁用
    exec env \
      MOZ_DISABLE_CONTENT_SANDBOX=1 \
      MOZ_DISABLE_GMP_SANDBOX=1 \
      MOZ_DISABLE_RDD_SANDBOX=1 \
      MOZ_DISABLE_SOCKET_PROCESS_SANDBOX=1 \
      MOZ_DISABLE_GPU_SANDBOX=1 \
      "${launch[@]}" >>"$log_file" 2>&1
  ) &
  pid=$!
  printf '%s\n' "$pid" > "$pid_file"
  chmod 600 "$pid_file"
  printf '已启动账号：%s（PID %s）\n' "$name" "$pid"
}

stop_account() {
  local name=${1:-} dir pid_file pid
  require_account "$name"
  dir=$(account_dir "$name")
  pid_file="$dir/run.pid"
  if [[ ! -f "$pid_file" ]]; then
    printf '账号 %s 未运行\n' "$name"
    return 0
  fi
  read -r pid < "$pid_file" || true
  if [[ ${pid:-} =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    # 整组终止（setsid 启动的进程自成进程组，可带走 xvfb-run/Xvfb/Firefox 全家）
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
  printf '已停止账号：%s\n' "$name"
}

set_schedule() {
  local name=${1:-} schedule=${2:-} dir old
  require_account "$name"
  valid_time "$schedule" || fail '时间格式必须为 HH:MM（00:00 至 23:59）'
  dir=$(account_dir "$name")
  old=$(<"$dir/schedule")
  if [[ "$old" == "$schedule" ]]; then
    printf '账号 %s 的每日启动时间已经是 %s，未做修改\n' "$name" "$schedule"
  else
    printf '%s\n' "$schedule" > "$dir/schedule"
    chmod 600 "$dir/schedule"
    printf '账号 %s 每日启动时间: %s → %s\n' "$name" "$old" "$schedule"
  fi
  # 必须重建并重启 timer：OnCalendar 是启动时解析的，
  # 只 enable --now 对已在运行的 timer 不会重新应用新的日历表达式。
  install_timers >/dev/null
  if systemctl --user restart "dafaguo-$name.timer" 2>/dev/null; then
    local next; next=$(systemctl --user show "dafaguo-$name.timer" -p NextElapseUSecRealtime --value 2>/dev/null)
    printf '定时器已更新，下次触发: %s\n' "${next:-（已生效，稍后可用 list-timers 查看）}"
  else
    printf '警告：定时器重启失败，若时间未生效请执行: multi-account.sh install-timers\n' >&2
  fi
}

set_proxy() {
  local name=${1:-} proxy=${2:-} dir env_file line tmp found=0
  require_account "$name"
  dir=$(account_dir "$name")
  env_file="$dir/account.env"
  [[ -f "$env_file" ]] || fail "账号凭证文件不存在：$env_file"
  tmp=$(mktemp) || fail '无法创建临时文件'
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ $line =~ ^[[:space:]]*PROXY[[:space:]]*= ]]; then
      found=1
    else
      printf '%s\n' "$line" >> "$tmp"
    fi
  done < "$env_file"
  if [[ -n "$proxy" ]]; then
    printf 'PROXY=%q\n' "$proxy" >> "$tmp"
  elif (( found )); then
    : # 已删除原 PROXY 行
  else
    printf 'PROXY=\n' >> "$tmp"
  fi
  cat "$tmp" > "$env_file"
  chmod 600 "$env_file"
  rm -f "$tmp"
  if [[ -n "$proxy" ]]; then
    printf '账号 %s 代理已设置：%s\n' "$name" "$(mask_proxy "$proxy")"
  else
    printf '账号 %s 代理已清除\n' "$name"
  fi
  printf '重启该账号后生效：multi-account.sh restart %s\n' "$name"
}

status_one() {
  local name=$1 dir pid_file pid schedule proxy_line proxy_val timer_unit next
  require_account "$name"
  dir=$(account_dir "$name")
  schedule=$(<"$dir/schedule")
  # 同时显示 systemd 实际排的下次触发时间。
  # 只显示 schedule 文件里的配置值会让人误判「改了没生效」——
  # 实际是否排上期要看 timer。两者不一致时以 timer 为准并给出提示。
  timer_unit="dafaguo-$name.timer"
  next=""
  if command -v systemctl >/dev/null 2>&1; then
    next=$(systemctl --user show "$timer_unit" -p NextElapseUSecRealtime --value 2>/dev/null)
  fi
  local sched_txt="$schedule"
  if [[ -n "$next" && "$next" != "n/a" ]]; then
    sched_txt="$schedule（下次 $next）"
    if [[ "$next" != *"${schedule}"* ]]; then
      sched_txt="$schedule ⚠ timer 实际为 $next"
    fi
  elif [[ -n "${next:-}" ]]; then
    sched_txt="$schedule ⚠ 定时器未排期，请执行 install-timers"
  else
    sched_txt="$schedule（未安装定时器）"
  fi
  proxy_val=""
  if [[ -f "$dir/account.env" ]]; then
    proxy_line=$(grep -E '^[[:space:]]*PROXY[[:space:]]*=' "$dir/account.env" 2>/dev/null | tail -1) || true
    if [[ -n "$proxy_line" ]]; then
      proxy_val=${proxy_line#*=}
      proxy_val=${proxy_val#\"}; proxy_val=${proxy_val%\"}
      proxy_val=${proxy_val#\'}; proxy_val=${proxy_val%\'}
    fi
  fi
  pid_file="$dir/run.pid"
  if [[ -n "$proxy_val" ]]; then
    proxy_val=$(mask_proxy "$proxy_val")
  fi
  if [[ -f "$pid_file" ]]; then
    read -r pid < "$pid_file" || true
    if [[ ${pid:-} =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%s：\033[32m运行中\033[0m，PID %s，每日 %s，代理 %s\n' \
        "$name" "$pid" "$sched_txt" "${proxy_val:-(无)}"
      return
    fi
    rm -f "$pid_file"
  fi
  printf '%s：\033[31m未运行\033[0m，每日 %s，代理 %s\n' "$name" "$sched_txt" "${proxy_val:-(无)}"
}

status_accounts() {
  local name=${1:-} dir found=0
  if [[ -n "$name" ]]; then status_one "$name"; return; fi
  [[ -d "$ACCOUNTS_DIR" ]] || { printf '尚未添加账号\n'; return; }
  for dir in "$ACCOUNTS_DIR"/*; do
    [[ -d "$dir" ]] || continue
    found=1
    status_one "${dir##*/}"
  done
  (( found )) || printf '尚未添加账号\n'
}

install_timers() {
  local dir name schedule quoted_script
  mkdir -p "$SYSTEMD_DIR"
  quoted_script=$(printf '%q' "$SCRIPT_DIR/multi-account.sh")
  [[ -d "$ACCOUNTS_DIR" ]] || fail '尚未添加账号'
  for dir in "$ACCOUNTS_DIR"/*; do
    [[ -d "$dir" ]] || continue
    name=${dir##*/}
    schedule=$(<"$dir/schedule")
    cat > "$SYSTEMD_DIR/dafaguo-$name.service" <<EOF
[Unit]
Description=dafaguo 多账号任务：$name

[Service]
Type=forking
ExecStart=$quoted_script start $name
ExecStop=$quoted_script stop $name
# 注意: 绝对不能加 RemainAfterExit=yes
# multi-account.sh start 会把挂机进程放到后台、父进程立刻退出，
# Type=forking 下 systemd 认为服务已启动。加上 RemainAfterExit 会让它
# 永远停在 active(exited)，而 timer 在自己触发的单元仍是 active 时
# 不会重新排期 —— 结果就是每天只触发一次，第二天起再也不自动启动
# (表现为 list-timers 里 NEXT/LEFT 全是 "-", Trigger: n/a)。
EOF
    cat > "$SYSTEMD_DIR/dafaguo-$name.timer" <<EOF
[Unit]
Description=dafaguo 每日定时启动：$name

[Timer]
OnCalendar=*-*-* $schedule:00
Persistent=true
Unit=dafaguo-$name.service

[Install]
WantedBy=timers.target
EOF
    chmod 644 "$SYSTEMD_DIR/dafaguo-$name.service" "$SYSTEMD_DIR/dafaguo-$name.timer"
    if [[ $SYSTEMD_DIR == "$HOME/.config/systemd/user" ]] && command -v systemctl >/dev/null 2>&1; then
      # 清理陈旧的 service 实例。
      # 早期版本的 service 带 RemainAfterExit=yes，会留下 active(exited) 的僵尸实例；
      # 单元文件重建后 systemd 的运行时状态并不会同步更新，于是下次 timer 触发时
      # 它认为「service 已经 active」而直接跳过 —— timer 显示触发了，账号却根本没起。
      # 只处理「SubState=exited 且挂机进程确实已死」的陈旧实例；
      # 绝不能对正在跑的账号 stop，那会触发 ExecStop 把挂机杀掉。
      local svc="dafaguo-$name.service" sub pid_file sp alive=0
      sub=$(systemctl --user show "$svc" -p SubState --value 2>/dev/null)
      pid_file="$dir/run.pid"
      if [[ -f "$pid_file" ]]; then
        read -r sp < "$pid_file" 2>/dev/null || true
        if [[ ${sp:-} =~ ^[0-9]+$ ]] && kill -0 "$sp" 2>/dev/null; then
          alive=1
        fi
      fi
      if [[ "$sub" == "exited" && $alive -eq 0 ]]; then
        systemctl --user stop "$svc" >/dev/null 2>&1
        systemctl --user reset-failed "$svc" >/dev/null 2>&1
      fi
      systemctl --user enable --now "dafaguo-$name.timer" >/dev/null
    fi
  done
  reload_systemd
  printf '已安装所有账号的用户级定时器\n'
}

remove_timers() {
  local file unit
  mkdir -p "$SYSTEMD_DIR"
  for file in "$SYSTEMD_DIR"/dafaguo-*.timer; do
    [[ -e "$file" ]] || continue
    unit=${file##*/}
    if [[ $SYSTEMD_DIR == "$HOME/.config/systemd/user" ]] && command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now "$unit" >/dev/null 2>&1 || true
    fi
  done
  rm -f "$SYSTEMD_DIR"/dafaguo-*.timer "$SYSTEMD_DIR"/dafaguo-*.service
  reload_systemd
  printf '已移除所有多账号定时器\n'
}

mkdir -p "$ACCOUNTS_DIR"
command=${1:-}
shift || true
case "$command" in
  add) add_account "$@" ;;
  delete|remove) delete_account "$@" ;;
  start) start_batch "$@" ;;
  stop) stop_batch "$@" ;;
  restart) restart_batch "$@" ;;
  set-proxy|proxy) set_proxy "$@" ;;
  set-schedule|time) set_schedule "$@" ;;
  status) status_accounts "$@" ;;
  list) list_accounts "$@" ;;
  watch) if (( $# > 0 )); then watch_batch "$@"; else watch_batch; fi ;;
  install-watch-timers) install_watch_cron ;;
  remove-watch-timers) remove_watch_cron ;;
  install-timers) install_timers "$@" ;;
  remove-timers) remove_timers "$@" ;;
  -h|--help|help|'') usage ;;
  *) usage >&2; exit 1 ;;
esac
