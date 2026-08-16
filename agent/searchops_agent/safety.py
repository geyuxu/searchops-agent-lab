"""工具的安全分级。

分级不是文档约定，而是随每个客户端方法一起登记的元数据。Agent 侧据此过滤可暴露的
工具集，测试据此断言"提案者永远拿不到审批与发布能力"。分级取自
`platform/docs/mcp-server-design.md` 中的 MCP 工具表，保持一一对应。
"""

from __future__ import annotations

import enum
import functools
from typing import Callable, TypeVar


class SafetyClass(enum.Enum):
    """按副作用与授权要求划分，序号越大权限越高。"""

    READ = 1
    """只读查询，无副作用。"""

    DRY_RUN = 2
    """计算并返回对比结果，但不写入任何状态。"""

    GOVERNED_WRITE = 3
    """写入状态，要求 actor、request_id 与 Idempotency-Key，但不改变线上策略。"""

    PRIVILEGED_WRITE = 4
    """授予审批权，产生 approval_token。人类角色专属。"""

    TOKEN_GATED_WRITE = 5
    """改变线上生效策略，额外要求有效的 approval_token。人类角色专属。"""


#: 允许自动化提案者持有的最高等级。GOVERNED_WRITE 之上必须有人类介入。
MAX_AUTOMATED = SafetyClass.GOVERNED_WRITE

F = TypeVar("F", bound=Callable)


def safety(level: SafetyClass) -> Callable[[F], F]:
    """给客户端方法登记安全等级，供工具注册表与测试内省。"""

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        wrapper.safety_class = level  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorate


def safety_class_of(fn: Callable) -> SafetyClass:
    """取出登记的等级；未登记视为最高危，失败关闭而不是默认放行。"""
    return getattr(fn, "safety_class", SafetyClass.TOKEN_GATED_WRITE)


def is_automatable(fn: Callable) -> bool:
    return safety_class_of(fn).value <= MAX_AUTOMATED.value
