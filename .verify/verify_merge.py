# -*- coding: utf-8 -*-
"""dafaguo 合并后一致性验证。

覆盖 7 组断言，重点是「shell 层 ↔ python 层」的跨文件契约：
  1. shell 语法
  2. python 语法
  3. install.sh 所有 case 分支目标函数都已定义
  4. install.sh 调用的 multi-account.sh 子命令都存在
  5. **完成标记路径对齐**（neoheberg.py 写的 == multi-account.sh 查的）
  6. neoheberg.py 无未定义全局名 / 无重复顶层定义
  7. neoheberg.py 的第三方 import 都被 install.sh 装到
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)

fails = []


def ck(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (("   " + extra) if extra else ""))
    if not cond:
        fails.append(name)


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")


def funcs_of(text):
    return set(re.findall(r"^([a-z_][a-z0-9_]*)\(\)", text, re.M))


install = open("install.sh", encoding="utf-8").read()
multi = open("multi-account.sh", encoding="utf-8").read()
neo = open("neoheberg.py", encoding="utf-8").read()

# ── 1. shell 语法 ────────────────────────────────────────────────
print("\n[1] shell 语法")
for f in ("install.sh", "multi-account.sh", "start.sh"):
    r = sh(f"bash -n {f}")
    ck(f"bash -n {f}", r.returncode == 0, r.stderr.strip()[:120])

# ── 2. python 语法 ──────────────────────────────────────────────
print("\n[2] python 语法")
r = sh("python -m py_compile neoheberg.py")
ck("py_compile neoheberg.py", r.returncode == 0, r.stderr.strip()[:200])

# ── 3. install.sh case 分支目标函数都存在 ───────────────────────
print("\n[3] install.sh 的 case 分支目标函数")
defined = funcs_of(install)
# 只看 menu() 里的数字选择分支：  <n>) func ;;
menu_body = re.search(r"^menu\(\) \{(.*?)^\}", install, re.S | re.M).group(1)
BRANCH = re.findall(r"^\s*([0-9]+)\)\s+([a-z_][a-z0-9_]*)", menu_body, re.M)
BUILTINS = {"echo", "exit", "true", "false", "printf"}
called = {fn for _, fn in BRANCH if fn not in BUILTINS}
missing = sorted(c for c in called if c not in defined)
ck("menu() 数字分支无悬空函数引用", not missing, f"缺失: {missing}")
max_no = max(int(n) for n, _ in BRANCH)
ck("menu() 最大编号与提示 [0-9] 一致", max_no == 9, f"最大编号={max_no}")
ck("menu() 编号连续无空洞", sorted(int(n) for n, _ in BRANCH) == list(range(0, max_no + 1)),
   str(sorted(int(n) for n, _ in BRANCH)))
for m in ("menu_multi", "menu_multi_add", "menu_multi_setproxy", "require_multi"):
    ck(f"{m} 已定义", m in defined)
for m in ("menu_install", "menu_account", "menu_tg", "menu_balance", "menu_schedule",
          "menu_status", "menu_update", "menu_uninstall"):
    ck(f"{m} 仍在菜单中可达", m in called)

# ── 4. install.sh 调用的 multi-account.sh 子命令都存在 ──────────
print("\n[4] install.sh ↔ multi-account.sh 子命令契约")
KW = {"fi", "ok", "if", "then", "else", "return", "printf", "systemctl", "info", "warn", "err", "true", "false"}
invoked = set(re.findall(r'bash "\$MULTI_SCRIPT"\s+([a-z][a-z-]*)', install))
invoked |= set(re.findall(r'DAFAGUO_MULTI_HOME="\$MULTI_HOME"\s+bash\s+"\$MULTI_SCRIPT"\s+([a-z][a-z-]*)', install))
invoked -= KW
disp = re.search(r"^case\s+\S.*?\sin$(.*?)^esac$", multi, re.S | re.M)
subs = set(re.findall(r"^\s*([a-z][a-z-]*)", disp.group(1), re.M)) if disp else set()
unknown = sorted(i for i in invoked if i not in subs)
ck("install.sh 调用的子命令都在 multi-account.sh 中", not unknown,
   f"调用={sorted(invoked)}  未知={unknown}  已定义={sorted(subs)}")

# ── 5. 完成标记路径对齐（核心跨文件契约）────────────────────────
print("\n[5] 完成标记路径对齐（neoheberg.py 写 == multi-account.sh 查）")
# 只取代码行（去掉注释），避免注释里的中文标点被 exec 当语法错误
_start = neo.index("SITE_RESET_HOUR = int(")
_end = neo.index("TG_BOT_TOKEN")
blk = "\n".join(l for l in neo[_start:_end].splitlines() if not l.lstrip().startswith("#"))

PRELUDE = ("import os, time\nfrom datetime import datetime, timezone, timedelta\n"
           "TZ_BJ = timezone(timedelta(hours=8))\n"
           "BASE_WORK_DIR = os.environ.get('BROWSER_WORK_DIR')\n"
           "WORK_DIR = os.path.join(BASE_WORK_DIR, 'profiles')\n")

os.environ["BROWSER_WORK_DIR"] = "/tmp/acct/state"
os.environ.pop("NH_SITE_RESET_HOUR", None)
os.environ.pop("DAFAGUO_SITE_RESET_HOUR", None)

results = {}
for hour in (0, 3, 7, 8, 12, 23):
    fake_now = f"datetime(2026, 10, 2, {hour}, 30, tzinfo=timezone(timedelta(hours=8)))"
    src = PRELUDE + blk.replace("datetime.now(TZ_BJ)", fake_now)
    ns2 = {}
    exec(compile(src, "blk", "exec"), ns2)
    py_marker = os.path.basename(ns2["done_marker"]())
    results[hour] = py_marker
    exp_day = "2026-10-01" if hour < 8 else "2026-10-02"
    ck(f"北京时间 {hour:02d}:30 → done-{exp_day}", py_marker == f"done-{exp_day}", f"实得 {py_marker}")

# 与 multi-account.sh 的 site_day() 交叉验证：真跑 shell 函数（用 shell 函数覆盖 date）
# 注意：Windows 上 PATH 里的 `bash` 会解析到被安全策略拦掉的 WSL，必须用 Git Bash 绝对路径。
BASH = os.environ.get("GIT_BASH") or r"C:/Users/HiWin10/.workbuddy-ai/binaries/PortableGit/versions/1.2.0/bin/bash.exe"
site_day_body = re.search(r"^site_day\(\) \{(.*?)^\}", multi, re.S | re.M).group(1)

TMP = tempfile.mkdtemp()
harness = os.path.join(TMP, "sd.sh")
with open(harness, "w", encoding="utf-8", newline="\n") as f:
    f.write("""#!/usr/bin/env bash
# 覆盖 date：让 site_day() 在受控的"当前时间"下运行
date() {
  case "$*" in
    "+%-H")             echo "$FAKE_H" ;;
    "-d 'yesterday' +%F"|'-d yesterday +%F') echo "$FAKE_YESTERDAY" ;;
    "+%F")              echo "$FAKE_TODAY" ;;
    *) echo "UNEXPECTED date $*" >&2; return 1 ;;
  esac
}
""")
    # site_day_body 是函数体（含 `local`），必须包回函数定义再调用，
    # 否则 `local` 在顶层非法 → if 分支永不执行 → 永远返回今天（假失败）。
    f.write("site_day() {")
    f.write(site_day_body)
    f.write("}\n\nsite_day\n")

cross = {}
for hour in (0, 3, 7, 8, 12, 23):
    env = dict(os.environ, FAKE_H=str(hour), FAKE_YESTERDAY="2026-10-01", FAKE_TODAY="2026-10-02")
    r = subprocess.run([BASH, harness], capture_output=True, text=True, env=env)
    cross[hour] = r.stdout.strip()
    ck(f"shell site_day() @{hour:02d} 时 == python 端",
       r.stdout.strip() == results[hour].replace("done-", ""),
       f"shell={r.stdout.strip()!r} python={results[hour]!r} stderr={r.stderr.strip()[:80]}")
shutil.rmtree(TMP, ignore_errors=True)
# 直接从 multi-account.sh 里抽出 site_day() 真跑，而不是复述它的逻辑
sh_site_day = re.search(r"^site_day\(\) \{(.*?)^\}", multi, re.S | re.M).group(1)
ck("multi-account.sh 的 site_day 用 h < reset 判据", "h < reset" in sh_site_day or "h <" in sh_site_day)
ck("multi-account.sh 的 site_day 取昨天用 date -d yesterday",
   "date -d 'yesterday'" in sh_site_day or 'date -d "yesterday"' in sh_site_day)

ck("路径形态与看护端一致 ($dir/state/profiles/done-*)",
   ns2["done_marker"]().replace("\\", "/").startswith("/tmp/acct/state/profiles/done-"),
   ns2["done_marker"]())
# 看护端拼出来的完整路径
ck("看护端拼的路径与 python 端同构",
   re.search(r'done_marker="([^"]+)"', multi).group(1).replace("\\", "/") == "$dir/state/profiles/done-$(site_day)",
   re.search(r'done_marker="([^"]+)"', multi).group(1))

# 默认 reset hour 必须与 multi-account.sh 一致
ma_default = re.search(r"reset=\$\{DAFAGUO_SITE_RESET_HOUR:-(\d+)\}", multi)
py_default = re.search(r'or os\.environ\.get\("DAFAGUO_SITE_RESET_HOUR"\) or "(\d+)"', neo)
ck("默认重置小时两边一致",
   bool(ma_default and py_default) and ma_default.group(1) == py_default.group(1),
   f"multi-account={ma_default.group(1) if ma_default else '?'}  neoheberg={py_default.group(1) if py_default else '?'}")

# 写标记 & 读标记都在代码里
ck("main() 会检查完成标记", "os.path.exists(_dm)" in neo)
ck("LIMIT_REACHED 会写完成标记", "写完成标记" in neo and "os.chmod(_dm, 0o600)" in neo)
ck("看护端查的是同一个文件名模板",
   'done-$(site_day)' in multi and 'done-{site_day()}' in neo)

# ── 6. neoheberg.py 静态检查 ───────────────────────────────────
print("\n[6] neoheberg.py 静态检查")
tree = ast.parse(neo)
defined_g = set(dir(__builtins__)) | {"__name__", "__file__", "__doc__"}


def harvest(nodes):
    for n in nodes:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined_g.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    defined_g.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            defined_g.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                defined_g.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.Try, ast.If, ast.With, ast.For, ast.While)):
            harvest(n.body)
            for attr in ("orelse", "finalbody", "handlers"):
                for h in getattr(n, attr, None) or []:
                    harvest(h.body if isinstance(h, ast.ExceptHandler) else [h])


harvest(tree.body)
undef = {}


class V(ast.NodeVisitor):
    def __init__(self):
        self.stack = [set()]

    def visit_FunctionDef(self, node):
        local = set()
        for a in ast.walk(node):
            if isinstance(a, ast.arg):
                local.add(a.arg)
            if isinstance(a, ast.Name) and isinstance(a.ctx, ast.Store):
                local.add(a.id)
            if isinstance(a, (ast.FunctionDef, ast.ClassDef)) and a is not node:
                local.add(a.name)
            if isinstance(a, (ast.Import, ast.ImportFrom)):
                for n2 in a.names:
                    local.add((n2.asname or n2.name).split(".")[0])
            if isinstance(a, ast.ExceptHandler) and a.name:
                local.add(a.name)
            if isinstance(a, ast.Lambda):
                for p in a.args.args:
                    local.add(p.arg)
        self.stack.append(local | self.stack[-1])
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id not in defined_g and node.id not in self.stack[-1]:
            undef.setdefault(node.id, []).append(node.lineno)


V().visit(tree)
ck("无未定义全局名", not undef, str({k: v[:3] for k, v in undef.items()}))
from collections import Counter
names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
dups = {k: v for k, v in Counter(names).items() if v > 1}
ck("无重复顶层定义", not dups, str(dups))

# ── 7. 依赖闭合 ────────────────────────────────────────────────
print("\n[7] 依赖闭合（neoheberg.py import ⊆ install.sh 安装）")
third = set()
for n in ast.walk(tree):
    if isinstance(n, ast.ImportFrom) and n.module:
        third.add(n.module.split(".")[0])
    elif isinstance(n, ast.Import):
        for a in n.names:
            third.add(a.name.split(".")[0])
stdlib = set(sys.stdlib_module_names)
ext = sorted(m for m in third if m not in stdlib and m not in ("curl_cffi",) or m == "curl_cffi")
installed_py = set()
for m in re.findall(r"\$pip install[^\n|]*?--quiet\s+([a-z_0-9 ]+)", install):
    installed_py |= set(m.split())
installed_py |= set(re.findall(r"pip install --quiet (curl_cffi|playwright)", install))
need = {m for m in ext if m in ("curl_cffi", "playwright", "requests", "ruyipage")}
ck("neoheberg.py 需要的外部包都在 install.sh 里装", need <= installed_py,
   f"需要={sorted(need)}  安装={sorted(installed_py)}")
ck("install.sh 不再装无用的 ruyipage", "ruyipage" not in installed_py, str(sorted(installed_py)))
ck("install.sh 有装 playwright chromium 运行时", "playwright install --with-deps chromium" in install)

# ── 8. REPO_RAW 指向本仓库 ─────────────────────────────────────
print("\n[8] REPO_RAW 指向")
ck("REPO_RAW 指向 jardanlau2020/dafaguo", "jardanlau2020/dafaguo" in re.search(r"REPO_RAW=.*", install).group(0),
   re.search(r"REPO_RAW=.*", install).group(0)[:90])
ck("install.sh 无残留 xxbb678 硬编码", "xxbb678" not in install)

# ── 9. 引擎一致性：fork = curl_cffi + playwright/Chromium ──────
# 上游是 ruyipage + Firefox；合并若把上游的 Firefox 安装/自检/沙箱变量带进来，
# 而 do_install_py_deps 已不再装 ruyipage，安装会在 `python -m ruyipage install` 处直接失败。
print("\n[9] 引擎一致性（无 ruyipage / Firefox 残留）")
startsh = open("start.sh", encoding="utf-8").read()
readme = open("README.md", encoding="utf-8").read()


def installs_ruyipage(t):
    # 只看可执行行：整行注释里提到 "ruyipage install"（说明为何不用它）不算违规
    code = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    return bool(re.search(r"ruyipage\s+install", code)) or bool(re.search(r"pip install[^\n]*\bruyipage\b", code))


ck("install.sh 不再安装/下载 ruyipage Firefox 运行时", not installs_ruyipage(install))
ck("install.sh 不再设置 MOZ_* 沙箱变量", "MOZ_DISABLE" not in install)
ck("start.sh 不再设置 MOZ_* 沙箱变量", "MOZ_DISABLE" not in startsh)
ck("multi-account.sh 不再设置 MOZ_* 沙箱变量", "MOZ_DISABLE" not in multi)
ck("install.sh 有 Chromium 冒烟自检（check_chromium_launch）", "check_chromium_launch" in install)
ck("install.sh 不再引用已删除的 do_install_firefox_libs", "do_install_firefox_libs" not in install)
ck("neoheberg.py 用 playwright + curl_cffi 且不含 ruyipage",
   "playwright" in neo and "curl_cffi" in neo and "ruyipage" not in neo)

# ── 10. README ↔ 代码一致性 ────────────────────────────────────
print("\n[10] README 与代码一致")
ck("README 安装 URL 指向 jardanlau2020/dafaguo", "jardanlau2020/dafaguo" in readme)
ck("README 无 xxbb678 残留", "xxbb678" not in readme)
ck("README 无 ruyipage 安装指令", not installs_ruyipage(readme))

# README 里记录的子命令必须真实存在于 install.sh 的 case 分派
_case = re.search(r"^case \S+ in$(.*?)^esac$", install, re.S | re.M)
actual_cli = set()
for grp in re.findall(r"^\s*([a-z][a-z|]*)\)", _case.group(1), re.M):
    actual_cli |= set(grp.split("|"))
doc_cli = set(re.findall(r"bash install\.sh ([a-z]+)", readme))
ck("README 记录的子命令都真实存在", doc_cli <= actual_cli,
   f"README={sorted(doc_cli)}  实际={sorted(actual_cli)}")
ck("README 不再教不存在的 run 子命令", "run" not in doc_cli)

# README 提到的菜单编号，描述必须与 install.sh menu() 的 echo 一致
_menu_entries = {int(n): d.strip() for n, d in
                 re.findall(r'\[(\d)\]\$\{NC\}\s+([^"]+)"', menu_body)}
for n, desc in re.findall(r"\*\*\[(\d)\]\s*([^*]+?)\s*\*\*", readme):
    n = int(n)
    ck(f"README 菜单 [{n}] 描述与 install.sh 一致",
       _menu_entries.get(n) == desc, f"README={desc!r}  install={_menu_entries.get(n)!r}")

print("\n" + "=" * 56)
if fails:
    print(f"❌ 失败 {len(fails)} 项：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
