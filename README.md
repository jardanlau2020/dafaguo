# NeoHeberg AFK 一键脚本

自动登录 [NeoHeberg](https://dash.neoheberg.fr) 并挂机刷广告额度，支持 Telegram 余额播报。

- 自动过 Cloudflare 验证并登录取 Cookie
- 纯 HTTP 极速挂机（curl_cffi 指纹伪装），无需全程开浏览器
- Cookie 失效时自动唤起浏览器重新登录
- 刷满 100/100 后自动汇总并退出
- 时间戳为北京时间

> 仅供学习交流，使用风险自负。

## 一键安装

    bash <(curl -fsSL https://raw.githubusercontent.com/xxbb678/neoheberg-oneclick/main/install.sh)

安装过程会自动装好 python3-venv、xvfb、xauth，创建虚拟环境，安装 curl_cffi 与 ruyipage，并下载 Firefox 运行时（约百兆，首次较慢）。

## 运行

安装完成后，用环境变量传入凭证并后台启动：

    EMAIL='你的邮箱' PASSWORD='你的密码' TG_BOT_TOKEN='机器人token' TG_CHAT_ID='chatid' \
      bash <(curl -fsSL https://raw.githubusercontent.com/xxbb678/neoheberg-oneclick/main/install.sh) run

凭证会写入 `/opt/neoheberg-afk/env`（权限 600），下次启动只需 `bash install.sh run`，无需重输。

## 命令

    install   安装依赖与浏览器运行时（默认）
    run       后台启动挂机（需凭证）
    stop      停止挂机
    status    查看状态与最近日志
    service   安装 systemd 守护（开机自启）

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `EMAIL` | 是 | NeoHeberg 登录邮箱 |
| `PASSWORD` | 是 | 登录密码 |
| `TG_BOT_TOKEN` | 否 | Telegram 机器人 Token |
| `TG_CHAT_ID` | 否 | Telegram chat id |
| `NOTIFY_NAME` | 否 | 节点名称，多台机器共用同一 TG 机器人时用于区分（通知顶部加一行 🖥️ 名称） |
| `PROXY` | 否 | 如 `socks5://user:pass@host:port` |
| `NH_WAIT` | 否 | 每轮间隔秒数，默认 65 |

## 常用命令

    tail -f /opt/neoheberg-afk/neoheberg.log    # 实时日志
    systemctl start neoheberg-afk               # systemd 启动
    journalctl -u neoheberg-afk -f              # systemd 日志

## 文件位置

| 路径 | 内容 |
|------|------|
| `/opt/neoheberg-afk/venv` | Python 虚拟环境 |
| `/opt/neoheberg-afk/neoheberg.py` | 主脚本 |
| `/opt/neoheberg-afk/env` | 凭证文件（600） |
| `/opt/neoheberg-afk/neoheberg.log` | 运行日志 |

## License

MIT
