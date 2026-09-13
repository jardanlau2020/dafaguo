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
    from ruyipage import FirefoxPage, FirefoxOptions
except ImportError:
    print("❌ 缺少依赖！请先在终端执行: pip install ruyipage")
    sys.exit(1)
# =============================================================

# ════════════════════════════════════════════════════════════════════
# 全局配置
# ════════════════════════════════════════════════════════════════════
BASE = "https://dash.neoheberg.fr"
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
# 浏览器“先锋队”模块
# ════════════════════════════════════════════════════════════════════
class NeohebergLoginBot:
    def __init__(self):
        self.email = os.environ.get("EMAIL", "")
        self.password = os.environ.get("PASSWORD", "")
        self.proxy_url = os.environ.get("PROXY", "")
        self.profile_dir = os.environ.get("BROWSER_USER_DATA_DIR", "").strip()
        
    def run(self) -> dict:
        if not self.email or not self.password:
            log.error("❌ 浏览器获取 Cookie 失败：未配置 EMAIL 或 PASSWORD 环境变量！")
            return {}

        if self.profile_dir and os.path.exists(self.profile_dir):
            for lock_name in ['lock', '.parentlock', 'parent.lock']:
                lf = os.path.join(self.profile_dir, lock_name)
                if os.path.exists(lf):
                    try: os.remove(lf)
                    except: pass

        page = None
        try:
            log.info("🤖 启动 Firefox 浏览器准备全自动打盾提取 Cookie...")
            opts = FirefoxOptions()
            opts.set_browser_path("/root/.cache/ruyipage/browsers/firefox-155.0-v1.2.69-linux-x86_64/firefox/firefox")
            if self.profile_dir:
                opts.set_profile(self.profile_dir)
            if self.proxy_url:
                opts.set_proxy(self.proxy_url)
            
            opts.headless(False)
            page = FirefoxPage(opts)

            log.info(f"🔗 正在访问探路页面: {ADS_URL}")
            page.get(ADS_URL)
            time.sleep(5)

            if "Just a moment" in page.title or "잠시만" in page.title:
                log.info("🛡️ 遇到初始 Cloudflare 盾，尝试突破...")
                page.handle_cloudflare_challenge()
                time.sleep(5)

            identifier_input = page.ele('css:input#identifier')
            is_login_visible = identifier_input and identifier_input.is_displayed

            if not is_login_visible and "login" not in page.url:
                log.info("✅ 浏览器内未检测到登录框，似乎已经是登录状态！直接提取 Cookie。")
            else:
                log.info("🔄 需要登录，开始执行流程...")
                
                if not is_login_visible:
                    conn_btn = page.ele('text:Connexion') or page.ele('text:Login')
                    if conn_btn:
                        conn_btn.click()
                        time.sleep(3)
                        identifier_input = page.ele('css:input#identifier')

                if identifier_input:
                    for _ in range(20):
                        if identifier_input.is_displayed: break
                        time.sleep(0.25)
                        
                if identifier_input and identifier_input.is_displayed:
                    log.info("✍️ [1/2] 正在对准账号框输入...")
                    identifier_input.input(self.email, clear=True)
                    time.sleep(1)
                    
                    next_btn = page.ele('css:button#goToPassword')
                    if next_btn:
                        next_btn.click()
                        time.sleep(2) 
                        
                    pwd_input = page.ele('css:input#password')
                    if pwd_input:
                        for _ in range(20):
                            if pwd_input.is_displayed: break
                            time.sleep(0.25)

                    if pwd_input and pwd_input.is_displayed:
                        log.info("🔑 [2/2] 正在对准密码框输入...")
                        pwd_input.input(self.password, clear=True)
                        time.sleep(1)

                        remember_label = page.ele('css:label[for="remember_me"]')
                        if remember_label:
                            try: remember_label.click()
                            except: pass
                        time.sleep(1)

                        cf_passed = False
                        for cf_retry in range(5):
                            log.info(f"🛡️ [第 {cf_retry+1}/5 次] 调用浏览器官方 API 处理 CF 验证码...")
                            page.handle_cloudflare_challenge(timeout=15)
                            
                            try:
                                cf_input = page.ele('css:input[name="cf-turnstile-response"]')
                                if cf_input:
                                    for _ in range(10): 
                                        if cf_input.attr("value"):
                                            cf_passed = True
                                            break
                                        time.sleep(1)
                                else:
                                    cf_passed = True
                            except: pass
                                
                            if cf_passed: break  

                        if not cf_passed:
                            log.error("❌ 致命错误：获取 CF Token 失败。")
                            return {}

                        log.info("🚀 提交表单...")
                        submit_btn = page.ele('css:button[type="submit"]')
                        if submit_btn:
                            submit_btn.click()
                        else:
                            pwd_input.input('\n')
                        
                        time.sleep(8)
                        
                        if "Just a moment" in page.title or "잠시만" in page.title:
                            page.handle_cloudflare_challenge()
                            time.sleep(5)
                else:
                    return {}

            log.info("🔗 验证最终状态，前往广告后台...")
            page.get(ADS_URL)
            time.sleep(4)
            
            if "login" not in page.url:
                log.info("✅ 成功进入广告后台！准备窃取 Cookie...")
                cookies_dict = {}
                for c in page.cookies:
                    if isinstance(c, dict):
                        if c.get('name'): cookies_dict[c['name']] = c.get('value', '')
                    else:
                        c_name = getattr(c, 'name', '')
                        if c_name: cookies_dict[c_name] = getattr(c, 'value', '')
                return cookies_dict
            else:
                log.error("❌ 登录失败。")
                return {}

        except Exception as e:
            log.error(f"❌ 浏览器执行异常: {e}")
            return {}
        finally:
            if page: page.quit()


# ════════════════════════════════════════════════════════════════════
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