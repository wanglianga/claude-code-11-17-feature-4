"""整批结论汇总。

单个项目通过 != 整批可用。生成批次结论的前置条件（全部满足才出 pass/fail/review）：
1. 取样归属明确：批次下每个样品都有档案且未失效（失效样品不能为有效批次背书）；
2. 必检项目齐备：该基质每条 required 规则在每个有效样品上都有生效结果；
3. 冲突已处理：不存在 open 冲突；
4. 待补清零：生效结果无 pending_* 状态。
任一不满足 -> hold，并在 detail 中列明缺口。判定走区间规则，横跨边界即 needs_review。

流转层叠加（仅在已登记流转的批次上生效）：
5. 适用范围收紧：该批次尚存数量中没有检验覆盖证据、或继承覆盖缺同质性依据的部分
   暂不放行。存在暂扣数量 -> 结论不得为 pass（降为 needs_review 并列明暂扣清单），
   批次被确认异常 -> hold。登记过收货但尚无任何覆盖时同样不能直接 pass。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.judgment import (
    VERDICT_CONFORM,
    VERDICT_NONCONFORM,
    VERDICT_REVIEW,
    judge_result,
)
from app.models import (
    BATCH_HOLD,
    BATCH_FAIL,
    BATCH_PASS,
    BATCH_REVIEW,
    CONFLICT_OPEN,
    RESTRICT_BLOCKED,
    RESTRICT_CLEAR,
    RESTRICT_PARTIAL,
    RESTRICT_RESTRICTED,
    SAMPLE_VALID,
    BatchConclusion,
    BatchRestriction,
    Conflict,
    DecisionRule,
    ResultGroup,
    Sample,
    StandardizedResult,
)
from app.units import UnitError, get_unit_map, resolve_conversion, scale_interval


def _interval_in_rule_unit(
    result: StandardizedResult, rule: DecisionRule, unit_map: dict, matrix: str
) -> tuple[float, float] | None:
    """把标准化区间换算到规则限值单位；量纲不符返回 None。"""
    try:
        factor, _, _ = resolve_conversion(
            result.canonical_unit, rule.limit_unit, matrix, unit_map
        )
    except UnitError:
        return None
    lower = float(result.lower_bound) * factor
    upper = float(result.upper_bound) * factor if result.upper_bound is not None else lower
    return (lower, upper) if lower <= upper else (upper, lower)


def evaluate_batch(db: Session, batch_no: str) -> BatchConclusion:
    samples = db.query(Sample).filter_by(batch_no=batch_no).all()
    blockers: list[str] = []
    item_details: list[dict] = []

    # 前置条件 1：样品档案与有效性
    if not samples:
        blockers.append(f"批次 {batch_no} 下没有任何样品接收记录，取样归属不明确")
    invalid_samples = [s for s in samples if s.status != SAMPLE_VALID]
    for s in invalid_samples:
        blockers.append(
            f"样品 {s.sample_no} 已失效（{s.invalid_reason or '原因未注明'}），其结果不能参与有效报告"
        )
    valid_samples = [s for s in samples if s.status == SAMPLE_VALID]
    matrices = {s.matrix for s in valid_samples}

    # 前置条件 3：批次相关样品上是否有未解决冲突
    sample_nos = [s.sample_no for s in samples]
    open_conflicts = (
        db.query(Conflict)
        .join(ResultGroup, Conflict.group_id == ResultGroup.id)
        .filter(
            ResultGroup.sample_no.in_(sample_nos or ["__none__"]),
            Conflict.status == CONFLICT_OPEN,
        )
        .all()
    )
    for c in open_conflicts:
        blockers.append(f"冲突未处理（conflict_id={c.id}）：{c.reason}")

    unit_map = get_unit_map(db)
    rules = db.query(DecisionRule).filter(DecisionRule.matrix.in_(matrices or ["__none__"])).all()
    required_rules = [r for r in rules if r.required]

    worst = BATCH_PASS

    # 前置条件 2/4：逐样品逐必检项目核对
    for sample in valid_samples:
        for rule in required_rules:
            group = (
                db.query(ResultGroup)
                .filter_by(sample_no=sample.sample_no, analyte=rule.analyte)
                .first()
            )
            if group is None or group.current_result_id is None:
                blockers.append(
                    f"样品 {sample.sample_no} 缺少必检项目 {rule.analyte} 的生效结果"
                )
                continue
            result = db.get(StandardizedResult, group.current_result_id)
            if result.calc_status != "standardized":
                blockers.append(
                    f"样品 {sample.sample_no} 项目 {rule.analyte} 仍处于 {result.calc_status}：{result.calc_note}"
                )
                continue

            interval = _interval_in_rule_unit(result, rule, unit_map, sample.matrix)
            judgement = judge_result(result, rule, interval)
            item_details.append(
                {
                    "sample_no": sample.sample_no,
                    "analyte": rule.analyte,
                    "result_id": result.id,
                    "version": result.version,
                    "interval": [
                        float(result.lower_bound),
                        None if result.upper_bound is None else float(result.upper_bound),
                    ],
                    "result_unit": result.canonical_unit,
                    "rule_code": rule.rule_code,
                    "limit": [float(rule.limit_value), rule.limit_unit],
                    "verdict": judgement.verdict,
                    "reason": judgement.reason,
                    "total_pending": result.total_pending,
                    "note": result.calc_note,
                }
            )
            if judgement.verdict == VERDICT_NONCONFORM:
                worst = BATCH_FAIL
            elif judgement.verdict == VERDICT_REVIEW and worst != BATCH_FAIL:
                worst = BATCH_REVIEW

    # 非必检但存在的结果也给出参考判定，不参与 pass/fail 否决
    required_pairs = {(s.sample_no, r.analyte) for s in valid_samples for r in required_rules}
    extra_groups = (
        db.query(ResultGroup)
        .filter(ResultGroup.sample_no.in_([s.sample_no for s in valid_samples] or ["__none__"]))
        .all()
    )
    for g in extra_groups:
        if (g.sample_no, g.analyte) in required_pairs or g.current_result_id is None:
            continue
        result = db.get(StandardizedResult, g.current_result_id)
        rule = (
            db.query(DecisionRule)
            .filter_by(analyte=g.analyte, required=False)
            .first()
        )
        if rule and result.calc_status == "standardized":
            owner = next((s for s in valid_samples if s.sample_no == g.sample_no), None)
            interval = _interval_in_rule_unit(
                result, rule, unit_map, owner.matrix if owner else "unknown"
            )
            j = judge_result(result, rule, interval)
            item_details.append(
                {
                    "sample_no": g.sample_no,
                    "analyte": g.analyte,
                    "result_id": result.id,
                    "version": result.version,
                    "verdict": j.verdict,
                    "required": False,
                    "reason": j.reason,
                }
            )

    # 流转层：可放行数量不足时收窄结论（未登记流转的批次不受影响）
    restriction = (
        db.query(BatchRestriction)
        .filter_by(batch_no=batch_no, is_current=True)
        .order_by(BatchRestriction.revision.desc())
        .first()
    )
    coverage_gaps: list[str] = []
    if restriction is not None:
        held = float(restriction.held_quantity)
        if restriction.status == RESTRICT_BLOCKED:
            blockers.append(
                f"批次 {batch_no} 已被确认异常，尚存 {held:g} 不得放行；"
                + "；".join(b.get("message", "") for b in restriction.bases if b.get("kind") == "batch_anomaly")
            )
        elif restriction.status in (RESTRICT_RESTRICTED, RESTRICT_PARTIAL, RESTRICT_CLEAR) and held > 0:
            coverage_gaps.append(
                f"尚存 {float(restriction.remaining_quantity):g} 中仅 "
                f"{float(restriction.released_quantity):g} 有检验覆盖证据，"
                f"其余 {held:g} 暂不放行（restriction_revision={restriction.revision}）"
            )
            for base in restriction.bases:
                if base.get("kind") in (
                    "pending_reinspection", "mixed_only", "uncovered_remaining", "upstream_anomaly"
                ):
                    coverage_gaps.append(
                        f"[{base.get('kind')}] {base.get('sample_no') or base.get('upstream_batch_no') or batch_no}/"
                        f"{base.get('analyte') or '-'}：{base.get('message', '')}"
                    )

    prerequisites_met = not blockers
    conclusion = BATCH_HOLD if blockers else worst
    if not blockers and coverage_gaps:
        # 部分可放行不等于整批可放行：不判 pass，交负责人按暂扣清单处置
        if conclusion == BATCH_PASS:
            conclusion = BATCH_REVIEW

    # 总量挂起不阻断浓度判定，但必须在结论中显式提示“总量仍待补”
    valid_sample_nos = [s.sample_no for s in valid_samples]
    total_warnings = [
        f"result_id={r.id}（{r.group.sample_no}/{r.group.analyte}）：{r.calc_note}"
        for r in db.query(StandardizedResult)
        .filter(StandardizedResult.total_pending.is_(True))
        .all()
        if r.group.sample_no in valid_sample_nos and not r.is_superseded
    ]

    last = (
        db.query(BatchConclusion)
        .filter_by(batch_no=batch_no)
        .order_by(BatchConclusion.version.desc())
        .first()
    )
    version = 1 if last is None else last.version + 1
    snapshot = [
        {"group_id": d["sample_no"] + ":" + d["analyte"], "result_id": d.get("result_id"),
         "version": d.get("version")}
        for d in item_details
    ]
    record = BatchConclusion(
        batch_no=batch_no,
        version=version,
        conclusion=conclusion,
        prerequisites_met=prerequisites_met,
        detail={
            "blockers": blockers,
            "total_pending_warnings": total_warnings,
            "items": item_details,
            "coverage_gaps": coverage_gaps,
            "trace_restriction": None if restriction is None else {
                "revision": restriction.revision,
                "status": restriction.status,
                "total_quantity": float(restriction.total_quantity),
                "transferred_quantity": float(restriction.transferred_quantity),
                "remaining_quantity": float(restriction.remaining_quantity),
                "released_quantity": float(restriction.released_quantity),
                "held_quantity": float(restriction.held_quantity),
                "bases": restriction.bases,
            },
            "valid_samples": [s.sample_no for s in valid_samples],
            "invalid_samples": [
                {"sample_no": s.sample_no, "reason": s.invalid_reason} for s in invalid_samples
            ],
        },
        result_snapshot=snapshot,
    )
    db.add(record)
    db.flush()
    return record
