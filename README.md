# 修复审批覆盖导致的放行审计丢失基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

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

## 质量准入决定链（silicon_qualification）

准入决定按不可变事件链管理，修复了"同一批次暂缓后再放行覆盖原决定"的缺陷：

- `analyze` 对当前全部测量生成**内容寻址的分析版本**（按测量快照 SHA-256 去重），每次决定必须引用一个分析版本；
- `POST /lots/{id}/decisions` 只追加、不覆盖：决定带批次内序号、`prev_decision_id`、当时的批次版本和分析版本；
- 批次以 `revision` 乐观锁约束后续动作（`expected_revision` 不匹配返回 409），状态机为
  `engineering → pending_review → hold/rejected → in_review → released …`（已放行不可复议）；
- 需要重新评审时必须先 `POST /lots/{id}/review-requests` 显式发起复议，复议决定须引用**更新的分析版本**、
  且由**上一决定人之外**的授权人员作出；
- 决定与复议请求都要求 `Idempotency-Key`：相同编号+相同载荷返回原结果，相同编号+不同载荷返回 409；
- `GET /lots/{id}/report` 同时返回 `current_decision`（当前有效决定）与 `decision_chain`（完整决定链），
  另有 `/decisions`；记录全部落 SQLite，进程重启后暂缓→复议→放行的先后关系仍可经审计事件追溯。

