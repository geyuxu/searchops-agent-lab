"""评测结果的配对统计检验。

存在理由不是"课上教过"，而是：搜索策略之间的指标差常常落在噪声范围内。只报均值
会把随机波动当成改进，进而把无效策略推上线。逐查询指标让配对检验成为可能——
同一批查询在两个策略下的表现是配对样本，不是独立样本。

只依赖 numpy，不引入 scipy。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

METRICS = ("ndcg10", "recall10", "precision10", "mrr10")


@dataclass(frozen=True)
class PairedResult:
    """一次配对比较的完整结论。字段命名刻意冗长，报告里直接引用不再二次解释。"""

    metric: str
    n: int
    baseline_mean: float
    candidate_mean: float
    delta: float
    ci_low: float
    ci_high: float
    p_value: float
    cliffs_delta: float

    @property
    def significant(self) -> bool:
        """置信区间不跨零。与 p 值互为印证，不单独依赖 p。"""
        return (self.ci_low > 0) or (self.ci_high < 0)

    @property
    def effect_label(self) -> str:
        a = abs(self.cliffs_delta)
        if a < 0.147:
            return "negligible"
        if a < 0.33:
            return "small"
        if a < 0.474:
            return "medium"
        return "large"

    def summary(self) -> str:
        mark = "显著" if self.significant else "不显著"
        return (
            f"{self.metric}: {self.baseline_mean:.4f} → {self.candidate_mean:.4f} "
            f"(Δ{self.delta:+.4f}, 95% CI [{self.ci_low:+.4f}, {self.ci_high:+.4f}], "
            f"p={self.p_value:.4f}, {mark}, 效应量 {self.effect_label})"
        )


def paired_bootstrap(
    baseline: np.ndarray, candidate: np.ndarray, *, iterations: int = 10_000, seed: int = 20260816
) -> tuple[float, float, float]:
    """对配对差值做 bootstrap，返回 (均值差, CI 下界, CI 上界)。

    重采样的是"查询"这一单位，因此保留了同一查询在两策略下的配对关系。
    """
    diff = candidate - baseline
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, diff.size, size=(iterations, diff.size))
    means = diff[idx].mean(axis=1)
    return float(diff.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def permutation_test(
    baseline: np.ndarray, candidate: np.ndarray, *, iterations: int = 10_000, seed: int = 20260816
) -> float:
    """配对置换检验：随机翻转每个查询上差值的符号，构造零分布。

    零假设是"策略标签与结果无关"，翻转符号正是该假设下的等价重排。
    """
    diff = candidate - baseline
    observed = abs(diff.mean())
    rng = np.random.default_rng(seed)
    signs = rng.choice((-1.0, 1.0), size=(iterations, diff.size))
    null = np.abs((diff * signs).mean(axis=1))
    # +1 平滑：避免在 iterations 有限时报出 p=0 这种不可能的精度
    return float((np.count_nonzero(null >= observed) + 1) / (iterations + 1))


def cliffs_delta(baseline: np.ndarray, candidate: np.ndarray) -> float:
    """非参数效应量，取值 [-1, 1]，对指标分布形态不敏感。"""
    diff = candidate - baseline
    n = diff.size
    if n == 0:
        return 0.0
    return float((np.count_nonzero(diff > 0) - np.count_nonzero(diff < 0)) / n)


def compare(
    baseline: dict[int, dict], candidate: dict[int, dict], *, metrics: tuple[str, ...] = METRICS, **kw
) -> list[PairedResult]:
    """按 query_id 对齐两次评测，逐指标做配对比较。

    只保留两侧都出现的 query_id——查询集不一致时静默取交集会掩盖问题，
    因此调用方应先用 align_report() 确认覆盖情况。
    """
    shared = sorted(set(baseline) & set(candidate))
    if not shared:
        raise ValueError("两次评测没有共同的 query_id，无法配对比较")

    results = []
    for metric in metrics:
        b = np.array([baseline[q][metric] for q in shared], dtype=float)
        c = np.array([candidate[q][metric] for q in shared], dtype=float)
        delta, lo, hi = paired_bootstrap(b, c, **kw)
        results.append(
            PairedResult(
                metric=metric,
                n=len(shared),
                baseline_mean=float(b.mean()),
                candidate_mean=float(c.mean()),
                delta=delta,
                ci_low=lo,
                ci_high=hi,
                p_value=permutation_test(b, c, **kw),
                cliffs_delta=cliffs_delta(b, c),
            )
        )
    return results


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """BH 校正。同时比较多个指标或多个候选策略时，不校正必然刷出假阳性。"""
    n = len(p_values)
    if n == 0:
        return []
    order = np.argsort(p_values)
    passed = np.zeros(n, dtype=bool)
    largest = -1
    for rank, idx in enumerate(order, start=1):
        if p_values[idx] <= alpha * rank / n:
            largest = rank
    if largest > 0:
        passed[order[:largest]] = True
    return passed.tolist()


def align_report(baseline: dict[int, dict], candidate: dict[int, dict]) -> str:
    """报告两次评测的查询集差异，避免"取交集"悄悄改变了比较对象。"""
    b, c = set(baseline), set(candidate)
    return (
        f"baseline {len(b)} 条 / candidate {len(c)} 条 / 共同 {len(b & c)} 条"
        f"（仅 baseline {len(b - c)}，仅 candidate {len(c - b)}）"
    )
