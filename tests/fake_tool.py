"""以独立进程模拟两种 CLI；不访问网络或读取真实凭据。"""

import json
import os
import sys
import time
from pathlib import Path

mode, *args = sys.argv[1:]
if "--version" in args:
    print("fake 0.2.1")
    sys.exit(0)
prompt = sys.stdin.read()
if mode == "codex":
    out = Path(args[args.index("--output-last-message") + 1])
    with Path("gpt-requests.jsonl").open("a") as log:
        log.write(json.dumps({"prompt": prompt, "planning": "--output-schema" in args}) + "\n")
    if "--output-schema" not in args:
        out.write_text("建议先确认接口返回格式，再生成实施计划。", encoding="utf-8")
        print(json.dumps({"type": "turn.completed"}))
        sys.exit(0)
    if "SLOW_PLAN" in prompt:
        time.sleep(30)
    blocked = "BLOCK_PLAN" in prompt
    if "FAIL_PLANNER" in prompt:
        print("模拟规划器失败", file=sys.stderr)
        sys.exit(1)
    out.write_text(
        json.dumps(
            {
                "title": "实现示例需求",
                "summary": "按照验收标准完成修改",
                "ready": "yes" if "INVALID_PLAN" in prompt else not blocked,
                "assumptions": [],
                "constraints": ["不提交代码"],
                "questions": ["需要明确接口协议"] if blocked else [],
                "tasks": []
                if blocked
                else [
                    {
                        "id": "T1",
                        "title": "实现",
                        "instructions": "修改文件",
                        "acceptance_criteria": ["运行检查"],
                        "test_commands": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"type": "turn.completed"}))
else:
    resumed = "--session-id" in args
    session = args[args.index("--session-id") + 1] if resumed else "session-test-001"
    print(json.dumps({"type": "session", "sessionId": session, "cwd": str(Path.cwd())}), flush=True)
    # 把投递轨迹记录在隔离工作区，供测试验证会话连续性。
    with Path("delivery.jsonl").open("a") as f:
        f.write(
            json.dumps(
                {"session": session, "resumed": resumed, "prompt": prompt, "pid": os.getpid()}
            )
            + "\n"
        )
    first_delivery = len(Path("delivery.jsonl").read_text().splitlines()) == 1
    if "SLOW_DS" in prompt or ("SLOW_ONCE" in prompt and first_delivery):
        time.sleep(30)
    if "BAD_PROTOCOL" in prompt:
        print("not-json", flush=True)
        sys.exit(0)
    if "WRONG_SESSION" in prompt:
        print(json.dumps({"type": "session", "sessionId": "wrong"}), flush=True)
    failed = "FAIL_DS" in prompt or ("FAIL_ONCE" in prompt and first_delivery)
    bad_terminal = "ERROR_WITH_ZERO_EXIT" in prompt
    reason = (
        {"kind": "error", "error": {"code": "TEST", "message": "模拟失败"}}
        if failed or bad_terminal
        else {"kind": "completed"}
    )
    print(json.dumps({"type": "status", "phase": "turn_end", "reason": reason}), flush=True)
    if "MISSING_FINAL" not in prompt:
        print(json.dumps({"type": "final", "text": "执行结果：已完成示例修改"}), flush=True)
    sys.exit(1 if failed else 0)
