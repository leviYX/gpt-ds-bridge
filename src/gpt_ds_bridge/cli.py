"""用户入口：所有变更操作共享工作区锁，查询操作可在执行期间使用。"""

import argparse
import asyncio
import json
import signal
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .config import BridgeError, initialize, load_config, state_dir
from .service import Bridge, read_text
from .store import Store, workspace_lock


def parser():
    root = argparse.ArgumentParser(description="GPT 规划 → DeepSeek Harness 持久会话执行")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--workspace", type=Path, default=Path.cwd(), help="实际要修改的项目目录")
    subs = root.add_subparsers(dest="command", required=True)
    subs.add_parser("init", help="初始化工作区配置，自动发现本机 CLI")
    subs.add_parser("doctor", help="检查配置、CLI 和本机会话绑定，不调用模型")
    cmd = subs.add_parser("chat", help="与 GPT 讨论，确认计划后执行，可随时介入")
    cmd.add_argument("--prd", type=Path, help="加载 PRD；省略时恢复上次讨论")
    for name in ["run", "plan"]:
        cmd = subs.add_parser(name, help="生成计划并自动执行" if name == "run" else "只生成计划")
        cmd.add_argument("--prd", type=Path, required=True, help="UTF-8 Markdown/文本 PRD")
    cmd = subs.add_parser("execute", help="执行已生成的最新计划")
    cmd.add_argument("run_id", nargs="?")
    cmd = subs.add_parser("followup", help="基于原 PRD、实际代码和历史结果规划补充需求并执行")
    group = cmd.add_mutually_exclusive_group(required=True)
    group.add_argument("--message")
    group.add_argument("--file", type=Path)
    cmd.add_argument("--plan-only", action="store_true")
    cmd = subs.add_parser("retry", help="检查副作用后，恢复失败或中断的执行任务")
    cmd.add_argument("run_id")
    cmd.add_argument(
        "--acknowledge-side-effects",
        action="store_true",
        help="确认已检查上次执行的实际代码和副作用",
    )
    cmd = subs.add_parser("abandon", help="放弃本轮，保留代码和会话，不回滚")
    cmd.add_argument("run_id")
    for name in ["status", "history"]:
        cmd = subs.add_parser(name, help="查询任务状态" if name == "status" else "查询历史")
        cmd.add_argument("--json", action="store_true")
        if name == "status":
            cmd.add_argument("run_id", nargs="?")
        else:
            cmd.add_argument("--limit", type=int, default=20)
    cmd = subs.add_parser("session", help="查看、绑定或重置 DS 会话映射")
    cmd.add_argument("action", nargs="?", choices=["show", "set", "reset"], default="show")
    cmd.add_argument("session_id", nargs="?")
    return root


def display(value, as_json=False):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, list):
        for run in value:
            print(f"{run['id']}  {run['status']:<12}  {run['created_at']}  {run['artifact_dir']}")
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def doctor(config, bridge):
    """只检查可执行程序；不读取或打印凭据，也不声称验证了模型连通性。"""
    failed = False
    for name, command in [
        ("Codex", config.codex_command),
        ("DeepSeek Harness", config.dsh_command),
    ]:
        try:
            result = subprocess.run(
                [*command, "--version"], capture_output=True, text=True, timeout=15
            )
            if result.returncode:
                raise BridgeError(f"版本检查退出码 {result.returncode}")
            print(f"{name}: {(result.stdout.strip() or result.stderr.strip())[:200]}")
        except (OSError, subprocess.TimeoutExpired, BridgeError) as exc:
            print(f"{name}: 不可用（{exc}）")
            failed = True
    for patch in config.dsh_patches:
        if not Path(patch).is_file():
            print(f"补丁不存在：{patch}")
            failed = True
    print(f"DSH_HOME: {config.dsh_home}")
    bridge.check_session()
    print(f"当前 DS 会话: {(bridge.session() or {}).get('id', '尚未建立')}")
    print("未调用模型，未验证账户额度、API 连通性或实际执行权限。")
    return 1 if failed else 0


async def dispatch(args, bridge):
    if args.command == "chat":
        from .chat import run_chat

        return await run_chat(bridge, args.prd)
    if args.command in {"run", "plan"}:
        run = await bridge.plan(read_text(args.prd.expanduser().resolve()))
        return await bridge.execute(run["id"]) if args.command == "run" else run
    if args.command == "execute":
        return await bridge.execute(args.run_id)
    if args.command == "retry":
        if not args.acknowledge_side_effects:
            raise BridgeError("请先检查上次的代码变更和副作用，再传 --acknowledge-side-effects")
        return await bridge.execute(args.run_id, retry=True)
    if args.command == "followup":
        latest = bridge.store.get()
        inputs = json.loads(
            (Path(latest["artifact_dir"]) / "input.json").read_text(encoding="utf-8")
        )
        message = read_text(args.file.expanduser().resolve()) if args.file else args.message
        if not message.strip():
            raise BridgeError("补充需求不能为空")
        # 每轮都保留累计补充要求，而不是只保存最近一次的一句话。
        followup = "\n\n".join(s for s in [inputs["followup"], message] if s)
        run = await bridge.plan(inputs["prd"], followup)
        return run if args.plan_only else await bridge.execute(run["id"])
    if args.command == "abandon":
        bridge.abandon(args.run_id)
        return bridge.store.get(args.run_id)
    if args.command == "session":
        if args.action == "reset":
            bridge.store.set_metadata("session", None)
        elif args.action == "set":
            if not args.session_id or not args.session_id.strip():
                raise BridgeError("session set 必须提供已有的 session ID")
            bridge.store.set_metadata(
                "session",
                {
                    "id": args.session_id,
                    "home": bridge.config.dsh_home,
                    "workspace": str(bridge.workspace),
                },
            )
        return bridge.session()
    raise BridgeError("未知命令")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    store = None
    try:
        if not workspace.is_dir():
            raise BridgeError(f"工作区目录不存在：{workspace}")
        if args.command == "init":
            print(f"已初始化：{initialize(workspace)}")
            return 0
        config = load_config(workspace)
        store = Store(state_dir(workspace))
        bridge = Bridge(workspace, config, store)
        if args.command == "doctor":
            return doctor(config, bridge)
        if args.command == "status":
            display(store.get(args.run_id), args.json)
            return 0
        if args.command == "history":
            if args.limit < 1:
                raise BridgeError("--limit 必须大于 0")
            display(store.history(args.limit), args.json)
            return 0
        if args.command == "session" and args.action == "show":
            display(bridge.session())
            return 0
        with workspace_lock(state_dir(workspace), config.lock_timeout) as lock_fd:
            bridge.lock_fd = lock_fd
            store.recover_abandoned()
            # asyncio.Runner 的 SIGINT 处理会先取消协程；SIGTERM 也走同一清理路径。
            previous = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *_: signal.raise_signal(signal.SIGINT))
            try:
                result = asyncio.run(dispatch(args, bridge))
            finally:
                signal.signal(signal.SIGTERM, previous)
            display(result)
        return 0
    except KeyboardInterrupt:
        print("已中断；已终止子进程，查看 status 后决定如何恢复。", file=sys.stderr)
        return 130
    except (BridgeError, ValidationError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
