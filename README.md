# 场馆安全事件协同

本项目维护场馆安全事件协同的领域约定、角色边界与样例数据，并提供零三方依赖（仅标准库，`requires-python >=3.11`）的 Python 服务端，供接口和自动化验证统一使用。

## 背景

学生团队活动中发生轻微受伤后，场馆、安全员、学校分别建档，容易出现：后续措施与原事件无法对应、敏感健康信息被普通志愿者看到。服务围绕领域契约的四条不变量设计：

- **事件来源合并**：重复报告可合并，但每条来源（建档渠道、建档人、原编号、外部单号）完整保留。
- **敏感信息分层**：身份字段与健康字段显式分层，按角色裁剪，志愿者两类均不可见。
- **措施任务依赖**：措施必须有采取依据、完成必须有完成依据；任务必须有依据且不可在未完成时结案。
- **逾期时钟调度**：所有时间判断走可注入时钟，逾期按阶梯（逾期当下 / +24h / +72h）产生幂等提醒。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/safety_incidents/`：服务端
  - `clock.py`：时钟抽象（`SystemClock` / 测试用 `FixedClock`）。
  - `models.py`：事件、来源、人员、证据、措施、任务、核验申请等实体。
  - `permissions.py`：角色边界与身份/健康字段可见性。
  - `service.py`：状态机、报告合并、独立核验、措施任务、逾期提醒。
  - `serializers.py`：按观看者裁剪的只读视图。
  - `api.py` / `__main__.py`：标准库 `http.server` JSON API 与启动入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域规则与 HTTP API 端到端测试。

## 角色与字段分层

| 角色 | 建档渠道 | 身份字段（姓名/联系方式） | 健康字段（伤情/医疗处置） | 处置 | 核验 |
|---|---|---|---|---|---|
| 场馆负责人 | 场馆/安全员 | 可见 | 可见 | 是 | 是（独立核验人） |
| 文博中心运营员 | 场馆/安全员 | 可见 | 可见 | 是 | 可申请 |
| 监护人 | 学校 | 仅本人监护学生 | 仅本人监护学生 | 否 | 否 |
| 志愿者 | 不可建档 | 不可见（仅角色标签） | 不可见 | 否 | 否 |

升级、转交、复开、结案四类动作必须发起核验申请，由**另一名**场馆负责人独立核验：申请人本人不能作为核验人；驳回不产生任何状态变化；结案时若存在未完成责任任务则核准失败；核验记录（申请人、核验人、时间、理由、结论）全程留痕。

## 运行

```bash
PYTHONPATH=src python3 -m safety_incidents
# http://127.0.0.1:8080，演示账号通过请求头 X-User-Id 传递：
# u-venue-op / u-volunteer / u-guardian / u-manager / u-officer / u-school
```

主要接口（均以 `/incidents/...` 为前缀，写操作为 POST，JSON 请求体）：

| 路径 | 说明 |
|---|---|
| `POST /incidents/report` | 场馆/安全员/学校建档（`reporter_org`） |
| `POST /incidents/merge` | 合并重复建档（`primary_id`/`duplicate_id`，来源保留） |
| `POST /incidents/{id}/submit` `/confirm` `/start` | 草拟 → 待核验 → 已确认 → 执行中 |
| `POST /incidents/{id}/people` `/evidence` `/measures` `/tasks` | 人员/证据/临时措施/责任任务 |
| `POST /incidents/{id}/measures/{m}/complete` | 措施完成（必填 `completion_basis`） |
| `POST /incidents/{id}/tasks/{t}/start` `/complete` | 任务执行与完成（完成必填依据） |
| `POST /incidents/{id}/verifications` | 申请升级/转交/复开/结案 |
| `POST /verifications/{vid}/decision` | 独立核验决定（`approve`/`decision_note`） |
| `POST /system/sweep-reminders` | 按注入时钟扫描逾期任务并产生提醒 |
| `GET /incidents` `/incidents/{id}` | 列表/详情，响应按 `X-User-Id` 角色裁剪 |

措施视图明确返回 `completed`、`completed_at`、`completed_by`、`completion_basis` 及采取时的 `basis`，任务视图返回 `overdue` 与历次 `reminders`；每个事件都带完整 `timeline`（建报/合并/状态变更/核验/措施/任务/逾期提醒）。

## 验证

```bash
python3 -m unittest discover -s tests -v        # 契约 + 领域规则 + API，共 18 项
python3 -m compileall -q src tools tests        # 编译检查
python3 tools/check_contract.py domain/contract.json
```
