"""计划的结构化协议：同时用于 GPT 输出约束和本地二次校验。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    test_commands: list[str]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    ready: bool
    assumptions: list[str]
    constraints: list[str]
    questions: list[str]
    tasks: list[Task]

    @model_validator(mode="after")
    def check_readiness(self):
        """有未解决问题时不得自动启动执行器，避免把猜测变成代码变更。"""
        if self.ready and (self.questions or not self.tasks):
            raise ValueError("ready=true 时必须有任务，且不能有待澄清问题")
        if not self.ready and not self.questions:
            raise ValueError("ready=false 时必须说明待澄清问题")
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("任务 ID 不能重复")
        return self

    def markdown(self) -> str:
        """输出便于人工阅读的计划；执行时始终以经过校验的 JSON 为准。"""
        lines = [f"# {self.title}", "", self.summary, "", f"可执行：{self.ready}"]
        for title, values in [
            ("假设", self.assumptions),
            ("约束", self.constraints),
            ("待澄清问题", self.questions),
        ]:
            lines += ["", f"## {title}", ""] + [f"- {value}" for value in values]
        for task in self.tasks:
            lines += ["", f"## {task.id} {task.title}", "", task.instructions, "", "验收标准：", ""]
            lines += [f"- {value}" for value in task.acceptance_criteria]
            lines += ["", "建议验证命令（由 DS 判断适用性后运行）：", ""]
            lines += [f"- `{value}`" for value in task.test_commands]
        return "\n".join(lines) + "\n"
