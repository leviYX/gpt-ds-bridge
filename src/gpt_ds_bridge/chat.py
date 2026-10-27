"""终端聊天：异步读取输入，使 GPT 回答或 DS 执行时仍能响应暂停命令。"""

import asyncio
import json
import os
import shlex
import sys
from collections import deque
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .adapters import discuss
from .config import BridgeError, state_dir
from .service import Bridge, read_text

HELP = """普通文字：与 GPT 讨论，不自动执行代码。
/prd 文件路径       加载或替换 PRD（路径含空格请加引号）
/plan              根据 PRD、讨论及当前代码生成计划
/execute           执行当前计划；暂停后继续原计划
/resume            不重新规划，恢复当前计划的执行
/status            查看任务状态、会话和后台工作
/pause             停止当前 GPT/DS 进程，保留已产生的变更和上下文
/revise --checked  确认已检查失败/中断的副作用，放弃旧任务并重新规划
/wait              等待当前 GPT/DS 工作结束
/history           查看本工具保存的 GPT 讨论记录
/help              查看帮助
/exit              停止后台工作、保存记录并退出
"""


class Chat:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.store = bridge.store
        self.gpt_job = None
        self.ds_job = None

    def say(self, text):
        # 后台任务也会输出，强制刷新，避免管道模式下迟迟看不到状态。
        print(text, flush=True)

    @staticmethod
    def active(job):
        return job is not None and not job.done()

    def load_prd(self, path: Path):
        prd = read_text(path.expanduser().resolve())
        self.store.set_metadata("chat_prd", prd)
        self.store.add_message("system", f"用户加载了新的 PRD：\n{prd}")
        self.say(f"已加载 PRD：{path}；当前计划保留，使用 /plan 才会生成替代计划。")

    def transcript(self):
        # 完整记录留在 SQLite；不静默截断已讨论的约束。
        return json.dumps(self.store.messages(), ensure_ascii=False)

    def ensure_idle_gpt(self):
        if self.active(self.gpt_job):
            raise BridgeError("GPT 正在回答或规划，请等待，或 /pause 停止后再操作")

    def ensure_idle(self):
        self.ensure_idle_gpt()
        if self.active(self.ds_job):
            raise BridgeError("DS 正在执行，请先 /pause，再调整执行计划")

    async def guarded(self, operation):
        """后台异常展示到终端，不让失败悄悄留在无人读取的 Task 中。"""
        try:
            await operation()
        except asyncio.CancelledError:
            self.say("后台工作已停止；如有代码变更，请先检查后再继续。")
            raise
        except (BridgeError, ValidationError, OSError, ValueError) as exc:
            self.say(f"错误：{exc}")

    def talk(self, message: str):
        self.ensure_idle_gpt()
        self.store.add_message("user", message)
        prompt = f"""你是此项目的 GPT 需求与实现顾问。请用中文与用户讨论。
本轮只分析、解释、澄清需求，不改代码，不执行写操作，不调用外部写入工具。
普通讨论不会改变当前计划。/plan 才生成新计划；/execute 或 /resume 按当前计划执行。
不要声称你已改变正在执行的 DS 任务；如用户决定采纳新需求，请提醒其主动生成新计划。
DS 可能正在修改代码；状态和结果仅为当前快照，不要假定中间文件已经稳定。
历史由本工具重建；消息中的建议不等于用户已经确认的要求，须区分待选方案与已定需求。
请结合当前工作区代码、PRD、完整讨论和执行结果回答用户的最后一个问题。

工作区：{self.bridge.workspace}
<prd>{self.store.metadata("chat_prd") or "尚未加载 PRD"}</prd>
<discussion>{self.transcript()}</discussion>
<execution_results>{self.bridge.previous_context()}</execution_results>
"""

        async def reply():
            root = state_dir(self.bridge.workspace) / "chat" / uuid4().hex[:12]
            response = await discuss(
                self.bridge.config, self.bridge.workspace, root, prompt, self.bridge.lock_fd
            )
            self.store.add_message("assistant", response)
            self.say(f"\nGPT：{response}\n")

        self.gpt_job = asyncio.create_task(self.guarded(reply))
        self.say("GPT 正在思考；仍可使用 /status 或 /pause。")

    def plan(self, *, revise: bool = False):
        self.ensure_idle()
        prd = self.store.metadata("chat_prd")
        if not prd:
            raise BridgeError("尚未加载 PRD，请使用 /prd 文件路径")
        discussion = "以下为完整讨论记录，请提取用户确认的约束，不将未确认建议当作要求。\n"
        discussion += self.transcript()
        if revise:
            discussion += "\n上次执行可能有部分变更；请先核对当前代码，只规划仍需完成的内容。"

        async def prepare():
            # 生成成功且 ready 后才替换当前计划；中途失败时原计划继续可用。
            run = await self.bridge.plan(prd, discussion, replace_current=True)
            content = (Path(run["artifact_dir"]) / "plan.md").read_text(encoding="utf-8")
            self.say(f"\n{content}\n已切换到新计划 {run['id']}。输入 /execute 执行。")

        self.gpt_job = asyncio.create_task(self.guarded(prepare))

    def execute(self, *, resume_only: bool = False):
        self.ensure_idle()
        run = self.store.current_plan()
        resume = run["status"] in {"failed", "interrupted"} and run["phase"] == "executing"
        if resume_only and not resume:
            raise BridgeError("当前计划没有中断的执行；未开始的计划请用 /execute")
        if not resume and run["status"] != "planned":
            raise BridgeError("当前计划已经完成，不会重复执行；新任务请用 /plan")

        async def work():
            result = await self.bridge.execute(run["id"], retry=resume)
            report = (Path(result["artifact_dir"]) / "report.md").read_text(encoding="utf-8")
            self.store.add_message("execution", report)
            self.say(f"\nDS 已结束，状态：{result['status']}\n{report}\n")

        self.ds_job = asyncio.create_task(self.guarded(work))
        self.say(
            f"已请求{'继续' if resume else '执行'}当前计划 {run['id']}；不会重新调用 GPT 规划。"
        )

    async def wait(self):
        jobs = [job for job in [self.gpt_job, self.ds_job] if job is not None]
        if jobs:
            # 退出输入循环时可能取消 /wait；实际后台任务统一由 pause 取消，避免二次取消清理。
            await asyncio.gather(*(asyncio.shield(job) for job in jobs), return_exceptions=True)

    async def pause(self):
        jobs = [job for job in [self.gpt_job, self.ds_job] if self.active(job)]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
            self.store.add_message("system", "用户停止了后台工作；原代码变更保留，继续前必须核查。")
            self.say("已暂停，当前计划和变更保留。/resume 继续原计划；/plan 才重新规划。")
            self.status()
        else:
            self.say("当前没有正在运行的后台工作。")

    def status(self):
        latest = self.store.history(1)
        try:
            current = self.store.current_plan()
        except BridgeError:
            current = None
        self.say(
            json.dumps(
                {
                    "gpt_busy": self.active(self.gpt_job),
                    "ds_busy": self.active(self.ds_job),
                    "session": self.bridge.session(),
                    "latest_run": latest[0] if latest else None,
                    "current_plan": current,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    async def handle(self, line: str) -> bool:
        if not line.startswith("/"):
            self.talk(line)
            return True
        args = shlex.split(line)
        command = args[0]
        if command in {"/exit", "/quit"}:
            return False
        if command == "/help":
            self.say(HELP)
        elif command == "/status":
            self.status()
        elif command == "/history":
            for message in self.store.messages():
                self.say(f"{message['role']}：{message['content']}\n")
        elif command == "/wait":
            await self.wait()
        elif command == "/pause":
            await self.pause()
        elif command == "/prd":
            self.ensure_idle()
            if len(args) != 2:
                raise BridgeError('用法：/prd "/绝对路径/PRD.md"')
            self.load_prd(Path(args[1]))
        elif command == "/plan":
            self.plan()
        elif command == "/execute":
            self.execute()
        elif command == "/resume":
            self.execute(resume_only=True)
        elif command == "/revise":
            if args[1:] != ["--checked"]:
                raise BridgeError("请先检查已产生的变更和副作用，再使用 /revise --checked")
            self.plan(revise=True)
        else:
            raise BridgeError("未知聊天命令，请输入 /help")
        return True


async def run_chat(bridge: Bridge, prd: Path | None):
    chat = Chat(bridge)
    if prd:
        chat.load_prd(prd)
    elif not chat.store.metadata("chat_prd") and (latest := chat.store.history(1)):
        inputs = json.loads((Path(latest[0]["artifact_dir"]) / "input.json").read_text())
        chat.store.set_metadata("chat_prd", inputs["prd"])
        if inputs["followup"]:
            chat.store.add_message("user", inputs["followup"])
    chat.say("已进入 GPT–DS 聊天。普通文字只讨论；/plan 规划，/execute 执行，/help 查看帮助。")
    chat.say(f"已恢复 {len(chat.store.messages())} 条讨论记录。")
    # 使用异步管道读取 stdin；不能在线程中永久阻塞 input()，否则退出后仍可能挂住。
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    interactive = sys.stdin.isatty()
    # transport 会在 EOF 关闭传入对象，使用副本，避免缓冲区尚有命令时 stdin 已关闭。
    pipe = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, pipe)

    async def handle(text):
        try:
            return await chat.handle(text)
        except (BridgeError, ValidationError, OSError, ValueError) as exc:
            chat.say(f"错误：{exc}")
            return True

    # /wait 是命令顺序屏障，但不能占住输入循环。等待时优先处理暂停、状态和退出。
    pending = deque()
    command_task = None
    command_text = None
    input_task = asyncio.create_task(reader.readline())
    eof = False
    if interactive:
        print("你> ", end="", flush=True)
    try:
        while True:
            if command_task is not None and command_task.done():
                if not command_task.result():
                    break
                command_task = None
            if command_task is None and pending:
                command_text = pending.popleft()
                command_task = asyncio.create_task(handle(command_text))
            if eof and not pending and command_task is None:
                break
            watchers = [task for task in [command_task, input_task] if task is not None]
            done, _ = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
            if input_task in done:
                line = input_task.result()
                if not line:
                    eof = True
                    input_task = None
                    continue
                text = line.decode("utf-8").strip()
                if (
                    interactive
                    and text in {"/exit", "/quit"}
                    and command_text == "/wait"
                    and command_task
                    and not command_task.done()
                ):
                    break
                if text:
                    if (
                        command_task
                        and not command_task.done()
                        and text in {"/pause", "/status", "/help"}
                    ):
                        await handle(text)
                    else:
                        pending.append(text)
                input_task = asyncio.create_task(reader.readline())
                if interactive:
                    print("你> ", end="", flush=True)
    finally:
        tasks = [task for task in [command_task, input_task] if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await chat.pause()
        transport.close()
    return {"chat": "closed", "discussion_saved": True}
