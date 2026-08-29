#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CCSwitch-CommandCode-Setup —— 一键把 CommandCode 订阅接入 Codex CLI（经 CC Switch 代理）。

流程：
  1. 校验用户输入的 CommandCode API Key，并识别订阅套餐（最低 GOAT，不支持 Go）
  2. 拉取上游全量模型目录（GET /provider/v1/models）
  3. 对每个模型发送 1-token 最小请求探测套餐可用性
     （200 = 可用收录；403 MODEL_NOT_IN_PLAN = 套餐不含剔除；
       429/503 = 上游临时问题，保留收录；Claude 系自动跳过）
  4. 把 CommandCode 提供商写入 CC Switch 数据库（幂等：已存在则原地更新）
  5. 重启 CC Switch，让其接管 Codex Live 配置并生成模型目录文件
  6. 可选 --verify：用干净的临时 CODEX_HOME 跑一次 codex exec 冒烟测试

仅限 macOS。仅依赖 Python 3 标准库。模型探测会消耗极少额度（每模型 1~16 token）。

用法示例：
  python3 setup_commandcode.py                       # 交互式（提示输入 Key）
  python3 setup_commandcode.py --dry-run             # 只预览，不写任何东西
  python3 setup_commandcode.py --verify              # 完成后自动跑 PONG 冒烟测试
  COMMANDCODE_API_KEY=user_xxx python3 setup_commandcode.py --yes   # 非交互
"""

import argparse
import getpass
import glob
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ---------------- 基础常量 ----------------

APP_NAME = "CC Switch"
APP_PATH = "/Applications/CC Switch.app"
PROC_NAME = "cc-switch"
HOME = os.path.expanduser("~")
DB_PATH = os.path.join(HOME, ".cc-switch", "cc-switch.db")
LOG_PATH = os.path.join(HOME, ".cc-switch", "logs", "cc-switch.log")
CODEX_DIR = os.path.join(HOME, ".codex")
CODEX_CONFIG = os.path.join(CODEX_DIR, "config.toml")
CODEX_CATALOG = os.path.join(CODEX_DIR, "cc-switch-model-catalog.json")
PROXY_HOST, PROXY_PORT = "127.0.0.1", 15721

API_BASE = "https://api.commandcode.ai"
UPSTREAM_BASE = API_BASE + "/provider/v1"
PATH_MODELS = "/provider/v1/models"
PATH_CHAT = "/provider/v1/chat/completions"
PATH_SUBS = "/alpha/billing/subscriptions"
PATH_WHOAMI = "/alpha/whoami"

DEFAULT_PROVIDER_NAME = "CommandCode"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_LEVELS = ["low", "medium", "high"]  # 非 KNOWN_EFFORTS 模型的兜底档位（上游已验证通用接受）

PROVIDER_WEBSITE = "https://commandcode.ai"
PROVIDER_CATEGORY = "third_party"
PROVIDER_ICON_COLOR = "#4F46E5"

# ---------------- 模型元数据（来源：dsh-commandcode-provider/src/adapter.ts，command-code@1.37.0 同步；2026-08-29 实测核对） ----------------

# 模型列表不靠内嵌清单：拉取上游全量目录后逐模型探测套餐可用性（见 probe_model），
# 上游新增/下架模型自动跟随。以下两表只负责"元数据标注"（思考档位、图像输入）。

# KNOWN_EFFORTS：官方标注了可选思考档位的模型（档位升序；默认档取最高档，与实测目录一致）。
# 不在表内的模型一律用 DEFAULT_LEVELS（上游网关对 effort 参数通用接受，实测仅 deepseek 拒绝 "none"）。
KNOWN_EFFORTS = {
    "Qwen/Qwen3.8-Max": ["low", "medium", "xhigh"],
    "Qwen/Qwen3.8-27B": ["low", "medium", "xhigh"],
    "Qwen/Qwen3.8-Flash": ["low", "medium", "xhigh"],
    "deepseek/deepseek-v4-flash": ["high", "max"],
    "deepseek/deepseek-v4-flash-vision-exp": ["high", "max"],
    "deepseek/deepseek-v4-pro": ["high", "max"],
    "google/gemini-3.1-flash-lite": ["low", "medium", "high"],
    "google/gemini-3.5-flash": ["low", "medium", "high"],
    "google/gemini-3.5-flash-lite": ["low", "medium", "high"],
    "google/gemini-3.6-flash": ["low", "medium", "high"],
    "google/gemini-3.7-flash": ["low", "medium", "high"],
    "gpt-5.3-codex": ["low", "medium", "high", "xhigh"],
    "gpt-5.4": ["low", "medium", "high", "xhigh"],
    "gpt-5.4-mini": ["low", "medium", "high"],
    "gpt-5.5": ["low", "medium", "high", "xhigh"],
    "gpt-5.6-luna": ["low", "medium", "high", "xhigh", "max"],
    "gpt-5.6-sol": ["low", "medium", "high", "xhigh", "max"],
    "gpt-5.6-terra": ["low", "medium", "high", "xhigh", "max"],
    "xai/grok-4.5": ["low", "medium", "high"],
    "xai/grok-4.6": ["low", "medium", "high", "xhigh"],
    "z-ai/glm-5.3-flash": ["low", "high", "max"],
    "zai-org/GLM-5.2": ["high", "max"],
    "zai-org/GLM-5.3": ["low", "high", "max"],
}

# KNOWN_IMAGE_MODELS：支持图像输入（inputModalities 含 image）。
KNOWN_IMAGE_MODELS = {
    "MiniMaxAI/MiniMax-M3", "Qwen/Qwen3.6-Plus", "Qwen/Qwen3.7-Flash", "Qwen/Qwen3.7-Plus",
    "Qwen/Qwen3.8-27B", "Qwen/Qwen3.8-Flash", "Qwen/Qwen3.8-Max",
    "deepseek/deepseek-v4-flash-vision-exp", "google/gemini-3.1-flash-lite", "google/gemini-3.5-flash",
    "google/gemini-3.5-flash-lite", "google/gemini-3.6-flash", "google/gemini-3.7-flash",
    "gpt-5.3-codex", "gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-sol",
    "meta/muse-spark-1.1", "meta/muse-spark-1.2", "meta/muse-spark-1.2-contributor",
    "minimax/minimax-m3-free", "moonshotai/Kimi-K2.5", "moonshotai/Kimi-K2.6",
    "moonshotai/Kimi-K2.7-Code", "moonshotai/Kimi-K2.7-Code-Highspeed", "moonshotai/Kimi-K3",
    "xiaomi/mimo-v2.5", "xiaomi/mimo-v2.5-pro", "z-ai/glm-5.3-flash", "xai/grok-4.5",
}

# meta：openai_chat 让 CC Switch 做 responses→chat 转换（上游只有 chat/completions，无 /responses）。
# codexChatReasoning 各字段为 CC Switch 认可的转换参数，勿随意改动。
PROVIDER_META = {
    "commonConfigEnabled": False,
    "endpointAutoSelect": True,
    "apiFormat": "openai_chat",
    "codexChatReasoning": {
        "supportsThinking": True,
        "supportsEffort": True,
        "thinkingParam": "none",
        "effortParam": "reasoning_effort",
        "effortValueMode": "zen",
        "outputFormat": "reasoning_content",
    },
}

CONFIG_TOML_TEMPLATE = """model_provider = "custom"
model = "{model}"
model_reasoning_effort = "{effort}"
disable_response_storage = true

[model_providers.custom]
name = "custom"
wire_api = "responses"
requires_openai_auth = true
base_url = "{base_url}"
"""

# ---------------- 输出辅助 ----------------

def log(msg=""):
    print(msg, flush=True)

def warn(msg):
    print(f"⚠️  {msg}", flush=True)

def die(msg, code=1):
    print(f"❌ {msg}", file=sys.stderr, flush=True)
    sys.exit(code)

# ---------------- HTTP ----------------

def _headers(key):
    return {"Authorization": f"Bearer {key}", "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": "ccswitch-commandcode-setup/1.0"}

def api_get(path, key, timeout=30):
    req = urllib.request.Request(API_BASE + path, headers=_headers(key), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), r.status
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {path} {body}") from None

def _try_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return raw

def _probe_http(model_id, key, body, timeout=60):
    data = json.dumps(body).encode()
    req = urllib.request.Request(API_BASE + PATH_CHAT, data=data, headers=_headers(key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _try_json(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, _try_json(e.read().decode("utf-8", "replace"))
    except Exception as e:
        return 0, f"网络异常: {e}"

PROBE_MARKS = {"OK": "✅", "NOT_IN_PLAN": "🚫", "UNSUPPORTED": "❌", "TRANSIENT": "⏳", "ERROR": "❌"}

def probe_model(model_id, key, attempts=3):
    """1-token 最小请求探测。返回 (类别, 说明)：
    OK=可用；NOT_IN_PLAN=套餐不含；UNSUPPORTED=上游拒绝；
    TRANSIENT=429/503 临时问题（保留收录）；ERROR=其他失败（剔除）。"""
    body = {"model": model_id, "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]}
    detail = ""
    for attempt in range(attempts):
        status, resp = _probe_http(model_id, key, body)
        if status == 200:
            return "OK", ""
        if status == 403:
            s = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
            return ("NOT_IN_PLAN", "套餐不含（MODEL_NOT_IN_PLAN）"
                    if "MODEL_NOT_IN_PLAN" in s else f"403: {s[:140]}")
        if status in (429, 503):
            detail = f"{status} 上游暂时不可用"
            time.sleep(3 * (attempt + 1))
            continue
        if status == 400:
            s = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
            if ">= 16" in s and body["max_tokens"] < 16:
                body["max_tokens"] = 16      # 部分模型要求 max_tokens >= 16
                continue
            return "UNSUPPORTED", s[:140]
        detail = f"{status}: {resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)}"[:140]
        time.sleep(2)
    if detail.startswith("429") or detail.startswith("503"):
        return ("TRANSIENT", detail)
    return ("ERROR", detail or "多次重试失败")

def probe_all(model_ids, key, workers=8):
    results, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe_model, mid, key): mid for mid in model_ids}
        for fut in as_completed(futs):
            mid = futs[fut]
            try:
                results[mid] = fut.result()
            except Exception as e:                       # 网络层异常
                results[mid] = ("ERROR", str(e)[:140])
            done += 1
            cls, note = results[mid]
            print(f"  [{done:>2}/{len(model_ids)}] {PROBE_MARKS.get(cls, '❌')} {mid:44} {note}", flush=True)
            time.sleep(0.2)                              # 温和限速
    return results

# ---------------- 业务逻辑 ----------------

def display_name(name):
    """去掉上游 (latest)/(exp) 后缀，得到干净显示名。"""
    return re.sub(r"\s*\((latest|exp)\)\s*$", "", name or "").strip()

def levels_for(slug):
    return list(KNOWN_EFFORTS.get(slug, DEFAULT_LEVELS))

def default_level(slug):
    return levels_for(slug)[-1]

def detect_plan(plan_id):
    p = (plan_id or "").lower()
    if "goat" in p:
        return "goat"
    if re.search(r"(^|[^a-z])go([^a-z]|$)", p):
        return "go"
    if "pro" in p:
        return "pro"
    if p:
        return "provider"
    return None

def build_entry(slug, upstream, ctx_fallback=128000):
    u = upstream.get(slug) or {}
    ctx = int(u.get("context_length") or 0)
    if ctx <= 0:
        warn(f"{slug}: 上游未提供 context_length，回退为 {ctx_fallback}")
        ctx = ctx_fallback
    return {
        "model": slug,
        "displayName": display_name(u.get("name")) or slug,
        "contextWindow": ctx,
        "reasoningLevels": levels_for(slug),
        "defaultReasoningLevel": default_level(slug),
        "inputModalities": ["text", "image"] if slug in KNOWN_IMAGE_MODELS else ["text"],
    }

def build_entries(upstream, key, extra_includes, skip_probe=False):
    """探测上游全量模型的套餐可用性，返回 (entries, excluded, notes)。
    判定与参考实现一致：OK/TRANSIENT 收录（429/503 属临时问题），
    NOT_IN_PLAN/UNSUPPORTED/ERROR 剔除；Claude 系一律剔除（只能走 Anthropic 路由）。"""
    notes, excluded = [], []
    if skip_probe:
        warn("已跳过探测（--skip-probe）：直接收录除 Claude 外的全部模型，其中失效模型在 Codex 里会报错。")
        results = {m: ("OK", "跳过探测") for m in upstream}
    else:
        log("\n→ 探测各模型在当前套餐下的可用性（每模型 1~16 token，8 并发，约 1~3 分钟）…")
        results = probe_all(list(upstream), key)
    entries = []
    for slug, (cls, note) in results.items():
        if slug.startswith("claude"):
            excluded.append((slug, "Claude 系只能走 Anthropic Messages 端点，无法经 chat/completions 使用"))
        elif cls in ("OK", "TRANSIENT"):
            entries.append(build_entry(slug, upstream))
            if cls == "TRANSIENT":
                notes.append(f"{slug}: 上游暂时不可用（{note}），已保留收录，请留意")
        else:
            excluded.append((slug, note))
    for slug in extra_includes:                       # --include 强制收录
        if slug not in upstream:
            notes.append(f"--include {slug}: 上游目录中不存在，忽略")
        elif slug.startswith("claude"):
            notes.append(f"--include {slug}: Claude 系无法经 chat/completions 使用，忽略")
        elif all(e["model"] != slug for e in entries):
            entries.append(build_entry(slug, upstream))
            notes.append(f"{slug}: 经 --include 强制收录")
    order = {m: i for i, m in enumerate(upstream)}    # 按上游目录顺序稳定排序
    entries.sort(key=lambda e: order.get(e["model"], 10 ** 9))
    return entries, excluded, notes

def validate_key(key):
    """校验 Key 并返回 (upstream_map, plan_id, account_label)。"""
    log("→ 校验 API Key …")
    try:
        models, _ = api_get(PATH_MODELS, key)
    except RuntimeError as e:
        if "HTTP 401" in str(e) or "HTTP 403" in str(e):
            die(f"Key 无效或无权限：{e}")
        die(f"无法访问 CommandCode API：{e}")
    data = models.get("data") or []
    if not data:
        die("上游 /models 返回为空，接口结构可能已变化。")
    plan_id, label = "", ""
    try:
        sub, _ = api_get(PATH_SUBS, key)
        plan_id = (sub.get("data") or {}).get("planId") or ""
    except RuntimeError as e:
        warn(f"订阅信息获取失败（不影响继续）：{e}")
    try:
        who, _ = api_get(PATH_WHOAMI, key)
        u = who.get("user") or {}
        label = u.get("email") or u.get("name") or u.get("id") or ""
    except RuntimeError:
        pass
    return {m["id"]: m for m in data if m.get("id")}, plan_id, label

def build_settings_config(key, entries, model, effort):
    toml = CONFIG_TOML_TEMPLATE.format(model=model, effort=effort, base_url=UPSTREAM_BASE)
    return {"auth": {"OPENAI_API_KEY": key}, "config": toml, "modelCatalog": {"models": entries}}

# ---------------- CC Switch 进程控制 ----------------

def app_running():
    return subprocess.run(["pgrep", "-x", PROC_NAME], capture_output=True).returncode == 0

def quit_app():
    if not app_running():
        return
    log("→ 退出 CC Switch …")
    subprocess.run(["osascript", "-e", f'tell application "{APP_NAME}" to quit'], capture_output=True, timeout=10)
    for _ in range(20):
        if not app_running():
            break
        time.sleep(0.5)
    if app_running():
        subprocess.run(["pkill", "-TERM", "-x", PROC_NAME], capture_output=True)
        for _ in range(10):
            if not app_running():
                break
            time.sleep(0.5)
    if app_running():
        die("CC Switch 未能退出，为避免数据库写入冲突已中止。请手动退出后重试。", 2)
    log("  已退出。")

def launch_app():
    log("→ 启动 CC Switch …")
    subprocess.run(["open", "-a", APP_PATH], check=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=0.5):
                log(f"  代理已监听 {PROXY_HOST}:{PROXY_PORT}")
                return True
        except OSError:
            time.sleep(1)
    warn(f"等待 {PROXY_PORT} 端口超时，请手动打开 CC Switch 检查。")
    return False

# ---------------- 数据库 ----------------

def backup_files(db, include_codex=True):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = []
    if os.path.exists(db):
        b = f"{db}.bak-{ts}"
        src = sqlite3.connect(db)
        dst = sqlite3.connect(b)
        src.backup(dst)
        dst.close(); src.close()
        backups.append(b)
    if include_codex:
        for f in (CODEX_CONFIG, CODEX_DIR + "/auth.json", CODEX_CATALOG):
            if os.path.exists(f):
                b = f + f".bak-{ts}"
                shutil.copy2(f, b)
                backups.append(b)
    return backups

def write_provider(db, name, sc_json, meta_json, upstream_url, make_current=True):
    """幂等写入：同名 codex 提供商存在则原地更新，否则新建。返回 (provider_id, action)。"""
    conn = sqlite3.connect(db, timeout=15)
    conn.execute("PRAGMA busy_timeout=10000")
    now_ms = int(time.time() * 1000)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM providers WHERE app_type='codex' AND name=? ORDER BY created_at LIMIT 1", (name,)
        ).fetchone()
        if row:
            pid = row[0]
            conn.execute("UPDATE providers SET settings_config=?, meta=? WHERE id=? AND app_type='codex'",
                         (sc_json, meta_json, pid))
            action = "updated"
        else:
            pid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO providers
                   (id, app_type, name, settings_config, website_url, category, created_at,
                    icon, icon_color, meta, is_current, in_failover_queue, cost_multiplier)
                   VALUES (?, 'codex', ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0, '1.0')""",
                (pid, name, sc_json, PROVIDER_WEBSITE, PROVIDER_CATEGORY, now_ms, PROVIDER_ICON_COLOR, meta_json, 0),
            )
            action = "created"
        conn.execute("DELETE FROM provider_endpoints WHERE provider_id=? AND app_type='codex'", (pid,))
        conn.execute("INSERT INTO provider_endpoints (provider_id, app_type, url, added_at) VALUES (?, 'codex', ?, ?)",
                     (pid, upstream_url, now_ms))
        if make_current:
            conn.execute("UPDATE providers SET is_current=0 WHERE app_type='codex'")
            conn.execute("UPDATE providers SET is_current=1 WHERE id=? AND app_type='codex'", (pid,))
        conn.execute(
            """INSERT INTO proxy_config (app_type, proxy_enabled, enabled) VALUES ('codex', 1, 1)
               ON CONFLICT(app_type) DO UPDATE SET proxy_enabled=1, enabled=1, updated_at=datetime('now')"""
        )
        conn.commit()
        return pid, action
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ---------------- 接管结果核验 ----------------

def post_verify(entries, db):
    ok = True
    log("→ 核验接管结果 …")
    cfg = CODEX_CONFIG
    if os.path.exists(cfg):
        content = open(cfg, encoding="utf-8").read()
        if f"{PROXY_HOST}:{PROXY_PORT}" in content:
            log(f"  ✓ {cfg} 已指向本机代理")
        else:
            warn(f"{cfg} 未指向 {PROXY_HOST}:{PROXY_PORT} —— 接管可能未生效，请在 CC Switch 中手动开启 Codex 代理。")
            ok = False
    else:
        warn(f"未找到 {cfg}")
        ok = False
    if os.path.exists(CODEX_CATALOG):
        try:
            cat = json.load(open(CODEX_CATALOG, encoding="utf-8"))
            models = cat.get("models", [])
            empty = [m["slug"] for m in models if not m.get("supported_reasoning_levels")]
            if len(models) == len(entries) and not empty:
                log(f"  ✓ 模型目录已生成：{len(models)} 个模型，档位完整")
            else:
                warn(f"模型目录异常：{len(models)}/{len(entries)} 个模型" + (f"，空档位：{empty}" if empty else ""))
                ok = False
        except json.JSONDecodeError as e:
            warn(f"模型目录 JSON 解析失败：{e}")
            ok = False
    else:
        warn(f"未找到 {CODEX_CATALOG}")
        ok = False
    return ok

# ---------------- Codex 冒烟测试 ----------------

def find_codex():
    p = shutil.which("codex")
    if p:
        return p
    pnpm = os.path.join(HOME, "Library/Application Support/deepseek-harness-desktop/node-runtime/packages/node_modules/.pnpm")
    hits = sorted(glob.glob(pnpm + "/@openai+codex@*/node_modules/@openai/codex/vendor/*/bin/codex"))
    return hits[-1] if hits else None

def e2e_verify(codex_bin, model, effort):
    log(f"\n→ Codex 冒烟测试（{model}，effort={effort}）…")
    tmp = tempfile_dir()
    shutil.copy2(CODEX_CATALOG, os.path.join(tmp, "cc-switch-model-catalog.json"))
    with open(os.path.join(tmp, "config.toml"), "w", encoding="utf-8") as f:
        f.write(f'''model_provider = "custom"
model = "{model}"
model_reasoning_effort = "{effort}"
model_catalog_json = "cc-switch-model-catalog.json"
disable_response_storage = true

[model_providers.custom]
name = "custom"
wire_api = "responses"
requires_openai_auth = true
base_url = "http://{PROXY_HOST}:{PROXY_PORT}/v1"
experimental_bearer_token = "PROXY_MANAGED"
''')
    with open(os.path.join(tmp, "auth.json"), "w", encoding="utf-8") as f:
        json.dump({"OPENAI_API_KEY": "PROXY_MANAGED"}, f)
    env = dict(os.environ, CODEX_HOME=tmp)
    try:
        r = subprocess.run(
            [codex_bin, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "Reply with exactly: PONG"],
            env=env, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        warn("codex exec 超时（120s）")
        return False
    out = (r.stdout or "") + (r.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-6:])
    if "PONG" in out:
        if "metadata not found" in out.lower():
            warn("输出含 Model metadata not found 警告")
        log("  ✓ 端到端打通，输出尾部：")
        log("  " + tail.replace("\n", "\n  "))
        return True
    warn("未看到 PONG，输出尾部：")
    log("  " + tail.replace("\n", "\n  "))
    return False

def tempfile_dir():
    import tempfile
    return tempfile.mkdtemp(prefix="codex-verify-")

# ---------------- 主流程 ----------------

def parse_args():
    ap = argparse.ArgumentParser(description="把 CommandCode（最低 GOAT 套餐）接入 Codex CLI（经 CC Switch 代理，仅限 macOS）")
    ap.add_argument("-k", "--key", help="CommandCode API Key（默认提示输入；也可用环境变量 COMMANDCODE_API_KEY）")
    ap.add_argument("--plan", choices=["goat", "pro", "provider"],
                    help="跳过自动识别，强制按该套餐档位（仅作闸门用；模型收录始终以探测结果为准）")
    ap.add_argument("-m", "--model", help=f"默认模型 slug（默认 {DEFAULT_MODEL}）")
    ap.add_argument("--name", default=DEFAULT_PROVIDER_NAME, help=f"CC Switch 中的提供商名称（默认 {DEFAULT_PROVIDER_NAME}）")
    ap.add_argument("--include", action="append", default=[], metavar="SLUG", help="额外纳入的模型 slug（可重复）")
    ap.add_argument("--skip-probe", action="store_true", help="跳过探测，直接收录目录里除 Claude 外的全部模型")
    ap.add_argument("--db", help=f"CC Switch 数据库路径（默认 {DB_PATH}；传其他路径用于演练，不触碰真实库）")
    ap.add_argument("--dry-run", action="store_true", help="只预览将要写入的内容，不做任何修改")
    ap.add_argument("--no-restart", action="store_true", help="写入后不重启 CC Switch")
    ap.add_argument("--verify", action="store_true", help="完成后用 codex exec 跑一次 PONG 冒烟测试")
    ap.add_argument("-y", "--yes", action="store_true", help="跳过确认提示（非交互）")
    return ap.parse_args()

def main():
    args = parse_args()
    if sys.platform != "darwin":
        die("本脚本仅支持 macOS。")

    live = not args.db  # --db 指向其他路径时视为演练，不触碰 CC Switch 进程
    if live and not os.path.exists(APP_PATH):
        die(f"未找到 {APP_PATH}，请先安装 CC Switch。")
    db = os.path.expanduser(args.db) if args.db else DB_PATH
    if not os.path.exists(db):
        die(f"未找到 CC Switch 数据库：{db}")

    key = args.key or os.environ.get("COMMANDCODE_API_KEY")
    if not key:
        if not sys.stdin.isatty():
            die("非交互环境请用 --key 或环境变量 COMMANDCODE_API_KEY 提供 Key。")
        key = getpass.getpass("请输入 CommandCode API Key（user_…，输入不回显）: ").strip()
    if not key:
        die("未提供 Key。")
    if not key.startswith("user_"):
        warn("Key 不是 user_ 前缀，请确认这是 CommandCode 的 API Key。")

    upstream, plan_id, account = validate_key(key)
    plan = args.plan or detect_plan(plan_id)
    if plan == "go":
        die("当前套餐为 Go：本脚本最低要求 GOAT 套餐，不支持 Go。请升级订阅后再试。")
    if plan is None:
        warn("无法识别订阅套餐（订阅接口失败或无数据）。")
        if not args.yes:
            if not sys.stdin.isatty() or input("仍要继续（以探测结果为准）? [y/N] ").strip().lower() not in ("y", "yes"):
                die("已取消。也可用 --plan goat 强制按 GOAT 处理。")
    log(f"✓ Key 有效（账户：{account or '未知'}），订阅 planId={plan_id or '未知'} → 套餐档位：{plan or '未知'}")

    entries, excluded, notes = build_entries(upstream, key, args.include, args.skip_probe)
    if not entries:
        die("没有任何模型可用（可能 Key 或套餐异常），未写入任何配置。")

    default_model = args.model or (DEFAULT_MODEL if any(e["model"] == DEFAULT_MODEL for e in entries) else entries[0]["model"])
    hit = next((e for e in entries if e["model"] == default_model), None)
    if hit is None:
        die(f"--model 指定的 {default_model} 不在套餐可用模型内。")
    effort = default_level(default_model)

    # ---- 预览 ----
    log(f"\n将写入模型目录（{len(entries)} 个）：")
    log(f"  {'#':>3}  {'slug':44} {'显示名':26} {'上下文':>9}  档位 / 图像")
    for i, e in enumerate(entries, 1):
        img = "🖼" if "image" in e["inputModalities"] else ""
        lv = "/".join(e["reasoningLevels"])
        log(f"  {i:>3}  {e['model']:44} {e['displayName']:26} {e['contextWindow']:>9}  {lv} {img}")
    for n in notes:
        warn(n)
    if excluded:
        log(f"\n未收录 {len(excluded)} 个：")
        for slug, why in sorted(excluded):
            log(f"    · {slug:44} {why}")
    log(f"\n默认模型：{default_model}（effort={effort}）")
    log(f"提供商名称：{args.name} ｜ 数据库：{db}")

    if args.dry_run:
        log("\n== DRY-RUN：未做任何修改 ==")
        return

    if not args.yes:
        if not sys.stdin.isatty():
            die("非交互环境请加 --yes 确认写入。")
        if input("\n确认写入 CC Switch 数据库并重启应用? [y/N] ").strip().lower() not in ("y", "yes"):
            die("已取消，未做任何修改。")

    # ---- 备份 → （退出应用）→ 写库 → 启动 ----
    backups = backup_files(db, include_codex=live)
    if backups:
        log("→ 已备份：")
        for b in backups:
            log(f"    {b}")

    if live:
        if args.no_restart:
            if app_running():
                warn("CC Switch 正在运行中写库，可能有内存态覆盖风险；建议写完手动重启。")
        else:
            quit_app()
    elif app_running():
        warn("演练模式：不触碰正在运行的 CC Switch。")

    sc = build_settings_config(key, entries, default_model, effort)
    pid, action = write_provider(db, args.name,
                                 json.dumps(sc, ensure_ascii=False, separators=(",", ":")),
                                 json.dumps(PROVIDER_META, ensure_ascii=False, separators=(",", ":")),
                                 UPSTREAM_BASE)
    log(f"→ 提供商已{('新建' if action == 'created' else '原地更新')}（id={pid}，app_type=codex，已设为当前提供商，代理接管已启用）")

    if not live or args.no_restart:
        log("\n== 完成（未重启 CC Switch）。请手动重启 CC Switch 使配置生效。==")
        return
    if not launch_app():
        sys.exit(3)
    if not post_verify(entries, db):
        warn("接管核验未全部通过，可查看 ~/.cc-switch/logs/cc-switch.log 或回滚（见上方 .bak 文件）。")
        sys.exit(3)

    if args.verify:
        codex_bin = find_codex()
        if not codex_bin:
            warn("未找到 codex 可执行文件，跳过冒烟测试。")
        else:
            deadline = time.time() + 20
            while time.time() < deadline:  # 等接管完成、目录文件生成
                if os.path.exists(CODEX_CATALOG):
                    try:
                        if len(json.load(open(CODEX_CATALOG))["models"]) == len(entries):
                            break
                    except Exception:
                        pass
                time.sleep(1)
            ok = e2e_verify(codex_bin, default_model, effort)
            if not ok:
                warn("冒烟测试未通过，请检查 ~/.cc-switch/logs/cc-switch.log。")
                sys.exit(4)

    log("\n== 全部完成 ✅ ==")
    log("在 Codex CLI 中即可选择 CommandCode 的模型；模型/思考档位由 CC Switch 接管注入。")
    log(f"如需回滚：先退出 CC Switch，再用上方 .bak 备份覆盖 {db} 与 ~/.codex/ 下对应文件。")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)
