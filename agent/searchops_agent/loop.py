"""提案循环：诊断 → 提案 → 干跑 → 门禁 → 提交待审。

刻意不是一个自由的 ReAct 循环。阶段顺序固定，每个提案都必须走完 preview 与门禁
才可能进入 submit——模型能决定"提什么"，不能决定"跳过哪一步"。

循环终止于 SUBMITTED。approve 与 publish 不在本模块的能力范围内，
因为它们不在工具注册表里（见 tools.ALLOWED）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import SearchOpsClient, ToolGatewayError
from .eval.gate import GateDecision, GatePolicy, evaluate_gate
from .models import StrategyConfig
from .proposers import Proposal, Proposer
from .tools import Tool, build_registry


@dataclass
class Outcome:
    proposal: Proposal
    preview_changed: int | None = None
    decision: GateDecision | None = None
    strategy_id: str | None = None
    status: str = "pending"
    error: str | None = None

    def render(self) -> str:
        head = f"[{self.origin_tag}] {self.proposal.name} → {self.status}"
        lines = [head, f"    理由：{self.proposal.rationale}"]
        if self.preview_changed is not None:
            lines.append(f"    干跑：{self.preview_changed} 个位次变化")
        if self.decision:
            lines += ["    " + ln for ln in self.decision.render().splitlines()]
        if self.error:
            lines.append(f"    错误：{self.error}")
        return "\n".join(lines)

    @property
    def origin_tag(self) -> str:
        return self.proposal.origin


@dataclass
class LoopReport:
    diagnosis_size: dict[str, int] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def submitted(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == "submitted"]

    def render(self) -> str:
        lines = [f"诊断：{self.diagnosis_size}", f"提案 {len(self.outcomes)} 条，"
                 f"通过门禁并提交 {len(self.submitted)} 条"]
        lines += [o.render() for o in self.outcomes]
        return "\n".join(lines)


class ProposalLoop:
    def __init__(
        self,
        client: SearchOpsClient,
        proposer: Proposer,
        *,
        policy: GatePolicy | None = None,
        preview_query: str | None = None,
        dry_run: bool = True,
    ) -> None:
        self.client = client
        self.proposer = proposer
        self.policy = policy or GatePolicy()
        self.preview_query = preview_query
        self.dry_run = dry_run
        #: 通过注册表访问客户端，越权在构造期就会抛 GovernanceViolation
        self.tools: dict[str, Tool] = build_registry(client)

    # -- 阶段 -------------------------------------------------------------

    def diagnose(self, limit: int = 50) -> dict[str, Any]:
        return {
            "zero_result_queries": self.tools["zero_result_queries"](limit=limit),
            "low_quality_queries": self.tools["low_quality_queries"](limit=limit),
            "current": self.tools["current_strategy"](),
        }

    def preview(self, config: StrategyConfig, query: str) -> int:
        resp = self.tools["preview"](query=query, config=config)
        return len(resp.changed_positions)

    def run(
        self, baseline: dict[int, dict] | None = None, candidate_eval: dict[int, dict] | None = None
    ) -> LoopReport:
        diagnosis = self.diagnose()
        current: StrategyConfig = diagnosis["current"].config
        report = LoopReport(
            diagnosis_size={
                "zero_result": len(diagnosis["zero_result_queries"]),
                "low_quality": len(diagnosis["low_quality_queries"]),
            }
        )

        for proposal in self.proposer.propose(diagnosis, current):
            outcome = Outcome(proposal=proposal)
            try:
                probe = self.preview_query or _first_query(diagnosis)
                if probe:
                    outcome.preview_changed = self.preview(proposal.config, probe)

                # 门禁需要候选策略的评测结果。没有就只能停在 previewed，
                # 绝不因为"看起来合理"而放行。
                if baseline is None or candidate_eval is None:
                    outcome.status = "previewed（缺候选评测，未进入门禁）"
                    report.outcomes.append(outcome)
                    continue

                outcome.decision = evaluate_gate(baseline, candidate_eval, self.policy)
                if not outcome.decision.promote:
                    outcome.status = "blocked"
                elif self.dry_run:
                    outcome.status = "gate-passed（dry_run，未建草稿）"
                else:
                    draft = self.tools["create_draft"](name=proposal.name, config=proposal.config)
                    self.tools["submit"](strategy_id=draft.id)
                    outcome.strategy_id, outcome.status = draft.id, "submitted"
            except ToolGatewayError as exc:
                outcome.status, outcome.error = "error", str(exc)
            report.outcomes.append(outcome)

        return report


def _first_query(diagnosis: dict[str, Any]) -> str | None:
    for key in ("low_quality_queries", "zero_result_queries"):
        items = diagnosis.get(key) or []
        if items:
            return str(items[0].get("query") or "") or None
    return None
