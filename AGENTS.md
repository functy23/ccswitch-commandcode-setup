# AGENTS.md — CCSwitch-CommandCode-Setup（AI 接手说明）

> 本文件面向 AI agent（ZCode / Claude Code / Codex / 其他），是项目的完整事实库与开发规程。
> 人类用户请读 [README.md](README.md)。
> 标注【实测】的事实均于 **2026-08-29** 在用户本机（macOS arm64，HOME=/Users/functy）验证过；
> 修改代码前先读完本文，不要凭记忆改动已验证的行为。怀疑过期时按 §8 重新验证。

---

## 1. 项目定位

一键把 **CommandCode 订阅**接入 **Codex CLI**（通过 CC Switch 代理），仅限 **macOS**，最低 **GOAT 套餐**（不支持 Go，检测到 go 直接中止）。

- 核心脚本：`setup_commandcode.py`（Python 3 标准库，零第三方依赖）
- 用户入口：`bootstrap.sh`（curl | bash 一键运行）或 `run.command`（双击）
- 它做的事：校验 Key → 识别套餐 → 拉取上游全量模型目录 → 逐模型探测可用性 → 生成模型目录（含思考档位标注）→ 写入 CC Switch 数据库 → 重启接管 → 可选 PONG 冒烟测试
- 它**不做**的事：不改 `~/.codex/config.toml` 主体（接管由 CC Switch 完成）；不支持 Claude 模型（上游只走 Anthropic 路由，作者没有 Claude API 无法调试——README 已声明）

线上仓库：<https://github.com/functy23/ccswitch-commandcode-setup>（public，分支 main）

## 2. 文件清单

| 文件 | 职责 |
|---|---|
| `setup_commandcode.py` | 主脚本，全部业务逻辑（校验/探测/元数据/写库/重启/核验/冒烟） |
| `bootstrap.sh` | curl\|bash 总脚本：下载最小文件集（仅 setup_commandcode.py）到 mktemp 目录 → 运行 → trap 自动清理 |
| `run.command` | macOS 双击启动器：cd 到脚本目录、检查 python3 与 CC Switch.app、exec 主脚本（参数透传） |
| `README.md` | 面向用户：一键命令、参数表、重要说明、回滚 |
| `AGENTS.md` | 本文件 |

## 3. 端到端链路【实测】

```
Codex CLI 0.147（vendor 二进制，不在 PATH；仅支持 wire_api=responses）
  → POST http://127.0.0.1:15721/v1/responses
  → CC Switch 代理（provider meta.apiFormat=openai_chat 时做 responses→chat 转换）
  → POST https://api.commandcode.ai/provider/v1/chat/completions
```

- Codex 二进制（版本升级后路径会变，用 glob `~/Library/Application Support/deepseek-harness-desktop/node-runtime/packages/node_modules/.pnpm/@openai+codex@*/node_modules/@openai/codex/vendor/*/bin/codex` 重新定位）：
  `~/Library/Application Support/deepseek-harness-desktop/node-runtime/packages/node_modules/.pnpm/@openai+codex@0.147.0-darwin-arm64/node_modules/@openai/codex/vendor/aarch64-apple-darwin/bin/codex`
- 上游**只有** `chat/completions`，没有 `/responses`（404）；Claude 系模型只走 `/provider/v1/messages`（Anthropic 形态）
- `~/.codex/config.toml` 由 CC Switch 接管时改写：`base_url` 换成 `http://127.0.0.1:15721/v1`、注入 `experimental_bearer_token = "PROXY_MANAGED"` 和 `model_catalog_json = "cc-switch-model-catalog.json"`；`~/.codex/auth.json` 写真实 CommandCode key（config 里的 bearer 是占位符 PROXY_MANAGED，两处并存是正常状态）

## 4. CC Switch 数据库写入规范（最关键的坑）

- 库：`~/.cc-switch/cc-switch.db`（SQLite）；CC Switch 进程名为 `cc-switch`（`pkill -x cc-switch`，**不要** `pkill -f cc-switch`——会误杀自身 shell）
- **【最重要】CC Switch 在接管/重启时会从数据库重新生成 `~/.codex/cc-switch-model-catalog.json`。任何只改磁盘 catalog 文件的做法都会在重启后被覆盖——必须写数据库。** 本脚本直接写库，天然规避。
- 涉及三张表（本脚本写哪些列已验证）：
  - `providers`：主键 `(id, app_type)`；CommandCode 行 `app_type='codex'`，id=`f4a0013a-37aa-41f5-828b-b8fe50aa16b9`（更新路径保留原 id）
  - `provider_endpoints`：每个 provider 的上游 URL（`endpointAutoSelect=true` 时由 meta 指示自动选择）
  - `proxy_config`：`app_type='codex'` 行需 `proxy_enabled=1, enabled=1` 才会接管
- `settings_config`（JSON，紧凑序列化）三段：
  - `auth`：`{"OPENAI_API_KEY": "<真实key>"}`
  - `config`：**TOML 片段**（存上游直连地址 `https://api.commandcode.ai/provider/v1`；接管时 CC Switch 自动换成代理地址并注入 token/catalog 配置——所以这里不要写代理地址）
  - `modelCatalog.models[]`：`{model, displayName, contextWindow, reasoningLevels[string数组], defaultReasoningLevel, inputModalities}` ——这是 Codex 模型选择器与思考档位的数据源
- `meta`（JSON）逐字使用，勿改动字段：
  ```json
  {"commonConfigEnabled":false,"endpointAutoSelect":true,"apiFormat":"openai_chat",
   "codexChatReasoning":{"supportsThinking":true,"supportsEffort":true,"thinkingParam":"none",
   "effortParam":"reasoning_effort","effortValueMode":"zen","outputFormat":"reasoning_content"}}
  ```
  `apiFormat=openai_chat` 是 responses→chat 转换的前提（若为 openai_responses 会原样透传 /responses → 上游 404）。
- `is_current`：同 `app_type` 下必须唯一；脚本先全部清零再置 CommandCode 为 1。
- 写库流程（脚本已实现，勿改顺序）：备份 → 退出 CC Switch（osascript → pkill -TERM -x 兜底，最多等 15s，退不掉就中止）→ `BEGIN IMMEDIATE` 单事务写入 → 重启 → 等 15721 监听（40s）→ 核验。
- 幂等性：同名（默认 `CommandCode`）provider 存在则原地 UPDATE（保留 id/created_at），否则 INSERT（uuid4，category=`third_party`）。可重复运行。

## 5. 主脚本结构与关键函数

`setup_commandcode.py`（约 620 行）自上而下：

| 区块 | 要点 |
|---|---|
| 常量 | 路径、API endpoint、`DEFAULT_MODEL=deepseek/deepseek-v4-flash`、`DEFAULT_LEVELS=[low,medium,high]` |
| 元数据表 | `KNOWN_EFFORTS`、`KNOWN_IMAGE_MODELS`（见 §7）；**没有**套餐模型清单（探测式，见 §6） |
| `display_name()` | 去掉上游 `(latest)`/`(exp)` 后缀 |
| `detect_plan()` | planId → goat/go/pro/provider；go 用于闸门拒绝 |
| `probe_model()` / `probe_all()` | 探测核心（§6） |
| `build_entry()` / `build_entries()` | 组装 modelCatalog 条目；分类：OK/TRANSIENT 收录，NOT_IN_PLAN/UNSUPPORTED/ERROR 剔除，claude-* 一律剔除；`--include` 强制收录；按上游目录顺序稳定排序 |
| `write_provider()` | 幂等写库（§4） |
| `post_verify()` | 重启后核验：config.toml 指向代理、catalog 重新生成且数量/档位完整 |
| `e2e_verify()` | 临时 CODEX_HOME 冒烟测试（§8） |
| `main()` | 流程编排 + 交互确认；`--db` 指向其他路径时为演练模式（不触碰 CC Switch 进程、不备份 codex 文件） |

## 6. 探测式模型列表（设计决策，勿回退成内嵌清单）

为什么不内嵌套餐→模型映射：上游模型增删频繁，探测式可自动跟随（2026-08-29 实测：上游路由故障的 `MiniMaxAI/MiniMax-M2.7` 被自动剔除，收录 43 个）。

- `GET /provider/v1/models` → `{object, data:[{id, name, context_length, owned_by, created}]}`（62 个，id 带厂商前缀如 `zai-org/GLM-5.3`）
- 对**每个**模型 POST `/provider/v1/chat/completions`：`{"model", "messages":[{role:user,content:"hi"}], "max_tokens":1}`，`ThreadPoolExecutor` 8 并发、完成间隔 0.2s
- 判定（与参考实现 `~/Desktop/CommandCode-Setup_副本` 一致）：
  - `200` → OK 收录；`403` 含 `MODEL_NOT_IN_PLAN` → 套餐不含剔除
  - `429/503` → 退避重试（3×(n+1)s，共 3 次）仍失败 → TRANSIENT，**保留收录**（瞬态问题）
  - `400` → 若报文含 `">= 16"` 且 max_tokens<16 则自动升级 max_tokens=16 重试（部分模型要求）；否则 UNSUPPORTED 剔除
  - 其他/网络异常 → 重试后 ERROR 剔除
  - `claude-*` → 无论探测结果一律剔除，理由固定为"Claude 系只能走 Anthropic Messages 端点"
- 探测成本：每模型 1~16 token，62 次请求；`--skip-probe` 可跳过（收录全部非 Claude 模型，失效模型会报错）
- 套餐闸门：`GET /alpha/billing/subscriptions` → `data.planId`（如 `individual-goat`）；映射 goat/pro/provider 放行、**go 中止**；识别失败时交互确认或 `--yes` 继续；`--plan` 可强制

## 7. 模型元数据权威表（内嵌，来源 `dsh-commandcode-provider/src/adapter.ts`，command-code@1.37.0 同步）

- `KNOWN_EFFORTS`：官方标注了可选思考档位的模型（如 deepseek [high,max]、GLM-5.2 [high,max]、GLM-5.3/glm-5.3-flash [low,high,max]、Qwen3.8 [low,medium,xhigh]、gpt-5.6 [low,medium,high,xhigh,max]、grok-4.6 [low,medium,high,xhigh]…）。**默认档取列表最高档**（与全部已验证目录一致）。
- 表外模型统一 `DEFAULT_LEVELS=[low,medium,high]` + 默认 `high`。依据：上游网关对 `reasoning_effort` 参数**通用接受**（实测 low/medium/high/xhigh/max 全 200，仅 deepseek 拒绝字面量 `none`），DSH 官方插件把这些模型标为"自动思考"但上游档位真实存在，给 Codex 可选档位是正确行为。
- `KNOWN_IMAGE_MODELS`：决定 `inputModalities` 是否含 `image`。注意三个易漏模型：`moonshotai/Kimi-K2.5`、`moonshotai/Kimi-K2.6`、`xai/grok-4.5`（早期手工版漏标，已修正）。
- 磁盘 catalog 的其余字段（`truncation_policy`、档位描述文案等）由 CC Switch 重新生成时自动补全，脚本不需要也不应该关心。

## 8. 测试规程（改代码后必做，按成本从低到高）

```bash
cd ~/Desktop/CCSwitch-CommandCode-Setup

# 0. 语法
python3 -m py_compile setup_commandcode.py && bash -n bootstrap.sh && bash -n run.command

# 1. 零成本机械测试（不探测、不写库）
COMMANDCODE_API_KEY=<key> python3 setup_commandcode.py --skip-probe --dry-run

# 2. 真探测预览（每模型 1~16 token，62 次请求，约 1~3 分钟；不写库）
COMMANDCODE_API_KEY=<key> python3 setup_commandcode.py --dry-run

# 3. 写库演练（对副本库，不触碰 CC Switch 进程）
sqlite3 ~/.cc-switch/cc-switch.db ".backup '/tmp/cc-test.db'"
COMMANDCODE_API_KEY=<key> python3 setup_commandcode.py --db /tmp/cc-test.db --skip-probe --yes

# 4. 实机全流程（会重启 CC Switch 并写真实库；改完主流程后必跑一次）
COMMANDCODE_API_KEY=<key> python3 setup_commandcode.py --yes --verify
```

- `--verify` 冒烟测试实现：复制重新生成的 catalog 到 mktemp 目录，写入最小 `config.toml`（base_url=代理、`experimental_bearer_token="PROXY_MANAGED"`、auth.json 同占位）+ 临时 `CODEX_HOME`，`codex exec --skip-git-repo-check --sandbox read-only "Reply with exactly: PONG"`（120s 超时），断言输出含 `PONG` 且无 `metadata not found`。预期还输出 `reasoning effort: <档位>`。
- Key 获取（仅本机调试，勿写入任何文件/输出）：`python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.codex/auth.json')))['OPENAI_API_KEY'])"`
- bootstrap.sh 测试：本地 `python3 -m http.server 8123` 模拟托管，`curl -fsSL http://127.0.0.1:8123/bootstrap.sh | bash -s -- --base http://127.0.0.1:8123 --skip-probe --dry-run`；跑完检查 `/tmp/ccswitch-commandcode.*` 无残留（trap 清理生效）。
- 推送后 raw 地址有 Fastly CDN 缓存，**最长约 5 分钟**才反映新 commit（cache-bust 查询参数无效）；急验证用 `gh api repos/.../contents/<file>` 读实时内容。

## 9. 发布流程

1. 改完跑 §8 的 0→4 全套
2. `git add -A && git commit -m "..." && git push`（main 分支）
3. README 的一键命令 URL 恒定不变（指向 main），无需改文档

## 10. 已知坑清单（都是踩过的）

- **catalog 覆盖**：CC Switch 重启会从 DB 重新生成磁盘 catalog —— 改磁盘文件无效，必须写 DB（§4）
- **pkill 误杀**：`pkill -f cc-switch` 会匹配到执行命令的 shell 自身，必须 `pkill -x cc-switch`
- **bash 多字节变量名**：`$VAR` 后紧跟全角字符（如 `）`）时，C locale 下 bash 会把多字节字节并进变量名 → `unbound variable`。shell 里引用变量凡后接非 ASCII 一律用 `${VAR}`
- **/dev/tty 误判**：`[ -r /dev/tty ]` 在无控制终端环境（CI/沙盒）会误通过但实际打开失败；bootstrap.sh 已改为真实试开 `{ true </dev/tty; } 2>/dev/null` 再决定重定向
- **curl|bash 的 stdin 是管道**：脚本的交互输入（getpass/input）在管道模式下必须走 /dev/tty，否则读到空流
- **sqlite 大 JSON 更新**：用 python `sqlite3` 参数绑定或生成 SQL 文件（JSON 内单引号需 `''` 转义）；CLI `?` 绑定不可用（当字面量）
- **备份先行**：任何写库操作前 `sqlite3 ".backup"` 到 `.bak-<时间戳>`；codex 三文件（config.toml / auth.json / catalog）同批备份。现有备份：`~/.cc-switch/cc-switch.db.bak-20260829-{131332,133623,135513}`
- **额度**：探测与冒烟测试消耗 CommandCode 额度（很小但非零）；机械测试一律 `--skip-probe`
- **敏感信息**：key 只存在于用户输入/环境变量/CC Switch DB/auth.json，绝不写进仓库文件、README、commit message 或终端回显

## 11. 变更历史

| 日期 | 变更 |
|---|---|
| 2026-08-29（上午） | 手工打通 Codex→CC Switch→CommandCode 链路；apiFormat 改 openai_chat；catalog 44 模型补思考档位（20 自动思考 + 10 未覆盖模型 [low,medium,high]，其余按权威表）；三模型端到端 PONG 验证 |
| 2026-08-29（下午） | 项目化：`setup_commandcode.py` v1（内嵌 GO/GOAT 清单）+ README；DB 副本演练验证更新/插入两路径 |
| 2026-08-29 | v2：改用探测式模型列表（对齐参考实现 `~/Desktop/CommandCode-Setup_副本`），取消 Go 套餐支持（最低 GOAT），M2.7 自动剔除 → 43 模型；README 加"上游模型更新频繁 / Claude 无法修复"说明；实机 `--verify` 通过 |
| 2026-08-29 | 新增 `run.command`、`bootstrap.sh`（curl\|bash，临时目录自动清理）；修 `${TMP}` 多字节坑与 /dev/tty 误判 |
| 2026-08-29 | 发布 GitHub 公开仓库 `functy23/ccswitch-commandcode-setup`；bootstrap 默认 BASE_URL 内置仓库 raw 地址；README 填入真实一键命令并线上验证通过 |
| 2026-08-29 | 新增本文件（AGENTS.md）作为 agent 交接文档 |
