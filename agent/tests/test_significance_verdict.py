"""显著性判定口径的可执行定义。

这些测试守的是一条方法学性质，不是实现细节：**"显著"这个标签必须只有一个定义，
并且和它旁边打印的 p 值自洽。**

背景：`PairedResult.significant` 曾经按"95% bootstrap CI 不跨零"判定，却把结论
印成"显著"。CI 与 p 来自两套不同的重采样过程（有放回重采样 vs 符号置换），在
边界处不必然一致。实测反例见 experiments/rerank-holdout-verdict.json 的
depth_ladder n20→n50：mrr10 的 p=0.0552（≥0.05）而 CI=[+0.0001,+0.0299]（不跨零），
旧口径打上"显著"。故障形状是"报告看起来自相矛盾"，读者只能二选一地猜。

下面的边界数据是合成的，并把 iterations / seed 钉死，因此结论可复现；同时保留
一个"同一份数据比较自身应判不显著"的零假设自检。
"""

from __future__ import annotations

import numpy as np
import pytest

from searchops_agent.eval.gate import GatePolicy, evaluate_gate
from searchops_agent.eval.stats import (
    ALPHA,
    PairedResult,
    compare,
    harm_evidence,
    paired_bootstrap,
    permutation_test,
    promotion_evidence,
)

# 边界数据对重采样参数敏感，必须钉死；改动这两个值等于换了一份数据。
ITERATIONS = 2000
SEED = 20260816

# 方向 A：CI 不跨零，但 p ≥ 0.05 —— 与实测 mrr10 反例同形状（旧口径会误标"显著"）。
DIFF_CI_SIG_P_NS = [
    0.4972, 0.4078, 0.2287, 0.0479, 0.3161, 0.4569, 0.1226, 0.1671, 0.3263,
    0.4515, 0.5390, 0.0842, 0.1187, 0.4817, 0.3903, 0.4354, 0.5981,
    -0.5641, -0.5074, -0.4685, -0.2431, -0.3883,
]

# 方向 B：p < 0.05，但 CI 跨零 —— 反向分歧，新口径会判"显著"但必须标注边界。
DIFF_P_SIG_CI_NS = [
    0.1539, 0.4039, 0.4196, 0.3285, 0.1634, 0.0112, 0.1255, 0.5019, 0.1352,
    0.0539, 0.1555, 0.3163, 0.3559, 0.5275, 0.5143, 0.5816, 0.5317, 0.2757,
    0.0937, 0.5739, 0.1450, 0.3771, 0.1660, 0.4919, 0.1285, 0.2583, 0.1396,
    0.5177, 0.3755,
    -0.3822, -0.1956, -0.1141, -0.5158, -0.4611, -0.4433, -0.5983, -0.3401,
    -0.1153, -0.3696, -0.1795, -0.3126, -0.2810,
]


def _runs(diff: list[float], *, base: float = 0.0, metric: str = "ndcg10"):
    """把差值向量铺成两份逐查询评测结果，供 compare()/evaluate_gate() 使用。

    base 取 0.0 是刻意的：`base + d - base` 在浮点下不必然等于 `d`，
    而下面的数值钉桩要求配对差值逐位等于 diff 本身。
    """
    baseline = {i: {metric: base, "zero_result": False} for i in range(len(diff))}
    candidate = {i: {metric: base + d, "zero_result": False} for i, d in enumerate(diff)}
    return baseline, candidate


def _result(diff: list[float], *, metric: str = "ndcg10") -> PairedResult:
    baseline, candidate = _runs(diff, metric=metric)
    (only,) = compare(baseline, candidate, metrics=(metric,), iterations=ITERATIONS, seed=SEED)
    return only


def _fixed(**overrides) -> PairedResult:
    """手工构造一个 PairedResult，用来单独钉住判定逻辑（不经过重采样）。"""
    fields = dict(
        metric="ndcg10", n=100, baseline_mean=0.5, candidate_mean=0.5,
        delta=0.0, ci_low=0.0, ci_high=0.0, p_value=1.0, cliffs_delta=0.0,
    )
    fields.update(overrides)
    return PairedResult(**fields)


# ---------------------------------------------------------------- 零假设自检

def test_same_data_compared_with_itself_is_not_significant():
    """零假设自检：同一份数据与自身比较，任何口径都必须判不显著。

    这条是整套检验的"体温计"——它一旦变红，说明检验本身有系统性偏向，
    此时所有既有结论都不可信。cli.py 的 selfcheck 子命令依赖同一性质。
    """
    rng = np.random.default_rng(20260816)
    same = {i: {m: float(v) for m, v in zip(("ndcg10", "recall10"), rng.random(2))} for i in range(120)}

    for r in compare(same, same, metrics=("ndcg10", "recall10"), iterations=ITERATIONS, seed=SEED):
        assert r.delta == 0.0
        assert r.p_value == 1.0, "全零差值下置换检验必须给出 p=1"
        assert r.ci_low == 0.0 and r.ci_high == 0.0
        assert r.significant is False
        assert r.ci_excludes_zero is False
        assert r.verdicts_agree is True
        assert "不显著" in r.summary() and "边界" not in r.summary()


# ------------------------------------------------- 口径定义：significant 只看 p

@pytest.mark.parametrize(
    "p_value, expected",
    [(0.0001, True), (0.0499, True), (0.05, False), (0.0552, False), (0.9, False)],
)
def test_significant_is_exactly_p_below_alpha(p_value, expected):
    """`significant` 的定义就是 p < ALPHA，边界取严（p == ALPHA 判不显著）。"""
    assert ALPHA == 0.05
    assert _fixed(p_value=p_value).significant is expected


def test_significant_ignores_the_confidence_interval():
    """CI 再怎么摆都不能改变 `significant`——它不再参与合成。"""
    assert _fixed(p_value=0.20, ci_low=0.001, ci_high=0.03).significant is False
    assert _fixed(p_value=0.01, ci_low=-0.03, ci_high=0.05).significant is True


def test_ci_excludes_zero_is_reported_separately():
    assert _fixed(ci_low=0.001, ci_high=0.03).ci_excludes_zero is True
    assert _fixed(ci_low=-0.03, ci_high=-0.001).ci_excludes_zero is True
    assert _fixed(ci_low=-0.01, ci_high=0.02).ci_excludes_zero is False
    assert _fixed(ci_low=0.0, ci_high=0.02).ci_excludes_zero is False, "下界贴零不算不跨零"


# -------------------------------------------------------- 边界：CI 与 p 不一致

def test_boundary_ci_excludes_zero_but_p_above_alpha():
    """旧口径的误标场景：CI 不跨零、p≥0.05。新口径判不显著，并显式标注分歧。"""
    r = _result(DIFF_CI_SIG_P_NS)

    assert r.ci_excludes_zero is True, "这份数据的前提就是 CI 不跨零"
    assert r.p_value >= ALPHA, "这份数据的前提就是 p 不小于 0.05"

    assert r.significant is False
    assert r.verdicts_agree is False
    assert r.verdict_label == "不显著(边界：p 与 CI 分歧，CI 不跨零)"

    s = r.summary()
    assert "不显著" in s and "边界" in s
    # 数值照常打印，读者能自己看到分歧的来源
    assert f"p={r.p_value:.4f}" in s
    assert f"CI [{r.ci_low:+.4f}, {r.ci_high:+.4f}]" in s


def test_boundary_p_below_alpha_but_ci_spans_zero():
    """反向分歧：p<0.05 但 CI 跨零。按口径判显著，但同样必须标注边界。"""
    r = _result(DIFF_P_SIG_CI_NS)

    assert r.p_value < ALPHA
    assert r.ci_excludes_zero is False

    assert r.significant is True
    assert r.verdicts_agree is False
    assert r.verdict_label == "显著(边界：p 与 CI 分歧，CI 跨零)"
    assert "边界" in r.summary()


def test_boundary_data_really_is_a_disagreement_not_an_artifact():
    """直接对底层两个过程取证，确认分歧来自过程本身而不是 PairedResult 的组装。"""
    for diff_list in (DIFF_CI_SIG_P_NS, DIFF_P_SIG_CI_NS):
        diff = np.array(diff_list, dtype=float)
        zeros = np.zeros_like(diff)
        _, lo, hi = paired_bootstrap(zeros, diff, iterations=ITERATIONS, seed=SEED)
        p = permutation_test(zeros, diff, iterations=ITERATIONS, seed=SEED)
        assert ((lo > 0) or (hi < 0)) != (p < ALPHA), "这份数据必须是分歧样本，否则测试失去意义"


# ------------------------------------------------------------------ 门禁口径

def test_promotion_evidence_needs_all_three_criteria():
    """晋级判据是合取：BH、p、CI 少一条都不放行（失败关闭）。"""
    strong = _fixed(p_value=0.001, ci_low=0.01, ci_high=0.05)
    assert promotion_evidence(strong, True) is True
    assert promotion_evidence(strong, False) is False, "BH 未通过不得放行"

    assert promotion_evidence(_fixed(p_value=0.20, ci_low=0.001, ci_high=0.03), True) is False
    assert promotion_evidence(_fixed(p_value=0.01, ci_low=-0.03, ci_high=0.05), True) is False


def test_harm_evidence_is_a_disjunction():
    """护栏判据是析取：任一过程报警就算劣化证据（方向相反的失败关闭）。"""
    assert harm_evidence(_fixed(p_value=0.20, ci_low=-0.03, ci_high=-0.001)) is True
    assert harm_evidence(_fixed(p_value=0.01, ci_low=-0.03, ci_high=0.05)) is True
    assert harm_evidence(_fixed(p_value=0.20, ci_low=-0.03, ci_high=0.05)) is False


def test_gate_blocks_on_both_kinds_of_boundary_data():
    """边界数据一律不得晋级——门禁在分歧带上不押注哪个过程是对的。"""
    for diff in (DIFF_CI_SIG_P_NS, DIFF_P_SIG_CI_NS):
        baseline, candidate = _runs(diff)
        decision = evaluate_gate(
            baseline, candidate,
            GatePolicy(primary_metric="ndcg10", guard_metrics=()),
            iterations=ITERATIONS, seed=SEED,
        )
        assert decision.promote is False
        assert any("未通过晋级显著性判据" in why for why in decision.reasons)


def test_gate_blocks_when_a_guard_metric_degrades_on_boundary_evidence():
    """护栏方向：主指标漂亮，护栏被打坏但证据只在 CI 一侧（p≥0.05 且 BH 拦不住）。

    旧实现在护栏上复用"BH 且 CI 不跨零"的合取，这一档会被判成"没有劣化证据"从而放行；
    析取口径下它必须 BLOCK。这条测试守的就是"失败关闭在护栏方向要反着来"。
    """
    n = len(DIFF_CI_SIG_P_NS)
    guard_diff = [-d for d in DIFF_CI_SIG_P_NS]  # 反号：护栏劣化，边界证据形状不变
    baseline = {i: {"ndcg10": 0.0, "recall10": 0.0, "zero_result": False} for i in range(n)}
    candidate = {
        i: {"ndcg10": 0.25, "recall10": guard_diff[i], "zero_result": False} for i in range(n)
    }

    decision = evaluate_gate(
        baseline, candidate,
        GatePolicy(primary_metric="ndcg10", guard_metrics=("recall10",)),
        iterations=ITERATIONS, seed=SEED,
    )

    guard = next(r for r in decision.results if r.metric == "recall10")
    assert guard.delta < 0
    assert guard.significant is False and guard.ci_excludes_zero is True, "前提：边界证据"

    assert decision.promote is False
    assert any("护栏指标 recall10 劣化" in why for why in decision.reasons)


def test_gate_promotes_when_both_procedures_agree():
    """两个过程都干净通过时仍然放行——修的是边界标签，不是把门禁焊死。"""
    diff = [0.25] * 60 + [0.20] * 60
    baseline, candidate = _runs(diff)
    decision = evaluate_gate(
        baseline, candidate,
        GatePolicy(primary_metric="ndcg10", guard_metrics=()),
        iterations=ITERATIONS, seed=SEED,
    )
    assert decision.promote is True
    assert decision.results[0].verdicts_agree is True
    assert decision.results[0].verdict_label == "显著"


# ----------------------------------------------------- 数值不得随标签改动而漂移

def test_numeric_outputs_are_pinned():
    """标签口径可以改，Δ / CI / p / 效应量不许动——这里按位钉住。

    期望值取自改动前的实现（同一 seed 与 iterations）。任何对重采样逻辑的
    "顺手优化"都会让这条变红。
    """
    r = _result(DIFF_CI_SIG_P_NS)
    assert r.n == 22
    assert r.delta == 0.15900454545454548
    assert r.ci_low == 0.006910113636363632
    assert r.ci_high == 0.303777159090909
    assert r.p_value == 0.05847076461769116
    assert r.cliffs_delta == pytest.approx(17 / 22 - 5 / 22)
    assert r.effect_label == "large"

    r2 = _result(DIFF_P_SIG_CI_NS)
    assert r2.n == 42
    assert r2.delta == 0.10748095238095237
    assert r2.ci_low == -0.0001758333333333347
    assert r2.ci_high == 0.20341339285714286
    assert r2.p_value == 0.046476761619190406


def test_gate_separates_undecidable_from_rejected():
    """受影响查询太少时报 INSUFFICIENT_EVIDENCE，不报 BLOCK。

    配对置换检验的零分布只由非零差值决定：受影响 k 条 → 只有 2^k 种符号组合 →
    可达最小 p = 1/2^k。k≤4 时下限 ≥0.0625 > α，检验根本不可能判显著。
    此时返回 BLOCK 等于把"无法裁决"说成"已被否决"——正是本项目一路在修的那类塌缩。
    """
    from searchops_agent.eval.gate import MIN_AFFECTED, GatePolicy, evaluate_gate

    base = {i: {"ndcg10": 0.5, "recall10": 0.5, "mrr10": 0.5, "zero_result": False} for i in range(200)}

    few = {i: dict(m) for i, m in base.items()}
    for i in range(MIN_AFFECTED - 1):          # 少于阈值
        few[i]["ndcg10"] = 0.95
    d = evaluate_gate(base, few, GatePolicy(), iterations=500)
    assert d.verdict == "INSUFFICIENT_EVIDENCE"
    assert d.undecidable and not d.promote
    assert d.affected == MIN_AFFECTED - 1

    many = {i: dict(m) for i, m in base.items()}
    for i in range(60):                        # 远超阈值，且是大幅提升
        many[i]["ndcg10"] = 0.95
    d2 = evaluate_gate(base, many, GatePolicy(), iterations=500)
    assert not d2.undecidable, "证据充足时不应再走证据不足分支"
    assert d2.verdict in ("PROMOTE", "BLOCK")
