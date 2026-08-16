"""晋级门禁：候选策略能否进入人工审批。

门禁刻意是"失败关闭"的——任何一条判据缺数据都判 BLOCK，而不是默认放行。
均值提升不足以晋级：还要求主指标的提升在配对检验下显著、zero-result 不劣化、
且没有一批查询被严重打崩（均值可以掩盖长尾崩塌）。

关于"显著"：门禁**不复用** stats.PairedResult.significant（那是报告口径，纯 p 值判定）。
门禁按方向使用两个不同的判据，因为"失败关闭"在两个方向上要求相反的严格度：

  主指标（要放行）  stats.promotion_evidence()  BH 通过 且 p<0.05 且 CI 不跨零（合取，难放行）
  护栏指标（要拦阻）stats.harm_evidence()       p<0.05 或 CI 不跨零（析取、不做 BH，易拦阻）

改动前两处共用一个"CI 不跨零"布尔：主指标方向上恰好等价于今天的合取（因为它
与 BH 标志相与），护栏方向上则偏松。理由与代价写在 stats.py 的两个函数里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .stats import PairedResult, benjamini_hochberg, compare, harm_evidence, promotion_evidence


@dataclass
class GatePolicy:
    primary_metric: str = "ndcg10"
    require_significant: bool = True
    """主指标必须显著提升，而不只是均值更高。"""

    max_zero_result_increase: float = 0.0
    """zero-result 率允许的绝对上升幅度。默认零容忍。"""

    max_catastrophic_ratio: float = 0.10
    """主指标跌幅超过 catastrophic_drop 的查询占比上限。"""

    catastrophic_drop: float = 0.30
    guard_metrics: tuple[str, ...] = ("recall10", "mrr10")
    """护栏指标：不要求提升，但不允许显著劣化。"""


#: 配对置换检验的零分布只由**非零差值**决定：受影响 k 条时只有 2^k 种符号组合，
#: 因此可达的最小 p 是 1/2^k。k=3 时下限 0.125、k=4 时 0.0625，都大于 α=0.05 ——
#: 也就是说**无论候选策略多好，检验都不可能判显著**。
#:
#: 这类情形必须与"测过了，没通过"分开报。两者都返回 BLOCK 的话，
#: 门禁就重演了本项目一路在修的同一个缺陷：把两种成因不同的状态塌缩成同一个信号
#: （见 AiRewriteStatus 的由来）。运维看到 BLOCK 会以为提案被否决了，
#: 实际是这批证据根本不足以裁决它。
MIN_AFFECTED = 5
"""判定所需的最少受影响查询数。低于它则不给结论，只报证据不足。"""


@dataclass
class GateDecision:
    promote: bool
    reasons: list[str] = field(default_factory=list)
    results: list[PairedResult] = field(default_factory=list)
    alignment: str = ""
    #: 本次判定是否**根本不具备分辨力**（受影响查询太少，检验的 p 下限就大于 α）。
    #: 与"测了但没通过"必须分开——理由见 MIN_AFFECTED 的注释。
    undecidable: bool = False
    affected: int = 0

    @property
    def verdict(self) -> str:
        if self.undecidable:
            return "INSUFFICIENT_EVIDENCE"
        return "PROMOTE" if self.promote else "BLOCK"

    def render(self) -> str:
        head = {
            "PROMOTE": "PROMOTE — 可进入人工审批",
            "BLOCK": "BLOCK — 不得提交审批",
            "INSUFFICIENT_EVIDENCE": "INSUFFICIENT_EVIDENCE — 证据不足以裁决，不得据此下结论",
        }[self.verdict]
        lines = [head, f"  配对覆盖：{self.alignment}"]
        lines += [f"  {r.summary()}" for r in self.results]
        lines += [f"  · {r}" for r in self.reasons]
        return "\n".join(lines)


def evaluate_gate(
    baseline: dict[int, dict], candidate: dict[int, dict], policy: GatePolicy | None = None, **kw
) -> GateDecision:
    policy = policy or GatePolicy()
    metrics = (policy.primary_metric,) + tuple(m for m in policy.guard_metrics if m != policy.primary_metric)
    results = compare(baseline, candidate, metrics=metrics, **kw)

    # 先看这次判定有没有分辨力：受影响查询太少时，检验的 p 下限就已经大于 α，
    # 再往下算也只会得到一个注定不显著的结果，然后被误读成"提案被否决"。
    shared_ids = sorted(set(baseline) & set(candidate))
    affected = sum(
        1 for q in shared_ids
        if abs(baseline[q][policy.primary_metric] - candidate[q][policy.primary_metric]) > 1e-9
    )
    if affected < MIN_AFFECTED:
        floor = 1.0 if affected == 0 else 1.0 / (2 ** affected)
        from .stats import align_report
        return GateDecision(
            promote=False,
            undecidable=True,
            affected=affected,
            results=results,
            alignment=align_report(baseline, candidate),
            reasons=[
                f"仅 {affected} 条查询的 {policy.primary_metric} 发生变化"
                f"（阈值 {MIN_AFFECTED}）；配对置换检验在该规模下的 p 下限为 {floor:.4f}，"
                "已大于显著性水平，本次判定不具备分辨力",
                "这不是『提案未通过』，而是『这批证据无法裁决该提案』——"
                "两者必须分开，否则会把无信息当成否定结论",
            ],
        )

    # 多指标同时判定，先做 BH 校正再谈显著性。
    # 门禁用的是 stats.promotion_evidence()——BH 通过 且 p<0.05 且 CI 不跨零 的合取，
    # 而不是报告口径 PairedResult.significant（那是纯 p 判定）。
    # 两者刻意不同：报告要说得清楚，门禁要失败关闭。理由与代价写在 promotion_evidence 里。
    flags = benjamini_hochberg([r.p_value for r in results])
    bh_pass = {r.metric: f for r, f in zip(results, flags)}
    survives = {r.metric: promotion_evidence(r, bh_pass[r.metric]) for r in results}

    reasons: list[str] = []
    promote = True
    primary = next(r for r in results if r.metric == policy.primary_metric)

    if policy.require_significant:
        if primary.delta <= 0:
            promote = False
            reasons.append(f"主指标 {primary.metric} 未提升（Δ{primary.delta:+.4f}）")
        elif not survives[primary.metric]:
            promote = False
            missing = []
            if not bh_pass[primary.metric]:
                missing.append("BH 校正未通过")
            if not primary.significant:
                missing.append("置换检验 p≥0.05")
            if not primary.ci_excludes_zero:
                missing.append("95% CI 跨零")
            reasons.append(
                f"主指标 {primary.metric} 提升未通过晋级显著性判据"
                f"（BH 校正 且 p<0.05 且 CI 不跨零）：{'、'.join(missing)}"
                f"（p={primary.p_value:.4f}，CI [{primary.ci_low:+.4f}, {primary.ci_high:+.4f}]）"
            )

    for r in results:
        if r.metric == policy.primary_metric:
            continue
        # 护栏用 harm_evidence()（析取、不做 BH），不是 survives（合取）。
        # 同一个"显著"在两个方向上的失败关闭方向相反：主指标要难以判定为提升，
        # 护栏要容易判定为劣化。旧实现两处共用一个布尔，护栏方向上其实是放松的。
        if r.delta < 0 and harm_evidence(r):
            promote = False
            reasons.append(
                f"护栏指标 {r.metric} 劣化且证据不可忽略（Δ{r.delta:+.4f}，"
                f"p={r.p_value:.4f}，CI [{r.ci_low:+.4f}, {r.ci_high:+.4f}]）"
            )

    shared = sorted(set(baseline) & set(candidate))
    b_zero = sum(bool(baseline[q].get("zero_result")) for q in shared) / len(shared)
    c_zero = sum(bool(candidate[q].get("zero_result")) for q in shared) / len(shared)
    if c_zero - b_zero > policy.max_zero_result_increase:
        promote = False
        reasons.append(f"zero-result 率上升 {c_zero - b_zero:+.4f}，超出容忍值 {policy.max_zero_result_increase}")

    drops = sum(
        1 for q in shared
        if baseline[q][policy.primary_metric] - candidate[q][policy.primary_metric] > policy.catastrophic_drop
    )
    ratio = drops / len(shared)
    if ratio > policy.max_catastrophic_ratio:
        promote = False
        reasons.append(
            f"{drops}/{len(shared)} ({ratio:.1%}) 条查询的 {policy.primary_metric} "
            f"跌幅超过 {policy.catastrophic_drop}，超出上限 {policy.max_catastrophic_ratio:.0%}"
        )

    if promote:
        reasons.append("全部判据通过；发布仍需人类审批与令牌，Agent 到此为止")

    from .stats import align_report
    return GateDecision(promote=promote, reasons=reasons, results=results,
                        alignment=align_report(baseline, candidate))
