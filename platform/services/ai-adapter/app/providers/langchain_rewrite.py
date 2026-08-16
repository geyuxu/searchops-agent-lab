"""真正调用 LLM 的 query-rewrite provider, 链路是 LangChain (ChatOpenAI + with_structured_output)。

这个模块和 ``echo_upper`` 的定位完全不同: echo_upper 只证明"非 mock 的 provider 能走通到
Elasticsearch", 本模块是第一个**声称要改善相关性**的实现。因此下面记录的是取舍与被否决的方案,
而不是代码做了什么 —— 做了什么读代码就知道, 为什么这么做只有写的时候知道。

为什么用 LangChain 而不是自己发 HTTP
-----------------------------------
本增量的目的之一就是把 LangChain 真正接进来: ``ChatOpenAI`` 走 OpenAI 兼容端点,
``with_structured_output(schema)`` 把"模型必须吐出结构化改写结果"下沉到 tool-calling 协议层,
而不是靠我们自己写正则去抠模型的自然语言。自己拼 httpx 当然也能跑, 但 structured output/
重试语义/异常类型就得重写一遍, 而且换 provider (Anthropic / Bedrock / 本地 vLLM) 还要再写一遍。
``tests/test_providers_langchain.py`` 的 ``no_network`` fixture 会毒化 ``socket.connect``,
所以"绕开 LangChain 自己发请求"在单测里会立刻炸 —— 这条约束是被强制执行的, 不是口头约定。

为什么默认 ``method="function_calling"`` 而不是 LangChain 的默认值 ``json_schema``
-----------------------------------------------------------------------------
``ChatOpenAI.with_structured_output`` 在 langchain-openai 1.5.x 里默认 ``method="json_schema"``,
那是 OpenAI 官方 Structured Outputs, 但兼容端点并不都支持: 实测 DeepSeek 的
``deepseek-v4-flash`` / ``deepseek-v4-pro`` 对 ``response_format={"type":"json_schema"}`` 直接
返回 HTTP 400 "This response_format type is unavailable now"; DashScope 上
``qwen3-30b-a3b-instruct-2507`` 同样 400。而 tool-calling (function_calling) 在实测过的
DashScope 四个模型与 DeepSeek 两个模型上全部可用。所以默认走 function_calling, 同时把方法本身
做成环境变量, 让用别的端点的人自己挑。

为什么必须能从环境变量注入 ``extra_body`` (``AI_EXTRA_BODY``)
------------------------------------------------------------
这是本模块最容易被忽略/后果最严重的一个开关。qwen3.x-flash / qwen3.x-plus 默认开启思考模式,
而思考模式下:

* 单次调用从约 1.0s 涨到 8-28s。readTimeout 是 5000ms, 于是 200 条评测几乎全部落到
  ``AiRewriteStatus.TIMEOUT``, 而 HTTP 层看不出任何异常;
* 强制 tool_choice 会被服务端直接拒绝。本会话实测报错原文:
  ``The tool_choice parameter does not support being set to required or object in thinking mode``;
* DeepSeek 侧是另一种表现: 思考吃掉全部 max_tokens, ``message.content`` 返回空串, 于是 Java 侧
  每次判 ``INVALID_RESPONSE`` 并降级 BM25 —— 看起来"能跑", 其实一条改写都没生效。

关掉思考的参数是厂商私有的 (DashScope 用 ``enable_thinking``, DeepSeek 用 ``reasoning_effort``),
OpenAI schema 里没有它的位置, 只能走 ``extra_body`` 透传到请求体顶层。**实测证明它只有在请求体
顶层才生效**: 嵌套成 ``{"extra_body": {"enable_thinking": false}}`` 时服务端静默忽略, 思考照旧
打开。这正是上一轮花整轮修掉的那类"参数放错位置不报错, 只是悄悄变慢/变错"的缺陷, 所以
``rewrite`` 里还有一条自检: 只要响应回报 reasoning token > 0 就打 WARN, 而不是指望人眼盯延迟。

为什么 ``max_retries`` 默认 0
----------------------------
Java 侧读超时 5000ms, 本 provider 单次调用预算默认 4000ms。任何一次重试都必然把总耗时推过
5000ms, 那时 Java 早就降级走了, 重试只是白占一个上游连接并继续计费。要重试应该在更大的时间
预算里由调用方做, 不是在这里。

为什么缺 key 就在构造函数里抛异常
--------------------------------
``load_provider()`` 在 ``app.main`` 的 import 期执行, 所以这里抛异常 = 服务起不来。这是**期望
行为**: 静默退化成 mock 或规则改写, 会把"模型压根没跑"污染成"AI 没效果", 而后者会被写进评测
结论。宁可服务起不来, 也不要出一份看起来正常的假指标。代价必须说清楚: 本地 ``uvicorn --reload``
下父进程仍持有 :8000 的监听 socket, 子进程 import 失败后端口照样 accept 但无人应答, 表现为
Java 侧整片 TIMEOUT 而不是连接被拒。**看到整片 TIMEOUT, 第一件事是去 dev-ai 终端找 import
traceback。** 所以每条异常信息都直接点名缺哪个环境变量。

为什么模型名和端点有默认值, 而 key 没有
--------------------------------------
key 有默认值等于把凭据写进代码, 绝对不行; 模型名/端点有默认值只是"开箱能跑", 二者风险不对称。
折中办法是: 默认值存在, 但一旦用上就打 WARN 说明"你正在跑默认档"。这样"我以为在跑 A, 其实在跑
B"这种在指标里完全看不出来的偏差, 至少在启动日志里可以对账。默认档是本会话真实验证过的
DashScope 兼容端点 + qwen3.7-flash-2026-07-15。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, ClassVar

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ..models import (
    ProposedChange,
    QueryRewriteRequest,
    RerankRequest,
    RerankScore,
    StrategySuggestRequest,
)
from ..provider import MockProvider, Provider

logger = logging.getLogger("ai-adapter")


class RewriteDraft(BaseModel):
    """``with_structured_output`` 的目标 schema。字段描述会进入 tool schema, 模型看得到。"""

    rewritten_query: str = Field(
        description=(
            "The rewritten English search query: plain words separated by single spaces. "
            "Return the original query unchanged if no rewrite is clearly better."
        )
    )
    confidence: float = Field(
        description=(
            "Your own probability, between 0 and 1, that this rewritten query ranks the "
            "shopper's intended product higher than the original query does. Use 0.3 or "
            "lower when you returned the query unchanged or are unsure."
        )
    )
    explanation: str = Field(
        description=(
            "One short English sentence naming what you changed and why, or stating that "
            "you changed nothing."
        )
    )


class LangChainRewriteProvider(Provider):
    """通过 LangChain 调用真实 LLM 做查询改写; rerank / suggest 仍逐字委托 MockProvider。"""

    name = "langchain-rewrite"

    # ---- 默认档 ------------------------------------------------------------
    _DEFAULT_MODEL: ClassVar[str] = "qwen3.7-flash-2026-07-15"
    _DEFAULT_BASE_URL: ClassVar[str] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 默认档配套的私有参数。为什么和上面两项绑定而不是无条件生效: enable_thinking 是 DashScope
    # 私有字段, 发给 OpenAI 官方端点会 400。所以只有整套默认档都在用时才带上它; 一旦调用方指定
    # 了自己的模型或端点, 就必须自己声明 AI_EXTRA_BODY (不需要就留空)。
    _DEFAULT_EXTRA_BODY: ClassVar[dict[str, Any]] = {"enable_thinking": False}

    #: 允许的 structured-output 方法。json_schema 在部分兼容端点上 400, 故不作默认值。
    _METHODS: ClassVar[frozenset[str]] = frozenset({"function_calling", "json_mode", "json_schema"})

    #: 会从模型输出里剥掉的包裹符号 (见 ``_sanitize``)。
    _WRAPPERS: ClassVar[str] = "\"'`()[]{}<>"

    #: 会从 token 开头剥掉的查询语法前缀。注意 ``#`` 不在其中: ``#2 pencils`` 的 ``#2`` 是铅笔
    #: 硬度型号, 属于商品属性而不是噪声。
    _PREFIXES: ClassVar[str] = "+-~^*"

    #: 大写布尔操作符才算语法; 小写 not/and/or 是自然语言, 必须保留 (否定语义)。
    _OPERATORS: ClassVar[frozenset[str]] = frozenset({"NOT", "AND", "OR"})

    #: 判断"这段文本还在表达排除"的词表, 用于 ``rewrite`` 里的否定守卫。
    _NEGATION_CUES: ClassVar[frozenset[str]] = frozenset(
        {
            "without",
            "not",
            "no",
            "non",
            "none",
            "never",
            "exclude",
            "excluding",
            "except",
            "minus",
            "free",
        }
    )

    # 提示词针对的是实测出来的失败模式, 不是泛泛的"让查询更好":
    #
    # 1. 200 条评测查询里有 66 条 ndcg@10 与 recall@10 同时为 0, 但 zero_result 全是 false。
    #    也就是说主要矛盾是 top-10 里没有 E/S 命中, 不是搜不到东西。所以第 8 条明确写
    #    "precision, not recall", 压制模型扩召回的本能 —— 在这份数据上扩召回只会把本来排第 11
    #    位的好商品挤得更靠后。
    # 2. 否定语义 (without / not / no) 占比很高, 而下游是 BM25 multi_match(operator=or), 根本
    #    无法表达"排除"。因此唯一安全的做法是原样保留否定词与被否定的名词, 再允许模型追加一个
    #    正向词 (solid / seamless) 说明"想要什么"。这不是万能解: BM25 里 "fence without holes"
    #    的 holes 仍会命中带孔围栏, 保留否定只是把损失控制在"不比原查询更差", 真正的排除要靠
    #    后续 rerank 或过滤, 不在本增量范围。被否决的两种写法都是实测踩出来的: DeepSeek 输出
    #    ``fence -holes`` (把 BM25 语法当自然语言; multi_match 不解析它, ``-holes`` 变成字面
    #    token 反而污染召回); DashScope 输出 ``solid mesh screen fence`` (整段丢掉 without
    #    holes, 语义反转)。第 2/4 条针对前者, 第 3 条加上 ``rewrite`` 里的否定守卫针对后者。
    # 3. 前导标点噪声必须区别对待, 这是本提示词里最需要拿捏的一条, 也是我最没把握的一条。
    #    "!awnmower tires without rims" 的 "!" 是纯噪声, 剥掉之后 "awnmower" 仍是错的, 还要纠成
    #    "lawnmower"; 但 "# 2 pencils not sharpened" 里的 "#2" 是铅笔硬度型号, 是核心检索信号,
    #    剥掉等于删信息 (实测 deepseek-v4-flash 正是这么干的, 而 deepseek-v4-pro 保留了它)。
    #    我的取舍: 不写死规则, 让模型逐 token 判断, 并把默认行为设成"保留"。理由是"这个前导标点
    #    是不是噪声"取决于它后面是不是一个真实英文词, 这需要词汇知识, 正是模型该做的事; 写死
    #    "剥掉所有前导标点"会稳定地毁掉型号类查询, 写死"全部保留"则对 "!awnmower" 无能为力。
    #    代价必须说清楚: 这条规则的执行是不稳定的 —— 实测同一模型同一 temperature=0 下,
    #    "# 2 pencils not sharpened" 有时被剥成 "pencils not sharpened", 有时保留 "# 2"。所以它
    #    划不划算只能靠 200 条评测判断, 不能靠几个例子。
    # 4. 强制英文: 商品库是英文 ESCI 子集, 任何中文/音译输出都必然零命中。
    # 5. 保守优先: 第 9 条允许模型原样返回。原查询在 Java 侧记 NO_CHANGE, 指标最多持平; 一个改
    #    坏的查询会让指标下跌。而且平均每查询只有 2.27 条人工标注, 检索到但未标注的文档按 I 记
    #    gain 0 —— 激进改写即使召回了真正的好商品也会被判退步。又一个"宁可不动"的理由。
    # 6. 提示词里出现 "JSON" 字样是有意为之: DashScope 在 response_format=json_object 下要求
    #    messages 必须包含 "json", 否则 400。这样把 AI_STRUCTURED_OUTPUT_METHOD 切成 json_mode
    #    时不必改提示词。
    _SYSTEM_PROMPT: ClassVar[str] = """\
You rewrite noisy shopper search queries for a BM25 keyword index of English Amazon product
listings. You are not a chatbot: never answer the query, never describe products, never put
commentary inside the query itself.

Rules, in priority order:
1. English only. Never translate and never transliterate. The catalogue is English.
2. Output plain search words separated by single spaces. No search operators at all: no
   quotes, no parentheses, no leading + or -, no uppercase NOT / AND / OR, no field:value
   syntax. Every character you emit is matched literally as a word, so an operator only
   adds a junk term that matches nothing.
3. Preserve negation exactly as the shopper wrote it. Words such as "without", "not", "no"
   and "free of", and the noun they negate, must all survive verbatim in your output.
   Dropping them inverts the shopper's intent and is the most damaging thing you can do.
4. You may append a positive term naming what the shopper does want instead (for example
   "solid" or "seamless"), but you may never delete the negated words to do it.
5. Fix noise only when you are sure. Remove a leading punctuation character only if what
   remains is a real English word or an obvious misspelling of one, for example
   "!awnmower" becomes "lawnmower". Keep punctuation that carries product meaning: grades,
   model numbers, sizes and counts such as "#2 pencils", "1/4 inch", "3.5mm", "size 8".
   When in doubt, keep the characters exactly as the shopper typed them.
6. Keep every content word from the original unless it is pure noise. You may add at most
   two widely used retail synonyms for the same product. Never add brands, prices, colours,
   sizes, materials or any attribute the shopper did not mention.
7. Stay short: at most 12 words.
8. Optimise for precision, not recall. The engine already returns results for almost every
   query; the problem is that the right product falls outside the top 10. Sharpen the words
   that identify the product rather than broadening them.
9. If you are not confident your rewrite is better, return the original query completely
   unchanged and report a confidence of 0.3 or lower. An unchanged query is always a safe
   answer; a wrong rewrite is not.

Answer as JSON with exactly the fields rewritten_query, confidence and explanation."""

    _USER_TEMPLATE: ClassVar[str] = "Locale: {locale}\nQuery: {query}\n\nRewrite this query."

    def __init__(self) -> None:
        # 零参构造是 load_provider() 的硬性要求。这里只做纯内存的配置校验和客户端对象构造, 不发
        # 起任何网络 I/O: ChatOpenAI 的构造只是建了一个 httpx client, 不会连上游。
        model, model_defaulted = self._setting("AI_MODEL", self._DEFAULT_MODEL)
        base_url, base_defaulted = self._setting("AI_API_BASE_URL", self._DEFAULT_BASE_URL)
        if model_defaulted or base_defaulted:
            self._warn_defaults(model, base_url, model_defaulted, base_defaulted)

        llm = ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=self._api_key(),
            temperature=self._number("AI_TEMPERATURE", 0.0, float),
            max_tokens=self._number("AI_MAX_TOKENS", 256, int),
            # 秒制。必须小于 AI_TIMEOUT_MS(5000ms), 否则 Java 先放弃而适配器还在等, 白占一个
            # 上游连接, 并且失败原因会被记成 TIMEOUT 而不是真正的上游错误。
            timeout=self._number("AI_REQUEST_TIMEOUT_MS", 4000, int) / 1000.0,
            max_retries=self._number("AI_MAX_RETRIES", 0, int),
            extra_body=self._extra_body(model_defaulted and base_defaulted),
        )
        # include_raw=True 换来两样东西: 一是 usage, 用于思考模式自检; 二是解析失败时能拿到
        # parsing_error, 从而把"模型没按 schema 输出"和"网络炸了"分开。代价是返回形状从
        # RewriteDraft 变成 {"raw","parsed","parsing_error"} 信封 —— ``_unwrap`` 两种都认。
        self._chain = llm.with_structured_output(
            RewriteDraft, method=self._method(), include_raw=True
        )
        self._delegate = MockProvider()

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
        """默认档不是错误, 但必须可见。

        报告里写着"用了模型 X", 实际跑的是默认档 —— 这种偏差在 NDCG 数字里完全看不出来, 只能靠
        启动日志对账。所以宁可多打一行 WARN。
        """
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

        为什么多一层间接: 不同厂商的变量名不同 (DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / ...),
        写死一个名字会逼运维改名或复制凭据。这层间接让"key 存在哪个变量"成为配置, 而 key 的值
        始终只经 ``os.environ`` 读取, 不出现在配置项/日志/异常/代码的任何地方。
        """
        key_var = os.getenv("AI_API_KEY_ENV", "").strip() or "AI_API_KEY"
        api_key = os.getenv(key_var, "").strip()
        if not api_key:
            raise RuntimeError(
                f"{self.name}: 缺少 API key, 拒绝启动。AI_API_KEY_ENV 当前指向环境变量 "
                f"{key_var!r}, 但该变量未设置或为空 (AI_API_KEY_ENV 未设置时默认指向 "
                f"AI_API_KEY)。请导出 {key_var} 后重启 ai-adapter —— 注意 uvicorn --reload 只"
                f"重载代码不重载环境变量, 改完 .env 必须重启父进程; 或者把 AI_API_KEY_ENV 改成"
                f"实际持有 key 的变量名, 例如 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY。本 provider "
                f"不会退化成 mock 或规则改写: 那会把'模型根本没跑'伪装成'AI 没效果'并写进评测。"
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

    @staticmethod
    def _number(name: str, default: float, cast: type) -> Any:
        value = os.getenv(name, "").strip()
        if not value:
            return default
        try:
            return cast(value)
        except ValueError as exc:
            raise RuntimeError(f"LangChainRewriteProvider: {name}={value!r} 不是数值。") from exc

    def _extra_body(self, profile_is_default: bool) -> dict[str, Any] | None:
        """把 ``AI_EXTRA_BODY`` (一段 JSON) 透传到请求体顶层。

        典型取值 (值里绝不含 key)::

            AI_EXTRA_BODY={"enable_thinking": false}     # DashScope qwen3.x-flash / plus
            AI_EXTRA_BODY={"reasoning_effort": "none"}   # DeepSeek v4
            AI_EXTRA_BODY={}                             # 显式声明"什么都不透传"

        未设置时: 只有模型与端点都走默认档才补上默认档配套的 ``{"enable_thinking": false}``;
        一旦调用方指定了自己的模型或端点, 这里返回 None, 由调用方自己决定要不要关思考。
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

    # ------------------------------------------------------------------ 输出处理

    @staticmethod
    def _unwrap(result: Any) -> tuple[Any, Any]:
        """返回 ``(raw_message, draft)``, 同时兼容信封形状和"直接返回结构体"的形状。

        ``with_structured_output(..., include_raw=True)`` 返回 ``{"raw","parsed",
        "parsing_error"}``; 但 Runnable 的返回形状终究取决于组合方式, 而这里唯一需要的是"能不能
        读到 rewritten_query"。两种都认, 比断言一种形状然后在换实现时炸掉要稳。
        """
        if isinstance(result, dict) and "parsed" in result:
            return result.get("raw"), result.get("parsed")
        return result, result

    @classmethod
    def _sanitize(cls, value: str) -> tuple[str, list[str]]:
        """剥掉模型自作主张输出的检索语法, 返回 ``(清洗后的查询, 被改动的原 token)``。

        为什么需要这一层: 实测 deepseek-v4-flash 把 "fence without holes" 改成 ``fence -holes``,
        把 BM25 的减号语法当成了自然语言。下游 ``SearchQueryCompiler`` 走 ``multi_match``, 不解析
        query_string 语法, ``-holes`` 会被当字面 token 参与匹配 —— 既没有排除效果, 又污染召回。

        这不是"规则改写兜底": 它从不在模型失败时替模型生成查询, 只删模型多写的语法字符, 并把删
        了什么原样写进 ``explanation``。所以它是可见的, 不是静默的。

        小写 not / and / or 一律保留 —— 它们是否定语义的载体, 删掉等于把查询改成反义。``#`` 也不
        在剥离集合里, 因为 ``#2 pencils`` 的 ``#2`` 是商品型号。
        """
        kept: list[str] = []
        touched: list[str] = []
        for token in value.split():
            if token in cls._OPERATORS:
                touched.append(token)
                continue
            cleaned = token.strip(cls._WRAPPERS).lstrip(cls._PREFIXES)
            if cleaned != token:
                touched.append(token)
            if cleaned:
                kept.append(cleaned)
        return " ".join(kept), touched

    @classmethod
    def _negates(cls, value: str) -> bool:
        """这段文本是否还在表达"排除"。只查词表, 不做句法分析 —— 够用, 且不会误伤。"""
        tokens = {token.strip(".,;:!?") for token in value.lower().split()}
        return bool(cls._NEGATION_CUES & tokens)

    @staticmethod
    def _normalize(value: str) -> str:
        """与 ``AiAdapterClient.normalize`` / ``SearchQueryCompiler.applyRewrite`` 保持一致。

        只用来判断这次改写在 Java 侧会记成 APPLIED 还是 NO_CHANGE, 从而让 explanation 说的话和
        ``ai_status`` 对得上, 不至于 explanation 说"改写了"而 ai_status 是 NO_CHANGE。
        """
        return " ".join(value.split()).lower()

    def _warn_if_thinking(self, raw: Any, request_id: str) -> None:
        """思考模式自检: 看 usage 里的 reasoning token, 而不是靠人眼盯延迟。

        思考模式的症状是延迟从约 1s 涨到 8-28s, 在 5000ms 读超时下表现为整片 TIMEOUT, 而 HTTP 层
        完全正常。等人发现时一轮评测已经废了, 所以必须由机器来发现。
        """
        usage = getattr(raw, "usage_metadata", None)
        if not isinstance(usage, dict):
            return
        reasoning = (usage.get("output_token_details") or {}).get("reasoning", 0)
        if reasoning:
            logger.warning(
                json.dumps(
                    {
                        "event": "ai.rewrite.thinking_enabled",
                        "service": "ai-adapter",
                        "provider": self.name,
                        "request_id": request_id,
                        "reasoning_tokens": reasoning,
                        "hint": (
                            "模型仍在思考模式下运行, 延迟会打穿 AI_TIMEOUT_MS。请用 "
                            'AI_EXTRA_BODY 关掉它, 例如 {"enable_thinking": false} 或 '
                            '{"reasoning_effort": "none"}。'
                        ),
                    }
                )
            )

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

    # ------------------------------------------------------------------ Provider 契约

    def rewrite(self, payload: QueryRewriteRequest) -> tuple[str, dict, float, str]:
        """调用模型改写查询。调用失败一律向上抛, 绝不返回一个"看起来正常"的降级结果。

        异常传播的准确后果 (不要写成"会记成 TIMEOUT"这种想当然的话): 本方法抛异常 → FastAPI 返回
        HTTP 500 → Java 侧 ``AiAdapterClient.classify`` 归为 ``TRANSPORT_ERROR`` 并降级 BM25。只有
        当上游慢到超过 Java 的 readTimeout(5000ms) 时 Java 才记 ``TIMEOUT``; 本 provider 默认单次
        预算 4000ms + 0 重试, 正是为了让适配器比 Java 先失败并快速释放连接。两种状态都如实表示
        "这次没有 AI 改写", 都不会被误读成"AI 跑了但没效果" —— 这才是要点。
        """
        messages = [
            SystemMessage(content=self._SYSTEM_PROMPT),
            HumanMessage(
                content=self._USER_TEMPLATE.format(locale=payload.locale, query=payload.query)
            ),
        ]
        try:
            result = self._chain.invoke(messages)
        except Exception as exc:
            # 只记异常类型和 request_id。异常文本可能带上游 URL 或请求体片段, 而日志是长期留存
            # 的, 不往里塞任何可能含凭据或原始 prompt 的内容。裸 raise 保留原始 traceback。
            self._log("ai.rewrite.failed", payload.request_id, error_type=type(exc).__name__)
            raise

        raw, draft = self._unwrap(result)
        self._warn_if_thinking(raw, payload.request_id)

        candidate = getattr(draft, "rewritten_query", None)
        if not isinstance(candidate, str) or not candidate.strip():
            # 模型可达但没给出可用的改写。这是模型的问题, 不是我们静默降级的理由: 若在这里返回
            # 原查询, Java 会记 NO_CHANGE (一个"成功"状态), 于是整轮评测会把纯 BM25 的结果算作
            # AI 的结果。抛出去换来 HTTP 500 → TRANSPORT_ERROR, 难看但诚实。
            raise ValueError(
                f"{self.name}: 模型没有返回可用的 rewritten_query "
                f"(得到 {type(candidate).__name__})。"
            )

        rewritten, touched = self._sanitize(candidate)
        if not rewritten:
            raise ValueError(f"{self.name}: 清洗掉模型输出的检索语法后, 改写结果为空。")

        notes: list[str] = []
        if touched:
            notes.append(f"Stripped query-syntax tokens the model emitted: {touched}.")

        # 否定守卫: 原查询在表达排除而改写把排除弄丢了, 就退回原查询。
        # 为什么退回而不是抛异常: 模型确实跑了、确实回答了, 这是内容质量问题, 不是基础设施故障。
        # 记成 TRANSPORT_ERROR 会污染上一轮刚建立起来的错误分类。退回后 Java 记 NO_CHANGE ——
        # 而"交给 ES 的查询等于原查询"正是此刻的事实, 如实。
        # 为什么这不算"静默降级": 守卫触发会打 WARN, 原因会写进返回给调用方的 explanation,
        # confidence 同时被压到 0.0。它是可见的。
        guarded = self._negates(payload.query) and not self._negates(rewritten)
        if guarded:
            self._log("ai.rewrite.negation_dropped", payload.request_id)
            notes.append(
                "Guard: the model dropped the negation, so the original query was kept. A "
                "rewrite that loses 'without'/'not' retrieves exactly the products the shopper "
                "is trying to avoid."
            )
            rewritten = " ".join(payload.query.split())

        # confidence 直接采用模型自报值, 只做区间夹取 (QueryRewriteResponse 要求 0..1, 越界会变成
        # pydantic ValidationError → HTTP 500)。不美化也不重标定: 它的含义就是"模型自己认为这次
        # 改写更好的概率"。已知它偏乐观 —— 实测模型对一次丢掉了 "without holes" 的改写仍自报
        # 0.95 —— 读指标时别把它当成校准过的分数。守卫触发时按 0.0 算, 因为此刻交出去的查询根本
        # 不是模型的输出。
        confidence = 0.0 if guarded else self._confidence(draft)

        changed = self._normalize(rewritten) != self._normalize(payload.query)
        model_note = str(getattr(draft, "explanation", "") or "").strip()
        parts = [
            f"LangChain rewrite ({'changed' if changed else 'unchanged'}):",
            model_note or "(model gave no explanation)",
            *notes,
        ]
        if not changed and not guarded:
            parts.append("Model chose not to rewrite; the original query is used as is.")

        return (
            rewritten,
            # filters 原样拷贝透传。本增量不做 filter 抽取: 下游 SearchQueryCompiler 目前根本不
            # 消费 extracted_filters, 在这里凭空造 filter 只会让后续对比失去参照。拷贝而不是直接
            # 返回 payload.filters, 是为了调用方改返回值时不会污染请求对象。
            dict(payload.filters),
            confidence,
            " ".join(parts),
        )

    @staticmethod
    def _confidence(draft: Any) -> float:
        try:
            return min(max(float(getattr(draft, "confidence", 0.0)), 0.0), 1.0)
        except (TypeError, ValueError):
            return 0.0

    # rerank / suggest 明确不在本增量范围内。逐字委托 MockProvider 保证: 把 AI_PROVIDER 切到本类
    # 时整个系统只有一处行为变化, 于是评测差异可以归因到"查询改写", 而不是"同时换了三样东西"。
    # 这一点与 echo_upper 一致, 是有意保持的。

    def rerank(self, payload: RerankRequest) -> tuple[list[str], list[RerankScore], str]:
        """逐字委托 :class:`~app.provider.MockProvider` (本增量不做 AI 重排)。"""
        return self._delegate.rerank(payload)

    def suggest(
        self, payload: StrategySuggestRequest
    ) -> tuple[list[ProposedChange], str, list[str], float, str]:
        """逐字委托 :class:`~app.provider.MockProvider` (本增量不做 AI 策略建议)。"""
        return self._delegate.suggest(payload)
