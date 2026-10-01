# 集装箱船配载方案核算与装船排程服务

基于 Python + FastAPI + SQLAlchemy(SQLite) + PuLP(CBC 整数规划求解器) 实现的集装箱船配载方案核算与装船排程 HTTP 服务。

录入按**堆栈（stack）与层（tier）**编号的槽位、各槽承重、纵向力臂、相邻槽位关系与全船允许力矩区间，以及箱号、重量、危险类别、卸港先后与不相容危险类别对；服务输出**每箱槽位、总力矩与方案修订号**。方案确认后，可再提交**岸桥数量与每箱吊装时长/时间窗**，服务输出**每箱开工、完工、岸桥编号与总体完工时间**。

## 一、配载约束（全部为硬性约束）

1. **承重**：任一槽位上箱的重量不得超过该槽承重；
2. **堆叠支撑**：某层有箱时，其下层（同堆栈层编号相邻的槽）必须有箱，即不允许悬空；
3. **重量次序**：下层箱不得轻于上层箱（`重量(下层) >= 重量(上层)`）；
4. **卸港先后**：早卸港箱不得压在晚卸港箱下方（`discharge_order` 小者先卸，先卸港在上、后卸港在下）；
5. **危险类别相邻**：不相容危险类别的两个箱不得占据互为相邻的槽位；
6. **总力矩**：全船总力矩 `Σ(箱重量 × 所在槽纵向力臂)` 必须落在允许区间 `[min_moment, max_moment]` 内；
7. 每个箱恰好占用一个槽，每个槽至多一箱，方案必须覆盖清单内**全部箱**。

## 二、优选规则（完整可行方案之间）

1. **最大占用层数最小**（占用到的最高 `tier` 最小）；
2. 最大占用层数相同时，**按箱号排序后取槽位编号序列字典序最小**者。

不存在完整可行方案时返回 `422` 并说明“无解”及原因，**不保存任何方案**。

## 三、修订与确认

- 槽位资料与箱清单分别维护，每次录入生成递增的**资料修订号**；
- 方案带**方案修订号**：对已确认方案再次核算得到新修订（`revision_no + 1`），同一方案链以 `root_id` 关联；
- 确认方案时必须提交该方案参与的**槽位资料修订号与箱清单修订号**，服务再与当前最新修订核对：
  - 提交修订与方案修订不一致，或资料已产生更新修订（过期）→ 返回 `409` 拒绝，**原方案保持不变**；
- 并发确认通过 SQLite `BEGIN IMMEDIATE` 写锁串行化 + `confirmed_slots(slot_code UNIQUE)` 唯一索引兜底，**不会产生冲突的已确认槽位占用**（一个槽位同一时刻至多属于一个已确认方案）；
- 新修订确认后，同一方案链的旧已确认修订变为 `superseded` 并释放槽位占用。

## 四、装船排程（仅对已确认方案修订）

方案确认后，按方案 ID（即某次具体方案修订）提交 **岸桥数量** 与**每箱吊装时长、时间窗**，服务用整数规划排出可执行的装船时间表。

### 排程硬约束

1. **每箱恰好安排一次**：每箱占用且仅占用一台岸桥；提交的箱项必须与该方案修订的箱项**完全一致**（不缺、不多），否则 `422` 拒绝且不保存；
2. **岸桥数量为正整数**，每箱吊装时长为**正整数**，最早开工、最迟完工为**非负整数**（单位均为分钟）；
3. **同岸桥不重叠**：同一岸桥作业区间 `[开工, 完工)` 不得重叠（半开区间，前后两箱首尾相接允许）；
4. **堆栈上下层先后**：同一堆栈下层箱完工后，上层箱才可开工（`开工_上 >= 完工_下`），即使岸桥空闲也不得先吊上层；
5. **每箱时间窗**：`最早开工 <= 开工` 且 `开工 + 吊装时长 <= 最迟完工`，**允许恰在最迟完工时刻结束**；不满足则按“时间窗无解”`422` 拒绝且不保存。

### 优化规则（可行排程之间）

1. 先使**全部箱的最晚完工时间（总体完工 makespan）最早**；
2. makespan 最优的前提下，**按箱号升序**，使各箱的 `(开工分钟, 岸桥编号)` 序列**字典序最小**（箱号内开工优先、岸桥编号其次；岸桥自 1 编号）。

### 确认与历史

- 只有 `confirmed` 方案可编排：`draft` 返回 `409`（未确认），被取代的 `superseded` 修订也返回 `409`；方案不存在返回 `404`；
- 求解完成后、**保存前在写锁内再次核对方案仍为已确认**（防止确认后/修订确认的并发竞态），复核不过则拒绝且不保存；
- 同一方案修订重复提交排程：**新排程为当前（`current`），旧排程标记为历史（`superseded`）**；旧排程仍可按排程 ID 查询，但不再列入当前排程；
- 方案被新修订确认取代后，**旧方案修订上的排程一并标记为历史**；
- **空箱方案**：提交 `tasks: []` 返回空排程、总体完工时间 `0`（岸桥数仍须为正）。

排程响应中的每箱 `stack/tier` 取自所绑定方案修订，返回结果按箱号升序排列。

## 五、启动

### 方式 A：Docker Compose（推荐，一键启动）

```bash
docker compose up --build -d
docker compose logs -f stowage     # 查看日志
docker compose down                # 停止（数据保留在卷 stowage-data 中）
```

镜像构建时完成依赖安装；容器入口 `scripts/entrypoint.sh` 会先幂等初始化数据库表结构，再启动 API。

### 方式 B：本地直接运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 初始化（幂等）
python -c "from app.database import init_db; init_db()"

# 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 方式 C：Docker

```bash
docker build -t stowage-service .
docker run -d -p 8000:8000 -v stowage-data:/app/data stowage-service
```

## 六、配置

全部配置通过环境变量覆盖（前缀 `STOWAGE_`，也可用 `.env`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STOWAGE_PORT` | `8000` | Compose 映射到宿主机的端口（仅 compose 用） |
| `STOWAGE_DATABASE_URL` | `sqlite:///./data/stowage.db` | SQLAlchemy 数据库地址；容器内固定 `sqlite:////app/data/stowage.db` |
| `STOWAGE_API_PREFIX` | `/api` | API 路由前缀 |

## 七、地址与端口

- 服务地址（本地）：`http://127.0.0.1:8000`
- 健康检查：`GET http://127.0.0.1:8000/health`
- API 前缀：`http://127.0.0.1:8000/api`
- 交互式文档（Swagger UI）：`http://127.0.0.1:8000/docs`
- OpenAPI：`http://127.0.0.1:8000/openapi.json`

## 八、HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/slot-revisions` | 录入槽位资料，生成新槽位资料修订号 |
| POST | `/api/manifest-revisions` | 录入箱清单，生成新箱清单修订号 |
| GET | `/api/slot-revisions/{rev}` | 查询槽位资料修订 |
| GET | `/api/manifest-revisions/{rev}` | 查询箱清单修订 |
| POST | `/api/plans/compute` | 核算新方案（无解返回 422，不保存） |
| POST | `/api/plans/{plan_id}/amend` | 基于现有方案重新核算，生成方案修订号 |
| POST | `/api/plans/{plan_id}/confirm` | 确认方案（核对资料修订，409=过期/冲突） |
| GET | `/api/plans` | 列出全部方案 |
| GET | `/api/plans/{plan_id}` | 查询单个方案 |
| POST | `/api/plans/{plan_id}/schedules` | 为已确认方案修订编排装船排程（409=未确认/已取代，422=箱项不一致/时间窗无解，无解不保存） |
| GET | `/api/schedules` | 列出排程（可按 `plan_id`、`status=current|superseded` 过滤） |
| GET | `/api/schedules/{schedule_id}` | 查询单个排程（历史排程可查，`status` 标记） |

### 录入字段说明

槽位 `slots[]`：

| 字段 | 含义 |
| --- | --- |
| `code` | 槽位编号（建议定长如 `A01`，字典序即层位次序） |
| `stack` | 堆栈编号；同堆栈按 `tier` 自底向上构成堆叠关系 |
| `tier` | 层编号，自底向上从 1 开始的正整数 |
| `capacity` | 槽位承重，非负 |
| `arm` | 纵向力臂 |

`adjacency`：相邻槽位编号对（无向），如 `[["A01","B01"]]`。
`incompatible_classes`：不相容危险类别对，如 `[["1","5"]]`。

箱 `containers[]`：

| 字段 | 含义 |
| --- | --- |
| `no` | 箱号（同一清单内唯一） |
| `weight` | 重量，必须大于 0（否则“非法重量”报错） |
| `hazmat` | 危险类别字符串；普通箱传 `null` |
| `discharge_order` | 卸港先后正整数，小者先卸 |

装船排程请求体：

| 字段 | 含义 |
| --- | --- |
| `cranes` | 岸桥数量，**正整数**（岸桥自 1 编号） |
| `tasks[].no` | 箱号；集合必须与方案箱项**完全一致**（不缺不多），且请求内不重复 |
| `tasks[].duration` | 每箱吊装时长（分钟），**正整数** |
| `tasks[].earliest_start` | 最早开工分钟，非负整数 |
| `tasks[].latest_finish` | 最迟完工分钟，非负整数；须满足 `earliest_start + duration <= latest_finish`，允许恰在该时刻结束 |

### 错误处理

- `400/404`：业务错误（修订/方案/排程不存在等），中文 `detail`；
- `409 Conflict`：资料修订过期/不一致、重复确认、并发槽位占用冲突，原方案不变；**方案未确认或已被取代时编排排程**、排程保存前复核方案已非已确认状态；
- `422`：① 录入校验失败（非法重量、重复箱号、无效槽位、重复槽位编号、力矩区间倒置、非正岸桥数/吊装时长、负时间窗、单箱时间窗无解等）；② 无完整可行方案或**排程时间窗无解、排程箱项缺失/多余**（**不保存**）。

## 九、请求示例（curl）

```bash
BASE=http://127.0.0.1:8000/api

# 1) 录入槽位资料（得到 slots_revision）
curl -s -X POST $BASE/slot-revisions -H 'Content-Type: application/json' -d '{
  "slots": [
    {"code":"A01","stack":"A","tier":1,"capacity":100,"arm":2.0},
    {"code":"A02","stack":"A","tier":2,"capacity":100,"arm":3.0},
    {"code":"B01","stack":"B","tier":1,"capacity":100,"arm":2.0},
    {"code":"B02","stack":"B","tier":2,"capacity":100,"arm":3.0}
  ],
  "adjacency": [["A01","B01"],["A02","B02"]],
  "incompatible_classes": [["1","5"]],
  "min_moment": -100000,
  "max_moment": 100000
}'

# 2) 录入箱清单（得到 manifest_revision）
curl -s -X POST $BASE/manifest-revisions -H 'Content-Type: application/json' -d '{
  "containers": [
    {"no":"C002","weight":20,"hazmat":null,"discharge_order":1},
    {"no":"C001","weight":10,"hazmat":null,"discharge_order":1}
  ]
}'

# 3) 核算方案（可省略修订号，默认取双方最新修订；无解返回 422 且不保存）
curl -s -X POST $BASE/plans/compute -H 'Content-Type: application/json' -d '{}'
# -> assignments: C001->A01, C002->B01；total_moment=60；revision_no=1

# 4) 确认方案（核对资料修订；过期/冲突返回 409，原方案不变）
curl -s -X POST $BASE/plans/<plan_id>/confirm \
  -H 'Content-Type: application/json' \
  -d '{"slots_revision":1,"manifest_revision":1}'

# 5) 资料更新后修订并重新核算（生成 revision_no=2 的草稿，原确认方案暂保持）
curl -s -X POST $BASE/plans/<plan_id>/amend \
  -H 'Content-Type: application/json' -d '{}'

# 6) 查询
curl -s $BASE/plans
curl -s $BASE/plans/<plan_id>

# 7) 对已确认方案编排装船排程（箱项与方案一致；时间窗无解返回 422 且不保存）
curl -s -X POST $BASE/plans/<plan_id>/schedules \
  -H 'Content-Type: application/json' \
  -d @examples/schedule.json

# 8) 查询排程：当前排程 / 某方案全部排程（含历史）/ 单个排程
curl -s "$BASE/schedules?status=current"
curl -s "$BASE/schedules?plan_id=<plan_id>"
curl -s $BASE/schedules/<schedule_id>
```

排程请求体与响应示例：

```json
// 请求
{
  "cranes": 2,
  "tasks": [
    {"no": "C001", "duration": 3, "earliest_start": 0,  "latest_finish": 60},
    {"no": "C002", "duration": 4, "earliest_start": 0,  "latest_finish": 60},
    {"no": "C003", "duration": 2, "earliest_start": 10, "latest_finish": 60},
    {"no": "C004", "duration": 5, "earliest_start": 0,  "latest_finish": 60}
  ]
}

// 响应（201）
{
  "schedule_id": "43fd68f9...",
  "plan_id": "<plan_id>",
  "status": "current",
  "cranes": 2,
  "makespan": 12,
  "root_id": "<plan_id>",
  "plan_revision_no": 1,
  "slots_revision": 1,
  "manifest_revision": 1,
  "tasks": [
    {"container_no": "C001", "stack": "A", "tier": 2, "start": 4,  "finish": 7,  "crane": 1},
    {"container_no": "C002", "stack": "A", "tier": 1, "start": 0,  "finish": 4,  "crane": 1},
    {"container_no": "C003", "stack": "B", "tier": 2, "start": 10, "finish": 12, "crane": 1},
    {"container_no": "C004", "stack": "B", "tier": 1, "start": 0,  "finish": 5,  "crane": 2}
  ],
  "created_at": "2026-10-01T00:00:00+00:00"
}
```

空箱方案对已确认方案提交 `{"cranes": 2, "tasks": []}`，返回 `tasks: []`、`makespan: 0`。

方案响应示例：

```json
{
  "plan_id": "2f1e529a...",
  "revision_no": 1,
  "status": "confirmed",
  "slots_revision": 1,
  "manifest_revision": 1,
  "total_moment": 60.0,
  "max_occupied_tier": 1,
  "assignments": [
    {"container_no": "C001", "slot_code": "A01", "stack": "A", "tier": 1},
    {"container_no": "C002", "slot_code": "B01", "stack": "B", "tier": 1}
  ],
  "parent_id": null,
  "root_id": "2f1e529a...",
  "created_at": "...",
  "confirmed_at": "..."
}
```

更多现成请求体见 `examples/` 目录（含装船排程 `schedule.json`），可直接：

```bash
curl -s -X POST $BASE/slot-revisions    -H 'Content-Type: application/json' -d @examples/slots.json
curl -s -X POST $BASE/manifest-revisions -H 'Content-Type: application/json' -d @examples/containers.json
```

## 十、测试

```bash
pip install pytest httpx
PYTHONPATH=. pytest -q
```

测试覆盖：配载求解器全部约束与优选规则、录入校验（非法重量/重复箱号/无效槽位）、无解不落库、过期修订拒绝、方案修订链、并发确认互斥；以及**装船排程求解器**（同桥非重叠、堆栈上下层先后、时间窗边界、贪心失败回退 MILP、空排程、makespan 最优与按箱号 `(开工,岸桥)` 字典序最小）和**排程 API**（未确认/已取代拒绝、箱项缺失多余拒绝、时间窗无解不保存、重排使旧排程转历史、修订确认联动、空箱方案空排程），共 50 个用例。

## 十一、目录结构

```
app/
  config.py      # 环境变量配置
  database.py    # 引擎/会话/建表，SQLite WAL、外键、busy_timeout
  models.py      # ORM：资料修订、方案、方案项、已确认占用、排程、排程项
  schemas.py     # Pydantic 模式与录入校验（含排程入参/出参）
  solver.py      # 配载 MILP 建模与两阶段优选
  scheduler.py   # 装船排程 MILP：makespan 最优 + (开工,岸桥) 字典序最小
  services.py    # 业务逻辑、并发安全确认、排程保存前复核与历史标记
  routers.py     # HTTP 路由
  main.py        # FastAPI 入口
scripts/entrypoint.sh  # 容器初始化入口
tests/           # 单元/接口/并发测试
examples/        # 请求示例（含 schedule.json）
Dockerfile / docker-compose.yml / requirements.txt
```
