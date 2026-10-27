# GPT → DeepSeek Bridge 实现计划

## 已确认的目标

用户提交一次 PRD，Python 自动调用 GPT 生成计划，并交给本机 DeepSeek Harness 执行；后续需求恢复相同 DS 会话。README 完整描述使用方式，核心代码使用中文注释。目标目录为空，无上级 AGENTS.md 或既有 Git 仓库，因此直接在用户指定目录实现。

## 设计

- Python 3.11+、asyncio、Pydantic、SQLite；先提供命令行入口。
- 默认规划器使用 `codex exec` 的只读模式、JSON Schema 与最终输出文件；读取实际仓库、PRD 和历史交付结果。不依赖 ChatGPT 网页自动化。
- 执行器使用 `dsh --profile headless --json`，首次记录 session 事件，后续传 `--session-id`。保持工作目录与 DSH_HOME 稳定。
- 每个工作区在 `.gpt-ds-bridge/` 保存配置、SQLite、计划、输入快照、进程日志和最终报告。文件锁串行化同一工作区内的操作。
- 状态机：planning → planned / blocked / failed；planned → executing → completed / failed / interrupted。恢复不自动重复执行有副作用的任务，必须显式 retry 并确认检查过副作用。
- 规划不明确则 blocked，不启动 DS。报告中的 completed 仅表示 Harness 成功结束，不等于验收或测试已自动通过。

## 文件职责

`config.py` 配置与本机命令发现；`models.py` 计划校验；`store.py` SQLite 与锁；`process.py` 异步进程及终止；`adapters.py` Codex/DS 协议；`service.py` 状态机与交付物；`cli.py` 用户命令；`tests/` 隔离子进程协议与恢复回归测试。

## 执行顺序

1. [x] 建立打包配置、测试夹具及端到端测试；先确认缺少功能时测试失败。
2. [x] 实现配置、计划校验、存储、文件锁与进程生命周期。
3. [x] 实现规划/执行适配器、会话绑定与自动交接。
4. [x] 实现 CLI：init、doctor、run、plan、execute、followup、retry、status、history、session。
5. [x] 补齐中文 README、示例 PRD、故障处理说明。
6. [x] 验证同会话续接、阻断计划、退出码/终止原因、超时、进程中断、并发锁、CLI 安装、实际本机工具版本。

## 验证边界

自动化测试使用模拟 Codex/DS 可执行程序，运行真正的子进程、SQLite 和 CLI，不消耗模型额度或修改用户业务代码。真实工具验证使用 version/help，不将未执行的模型调用声称为已跑通。

## 验证记录

- 先运行缺少实现时的测试，观察失败；再实现并通过会话交接与异常场景测试。
- 真实 CLI 子进程回归覆盖：原会话续接、累计需求、blocked、计划/执行分离、防止重复执行、失败后显式重试、原会话校验、错误协议、错误终止原因、超时、SIGTERM、父进程 SIGKILL 后的锁继承。
- `uv sync --python 3.12 --extra dev` 安装项目与命令入口成功。
- 在临时工作区运行真实 `init` / `doctor`，发现 Codex `0.164.0-alpha.11.1` 和 DeepSeek Harness `0.2.1-rc.2`。
- 不调用真实模型，不修改用户已有业务仓库或 Harness 的全局配置；模型真实连通性尚未验证。
