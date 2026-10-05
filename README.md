# 场馆安全事件协同

本项目维护场馆安全事件协同的领域约定、角色边界与样例数据，并提供一个零第三方依赖的 Python 服务端，统一落实四条领域不变量。

## 领域约定

- **角色**：文博中心运营员、志愿者、监护人、场馆负责人（另含安全员、学校负责人）。
- **事件状态**：草拟 → 待核验 → 已确认 → 执行中 → 已归档（复开回到执行中）。
- **不变量**：
  - **事件来源合并**：场馆、安全员、学校可分别建档；同地点 24 小时内的事件会提示潜在重复，合并后各来源报告逐条保留，被并事件的访问自动导向主事件。
  - **敏感信息分层**：健康字段（伤情、就医记录）与身份/联系方式按角色裁剪；普通志愿者只见脱敏姓氏、无联系方式、无健康字段、无来源正文；监护人仅可见本人监护对象的完整信息。
  - **措施任务依赖**：临时措施记录处置依据；标记完成必须给出结论依据并可关联证据；结案前必须没有未完成的责任任务。
  - **逾期时钟调度**：任务截止时间配合可注入时钟，产生「即将到期 / 已逾期」提醒，每个任务每类只提醒一次。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/safety_service/`：协同服务端。
  - `clock.py`：`SystemClock` / `FixedClock`（可注入时钟）。
  - `models.py`：事件、来源、人员、证据、时间线、措施、任务、核验申请。
  - `repository.py`：线程安全内存仓库。
  - `service.py`：建档、合单、四眼核验、提醒扫描、工作流。
  - `serializers.py`：按角色裁剪身份与健康字段。
  - `scheduler.py`：逾期提醒周期调度（也可 `run_once` 确定性触发）。
  - `app.py`：标准库 `http.server` HTTP API 与启动入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域服务、HTTP 端到端测试。

## 运行

```bash
PYTHONPATH=src python3 -m safety_service.app --host 127.0.0.1 --port 8080
```

请求通过 `X-User-Id` 头标识用户，预置账号：

| X-User-Id | 角色 |
| --- | --- |
| `u-op-1` | 文博中心运营员 |
| `u-vol-1` | 志愿者 |
| `u-guard-1` | 监护人 |
| `u-mgr-1` | 场馆负责人 |
| `u-safe-1` | 安全员 |
| `u-school-1` | 学校负责人 |

## 主要 API

- `POST /api/incidents` 建档（响应含 `duplicate_candidates` 潜在重复提示）
- `POST /api/incidents/merge` 合单（`source_id` → `target_id`，来源全部保留）
- `GET /api/incidents/{id}` 事件全貌：时间线、人员、证据、措施、任务、核验记录（按角色裁剪）
- `POST /api/incidents/{id}/reports` 追加来源报告
- `POST /api/incidents/{id}/people|evidence|measures|tasks` 登记人员/证据/措施/任务
- `POST /api/incidents/{id}/measures/{mid}/resolve` 标记措施完成，必填 `completion_note`，可附 `basis_evidence_ids`
- `POST /api/incidents/{id}/submit` / `confirm` / `start`：提交核验（确认人不能是提报人）
- `POST /api/incidents/{id}/verifications` 发起升级/转交/结案/复开申请
- `POST /api/verifications/{rid}/decide` 独立核验（核验人不能是申请人本人）
- `GET /api/reminders/due` 按当前时钟扫描临期/逾期任务并产生提醒

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
