# 实验室原料检验数据治理服务

接收实验室原始记录（CSV / JSON），关联取样与供货批次，统一计量含义，按随附业务规则
生成**可复核的项目判定与整批结论**。针对题目中的两类真实事故设计：

1. **单位口径不一致**：同一项目一份 `mg/kg`、一份 `ug/kg`，汇总表只比数字导致高风险批次
   被误判合格 —— 本服务换算前先核对量纲与适用基质，全部归一到基准单位后再判定。
2. **低于检出限被填成零**：本服务把**数值、上下界、定性描述分开保存**，未检出存为
   区间 `[0, 检出限]` 而不是精确值 0；判定按区间比较，横跨限值即交人工复核。

> 判定限值全部来自随附 `data/reference/decision_rules.json`（演示用虚构规则），
> 系统不自行套用任何真实行业限值。

在检验结果之上，**新增第四层：供货批次拆分/混合后的检验适用范围追溯**。
一个来料批次拆成多份、多份又混合再拆分时，本服务不把“某次取样的合格结论”
顺延成整条链的保证，而是：

- 按实际**投入量、产出量、损耗、生效时点**登记流转，建立有数量约束的流向关系；
- 检验引用同时绑定**原取样批次**与**覆盖数量**，仅凭编号上下级不沿用结论；
- 覆盖沿实际流向**按数量稀释传播**（拆分按产出比例、混合按投料份额）；
- 拆分/混合**未保留可追溯的同质性依据**时，下游对应数量列为**待补检，暂不放行**；
- **混合样结果只证明混合批次本身**，不自动证明每个组成批次；
- 投入与产出对不上、或关系成环时**挂起该次登记**，不改既有数量事实来凑平；
- 迟到登记按生效时点插入并**版本化重算**受影响范围，旧版本保留；
- 批次事后被确认异常时，沿实际流向算**尚存数量与已转出范围**（汇合路径不重复计算）；
- 已签报告通过**新修订说明**更正，不修改旧报告原文、不把旧报告改成另一批次；
- 两个拆分争用同一剩余数量时只接受满足可用量的一份（数据库行锁串行化）；
- 负责人可按批次逐层查询：某项限制由哪份检验、经哪几步流转传播而来。

## 一、技术栈与存储分层

- Python 3.12 + FastAPI + SQLAlchemy 2 + PostgreSQL（纯后端，提供 OpenAPI 3.1 / Swagger UI）
- 三层存储，职责分离：

| 层 | 表 | 说明 |
| --- | --- | --- |
| 采集原件 | `import_files`、`raw_records` | 文件与行级原文、内容指纹、坏行原因，永不丢弃 |
| 标准化结果 | `units`、`decision_rules`、`samples`、`result_groups`、`standardized_results`、`conflicts` | 版本化结果，数值/区间/定性三分，冲突并列 |
| 批准记录 | `batch_conclusions`、`reports`、`audit_events` | 批次结论、报告版本与撤回历史链、操作留痕 |
| 流转追溯 | `flow_operations`、`flow_lines`、`material_batches` | 流转登记（投入/产出/损耗/生效时点）与数量台账 |
| 覆盖判定 | `inspection_bindings`、`coverage_determinations` | 检验引用（原取样批次 + 覆盖数量）、逐批次覆盖判定（版本化） |
| 批次限制 | `batch_restrictions`、`batch_anomalies`、`report_revisions` | 可放行/暂扣数量与依据链、异常确认及影响面、报告修订说明 |

流转登记、检验覆盖判定与批次限制**分别留存**：`flow_operations` 只记事实，
`coverage_determinations` 只记“这份检验能证明哪些数量”，`batch_restrictions`
只记“当前有多少可放行、多少暂扣、依据是什么”，三者互不改写原始记录。

## 二、目录结构

```
app/                后端源码（models/units/parser/processing/judgment/batch/reports/trace/routers）
data/reference/     随附参考数据：单位表、样品档案、判定规则、异常记录
data/raw/           随附原始记录：LAB-A CSV、LAB-B JSON、同号修订 CSV
scripts/            容器入口与一键演示脚本
tests/              63 个 pytest 用例（纯逻辑 + 全链路 + HTTP 接口 + 流转追溯）
Dockerfile / docker-compose.yml  一键启动
```

## 三、本地启动

### 方式 A：Docker Compose（一键，自带 PostgreSQL）

```bash
docker compose up --build
# 服务：http://localhost:8000  ；Swagger UI：http://localhost:8000/docs
```

容器入口会自动等待数据库、建表、幂等装载 `data/reference` 参考数据后启动 API。

### 方式 B：本地 Python（使用容器内/本机已运行的 PostgreSQL）

```bash
# 1. 建虚拟环境并装依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 准备数据库（库/用户/密码 ccdev，与本机 PostgreSQL 一致）
createdb -h 127.0.0.1 -U ccdev qc_lab   # 如已存在可跳过

# 3. 建表 + 装载随附参考数据（单位/样品/规则/异常记录，幂等可重复执行）
.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli seed

# 4. 启动服务
.venv/bin/python -m app.cli run --port 8000
```

配置通过环境变量或 `.env`（见 `.env.example`）覆盖，默认
`DATABASE_URL=postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/qc_lab`。

## 四、测试账号（仅隔离开发环境）

角色通过 `Authorization: Bearer <token>` 或 `X-API-Token` 传递：

| 角色 | 令牌 | 权限 |
| --- | --- | --- |
| inspector 检验员 | `token-inspector-001` | 导入记录、提交报告、查询 |
| reviewer 复核员 | `token-reviewer-001` | 冲突处理、样品失效、批准/撤回、查询 |
| admin 管理员 | `token-admin-001` | 参考数据种子、全量查询 |

## 五、验证方式

### 自动化测试（推荐，无需起服务）

```bash
# 测试库 qc_lab_test 需可连接（PostgreSQL 本机 ccdev 账号）
createdb -h 127.0.0.1 -U ccdev qc_lab_test   # 如已存在可跳过
.venv/bin/python -m pytest
```

覆盖：量纲/基质核对、稀释只修正一次、区间判定三种走向、行级坏行容错、
指纹去重、同号修订版本链、异源冲突并列、缺质量挂起总量、失效样品隔离、
批次前置条件、批准前重读漂移打回、撤回历史链；以及流转追溯（`tests/test_trace.py`）：

- 投入 != 产出 + 损耗 → 挂起登记，原始行数量与损耗原样保留，台账不变；
- 流转成环 → 挂起；两个拆分争用同一剩余数量 → 只接受一份（行锁串行化，双会话并发验证）；
- 检验引用绑定原取样批次与覆盖数量，超量绑定、改挂别的批次被拒；
- 拆分无同质性依据 → 下游 `pending_reinspection` 全部暂扣；有依据 → 按产出比例继承；
- 损耗按投入比例扣减传播数量；混合只覆盖本批次中来料份额，混合样自身仅 `mixed_only`；
- 迟到登记按生效时点插入 → 覆盖判定新增版本，旧版本保留；
- 批次事后异常 → 尚存/已转出影响面，汇合路径不重复计算，污染份额即使有本地检验也不放行；
- 已签报告在重算变化后追加修订说明，原文状态与批次编号均不变。

### 一键 HTTP 演示（服务已启动时）

```bash
bash scripts/demo.sh
```

### 手工关键接口（Swagger UI 可直接试）

流转追溯（完整链路见 `scripts/demo.sh` 第 10–11 步）：

```bash
IH="Authorization: Bearer token-inspector-001"
RH="Authorization: Bearer token-reviewer-001"

# 收货 1000 L
curl -X POST http://127.0.0.1:8000/api/trace/flows -H "$IH" -H "Content-Type: application/json" \
  -d '{"kind":"receive","inputs":[{"batch_no":"SUP","quantity":1000}],
       "outputs":[{"batch_no":"B20261102","quantity":1000}],"quantity_unit":"L"}'

# 拆成两份（声明保留同质性依据）
curl -X POST http://127.0.0.1:8000/api/trace/flows -H "$IH" -H "Content-Type: application/json" \
  -d '{"kind":"split","inputs":[{"batch_no":"B20261102","quantity":1000}],
       "outputs":[{"batch_no":"LOT-H","quantity":500},{"batch_no":"LOT-K","quantity":500}],
       "quantity_unit":"L","homogeneity_retained":true,
       "homogeneity_evidence":"同罐分装，分装前后留样比对一致"}'

# 检验引用绑定：原取样批次 + 覆盖数量
curl -X POST http://127.0.0.1:8000/api/trace/bindings -H "$IH" -H "Content-Type: application/json" \
  -d '{"result_id":12,"covered_quantity":600,"sampling_basis":"S-101 代表 600 L 来料"}'

# 逐批次覆盖判定（含逐层流转路径）/ 批次限制 / 数量台账
curl http://127.0.0.1:8000/api/trace/coverage/LOT-H -H "$IH"
curl http://127.0.0.1:8000/api/trace/restrictions/LOT-K -H "$IH"
curl http://127.0.0.1:8000/api/trace/batches/LOT-K/lane -H "$IH"

# 批次事后异常：沿实际流向算尚存与已转出
curl -X POST http://127.0.0.1:8000/api/trace/anomalies -H "$RH" -H "Content-Type: application/json" \
  -d '{"batch_no":"B20261102","reason":"供应商召回"}'

# 已签报告的新修订说明（不改原文）
curl -X POST http://127.0.0.1:8000/api/trace/reports/1/revisions -H "$RH" -H "Content-Type: application/json" \
  -d '{"reason":"批次异常确认","note":"该批次材料已暂扣","affected_batch_no":"B20261102"}'
```

接口一览（均出现在 Swagger UI）：

| 方法与路径 | 作用 | 角色 |
| --- | --- | --- |
| `POST /api/trace/flows` | 登记收货/拆分/混合（数量不齐或成环则挂起） | inspector |
| `GET /api/trace/flows` | 按状态/批次查询流转登记及逐笔平衡 | 只读 |
| `POST /api/trace/flows/{id}/retry` | 挂起登记按当前台账重新校验（不改数量） | reviewer |
| `POST /api/trace/flows/{id}/void` | 作废登记并触发版本化重算 | reviewer |
| `GET /api/trace/batches/{batch}/lane` | 逐层数量账（来源、去向、尚存） | 只读 |
| `POST /api/trace/bindings` | 检验结果绑定原取样批次与覆盖数量 | inspector |
| `GET /api/trace/bindings` | 查询检验引用及其覆盖判定 | 只读 |
| `GET /api/trace/coverage/{batch}` | 该批次覆盖判定（可带 `history=true`） | 只读 |
| `GET /api/trace/restrictions/{batch}` | 批次限制（可放行/暂扣数量与依据链） | 只读 |
| `GET /api/trace/restrictions` | 全部批次限制汇总 | 只读 |
| `POST /api/trace/anomalies` | 确认批次异常并计算影响面 | reviewer |
| `GET /api/trace/anomalies/{batch}/impact` | 异常影响面（尚存 + 已转出逐层明细） | 只读 |
| `POST /api/trace/reports/{id}/revisions` | 已签报告追加修订说明 | reviewer |
| `GET /api/trace/reports/{id}/revisions` | 报告修订历史 | 只读 |

```bash
# 导入原始记录（multipart：file + source_ref）
curl -X POST http://127.0.0.1:8000/api/import/records \
  -H "Authorization: Bearer token-inspector-001" \
  -F "source_ref=LAB-A" -F "file=@data/raw/lab_a_20261105.csv"

# 待处理视图：坏行 + 待补/挂起项（哪些项目还不能确定）
curl "http://127.0.0.1:8000/api/query/pending?batch_no=B20261101" \
  -H "Authorization: Bearer token-inspector-001"

# 生成批次结论 → 提交报告 → 复核批准 → 撤回
curl -X POST http://127.0.0.1:8000/api/reports/batch-conclusions \
  -H "Authorization: Bearer token-inspector-001" -H "Content-Type: application/json" \
  -d '{"batch_no":"B20261102"}'
```

## 六、随附数据与场景对照

| 题面要求 | 随附数据 / 系统行为 |
| --- | --- |
| mg/kg 与 ug/kg 口径陷阱 | `RA-001` 报 `18 ug/kg`（稀释10）归一为 `0.18 mg/kg`；`RB-001` 报 `0.35 mg/kg`，两者冲突并列 |
| 低于检出限不能填零 | `RA-003` ND + 检出限 2 ug/kg → 区间 `[0, 0.002] mg/kg`，`numeric_value` 为空 |
| 坏行进待处理不漏掉 | `RA-006` 缺单位、`RA-013` 读数 `NA`：文件整体入库，2 行带原因 `pending` |
| 未检出却无检出限 | `RA-007` → `pending_value`，不能确定上界 |
| 缺质量/体积停待补 | S-003 无质量、S-102 无体积：浓度仍判定，`total_pending=true`，不擅自换算总量 |
| 量纲/基质不符拒绝 | `RB-002` 用液体单位 `mg/L` 报固体样品 → `pending_unit` |
| 稀释只修正一次 | 读数与检出限同步 × 倍数，`dilution_applied` 与换算链全程留痕可回查 |
| 冲突先列不覆盖 | LAB-A/LAB-B 铅结果不一致 → `conflicts` 开放，生效版本置空 |
| 同号变值保留修订 | `lab_a_20261105_revision.csv` 的 `RA-001` → 新版本，旧版 `superseded` |
| 失效样品隔离 | S-004 由异常记录标记失效，新结果仍存原件但批次结论列为硬阻断 |
| 区间横跨限值交复核 | `RA-004` 汞 `<0.06` 对限值 0.05 → 判定 `review`，不猜合格 |
| 批准前重读版本 | 复核与修订同时到达 → 批准返回 409 版本漂移，打回重提 |
| 撤回留痕不改原文 | 已签发报告 `withdrawn` 并以 `superseded_by_id` 关联新报告，历史保留 |
| 单项通过≠整批可用 | 批次结论需归属明确、必检齐备、冲突清零、待补清零，否则 `hold` |
| 拆分后能否沿用原报告 | 拆分登记 `homogeneity_retained=true` 才按产出比例继承覆盖；否则下游 `pending_reinspection` |
| 混合样能否证明各组成批次 | 不能：混合样只记 `mixed_only`，各组成批次按投料份额得到 `partial` 覆盖 |
| 覆盖不能超出实际取样量 | 绑定数量 + 历史绑定 ≤ 批次累计投入，超出即 400；原取样批次必须与样品档案一致 |
| 数量对不上不凑平 | 投入 != 产出 + 损耗 → 登记 `suspended` 并留存差额，台账不新增该笔 |
| 关系成环挂起 | 让产出批次回流到投入批次 → `cycle_detected` 挂起 |
| 并发拆分争用 | 同一剩余数量两份拆分：一份 `active`、一份因 `insufficient_available` 挂起 |
| 事后异常沿流向影响 | 尚存数量 + 已转出到各下游的实际残留；汇合路径按登记量分配，同一份材料只计一次 |
| 迟到拆分重算 | 生效时点更早的登记插入 → 覆盖判定与限制出新版本，旧版本保留 |
| 已签报告更正 | 重算影响已签批次 → `report_revisions` 追加说明，旧报告状态与批次不变 |
| 限制来源可追溯 | `coverage_determinations.paths` 记录每步操作号、数量与同质性依据，逐层可查 |

## 七、实际验证结论（本次开发中真实执行）

- `pytest`：**63 passed**（tests/ 下 6 个测试文件，PostgreSQL 测试库真实建表执行；
  含双会话并发争用剩余数量的用例）。
- 本机 uvicorn 实跑 HTTP 全链路：导入/重传去重/冲突复核/批次结论/报告批准/
  修订后 409 漂移打回/撤回历史链，均返回预期；`/docs` 返回 200，`/openapi.json`
  为 OpenAPI 3.1、30 个路径（其中 12 个为流转追溯路径）。
- 流转追溯实测（`bash scripts/demo.sh` 第 10–11 步真实输出）：收货生效、声明同质性依据的
  拆分生效、投入产出不齐的拆分以 `quantity_mismatch` 挂起；600 L 检验绑定后
  LOT-H / LOT-K 各得 300 可放行、各自 200 暂扣；确认 B20261102 异常后尚存 0、
  已转出 1000，下游 LOT-H / LOT-K 各实际残留 500 并全部暂扣。
- 典型判定核对：采信 LAB-B 后 S-001 铅 `0.35 mg/kg` 对限值 `0.2` 判 `nonconform`；
  S-002 铅 `0.25` 判 `nonconform`；液体批次 B20261102 前置条件满足判 `pass`；
  固体批次 B20261101 因失效样品与待补项判 `hold` 并逐条列明阻断原因。
- Docker 交付物（Dockerfile / docker-compose.yml / entrypoint）已随工程提供；
  当前隔离容器内无 docker 守护进程，未在本机执行 `docker compose up`，镜像构建需在具备 Docker 的环境验证。
- 说明：流转追溯在本轮为**新增能力**，入口在 `scripts/entrypoint.sh` / `app.cli init-db`
  建表时自动包含新表，无需额外迁移步骤。
