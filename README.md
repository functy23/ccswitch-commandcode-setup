# CCSwitch-CommandCode-Setup

一键把 **CommandCode 订阅**接入 **Codex CLI**（通过 CC Switch 代理），仅限 **macOS**。最低需要 **GOAT 套餐**（不支持 Go）。

脚本做的是此前手工完成的全套工作：校验 Key → 拉取上游全量模型目录 → **逐模型探测套餐可用性** → 自动生成模型目录（含思考档位标注）→ 写入 CC Switch 数据库 → 重启接管 → 可选端到端冒烟测试。

## 环境要求

- macOS（已验证 CC Switch v3.20.0）
- 已安装并至少启动过一次 [CC Switch](https://github.com/farion1231/cc-switch)（`/Applications/CC Switch.app`）
- 已安装 Codex CLI（仅 `--verify` 冒烟测试需要；自动在 PATH 和 deepseek-harness-desktop 运行时里查找）
- Python 3（仅标准库，无需 pip 安装任何依赖）

## 快速开始（一键运行，推荐）

脚本类项目经典用法：`curl` 一个总脚本下来，它会自动拉取其余脚本到临时目录运行，**结束后自动清理——无需下载整个仓库再手动删除**。

```bash
# 交互式（会提示输入 Key）
curl -fsSL https://raw.githubusercontent.com/functy23/ccswitch-commandcode-setup/main/bootstrap.sh | bash

# 带参数（参数原样透传给 setup_commandcode.py）
curl -fsSL https://raw.githubusercontent.com/functy23/ccswitch-commandcode-setup/main/bootstrap.sh | bash -s -- --verify
```

说明：`bootstrap.sh` 只下载运行所需的最小文件集合（`setup_commandcode.py`）到 `/tmp` 临时目录，运行结束自动删除；交互输入自动改走 `/dev/tty`，所以在 `curl | bash` 下照常可以输入 Key。`curl | bash` 模式下传参要用 `bash -s --` 分隔。

也可以用环境变量或本地运行指定脚本来源（自建托管/镜像时替换地址即可）：

```bash
COMMANDCODE_SETUP_BASE=https://raw.githubusercontent.com/functy23/ccswitch-commandcode-setup/main ./bootstrap.sh   # 环境变量方式
./bootstrap.sh --base https://raw.githubusercontent.com/functy23/ccswitch-commandcode-setup/main --verify           # 参数方式
```

## 本地运行

仓库已在本地时，无需任何下载：

- **双击 `run.command`**（Finder 里双击会自动打开终端运行）；或
- `python3 setup_commandcode.py`（推荐先 `--dry-run` 预览）

```bash
python3 setup_commandcode.py --dry-run    # 只预览，不写任何东西
python3 setup_commandcode.py --verify     # 完成后自动跑 PONG 冒烟测试
COMMANDCODE_API_KEY=user_xxx python3 setup_commandcode.py --yes --verify   # 非交互
```

### 项目文件

| 文件 | 用途 |
|---|---|
| `bootstrap.sh` | 总脚本（curl \| bash 入口）：下载最小文件集到临时目录、运行、自动清理 |
| `setup_commandcode.py` | 主脚本：校验 Key → 探测模型 → 写入 CC Switch → 重启接管 → 可选冒烟测试 |
| `run.command` | macOS 双击启动器：进入本目录并运行主脚本 |

### 全部参数

| 参数 | 说明 |
|---|---|
| `-k, --key` | CommandCode API Key；缺省则提示输入，也可用环境变量 `COMMANDCODE_API_KEY` |
| `--plan goat\|pro\|provider` | 跳过订阅自动识别，强制按该套餐档位（仅作闸门用；模型收录始终以探测结果为准） |
| `-m, --model SLUG` | 默认模型（写入 Codex `model =`；默认 `deepseek/deepseek-v4-flash`） |
| `--name NAME` | CC Switch 中的提供商名称（默认 `CommandCode`） |
| `--include SLUG` | 额外强制收录的模型 slug，可重复 |
| `--skip-probe` | 跳过探测，直接收录目录里除 Claude 外的全部模型（失效模型会报错） |
| `--db PATH` | 指定数据库路径（演练用，不触碰真实库、不重启应用） |
| `--dry-run` | 只预览模型目录与写入内容 |
| `--no-restart` | 写库后不重启 CC Switch（需手动重启生效） |
| `--verify` | 完成后用干净临时 CODEX_HOME 跑 `codex exec` PONG 冒烟测试 |
| `-y, --yes` | 跳过确认提示 |

## 重要说明

由于上游模型更新频繁，可能会出现模型失效或被下架的情况，导致无法使用。如果出现 Claude 模型无法使用的情况，暂时是没法修复的，因为作者没有 Claude 的 API。

## 它具体做了什么

1. **校验 Key**：请求 `/provider/v1/models`、`/alpha/billing/subscriptions`、`/alpha/whoami`，401/403 直接中止。
2. **套餐闸门**：由 `planId`（如 `individual-goat`）识别套餐档位，**最低 GOAT**——检测到 Go 套餐直接中止（可用 `--plan` 强制指定）。
3. **探测可用模型**：拉取上游全量模型目录后，对每个模型发送 `max_tokens=1` 的最小请求（8 并发，每模型 1~16 token 额度）：
   - `200` = 可用收录；`403 MODEL_NOT_IN_PLAN` = 套餐不含，剔除；
   - `429/503` = 上游临时问题，自动重试，仍失败也**保留收录**；
   - `400` = 上游拒绝（如路由故障），剔除；部分模型要求 `max_tokens>=16` 会自动升级重试；
   - Claude 系模型只能走 Anthropic Messages 端点、无法经 chat/completions 使用，一律剔除。
4. **构建模型元数据**（写入 CC Switch DB 的 `settings_config.modelCatalog`）：显示名、上下文窗口取自上游 `/models`（自动去掉 `(latest)`/`(exp)` 后缀）；思考档位用内嵌 `KNOWN_EFFORTS` 权威表（来自 `dsh-commandcode-provider/src/adapter.ts`，command-code@1.37.0 同步），表外模型统一 `low/medium/high`、默认最高档（上游网关对 `reasoning_effort` 通用接受，已实测）；图像输入由内嵌 `KNOWN_IMAGE_MODELS` 决定。
4. **写入 CC Switch 数据库**（幂等：同名提供商已存在则原地更新）：
   - `providers` 表：`settings_config`（auth + codex config 片段 + modelCatalog）、`meta`（`apiFormat=openai_chat` + `codexChatReasoning`，CC Switch 据此做 responses→chat 转换）、`is_current=1`；
   - `provider_endpoints`：上游 `https://api.commandcode.ai/provider/v1`；
   - `proxy_config`：开启 codex 的代理与接管开关。
5. **重启 CC Switch**：退出（osascript → pkill 兜底）→ 写库 → 重新启动 → 等待 `127.0.0.1:15721` 监听 → 核验 `~/.codex/config.toml` 已指向代理、模型目录文件已重新生成且档位完整。
6. **可选冒烟测试**：复制生成的模型目录到临时 `CODEX_HOME`，跑 `codex exec --sandbox read-only "Reply with exactly: PONG"`，校验模型/effort 正确显示、无 metadata 警告。

> **关键机制**：CC Switch 会在接管/重启时**从数据库重新生成** `~/.codex/cc-switch-model-catalog.json`，所以任何对磁盘 catalog 的手工修改都会丢——必须改数据库。本脚本直接写数据库，因此天然不会被覆盖。

## 备份与回滚

每次实际写入前，脚本自动备份（时间戳后缀 `.bak-YYYYmmdd-HHMMSS`）：

- `~/.cc-switch/cc-switch.db`
- `~/.codex/config.toml`、`~/.codex/auth.json`、`~/.codex/cc-switch-model-catalog.json`

回滚：退出 CC Switch → 用 `.bak` 文件覆盖回去 → 重新打开 CC Switch。

## 已知边界

- 模型收录以实时探测为准，上游新增/下架/恢复模型后**重跑脚本即可自动跟随**，无需改代码；`KNOWN_EFFORTS` / `KNOWN_IMAGE_MODELS` 两张标注表按 command-code@1.37.0 同步（2026-08-29 实测核对），上游新增模型如档位/图像标注不符可补充表项。
- `minimax/*-free` 与 `MiniMaxAI/*` 是同一模型的两条路由，上游显示名相同，选择器中会出现同名项。
- 对 `tencent/hy4-preview` 这类上游持续 429 的模型，探测归为临时问题、照常纳入，但在 Codex 里调用可能偶发失败。
