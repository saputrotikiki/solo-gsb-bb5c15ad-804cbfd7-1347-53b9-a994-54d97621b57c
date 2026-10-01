"""装船排程求解器单元测试。"""

import pytest

from app.scheduler import InfeasibleSchedule, Task, solve_schedule


def T(no, stack="A", tier=1, duration=2, earliest=0, latest=100):
    return Task(no, stack, tier, duration, earliest, latest)


def _validate(result, cranes):
    by_no = {t.no: t for t in result.tasks}
    # 同岸桥不重叠
    by_crane = {}
    for t in result.tasks:
        by_crane.setdefault(t.crane, []).append(t)
    for ts in by_crane.values():
        ts = sorted(ts, key=lambda x: x.start)
        for a, b in zip(ts, ts[1:]):
            assert a.finish <= b.start
    # makespan = 最大完工；岸桥编号合法
    assert result.makespan == max((t.finish for t in result.tasks), default=0)
    assert all(1 <= t.crane <= cranes for t in result.tasks)
    return by_no


def test_single_crane_serial_order():
    tasks = [T("C1", duration=3), T("C2", duration=4), T("C3", duration=2)]
    r = solve_schedule(tasks, 1)
    assert r.makespan == 9
    by_no = _validate(r, 1)
    # 全部同一岸桥
    assert {t.crane for t in r.tasks} == {1}
    # 字典序：C1 开工最小（0）
    assert by_no["C1"].start == 0


def test_two_cranes_parallel_makespan():
    # 两台桥、三个等长（时长2）箱（互不构成上下层）：一台桥必装两箱 -> 4
    tasks = [T("C1", "A", 1), T("C2", "B", 1), T("C3", "C", 1)]
    r = solve_schedule(tasks, 2)
    assert r.makespan == 4
    _validate(r, 2)
    starts = sorted(t.start for t in r.tasks)
    assert starts == [0, 0, 2]


def test_stack_precedence_lower_before_upper():
    tasks = [
        T("LOW", "A", 1, duration=3),
        T("MID", "A", 2, duration=2),
        T("UP", "A", 3, duration=1),
        T("OTHER", "B", 1, duration=5),
    ]
    r = solve_schedule(tasks, 2)
    by_no = _validate(r, 2)
    assert by_no["MID"].start >= by_no["LOW"].finish
    assert by_no["UP"].start >= by_no["MID"].finish
    assert r.makespan == 6  # 堆栈链 3+2+1=6，与 OTHER(5) 并行


def test_time_window_latest_finish_boundary_allowed():
    # 允许恰在最迟完工时刻结束：0 + 5 == 5
    r = solve_schedule([T("X", duration=5, earliest=0, latest=5)], 1)
    assert r.tasks[0].start == 0 and r.tasks[0].finish == 5
    assert r.makespan == 5


def test_earliest_start_respected():
    tasks = [
        T("LATE", "A", 1, duration=2, earliest=10, latest=20),
        T("EARLY", "B", 1, duration=2, earliest=0, latest=20),
    ]
    r = solve_schedule(tasks, 2)
    by_no = _validate(r, 2)
    assert by_no["LATE"].start >= 10
    assert r.makespan == 12


def test_window_forces_serialization_infeasible_on_one_crane():
    # 两箱窗口都是 [0,3)，时长 2，一台桥无法都在窗口内完成
    tasks = [
        T("A", duration=2, earliest=0, latest=3),
        T("B", duration=2, earliest=0, latest=3),
    ]
    with pytest.raises(InfeasibleSchedule):
        solve_schedule(tasks, 1)
    # 两台桥并行即可
    r = solve_schedule(tasks, 2)
    assert r.makespan == 2


def test_precheck_single_box_window_infeasible():
    with pytest.raises(InfeasibleSchedule):
        solve_schedule([T("X", duration=5, earliest=0, latest=4)], 1)


def test_invalid_cranes():
    with pytest.raises(InfeasibleSchedule):
        solve_schedule([T("X")], 0)


def test_empty_schedule():
    r = solve_schedule([], 3)
    assert r.tasks == [] and r.makespan == 0


def test_greedy_fails_but_milp_feasible_fallback():
    # 贪心按箱号先排 A 会把 B 挤出窗口；可行解要求 B 先于 A。
    # 贪心返回 None 时，必须退回以最迟完工为视界交 MILP 正确求解。
    tasks = [
        Task("A", "K1", 1, duration=1, earliest=3, latest=5),
        Task("B", "K2", 1, duration=4, earliest=0, latest=4),
    ]
    r = solve_schedule(tasks, 1)
    by_no = {t.no: t for t in r.tasks}
    assert by_no["B"].start == 0 and by_no["B"].finish == 4
    assert by_no["A"].start == 4 and by_no["A"].finish == 5
    assert r.makespan == 5


def test_lex_minimizes_start_then_crane():
    # 两箱同窗口、两台桥：makespan=2 时 C001 必须取 (0, 岸桥1)；
    # C002 亦 (0, 岸桥2)
    tasks = [
        Task("C002", "A", 1, 2, 0, 100),
        Task("C001", "B", 1, 2, 0, 100),
    ]
    r = solve_schedule(tasks, 2)
    by_no = _validate(r, 2)
    assert (by_no["C001"].start, by_no["C001"].crane) == (0, 1)
    assert (by_no["C002"].start, by_no["C002"].crane) == (0, 2)


def test_lex_start_takes_priority_over_crane():
    # 三台桥、三箱窗口 [0..]：C001 先占 (0,桥1)；C002 可行选择 (0,桥2) 或
    # 更早开工不存在 -> (0,2)。再构造 C003 窗口使其可选 (0,桥3) 或等待，
    # 验证开工优先于岸桥编号。
    tasks = [
        Task("C001", "A", 1, 3, 0, 100),
        Task("C002", "B", 1, 3, 0, 100),
        Task("C003", "C", 1, 3, 0, 100),
        Task("C004", "D", 1, 1, 0, 100),
    ]
    r = solve_schedule(tasks, 3)
    by_no = _validate(r, 3)
    assert r.makespan == 4
    assert (by_no["C001"].start, by_no["C001"].crane) == (0, 1)
    assert (by_no["C002"].start, by_no["C002"].crane) == (0, 2)
    assert (by_no["C003"].start, by_no["C003"].crane) == (0, 3)
