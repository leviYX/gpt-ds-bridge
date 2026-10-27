# GPT–DeepSeek Bridge

## 1. 用途和目的

将 **与 GPT 讨论需求 → GPT 读取 PRD 和代码生成计划 → DeepSeek Harness 执行代码** 串起来，省去在两个工具之间复制计划、重复发送执行指令的步骤。

- **交互聊天**：在终端里与 GPT 讨论，确认计划后交给 DS；执行中可以查看状态、继续讨论或暂停介入。
- **一次提交**：给出 PRD 文件，GPT 生成明确计划后自动交给 DS 执行。

GPT 的讨论记录保存在本工具的 SQLite 中，每轮调用从记录重建上下文；DS 使用 Harness 原生持久化 session。退出后再次进入，可以继续讨论和原 DS 会话。

**GPT 端使用本机 Codex CLI 配置的模型，不是 ChatGPT 网页聊天窗口。** 普通聊天消息不会自动修改代码，必须通过 `/plan` 和 `/execute` 进入执行阶段。`completed` 表示 DS 正常结束，是否满足验收、测试是否通过，以实际执行报告为准。

## 2. 需要的环境

| 环境 | 要求 |
|---|---|
| 系统 | macOS 或 Linux |
| Python | 3.11+，建议通过 uv 使用 3.12 |
| Python 工具 | 推荐 uv，也可以使用 venv + pip |
| GPT | 安装 Codex CLI，完成登录或 API 认证，确保 `codex exec` 可用；使用其配置的模型，也可在本工具配置中指定模型 ID |
| DeepSeek | 安装 DeepSeek Harness，支持 headless 的 `--json`、`--session-id`；本机核实版本为 `0.2.1-rc.2` |
| DS 认证 | 可用的 DeepSeek API Key，或显式启用 Harness 已保存的登录凭据 |
| 业务项目 | 一个已存在、GPT 可以读取、DS 可以修改的目录；PRD 使用 UTF-8 文本或 Markdown |

安装桥接器：

```bash
cd /Users/levi/develop/project/my/dsh/gpt-ds-bridge
uv sync --python 3.12 --extra dev
uv run gpt-ds-bridge --help
```

没有 uv 时：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/gpt-ds-bridge --help
```

**DS headless 默认可能不使用 Web 中保存的登录信息。** 需要二选一：

1. 在启动工具的终端设置 `DEEPSEEK_API_KEY`，或按照 Harness 约定配置工作区 / `DSH_HOME` 中的 `.env`。
2. 使用 Harness 已保存的登录：在工作区配置的 `dsh_patches` 中加入下面的补丁路径，并让 `dsh_home` 指向保存登录信息的原目录。

```toml
dsh_patches = ["/Users/levi/develop/project/my/dsh/gpt-ds-bridge/examples/use-saved-credentials.patch.yml"]
```

补丁仅影响本次 headless 启动，不修改已有 Web 配置。Web 中额外安装的插件也不会自动加入 headless。本工具不保存模型密钥，不提供账户或额度。

## 3. 具体怎么用

以下命令在桥接器源码目录运行。`--workspace` 指定真正要修改的业务项目，放在子命令前面；路径含空格时加引号。

### 初始化和检查

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/project init
uv run gpt-ds-bridge --workspace /absolute/path/to/project doctor
```

`init` 自动发现本机命令，在业务项目创建 `.gpt-ds-bridge/config.toml`，不会覆盖已有配置，也不会联网下载 Harness。找不到 PATH 中的 `dsh` 时，会尝试发现本机 npm 缓存中的安装。

可按需修改配置，以 `init` 生成的实际路径为准：

```toml
codex_command = ["codex"]
dsh_command = ["dsh"]
dsh_home = "/absolute/path/to/.dsh"
codex_model = ""              # 空字符串表示沿用 Codex 配置
dsh_patches = []              # 可填写上面的已保存凭据补丁
planner_timeout = 1200        # 秒
executor_timeout = 3600
lock_timeout = 30
```

如果用 Node 启动 DS，应写成 `["node", "/absolute/path/to/dsh/lib/bin.js"]`，不要把整条 shell 命令写成一个字符串。npm 缓存被清理后需要更新实际路径。

`doctor` 检查工具版本、配置路径和会话映射，不调用模型，不验证账户额度或 API 连通性。

### 聊天、确认计划和介入

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/project chat --prd /absolute/path/to/prd.md
```

进入后直接输入中文。每次输入一条消息，等待 GPT 回答再继续；GPT 思考和 DS 执行时，状态及暂停命令仍可使用。

| 输入 | 作用 |
|---|---|
| 普通文字 | 与 GPT 讨论，不会投递给正在执行的 DS |
| `/prd "/path/to/prd.md"` | 加载或替换 PRD |
| `/plan` | 根据 PRD、讨论、实际代码和历史结果生成计划并显示 |
| `/execute` | 执行当前计划；如果已暂停，则继续原计划 |
| `/resume` | 恢复暂停/中断的当前计划，不重新调用 GPT 规划 |
| `/status` | 查看后台状态、任务状态和 DS session |
| `/pause` | 停止当前 GPT / DS 进程，保留日志、上下文和已有代码变更 |
| `/revise --checked` | 兼容命令：明确检查变更后生成新计划；通常直接用 `/plan` 即可 |
| `/wait` | 等待当前 GPT / DS 工作结束 |
| `/history` | 查看保存的 GPT 讨论记录 |
| `/help` | 查看聊天命令 |
| `/exit` | 保存记录、停止后台工作并退出 |

**当前计划一直保留，普通讨论、加载 PRD、暂停或退出重开都不会让它失效。** `/execute` 始终执行当前计划，暂停后可以直接 `/resume`（或 `/execute`）继续，不需要重新生成计划。讨论中的新要求、后来加载的 PRD，只有在主动 `/plan` 成功后才会进入新的执行计划；它们不会直接插入 DS 正在执行的任务。

如果决定改变实现方向，先 `/pause`，讨论新要求，再 `/plan`、`/execute`。只有新计划生成成功且可执行，才会替换当前计划；生成失败、被中断或需要澄清时，原计划仍然可用。

“暂停”是**终止当前进程**，不是冻结每个工具调用；已完成修改不会回滚。恢复时仍使用原 DS 会话和原计划，并要求 DS 先核对当前代码与已完成操作，只补做剩余部分，不盲目重放。需要改变计划时再主动 `/plan`。

再次进入不必重复加载 PRD：

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/project chat
```

聊天期间工作区由当前进程持锁，可以从另一终端查询 `status`，不要同时启动另一个修改任务或在 Harness Web 驱动同一会话。`/exit`、Ctrl+C 或输入结束会停止后台工作，不会让 DS 无人管理地继续运行。

### 一次提交、自动执行

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/project run --prd /absolute/path/to/prd.md
uv run gpt-ds-bridge --workspace /absolute/path/to/project followup --message "增加参数校验"
```

`run` 自动规划并执行；`followup` 根据上轮 PRD、累计补充需求和实际结果重新规划并执行，恢复同一 DS 会话。有关键歧义时停在 `blocked`，补充回答后再提交。

只生成计划使用 `plan --prd 文件`，之后使用 `execute`。查询任务和报告位置：

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/project status
uv run gpt-ds-bridge --workspace /absolute/path/to/project history
uv run gpt-ds-bridge --workspace /absolute/path/to/project session
```

任务输出中的 `artifact_dir` 包含 `plan.md`、`plan.json`、`report.md` 和 `attempts/` 执行日志。聊天调用日志位于 `.gpt-ds-bridge/chat/`，讨论记录位于 SQLite。保留实际代码、工作区运行数据和原 `DSH_HOME`，才能恢复完整上下文；日志可能包含 PRD 和代码，不应公开提交。

单次任务失败后，检查变更再使用 `retry TASK_ID --acknowledge-side-effects`；如需放弃旧计划后重新提交，使用 `abandon TASK_ID`。详细恢复方法见 [运行参考](docs/operations.md)。

## 4. 一个完整例子

从一个空业务目录开始，让 GPT 和 DS 实现标准库 HTTP 服务，随后补充需求并介入执行。下面聊天中的“你”表示需要输入的内容，模型实际回答会不同。

### 第一步：安装、创建项目和 PRD

```bash
cd /Users/levi/develop/project/my/dsh/gpt-ds-bridge
uv sync --python 3.12 --extra dev
mkdir -p /tmp/gpt-ds-demo

cat > /tmp/gpt-ds-demo/PRD.md <<'PRD'
# 健康检查服务

这是一个空目录，请新建 Python 3.11+ 项目，仅使用标准库。

需求：
1. server.py 提供 HTTP 服务，支持 --port 参数，默认 8765，仅监听 127.0.0.1。
2. GET /health 返回 HTTP 200、application/json 和 {"status":"ok"}。
3. 未知路径返回 HTTP 404。
4. 在 tests/ 下使用 unittest 验证成功响应、Content-Type 和未知路径。
5. README 写明启动和测试命令。

验收：运行 python -m unittest discover -s tests -v 并报告实际结果。
不得安装第三方依赖，不要提交、推送或部署。
PRD

uv run gpt-ds-bridge --workspace /tmp/gpt-ds-demo init
```

确认 Codex 已完成认证。DS 二选一：在当前终端提供可用的 `DEEPSEEK_API_KEY`；或者，若已经通过 Harness 保存登录，编辑 `/tmp/gpt-ds-demo/.gpt-ds-bridge/config.toml`，将 `dsh_patches` 设置为第 2 节给出的补丁路径，并确认 `dsh_home` 正确。

检查环境并进入聊天：

```bash
uv run gpt-ds-bridge --workspace /tmp/gpt-ds-demo doctor
uv run gpt-ds-bridge --workspace /tmp/gpt-ds-demo chat --prd /tmp/gpt-ds-demo/PRD.md
```

### 第二步：先讨论，再执行

```text
你> 请先读 PRD，说明建议的文件结构和测试方式，暂时不要执行。
GPT：……

你> 保持简单，测试使用临时端口，不依赖固定端口或外部网络。
GPT：……

你> /plan
（等待计划显示，检查范围和验收标准）

你> /execute
（DS 开始执行，仍可继续输入）

你> /status
（查看执行状态和原生 DS session ID）

你> /wait
（等待 DS 结束，查看终端里的执行报告）
```

### 第三步：补充需求并介入

```text
你> 增加 GET /version，返回 {"version":"0.1.0"}，需要补哪些测试？
GPT：……

你> /plan
（等待计划生成）

你> /execute
```

如果 DS 仍在执行，而你想改变方向：

```text
你> /pause
（工具终止当前进程，保留已产生的代码变更）
```

如果只是暂时停一下，直接继续原计划，无需重新规划：

```text
你> /resume
你> /wait
```

如果确实想改变要求，可以查看现有修改后继续讨论，并主动生成新计划：

```text
你> 我已检查变更。版本号改为 0.2.0，保留已完成部分，只调整剩余实现和测试。
GPT：……

你> /plan
（新计划成功生成后，才替换原计划）

你> /execute
（DS 恢复原会话执行新计划）

你> /wait
你> /exit
```

如果任务很快，`/pause` 时已经完成，工具会提示没有后台工作；仍可讨论补充要求，再 `/plan`、`/execute`。

### 第四步：验证交付，下次继续

退出聊天后独立验证：

```bash
cd /tmp/gpt-ds-demo
python3.12 -m unittest discover -s tests -v
python3.12 server.py --port 8765
```

另开一个终端：

```bash
curl -i http://127.0.0.1:8765/health
curl -i http://127.0.0.1:8765/version
```

验证后在服务终端按 Ctrl+C 停止。下次继续讨论：

```bash
cd /Users/levi/develop/project/my/dsh/gpt-ds-bridge
uv run gpt-ds-bridge --workspace /tmp/gpt-ds-demo chat
```

再次对话会带上保存的讨论和执行结果；下一轮执行继续采用同一个 DS session。以上是完整使用步骤，真实模型调用取决于已配置模型、认证、额度和执行权限。
