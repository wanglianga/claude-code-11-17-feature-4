#!/bin/bash
# 端到端演示：导入 -> 去重 -> 冲突复核 -> 批次结论 -> 报告批准/待处理视图。
# 前置：服务已在 http://127.0.0.1:8000 运行，参考数据已 seed。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
IH="Authorization: Bearer token-inspector-001"
RH="Authorization: Bearer token-reviewer-001"
py() { python3 -c "import sys,json;x=json.load(sys.stdin);$1"; }

echo "== 1. 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 2. 导入 LAB-A CSV（坏行进入待处理，不丢行） =="
curl -s -X POST "$BASE/api/import/records" -H "$IH" \
  -F "source_ref=LAB-A" -F "file=@data/raw/lab_a_20261105.csv" \
  | py "print('status=',x['status'],'total=',x['total_rows'],'parsed=',x['parsed_rows'],'pending=',x['pending_rows'])"

echo "== 3. 同文件重传（来源编号+指纹识别为重复，跳过） =="
curl -s -X POST "$BASE/api/import/records" -H "$IH" \
  -F "source_ref=LAB-A" -F "file=@data/raw/lab_a_20261105.csv" \
  | py "print('status=',x['status'],'note=',x['note'])"

echo "== 4. 导入 LAB-B JSON（异源结果不一致 -> 列冲突，不覆盖） =="
curl -s -X POST "$BASE/api/import/records" -H "$IH" \
  -F "source_ref=LAB-B" -F "file=@data/raw/lab_b_20261105.json" \
  | py "print('conflicts_raised=',x['conflicts_raised'])"

echo "== 5. 复核员查看并处理冲突（采信 LAB-B 的 0.35 mg/kg 高值） =="
CID=$(curl -s "$BASE/api/review/conflicts?status=open" -H "$RH" \
  | py "print(x[0]['id'])")
KEEP=$(curl -s "$BASE/api/query/samples/S-001/groups" -H "$IH" | py "
g=[g for g in x if g['analyte']=='lead'][0]
print([r['id'] for r in g['results'] if r['record_no']=='RB-001'][0])")
echo "conflict_id=$CID, keep result_id=$KEEP"
curl -s -X POST "$BASE/api/review/conflicts/$CID/resolve" -H "$RH" -H "Content-Type: application/json" \
  -d "{\"keep_result_id\":$KEEP,\"note\":\"演示：复测采信LAB-B高值\"}" \
  | py "print('conflict status=',x['status'])"

echo "== 6. 生成批次结论（固体 B20261101=hold，液体 B20261102=pass） =="
curl -s -X POST "$BASE/api/reports/batch-conclusions" -H "$IH" -H "Content-Type: application/json" \
  -d '{"batch_no":"B20261101"}' \
  | py "print('B20261101 =>',x['conclusion'],'| 阻断数=',len(x['detail']['blockers']))"
LCID=$(curl -s -X POST "$BASE/api/reports/batch-conclusions" -H "$IH" -H "Content-Type: application/json" \
  -d '{"batch_no":"B20261102"}' | py "print(x['id'])")

echo "== 7. 提交并批准液体批次报告 =="
RID=$(curl -s -X POST "$BASE/api/reports/submit" -H "$IH" -H "Content-Type: application/json" \
  -d "{\"batch_no\":\"B20261102\",\"batch_conclusion_id\":$LCID}" | py "print(x['id'])")
curl -s -X POST "$BASE/api/reports/$RID/approve" -H "$RH" \
  | py "print('report',x['id'],'=>',x['status'])"

echo "== 8. 待处理视图（采购可见的不确定项） =="
curl -s "$BASE/api/query/pending?batch_no=B20261101" -H "$IH" \
  | py "print('raw_pending(坏行)=',len(x['raw_pending']),'result_pending(待补/总量挂起)=',len(x['result_pending']))"

echo "== 9. 批次报告视图中的关键判定 =="
curl -s "$BASE/api/query/batches/B20261101/report" -H "$IH" | py "
c=x['latest_conclusion']
print('结论=',c['conclusion'])
for it in c['detail']['items']:
    if it['analyte'] in ('lead','mercury') and it['sample_no'] in ('S-001','S-002'):
        print(' ',it['sample_no'],it['analyte'],it['verdict'],it['interval'],it['limit'])"

echo "== 10. 流转追溯：一批拆两份，一份混合，检验覆盖沿实际流向传播 =="
IH="Authorization: Bearer token-inspector-001"
RH="Authorization: Bearer token-reviewer-001"
curl -s -X POST "$BASE/api/trace/flows" -H "$IH" -H "Content-Type: application/json" \
  -d '{"kind":"receive","inputs":[{"batch_no":"SUP-DEMO","quantity":1000}],
       "outputs":[{"batch_no":"B20261102","quantity":1000}],
       "quantity_unit":"L","effective_at":"2026-11-01T08:00:00Z"}' \
  | py "print('收货:',x['operation_no'],x['status'])"
curl -s -X POST "$BASE/api/trace/flows" -H "$IH" -H "Content-Type: application/json" \
  -d '{"kind":"split","inputs":[{"batch_no":"B20261102","quantity":1000}],
       "outputs":[{"batch_no":"LOT-H","quantity":500},{"batch_no":"LOT-K","quantity":500}],
       "quantity_unit":"L","effective_at":"2026-11-02T08:00:00Z",
       "homogeneity_retained":true,"homogeneity_evidence":"同罐分装，分装前后留样比对一致"}' \
  | py "print('声明保留同质性依据的拆分:',x['status'])"
curl -s -X POST "$BASE/api/trace/flows" -H "$IH" -H "Content-Type: application/json" \
  -d '{"kind":"split","inputs":[{"batch_no":"LOT-K","quantity":500}],
       "outputs":[{"batch_no":"LOT-X","quantity":400}],
       "quantity_unit":"L","effective_at":"2026-11-03T08:00:00Z"}' \
  | py "print('投入产出不齐的拆分:',x['status'],'|',[r['code'] for r in x['suspend_reasons']])"

RESULT_ID=$(curl -s "$BASE/api/query/samples/S-101/groups" -H "$IH" | py "
g=[g for g in x if g['analyte']=='lead'][0]; print(g['current_result_id'])")
curl -s -X POST "$BASE/api/trace/bindings" -H "$IH" -H "Content-Type: application/json" \
  -d "{\"result_id\":$RESULT_ID,\"covered_quantity\":600,\"sampling_basis\":\"S-101 代表 600 L 来料\"}" \
  | py "print('绑定:',x['binding_no'],'源批次',x['source_batch_no'],'覆盖数量',x['covered_quantity'])"
curl -s "$BASE/api/trace/coverage/LOT-H" -H "$IH" | py "
print('LOT-H 覆盖:',[(c['status'],c['covered_quantity']) for c in x['determinations']])"
curl -s "$BASE/api/trace/coverage/LOT-K" -H "$IH" | py "
print('LOT-K 覆盖:',[(c['status'],c['covered_quantity'],c['pending_quantity']) for c in x['determinations']])"
curl -s "$BASE/api/trace/restrictions?status=partial" -H "$IH" | py "
print('受限批次:',[(r['batch_no'],r['released_quantity'],r['held_quantity']) for r in x])"

echo "== 11. 批次事后确认异常：沿实际流向算尚存与已转出 =="
curl -s -X POST "$BASE/api/trace/anomalies" -H "$RH" -H "Content-Type: application/json" \
  -d '{"batch_no":"B20261102","reason":"演示：供应商召回通知"}' | py "
print('异常:',x['batch_no'],'尚存',x['impact']['remaining_quantity'],
      '已转出',x['impact']['transferred_quantity'])
for l in x['impact']['affected_downstream']:
    print('  下游',l['batch_no'],'实际残留',l['resident_quantity'])"

echo
echo "演示完成。Swagger UI: $BASE/docs"
