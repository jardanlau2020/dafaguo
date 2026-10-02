# NeoHeberg AFK 一键脚本

自动登录 [NeoHeberg](https://dash.neoheberg.fr) 并挂机刷广告额度，支持 Telegram 余额播报。

- 纯 HTTP 极速挂机（`curl_cffi` 指纹伪装），无需全程开浏览器
- Cookie 失效时自动唤起 Playwright + Chromium 过 Cloudflare / Cap 验证码，重新登录取 Cookie
- 实时读取余额与今日进度（N/100），每轮打印单轮收益
- 刷满 100/100 后写完成标记 `done-<站点日>` 并自动退出
- 支持 Telegram 播报（启动、收盘、异常）

> 仅供学习交流，使用风险自负。

## 🚀 一键安装

    bash <(curl -fsSL https://raw.githubusercontent.com/jardanlau2020/dafaguo/main/install.sh)

安装过程自动完成：装好 `python3-venv` / `xvfb` / `xauth` → 创建虚拟环境 → 安装 `curl_cffi` 与 `playwright` → 下载 Playwright Chromium 运行时（含系统库，首次约 150MB，较慢）。

## 启动

安装完成后，直接进交互菜单：

    bash install.sh

菜单里：

- **[2] 账号密码** —— 填邮箱密码，写入 `/root/dafaguo/env`（权限 600）
- **[6] 运行状态** —— 按 `s` 启动、`k` 停止、`r` 重启、`l` 看实时日志
- **[5] 每日定时挂机** —— 装 systemd timer，每天定点自动拉起

也支持非交互子命令：

    bash install.sh account      # 填写账号密码
    bash install.sh tg           # 配置 Telegram 通知（含节点名称）
    bash install.sh balance      # 实时查余额
    bash install.sh status       # 查看运行状态与最近日志
    bash install.sh schedule     # 每日定时挂机
    bash install.sh multi        # 多账号管理
    bash install.sh update       # 更新主脚本到最新版（不动依赖）
    bash install.sh uninstall    # 卸载（进程、凭证、依赖全删）

更新主脚本（已安装的机器）：

    bash <(curl -fsSL https://raw.githubusercontent.com/jardanlau2020/dafaguo/main/install.sh) update

## 多账号分时启动

多账号功能由 `multi-account.sh` 单独管理，不修改 `start.sh` 或单账号目录。每个账号都有独立的环境文件、每日日程、日志、PID、状态目录和运行状态；无显示环境下自动走 `xvfb-run` 启动（与单账号一致）。

> 交互菜单里已集成：`bash install.sh` 进菜单后选 **[7] 多账号管理**，可直接添加账号（交互填邮箱密码、TG 通知、代理或 env 文件）、批量启停、安装每日定时、看日志，无需手动敲命令。添加账号时可一并填写 TG 机器人 Token 与 Chat ID（可选，留空=不接收通知）。`install` / `update` 会自动把 `multi-account.sh` 部署/更新到安装目录 `/root/dafaguo`。

先为每个账号准备环境文件，例如 `account-a.env`：

```bash
EMAIL='账号邮箱'
PASSWORD='账号密码'
TG_BOT_TOKEN='可选的机器人 token'
TG_CHAT_ID='可选的 chat id'
NOTIFY_NAME='账号 A'
PROXY='可选代理'
NH_WAIT='30'
```

环境文件应只允许当前用户读取：

```bash
chmod 600 account-a.env
```

添加账号并设置每日启动时间：

```bash
./multi-account.sh add account-a 06:30 account-a.env
./multi-account.sh add account-b 08:15 account-b.env
```

常用命令：

```bash
./multi-account.sh start account-a       # 立即启动指定账号
./multi-account.sh start                 # 立即启动全部账号
./multi-account.sh start a b c           # 批量启动指定多个账号
./multi-account.sh stop account-a        # 停止指定账号
./multi-account.sh stop                  # 停止全部账号
./multi-account.sh restart               # 重启全部账号
./multi-account.sh restart account-a     # 重启指定账号（可传多个）
./multi-account.sh set-proxy account-a socks5://user:pass@host:port  # 设置账号代理
./multi-account.sh set-proxy account-a   # 清除账号代理
./multi-account.sh status account-a      # 查看单个账号状态（含代理）
./multi-account.sh status                # 查看全部账号状态
./multi-account.sh list                  # 列出全部账号名
./multi-account.sh delete account-a      # 停止并删除账号及其独立数据
./multi-account.sh install-timers         # 安装并启用所有账号的用户级定时器
./multi-account.sh remove-timers          # 停用并移除多账号定时器
```

批量命令中遇到不存在的账号名会跳过并提示，其余账号正常处理；`start`/`stop`/`restart` 全部成功才返回 0。

账号数据默认保存在 `~/.local/share/dafaguo-multi/accounts/<账号名>/`。环境文件会复制为权限 `600` 的 `account.env`，命令输出和 systemd 单元均不会包含密码。账号名只允许字母、数字、下划线和连字符，防止路径穿越。

`install-timers` 使用 systemd 用户级定时器。若注销后仍需执行，可按系统配置启用 linger：

```bash
loginctl enable-linger "$USER"
```

## 环境要求

- Linux（测试于 Debian/Ubuntu），需能访问目标站点
- **出口 IP 非数据中心（推荐住宅 / WARP 类出口）**。数据中心 IP 会被广告网络直接甩走、跳过结算流程，导致「能运行但不涨币」。

## 依赖

```bash
python3 -m venv venv
venv/bin/pip install curl_cffi playwright
venv/bin/python -m playwright install --with-deps chromium   # 下载 Chromium 运行时（约 150MB）
apt install -y xvfb
```

脚本会自动使用 Playwright 自带的 Chromium，无需硬编码浏览器路径；如需指定自备 Chromium，用 `NH_CHROMIUM_PATH`。

## 环境变量

写入 `env` 文件（权限 600），或直接导出：

| 变量 | 必填 | 说明 |
|------|------|------|
| `EMAIL` | 是 | NeoHeberg 登录账号 |
| `PASSWORD` | 是 | 登录密码 |
| `TG_BOT_TOKEN` | 否 | Telegram 机器人 Token |
| `TG_CHAT_ID` | 否 | Telegram chat id |
| `NOTIFY_NAME` | 否 | 节点名称，多台机器共用同一 TG 机器人时用于区分 |
| `PROXY` | 否 | 如 `socks5://user:pass@host:port` |
| `NH_WAIT` | 否 | 每轮间隔秒数，默认 `65`（小于 60 会被抬回 65）|
| `NH_UA` | 否 | 挂机请求的 User-Agent，默认内置 Chrome 120 UA |
| `NH_COOKIE` | 否 | 自备 Cookie 种子（浏览器里复制一份），可跳过登录关卡 |
| `NH_HEADLESS` | 否 | `0` 显示浏览器窗口（调试用），默认 `1` 无头 |
| `NH_CHROMIUM_PATH` | 否 | 指定 Chromium 可执行文件路径，默认用 Playwright 自带 |
| `NH_CAP_WAIT` | 否 | 等待 Cap 验证码通过的秒数上限 |
| `BROWSER_WORK_DIR` | 否 | 工作目录，默认 `/home/browser/browser-work`；Cookie/状态文件在 `<该目录>/profiles` 下 |
| `NH_SITE_RESET_HOUR` | 否 | 站点计费日切换小时（北京时间），默认 `8`；决定完成标记 `done-<站点日>` 的名字 |
| `NEOHEBERG_STRICT` | 否 | 设 `1` 启用 `set -u` 严格模式，尽早暴露变量名拼写错误（默认关闭）|
| `NEOHEBERG_GETPIP_SHA256` | 否 | get-pip.py 的期望 SHA256，提供后启用强校验；不提供则用 Python 语法编译做兜底校验 |

`DAFAGUO_SITE_RESET_HOUR` 是 `NH_SITE_RESET_HOUR` 的别名，两者取其一即可（多账号脚本读的是前者）。

### 多账号 / 看护调优

| 变量 | 默认 | 说明 |
|------|------|------|
| `DAFAGUO_MULTI_HOME` | `~/.local/share/dafaguo-multi` | 多账号数据根目录 |
| `DAFAGUO_WATCH_MAX_RESTARTS` | `5` | **单账号每小时最多自动拉起次数**，超出即熔断暂停（防止进程反复崩溃时每分钟无限重启）|
| `DAFAGUO_LOG_RETENTION_DAYS` | `7` | 日志保留天数，超期自动删除（每天执行一次）|

进程看护：每分钟检查一次，进程意外死亡时自动重新拉起。若发现完成标记 `done-<站点日>` 存在，则认为当日已正常刷满，不再拉起、也不重复推送 TG。手动触发 `multi-account.sh watch`，安装 cron：`multi-account.sh install-watch-timers`。

## 日志样例

```
🤖 启动 Chromium（Playwright）准备登录 + 提取 Cookie…
✍️ [1/2] 输入账号…
🔑 [2/2] 输入密码…
✅ 成功进入广告后台！准备提取 Cookie…
🍪 提取到 6 个 Cookie: ...
✅ 已拿到新鲜 Cookie（6 個），即将交接给底层挂机协议！
启动成功，缓存 Cookie 有效，当前余额: 13.740000
第 29 轮完成 (今日第 29 轮, 余额 14.244600, 本轮 +0.034600)
📌 已写完成标记 done-2026-10-02（看护 cron 不会再拉起）
```

## 常用命令

```bash
tail -f /root/dafaguo/neoheberg.log        # 实时日志（start.sh 启动时另有按日切分的 neoheberg-YYYY-MM-DD.log）
pkill -9 -f neoheberg.py                   # 停止
```
