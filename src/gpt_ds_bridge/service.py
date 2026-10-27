"""编排状态机：计划自动交接、会话绑定、交付物及显式故障恢复。"""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from .adapters import execute_ds, generate_plan
from .config import BridgeError, Config, state_dir
from .models import Plan
from .process import ProcessInterrupted
from .store import Store, now


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_text(path: Path) -> str:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise BridgeError("输入文件超过 2 MiB，请精简 PRD 或补充说明")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise BridgeError("输入不能为空")
    return text


class Bridge:
    def __init__(self, workspace: Path, config: Config, store: Store):
        self.workspace, self.config, self.store = workspace, config, store
        self.lock_fd = None

    def session(self):
        return self.store.metadata("session")

    def check_session(self):
        binding = self.session()
        if binding and (
            binding["home"] != self.config.dsh_home or binding["workspace"] != str(self.workspace)
        ):
            raise BridgeError("DSH_HOME 或工作目录已经改变；请恢复原配置，或检查后 session reset")
        return binding

    def check_previous(self):
        rows = self.store.history(1)
        if rows and rows[0]["status"] in {"failed", "interrupted", "planned"}:
            raise BridgeError(
                "上个任务尚未处理：planned 请 execute；失败/中断请检查后 retry 或 abandon"
            )

    def previous_context(self) -> str:
        chunks = []
        # 近期交付摘要传给 GPT；更早事实可由规划器读取当前代码和状态文件。
        for run in reversed(self.store.history(5)):
            root = Path(run["artifact_dir"])
            report = root / "report.md"
            chunks.append(
                f"任务 {run['id']}，状态 {run['status']}\n"
                + (report.read_text(encoding="utf-8")[-6000:] if report.exists() else "")
            )
        return "\n\n".join(chunks)

    async def plan(
        self,
        prd: str,
        followup: str = "",
        *,
        replace_current: bool = False,
    ) -> dict:
        self.check_session()
        if not replace_current:
            self.check_previous()
        try:
            previous = self.store.current_plan()
        except BridgeError:
            previous = None
        run_id = uuid4().hex[:12]
        root = state_dir(self.workspace) / "runs" / run_id
        root.mkdir(parents=True, mode=0o700)
        parent = self.store.history(1)
        self.store.create(run_id, root, parent[0]["id"] if parent else None)
        write_json(root / "input.json", {"prd": prd, "followup": followup})
        write_json(
            root / "context.json",
            {
                "execution_revision": self.store.metadata("execution_revision") or 0,
                "session": self.session(),
            },
        )
        print(f"[{run_id}] GPT 正在读取项目并生成计划…", file=sys.stderr)
        prompt = f"""你是项目规划者。请先读取当前工作区的代码、README 和适用的 AGENTS.md，
根据 PRD 和补充需求输出满足 JSON Schema 的可执行计划，不修改任何代码，不调用会产生外部写入的工具。
任务要包含具体修改位置、依赖顺序、验收标准和适用的测试命令。
如果存在阻止实现的歧义，ready=false 并填写 questions，禁止擅自补全关键业务规则。
能合理确定的非关键假设请列入 assumptions。禁止要求自动提交、推送或部署。
项目会话依赖磁盘状态；不要假定你记得上一轮对话。历史摘要仅供参考，以当前代码为准。

工作区：{self.workspace}

<prd>
{prd}
</prd>
<followup>
{followup}
</followup>
<previous_results>
{self.previous_context()}
</previous_results>
"""
        try:
            plan = await generate_plan(self.config, self.workspace, root, prompt, self.lock_fd)
            (root / "plan.md").write_text(plan.markdown(), encoding="utf-8")
            if plan.ready:
                previous_id = previous["id"] if previous else None
                self.store.activate_plan(run_id, previous_id)
                if previous and self.store.get(previous_id)["status"] == "abandoned":
                    self.report(previous_id, f"已由新计划 {run_id} 替换；原日志和代码变更保留。")
            else:
                self.store.update(run_id, status="blocked")
            if not plan.ready:
                self.report(run_id, "待澄清问题：\n\n" + "\n".join(plan.questions))
                raise BridgeError(f"计划需要澄清，未启动 DS：{'；'.join(plan.questions)}")
        except BaseException as exc:
            if self.store.get(run_id)["status"] == "planning":
                self.fail(run_id, exc)
            raise
        return self.store.get(run_id)

    def fail(self, run_id: str, exc: BaseException):
        interrupted = isinstance(
            exc, (ProcessInterrupted, asyncio.CancelledError, KeyboardInterrupt)
        )
        self.store.update(
            run_id,
            status="interrupted" if interrupted else "failed",
            error=str(exc) or "执行被中断",
        )
        self.report(
            run_id,
            f"异常：{str(exc) or '执行被中断'}\n\n"
            "请先检查代码变更和日志；编排器不会自动重放有副作用的任务。",
        )

    def report(self, run_id: str, detail: str):
        run = self.store.get(run_id)
        text = (
            f"# 任务报告 {run_id}\n\n状态：{run['status']}\n\n"
            f"更新时间：{now()}\n\nDS 会话：{run['session_id'] or '尚未收到会话事件'}\n\n"
            "completed 仅表示 Harness 正常结束；验收与测试证据以执行报告为准。\n\n" + detail
        )
        (Path(run["artifact_dir"]) / "report.md").write_text(text + "\n", encoding="utf-8")

    async def execute(self, run_id: str | None = None, *, retry: bool = False):
        current = self.store.current_plan()
        run = self.store.get(run_id) if run_id else current
        run_id = run["id"]
        if run_id != current["id"]:
            raise BridgeError("只允许执行当前计划；这个计划已被替换或放弃")
        root = Path(run["artifact_dir"])
        if retry:
            if run["status"] not in {"failed", "interrupted"} or run["phase"] != "executing":
                raise BridgeError("此记录不是可重试的执行任务；规划失败请 abandon 后重新提交")
        elif run["status"] != "planned":
            raise BridgeError("只有 planned 状态可以 execute；不会重复投递已执行的任务")
        binding = self.check_session()
        if retry and not binding:
            raise BridgeError(
                "缺少原 DS 会话 ID；先从日志确认后 session set，或 abandon 后重新规划"
            )
        if retry and run["session_id"] and binding["id"] != run["session_id"]:
            raise BridgeError("重试必须恢复该任务的原 DS 会话；请恢复原映射，或放弃后重新规划")
        if retry:
            attempts = sorted((root / "attempts").iterdir())
            previous_execution = json.loads(
                (attempts[-1] / "execution-context.json").read_text(encoding="utf-8")
            )
            if previous_execution != {
                "home": self.config.dsh_home,
                "workspace": str(self.workspace),
            }:
                raise BridgeError("重试的 DSH_HOME 或工作区与原执行不同；请恢复原配置")
        context = json.loads((root / "context.json").read_text(encoding="utf-8"))
        revision = self.store.metadata("execution_revision") or 0
        if not retry and (
            context["execution_revision"] != revision or context["session"] != binding
        ):
            raise BridgeError("计划生成后的执行状态或会话已改变，请 abandon 后重新规划")
        plan = Plan.model_validate_json((root / "plan.json").read_text(encoding="utf-8"))
        if not plan.ready:
            raise BridgeError("存在待澄清问题，不能执行")
        inputs = json.loads((root / "input.json").read_text(encoding="utf-8"))
        attempts = root / "attempts"
        attempts.mkdir(exist_ok=True)
        attempt = attempts / f"{len(list(attempts.iterdir())) + 1:03d}"
        attempt.mkdir()
        write_json(
            attempt / "execution-context.json",
            {
                "home": self.config.dsh_home,
                "workspace": str(self.workspace),
            },
        )
        self.store.update(run_id, status="executing", phase="executing", error=None)
        self.store.set_metadata("execution_revision", revision + 1)
        session_id = binding["id"] if binding else None
        if session_id:
            self.store.update(run_id, session_id=session_id)

        def bind_session(value):
            self.store.set_metadata(
                "session",
                {"id": value, "home": self.config.dsh_home, "workspace": str(self.workspace)},
            )
            self.store.update(run_id, session_id=value)

        retry_instruction = (
            "上次执行失败或中断。先检查当前代码、历史工具结果和实际副作用，"
            "只补做未完成部分，不盲目重复任何操作。"
            if retry
            else ""
        )
        prompt = f"""请在当前代码仓库按以下计划执行，遵守适用的 AGENTS.md。
先核对当前实现，再逐项修改并验证；不要自动提交、推送、部署或执行破坏性操作。
业务规则仍有歧义或需要交互确认时，停止并在最终报告中说明。
最终报告使用中文，列出实际修改、测试命令及结果、未完成事项，不能把未运行的测试说成通过。
{retry_instruction}

<prd>
{inputs["prd"]}
</prd>
<followup>
{inputs["followup"]}
</followup>
<plan>
{plan.model_dump_json(indent=2)}
</plan>
"""
        print(f"[{run_id}] DS 开始执行（会话：{session_id or '新建'}）…", file=sys.stderr)
        try:
            result = await execute_ds(
                self.config, self.workspace, attempt, prompt, session_id, bind_session, self.lock_fd
            )
            self.store.update(run_id, status="completed")
            self.report(run_id, result)
        except BaseException as exc:
            self.fail(run_id, exc)
            raise
        return self.store.get(run_id)

    def abandon(self, run_id: str):
        run = self.store.get(run_id)
        if run["status"] not in {"failed", "interrupted", "planned", "blocked"}:
            raise BridgeError("只能放弃尚未完成的任务")
        self.store.update(run_id, status="abandoned")
        self.report(run_id, "用户放弃本轮任务。保留 DS 会话和实际代码变更，不执行回滚。")
