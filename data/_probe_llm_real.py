# -*- coding: utf-8 -*-
"""真实 LLM 网关连通性探测（批次12 · U1 到位验证）

用生产同款 chat() 通道（httpx + schema 硬校验）发一条最小结构化请求，
验证：① key 有效 ② 输出可被 parse_prediction 接受 ③ 延迟正常。
只读，不写任何表。
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from crypto.instinct import llm_gateway as gw

assert gw.is_configured(), 'LLM 配置未生效，检查 api_config.py / 环境变量'

prompt = (
    '你是行情盘感助手。请只输出一个 JSON 对象，字段要求：'
    'judgment 必须是 rise/fall/watch 之一；confidence 是 0~1 小数；'
    'rationale 是非空字符串；cited_rule_ids 和 cited_corpus_ids 是整数数组（可为空）；'
    'meta_cognition 是字符串。'
    '场景：某合约短周期方向 up，长周期方向 up，ATR 分位 0.5。请给出判断。'
)

t0 = time.time()
pred, latency_ms, model = gw.chat(prompt)
print(f'[OK] model={model} latency={latency_ms}ms wall={time.time()-t0:.1f}s')
print('[pred]', __import__('json').dumps(pred, ensure_ascii=False))
