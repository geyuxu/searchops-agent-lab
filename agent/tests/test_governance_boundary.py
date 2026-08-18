"""治理边界的可执行断言。

这些测试是本项目的核心主张——"Agent 可提案不可批准"不是提示词里的一句话，
而是一条会让 CI 变红的性质。
"""

from __future__ import annotations

from pathlib import Path

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


def test_candidate_evaluation_is_dry_run(registry):
    """Agent 自证能力必须是 DRY_RUN 级：能算，但不写任何状态。

    这条保护的是提案闭环的正当性——Agent 之所以可以反复评测自己的候选策略，
    正是因为这条路径不落库、不读写 strategy 版本、不产生治理事件。
    一旦它被误标成写入级，Agent 试错就会污染策略历史与审计流。
    """
    assert "evaluate_candidate" in registry, "Agent 缺少自证能力，提案无法在提交前被验证"
    assert registry["evaluate_candidate"].safety_class is SafetyClass.DRY_RUN


def test_diagnosis_evidence_never_includes_holdout_queries():
    """诊断证据必须限定在 train 内——holdout 混进证据就是"用考题出题"。

    实测发生过：线上分析表不按 split 过滤，模型拿到 holdout 查询当证据，
    写出逐字钉死该查询的 rewrite_rule。那一轮评测在 train 上做，所以门禁判定没被污染；
    但这类提案一旦拿去 holdout 验收，那个数字就再也不能用了。

    query_id 75 与 288 是真实泄漏过的那两条，保留它们作为回归锚点。

    本用例刻意**不加载真实语料**：TrainBench.load() 要读
    platform/data/processed/queries.jsonl，而那是 gitignored 的派生产物，
    全新检出（例如 CI）里根本不存在。被测性质只依赖 bench 的两个集合，
    用合成的 bench 就足够——测试要守的是过滤逻辑，不是语料是否在场。
    """
    import types

    from searchops_agent.loop import ProposalLoop

    # 合成一个最小 bench：只提供 _train_only 真正读的那两个字段。
    train_ids = [11, 12, 13]
    train_texts = frozenset({"a train query", "another train query"})
    bench = types.SimpleNamespace(split_train_ids=train_ids, train_query_texts=train_texts)
    stub = types.SimpleNamespace(bench=bench, diagnosis_dropped=0)

    holdout_ids = {75, 288}  # 真实泄漏过的两条，此处不在 train 集合内
    rows = [
        {"query_id": 75, "query": "$13 bb guns without a yellow tube"},
        {"query_id": 288, "query": "- w nest cam iq outdoor security camera without the magnet"},
        {"query_id": 11, "query": "a train query"},          # 按 id 命中
        {"query": "another train query"},                    # 按文本命中（分析表可能不带 id）
        {"query": "来历不明的查询"},                          # 两路都不命中
    ]
    kept = ProposalLoop._train_only(stub, rows)

    assert not [r for r in kept if r.get("query_id") in holdout_ids], "holdout 查询混进了诊断证据"
    assert {r.get("query_id") for r in kept} == {11, None}, "train 侧的两条都该保留（id 与文本各一条）"
    # 归属不明的行一并丢弃：宁可少给证据，也不给来历不明的证据（失败关闭）。
    assert stub.diagnosis_dropped == 3
