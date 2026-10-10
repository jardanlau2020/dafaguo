#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys

# ================= 强制干预环境：严禁 Python 把操控浏览器的内网通讯发给面板代理 =================
os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

# 北京时间 (UTC+8)
TZ_BJ = timezone(timedelta(hours=8))

# ================= 导入防指纹请求库与浏览器自动化库 =================
try:
    from curl_cffi import requests, CurlOpt
except ImportError:
    print("❌ 缺少依赖！请先在终端执行: pip install curl_cffi")
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("❌ 缺少依赖！请先执行: pip install playwright && python -m playwright install chromium")
    sys.exit(1)
# =============================================================

# ════════════════════════════════════════════════════════════════════
# 全局配置
# ════════════════════════════════════════════════════════════════════
BASE = "https://dash.neoheberg.fr"
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
ADS_URL = f"{BASE}/shop/ads"
LOGIN_URL = f"{BASE}/login"

BASE_WORK_DIR = os.environ.get("BROWSER_WORK_DIR", "/home/browser/browser-work")
WORK_DIR = os.path.join(BASE_WORK_DIR, "profiles")
os.makedirs(WORK_DIR, exist_ok=True)
try:
    os.chmod(WORK_DIR, 0o755)
except Exception:
    pass

STATE_FILE = os.path.join(WORK_DIR, "neoheberg_afk_state.json")

# ── 站点计费周期（与 multi-account.sh 的 site_day() 必须同一套算法）────────────
# 站点每日额度在北京时间 08:00 重置。08:00 之前触发时站点仍显示「昨天」的
# 100/100；若直接拿北京日期做完成标记，会误判「今天已完成」并写死标记，
# 结果当天剩余时间全部不挂机，而且状态一切正常、无人察觉。
# multi-account.sh 读 DAFAGUO_SITE_RESET_HOUR，这里两个名字都认，避免两边跑偏。
SITE_RESET_HOUR = int(os.environ.get("NH_SITE_RESET_HOUR")
                      or os.environ.get("DAFAGUO_SITE_RESET_HOUR") or "8")


def site_day() -> str:
    """当前属于站点的哪个计费日（北京 08:00 为界），返回 YYYY-MM-DD。

    ⚠️ 每次调用都要重算，不能在模块加载时算一次存起来：跨 08:00 运行的长任务
    若沿用启动时的日期，写出的标记与看护端要查的名字会差一天 → 看护认不出
    「正常收工」，每分钟重新拉起一次并重复推送 TG 战报。
    """
    bj = datetime.now(TZ_BJ)
    if bj.hour < SITE_RESET_HOUR:
        bj = bj - timedelta(days=1)
    return bj.strftime("%Y-%m-%d")


def done_marker() -> str:
    """今日完成标记的路径。

    用途：刷满 100 条后进程是**正常收工退出**，但 multi-account.sh 的每分钟看护
    cron 只看 PID 存活，会把干净退出误判成「意外死亡」而反复拉起（上游 f0fb741
    就是这个 bug）。写上这个标记后，启动时与看护端都会先查它。
    """
    return os.path.join(WORK_DIR, f"done-{site_day()}")


TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "")

# 通知名称：多台机器共用同一个 TG 机器人时，用来区分是哪台在报警
NOTIFY_NAME   = os.environ.get("NOTIFY_NAME", "").strip()

NH_WAIT       = int(os.environ.get("NH_WAIT", "65"))   
if NH_WAIT < 60:
    NH_WAIT = 65

# 恢复成激进抢单模式，休息 7 秒就立刻去敲门
SETTLE_SECONDS = 7   

NH_UA         = os.environ.get("NH_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
RETRY_COOLDOWN = 30  

logging.Formatter.converter = lambda *args: datetime.now(TZ_BJ).timetuple()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("neoheberg-afk")

def send_tg(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    # 多台機器區分（NOTIFY_NAME）已併入 tg_head() 表頭，唔再另開一行水印
    try:
        data = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                                      data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log.warning("TG 通知失败: %s", e)


def now_local() -> str:
    """UTC+8 當地時間 MM-DD HH:MM（獨立 helper，方便日後重用）"""
    return datetime.now(TZ_BJ).strftime("%m-%d %H:%M")


def tg_head(status: str) -> str:
    """瘦身通知表頭：🎮 服務（節點名）｜ MM-DD HH:MM ｜ 狀態"""
    svc = f"NeoHeberg（{NOTIFY_NAME}）" if NOTIFY_NAME else "NeoHeberg"
    return f"🎮 {svc} ｜ {now_local()} ｜ {status}"


def msg_report(balance: float, earned: float, rounds: int, first: bool = False) -> str:
    """上線／掛機心跳：表頭一行 ＋ 餘額收益一行"""
    return (f"{tg_head('✅ 已上線' if first else '✅ 掛機中')}\n"
            f"💰 {balance:.4f} 🪙 · 今日 +{earned:.4f} 🪙（{rounds} 輪）")


def msg_dayend(bal_end: Optional[float], earned: Optional[float], rounds: int) -> str:
    """今日額度刷滿收工：表頭一行 ＋ 結算一行"""
    head = tg_head("✅ 今日刷滿 100/100")
    if bal_end is None or earned is None:
        return f"{head}\n⚠️ 收盤餘額讀取失敗，今日收益未能結算"
    return f"{head}\n💰 收盤 {bal_end:.4f} 🪙 · 今日 +{earned:.4f} 🪙（{rounds} 輪）"


def msg_login_failed() -> str:
    """兩條登入路都死：表頭一行 ＋ 對策一行"""
    return (f"{tg_head('❌ 無法登入')}\n"
            f"💡 種子 NH_COOKIE 已失效、真瀏覽器登入亦失敗；抄新 Cookie 貼入 secret NH_COOKIE"
            f"（Cap/CF 診斷見 log）")


def msg_zero_gain(zs: int, balance: Optional[float]) -> str:
    """連續兌換未入賬告警：表頭一行 ＋ 原因一行"""
    bal = balance if balance is not None else 0.0
    return (f"{tg_head(f'⚠️ 連續 {zs} 次兌換未入賬')}\n"
            f"💰 {bal:.4f} 🪙 · 廣告結算鏈路可能異常（非腳本故障）")


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"start_balance": None, "day_start_balance": None, "total": 0.0, "rounds": 0, "day_rounds": 0, "last_report": 0, "last_balance": None, "zero_gain_streak": 0, "saved_cookies": {}}

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
        os.chmod(STATE_FILE, 0o644)
    except Exception:
        pass

# ════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════
# 浏览器“先锋队”模块（Playwright + Chromium）
#
# 2026-09-17 重写：站方登录闸**唔系** Cloudflare Turnstile，而系自架 Cap 验证码
# （cap-widget v0.1.57，endpoint https://trycap.axel-l.fr/6a74828fe6/，PoW + 行为探针）。
# 旧版死等一个唔存在嘅 `cf-turnstile-response`，所以永远交唔到 `cap-token` → 必败。
#
# 实测通过嘅新流程（真 Chromium，机房 IP 都过）：
#   填 email → 按「继续」→ 填密码 → **点击 cap-widget**
#   → widget 自己跑 sha256 PoW（约 5~6 秒）并填好 `input[name=cap-token]` → 提交。
# ════════════════════════════════════════════════════════════════════
class NeohebergLoginBot:
    """用真浏览器完成登录（过 CF 盾 + 点 Cap 验证码），回传 Cookie 字典。"""

    def __init__(self):
        self.email = os.environ.get("EMAIL", "").strip()
        self.password = os.environ.get("PASSWORD", "")
        self.proxy_url = os.environ.get("PROXY", "").strip()
        self.headless = os.environ.get("NH_HEADLESS", "1").strip() != "0"
        self.chromium_path = os.environ.get("NH_CHROMIUM_PATH", "").strip()
        self.cap_wait = int(os.environ.get("NH_CAP_WAIT", "60"))

    # ---------------------------- 工具 ----------------------------
    @staticmethod
    def _title(pg) -> str:
        try:
            return (pg.title() or "").lower()
        except Exception:
            return ""

    def _wait_past_cf(self, pg, timeout: int = 45) -> bool:
        """等 Cloudflare 托管盾自己过（真浏览器通常 3~10 秒自动放行）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = self._title(pg)
            if "just a moment" not in t and "un instant" not in t:
                return True
            time.sleep(1)
        return False

    @staticmethod
    def _cap_token(pg) -> str:
        try:
            return pg.evaluate(
                """() => { const e = document.querySelector('input[name="cap-token"]');"""
                """ return e ? (e.value || "") : ""; }"""
            ) or ""
        except Exception:
            return ""

    # ── Cap 診斷（2026-09-20）：widget 內部狀態 / 網絡事件，用嚟定案 cap-token 為何一直為空 ──
    @staticmethod
    def _cap_debug(pg, tag=""):
        try:
            info = pg.evaluate("""() => {
                const w = document.querySelector('cap-widget');
                if (!w) return {found: false};
                const sr = w.shadowRoot;
                const r = w.getBoundingClientRect();
                const out = {found: true, token_len: ((w.token || '') + '').length,
                             box: {x: Math.round(r.x), y: Math.round(r.y),
                                   w: Math.round(r.width), h: Math.round(r.height)},
                             html_len: sr ? (sr.innerHTML || '').length : 0};
                if (sr) {
                    out.text = (sr.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 160);
                    const cb = sr.querySelector('button, .checkbox, [role="checkbox"], input[type="checkbox"]');
                    if (cb) {
                        const cr = cb.getBoundingClientRect();
                        out.checkbox = {tag: cb.tagName, cls: (cb.className || '').slice(0, 40),
                                        x: Math.round(cr.x), y: Math.round(cr.y),
                                        w: Math.round(cr.width), h: Math.round(cr.height)};
                    }
                }
                const inp = document.querySelector('input[name="cap-token"]');
                out.input_found = !!inp;
                return out;
            }""")
            log.info("   [CAP-DEBUG%s] %s", tag, json.dumps(info, ensure_ascii=False)[:600])
            return info
        except Exception as e:
            log.warning("   [CAP-DEBUG%s] 讀取失敗: %s", tag, repr(e)[:120])
            return None

    def _click_cap_widget(self, pg, box) -> str:
        """點 Cap widget（純點擊，冇 solver）：優先 Playwright 直接選中 shadow DOM 內嘅 checkbox
        （Playwright 嘅 CSS 選擇器會穿透 open shadow root），失敗先退返真鼠標點座標。"""
        for sel in ("cap-widget button", "cap-widget .checkbox",
                    "cap-widget [role=checkbox]", "cap-widget input[type=checkbox]", "cap-widget"):
            try:
                pg.click(sel, timeout=4000)
                return f"playwright:{sel}"
            except Exception:
                continue
        if not box:
            return "fail:no_box"
        try:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            pg.mouse.move(box["x"] + 20, box["y"] + 15)
            time.sleep(0.35)
            pg.mouse.move(cx, cy, steps=12)
            time.sleep(0.25)
            pg.mouse.click(cx, cy)
            return "mouse:widget_center"
        except Exception as e:
            return f"fail:{repr(e)[:80]}"

    def _solve_cap(self, pg) -> bool:
        """点击 Cap widget，等佢自己跑完 PoW 并把 cap-token 填好。"""
        try:
            widget = pg.query_selector("cap-widget")
        except Exception:
            widget = None
        if widget is None:
            log.warning("⚠️ 页面揾唔到 cap-widget（站方可能改版），照样尝试直接提交…")
            return True
        self._cap_debug(pg, tag="[點擊前]")
        for attempt in range(1, 4):
            if self._cap_token(pg):
                return True
            try:
                box = widget.bounding_box()
            except Exception:
                box = None
            if not box:
                log.info("⏳ cap-widget 暂时唔可见（未到第二步？）等一等…")
                time.sleep(2)
                continue
            log.info(" [第 %d/3 次] 点击 Cap 验证码 widget …", attempt)
            log.info("   [CAP] 點擊方式: %s", self._click_cap_widget(pg, box))
            for sec in range(self.cap_wait):
                time.sleep(1)
                if self._cap_token(pg):
                    log.info("   ✅ %ds 拿到 cap-token", sec + 1)
                    return True
                if sec in (5, 15, 30):
                    self._cap_debug(pg, tag=f"[等了 {sec + 1}s]")
            log.warning("   本轮 %ds 内未拿到 cap-token，重试…", self.cap_wait)
            self._cap_debug(pg, tag=f"[第 {attempt} 次點擊後仍無 token]")
        try:
            pg.screenshot(path="cap_fail.png", full_page=True)
            log.info("   [CAP] 已保存失敗截圖 cap_fail.png")
        except Exception as e:
            log.warning("   [CAP] 截圖失敗: %s", repr(e)[:80])
        return bool(self._cap_token(pg))

    # ---------------------------- 主流程 ----------------------------
    @staticmethod
    def _accept_privacy_gate(pg, settle_ms: int = 7000) -> bool:
        """被 Privacy Policy/CGU 同意牆攔住就撳「J'accepte」，回傳有冇撳過。

        判據：只認 **submit 掣嘅文字**含「accepte」。頁面永遠同時有
        「déjà accepté…」（已簽）同「J'accepte…」（待簽）兩句，拿文案判會誤判。
        """
        try:
            blocked = _is_privacy_gate(pg.url, "")
            btn = pg.locator("button[type=submit]")
            n = btn.count()
            btn_txt = btn.first.inner_text().strip() if n else ""
            pending = "accepte" in btn_txt.lower()
            if not (blocked or pending):
                return False
            if not (pending and n):
                log.warning("⚠️ 疑似 CGU 牆（url=%s）但搵唔到可撳嘅 J'accepte 掣", pg.url)
                return False
            log.info("📋 偵測到 Privacy Policy/CGU 同意牆，撳「%s」…", btn_txt[:60])
            btn.first.click()
            pg.wait_for_timeout(settle_ms)
            log.info("✅ 已撳同意掣 → url=%s", pg.url)
            return True
        except Exception as e:
            log.warning("⚠️ 處理 CGU 同意牆時異常(%s): %s", type(e).__name__, e)
            return False

    def run(self) -> dict:
        if not self.email or not self.password:
            log.error("❌ 浏览器获取 Cookie 失败：未配置 EMAIL 或 PASSWORD 环境变量！")
            return {}

        pw = browser = None
        try:
            log.info("🤖 启动 Chromium（Playwright）准备登录 + 提取 Cookie…")
            pw = sync_playwright().start()
            launch_kw = {
                "headless": self.headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage",
                         "--disable-blink-features=AutomationControlled"],
            }
            if self.chromium_path:
                launch_kw["executable_path"] = self.chromium_path
            if self.proxy_url:
                launch_kw["proxy"] = {"server": self.proxy_url}
                log.info("🌐 浏览器走代理: %s", self.proxy_url.split("@")[-1])
            browser = pw.chromium.launch(**launch_kw)
            ctx = browser.new_context(
                user_agent=UA_CHROME,
                viewport={"width": 1366, "height": 900},
                locale="fr-FR",
                timezone_id="Europe/Paris",
            )
            pg = ctx.new_page()

            # ── 診斷（2026-09-20）：Cap 相關網絡請求 + JS 錯誤，用嚟查 cap-token 為何一直為空 ──
            _cap_kw = ("cap", "challenge", "redeem", "trycap")

            def _on_response(resp):
                try:
                    u = resp.url
                    if any(k in u for k in _cap_kw):
                        log.info("   [NET] %s %s %s", resp.status, resp.request.method, u[:130])
                except Exception:
                    pass

            def _on_failed(req):
                try:
                    u = req.url
                    if any(k in u for k in _cap_kw):
                        log.warning("   [NET-FAIL] %s %s", req.failure, u[:130])
                except Exception:
                    pass

            def _on_pageerror(err):
                log.warning("   [JS-ERROR] %s", str(err)[:200])

            def _on_console(msg):
                try:
                    if msg.type in ("error", "warning"):
                        log.info("   [CONSOLE:%s] %s", msg.type, msg.text[:200])
                except Exception:
                    pass

            pg.on("response", _on_response)
            pg.on("requestfailed", _on_failed)
            pg.on("pageerror", _on_pageerror)
            pg.on("console", _on_console)

            log.info("🔗 访问登录页: %s", LOGIN_URL)
            pg.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            self._wait_past_cf(pg)

            # 2026-10-10 修：原本「query_selector 搵唔到 input#identifier 就當已登入」太樂觀。
            # SPA 未 render 完、或者仲卡喺 CF/Cap 關卡時都會搵唔到 → 結果**從來冇填過帳密**
            # 就直去後台，被彈返 /login 收場（實證 run 38034799493：15:52:13「未見到登录框」
            # → 15:52:17「❌ 登录失败：访问广告后台被弹回登录页」，而 log 全程冇「输入账号」）。
            # 新邏輯：先輪詢等表單（3 次 × 10s，中間過一次 CF/Cap）；真係冇表單時，
            # **只有唔喺 /login 先當已登入**，仍然喺 /login 就明確報失敗，唔好靜默當成功。
            ident = None
            for _try in range(3):
                try:
                    ident = pg.wait_for_selector("input#identifier", timeout=10000)
                except Exception:
                    ident = None
                if ident is not None:
                    break
                log.info("⏳ 未見 login 表單（第 %d/3 次），可能仲喺 CF/Cap 關卡，等一等…", _try + 1)
                self._wait_past_cf(pg, timeout=15)
                time.sleep(2)

            if ident is None:
                if "/login" in (pg.url or ""):
                    log.error("❌ 等咗 30 秒都冇 login 表單（url=%s）→ 唔敢當已登入，報失敗", pg.url)
                    try:
                        pg.screenshot(path="login_form_missing.png")
                    except Exception:
                        pass
                    return {}
                log.info("✅ /login 冇表單但已唔喺 /login（url=%s）→ 當已登入，去後台驗證…", pg.url)
            else:
                log.info("✍️ [1/2] 输入账号…")
                pg.fill("input#identifier", self.email)
                time.sleep(0.6)
                nxt = pg.query_selector("button#goToPassword")
                if nxt:
                    nxt.click()
                else:
                    pg.press("input#identifier", "Enter")
                time.sleep(1.8)

                log.info("🔑 [2/2] 输入密码…")
                pg.wait_for_selector("input#password", state="visible", timeout=20000)
                pg.fill("input#password", self.password)
                time.sleep(0.5)
                try:
                    cb = pg.query_selector("input#remember_me")
                    if cb and not cb.is_checked():
                        cb.check()
                except Exception:
                    pass

                if not self._solve_cap(pg):
                    log.error("❌ 致命错误：Cap 验证码未能通过（cap-token 一直为空）。")
                    return {}

                log.info("🚀 提交登录表单…")
                btn = (pg.query_selector('form button[type="submit"]')
                       or pg.query_selector("form button:not([type])"))
                if btn:
                    btn.click()
                else:
                    pg.press("input#password", "Enter")
                pg.wait_for_timeout(6000)
                # 提交後 CF 可能再彈盾：畀耐啲（90 秒）並定時郁下鼠標，等 JS 盾自己過
                for _w in range(6):
                    if self._wait_past_cf(pg, timeout=15):
                        break
                    try:
                        pg.mouse.move(320 + _w * 7, 240 + _w * 5)
                    except Exception:
                        pass

                if "/login" in pg.url:
                    title = self._title(pg)
                    body = ""
                    try:
                        body = pg.inner_text("body").lower()
                    except Exception:
                        pass
                    log.error("❌ 提交後仍然停喺 /login｜title=%r｜url=%s", title, pg.url)
                    log.error("   body 前 220 字: %s", " ".join(body.split())[:220] or "(讀唔到 body)")
                    if "just a moment" in title or "un instant" in title:
                        log.error("   判定：CF 盾未過（提交後被 Cloudflare 攔住）。")
                    elif "identifiants invalides" in body:
                        log.error("   判定：账号或密码唔啱（Cap 已过，唔系验证码问题）。")
                    elif "captcha" in body:
                        log.error("   判定：仍然被 Cap 验证码挡（token 未被接受）。")
                    else:
                        log.error("   判定：未识别（睇上面 title / body）。")
                    return {}

            log.info(" 验证登录态：前往广告后台 %s", ADS_URL)
            pg.goto(ADS_URL, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(4000)
            self._wait_past_cf(pg)
            # Privacy Policy/CGU 同意牆（2026-10-05 站方更新）：唔撳就永遠入唔到
            # 廣告場 → 餘額讀 0、掛機零產出（10-05 空轉 5h48m 嘅真因）。
            # 移植自上游 xxbb678/dafaguo 10-06 嘅 accept_privacy_gate()，
            # 判據同我哋 nh-login-check 第四段一致（認 submit 掣文字，唔認文案）。
            self._accept_privacy_gate(pg)
            if "/login" in pg.url:
                log.error("❌ 登录失败：访问广告后台被弹回登录页。")
                return {}
            log.info("✅ 成功进入广告后台！准备提取 Cookie…")
            cookies = {}
            for c in ctx.cookies():
                if c.get("name"):
                    cookies[c["name"]] = c.get("value", "")
            log.info("🍪 提取到 %d 个 Cookie: %s", len(cookies),
                     ", ".join(sorted(cookies))[:200])
            return cookies

        except Exception as e:
            log.error(f"❌ 浏览器执行异常: {e}")
            return {}
        finally:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass# ════════════════════════════════════════════════════════════════════
# 底层 HTTP 极速挂机逻辑
# ════════════════════════════════════════════════════════════════════
class PrivacyGateRequired(RuntimeError):
    """站方 Privacy Policy / CGU 同意牆攔住（NeoHeberg 2026-10-05 起新增）。

    已登入帳號會被 302 去 `/account/cgu?next=/shop/ads`，頁面冇「Solde … coins」
    字樣。**唔可以當「餘額 0」** —— 上游 xxbb678/dafaguo 2026-10-06 同款教訓：
    當成 0 就會令 start_balance 記錯，之後成日收益統計全歪（實證：空轉 5h48m、
    run 零產出）。正確做法＝拋呢個專用例外，交由瀏覽器路徑撳一次「J'accepte」。

    判據刻意唔靠文案：頁面**永遠同時**有「déjà accepté …conditions」（已簽）同
    「J'accepte … Privacy Policy」（待簽）兩句，所以只認 URL 同 submit 掣文字。
    """


_GATE_URL_RE = re.compile(r"/account/cgu|/cgu\?", re.I)
_GATE_BTN_RE = re.compile(
    r'<button[^>]*type=["\']submit["\'][^>]*>[^<]{0,80}?accepte', re.I | re.S)


def _is_privacy_gate(url: str, html: str) -> bool:
    """呢一頁係唔係 Privacy Policy/CGU 同意牆？（純函數，方便單測）"""
    if url and _GATE_URL_RE.search(url):
        return True
    return bool(html and _GATE_BTN_RE.search(html))


def _get_balance(s: requests.Session) -> float:
    last_err = None
    for attempt in range(5):
        try:
            r = s.get(ADS_URL, timeout=20)
            # CF 瞬时拦截/限流：等一会儿重试，不要误判为 session 失效
            if r.status_code in (403, 429, 503):
                last_err = RuntimeError(f"CF拦截 status={r.status_code}")
                log.warning("⚠️ 余额检测被拦截 status=%s，第 %d/5 次重试...", r.status_code, attempt + 1)
                time.sleep(8 + attempt * 5)
                continue

            if "/login" in r.url or "Connexion" in r.text[:600].replace(" ", ""):
                raise PermissionError("session 过期")

            # Privacy Policy/CGU 同意牆：一定要同「讀唔到餘額」分開，
            # 唔可以當 0（否則 start_balance 記錯，成日統計全歪）。
            if _is_privacy_gate(r.url, r.text):
                raise PrivacyGateRequired(
                    f"被 Privacy Policy/CGU 同意牆攔住（url={r.url}）")

            clean_text = re.sub(r'<[^>]+>', ' ', r.text)
            m = re.search(r'Solde\s*([\d,\.]+)\s*coins', clean_text, re.IGNORECASE)
            if not m: m = re.search(r'Balance\s*([\d,\.]+)\s*coins', clean_text, re.IGNORECASE)
            if not m: m = re.search(r'([\d,\.]+)\s*coins', clean_text, re.IGNORECASE)

            if m: return float(m.group(1).replace(",", "."))
            last_err = RuntimeError("页面中未找到余额")
            log.warning("⚠️ 未匹配到余额字样，第 %d/5 次重试...", attempt + 1)
            time.sleep(5)
        except (PermissionError, PrivacyGateRequired):
            raise
        except Exception as e:
            last_err = e
            log.warning("⚠️ 余额请求异常(%s)，第 %d/5 次重试...", type(e).__name__, attempt + 1)
            time.sleep(8 + attempt * 5)
    raise last_err if last_err else RuntimeError("余额检测失败")

def _get_csrf(s: requests.Session) -> tuple:
    r = s.get(ADS_URL, timeout=20)
    if "/login" in r.url or "Connexion" in r.text[:600]:
        raise PermissionError("session 过期")
        
    m = re.search(r'name=["\'](csrf_token|_token)["\']\s+value=["\']([a-fA-F0-9]+)["\']', r.text)
    if m: return m.group(1), m.group(2)
    raise RuntimeError("csrf token 未找到")

def gen_callback(s: requests.Session, csrf_name: str, csrf_val: str) -> Optional[str]:
    data = {csrf_name: csrf_val}
    r = s.post(ADS_URL, data=data, allow_redirects=False, timeout=20)
    loc = r.headers.get("Location")
    
    if not loc:
        clean_text = re.sub(r'<[^>]+>', ' ', r.text)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        limit_m = re.search(r'(?:vues aujourd[\'’]hui|viewed today|今日).*?(\d+)\s*/\s*(\d+)', clean_text, re.IGNORECASE)
        if limit_m and limit_m.group(1) == limit_m.group(2):
            return "LIMIT_REACHED"
            
        # 无重定向且非满额：记录诊断信息后返回 WAIT
        log.debug("gen_callback: 无重定向, HTTP %d, body[:200]=%s", r.status_code, clean_text[:200])
        return "WAIT"
        
    m = re.search(r"url=([^&]+)", loc)
    if not m: return None
    return urllib.parse.unquote(m.group(1))

def redeem(s: requests.Session, callback: str) -> int:
    r = s.get(callback, headers={"Referer": "https://clipurl.fr/"}, timeout=20)
    return r.status_code

def make_session(state: dict) -> requests.Session:
    s = requests.Session(impersonate="chrome120")
    # 强制 IPv4：本机 WARP 只代理 IPv4，走原生 IPv6 会被 Cloudflare 403 拦截。
    # 可用 NH_FORCE_IPV6=1 关闭此行为。
    if os.environ.get("NH_FORCE_IPV6", "").strip() not in ("1", "true", "yes"):
        try:
            s.curl_options = {CurlOpt.IPRESOLVE: 1}
        except Exception:
            pass
    _px = os.environ.get("PROXY", "").strip()
    if _px:
        _pxh = _px.replace("socks5://", "socks5h://")
        s.proxies = {"http": _pxh, "https": _pxh}
    s.headers.update({
        "User-Agent": NH_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": BASE,
        "Referer": ADS_URL,
        "Upgrade-Insecure-Requests": "1"
    })
    
    saved_cookies = state.get("saved_cookies", {})
    for k, v in saved_cookies.items():
        s.cookies.set(k, v, domain="dash.neoheberg.fr")
    return s

def report(state: dict, balance: float, force: bool = False) -> None:
    now = time.time()
    if not force and now - state.get("last_report", 0) < 3600: return
    state["last_report"] = now
    save_state(state)
    earned = (balance - state.get("day_start_balance")) if state.get("day_start_balance") is not None else 0.0
    msg = msg_report(balance, earned, state.get("day_rounds", 0), first=force)
    send_tg(msg)
    log.info("TG 报告: 余额=%s 今日收益=%s", balance, earned)

def parse_cookie_seed(raw: str) -> dict:
    """解析 NH_COOKIE secret（`k=v; k=v` 格式）成 cookie dict。"""
    out = {}
    for part in (raw or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k:
                out[k] = v
    return out


def run_browser_extractor(state: dict) -> requests.Session:
    # ── 2026-09-20 定案：Cap 服務端本身被 Cloudflare 擋 ──────────────────
    # 實測（NAS 直連 curl）：POST https://trycap.axel-l.fr/6a74828fe6/challenge
    #   → HTTP 403 + `cf-mitigated: challenge` + <title>Just a moment...</title>
    # 即係話「驗證碼服務商嘅 API 自己被 CF 託管挑戰擋住」。瀏覽器入面，cap-widget
    # 對該 endpoint 嘅 cross-origin POST 因為 CF 回嘅挑戰頁冇 Access-Control-Allow-Origin
    # 而直接 CORS 失敗（run 35494907320 / 35496596633 / 35496911871 全部一樣：
    # [NET-FAIL] net::ERR_FAILED .../challenge + [JS-ERROR] Failed to fetch）。
    # 走代理（Luxvps TUIC）同直連（Azure runner）結果一樣 → 唔係換 IP 可以解決，
    # 機房 IP 由瀏覽器自動登入呢條路原理上封死。
    # 對策：用戶由自己瀏覽器貼一份 Cookie 入 secret NH_COOKIE，直接餵底層協議，
    # 完全跳過登入關卡（AFK 迴圈本來就只用 cookie，唔需要每次重新登入）。
    # ── 2026-09-24 更新：登入關卡已經通返，種子要驗證 + 自動退回瀏覽器 ──────
    # 今日實測：真瀏覽器（+代理）2 秒就過 CF 盾同自架 Cap，即係 09-20 嗰個
    # 「trycap 端點被 CF 擋」已經唔再成立。所以種子（NH_COOKIE）唔再係唯一出路：
    # 種子失效就自動退回真瀏覽器登入，唔好再好似 run 35964033696 咁 1 分鐘就死。
    # 順序：secret 種子（快，先驗）→ 真瀏覽器登入（慢，但係真嘅）→ 兩條都死先報紅。
    seed = parse_cookie_seed(os.environ.get("NH_COOKIE", ""))
    if seed:
        log.info("🔑 使用 NH_COOKIE secret 種子 Cookie（%d 個）", len(seed))
        state["saved_cookies"] = seed
        save_state(state)
        s = make_session(state)
        try:
            bal = _get_balance(s)
            log.info("✅ 種子 Cookie 有效（余额 %.4f），跳過登入關卡。", bal)
            return s
        except PermissionError:
            log.warning("⚠️ 種子 Cookie 已被伺服器淘汰（session 過期），自動退回真瀏覽器登入…")
        except Exception as e:
            log.warning("⚠️ 用種子驗證時遇到異常(%s): %s，照樣試真瀏覽器登入…", type(e).__name__, e)

    log.warning("⚠️ 呼叫浏览器先锋队提取全新 Cookie...")
    bot = NeohebergLoginBot()
    fresh_cookies = bot.run()
    if fresh_cookies:
        state["saved_cookies"] = fresh_cookies
        save_state(state)
        log.info("✅ 已拿到新鲜 Cookie（%d 個），即将交接给底层挂机协议！", len(fresh_cookies))
        return make_session(state)
    else:
        send_tg(msg_login_failed())
        sys.exit(1)

def main() -> None:
    # 今日已完成：直接退出，不登录、不重复推送 TG 战报。
    # 看护 cron（multi-account.sh watch）也会查同一个标记，所以这里退出是「正常收工」。
    # 刻意**不**删 STATE_FILE —— 里面存着 saved_cookies，删了下次要重跑浏览器登录。
    _dm = done_marker()
    if os.path.exists(_dm):
        log.info("ℹ️ 今日 100 条广告已刷满（完成标记 %s 存在），跳过本次启动。",
                 os.path.basename(_dm))
        sys.exit(0)

    state = load_state()
    # 单日轮次计数：按**站点计费日**（北京 08:00 为界）清零，与完成标记同一套日界，
    # 否则 00:00~08:00 之间重启会错误清零，TG 战报的「今日 N 轮」也会与站点对不上。
    _today = site_day()
    if state.get("day_date") != _today:
        state["day_rounds"] = 0
        # 切日：记录当日起点余额，用于计算真正的当日收益（下方读到余额后回填）
        state["day_start_balance"] = None
        state["day_date"] = _today
        log.info("新的一天 %s，单日轮次已归零", _today)
    else:
        log.info("同日重启，单日轮次继续累计：今日已 %s 轮", state.get("day_rounds", 0))
    save_state(state)
    s = make_session(state)

    ok = False
    try:
        if not state.get("saved_cookies"): raise PermissionError("首次运行无缓存")
        bal = _get_balance(s)
        if state.get("start_balance") is None: state["start_balance"] = bal
        if state.get("day_start_balance") is None: state["day_start_balance"] = bal
        state["last_balance"] = bal  # 为兑换真实性校验建立基准值
        log.info("启动成功，缓存 Cookie 有效，当前余额: %s", bal)
        report(state, bal, force=True)
        ok = True
    except PermissionError:
        log.warning("⚠️ 初次检测：发现未配置 Cookie 或缓存已作废！")
    except PrivacyGateRequired as e:
        # 唔係 cookie 壞，係站方要撳一次同意掣 → 交畀下面瀏覽器路徑處理
        log.warning("📋 初次检测：%s → 改用瀏覽器撳同意掣", e)
    except Exception as e:
        # 网络/CF 瞬时抖动：不要瞬退，重试两轮后再决定
        log.warning("⚠️ 启动余额检测失败(%s): %s，稍后重试...", type(e).__name__, e)
        for _ in range(3):
            time.sleep(15)
            try:
                s = make_session(state)
                bal = _get_balance(s)
                if state.get("start_balance") is None: state["start_balance"] = bal
                if state.get("day_start_balance") is None: state["day_start_balance"] = bal
                state["last_balance"] = bal
                log.info("启动成功(重试)，当前余额: %s", bal)
                report(state, bal, force=True)
                ok = True
                break
            except (PermissionError, PrivacyGateRequired):
                break
            except Exception as e2:
                log.warning("重试仍失败(%s): %s", type(e2).__name__, e2)
        if not ok and not state.get("saved_cookies"):
            pass

    if not ok:
        log.warning("⚠️ 缓存 Cookie 不可用，改用浏览器重新登录取 Cookie...")
        s = run_browser_extractor(state)
        try:
            bal = _get_balance(s)
            if state.get("start_balance") is None: state["start_balance"] = bal
            if state.get("day_start_balance") is None: state["day_start_balance"] = bal
            state["last_balance"] = bal
            log.info("✅ 新 Cookie 验证通过！当前余额: %s", bal)
            report(state, bal, force=True)
        except PermissionError:
            log.error("❌ 新 Cookie 仍然无效，退出。")
            sys.exit(1)
        except Exception as e:
            # 即使新 Cookie 验证撞上 CF 拦截，也先进入主循环，由重试逻辑接管
            log.warning("⚠️ 新 Cookie 验证异常(%s): %s，先进入主循环。", type(e).__name__, e)

    consecutive_fail = 0
    while True:
        try:
            csrf_name, csrf_val = _get_csrf(s)
            cb = gen_callback(s, csrf_name, csrf_val)
            
            if cb == "LIMIT_REACHED":
                # 收尾前再读一次真实余额，算准当日收益
                try:
                    bal_end = _get_balance(s)
                except Exception as e:
                    log.warning("⚠️ 收尾余额读取失败(%s)，沿用上次读数", type(e).__name__)
                    bal_end = state.get("last_balance")
                if bal_end is not None:
                    state["last_balance"] = bal_end
                    save_state(state)
                earned = (bal_end - state["day_start_balance"]) if (bal_end is not None and state.get("day_start_balance") is not None) else None
                msg = msg_dayend(bal_end, earned, state.get("day_rounds", 0))
                log.info(msg.replace("\n", " | "))
                send_tg(msg)
                # 写完成标记：告知看护 cron「这是正常收工，别再拉起」。
                # 名字用写入时刻的 site_day()，与看护端查询时用的一致。
                try:
                    _dm = done_marker()
                    with open(_dm, "w") as f:
                        f.write(f"{datetime.now(TZ_BJ).strftime('%Y-%m-%d %H:%M:%S')} 北京  "
                                f"余额 {bal_end if bal_end is not None else 'N/A'}  "
                                f"今日 {state.get('day_rounds', 0)} 轮\n")
                    os.chmod(_dm, 0o600)
                    log.info("📌 已写完成标记 %s（看护 cron 不会再拉起）", os.path.basename(_dm))
                except Exception as e:
                    log.warning("⚠️ 写完成标记失败（看护可能重复拉起）: %s", e)
                sys.exit(0)
                
            # 【终极优化】：遇到拦截（冷却中或缺货），完全不打红字，在后台默默等待 20 秒
            if cb == "WAIT" or not cb:
                time.sleep(20)
                continue
                
            time.sleep(NH_WAIT)
            st = redeem(s, cb)
                
            time.sleep(SETTLE_SECONDS)

            # 真实性校验：redeem 返回 200 不代表金币到账。
            # 只有余额真的增长才算一轮成功，避免虚增轮次造成"在跑但不涨"的假象。
            prev = state.get("last_balance")
            new_bal = None
            try:
                new_bal = _get_balance(s)
            except Exception:
                pass

            if new_bal is not None and prev is not None and new_bal > prev:
                state["rounds"] += 1
                state["day_rounds"] = state.get("day_rounds", 0) + 1
                state["zero_gain_streak"] = 0
                consecutive_fail = 0
                state["last_balance"] = new_bal
                log.info("第 %d 轮完成 (今日第 %d 轮, 余额 %.4f, 本轮 +%.4f)",
                         state["rounds"], state["day_rounds"], new_bal, new_bal - prev)
            else:
                zs = state.get("zero_gain_streak", 0) + 1
                state["zero_gain_streak"] = zs
                if new_bal is not None:
                    state["last_balance"] = new_bal
                cur = new_bal if new_bal is not None else prev
                log.warning("第 %d 次兑换未入账（HTTP 200 但余额未增，当前 %.4f），累计 %d 次",
                            state.get("rounds", 0), (cur if cur is not None else 0.0), zs)
                if zs in (10, 30, 60):
                    send_tg(msg_zero_gain(zs, cur))
                time.sleep(RETRY_COOLDOWN)

        except PermissionError:
            s = run_browser_extractor(state)
            consecutive_fail = 0
            continue
        except Exception:
            time.sleep(RETRY_COOLDOWN)

        try:
            bal = _get_balance(s)
            report(state, bal)
            state["saved_cookies"] = s.cookies.get_dict()
            save_state(state)
        except Exception: pass

if __name__ == "__main__":
    main()