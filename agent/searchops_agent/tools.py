"""提案者可见的工具注册表。

治理边界在这里成为代码事实而不是提示词约定：注册表通过白名单 + 安全等级双重过滤
构建，`approve` / `publish` / `rollback` 在任何配置下都不可能进入。
`tests/test_governance_boundary.py` 对此做断言。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .client import SearchOpsClient
from .safety import MAX_AUTOMATED, SafetyClass, safety_class_of

#: 提案者被允许调用的方法名。白名单而非黑名单——新增客户端方法默认不可见。
ALLOWED = (
    "query_metrics",
    "zero_result_queries",
    "low_quality_queries",
    "current_strategy",
    "strategy_history",
    "preview",
    "evaluate_query",
    "create_draft",
    "submit",
)

#: 无论如何都不得暴露。与 ALLOWED 冗余，是为了让越界在测试里立刻显形。
FORBIDDEN = ("approve", "publish", "rollback")


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., Any]
    safety_class: SafetyClass

    def __call__(self, *a: Any, **kw: Any) -> Any:
        return self.fn(*a, **kw)


class GovernanceViolation(RuntimeError):
    """构建注册表时越界即抛出，不留到运行期。"""


def build_registry(client: SearchOpsClient) -> dict[str, Tool]:
    registry: dict[str, Tool] = {}
    for name in ALLOWED:
        if name in FORBIDDEN:
            raise GovernanceViolation(f"{name} 同时出现在白名单与禁止名单中")
        bound = getattr(client, name)
        level = safety_class_of(getattr(type(client), name))
        if level.value > MAX_AUTOMATED.value:
            raise GovernanceViolation(
                f"{name} 的安全等级 {level.name} 高于自动化上限 {MAX_AUTOMATED.name}"
            )
        registry[name] = Tool(name=name, fn=bound, safety_class=level)
    return registry


def describe(registry: dict[str, Tool]) -> str:
    rows = [f"  {t.safety_class.name:<20} {t.name}" for t in registry.values()]
    return "提案者可见工具：\n" + "\n".join(rows) + f"\n  （{', '.join(FORBIDDEN)} 保留给人类角色）"
