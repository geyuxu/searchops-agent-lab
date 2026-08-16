"""评测结果的配对统计检验。

存在理由不是"课上教过"，而是：搜索策略之间的指标差常常落在噪声范围内。只报均值
会把随机波动当成改进，进而把无效策略推上线。逐查询指标让配对检验成为可能——
同一批查询在两个策略下的表现是配对样本，不是独立样本。

只依赖 numpy，不引入 scipy。

判定口径（方法学选择，不是实现细节）
------------------------------------
本模块给出两个互不相同的证据：

* `p_value` —— 配对置换检验。直接检验零假设"策略标签与结果无关"。
* `[ci_low, ci_high]` —— bootstrap 百分位区间。是对差值的**区间估计**，
  "不跨零"只是检验的一个近似代理。

两者出自两套不同的重采样过程（置换 vs 有放回重采样），零分布也不同：
置换检验在"符号可翻转"的零假设下构造分布，bootstrap 则围绕观测到的差值分布重采样。
在效应接近判定阈值时，二者**不必然一致**——这不是 bug，是两种推断的固有差异。
实测反例（experiments/rerank-holdout-verdict.json，depth_ladder n20→n50）：
mrr10 的 p=0.0552（≥0.05）而 95% CI = [+0.0001, +0.0299]（不跨零）。

**本模块采用口径 (a)：单一布尔 `significant` 一律以 p 值为准（p < ALPHA）。**

理由：
1. "显著"这个词在统计学里有约定含义，就是"检验的 p 小于显著性水平"。摘要里
   紧挨着标签打印的正是 p 值；标签与它相矛盾，读者无法自洽解读。
2. 置换检验是**针对零假设的检验**，而百分位 CI 是估计量。用估计量的边界去
   冒充检验结论，等于换了一个没写下来的判据——原实现正是这么错的。
3. 只有一个判据能被单独说清楚，才谈得上"口径"。

代价（明写，不掩盖）：
* 习惯"看 CI 是否跨零"的读者会在边界处感到意外。为此 `summary()` 在两者
  不一致时**显式打印"边界"**并说明分歧方向，而不是悄悄按 p 值下结论。
* 单纯的 p 判定不含效应量信息，所以 CI、Δ、Cliff's delta 一律照常报告；
  `significant` 从来不是唯一结论，只是不再由 CI 冒名顶替。
* 晋级门禁（gate.py）需要的是**失败关闭**，而失败关闭在两个方向上要求相反的
  严格度，所以门禁不复用本口径，另有两个各自具名的判据：
    - `promotion_evidence()` 合取（BH 且 p<0.05 且 CI 不跨零）—— 难以判定为"提升"；
    - `harm_evidence()`      析取（p<0.05 或 CI 不跨零）      —— 容易判定为"劣化"。
  门禁与报告不同是有意为之：三个判据各有名字、各有定义，不再共用一个"显著"。

只改标签与布尔判定；Δ / CI / p / 效应量的计算逻辑一律未动。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

METRICS = ("ndcg10", "recall10", "precision10", "mrr10")

ALPHA = 0.05
"""显著性水平。`significant` 的唯一阈值，也是 BH 校正的默认 alpha。"""


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
        """唯一的显著性判定：置换检验 p < ALPHA。

        历史：这里曾经写的是"CI 不跨零"，于是 p=0.0552 / CI [+0.0001,+0.0299]
        的边界情形被打上"显著"。见模块头部的口径说明。
        """
        return self.is_significant()

    def is_significant(self, alpha: float = ALPHA) -> bool:
        """显式给定显著性水平的版本，供需要非 0.05 阈值的调用方使用。"""
        return self.p_value < alpha

    @property
    def ci_excludes_zero(self) -> bool:
        """95% bootstrap 区间是否不跨零。

        独立报告项，**不**参与 `significant` 的合成。它比 p 值多携带方向与
        幅度信息，因此仍然打印；但它是区间估计，不是检验结论。
        """
        return (self.ci_low > 0) or (self.ci_high < 0)

    @property
    def verdicts_agree(self) -> bool:
        """p 判定与 CI 判定是否一致。不一致本身就是要报告的信息。"""
        return self.significant == self.ci_excludes_zero

    @property
    def verdict_label(self) -> str:
        """摘要里的显著性标签。以 p 为准，分歧时把分歧写在脸上。"""
        base = "显著" if self.significant else "不显著"
        if self.verdicts_agree:
            return base
        note = "CI 跨零" if self.significant else "CI 不跨零"
        return f"{base}(边界：p 与 CI 分歧，{note})"

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
        mark = self.verdict_label
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


def promotion_evidence(result: PairedResult, bh_pass: bool, *, alpha: float = ALPHA) -> bool:
    """晋级门禁专用的显著性判据：比报告口径更严的**合取**。

    要求三件事同时成立：
      1. `bh_pass` —— 多指标 BH 校正后仍然通过（控制族错误率）；
      2. `result.is_significant(alpha)` —— 置换检验本身显著；
      3. `result.ci_excludes_zero` —— bootstrap 区间不跨零。

    为什么门禁不复用报告口径 (a)：报告要的是"说得清楚"，门禁要的是"失败关闭"。
    在 p 与 CI 分歧的边界带上，没有理由押注其中一个是对的——两个重采样过程都
    没给出干净结论时，正确的动作是不晋级、去拿更多数据或复跑，而不是放行。

    代价：功效降低。真实存在但幅度贴近噪声的提升会被挡下（假阴性）。对一个
    只能提案、必须由人批准的门禁来说，假阴性远比假阳性便宜。

    注意这与改动前的门禁行为**等价或更严**——改动前是 `bh_pass and CI 不跨零`，
    这里只是把隐式的合取写成显式的，并补上第 2 条（BH 通过已蕴含 p ≤ alpha，
    仅在 p 恰好等于 alpha 的极端情形下更严，方向仍是失败关闭）。
    """
    return bool(bh_pass) and result.is_significant(alpha) and result.ci_excludes_zero


def harm_evidence(result: PairedResult, *, alpha: float = ALPHA) -> bool:
    """护栏指标专用：是否有证据表明该指标被打坏。与晋级判据方向相反的**析取**。

    晋级判据和护栏判据不能是同一个式子，因为"失败关闭"在两个方向上要求相反的严格度：
    * 主指标：证据要强才放行 → 合取（难以判定为"提升"）。
    * 护栏指标：证据只要有苗头就拦 → 析取（容易判定为"劣化"）。
      若护栏也用合取，等于"更难证明被打坏"，反而更容易放行——方向错了。

    因此这里取 `p < alpha` 或 `CI 不跨零` 任一成立即算劣化，且**不做 BH 校正**：
    BH 压的是假阳性，而在护栏方向上假阳性（多拦一次）正是我们愿意付的代价。

    代价：护栏更容易触发，正常波动可能把一个真实有效的候选挡在门外，需要人去看
    coverage 与逐查询分布来推翻。这条代价是明知故犯的。

    调用方仍需自行判断方向（只在 delta < 0 时询问本函数）——本函数只回答
    "差异是否强到不该忽略"，不回答"差异往哪边"。
    """
    return result.is_significant(alpha) or result.ci_excludes_zero


def benjamini_hochberg(p_values: list[float], alpha: float = ALPHA) -> list[bool]:
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
