"""通过 LangChain 调用真实 LLM 做候选重排; rewrite / suggest 逐字委托 MockProvider。

本模块的定位与 ``langchain_rewrite`` 平行, 但瞄准的失败模式完全不同, 所以先把"为什么值得做"写在
最前面 —— 做了什么读代码就知道, 为什么这么做只有写的时候知道。

为什么是重排, 而不是继续调 BM25
------------------------------
留出集诊断给出的分布是明确的: 在 NDCG@10 与 Recall@10 同时为 0 的查询里, 相关商品排在 11-50 位
的占 30.3%, 51-200 位的占 27.3%, 200 位之外的占 42.4%。也就是说 **57.6% 的彻底失败, 商品其实已经
被召回了, 只是排在第 10 名以后** —— 这部分靠改召回参数永远修不掉, 只能靠重排。另一侧的证据是
218 组 BM25 配置扫描在 holdout 上最多只有 +0.0035 (p=0.1202, 不显著), 头部空间已经用尽。两条证据
合起来才构成"重排值得做"的论证: 一是有靶子, 二是没有更便宜的替代品。

重排的风险与收益是不对称的, 这直接决定了下面所有的保守选择
--------------------------------------------------------
标注极稀疏: top-10 中只有约 12% 的位置有人工标注, 每查询平均 1.68 条 E/S 标注, 未标注文档一律记
gain 0。重排不改变候选集合, 所以它 **不可能改善召回**, 也不可能让一个没被召回的商品出现; 它能做
的只有换位置。于是:

* 把一个未标注商品换到前面 = 0 分换 0 分, 指标上是中性的 (大多数动作属于这一类);
* 把一个已标注 E/S 商品挤出 top-10 = 净亏损, 而且亏得很干脆。

结论是: 重排的期望收益来自"把已标注的好商品从 11+ 位拉进来", 风险几乎全部集中在"别把已经在
top-10 里的好商品挤出去"。所以本模块处处偏向"不动": 模型只被要求给出它认为相关的少数几个, 剩下
的位置原样保留 BM25 顺序 (见 ``AI_RERANK_TOP_K``); 模型分不出高下时被明确要求保留原序; 校验层在
任何异常情况下都退回原序而不是丢弃候选。

为什么模型输出的是"候选编号"而不是 product_id
--------------------------------------------
这是本模块最关键的一个协议选择, 有三个独立的理由, 每个都足以单独成立:

1. **输出 token 直接等于延迟。** ASIN 形如 ``B07XYZ1234``, 分词后约 5-7 个 token; 编号 ``[37]``
   约 1-2 个。N=50 的全排列用 id 表达要 300+ 输出 token, 用编号只要 100 出头。输出 token 是自回归
   生成的, 是延迟的主要来源 —— 输入侧再长也只是一次 prefill。
2. **集外检测从"字符串比对"变成"区间检查"。** 模型幻觉出一个不存在的 ASIN 时, 我们只知道"这个
   id 不在候选集里", 无法判断它是幻觉还是候选集本身有问题; 而 ``52`` 在 N=50 时一望即知越界。
3. **它是一道结构性的注入防线。** 商品标题来自外部目录, 是不可信数据; 一条标题完全可能写着
   "ignore previous instructions"。由于本协议下模型唯一能产出的东西就是一串编号, 而编号又被校验
   成 1..N 的排列, 一次成功的注入所能造成的最坏后果 **就是一个不同的排序** —— 它拿不到别的动作
   面。这不是靠提示词求来的, 是协议形状本身给的。

代价必须说清楚: 编号让模型多做一层"编号↔商品"的对应, 理论上比直接抄 id 更容易错位。实测缓解手段
是把编号放在每行行首 (``[12] Title | Brand``), 这是模型见过最多的列表格式。

为什么只让模型排 top-K, 而不是排完整个候选集
------------------------------------------
评测口径是 NDCG@10 / Recall@10 / P@10, 只有前 10 个位置进入指标。让模型对第 40 名和第 41 名谁更好
表态, 既花钱又没有任何指标影响, 而且每多一个输出 token 就多一分把整轮评测拖进超时的风险。所以默认
``AI_RERANK_TOP_K=10``: 模型给出它认为最相关的若干个 (**允许少于 10 个**), 其余位置按原 BM25 顺序
补齐。这同时带来一个很好的退化性质 —— 模型答得越短, 结果就越接近纯 BM25, 而不是越接近随机。

为什么提示词里不给 bm25_score
----------------------------
候选是按 BM25 降序给的, 顺序本身已经完整编码了这个信息; 再附一个裸浮点数只会让模型去锚定一个它
无法解释的数值, 还要为每条候选多花几个 token。顺序作为先验的用法被写进提示词第 6 条: 只用来给
"分不出高下"的候选打破平局。

为什么 description 默认不进提示词
--------------------------------
``Candidate.description`` 契约上限 5000 字符, N=50 就是 25 万字符。而 ESCI 的 Exact 判定几乎完全由
标题承载 (品类词 / 品牌 / 型号 / 数量 / 尺寸都在标题里), description 多是重复的营销文案。默认
``AI_RERANK_DESCRIPTION_CHARS=0`` 即完全不发; 需要做对照实验时可以调大, 但要预期延迟按倍数涨。

为什么缺 key 就在构造函数里抛异常 / 为什么 max_retries 默认 0 / 为什么必须能注入 extra_body
-----------------------------------------------------------------------------------------
这三条与 ``langchain_rewrite`` 完全同源, 不在这里重复论证, 只记差异:

* ``AI_EXTRA_BODY``: DashScope flash 系必须在 **请求体顶层** 带 ``enable_thinking=false``, 嵌套一层
  会被静默忽略。重排的提示词比改写长一个数量级, 思考模式下的延迟惩罚也随之放大, 所以这里比改写
  侧更要命。同样保留了基于 usage 的 reasoning-token 自检, 不指望人眼盯延迟。
* ``AI_MAX_TOKENS``: 改写侧默认 256 是够的, 重排不是 —— 输出长度随 top_k 线性增长。所以本模块的
  默认值按 top_k 推导 (见 ``_default_max_tokens``), 而不是照抄一个常数。max_tokens 是上限不是消耗,
  设大不花钱, 设小会把排序截断成半截。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, ClassVar

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ..models import (
    Candidate,
    ProposedChange,
    QueryRewriteRequest,
    RerankRequest,
    RerankScore,
    StrategySuggestRequest,
)
from ..provider import MockProvider, Provider

logger = logging.getLogger("ai-adapter")


class RankedOrder(BaseModel):
    """``with_structured_output`` 的目标 schema。字段描述会进入 tool schema, 模型看得到。

    字段顺序是有意的: ``order`` 在前, ``explanation`` 在后。tool-calling 下模型按 schema 声明顺序
    自回归生成, 把解释放在前面等于在真正有用的内容之前先付一笔延迟。
    """

    order: list[int] = Field(
        description=(
            "The candidate numbers of the relevant products, most relevant first. Use only "
            "numbers shown in the candidate list, never repeat a number, and never invent one. "
            "Return only the candidates that are actually relevant to the query: a short list "
            "is a valid and often correct answer."
        )
    )
    explanation: str = Field(
        description=(
            "At most 12 English words naming why the first candidate wins, or stating that the "
            "given order was already right."
        )
    )


class LangChainRerankProvider(Provider):
    """通过 LangChain 调用真实 LLM 重排候选; rewrite / suggest 逐字委托 MockProvider。"""

    name = "langchain-rerank"

    # ---- 默认档 ------------------------------------------------------------
    # 与 langchain_rewrite 保持同一套默认档, 这样两个 provider 可以共用一份 .env 配置, 换 provider
    # 只改 AI_PROVIDER 一处。
    _DEFAULT_MODEL: ClassVar[str] = "qwen3.7-flash-2026-07-15"
    _DEFAULT_BASE_URL: ClassVar[str] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    _DEFAULT_EXTRA_BODY: ClassVar[dict[str, Any]] = {"enable_thinking": False}

    #: 单次调用的时间预算 (毫秒)。为什么是 6000 而不是改写侧的 4000: 实测 N=50 / top_k=10 的单次
    #: 往返 p50 约 2.4s, 尾部到 3.5s; 4000ms 会把正常的尾部请求切掉。这个值 **必然大于** 目前
    #: .env 里的 AI_TIMEOUT_MS=5000, 也就是说 Java 侧接重排链路时必须为重排单独放宽读超时, 否则
    #: Java 会先放弃而适配器还在等。把这个矛盾留在代码里显式写着, 好过留一个"看起来自洽"的 4000。
    _DEFAULT_REQUEST_TIMEOUT_MS: ClassVar[int] = 6000

    #: 只有前 K 个位置进入 NDCG@10 / Recall@10 / P@10, 所以默认只让模型对前 K 个表态。
    _DEFAULT_TOP_K: ClassVar[int] = 10

    #: 进提示词的标题字符上限。ESCI 标题是关键词堆砌的长句, 但品类词 / 品牌 / 型号 / 数量几乎总在
    #: 前段; 120 字符约 30 token, N=50 时输入侧约 2k token, 是延迟可接受的一档。
    _DEFAULT_TITLE_CHARS: ClassVar[int] = 120
    _DEFAULT_BRAND_CHARS: ClassVar[int] = 40
    _DEFAULT_DESCRIPTION_CHARS: ClassVar[int] = 0

    _METHODS: ClassVar[frozenset[str]] = frozenset({"function_calling", "json_mode", "json_schema"})

    # ---- 否定语义 ----------------------------------------------------------
    # 下面这一组常量服务于同一件事: 从查询里抽出"被排除的东西", 既用来给提示词加一条针对性提醒,
    # 也用来在模型输出上做一次守卫。两者共用同一份抽取结果, 所以提示词说的和代码守的永远是同一个词。
    #
    # 词表是被 ESCI 真实查询裁剪过的, 每一条排除项都对应一个具体的坑:
    #
    # * ``free`` 只在 ``free of X`` 与 ``X-free`` 两种形态下算否定。裸的 ``free`` 在电商查询里绝大
    #   多数是 "free shipping" / "free returns", 把 "cookies free shipping" 抽成"排除 cookies"会
    #   反向摧毁这条查询。空格形式的 "sugar free cookies" 因此 **不受代码守卫保护** —— 只由提示词
    #   兜着。这是明知故犯的取舍: 宁可漏保护, 不可误伤。
    # * ``no`` 后面跟数字或写成 ``no.`` 时是 "number" 的缩写 (``no. 2 pencils``), 不是否定。这与
    #   langchain_rewrite 里 ``#2 pencils`` 必须保留的是同一个坑的两种写法。
    _EXCLUSION_CUES: ClassVar[frozenset[str]] = frozenset(
        {"without", "not", "no", "non", "excluding", "except", "minus", "sans", "free"}
    )

    #: 紧跟在否定词后面、本身不承载语义、需要再往后看一个 token 的功能词。
    _SKIP_AFTER_CUE: ClassVar[frozenset[str]] = frozenset({"a", "an", "the", "any", "of"})

    #: 抽出来的排除词必须避开的高频词。命中这些说明我们没抽到真正的名词, 应当放弃守卫而不是硬上。
    _WEAK_TERMS: ClassVar[frozenset[str]] = frozenset(
        {"and", "for", "with", "that", "this", "one", "two", "size", "pack", "set", "new", "use"}
    )

    #: 标题里出现在排除词附近、说明这条标题其实是在 **否定** 该属性的线索词。
    #: "solid fence no holes" 里的 holes 不是缺陷而是卖点, 守卫必须放过它。
    _TITLE_NEGATORS: ClassVar[frozenset[str]] = frozenset(
        {"no", "not", "non", "without", "free", "zero", "never", "anti", "less"}
    )

    _PUNCT: ClassVar[str] = "\"'`.,;:!?()[]{}<>"

    # 提示词针对的是 ESCI 的判定口径与本数据集实测出来的失败模式, 不是泛泛的"挑出好商品":
    #
    # 1. 显式写出 E/S/C/I 四档并说明"只有 E/S 计入", 是因为评测的 relevant 就是这么定义的
    #    (gain 线性 E=3/S=2/C=1/其余 0)。让模型按评测的尺子打分, 而不是按它自己的"有用程度"。
    # 2. 第 1 条压的是"发散联想"。ESCI 的 Exact 大量是字面直接匹配 —— 品类词对上、型号对上、数量
    #    对上就是 Exact。模型的默认行为却是做语义关联 ("dog bowl" 联想到 dog food), 在这份数据上
    #    这种联想稳定地把 Complement 排到 Exact 前面。所以第 1 条给的是一个 **可执行的自问句**
    #    ("is this the thing the shopper typed?"), 而不是一句"注意字面匹配"。
    # 3. 第 2 条是否定语义, 也是唯一被标成"最具破坏性"的一条。理由要说清楚: 带孔围栏在
    #    "fence without holes" 上会命中 fence / holes 两个词, BM25 分数最高, 模型的相似度直觉也
    #    最高 —— 也就是说 **所有非语义的信号都指向那个错误答案**, 只有理解否定才能救。运行时还会
    #    在用户消息末尾追加一条针对本查询的具体提醒 (见 ``_exclusion_notice``), 因为通用规则在长
    #    候选列表后面很容易被冲淡。
    # 4. 第 6 条把 BM25 原序定义为"平局时的正确答案"。这是给保守性一个可执行的落点: 稀疏标注下,
    #    分不出高下时保持原序的期望收益为 0 而风险也为 0, 随手换位则是 0 收益 + 正风险。
    # 5. 第 7 条允许返回不足 K 个。模型答得越短, 结果越接近纯 BM25 —— 这正是我们要的退化方向。
    # 6. 第 5 条禁止翻译/改写标题。除了避免污染, 还有一个协议层的理由: 模型唯一被允许输出的就是
    #    编号, 任何自然语言的商品文本出现在输出里都说明它偏离了协议。
    # 7. 结尾出现 "JSON" 字样是有意为之: DashScope 在 response_format=json_object 下要求 messages
    #    必须包含 "json", 否则 400。这样把 AI_STRUCTURED_OUTPUT_METHOD 切成 json_mode 时不必改
    #    提示词。
    _SYSTEM_PROMPT: ClassVar[str] = """\
You rank candidate products against a shopper's search query on an English Amazon catalogue.
You are a relevance judge, not a shopping assistant: never answer the query, never describe or
recommend a product, never write a product name. You emit candidate numbers and nothing else.

Use the catalogue's own four-way scale:
- Exact       the candidate IS the product the query names. Every constraint the shopper wrote
              - product type, brand, model number, size, count, pack quantity, compatibility -
              is satisfied by the candidate's title.
- Substitute  a different product that still does the job the query names.
- Complement  an accessory or add-on FOR the queried product, not the product itself.
- Irrelevant  everything else, and anything the query explicitly excluded.
Only Exact and Substitute count as relevant. Every Exact ranks above every Substitute, and both
rank above Complement and Irrelevant.

Rules, in priority order:
1. Literal identity first. Ask "is this the thing the shopper typed?", not "is this related to
   what the shopper typed?". A candidate that shares the topic but names a different product is
   never Exact: "dog bowl" is not answered by dog food, "iphone 13 case" is not answered by an
   iphone 14 case, "3 pack" is not answered by a single unit, "replacement blade" is not
   answered by the whole tool.
2. Exclusions are binding. When the query says without X, no X, not X, non-X or X-free, a
   candidate whose title says it HAS X is Irrelevant however well the rest matches, and must
   rank below every candidate that does not have X. "fence without holes" is not answered by a
   fence with holes; "#2 pencils not sharpened" is not answered by pre-sharpened pencils. This
   is the most damaging mistake available to you, precisely because the excluded product
   matches every other word in the query and therefore looks like the best candidate.
3. Every stated constraint is a hard constraint: model number, size, count, pack quantity,
   colour, material, and "for <device>" compatibility. Missing one drops a candidate to
   Substitute at best, even if it is otherwise the same kind of product.
4. Constraints the shopper did not state are not constraints. Do not prefer a candidate for
   being a famous brand, cheaper, larger or more premium when the query never asked for it.
5. Judge each title exactly as written. Titles are English: never translate them, never
   transliterate them, and never rewrite them. Your answer contains numbers only.
6. The candidates are listed in the keyword engine's own order. Use that order only to break
   ties: when you cannot tell two candidates apart, keeping the order you were given is the
   correct answer, not a cop-out.
7. Return at most the number of candidates you are asked for, best first, and never pad the
   list. If only three candidates are relevant, return three. If none is relevant, return an
   empty list. Never repeat a number and never emit a number that is not in the list.

Answer as JSON with exactly the fields order and explanation."""

    _USER_TEMPLATE: ClassVar[str] = (
        "Query: {query}\n\nCandidates:\n{candidates}\n\n{instruction}{notice}"
    )

    def __init__(self) -> None:
        # 零参构造是 load_provider() 的硬性要求。这里只做纯内存的配置校验与客户端对象构造, 不发起
        # 任何网络 I/O: ChatOpenAI 的构造只是建了一个 httpx client, 不会连上游。
        model, model_defaulted = self._setting("AI_MODEL", self._DEFAULT_MODEL)
        base_url, base_defaulted = self._setting("AI_API_BASE_URL", self._DEFAULT_BASE_URL)
        if model_defaulted or base_defaulted:
            self._warn_defaults(model, base_url, model_defaulted, base_defaulted)

        # 回报给调用方的模型标识 (Provider.model → RerankResponse.model → Java 侧
        # rerank_model → EvaluationResult.rerank_model)。记的是**实际交给 ChatOpenAI 的那个值**,
        # 而不是 os.getenv("AI_MODEL") —— 走默认档时后者是空串, 产物会说"没有模型", 而真相是
        # "跑了默认档的 qwen3.7-flash-2026-07-15"。
        #
        # 这条链路上这个字段最要紧: 留出集 +0.1349 (CI [+0.1133,+0.1570]) 全部由重排产生, 而
        # 重排结果的形状 (一个候选集的排列) 对模型身份完全不敏感 —— 换个模型跑出别的数字, 从
        # 产物里看不出任何差别。此前只能靠人手写进 experiments/*.json 的 ai 块, 那是注释, 不是证据。
        self.model = model

        self._top_k = self._number("AI_RERANK_TOP_K", self._DEFAULT_TOP_K, int)
        # 0 或负数表示"不设上限, 候选发多少判多少"。截断本身不是错误, 但必须可见 —— 见 rerank()。
        self._max_candidates = max(0, self._number("AI_RERANK_MAX_CANDIDATES", 0, int))
        self._title_chars = max(
            0, self._number("AI_RERANK_TITLE_CHARS", self._DEFAULT_TITLE_CHARS, int)
        )
        self._brand_chars = max(
            0, self._number("AI_RERANK_BRAND_CHARS", self._DEFAULT_BRAND_CHARS, int)
        )
        self._description_chars = max(
            0, self._number("AI_RERANK_DESCRIPTION_CHARS", self._DEFAULT_DESCRIPTION_CHARS, int)
        )

        llm = ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=self._api_key(),
            temperature=self._number("AI_TEMPERATURE", 0.0, float),
            max_tokens=self._number("AI_MAX_TOKENS", self._default_max_tokens(), int),
            # 秒制。见 _DEFAULT_REQUEST_TIMEOUT_MS 上的说明: 它必须和 Java 侧的重排读超时一起定,
            # 单独调这一个只会把失败从"上游超时"搬到"Java 超时"。
            timeout=self._number("AI_REQUEST_TIMEOUT_MS", self._DEFAULT_REQUEST_TIMEOUT_MS, int)
            / 1000.0,
            max_retries=self._number("AI_MAX_RETRIES", 0, int),
            extra_body=self._extra_body(model_defaulted and base_defaulted),
        )
        # include_raw=True 换来两样东西: 一是 usage, 用于思考模式自检与延迟归因; 二是解析失败时能
        # 拿到 parsing_error, 从而把"模型没按 schema 输出"和"网络炸了"分开。
        self._chain = llm.with_structured_output(
            RankedOrder, method=self._method(), include_raw=True
        )
        self._delegate = MockProvider()

    def _default_max_tokens(self) -> int:
        """按 top_k 推导输出上限, 而不是照抄改写侧的常数 256。

        一个编号约 1-3 个 token, 加上 JSON 括号逗号与 12 词以内的 explanation。留足两倍余量:
        截断一个排序的代价 (后半段静默退回原序, 而且看不出来) 远高于把上限设大 (上限不是消耗)。
        """
        width = self._top_k if self._top_k > 0 else (self._max_candidates or 200)
        return max(256, 64 + 8 * width)

    # ------------------------------------------------------------------ 配置读取

    @staticmethod
    def _setting(name: str, default: str) -> tuple[str, bool]:
        """读一个有默认值的字符串配置, 并回报"是否用上了默认值"。

        空串按未设置处理: ``platform/.env`` 里这些变量本来就是空值占位, 而 ``make dev-ai`` 会把
        它们原样注入进程环境。空串和未设置在这里必须等价, 否则默认值永远轮不到生效。
        """
        value = os.getenv(name, "").strip()
        return (value, False) if value else (default, True)

    def _warn_defaults(self, model: str, base_url: str, model_def: bool, base_def: bool) -> None:
        """默认档不是错误, 但必须可见 —— 报告里写"用了模型 X"而实际跑默认档, 在指标里看不出来。"""
        logger.warning(
            json.dumps(
                {
                    "event": "ai.provider.defaults_applied",
                    "service": "ai-adapter",
                    "provider": self.name,
                    "model": model,
                    "base_url": base_url,
                    "defaulted": [
                        name
                        for name, used in (("AI_MODEL", model_def), ("AI_API_BASE_URL", base_def))
                        if used
                    ],
                }
            )
        )

    def _api_key(self) -> str:
        """按 ``AI_API_KEY_ENV`` 指定的变量名去环境里取 key。key 本身没有默认值。

        为什么多一层间接: 不同厂商的变量名不同 (DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / ...), 写死
        一个名字会逼运维改名或复制凭据。这层间接让"key 存在哪个变量"成为配置, 而 key 的值始终只经
        ``os.environ`` 读取, 不出现在配置项 / 日志 / 异常 / 提示词 / 代码的任何地方。
        """
        key_var = os.getenv("AI_API_KEY_ENV", "").strip() or "AI_API_KEY"
        api_key = os.getenv(key_var, "").strip()
        if not api_key:
            raise RuntimeError(
                f"{self.name}: 缺少 API key, 拒绝启动。AI_API_KEY_ENV 当前指向环境变量 "
                f"{key_var!r}, 但该变量未设置或为空 (AI_API_KEY_ENV 未设置时默认指向 "
                f"AI_API_KEY)。请导出 {key_var} 后重启 ai-adapter —— 注意 uvicorn --reload 只重载"
                f"代码不重载环境变量, 改完 .env 必须重启父进程; 或者把 AI_API_KEY_ENV 改成实际持有 "
                f"key 的变量名, 例如 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY。本 provider 不会退化成 "
                f"mock 重排: 那会把'模型根本没跑'伪装成'重排没效果'并写进评测结论, 而重排的评测本来"
                f"就在跟一个只差 0.01 量级的基线比, 这种污染是察觉不到的。"
            )
        return api_key

    def _method(self) -> str:
        method = os.getenv("AI_STRUCTURED_OUTPUT_METHOD", "").strip() or "function_calling"
        if method not in self._METHODS:
            raise RuntimeError(
                f"{self.name}: AI_STRUCTURED_OUTPUT_METHOD={method!r} 不合法, "
                f"只能是 {sorted(self._METHODS)} 之一。"
            )
        return method

    def _number(self, name: str, default: float, cast: type) -> Any:
        value = os.getenv(name, "").strip()
        if not value:
            return default
        try:
            return cast(value)
        except ValueError as exc:
            raise RuntimeError(f"{self.name}: {name}={value!r} 不是数值。") from exc

    def _extra_body(self, profile_is_default: bool) -> dict[str, Any] | None:
        """把 ``AI_EXTRA_BODY`` (一段 JSON) 透传到请求体顶层。

        典型取值 (值里绝不含 key)::

            AI_EXTRA_BODY={"enable_thinking": false}     # DashScope qwen3.x-flash / plus
            AI_EXTRA_BODY={"reasoning_effort": "none"}   # DeepSeek v4
            AI_EXTRA_BODY={}                             # 显式声明"什么都不透传"

        未设置时: 只有模型与端点都走默认档才补上默认档配套的 ``{"enable_thinking": false}``; 一旦
        调用方指定了自己的模型或端点, 这里返回 None, 由调用方自己决定要不要关思考。
        """
        value = os.getenv("AI_EXTRA_BODY", "").strip()
        if not value:
            return dict(self._DEFAULT_EXTRA_BODY) if profile_is_default else None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.name}: AI_EXTRA_BODY 不是合法 JSON ({exc.msg})。") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{self.name}: AI_EXTRA_BODY 必须是一个 JSON 对象。")
        return parsed

    # ------------------------------------------------------------------ 提示词构造

    @staticmethod
    def _clip(value: str, limit: int) -> str:
        """按词边界截断, 顺便把换行 / 竖线压成空格。

        压掉换行与竖线不是美化: 候选是按 ``[n] title | brand`` 一行一条渲染的, 标题里混进换行会
        让模型看到一条对不上编号的孤行, 而编号错位在输出端是完全看不出来的 —— 它只是一个合法的
        排列。
        """
        flat = " ".join(value.replace("|", " ").split())
        if limit <= 0 or len(flat) <= limit:
            return flat
        cut = flat[:limit]
        head, _, _tail = cut.rpartition(" ")
        return head or cut

    def _render_candidate(self, number: int, candidate: Candidate) -> str:
        """渲染成 ``[n] title | brand`` 一行。

        字符上限的语义在标题和其余字段上是 **不同的**, 这是有意的, 但必须记下来:

        * ``AI_RERANK_TITLE_CHARS=0`` = 标题不截断。标题是 Exact 判定的唯一必需信号, "0 表示不发
          标题"是个没有任何合理用途的配置。
        * ``AI_RERANK_BRAND_CHARS=0`` / ``AI_RERANK_DESCRIPTION_CHARS=0`` = 该字段整条不发。这两个
          是可选增强, 而 0 是 description 的默认值 —— 默认值必须表示"不发", 否则 N=50 时会把 25 万
          字符的营销文案送进提示词。
        """
        parts = [f"[{number}] {self._clip(candidate.title, self._title_chars) or '(no title)'}"]
        # 品牌已经在标题里时不重复发: ESCI 标题绝大多数以品牌开头, 重复一遍是纯浪费。品牌本身仍是
        # Exact 判定的硬约束, 所以只在标题没带它时才补。
        if self._brand_chars:
            brand = self._clip(candidate.brand, self._brand_chars)
            if brand and brand.lower() not in parts[0].lower():
                parts.append(brand)
        if self._description_chars:
            description = self._clip(candidate.description, self._description_chars)
            if description:
                parts.append(description)
        return " | ".join(parts)

    def _exclusion_notice(self, terms: set[str]) -> str:
        """把从查询里抽出来的排除词, 变成一条贴在提示词末尾的具体提醒。

        为什么系统提示词里已经有第 2 条还要再来一次: 系统提示词在 N=50 的候选列表之前, 中间隔着
        约 2000 个 token 的商品标题, 而那些标题里的词又高度重复地命中被排除的属性。把提醒放在最后
        一行, 是让它出现在模型开始生成之前的最近位置。
        """
        if not terms:
            return ""
        listed = ", ".join(sorted(terms))
        return (
            f"\n\nThis query excludes: {listed}. A candidate whose title says it has "
            f"{listed} is a wrong answer, not a near miss: rank it below every candidate that "
            f"does not."
        )

    def _build_messages(self, query: str, candidates: list[Candidate], terms: set[str]) -> list:
        rendered = "\n".join(
            self._render_candidate(number, candidate)
            for number, candidate in enumerate(candidates, start=1)
        )
        if self._top_k > 0:
            wanted = min(self._top_k, len(candidates))
            instruction = (
                f"List the candidate numbers of the best {wanted} products for this query, best "
                f"first. Return fewer than {wanted} if fewer are relevant."
            )
        else:
            instruction = (
                "List every candidate number in order, best first. Each number must appear "
                "exactly once."
            )
        return [
            SystemMessage(content=self._SYSTEM_PROMPT),
            HumanMessage(
                content=self._USER_TEMPLATE.format(
                    query=query,
                    candidates=rendered,
                    instruction=instruction,
                    notice=self._exclusion_notice(terms),
                )
            ),
        ]

    # ------------------------------------------------------------------ 否定语义

    @classmethod
    def _usable_term(cls, term: str) -> bool:
        """抽出来的排除词够不够格用来做守卫。宁可判"不够格"而放弃守卫, 也不要拿一个噪声词去降权。"""
        return len(term) >= 3 and term.isalpha() and term not in cls._WEAK_TERMS

    @classmethod
    def _excluded_terms(cls, query: str) -> set[str]:
        """从查询里抽出"被排除的东西"。只做词表 + 位置规则, 不做句法分析。

        它同时喂给提示词 (``_exclusion_notice``) 和输出守卫 (``_apply_negation_guard``), 所以两边
        永远说的是同一个词; 抽不到就两边都不动, 而不是一边动一边不动。
        """
        raw = query.lower().split()
        words = [token.strip(cls._PUNCT) for token in raw]
        terms: set[str] = set()
        for index, word in enumerate(words):
            # "sugar-free" / "gluten-free": 被排除的东西粘在连字符前面。
            if word.endswith("-free") and cls._usable_term(word[: -len("-free")].strip("-")):
                terms.add(word[: -len("-free")].strip("-"))
                continue
            if word not in cls._EXCLUSION_CUES:
                continue
            following = words[index + 1 : index + 4]
            # "no. 2 pencils" / "no 2" 里的 no 是 number 的缩写。和 langchain_rewrite 里 "#2" 必须
            # 保留是同一个坑: 把型号当否定, 会把唯一正确的那批商品全部降权。
            if word == "no":
                numbered = raw[index].endswith(".") or (following and following[0].isdigit())
                if numbered:
                    continue
            # 裸 "free" 在电商查询里几乎总是 free shipping / free returns。只认 "free of X"。
            if word == "free":
                if not following or following[0] != "of":
                    continue
            for candidate_term in following:
                if candidate_term in cls._SKIP_AFTER_CUE:
                    continue
                if cls._usable_term(candidate_term):
                    terms.add(candidate_term)
                break
        return terms

    @staticmethod
    def _word_forms(term: str) -> set[str]:
        """term 的单复数两种写法。只做最简单的 -s 规则: 越聪明的词形还原, 误伤面越大。"""
        if term.endswith("s") and len(term) > 3:
            return {term, term[:-1]}
        return {term, term + "s"}

    @classmethod
    def _asserts_excluded(cls, text: str, terms: set[str]) -> bool:
        """这条标题是否 **正面声称** 自己带有被排除的属性。

        必须是"正面声称": "solid fence no holes" 里的 holes 是卖点不是缺陷, 把它降权正好搞反。所以
        命中后还要看附近有没有否定线索 (前 3 个 / 后 2 个 token) —— 有就放过。
        """
        if not terms:
            return False
        forms: set[str] = set()
        for term in terms:
            forms |= cls._word_forms(term)
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        for position, token in enumerate(tokens):
            if token not in forms:
                continue
            window = set(tokens[max(0, position - 3) : position]) | set(
                tokens[position + 1 : position + 3]
            )
            if cls._TITLE_NEGATORS & window:
                continue
            return True
        return False

    def _apply_negation_guard(
        self, head: list[int], candidates: list[Candidate], terms: set[str], request_id: str
    ) -> tuple[list[int], bool]:
        """在模型选出的头部内部做一次稳定分区: 不带被排除属性的排在前, 带的排在后。

        为什么只在 **模型选出的头部** 内部动, 而不是对整个候选集做分区: 这样守卫永远不会把一个模型
        没选中的候选提进 top-10, 它只能改变模型已经背书过的那几条之间的先后。损失上界因此是明确的
        —— 守卫误判时最坏只是把头部内部换个序, 而不是凭空塞进一条 BM25 排在第 80 位的垃圾。

        代价说清楚: 如果模型选出的 10 条全都带被排除属性, 分区是空操作, 守卫救不了这条查询。这个
        缺口是有意留着的 —— 补它就必须允许守卫从尾部提人, 那就越过了上面那条上界。
        """
        if not terms or len(head) < 2:
            return head, False
        clean: list[int] = []
        flagged: list[int] = []
        for number in head:
            candidate = candidates[number - 1]
            text = f"{candidate.title} {candidate.brand}"
            (flagged if self._asserts_excluded(text, terms) else clean).append(number)
        reordered = clean + flagged
        if not flagged or not clean or reordered == head:
            return head, False
        self._log(
            "ai.rerank.negation_guard",
            request_id,
            excluded_terms=sorted(terms),
            demoted_positions=[head.index(number) for number in flagged],
        )
        return reordered, True

    # ------------------------------------------------------------------ 输出校验

    @staticmethod
    def _unwrap(result: Any) -> tuple[Any, Any]:
        """返回 ``(raw_message, draft)``, 同时兼容信封形状和"直接返回结构体"的形状。"""
        if isinstance(result, dict) and "parsed" in result:
            return result.get("raw"), result.get("parsed")
        return result, result

    @classmethod
    def _coerce_number(cls, value: Any) -> int | None:
        """把模型给的一项转成候选编号。转不动就返回 None 交给调用方计数丢弃。

        为什么要容忍字符串: 结构化输出声明的是 ``list[int]``, 但兼容端点的 tool-calling 参数是模型
        生成的 JSON 文本再解析的, 实测会出现 ``"3"`` 甚至 ``"[3]"``。硬性 isinstance(int) 会把一次
        本来完全可用的排序整条丢掉。
        """
        if isinstance(value, bool):  # bool 是 int 的子类, 但 True 不是编号 1
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            digits = re.search(r"-?\d+", value)
            if digits:
                return int(digits.group())
        return None

    def _repair(self, order: Any, total: int) -> tuple[list[int], dict[str, int]]:
        """把模型说的任意东西, 修成 1..total 的一个排列。

        修复动作与任务要求一一对应: **集外编号丢弃 / 重复编号去重 / 缺失编号按原序补齐**。

        为什么 provider 侧要做这件事, 而 Java 侧还要再做一次同样的防御 —— 两层的理由不同, 缺一
        不可:

        * provider 这一层守的是 **协议本身**。``RerankResponse.ranked_product_ids`` 对所有调用方
          (Java 搜索链路、agent 包的 Python 客户端、直接打 /ai/rerank 的人) 都承诺是候选集的一个
          排列; 让每个调用方各自重新实现这条不变量, 等于把同一个 bug 复制 N 份。而且只有这一层
          知道模型说的其实是编号 —— 到了 Java 眼里全是 id, "52 越界"这种最容易发生的错误在那里
          已经无法区分于"幻觉出的 id"。
        * Java 那一层守的是 **对面不可信**。它面对的可能是 mock provider、echo provider、别人写的
          provider, 或者本模块的一个未来版本; 把不变量的执行寄托在对面的善意上, 正是这个仓库反复
          栽过的那种坑。
        * 两层都失效时的后果是同一个, 而且很难察觉: ranked_product_ids 短一条, 就等于 ES 明明召回
          了却没进结果页 —— 表现为召回退步, 但归因会落到排序上, 完全查不出来。所以这条不变量在
          函数结尾有一条断言兜底: 修完之后长度和集合都必须精确等于输入。
        """
        stats = {"out_of_range": 0, "duplicate": 0, "unparsable": 0, "missing": 0}
        head: list[int] = []
        seen: set[int] = set()
        for item in order if isinstance(order, (list, tuple)) else []:
            number = self._coerce_number(item)
            if number is None:
                stats["unparsable"] += 1
                continue
            if not 1 <= number <= total:
                stats["out_of_range"] += 1
                continue
            if number in seen:
                stats["duplicate"] += 1
                continue
            seen.add(number)
            head.append(number)
        tail = [number for number in range(1, total + 1) if number not in seen]
        stats["missing"] = len(tail)
        return head, stats

    # ------------------------------------------------------------------ 日志

    def _log(self, event: str, request_id: str, **fields: Any) -> None:
        logger.warning(
            json.dumps(
                {
                    "event": event,
                    "service": "ai-adapter",
                    "provider": self.name,
                    "request_id": request_id,
                    **fields,
                }
            )
        )

    def _warn_if_thinking(self, raw: Any, request_id: str) -> None:
        """思考模式自检: 看 usage 里的 reasoning token, 而不是靠人眼盯延迟。

        重排的提示词比改写长一个数量级, 思考模式下的延迟惩罚也随之放大 —— 改写侧是 1s 涨到 8-28s,
        重排侧只会更糟, 而 HTTP 层完全正常。等人发现时一轮评测已经废了。
        """
        usage = getattr(raw, "usage_metadata", None)
        if not isinstance(usage, dict):
            return
        reasoning = (usage.get("output_token_details") or {}).get("reasoning", 0)
        if reasoning:
            self._log(
                "ai.rerank.thinking_enabled",
                request_id,
                reasoning_tokens=reasoning,
                hint=(
                    "模型仍在思考模式下运行, 延迟会打穿读超时。请用 AI_EXTRA_BODY 关掉它, 例如 "
                    '{"enable_thinking": false} 或 {"reasoning_effort": "none"}。'
                ),
            )

    # ------------------------------------------------------------------ Provider 契约

    def rerank(self, payload: RerankRequest) -> tuple[list[str], list[RerankScore], str]:
        """调用模型重排候选。调用失败一律向上抛, 绝不返回一个"看起来正常"的降级结果。

        异常传播的后果与改写侧一致: 本方法抛异常 → FastAPI 返回 HTTP 500 → 调用方按传输错误降级到
        BM25 原序。这与"返回 BM25 原序并声称成功"在结果上完全一样, 但在 **可观测性** 上天差地别:
        后者会让一轮"重排没效果"的评测结论建立在一次模型压根没跑的运行上, 而重排本来就是在跟一个
        差 0.01 量级的基线比, 这种污染在指标里绝对看不出来。
        """
        candidates = list(payload.candidates)
        total = len(candidates)

        # 截断不是错误, 但绝不能静默: 操作者以为发了 100 条都被判过, 实际只判了前 50, 这种偏差在
        # NDCG 数字里看不出来。截掉的部分保持 BM25 原序进入尾部。
        judged = candidates
        truncation_note = ""
        if self._max_candidates and total > self._max_candidates:
            judged = candidates[: self._max_candidates]
            self._log(
                "ai.rerank.candidates_truncated",
                payload.request_id,
                received=total,
                judged=len(judged),
                limit=self._max_candidates,
            )
            truncation_note = (
                f"Only the first {len(judged)} of {total} candidates were sent to the model "
                f"(AI_RERANK_MAX_CANDIDATES); the rest keep their BM25 order."
            )

        terms = self._excluded_terms(payload.query)
        messages = self._build_messages(payload.query, judged, terms)
        try:
            result = self._chain.invoke(messages)
        except Exception as exc:
            # 只记异常类型和 request_id。异常文本可能带上游 URL 或请求体片段 (而请求体里有整份候选
            # 列表), 日志是长期留存的, 不往里塞这些。裸 raise 保留原始 traceback。
            self._log("ai.rerank.failed", payload.request_id, error_type=type(exc).__name__)
            raise

        raw, draft = self._unwrap(result)
        self._warn_if_thinking(raw, payload.request_id)

        order = getattr(draft, "order", None)
        if order is None and isinstance(draft, dict):
            order = draft.get("order")
        if not isinstance(order, (list, tuple)):
            # 模型可达但没给出可用的排序。抛出去而不是返回原序: 返回原序会被记成一次成功的重排,
            # 于是纯 BM25 的结果会被算进 AI 的账上。
            raise ValueError(
                f"{self.name}: 模型没有返回可用的 order 列表 (得到 {type(order).__name__})。"
            )

        head, stats = self._repair(order, len(judged))
        if self._top_k > 0:
            # 模型多给了也只取前 K: 超出 K 的部分对 NDCG@10 没有影响, 而模型在长尾上的判断质量最差,
            # 采纳它只是拿一个没有收益的位置去冒险。
            head, dropped = head[: self._top_k], head[self._top_k :]
            stats["beyond_top_k"] = len(dropped)

        head, guarded = self._apply_negation_guard(head, judged, terms, payload.request_id)

        chosen = set(head)
        ranked_numbers = head + [n for n in range(1, len(judged) + 1) if n not in chosen]
        ranked = [judged[number - 1].product_id for number in ranked_numbers]
        ranked.extend(candidate.product_id for candidate in candidates[len(judged) :])

        # 不变量兜底。走到这里说明上面的修复逻辑本身有 bug (不是模型的问题), 所以是 RuntimeError
        # 而不是 ValueError, 而且必须炸: 少一条 id 就是一条被 ES 召回却没进结果页的商品。
        if len(ranked) != total or sorted(ranked) != sorted(c.product_id for c in candidates):
            raise RuntimeError(
                f"{self.name}: 内部错误, 重排结果不是候选集的排列 "
                f"(输入 {total} 条, 输出 {len(ranked)} 条)。"
            )

        # 分数是**位置的函数**, 不是模型自报的相关性。为什么不让模型打分: 逐条打分要 N 个输出而不是
        # K 个, 延迟按倍数涨; 而且模型自报的分数没有校准, 拿它去和 BM25 分数混合只会引入一个没人
        # 能解释的量纲。这里给的是严格递减的占位值, 唯一的语义就是"顺序"。调用方若要做分数融合,
        # 必须知道这一点, 所以 explanation 里也写着。
        scores = [
            RerankScore(product_id=product_id, score=round(1.0 - index / total, 6))
            for index, product_id in enumerate(ranked)
        ]
        explanation = self._explanation(
            draft, len(head), len(judged), stats, guarded, truncation_note
        )
        return ranked, scores, explanation

    def _explanation(
        self,
        draft: Any,
        adopted: int,
        judged: int,
        stats: dict[str, int],
        guarded: bool,
        truncation_note: str,
    ) -> str:
        """把这次重排到底发生了什么, 原样写给调用方。

        这是本模块唯一对外可见的诊断通道 (日志留在适配器进程里, 评测报告看不到)。所以修复动作的
        计数必须出现在这里 —— 一次"模型给了 12 个编号, 其中 3 个越界" 和一次干净的重排, 在
        ranked_product_ids 上是完全一样的, 只有这里能把两者分开。

        ``judged`` 必须由调用方传进来, 不能从 ``stats`` 反推: ``stats['missing']`` 是在 top_k 截断
        **之前** 算的, 用 ``missing + len(head)`` 去还原总数, 在截断发生时会少算一截, 于是这句诊断
        本身开始说谎。诊断通道说谎比没有诊断通道更糟。
        """
        parts = [
            f"LangChain rerank: adopted {adopted} of {judged} candidates from the model; "
            f"the rest keep their BM25 order."
        ]
        model_note = str(getattr(draft, "explanation", "") or "").strip()
        if model_note:
            parts.append(model_note if model_note.endswith(".") else f"{model_note}.")
        repairs = [
            f"{count} {label}"
            for label, count in (
                ("out-of-range", stats.get("out_of_range", 0)),
                ("duplicate", stats.get("duplicate", 0)),
                ("unparsable", stats.get("unparsable", 0)),
                ("beyond-top-k", stats.get("beyond_top_k", 0)),
            )
            if count
        ]
        if repairs:
            parts.append(f"Discarded {', '.join(repairs)} entries from the model's list.")
        if guarded:
            parts.append(
                "Guard: candidates whose titles assert an attribute the query excludes were "
                "moved below the ones that do not."
            )
        if truncation_note:
            parts.append(truncation_note)
        parts.append("Scores are positional placeholders, not calibrated relevance.")
        return " ".join(parts)

    # rewrite / suggest 明确不在本增量范围内。逐字委托 MockProvider 保证: 把 AI_PROVIDER 切到本类
    # 时整个系统只有一处行为变化, 于是评测差异可以归因到"重排", 而不是"同时换了两样东西"。这一点
    # 与 langchain_rewrite 对 rerank 的处理是镜像的, 是有意保持的对称。
    #
    # 注意这意味着 **本 provider 不做查询改写**: 想同时评测改写 + 重排, 必须先有一个把两者组合起来
    # 的 provider, 而那应该是一次独立的、有自己对照组的增量。

    def model_for(self, capability: str) -> str | None:
        """只有 ``rerank`` 是本 provider 自己跑的; 另外两条连模型身份一起委托给 MockProvider。

        不这么做的话, 一个跑着本 provider 的适配器在 ``/ai/query-rewrite`` 上会回报
        ``model=<配置的模型>``, 而那次改写其实是确定性 mock 做的 —— 一个比"没有模型标识"更坏的
        结果: 它看起来像证据。
        """
        return self.model if capability == "rerank" else self._delegate.model_for(capability)

    def rewrite(self, payload: QueryRewriteRequest) -> tuple[str, dict, float, str]:
        """逐字委托 :class:`~app.provider.MockProvider` (本增量不做 AI 改写)。"""
        return self._delegate.rewrite(payload)

    def suggest(
        self, payload: StrategySuggestRequest
    ) -> tuple[list[ProposedChange], str, list[str], float, str]:
        """逐字委托 :class:`~app.provider.MockProvider` (本增量不做 AI 策略建议)。"""
        return self._delegate.suggest(payload)
