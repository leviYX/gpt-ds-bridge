"""读取工作区配置，并发现本机已安装的 CLI，不自动联网下载 Harness。"""

import json
import os
import shutil
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BridgeError(Exception):
    """可直接展示给用户的错误，不在普通 CLI 输出中打印调用栈。"""


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codex_command: list[str] = Field(min_length=1)
    dsh_command: list[str] = Field(min_length=1)
    dsh_home: str
    codex_model: str = ""
    dsh_patches: list[str] = Field(default_factory=list)
    planner_timeout: float = Field(default=1200, gt=0)
    executor_timeout: float = Field(default=3600, gt=0)
    lock_timeout: float = Field(default=30, ge=0)

    @field_validator("codex_command", "dsh_command")
    @classmethod
    def nonempty_arguments(cls, value):
        if any(not arg.strip() for arg in value):
            raise ValueError("命令参数不能是空字符串")
        return value

    @field_validator("dsh_home")
    @classmethod
    def absolute_home(cls, value):
        return str(Path(value).expanduser().resolve())


def state_dir(workspace: Path) -> Path:
    return workspace / ".gpt-ds-bridge"


def discover_dsh() -> list[str]:
    """优先 PATH，其次发现 npm 缓存中的本地安装；始终使用参数数组启动。"""
    if executable := shutil.which("dsh"):
        return [executable]
    node = shutil.which("node")
    if node:
        candidates = list(Path.home().glob(".npm/_npx/*/node_modules/@deepseek-ai/dsh/lib/bin.js"))
        if candidates:
            candidate = max(candidates, key=lambda p: p.stat().st_mtime)
            return [node, str(candidate)]
    return ["dsh"]


def initialize(workspace: Path) -> Path:
    root = state_dir(workspace)
    root.mkdir(mode=0o700, exist_ok=True)
    target = root / "config.toml"
    if target.exists():
        raise BridgeError(f"配置已经存在，不覆盖：{target}")
    values = {
        "codex_command": [shutil.which("codex") or "codex"],
        "dsh_command": discover_dsh(),
        "dsh_home": str(Path(os.environ.get("DSH_HOME", "~/.dsh")).expanduser().resolve()),
        "codex_model": "",
        "dsh_patches": [],
        "planner_timeout": 1200,
        "executor_timeout": 3600,
        "lock_timeout": 30,
    }
    # JSON 的字符串/数组写法也是合法 TOML，避免手工转义路径中的空格和引号。
    text = "# 命令是参数数组，不经过 shell；不要在此保存 API Key。\n"
    text += "\n".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items()
    )
    with target.open("x", encoding="utf-8") as handle:
        handle.write(text + "\n")
    target.chmod(0o600)
    (root / ".gitignore").write_text("*\n", encoding="utf-8")
    return target


def load_config(workspace: Path) -> Config:
    path = state_dir(workspace) / "config.toml"
    if not path.is_file():
        raise BridgeError("工作区尚未初始化，请先运行 init")
    with path.open("rb") as handle:
        config = Config.model_validate(tomllib.load(handle))
    # 补丁路径相对于工作区，确保从别的目录调用也使用同一份配置。
    config.dsh_patches = [
        str((workspace / Path(p).expanduser()).resolve()) for p in config.dsh_patches
    ]
    return config
