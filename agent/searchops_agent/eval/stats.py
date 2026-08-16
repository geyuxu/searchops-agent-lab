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

两种分析并列（本模块后半段）
----------------------------
上面这套是**整体配对检验**：在预先定好的评测子集上比均值，是门禁的唯一依据。
但符号空间（synonyms / rewrite_rules）的提案效应是稀疏的，稀疏效应在整体均值上
必然被稀释成"测不出来"。所以模块后半段另给一套**受影响子集**分析
（`affected_analysis`）：先机械算出哪些查询的指标真的动了，再在那个子集上单独做
同一套检验。它是事后条件化的，只作描述性证据，永远不参与门禁——理由与代价写在
`POST_HOC_CAVEAT` 与该节的注释里。
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


# =============================================================================
# 受影响子集：稀疏效应的事后条件化分析
# =============================================================================
#
# 为什么需要它
# ------------
# 符号空间（synonyms / rewrite_rules）的提案效应是**稀疏**的。引擎侧的机制决定了
# 稀疏程度（见 SearchQueryCompiler.applyRewrite / expandSynonyms）：
#   * rewrite_rules 走 `normalized.equals(rule.match())` —— 整串相等 + 整条替换，
#     所以一条规则最多影响一条查询；30 条规则在 1400 条评测集上最多动 30 条，
#     对均值的影响上界约 0.02，而 n=1400 的最小可检出差已经约 0.0103。
#   * synonyms 走 `lower.contains(term)` —— 子串包含触发，能泛化到证据之外，
#     是唯一可能撬动均值的杠杆，代价是会误伤（term="pen" 在 open/pencil 上也触发）。
#
# 结果就是：整体均值上"什么都没发生"，与"20 条查询被显著改善、10 条被改坏"这两种
# 完全不同的事实，在全子集配对检验里长得一模一样。只报整体检验会把后者当成前者。
#
# 这个模块给出的是**机械可算的**第二视角：先逐查询比对候选与基线，取指标真的动了的
# 那些 query_id，再在这个子集上单独做同一套配对检验。
#
# 为什么它不能当门禁依据（必须一直写在脸上）
# ------------------------------------------
# 受影响子集是**事后条件化**（post-hoc conditioning / selection on the outcome）的：
# 成员资格由候选自己的评测结果决定，选中条件恰好就是"这条查询的指标动了"。
# 于是零差值样本被系统性地排除在外，子集内的差值天然远离零 —— 检验的名义错误率
# 不再成立，p 值不能与预注册的整体检验同等看待。
#
# 它回答的是"提案实际做了什么"（描述性），不回答"提案是否有效"（推断性）。
# 门禁只依据整体检验；用子集结论去放行等于挑一个对自己有利的切片。
# 调用方（loop.py）在产物里必须把这条 caveat 原样带上。

AFFECTED_TOLERANCE = 1e-9
"""判定"指标发生了变化"的浮点容差。**绝不用 `==` 比较浮点。**

取 1e-9 不是拍脑袋：k=10 下 NDCG@10 / Recall@10 的**最小可实现变化**在 1e-3 量级
（换一个位次或换一个命中文档，量级就是 1/log2(i+1) 或 1/|relevant|），而 JSON
往返与双精度求和引入的噪声在 1e-15 量级。1e-9 落在两者中间足足六个数量级的空档里，
所以这个阈值在 [1e-12, 1e-6] 内任取都得到同一个子集——结论对容差不敏感。
"""

AFFECTED_DETECT_METRICS = ("ndcg10", "recall10")
"""默认的**检出口径**：判定一条查询是否"受影响"时看哪些指标。

以 ndcg10 为准（它对位次与命中都敏感，是主指标），同时记录 recall10（它对
"召回集合变了但排序没变"更敏感）。任一指标动了就算受影响——取并集是保守方向：
宁可把一条其实没怎么动的查询算进来，也不要漏掉一条真的动了的。
"""


class MetricMissing(KeyError):
    """逐查询比对时某条查询缺了要比的指标。失败关闭：不猜、不跳过、直接报错。"""


def _metric_value(row: dict, metric: str, query_id: int, side: str) -> float:
    try:
        return float(row[metric])
    except KeyError as exc:
        raise MetricMissing(f"{side} 的 query_id={query_id} 缺少指标 {metric!r}") from exc


@dataclass(frozen=True)
class WinLoss:
    """某指标在一批查询上的方向计数。均值会把"20 涨 10 跌"抹成一个小正数，这里不抹。"""

    metric: str
    wins: int
    losses: int
    ties: int

    @property
    def n(self) -> int:
        return self.wins + self.losses + self.ties

    def summary(self) -> str:
        return f"{self.metric}: win {self.wins} / loss {self.losses} / tie {self.ties}"


def win_loss(
    baseline: dict[int, dict],
    candidate: dict[int, dict],
    metric: str,
    *,
    query_ids: list[int] | tuple[int, ...] | None = None,
    tolerance: float = AFFECTED_TOLERANCE,
) -> WinLoss:
    """逐查询数涨跌。`tolerance` 之内算平局——与受影响子集用同一把尺子，两处不能各判各的。"""
    ids = sorted(set(baseline) & set(candidate)) if query_ids is None else list(query_ids)
    wins = losses = ties = 0
    for q in ids:
        d = _metric_value(candidate[q], metric, q, "candidate") - _metric_value(
            baseline[q], metric, q, "baseline"
        )
        if d > tolerance:
            wins += 1
        elif d < -tolerance:
            losses += 1
        else:
            ties += 1
    return WinLoss(metric=metric, wins=wins, losses=losses, ties=ties)


def changed_query_ids(
    baseline: dict[int, dict],
    candidate: dict[int, dict],
    *,
    metrics: tuple[str, ...] = AFFECTED_DETECT_METRICS,
    tolerance: float = AFFECTED_TOLERANCE,
) -> dict[str, list[int]]:
    """metric → 该指标发生了变化的 query_id 列表（容差比较，两侧都出现的查询才算）。"""
    shared = sorted(set(baseline) & set(candidate))
    out: dict[str, list[int]] = {}
    for metric in metrics:
        out[metric] = [
            q
            for q in shared
            if abs(
                _metric_value(candidate[q], metric, q, "candidate")
                - _metric_value(baseline[q], metric, q, "baseline")
            )
            > tolerance
        ]
    return out


def zero_result_flips(baseline: dict[int, dict], candidate: dict[int, dict]) -> list[int]:
    """zero_result 布尔翻转的 query_id。

    单独报告、**不**并入受影响子集的定义（子集定义按指标值变化，见
    `AFFECTED_DETECT_METRICS`）。存在的意义是补一个盲区：基线与候选都没命中任何
    相关文档时，指标可以两边都是 0 而检索结果其实换了一批，此时指标口径看不见它。
    """
    shared = sorted(set(baseline) & set(candidate))
    return [q for q in shared if bool(baseline[q].get("zero_result")) != bool(candidate[q].get("zero_result"))]


POST_HOC_CAVEAT = (
    "受影响子集是事后条件化（post-hoc conditioning）的：成员资格由候选自己的评测结果决定，"
    "选中条件恰好是『这条查询的指标动了』，零差值样本被系统性排除，子集内的差值天然远离零。"
    "因此该子集的 p 值不具备预注册检验的错误率保证，只能当描述性证据看，"
    "不得与整体配对检验的结论同等看待。门禁判定只依据整体检验。"
)

AFFECTED_DEFINITION = (
    "候选评测与基线逐 query_id 比对，检出口径内任一指标的差值绝对值 > tolerance 即计入；"
    "浮点用容差比较，不用等号。"
)


@dataclass(frozen=True)
class AffectedSubset:
    """受影响子集的完整分析结果。**描述性**，不参与任何门禁判定。

    `post_hoc` 恒为 True 且没有关掉它的入口——这个字段的用途就是让产物里永远带着
    这条限定，而不是靠写报告的人记得加一句。
    """

    detect_metrics: tuple[str, ...]
    tolerance: float
    overall_n: int
    query_ids: tuple[int, ...]
    changed_by_metric: dict[str, tuple[int, ...]]
    zero_result_flip_ids: tuple[int, ...]
    results: tuple[PairedResult, ...]
    win_loss: dict[str, WinLoss]
    post_hoc: bool = True
    caveat: str = POST_HOC_CAVEAT
    definition: str = AFFECTED_DEFINITION

    @property
    def size(self) -> int:
        return len(self.query_ids)

    @property
    def ratio(self) -> float:
        return self.size / self.overall_n if self.overall_n else 0.0

    @property
    def is_empty(self) -> bool:
        """子集为空：候选与基线逐查询完全一致，提案在这批查询上是恒等变换。

        这本身是一条很有信息量的结论——例如"剥前导标点"的改写，分析器在分词阶段
        已经剥掉了，token 层面是恒等的，于是一条查询都不会动。
        """
        return self.size == 0

    def result_for(self, metric: str) -> PairedResult | None:
        return next((r for r in self.results if r.metric == metric), None)

    def summary(self) -> str:
        head = (
            f"受影响子集（事后条件化，非门禁依据）：{self.size}/{self.overall_n} 条"
            f"（{self.ratio:.2%}），检出口径 {'/'.join(self.detect_metrics)}，容差 {self.tolerance:g}"
        )
        if self.is_empty:
            return head + "\n  子集为空：候选与基线逐查询完全一致（提案在这批查询上是恒等变换）"
        lines = [head]
        for r in self.results:
            wl = self.win_loss.get(r.metric)
            lines.append(f"  {r.summary()}" + (f"，{wl.summary()}" if wl else ""))
        if self.zero_result_flip_ids:
            lines.append(f"  zero_result 翻转 {len(self.zero_result_flip_ids)} 条（不计入子集定义）")
        return "\n".join(lines)


def affected_analysis(
    baseline: dict[int, dict],
    candidate: dict[int, dict],
    *,
    metrics: tuple[str, ...] = METRICS,
    detect_metrics: tuple[str, ...] = AFFECTED_DETECT_METRICS,
    tolerance: float = AFFECTED_TOLERANCE,
    **kw,
) -> AffectedSubset:
    """算出受影响子集，并在该子集上跑同一套配对检验（复用 `compare`）。

    `metrics` 是在子集上报告的指标（调用方应传门禁实际比过的那几个，好让"整体 vs 子集"
    逐指标对齐）；`detect_metrics` 是判定"是否受影响"的检出口径，两者互不相同。

    子集上的检验**刻意复用 `compare`**（同一 bootstrap / 同一置换检验 / 同一随机种子），
    这样两套结论的唯一差别就是样本集合，不掺入第二套统计实现带来的差异。

    注意子集上的 p 值**不做 BH 校正**：BH 控制的是一个预先定好的检验族的 FDR，而这里
    的检验根本不在预注册族里。给它套一个 BH 会让它看起来像门禁族的一员——正好是要避免的。
    """
    shared = set(baseline) & set(candidate)
    if not shared:
        raise ValueError("两次评测没有共同的 query_id，无法计算受影响子集")

    changed = changed_query_ids(baseline, candidate, metrics=detect_metrics, tolerance=tolerance)
    ids = tuple(sorted({q for qs in changed.values() for q in qs}))

    results: tuple[PairedResult, ...] = ()
    wl: dict[str, WinLoss] = {}
    if ids:
        sub_b = {q: baseline[q] for q in ids}
        sub_c = {q: candidate[q] for q in ids}
        results = tuple(compare(sub_b, sub_c, metrics=metrics, **kw))
        wl = {
            m: win_loss(baseline, candidate, m, query_ids=ids, tolerance=tolerance) for m in metrics
        }

    return AffectedSubset(
        detect_metrics=tuple(detect_metrics),
        tolerance=tolerance,
        overall_n=len(shared),
        query_ids=ids,
        changed_by_metric={m: tuple(qs) for m, qs in changed.items()},
        zero_result_flip_ids=tuple(zero_result_flips(baseline, candidate)),
        results=results,
        win_loss=wl,
    )
