"""对接本机 Codex 与 DeepSeek Harness，隔离两种 CLI 的协议差异。"""

import json
import os
from collections.abc import Callable
from pathlib import Path

from .config import BridgeError, Config
from .models import Plan
from .process import run_process


async def discuss(
    config: Config,
    workspace: Path,
    artifact: Path,
    prompt: str,
    lock_fd: int | None = None,
) -> str:
    """聊天使用普通文本输出；通过磁盘记录重建上下文，不猜测最近 Codex 会话。"""
    artifact.mkdir(parents=True, mode=0o700)
    output = artifact / "response.md"
    (artifact / "prompt.md").write_text(prompt, encoding="utf-8")
    command = [
        *config.codex_command,
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--json",
        "--color",
        "never",
        "--output-last-message",
        str(output),
    ]
    if config.codex_model:
        command += ["--model", config.codex_model]
    command.append("-")
    code = await run_process(
        command,
        cwd=workspace,
        prompt=prompt,
        log_prefix=artifact / "codex",
        timeout=config.planner_timeout,
        lock_fd=lock_fd,
    )
    if code or not output.is_file():
        raise BridgeError(f"GPT 对话失败，退出码 {code}；查看 {artifact}")
    response = output.read_text(encoding="utf-8").strip()
    if not response:
        raise BridgeError("GPT 没有返回对话内容")
    return response


async def generate_plan(
    config: Config,
    workspace: Path,
    artifact: Path,
    prompt: str,
    lock_fd: int | None = None,
) -> Plan:
    schema = artifact / "plan.schema.json"
    output = artifact / "plan.json"
    schema.write_text(
        json.dumps(Plan.model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (artifact / "planner-prompt.md").write_text(prompt, encoding="utf-8")
    command = [
        *config.codex_command,
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--json",
        "--color",
        "never",
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(output),
    ]
    if config.codex_model:
        command += ["--model", config.codex_model]
    command.append("-")
    code = await run_process(
        command,
        cwd=workspace,
        prompt=prompt,
        log_prefix=artifact / "codex",
        timeout=config.planner_timeout,
        lock_fd=lock_fd,
    )
    if code != 0:
        raise BridgeError(f"GPT 规划失败，退出码 {code}；查看 {artifact / 'codex.stderr.log'}")
    if not output.is_file():
        raise BridgeError("规划器没有生成 plan.json；请检查 Codex 版本和日志")
    # 不能因为模型声称满足 schema 就跳过本地校验。
    return Plan.model_validate_json(output.read_text(encoding="utf-8"))


async def execute_ds(
    config: Config,
    workspace: Path,
    artifact: Path,
    prompt: str,
    session_id: str | None,
    on_session: Callable[[str], None],
    lock_fd: int | None = None,
) -> str:
    command = [*config.dsh_command, "--profile", "headless"]
    for patch in config.dsh_patches:
        command += ["--patch", patch]
    command.append("--json")
    if session_id:
        command += ["--session-id", session_id]
    command.append("-")
    observed_session = None
    final_text = None
    terminal_kind = None
    protocol_errors = []

    def accept(line: str):
        nonlocal observed_session, final_text, terminal_kind
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BridgeError("DS stdout 不是合法 JSONL；执行可能已发生，请检查原始日志") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise BridgeError("DS 事件格式不合法")
        kind = event["type"]
        if kind == "session":
            value = event.get("sessionId")
            if not isinstance(value, str) or not value.strip():
                raise BridgeError("DS 没有返回有效 sessionId")
            if (session_id and value != session_id) or (
                observed_session and value != observed_session
            ):
                raise BridgeError("DS 返回的会话与预期不一致，已停止执行")
            if event.get("cwd") and Path(event["cwd"]).resolve() != workspace:
                raise BridgeError("DS 返回的工作目录与当前工作区不一致")
            observed_session = value
            # 收到会话事件立刻提交 SQLite，后续失败也不丢失上下文定位。
            on_session(value)
        elif kind == "final":
            if not isinstance(event.get("text"), str):
                raise BridgeError("DS final 事件缺少文本")
            final_text = event["text"]
        elif kind == "status" and event.get("phase") == "turn_end":
            reason = event.get("reason")
            terminal_kind = reason.get("kind") if isinstance(reason, dict) else None
        elif kind == "error":
            protocol_errors.append(str(event.get("message", "DS 返回错误")))

    (artifact / "executor-prompt.md").write_text(prompt, encoding="utf-8")
    code = await run_process(
        command,
        cwd=workspace,
        prompt=prompt,
        log_prefix=artifact / "dsh",
        timeout=config.executor_timeout,
        env={**os.environ, "DSH_HOME": config.dsh_home},
        on_line=accept,
        lock_fd=lock_fd,
    )
    if code != 0 or protocol_errors:
        detail = "; ".join(protocol_errors)[:1000]
        raise BridgeError(
            f"DS 执行失败，退出码 {code}。{detail} 查看 {artifact / 'dsh.stderr.log'}"
        )
    if not observed_session or final_text is None or terminal_kind != "completed":
        raise BridgeError("DS 缺少成功结束标记；不会仅凭 final 文本或退出码判定成功")
    (artifact / "response.md").write_text(final_text, encoding="utf-8")
    return final_text
