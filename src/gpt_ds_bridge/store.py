"""SQLite 状态及工作区互斥锁。锁随进程退出释放，不依赖猜测 PID 是否存活。"""

import fcntl
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .config import BridgeError


def now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def workspace_lock(root: Path, timeout: float):
    """等待已有编排进程完成；同工作区只能有一个写入者。"""
    with (root / "workspace.lock").open("a+") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BridgeError("工作区锁被占用，已有任务在运行；请等待后重试") from None
                time.sleep(0.1)
        try:
            # 子进程继承同一个锁描述符：编排器被 SIGKILL 时，DS 若还活着就继续持锁。
            yield handle.fileno()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, root: Path):
        self.db = sqlite3.connect(root / "state.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
                content TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, artifact_dir TEXT NOT NULL, session_id TEXT,
                error TEXT, parent_id TEXT, phase TEXT NOT NULL DEFAULT 'planning'
            );
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def add_message(self, role: str, content: str):
        """GPT 讨论单独持久化，不与 DS 的原生会话日志混用。"""
        with self.db:
            self.db.execute(
                "INSERT INTO chat_messages (role,content,created_at) VALUES (?,?,?)",
                (role, content, now()),
            )

    def messages(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM chat_messages ORDER BY id")]

    def metadata(self, key: str):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_metadata(self, key: str, value):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def create(self, run_id: str, artifact_dir: Path, parent_id: str | None = None):
        stamp = now()
        with self.db:
            self.db.execute(
                "INSERT INTO runs (id,status,created_at,updated_at,artifact_dir,parent_id) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, "planning", stamp, stamp, str(artifact_dir), parent_id),
            )

    def update(self, run_id: str, **fields):
        allowed = {"status", "session_id", "error", "phase"}
        if not fields.keys() <= allowed:
            raise ValueError("无效状态字段")
        fields["updated_at"] = now()
        with self.db:
            self.db.execute(
                f"UPDATE runs SET {','.join(key + '=?' for key in fields)} WHERE id=?",
                (*fields.values(), run_id),
            )

    def get(self, run_id: str | None = None):
        if run_id:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        else:
            row = self.db.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        if row is None:
            raise BridgeError("没有找到任务记录")
        return dict(row)

    def history(self, limit: int = 20):
        return [
            dict(row)
            for row in self.db.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,))
        ]

    def current_plan(self):
        """最近一次成功生成且尚未被放弃的计划；失败的规划尝试不覆盖它。"""
        row = self.db.execute(
            "SELECT * FROM runs WHERE status IN ('planned','executing','completed') "
            "OR (phase='executing' AND status IN ('failed','interrupted')) "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise BridgeError("尚无可执行计划，请先 /plan")
        return dict(row)

    def activate_plan(self, run_id: str, previous_id: str | None):
        """新计划通过校验后才原子替换旧计划，避免生成失败导致原计划丢失。"""
        stamp = now()
        with self.db:
            if previous_id:
                self.db.execute(
                    "UPDATE runs SET status='abandoned',updated_at=? WHERE id=? "
                    "AND status IN ('planned','failed','interrupted')",
                    (stamp, previous_id),
                )
            self.db.execute(
                "UPDATE runs SET status='planned',updated_at=? WHERE id=?", (stamp, run_id)
            )

    def recover_abandoned(self):
        """只能持有工作区锁时调用：此时仍处于运行态的记录来自已中断的旧进程。"""
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='interrupted', updated_at=?, error=? "
                "WHERE status IN ('planning','executing')",
                (now(), "上次编排进程未正常结束；请检查日志和代码变更后显式恢复"),
            )
