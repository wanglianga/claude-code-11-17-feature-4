"""流转追溯测试：数量约束、覆盖传播、待补检、异常影响、迟到登记、并发争用。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app import trace
from app.batch import evaluate_batch
from app.models import (
    BATCH_HOLD,
    BATCH_PASS,
    BATCH_REVIEW,
    CALC_OK,
    COV_MIXED_ONLY,
    COV_PARTIAL,
    COV_PENDING_REINSPECTION,
    COV_VALID,
    FLOW_ACTIVE,
    QUAL_QUANTIFIED,
    FLOW_SUSPENDED,
    RESTRICT_BLOCKED,
    RESTRICT_CLEAR,
    RESTRICT_RESTRICTED,
    BatchRestriction,
    CoverageDetermination,
    ReportRevision,
    ResultGroup,
    Sample,
    StandardizedResult,
)
from app.parser import parse_file
from app.processing import persist_import
from app.reports import approve_report, submit_report
from app.trace import TraceError

DATA = Path(__file__).resolve().parents[1] / "data"
T0 = dt.datetime(2026, 11, 1, 8, 0, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2026, 11, 2, 8, 0, tzinfo=dt.timezone.utc)
T2 = dt.datetime(2026, 11, 3, 8, 0, tzinfo=dt.timezone.utc)
T3 = dt.datetime(2026, 11, 4, 8, 0, tzinfo=dt.timezone.utc)


def _import(db, name, source_ref):
    path = DATA / "raw" / name
    return persist_import(db, parse_file(path.read_bytes(), source_ref, name))


def _import_via_http(client, name, source_ref):
    from tests.conftest import auth_header
    with (DATA / "raw" / name).open("rb") as f:
        return client.post(
            "/api/import/records",
            headers=auth_header("inspector"),
            files={"file": (name, f)},
            data={"source_ref": source_ref},
        )


def _mk_result(db, sample_no, batch_no, analyte="cadmium", matrix="solid"):
    """为任意流转批次造一条生效检验结果（用于混合后取样的引用绑定）。"""
    sample = db.query(Sample).filter_by(sample_no=sample_no).first()
    if sample is None:
        sample = Sample(
            sample_no=sample_no, batch_no=batch_no, supplier="内部流转",
            material="混合料", matrix=matrix, status="valid",
        )
        db.add(sample)
        db.flush()
    group = db.query(ResultGroup).filter_by(
        sample_no=sample_no, analyte=analyte, method="ICP-MS"
    ).first()
    if group is None:
        group = ResultGroup(sample_no=sample_no, analyte=analyte, method="ICP-MS")
        db.add(group)
        db.flush()
    from app.models import ImportFile, RawRecord
    imp = db.query(ImportFile).filter_by(source_ref="MIX-LAB").first()
    if imp is None:
        imp = ImportFile(source_ref="MIX-LAB", filename="mix.json", file_format="json",
                         content_hash="mix-lab-hash", status="imported")
        db.add(imp)
        db.flush()
    raw = RawRecord(
        import_file_id=imp.id, source_ref="MIX-LAB", record_no=f"RM-{sample_no}",
        line_no=1, sample_no=sample_no, analyte=analyte, method="ICP-MS",
        raw_payload={"sample_no": sample_no, "analyte": analyte}, row_hash=f"h-{sample_no}",
    )
    db.add(raw)
    db.flush()
    result = StandardizedResult(
        group_id=group.id, version=1, raw_record_id=raw.id, source_ref="MIX-LAB",
        record_no=f"RM-{sample_no}", reading_text="0.01", reading_original=0.01,
        unit_original="mg/kg", dilution_factor=1, dilution_applied=1,
        canonical_unit="mg/kg", value_type=QUAL_QUANTIFIED, numeric_value=0.01,
        lower_bound=0.01, upper_bound=0.01, qualitative=QUAL_QUANTIFIED,
        calc_status=CALC_OK, conversion_chain=[{"from": "mg/kg", "to": "mg/kg", "factor": 1.0}],
    )
    db.add(result)
    db.flush()
    group.current_result_id = result.id
    db.flush()
    return result.id


def _current_result(db, sample_no, analyte):
    g = db.query(ResultGroup).filter_by(sample_no=sample_no, analyte=analyte).one()
    assert g.current_result_id is not None
    return g.current_result_id


@pytest.fixture()
def seeded_imports(seeded, db_session):
    _import(db_session, "lab_a_20261105.csv", "LAB-A")
    _import(db_session, "lab_b_20261105.json", "LAB-B")
    db_session.commit()
    return db_session


def _receive(db, batch_no, qty, at=T0, source=None):
    return trace.register_operation(
        db, kind="receive",
        inputs=[(source or f"SUP-{batch_no}", qty)],
        outputs=[(batch_no, qty)],
        effective_at=at,
    )


def _restriction(db, batch_no):
    return (
        db.query(BatchRestriction)
        .filter_by(batch_no=batch_no, is_current=True)
        .order_by(BatchRestriction.revision.desc())
        .first()
    )


# ---------------------------------------------------------------- 流转登记

def test_mismatched_quantity_suspends_without_rewriting_facts(db_session):
    _receive(db_session, "B-A", 1000)
    op = trace.register_operation(
        db_session, kind="split", inputs=[("B-A", 1000)],
        outputs=[("B-A1", 600), ("B-A2", 300)], loss=50, effective_at=T1,
    )
    assert op.status == FLOW_SUSPENDED
    codes = {r["code"] for r in op.suspend_reasons}
    assert "quantity_mismatch" in codes
    # 原始行数量原样保留
    assert {ln.batch_no: float(ln.quantity) for ln in op.lines} == {
        "B-A": 1000.0, "B-A1": 600.0, "B-A2": 300.0,
    }
    assert float(op.loss_quantity) == 50.0
    # 挂起登记不进入台账，既有库存事实不变
    assert trace.lane_snapshot(db_session, "B-A")["remaining"] == pytest.approx(1000)
    # 重试仍不齐 -> 保持挂起
    again = trace.retry_operation(db_session, op.id)
    assert again.status == FLOW_SUSPENDED


def test_cycle_is_suspended(db_session):
    _receive(db_session, "B-A", 100)
    _receive(db_session, "B-B", 100)
    op1 = trace.register_operation(
        db_session, kind="merge", inputs=[("B-A", 100), ("B-B", 100)],
        outputs=[("B-M", 200)], effective_at=T1,
    )
    assert op1.status == FLOW_ACTIVE
    op2 = trace.register_operation(
        db_session, kind="split", inputs=[("B-M", 200)], outputs=[("B-A", 200)],
        effective_at=T2,
    )
    assert op2.status == FLOW_SUSPENDED
    assert any(r["code"] == "cycle_detected" for r in op2.suspend_reasons)
    # 成环登记被挂起后，台账仍保持无环
    assert trace.lane_snapshot(db_session, "B-A")["remaining"] == pytest.approx(0)


def test_competing_splits_only_one_accepted(db_session):
    _receive(db_session, "B-A", 100)
    first = trace.register_operation(
        db_session, kind="split", inputs=[("B-A", 80)], outputs=[("B-A1", 80)],
        effective_at=T1,
    )
    second = trace.register_operation(
        db_session, kind="split", inputs=[("B-A", 80)], outputs=[("B-A2", 80)],
        effective_at=T1,
    )
    assert first.status == FLOW_ACTIVE
    assert second.status == FLOW_SUSPENDED
    assert any(r["code"] == "insufficient_available" for r in second.suspend_reasons)
    assert trace.lane_snapshot(db_session, "B-A")["remaining"] == pytest.approx(20)
    assert trace.lane_snapshot(db_session, "B-A2")["remaining"] == pytest.approx(0)


# ---------------------------------------------------------------- 覆盖传播

def test_split_without_homogeneity_is_pending_reinspection(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261101", 1000)
    trace.register_operation(
        db, kind="split", inputs=[("B20261101", 1000)],
        outputs=[("SP-1", 400), ("SP-2", 600)], effective_at=T1,
        homogeneity_retained=False,
    )
    binding = trace.register_binding(
        db, result_id=_current_result(db, "S-002", "cadmium"),
        covered_quantity=200, sampling_basis="S-002 代表 200 kg 来料",
    )
    dets = {
        d.covered_batch_no: d for d in
        db.query(CoverageDetermination).filter_by(binding_id=binding.id, is_current=True)
    }
    # 源批次材料已全部分出，覆盖随流转记入下游
    assert dets["B20261101"].status in (COV_VALID, "transferred")
    # 400/1000 -> 200 覆盖的一部分：80 进 SP-1，120 进 SP-2
    assert dets["SP-1"].status == COV_PENDING_REINSPECTION
    assert float(dets["SP-1"].covered_quantity) == pytest.approx(80)
    assert dets["SP-2"].status == COV_PENDING_REINSPECTION
    assert float(dets["SP-2"].pending_quantity) == pytest.approx(120)
    r1 = _restriction(db, "SP-1")
    assert r1.status == RESTRICT_RESTRICTED
    assert float(r1.released_quantity) == pytest.approx(0)
    assert float(r1.held_quantity) == pytest.approx(400)
    assert any(b["kind"] == "pending_reinspection" for b in r1.bases)


def test_split_with_homogeneity_inherits_coverage(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261101", 1000)
    trace.register_operation(
        db, kind="split", inputs=[("B20261101", 1000)],
        outputs=[("SP-1", 500), ("SP-2", 500)], loss=0, effective_at=T1,
        homogeneity_retained=True, homogeneity_evidence="同罐同批，拆前拆后留样比对一致",
    )
    binding = trace.register_binding(
        db, result_id=_current_result(db, "S-002", "cadmium"),
        covered_quantity=400, sampling_basis="S-002 代表 400 kg",
    )
    dets = {
        d.covered_batch_no: d for d in
        db.query(CoverageDetermination).filter_by(binding_id=binding.id, is_current=True)
    }
    assert dets["SP-1"].status == COV_VALID
    assert float(dets["SP-1"].covered_quantity) == pytest.approx(200)
    assert float(dets["SP-1"].pending_quantity) == pytest.approx(0)
    r1 = _restriction(db, "SP-1")
    assert float(r1.released_quantity) == pytest.approx(200)
    assert float(r1.held_quantity) == pytest.approx(300)


def test_loss_reduces_propagated_quantity(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261101", 1000)
    trace.register_operation(
        db, kind="split", inputs=[("B20261101", 1000)],
        outputs=[("SP-1", 900)], loss=100, effective_at=T1,
        homogeneity_retained=True, homogeneity_evidence="拆前拆后留样一致",
    )
    binding = trace.register_binding(
        db, result_id=_current_result(db, "S-002", "cadmium"),
        covered_quantity=1000, sampling_basis="整批取样",
    )
    det = (
        db.query(CoverageDetermination)
        .filter_by(binding_id=binding.id, covered_batch_no="SP-1", is_current=True)
        .one()
    )
    assert float(det.covered_quantity) == pytest.approx(900)


def test_merge_only_covers_share_and_mixed_sample_is_not_proof(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261101", 1000)
    _receive(db, "B-OTHER", 1000)
    trace.register_operation(
        db, kind="merge",
        inputs=[("B20261101", 500), ("B-OTHER", 500)], outputs=[("MIX-1", 1000)],
        effective_at=T1, homogeneity_retained=True, homogeneity_evidence="混合均匀性验证通过",
    )
    binding = trace.register_binding(
        db, result_id=_current_result(db, "S-002", "cadmium"),
        covered_quantity=400, sampling_basis="S-002 代表 400 kg",
    )
    det = (
        db.query(CoverageDetermination)
        .filter_by(binding_id=binding.id, covered_batch_no="MIX-1", is_current=True)
        .one()
    )
    assert det.status == COV_PARTIAL
    # 400 * 500/1000 = 200，只覆盖本批次中来自原取样批次的那一半
    assert float(det.covered_quantity) == pytest.approx(200)
    r = _restriction(db, "MIX-1")
    assert float(r.released_quantity) == pytest.approx(200)
    assert float(r.held_quantity) == pytest.approx(800)

    # 混合样自身的结果绑定后只能证明混合批次整体
    mix_result = _mk_result(db, "S-MIX-1", "MIX-1")
    mix_sample_binding = trace.register_binding(
        db, result_id=mix_result,
        covered_quantity=100, sampling_basis="混合后取样 100 kg",
    )
    statuses = {
        d.status for d in db.query(CoverageDetermination).filter_by(
            binding_id=mix_sample_binding.id, is_current=True
        )
    }
    assert COV_MIXED_ONLY in statuses


def test_binding_cannot_exceed_input_or_rebind_other_batch(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261101", 1000)
    result_id = _current_result(db, "S-002", "cadmium")
    with pytest.raises(TraceError):
        trace.register_binding(
            db, result_id=result_id, covered_quantity=1200, sampling_basis="超量绑定",
        )
    with pytest.raises(TraceError):
        trace.register_binding(
            db, result_id=result_id, covered_quantity=10,
            sampling_basis="挂到别的批次", source_batch_no="B20261102",
        )
    # 超量绑定被拒后，之前的数据不被篡改
    assert trace.lane_snapshot(db, "B20261101")["inflow"] == pytest.approx(1000)


# ---------------------------------------------------------------- 异常影响面

def test_anomaly_propagates_and_counts_merged_paths_once(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261101", 1000)
    # 拆成两半
    trace.register_operation(
        db, kind="split", inputs=[("B20261101", 1000)],
        outputs=[("SP-1", 500), ("SP-2", 500)], effective_at=T1,
        homogeneity_retained=True, homogeneity_evidence="同批拆分",
    )
    # 两个分支各自留下 100，其余再汇合
    trace.register_operation(
        db, kind="merge",
        inputs=[("SP-1", 400), ("SP-2", 400)], outputs=[("MIX-9", 800)],
        effective_at=T2, homogeneity_retained=True, homogeneity_evidence="混合均匀性验证",
    )
    anomaly = trace.confirm_anomaly(db, "B20261101", "上游供应商自检发现禁用物质", actor="reviewer")
    impact = anomaly.impact
    # B20261101 自己此刻已全部分出
    assert impact["remaining_quantity"] == pytest.approx(0)
    # MIX-9 中来自异常批次的量 = 400+400 两路，合计 800，而不是 前 800*...重复算
    layer = next(x for x in impact["affected_downstream"] if x["batch_no"] == "MIX-9")
    assert layer["resident_quantity"] == pytest.approx(800)
    # 各下游的限制：异常来源份额全部暂扣
    for batch_no in ("SP-1", "SP-2", "MIX-9"):
        r = _restriction(db, batch_no)
        assert float(r.released_quantity) == pytest.approx(0)
        assert any(b["kind"] == "upstream_anomaly" for b in r.bases)
    # 异常批次自身的限制状态
    src = _restriction(db, "B20261101")
    assert src.status in (RESTRICT_CLEAR, RESTRICT_BLOCKED)


def test_anomaly_with_separate_good_inspection_still_holds_polluted_share(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261101", 1000)
    _receive(db, "B-GOOD", 1000)
    trace.register_operation(
        db, kind="merge",
        inputs=[("B20261101", 500), ("B-GOOD", 500)], outputs=[("MIX-2", 1000)],
        effective_at=T1, homogeneity_retained=True, homogeneity_evidence="混合均一性验证",
    )
    # 混合后对 MIX-2 取样 600：能证明混合批次整体，但分不清哪一半来自异常批次
    mix_result = _mk_result(db, "S-MIX-2", "MIX-2")
    trace.register_binding(
        db, result_id=mix_result, covered_quantity=600,
        sampling_basis="混合罐取样代表 600 kg",
    )
    db.flush()
    trace.confirm_anomaly(db, "B20261101", "供应商召回", actor="reviewer")
    db.flush()
    r = _restriction(db, "MIX-2")
    # 异常批次的 500 份额没有可区分的独立覆盖 -> 至少这 500 暂扣
    assert float(r.held_quantity) >= 500 - 1e-6
    assert any(b["kind"] == "upstream_anomaly" for b in r.bases)
    assert any(b["kind"] == "mixed_only" for b in r.bases)
    # 异常批次本身全部暂扣
    src = _restriction(db, "B20261101")
    assert src.status == RESTRICT_BLOCKED
    assert float(src.held_quantity) == pytest.approx(500)


# ---------------------------------------------------------------- 迟到登记

def test_late_split_triggers_versioned_recompute(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261101", 1000)
    trace.register_operation(
        db, kind="split", inputs=[("B20261101", 600)],
        outputs=[("SP-LATE", 600)], effective_at=T2,
        homogeneity_retained=True, homogeneity_evidence="同批拆分留样",
    )
    binding = trace.register_binding(
        db, result_id=_current_result(db, "S-002", "cadmium"),
        covered_quantity=300, sampling_basis="S-002 代表 300 kg",
    )
    db.flush()
    before = (
        db.query(CoverageDetermination)
        .filter_by(binding_id=binding.id, covered_batch_no="SP-LATE", is_current=True)
        .one()
    )
    assert float(before.covered_quantity) == pytest.approx(180)
    rev_before = before.revision

    # 迟到登记：生效时点在第一次拆分之前，先拆出 400
    late = trace.register_operation(
        db, kind="split", inputs=[("B20261101", 400)],
        outputs=[("SP-EARLY", 400)], effective_at=T1,
        homogeneity_retained=True, homogeneity_evidence="同批拆分留样",
    )
    assert late.status == FLOW_ACTIVE
    trace.recompute_all(db, reason="迟到登记插入生效")
    db.flush()
    current = {
        d.covered_batch_no: d for d in
        db.query(CoverageDetermination).filter_by(
            binding_id=binding.id, is_current=True
        ).all()
    }
    # 300/1000 -> 120 先随 400 进 SP-EARLY，180 随 600 进 SP-LATE
    assert float(current["SP-EARLY"].covered_quantity) == pytest.approx(120)
    assert float(current["SP-LATE"].covered_quantity) == pytest.approx(180)
    # 旧版本保留，标记为非当前
    old = db.query(CoverageDetermination).filter_by(
        binding_id=binding.id, covered_batch_no="SP-LATE", revision=rev_before
    ).one()
    assert old.is_current is False
    # 源批次尚存 0，覆盖全部转到下游
    assert trace.lane_snapshot(db, "B20261101")["remaining"] == pytest.approx(0)


def test_approved_report_gets_revision_not_overwrite(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261102", 1000, at=T0)
    trace.register_binding(
        db, result_id=_current_result(db, "S-101", "lead"),
        covered_quantity=200, sampling_basis="S-101 代表 200 L",
    )
    db.flush()
    conclusion = evaluate_batch(db, "B20261102")
    report = submit_report(db, "B20261102", inspector="inspector", conclusion_id=conclusion.id)
    approve_report(db, report.id, reviewer="reviewer")
    db.commit()

    # 迟到登记：生效时点早于当前，材料被拆走，覆盖范围随之变化
    trace.register_operation(
        db, kind="split", inputs=[("B20261102", 1000)],
        outputs=[("SP-L1", 400), ("SP-L2", 600)], effective_at=T1,
        homogeneity_retained=False,
    )
    trace.recompute_all(db, reason="迟到拆分登记导致覆盖范围变化", actor="reviewer")
    db.commit()

    db.refresh(report)
    assert report.status == "approved"        # 旧报告原文状态不变
    assert report.batch_no == "B20261102"     # 没有把旧报告改成另一批次
    revisions = db.query(ReportRevision).filter_by(report_id=report.id).all()
    assert revisions, "重算影响已签批次时应生成修订说明"
    assert revisions[0].affected_batch_no == "B20261102"
    payload = revisions[0].snapshot
    assert payload["report_no"] == report.report_no
    assert payload["batch_no"] == "B20261102"


# ---------------------------------------------------------------- 批次结论

def test_batch_conclusion_downgrades_when_coverage_incomplete(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261102", 1000)
    trace.register_binding(
        db, result_id=_current_result(db, "S-101", "lead"),
        covered_quantity=300, sampling_basis="S-101 代表 300 L",
    )
    db.flush()
    trace.recompute_all(db)
    conclusion = evaluate_batch(db, "B20261102")
    assert conclusion.conclusion != BATCH_PASS
    assert conclusion.conclusion in (BATCH_REVIEW, BATCH_HOLD)
    assert conclusion.detail["coverage_gaps"]
    assert conclusion.detail["trace_restriction"]["released_quantity"] == pytest.approx(300)


def test_batch_conclusion_hold_on_confirmed_anomaly(seeded_imports):
    db = seeded_imports
    _receive(db, "B20261102", 1000)
    trace.register_binding(
        db, result_id=_current_result(db, "S-101", "lead"),
        covered_quantity=1000, sampling_basis="S-101 代表全部",
    )
    db.flush()
    assert evaluate_batch(db, "B20261102").conclusion == BATCH_PASS
    trace.confirm_anomaly(db, "B20261102", "复检发现异常", actor="reviewer")
    db.flush()
    conclusion = evaluate_batch(db, "B20261102")
    assert conclusion.conclusion == BATCH_HOLD
    assert any("异常" in b for b in conclusion.detail["blockers"])


# ---------------------------------------------------------------- HTTP 接口

def _auth(role):
    from tests.conftest import auth_header
    return auth_header(role)


def test_http_flow_and_coverage_endpoints(client, seeded_imports):
    _import_via_http(client, "lab_b_20261105.json", "LAB-B")
    headers = _auth("inspector")

    r = client.post("/api/trace/flows", headers=headers, json={
        "kind": "receive",
        "inputs": [{"batch_no": "SUP-1", "quantity": 1000}],
        "outputs": [{"batch_no": "B20261102", "quantity": 1000}],
        "quantity_unit": "L",
        "effective_at": T0.isoformat(),
    })
    assert r.status_code == 201
    assert r.json()["status"] == "active"

    # 投入产出不齐 -> 挂起，但 201 仍记录该次登记
    r = client.post("/api/trace/flows", headers=headers, json={
        "kind": "split",
        "inputs": [{"batch_no": "B20261102", "quantity": 400}],
        "outputs": [{"batch_no": "SP-1", "quantity": 350}],
        "loss": 20,
        "quantity_unit": "L",
        "effective_at": T1.isoformat(),
    })
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "suspended"
    assert any(x["code"] == "quantity_mismatch" for x in body["suspend_reasons"])

    lane = client.get("/api/trace/batches/B20261102/lane", headers=_auth("reviewer")).json()
    assert lane["remaining"] == pytest.approx(1000)

    # 绑定检验：原取样批次必须来自样品档案
    groups = client.get("/api/query/samples/S-101/groups", headers=headers).json()
    result_id = next(g["current_result_id"] for g in groups if g["analyte"] == "lead")
    r = client.post("/api/trace/bindings", headers=headers, json={
        "result_id": result_id,
        "covered_quantity": 600,
        "sampling_basis": "S-101 取样代表 600 L",
    })
    assert r.status_code == 201
    binding = r.json()
    assert binding["source_batch_no"] == "B20261102"
    assert any(c["status"] == "valid" for c in binding["coverage"])

    # 覆盖数量超出投入量被拒绝
    r = client.post("/api/trace/bindings", headers=headers, json={
        "result_id": result_id, "covered_quantity": 2000,
        "sampling_basis": "超量",
    })
    assert r.status_code == 400

    # 换个批次编号绑定同一检验 -> 拒绝
    r = client.post("/api/trace/bindings", headers=headers, json={
        "result_id": result_id, "covered_quantity": 10,
        "sampling_basis": "挂到别的批次", "source_batch_no": "B20261101",
    })
    assert r.status_code == 400
    assert "原取样批次不符" in r.json()["detail"]

    cov = client.get("/api/trace/coverage/B20261102", headers=headers).json()
    assert cov["determinations"]
    restriction = client.get("/api/trace/restrictions/B20261102", headers=headers).json()
    # 600 有覆盖可以放行，剩余 400 无覆盖证据，状态为“部分放行”
    assert restriction[0]["status"] == "partial"
    assert restriction[0]["released_quantity"] == pytest.approx(600)
    assert restriction[0]["held_quantity"] == pytest.approx(400)


def test_http_anomaly_and_report_revision(client, seeded_imports):
    _import_via_http(client, "lab_b_20261105.json", "LAB-B")
    inspector = _auth("inspector")
    reviewer = _auth("reviewer")
    client.post("/api/trace/flows", headers=inspector, json={
        "kind": "receive",
        "inputs": [{"batch_no": "SUP-2", "quantity": 1000}],
        "outputs": [{"batch_no": "B20261102", "quantity": 1000}],
        "quantity_unit": "L", "effective_at": T0.isoformat(),
    })
    groups = client.get("/api/query/samples/S-101/groups", headers=inspector).json()
    result_id = next(g["current_result_id"] for g in groups if g["analyte"] == "lead")
    client.post("/api/trace/bindings", headers=inspector, json={
        "result_id": result_id, "covered_quantity": 1000,
        "sampling_basis": "S-101 全部取样",
    })

    conclusion = client.post("/api/reports/batch-conclusions", headers=inspector,
                             json={"batch_no": "B20261102"}).json()
    assert conclusion["conclusion"] == "pass"
    report = client.post("/api/reports/submit", headers=inspector,
                         json={"batch_no": "B20261102"}).json()
    approved = client.post(f"/api/reports/{report['id']}/approve", headers=reviewer)
    assert approved.status_code == 200

    # 确认异常 -> 影响面 + 限制全部暂扣
    r = client.post("/api/trace/anomalies", headers=reviewer,
                    json={"batch_no": "B20261102", "reason": "供应商召回通知"})
    assert r.status_code == 201
    impact = r.json()["impact"]
    assert impact["remaining_quantity"] == pytest.approx(1000)
    r = client.get("/api/trace/restrictions/B20261102", headers=inspector).json()
    assert r[0]["status"] == "blocked"
    assert r[0]["held_quantity"] == pytest.approx(1000)

    # 已签报告：追加修订说明，不修改原文
    r = client.post(f"/api/trace/reports/{report['id']}/revisions", headers=reviewer, json={
        "reason": "批次异常确认",
        "note": "该批次材料已全部暂扣，原结论不再适用于放行",
        "affected_batch_no": "B20261102",
    })
    assert r.status_code == 201
    # 异常确认触发的自动修订说明可能已存在；手工修订在其后追加
    assert r.json()["revision_no"] >= 1
    report_now = next(
        x for x in client.get("/api/reports?batch_no=B20261102", headers=inspector).json()
        if x["id"] == report["id"]
    )
    assert report_now["status"] == "approved"
    assert report_now["batch_no"] == "B20261102"
    # inspector 无权签发修订说明
    r = client.post(f"/api/trace/reports/{report['id']}/revisions", headers=inspector, json={
        "reason": "无权限", "note": "不应通过",
    })
    assert r.status_code == 403


def test_http_retry_and_void_suspended_flow(client, seeded_imports):
    inspector = _auth("inspector")
    reviewer = _auth("reviewer")
    client.post("/api/trace/flows", headers=inspector, json={
        "kind": "receive",
        "inputs": [{"batch_no": "SUP-3", "quantity": 1000}],
        "outputs": [{"batch_no": "B20261102", "quantity": 1000}],
        "quantity_unit": "L", "effective_at": T0.isoformat(),
    })
    bad = client.post("/api/trace/flows", headers=inspector, json={
        "kind": "split",
        "inputs": [{"batch_no": "B20261102", "quantity": 500}],
        "outputs": [{"batch_no": "SP-X", "quantity": 450}],
        "loss": 20, "quantity_unit": "L", "effective_at": T1.isoformat(),
    }).json()
    assert bad["status"] == "suspended"
    # inspector 无复核权限
    r = client.post(f"/api/trace/flows/{bad['id']}/retry", headers=inspector)
    assert r.status_code == 403
    # 重试：数量事实不变，仍挂起
    r = client.post(f"/api/trace/flows/{bad['id']}/retry", headers=reviewer)
    assert r.status_code == 200
    assert r.json()["status"] == "suspended"
    assert r.json()["balance"]["difference"] == pytest.approx(30)
    # 作废后不再进入台账
    r = client.post(f"/api/trace/flows/{bad['id']}/void?reason=重复登记", headers=reviewer)
    assert r.status_code == 200
    assert r.json()["status"] == "void"
    lane = client.get("/api/trace/batches/B20261102/lane", headers=inspector).json()
    assert lane["remaining"] == pytest.approx(1000)


def test_docs_cover_trace_endpoints(client):
    schema = client.get("/openapi.json").json()
    assert schema["openapi"].startswith("3.")
    paths = schema["paths"]
    assert "/api/trace/flows" in paths
    assert "/api/trace/bindings" in paths
    assert "/api/trace/coverage/{batch_no}" in paths
    assert "/api/trace/restrictions/{batch_no}" in paths
    assert "/api/trace/anomalies" in paths
    assert "/api/trace/reports/{report_id}/revisions" in paths


def test_concurrent_splits_serialized_by_row_lock(engine, db_session):
    """两个会话同时争用同一剩余数量：先提交的一份生效，后一份因可用量不足被挂起。"""
    import threading
    from sqlalchemy.orm import sessionmaker

    db_session.add(Sample(
        sample_no="S-CONC", batch_no="B-CONC", supplier="并发演示",
        material="原料", matrix="solid", status="valid",
    ))
    db_session.commit()
    _receive(db_session, "B-CONC", 100)
    db_session.commit()

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def worker(name: str):
        s = Session()
        try:
            barrier.wait(timeout=5)
            op = trace.register_operation(
                s, kind="split", inputs=[("B-CONC", 80)],
                outputs=[(f"OUT-{name}", 80)], effective_at=T1,
            )
            s.commit()
            results[name] = op.status
        except Exception as exc:  # pragma: no cover - 失败时暴露真实原因
            s.rollback()
            results[name] = f"error:{exc}"
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert sorted(results.values()) == [FLOW_ACTIVE, FLOW_SUSPENDED], results
    db_session.expire_all()
    assert trace.lane_snapshot(db_session, "B-CONC")["remaining"] == pytest.approx(20)




def test_end_to_end_chain_provenance(client, seeded_imports):
    """一批拆两份、一份混合、一分支单独取样：限制可逐层追溯到具体检验。"""
    inspector = _auth("inspector")
    reviewer = _auth("reviewer")
    db = seeded_imports

    db.add(Sample(sample_no="S-LOTH", batch_no="LOT-H", supplier="内部流转",
                  material="分装料", matrix="liquid", status="valid"))
    db.flush()
    h_result = _mk_result(db, "S-LOTH", "LOT-H", matrix="liquid")
    db.commit()

    def flow(**body):
        return client.post("/api/trace/flows", headers=inspector, json=body)

    assert flow(
        kind="receive", inputs=[{"batch_no": "SUP-9", "quantity": 1000}],
        outputs=[{"batch_no": "B20261102", "quantity": 1000}],
        quantity_unit="L", effective_at=T0.isoformat(),
    ).json()["status"] == "active"
    split = flow(
        kind="split", inputs=[{"batch_no": "B20261102", "quantity": 1000}],
        outputs=[{"batch_no": "LOT-H", "quantity": 500}, {"batch_no": "LOT-K", "quantity": 500}],
        quantity_unit="L", effective_at=T1.isoformat(),
        homogeneity_retained=True, homogeneity_evidence="同罐分装，分装前后留样比对一致",
    ).json()
    assert split["status"] == "active"
    assert flow(
        kind="receive", inputs=[{"batch_no": "SUP-OTHER", "quantity": 500}],
        outputs=[{"batch_no": "OTHER", "quantity": 500}],
        quantity_unit="L", effective_at=T0.isoformat(),
    ).json()["status"] == "active"
    assert flow(
        kind="merge",
        inputs=[{"batch_no": "LOT-K", "quantity": 500}, {"batch_no": "OTHER", "quantity": 500}],
        outputs=[{"batch_no": "BLEND-1", "quantity": 1000}],
        quantity_unit="L", effective_at=T2.isoformat(),
        homogeneity_retained=True, homogeneity_evidence="混合 30 分钟后三点取样一致",
    ).json()["status"] == "active"

    # 原批次取样 600 L 的检验：一半随 LOT-K 进混合，一半留在 LOT-H
    client_result = _current_result(db, "S-101", "lead")
    r = client.post("/api/trace/bindings", headers=inspector, json={
        "result_id": client_result, "covered_quantity": 600,
        "sampling_basis": "S-101 取样代表 600 L 来料",
    })
    assert r.status_code == 201

    # LOT-H 自己的检验：300 L（同时也让 LOT-H 有一份本地证据）
    r = client.post("/api/trace/bindings", headers=inspector, json={
        "result_id": h_result, "covered_quantity": 300,
        "sampling_basis": "LOT-H 分装后取样 300 L",
    })
    assert r.status_code == 201

    cov = client.get("/api/trace/coverage/LOT-H", headers=inspector).json()["determinations"]
    from_orig = next(c for c in cov if c["source_batch_no"] == "B20261102")
    assert float(from_orig["covered_quantity"]) == pytest.approx(300)
    # 逐层可查：路径上每一步都记录了操作号与同质性依据
    chain = from_orig["paths"][0]["chain"]
    assert chain[0]["kind"] == "split"
    assert chain[0]["homogeneity_retained"] is True
    assert from_orig["binding_no"]

    blend = client.get("/api/trace/coverage/BLEND-1", headers=inspector).json()["determinations"]
    assert len(blend) == 1
    assert blend[0]["status"] == "partial"
    assert float(blend[0]["covered_quantity"]) == pytest.approx(300)
    assert blend[0]["paths"][0]["chain"][-1]["kind"] == "merge"

    lot_h = client.get("/api/trace/restrictions/LOT-H", headers=inspector).json()[0]
    # 500 中：铅(lead) 有 300 覆盖，镉(cadmium) 本地检验 300 覆盖；
    # 按最弱的项目取上限，只能放行二者都覆盖到的 300
    assert float(lot_h["released_quantity"]) == pytest.approx(300)
    assert float(lot_h["held_quantity"]) == pytest.approx(200)

    blend_res = client.get("/api/trace/restrictions/BLEND-1", headers=inspector).json()[0]
    assert float(blend_res["released_quantity"]) == pytest.approx(300)
    assert float(blend_res["held_quantity"]) == pytest.approx(700)

    # 原批次异常：沿流向影响，LOT-H/BLEND-1 中来自它的部分全部暂扣
    r = client.post("/api/trace/anomalies", headers=reviewer,
                    json={"batch_no": "B20261102", "reason": "供应商召回"})
    assert r.status_code == 201
    impact = r.json()["impact"]
    layers = {x["batch_no"]: x for x in impact["affected_downstream"]}
    assert float(layers["BLEND-1"]["resident_quantity"]) == pytest.approx(500)
    assert float(layers["LOT-H"]["resident_quantity"]) == pytest.approx(500)
    # LOT-H 自身的本地检验不能覆盖异常来源的那 500
    lot_h = client.get("/api/trace/restrictions/LOT-H", headers=inspector).json()[0]
    assert float(lot_h["held_quantity"]) == pytest.approx(500)
    assert any(b["kind"] == "upstream_anomaly" for b in lot_h["bases"])
    blend_res = client.get("/api/trace/restrictions/BLEND-1", headers=inspector).json()[0]
    assert float(blend_res["released_quantity"]) == pytest.approx(0)
    assert float(blend_res["held_quantity"]) == pytest.approx(1000)


def test_partial_split_leaves_residual_coverage(seeded_imports):
    """只拆走一部分：覆盖按比例留在源批次，留在源批次的剩余部分仍可放行。"""
    db = seeded_imports
    _receive(db, "B20261101", 1000)
    trace.register_operation(
        db, kind="split", inputs=[("B20261101", 600)],
        outputs=[("SP-HALF", 600)], effective_at=T1,
        homogeneity_retained=True, homogeneity_evidence="同批拆分留样",
    )
    binding = trace.register_binding(
        db, result_id=_current_result(db, "S-002", "cadmium"),
        covered_quantity=400, sampling_basis="S-002 代表 400 kg",
    )
    db.flush()
    dets = {
        d.covered_batch_no: d for d in
        db.query(CoverageDetermination).filter_by(binding_id=binding.id, is_current=True)
    }
    # 400 * 600/1000 = 240 拆走，160 留在源批次
    assert float(dets["SP-HALF"].covered_quantity) == pytest.approx(240)
    assert float(dets["B20261101"].covered_quantity) == pytest.approx(160)
    assert dets["B20261101"].status == COV_VALID
    src = _restriction(db, "B20261101")
    assert float(src.released_quantity) == pytest.approx(160)
    assert float(src.held_quantity) == pytest.approx(240)


def test_multiple_receipts_proportional_seeding(seeded_imports):
    """同一批次分两次收货：覆盖按累计流入比例分摊到每一笔流入。"""
    db = seeded_imports
    _receive(db, "B20261101", 400, at=T0, source="SUP-G1")
    _receive(db, "B20261101", 600, at=T1, source="SUP-G2")
    trace.register_operation(
        db, kind="split", inputs=[("B20261101", 500)],
        outputs=[("SP-R", 500)], effective_at=T2,
        homogeneity_retained=True, homogeneity_evidence="同批拆分留样",
    )
    binding = trace.register_binding(
        db, result_id=_current_result(db, "S-002", "cadmium"),
        covered_quantity=200, sampling_basis="S-002 代表 200 kg",
    )
    db.flush()
    det = (
        db.query(CoverageDetermination)
        .filter_by(binding_id=binding.id, covered_batch_no="SP-R", is_current=True)
        .one()
    )
    assert float(det.covered_quantity) == pytest.approx(100)
    lane = trace.lane_snapshot(db, "B20261101")
    assert lane["inflow"] == pytest.approx(1000)
    assert lane["remaining"] == pytest.approx(500)


def test_registered_batch_conclusion_not_silently_pass(client, seeded_imports):
    """已登记收货但尚无任何检验覆盖：批次结论不判 pass，交负责人按暂扣清单处置。"""
    inspector = _auth("inspector")
    client.post("/api/trace/flows", headers=inspector, json={
        "kind": "receive",
        "inputs": [{"batch_no": "SUP-NCOV", "quantity": 1000}],
        "outputs": [{"batch_no": "B20261102", "quantity": 1000}],
        "quantity_unit": "L", "effective_at": T0.isoformat(),
    })
    conclusion = client.post("/api/reports/batch-conclusions", headers=inspector,
                             json={"batch_no": "B20261102"}).json()
    assert conclusion["conclusion"] == BATCH_REVIEW
    assert conclusion["detail"]["coverage_gaps"]
    trace_restriction = conclusion["detail"]["trace_restriction"]
    assert trace_restriction is not None, conclusion["detail"]
    assert trace_restriction["held_quantity"] == 1000
