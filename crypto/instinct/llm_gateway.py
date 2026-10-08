# -*- coding: utf-8 -*-
"""
LLM 网关（批次12 · P3，方案 D5）
================================
OpenAI 兼容 /chat/completions 的薄封装：httpx 显式超时 30s、失败重试 1 次、
JSON 输出 schema 硬校验。配置只从环境变量读取（不落库、不写代码）：

    CRYPTO_LLM_BASE_URL   例 https://api.deepseek.com/v1
    CRYPTO_LLM_API_KEY    sk-...
    CRYPTO_LLM_MODEL      例 deepseek-chat

本地开发可放 api_config.py（已 gitignore）同名三常量作为兜底。
未配置时 is_configured()=False，调用即抛 GatewayConfigError——
影子预测任务据此跳过并记日志，绝不静默假成功。

mock 网关（确定性启发式，专供管线验证/冒烟）：走同一 parse/校验/引用真实性
通道但零网络；model 列强制写 'mock'，统计侧 where model!='mock' 即可隔离。
"""

import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

TIMEOUT_S = 30.0
MAX_RETRY = 1
_JUDGMENTS = ('rise', 'fall', 'watch')


class GatewayConfigError(RuntimeError):
    """LLM 未配置或配置不全"""


class ResponseInvalidError(ValueError):
    """模型输出无法解析/不符合 schema（重试后仍失败）"""


def get_config() -> Optional[Dict[str, str]]:
    """读三项配置；不全返回 None（env 优先，api_config 兜底）"""
    base = os.environ.get('CRYPTO_LLM_BASE_URL', '').strip()
    key = os.environ.get('CRYPTO_LLM_API_KEY', '').strip()
    model = os.environ.get('CRYPTO_LLM_MODEL', '').strip()
    if not (base and key and model):
        try:
            from .. import api_config as ac
            base = base or str(getattr(ac, 'CRYPTO_LLM_BASE_URL', '') or '').strip()
            key = key or str(getattr(ac, 'CRYPTO_LLM_API_KEY', '') or '').strip()
            model = model or str(getattr(ac, 'CRYPTO_LLM_MODEL', '') or '').strip()
        except ImportError:
            pass
    if not (base and key and model):
        return None
    return {'base_url': base.rstrip('/'), 'api_key': key, 'model': model}


def is_configured() -> bool:
    return get_config() is not None


# =============================================================================
# JSON schema 校验（渲染/落库共用）
# =============================================================================

def parse_prediction(raw: str) -> Dict:
    """模型原始输出 → 校验后的预测 dict。容忍 ```json 围栏，其余一律严格。"""
    s = (raw or '').strip()
    m = re.search(r'\{.*\}', s, re.S)
    if not m:
        raise ResponseInvalidError(f'输出中找不到 JSON 对象: {s[:80]!r}')
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ResponseInvalidError(f'JSON 解析失败: {e}')
    j = str(d.get('judgment', '')).strip().lower()
    if j not in _JUDGMENTS:
        raise ResponseInvalidError(f'judgment 非法: {j!r}')
    try:
        c = float(d.get('confidence'))
    except (TypeError, ValueError):
        raise ResponseInvalidError('confidence 缺失或非数值')
    if not 0.0 <= c <= 1.0:
        raise ResponseInvalidError(f'confidence 越界: {c}')
    if not str(d.get('rationale', '')).strip():
        raise ResponseInvalidError('rationale 为空')
    def _ids(k):
        # 引用编号归一：真实模型会把 prompt 里的 [C1]/[R2] 标签原样填回
        # （DeepSeek 实测 cited_corpus_ids=["C1","C2"]），接受 整数 / "3" /
        # "C1" / "R2" 四种形态；其余野格式仍拒绝。越界由 assert_citations_real 兜底。
        v = d.get(k) or []
        if not isinstance(v, list):
            raise ResponseInvalidError(f'{k} 必须是整数数组: {v!r}')
        out = []
        for x in v:
            if isinstance(x, bool):
                raise ResponseInvalidError(f'{k} 必须是整数数组: {v!r}')
            if isinstance(x, int):
                out.append(x)
                continue
            m = re.fullmatch(r'[A-Za-z]?(\d+)', str(x).strip())
            if not m:
                raise ResponseInvalidError(f'{k} 含非法引用项: {x!r} (整组 {v!r})')
            out.append(int(m.group(1)))
        return out
    return {'judgment': j, 'confidence': round(c, 3),
            'rationale': str(d['rationale']).strip()[:300],
            'cited_rule_ids': _ids('cited_rule_ids'),
            'cited_corpus_ids': _ids('cited_corpus_ids'),
            'meta_cognition': str(d.get('meta_cognition', '')).strip()[:300]}


def assert_citations_real(pred: Dict, n_rules: int, n_cases: int):
    """引用真实性：cited_* 必须落在本次 prompt 提供的编号范围内（1 基）"""
    bad_r = [i for i in pred['cited_rule_ids'] if not 1 <= i <= n_rules]
    bad_c = [i for i in pred['cited_corpus_ids'] if not 1 <= i <= n_cases]
    if bad_r or bad_c:
        raise ResponseInvalidError(
            f'引用了不存在的编号 rules={bad_r}(共{n_rules}) cases={bad_c}(共{n_cases})')


# =============================================================================
# 调用
# =============================================================================

def chat(prompt_text: str, timeout: float = TIMEOUT_S) -> Tuple[Dict, int, str]:
    """真实网关：返回 (校验后预测, latency_ms, model)。失败重试 1 次后抛错。"""
    cfg = get_config()
    if cfg is None:
        raise GatewayConfigError(
            'LLM 未配置：设置环境变量 CRYPTO_LLM_BASE_URL / CRYPTO_LLM_API_KEY / '
            'CRYPTO_LLM_MODEL 后重试（影子预测将跳过）')
    url = cfg['base_url'] + '/chat/completions'
    body = {'model': cfg['model'], 'temperature': 0.2,
            'messages': [{'role': 'user', 'content': prompt_text}]}
    headers = {'Authorization': f"Bearer {cfg['api_key']}"}
    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRY + 1):
        t0 = time.monotonic()
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=timeout)
            r.raise_for_status()
            content = r.json()['choices'][0]['message']['content']
            pred = parse_prediction(content)
            return pred, int((time.monotonic() - t0) * 1000), cfg['model']
        except (httpx.HTTPError, KeyError, IndexError,
                ResponseInvalidError, json.JSONDecodeError) as e:
            last_err = e
            logger.warning('[LLMGateway] 第 %d 次调用失败: %s', attempt + 1, e)
            if attempt < MAX_RETRY:
                time.sleep(1.5)
    raise ResponseInvalidError(f'LLM 调用/解析最终失败: {last_err}')


def mock_chat(prompt_text: str, cases: List[Dict], rules: List[Dict]) -> Tuple[Dict, int, str]:
    """确定性启发式网关（管线验证专用，零网络）：
    邻居判断多数票；平票/无邻居 → watch 低置信。与真实网关走同一校验出口。"""
    votes = [c.get('judgment') for c in cases if c.get('judgment') in _JUDGMENTS]
    pred = {'judgment': 'watch', 'confidence': 0.3,
            'rationale': 'mock：无邻居判断，强制观望',
            'cited_rule_ids': [], 'cited_corpus_ids': [], 'meta_cognition': ''}
    if votes:
        top = max(set(votes), key=lambda v: (votes.count(v), v))
        n_top = votes.count(top)
        pred.update({
            'judgment': top,
            'confidence': round(0.3 + 0.1 * n_top, 3),
            'rationale': f'mock：{n_top}/{len(votes)} 个相似历史局面当时判断为 {top}',
            'cited_corpus_ids': [i + 1 for i, c in enumerate(cases)
                                 if c.get('judgment') == top][:6],
        })
    if rules:
        pred['cited_rule_ids'] = [1]
        pred['meta_cognition'] = 'mock：存在生效规则，引用 R1'
    return parse_prediction(json.dumps(pred, ensure_ascii=False)), 0, 'mock'
