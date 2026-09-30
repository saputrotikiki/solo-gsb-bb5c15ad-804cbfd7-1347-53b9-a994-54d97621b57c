# 集装箱船配载方案核算服务

基于 Python + FastAPI + SQLAlchemy(SQLite) + PuLP(CBC 整数规划求解器) 实现的集装箱船配载方案核算 HTTP 服务。

录入按**堆栈（stack）与层（tier）**编号的槽位、各槽承重、纵向力臂、相邻槽位关系与全船允许力矩区间，以及箱号、重量、危险类别、卸港先后与不相容危险类别对；服务输出**每箱槽位、总力矩与方案修订号**。

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

## 四、启动

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

## 五、配置

全部配置通过环境变量覆盖（前缀 `STOWAGE_`，也可用 `.env`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STOWAGE_PORT` | `8000` | Compose 映射到宿主机的端口（仅 compose 用） |
| `STOWAGE_DATABASE_URL` | `sqlite:///./data/stowage.db` | SQLAlchemy 数据库地址；容器内固定 `sqlite:////app/data/stowage.db` |
| `STOWAGE_API_PREFIX` | `/api` | API 路由前缀 |

## 六、地址与端口

- 服务地址（本地）：`http://127.0.0.1:8000`
- 健康检查：`GET http://127.0.0.1:8000/health`
- API 前缀：`http://127.0.0.1:8000/api`
- 交互式文档（Swagger UI）：`http://127.0.0.1:8000/docs`
- OpenAPI：`http://127.0.0.1:8000/openapi.json`

## 七、HTTP API

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

### 错误处理

- `400/404`：业务错误（修订不存在等），中文 `detail`；
- `409 Conflict`：资料修订过期/不一致、重复确认、并发槽位占用冲突，原方案不变；
- `422`：① 录入校验失败（非法重量、重复箱号、无效槽位、重复槽位编号、力矩区间倒置等）；② 无完整可行方案（**不保存**）。

## 八、请求示例（curl）

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
```

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

更多现成请求体见 `examples/` 目录，可直接：

```bash
curl -s -X POST $BASE/slot-revisions    -H 'Content-Type: application/json' -d @examples/slots.json
curl -s -X POST $BASE/manifest-revisions -H 'Content-Type: application/json' -d @examples/containers.json
```

## 九、测试

```bash
pip install pytest httpx
PYTHONPATH=. pytest -q
```

测试覆盖求解器全部约束与优选规则、录入校验（非法重量/重复箱号/无效槽位）、无解不落库、过期修订拒绝、方案修订链以及并发确认互斥（25 个用例）。

## 十、目录结构

```
app/
  config.py      # 环境变量配置
  database.py    # 引擎/会话/建表，SQLite WAL、外键、busy_timeout
  models.py      # ORM：资料修订、方案、方案项、已确认占用
  schemas.py     # Pydantic 模式与录入校验
  solver.py      # MILP 建模与两阶段优选（核心）
  services.py    # 业务逻辑与并发安全确认
  routers.py     # HTTP 路由
  main.py        # FastAPI 入口
scripts/entrypoint.sh  # 容器初始化入口
tests/           # 单元/接口/并发测试
examples/        # 请求示例
Dockerfile / docker-compose.yml / requirements.txt
```
