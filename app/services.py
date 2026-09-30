"""业务服务层。"""

from __future__ import annotations

from datetime import timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.schemas import ManifestIn, PlanConfirmIn, SlotDataIn
from app.solver import (
    Container,
    InfeasibleStowage,
    Slot,
    solve_stowage,
)


class BusinessError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ConflictError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---------- 资料修订 ----------


def create_slot_revision(db: Session, data: SlotDataIn) -> models.SlotRevision:
    next_rev = (db.scalar(select(func.max(models.SlotRevision.revision))) or 0) + 1
    rev = models.SlotRevision(
        revision=next_rev,
        min_moment=data.min_moment,
        max_moment=data.max_moment,
    )
    for s in data.slots:
        rev.slots.append(
            models.SlotRow(
                code=s.code, stack=s.stack, tier=s.tier,
                capacity=s.capacity, arm=s.arm,
            )
        )
    for a, b in {tuple(sorted(p)) for p in data.adjacency}:
        rev.adjacencies.append(models.AdjacencyRow(slot_a=a, slot_b=b))
    for a, b in {
        tuple(sorted((p[0].strip(), p[1].strip())))
        for p in data.incompatible_classes
    }:
        rev.incompatibilities.append(models.IncompatRow(class_a=a, class_b=b))
    db.add(rev)
    db.commit()
    db.refresh(rev)
    return rev


def create_manifest_revision(db: Session, data: ManifestIn) -> models.ManifestRevision:
    next_rev = (db.scalar(select(func.max(models.ManifestRevision.revision))) or 0) + 1
    rev = models.ManifestRevision(revision=next_rev)
    for c in data.containers:
        rev.containers.append(
            models.ContainerRow(
                no=c.no,
                weight=c.weight,
                hazmat=c.hazmat,
                discharge_order=c.discharge_order,
            )
        )
    db.add(rev)
    db.commit()
    db.refresh(rev)
    return rev


def get_slot_revision(db: Session, revision: int | None) -> models.SlotRevision:
    if revision is None:
        rev = db.scalar(
            select(models.SlotRevision).order_by(models.SlotRevision.revision.desc())
        )
    else:
        rev = db.scalar(
            select(models.SlotRevision).where(models.SlotRevision.revision == revision)
        )
    if rev is None:
        raise BusinessError(f"槽位资料修订 {revision} 不存在", 404)
    return rev


def get_manifest_revision(db: Session, revision: int | None) -> models.ManifestRevision:
    if revision is None:
        rev = db.scalar(
            select(models.ManifestRevision).order_by(
                models.ManifestRevision.revision.desc()
            )
        )
    else:
        rev = db.scalar(
            select(models.ManifestRevision).where(
                models.ManifestRevision.revision == revision
            )
        )
    if rev is None:
        raise BusinessError(f"箱清单修订 {revision} 不存在", 404)
    return rev


def _to_solver_inputs(slot_rev, manifest_rev):
    slots = [
        Slot(
            code=r.code, stack=r.stack, tier=r.tier,
            capacity=r.capacity, arm=r.arm,
        )
        for r in slot_rev.slots
    ]
    containers = [
        Container(
            no=r.no, weight=r.weight, hazmat=r.hazmat,
            discharge_order=r.discharge_order,
        )
        for r in manifest_rev.containers
    ]
    adjacency = [(r.slot_a, r.slot_b) for r in slot_rev.adjacencies]
    incompat = [(r.class_a, r.class_b) for r in slot_rev.incompatibilities]
    return slots, containers, adjacency, incompat


# ---------- 方案 ----------


def _save_plan(
    db: Session,
    *,
    slots_rev,
    manifest_rev,
    result,
    parent: models.Plan | None,
    status: str = "draft",
) -> models.Plan:
    new_id = models._new_id()
    plan = models.Plan(
        id=new_id,
        root_id=parent.root_id if parent else new_id,
        revision_no=(parent.revision_no + 1) if parent else 1,
        status=status,
        slots_revision=slots_rev.revision,
        manifest_revision=manifest_rev.revision,
        total_moment=result.total_moment,
        max_tier=result.max_occupied_tier,
        parent_id=parent.id if parent else None,
    )
    for a in result.assignments:
        plan.items.append(
            models.PlanItem(
                container_no=a.container_no,
                slot_code=a.slot_code,
                stack=a.stack,
                tier=a.tier,
            )
        )
    db.add(plan)
    db.flush()
    return plan


def compute_plan(
    db: Session,
    *,
    slots_revision: int | None,
    manifest_revision: int | None,
    parent_id: str | None = None,
) -> models.Plan:
    slot_rev = get_slot_revision(db, slots_revision)
    manifest_rev = get_manifest_revision(db, manifest_revision)
    parent = None
    if parent_id is not None:
        parent = db.get(models.Plan, parent_id)
        if parent is None:
            raise BusinessError(f"方案 {parent_id} 不存在", 404)

    slots, containers, adjacency, incompat = _to_solver_inputs(slot_rev, manifest_rev)
    try:
        result = solve_stowage(
            containers, slots, adjacency, incompat,
            slot_rev.min_moment, slot_rev.max_moment,
        )
    except InfeasibleStowage as exc:
        raise BusinessError(f"无解：{exc.reason}", 422)

    plan = _save_plan(
        db, slots_rev=slot_rev, manifest_rev=manifest_rev,
        result=result, parent=parent,
    )
    db.commit()
    db.refresh(plan)
    return plan


def list_plans(db: Session, root_id: str | None = None) -> list[models.Plan]:
    stmt = select(models.Plan).order_by(
        models.Plan.root_id, models.Plan.revision_no
    )
    if root_id:
        stmt = stmt.where(models.Plan.root_id == root_id)
    return list(db.scalars(stmt))


def get_plan(db: Session, plan_id: str) -> models.Plan:
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        raise BusinessError(f"方案 {plan_id} 不存在", 404)
    return plan


def confirm_plan(
    db: Session,
    plan_id: str,
    payload: PlanConfirmIn,
) -> models.Plan:
    plan = get_plan(db, plan_id)

    if plan.status == "confirmed":
        # 幂等：同一方案以其自身资料修订重复确认，直接返回
        if (
            payload.slots_revision == plan.slots_revision
            and payload.manifest_revision == plan.manifest_revision
        ):
            return plan
        raise ConflictError("方案已确认，不能重复确认（如资料已更新请修订方案后重新确认）")

    # 核对参与方案的箱与槽位资料修订；过期则拒绝，原方案保持草稿不变
    latest_slots = _latest_slot_revision(db)
    latest_manifest = _latest_manifest_revision(db)
    if payload.slots_revision != plan.slots_revision:
        raise ConflictError(
            f"提交的槽位资料修订 {payload.slots_revision} 与方案修订 "
            f"{plan.slots_revision} 不一致，拒绝确认"
        )
    if payload.manifest_revision != plan.manifest_revision:
        raise ConflictError(
            f"提交的箱清单修订 {payload.manifest_revision} 与方案修订 "
            f"{plan.manifest_revision} 不一致，拒绝确认"
        )
    if plan.slots_revision != latest_slots:
        raise ConflictError(
            f"槽位资料已过期：方案基于修订 {plan.slots_revision}，"
            f"当前最新修订 {latest_slots}，原方案保持不变"
        )
    if plan.manifest_revision != latest_manifest:
        raise ConflictError(
            f"箱清单已过期：方案基于修订 {plan.manifest_revision}，"
            f"当前最新修订 {latest_manifest}，原方案保持不变"
        )

    # 读检查完成后快照本方案占用槽位，并释放只读事务；
    # 随后在独立连接上以 IMMEDIATE 立即拿写锁，串行化并发确认；
    # 槽位唯一索引兜底冲突占用。锁内不使用 ORM 会话，避免隐式事务干扰。
    plan_slots = tuple(i.slot_code for i in plan.items)
    db.rollback()

    from app.database import engine

    conn = engine.pool.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            "SELECT status, revision_no, root_id FROM plans WHERE id = ?",
            (plan.id,),
        )
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            raise ConflictError("方案在确认期间被删除，确认被拒绝")
        status_locked, rev_no_locked, root_locked = row
        if status_locked != "draft":
            conn.rollback()
            raise ConflictError(
                f"方案状态已为 {status_locked}（并发操作），本次确认被拒绝，原方案不变"
            )

        cursor.execute(
            "SELECT id FROM plans WHERE root_id = ? AND status = 'confirmed' "
            "AND revision_no > ? LIMIT 1",
            (root_locked, rev_no_locked),
        )
        if cursor.fetchone() is not None:
            conn.rollback()
            raise ConflictError(
                "同一方案已存在更新的已确认修订，本次确认被拒绝，原方案不变"
            )

        # 同根旧已确认修订释放槽位占用并标记为被取代
        cursor.execute(
            "SELECT id FROM plans WHERE root_id = ? AND status = 'confirmed'",
            (root_locked,),
        )
        old_ids = [r[0] for r in cursor.fetchall()]
        for old_id in old_ids:
            cursor.execute("DELETE FROM confirmed_slots WHERE plan_id = ?", (old_id,))
            cursor.execute(
                "UPDATE plans SET status = 'superseded' WHERE id = ?", (old_id,)
            )

        # 冲突预检：本方案占用槽位是否已被其他方案的确认占用
        conflict_slot = None
        if plan_slots:
            placeholders = ",".join("?" for _ in plan_slots)
            cursor.execute(
                f"SELECT slot_code FROM confirmed_slots WHERE slot_code IN "
                f"({placeholders})",
                plan_slots,
            )
            row_conflict = cursor.fetchone()
            conflict_slot = row_conflict[0] if row_conflict else None
        if conflict_slot is not None:
            conn.rollback()
            raise ConflictError(
                f"并发确认冲突：槽位 {conflict_slot} 已被其他已确认方案占用，"
                "本次确认被拒绝，原方案不变"
            )

        now = models._utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        cursor.execute(
            "UPDATE plans SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
            (now, plan.id),
        )
        cursor.executemany(
            "INSERT INTO confirmed_slots (plan_id, slot_code) VALUES (?, ?)",
            [(plan.id, i.slot_code) for i in plan.items],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    db.expire_all()
    return get_plan(db, plan_id)


def _latest_slot_revision(db: Session) -> int:
    return db.scalar(select(func.max(models.SlotRevision.revision))) or 0


def _latest_manifest_revision(db: Session) -> int:
    return db.scalar(select(func.max(models.ManifestRevision.revision))) or 0


def utc_iso(dt) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.astimezone(timezone.utc).isoformat()
