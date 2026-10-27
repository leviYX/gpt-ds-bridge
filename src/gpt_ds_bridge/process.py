"""无 shell 的异步进程驱动，持续落盘，异常时终止整个子进程组。"""

import asyncio
import json
import os
import signal
from collections.abc import Callable
from pathlib import Path

from .config import BridgeError


class ProcessInterrupted(BridgeError):
    pass


async def stop_group(process):
    """先温和终止，再清理仍存活的后代，避免 Harness 留在后台继续改文件。"""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except TimeoutError:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


async def run_process(
    command: list[str],
    *,
    cwd: Path,
    prompt: str,
    log_prefix: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
    lock_fd: int | None = None,
) -> int:
    """stdin 传递需求，避免长提示词进入进程列表或被 shell 展开。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=() if lock_fd is None else (lock_fd,),
            # Harness final 事件可能很大，不能使用默认 64 KiB 的 readline 限制。
            limit=16 * 1024 * 1024,
        )
    except OSError as exc:
        raise BridgeError(f"无法启动命令 {command[0]}：{exc}") from exc

    log_prefix.with_suffix(".process.json").write_text(
        json.dumps(
            {
                "pid": process.pid,
                "process_group": process.pid,
                "command": command,
                "cwd": str(cwd),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def feed():
        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def consume(stream, path, callback=None):
        with path.open("wb") as log:
            while line := await stream.readline():
                log.write(line)
                log.flush()
                if callback:
                    callback(line.decode("utf-8", errors="replace"))

    tasks = [
        asyncio.create_task(feed()),
        asyncio.create_task(
            consume(process.stdout, log_prefix.with_suffix(".stdout.jsonl"), on_line)
        ),
        asyncio.create_task(consume(process.stderr, log_prefix.with_suffix(".stderr.log"))),
    ]
    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(*tasks)
            return await process.wait()
    except TimeoutError as exc:
        await stop_group(process)
        raise ProcessInterrupted(
            f"进程超时（{timeout:g} 秒）；已终止，执行结果需要人工检查"
        ) from exc
    except BaseException:
        await stop_group(process)
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
