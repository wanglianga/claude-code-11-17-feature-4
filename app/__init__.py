"""实验室原料检验数据治理服务。

模块划分：
- models:       PostgreSQL 表结构（采集原件 / 标准化结果 / 批准记录三层存储）
- units:        单位量纲核对与换算
- parser:       CSV / JSON 原始记录解析（行级容错）
- processing:   原始记录 -> 标准化结果 -> 判定 的处理管线
- batch:        整批结论汇总
- reports:      报告批准 / 撤回 / 版本留痕
- trace:        供货批次拆分/混合的流转追溯与检验适用范围传播
- routers:      FastAPI 接口
"""
__version__ = "1.1.0"
