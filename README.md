# Ontology Shopping Agent

一个面向“知识图谱问答 + 业务动作执行”的演示项目。它把 MySQL 中的订单数据与表结构同步到 Neo4j，基于本体结构回答自然语言问题，并在用户发起下单、支付、推进订单阶段、商品改价、补库存等动作后，把最新业务数据再次回流到图谱中。

这个仓库目前由 3 个 Python 服务组成：

- `ontology-etl`：从 MySQL 读取表结构和实例数据，写入 Neo4j。
- `ontology-query-service`：提供自然语言查询、知识库检索、Agent 推理、动作卡片和执行入口。
- `order-biz-service`：提供订单与商品相关的真实业务接口。

## Why This Project

这个项目的核心价值不在“用 LLM 调接口”，而在于做了一个完整闭环：

1. 用 MySQL 作为真实业务事实源。
2. 用 Neo4j 存储“本体结构 + Demo 实例 + 可执行动作定义”。
3. 用规则规划器、LLM 规划器和 ReAct Agent 把自然语言映射成图查询或业务动作。
4. 动作执行成功后再触发 ETL 增量同步，让图谱保持接近实时。

这套设计适合拿来展示下面几类能力：

- 结构化知识建模能力。
- 自然语言到结构化查询/动作的映射能力。
- 读写分离与多存储协作能力。
- Agent 在“查信息”和“做动作”之间的边界控制能力。

## Repository Layout

```text
.
├── ontology-etl/
│   ├── config.py
│   ├── main.py
│   ├── incremental_sync.py
│   ├── mysql_reader.py
│   ├── neo4j_writer.py
│   └── relation_extractor.py
├── ontology-query-service/
│   ├── app.py
│   ├── dao/
│   ├── service/
│   └── web/
├── order-biz-service/
│   ├── app.py
│   ├── dao/
│   └── service/
├── docs/
│   └── 面试资料/
└── re.txt
```

## Architecture

```text
MySQL
  ├─ t_user / t_product / t_order / t_order_item / t_stage
  │
  ▼
ontology-etl
  ├─ 读取 information_schema 生成 Ont_Entity / Ont_Attribute / Ont_Relation
  ├─ 导入 Demo_User / Demo_Order / Demo_Product / Demo_Stage / Demo_OrderItem
  └─ 写入 Ont_Action（业务动作定义 + UI Schema）
  │
  ▼
Neo4j
  ├─ Ont_*     -> 本体结构
  ├─ Demo_*    -> 实例数据
  └─ Ont_Action -> 动作元数据
  │
  ▼
ontology-query-service
  ├─ NLQPlanner / LLMPlanner -> 识别查询意图或执行意图
  ├─ QueryService            -> 固定模板 Cypher 查询
  ├─ ChatService             -> 回答生成、动作执行、ETL 回流
  ├─ ReActAgent              -> 多工具推理
  └─ ActionExecutor          -> 调用 order-biz-service
  │
  ▼
order-biz-service
  ├─ 创建订单
  ├─ 支付订单
  ├─ 推进阶段
  ├─ 商品列表 / 详情
  └─ 补库存 / 改价 / 上下架
```

## Tech Stack

- Python 3.11+ 推荐
- Flask
- PyMySQL
- Neo4j Python Driver
- MySQL 8.x
- Neo4j 5.x
- OpenAI-compatible LLM API（示例使用 DeepSeek）

## Environment

`ontology-query-service` 和 `order-biz-service` 会从各自目录下的 `.env` 读取配置。下面是从 `re.txt` 整理出的最小可用配置示例：

```env
LLM_PROVIDER=openai_compat
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=
LLM_MODEL=deepseek-chat
LLM_TEMPERATURE=0.2

NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=12345678
NEO4J_DATABASE=ontology

MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=123456
MYSQL_DATABASE=ontology2

SQL_ALLOWED_TABLES=t_user,t_order,t_stage

FLASK_HOST=127.0.0.1
FLASK_PORT=5000

BIZ_BASE_URL=http://127.0.0.1:18080

ETL_SYNC_ENABLED=1
ETL_SYNC_TIMEOUT=60
# ETL_SYNC_FULL=1
# ETL_DIR=/absolute/path/to/ontology-etl
# ETL_SYNC_SCRIPT=/absolute/path/to/ontology-etl/incremental_sync.py

KB_VECTOR_ENABLED=1

EXEC_AGENT_PREFILL=1
EXEC_AGENT_INTERPRET=1
EXEC_AGENT_PREFILL_MODE=react
```

说明：

- `ontology-query-service/app.py` 会读取本目录的 `.env`。
- `order-biz-service/app.py` 也会读取本目录的 `.env`。
- `ontology-etl` 当前不读取 `.env`，而是直接读取 `config.py` 中的常量。
- 如果你使用的是 Neo4j Community，通常需要把 `NEO4J_DATABASE` 改成 `neo4j`。

## Quick Start

建议按下面顺序启动。

### 1. 启动基础依赖

- 启动 MySQL，并准备业务库 `ontology2`。
- 启动 Neo4j，并确认能访问 `NEO4J_DATABASE`。

### 2. 启动 ETL

首次建议先同步结构，再同步实例：

```bash
cd ontology-etl
python main.py --schema-only
python incremental_sync.py --full
```

如果你想一次性导入结构、动作和全表实例，也可以直接执行：

```bash
cd ontology-etl
python main.py
```

后续增量同步：

```bash
cd ontology-etl
python incremental_sync.py
```

`re.txt` 里还保留了 Windows 环境下的原始启动命令，当前 `README` 已将其转换为仓库内相对路径的启动方式。

### 3. 启动订单业务服务

```bash
cd order-biz-service
python -m pip install -r requirements.txt
python app.py
```

默认监听：

- `http://127.0.0.1:6000`，或由 `BIZ_HOST` / `BIZ_PORT` 覆盖。

注意：`re.txt` 里给出的 `BIZ_BASE_URL` 是 `http://127.0.0.1:18080`，如果你沿用这个配置，需要让业务服务也监听 `18080`，否则查询服务调用动作时会失败。

### 4. 启动查询服务

```bash
cd ontology-query-service
python -m pip install -r requirements.txt
python app.py
```

默认监听：

- `http://127.0.0.1:5000`

访问首页：

- [http://127.0.0.1:5000/](http://127.0.0.1:5000/)

## Typical Flows

### 1. 自然语言查本体

示例问题：

- `Order 有哪些字段`
- `Order 和 User 什么关系`
- `Order 有哪些动作`

处理方式：

1. `NLQPlanner` 先识别意图。
2. `QueryService` 选择固定 Cypher 模板查询 `Ont_Entity`、`Ont_Relation`、`Ont_Action`。
3. `ChatService` 把结构化结果转换成自然语言答案。

### 2. 查实例数据

示例问题：

- `Alice 有哪些订单`
- `订单 101 的阶段`
- `订单 101 商品明细`

处理方式：

1. 先从 Neo4j 的 `Demo_*` 节点上查。
2. 查询模板在 `QueryService` 中固定实现，不让模型直接生成 Cypher。
3. 如果开启知识库功能，回答时会把 `kb_hits` 也纳入参考。

### 3. 执行业务动作

示例指令：

- `给 Alice 创建订单`
- `支付订单 101`
- `把商品 3 改价到 199`

处理方式：

1. 规划器识别为 `execute`。
2. `ActionExecutor` 先从 Neo4j 的 `Ont_Action` 中解析 `apiPath` 和 `method`。
3. 再调用 `order-biz-service` 的真实接口。
4. 成功后由 `ChatService` 触发 `ontology-etl/incremental_sync.py`，把 MySQL 最新变更同步回 Neo4j。

## Key Endpoints

查询服务：

- `POST /query`：按指定 `queryType` 和 `name` 查询。
- `POST /ask`：规则规划 + 图查询。
- `POST /chat`：规则/LLM 规划 + 回答生成。
- `POST /smart`：带动作卡片、预填充和 Agent 行为控制的统一入口。
- `POST /ui/plan`：只做动作规划，返回表单卡片。
- `POST /ui/execute`：执行确认后的动作。
- `POST /kb/upsert`：向知识库写文档。
- `POST /kb/search`：知识库检索。
- `POST /react`：显式走 ReAct Agent。

业务服务：

- `POST /api/order/create`
- `POST /api/order/pay`
- `POST /api/order/stage/advance`
- `GET /api/product/list`
- `GET /api/product/list_admin`
- `GET /api/product/detail`
- `POST /api/product/restock`
- `POST /api/product/price/update`
- `POST /api/product/status/update`

## 面试资料

为了方便快速理解项目和准备面试，仓库新增了 `docs/面试资料`：

- `导航.md`：说明整套面试资料怎么阅读，避免和项目根目录 `README` 混淆。
- `项目总览.md`：从业务目标、系统边界和执行链路解释项目。
- `核心实现.md`：拆解 ETL、查询、知识库、Agent、动作执行与回流同步。
- `面试指南.md`：面试表达、考点、亮点和不足点。
- `代码审查.md`：基于当前代码的审查报告与改进建议。

## Known Gaps

当前代码更偏“演示工程”而不是“可直接生产”的系统，已知缺口包括：

- ETL 配置仍写死在 `ontology-etl/config.py`。
- 订单创建、支付、推进阶段缺少事务与状态机约束。
- 多处规则和正则逻辑分散在不同模块，维护成本较高。
- 缺少自动化测试与一键初始化脚本。

这些问题我已经在 `docs/面试资料/代码审查.md` 里按严重级别展开说明。
