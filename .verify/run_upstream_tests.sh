#!/usr/bin/env bash
# 在 Windows / Git-Bash 上运行上游 tests/test_multi_account.sh。
#
# 为什么需要 shim：Git Bash 的 chmod 在 NTFS 上不生效，文件永远显示 644/666，
# 于是 assert_mode(...,600) 必失败。这里把 chmod 的「意图权限」记到 sidecar 文件，
# stat -c '%a' 时再读回来——权限断言之外的所有逻辑断言照常真跑。
#
# 用法:  bash .verify/run_upstream_tests.sh
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$HERE"

SHIM=$(mktemp -d)
MAP="$SHIM/.modes"
: > "$MAP"

REAL_STAT=$(command -v stat)
REAL_CHMOD=$(command -v chmod)
export REAL_STAT REAL_CHMOD MODE_MAP="$MAP"

cat > "$SHIM/chmod" <<'SH'
#!/usr/bin/env bash
# 记录意图权限后转交真实 chmod（NTFS 上真实 chmod 可能无效果，但不影响断言）
if [ "${1:-}" = "-R" ]; then shift; fi
mode="${1:-}"; shift || true
for f in "$@"; do printf '%s\t%s\n' "$f" "$mode" >> "$MODE_MAP"; done
exec "$REAL_CHMOD" "$mode" "$@" 2>/dev/null || true
SH

cat > "$SHIM/stat" <<'SH'
#!/usr/bin/env bash
# 只拦截 stat -c '%a'；命中记录则回放意图权限，否则转交真实 stat
if [ "${1:-}" = "-c" ] && [ "${2:-}" = "%a" ] && [ -n "${3:-}" ]; then
    m=$(awk -F'\t' -v f="$3" '$1==f{m=$2} END{if(m!="")print m}' "$MODE_MAP" 2>/dev/null)
    if [ -n "$m" ]; then echo "$m"; exit 0; fi
fi
exec "$REAL_STAT" "$@"
SH

chmod +x "$SHIM/chmod" "$SHIM/stat"

PATH="$SHIM:$PATH" bash tests/test_multi_account.sh
rc=$?
rm -rf "$SHIM"
exit $rc
