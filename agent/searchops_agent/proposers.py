"""策略提案者。

两个实现构成对照组：

- `RuleProposer` 不调用任何模型，按检索诊断规则生成候选。它是基线，不是占位符——
  没有它就无法回答"LLM 提案到底带来了什么"。
- `LLMProposer` 用 LangChain 让模型基于同样的诊断证据提案，受同一套 schema 约束。

两者输出同一种 `Proposal`，因此可以在同一门禁下直接比较。


LLMProposer 的三条设计选择（写在这里，免得后面靠猜）
====================================================

**(1) 模型够不着 `field_weights` / `brand_boosts`——它们根本不在结构化输出的 schema 里。**
这不是"提示词里请求模型别调权重"，是结构上不给它这个字段。理由是三条实测结论：
218 组权重配置的扫描在 holdout 上只剩 +0.0035（p=0.1202，门禁 BLOCK）；`category` 字段
单桶 `Other`/20000 篇，IDF≈0，扫遍 18 档指标逐位不变；`best_fields` 无 `tie_breaker` 时
等比放大权重向量是恒等变换。也就是说连续空间的头部已经被数值搜索搜干净了，让 LLM 继续
拧权重只会产出必然被拦下的提案，而那会把"这条路本来就走到头了"误读成"LLM 没用"。
LLM 唯一可能还有增量的地方是符号空间：`synonyms` 与 `rewrite_rules`。
代价说清楚：如果日后想让模型重新参与权重，改一处（给 schema 加字段）即可，
约束只有这一个落点，不会散落在提示词的若干行里等着被模型忽略。

**(2) 引用与可行性由代码校验，不由提示词承诺。**
提示词可以要求"必须引用给定证据里的查询"，但只有代码能保证。`_Compiler` 逐条检查：
引用的 `query_id` 是否真在本轮诊断里（凭空捏造 → 整条提案作废）；同义词的 term 是否
真的能在某条证据查询里子串命中（`expandSynonyms` 的触发条件就是子串包含）；rewrite 的
`match` 归一化后是否真的等于某条证据查询（`applyRewrite` 是整串相等比较，不是模式匹配）。
被判掉的条目一律记进 `Proposal.guard_notes` 并打 WARN——是可见的，不是静默的。
特别说明否定守卫：与 `platform/services/ai-adapter` 的 `LangChainRewriteProvider` 同源同
词表。一条把 `without` 删掉的整串改写，交给 `operator=or` 的 `multi_match` 之后正好召回
用户想排除的商品，这是本数据集上最容易发生也最伤指标的一种"看起来合理"的改动。

**(3) 缺 key 在构造函数里抛异常，绝不退化成 RuleProposer。**
`cli.py` 的 `--proposer llm` 走到这里，如果静默退化，产出的报告会说"LLM 提案器跑了、
指标没提升"，而真相是模型压根没被调用。那份报告会进对照结论，比服务起不来坏得多。
代价：`import searchops_agent` 时 `__init__.py` 会导入本模块，所以 LangChain 的导入必须
延迟到 `__init__` 里做——没装 llm 附加依赖的人仍然可以跑规则基线。
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, Field

from .models import StrategyConfig
from .prompts import MAX_PROPOSALS, NEGATION_CUES, SYSTEM_PROMPT, USER_TEMPLATE

logger = logging.getLogger("searchops-agent")

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class Proposal:
    name: str
    config: StrategyConfig
    rationale: str
    evidence: list[str] = field(default_factory=list)
    origin: str = "unknown"
    model: str | None = None
    """实际产出这条提案的模型名；规则提案器为 None。

    为什么每条提案都要带：产物 JSON 里 `model` 只在 run 级别记一次
    （`loop._artifact`），而提案是逐条归档的。一份只在顶层写了模型名的产物，在提案被
    摘出来单独引用时就再也说不清是谁提的——这个可复现性缺口在本项目已经出现过两次。
    同样的理由，`origin` 也带上模型名（见 `LLMProposer.origin`），因为 `loop.py` 的
    逐提案产物字段里恰好有 `origin` 而没有 `model`。
    """
    guard_notes: list[str] = field(default_factory=list)
    """被守卫判掉的条目及原因。空列表表示模型的输出原样通过。"""


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


# --- 结构化输出 schema --------------------------------------------------------
#
# 为什么不让模型直接吐一整份 StrategyConfig（旧实现的做法）：
#   * 一份完整配置里，模型够得着的只有两个字段，其余六个字段它只能原样抄回来。让它抄
#     一遍，就是给它六个抄错的机会（实测里模型很爱"顺手优化"一下权重）。
#   * `dict[str, list[str]]` 这种"任意键"的映射在 tool-calling 的 JSON Schema 里要靠
#     `additionalProperties`，兼容端点对它的支持参差；而"对象数组"是所有端点都稳的形状。
# 所以模型产出的是**增量**，由 `_Compiler` 合并到 `current` 之上，再整体过一次契约校验。
#
# Field(description=...) 会进 tool schema，模型看得到——这些描述是提示词的一部分，
# 承担的是"这个字段到底怎么被引擎消费"的说明，与 prompts.py 里的长文互补而不重复。


class _SynonymEntry(BaseModel):
    term: str = Field(
        description=(
            "The trigger term, lowercase. The engine fires this entry when the lowercased "
            "query CONTAINS this term as a substring, so it must be distinctive enough not "
            "to fire on unrelated words."
        )
    )
    expansions: list[str] = Field(
        description=(
            "Words appended to the query when the term fires. Each entry adds tokens the "
            "shopper did not type, such as a canonical brand or the standard retail noun. "
            "Never a respelling of a word already in the query: fuzziness AUTO covers that."
        )
    )
    evidence_query_ids: list[int] = Field(
        description="query_id values from the evidence that this entry is meant to fix."
    )


class _RewriteRuleEntry(BaseModel):
    match: str = Field(
        description=(
            "The complete query this rule replaces, copied verbatim from the evidence. The "
            "engine compares it for equality against the whole lowercased query, so anything "
            "that is not exactly one of the evidence queries can never fire."
        )
    )
    rewrite: str = Field(
        description=(
            "The query sent to the engine instead. Plain search words separated by single "
            "spaces, no operators, no field syntax. If the matched query excludes something, "
            "the negation cue and the excluded noun must survive here verbatim."
        )
    )
    evidence_query_ids: list[int] = Field(
        description="query_id values from the evidence that this rule is meant to fix."
    )


class _ProposedChange(BaseModel):
    name: str = Field(description="Short English name for this candidate strategy.")
    rationale: str = Field(
        description=(
            "Why this change should raise NDCG@10, argued only from the evidence given, in "
            "at most three sentences. State no catalogue fact that is not visible in the "
            "evidence."
        )
    )
    synonyms: list[_SynonymEntry] = Field(
        default_factory=list, description="Synonym entries added by this proposal."
    )
    rewrite_rules: list[_RewriteRuleEntry] = Field(
        default_factory=list, description="Rewrite rules added by this proposal."
    )


class _ProposalBatch(BaseModel):
    proposals: list[_ProposedChange] = Field(
        description=(
            "At most three candidate changes, each aimed at one identifiable failure "
            "pattern. An empty list is a valid answer when no change would survive the gate."
        )
    )


# --- 证据与守卫 ---------------------------------------------------------------


def _normalize_query(value: str) -> str:
    """与 Java 侧 `SearchQueryCompiler.applyRewrite` 的归一化逐字一致。

    那边是 `String.join(" ", query.trim().split("\\s+")).toLowerCase()`。这里必须同口径，
    否则"这条 rewrite 规则会不会触发"的判断在两侧会给出不同答案。
    """
    return " ".join(str(value).split()).lower()


def _negates(value: str) -> bool:
    """这段文本是否还在表达"排除"。只查词表，不做句法分析——够用，且不会误伤。"""
    return bool(NEGATION_CUES & {tok.strip(".,;:!?") for tok in str(value).lower().split()})


def _metric(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "n/a"


@dataclass(frozen=True)
class _Evidence:
    """本轮诊断里模型可以引用的全部事实。超出这个集合的引用一律判为捏造。"""

    by_id: dict[int, str]
    """query_id → 归一化后的查询串。"""

    rendered_low: str
    rendered_zero: str

    @property
    def queries(self) -> set[str]:
        return set(self.by_id.values())

    @classmethod
    def build(cls, diagnosis: dict[str, Any], max_low: int, max_zero: int) -> _Evidence:
        by_id: dict[int, str] = {}

        def render(items: Any, limit: int) -> str:
            lines: list[str] = []
            for item in list(items or [])[:limit]:
                query = str(item.get("query", ""))
                if not query.strip():
                    continue
                try:
                    qid = int(item.get("query_id"))
                except (TypeError, ValueError):
                    continue
                by_id[qid] = _normalize_query(query)
                flag = " [NEGATION]" if _negates(query) else ""
                lines.append(
                    f"- query_id={qid} ndcg@10={_metric(item.get('ndcg10'))} "
                    f"recall@10={_metric(item.get('recall10'))}{flag} query={query!r}"
                )
            return "\n".join(lines) or "(none)"

        # 查询串用 repr 渲染：本数据集里前导标点（'!awnmower' / '# 2 pencils'）是真实信号，
        # 裸着打印会让模型看不出首尾空白与标点的确切位置。
        low = render(diagnosis.get("low_quality_queries"), max_low)
        zero = render(diagnosis.get("zero_result_queries"), max_zero)
        return cls(by_id=by_id, rendered_low=low, rendered_zero=zero)


class _Compiler:
    """把模型的增量编译成合法的 `StrategyConfig`，并逐条执行守卫。

    守卫全部是**机械可判定**的，没有一条依赖对语义的主观判断：要么对照证据集合查存在性，
    要么对照 `SearchQueryCompiler` 的触发条件查可行性，要么查契约上下界。
    """

    #: 契约上界，抄自 platform 的 `lab.searchops.domain.StrategyConfig` 注解。
    #: 超限时服务端返回 400，所以在这里先截断/拒绝，而不是把一份注定被拒的提案送出去。
    MAX_SYNONYM_KEYS: ClassVar[int] = 100
    MAX_SYNONYM_VALUES: ClassVar[int] = 20
    MAX_REWRITE_RULES: ClassVar[int] = 100
    MAX_RULE_CHARS: ClassVar[int] = 500
    MAX_FIELD_WEIGHTS: ClassVar[int] = 10
    FIELD_WEIGHT_RANGE: ClassVar[tuple[float, float]] = (0.0, 20.0)
    BRAND_BOOST_RANGE: ClassVar[tuple[float, float]] = (0.0, 100.0)

    def __init__(self, evidence: _Evidence, current: StrategyConfig) -> None:
        self.evidence = evidence
        self.current = current
        #: 只累计**存活下来的**条目所引用的 query_id。被守卫判掉的条目不算数——
        #: 否则一条被丢弃的规则会把它引用的查询写进 `Proposal.evidence`，
        #: 让产物声称"这条提案针对 query 17"，而实际配置里根本没有任何东西碰 query 17。
        self.cited: list[int] = []

    def _credit(self, ids: list[int]) -> None:
        for qid in ids:
            if qid not in self.cited:
                self.cited.append(qid)

    # -- 引用校验 ---------------------------------------------------------

    def _cited(self, ids: list[int], notes: list[str], what: str) -> list[int] | None:
        """返回被引用且真实存在的 query_id；捏造或缺失引用则返回 None（该条目作废）。"""
        if not ids:
            notes.append(f"丢弃 {what}：没有引用任何证据查询")
            return None
        unknown = [q for q in ids if int(q) not in self.evidence.by_id]
        if unknown:
            notes.append(f"丢弃 {what}：引用了本轮证据里不存在的 query_id {unknown}")
            return None
        return [int(q) for q in ids]

    # -- 两个旋钮 ---------------------------------------------------------

    def _synonyms(self, entries: list[_SynonymEntry], notes: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for entry in entries:
            term = _normalize_query(entry.term)
            label = f"同义词 {term!r}"
            if not term:
                notes.append("丢弃同义词：term 为空")
                continue
            cited = self._cited(entry.evidence_query_ids, notes, label)
            if cited is None:
                continue
            # `expandSynonyms` 的触发条件是 `query.toLowerCase().contains(term)`。term 若在
            # 任何一条证据查询里都不出现，这条同义词对本轮证据一定不触发——它不是保守的提案，
            # 是空提案，而空提案会被评测记成"LLM 没有收益"。
            if not any(term in q for q in self.evidence.queries):
                notes.append(f"丢弃 {label}：不是任何证据查询的子串，按引擎的子串触发条件永不生效")
                continue
            values: list[str] = []
            for raw in entry.expansions:
                value = " ".join(str(raw).split())
                if not value or value.lower() == term:
                    continue  # 把 term 自己加回查询是恒等操作
                if value not in values:
                    values.append(value)
            if not values:
                notes.append(f"丢弃 {label}：没有有效的展开项")
                continue
            if len(values) > self.MAX_SYNONYM_VALUES:
                notes.append(f"{label}：展开项 {len(values)} 条超过契约上限，截断到 {self.MAX_SYNONYM_VALUES}")
                values = values[: self.MAX_SYNONYM_VALUES]
            out[term] = values
            self._credit(cited)
        return out

    def _rewrite_rules(self, entries: list[_RewriteRuleEntry], notes: list[str]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in entries:
            match = _normalize_query(entry.match)
            rewrite = " ".join(str(entry.rewrite).split())
            label = f"改写规则 {match!r}"
            if not match or not rewrite:
                notes.append("丢弃改写规则：match 或 rewrite 为空")
                continue
            cited = self._cited(entry.evidence_query_ids, notes, label)
            if cited is None:
                continue
            # `applyRewrite` 是整串相等比较。match 不是证据里的某条查询 → 规则永不触发，
            # 同时也意味着模型凭空写了一条查询（违反"不得凭空发明"）。
            if match not in self.evidence.queries:
                notes.append(f"丢弃 {label}：与任何证据查询都不逐字相等，按整串相等的触发条件永不生效")
                continue
            if _normalize_query(rewrite) == match:
                notes.append(f"丢弃 {label}：rewrite 与 match 归一化后相同，是恒等改写")
                continue
            # 否定守卫。与 ai-adapter 的 LangChainRewriteProvider 同源：整串替换一旦丢掉
            # 否定词，交给 operator=or 的 multi_match 之后正好召回用户要排除的商品。
            if _negates(match) and not _negates(rewrite):
                notes.append(f"丢弃 {label}：原查询表达排除而改写把否定丢了（→ {rewrite!r}）")
                continue
            if len(match) > self.MAX_RULE_CHARS or len(rewrite) > self.MAX_RULE_CHARS:
                notes.append(f"丢弃 {label}：match/rewrite 超过契约的 {self.MAX_RULE_CHARS} 字符上限")
                continue
            if match in seen:
                notes.append(f"丢弃 {label}：同一条提案里重复的 match")
                continue
            seen.add(match)
            out.append({"match": match, "rewrite": rewrite})
            self._credit(cited)
        return out

    # -- 合并与契约校验 ---------------------------------------------------

    def compile(
        self, change: _ProposedChange
    ) -> tuple[StrategyConfig | None, list[str], list[int]]:
        """返回 (候选配置, 守卫记录, 被引用的 query_id)；无任何有效改动时配置为 None。

        为什么无效时也把 `notes` 带回来，而不是直接返回 None：那些记录正是"这条提案为什么
        没了"的唯一解释。丢掉它们，日志里就只剩"模型提了 3 条、留下 0 条"，无从分辨是模型
        在捏造引用、还是它写了一堆永不触发的规则——而这两者对模型的评价完全不同。

        每次调用都重置引用累加器：一个 `_Compiler` 会被同一轮的多条提案复用，
        引用若跨提案累加，第二条提案就会声称自己针对第一条提案的查询。
        """
        notes: list[str] = []
        self.cited = []
        synonyms = self._synonyms(change.synonyms, notes)
        rules = self._rewrite_rules(change.rewrite_rules, notes)
        if not synonyms and not rules:
            notes.append("本条提案没有留下任何有效改动，已整条丢弃")
            return None, notes, []

        cfg = self.current.model_copy(deep=True)
        merged_syn = dict(cfg.synonyms)
        merged_syn.update(synonyms)
        if len(merged_syn) > self.MAX_SYNONYM_KEYS:
            raise ValueError(
                f"合并后同义词 {len(merged_syn)} 键超过契约上限 {self.MAX_SYNONYM_KEYS}"
            )
        merged_rules = list(cfg.rewrite_rules) + rules
        if len(merged_rules) > self.MAX_REWRITE_RULES:
            raise ValueError(
                f"合并后改写规则 {len(merged_rules)} 条超过契约上限 {self.MAX_REWRITE_RULES}"
            )
        cfg.synonyms = merged_syn
        cfg.rewrite_rules = merged_rules

        return self.assert_contract(cfg), notes, sorted(self.cited)

    @classmethod
    def assert_contract(cls, cfg: StrategyConfig) -> StrategyConfig:
        """按 platform 的 StrategyConfig 注解做一次上下界检查。

        Python 侧的 pydantic 模型只声明了类型，没有 Java 侧的 @Size/@Min/@Max。不在这里查，
        违约就要等到 POST /evaluations/candidate 返回 400 才发现——那时错误信息落在 HTTP 层，
        跟"是哪条提案越界了"对不上号。模型够不着的字段（权重/加权）也一并查：它们是从线上
        current 继承来的，继承一份非法配置同样会让整轮提案在服务端炸掉。
        """
        weights = cfg.field_weights or {}
        if len(weights) > cls.MAX_FIELD_WEIGHTS:
            raise ValueError(f"field_weights {len(weights)} 项超过契约上限 {cls.MAX_FIELD_WEIGHTS}")
        lo, hi = cls.FIELD_WEIGHT_RANGE
        for name, value in weights.items():
            if not lo <= float(value) <= hi:
                raise ValueError(f"field_weights[{name}]={value} 越界 [{lo}, {hi}]")
        lo, hi = cls.BRAND_BOOST_RANGE
        for name, value in (cfg.brand_boosts or {}).items():
            if not lo <= float(value) <= hi:
                raise ValueError(f"brand_boosts[{name}]={value} 越界 [{lo}, {hi}]")
        for term, values in (cfg.synonyms or {}).items():
            if not str(term).strip():
                raise ValueError("同义词的 term 不得为空（Java 侧 @NotBlank）")
            if len(values) > cls.MAX_SYNONYM_VALUES:
                raise ValueError(
                    f"同义词 {term!r} 的展开项 {len(values)} 条超过契约上限 {cls.MAX_SYNONYM_VALUES}"
                )
            if any(not str(v).strip() for v in values):
                raise ValueError(f"同义词 {term!r} 含空展开项（Java 侧 @NotBlank）")
        for rule in cfg.rewrite_rules:
            if not str(rule.get("match", "")).strip() or not str(rule.get("rewrite", "")).strip():
                raise ValueError(f"改写规则的 match/rewrite 不得为空：{rule}")
        # 最后再走一次 pydantic：保证返回的确实是一个可序列化、可送上线的 StrategyConfig。
        return StrategyConfig.model_validate(cfg.model_dump())


# --- LLM 提案者 ---------------------------------------------------------------


class LLMProposer:
    """LangChain 提案者：`ChatOpenAI` + `with_structured_output`，走 OpenAI 兼容端点。

    为什么是 LangChain 而不是自己发 HTTP：`with_structured_output` 把"模型必须吐出结构化
    提案"下沉到 tool-calling 协议层，而不是靠正则去抠自然语言；换 provider（DeepSeek /
    Anthropic / 本地 vLLM）时这一层不用重写。这与
    `platform/services/ai-adapter/app/providers/langchain_rewrite.py` 是同一条链路、
    同一套配置风格，两处对齐可以省掉一次"到底是谁的问题"的排查。

    未配置 key 时构造即失败，绝不静默退化成规则提案——否则对照实验会被污染。
    """

    name = "llm"

    # ---- 默认档（本仓库实测跑通过的一套） ----------------------------------
    _DEFAULT_MODEL: ClassVar[str] = "qwen3.7-flash-2026-07-15"
    _DEFAULT_BASE_URL: ClassVar[str] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # DashScope 私有参数，必须在请求体**顶层**才生效（嵌套进去服务端静默忽略）。
    # 思考模式下 qwen3.x-flash 的单次调用从约 1s 涨到 8-28s，且强制 tool_choice 会被直接
    # 拒绝（"The tool_choice parameter does not support being set to required or object in
    # thinking mode"）——而 with_structured_output(method="function_calling") 正是要强制
    # tool_choice。所以这个开关不是调优项，是这条链路能不能跑通的前提。
    # 只在模型与端点**都**走默认档时才带上：发给别家端点会 400。
    _DEFAULT_EXTRA_BODY: ClassVar[dict[str, Any]] = {"enable_thinking": False}

    #: 允许的 structured-output 方法。`json_schema` 在部分兼容端点上 400，故不作默认值
    #: （DashScope / DeepSeek 实测：`response_format={"type":"json_schema"}` 直接被拒，
    #: 而 tool-calling 在两家都可用）。
    _METHODS: ClassVar[frozenset[str]] = frozenset({"function_calling", "json_mode", "json_schema"})

    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str | None = None,
        temperature: float | None = None,
        max_low: int = 40,
        max_zero: int = 20,
        max_proposals: int = MAX_PROPOSALS,
    ) -> None:
        # 延迟导入：`searchops_agent/__init__.py` 会导入本模块，而没装 llm 附加依赖的人
        # 必须仍能跑 RuleProposer 与整套评测 CLI。导入失败时给出确切的安装命令。
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - 取决于安装的附加依赖
            raise RuntimeError(
                "LLMProposer 需要 LangChain：pip install -e 'agent[llm]'（版本在 "
                "agent/pyproject.toml 的 llm 组里精确锁定）。本提案器不会退化成规则提案。"
            ) from exc
        self._HumanMessage = HumanMessage
        self._SystemMessage = SystemMessage

        resolved_model, model_defaulted = self._setting("AGENT_LLM_MODEL", self._DEFAULT_MODEL, model)
        resolved_base, base_defaulted = self._setting("AGENT_LLM_BASE_URL", self._DEFAULT_BASE_URL, base_url)

        #: 实际交给 ChatOpenAI 的模型名。刻意记这个值而不是 `os.getenv("AGENT_LLM_MODEL")`：
        #: 走默认档时后者是空串，于是产物会说"没有模型"，而真相是"跑了默认档的 qwen"。
        #: `loop._proposer_model()` 读的就是这个属性，它必须在默认档下也说真话。
        self.model = resolved_model
        self.base_url = resolved_base
        self.max_low = max_low
        self.max_zero = max_zero
        self.max_proposals = max_proposals

        if model_defaulted or base_defaulted:
            logger.warning(
                json.dumps(
                    {
                        "event": "agent.llm.defaults_applied",
                        "proposer": self.name,
                        "model": resolved_model,
                        "base_url": resolved_base,
                        "defaulted": [
                            n
                            for n, used in (
                                ("model", model_defaulted),
                                ("base_url", base_defaulted),
                            )
                            if used
                        ],
                    },
                    ensure_ascii=False,
                )
            )

        llm = ChatOpenAI(
            model=resolved_model,
            base_url=resolved_base,
            api_key=self._api_key(),
            temperature=self._number("AGENT_LLM_TEMPERATURE", 0.0, float) if temperature is None else temperature,
            # 提案比查询改写长得多：三条提案 × 若干条目 + 理由。这个值实测过：2048 时
            # qwen3.7-flash 有相当比例的调用打满预算，finish_reason 变成 "length"，
            # 而**被截断的 tool-call 参数在 LangChain 里既不抛异常也不填 parsing_error**，
            # 只是把 parsed 置为 None（见 propose() 里的截断自检）。默认给 4096。
            max_tokens=self._number("AGENT_LLM_MAX_TOKENS", 4096, int),
            # 秒制。提案是离线批处理，没有 ai-adapter 那边 5s 读超时的约束，
            # 但仍要有上界：挂住的调用会把整轮闭环卡死在第一步。
            timeout=self._number("AGENT_LLM_TIMEOUT_MS", 60_000, int) / 1000.0,
            # 默认 0：一次失败必须响亮且可归因。重试会把一次超时变成三次计费的超时，
            # 并且掩盖"端点参数配错了"这类必然复现的错误。要重试请由调用方在更大的
            # 时间预算里做，并显式设 AGENT_LLM_MAX_RETRIES。
            max_retries=self._number("AGENT_LLM_MAX_RETRIES", 0, int),
            extra_body=self._extra_body(model_defaulted and base_defaulted),
        )
        # include_raw=True 换来两样东西：usage（用于思考模式自检）与 parsing_error
        # （把"模型没按 schema 输出"和"网络炸了"分开）。
        self._chain = llm.with_structured_output(
            _ProposalBatch, method=self._method(), include_raw=True
        )

    # ------------------------------------------------------------------ 配置读取

    @staticmethod
    def _setting(env_name: str, default: str, explicit: str | None) -> tuple[str, bool]:
        """优先级：构造参数 > 环境变量 > 默认档。返回 (取值, 是否用上了默认档)。

        空串按未设置处理——`.env` 里这些变量常常是空值占位，空串与未设置必须等价，
        否则默认值永远轮不到生效。
        """
        if explicit and explicit.strip():
            return explicit.strip(), False
        value = os.getenv(env_name, "").strip()
        return (value, False) if value else (default, True)

    def _api_key(self) -> str:
        """按 `AGENT_LLM_API_KEY_ENV` 指定的**变量名**去环境里取 key。key 本身没有默认值。

        为什么多一层间接：不同厂商的变量名不同（DASHSCOPE_API_KEY / DEEPSEEK_API_KEY /
        KIMI_API_KEY / MINIMAX_API_KEY），写死一个名字会逼人复制凭据。这层间接让"key 存在
        哪个变量"成为配置，而 key 的值始终只经 `os.environ` 读取，不出现在配置项、日志、
        异常文本与产物里的任何地方。
        """
        key_var = os.getenv("AGENT_LLM_API_KEY_ENV", "").strip() or "DASHSCOPE_API_KEY"
        api_key = os.getenv(key_var, "").strip()
        if not api_key:
            raise RuntimeError(
                f"LLMProposer: 缺少 API key，拒绝构造。AGENT_LLM_API_KEY_ENV 当前指向环境变量 "
                f"{key_var!r}，但它未设置或为空（未设置 AGENT_LLM_API_KEY_ENV 时默认指向 "
                f"DASHSCOPE_API_KEY）。key 通常写在 ~/.zshrc 里，只有交互式 shell 会加载它，"
                f"因此请用 `zsh -ic '...'` 运行，或把 AGENT_LLM_API_KEY_ENV 改成实际持有 key "
                f"的变量名。本提案器不会退化成 RuleProposer——那会把'模型压根没跑'伪装成"
                f"'LLM 提案没有收益'，并写进对照结论。"
            )
        return api_key

    def _method(self) -> str:
        method = os.getenv("AGENT_LLM_STRUCTURED_OUTPUT_METHOD", "").strip() or "function_calling"
        if method not in self._METHODS:
            raise RuntimeError(
                f"LLMProposer: AGENT_LLM_STRUCTURED_OUTPUT_METHOD={method!r} 不合法，"
                f"只能是 {sorted(self._METHODS)} 之一。"
            )
        return method

    @staticmethod
    def _number(name: str, default: float, cast: type) -> Any:
        value = os.getenv(name, "").strip()
        if not value:
            return default
        try:
            return cast(value)
        except ValueError as exc:
            raise RuntimeError(f"LLMProposer: {name}={value!r} 不是数值。") from exc

    def _extra_body(self, profile_is_default: bool) -> dict[str, Any] | None:
        """把 `AGENT_LLM_EXTRA_BODY`（一段 JSON）透传到请求体顶层。

        典型取值（值里绝不含 key）::

            AGENT_LLM_EXTRA_BODY={"enable_thinking": false}     # DashScope qwen3.x
            AGENT_LLM_EXTRA_BODY={"reasoning_effort": "none"}   # DeepSeek v4
            AGENT_LLM_EXTRA_BODY={}                             # 显式声明"什么都不透传"
        """
        value = os.getenv("AGENT_LLM_EXTRA_BODY", "").strip()
        if not value:
            return dict(self._DEFAULT_EXTRA_BODY) if profile_is_default else None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LLMProposer: AGENT_LLM_EXTRA_BODY 不是合法 JSON（{exc.msg}）。") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("LLMProposer: AGENT_LLM_EXTRA_BODY 必须是一个 JSON 对象。")
        return parsed

    # ------------------------------------------------------------------ 输出处理

    @property
    def origin(self) -> str:
        """提案的来源标签，带上模型名。

        `loop.py` 的逐提案产物字段是 name/origin/rationale/evidence/config，没有 model；
        把模型名编进 origin，是让"这条提案是谁提的"跟着提案一起归档的唯一途径。
        """
        return f"{self.name}:{self.model}"

    @staticmethod
    def _unwrap(result: Any) -> tuple[Any, Any, Any]:
        """返回 (raw_message, parsed, parsing_error)，兼容信封与"直接返回结构体"两种形状。"""
        if isinstance(result, dict) and "parsed" in result:
            return result.get("raw"), result.get("parsed"), result.get("parsing_error")
        return result, result, None

    @staticmethod
    def _assert_not_truncated(raw: Any) -> None:
        """输出被 max_tokens 截断时立刻报错，而不是让它伪装成"模型没有提案"。

        实测（qwen3.7-flash-2026-07-15 + DashScope 兼容端点）：max_tokens 打满时
        `finish_reason` 是 `"length"`，而 `with_structured_output(include_raw=True)` 的信封里
        **`parsing_error` 仍是 None、`parsed` 直接是 None**——tool-call 的参数 JSON 被截断在
        半途，LangChain 既没有抛异常也没有登记解析错误。若不显式查这一项，症状就是提案器
        时不时返回空，看起来像"模型这轮没想法"，而真相是它的答案被砍掉了一半。这正是本项目
        反复强调的那类"不报错、只是悄悄变错"的缺陷，所以由机器来发现。
        """
        meta = getattr(raw, "response_metadata", None)
        if isinstance(meta, dict) and meta.get("finish_reason") == "length":
            used = (meta.get("token_usage") or {}).get("completion_tokens")
            raise RuntimeError(
                f"LLMProposer: 模型输出被 max_tokens 截断（finish_reason=length，"
                f"completion_tokens={used}），结构化输出不完整。请调大 AGENT_LLM_MAX_TOKENS。"
                "不返回部分提案，也不返回空提案——两者都会被误读成模型的判断。"
            )

    def _warn_if_thinking(self, raw: Any) -> None:
        """思考模式自检：看 usage 里的 reasoning token，而不是靠人眼盯延迟。"""
        usage = getattr(raw, "usage_metadata", None)
        if not isinstance(usage, dict):
            return
        reasoning = (usage.get("output_token_details") or {}).get("reasoning", 0)
        if reasoning:
            logger.warning(
                json.dumps(
                    {
                        "event": "agent.llm.thinking_enabled",
                        "proposer": self.name,
                        "model": self.model,
                        "reasoning_tokens": reasoning,
                        "hint": (
                            "模型仍在思考模式下运行，延迟会飙到 8-28s，且强制 tool_choice 会被"
                            'DashScope 拒绝。请用 AGENT_LLM_EXTRA_BODY={"enable_thinking": false} '
                            "关掉它。"
                        ),
                    },
                    ensure_ascii=False,
                )
            )

    # ------------------------------------------------------------------ Proposer 契约

    def propose(self, diagnosis: dict[str, Any], current: StrategyConfig) -> list[Proposal]:
        """调用模型提案。调用失败一律向上抛，绝不返回一个"看起来正常"的空列表。

        空列表在这里有确切且唯一的含义：**模型跑了，但它提的东西没有一条通过守卫**
        （或者它自己选择了弃权）。它绝不表示"模型没跑"——那种情况在构造期就已经抛异常了。
        """
        evidence = _Evidence.build(diagnosis, self.max_low, self.max_zero)
        if not evidence.by_id:
            raise ValueError(
                "本轮诊断没有任何可引用的查询证据（zero-result 与 low-quality 都是空）；"
                "拒绝在无证据的情况下向模型要提案。"
            )

        messages = [
            self._SystemMessage(content=SYSTEM_PROMPT),
            self._HumanMessage(
                content=USER_TEMPLATE.format(
                    current=current.model_dump_json(indent=2),
                    low=evidence.rendered_low,
                    zero=evidence.rendered_zero,
                    max_proposals=self.max_proposals,
                )
            ),
        ]

        try:
            result = self._chain.invoke(messages)
        except Exception as exc:
            # 只记异常类型。异常文本可能带上游 URL 或请求体片段，而日志是长期留存的。
            # 裸 raise 保留原始 traceback。
            logger.warning(
                json.dumps(
                    {"event": "agent.llm.call_failed", "proposer": self.name,
                     "model": self.model, "error_type": type(exc).__name__},
                    ensure_ascii=False,
                )
            )
            raise

        raw, parsed, parsing_error = self._unwrap(result)
        self._warn_if_thinking(raw)
        self._assert_not_truncated(raw)
        if parsing_error is not None:
            raise RuntimeError(
                f"LLMProposer: 模型输出不符合提案 schema（{type(parsing_error).__name__}）："
                f"{str(parsing_error)[:600]}。"
                "这是模型/端点问题，不是降级理由——不返回空提案，以免被读成'模型没有想法'。"
            )
        if parsed is None or not isinstance(parsed, _ProposalBatch):
            raise RuntimeError(f"LLMProposer: 结构化输出为空或类型异常（{type(parsed).__name__}）。")

        compiler = _Compiler(evidence, current)
        proposals: list[Proposal] = []
        for change in parsed.proposals[: self.max_proposals]:
            config, notes, cited = compiler.compile(change)
            if config is None:
                # 整条提案被守卫清空。丢弃但必须留痕，连同逐条原因：否则"模型提了三条、
                # 三条全是空转"会显示成"模型只提了零条"，两者对模型的评价完全不同。
                logger.warning(
                    json.dumps(
                        {"event": "agent.llm.proposal_discarded", "proposer": self.name,
                         "model": self.model, "name": change.name,
                         "synonyms": len(change.synonyms),
                         "rewrite_rules": len(change.rewrite_rules), "notes": notes},
                        ensure_ascii=False,
                    )
                )
                continue
            evidence_lines = [
                f"query_id={qid} {evidence.by_id[qid]!r}" for qid in cited
            ]
            proposals.append(
                Proposal(
                    name=change.name,
                    config=config,
                    rationale=change.rationale,
                    evidence=evidence_lines,
                    origin=self.origin,
                    model=self.model,
                    guard_notes=notes,
                )
            )
            if notes:
                logger.warning(
                    json.dumps(
                        {"event": "agent.llm.guard_notes", "proposer": self.name,
                         "model": self.model, "name": change.name, "notes": notes},
                        ensure_ascii=False,
                    )
                )
        return proposals


# --- 符号启发式提案者（LLM 臂的同空间对照组） ----------------------------------
#
# 为什么要有这个类, 一句话: 上一轮 `RuleProposer` 只输出 `field_weights`, 而 `LLMProposer`
# 的结构化输出 schema 里**根本没有** `field_weights`。两臂的动作空间不重叠, "LLM 输给规则"
# 因此是个假命题——两边根本没在比同一件事。本类只输出 `synonyms` 与 `rewrite_rules`, 与
# `_ProposedChange` 逐字段同构, 交给**同一个** `_Compiler`、**同一套**守卫, 再走同一个
# `ProposalLoop`。只有这样,"LLM 相对一个朴素基线到底多做了什么"才第一次成为可测的问题。
#
# 设计原则: 只做**显而易见的机械操作**。这个类存在的目的不是赢, 是给 LLM 臂立一条地板线。
# 上一轮 LLM 的全部产出是 30 条"剥前导标点"的整串改写
# (`experiments/propose-llm-20260816T1344.json`); 如果一个正则基线也能产出同样的东西,
# 那 LLM 在这条链路上的增量就是零, 而这正是需要被量出来的数字。


_ASCII_FOLD_EXTRA: dict[str, str] = {
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
}
"""`asciifolding` 里几个 NFKD 分解不掉、但本数据集真实出现的映射。

索引分析器实测是 ``standard`` tokenizer + ``lowercase`` + ``asciifolding``
(``GET /<index>/_settings`` 的 ``analysis.analyzer.searchops_text``)。NFKD 只能去掉组合
附加符号 (``café`` → ``cafe``), 而 ``’`` (U+2019) 这类标点要靠 Lucene 的映射表。本数据集里
``$60 ps4 that’s not …`` 就带 U+2019, 不映射的话"改写前后 token 是否相同"会误判。
"""


def _fold(text: str) -> str:
    """近似 Lucene `ASCIIFoldingFilter`: 先查显式映射表, 再 NFKD 去组合符号。"""
    mapped = "".join(_ASCII_FOLD_EXTRA.get(ch, ch) for ch in text)
    return "".join(ch for ch in unicodedata.normalize("NFKD", mapped) if not unicodedata.combining(ch))


def _analyzed_tokens(text: str) -> list[str]:
    """本地近似索引分析器的分词结果, **只用于判定"这条改写在 token 层面动了没有"**。

    为什么需要它: 引擎在分词阶段就把前导标点丢掉了, 所以"剥标点"这类改写很可能是恒等变换。
    `_Compiler` 已有的恒等守卫是**字符串**层面的 (`rewrite` 归一化后等于 `match`), 抓不到
    "字符串变了但 token 流没变"这一类。本函数把这类改写识别出来, 分流到"外观改写"提案里,
    与"真的改变了 token 流"的提案分开评测——两者混在一条提案里, 事后就分不清是谁的功劳。

    为什么不去问 Elasticsearch: 提案器必须是纯确定性的、可离线复现的, 一旦依赖服务端的
    `_analyze`, 同一份证据在服务不可用时就产出不同的提案, 第 4 条要求 (确定性) 直接失效。
    代价是这里只是**近似**, 所以它的结论只进 `rationale`/日志, 不参与任何丢弃决策。

    近似的具体口径 (照 UAX#29 的 word break, 已逐条对着本索引的 `_analyze` 核对过):
      * ``.`` 连接两侧的 alnum   —— ``3.5`` → 一个 token
      * ``,`` 连接两侧的数字     —— ``1,000`` → 一个 token, 而 ``a,b`` 分开
      * ``'`` 连接两侧的字母     —— ``we're`` → 一个 token, 而 ``90's`` → ``90`` + ``s``
      * ``_`` 属于词内字符       —— ``c_d`` → 一个 token
      * 其余非 alnum 一律是分隔符 —— ``&#34;`` → ``34``, ``.22`` → ``22``, ``9x5"`` → ``9x5``
    """
    # 先折叠再小写: NFKD 分解个别字符会产出大写 (``Ⅻ`` → ``XII``), 反过来做会漏掉它们。
    s = _fold(str(text)).lower()
    tokens: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if not (s[i].isalnum() or s[i] == "_"):
            i += 1
            continue
        start = i
        while i < n:
            ch = s[i]
            if ch.isalnum() or ch == "_":
                i += 1
                continue
            # i > start 恒成立(起点必是 alnum), 所以 s[i-1] 一定存在。
            prev, nxt = s[i - 1], (s[i + 1] if i + 1 < n else "")
            if ch == "." and prev.isalnum() and nxt.isalnum():
                i += 2
                continue
            if ch == "," and prev.isdigit() and nxt.isdigit():
                i += 2
                continue
            if ch == "'" and prev.isalpha() and nxt.isalpha():
                i += 2
                continue
            break
        tokens.append(s[start:i])
    return tokens


_EDGE_PUNCT = re.compile(r"^[^0-9a-z]+|[^0-9a-z]+$")


def _strip_edge_punct(query: str) -> str:
    """逐个空白分片剥掉首尾的非字母数字字符, 空片丢弃。**这就是"朴素正则基线"本身。**

    依据: 上一轮 LLM 的 30 条改写里, 有 24 条的实质内容恰好是这一个操作
    (``'-headphones without mic'`` → ``'headphones without mic'`` 等)。要回答"LLM 比正则多做了
    什么", 就必须先有这个正则。

    刻意只剥**首尾**而不剥词内标点: 词内的 ``.``/``,``/``'`` 在 UAX#29 下是连接符
    (``3.5``/``1,000``/``we're`` 都是单 token), 一律替换成空格反而会把一个 token 拆成两个,
    那是引入改动而不是复制引擎已有的行为——朴素基线不该比引擎更激进。

    顺带做了 asciifolding (``that’s`` → ``that's``): 索引侧本来就折, 所以这对 token 流是恒等的,
    但能让产出的 rewrite 串是纯 ASCII, 人读产物时不会把 U+2019 误认成 U+0027。
    """
    parts = [_EDGE_PUNCT.sub("", chunk) for chunk in _fold(str(query)).lower().split()]
    return " ".join(p for p in parts if p)


#: 符号 → 零售词的展开表。**顺序固定**(元组而非 dict 推导), 确定性的一部分。
#: 每条都附了"为什么这是机械操作"以及"防护条件"。
_SYMBOL_REWRITES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # ``$5`` → ``5 dollar``。价格符号在英文零售语境里只有一个读法; 商品标题里写的是
    # "dollar"/"dollars" 而不是 ``$``, 而分析器会把 ``$`` 整个丢掉, 于是这条查询里的价格
    # 信息在 token 层面凭空消失。允许 ``$ 5`` 中间有一个空格。
    ("money", re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)"), r"\1 dollar"),
    # ``5"`` → ``5 inch``。防护: 双引号前必须紧跟数字, 否则它是引号不是英寸符
    # (证据里的 ``&#34;we're in the army…&#34;`` 正是引号, 前面是空白, 不会命中)。
    ("inch", re.compile(r"(?<=[0-9])\s*\""), " inch"),
    # ``6'`` → ``6 foot``。防护双侧: 前必须是数字(排除 ``don't``), 后不能是字母
    # (排除 ``90's``)。本轮证据里一次都没触发, 保留是因为它与 inch 同源同形。
    ("foot", re.compile(r"(?<=[0-9])'(?![a-z])"), " foot"),
    ("percent", re.compile(r"(?<=[0-9])\s*%"), " percent"),
    # 独立成词的 ``&`` → ``and``。HTML 实体解码在它之前跑, 所以 ``&#34;`` 里的 ``&`` 到这里
    # 已经不存在了, 不会被误当成连词。
    ("ampersand", re.compile(r"(?<=\s)&(?=\s)"), "and"),
)

_ENTITY = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")


def _decode_entities(query: str) -> str:
    """把 HTML 实体解回字符。``html.unescape`` 是标准库, 纯函数, 确定性。

    这是本文件里唯一一个**能证明**自己不是恒等变换的机械操作: ``&#34;`` 在分析器下产出一个
    垃圾 token ``34`` (实测 ``&#34;we're …&#34;`` → ``['34', "we're", …, '34']``), 解码后这两个
    ``34`` 消失。剥标点做不到这件事——``&#34;we're`` 剥掉首部的 ``&#`` 之后剩 ``34;we're``,
    token 流一字不变。
    """
    return html.unescape(query) if _ENTITY.search(query) else query


_MODEL_YEAR = re.compile(r"^(0\d|1\d|2\d)\s+[a-z]")
"""查询以两位数字 + 空格 + 字母开头 → 在汽配语境里几乎必然是车型年份。

依据: 本轮 train 侧证据里三条 NDCG@10 = 0 的查询全是这个形状
(``03 durango front calipers…`` / ``04 nissan titan…`` / ``07 passat…``), 而它们**没有任何
标点可剥**——剥标点的启发式对它们完全无能为力, 只有同义词能碰到它们。
"""

#: 频次挖掘时要跳过的功能词。不含具体品类词, 免得把真正的信号也滤掉。
_FUNCTION_WORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "for", "with", "that", "this", "are", "is", "was",
        "on", "in", "at", "of", "to", "it", "its", "be", "but", "have", "has", "do", "does",
        "can", "will", "would", "should", "what", "which", "who", "when", "where", "how",
        "my", "me", "you", "your", "they", "them", "there", "here", "one", "two", "pack",
    }
)


@dataclass(frozen=True)
class _Family:
    """一族启发式的产出。一族 = 一条提案, 因为门禁是逐提案判的。

    为什么不把三族合成一条提案: `ProposalLoop` 对每条提案跑一次完整评测, 合成一条就只能得到
    "这一堆东西合起来有没有用"。而本轮要回答的问题恰恰是分族的——"剥标点"预期是恒等变换,
    "符号展开"预期能动 token 流, 两者混在一起, 测出 Δ=0 时无法分辨是两族都没用, 还是一族有用
    一族有害正好抵消。
    """

    key: str
    name: str
    rationale: str
    synonyms: list[_SynonymEntry] = field(default_factory=list)
    rewrite_rules: list[_RewriteRuleEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """本族在**送进 `_Compiler` 之前**就自我否决的条目。与 `_Compiler` 的守卫记录合并进
    `Proposal.guard_notes`, 但加了前缀以示区分: 一个是启发式自己的取舍, 一个是共用守卫的裁决。
    """


class SymbolicRuleProposer:
    """确定性的符号提案器 —— 与 `LLMProposer` **同空间**的对照臂。

    与 `LLMProposer` 的关系, 逐条对齐 (对照成立的全部前提):

    ======================  ==========================================================
    证据                    同一个 `_Evidence.build(diagnosis, max_low, max_zero)`,
                            默认窗口 (40 / 20) 也相同
    输出形状                同一组 `_SynonymEntry` / `_RewriteRuleEntry` / `_ProposedChange`
    守卫                    同一个 `_Compiler.compile()` —— 引用校验、子串/整串可行性、
                            否定守卫、契约上下界, 一条不多一条不少
    条数上限                同一个 `MAX_PROPOSALS`
    无证据时                同样抛 `ValueError`, 不返回空列表
    闭环                    同一个 `ProposalLoop.assess()`, 同一份基线, 同一批评测查询
    ======================  ==========================================================

    唯一的差别只有"提案从哪来"。任何别的差别都会让对照失效, 所以本类**不**新增守卫、
    **不**放宽守卫、**不**碰 `field_weights` / `brand_boosts` / `minimum_score`。

    三族启发式, 以及**故意不做**的事
    --------------------------------

    **(1) `punctuation` —— 首尾标点规范化。** 上一轮 LLM 提案的全部内容就是这个 (30 条改写里
    24 条的实质操作), 所以它必须在基线里。预期结论: **恒等**。分析器在分词阶段已经把首尾标点
    丢了 (实测 ``'# 2 pencils not sharpened'`` 与 ``'2 pencils not sharpened'`` 分词结果逐位相同),
    所以这一族在 token 层面很可能什么都没改。把它单列成一条提案, 就是为了让这个"什么都没改"
    被评测直接量出来, 而不是靠嘴说。

    **(2) `symbol` —— 符号与 HTML 实体展开。** 只收 `_analyzed_tokens` 判定为**真的改变了
    token 流**的改写: ``$5`` → ``5 dollar``、``5"`` → ``5 inch``、``&#34;`` 解码掉两个垃圾
    token ``34``。这是"机械操作里唯一可能有效的那一半", 与 (1) 分开评测。

    **(3) `synonym` —— 符号同义词与车型年份。** 只有这一族能泛化到证据之外: `expandSynonyms`
    是**子串包含**触发, 所以 term ``"`` 会命中 train 里全部 4 条带英寸符的查询, 而证据里只有
    1 条。这是本轮唯一可能撬动均值的杠杆 (改写是整串相等, 一条规则只影响一条查询)。
    子串误伤的防护写在每条候选旁边。

    **故意不做的事 (每条都有依据):**

    * **不做拼写纠错。** ``multi_match`` 开了 ``fuzziness: AUTO``: 长度 > 5 的 token 容许 2 次
      编辑。上一轮 LLM 把 ``!awnmower`` 改成 ``lawnmower`` (编辑距离 1, 长度 8) ——
      引擎本来就能匹配上, 这条改写的边际收益是零。同理 ``lacies`` → ``laces``。
      把它留给 LLM 臂: 如果 LLM 的增量全落在这里, 那增量就是零, 而这是个结论不是遗漏。
    * **不插入语义名词。** ``.22`` → ``.22 caliber``、``#15`` → ``size 15``、
      ``$1 million`` → ``1 million dollar bills`` 都需要品类知识, 正则给不出。
      **这正是 LLM 臂可能的真实增量, 所以必须留空**——基线把它做了, 对照就白做了。
    * **不做 ``#`` → ``number``/``size``。** ``#`` 在本数据集里至少有三种读法 (``# 2 pencils``
      的份量号、``#9x5`` 的螺丝规格、``#15 charm`` 的尺码), 机械展开必然选错一种; 而分析器
      本来就把 ``#`` 丢掉, 展开只会凭空加一个标题里多半不存在的 token。
    * **不做频次挖掘出来的同义词。** train 侧证据只有个位数条查询, 其中出现次数 ≥2 的
      token 是 ``not`` / ``without`` / ``the`` 这类功能词, 而给否定词做同义词展开正是
      `NEGATION_CUES` 守卫存在的理由。挖掘照跑, 结果写进 `guard_notes` 存证, 但不产出条目 ——
      "机械地弃权"也是一个需要被记录的动作。
    * **不删词、不缩短查询。** ``operator=or`` 下删掉购物者打出来的词只会丢信号,
      而且"哪个词该删"没有机械判据。
    """

    name = "symbolic"
    origin = "symbolic-rule"

    #: 与 `LLMProposer.__init__` 的默认值逐位相同 —— 两臂看到的证据窗口必须一样宽。
    _DEFAULT_MAX_LOW: ClassVar[int] = 40
    _DEFAULT_MAX_ZERO: ClassVar[int] = 20

    #: 单字符符号同义词。子串触发, 所以只收"在英文零售查询里读法唯一"的符号。
    #: 每一项: (符号, 展开项, 防护说明)。故意做成有序元组而不是 dict, 确定性的一部分。
    _SYMBOL_SYNONYMS: ClassVar[tuple[tuple[str, str, str], ...]] = (
        ('"', "inch", "双引号在商品查询里几乎只作英寸符; 只在证据里它紧跟数字时才提"),
        ("$", "dollar", "价格符号读法唯一"),
        ("%", "percent", "百分号读法唯一"),
    )

    def __init__(
        self,
        *,
        max_low: int = _DEFAULT_MAX_LOW,
        max_zero: int = _DEFAULT_MAX_ZERO,
        max_proposals: int = MAX_PROPOSALS,
        min_support: int = 2,
    ) -> None:
        self.max_low = max_low
        self.max_zero = max_zero
        self.max_proposals = max_proposals
        self.min_support = min_support
        # 刻意**不**定义 self.model / model_name / llm_model:
        # `loop._proposer_model()` 按这三个属性名取模型名, 对照报告里"模型"那一栏必须为空,
        # 否则一个没有调用任何模型的臂会在产物里显示成有模型。

    # ------------------------------------------------------------------ 三族启发式

    def _punctuation_family(self, ordered: list[tuple[int, str]]) -> _Family:
        """首尾标点规范化 —— 朴素正则基线, 预期恒等。"""
        entries: list[_RewriteRuleEntry] = []
        notes: list[str] = []
        for qid, query in ordered:
            cleaned = _strip_edge_punct(query)
            if not cleaned:
                notes.append(f"[启发式] 跳过 query_id={qid}: 剥掉首尾标点后不剩任何字符")
                continue
            # 与 match 逐字相同的改写交给 `_Compiler` 的恒等守卫去判, 不在这里预先滤掉 ——
            # 守卫记录本身就是产物的一部分("这条查询根本没有标点可剥"是一条结论)。
            entries.append(
                _RewriteRuleEntry(match=query, rewrite=cleaned, evidence_query_ids=[qid])
            )
        same, diff = self._token_split(entries)
        if diff:
            notes.append(
                "[启发式] 本族有 "
                f"{len(diff)} 条改写在本地近似分词下改变了 token 流 ({', '.join(diff)}); "
                "按设计它们本该只出现在 symbol 族, 请复核 _strip_edge_punct"
            )
        return _Family(
            key="punctuation",
            name="Strip leading and trailing punctuation from evidence queries",
            rationale=(
                "A whole-query rewrite that removes only the punctuation at the edges of each "
                "token. This is the naive regular-expression baseline and it is deliberately "
                f"nothing more. {same} of the {len(entries)} candidate rewrites leave the "
                "analysed token stream unchanged (the remainder are dropped by the shared "
                "compiler guard as string-level identities), so this proposal is expected to "
                "move NDCG@10 by exactly zero; measuring that zero is the point."
            ),
            rewrite_rules=entries,
            notes=notes,
        )

    def _symbol_family(self, ordered: list[tuple[int, str]]) -> _Family:
        """符号 / HTML 实体展开 —— 只收真的改变了 token 流的改写。"""
        entries: list[_RewriteRuleEntry] = []
        notes: list[str] = []
        fired: list[str] = []
        for qid, query in ordered:
            expanded = _decode_entities(query)
            hits: list[str] = ["entity"] if expanded != query else []
            for label, pattern, repl in _SYMBOL_REWRITES:
                expanded, count = pattern.subn(repl, expanded)
                if count:
                    hits.append(label)
            if not hits:
                continue  # 这条查询里没有任何可展开的符号, 本族对它无话可说
            rewrite = _strip_edge_punct(expanded)
            if not rewrite:
                continue
            if _analyzed_tokens(rewrite) == _analyzed_tokens(query):
                # token 流没变 = 对引擎而言是恒等操作。留在 punctuation 族即可,
                # 放进本族会污染"本族只含有效改动"这个前提。
                notes.append(
                    f"[启发式] 跳过 query_id={qid}: 展开后 token 流与原查询相同, "
                    "属于外观改动, 已归入 punctuation 族"
                )
                continue
            entries.append(
                _RewriteRuleEntry(match=query, rewrite=rewrite, evidence_query_ids=[qid])
            )
            fired.append(f"query_id={qid}({'+'.join(hits)})")
        return _Family(
            key="symbol",
            name="Expand retail symbols and decode HTML entities",
            rationale=(
                "Rewrites that change what the analyser actually indexes: a currency or unit "
                "symbol becomes the retail word the catalogue spells out, and an HTML entity "
                "is decoded so that its digits stop entering the index as a junk token. Only "
                "rewrites whose analysed token stream differs from the original are kept "
                f"({len(entries)} of them: {', '.join(fired) or 'none'}), which is what "
                "separates this proposal from the punctuation one."
            ),
            rewrite_rules=entries,
            notes=notes,
        )

    def _synonym_family(self, ordered: list[tuple[int, str]]) -> _Family:
        """符号同义词 + 车型年份 —— 唯一能泛化到证据之外的一族。"""
        entries: list[_SynonymEntry] = []
        notes: list[str] = []

        # (a) 单字符符号同义词。`expandSynonyms` 是子串包含触发, 所以这些 term 会命中
        #     **整个评测集**里含该符号的查询, 而不只是证据里的那几条 —— 这正是要的泛化。
        for symbol, expansion, guard in self._SYMBOL_SYNONYMS:
            cited = [qid for qid, q in ordered if symbol in q]
            if not cited:
                # 不在证据里出现的 term, `_Compiler` 也会拦 (子串触发条件不成立)。
                # 这里先记一笔, 让"为什么没提 $ → dollar"在产物里说得清。
                notes.append(f"[启发式] 未提同义词 {symbol!r} → {expansion!r}: 证据里没有这个符号")
                continue
            if symbol == '"':
                # 防护: 双引号既是英寸符也是引号。只有当证据里它**紧跟数字**时才敢展开成 inch。
                numeric = [qid for qid, q in ordered if re.search(r'[0-9]\s*"', q)]
                if not numeric:
                    notes.append(
                        '[启发式] 未提同义词 \'"\' → \'inch\': 证据里的双引号都不紧跟数字, '
                        "更像引号而非英寸符"
                    )
                    continue
                cited = numeric
            entries.append(
                _SynonymEntry(term=symbol, expansions=[expansion], evidence_query_ids=cited)
            )
            notes.append(f"[启发式] 同义词 {symbol!r} → {expansion!r} 的防护依据: {guard}")

        # (b) 车型年份。两位数字开头 + 空格 + 字母 → 展开成四位年份。
        #     **子串误伤必须说清楚**: `_normalize_query` 会把 term 首尾空白折掉, 所以 term 只能是
        #     裸的两位数字 ``03``, 它会在任何含 "03" 的查询上触发 (实测 train 1400 条里
        #     ``03`` 命中 2 条: 目标查询, 外加 ``.030 lincoln flux core`` 这条误伤)。
        #     接受这个代价的理由: 展开项只是**追加**一个 token, 原查询一字不动, 所以误伤的
        #     上限是"多一个 OR 分支", 而不是语义反转; 而三条 NDCG@10 = 0 的汽配查询除此之外
        #     没有任何机械抓手可用。
        seen_years: list[str] = []
        for qid, query in ordered:
            if not _MODEL_YEAR.match(query):
                continue
            two = query[:2]
            if two in seen_years:
                continue
            seen_years.append(two)
            entries.append(
                _SynonymEntry(
                    term=two,
                    # `_MODEL_YEAR` 只匹配 00–29 开头, 所以世纪前缀恒为 "20"。
                    expansions=["20" + two],
                    evidence_query_ids=[q for q, s in ordered if s[:2] == two and _MODEL_YEAR.match(s)],
                )
            )
        if seen_years:
            notes.append(
                f"[启发式] 车型年份同义词 {seen_years}: term 只能是裸两位数字 "
                "(_normalize_query 折掉首尾空白), 会在任何含该数字串的查询上子串触发; "
                "展开项是追加而非替换, 误伤上限为多一个 OR 分支"
            )

        # (c) 频次挖掘 —— 跑, 但机械地弃权。见类文档"故意不做的事"。
        counts: dict[str, int] = {}
        for _, query in ordered:
            for tok in sorted(set(_analyzed_tokens(query))):
                if len(tok) > 2 and tok not in _FUNCTION_WORDS and tok not in NEGATION_CUES:
                    counts[tok] = counts.get(tok, 0) + 1
        recurring = sorted(t for t, c in counts.items() if c >= self.min_support)
        notes.append(
            f"[启发式] 频次挖掘: 证据里出现 ≥{self.min_support} 次的实词 {recurring or '无'}; "
            "一律不产出同义词 —— 展开项需要品类知识, 正则给不出, 而给否定词/功能词做展开正是"
            "否定守卫要拦的东西。这一条是刻意的弃权, 不是遗漏。"
        )

        return _Family(
            key="synonym",
            name="Symbol and model-year synonyms",
            rationale=(
                "Synonym entries fire on SUBSTRING containment, so unlike a rewrite rule they "
                "generalise past the evidence: a term of '\"' reaches every query in the "
                "evaluation set that carries an inch mark, not only the one cited here. These "
                "entries append a token the shopper did not type — the retail word for a unit "
                "symbol, or the four-digit form of a two-digit model year — and never remove "
                "anything, so a misfire costs one extra OR branch and cannot invert intent."
            ),
            synonyms=entries,
            notes=notes,
        )

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _token_split(entries: list[_RewriteRuleEntry]) -> tuple[int, list[str]]:
        """返回 (token 流未变的条数, token 流变了的条目标签)。只用于自检与文案。"""
        same, diff = 0, []
        for e in entries:
            if _analyzed_tokens(e.rewrite) == _analyzed_tokens(e.match):
                same += 1
            else:
                diff.append(f"query_id={e.evidence_query_ids[0] if e.evidence_query_ids else '?'}")
        return same, diff

    # ------------------------------------------------------------------ Proposer 契约

    def propose(self, diagnosis: dict[str, Any], current: StrategyConfig) -> list[Proposal]:
        """按证据机械地产出提案。确定性: 同一份 `diagnosis` 两次调用产出逐位相同的结果。

        确定性从哪来: (a) 证据按 `query_id` 排序后再遍历, 不依赖诊断列表的到达顺序;
        (b) 符号表、同义词表都是有序元组; (c) 频次挖掘的结果 `sorted` 之后才用;
        (d) 全程不取时间、不取随机数、不调用任何网络服务(包括不问 ES 要分词结果)。

        空列表的含义与 `LLMProposer` 一致: **跑了, 但没有一条通过守卫**。"没跑"在无证据时
        会抛 `ValueError` —— 两臂在这一点上的行为必须一样, 否则"某臂返回空"在两边含义不同。
        """
        evidence = _Evidence.build(diagnosis, self.max_low, self.max_zero)
        if not evidence.by_id:
            raise ValueError(
                "本轮诊断没有任何可引用的查询证据（zero-result 与 low-quality 都是空）；"
                "拒绝在无证据的情况下产出提案。"
            )

        # 按 query_id 排序 —— 确定性的第一道保证。
        ordered = sorted(evidence.by_id.items())

        families = [
            self._punctuation_family(ordered),
            self._symbol_family(ordered),
            self._synonym_family(ordered),
        ][: self.max_proposals]

        compiler = _Compiler(evidence, current)
        proposals: list[Proposal] = []
        for family in families:
            change = _ProposedChange(
                name=family.name,
                rationale=family.rationale,
                synonyms=family.synonyms,
                rewrite_rules=family.rewrite_rules,
            )
            config, guard_notes, cited = compiler.compile(change)
            notes = family.notes + guard_notes
            if config is None:
                logger.warning(
                    json.dumps(
                        {"event": "agent.symbolic.proposal_discarded", "proposer": self.name,
                         "family": family.key, "name": family.name,
                         "synonyms": len(family.synonyms),
                         "rewrite_rules": len(family.rewrite_rules), "notes": notes},
                        ensure_ascii=False,
                    )
                )
                continue
            proposals.append(
                Proposal(
                    name=family.name,
                    config=config,
                    rationale=family.rationale,
                    evidence=[f"query_id={qid} {evidence.by_id[qid]!r}" for qid in cited],
                    # 族名编进 origin: `loop._outcome_payload` 归档 origin 而不归档族,
                    # 不带上就没法在产物里分辨"这条 Δ 是哪族启发式做出来的"。
                    origin=f"{self.origin}:{family.key}",
                    model=None,  # 没有模型。对照报告里这一栏必须与 LLM 臂可区分。
                    guard_notes=notes,
                )
            )
            if guard_notes:
                logger.warning(
                    json.dumps(
                        {"event": "agent.symbolic.guard_notes", "proposer": self.name,
                         "family": family.key, "notes": guard_notes},
                        ensure_ascii=False,
                    )
                )
        return proposals


__all__ = [
    "Proposal",
    "Proposer",
    "RuleProposer",
    "LLMProposer",
    "SymbolicRuleProposer",
]
