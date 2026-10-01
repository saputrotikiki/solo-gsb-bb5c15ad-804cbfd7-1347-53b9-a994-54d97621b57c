"""装船排程 API 端到端测试。"""

from tests.test_api_workflow import MANIFEST_PAYLOAD, SLOT_PAYLOAD


def _seed_confirmed_plan(client):
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    return plan


def _tasks(containers, duration=2, earliest=0, latest=100):
    return [
        {
            "no": c["no"], "duration": duration,
            "earliest_start": earliest, "latest_finish": latest,
        }
        for c in containers
    ]


def test_create_schedule_for_confirmed_plan(client):
    plan = _seed_confirmed_plan(client)
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 2, "tasks": _tasks(MANIFEST_PAYLOAD["containers"])
    })
    assert r.status_code == 201, r.text
    sched = r.json()
    assert sched["status"] == "current"
    assert sched["cranes"] == 2
    assert sched["makespan"] == 2
    assert sched["plan_id"] == plan["plan_id"]
    assert sched["plan_revision_no"] == 1
    # 两箱互不构成上下层（同层不同堆栈），两台桥并行
    by_no = {t["container_no"]: t for t in sched["tasks"]}
    assert set(by_no) == {"C001", "C002"}
    c1, c2 = by_no["C001"], by_no["C002"]
    assert (c1["start"], c1["crane"]) == (0, 1)
    assert (c2["start"], c2["crane"]) == (0, 2)
    for t in sched["tasks"]:
        assert t["finish"] == t["start"] + 2
        assert t["stack"] in ("A", "B") and t["tier"] == 1


def test_schedule_validates_all_constraints(client):
    plan = _seed_confirmed_plan(client)
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 1,
        "tasks": _tasks(MANIFEST_PAYLOAD["containers"], duration=4,
                        earliest=0, latest=5),
    })
    # 一台桥串行：第二箱最早 4 开工，4+4 > 5 -> 时间窗无解
    assert r.status_code == 422
    assert "时间窗无解" in r.json()["detail"]
    # 无解不落库
    assert client.get("/api/schedules").json() == []


def test_schedule_missing_and_extra_containers_rejected(client):
    plan = _seed_confirmed_plan(client)
    # 缺少 C002
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 1, "tasks": _tasks([MANIFEST_PAYLOAD["containers"][1]])
    })
    assert r.status_code == 422
    assert "缺少箱项" in r.json()["detail"] and "C002" in r.json()["detail"]

    # 多余箱
    extra = _tasks(MANIFEST_PAYLOAD["containers"]) + [{
        "no": "GHOST", "duration": 1, "earliest_start": 0, "latest_finish": 10
    }]
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 1, "tasks": extra
    })
    assert r.status_code == 422
    assert "多余箱项" in r.json()["detail"] and "GHOST" in r.json()["detail"]
    assert client.get("/api/schedules").json() == []


def test_unconfirmed_plan_schedule_rejected(client):
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD)
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 1, "tasks": _tasks(MANIFEST_PAYLOAD["containers"])
    })
    assert r.status_code == 409
    assert "尚未确认" in r.json()["detail"]
    assert client.get("/api/schedules").json() == []


def test_schedule_plan_not_found_404(client):
    r = client.post("/api/plans/nope/schedules", json={"cranes": 1, "tasks": []})
    assert r.status_code == 404


def test_invalid_crane_and_duration_rejected(client):
    plan = _seed_confirmed_plan(client)
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 0, "tasks": _tasks(MANIFEST_PAYLOAD["containers"])
    })
    assert r.status_code == 422
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 1,
        "tasks": [
            {"no": c["no"], "duration": 0,
             "earliest_start": 0, "latest_finish": 10}
            for c in MANIFEST_PAYLOAD["containers"]
        ],
    })
    assert r.status_code == 422
    # 窗口倒置（Pydantic 层拦截）
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 1,
        "tasks": [
            {"no": c["no"], "duration": 3,
             "earliest_start": 5, "latest_finish": 6}
            for c in MANIFEST_PAYLOAD["containers"]
        ],
    })
    assert r.status_code == 422
    assert "时间窗无解" in r.text


def test_reschedule_marks_previous_historical(client):
    plan = _seed_confirmed_plan(client)
    s1 = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 1, "tasks": _tasks(MANIFEST_PAYLOAD["containers"])
    }).json()
    s2 = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 2, "tasks": _tasks(MANIFEST_PAYLOAD["containers"])
    }).json()
    assert s2["status"] == "current"
    # 旧排程仍可按 ID 查询，但标记为历史
    old = client.get(f"/api/schedules/{s1['schedule_id']}").json()
    assert old["status"] == "superseded"
    assert client.get(f"/api/schedules/{s2['schedule_id']}").json()["status"] == "current"

    # 当前排程列表只有一个
    current = client.get(
        f"/api/schedules?plan_id={plan['plan_id']}&status=current"
    ).json()
    assert [s["schedule_id"] for s in current] == [s2["schedule_id"]]
    all_sched = client.get(f"/api/schedules?plan_id={plan['plan_id']}").json()
    assert {s["status"] for s in all_sched} == {"current", "superseded"}


def test_amend_confirm_marks_old_revision_schedules_historical(client):
    p1 = _seed_confirmed_plan(client)
    s1 = client.post(f"/api/plans/{p1['plan_id']}/schedules", json={
        "cranes": 2, "tasks": _tasks(MANIFEST_PAYLOAD["containers"])
    }).json()

    # 新清单（一个箱），修订并确认 revision 2
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1},
        ]
    })
    p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": p2["slots_revision"],
        "manifest_revision": p2["manifest_revision"],
    })
    assert ok.status_code == 200

    # 旧修订（p1）上的排程变为历史，且不再列为当前排程
    old = client.get(f"/api/schedules/{s1['schedule_id']}").json()
    assert old["status"] == "superseded"
    current = client.get("/api/schedules?status=current").json()
    assert all(s["plan_id"] != p1["plan_id"] for s in current)


def test_empty_plan_empty_schedule_zero_makespan(client):
    # 空箱清单方案
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    client.post("/api/manifest-revisions", json={"containers": []})
    plan = client.post("/api/plans/compute", json={}).json()
    assert plan["assignments"] == []
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": plan["slots_revision"],
        "manifest_revision": plan["manifest_revision"],
    })
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 3, "tasks": []
    })
    assert r.status_code == 201, r.text
    sched = r.json()
    assert sched["tasks"] == [] and sched["makespan"] == 0
    assert sched["cranes"] == 3 and sched["status"] == "current"

    # 空方案提交了箱项 -> 多余箱项拒绝
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 1,
        "tasks": [{"no": "X", "duration": 1,
                   "earliest_start": 0, "latest_finish": 5}],
    })
    assert r.status_code == 422 and "多余箱项" in r.json()["detail"]


def test_duplicate_task_no_rejected(client):
    plan = _seed_confirmed_plan(client)
    tasks = _tasks(MANIFEST_PAYLOAD["containers"]) * 2
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 1, "tasks": tasks
    })
    assert r.status_code == 422 and "排程箱号重复" in r.text


def test_stack_precedence_on_real_two_tier_plan(client):
    """4 箱占两层：同堆栈下层箱完工后上层箱才可开工（即使岸桥充足也不并行）。"""
    slots = {
        "slots": [
            {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
            {"code": "A02", "stack": "A", "tier": 2, "capacity": 100, "arm": 3.0},
            {"code": "B01", "stack": "B", "tier": 1, "capacity": 100, "arm": 2.0},
            {"code": "B02", "stack": "B", "tier": 2, "capacity": 100, "arm": 3.0},
        ],
        "adjacency": [],
        "incompatible_classes": [],
        "min_moment": -100000,
        "max_moment": 100000,
    }
    # 重者在下（tier1）、轻者在上（tier2）
    manifest = {
        "containers": [
            {"no": "UP_A", "weight": 10, "hazmat": None, "discharge_order": 1},
            {"no": "LOW_A", "weight": 30, "hazmat": None, "discharge_order": 2},
            {"no": "UP_B", "weight": 10, "hazmat": None, "discharge_order": 1},
            {"no": "LOW_B", "weight": 30, "hazmat": None, "discharge_order": 2},
        ]
    }
    client.post("/api/slot-revisions", json=slots)
    client.post("/api/manifest-revisions", json=manifest)
    plan = client.post("/api/plans/compute", json={}).json()
    # 每堆栈各占两层，由实际方案位置推导下层/上层箱
    by_stack = {}
    for a in plan["assignments"]:
        by_stack.setdefault(a["stack"], {})[a["tier"]] = a["container_no"]
    assert sorted(by_stack) == ["A", "B"]
    lower = {s: tiers[1] for s, tiers in by_stack.items()}
    upper = {s: tiers[2] for s, tiers in by_stack.items()}
    # 下重上轻（下层不轻于上层）
    weight = {c["no"]: c["weight"] for c in manifest["containers"]}
    for s in ("A", "B"):
        assert weight[lower[s]] >= weight[upper[s]]
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": plan["slots_revision"],
        "manifest_revision": plan["manifest_revision"],
    })

    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "cranes": 4,
        "tasks": [
            {"no": no, "duration": dur, "earliest_start": 0, "latest_finish": 100}
            for no, dur in [
                (lower["A"], 3), (upper["A"], 2),
                (lower["B"], 3), (upper["B"], 2),
            ]
        ],
    })
    assert r.status_code == 201, r.text
    sched = r.json()
    # 两堆栈链并行，每链 3+2=5
    assert sched["makespan"] == 5
    by_no = {t["container_no"]: t for t in sched["tasks"]}
    for s in ("A", "B"):
        assert by_no[upper[s]]["start"] >= by_no[lower[s]]["finish"]
        assert by_no[lower[s]]["start"] == 0


def test_list_schedules_filter_invalid_status(client):
    r = client.get("/api/schedules?status=bogus")
    assert r.status_code == 400


def test_get_nonexistent_schedule_404(client):
    assert client.get("/api/schedules/nope").status_code == 404
