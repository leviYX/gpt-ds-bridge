"""通过真实 CLI 子进程验证交接、持久化及异常恢复。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path):
    """所有测试仅使用临时目录和模拟程序。"""
    workspace = tmp_path / "repo with spaces"
    workspace.mkdir()
    command = [sys.executable, str(Path(__file__).with_name("fake_tool.py"))]
    state = workspace / ".gpt-ds-bridge"
    state.mkdir()
    (state / "config.toml").write_text(
        f"codex_command = {json.dumps(command + ['codex'])}\n"
        f"dsh_command = {json.dumps(command + ['dsh'])}\n"
        f"dsh_home = {json.dumps(str(tmp_path / 'dsh-home'))}\n"
        "planner_timeout = 20\nexecutor_timeout = 20\nlock_timeout = 1\n",
        encoding="utf-8",
    )
    return workspace


def cli(project, *args, ok=True):
    result = subprocess.run(
        [sys.executable, "-m", "gpt_ds_bridge", "--workspace", str(project), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    if ok:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, result.stdout + result.stderr
    return result


def prd(project, text="实现一个示例接口"):
    path = project / "prd.md"
    path.write_text(text, encoding="utf-8")
    return path


def runs(project):
    return json.loads(cli(project, "history", "--json").stdout)


def test_run_and_followup_resume_same_session(project):
    cli(project, "run", "--prd", str(prd(project)))
    cli(project, "followup", "--message", "补充错误处理")
    deliveries = [json.loads(s) for s in (project / "delivery.jsonl").read_text().splitlines()]
    assert len(deliveries) == 2
    assert deliveries[0]["resumed"] is False
    assert deliveries[1]["resumed"] is True
    assert deliveries[0]["session"] == deliveries[1]["session"]
    assert "补充错误处理" in deliveries[1]["prompt"]
    assert all(r["status"] == "completed" for r in runs(project))
    assert all(Path(r["artifact_dir"]).joinpath("report.md").is_file() for r in runs(project))


def test_blocked_plan_never_starts_ds(project):
    cli(project, "run", "--prd", str(prd(project, "BLOCK_PLAN")), ok=False)
    assert runs(project)[0]["status"] == "blocked"
    assert not (project / "delivery.jsonl").exists()


def test_plan_then_execute_and_no_duplicate_delivery(project):
    cli(project, "plan", "--prd", str(prd(project)))
    assert runs(project)[0]["status"] == "planned"
    assert not (project / "delivery.jsonl").exists()
    cli(project, "execute")
    cli(project, "execute", ok=False)
    assert len((project / "delivery.jsonl").read_text().splitlines()) == 1


def test_failure_keeps_session_and_requires_explicit_retry(project):
    cli(project, "run", "--prd", str(prd(project, "FAIL_DS")), ok=False)
    failed = runs(project)[0]
    assert failed["status"] == "failed"
    assert failed["session_id"] == "session-test-001"
    cli(project, "followup", "--message", "继续", ok=False)
    cli(project, "retry", failed["id"], ok=False)
    assert len((project / "delivery.jsonl").read_text().splitlines()) == 1


def test_malformed_output_is_not_success(project):
    cli(project, "run", "--prd", str(prd(project, "BAD_PROTOCOL")), ok=False)
    assert runs(project)[0]["status"] == "failed"


def test_session_mismatch_rejected(project):
    cli(project, "run", "--prd", str(prd(project, "WRONG_SESSION")), ok=False)
    assert runs(project)[0]["status"] == "failed"


def test_timeout_kills_process_and_records_interruption(project):
    cfg = project / ".gpt-ds-bridge/config.toml"
    cfg.write_text(cfg.read_text().replace("executor_timeout = 20", "executor_timeout = 0.3"))
    cli(project, "run", "--prd", str(prd(project, "SLOW_DS")), ok=False)
    assert runs(project)[0]["status"] == "interrupted"


def test_changed_home_cannot_silently_lose_context(project):
    cli(project, "run", "--prd", str(prd(project)))
    cfg = project / ".gpt-ds-bridge/config.toml"
    cfg.write_text(cfg.read_text().replace("dsh-home", "different-home"))
    cli(project, "followup", "--message", "继续实现", ok=False)
    assert len((project / "delivery.jsonl").read_text().splitlines()) == 1


def test_workspace_lock_prevents_parallel_execution(project):
    import fcntl

    with (project / ".gpt-ds-bridge/workspace.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = cli(project, "run", "--prd", str(prd(project)), ok=False)
        assert "锁" in result.stderr
    assert not (project / "delivery.jsonl").exists()


@pytest.mark.parametrize("marker", ["INVALID_PLAN", "FAIL_PLANNER"])
def test_invalid_or_failed_planning_never_delivers(project, marker):
    cli(project, "run", "--prd", str(prd(project, marker)), ok=False)
    assert runs(project)[0]["status"] == "failed"
    assert not (project / "delivery.jsonl").exists()


@pytest.mark.parametrize("marker", ["MISSING_FINAL", "ERROR_WITH_ZERO_EXIT"])
def test_zero_exit_alone_does_not_mean_success(project, marker):
    cli(project, "run", "--prd", str(prd(project, marker)), ok=False)
    assert runs(project)[0]["status"] == "failed"


def test_explicit_retry_reuses_session_and_keeps_attempt_logs(project):
    cli(project, "run", "--prd", str(prd(project, "FAIL_ONCE")), ok=False)
    failed = runs(project)[0]
    cli(project, "retry", failed["id"], "--acknowledge-side-effects")
    assert runs(project)[0]["status"] == "completed"
    deliveries = [json.loads(s) for s in (project / "delivery.jsonl").read_text().splitlines()]
    assert deliveries[-1]["resumed"] is True
    assert "先检查当前代码" in deliveries[-1]["prompt"]
    attempts = Path(failed["artifact_dir"]) / "attempts"
    assert (attempts / "001/dsh.stdout.jsonl").is_file()
    assert (attempts / "002/dsh.stdout.jsonl").is_file()


def test_reset_after_plan_requires_replanning(project):
    cli(project, "plan", "--prd", str(prd(project)))
    cli(project, "session", "set", "manually-adopted")
    cli(project, "execute", ok=False)
    assert not (project / "delivery.jsonl").exists()


def test_followup_accumulates_requirements(project):
    cli(project, "run", "--prd", str(prd(project)))
    cli(project, "followup", "--message", "补充要求一")
    cli(project, "followup", "--message", "补充要求二")
    latest = json.loads((project / "delivery.jsonl").read_text().splitlines()[-1])
    assert "补充要求一" in latest["prompt"]
    assert "补充要求二" in latest["prompt"]


def start_slow(project):
    path = prd(project, "SLOW_DS")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gpt_ds_bridge",
            "--workspace",
            str(project),
            "run",
            "--prd",
            str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        delivery = project / "delivery.jsonl"
        if delivery.exists() and delivery.read_text().strip():
            return process, json.loads(delivery.read_text().splitlines()[0])["pid"]
        if process.poll() is not None:
            break
        time.sleep(0.05)
    process.kill()
    process.wait()
    raise AssertionError("模拟 DS 未开始执行")


def test_sigterm_cleans_up_ds_and_records_interruption(project):
    process, child_pid = start_slow(project)
    process.terminate()
    process.wait(timeout=8)
    assert process.returncode == 130
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert runs(project)[0]["status"] == "interrupted"


def test_parent_hard_kill_does_not_allow_overlapping_ds(project):
    import signal

    process, child_pid = start_slow(project)
    try:
        process.kill()
        process.wait(timeout=5)
        result = cli(project, "execute", ok=False)
        assert "锁" in result.stderr
    finally:
        # 硬杀编排进程无法运行清理逻辑；测试主动回收模拟 DS 进程组。
        os.killpg(child_pid, signal.SIGKILL)


def test_abandon_keeps_context_and_allows_new_planning(project):
    cli(project, "run", "--prd", str(prd(project, "FAIL_ONCE")), ok=False)
    failed = runs(project)[0]
    cli(project, "abandon", failed["id"])
    cli(project, "followup", "--message", "已检查，继续完善")
    assert runs(project)[0]["status"] == "completed"
    latest = json.loads((project / "delivery.jsonl").read_text().splitlines()[-1])
    assert latest["resumed"] is True


def test_retry_cannot_switch_to_another_session(project):
    cli(project, "run", "--prd", str(prd(project, "FAIL_ONCE")), ok=False)
    failed = runs(project)[0]
    cli(project, "session", "set", "another-session")
    cli(project, "retry", failed["id"], "--acknowledge-side-effects", ok=False)
    assert len((project / "delivery.jsonl").read_text().splitlines()) == 1
