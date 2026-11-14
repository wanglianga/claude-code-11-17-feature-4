"""SQLAlchemy 模型。

存储分三层，对应需求中的三类数据：
1. 采集原件：import_files / raw_records（原文与失败原因永不丢弃）
2. 标准化结果：units / decision_rules / samples / result_groups /
   standardized_results / conflicts（数值、上下界、定性描述分开）
3. 批准记录：batch_conclusions / reports / audit_events
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# ---- 常量（集中定义，避免魔法字符串散落）---------------------------------

# 样品状态
SAMPLE_VALID = "valid"
SAMPLE_INVALID = "invalid"

# 原始行解析状态
ROW_PARSED = "parsed"
ROW_PENDING = "pending"

# 标准化计算状态
CALC_OK = "standardized"              # 已完成标准化
CALC_PENDING_UNIT = "pending_unit"    # 单位量纲/基质不匹配，待处理
CALC_PENDING_MASS = "pending_mass"    # 缺样品质量/体积，无法换算总量，待补
CALC_PENDING_VALUE = "pending_value"  # 读数无法识别（如零值无检出限说明）

# 定性描述（与数值分开保存，禁止把未检出伪装成精确零值）
QUAL_QUANTIFIED = "quantified"
QUAL_BELOW_DL = "below_detection_limit"
QUAL_ABOVE_QL = "above_quantitation_limit"
QUAL_INTERVAL = "estimated_interval"

# 冲突状态
CONFLICT_OPEN = "open"
CONFLICT_RESOLVED = "resolved"

# 报告状态
REPORT_SUBMITTED = "submitted"
REPORT_APPROVED = "approved"
REPORT_WITHDRAWN = "withdrawn"

# 批次结论
BATCH_PASS = "pass"
BATCH_FAIL = "fail"
BATCH_REVIEW = "needs_review"
BATCH_HOLD = "hold"  # 前置条件不满足，不能下结论


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---- 参考数据 -------------------------------------------------------------

class Unit(Base):
    __tablename__ = "units"

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    unit_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 量纲：mass_fraction（质量分数类）/ mass_concentration（质量-体积浓度）/ mass / volume
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    # 换算到该量纲基准单位的乘法因子，如 ug/kg -> mg/kg 为 0.001
    factor_to_canonical: Mapped[float] = mapped_column(Numeric(18, 12), nullable=False)
    canonical_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    # 适用基质列表，如 ["solid","semi_solid"]；空列表表示不限
    applicable_matrix: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class DecisionRule(Base):
    __tablename__ = "decision_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    analyte: Mapped[str] = mapped_column(String(64), nullable=False)
    matrix: Mapped[str] = mapped_column(String(32), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # upper = 上限（结果不得高于），lower = 下限
    limit_type: Mapped[str] = mapped_column(String(8), nullable=False)
    limit_value: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    limit_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("analyte", "matrix", name="uq_rule_analyte_matrix"),
    )


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    supplier: Mapped[str] = mapped_column(String(128), nullable=False)
    material: Mapped[str] = mapped_column(String(128), nullable=False)
    matrix: Mapped[str] = mapped_column(String(32), nullable=False)
    sampling_location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 样品质量(g)/体积(mL)：换算总量时必须；缺失则相关结果停在待补
    sample_mass_g: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    sample_volume_ml: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=SAMPLE_VALID)
    invalid_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---- 第一层：采集原件 ------------------------------------------------------

class ImportFile(Base):
    __tablename__ = "import_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 来源编号：同一来源的重传靠它关联
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    file_format: Mapped[str] = mapped_column(String(8), nullable=False)  # csv / json
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # imported=正常入库（可能仍含待处理行）/ duplicate=指纹重复整体跳过 / failed=文件无法解析
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parsed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    records: Mapped[list["RawRecord"]] = relationship(back_populates="import_file")

    __table_args__ = (
        UniqueConstraint("source_ref", "content_hash", name="uq_source_hash"),
    )


class RawRecord(Base):
    """行级采集原件。无论后续成败，原始 payload 都在此保留。"""

    __tablename__ = "raw_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_file_id: Mapped[int] = mapped_column(ForeignKey("import_files.id"), nullable=False, index=True)
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    # 来源内记录编号：同 source_ref + record_no 再次出现即“同号修订”
    record_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_no: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    analyte: Mapped[str | None] = mapped_column(String(64), nullable=True)
    method: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 行内容指纹：用于同号变值识别
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False, default=ROW_PARSED)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    import_file: Mapped[ImportFile] = relationship(back_populates="records")


# ---- 第二层：标准化结果 ----------------------------------------------------

class ResultGroup(Base):
    """同一（样品 × 检测项目 × 方法）的归并组，承载版本链与冲突状态。"""

    __tablename__ = "result_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    analyte: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(128), nullable=False)
    conflict_status: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # 无冲突时指向当前生效版本；冲突未解决时为 NULL（禁止带病出报告）
    current_result_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "standardized_results.id",
            use_alter=True,
            name="fk_group_current_result",
        ),
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    results: Mapped[list["StandardizedResult"]] = relationship(
        back_populates="group", foreign_keys="StandardizedResult.group_id"
    )

    __table_args__ = (
        UniqueConstraint("sample_no", "analyte", "method", name="uq_group_key"),
    )


class StandardizedResult(Base):
    """标准化结果（版本化，不可变；修订即新增版本，旧版本标记 superseded）。"""

    __tablename__ = "standardized_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("result_groups.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_record_id: Mapped[int] = mapped_column(ForeignKey("raw_records.id"), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    record_no: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # —— 原值回查 ——
    reading_text: Mapped[str] = mapped_column(String(64), nullable=False)
    reading_original: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    unit_original: Mapped[str] = mapped_column(String(32), nullable=False)
    dilution_factor: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=1)
    detection_limit_original: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    instrument_run: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # —— 换算链回查：[{"from":"ug/kg","to":"mg/kg","factor":0.001}, ...] ——
    conversion_chain: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    dilution_applied: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=1)
    canonical_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # —— 数值 / 区间 / 定性描述三分 ——
    # numeric_value 仅在 quantified 时有值；未检出绝不写 0
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)
    numeric_value: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    lower_bound: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    upper_bound: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    qualitative: Mapped[str] = mapped_column(String(48), nullable=False)

    # —— 总量（仅在样品质量/体积齐备时计算；浓度不允许擅自当总量）——
    total_mass_mg: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    total_mass_lower: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    total_mass_upper: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    # 总量是否因缺质量/体积而挂起：浓度结果仍可判定，仅总量待补
    total_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    calc_status: Mapped[str] = mapped_column(String(24), nullable=False, default=CALC_OK)
    calc_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    group: Mapped[ResultGroup] = relationship(back_populates="results", foreign_keys=[group_id])


class Conflict(Base):
    """同一项目出现不同结果：先列冲突，不按最后到达覆盖。"""

    __tablename__ = "conflicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("result_groups.id"), nullable=False, index=True)
    result_id_a: Mapped[int] = mapped_column(ForeignKey("standardized_results.id"), nullable=False)
    result_id_b: Mapped[int] = mapped_column(ForeignKey("standardized_results.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=CONFLICT_OPEN)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_result_id: Mapped[int | None] = mapped_column(ForeignKey("standardized_results.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---- 第三层：批次结论与批准记录 --------------------------------------------

class BatchConclusion(Base):
    __tablename__ = "batch_conclusions"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    conclusion: Mapped[str] = mapped_column(String(16), nullable=False)
    # 结构性前置条件是否满足（归属明确 / 必检齐备 / 冲突已处理 / 无待补）
    prerequisites_met: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 生成时引用的结果版本快照，批准时据此重读核对
    result_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("batch_no", "version", name="uq_batch_version"),
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    batch_conclusion_id: Mapped[int | None] = mapped_column(ForeignKey("batch_conclusions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=REPORT_SUBMITTED)
    # 提交时固化的结果版本引用；批准前重读，版本漂移即打回重提
    result_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    payload_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    submitted_by: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdraw_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    withdrawn_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    withdrawn_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 撤回后由哪份新报告接续，形成历史链；原文不修改
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("reports.id"), nullable=True)


class AuditEvent(Base):
    """关键动作留痕：导入、修订、冲突处理、失效、提交、批准、撤回。"""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- 第四层：供货批次流转与检验适用范围追溯 --------------------------------
#
# 三层职责分离（题目要求分别留存）：
# 1) 流转登记：flow_operations / flow_lines（投入量、产出量、损耗、生效时点）
# 2) 检验覆盖判定：inspection_bindings（检验引用绑定原取样批次与覆盖范围）
#    + coverage_determinations（按实际流转计算的逐批次覆盖，版本化留存）
# 3) 批次限制：batch_restrictions（释放/暂扣数量与限制依据）
# 另有 batch_anomalies（批次事后确认异常）与 report_revisions（已签报告的新修订说明）。

# 流转类型
FLOW_RECEIVE = "receive"
FLOW_SPLIT = "split"
FLOW_MERGE = "merge"

# 流转登记状态
FLOW_ACTIVE = "active"
FLOW_SUSPENDED = "suspended"
FLOW_VOID = "void"

# 挂起原因码
SUSPEND_QUANTITY_MISMATCH = "quantity_mismatch"   # 投入 != 产出 + 损耗
SUSPEND_CYCLE = "cycle_detected"                  # 流转关系成环
SUSPEND_INSUFFICIENT = "insufficient_available"   # 争用同一剩余数量，可用量不足
SUSPEND_STRUCTURE = "invalid_structure"           # 结构/数量非法
SUSPEND_UNIT = "unit_mismatch"                    # 数量单位不一致

# 覆盖判定状态
COV_VALID = "valid"                     # 有效覆盖
COV_TRANSFERRED = "transferred"         # 覆盖材料已全部转出该批次
COV_PARTIAL = "partial"                 # 仅覆盖混合批次中的来料份额
COV_PENDING_REINSPECTION = "pending_reinspection"  # 缺同质性依据，待补检
COV_MIXED_ONLY = "mixed_only"           # 混合样结果仅能证明混合批次本身
COV_NOT_COVERED = "not_covered"         # 无覆盖

# 批次限制状态
RESTRICT_CLEAR = "clear"
RESTRICT_RESTRICTED = "restricted"
RESTRICT_BLOCKED = "blocked"
RESTRICT_PARTIAL = "partial"  # 部分材料有覆盖可放行，其余暂扣


class MaterialBatch(Base):
    """流转台账中的材料批次（供货编号）。与 samples.batch_no 使用同一套编号。"""

    __tablename__ = "material_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    material: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quantity_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="kg")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    abnormal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 外部来料来源（仅作为收货登记的投入方出现）：不计入内部库存与放行限制
    external: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FlowOperation(Base):
    """一次流转登记：收货、拆分或混合。原始数量事实不可修改，只可挂起/作废后重登。"""

    __tablename__ = "flow_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="kg")
    loss_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    # 生效时点：迟到登记的排序依据；重算按 (effective_at, id) 进行
    effective_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    recorded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=FLOW_ACTIVE, index=True)
    # [{"code": "...", "message": "...", ...}] 挂起时保留原因，不改动原始数量来凑平
    suspend_reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 拆分/混合是否保留可追溯的同质性依据（无依据则下游不能引用原报告）
    homogeneity_retained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    homogeneity_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    material: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)

    lines: Mapped[list["FlowLine"]] = relationship(
        back_populates="operation", cascade="all, delete-orphan", order_by="FlowLine.id"
    )


class FlowLine(Base):
    """流转登记的数量行：direction=input 为投入，output 为产出。"""

    __tablename__ = "flow_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("flow_operations.id"), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # input / output
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)

    operation: Mapped[FlowOperation] = relationship(back_populates="lines")


class InspectionBinding(Base):
    """检验引用绑定：一条生效检验结果同时绑定“原取样批次”和“覆盖数量范围”。

    不能只凭批次编号上下级关系沿用结论；覆盖沿流转推导时必须带同质性依据。
    """

    __tablename__ = "inspection_bindings"

    id: Mapped[int] = mapped_column(primary_key=True)
    binding_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    result_id: Mapped[int] = mapped_column(ForeignKey("standardized_results.id"), nullable=False, index=True)
    sample_no: Mapped[str] = mapped_column(String(64), nullable=False)
    analyte: Mapped[str] = mapped_column(String(64), nullable=False)
    # 原取样批次：必须等于该样品档案登记的批次
    source_batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 覆盖范围（数量）：该检验能证明的来料数量，不得超过批次累计投入量
    covered_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    sampling_basis: Mapped[str] = mapped_column(Text, nullable=False)
    homogeneity_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    registered_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    determinations: Mapped[list["CoverageDetermination"]] = relationship(
        back_populates="binding", cascade="all, delete-orphan"
    )


class CoverageDetermination(Base):
    """检验覆盖判定：按实际流向逐批次计算覆盖数量，版本化留存（旧版本不删除）。"""

    __tablename__ = "coverage_determinations"

    id: Mapped[int] = mapped_column(primary_key=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("inspection_bindings.id"), nullable=False, index=True)
    result_id: Mapped[int] = mapped_column(ForeignKey("standardized_results.id"), nullable=False)
    sample_no: Mapped[str] = mapped_column(String(64), nullable=False)
    analyte: Mapped[str] = mapped_column(String(64), nullable=False)
    source_batch_no: Mapped[str] = mapped_column(String(64), nullable=False)
    covered_batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    # 累计进入该批次的数量 / 其中带同质性依据进入的数量
    entered_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    entered_proven_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    # 仍留在该批次的数量 / 因缺同质性依据而待补检的数量
    covered_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    pending_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    via_merge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 逐层路径：[ [ {"operation_id","from_batch","to_batch","kind","quantity","evidence"} ] ]
    paths: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    binding: Mapped["InspectionBinding"] = relationship(back_populates="determinations")

    __table_args__ = (
        UniqueConstraint("binding_id", "covered_batch_no", "revision", name="uq_coverage_rev"),
    )


class BatchRestriction(Base):
    """批次限制：当前总数量中可释放/暂扣多少，以及每条限制的依据链。"""

    __tablename__ = "batch_restrictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    total_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    transferred_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    remaining_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    released_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    held_quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=RESTRICT_CLEAR)
    bases: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("batch_no", "revision", name="uq_restriction_rev"),
    )


class BatchAnomaly(Base):
    """批次事后被确认异常。确认后沿实际流向重算尚存/已转出数量。"""

    __tablename__ = "batch_anomalies"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    confirmed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 确认时的流向快照（尚存数量、已转出范围、汇合去重后的实际数量）
    impact: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ReportRevision(Base):
    """已签报告的新修订说明：不修改旧报告原文，也不把旧报告改成另一批次。"""

    __tablename__ = "report_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), nullable=False, index=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    affected_batch_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("report_id", "revision_no", name="uq_report_revision_no"),
    )
