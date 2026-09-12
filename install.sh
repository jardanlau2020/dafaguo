#!/usr/bin/env bash
# ============================================================
# NeoHeberg AFK 一键部署与运行脚本
# 适用：Debian / Ubuntu（其他发行版需自行确保 python3-venv / xvfb）
# 用法：
#   bash <(curl -fsSL https://raw.githubusercontent.com/xxbb678/neoheberg-oneclick/main/install.sh)
#   或下载后：EMAIL='...' PASSWORD='...' bash install.sh run
# ============================================================
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/xxbb678/neoheberg-oneclick/main"
APP_DIR="/opt/neoheberg-afk"
VENV="$APP_DIR/venv"
SCRIPT="$APP_DIR/neoheberg.py"
LOG="$APP_DIR/neoheberg.log"
ENV_FILE="$APP_DIR/env"

GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; NC=$'\033[0m'

info()  { echo "${GREEN}[*]${NC} $*"; }
warn()  { echo "${YELLOW}[!]${NC} $*"; }
err()   { echo "${RED}[x]${NC} $*"; }

need_root() {
    [ "$(id -u)" = "0" ] || { err "请用 root 运行（sudo -i 后重试）"; exit 1; }
}

install_deps() {
    info "检查系统与依赖..."
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq || true
        apt-get install -y -qq python3 python3-venv python3-pip xvfb xauth curl ca-certificates \
            >/dev/null 2>&1 || {
            warn "部分包安装失败，尝试单个安装..."
            for p in python3 python3-venv python3-pip xvfb xauth curl ca-certificates; do
                apt-get install -y -qq "$p" >/dev/null 2>&1 || warn "安装 $p 失败，请手动检查"
            done
        }
    else
        err "未检测到 apt-get，仅支持 Debian/Ubuntu。请手动安装: python3-venv xvfb xauth"
        exit 1
    fi

    info "创建工作目录 $APP_DIR"
    mkdir -p "$APP_DIR"
    chmod 700 "$APP_DIR"

    if [ ! -x "$VENV/bin/python" ]; then
        info "创建 Python 虚拟环境..."
        python3 -m venv "$VENV"
    fi

    info "安装 Python 依赖 curl_cffi / ruyipage（首次较慢）..."
    "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    "$VENV/bin/pip" install --quiet curl_cffi ruyipage || { err "依赖安装失败"; exit 1; }

    info "准备 ruyipage Firefox 运行时（约百兆，首次下载）..."
    if ! ls -d /root/.cache/ruyipage/browsers/firefox-* >/dev/null 2>&1; then
        "$VENV/bin/python" -m ruyipage install || { err "Firefox 运行时下载失败"; exit 1; }
    else
        info "Firefox 运行时已存在，跳过下载"
    fi
}

fetch_script() {
    if [ -f "$APP_DIR/neoheberg.py.local" ]; then
        cp "$APP_DIR/neoheberg.py.local" "$SCRIPT"
        info "使用本地脚本"
        return
    fi
    info "下载最新版 neoheberg.py ..."
    curl -fsSL "$REPO_RAW/neoheberg.py" -o "$SCRIPT" || { err "脚本下载失败"; exit 1; }
}

write_env_file() {
    # 将凭证写入 600 权限的 env 文件，避免每次手输
    : > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    [ -n "${EMAIL:-}" ]        && echo "EMAIL='${EMAIL}'"               >> "$ENV_FILE"
    [ -n "${PASSWORD:-}" ]     && echo "PASSWORD='${PASSWORD}'"         >> "$ENV_FILE"
    [ -n "${TG_BOT_TOKEN:-}" ] && echo "TG_BOT_TOKEN='${TG_BOT_TOKEN}'" >> "$ENV_FILE"
    [ -n "${TG_CHAT_ID:-}" ]   && echo "TG_CHAT_ID='${TG_CHAT_ID}'"     >> "$ENV_FILE"
    [ -n "${PROXY:-}" ]        && echo "PROXY='${PROXY}'"               >> "$ENV_FILE"
    [ -n "${NH_WAIT:-}" ]      && echo "NH_WAIT='${NH_WAIT}'"           >> "$ENV_FILE"
    info "凭证已保存到 $ENV_FILE（权限 600）"
}

run_now() {
    if [ -f "$ENV_FILE" ]; then
        info "从 $ENV_FILE 加载凭证"
        # shellcheck disable=SC1090
        set -a; . "$ENV_FILE"; set +a
    fi

    if [ -z "${EMAIL:-}" ] || [ -z "${PASSWORD:-}" ]; then
        err "缺少 EMAIL / PASSWORD。例：EMAIL='a@b.com' PASSWORD='xxx' bash install.sh run"
        exit 1
    fi

    info "启动挂机（日志：$LOG）"
    cd "$APP_DIR"
    nohup xvfb-run -a "$VENV/bin/python" "$SCRIPT" >> "$LOG" 2>&1 &
    echo $! > "$APP_DIR/neoheberg.pid"
    info "已后台运行，PID=$(cat "$APP_DIR/neoheberg.pid")"
    info "查看日志：tail -f $LOG"
}

stop_now() {
    if [ -f "$APP_DIR/neoheberg.pid" ]; then
        PID=$(cat "$APP_DIR/neoheberg.pid")
        kill "$PID" 2>/dev/null && info "已停止 PID=$PID" || warn "PID=$PID 不存在或已退出"
        rm -f "$APP_DIR/neoheberg.pid"
    fi
    pkill -f "$SCRIPT" 2>/dev/null || true
    info "清理完成"
}

status_now() {
    if pgrep -f "$SCRIPT" >/dev/null 2>&1; then
        info "运行中 (PID: $(pgrep -f "$SCRIPT" | tr '\n' ' '))"
    else
        warn "未运行"
    fi
    [ -f "$LOG" ] && { echo "--- 最近日志 ---"; tail -n 8 "$LOG"; }
}

install_service() {
    info "安装 systemd 守护服务 neoheberg-afk..."
    cat > /etc/systemd/system/neoheberg-afk.service <<EOF
[Unit]
Description=NeoHeberg AFK Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/xvfb-run -a $VENV/bin/python $SCRIPT
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable neoheberg-afk >/dev/null 2>&1 || true
    info "服务已安装。常用："
    echo "    systemctl start neoheberg-afk     # 启动"
    echo "    systemctl stop neoheberg-afk      # 停止"
    echo "    systemctl status neoheberg-afk    # 状态"
    echo "    journalctl -u neoheberg-afk -f    # 日志"
}

usage() {
    cat <<EOF
NeoHeberg AFK 一键脚本

用法：$0 [命令]

  install    安装依赖与浏览器运行时（默认）
  run        后台启动挂机（需凭证）
  stop       停止挂机
  status     查看状态与日志
  service    安装 systemd 守护（开机自启）

环境变量（run 时传入，或写入 $ENV_FILE）：
  EMAIL         必填，NeoHeberg 登录邮箱
  PASSWORD      必填，登录密码
  TG_BOT_TOKEN  可选，Telegram 机器人 Token
  TG_CHAT_ID    可选，Telegram chat id
  PROXY         可选，如 socks5://user:pass@host:port
  NH_WAIT       可选，每轮间隔秒（默认 65）
EOF
}

main() {
    need_root
    case "${1:-install}" in
        install)
            install_deps
            fetch_script
            [ -n "${EMAIL:-}" ] && write_env_file || true
            info "安装完成。下一步："
            echo "    EMAIL='你的邮箱' PASSWORD='密码' bash $0 run"
            ;;
        run)
            [ -x "$VENV/bin/python" ] || { warn "尚未安装，先执行安装..."; install_deps; fetch_script; }
            write_env_file
            run_now
            ;;
        stop)    stop_now ;;
        status)  status_now ;;
        service) install_service ;;
        *)       usage ;;
    esac
}

main "$@"
