# 单次任务、故障恢复与开发参考

## 6. 查询状态、日志和报告

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project status
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project status TASK_ID --json
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project history --limit 30
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project history --json
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project session
```

`TASK_ID` 替换为输出中的真实 ID。查询可以在另一个终端中进行，不需要等待执行锁。

每个业务工作区的数据结构如下：

```text
.gpt-ds-bridge/
├── .gitignore                  # 忽略本目录全部运行数据
├── config.toml                 # 当前工作区配置
├── state.sqlite3               # 状态、任务关系、会话映射
├── workspace.lock              # 工作区写入互斥锁
└── runs/<任务ID>/
    ├── input.json              # 原 PRD 与累计补充需求快照
    ├── context.json            # 规划时的会话与执行版本
    ├── planner-prompt.md       # 实际交给 GPT 的文本
    ├── plan.schema.json        # 计划 JSON Schema
    ├── plan.json               # 结构化计划
    ├── plan.md                 # 易读版计划
    ├── codex.process.json      # 进程 ID、命令数组及工作目录
    ├── codex.stdout.jsonl
    ├── codex.stderr.log
    ├── report.md               # 最新尝试的任务报告
    └── attempts/001/           # 每次执行/重试单独保存，不覆盖旧日志
        ├── execution-context.json # 执行时的 DSH_HOME 与工作区绑定
        ├── executor-prompt.md
        ├── dsh.process.json
        ├── dsh.stdout.jsonl
        ├── dsh.stderr.log
        └── response.md         # DS 正常完成时保存的最终回答
```

部分文件只有进入相应阶段后才存在。需要实时看工具事件时，对 `status` 返回的实际 `artifact_dir` 下的 `attempts/001/dsh.stdout.jsonl` 使用 `tail -f`。

DS 的原生历史仍由 Harness 保存到配置指定的 `DSH_HOME`；桥接器数据库只保存映射和运行状态。**恢复完整上下文需要同时保留工作区运行数据、DSH_HOME 和实际代码。** 不要仅复制 SQLite 后就删除原 Harness 会话文件，也不要随意移动整个工作区。

这些日志包含 PRD、模型输出和可能的代码内容；不要提交到公共仓库。桥接器初始化时将状态目录设置为当前用户可访问，配置不存密钥，但日志不是凭据脱敏器。

## 7. 失败、中断和恢复

### 查看失败

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project status
```

状态含义：

| 状态 | 含义 / 下一步 |
|---|---|
| `planning` | GPT 规划中 |
| `planned` | 计划已生成，等待 `execute` |
| `blocked` | 需要澄清，使用 `followup` 回答问题 |
| `executing` | DS 执行中 |
| `completed` | DS 正常结束，阅读报告核对验收 |
| `failed` | 模型、协议、认证或进程失败，先看日志 |
| `interrupted` | 超时或中断，先核对实际代码和副作用 |
| `abandoned` | 已放弃本轮，代码和原会话仍保留 |

DS 的成功判定同时要求：退出码为 0、有效 session 事件、回合结束原因为 `completed`、收到 `final`、没有协议错误。仅收到最终文本不算成功。

### 显式重试执行

确认已经检查实际代码、测试和外部副作用后：

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project retry TASK_ID \
  --acknowledge-side-effects
```

重试只适用于**当前计划对应的失败/中断 DS 执行任务**，恢复原会话并要求先核对已完成部分。它仍是再次投递任务，**不保证 exactly-once**，不会自动撤销先前变更，也不等于每个工具调用可安全重放。

规划阶段失败不能使用 `retry`：先修复认证/输出问题，`abandon TASK_ID` 后重新 `run` 或 `followup`。

### 放弃本轮，重新规划

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project abandon TASK_ID
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project followup \
  --message "我已检查上次变更，请保留已完成部分，调整剩余实现"
```

`abandon` 只改变桥接器任务状态，保留实际代码和 DS 历史。最新任务处于 `planned` / `failed` / `interrupted` 时，新 `run` / `followup` 会被阻止，避免默默覆盖未处理任务。

### 会话 ID 丢失或希望采用已有会话

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project session set EXISTING_SESSION_ID
```

只绑定你确认属于该项目、且已经持久化的 DS 原生会话。此命令不创建原生会话，也不预先验证它是否存在；下一次 Harness 恢复时进行验证。如果 ID 不存在、工作目录不一致、会话类型或 profile 不兼容，Harness 会报错，桥接器不会偷偷创建替代会话。

如果第一次执行在收到 session 事件前中断，无法凭空知道原会话 ID。检查 `dsh.stdout.jsonl` 或 Harness 侧的会话记录后再绑定。若始终无法找回，先核对副作用，放弃任务后重新规划。

### 主动开始新的 DS 会话

```bash
uv run gpt-ds-bridge --workspace /absolute/path/to/your-project session reset
```

这只清空当前映射，不删除历史、不回滚代码，也不清除累计 PRD。下一次执行创建新会话。新会话仍会收到 PRD 和计划，但不再拥有原生聊天历史。如果希望切换到完全不同的项目，请使用另一个工作区。

计划生成后若手工换 session 或修改 DSH_HOME，旧计划可能失效，需要放弃并重新规划。修改 `dsh_command` 或补丁不会自动迁移已有会话；建议先核对 Harness 版本和 profile 兼容性。

### 超时、Ctrl+C、进程被强杀

- 超时、Ctrl+C、SIGTERM：终止工具进程组，尽可能记录 `interrupted`；收到过的 DS session ID 仍保留。
- SIGKILL / 掉电：编排器无法执行清理。下次取得工作区锁后，把遗留 `planning` / `executing` 记录标记为 `interrupted`。在此之前，`status` 可能仍显示旧的运行状态。
- 子进程继承工作区锁，所以编排器被强杀后，仍存活并保留描述符的 DS 会继续持锁，避免第二个桥接器同时驱动项目。若提示锁一直被占用，请查看 `dsh.process.json` 的进程信息并核对真实进程状态；不要仅删除锁文件来“解锁”，那可能造成并发修改。
- 该锁只约束本桥接器；不要同时在 Harness Web 或另一个工具里驱动相同项目/会话。某些子进程可能自行关闭继承的文件描述符，硬杀后仍须核查是否有残留执行进程。

## 8. 常见问题

**`No module named` / Python 版本错误？** 在桥接器目录使用 `uv sync --python 3.12 --extra dev` 和 `uv run`，不要用默认的 Python 3.8。

**Codex 或 dsh 找不到？** 检查配置中的命令数组。缓存路径被清理后需要更新 `dsh_command`。不要把 `"node /path/bin.js"` 写成一个数组元素，应写成两个参数。

**Web 有登录，DS 仍报认证失败？** 参见第 3 节的 headless 凭据说明。桥接器不会擅自把 GUI 凭据注入环境变量，也不会修改你现有 Web 配置。

**DS 提示需要权限或交互？** headless 不能代替 GUI 交互。按 Harness 的权限配置提供所需执行能力，再检查后重试；桥接器不会自动提升权限或把模型回答当成授权。

**工作区路径变了 / DSH_HOME 改了？** 优先恢复原目录和配置。桥接器检测到绑定不一致会拒绝执行，避免悄悄丢失上下文。

**测试通过了吗？** `report.md` 是 DS 原文交付结果；请核对实际命令与输出。编排器没有独立的通用验收引擎，也没有默认自动循环审查或修复。

**会不会自动提交、推送或部署？** 桥接器没有这些主动操作；发送给模型的提示词也明确禁止。模型工具的真实权限仍取决于 Codex / Harness 配置，敏感项目建议使用隔离工作区。

## 9. 开发与验证

```bash
uv sync --python 3.12 --extra dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

测试在临时目录启动模拟 CLI 子进程，覆盖真实编排、SQLite、会话恢复、阻断计划、协议错误、超时、信号退出及锁行为。不调用真实模型，不消耗 API 额度，不修改业务项目。

本机已核实 DeepSeek Harness `0.2.1-rc.2` 的 headless `--session-id` / `--json` 接口。其他版本如有协议变化，请先运行 `doctor` 并核对该版本文档。真实模型端到端调用与模拟测试是两种不同的验证，不应混淆。

代码分工：

| 文件 | 职责 |
|---|---|
| `config.py` | 配置校验、本机命令发现 |
| `models.py` | 计划结构与 ready 规则 |
| `store.py` | SQLite 与工作区锁 |
| `process.py` | 子进程、日志、超时及清理 |
| `adapters.py` | Codex / Harness 协议适配 |
| `service.py` | 状态机、上下文和任务报告 |
| `cli.py` | 用户命令与中文错误提示 |

后续可以保留状态机与存储，新增 SDK 适配器或 FastAPI 界面；不必重写 DS 会话管理。当前没有 Web 服务、MCP 服务、后台守护进程、分布式队列或自动部署功能。

## 聊天暂停后继续原计划

普通讨论和重新加载 PRD 不会让当前计划失效。`/resume` 或暂停后的 `/execute` 直接恢复当前计划，不重新调用 GPT。新 `/plan` 成功且可执行才替换当前计划；失败、取消或 blocked 保留原计划。聊天 `/status` 同时展示最新规划尝试与 `current_plan`，二者可能不同。
