"""策略提案者。

两个实现构成对照组：

- `RuleProposer` 不调用任何模型，按检索诊断规则生成候选。它是基线，不是占位符——
  没有它就无法回答"LLM 提案到底带来了什么"。
- `LLMProposer` 用 LangChain 让模型基于同样的诊断证据提案，受同一套 schema 约束。

两者输出同一种 `Proposal`，因此可以在同一门禁下直接比较。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import StrategyConfig

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class Proposal:
    name: str
    config: StrategyConfig
    rationale: str
    evidence: list[str] = field(default_factory=list)
    origin: str = "unknown"


class Proposer(Protocol):
    name: str

    def propose(self, diagnosis: dict[str, Any], current: StrategyConfig) -> list[Proposal]: ...


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class RuleProposer:
    """基于检索失败模式的启发式提案（FOAM 的非 LLM 对照组）。

    覆盖三类可由证据直接推出的动作：
    1. zero-result 查询中反复出现的词 → 候选同义词扩展；
    2. 低质量查询里标题命中弱而正文命中强 → 上调 description/bullet 权重；
    3. 长查询召回差 → 下调 minimum_score 放宽门槛。
    """

    name = "rule"

    def __init__(self, weight_step: float = 0.25, min_support: int = 2) -> None:
        self.weight_step = weight_step
        self.min_support = min_support

    def propose(self, diagnosis: dict[str, Any], current: StrategyConfig) -> list[Proposal]:
        proposals: list[Proposal] = []
        zero = diagnosis.get("zero_result_queries", [])
        low = diagnosis.get("low_quality_queries", [])

        counts: dict[str, int] = {}
        for item in zero:
            for tok in _tokens(str(item.get("query", ""))):
                if len(tok) > 2:
                    counts[tok] = counts.get(tok, 0) + 1
        recurring = sorted((t for t, c in counts.items() if c >= self.min_support), key=lambda t: -counts[t])[:5]

        if recurring:
            cfg = current.model_copy(deep=True)
            cfg.minimum_score = max(0.0, current.minimum_score - 0.1)
            proposals.append(
                Proposal(
                    name=f"放宽最低分以覆盖 {len(recurring)} 个高频零结果词",
                    config=cfg,
                    rationale=(
                        f"零结果查询中反复出现 {', '.join(recurring[:3])}，"
                        f"提示门槛过高而非词表缺失，先放宽 minimum_score 验证。"
                    ),
                    evidence=[f"zero-result 查询 {len(zero)} 条，高频词 {recurring}"],
                    origin=self.name,
                )
            )

        if low:
            cfg = current.model_copy(deep=True)
            w = dict(cfg.field_weights)
            for f in ("description", "bullet_point"):
                w[f] = round(w.get(f, 1.0) + self.weight_step, 3)
            cfg.field_weights = w
            proposals.append(
                Proposal(
                    name="上调正文字段权重",
                    config=cfg,
                    rationale=(
                        f"{len(low)} 条低质量查询的意图词多落在描述与卖点而非标题，"
                        f"上调 description/bullet_point 权重 +{self.weight_step}。"
                    ),
                    evidence=[f"low-quality 查询 {len(low)} 条"],
                    origin=self.name,
                )
            )

        return proposals


class LLMProposer:
    """LangChain 提案者。模型只能产出结构化的 StrategyConfig，不能自由写字段。

    未配置模型时构造即失败，绝不静默退化成规则提案——否则对照实验会被污染。
    """

    name = "llm"

    def __init__(self, model: str = "claude-sonnet-5", temperature: float = 0.0) -> None:
        from langchain.chat_models import init_chat_model  # 延迟导入：无 key 时也能跑规则基线

        self._llm = init_chat_model(model, temperature=temperature).with_structured_output(_ProposalSchema)

    def propose(self, diagnosis: dict[str, Any], current: StrategyConfig) -> list[Proposal]:
        from .prompts import PROPOSAL_PROMPT

        out = self._llm.invoke(
            PROPOSAL_PROMPT.format(
                current=current.model_dump_json(indent=2),
                zero=_brief(diagnosis.get("zero_result_queries", [])),
                low=_brief(diagnosis.get("low_quality_queries", [])),
            )
        )
        return [
            Proposal(
                name=p.name,
                config=StrategyConfig.model_validate(p.config),
                rationale=p.rationale,
                evidence=p.evidence,
                origin=self.name,
            )
            for p in out.proposals
        ]


def _brief(items: list[dict], limit: int = 25) -> str:
    return "\n".join(f"- {i.get('query', '')}" for i in items[:limit]) or "（无）"


try:  # schema 仅在装了 pydantic 时定义，规则基线不依赖它
    from pydantic import BaseModel, Field

    class _OneProposal(BaseModel):
        name: str = Field(description="简短的策略名")
        rationale: str = Field(description="基于给定证据的理由，不得引入证据之外的事实")
        evidence: list[str] = Field(default_factory=list, description="引用的具体查询或指标")
        config: dict = Field(description="完整的 StrategyConfig")

    class _ProposalSchema(BaseModel):
        proposals: list[_OneProposal]

except ImportError:  # pragma: no cover
    _ProposalSchema = None  # type: ignore
