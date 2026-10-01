"""Pydantic 模式与录入校验。"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

_SLOT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
_STACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")


def _require_nonempty_str(v: str, label: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{label}不能为空")
    return v.strip()


# ---------- 录入：槽位资料 ----------


class SlotIn(BaseModel):
    code: str = Field(..., description="槽位编号，如 S0101")
    stack: str = Field(..., description="堆栈编号")
    tier: int = Field(..., ge=1, description="层编号，自底向上从 1 开始")
    capacity: float = Field(..., description="槽位承重（重量单位）")
    arm: float = Field(..., description="纵向力臂")

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        v = _require_nonempty_str(v, "槽位编号")
        if not _SLOT_CODE_RE.match(v):
            raise ValueError("槽位编号只能含字母、数字、下划线或连字符，且以字母/数字开头")
        return v

    @field_validator("stack")
    @classmethod
    def _stack(cls, v: str) -> str:
        v = _require_nonempty_str(v, "堆栈编号")
        if not _STACK_RE.match(v):
            raise ValueError("堆栈编号只能含字母、数字、下划线或连字符，且以字母/数字开头")
        return v

    @field_validator("capacity")
    @classmethod
    def _capacity(cls, v: float) -> float:
        if v < 0:
            raise ValueError("槽位承重不得为负数（非法承重）")
        return v


class SlotDataIn(BaseModel):
    slots: list[SlotIn] = Field(..., min_length=1)
    adjacency: list[list[str]] = Field(
        default_factory=list, description="相邻槽位编号对（无向），如 [['S0101','S0201']]"
    )
    incompatible_classes: list[list[str]] = Field(
        default_factory=list, description="不相容危险类别对，如 [['1','5']]"
    )
    min_moment: float = Field(..., description="全船允许总力矩下限")
    max_moment: float = Field(..., description="全船允许总力矩上限")

    @model_validator(mode="after")
    def _validate(self) -> "SlotDataIn":
        if self.min_moment > self.max_moment:
            raise ValueError("允许力矩区间下限不得大于上限")

        codes = [s.code for s in self.slots]
        if len(set(codes)) != len(codes):
            dup = sorted({c for c in codes if codes.count(c) > 1})
            raise ValueError(f"槽位编号重复：{', '.join(dup)}")
        code_set = set(codes)

        for pair in self.adjacency:
            if len(pair) != 2:
                raise ValueError("相邻关系必须是两个槽位编号组成的对")
            a, b = pair
            if a == b:
                raise ValueError(f"槽位 {a} 不能与自身相邻")
            missing = [c for c in (a, b) if c not in code_set]
            if missing:
                raise ValueError(
                    f"相邻关系引用了不存在的槽位 {missing[0]}（无效槽位）"
                )

        norm_adj = {tuple(sorted(p)) for p in self.adjacency}
        if len(norm_adj) != len(self.adjacency):
            raise ValueError("相邻关系存在重复对")

        for pair in self.incompatible_classes:
            if len(pair) != 2:
                raise ValueError("不相容危险类别必须是两个类别组成的对")
            a, b = (p.strip() for p in pair)
            if not a or not b:
                raise ValueError("危险类别不能为空")
        norm_inc = {tuple(sorted((p[0].strip(), p[1].strip()))) for p in self.incompatible_classes}
        if len(norm_inc) != len(self.incompatible_classes):
            raise ValueError("不相容危险类别对存在重复")
        return self


# ---------- 录入：箱清单 ----------


class ContainerIn(BaseModel):
    no: str = Field(..., description="箱号")
    weight: float = Field(..., description="重量（重量单位）")
    hazmat: str | None = Field(None, description="危险类别，普通箱为 null")
    discharge_order: int = Field(..., ge=1, description="卸港先后序号，小者先卸")

    @field_validator("no")
    @classmethod
    def _no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")

    @field_validator("weight")
    @classmethod
    def _weight(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("箱重量必须大于 0（非法重量）")
        return v

    @field_validator("hazmat")
    @classmethod
    def _hazmat(cls, v):
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError("危险类别为空字符串时请使用 null")
        return v.strip()


class ManifestIn(BaseModel):
    containers: list[ContainerIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> "ManifestIn":
        nos = [c.no for c in self.containers]
        if len(set(nos)) != len(nos):
            dup = sorted({n for n in nos if nos.count(n) > 1})
            raise ValueError(f"箱号重复：{', '.join(dup)}")
        return self


# ---------- 方案 ----------


class PlanComputeIn(BaseModel):
    slots_revision: int | None = Field(None, description="槽位资料修订号；缺省取最新")
    manifest_revision: int | None = Field(None, description="箱清单修订号；缺省取最新")


class PlanAmendIn(BaseModel):
    slots_revision: int | None = Field(None)
    manifest_revision: int | None = Field(None)


class PlanConfirmIn(BaseModel):
    slots_revision: int = Field(..., description="确认时核对的槽位资料修订号")
    manifest_revision: int = Field(..., description="确认时核对的箱清单修订号")


class AssignmentOut(BaseModel):
    container_no: str
    slot_code: str
    stack: str
    tier: int


class PlanOut(BaseModel):
    plan_id: str
    revision_no: int
    status: str
    slots_revision: int
    manifest_revision: int
    total_moment: float
    max_occupied_tier: int
    assignments: list[AssignmentOut]
    created_at: str
    confirmed_at: str | None = None
    parent_id: str | None = None
    root_id: str


class SlotRevisionOut(BaseModel):
    slots_revision: int
    slot_count: int
    min_moment: float
    max_moment: float
    created_at: str


class ManifestRevisionOut(BaseModel):
    manifest_revision: int
    container_count: int
    created_at: str


class MessageOut(BaseModel):
    detail: str


# ---------- 装船排程 ----------


class ScheduleTaskIn(BaseModel):
    no: str = Field(..., description="箱号，必须与方案中的箱项完全一致")
    duration: int = Field(..., ge=1, description="每箱吊装时长（正整数，分钟）")
    earliest_start: int = Field(..., ge=0, description="最早开工分钟（非负整数）")
    latest_finish: int = Field(..., ge=0, description="最迟完工分钟（非负整数，允许恰在该时刻结束）")

    @field_validator("no")
    @classmethod
    def _no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")

    @model_validator(mode="after")
    def _validate(self) -> "ScheduleTaskIn":
        if self.earliest_start + self.duration > self.latest_finish:
            raise ValueError(
                f"箱 {self.no} 时间窗无解：最早开工 {self.earliest_start} + 吊装时长 "
                f"{self.duration} > 最迟完工 {self.latest_finish}"
            )
        return self


class ScheduleCreateIn(BaseModel):
    cranes: int = Field(..., ge=1, description="岸桥数量（正整数）")
    tasks: list[ScheduleTaskIn] = Field(
        ..., description="每箱吊装时长与时间窗；箱项必须与方案完全一致（不缺不多）"
    )

    @model_validator(mode="after")
    def _validate(self) -> "ScheduleCreateIn":
        nos = [t.no for t in self.tasks]
        if len(set(nos)) != len(nos):
            dup = sorted({n for n in nos if nos.count(n) > 1})
            raise ValueError(f"排程箱号重复：{', '.join(dup)}")
        return self


class ScheduleItemOut(BaseModel):
    container_no: str
    stack: str
    tier: int
    start: int
    finish: int
    crane: int


class ScheduleOut(BaseModel):
    schedule_id: str
    plan_id: str
    status: str
    cranes: int
    makespan: int
    root_id: str
    plan_revision_no: int
    slots_revision: int
    manifest_revision: int
    tasks: list[ScheduleItemOut]
    created_at: str
