"""治理边界的可执行断言。

这些测试是本项目的核心主张——"Agent 可提案不可批准"不是提示词里的一句话，
而是一条会让 CI 变红的性质。
"""

from __future__ import annotations

import pytest

from searchops_agent.client import SearchOpsClient
from searchops_agent.safety import MAX_AUTOMATED, SafetyClass, safety_class_of
from searchops_agent.tools import ALLOWED, FORBIDDEN, GovernanceViolation, build_registry


@pytest.fixture
def registry():
    return build_registry(SearchOpsClient(base_url="http://127.0.0.1:9"))


def test_privileged_operations_absent_from_registry(registry):
    for name in FORBIDDEN:
        assert name not in registry, f"{name} 不得暴露给提案者"


def test_every_exposed_tool_is_within_automation_ceiling(registry):
    for tool in registry.values():
        assert tool.safety_class.value <= MAX_AUTOMATED.value, (
            f"{tool.name} 的等级 {tool.safety_class.name} 越过自动化上限"
        )


def test_approve_publish_rollback_really_are_privileged():
    """防止有人把危险方法降级来绕过上面的检查。"""
    expected = {
        "approve": SafetyClass.PRIVILEGED_WRITE,
        "publish": SafetyClass.TOKEN_GATED_WRITE,
        "rollback": SafetyClass.TOKEN_GATED_WRITE,
    }
    for name, level in expected.items():
        assert safety_class_of(getattr(SearchOpsClient, name)) is level


def test_unregistered_method_defaults_to_most_dangerous():
    """未登记等级的方法必须按最高危处理——失败关闭而不是默认放行。"""
    assert safety_class_of(lambda: None) is SafetyClass.TOKEN_GATED_WRITE


def test_registry_rejects_privileged_method_added_to_allowlist(monkeypatch):
    monkeypatch.setattr("searchops_agent.tools.ALLOWED", ALLOWED + ("publish",))
    with pytest.raises(GovernanceViolation):
        build_registry(SearchOpsClient(base_url="http://127.0.0.1:9"))


def test_writes_require_idempotency_key():
    """所有治理写操作都必须带 Idempotency-Key，重试才不会重复建单。"""
    import inspect

    for name in ("create_draft", "submit"):
        params = inspect.signature(getattr(SearchOpsClient, name)).parameters
        assert "idempotency_key" in params, f"{name} 缺少幂等键参数"
