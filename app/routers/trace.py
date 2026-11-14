"""流转与检验适用范围接口。

角色分工（复用现有令牌）：
- inspector：登记流转、登记检验引用绑定、提交报告修订（只读自己的台账）
- reviewer：确认批次异常、重试/作废挂起登记、签发报告修订说明
- admin/任何 read：查询台账、覆盖判定、批次限制与传播链
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import require
from app.db import get_db
from app.models import (
    AuditEvent,
    BatchAnomaly,
    BatchRestriction,
    CoverageDetermination,
    FlowOperation,
    InspectionBinding,
    ReportRevision,
)
from app.schemas import (
    AnomalyConfirmRequest,
    AnomalyOut,
    BindingCreateRequest,
    BindingOut,
    CoverageOut,
    FlowOperationOut,
    FlowRegisterRequest,
    ReportRevisionOut,
    ReportRevisionRequest,
    RestrictionOut,
)
from app.trace import (
    TraceError,
    anomaly_impact,
    confirm_anomaly,
    lane_snapshot,
    operation_balance,
    register_binding,
    register_operation,
    retry_operation,
    void_operation,
    add_report_revision,
)

router = APIRouter(prefix="/api/trace", tags=["trace"])


def _op_out(op: FlowOperation) -> FlowOperationOut:
    return FlowOperationOut(
        id=op.id,
        operation_no=op.operation_no,
        kind=op.kind,
        quantity_unit=op.quantity_unit,
        loss_quantity=float(op.loss_quantity),
        effective_at=op.effective_at,
        recorded_at=op.recorded_at,
        status=op.status,
        suspend_reasons=op.suspend_reasons or [],
        homogeneity_retained=op.homogeneity_retained,
        homogeneity_evidence=op.homogeneity_evidence,
        material=op.material,
        note=op.note,
        balance=operation_balance(op),
        lines=[
            {
                "id": ln.id,
                "direction": ln.direction,
                "batch_no": ln.batch_no,
                "quantity": float(ln.quantity),
            }
            for ln in op.lines
        ],
    )


def _binding_out(db: Session, b: InspectionBinding) -> BindingOut:
    dets = (
        db.query(CoverageDetermination)
        .filter_by(binding_id=b.id, is_current=True)
        .order_by(CoverageDetermination.covered_batch_no, CoverageDetermination.revision)
        .all()
    )
    return BindingOut(
        id=b.id,
        binding_no=b.binding_no,
        result_id=b.result_id,
        sample_no=b.sample_no,
        analyte=b.analyte,
        source_batch_no=b.source_batch_no,
        covered_quantity=float(b.covered_quantity),
        sampling_basis=b.sampling_basis,
        homogeneity_evidence=b.homogeneity_evidence,
        status=b.status,
        note=b.note,
        registered_by=b.registered_by,
        created_at=b.created_at,
        coverage=[CoverageOut.model_validate(d, from_attributes=True) for d in dets],
    )


@router.post("/flows", response_model=FlowOperationOut, status_code=201)
def create_flow(
    body: FlowRegisterRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require("import")),
):
    """登记收货/拆分/混合。投入 != 产出 + 损耗 或成环时挂起，原始数量不改动。"""
    try:
        op = register_operation(
            db,
            kind=body.kind,
            inputs=[(x.batch_no, x.quantity) for x in body.inputs],
            outputs=[(x.batch_no, x.quantity) for x in body.outputs],
            loss=body.loss,
            quantity_unit=body.quantity_unit,
            effective_at=body.effective_at,
            homogeneity_retained=body.homogeneity_retained,
            homogeneity_evidence=body.homogeneity_evidence,
            material=body.material,
            note=body.note,
            actor=role,
        )
    except TraceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(AuditEvent(
        event_type="flow_registered", actor=role, target=op.operation_no,
        detail={"kind": op.kind, "status": op.status, "suspend_reasons": op.suspend_reasons},
    ))
    db.commit()
    db.refresh(op)
    return _op_out(op)


@router.get("/flows", response_model=list[FlowOperationOut])
def list_flows(
    status: str | None = None,
    batch_no: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(FlowOperation)
    if status:
        q = q.filter(FlowOperation.status == status)
    ops = q.order_by(FlowOperation.effective_at, FlowOperation.id).all()
    if batch_no:
        ops = [op for op in ops if any(ln.batch_no == batch_no for ln in op.lines)]
    return [_op_out(op) for op in ops]


@router.post("/flows/{operation_id}/retry", response_model=FlowOperationOut)
def retry_flow(
    operation_id: int,
    db: Session = Depends(get_db),
    role: str = Depends(require("review")),
):
    """对挂起登记按当前台账重新校验（数量事实不变）。"""
    try:
        op = retry_operation(db, operation_id, actor=role)
    except TraceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(AuditEvent(event_type="flow_retried", actor=role, target=op.operation_no,
                      detail={"status": op.status, "suspend_reasons": op.suspend_reasons}))
    db.commit()
    db.refresh(op)
    return _op_out(op)


@router.post("/flows/{operation_id}/void", response_model=FlowOperationOut)
def void_flow(
    operation_id: int,
    reason: str = Query(default="", max_length=300),
    db: Session = Depends(get_db),
    role: str = Depends(require("review")),
):
    try:
        op = void_operation(db, operation_id, actor=role, reason=reason)
    except TraceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(AuditEvent(event_type="flow_voided", actor=role, target=op.operation_no,
                      detail={"reason": reason}))
    db.commit()
    db.refresh(op)
    return _op_out(op)


@router.get("/batches/{batch_no}/lane")
def batch_lane(
    batch_no: str,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    """逐层查明：该批次的数量从哪来、到哪去、还剩多少。"""
    return lane_snapshot(db, batch_no)


@router.post("/bindings", response_model=BindingOut, status_code=201)
def create_binding(
    body: BindingCreateRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require("import")),
):
    """把检验结果绑定到原取样批次与覆盖数量；覆盖不得超出实际投入。"""
    try:
        binding = register_binding(
            db,
            result_id=body.result_id,
            covered_quantity=body.covered_quantity,
            sampling_basis=body.sampling_basis,
            source_batch_no=body.source_batch_no,
            homogeneity_evidence=body.homogeneity_evidence,
            note=body.note,
            actor=role,
        )
    except TraceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(AuditEvent(
        event_type="inspection_binding_added", actor=role, target=binding.binding_no,
        detail={"result_id": binding.result_id, "source_batch_no": binding.source_batch_no,
                "covered_quantity": float(binding.covered_quantity)},
    ))
    db.commit()
    db.refresh(binding)
    return _binding_out(db, binding)


@router.get("/bindings", response_model=list[BindingOut])
def list_bindings(
    sample_no: str | None = None,
    source_batch_no: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(InspectionBinding)
    if sample_no:
        q = q.filter(InspectionBinding.sample_no == sample_no)
    if source_batch_no:
        q = q.filter(InspectionBinding.source_batch_no == source_batch_no)
    return [_binding_out(db, b) for b in q.order_by(InspectionBinding.id).all()]


@router.get("/coverage/{batch_no}")
def batch_coverage(
    batch_no: str,
    history: bool = False,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    """某批次的检验覆盖判定：哪些数量有依据、哪些待补检、限制从哪份检验传播而来。"""
    q = db.query(CoverageDetermination).filter_by(covered_batch_no=batch_no)
    if not history:
        q = q.filter(CoverageDetermination.is_current.is_(True))
    dets = q.order_by(
        CoverageDetermination.binding_id, CoverageDetermination.revision
    ).all()
    items = []
    for d in dets:
        binding = db.get(InspectionBinding, d.binding_id)
        items.append({
            **CoverageOut.model_validate(d, from_attributes=True).model_dump(),
            "binding_no": binding.binding_no if binding else None,
            "sampling_basis": binding.sampling_basis if binding else None,
        })
    return {"batch_no": batch_no, "determinations": items}


@router.get("/restrictions/{batch_no}", response_model=list[RestrictionOut])
def restrictions(
    batch_no: str,
    history: bool = False,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(BatchRestriction).filter_by(batch_no=batch_no)
    if not history:
        q = q.filter(BatchRestriction.is_current.is_(True))
    return q.order_by(BatchRestriction.revision).all()


@router.get("/restrictions")
def restrictions_all(
    status: str | None = None,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    q = db.query(BatchRestriction).filter(BatchRestriction.is_current.is_(True))
    if status:
        q = q.filter(BatchRestriction.status == status)
    return [
        {
            "batch_no": r.batch_no,
            "revision": r.revision,
            "status": r.status,
            "total_quantity": float(r.total_quantity),
            "transferred_quantity": float(r.transferred_quantity),
            "remaining_quantity": float(r.remaining_quantity),
            "released_quantity": float(r.released_quantity),
            "held_quantity": float(r.held_quantity),
            "bases": r.bases,
        }
        for r in q.order_by(BatchRestriction.batch_no).all()
    ]


@router.post("/anomalies", response_model=AnomalyOut, status_code=201)
def confirm_batch_anomaly(
    body: AnomalyConfirmRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require("review")),
):
    """批次事后确认异常：沿实际流向计算尚存与已转出范围，触发影响面重算。"""
    anomaly = confirm_anomaly(db, body.batch_no, body.reason, actor=role)
    db.add(AuditEvent(event_type="batch_anomaly_confirmed", actor=role, target=body.batch_no,
                      detail={"reason": body.reason, "impact": anomaly.impact}))
    db.commit()
    db.refresh(anomaly)
    return anomaly


@router.get("/anomalies/{batch_no}")
def list_anomalies(
    batch_no: str,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    return (
        db.query(BatchAnomaly)
        .filter_by(batch_no=batch_no)
        .order_by(BatchAnomaly.id)
        .all()
    )


@router.get("/anomalies/{batch_no}/impact")
def batch_anomaly_impact(
    batch_no: str,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    return anomaly_impact(db, batch_no)


@router.post("/reports/{report_id}/revisions", response_model=ReportRevisionOut, status_code=201)
def create_report_revision(
    report_id: int,
    body: ReportRevisionRequest,
    db: Session = Depends(get_db),
    role: str = Depends(require("review")),
):
    """已签报告的新修订说明：不改旧报告原文，也不把旧报告改成另一批次。"""
    try:
        rec = add_report_revision(
            db, report_id,
            reason=body.reason, note=body.note,
            affected_batch_no=body.affected_batch_no, actor=role,
        )
    except TraceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(AuditEvent(event_type="report_revision_added", actor=role, target=str(report_id),
                      detail={"revision_no": rec.revision_no, "reason": body.reason}))
    db.commit()
    db.refresh(rec)
    return rec


@router.get("/reports/{report_id}/revisions", response_model=list[ReportRevisionOut])
def list_report_revisions(
    report_id: int,
    db: Session = Depends(get_db),
    role: str = Depends(require("read")),
):
    return (
        db.query(ReportRevision)
        .filter_by(report_id=report_id)
        .order_by(ReportRevision.revision_no)
        .all()
    )
