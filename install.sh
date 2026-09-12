#!/usr/bin/env bash
# ============================================================
# NeoHeberg AFK 交互式管理脚本
# 1 安装与添加账号密码
# 2 配置 Telegram 通知
# 3 查看运行状态
# 4 卸载
# 0 退出
# 用法：bash <(curl -fsSL https://raw.githubusercontent.com/xxbb678/neoheberg-oneclick/main/install.sh)
# ============================================================
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/xxbb678/neoheberg-oneclick/main"
APP_DIR="/opt/neoheberg-afk"
VENV="$APP_DIR/venv"
SCRIPT="$APP_DIR/neoheberg.py"
LOG="$APP_DIR/neoheberg.log"
ENV_FILE="$APP_DIR/env"
PID_FILE="$APP_DIR/neoheberg.pid"
SERVICE="neoheberg-afk"

GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; NC=$'\033[0m'

info() { echo "${GREEN}[*]${NC} $*"; }
warn() { echo "${YELLOW}[!]${NC} $*"; }
err()  { echo "${RED}[x]${NC} $*"; }
ok()   { echo "${GREEN}[✓]${NC} $*"; }


need_root() {
    [ "$(id -u)" = "0" ] || { err "请用 root 运行（sudo -i 后重试）"; exit 1; }
}

# ---------- 环境适配（老系统） ----------
# 拆出系统代号（bullseye=11 / bookworm=12 / jammy / focal ...）
os_codename() {
    ( . /etc/os-release 2>/dev/null; echo "${VERSION_CODENAME:-}" )
}

os_id() {
    ( . /etc/os-release 2>/dev/null; echo "${ID:-}" )
}

# 判断某个 apt 源是否真的可用（Release 文件能拉到）
apt_repo_alive() {
    local url="$1"
    curl -fsS --max-time 12 -o /dev/null "${url}/dists/${2}/Release" 2>/dev/null
}

# 老系统（已 EOL）自动改用 archive.debian.org 归档源
# 仅处理 Debian 官方源行，不碰第三方源；且仅在原源已失效时改写
fix_legacy_apt_sources() {
    [ "$(os_id)" = "debian" ] || return 0
    command -v apt-get >/dev/null 2>&1 || return 0

    local codename
    codename=$(os_codename)
    [ -z "$codename" ] && return 0

    # 当前官方源还能用就不动
    if apt_repo_alive "http://deb.debian.org/debian" "$codename"; then
        return 0
    fi

    warn "检测到 Debian $codename 官方源已失效（EOL），自动切换到 archive.debian.org"
    [ -f /etc/apt/sources.list ] && cp -n /etc/apt/sources.list "/etc/apt/sources.list.bak.$(date +%s)" 2>/dev/null || true

    # 将 sources.list 中的官方域名替换为归档站，并关掉过期校验
    if [ -f /etc/apt/sources.list ]; then
        sed -i \
            -e "s|https\?://deb\.debian\.org/debian-security|http://archive.debian.org/debian-security|g" \
            -e "s|https\?://security\.debian\.org/debian-security|http://archive.debian.org/debian-security|g" \
            -e "s|https\?://deb\.debian\.org/debian|http://archive.debian.org/debian|g" \
            /etc/apt/sources.list
        # 归档源的 debian-security 不再提供 Release，删掉该行避免 apt update 报错
        sed -i "/archive\.debian\.org\/debian-security/d" /etc/apt/sources.list
    fi

    # 关闭 Valid-Until 校验（归档源签名时间很旧）
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99neoheberg-no-valid-until

    # bullseye-backports 等兄弟源也一并清理（已不存在于归档站）
    if [ -d /etc/apt/sources.list.d ]; then
        sed -i "/backports/d" /etc/apt/sources.list.d/*.list 2>/dev/null || true
    fi
    return 0
}

# 系统依赖（含老系统兼容）
do_install_system_pkgs() {
    command -v apt-get >/dev/null 2>&1 || { err "未检测到 apt-get，仅支持 Debian/Ubuntu"; return 1; }

    export DEBIAN_FRONTEND=noninteractive
    fix_legacy_apt_sources
    apt-get update -qq || true

    info "安装基础软件包..."
    apt-get install -y -qq python3 python3-pip curl ca-certificates xauth >/dev/null 2>&1 || true

    # venv 与 distutils（老 Python 3.9 必需）
    apt-get install -y -qq python3-venv python3-distutils python3-setuptools >/dev/null 2>&1 || true

    # xvfb（某些源里包名不同）
    if ! command -v xvfb-run >/dev/null 2>&1; then
        apt-get install -y -qq xvfb >/dev/null 2>&1 || true
    fi

    # Firefox 运行时所需的 GUI 库（老系统默认不安装，导致 libgtk-3.so.0 缺失）
    if ! ldconfig -p 2>/dev/null | grep -q "libgtk-3\.so\.0"; then
        info "补齐 Firefox 所需 GUI 库..."
        apt-get install -y -qq \
            libgtk-3-0 libdbus-glib-1-2 libasound2 libxt6 libx11-xcb1 \
            libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
            libpango-1.0-0 libcairo2 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
            libxkbcommon0 libxshmfence1 libdrm2 libxcb1 libnspr4 libnss3 \
            >/dev/null 2>&1 || true
    fi

    # 校验关键依赖
    command -v xvfb-run >/dev/null 2>&1 || warn "xvfb-run 未安装成功，可能影响浏览器启动"
    ldconfig -p 2>/dev/null | grep -q "libgtk-3\.so\.0" || warn "libgtk-3 未找到，Firefox 可能无法启动"
    return 0
}

# 创建虚拟环境：优先用 venv，老系统 ensurepip 缺失时用 get-pip.py 引导
ensure_venv() {
    mkdir -p "$APP_DIR"; chmod 700 "$APP_DIR"
    [ -x "$VENV/bin/python" ] && return 0

    rm -rf "$VENV"
    info "创建 Python 虚拟环境..."
    if python3 -m venv "$VENV" >/dev/null 2>&1 && [ -x "$VENV/bin/pip" ]; then
        ok "虚拟环境就绪"
        return 0
    fi

    # 老系统（如 Debian 11 + Python 3.9）：ensurepip 缺失，venv --without-pip + get-pip.py
    warn "常规 venv 创建失败（可能 ensurepip 缺失），改用 get-pip.py 引导..."
    rm -rf "$VENV"
    python3 -m venv "$VENV" --without-pip >/dev/null 2>&1 || true

    local pyver gp
    pyver=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "3.9")
    gp="/tmp/neoheberg-get-pip.py"
    case "$pyver" in
        3.6|3.7|3.8|3.9) curl -fsSL "https://bootstrap.pypa.io/pip/${pyver}/get-pip.py" -o "$gp" ;;
        *)               curl -fsSL "https://bootstrap.pypa.io/get-pip.py" -o "$gp" ;;
    esac || { err "get-pip.py 下载失败"; return 1; }

    if [ -x "$VENV/bin/python" ]; then
        "$VENV/bin/python" "$gp" --no-warn-script-location >/dev/null 2>&1 || { err "pip 安装失败"; return 1; }
    else
        python3 "$gp" --no-warn-script-location >/dev/null 2>&1 || { err "pip 安装失败"; return 1; }
        # 全局 pip 安好后再试 venv（无 pip 模式 + 复制）
        rm -rf "$VENV"
        python3 -m venv "$VENV" >/dev/null 2>&1 || python3 -m venv "$VENV" --without-pip >/dev/null 2>&1 || true
    fi

    rm -f "$gp"
    [ -x "$VENV/bin/python" ] || { err "虚拟环境创建失败"; return 1; }
    ok "虚拟环境就绪（兼容模式）"
    return 0
}

# 在虚拟环境里安装 Python 依赖
do_install_py_deps() {
    local pip="$VENV/bin/pip"
    if [ ! -x "$pip" ]; then
        # venv 内无 pip：用 python -m pip 或重新引导
        if "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
            pip="$VENV/bin/python -m pip"
        else
            local gp="/tmp/neoheberg-get-pip.py"
            local pyver
            pyver=$("$VENV/bin/python" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "3.9")
            case "$pyver" in
                3.6|3.7|3.8|3.9) curl -fsSL "https://bootstrap.pypa.io/pip/${pyver}/get-pip.py" -o "$gp" ;;
                *)               curl -fsSL "https://bootstrap.pypa.io/get-pip.py" -o "$gp" ;;
            esac || { err "get-pip.py 下载失败"; return 1; }
            "$VENV/bin/python" "$gp" --no-warn-script-location >/dev/null 2>&1 || { err "venv 内 pip 安装失败"; return 1; }
            rm -f "$gp"
            pip="$VENV/bin/pip"
        fi
    fi

    info "升级 pip 并安装 curl_cffi / ruyipage（首次较慢）..."
    $pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    $pip install --quiet curl_cffi ruyipage || { err "Python 依赖安装失败"; return 1; }

    # 提前验证导入，避免后续运行才报错
    "$VENV/bin/python" -c 'import curl_cffi, ruyipage' 2>/dev/null || { err "依赖导入失败，请看上方 pip 输出"; return 1; }
    ok "Python 依赖就绪"
    return 0
}

# 主脚本语法适配：Python < 3.10 不支持 X | Y 类型注解，自动降级为 object
adapt_script_for_old_python() {
    [ -f "$SCRIPT" ] || return 0
    local pyver
    pyver=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "3.9")
    case "$pyver" in
        3.9|3.8|3.7|3.6)
            if grep -q 'str | None' "$SCRIPT" 2>/dev/null; then
                sed -i 's/-> str | None:/-> object:/g' "$SCRIPT"
                info "已适配 Python ${pyver}（X | Y 注解 → object）"
            fi
            ;;
    esac
    return 0
}

load_env() {
    if [ -f "$ENV_FILE" ]; then
        set -a; . "$ENV_FILE" 2>/dev/null || true; set +a
    fi
    return 0
}

write_env_file() {
    mkdir -p "$APP_DIR"
    : > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    [ -n "${EMAIL:-}" ]        && echo "EMAIL='${EMAIL}'"               >> "$ENV_FILE"
    [ -n "${PASSWORD:-}" ]     && echo "PASSWORD='${PASSWORD}'"         >> "$ENV_FILE"
    [ -n "${TG_BOT_TOKEN:-}" ] && echo "TG_BOT_TOKEN='${TG_BOT_TOKEN}'" >> "$ENV_FILE"
    [ -n "${TG_CHAT_ID:-}" ]   && echo "TG_CHAT_ID='${TG_CHAT_ID}'"     >> "$ENV_FILE"
    [ -n "${PROXY:-}" ]        && echo "PROXY='${PROXY}'"               >> "$ENV_FILE"
    [ -n "${NH_WAIT:-}" ]      && echo "NH_WAIT='${NH_WAIT}'"           >> "$ENV_FILE"
    return 0
}

# ---------------- 1. 安装与添加账号密码 ----------------
menu_install() {
    echo ""
    echo "${CYAN}=== 安装与添加账号密码 ===${NC}"
    load_env

    local def_email="${EMAIL:-}" in_email in_pass
    printf "NeoHeberg 登录邮箱"
    [ -n "$def_email" ] && printf " [当前: %s]" "$def_email"
    printf ": "
    read -r in_email || in_email=""
    [ -z "$in_email" ] && in_email="$def_email"

    if [ -n "${PASSWORD:-}" ]; then
        printf "登录密码 [直接回车沿用已保存的]: "
    else
        printf "登录密码: "
    fi
    read -r in_pass || in_pass=""
    [ -z "$in_pass" ] && in_pass="${PASSWORD:-}"

    if [ -z "$in_email" ] || [ -z "$in_pass" ]; then
        err "邮箱与密码不能为空"
        return 1
    fi
    EMAIL="$in_email"; PASSWORD="$in_pass"
    write_env_file
    ok "账号密码已保存到 $ENV_FILE"

    if [ -x "$VENV/bin/python" ] && [ -f "$SCRIPT" ]; then
        ok "依赖与主脚本已存在，跳过安装"
    else
        do_install_deps || return 1
    fi

    printf "是否立即启动挂机？[y/N]: "
    local ans
    read -r ans || ans=""
    case "$ans" in
        y|Y|yes|YES) start_bot ;;
        *) info "已保存，可选菜单 [3] 随时启动" ;;
    esac
}

do_install_deps() {
    do_install_system_pkgs || return 1
    ensure_venv || return 1
    do_install_py_deps || return 1

    if ! ls -d /root/.cache/ruyipage/browsers/firefox-* >/dev/null 2>&1; then
        info "下载 Firefox 运行时（约百兆，首次较慢）..."
        "$VENV/bin/python" -m ruyipage install || { err "Firefox 运行时下载失败"; return 1; }
    else
        ok "Firefox 运行时已存在"
    fi

    info "下载主脚本..."
    if [ -f "$APP_DIR/neoheberg.py.local" ]; then
        cp "$APP_DIR/neoheberg.py.local" "$SCRIPT"
    else
        curl -fsSL "$REPO_RAW/neoheberg.py" -o "$SCRIPT" || { err "脚本下载失败"; return 1; }
    fi

    adapt_script_for_old_python || true

    # 实测 Firefox 能否启动，提前暴露缺库问题
    if ! xvfb-run -a "$VENV/bin/python" -c '
import subprocess, sys, time, os, glob
ff = glob.glob("/root/.cache/ruyipage/browsers/firefox-*/firefox/firefox")
if not ff:
    sys.exit(1)
subprocess.run([ff[0], "--headless", "--version"], capture_output=True, timeout=60)
' >/dev/null 2>&1; then
        warn "Firefox 启动自检未通过（可能缺 GUI 库），将尝试补齐..."
        do_install_firefox_libs >/dev/null 2>&1 || true
    fi

    ok "安装完成"
    return 0
}

# 补齐 Firefox GUI 库（独立函数，供自检失败时调用）
do_install_firefox_libs() {
    command -v apt-get >/dev/null 2>&1 || return 0
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -qq \
        libgtk-3-0 libdbus-glib-1-2 libasound2 libxt6 libx11-xcb1 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
        libpango-1.0-0 libcairo2 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libxkbcommon0 libxshmfence1 libdrm2 libxcb1 libnspr4 libnss3 \
        >/dev/null 2>&1 || true
    return 0
}

# ---------------- 启停 ----------------
start_bot() {
    if pgrep -f "$SCRIPT" >/dev/null 2>&1; then
        warn "已在运行中，无需重复启动"
        return 0
    fi
    load_env
    if [ -z "${EMAIL:-}" ] || [ -z "${PASSWORD:-}" ]; then
        err "未配置账号密码，请先选菜单 [1]"
        return 1
    fi
    if [ ! -x "$VENV/bin/python" ] || [ ! -f "$SCRIPT" ]; then
        err "尚未安装，请先选菜单 [1]"
        return 1
    fi
    cd "$APP_DIR"
    # setsid 脱离会话：SSH 断开也不会把挂机进程带走
    if command -v setsid >/dev/null 2>&1; then
        setsid xvfb-run -a "$VENV/bin/python" "$SCRIPT" >> "$LOG" 2>&1 < /dev/null &
    else
        nohup xvfb-run -a "$VENV/bin/python" "$SCRIPT" >> "$LOG" 2>&1 &
    fi
    echo $! > "$PID_FILE"
    sleep 3
    if pgrep -f "$SCRIPT" >/dev/null 2>&1; then
        ok "已后台启动（PID $(cat "$PID_FILE" 2>/dev/null)）"
    else
        err "启动失败，请看日志: tail -20 $LOG"
    fi
}

stop_bot() {
    if [ -f "$PID_FILE" ]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
    pkill -f "$SCRIPT" 2>/dev/null || true
    ok "已停止"
}

# ---------------- 2. 配置 Telegram 通知 ----------------
menu_tg() {
    echo ""
    echo "${CYAN}=== 配置 Telegram 通知 ===${NC}"
    load_env

    printf "Bot Token"
    if [ -n "${TG_BOT_TOKEN:-}" ]; then
        printf " [当前: %s...%s]" "$(echo "$TG_BOT_TOKEN" | cut -c1-10)" "$(echo "$TG_BOT_TOKEN" | rev | cut -c1-4 | rev)"
    fi
    printf ": "
    local in_token in_chat
    read -r in_token || in_token=""
    [ -z "$in_token" ] && in_token="${TG_BOT_TOKEN:-}"

    printf "Chat ID"
    [ -n "${TG_CHAT_ID:-}" ] && printf " [当前: %s]" "$TG_CHAT_ID"
    printf ": "
    read -r in_chat || in_chat=""
    [ -z "$in_chat" ] && in_chat="${TG_CHAT_ID:-}"

    TG_BOT_TOKEN="$in_token"; TG_CHAT_ID="$in_chat"
    write_env_file

    if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
        info "正在发送测试消息..."
        if curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TG_CHAT_ID}" \
            -d text="✅ NeoHeberg 通知已配置成功" >/dev/null 2>&1; then
            ok "测试消息已发送，请查看 Telegram"
        else
            err "发送失败，请检查 Token 与 Chat ID"
        fi
    else
        warn "未填写完整，已清空 TG 配置"
    fi

    if pgrep -f "$SCRIPT" >/dev/null 2>&1; then
        printf "通知修改需重启才生效，是否立即重启？[y/N]: "
        local rr
        read -r rr || rr=""
        case "$rr" in
            y|Y|yes|YES) stop_bot; start_bot ;;
            *) info "请手动重启使其生效" ;;
        esac
    fi
}

# ---------------- 3. 查看运行状态 ----------------
menu_status() {
    echo ""
    echo "${CYAN}=== 运行状态 ===${NC}"

    if [ ! -d "$APP_DIR" ]; then
        warn "未安装（$APP_DIR 不存在）"
        return 0
    fi

    load_env
    if pgrep -f "$SCRIPT" >/dev/null 2>&1; then
        ok "进程：运行中 (PID: $(pgrep -f "$SCRIPT" | tr '\n' ' '))"
    else
        warn "进程：未运行"
    fi

    if command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/${SERVICE}.service" ]; then
        echo "    systemd: $(systemctl is-active "$SERVICE" 2>/dev/null || echo unknown) / $(systemctl is-enabled "$SERVICE" 2>/dev/null || echo unknown)"
    fi

    if schedule_installed; then
        local _next
        _next=$(systemctl list-timers "${SCHED_SERVICE}.timer" --no-pager 2>/dev/null | awk 'NR==2{print $1, $2, $3}')
        echo "    定时:   每日自动挂机已启用 (下次: ${_next:-?})"
    else
        echo "    定时:   未设置"
    fi

    echo "    账号：${EMAIL:-<未配置>}"
    if [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ]; then
        echo "    TG 通知：已配置 (chat ${TG_CHAT_ID})"
    else
        echo "    TG 通知：未配置"
    fi

    if [ -f "$LOG" ]; then
        local rounds bal bj
        rounds=$(grep -c '轮完成' "$LOG" 2>/dev/null || echo 0)
        bal=$(grep -E 'TG 报告: 余额=' "$LOG" 2>/dev/null | tail -1 | sed 's/.*余额=//' | awk '{print $1}' || echo "-")
        [ -z "$bal" ] && bal="-"
        echo "────────────────────────────"
        echo "    今日轮次：$rounds"
        echo "    最新余额：$bal"
        echo "    最近日志（北京时间）："
        tail -n 6 "$LOG" | sed 's/^/      /'
    fi

    echo ""
    echo "${CYAN}--- 操作 ---${NC}"
    echo "  [s] 启动 / [k] 停止 / [r] 重启 / [l] 实时日志 / 回车返回"
    local op
    read -r op || op=""
    case "$op" in
        s|S) start_bot ;;
        k|K) stop_bot ;;
        r|R) stop_bot; start_bot ;;
        l|L) tail -f "$LOG" ;;
        *) : ;;
    esac
}

# ---------------- 每日 09:00 自动挂机（systemd timer） ----------------
# 设计：每天 09:00 拉起挂机，刷满额度后脚本自行退出；次日 09:00 再次拉起。
SCHED_SERVICE="neoheberg-afk-daily"

schedule_installed() {
    [ -f "/etc/systemd/system/${SCHED_SERVICE}.service" ] && [ -f "/etc/systemd/system/${SCHED_SERVICE}.timer" ]
}

schedule_install() {
    command -v systemctl >/dev/null 2>&1 || { err "未检测到 systemd，无法设置定时"; return 1; }

    if [ ! -x "$VENV/bin/python" ] || [ ! -f "$SCRIPT" ]; then
        err "尚未安装，请先选菜单 [1]"
        return 1
    fi
    if [ ! -f "$ENV_FILE" ]; then
        err "未配置账号密码，请先选菜单 [1]"
        return 1
    fi

    local hour minute tm
    printf "每日几点开始挂机 [默认 09:00]: "
    read -r tm || tm=""
    hour="09"; minute="00"
    case "$tm" in
        "") : ;;
        *:*)
            hour=$(echo "$tm" | cut -d: -f1 | sed 's/^0*//')
            minute=$(echo "$tm" | cut -d: -f2 | cut -c1-2 | sed 's/^0*//')
            [ -z "$hour" ] && hour=0
            [ -z "$minute" ] && minute=0
            [ "$hour" -ge 0 ] 2>/dev/null && [ "$hour" -le 23 ] 2>/dev/null || { err "小时应为 0-23"; return 1; }
            [ "$minute" -ge 0 ] 2>/dev/null && [ "$minute" -le 59 ] 2>/dev/null || { err "分钟应为 0-59"; return 1; }
            ;;
        *) err "格式应为 HH:MM，例 09:00"; return 1 ;;
    esac
    # 强制十进制：bash 中 09 会被当作无效八进制数，导致 default 变 00
    hour=$((10#$hour))
    minute=$((10#$minute))
    printf -v hour "%02d" "$hour"
    printf -v minute "%02d" "$minute"

    cat > "/etc/systemd/system/${SCHED_SERVICE}.service" <<EOF
[Unit]
Description=NeoHeberg AFK daily run
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/xvfb-run -a $VENV/bin/python $SCRIPT
EOF

    cat > "/etc/systemd/system/${SCHED_SERVICE}.timer" <<EOF
[Unit]
Description=Run NeoHeberg AFK every day at ${hour}:${minute}

[Timer]
OnCalendar=*-*-* ${hour}:${minute}:00
Persistent=true
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
    systemctl enable --now "${SCHED_SERVICE}.timer" >/dev/null 2>&1 || { err "启用定时器失败"; return 1; }
    ok "已设置每日 ${hour}:${minute} 自动挂机"
    echo "    下次执行: $(systemctl list-timers "${SCHED_SERVICE}.timer" --no-pager 2>/dev/null | awk 'NR==2{print $1, $2, $3}')"
    echo "    查看状态: systemctl status ${SCHED_SERVICE}.timer"
    echo "    立即跑一次: systemctl start ${SCHED_SERVICE}.service"
    return 0
}

schedule_remove() {
    if ! schedule_installed; then
        warn "未设置定时挂机"
        return 0
    fi
    systemctl disable --now "${SCHED_SERVICE}.timer" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${SCHED_SERVICE}.service" "/etc/systemd/system/${SCHED_SERVICE}.timer"
    systemctl daemon-reload >/dev/null 2>&1 || true
    ok "已取消每日定时挂机"
    return 0
}

schedule_show() {
    if ! schedule_installed; then
        warn "未设置定时挂机"
        return 0
    fi
    echo "    定时器: $(systemctl is-active "${SCHED_SERVICE}.timer" 2>/dev/null || echo unknown) / $(systemctl is-enabled "${SCHED_SERVICE}.timer" 2>/dev/null || echo unknown)"
    systemctl list-timers "${SCHED_SERVICE}.timer" --no-pager 2>/dev/null | sed -n '1,2p' | sed 's/^/    /'
    return 0
}

menu_schedule() {
    echo ""
    echo "${CYAN}=== 每日定时挂机 ===${NC}"
    schedule_show
    echo ""
    echo "  [1] 设置/修改每日自动挂机"
    echo "  [2] 取消定时挂机"
    echo "  [3] 立即执行一次"
    echo "  [0] 返回"
    printf "请选择 [0-3]: "
    local c
    read -r c || c="0"
    case "$c" in
        1) schedule_install ;;
        2) schedule_remove ;;
        3)
            systemctl start "${SCHED_SERVICE}.service" 2>/dev/null && ok "已触发一次执行" || err "触发失败（需先设置定时）"
            ;;
        *) : ;;
    esac
}

# ---------------- 4. 卸载 ----------------
menu_uninstall() {
    echo ""
    echo "${CYAN}=== 卸载 ===${NC}"
    printf "确定卸载 NeoHeberg AFK 吗？进程、凭证、依赖都将删除 [y/N]: "
    local c
    read -r c || c=""
    case "$c" in
        y|Y|yes|YES) ;;
        *) info "已取消"; return 0 ;;
    esac

    stop_bot >/dev/null 2>&1 || true

    if command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/${SERVICE}.service" ]; then
        systemctl stop "$SERVICE" >/dev/null 2>&1 || true
        systemctl disable "$SERVICE" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/${SERVICE}.service"
        systemctl daemon-reload >/dev/null 2>&1 || true
        ok "systemd 服务已移除"
    fi

    if schedule_installed; then
        schedule_remove >/dev/null 2>&1 || true
    fi

    rm -rf "$APP_DIR"
    ok "已删除 $APP_DIR（含 venv、凭证、日志）"
    info "Firefox 运行时保留在 /root/.cache/ruyipage，如需彻底清理：rm -rf /root/.cache/ruyipage"
}

# ---------------- 菜单 ----------------
menu() {
    while true; do
        local st
        if pgrep -f "${SCRIPT}" >/dev/null 2>&1; then
            st="${GREEN}运行中${NC}"
        elif [ -d "$APP_DIR" ]; then
            st="${YELLOW}已安装未运行${NC}"
        else
            st="${RED}未安装${NC}"
        fi

        clear 2>/dev/null || printf '\033[2J\033[H'
        echo -e "${GREEN}===============================================${NC}"
        echo -e " NeoHeberg AFK 管理脚本"
        echo -e " 服务状态: $st"
        echo -e "${GREEN}===============================================${NC}"
        echo -e " ${CYAN}[1]${NC} 安装与添加账号密码"
        echo -e " ${CYAN}[2]${NC} 配置 Telegram 通知"
        echo -e " ${CYAN}[3]${NC} 查看运行状态"
        echo -e " ${CYAN}[4]${NC} 每日定时挂机"
        echo -e " ${CYAN}[5]${NC} 卸载"
        echo -e " ${CYAN}[0]${NC} 退出脚本"
        echo -e "${GREEN}===============================================${NC}"
        printf "请输入数字选择 [0-5]: "
        local choice
        read -r choice || choice="0"

        case "$choice" in
            1) menu_install ;;
            2) menu_tg ;;
            3) menu_status ;;
            4) menu_schedule ;;
            5) menu_uninstall ;;
            0) echo "已退出"; exit 0 ;;
            *) err "无效选择"; sleep 1 ;;
        esac

        echo ""
        printf "按回车返回主菜单..."
        read -r _pause || true
    done
}

# ---------------- 入口（支持命令模式与交互模式） ----------------
need_root
case "${1:-}" in
    install)   menu_install ;;
    tg)        menu_tg ;;
    status)    menu_status ;;
    schedule)  menu_schedule ;;
    uninstall|remove|del) menu_uninstall ;;
    *)
        if [ -t 0 ]; then
            menu
        elif [ -r /dev/tty ]; then
            # 管道方式（curl ... | bash）：交互从 /dev/tty 读取
            exec < /dev/tty
            menu
        else
            echo "非交互环境。可用: $0 [install|tg|status|uninstall]"
            exit 1
        fi
        ;;
esac
