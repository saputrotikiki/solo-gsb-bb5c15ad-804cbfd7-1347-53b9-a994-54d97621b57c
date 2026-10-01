"""装船排程求解器（混合整数规划）。

输入已确认配载方案中每箱所在堆栈/层，以及每箱的吊装时长、时间窗
（最早开工 ``es``、最迟完工 ``lf``，单位：分钟），在 ``m`` 台同型岸桥下
排出每箱的开工、完工与岸桥编号。

硬性约束：

* 每箱恰好安排一次，占用且仅占用一台岸桥；
* 同一岸桥作业区间 ``[开工, 完工)`` 不得重叠（半开区间，首尾相接允许）；
* 同堆栈下层箱完工后，上层箱才可开工（``完工_下 <= 开工_上``）；
* 每箱满足自身时间窗：``es <= 开工`` 且 ``完工 = 开工 + 时长 <= lf``
  （允许恰在最迟完工时刻结束）。

优化目标（字典序）：

1. 全部箱的最晚完工时间（makespan）``C`` 最小；
2. 在 ``C`` 最优的前提下，按箱号升序，使各箱 ``(开工分钟, 岸桥编号)``
   序列字典序最小。箱号内以开工为主、岸桥编号为辅，编码为
   ``pos = 开工 * m + (岸桥序号-1)``，分块以基数 ``B=(H+1)*m`` 加权，
   数学上与完整字典序最小严格等价（分块仅为把系数控制在双精度精确
   整数范围内）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pulp

# 单次 CBC 求解时间上限（秒）
TIME_LIMIT = 30

# 同岸桥非重叠析取约束的大 M：所有开工/完工都落在 [0, H] 内，M=H+1
# 可保证约束被放松时恒成立、被启用时严格有效。


class InfeasibleSchedule(Exception):
    """不存在满足全部约束的装船排程。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Task:
    no: str
    stack: str
    tier: int
    duration: int  # 吊装时长（正整数，分钟）
    earliest: int  # 最早开工（非负整数，分钟）
    latest: int  # 最迟完工（非负整数，分钟）


@dataclass
class ScheduledTask:
    no: str
    start: int
    finish: int
    crane: int  # 岸桥编号，1..m


@dataclass
class ScheduleResult:
    tasks: list[ScheduledTask]
    makespan: int


def _prechecks(tasks: Sequence[Task], cranes: int) -> None:
    for t in tasks:
        if t.duration <= 0:
            raise InfeasibleSchedule(f"箱 {t.no} 吊装时长必须为正")
        if t.earliest < 0 or t.latest < 0:
            raise InfeasibleSchedule(f"箱 {t.no} 时间窗不得为负")
        if t.earliest + t.duration > t.latest:
            raise InfeasibleSchedule(
                f"箱 {t.no} 时间窗无解：最早开工 {t.earliest} + 吊装时长 "
                f"{t.duration} > 最迟完工 {t.latest}"
            )


def _greedy_schedule(
    tasks: Sequence[Task], cranes: int
) -> dict[str, tuple[int, int]] | None:
    """贪心构造一个可行排程（列表调度）。

    按堆栈/层拓扑序（下层先排），每箱取最早可行开工时刻：
    max(最早时间窗, 所有下层完工, 所选岸桥上一箱完工)，尝试各岸桥并取
    开工最早（平手取小编号桥）。成功返回 箱号->(开工,岸桥)，失败返回 None。
    用于：① 阶段一/字典序阶段的 CBC 热启动；② 收紧 makespan 上界与大 M。
    """
    # 拓扑序：同堆栈按层升序，不同堆栈任意（按箱号稳定排序）
    ordered = sorted(tasks, key=lambda t: (t.stack, t.tier, t.no))
    finish: dict[str, int] = {}
    # 每台岸桥：已排箱 [(no, start, finish)]
    crane_jobs: list[list[tuple[str, int, int]]] = [[] for _ in range(cranes)]
    assign: dict[str, tuple[int, int]] = {}

    for t in ordered:
        same_stack = sorted(
            (x for x in ordered if x.stack == t.stack and x.tier < t.tier),
            key=lambda x: x.tier,
        )
        precedence_ready = max(
            (finish[x.no] for x in same_stack if x.no in finish), default=0
        )
        best = None
        for j in range(cranes):
            crane_ready = max((f for _, _, f in crane_jobs[j]), default=0)
            start = max(t.earliest, precedence_ready, crane_ready)
            if start + t.duration <= t.latest:
                if best is None or start < best[0]:
                    best = (start, j)
        if best is None:
            return None
        start, j = best
        f = start + t.duration
        crane_jobs[j].append((t.no, start, f))
        finish[t.no] = f
        assign[t.no] = (start, j + 1)
    return assign


def _build_model(
    tasks: Sequence[Task],
    cranes: int,
    *,
    horizon: int,
    c_bound: int | None = None,
    fixed: Mapping[str, tuple[int, int]] | None = None,
    lex_block: Sequence[str] | None = None,
    warm: Mapping[str, tuple[int, int]] | None = None,
):
    """构建排程 MILP。

    * horizon: 开工/完工与大 M 的统一上界（取贪心可行排程 makespan）；
    * c_bound: 约束总完工 C <= c_bound；
    * fixed: 箱号 -> (开工, 岸桥编号)，字典序优化中已固定前缀；
    * lex_block: 目标改为该箱号序列（重要性从高到低）的
      (开工, 岸桥) 字典序加权和；
    * warm: 箱号 -> (开工, 岸桥编号) 初始可行解，用于 CBC 热启动。
    """
    fixed = fixed or {}
    n = len(tasks)
    H = horizon
    M = H + 1

    prob = pulp.LpProblem("loading_schedule", pulp.LpMinimize)

    # s_i: 开工分钟；y_{i,j}: 箱 i 是否分配给岸桥 j（编号 j+1）
    s = {
        t.no: pulp.LpVariable(f"s_{i}", lowBound=0, upBound=H, cat="Integer")
        for i, t in enumerate(tasks)
    }
    y = {
        (t.no, j): pulp.LpVariable(f"y_{i}_{j}", cat="Binary")
        for i, t in enumerate(tasks)
        for j in range(cranes)
    }
    C = pulp.LpVariable("C", lowBound=0, upBound=H, cat="Integer")

    if lex_block:
        base = (H + 1) * cranes
        k = len(lex_block)
        prob += pulp.lpSum(
            (base ** (k - 1 - p))
            * (
                cranes * s[cno]
                + pulp.lpSum(j * y[cno, j] for j in range(cranes))
            )
            for p, cno in enumerate(lex_block)
        ), "lex_objective"
    else:
        prob += C, "min_makespan"

    # 每箱恰好一台岸桥；时间窗；C >= 各箱完工
    for t in tasks:
        prob += pulp.lpSum(y[t.no, j] for j in range(cranes)) == 1, f"once_{t.no}"
        prob += s[t.no] >= t.earliest, f"es_{t.no}"
        prob += s[t.no] + t.duration <= t.latest, f"lf_{t.no}"
        prob += C >= s[t.no] + t.duration, f"C_{t.no}"
    if c_bound is not None:
        prob += C <= c_bound, "C_bound"

    # 岸桥对称破缺：岸桥同型，箱按序号 i 升序“填空”——箱 i 要上 j+1 号
    # 桥，必须有序号更小的箱已在 j 号桥上。消除岸桥排列对称，且与
    # “(开工, 岸桥编号) 字典序最小、岸桥编号越小越优”的取向一致。
    for j in range(1, cranes):
        for i in range(1, n):
            prob += (
                y[tasks[i].no, j]
                <= pulp.lpSum(y[tasks[k].no, j - 1] for k in range(i))
            ), f"sym_{i}_{j}"

    # 字典序前缀固定
    for cno, (st, crane) in fixed.items():
        prob += s[cno] == st, f"fix_s_{cno}"
        prob += y[cno, crane - 1] == 1, f"fix_y_{cno}"

    # CBC 热启动初始解
    if warm:
        for cno, (st, crane) in warm.items():
            s[cno].setInitialValue(st)
            for j in range(cranes):
                y[cno, j].setInitialValue(1 if j == crane - 1 else 0)

    # 同岸桥不重叠：每对箱一个排序二元变量 z=1 表示 i 在 k 之前。
    # 大 M 按箱对时间窗收紧：正向松弛只需覆盖 s_i+d_i-s_k 的上界
    # lf_i - es_k，反向同理，取两者（及 1）的最大值，保持放松有效。
    for i in range(n):
        ti = tasks[i]
        for k in range(i + 1, n):
            tk = tasks[k]
            m_pair = max(ti.latest - tk.earliest, tk.latest - ti.earliest, 1)
            z = pulp.LpVariable(f"z_{i}_{k}", cat="Binary")
            for j in range(cranes):
                share_relax = m_pair * (2 - y[ti.no, j] - y[tk.no, j])
                prob += (
                    s[ti.no] + ti.duration
                    <= s[tk.no] + m_pair * (1 - z) + share_relax
                ), f"ord1_{i}_{k}_{j}"
                prob += (
                    s[tk.no] + tk.duration
                    <= s[ti.no] + m_pair * z + share_relax
                ), f"ord2_{i}_{k}_{j}"

    # 同堆栈：只约束相邻（按已占用层排序）两层——下层完工 <= 上层开工；
    # 非相邻层由正吊装时长经传递性自动满足。
    by_stack: dict[str, list[Task]] = {}
    for t in tasks:
        by_stack.setdefault(t.stack, []).append(t)
    for stack_tasks in by_stack.values():
        ordered = sorted(stack_tasks, key=lambda t: t.tier)
        for lower, upper in zip(ordered, ordered[1:]):
            if lower.tier >= upper.tier:
                continue
            prob += (
                s[upper.no] >= s[lower.no] + lower.duration
            ), f"stack_{lower.no}_{upper.no}"

    return prob, s, y, C


def _solve(prob: pulp.LpProblem, warm: bool = False) -> str:
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=TIME_LIMIT, gapRel=0, warmStart=warm)
    prob.solve(solver)
    return pulp.LpStatus[prob.status]


def solve_schedule(
    tasks: Sequence[Task],
    cranes: int,
) -> ScheduleResult:
    """求装船排程；无可行排程时抛出 :class:`InfeasibleSchedule`。"""
    if cranes <= 0:
        raise InfeasibleSchedule("岸桥数量必须为正")
    tasks = sorted(tasks, key=lambda t: t.no)
    _prechecks(tasks, cranes)

    if not tasks:
        return ScheduleResult([], 0)

    dur_by_no = {t.no: t.duration for t in tasks}

    # 贪心可行解：热启动 + 收紧 makespan 上界（从而收紧大 M 与变量界）。
    # 贪心可能失败（窗口紧时），此时退回以最迟完工为视界，交由 MILP 判定。
    greedy = _greedy_schedule(tasks, cranes)
    if greedy is not None:
        horizon = max(st + dur_by_no[no] for no, (st, _) in greedy.items())
    else:
        horizon = max(t.latest for t in tasks)

    # 第一阶段：最小化总完工时间
    prob1, s1, y1, C1 = _build_model(tasks, cranes, horizon=horizon, warm=greedy)
    status = _solve(prob1, warm=greedy is not None)
    if status == "Infeasible":
        raise InfeasibleSchedule(
            "时间窗无解：在给定岸桥数量、各箱吊装时长与时间窗、"
            "同堆栈上下层先后次序下不存在可行排程"
        )
    if status != "Optimal":
        raise InfeasibleSchedule(
            f"求解器在 {TIME_LIMIT}s 内未能判定可行排程（状态：{status}），"
            "请缩小规模或重试"
        )
    c_star = int(round(pulp.value(C1)))

    def _assignment(sv, yv) -> dict[str, tuple[int, int]]:
        out = {}
        for t in tasks:
            st = int(round(pulp.value(sv[t.no])))
            crane = next(
                j + 1 for j in range(cranes)
                if pulp.value(yv[t.no, j]) is not None and pulp.value(yv[t.no, j]) > 0.5
            )
            out[t.no] = (st, crane)
        return out

    # 阶段一最优解作为字典序阶段的初始可行解（热启动）
    warm = _assignment(s1, y1)

    # 第二阶段：C <= c_star 下，按箱号升序分块求 (开工, 岸桥) 字典序最小。
    # 基数 B=(视界+1)*m；分块使 B^chunk <= 1e13，保证目标系数为双精度精确整数。
    base = (horizon + 1) * cranes
    chunk = max(1, int(13 // max(1, len(str(base)))))
    ordered_nos = [t.no for t in tasks]
    fixed: dict[str, tuple[int, int]] = {}
    last_s, last_y = s1, y1

    for start in range(0, len(ordered_nos), chunk):
        block = ordered_nos[start:start + chunk]
        prob, s, y, _ = _build_model(
            tasks, cranes, horizon=horizon, c_bound=c_star,
            fixed=fixed, lex_block=block, warm=warm,
        )
        blk_status = _solve(prob, warm=True)
        if blk_status != "Optimal":
            raise InfeasibleSchedule(
                f"字典序优化在 {TIME_LIMIT}s 内未能完成（状态：{blk_status}），"
                "请缩小规模或重试"
            )
        block_assign = _assignment(s, y)
        fixed.update(block_assign)
        warm.update(block_assign)
        last_s, last_y = s, y

    scheduled: list[ScheduledTask] = []
    for t in tasks:
        st = int(round(pulp.value(last_s[t.no])))
        crane = next(
            j + 1 for j in range(cranes)
            if pulp.value(last_y[t.no, j]) is not None
            and pulp.value(last_y[t.no, j]) > 0.5
        )
        scheduled.append(
            ScheduledTask(no=t.no, start=st, finish=st + dur_by_no[t.no], crane=crane)
        )

    result = ScheduleResult(tasks=scheduled, makespan=c_star)
    _verify(result, tasks)
    return result


def _verify(result: ScheduleResult, tasks: Sequence[Task]) -> None:
    """对 MILP 输出做独立的硬性约束复核（防御性校验，不应触发）。"""
    src = {t.no: t for t in tasks}
    if len(result.tasks) != len(tasks) or {t.no for t in result.tasks} != set(src):
        raise InfeasibleSchedule("排程求解结果未覆盖全部箱（内部错误）")
    for t in result.tasks:
        q = src[t.no]
        if t.crane < 1 or t.finish != t.start + q.duration:
            raise InfeasibleSchedule("排程求解结果岸桥或工时异常（内部错误）")
        if t.start < q.earliest or t.finish > q.latest:
            raise InfeasibleSchedule("排程求解结果违反时间窗（内部错误）")
    # 同岸桥不重叠
    by_crane: dict[int, list[ScheduledTask]] = {}
    for t in result.tasks:
        by_crane.setdefault(t.crane, []).append(t)
    for jobs in by_crane.values():
        jobs.sort(key=lambda x: (x.start, x.finish))
        for a, b in zip(jobs, jobs[1:]):
            if a.finish > b.start:
                raise InfeasibleSchedule("排程求解结果存在同岸桥作业重叠（内部错误）")
    # 同堆栈下层完工 <= 上层开工
    by_stack: dict[str, list[ScheduledTask]] = {}
    for t in result.tasks:
        by_stack.setdefault(src[t.no].stack, []).append(t)
    for jobs in by_stack.values():
        jobs.sort(key=lambda x: src[x.no].tier)
        for lower, upper in zip(jobs, jobs[1:]):
            if src[lower.no].tier < src[upper.no].tier and lower.finish > upper.start:
                raise InfeasibleSchedule("排程求解结果违反堆栈上下层先后（内部错误）")
    if result.makespan != max((t.finish for t in result.tasks), default=0):
        raise InfeasibleSchedule("排程求解总体完工时间不一致（内部错误）")
