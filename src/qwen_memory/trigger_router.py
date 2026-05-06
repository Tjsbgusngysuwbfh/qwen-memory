"""
Qwen Memory Trigger Router — 上下文注入触发路由器

根据用户首条消息 + 会话上下文，决定记忆系统以何种力度注入上下文。

动作级别：
  SKIP   — 不注入任何历史上下文（闲聊、问候）
  LIGHT  — 轻量注入：仅最近会话摘要 + 高重要度观察（项目跟进、简单查询）
  FULL   — 全量注入：融合检索 + 语义搜索 + 完整上下文（复杂任务、深度恢复）

触发类型：
  keyword  — 消息包含指定关键词
  pattern  — 消息匹配指定正则模式
  always   — 无条件匹配
  never    — 无条件不匹配（优先级最高）
"""
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

# 动作常量
SKIP = "SKIP"
LIGHT = "LIGHT"
FULL = "FULL"

# 触发类型常量
KEYWORD = "keyword"
PATTERN = "pattern"
ALWAYS = "always"
NEVER = "never"


@dataclass
class TriggerResult:
    """触发路由结果"""
    action: str                    # SKIP / LIGHT / FULL
    token_budget: int              # 建议的 token 预算
    matched_rule: Optional[str]    # 命中的规则名称（None 表示默认）
    matched_type: str = ""         # 命中的触发类型
    confidence: float = 0.0        # 匹配置信度 0.0 ~ 1.0
    reason: str = ""               # 为什么命中这条规则
    matched_keywords: List[str] = field(default_factory=list)  # 实际命中的关键词
    rule_description: str = ""     # 规则的描述文字


# token 预算映射
TOKEN_BUDGETS = {
    SKIP: 0,
    LIGHT: 800,
    FULL: 4000,
}


@dataclass
class MatchDetail:
    """规则匹配详情"""
    matched: bool                           # 是否匹配
    matched_keywords: List[str] = field(default_factory=list)  # 命中的关键词
    reason: str = ""                        # 匹配原因描述
    pattern_match: str = ""                 # 正则匹配到的文本（仅 pattern 类型）


@dataclass
class TriggerRule:
    """单条触发规则"""
    name: str
    trigger_type: str          # keyword / pattern / always / never
    action: str                # SKIP / LIGHT / FULL
    enabled: bool = True
    priority: int = 0          # 越大越优先
    keywords: List[str] = field(default_factory=list)
    pattern: str = ""
    description: str = ""
    token_budget: Optional[int] = None  # 覆盖默认预算

    def match(self, text: str) -> MatchDetail:
        """
        检查文本是否匹配此规则，返回匹配详情。

        Returns:
            MatchDetail: matched=True 表示命中，附带匹配到的关键词和原因说明。
        """
        if not self.enabled:
            return MatchDetail(matched=False)

        if self.trigger_type == NEVER:
            return MatchDetail(
                matched=True,
                reason="never 规则无条件匹配（标记为禁止注入场景）",
            )

        if self.trigger_type == ALWAYS:
            return MatchDetail(
                matched=True,
                reason=f"always 规则无条件匹配 → {self.action}",
            )

        if self.trigger_type == KEYWORD:
            text_lower = text.lower()
            hits = [kw for kw in self.keywords if kw.lower() in text_lower]
            if hits:
                return MatchDetail(
                    matched=True,
                    matched_keywords=hits,
                    reason=f"消息包含关键词「{'」「'.join(hits)}」",
                )
            return MatchDetail(matched=False)

        if self.trigger_type == PATTERN:
            try:
                m = re.search(self.pattern, text, re.IGNORECASE)
                if m:
                    return MatchDetail(
                        matched=True,
                        pattern_match=m.group(0),
                        reason=f"消息匹配正则模式 /{self.pattern}/",
                    )
            except re.error:
                pass
            return MatchDetail(matched=False)

        return MatchDetail(matched=False)


# ============ 默认规则 ============

DEFAULT_RULES = [
    TriggerRule(
        name="greeting_skip",
        trigger_type=KEYWORD,
        action=SKIP,
        priority=100,
        keywords=["你好", "hi", "hello", "嗨", "早上好", "晚上好", "下午好",
                   "在吗", "在不在", "hey"],
        description="问候语 → 跳过注入",
    ),
    TriggerRule(
        name="never_inject_sensitive",
        trigger_type=NEVER,
        action=SKIP,
        priority=999,
        keywords=[],  # never 不需要关键词
        description="绝不注入敏感场景（密码、token 等）",
    ),
    TriggerRule(
        name="session_start_full",
        trigger_type=KEYWORD,
        action=FULL,
        priority=80,
        keywords=["继续上次", "恢复上下文", "聊天记录丢了", "接着做",
                   "继续之前", "上次做到哪了", "回到项目"],
        description="会话恢复请求 → 全量注入",
    ),
    TriggerRule(
        name="project_context_full",
        trigger_type=KEYWORD,
        action=FULL,
        priority=70,
        keywords=["项目", "代码", "bug", "报错", "修复", "部署", "上线",
                   "重构", "review", "测试", "编译", "构建", "pipeline",
                   "ci", "cd", "docker", "k8s", "nginx"],
        description="项目/技术关键词 → 全量注入",
    ),
    TriggerRule(
        name="memory_query_light",
        trigger_type=KEYWORD,
        action=LIGHT,
        priority=60,
        keywords=["之前", "上次", "以前", "记得", "历史", "记录",
                   "搜索", "查一下", "找一下"],
        description="记忆查询关键词 → 轻量注入",
    ),
    TriggerRule(
        name="phone_control_full",
        trigger_type=KEYWORD,
        action=FULL,
        priority=75,
        keywords=["手机", "adb", "android", "scrcpy", "抓包", "reqable",
                   "控制手机", "自动点按"],
        description="手机控制关键词 → 全量注入",
    ),
    TriggerRule(
        name="routine_chat_skip",
        trigger_type=PATTERN,
        action=SKIP,
        priority=50,
        pattern=r"^(.{0,5})$|^(ok|好的|嗯|行|收到|了解|thx|thanks|谢谢|辛苦了|辛苦)$",
        description="极短回复/确认 → 跳过注入",
    ),
    TriggerRule(
        name="question_light",
        trigger_type=PATTERN,
        action=LIGHT,
        priority=40,
        pattern=r"\?|？|怎么|如何|为什么|什么|哪里|哪个|能不能|可不可以|有没有",
        description="疑问句 → 轻量注入",
    ),
    TriggerRule(
        name="default_light",
        trigger_type=ALWAYS,
        action=LIGHT,
        priority=10,
        description="默认兜底 → 轻量注入",
    ),
]


class TriggerRouter:
    """触发路由器：根据消息内容决定上下文注入策略"""

    def __init__(self, rules: Optional[List[TriggerRule]] = None):
        """
        初始化路由器

        Args:
            rules: 自定义规则列表。None 则使用默认规则。
        """
        self._rules: List[TriggerRule] = []
        if rules is not None:
            self._rules = list(rules)
        else:
            self._rules = list(DEFAULT_RULES)
        # 按优先级降序排列
        self._rules.sort(key=lambda r: r.priority, reverse=True)

    def add_rule(self, rule: TriggerRule):
        """添加规则并重新排序"""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority, reverse=True)

    def remove_rule(self, name: str) -> bool:
        """按名称移除规则"""
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.name != name]
        return len(self._rules) < before

    def get_rules(self) -> List[TriggerRule]:
        """返回当前规则列表（按优先级排序）"""
        return list(self._rules)

    def evaluate(self, first_message: str,
                 session_context: Optional[Dict[str, Any]] = None) -> TriggerResult:
        """
        评估首条消息，决定注入动作

        Args:
            first_message: 用户的首条消息
            session_context: 可选的会话上下文，支持字段：
                - has_history: bool — 是否有历史会话
                - is_project_session: bool — 是否为项目会话
                - session_count: int — 历史会话总数
                - last_session_tags: list — 上次会话的标签

        Returns:
            TriggerResult
        """
        ctx = session_context or {}
        text = (first_message or "").strip()

        # 空消息 → SKIP
        if not text:
            return TriggerResult(
                action=SKIP,
                token_budget=TOKEN_BUDGETS[SKIP],
                matched_rule="empty_message",
                matched_type="special",
                confidence=1.0,
                reason="消息为空，跳过注入",
            )

        # 逐规则匹配（已按优先级排序）
        for rule in self._rules:
            detail = rule.match(text)
            if not detail.matched:
                continue

            budget = rule.token_budget or TOKEN_BUDGETS.get(rule.action, 800)

            # never 规则需要额外检查：是否真的包含敏感内容
            if rule.trigger_type == NEVER and rule.name == "never_inject_sensitive":
                sensitive_patterns = [
                    r"password", r"密码", r"token", r"secret",
                    r"api[_-]?key", r"密钥", r"证书",
                ]
                is_sensitive = any(
                    re.search(p, text, re.IGNORECASE)
                    for p in sensitive_patterns
                )
                if not is_sensitive:
                    continue  # 不敏感，跳过 never 规则，继续匹配后续

            # 上下文增强：有历史会话时可提升动作级别
            action = self._enhance_action(rule.action, ctx)
            action_changed = action != rule.action

            # 组装解释信息
            reason = detail.reason
            if action_changed:
                reason += f"（上下文增强：{rule.action} → {action}）"

            return TriggerResult(
                action=action,
                token_budget=rule.token_budget or TOKEN_BUDGETS.get(action, 800),
                matched_rule=rule.name,
                matched_type=rule.trigger_type,
                confidence=1.0,
                reason=reason,
                matched_keywords=detail.matched_keywords,
                rule_description=rule.description,
            )

        # 理论上不会到这里（有 ALWAYS 兜底），防御性返回
        return TriggerResult(
            action=LIGHT,
            token_budget=TOKEN_BUDGETS[LIGHT],
            matched_rule="fallback",
            matched_type="default",
            confidence=0.5,
            reason="未命中任何规则，使用防御性默认值",
        )

    def _enhance_action(self, action: str, ctx: Dict[str, Any]) -> str:
        """
        上下文增强：根据会话状态决定是否提升动作级别

        规则：
        - 有历史会话 + 原本 SKIP → 提升到 LIGHT
        - 是项目会话 + 原本 SKIP → 提升到 LIGHT
        - 会话数 > 5 + 原本 LIGHT → 提升到 FULL
        """
        has_history = ctx.get("has_history", False)
        is_project = ctx.get("is_project_session", False)
        session_count = ctx.get("session_count", 0)

        if action == SKIP:
            if has_history or is_project:
                return LIGHT

        if action == LIGHT:
            if session_count > 5:
                return FULL

        return action


# ============ 模块级便捷函数 ============

_default_router: Optional[TriggerRouter] = None


def get_router() -> TriggerRouter:
    """获取默认路由器（单例）"""
    global _default_router
    if _default_router is None:
        _default_router = TriggerRouter()
    return _default_router


def evaluate_trigger(first_message: str,
                     session_context: Optional[Dict[str, Any]] = None) -> TriggerResult:
    """
    模块级入口：评估首条消息的触发动作

    Args:
        first_message: 用户首条消息
        session_context: 会话上下文（可选）

    Returns:
        TriggerResult
    """
    router = get_router()
    return router.evaluate(first_message, session_context)
