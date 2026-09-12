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
    if command -v apt-get >/dev/null 2>&1; then
        info "正在安装系统依赖（首次较慢）..."
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq || true
        apt-get install -y -qq python3 python3-venv python3-pip xvfb xauth curl ca-certificates >/dev/null 2>&1 || true
    else
        err "未检测到 apt-get，仅支持 Debian/Ubuntu"
        return 1
    fi

    mkdir -p "$APP_DIR"; chmod 700 "$APP_DIR"
    [ -d "$VENV" ] || { info "创建虚拟环境..."; rm -rf "$VENV"; python3 -m venv "$VENV"; }

    info "安装 curl_cffi / ruyipage ..."
    "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    "$VENV/bin/pip" install --quiet curl_cffi ruyipage || { err "依赖安装失败"; return 1; }

    if ! ls -d /root/.cache/ruyipage/browsers/firefox-* >/dev/null 2>&1; then
        info "下载 Firefox 运行时（约百兆）..."
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
    ok "安装完成"
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
    nohup xvfb-run -a "$VENV/bin/python" "$SCRIPT" >> "$LOG" 2>&1 &
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
        echo -e " ${CYAN}[4]${NC} 卸载"
        echo -e " ${CYAN}[0]${NC} 退出脚本"
        echo -e "${GREEN}===============================================${NC}"
        printf "请输入数字选择 [0-4]: "
        local choice
        read -r choice || choice="0"

        case "$choice" in
            1) menu_install ;;
            2) menu_tg ;;
            3) menu_status ;;
            4) menu_uninstall ;;
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
