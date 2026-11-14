"""FastAPI 应用入口。"""
from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.db import Base, engine
from app.routers import imports, queries, reports, review, trace

# 导入模型以保证 Base.metadata 完整
from app import models  # noqa: F401

app = FastAPI(
    title="实验室原料检验数据治理服务",
    version=__version__,
    description=(
        "接收实验室原始记录（CSV/JSON），关联取样与供货批次，统一计量含义，"
        "以数值/区间/定性三分方式保存结果，按随附业务规则做区间判定，"
        "并提供冲突复核、批次结论与报告批准/撤回留痕。\n\n"
        "另含供货批次拆分/混合的流转追溯：按实际投入产出与损耗、生效时点登记流转，"
        "把检验引用绑定到原取样批次与覆盖数量，沿实际流向按数量传播覆盖；"
        "混合样不自动证明各组成批次，拆分缺同质性依据的部分列为待补检；"
        "投入产出不符或成环的登记挂起而不改库存事实；批次事后异常可沿流向算尚存与已转出范围；"
        "迟到登记触发版本化重算，已签报告通过新修订说明更正原文。"
    ),
)

app.include_router(imports.router)
app.include_router(review.router)
app.include_router(reports.router)
app.include_router(trace.router)
app.include_router(queries.router)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok", "version": __version__}


@app.post("/admin/init-db", tags=["system"])
def init_db():
    """建表（开发/演示用；生产应由迁移工具管理）。"""
    Base.metadata.create_all(bind=engine)
    return {"status": "created", "tables": sorted(Base.metadata.tables.keys())}
