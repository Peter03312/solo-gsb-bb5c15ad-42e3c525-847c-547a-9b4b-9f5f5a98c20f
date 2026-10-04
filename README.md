# 电影节片源放映许可核验服务

基于 **Python + FastAPI + SQLite** 的放映许可核验服务：录入影片/场馆授权（有效时段、场次上限、撤销记录），对待核验场次进行**幂等、并发安全**的准入判定，返回准入结果、命中授权、剩余额度与拒绝原因。

## 功能特性

- **授权管理**：录入影片、场馆、有效时段、场次上限；查询实时已用/剩余额度
- **核验判定**：多份授权同时有效时只消耗一份额度，**优先最早到期且有余额**的授权
- **并发安全**：核验的"查额度 + 扣额度"在单个 `BEGIN IMMEDIATE` 事务内完成，SQLite WAL 模式下写请求串行化，**并发核验绝不超额**
- **幂等语义**：同一业务幂等键重试返回原结果（`replay=true`，不重复扣额度）；键相同但请求内容变化返回 **409 冲突**
- **撤销语义**：撤销仅阻止后续核验；已核准场次记录保留、可追溯
- **明确拒绝**：时间不在任何授权时段、授权已撤销、额度用尽、无授权时分别返回具体拒绝原因

## 快速开始

### 方式一：Docker Compose 一键启动（推荐）

```bash
docker compose up --build -d
```

启动即自动完成建表初始化；`SEED_DEMO_DATA=1`（compose 默认开启）时，空库会写入两条演示授权，可直接体验核验流程。

- 服务地址：http://localhost:8000
- 交互式接口文档：http://localhost:8000/docs
- 健康检查：`curl http://localhost:8000/health`

数据持久化在命名卷 `licensing-data`（容器内 `/data/filmfest.db`）。停止并清理：

```bash
docker compose down        # 保留数据卷
docker compose down -v     # 连同数据一起删除
```

### 方式二：本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pytest -q        # 15 个用例，含 20 线程并发不超额的验证
```

### 调用示例脚本

服务启动后执行完整演示（录入 → 核验 → 幂等重放 → 冲突 → 撤销 → 追溯）：

```bash
bash examples/demo.sh
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FESTIVAL_DB_PATH` | `./filmfest.db`（容器内 `/data/filmfest.db`） | SQLite 数据库文件路径 |
| `PORT` | `8000` | 服务监听端口（容器内） |
| `SEED_DEMO_DATA` | 关闭 | 设为 `1` 时，空库启动写入两条演示授权 |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/licenses` | 录入授权（影片、场馆、有效时段、场次上限） |
| GET | `/licenses` | 授权列表（含已用/剩余额度、撤销状态），可按 `film_id`/`venue_id` 过滤 |
| GET | `/licenses/{id}` | 单条授权详情 |
| POST | `/licenses/{id}/revoke` | 撤销授权（幂等保护：重复撤销返回 409） |
| POST | `/verifications` | 核验场次（幂等） |
| GET | `/verifications` | 核验流水，可按 `film_id`/`venue_id`/`decision`/`license_id` 过滤 |
| GET | `/verifications/{idempotency_key}` | 按幂等键查询单条核验记录 |

### 调用示例（curl）

录入授权：

```bash
curl -X POST http://localhost:8000/licenses \
  -H 'Content-Type: application/json' \
  -d '{"film_id":"film-autumn-river","venue_id":"hall-1",
       "valid_from":"2026-10-01T00:00:00Z","valid_to":"2026-10-31T23:59:59Z",
       "max_sessions":5,"note":"主竞赛单元"}'
```

核验场次：

```bash
curl -X POST http://localhost:8000/verifications \
  -H 'Content-Type: application/json' \
  -d '{"film_id":"film-autumn-river","venue_id":"hall-1",
       "start_time":"2026-10-05T19:30:00Z",
       "idempotency_key":"screening-0001"}'
```

准入响应（命中授权 3，本次消耗后剩余 4 场）：

```json
{
  "id": 1,
  "idempotency_key": "screening-0001",
  "film_id": "film-autumn-river",
  "venue_id": "hall-1",
  "start_time": "2026-10-05T19:30:00.000000Z",
  "decision": "approved",
  "license_id": 3,
  "reject_reason": null,
  "remaining_quota": 4,
  "verified_at": "2026-10-04T08:00:00.000000Z",
  "replay": false
}
```

拒绝响应：

```json
{
  "decision": "rejected",
  "license_id": null,
  "reject_reason": "QUOTA_EXHAUSTED",
  "remaining_quota": null,
  "replay": false
}
```

撤销授权：

```bash
curl -X POST http://localhost:8000/licenses/3/revoke \
  -H 'Content-Type: application/json' -d '{"reason":"版权方临时撤片"}'
```

## 核验规则说明

**授权选择**：取该 `影片+场馆` 的全部授权，按 `valid_to` 升序遍历，命中第一个"覆盖开始时间、未撤销、剩余额度 > 0"的授权——即最早到期且有余额者；一次核验只消耗一份额度（`remaining_quota` 为消耗后的剩余值）。

**幂等**：`idempotency_key` 全库唯一。重试同一键且请求内容（影片、场馆、开始时间）一致 → 返回首次存储的结果并标记 `replay=true`，不重复扣额度；内容不一致 → `409` 并在 `existing` 字段附上首次结果。

**撤销**：撤销记录独立存表。被撤销的授权不再参与后续核验；撤销前已核准的场次记录原样保留，可通过 `/verifications` 与 `/verifications/{key}` 追溯。

**并发**：所有核验在 `BEGIN IMMEDIATE` 事务内完成"查重 → 选授权 → 扣额度 → 落库"，SQLite 写锁将并发请求串行化；WAL 模式 + 30s busy_timeout 保证读写不互相阻塞。测试以 20 线程并发抢 3 场额度验证：恰好 3 条 approved，其余均为 `QUOTA_EXHAUSTED`。

**拒绝原因**（`reject_reason`）：

| 值 | 含义 |
|---|---|
| `NO_LICENSE` | 该影片+场馆没有任何授权 |
| `TIME_OUT_OF_WINDOW` | 有授权，但开始时间不在任何有效时段内 |
| `LICENSE_REVOKED` | 覆盖该时段的授权均已撤销 |
| `QUOTA_EXHAUSTED` | 授权有效但场次额度已用完 |

**时间处理**：请求时间按 ISO-8601 解析，不带时区按 UTC 处理；无法解析返回 422。库内统一存定长 UTC 字符串，保证比较即时间序。

## 项目结构

```
app/
  main.py       # FastAPI 入口与路由（create_app 工厂）
  service.py    # 业务逻辑：授权/撤销/幂等核验/授权选择
  db.py         # SQLite 连接与建表（WAL、外键、busy_timeout）
  schemas.py    # Pydantic 请求/响应模型
  timeutil.py   # UTC 时间规范化
tests/
  test_api.py   # API 端到端测试 + 并发不超额测试
examples/
  demo.sh       # 完整调用示例脚本
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
