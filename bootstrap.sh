#!/bin/bash
# ============================================================================
# CCSwitch-CommandCode-Setup 一键引导脚本（总脚本）
#
# 用法（把 <RAW_BASE> 换成仓库 raw 地址，如 GitHub：
#       https://raw.githubusercontent.com/<用户名>/<仓库>/main）：
#
#   curl -fsSL <RAW_BASE>/bootstrap.sh | bash
#   curl -fsSL <RAW_BASE>/bootstrap.sh | bash -s -- --verify        # 带参数
#
#   ./bootstrap.sh --base <RAW_BASE> [setup参数]                     # 本地运行
#   COMMANDCODE_SETUP_BASE=<RAW_BASE> ./bootstrap.sh [setup参数]
#
# 行为：只下载运行所需的最小文件集合（setup_commandcode.py）到临时目录，
# 运行结束后自动清理 —— 无需克隆/下载整个仓库再手动删除。
# ============================================================================
set -euo pipefail

BASE_URL="${COMMANDCODE_SETUP_BASE:-}"

# ---- 解析参数：--base <url> / 直接给 URL；其余透传给 setup_commandcode.py ----
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE_URL="$2"; shift 2 ;;
    http://*|https://*) BASE_URL="$1"; shift ;;
    *) args+=("$1"); shift ;;
  esac
done

die() { echo "❌ $1" >&2; exit 1; }

# ---- 环境检查 ----
[ "$(uname)" = "Darwin" ] || die "本脚本仅支持 macOS。"
command -v python3 >/dev/null 2>&1 || die "未找到 python3：请先安装 Xcode Command Line Tools（xcode-select --install）"
command -v curl >/dev/null 2>&1 || die "未找到 curl。"

if [ -z "$BASE_URL" ]; then
  cat >&2 <<'EOF'
❌ 未指定脚本来源（BASE_URL）。
   用法：curl -fsSL <RAW_BASE>/bootstrap.sh | bash
   或：  ./bootstrap.sh --base <RAW_BASE> [setup参数]
   也可设置环境变量 COMMANDCODE_SETUP_BASE=<RAW_BASE>。
   <RAW_BASE> 为仓库 raw 地址，需包含 bootstrap.sh 与 setup_commandcode.py。
EOF
  exit 1
fi

# strip 末尾斜杠
BASE_URL="${BASE_URL%/}"

# ---- 下载到临时目录 ----
TMP="$(mktemp -d /tmp/ccswitch-commandcode.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

echo "→ 从 $BASE_URL 下载脚本 …"
curl -fsSL "$BASE_URL/setup_commandcode.py" -o "$TMP/setup_commandcode.py" \
  || die "下载 setup_commandcode.py 失败：请检查 BASE_URL 是否正确（404 多为地址/分支写错）。"
[ "$(wc -c < "$TMP/setup_commandcode.py")" -gt 1000 ] || die "下载内容异常（文件过小），请检查 BASE_URL。"

echo "→ 启动配置脚本（结束后本临时目录会自动删除：${TMP}）…"
# curl|bash 时 stdin 是管道，交互输入改走 /dev/tty；
# 无控制终端（如 CI/沙盒）时回退 stdin（交互输入不可用，请用环境变量/参数提供 Key）
if [ -r /dev/tty ] && { true </dev/tty; } 2>/dev/null; then
  python3 "$TMP/setup_commandcode.py" ${args+"${args[@]}"} < /dev/tty
else
  python3 "$TMP/setup_commandcode.py" ${args+"${args[@]}"}
fi
