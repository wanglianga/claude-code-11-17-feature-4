"""供货批次拆分/混合后的检验适用范围追溯。

核心立场（不把局部取样结论扩散成整条链的保证）：

1. 流转登记只记录事实：投入量、产出量、损耗、生效时点。投入 != 产出 + 损耗，
   或关系成环时，把该次登记**挂起**（保留原始数量与原因），不修改既有库存事实凑平；
   与生效登记争用同一剩余数量时，后到的一份因可用量不足被挂起。
2. 检验引用 =（结果, 原取样批次, 覆盖数量）。原取样批次必须与样品档案登记一致；
   覆盖数量不得超过该批次累计投入。仅凭编号上下级不能沿用合格结论。
3. 覆盖沿**实际流转**按数量传播：拆分按产出比例，混合按投料份额。
   - 拆分/混合声明保留可追溯的同质性依据 -> 覆盖可继承；
   - 未声明 -> 下游对应数量列入 `pending_reinspection`（待补检），暂不放行；
   - 混合样结果本身只证明混合批次（`mixed_only`），不自动证明组成批次。
4. 批次被事后确认异常时，沿实际流向计算尚存数量与已转出范围；
   多条路径汇合时按登记数量分配，同一份材料只计一次。
5. 迟到登记（生效时点更早）触发受影响范围的版本化重算：
   新的 coverage_determinations / batch_restrictions 版本，旧版本保留。
6. 已签报告遇到重算结果变化，通过 report_revisions 追加修订说明，
   不修改旧报告原文，也不把旧报告改成另一批次。

依赖方向：本模块只读 samples / standardized_results（当前生效检验），
不改动检验层数据；批次结论（app.batch）在流转层之上再做适用范围收紧。
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import (
    COV_MIXED_ONLY,
    COV_PARTIAL,
    COV_PENDING_REINSPECTION,
    COV_TRANSFERRED,
    COV_VALID,
    FLOW_ACTIVE,
    FLOW_MERGE,
    FLOW_RECEIVE,
    FLOW_SPLIT,
    FLOW_SUSPENDED,
    FLOW_VOID,
    RESTRICT_BLOCKED,
    RESTRICT_CLEAR,
    RESTRICT_PARTIAL,
    RESTRICT_RESTRICTED,
    SUSPEND_CYCLE,
    SUSPEND_INSUFFICIENT,
    SUSPEND_QUANTITY_MISMATCH,
    SUSPEND_UNIT,
    BatchAnomaly,
    BatchRestriction,
    CoverageDetermination,
    FlowLine,
    FlowOperation,
    InspectionBinding,
    MaterialBatch,
    Report,
    ReportRevision,
    Sample,
    StandardizedResult,
    utcnow,
)

_QTY_EPS = 1e-6


class TraceError(ValueError):
    """流转/追溯登记被业务规则拒绝（HTTP 层映射为 4xx）。"""


def _qty(v) -> float:
    return round(float(v), 6)


# --------------------------------------------------------------------------
# 台账：按生效时点回放有效登记
# --------------------------------------------------------------------------

def ordered_operations(db: Session, until: dt.datetime | None = None) -> list[FlowOperation]:
    """按 (生效时点, id) 回放；迟到登记按生效时点插入正确位置。"""
    q = db.query(FlowOperation).filter(FlowOperation.status != FLOW_VOID)
    if until is not None:
        q = q.filter(FlowOperation.effective_at <= until)
    return q.order_by(FlowOperation.effective_at, FlowOperation.id).all()


def operation_balance(op: FlowOperation) -> dict:
    ins = sum(_qty(ln.quantity) for ln in op.lines if ln.direction == "input")
    outs = sum(_qty(ln.quantity) for ln in op.lines if ln.direction == "output")
    loss = _qty(op.loss_quantity)
    return {
        "input_total": _qty(ins),
        "output_total": _qty(outs),
        "loss": loss,
        "difference": _qty(ins - outs - loss),
        "balanced": abs(ins - outs - loss) <= _QTY_EPS,
    }


def _ledger(db: Session, batch_no: str, until: dt.datetime | None = None) -> dict:
    """单一批次的数量账：来料、来源份额、去向份额、损耗、尚存。"""
    received = 0.0
    inflow_sources: dict[str, float] = {}
    outflow_targets: dict[str, float] = {}
    consumed_total = 0.0
    loss_allocated = 0.0

    for op in ordered_operations(db, until):
        if op.status != FLOW_ACTIVE:
            continue
        ins = [(ln.batch_no, _qty(ln.quantity)) for ln in op.lines if ln.direction == "input"]
        outs = [(ln.batch_no, _qty(ln.quantity)) for ln in op.lines if ln.direction == "output"]
        total_in = sum(q for _, q in ins)
        if not ins or not outs:
            continue

        # 本批次作为投入：被消耗
        my_in = sum(q for b, q in ins if b == batch_no)
        if my_in > 0:
            if op.kind == FLOW_RECEIVE:
                received += my_in
            else:
                consumed_total += my_in
                if op.kind == FLOW_SPLIT:
                    for out_b, out_q in outs:
                        outflow_targets[out_b] = outflow_targets.get(out_b, 0.0) + out_q
                else:
                    for out_b, out_q in outs:
                        outflow_targets[out_b] = (
                            outflow_targets.get(out_b, 0.0) + out_q * (my_in / total_in)
                        )
                loss_allocated += _qty(op.loss_quantity) * (my_in / total_in)

        # 本批次作为产出：流入
        for out_b, out_q in outs:
            if out_b != batch_no:
                continue
            if op.kind == FLOW_SPLIT:
                src_b, _ = ins[0]
                inflow_sources[src_b] = inflow_sources.get(src_b, 0.0) + out_q
            else:
                for in_b, in_q in ins:
                    inflow_sources[in_b] = (
                        inflow_sources.get(in_b, 0.0) + out_q * (in_q / total_in)
                    )

    inflow = received + sum(inflow_sources.values())
    net = _qty(inflow - consumed_total)
    return {
        "received": _qty(received),
        "inflow_sources": {k: _qty(v) for k, v in inflow_sources.items()},
        "outflow_targets": {k: _qty(v) for k, v in outflow_targets.items()},
        "inflow": _qty(inflow),
        "consumed": _qty(consumed_total),
        "outflow": _qty(sum(outflow_targets.values())),
        "loss": _qty(loss_allocated),
        # 迟到登记插入后若累计消耗超过流入，说明时间线上出现超用：
        # 不修改任何既有登记，剩余按 0 呈现并把差额显式暴露出来
        "remaining": _qty(max(0.0, net)),
        "over_consumed": _qty(max(0.0, -net)),
    }


def available_quantity(db: Session, batch_no: str, at: dt.datetime | None = None) -> float:
    """批次在给定时点的可用数量（已扣减生效登记消耗的部分）。"""
    return _ledger(db, batch_no, at)["remaining"]


def lane_snapshot(db: Session, batch_no: str) -> dict:
    """逐层明细：本批次此刻的数量账，供负责人查明来源与去向。"""
    led = _ledger(db, batch_no)
    mb = db.query(MaterialBatch).filter_by(batch_no=batch_no).first()
    return {
        "batch_no": batch_no,
        "quantity_unit": mb.quantity_unit if mb else "kg",
        "material": mb.material if mb else None,
        "status": mb.status if mb else "normal",
        "abnormal_reason": mb.abnormal_reason if mb else None,
        **led,
    }


# --------------------------------------------------------------------------
# 结构校验与流转登记
# --------------------------------------------------------------------------

def _validate_operation(
    db: Session, kind: str, inputs: list[tuple[str, float]], outputs: list[tuple[str, float]],
    loss: float, quantity_unit: str, homogeneity: bool, homogeneity_evidence: str | None,
    effective_at: dt.datetime,
) -> list[dict]:
    """返回挂起原因（空列表表示可生效）。结构非法直接拒绝；数量不齐则挂起。"""
    if kind not in (FLOW_RECEIVE, FLOW_SPLIT, FLOW_MERGE):
        raise TraceError(f"未知流转类型：{kind}")
    if not inputs:
        raise TraceError("流转登记至少需要一条投入行")
    if kind == FLOW_RECEIVE and len(inputs) > 1:
        raise TraceError("收货登记只能有一个来料批次作为投入")
    if kind == FLOW_SPLIT and len(inputs) != 1:
        raise TraceError("拆分必须恰好来自一个上游批次")
    if kind == FLOW_MERGE and len(inputs) < 2:
        raise TraceError("混合至少需要两个投入批次")
    if not outputs:
        raise TraceError("流转登记至少需要一条产出行")
    if loss < -_QTY_EPS:
        raise TraceError("损耗不得为负")
    if homogeneity and not (homogeneity_evidence or "").strip():
        raise TraceError("声明保留同质性依据时必须给出依据说明（homogeneity_evidence）")
    if not (quantity_unit or "").strip():
        raise TraceError("缺少数量单位")
    for batch_no, q in inputs + outputs:
        if not (batch_no or "").strip():
            raise TraceError("批次编号不得为空")
        if q <= _QTY_EPS:
            raise TraceError(f"批次 {batch_no} 的数量必须大于 0")

    reasons: list[dict] = []
    for batch_no, _ in inputs + outputs:
        mb = db.query(MaterialBatch).filter_by(batch_no=batch_no).first()
        if mb is not None and mb.quantity_unit != quantity_unit:
            reasons.append({
                "code": SUSPEND_UNIT,
                "message": (
                    f"批次 {batch_no} 的数量单位登记为 {mb.quantity_unit}，"
                    f"本次登记为 {quantity_unit}"
                ),
            })

    in_total = sum(q for _, q in inputs)
    out_total = sum(q for _, q in outputs)
    if abs(in_total - (out_total + loss)) > _QTY_EPS:
        reasons.append({
            "code": SUSPEND_QUANTITY_MISMATCH,
            "message": (
                f"投入合计 {in_total:g} != 产出合计 {out_total:g} + 损耗 {loss:g}"
                f"（差额 {in_total - out_total - loss:+g}）；已挂起，不修改既有数量事实"
            ),
            "input_total": in_total,
            "output_total": out_total,
            "loss": loss,
            "difference": _qty(in_total - out_total - loss),
        })

    for batch_no, need in inputs:
        if kind == FLOW_RECEIVE:
            continue
        available = available_quantity(db, batch_no, at=effective_at)
        if need > available + _QTY_EPS:
            reasons.append({
                "code": SUSPEND_INSUFFICIENT,
                "message": (
                    f"批次 {batch_no} 在生效时点 {effective_at.isoformat()} 可用量 "
                    f"{available:g}，本次需投入 {need:g}；不足，已挂起"
                    "（并发争用同一剩余数量时只接受满足可用量的一份）"
                ),
                "batch_no": batch_no,
                "available": available,
                "requested": need,
            })
    return reasons


def _would_create_cycle(db: Session, inputs: list[str], outputs: list[str]) -> list[str]:
    """若本次登记让某产出批次能回到某投入批次，即为成环。返回环上的批次对。"""
    edges: dict[str, set[str]] = {}
    for op in db.query(FlowOperation).filter(FlowOperation.status == FLOW_ACTIVE).all():
        ins = [ln.batch_no for ln in op.lines if ln.direction == "input"]
        outs = [ln.batch_no for ln in op.lines if ln.direction == "output"]
        for i in ins:
            edges.setdefault(i, set()).update(outs)
    for src in inputs:
        for dst in outputs:
            edges.setdefault(src, set()).add(dst)

    bad: list[str] = []
    for src in outputs:
        stack, seen = [src], set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for nxt in edges.get(cur, ()):
                if nxt in inputs:
                    bad.append(f"{src} -> ... -> {nxt}")
                stack.append(nxt)
    return bad


def _ensure_batches(
    db: Session, kind: str, inputs: list[tuple[str, float]], outputs: list[tuple[str, float]],
    quantity_unit: str, material: str | None,
) -> None:
    """确保所有涉及批次都有台账行，并对被消耗的批次加行锁（串行化可用量检查）。"""
    for batch_no, _ in inputs + outputs:
        is_external = kind == FLOW_RECEIVE and any(b == batch_no for b, _ in inputs)
        if db.query(MaterialBatch).filter_by(batch_no=batch_no).first() is None:
            db.add(MaterialBatch(
                batch_no=batch_no,
                material=material,
                quantity_unit=quantity_unit,
                external=is_external,
            ))
    db.flush()
    if kind == FLOW_RECEIVE:
        return
    # 并发争用同一剩余数量：按编号顺序锁住被投入的批次行，后到者看到的可用量已扣减
    for batch_no in sorted({b for b, _ in inputs}):
        (
            db.query(MaterialBatch)
            .filter_by(batch_no=batch_no)
            .with_for_update()
            .first()
        )


def register_operation(
    db: Session, *,
    kind: str,
    inputs: list[tuple[str, float]],
    outputs: list[tuple[str, float]],
    loss: float = 0.0,
    quantity_unit: str = "kg",
    effective_at: dt.datetime | None = None,
    homogeneity_retained: bool = False,
    homogeneity_evidence: str | None = None,
    material: str | None = None,
    note: str | None = None,
    actor: str = "system",
) -> FlowOperation:
    """登记一次收货/拆分/混合。数量不齐或成环 -> 挂起（保留事实与原因）。"""
    if effective_at is None:
        effective_at = utcnow()
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=dt.timezone.utc)

    _ensure_batches(db, kind, inputs, outputs, quantity_unit, material)
    reasons = _validate_operation(
        db, kind, inputs, outputs, loss, quantity_unit,
        homogeneity_retained, homogeneity_evidence, effective_at,
    )
    if kind != FLOW_RECEIVE:
        cycles = _would_create_cycle(
            db, [b for b, _ in inputs], [b for b, _ in outputs]
        )
        if cycles:
            reasons.append({
                "code": SUSPEND_CYCLE,
                "message": "流转关系成环，已挂起：" + "；".join(cycles),
                "cycles": cycles,
            })

    op = FlowOperation(
        operation_no=f"FOP-{uuid.uuid4().hex[:10].upper()}",
        kind=kind,
        quantity_unit=quantity_unit,
        loss_quantity=_qty(loss),
        effective_at=effective_at,
        status=FLOW_SUSPENDED if reasons else FLOW_ACTIVE,
        suspend_reasons=reasons,
        homogeneity_retained=homogeneity_retained,
        homogeneity_evidence=homogeneity_evidence,
        material=material,
        note=note,
        created_by=actor,
    )
    db.add(op)
    db.flush()
    for batch_no, q in inputs:
        db.add(FlowLine(operation_id=op.id, direction="input", batch_no=batch_no, quantity=_qty(q)))
    for batch_no, q in outputs:
        db.add(FlowLine(operation_id=op.id, direction="output", batch_no=batch_no, quantity=_qty(q)))

    db.flush()
    if op.status == FLOW_ACTIVE:
        # 新登记（含迟到插入）改变实际流向 -> 受影响范围版本化重算
        recompute_all(db, reason=f"新增流转登记 {op.operation_no}", actor=actor)
    return op


def retry_operation(db: Session, operation_id: int, actor: str = "system") -> FlowOperation:
    """对挂起登记按当前台账重新校验（如补齐数量单位后重试）。原始数量不修改。"""
    op = db.get(FlowOperation, operation_id)
    if op is None:
        raise TraceError(f"流转登记 {operation_id} 不存在")
    if op.status != FLOW_SUSPENDED:
        raise TraceError("只有挂起的登记允许重新校验")
    ins = [(ln.batch_no, _qty(ln.quantity)) for ln in op.lines if ln.direction == "input"]
    outs = [(ln.batch_no, _qty(ln.quantity)) for ln in op.lines if ln.direction == "output"]
    reasons = _validate_operation(
        db, op.kind, ins, outs, _qty(op.loss_quantity), op.quantity_unit,
        op.homogeneity_retained, op.homogeneity_evidence, op.effective_at,
    )
    if op.kind != FLOW_RECEIVE:
        cycles = _would_create_cycle(db, [b for b, _ in ins], [b for b, _ in outs])
        if cycles:
            reasons.append({
                "code": SUSPEND_CYCLE,
                "message": "流转关系成环，已挂起：" + "；".join(cycles),
                "cycles": cycles,
            })
    op.suspend_reasons = reasons
    if not reasons:
        op.status = FLOW_ACTIVE
        db.flush()
        recompute_all(db, reason=f"挂起登记 {op.operation_no} 恢复生效", actor=actor)
    else:
        db.flush()
    return op


def void_operation(db: Session, operation_id: int, actor: str = "system", reason: str = "") -> FlowOperation:
    op = db.get(FlowOperation, operation_id)
    if op is None:
        raise TraceError(f"流转登记 {operation_id} 不存在")
    if op.status == FLOW_VOID:
        return op
    op.status = FLOW_VOID
    op.note = (op.note + "；" if op.note else "") + f"作废：{reason or '未注明'}"
    db.flush()
    recompute_all(db, reason=f"登记 {op.operation_no} 作废", actor=actor)
    return op


# --------------------------------------------------------------------------
# 检验引用绑定
# --------------------------------------------------------------------------

def _validate_binding_result(db: Session, result_id: int):
    result = db.get(StandardizedResult, result_id)
    if result is None:
        raise TraceError(f"结果 {result_id} 不存在")
    group = result.group
    if group is None or group.current_result_id != result.id:
        raise TraceError("只能绑定当前生效的检验结果（被替换/冲突中的版本不能作为覆盖依据）")
    if result.calc_status != "standardized":
        raise TraceError(f"结果 {result_id} 处于 {result.calc_status}，资料未齐，不能作为覆盖依据")
    if result.is_superseded:
        raise TraceError("该结果已被修订失效，不能绑定为覆盖依据")
    sample = db.query(Sample).filter_by(sample_no=group.sample_no).first()
    if sample is None:
        raise TraceError(f"样品 {group.sample_no} 无接收档案，无法确认原取样批次")
    return result, group, sample


def register_binding(
    db: Session, *,
    result_id: int,
    covered_quantity: float,
    sampling_basis: str,
    source_batch_no: str | None = None,
    homogeneity_evidence: str | None = None,
    note: str | None = None,
    actor: str = "system",
) -> InspectionBinding:
    """把一条生效检验结果绑定到原取样批次与覆盖数量。"""
    result, group, sample = _validate_binding_result(db, result_id)
    if source_batch_no is not None and source_batch_no != sample.batch_no:
        raise TraceError(
            f"原取样批次不符：样品 {sample.sample_no} 档案登记为 {sample.batch_no}，"
            f"登记为 {source_batch_no}；不能把别的批次结论挂到该样品上"
        )
    qty = _qty(covered_quantity)
    if qty <= _QTY_EPS:
        raise TraceError("覆盖数量必须大于 0")

    total_in = _ledger(db, sample.batch_no)["inflow"]
    if total_in <= _QTY_EPS:
        raise TraceError(
            f"批次 {sample.batch_no} 尚未登记流转数量，无法确定检验覆盖范围；"
            "请先登记收货/拆分/混合的投入产出量"
        )
    already = sum(
        _qty(b.covered_quantity)
        for b in db.query(InspectionBinding).filter_by(
            source_batch_no=sample.batch_no, status="active"
        ).all()
    )
    if qty + already > total_in + _QTY_EPS:
        raise TraceError(
            f"覆盖数量 {qty:g} 超出批次 {sample.batch_no} 的投入量 {total_in:g}"
            f"（已绑定 {already:g}）；检验只能覆盖实际取样的那部分材料"
        )

    binding = InspectionBinding(
        binding_no=f"BND-{uuid.uuid4().hex[:10].upper()}",
        result_id=result.id,
        sample_no=sample.sample_no,
        analyte=group.analyte,
        source_batch_no=sample.batch_no,
        covered_quantity=qty,
        sampling_basis=sampling_basis,
        homogeneity_evidence=homogeneity_evidence,
        note=note,
        registered_by=actor,
    )
    db.add(binding)
    db.flush()
    recompute_binding(db, binding)
    recompute_restrictions(db, recompute=False)
    return binding


# --------------------------------------------------------------------------
# 覆盖传播
# --------------------------------------------------------------------------

@dataclass
class _Lot:
    """一批带来源证据的材料份额：数量、其中仍有同质性依据的数量、流转路径。"""
    quantity: float
    proven: float
    path: list[dict] = field(default_factory=list)
    via_merge: bool = False


def _split_lot(lot: _Lot, take: float) -> _Lot:
    """按比例从 lot 中取走 take 数量，返回取走部分（proven 按同比缩减）。"""
    if lot.quantity <= _QTY_EPS:
        return _Lot(0.0, 0.0, list(lot.path), lot.via_merge)
    ratio = min(1.0, take / lot.quantity)
    return _Lot(
        quantity=_qty(lot.quantity * ratio),
        proven=_qty(lot.proven * ratio),
        path=list(lot.path),
        via_merge=lot.via_merge,
    )


def _inflow_total(db: Session, batch_no: str) -> float:
    return _ledger(db, batch_no)["inflow"]


def _simulate(db: Session, source_batch: str, covered: float) -> dict[str, dict]:
    """按时间顺序回放流转，把一条检验的覆盖量沿实际流向分配。

    - 覆盖被理解为原取样批次**每一笔流入中占固定比例**的那部分材料
      （多时点收货/分批流入时也成立），比例 = 覆盖数量 / 累计流入。
    - 覆盖材料在批次内均匀分布；拆分/混合按实际消耗比例带走覆盖。
    - 未声明保留同质性依据的登记 -> 带走的覆盖仍有数量，但 proven 归零（待补检）。
    - 多条路径汇合时，各路径各持有自己的份额，同一份材料只计一次。
    """
    ops = ordered_operations(db)
    total_in = _inflow_total(db, source_batch)
    ratio = 1.0 if total_in <= _QTY_EPS else min(1.0, covered / total_in)

    stock: dict[str, float] = {}
    lots: dict[str, list[_Lot]] = {}
    entered: dict[str, float] = {}
    entered_proven: dict[str, float] = {}
    via_merge: dict[str, bool] = {}
    chains: dict[str, list[dict]] = {}

    def seed_inflow(batch_no: str, q: float) -> None:
        """原取样批次新流入 q 时，按固定比例登记覆盖份额。"""
        if batch_no != source_batch or q <= _QTY_EPS:
            return
        part = _qty(q * ratio)
        if part <= _QTY_EPS:
            return
        lots.setdefault(batch_no, []).append(_Lot(part, part))
        entered[batch_no] = _qty(entered.get(batch_no, 0.0) + part)
        entered_proven[batch_no] = _qty(entered_proven.get(batch_no, 0.0) + part)

    for op in ops:
        if op.status != FLOW_ACTIVE:
            continue
        ins = [(ln.batch_no, _qty(ln.quantity)) for ln in op.lines if ln.direction == "input"]
        outs = [(ln.batch_no, _qty(ln.quantity)) for ln in op.lines if ln.direction == "output"]
        if not ins or not outs:
            continue
        total_in_op = sum(q for _, q in ins)
        total_out_op = sum(q for _, q in outs)
        loss = _qty(op.loss_quantity)

        moved: list[_Lot] = []
        for in_b, in_q in ins:
            if op.kind == FLOW_RECEIVE:
                stock[in_b] = _qty(max(0.0, stock.get(in_b, 0.0) - in_q))
                continue
            have = stock.get(in_b, 0.0)
            frac = 1.0 if have <= _QTY_EPS else min(1.0, in_q / have)
            stock[in_b] = _qty(max(0.0, have - in_q))
            remaining: list[_Lot] = []
            for lot in lots.get(in_b, []):
                take = _split_lot(lot, lot.quantity * frac)
                lot.quantity = _qty(lot.quantity - take.quantity)
                lot.proven = _qty(max(0.0, lot.proven - take.proven))
                if take.quantity > _QTY_EPS:
                    moved.append(take)
                if lot.quantity > _QTY_EPS:
                    remaining.append(lot)
            lots[in_b] = remaining

        if op.kind == FLOW_RECEIVE:
            for out_b, out_q in outs:
                stock[out_b] = _qty(stock.get(out_b, 0.0) + out_q)
                seed_inflow(out_b, out_q)
            continue

        # 损耗按投入比例扣减，剩余才进入产出
        keep = max(0.0, (total_in_op - loss) / total_in_op) if total_in_op else 0.0
        moved = [
            _Lot(_qty(l.quantity * keep), _qty(l.proven * keep), list(l.path), l.via_merge)
            for l in moved
        ]
        for out_b, out_q in outs:
            share = (out_q / total_out_op) if total_out_op else 0.0
            for lot in moved:
                part_q = _qty(lot.quantity * share)
                if part_q <= _QTY_EPS:
                    continue
                part_p = _qty(lot.proven * share) if op.homogeneity_retained else 0.0
                step = {
                    "operation_id": op.id,
                    "operation_no": op.operation_no,
                    "kind": op.kind,
                    "from_batch": ins[0][0] if op.kind == FLOW_SPLIT else None,
                    "from_batches": [b for b, _ in ins] if op.kind == FLOW_MERGE else None,
                    "to_batch": out_b,
                    "quantity": part_q,
                    "homogeneity_retained": bool(op.homogeneity_retained),
                    "effective_at": op.effective_at.isoformat(),
                }
                new_lot = _Lot(
                    part_q, part_p, lot.path + [step],
                    lot.via_merge or op.kind == FLOW_MERGE,
                )
                lots.setdefault(out_b, []).append(new_lot)
                entered[out_b] = _qty(entered.get(out_b, 0.0) + part_q)
                entered_proven[out_b] = _qty(entered_proven.get(out_b, 0.0) + part_p)
                if op.kind == FLOW_MERGE:
                    via_merge[out_b] = True
                chains.setdefault(out_b, []).append({
                    "quantity": part_q,
                    "proven": part_p,
                    "degraded": part_p <= _QTY_EPS and part_q > _QTY_EPS,
                    "chain": new_lot.path,
                })
            stock[out_b] = _qty(stock.get(out_b, 0.0) + out_q)
            seed_inflow(out_b, out_q)

    result: dict[str, dict] = {}
    for batch_no in set(list(entered) + [b for b, items in lots.items() if items]):
        resident = sum(l.quantity for l in lots.get(batch_no, []))
        resident_p = sum(l.proven for l in lots.get(batch_no, []))
        result[batch_no] = {
            "entered": _qty(entered.get(batch_no, 0.0)),
            "entered_proven": _qty(entered_proven.get(batch_no, 0.0)),
            "resident": _qty(resident),
            "resident_proven": _qty(resident_p),
            "via_merge": bool(via_merge.get(batch_no, False)),
            "paths": chains.get(batch_no, []),
        }
    return result


def _next_revision(db: Session, model, **filters) -> int:
    attr = "revision" if hasattr(model, "revision") else "revision_no"
    col = getattr(model, attr)
    last = db.query(model).filter_by(**filters).order_by(col.desc()).first()
    return 1 if last is None else getattr(last, attr) + 1


def _current_determinations(db: Session, binding_id: int) -> list[CoverageDetermination]:
    return (
        db.query(CoverageDetermination)
        .filter_by(binding_id=binding_id, is_current=True)
        .all()
    )


def recompute_binding(db: Session, binding: InspectionBinding) -> list[CoverageDetermination]:
    """按当前实际流向重算该检验引用的覆盖；旧版本标记非当前，不删除。"""
    db.flush()
    src = binding.source_batch_no
    covered = _qty(binding.covered_quantity)
    sim = _simulate(db, src, covered)

    # 源批次是否为某次混合的产出：混合样结果只证明混合批次本身
    merge_src = next(
        (op for op in ordered_operations(db)
         if op.status == FLOW_ACTIVE and op.kind == FLOW_MERGE
         and any(ln.batch_no == src for ln in op.lines if ln.direction == "output")),
        None,
    )

    for existing in _current_determinations(db, binding.id):
        existing.is_current = False

    out: list[CoverageDetermination] = []
    for batch_no, node in sim.items():
        entered = _qty(node["entered"])
        if entered <= _QTY_EPS:
            continue
        resident = _qty(node["resident"])
        resident_proven = _qty(min(node["resident_proven"], resident))
        pending_q = _qty(max(0.0, resident - resident_proven))

        if resident <= _QTY_EPS:
            status = COV_TRANSFERRED
            reason = "覆盖材料已全部转出该批次，覆盖随流转记入下游批次"
            pending_q = 0.0
        elif batch_no == src and merge_src is not None:
            status = COV_MIXED_ONLY
            reason = (
                "该检验取自混合批次本身，只能证明混合批次整体，不能自动证明每个组成批次分别合格；"
                "各组成批次需各自的取样覆盖"
            )
        elif batch_no == src:
            status = COV_VALID
            reason = "检验直接取自该批次，覆盖其实际取样的数量"
        elif resident_proven <= _QTY_EPS and pending_q > _QTY_EPS:
            status = COV_PENDING_REINSPECTION
            reason = "沿流转继承的覆盖缺可追溯的同质性依据，该部分列为待补检，暂不放行"
        elif node["via_merge"]:
            status = COV_PARTIAL
            reason = (
                "经混合流入，仅覆盖本批次中来自原取样批次的份额，不能代表其余组成批次"
                + ("；其中部分路径缺同质性依据，待补检" if pending_q > _QTY_EPS else "")
            )
        elif pending_q > _QTY_EPS:
            status = COV_PARTIAL
            reason = "部分路径缺同质性依据，已覆盖部分放行，其余待补检"
        else:
            status = COV_VALID
            reason = "经拆分继承，拆分登记声明保留同质性依据"

        rev = _next_revision(
            db, CoverageDetermination, binding_id=binding.id, covered_batch_no=batch_no
        )
        det = CoverageDetermination(
            binding_id=binding.id,
            result_id=binding.result_id,
            sample_no=binding.sample_no,
            analyte=binding.analyte,
            source_batch_no=src,
            covered_batch_no=batch_no,
            revision=rev,
            is_current=True,
            entered_quantity=entered,
            entered_proven_quantity=_qty(min(node["entered_proven"], entered)),
            covered_quantity=resident,
            pending_quantity=pending_q,
            status=status,
            via_merge=bool(node["via_merge"]),
            paths=node["paths"],
            reason=reason,
        )
        db.add(det)
        out.append(det)
    db.flush()
    return out


# --------------------------------------------------------------------------
# 批次限制
# --------------------------------------------------------------------------

def abnormal_provenance(db: Session) -> dict[str, list[dict]]:
    """对每个异常批次，沿实际流向计算其材料在各下游批次的残留数量（汇合去重）。"""
    out: dict[str, list[dict]] = {}
    abnormal = db.query(MaterialBatch).filter_by(status="abnormal").all()
    for mb in abnormal:
        led = _ledger(db, mb.batch_no)
        sim = _simulate(db, mb.batch_no, led["inflow"])
        for batch_no, node in sim.items():
            if batch_no == mb.batch_no:
                continue
            resident = _qty(node["resident"])
            if resident <= _QTY_EPS:
                continue
            out.setdefault(batch_no, []).append({
                "kind": "upstream_anomaly",
                "upstream_batch_no": mb.batch_no,
                "batch_no": batch_no,
                "quantity": resident,
                "reason": mb.abnormal_reason,
                "paths": node["paths"],
                "message": (
                    f"上游异常批次 {mb.batch_no} 的材料仍有 {resident:g} 留在本批次，"
                    "该部分同样暂扣，不得因下游另有检验而继续放行"
                ),
            })
    return out


def _chain_hits(db: Session, det: CoverageDetermination, abnormal: set[str]) -> bool:
    """该覆盖的流转路径是否经过已被确认异常的批次。

    经过异常批次的那部分覆盖，其材料正属于异常来源，不能用来放行。
    """
    for item in det.paths or []:
        for link in item.get("chain") or []:
            involved = {link.get("from_batch"), link.get("to_batch")}
            involved.discard(None)
            involved.update(link.get("from_batches") or [])
            if involved & abnormal:
                return True
    return False


def _binding_detail(db: Session, d: CoverageDetermination) -> dict:
    binding = db.get(InspectionBinding, d.binding_id)
    result = db.get(StandardizedResult, d.result_id)
    return {
        "binding_id": d.binding_id,
        "binding_no": binding.binding_no if binding else None,
        "result_id": d.result_id,
        "source_batch_no": d.source_batch_no,
        "sample_no": d.sample_no,
        "analyte": d.analyte,
        "covered_quantity": _qty(d.covered_quantity),
        "pending_quantity": _qty(d.pending_quantity),
        "status": d.status,
        "paths": d.paths,
        "reason": d.reason,
        "result_reading": None if result is None else {
            "value_type": result.value_type,
            "interval": [
                None if result.lower_bound is None else float(result.lower_bound),
                None if result.upper_bound is None else float(result.upper_bound),
            ],
            "unit": result.canonical_unit,
        },
    }


def recompute_restrictions(
    db: Session, reason: str = "", actor: str = "system", recompute: bool = True,
) -> list[BatchRestriction]:
    """按当前覆盖与流向，重算每个批次的释放/暂扣数量（版本化留存）。

    逐批次口径：
      remaining = 累计流入 - 累计被消耗
      released  = min(remaining, 仍留在该批次的“有依据覆盖”) - 上游异常流入份额
      held      = remaining - released
    异常批次本身：released=0，held=remaining，status=blocked。
    """
    db.flush()
    if recompute:
        for binding in db.query(InspectionBinding).filter_by(status="active").all():
            recompute_binding(db, binding)
        db.flush()

    batches = {mb.batch_no for mb in db.query(MaterialBatch).filter_by(external=False).all()}
    for op in ordered_operations(db):
        if op.kind == FLOW_RECEIVE:
            continue
        for ln in op.lines:
            batches.add(ln.batch_no)

    provenance = abnormal_provenance(db)
    abnormal_batches = {mb.batch_no for mb in db.query(MaterialBatch).filter_by(status="abnormal").all()}
    results: list[BatchRestriction] = []
    for batch_no in sorted(batches):
        ledger = _ledger(db, batch_no)
        total = ledger["inflow"]
        transferred = ledger["consumed"]
        remaining = ledger["remaining"]

        dets = (
            db.query(CoverageDetermination)
            .filter_by(covered_batch_no=batch_no, is_current=True)
            .all()
        )
        mb0 = db.query(MaterialBatch).filter_by(batch_no=batch_no).first()
        if (
            total <= _QTY_EPS
            and not dets
            and not (mb0 is not None and mb0.status == "abnormal")
        ):
            # 只出现在挂起登记里的编号：既无数量也无覆盖，不出限制记录
            continue
        # 同一检验对同一批次以当前唯一判定计（数量按覆盖余量，而非累计进入量）
        releasable_by_analyte: dict[str, float] = {}
        bases: list[dict] = []
        seen_bindings: set[int] = set()
        for d in sorted(dets, key=lambda x: (x.binding_id, -x.covered_quantity)):
            if d.binding_id in seen_bindings:
                continue
            seen_bindings.add(d.binding_id)
            resident = _qty(d.covered_quantity)
            pending = _qty(d.pending_quantity)
            proven = _qty(max(0.0, resident - pending))
            if d.status in (COV_VALID, COV_PARTIAL) and not _chain_hits(db, d, abnormal_batches):
                releasable_by_analyte[d.analyte] = _qty(
                    releasable_by_analyte.get(d.analyte, 0.0) + proven
                )
            elif d.status in (COV_VALID, COV_PARTIAL):
                bases.append({
                    "kind": "anomalous_coverage",
                    "quantity": proven,
                    "message": (
                        "该检验的覆盖路径经过已确认异常的批次，其覆盖数量按异常来源处理，不再放行"
                    ),
                    **_binding_detail(db, d),
                })
            if pending > _QTY_EPS or d.status in (
                COV_PENDING_REINSPECTION, COV_MIXED_ONLY
            ):
                bases.append({
                    "kind": (
                        "pending_reinspection"
                        if d.status in (COV_PENDING_REINSPECTION, COV_PARTIAL)
                        else "mixed_only"
                    ),
                    "quantity": pending,
                    "message": d.reason,
                    **_binding_detail(db, d),
                })

        mb = db.query(MaterialBatch).filter_by(batch_no=batch_no).first()
        blocked_by_anomaly = mb is not None and mb.status == "abnormal"
        anomaly = None
        if blocked_by_anomaly:
            anomaly = (
                db.query(BatchAnomaly)
                .filter_by(batch_no=batch_no, status="active")
                .order_by(BatchAnomaly.id.desc())
                .first()
            )
            bases.insert(0, {
                "kind": "batch_anomaly",
                "batch_no": batch_no,
                "anomaly_id": anomaly.id if anomaly else None,
                "reason": mb.abnormal_reason,
                "quantity": remaining,
                "message": f"批次已被确认异常（{mb.abnormal_reason or '原因未注明'}），尚存数量不得放行",
            })

        upstream_anomaly = provenance.get(batch_no, [])
        bases.extend(upstream_anomaly)
        if ledger.get("over_consumed", 0.0) > _QTY_EPS:
            bases.append({
                "kind": "quantity_over_consumed",
                "batch_no": batch_no,
                "quantity": ledger["over_consumed"],
                "message": (
                    f"该批次累计转入 {total:g}，但已登记转出与损耗合计超出 "
                    f"{ledger['over_consumed']:g}（多为迟到登记与既有登记重叠）；"
                    "不修改既有登记，超用部分单列待核"
                ),
            })
        upstream_held = _qty(sum(item["quantity"] for item in upstream_anomaly))

        # 按项目分别统计覆盖，取最弱的项目作为该批次可放行上限：
        # 多个项目都覆盖到的材料才可放行，不能拿覆盖最多的项目代表整批
        releasable = min(releasable_by_analyte.values()) if releasable_by_analyte else 0.0
        if blocked_by_anomaly:
            released = 0.0
            held = remaining
            status = RESTRICT_BLOCKED
            bases.append({
                "kind": "anomaly_remaining",
                "batch_no": batch_no,
                "quantity": remaining,
                "message": "异常批次尚存数量全部暂扣，待确认后处置",
            })
        else:
            # 异常来源份额先扣掉，剩余部分才可能凭覆盖证据放行
            releasable_qty = max(0.0, remaining - upstream_held)
            released = _qty(max(0.0, min(releasable_qty, releasable)))
            if total > _QTY_EPS and releasable <= _QTY_EPS:
                bases.insert(0, {
                    "kind": "uncovered_remaining",
                    "batch_no": batch_no,
                    "quantity": remaining,
                    "message": "该批次尚无任何检验覆盖证据，尚存数量暂不放行",
                })
            held = _qty(max(0.0, remaining - released))
            if held > _QTY_EPS and released > _QTY_EPS:
                status = RESTRICT_PARTIAL
            elif held > _QTY_EPS:
                status = RESTRICT_RESTRICTED
            else:
                status = RESTRICT_CLEAR
            if (
                held > _QTY_EPS
                and total > _QTY_EPS
                and not any(
                    b["kind"] in (
                        "pending_reinspection", "mixed_only", "upstream_anomaly",
                        "uncovered_remaining", "anomalous_coverage",
                    )
                    for b in bases
                )
            ):
                bases.append({
                    "kind": "uncovered_remaining",
                    "batch_no": batch_no,
                    "quantity": _qty(max(0.0, remaining - min(remaining, releasable))),
                    "message": "尚存数量中没有对应的取样覆盖证据，暂不放行",
                })

        rev = _next_revision(db, BatchRestriction, batch_no=batch_no)
        for old in db.query(BatchRestriction).filter_by(batch_no=batch_no, is_current=True).all():
            old.is_current = False
        rec = BatchRestriction(
            batch_no=batch_no,
            revision=rev,
            is_current=True,
            total_quantity=_qty(total),
            transferred_quantity=_qty(transferred),
            remaining_quantity=_qty(remaining),
            released_quantity=_qty(released),
            held_quantity=_qty(held),
            status=status,
            bases=bases,
        )
        db.add(rec)
        results.append(rec)
    db.flush()
    return results


def _coverage_signature(db: Session, binding: InspectionBinding) -> tuple:
    dets = _current_determinations(db, binding.id)
    return tuple(sorted(
        (d.covered_batch_no, d.status,
         _qty(d.covered_quantity), _qty(d.pending_quantity))
        for d in dets
    ))


def recompute_all(db: Session, reason: str = "", actor: str = "system") -> list[BatchRestriction]:
    """全量重算（流转登记变化/异常确认后调用），并为受影响批次的已签报告追加修订说明。"""
    db.flush()
    bindings = db.query(InspectionBinding).filter_by(status="active").all()
    before = {b.id: _coverage_signature(db, b) for b in bindings}

    for binding in bindings:
        recompute_binding(db, binding)
    db.flush()
    restrictions = recompute_restrictions(db, reason=reason, actor=actor, recompute=False)

    affected: set[str] = {r.batch_no for r in restrictions}
    # 覆盖范围本身发生变化的原取样批次同样属于“受影响批次”
    for binding in bindings:
        if _coverage_signature(db, binding) != before[binding.id]:
            affected.add(binding.source_batch_no)

    _auto_report_revisions(db, affected, reason=reason, actor=actor)
    return restrictions


def _auto_report_revisions(
    db: Session, affected_batches: set[str], reason: str, actor: str
) -> None:
    """已签报告所在批次的重算结果有变化时，追加修订说明（不改旧报告原文）。"""
    if not affected_batches:
        return
    reports = (
        db.query(Report)
        .filter(Report.status == "approved", Report.batch_no.in_(affected_batches))
        .all()
    )
    for report in reports:
        restriction = (
            db.query(BatchRestriction)
            .filter_by(batch_no=report.batch_no, is_current=True)
            .order_by(BatchRestriction.revision.desc())
            .first()
        )
        if restriction is None:
            continue
        # 相对上一版确有变化才追发；首次算出该批次限制时不追发
        prev = (
            db.query(BatchRestriction)
            .filter_by(batch_no=report.batch_no, is_current=False)
            .order_by(BatchRestriction.revision.desc())
            .first()
        )
        restriction_changed = prev is not None and not (
            prev.status == restriction.status
            and abs(float(prev.released_quantity) - float(restriction.released_quantity)) < _QTY_EPS
            and abs(float(prev.held_quantity) - float(restriction.held_quantity)) < _QTY_EPS
        )
        if not restriction_changed:
            continue
        signature = {
            "restriction_revision": restriction.revision,
            "status": restriction.status,
            "released_quantity": _qty(restriction.released_quantity),
            "held_quantity": _qty(restriction.held_quantity),
        }
        last = (
            db.query(ReportRevision)
            .filter_by(report_id=report.id)
            .order_by(ReportRevision.revision_no.desc())
            .first()
        )
        if last is not None and last.snapshot.get("restriction") == signature:
            continue
        add_report_revision(
            db, report.id,
            reason=reason or "流转重算导致适用范围变化",
            note=(
                "依据最新流转登记与检验覆盖重算，本报告所涉批次的可放行数量发生变化；"
                "原报告结论不作修改，以本修订说明为准"
            ),
            affected_batch_no=report.batch_no,
            actor=actor,
        )


# --------------------------------------------------------------------------
# 异常确认与影响面
# --------------------------------------------------------------------------

def confirm_anomaly(
    db: Session, batch_no: str, reason: str, actor: str = "system"
) -> BatchAnomaly:
    """确认某批次异常：沿实际流向计算尚存/已转出范围，并触发重算。"""
    mb = db.query(MaterialBatch).filter_by(batch_no=batch_no).first()
    if mb is None:
        mb = MaterialBatch(batch_no=batch_no)
        db.add(mb)
        db.flush()
    mb.status = "abnormal"
    mb.abnormal_reason = reason

    impact = anomaly_impact(db, batch_no)
    anomaly = BatchAnomaly(
        batch_no=batch_no,
        reason=reason,
        status="active",
        confirmed_by=actor,
        impact=impact,
    )
    db.add(anomaly)
    db.flush()
    recompute_all(db, reason=f"批次 {batch_no} 确认异常", actor=actor)
    return anomaly


def anomaly_impact(db: Session, batch_no: str) -> dict:
    """沿实际流向计算异常影响：尚存数量 + 已转出到各下游的实际数量（汇合去重）。"""
    ledger = _ledger(db, batch_no)
    sim = _simulate(db, batch_no, ledger["inflow"])

    direct_out: list[dict] = []
    for op in ordered_operations(db):
        if op.status != FLOW_ACTIVE:
            continue
        ins = [(ln.batch_no, _qty(ln.quantity)) for ln in op.lines if ln.direction == "input"]
        outs = [(ln.batch_no, _qty(ln.quantity)) for ln in op.lines if ln.direction == "output"]
        if not any(b == batch_no for b, _ in ins):
            continue
        total_in = sum(q for _, q in ins) or 1.0
        mine = sum(q for b, q in ins if b == batch_no)
        for out_b, out_q in outs:
            q = out_q if op.kind == FLOW_SPLIT else out_q * (mine / total_in)
            direct_out.append({
                "operation_no": op.operation_no,
                "kind": op.kind,
                "to_batch": out_b,
                "quantity": _qty(q),
                "effective_at": op.effective_at.isoformat(),
                "homogeneity_retained": bool(op.homogeneity_retained),
            })

    layers = [
        {
            "batch_no": b,
            "entered_quantity": node["entered"],
            "resident_quantity": node["resident"],
            "via_merge": node["via_merge"],
            "paths": node["paths"],
        }
        for b, node in sim.items()
        if b != batch_no and node["entered"] > _QTY_EPS
    ]
    direct_total = sum(item["quantity"] for item in direct_out)
    return {
        "batch_no": batch_no,
        "remaining_quantity": ledger["remaining"],
        "transferred_quantity": _qty(direct_total),
        "direct_downstream": direct_out,
        "affected_downstream": layers,
        "note": (
            "已转出数量按各登记的实际投入/产出份额分配；多条路径汇合时同一份材料只计一次。"
            "尚存数量不因异常确认而修改，只做限制。"
        ),
    }


# --------------------------------------------------------------------------
# 报告修订
# --------------------------------------------------------------------------

def add_report_revision(
    db: Session, report_id: int, *, reason: str, note: str,
    affected_batch_no: str | None = None, actor: str = "system",
) -> ReportRevision:
    """为已签报告追加修订说明：不改旧报告原文，不把旧报告改成另一批次。"""
    report = db.get(Report, report_id)
    if report is None:
        raise TraceError(f"报告 {report_id} 不存在")
    if report.status != "approved":
        raise TraceError("只有已签发（approved）的报告需要以修订说明方式更正")
    rev = _next_revision(db, ReportRevision, report_id=report_id)
    snapshot: dict = {
        "report_no": report.report_no,
        "batch_no": report.batch_no,
        "status": report.status,
        "result_refs": report.result_refs,
    }
    if affected_batch_no is not None:
        restriction = (
            db.query(BatchRestriction)
            .filter_by(batch_no=affected_batch_no, is_current=True)
            .order_by(BatchRestriction.revision.desc())
            .first()
        )
        if restriction is not None:
            snapshot["restriction"] = {
                "restriction_revision": restriction.revision,
                "status": restriction.status,
                "released_quantity": _qty(restriction.released_quantity),
                "held_quantity": _qty(restriction.held_quantity),
            }
            snapshot["affected_batch_restriction"] = {
                "batch_no": restriction.batch_no,
                "revision": restriction.revision,
                "status": restriction.status,
                "total_quantity": _qty(restriction.total_quantity),
                "transferred_quantity": _qty(restriction.transferred_quantity),
                "remaining_quantity": _qty(restriction.remaining_quantity),
                "released_quantity": _qty(restriction.released_quantity),
                "held_quantity": _qty(restriction.held_quantity),
                "bases": restriction.bases,
            }
    rec = ReportRevision(
        report_id=report_id,
        revision_no=rev,
        reason=reason,
        note=note,
        affected_batch_no=affected_batch_no,
        snapshot=snapshot,
        created_by=actor,
    )
    db.add(rec)
    db.flush()
    return rec
