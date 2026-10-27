"""聊天入口的独立进程验证，确保讨论、计划与执行边界清晰。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_bridge import cli, prd, runs


def chat(project, lines, *, with_prd=True):
    command = [sys.executable, "-m", "gpt_ds_bridge", "--workspace", str(project), "chat"]
    if with_prd:
        command += ["--prd", str(prd(project))]
    result = subprocess.run(
        command,
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def test_discussion_never_delivers_and_restores_history(project):
    chat(project, ["返回格式增加 trace_id", "/wait", "/exit"])
    assert not (project / "delivery.jsonl").exists()
    chat(project, ["是否需要分页？", "/wait", "/exit"], with_prd=False)
    requests = [json.loads(s) for s in (project / "gpt-requests.jsonl").read_text().splitlines()]
    assert "返回格式增加 trace_id" in requests[-1]["prompt"]
    assert "建议先确认接口返回格式" in requests[-1]["prompt"]


def test_chat_plan_execute_and_followup_share_ds_session(project):
    chat(
        project,
        [
            "接口保持兼容",
            "/wait",
            "/plan",
            "/wait",
            "/execute",
            "/wait",
            "增加参数校验",
            "/wait",
            "/plan",
            "/wait",
            "/execute",
            "/wait",
            "/exit",
        ],
    )
    deliveries = [json.loads(s) for s in (project / "delivery.jsonl").read_text().splitlines()]
    assert len(deliveries) == 2
    assert deliveries[1]["resumed"] is True
    assert "增加参数校验" in deliveries[1]["prompt"]
    assert all(r["status"] == "completed" for r in runs(project))


def test_discussion_keeps_current_plan_usable(project):
    chat(project, ["/plan", "/wait", "改为异步实现", "/wait", "/execute", "/wait", "/exit"])
    assert runs(project)[0]["status"] == "completed"
    delivery = json.loads((project / "delivery.jsonl").read_text().splitlines()[0])
    assert "改为异步实现" not in delivery["prompt"]


def test_current_plan_survives_discussion_and_chat_restart(project):
    chat(project, ["/plan", "/wait", "改为异步实现", "/wait", "/exit"])
    chat(project, ["/execute", "/wait", "/exit"], with_prd=False)
    assert runs(project)[0]["status"] == "completed"


def test_chat_replans_draft_after_discussion(project):
    chat(
        project,
        ["/plan", "/wait", "增加参数校验", "/wait", "/plan", "/wait", "/execute", "/wait", "/exit"],
    )
    assert runs(project)[0]["status"] == "completed"
    assert runs(project)[1]["status"] == "abandoned"


@pytest.mark.parametrize("waiting", [False, True])
def test_pause_remains_responsive_and_records_interruption(project, waiting):
    path = prd(project, "SLOW_DS")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gpt_ds_bridge",
            "--workspace",
            str(project),
            "chat",
            "--prd",
            str(path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        process.stdin.write("/plan\n/wait\n/execute\n" + ("/wait\n" if waiting else ""))
        process.stdin.flush()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if (project / "delivery.jsonl").exists():
                break
            time.sleep(0.05)
        assert (project / "delivery.jsonl").exists()
        process.stdin.write("/status\n/pause\n/exit\n")
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stdout + stderr
        assert "interrupted" in stdout
        assert "executing" in stdout
        assert runs(project)[0]["status"] == "interrupted"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_revise_requires_checked_flag(project):
    output = chat(project, ["/revise", "/exit"])
    assert "--checked" in output


def test_plan_rejects_chat_without_prd(project):
    output = chat(project, ["/plan", "/exit"], with_prd=False)
    assert "PRD" in output


def start_running_chat(project, text):
    path = prd(project, text)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gpt_ds_bridge",
            "--workspace",
            str(project),
            "chat",
            "--prd",
            str(path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    process.stdin.write("/plan\n/wait\n/execute\n")
    process.stdin.flush()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        path = project / "delivery.jsonl"
        if path.exists() and path.read_text().strip():
            return process
        time.sleep(0.05)
    process.terminate()
    process.communicate(timeout=8)
    raise AssertionError("DS 没有启动")


def cleanup(process):
    if process.poll() is None:
        process.terminate()
        process.communicate(timeout=8)


@pytest.mark.parametrize("replan_command", ["/plan", "/revise --checked"])
def test_pause_revise_and_execute_continues_original_session(project, replan_command):
    process = start_running_chat(project, "SLOW_ONCE")
    try:
        output, errors = process.communicate(
            f"/pause\n我已检查变更，增加参数校验\n/wait\n{replan_command}\n/wait\n"
            "/execute\n/wait\n/exit\n",
            timeout=15,
        )
        assert process.returncode == 0, output + errors
        deliveries = [json.loads(s) for s in (project / "delivery.jsonl").read_text().splitlines()]
        assert len(deliveries) == 2
        assert deliveries[1]["resumed"] is True
        assert deliveries[0]["session"] == deliveries[1]["session"]
        assert runs(project)[0]["status"] == "completed"
        assert runs(project)[1]["status"] == "abandoned"
    finally:
        cleanup(process)


def test_can_discuss_with_gpt_while_ds_is_running(project):
    process = start_running_chat(project, "SLOW_DS")
    try:
        process.stdin.write("我想了解测试覆盖范围，先不要改变当前任务\n")
        process.stdin.flush()
        deadline = time.monotonic() + 8
        requests = []
        while time.monotonic() < deadline:
            requests = [
                json.loads(s) for s in (project / "gpt-requests.jsonl").read_text().splitlines()
            ]
            if any(not r["planning"] for r in requests):
                break
            time.sleep(0.05)
        assert any(not r["planning"] for r in requests)
        assert runs(project)[0]["status"] == "executing"
        output, errors = process.communicate("/pause\n/exit\n", timeout=8)
        assert process.returncode == 0, output + errors
        assert len((project / "delivery.jsonl").read_text().splitlines()) == 1
    finally:
        cleanup(process)


def test_chat_sigterm_stops_ds_and_saves_state(project):
    process = start_running_chat(project, "SLOW_DS")
    try:
        pid = json.loads((project / "delivery.jsonl").read_text().splitlines()[0])["pid"]
        process.terminate()
        process.communicate(timeout=8)
        assert process.returncode == 130
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert runs(project)[0]["status"] == "interrupted"
    finally:
        cleanup(process)


def test_chat_supports_real_terminal_input(project):
    import pty

    primary, secondary = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "gpt_ds_bridge", "--workspace", str(project), "chat"],
        stdin=secondary,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    os.close(secondary)
    try:
        os.write(primary, b"/help\n/exit\n")
        output, errors = process.communicate(timeout=8)
        assert process.returncode == 0, output + errors
        assert "你>" in output
        assert "/revise --checked" in output
    finally:
        os.close(primary)
        cleanup(process)


def test_discussion_can_keep_using_the_current_cli_plan(project):
    cli(project, "plan", "--prd", str(prd(project)))
    output = chat(
        project, ["我修改了验收标准", "/wait", "/execute", "/wait", "/exit"], with_prd=False
    )
    assert "DS 已结束" in output
    assert runs(project)[0]["status"] == "completed"


def test_cli_execute_keeps_the_current_chat_plan(project):
    chat(project, ["/plan", "/wait", "调整返回结构", "/wait", "/exit"])
    cli(project, "execute")
    assert runs(project)[0]["status"] == "completed"


def test_chat_can_replan_an_existing_cli_project_without_extra_discussion(project):
    cli(project, "run", "--prd", str(prd(project)))
    output = chat(project, ["/plan", "/wait", "/execute", "/wait", "/exit"], with_prd=False)
    assert "DS 已结束" in output
    assert runs(project)[0]["status"] == "completed"


@pytest.mark.parametrize("restart", [False, True])
def test_pause_resume_keeps_plan_and_skips_gpt_replanning(project, restart):
    process = start_running_chat(project, "SLOW_ONCE")
    try:
        tail = "/pause\n只是问个问题，不改变当前计划\n/wait\n"
        if restart:
            output, errors = process.communicate(tail + "/exit\n", timeout=10)
            assert process.returncode == 0, output + errors
            chat(project, ["/resume", "/wait", "/exit"], with_prd=False)
        else:
            output, errors = process.communicate(tail + "/execute\n/wait\n/exit\n", timeout=10)
            assert process.returncode == 0, output + errors
        assert len(runs(project)) == 1
        assert runs(project)[0]["status"] == "completed"
        deliveries = [json.loads(s) for s in (project / "delivery.jsonl").read_text().splitlines()]
        assert len(deliveries) == 2
        assert deliveries[1]["resumed"] is True
        assert "只是问个问题" not in deliveries[1]["prompt"]
        assert "先检查当前代码" in deliveries[1]["prompt"]
        requests = [
            json.loads(s) for s in (project / "gpt-requests.jsonl").read_text().splitlines()
        ]
        assert sum(r["planning"] for r in requests) == 1
    finally:
        cleanup(process)


@pytest.mark.parametrize("marker", ["FAIL_PLANNER", "BLOCK_PLAN"])
def test_unsuccessful_replanning_keeps_previous_plan(project, marker):
    chat(
        project, ["/plan", "/wait", marker, "/wait", "/plan", "/wait", "/execute", "/wait", "/exit"]
    )
    deliveries = [json.loads(s) for s in (project / "delivery.jsonl").read_text().splitlines()]
    assert len(deliveries) == 1
    assert marker not in deliveries[0]["prompt"]
    assert runs(project)[1]["status"] == "completed"


def test_new_ready_plan_replaces_previous_plan(project):
    chat(
        project,
        ["/plan", "/wait", "增加参数校验", "/wait", "/plan", "/wait", "/execute", "/wait", "/exit"],
    )
    delivery = json.loads((project / "delivery.jsonl").read_text().splitlines()[0])
    assert "增加参数校验" in delivery["prompt"]
    assert runs(project)[1]["status"] == "abandoned"
    old = runs(project)[1]
    cli(project, "execute", old["id"], ok=False)
    assert len((project / "delivery.jsonl").read_text().splitlines()) == 1


def test_cancelled_replanning_keeps_the_current_plan(project):
    cli(project, "plan", "--prd", str(prd(project)))
    process = subprocess.Popen(
        [sys.executable, "-m", "gpt_ds_bridge", "--workspace", str(project), "chat"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        process.stdin.write("SLOW_PLAN\n/wait\n/plan\n")
        process.stdin.flush()
        deadline = time.monotonic() + 8
        requests = []
        while time.monotonic() < deadline:
            requests = [
                json.loads(s) for s in (project / "gpt-requests.jsonl").read_text().splitlines()
            ]
            if sum(r["planning"] for r in requests) == 2:
                break
            time.sleep(0.05)
        assert sum(r["planning"] for r in requests) == 2
        output, errors = process.communicate("/pause\n/execute\n/wait\n/exit\n", timeout=10)
        assert process.returncode == 0, output + errors
        assert runs(project)[0]["status"] == "interrupted"
        assert runs(project)[1]["status"] == "completed"
        assert len((project / "delivery.jsonl").read_text().splitlines()) == 1
    finally:
        cleanup(process)
