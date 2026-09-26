# 修复审批覆盖导致的放行审计丢失基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 质量审批决定链（silicon_qualification）

质量审批不再覆盖历史，而是形成只追加、不可变的决定链：

- 每次 `analyze` 产生一个内容寻址的**分析版本**（输入测量相同时复用同一版本），并推进批次 `revision`；
- 首次审批决定引用当前分析版本即可；此后任何新决定都必须先 `POST /lots/{lot}/reviews` **显式发起复议**，由**不同于上一决定人**的授权质量人员引用**新的分析版本**作出，批次进入 `in_review`，决定后复议标记为 `consumed`；
- 决定与复议均带批次 `revision`，写操作可用 `expected_revision` 做乐观并发控制，批次当前状态约束后续动作（已放行/拒收的批次必须经复议才能再决定）；
- 审批支持 `Idempotency-Key`：同键同载荷的重复请求返回原结果（HTTP 200，`replayed=true`），同键不同载荷返回 409 冲突，绝不重复落库；
- `GET /lots/{lot}/approvals` 同时返回**当前有效决定**（`current_decision`）与**完整决定链**（`decision_chain`，每条决定带有所引用分析版本与复议编号）；
- 全部记录持久化在 SQLite，进程重启后暂缓、复议、放行的先后顺序仍可通过 `seq`、`review_id` 与审计事件完整追溯；旧版只存“最后状态”的 `approvals` 表在打开数据库时自动迁移为决定链起点。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析和芯片准入流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。
