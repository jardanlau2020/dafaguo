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
    os.chmod(WORK_DIR, 0o777)
except Exception:
    pass

STATE_FILE = os.path.join(WORK_DIR, "neoheberg_afk_state.json")

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
    # 多台机器区分：配了 NOTIFY_NAME 就加一行标识头
    if NOTIFY_NAME:
        text = f"🖥️ <b>{NOTIFY_NAME}</b>\n{text}"
    try:
        data = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                                      data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        pass

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
        os.chmod(STATE_FILE, 0o666)
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

            ident = pg.query_selector("input#identifier")
            if ident is None:
                log.info("✅ 未見到登录框（可能已是登录态），直接去后台验证…")
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

            clean_text = re.sub(r'<[^>]+>', ' ', r.text)
            m = re.search(r'Solde\s*([\d,\.]+)\s*coins', clean_text, re.IGNORECASE)
            if not m: m = re.search(r'Balance\s*([\d,\.]+)\s*coins', clean_text, re.IGNORECASE)
            if not m: m = re.search(r'([\d,\.]+)\s*coins', clean_text, re.IGNORECASE)

            if m: return float(m.group(1).replace(",", "."))
            last_err = RuntimeError("页面中未找到余额")
            log.warning("⚠️ 未匹配到余额字样，第 %d/5 次重试...", attempt + 1)
            time.sleep(5)
        except PermissionError:
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
            
        # 【强迫症修改】：完全屏蔽错误提示，遇到拦截静默返回 "WAIT"
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
    ts = datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M 北京时间")
    msg = (f"🪄 NeoHeberg AFK\n📅 {ts}\n\n💰 <b>余额</b>: {balance:.4f} 🪙\n📈 <b>今日收益</b>: +{earned:.4f} 🪙（今日 {state.get('day_rounds', 0)} 轮）")
    send_tg(msg)
    log.info("TG 报告: 余额=%s 今日收益=%s", balance, earned)

def run_browser_extractor(state: dict) -> requests.Session:
    log.warning("⚠️ 呼叫浏览器先锋队提取全新 Cookie...")
    bot = NeohebergLoginBot()
    fresh_cookies = bot.run()
    if fresh_cookies:
        state["saved_cookies"] = fresh_cookies
        save_state(state)
        log.info("✅ 已拿到新鲜 Cookie，即将交接给底层挂机协议！")
        return make_session(state)
    else:
        msg = "❌ 浏览器获取 Cookie 失败！不再死等，向面板报告任务异常退出。"
        send_tg(msg)
        sys.exit(1)  

def main() -> None:
    state = load_state()
    # 单日轮次计数：仅在日期变化时清零；同一天内重启继续累加（rounds 仍为跨天累计值）
    _today = datetime.now(TZ_BJ).strftime("%Y-%m-%d")
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
            except PermissionError:
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
                if earned is not None:
                    msg = (f"🎉 NeoHeberg 今日挂机结束\n\n"
                           f"✅ 广告额度：100/100 已刷满\n"
                           f"🔄 今日轮次：{state.get('day_rounds', 0)} 轮\n"
                           f"💰 收盘余额：{bal_end:.4f} 🪙\n"
                           f"📈 今日收益：+{earned:.4f} 🪙")
                else:
                    msg = (f"🎉 NeoHeberg 今日挂机结束\n\n"
                           f"✅ 广告额度：100/100 已刷满\n"
                           f"🔄 今日轮次：{state.get('day_rounds', 0)} 轮\n"
                           f"⚠️ 余额读取失败，收益未能结算")
                log.info(msg.replace("\n", " | "))
                send_tg(msg)
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
                    send_tg(f"⚠️ NeoHeberg 已连续 {zs} 次兑换未入账\n💰 当前余额：{(cur if cur is not None else 0.0):.4f} 🪙\n（广告结算链路可能异常，非脚本故障）")
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