"""
Qwen Memory Token Budget Manager — 预算感知的记忆注入
解决核心问题：历史记忆 + 当前上下文 总 token 可能超限

核心能力：
  1. fused_score  — FTS / TF-IDF / 时间衰减 / 重要度 三路融合打分
  2. trim_results — 贪心裁剪：按分数降序装入 token 预算
  3. compact_summary — 压缩摘要：把长摘要 + 观察列表压缩成精简版
  4. adaptive_injection — 自适应注入：搜索 → 打分 → 裁剪 → 返回预算内的结果
"""
import math
import json
import hashlib
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

# ============ Token 估算 ============

def estimate_tokens(text: str) -> int:
    """
    粗估 token 数：中文约 1 字符 ≈ 1.5 token，英文约 1 词 ≈ 1 token
    混合文本取 len/2 作为下界估计，再加 20% 余量
    """
    if not text:
        return 0
    # 简单启发式：按字符数 / 2 * 1.2，覆盖中英混合场景
    return max(1, int(len(text) * 0.6))


# ============ 时间衰减 ============

def _recency_score(created_at: str, half_life_days: float = 30.0) -> float:
    """
    时间衰减分数：半衰期默认 30 天
    created_at 为 UTC ISO 格式，返回 0.0 ~ 1.0
    """
    if not created_at:
        return 0.5
    try:
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta_days = max(0, (now - dt).total_seconds() / 86400)
        return math.exp(-0.693 * delta_days / half_life_days)  # ln(2) ≈ 0.693
    except Exception:
        return 0.5


# ============ 融合打分 ============

def fused_score(
    fts_rank: float = 0.0,
    tfidf_score: float = 0.0,
    created_at: str = "",
    importance: float = 0.5,
    weights: Optional[Dict[str, float]] = None,
) -> float:
    """
    三路融合打分：
      - 关键词相关性（FTS rank 归一化）
      - 语义相关性（TF-IDF 余弦相似度）
      - 时效性 × 重要度（复合衰减）

    返回 0.0 ~ 1.0，越高越优先注入。

    weights 默认值：
      fts       0.4  — 关键词匹配最直接
      tfidf     0.3  — 语义补充
      time      0.15 — 时效性
      importance 0.15 — 重要度
    """
    w = weights or {"fts": 0.4, "tfidf": 0.3, "time": 0.15, "importance": 0.15}

    # FTS rank 归一化：rank 越小越好，用 1/(1+rank) 映射到 (0,1]
    fts_norm = 1.0 / (1.0 + max(0.0, fts_rank))

    # TF-IDF 已在 [0,1] 范围
    tfidf_norm = max(0.0, min(1.0, tfidf_score))

    # 时间衰减
    time_norm = _recency_score(created_at)

    # 重要度已在 [0,1] 范围
    imp_norm = max(0.0, min(1.0, importance))

    score = (
        w["fts"] * fts_norm
        + w["tfidf"] * tfidf_norm
        + w["time"] * time_norm
        + w["importance"] * imp_norm
    )
    return round(min(1.0, max(0.0, score)), 4)


# ============ 结果裁剪 ============

def trim_results(
    results: List[Dict[str, Any]],
    token_budget: int,
    token_key: str = "token_estimate",
    score_key: str = "fused_score",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    贪心裁剪：按 fused_score 降序，依次装入直到 token 预算用完。

    参数：
      results      — 带 score_key 和 token_key 的字典列表
      token_budget — 可用 token 上限

    返回：
      (selected, stats) — 选中的结果列表 + 统计信息
    """
    if not results or token_budget <= 0:
        return [], {"selected": 0, "total_tokens": 0, "budget": token_budget, "dropped": len(results or [])}

    # 按分数降序
    sorted_results = sorted(results, key=lambda r: r.get(score_key, 0), reverse=True)

    selected = []
    used_tokens = 0
    for item in sorted_results:
        # 优先使用 summary_compact 计算 token 消耗，回退到其他字段
        compact = (item.get("summary_compact") or "").strip()
        if compact and "summary_compact" in item:
            item_tokens = estimate_tokens(compact)
        else:
            item_tokens = item.get(token_key, estimate_tokens(item.get("text", "") or item.get("content", "") or item.get("summary", "")))
        if used_tokens + item_tokens <= token_budget:
            selected.append(item)
            used_tokens += item_tokens
        # 预算用完就停

    # 按原始顺序恢复（保持时间线）
    order_map = {id(r): i for i, r in enumerate(results)}
    selected.sort(key=lambda r: order_map.get(id(r), 0))

    stats = {
        "selected": len(selected),
        "total_tokens": used_tokens,
        "budget": token_budget,
        "dropped": len(results) - len(selected),
    }
    return selected, stats


# ============ 压缩摘要 ============

def compact_summary(
    summary: str,
    observations: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 200,
) -> str:
    """
    压缩摘要生成：
      1. 保留摘要前 N 句
      2. 观察按重要度取 top-K，压缩为一行
      3. 总 token 不超 max_tokens

    返回压缩后的文本。
    """
    parts = []
    budget_left = max_tokens

    # 摘要：取前 2 句（按句号/感叹号/问号分割）
    if summary:
        sentences = _split_sentences(summary)
        # 贪心装入摘要句子
        for sent in sentences[:3]:
            t = estimate_tokens(sent)
            if t <= budget_left:
                parts.append(sent.strip())
                budget_left -= t

    # 观察：按重要度取 top 3，每条压缩为一行
    if observations:
        sorted_obs = sorted(observations, key=lambda o: o.get("importance", 0.5), reverse=True)
        for obs in sorted_obs[:3]:
            content = (obs.get("content") or "")[:80]
            obs_type = obs.get("obs_type", "")
            line = f"[{obs_type}] {content}" if obs_type else content
            t = estimate_tokens(line)
            if t <= budget_left:
                parts.append(line)
                budget_left -= t

    result = " | ".join(parts) if parts else (summary or "")[:max_tokens * 2]
    # 最终截断保底
    return _truncate_to_tokens(result, max_tokens)


def _split_sentences(text: str) -> List[str]:
    """中文/英文混合分句"""
    import re
    # 按句号、感叹号、问号、换行分割
    sents = re.split(r'(?<=[。！？\.\!\?\n])\s*', text)
    return [s.strip() for s in sents if s.strip()]


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """截断到指定 token 数"""
    if estimate_tokens(text) <= max_tokens:
        return text
    # 粗暴截断：按字符数反推
    max_chars = int(max_tokens / 0.6)
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


# ============ 自适应注入 ============

def adaptive_injection(
    query: str,
    max_tokens: int = 500,
    session_limit: int = 5,
    obs_limit: int = 10,
    include_summary: bool = True,
) -> Dict[str, Any]:
    """
    自适应注入：端到端管道
      1. 融合检索（关键词 + 语义）
      2. 三路打分
      3. token 预算裁剪
      4. 返回可直接注入上下文的结果

    返回：
      {
        "sessions": [...],
        "observations": [...],
        "compact_text": "...",   # 可直接拼入 prompt 的压缩文本
        "stats": {...}
      }
    """
    import store

    # 1. 融合检索
    raw = store.fused_search(query, limit=max(session_limit, obs_limit))

    scored_sessions = []
    scored_obs = []

    # 2. 三路打分
    for s in raw.get("sessions", []):
        score = fused_score(
            fts_rank=0.0,  # fused_search 已排序，rank 信息不直接传递
            tfidf_score=0.0,
            created_at=s.get("created_at", ""),
            importance=s.get("importance", 0.5),
        )
        s["fused_score"] = score
        # 优先使用 summary_compact（LIGHT 模式），回退到 summary（FULL 模式）
        compact = s.get("summary_compact", "") or ""
        s["token_estimate"] = estimate_tokens(
            compact if compact else (s.get("summary", "") or s.get("summary_short", ""))
        )
        scored_sessions.append(s)

    for o in raw.get("observations", []):
        score = fused_score(
            fts_rank=0.0,
            tfidf_score=0.0,
            created_at=o.get("created_at", ""),
            importance=o.get("importance", 0.5),
        )
        o["fused_score"] = score
        o["token_estimate"] = estimate_tokens(o.get("content", ""))
        scored_obs.append(o)

    # 3. 预算分配：会话占 60%，观察占 40%
    session_budget = int(max_tokens * 0.6)
    obs_budget = max_tokens - session_budget

    # 4. 裁剪
    sel_sessions, s_stats = trim_results(scored_sessions, session_budget)
    sel_obs, o_stats = trim_results(scored_obs, obs_budget)

    # 5. 生成可注入文本（优先使用 summary_compact）
    compact_parts = []
    if include_summary:
        for s in sel_sessions:
            compact = (s.get("summary_compact") or "").strip()
            short = compact if compact else (s.get("summary_short") or (s.get("summary") or "")[:120])
            compact_parts.append(f"[{s.get('session_id', '')}] {short}")
        for o in sel_obs:
            compact_parts.append(f"[{o.get('obs_type', '')}] {(o.get('content') or '')[:100]}")

    compact_text = "\n".join(compact_parts) if compact_parts else ""

    stats = {
        "query": query,
        "max_tokens": max_tokens,
        "session_tokens": s_stats["total_tokens"],
        "obs_tokens": o_stats["total_tokens"],
        "total_tokens": s_stats["total_tokens"] + o_stats["total_tokens"],
        "sessions_selected": s_stats["selected"],
        "sessions_dropped": s_stats["dropped"],
        "obs_selected": o_stats["selected"],
        "obs_dropped": o_stats["dropped"],
    }

    # 6. 记录注入日志
    try:
        log_budget(
            query=query,
            max_tokens=max_tokens,
            used_tokens=stats["total_tokens"],
            sessions_injected=s_stats["selected"],
            obs_injected=o_stats["selected"],
            compact_text=compact_text,
        )
    except Exception:
        pass  # 日志失败不阻塞主流程

    return {
        "sessions": sel_sessions,
        "observations": sel_obs,
        "compact_text": compact_text,
        "stats": stats,
    }


# ============ 预算日志（写 store.py 的 memory_budget_log 表） ============

def log_budget(
    query: str,
    max_tokens: int,
    used_tokens: int,
    sessions_injected: int = 0,
    obs_injected: int = 0,
    compact_text: str = "",
    cache_hit: int = 0,
):
    """记录每次注入的预算使用情况"""
    import store
    store.log_budget(
        query=query,
        max_tokens=max_tokens,
        used_tokens=used_tokens,
        sessions_injected=sessions_injected,
        obs_injected=obs_injected,
        compact_text=compact_text,
        cache_hit=cache_hit,
    )
