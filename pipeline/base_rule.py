"""
pipeline/base_rule.py — 规则链基础定义。

RuleContext:   规则评估上下文（事件 + 配置）
RuleDecision:  规则决策结果（accepted + reason）
BaseRule:      规则抽象基类
RuleChain:     规则链执行器（顺序执行 + 短路）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    from domain.models import EventEnvelope
except ImportError:
    from ..domain.models import EventEnvelope


@dataclass
class RuleContext:
    """规则评估上下文。"""

    envelope: EventEnvelope
    config: dict[str, Any] = field(default_factory=dict)
    session_config: dict[str, Any] | None = None

    @property
    def event(self):
        return self.envelope.event

    @property
    def source_id(self) -> str:
        return self.envelope.source_id

    @property
    def event_type(self) -> str:
        return self.envelope.event_type


@dataclass
class RuleDecision:
    """规则决策结果。"""

    accepted: bool
    reason: str = ""
    rule_name: str = ""

    @classmethod
    def accept(cls, reason: str = "", rule_name: str = "") -> RuleDecision:
        return cls(accepted=True, reason=reason, rule_name=rule_name)

    @classmethod
    def reject(cls, reason: str, rule_name: str = "") -> RuleDecision:
        return cls(accepted=False, reason=reason, rule_name=rule_name)


class BaseRule:
    """规则抽象基类。"""

    name: str = "base_rule"

    def evaluate(self, context: RuleContext) -> RuleDecision:
        """评估规则，返回决策结果。"""
        raise NotImplementedError


class RuleChain:
    """规则链 — 顺序执行多个规则，首个拒绝则短路。"""

    def __init__(self, rules: list[BaseRule] | None = None):
        self.rules = list(rules or [])

    def evaluate(self, context: RuleContext) -> RuleDecision:
        for rule in self.rules:
            decision = rule.evaluate(context)
            if not decision.accepted:
                return decision
        return RuleDecision.accept(reason="规则链全部通过")
