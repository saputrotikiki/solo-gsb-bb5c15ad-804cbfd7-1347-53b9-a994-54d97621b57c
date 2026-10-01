"""ORM 模型：槽位资料修订、箱清单修订、方案修订与已确认占用。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


from app.database import Base  # noqa: E402


class SlotRevision(Base):
    __tablename__ = "slot_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    min_moment: Mapped[float] = mapped_column(Float, nullable=False)
    max_moment: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    slots: Mapped[list["SlotRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )
    adjacencies: Mapped[list["AdjacencyRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )
    incompatibilities: Mapped[list["IncompatRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("min_moment <= max_moment", name="ck_moment_range"),
    )


class SlotRow(Base):
    __tablename__ = "slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("slot_revisions.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    capacity: Mapped[float] = mapped_column(Float, nullable=False)
    arm: Mapped[float] = mapped_column(Float, nullable=False)

    rev: Mapped[SlotRevision] = relationship(back_populates="slots")

    __table_args__ = (
        UniqueConstraint("revision_id", "code", name="uq_slot_code_in_rev"),
    )


class AdjacencyRow(Base):
    __tablename__ = "adjacencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("slot_revisions.id", ondelete="CASCADE"), index=True
    )
    slot_a: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_b: Mapped[str] = mapped_column(String(64), nullable=False)

    rev: Mapped[SlotRevision] = relationship(back_populates="adjacencies")

    __table_args__ = (
        UniqueConstraint("revision_id", "slot_a", "slot_b", name="uq_adj_in_rev"),
    )


class IncompatRow(Base):
    __tablename__ = "incompatibilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("slot_revisions.id", ondelete="CASCADE"), index=True
    )
    class_a: Mapped[str] = mapped_column(String(64), nullable=False)
    class_b: Mapped[str] = mapped_column(String(64), nullable=False)

    rev: Mapped[SlotRevision] = relationship(back_populates="incompatibilities")

    __table_args__ = (
        UniqueConstraint("revision_id", "class_a", "class_b", name="uq_incompat_in_rev"),
    )


class ManifestRevision(Base):
    __tablename__ = "manifest_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    containers: Mapped[list["ContainerRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )


class ContainerRow(Base):
    __tablename__ = "containers"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("manifest_revisions.id", ondelete="CASCADE"), index=True
    )
    no: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    hazmat: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)

    rev: Mapped[ManifestRevision] = relationship(back_populates="containers")

    __table_args__ = (
        UniqueConstraint("revision_id", "no", name="uq_container_no_in_rev"),
        CheckConstraint("weight >= 0", name="ck_container_weight"),
    )


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    # 当前修订所基于的资料
    slots_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    total_moment: Mapped[float] = mapped_column(Float, nullable=False)
    max_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 修订链
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("plans.id", ondelete="SET NULL"), nullable=True
    )
    root_id: Mapped[str] = mapped_column(String(64), nullable=False)

    items: Mapped[list["PlanItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_plans_root", "root_id"),
        Index("ix_plans_status", "status"),
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'superseded')",
            name="ck_plan_status",
        ),
    )


class PlanItem(Base):
    __tablename__ = "plan_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)

    plan: Mapped[Plan] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("plan_id", "container_no", name="uq_item_container_in_plan"),
    )


class ConfirmedSlot(Base):
    """已确认方案对槽位的占用。

    唯一索引保证：并发确认时同一槽位不可能被两个已确认方案占用。
    """

    __tablename__ = "confirmed_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slot_code: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("slot_code", name="uq_confirmed_slot"),
    )


class Schedule(Base):
    """装船排程，绑定某个具体方案修订（plan_id）。

    status: current=当前排程；superseded=历史排程（被同方案修订的新排程
    取代，或所绑定方案修订被新修订取代）。
    """

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    cranes: Mapped[int] = mapped_column(Integer, nullable=False)
    makespan: Mapped[int] = mapped_column(Integer, nullable=False)
    # 绑定快照：方案修订链与资料修订，便于排程脱离方案独立展示
    root_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    slots_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    items: Mapped[list["ScheduleItem"]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_schedules_plan", "plan_id"),
        Index("ix_schedules_root", "root_id"),
        Index("ix_schedules_status", "status"),
        CheckConstraint(
            "status IN ('current', 'superseded')", name="ck_schedule_status"
        ),
        CheckConstraint("cranes > 0", name="ck_schedule_cranes_pos"),
    )


class ScheduleItem(Base):
    """排程中的单箱作业：开工/完工分钟与岸桥编号。"""

    __tablename__ = "schedule_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[str] = mapped_column(
        ForeignKey("schedules.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    start_min: Mapped[int] = mapped_column(Integer, nullable=False)
    finish_min: Mapped[int] = mapped_column(Integer, nullable=False)
    crane_no: Mapped[int] = mapped_column(Integer, nullable=False)

    schedule: Mapped[Schedule] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("schedule_id", "container_no", name="uq_sched_container"),
    )
