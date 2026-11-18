# bug-11 复现与处置说明

**现象**：银区已闭合，但铜区依赖仍在收集，解析链路在依赖门禁提前掐断。

种子库即复现态：`silver_orders`（订单银区，2026-11-12）已闭合，依赖
`bronze_orders`（订单铜区，2026-11-14）仍为收集中，编排任务停在「依赖门禁 / 待处置」。

## 复现（本地秒级）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py            # http://127.0.0.1:6900/login  admin / 123456
```

1. 打开「依赖卡片」：`订单银区 ← 订单铜区`，芯片 `待处置`，依赖对齐显示「未对齐（银闭铜未齐）」。
2. 打开「批次详情」→ 订单银区：同样出现「银区详情 · 依赖门禁」卡片，按钮「整笔回滚银区 / 隔离缺口放行」。
3. 打开「解析编排台」：任务 #1 阶段「依赖门禁」，状态 `待处置`。
4. 跑「可见性对账」：摘要含「依赖缺口 1」，`错位` 计数 ≥ 1。

命令行走一遍：

```bash
curl -c /tmp/c -s -X POST localhost:6900/api/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"123456"}'
curl -b /tmp/c -s localhost:6900/api/dependencies
```

## 处置提交边界（只能定一边）

对同一依赖 `POST /api/dependencies/1/dispose`，`mode` 二选一：

### A. 整笔回滚 `rollback`

```bash
curl -b /tmp/c -s -X POST localhost:6900/api/dependencies/1/dispose \
  -H 'Content-Type: application/json' \
  -d '{"mode":"rollback","expected_version":1}'
```

结果：银区 `收集中`、`closed_object_count=NULL`、两个对象退回 `已登记`，
`close_runs.ok=0`；铜区继续收集。芯片变 `回滚`，凭证 `VCH-…` 落库。

### B. 隔离缺口放行 `quarantine`

```bash
curl -b /tmp/c -s -X POST localhost:6900/api/dependencies/1/dispose \
  -H 'Content-Type: application/json' \
  -d '{"mode":"quarantine","expected_version":1}'
```

结果：银区保持 `已闭合`，铜区缺口被登记为隔离，写本机隔离凭证。
芯片变 `隔离`；`invariant_ok=true`（缺口有凭证收口）。

**两种模式不要混用**：先成功的那笔决定铜区命运与银区是否保持已闭合。
再点另一笔返回 `409 依赖已处置`。

## 对齐与拦截

- 回滚后银区变收集中；铜区未齐套时点「闭合清单」返回
  `409 上游铜区未齐套，禁止闭合`。
- 给铜区补登记对象并闭合（齐套）后，银区可再次闭合，`close_runs` 就地更新。
- 齐套后对账 `错位` 归零，摘要不再出现「依赖缺口」。

## 并发：两名编排员齐点

处置是单写事务（`BEGIN IMMEDIATE`）+ 下游 `version` 乐观校验。
同时提交 rollback 与 quarantine，只有一笔 200，另一笔 409；
`/api/dispositions` 只有一张凭证，不会双写银区态。

```bash
# 并发验证见 tests/test_dependencies.py::test_concurrent_dispose_only_one_wins
pytest -q tests/test_dependencies.py
```

## 四处一致

依赖卡片、批次详情（银区/铜区门禁）、解析编排台、可见性对账读同一份依赖状态；
芯片与凭证来自 `dispositions` 落库记录，前端不推断、不硬编码。
「解析编排回看」列出的每张凭证，都能在「本机库存证」按凭证号对上。
