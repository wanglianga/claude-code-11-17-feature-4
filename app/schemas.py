"""请求/响应 schema。"""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field


class ImportResponse(BaseModel):
    import_file_id: int
    source_ref: str
    status: str
    total_rows: int
    parsed_rows: int
    pending_rows: int
    new_results: int = 0
    new_versions: int = 0
    duplicate_rows: int = 0
    conflicts_raised: int = 0
    pending_reasons: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


class ConflictResolveRequest(BaseModel):
    keep_result_id: int
    note: str = Field(min_length=2, max_length=500)


class ConflictOut(BaseModel):
    id: int
    group_id: int
    result_id_a: int
    result_id_b: int
    reason: str
    status: str
    resolution_note: str | None = None
    resolved_by: str | None = None
    created_at: dt.datetime
    resolved_at: dt.datetime | None = None


class ResultOut(BaseModel):
    id: int
    group_id: int
    version: int
    source_ref: str
    record_no: str | None
    reading_text: str
    unit_original: str
    dilution_factor: float
    detection_limit_original: float | None
    conversion_chain: list[dict[str, Any]]
    dilution_applied: float
    canonical_unit: str | None
    value_type: str
    numeric_value: float | None
    lower_bound: float | None
    upper_bound: float | None
    qualitative: str
    total_mass_mg: float | None
    total_pending: bool
    calc_status: str
    calc_note: str | None
    is_superseded: bool
    created_at: dt.datetime


class GroupOut(BaseModel):
    id: int
    sample_no: str
    analyte: str
    method: str
    conflict_status: str
    current_result_id: int | None
    results: list[ResultOut] = Field(default_factory=list)


class BatchConclusionOut(BaseModel):
    id: int
    batch_no: str
    version: int
    conclusion: str
    prerequisites_met: bool
    detail: dict[str, Any]
    generated_at: dt.datetime


class ReportSubmitRequest(BaseModel):
    batch_no: str
    batch_conclusion_id: int | None = None


class ReportOut(BaseModel):
    id: int
    report_no: str
    batch_no: str
    batch_conclusion_id: int | None
    status: str
    result_refs: list[dict[str, Any]]
    payload_snapshot: dict[str, Any]
    submitted_by: str
    submitted_at: dt.datetime
    reviewed_by: str | None
    reviewed_at: dt.datetime | None
    withdraw_reason: str | None
    withdrawn_by: str | None
    withdrawn_at: dt.datetime | None
    superseded_by_id: int | None


class WithdrawRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    new_report_id: int | None = None


class InvalidateRequest(BaseModel):
    sample_no: str
    reason: str = Field(min_length=2, max_length=500)


class RawRecordOut(BaseModel):
    id: int
    import_file_id: int
    source_ref: str
    record_no: str | None
    line_no: int
    sample_no: str | None
    analyte: str | None
    method: str | None
    raw_payload: dict[str, Any]
    row_hash: str
    parse_status: str
    error_reason: str | None
    created_at: dt.datetime


class PendingOut(BaseModel):
    """待处理视图：坏行 + 待补结果，采购/复核一眼看到哪些项目还不能确定。"""
    raw_pending: list[RawRecordOut]
    result_pending: list[ResultOut]


# ---- 流转与检验适用范围 --------------------------------------------------

class FlowLineIn(BaseModel):
    batch_no: str = Field(min_length=1, max_length=64)
    quantity: float = Field(gt=0)


class FlowRegisterRequest(BaseModel):
    kind: Literal["receive", "split", "merge"]
    inputs: list[FlowLineIn] = Field(min_length=1)
    outputs: list[FlowLineIn] = Field(min_length=1)
    loss: float = Field(default=0, ge=0)
    quantity_unit: str = Field(default="kg", min_length=1, max_length=32)
    effective_at: dt.datetime | None = None
    homogeneity_retained: bool = False
    homogeneity_evidence: str | None = None
    material: str | None = None
    note: str | None = None


class FlowLineOut(BaseModel):
    id: int
    direction: str
    batch_no: str
    quantity: float


class FlowOperationOut(BaseModel):
    id: int
    operation_no: str
    kind: str
    quantity_unit: str
    loss_quantity: float
    effective_at: dt.datetime
    recorded_at: dt.datetime
    status: str
    suspend_reasons: list[dict[str, Any]] = Field(default_factory=list)
    homogeneity_retained: bool
    homogeneity_evidence: str | None = None
    material: str | None = None
    note: str | None = None
    balance: dict[str, Any] = Field(default_factory=dict)
    lines: list[FlowLineOut] = Field(default_factory=list)


class BindingCreateRequest(BaseModel):
    result_id: int
    covered_quantity: float = Field(gt=0)
    sampling_basis: str = Field(min_length=2, max_length=500)
    source_batch_no: str | None = None
    homogeneity_evidence: str | None = None
    note: str | None = None


class CoverageOut(BaseModel):
    id: int
    binding_id: int
    result_id: int
    sample_no: str
    analyte: str
    source_batch_no: str
    covered_batch_no: str
    revision: int
    is_current: bool
    entered_quantity: float
    entered_proven_quantity: float
    covered_quantity: float
    pending_quantity: float
    status: str
    via_merge: bool
    paths: list[Any]
    reason: str | None = None
    computed_at: dt.datetime


class BindingOut(BaseModel):
    id: int
    binding_no: str
    result_id: int
    sample_no: str
    analyte: str
    source_batch_no: str
    covered_quantity: float
    sampling_basis: str
    homogeneity_evidence: str | None = None
    status: str
    note: str | None = None
    registered_by: str
    created_at: dt.datetime
    coverage: list[CoverageOut] = Field(default_factory=list)


class RestrictionOut(BaseModel):
    id: int
    batch_no: str
    revision: int
    is_current: bool
    total_quantity: float
    transferred_quantity: float
    remaining_quantity: float
    released_quantity: float
    held_quantity: float
    status: str
    bases: list[Any]
    computed_at: dt.datetime


class AnomalyConfirmRequest(BaseModel):
    batch_no: str
    reason: str = Field(min_length=2, max_length=500)


class AnomalyOut(BaseModel):
    id: int
    batch_no: str
    reason: str
    status: str
    confirmed_by: str
    confirmed_at: dt.datetime
    impact: dict[str, Any]


class ReportRevisionRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=200)
    note: str = Field(min_length=2, max_length=1000)
    affected_batch_no: str | None = None


class ReportRevisionOut(BaseModel):
    id: int
    report_id: int
    revision_no: int
    reason: str
    note: str
    affected_batch_no: str | None
    snapshot: dict[str, Any]
    created_by: str
    created_at: dt.datetime
