# `agent/` — SearchOps 提案 Agent

一个 Python 包（`searchops_agent`），承载本项目最核心的两条主张：

1. **治理边界是代码事实，不是提示词约定。** Agent 能提案，不能审批、不能发布、不能回滚——
   因为这三个方法根本不在它的工具注册表里，而注册表由白名单 + 安全等级双重过滤构建，
   越界在构造期就抛异常。
2. **晋级由统计门禁决定，且失败关闭。** 均值更高不足以晋级；证据不足时门禁给出的是
   "无法裁决"而不是"已被否决"。

这两条都有可执行断言守着（`tests/`，26 条）。

> 系统全景、读代码的入口顺序与跨模块不变式：见 [`../docs/code-structure.md`](../docs/code-structure.md)。
> 服务端形态、端口与运行方式：见 [`../platform/docs/architecture.md`](../platform/docs/architecture.md)
> 与 [`../platform/docs/runbook.md`](../platform/docs/runbook.md)。

---

## 目录与模块地图

```
agent/
├── pyproject.toml               # 包元数据；llm 附加依赖精确锁版本（理由见文件内注释）
├── searchops_agent/
│   ├── __init__.py              # 顶层导出
│   ├── safety.py                # 安全分级：随客户端方法登记的元数据
│   ├── tools.py                 # 提案者可见的工具注册表（白名单 + 等级双重过滤）
│   ├── client.py                # Agent Tool Gateway 的 HTTP 客户端 + 幂等键
│   ├── models.py                # 与 Java 侧 ApiModels 对齐的传输模型
│   ├── prompts.py               # LLM 提示词 + 与代码守卫共用的 NEGATION_CUES
│   ├── proposers.py             # 三个提案器 + 共用的证据/守卫/编译器
│   ├── loop.py                  # 提案闭环与 TrainBench，产物写 experiments/
│   ├── cli.py                   # 命令行入口
│   └── eval/
│       ├── loader.py            # 读 platform 产出的评测 JSON
│       ├── stats.py             # 配对统计检验 + 受影响子集分析
│       ├── gate.py              # 晋级门禁
│       ├── splits.py            # train / holdout 确定性划分
│       └── sweep.py             # BM25 字段权重扫描（train 选择 / holdout 验收分离）
└── tests/
    ├── test_governance_boundary.py     # 治理边界与证据隔离
    └── test_significance_verdict.py    # 显著性口径与门禁判据
```

| 模块 | 行数 | 它回答的问题 |
|---|---:|---|
| `safety.py` | 60 | 一个能力有多危险？谁能用它？ |
| `tools.py` | 65 | 自动化提案者到底看得见哪些工具？ |
| `client.py` | 184 | 这些工具在 HTTP 上长什么样？重试会不会重复建单？ |
| `models.py` | 68 | 两侧的数据形状怎么保证不漂移？ |
| `prompts.py` | 214 | 提示词为什么这么写？哪些词表是两侧共用的？ |
| `proposers.py` | 1366 | 候选策略从哪来？模型的输出凭什么可信？ |
| `loop.py` | 913 | 提案怎么自证？产物凭什么可复现、可审计？ |
| `eval/stats.py` | 526 | 这个差值是真的吗？"显著"到底是什么意思？ |
| `eval/gate.py` | 182 | 这个候选能不能进入人工审批？ |
| `eval/splits.py` | 417 | 调参用哪批数据？验收用哪批？怎么保证不串？ |
| `eval/sweep.py` | 711 | 纯调参的天花板在哪？ |

---

## 安装与运行

```bash
cd agent
python -m venv .venv && . .venv/bin/activate
pip install -e .            # httpx / pydantic / numpy
pip install -e '.[llm]'     # 额外装 LangChain 一组（LLMProposer 才需要）
pip install -e '.[dev]'     # pytest
```

`[llm]` 组里四个包**精确锁版本**（`langchain-core` / `langchain-openai` / `openai` /
`tiktoken`），并与 `platform/services/ai-adapter/requirements.txt` 逐位对齐。理由写在
`pyproject.toml` 里：其中 `openai` 的 `extra_body` 顶层透传行为、`tiktoken` 的编码表、
`langchain-*` 的 `with_structured_output` 返回形状，都属于"换版本不报错、只是静默改行为"
的一类；两侧版本分叉时"到底是谁的问题"就无法归因。

### CLI

包装成 console script `searchops-agent`，也可以 `python -m searchops_agent.cli`：

| 子命令 | 作用 |
|---|---|
| `tools` | 打印提案者可见的工具与安全等级（治理边界的自描述） |
| `show <run.json>` | 打印一次评测总览 |
| `selfcheck [run]` | 零假设自检：同一份数据与自身比较必须全部不显著 |
| `compare <a> <b>` | 两次评测的逐指标配对比较 |
| `gate <a> <b>` | 对候选执行晋级门禁（PROMOTE 返回 0，BLOCK 返回 1） |
| `propose --proposer rule\|llm` | 完整闭环：诊断 → 提案 → 干跑 → 自证评测 → 门禁 →（`--apply` 时）建草稿并提交待审 |

另有两个模块级 CLI：

```bash
python -m searchops_agent.eval.splits --out ../experiments --baseline   # 划分 + 两侧基线
python -m searchops_agent.eval.sweep  sweep  --manifest … --out …       # 只在 train 上扫描
python -m searchops_agent.eval.sweep  verify --manifest … --best … --out …  # holdout 验收一次
```

### 前置条件

- **搜索服务必须在跑**（默认 `http://127.0.0.1:8080`）。`propose` / `splits --baseline` /
  `sweep` 都要调 `/api/v1/tools/*`。
- **`platform/data/processed/queries.jsonl` 必须存在**。它被 `.gitignore` 排除（可由
  `make data` 重建），而 `eval/splits.py` 的默认查询源就指向它。缺文件时 `load_split()`
  会在指纹校验之前就打不开文件。
- **LLM key 不进仓库**。`LLMProposer` 通过 `AGENT_LLM_API_KEY_ENV` 拿到**变量名**，
  再去环境里读值（未设置时默认指向 `DASHSCOPE_API_KEY`）。key 的值不出现在配置项、
  日志、异常文本与任何产物里。key 通常写在 `~/.zshrc`，只有交互式 shell 会加载它，
  所以需要 `zsh -ic '…'` 之类的方式运行。

---

## 逐模块：做什么，以及为什么这样设计

### `safety.py` — 分级随方法登记，未登记按最危险处理

五级枚举，序号越大权限越高：

| 等级 | 含义 |
|---|---|
| `READ` | 只读查询，无副作用 |
| `DRY_RUN` | 计算并返回对比结果，不写入任何状态 |
| `GOVERNED_WRITE` | 写状态，要求 actor / request_id / `Idempotency-Key`，但不改变线上策略 |
| `PRIVILEGED_WRITE` | 授予审批权，产生 `approval_token`。人类角色专属 |
| `TOKEN_GATED_WRITE` | 改变线上生效策略，额外要求有效 `approval_token`。人类角色专属 |

`MAX_AUTOMATED = GOVERNED_WRITE` 是自动化的天花板：它之上必须有人类介入。

三条设计选择：

- **分级是元数据，不是文档表格。** `@safety(SafetyClass.X)` 装饰器把等级挂到函数对象上
  （`wrapper.safety_class`），于是注册表和测试都能**内省**它。写在 Markdown 里的分级表
  没人能断言，写在函数上的可以。
- **`safety_class_of()` 对未登记的函数返回 `TOKEN_GATED_WRITE`**，即最高危。
  失败关闭而不是默认放行：日后有人给客户端加了新方法却忘了登记等级，它默认被挡在
  自动化之外，而不是默认可用。`test_unregistered_method_defaults_to_most_dangerous`
  钉住这条。
- **等级取自 `platform/docs/mcp-server-design.md` 的 MCP 工具表**，一一对应。
  这样两处不是各写各的，而是同一份分级的两个视图。

### `tools.py` — 白名单 + 等级双重过滤，冗余是刻意的

```python
ALLOWED   = ("query_metrics", "zero_result_queries", "low_quality_queries",
             "current_strategy", "strategy_history", "preview",
             "evaluate_query", "evaluate_candidate", "create_draft", "submit")
FORBIDDEN = ("approve", "publish", "rollback")
```

`build_registry(client)` 只遍历 `ALLOWED`，并在两种情况下抛 `GovernanceViolation`：
名字同时出现在两个列表里，或者方法的登记等级高于 `MAX_AUTOMATED`。

为什么这套过滤看起来冗余，却必须冗余：

- **白名单而非黑名单**：新增的客户端方法**默认不可见**。黑名单的失效方式是"忘了往里加"，
  白名单的失效方式是"忘了往里加，于是新功能不生效"——后者会被立刻发现，前者不会。
- **`FORBIDDEN` 与 `ALLOWED` 逻辑上互斥**（遍历的是白名单，禁止名单永远轮不到生效）。
  保留它是为了让**越界在测试里立刻显形**：
  `test_registry_rejects_privileged_method_added_to_allowlist` 往 `ALLOWED` 里塞一个
  `publish` 就必须炸。
- **等级检查是第二道**：即使有人把 `publish` 加进白名单**并且**从 `FORBIDDEN` 里删掉，
  它的 `TOKEN_GATED_WRITE` 等级仍然会让构造失败。绕过一道防线不够，得同时改三处，
  而三处都有测试。

`describe(registry)` 打印一份可读的能力清单——`searchops-agent tools` 就是它。

### `client.py` — 网关客户端与幂等键

对应 `/api/v1/tools/*` 的每个端点，每个方法带 `@safety(...)`。

- **写操作的 `Idempotency-Key` 有默认值**：`idempotency_key or rid`，其中
  `rid = request_id or self.new_request_id()`。调用方可以显式传入以获得重试语义；
  不传时按内容+新 UUID 派生。服务端 `StrategyService.idempotent()` 用
  `(key, operation)` 查已有结果，因此**重试不会重复建单**。
  `test_writes_require_idempotency_key` 用 `inspect.signature` 断言这两个写方法确实有
  这个参数（签名级断言，不依赖跑通网络）。
- **`approve` / `publish` / `rollback` 存在于客户端，但永远不在注册表里。**
  它们留在这里，是为了让人类操作者与集成测试能走完整生命周期；Agent 侧的隔离由
  `tools.py` 负责。文件里对这三个方法有一段专门的注释说明这个分工。
- **`evaluate_candidate` 是整个提案闭环的支点。** 它是 `DRY_RUN` 级：对一个**尚未发布**的
  配置跑整轮离线评测，服务端强制 `persist=false`、不读写任何 strategy 版本、返回
  `strategy_version=-1`。正因为这条路径不污染策略历史与审计流，Agent 才可以**自由试错**。
  请求体里显式写 `"persist": False`——服务端也会强制，这里写出来是为了让意图在调用点可见。

### `models.py` — 契约对齐，不做重命名

pydantic 模型与 `lab.searchops.domain.ApiModels` 字段名逐一对应（`@JsonProperty` 是什么，
这里就是什么）。刻意不做 snake/camel 重命名：多一层映射就多一处能悄悄漂移的地方。

`QueryMetric` 是**配对统计检验的最小单位**——评测响应里的 `queries` 数组本身就是逐查询指标，
因此不需要为统计分析额外埋点。

### `prompts.py` — 提示词的"为什么"，以及两侧共用的词表

文件的 docstring 记录的是**提示词为什么这么写**，每条规则都对应一个已经在本仓库实测出来的
失败模式，没有一条是泛泛的"好好干活"。要点：

- **整份提示词是英文**：被操作的对象（查询串、同义词展开项、rewrite 目标串）全是英文，
  最终原样进 BM25 的 `multi_match`。让模型用中文推理再产出英文 token，等于在链路里插一次
  翻译——`ai-adapter` 侧实测过这个失败模式（音译/意译后整条查询零命中）。
- **必须把两个旋钮的触发机制原样告诉模型**：`expandSynonyms` 是**子串包含**触发 + 追加，
  `applyRewrite` 是**整串相等**触发 + 整条替换。这与"同义词/改写"两个词的常识含义不一致，
  不说清楚，模型必然按"词表"和"子串替换"去想，产出一堆**永不触发**的规则——那不是坏提案，
  是空提案，而空提案会被评测记成"LLM 没有收益"，污染对照结论。
- **`NEGATION_CUES` 与 `ai-adapter` 的 `_NEGATION_CUES` 逐字相同**，并且**同时**被提示词
  （由这个 frozenset 直接渲染进 `SYSTEM_PROMPT`）和 `proposers.py` 的守卫使用。
  模型看到的禁令和代码执行的检查是同一份列表，不会分叉。
  `free` 是明知会误报的一个（`free weights` 里它不是否定）——保留它是因为这个词表只用于
  **要求保留**，误报使守卫更严，漏报则放行语义反转的规则，两种错误代价不对称。
- **明确允许弃权**：空列表是合法答案。稀疏标注下激进扩召回几乎必然掉分，"模型硬凑三条提案"
  会让对照实验读起来像在乱开药方。

### `proposers.py` — 三个提案器，一套证据与守卫

所有提案器产出同一种 `Proposal`（`name` / `config` / `rationale` / `evidence` / `origin` /
`model` / `guard_notes`），因此可以在同一门禁下直接比较。

`Proposal.model` 与 `origin` 都带模型名，是刻意的冗余：产物 JSON 里 `model` 只在 run 级别
记一次，而提案是逐条归档的；一份只在顶层写了模型名的产物，在提案被单独摘出来引用时就再也
说不清是谁提的。

#### `RuleProposer`（非 LLM 基线）

不调任何模型，按检索诊断规则产出候选。它是**对照组，不是占位符**——没有它就无法回答
"LLM 提案到底带来了什么"。实现里落地的是两个动作：零结果查询里反复出现的词 → 放宽
`minimum_score`；存在低质量查询 → 上调 `description` / `bullet_point` 权重。
（类 docstring 列了三类失败模式，代码实际发射两条提案；读代码时以代码为准。）

#### `SymbolicRuleProposer`（与 LLM **同空间**的对照臂）

存在理由一句话：`RuleProposer` 只动 `field_weights`，而 `LLMProposer` 的输出 schema 里
**根本没有** `field_weights`。两臂动作空间不重叠，"LLM 输给规则"因此是个假命题——两边根本
没在比同一件事。这个类只输出 `synonyms` 与 `rewrite_rules`，与 `_ProposedChange` 逐字段同构，
交给**同一个** `_Compiler`、**同一套**守卫、**同一个** `ProposalLoop`。唯一的差别只有
"提案从哪来"。

三族启发式，每族一条提案（因为门禁是逐提案判的，合成一条就只能得到"这一堆合起来有没有用"）：

| 族 | 内容 | 预期 |
|---|---|---|
| `punctuation` | 剥首尾标点（朴素正则基线） | **恒等**——分析器在分词阶段已经剥掉了 |
| `symbol` | 符号与 HTML 实体展开（`$5`→`5 dollar`、`5"`→`5 inch`、`&#34;` 解码） | 真的改变 token 流 |
| `synonym` | 符号同义词 + 车型年份 | 唯一能泛化到证据之外的一族（子串触发） |

`_analyzed_tokens()` 在本地近似索引分析器（`standard` + `lowercase` + `asciifolding`），
**只用来判定"这条改写在 token 层面动了没有"**，把 `punctuation` 与 `symbol` 分流。
它刻意不去问 Elasticsearch 的 `_analyze`：提案器必须是纯确定性、可离线复现的，一旦依赖
服务端，同一份证据在服务不可用时就产出不同的提案。代价是它只是近似，所以它的结论只进
`rationale` / 日志，**不参与任何丢弃决策**。

类文档里还有一节"**故意不做的事**"，每条都有依据：不做拼写纠错（`fuzziness: AUTO` 已覆盖）、
不插入语义名词（需要品类知识，**这正是 LLM 臂可能的真实增量，基线做了对照就白做了**）、
不做 `#` 展开（本数据集里至少三种读法）、不做频次挖掘出的同义词（跑，但机械弃权，结果写进
`guard_notes` 存证）、不删词。

#### `LLMProposer`

`ChatOpenAI` + `with_structured_output(_ProposalBatch, method="function_calling",
include_raw=True)`，走 OpenAI 兼容端点。三条设计选择：

**(1) 模型够不着 `field_weights` / `brand_boosts`——它们不在 schema 里。**
这不是"提示词里请求模型别调权重"，是结构上不给它这个字段。依据是三条实测结论（权重扫描的
头部已被数值搜索搜干净、`category` 字段 IDF≈0、等比缩放权重向量是恒等变换，详见
`experiments/sweep-*.json` 与 `prompts.py` 的 docstring）。让 LLM 继续拧权重只会产出必然被
拦下的提案，而那会把"这条路本来就走到头了"误读成"LLM 没用"。约束只有这一个落点——日后想
让模型重新参与权重，改一处即可，不会散落在提示词的若干行里等着被忽略。

**(2) 引用与可行性由代码校验，不由提示词承诺。** 见下面的 `_Compiler`。

**(3) 缺 key 在构造函数里抛异常，绝不退化成 `RuleProposer`。**
静默退化会产出一份说"LLM 提案器跑了、指标没提升"的报告，而真相是模型压根没被调用；
那份报告会进对照结论，比服务起不来坏得多。代价是 LangChain 的导入必须延迟到 `__init__`
里做（`searchops_agent/__init__.py` 会导入本模块，没装 `[llm]` 的人仍要能跑规则基线）。

两个自检，都是为了把"不报错、只是悄悄变错"交给机器发现：

- **截断自检** `_assert_not_truncated()`：`max_tokens` 打满时 `finish_reason == "length"`，
  而 `include_raw=True` 的信封里 **`parsing_error` 仍是 `None`、`parsed` 直接是 `None`**——
  LangChain 既不抛异常也不登记解析错误。不显式查这一项，症状就是提案器时不时返回空，
  看起来像"模型这轮没想法"。
- **思考模式自检** `_warn_if_thinking()`：看 `usage_metadata` 里的 reasoning token，
  而不是靠人眼盯延迟。DashScope 的 `enable_thinking=false` 必须在请求体**顶层**（嵌套会被
  服务端忽略），且思考模式下强制 `tool_choice` 会被直接拒绝——而
  `with_structured_output(method="function_calling")` 正是要强制它。所以这个开关不是调优项，
  是这条链路能不能跑通的前提。默认档（模型与端点**都**没被覆盖时）自动带上它；发给别家
  端点会 400，所以只在默认档下带。

其他默认值的理由：`max_retries` 默认 **0**（一次失败必须响亮且可归因；重试会把一次超时变成
三次计费的超时，并掩盖"端点参数配错了"这类必然复现的错误）；`propose()` 里调用失败一律
向上抛，**绝不返回一个"看起来正常"的空列表**——空列表在这里有唯一含义：模型跑了，但没有
一条通过守卫。

#### `_Evidence` 与 `_Compiler`：机械可判定的守卫

`_Evidence.build()` 收集本轮可引用的全部事实（`query_id → 归一化查询串`），并渲染成提示词
里的两段证据。查询串用 `repr` 渲染——本数据集里前导标点是真实信号，裸着打印会让模型看不出
首尾空白与标点的确切位置。

`_Compiler` 逐条执行守卫，**没有一条依赖对语义的主观判断**：

| 守卫 | 判据 | 对应的引擎行为 |
|---|---|---|
| 引用存在性 | `query_id` 必须在本轮证据里 | —（凭空捏造 → 整条作废） |
| 同义词可行性 | term 必须是某条证据查询的**子串** | `expandSynonyms` 的 `contains` 触发 |
| 改写可行性 | `match` 归一化后必须**逐字等于**某条证据查询 | `applyRewrite` 的整串相等 |
| 恒等改写 | `rewrite` 归一化后 == `match` 则丢弃 | 空提案会被记成"没有收益" |
| 否定守卫 | `match` 表达排除而 `rewrite` 丢了否定 → 丢弃 | `operator=or` 表达不了排除 |
| 契约上下界 | 同义词键/值数、规则条数、字符长度、权重区间 | Java 侧 `@Size/@Min/@Max` |

`_normalize_query()` 与 Java 侧 `SearchQueryCompiler.applyRewrite` 的归一化**逐字一致**
（`String.join(" ", query.trim().split("\\s+")).toLowerCase()`），否则"这条规则会不会触发"
在两侧会给出不同答案。

两个容易忽略但关键的细节：

- **被判掉的条目一律记进 `Proposal.guard_notes` 并打 WARN**，不是静默丢弃。丢掉这些记录，
  日志里就只剩"模型提了 3 条、留下 0 条"，无从分辨是模型在捏造引用、还是它写了一堆永不触发
  的规则——而这两者对模型的评价完全不同。
- **`self.cited` 每次 `compile()` 都重置，且只累计存活下来的条目所引用的 id。**
  一个 `_Compiler` 会被同一轮的多条提案复用；引用若跨提案累加，第二条提案就会声称自己
  针对第一条提案的查询。而如果被丢弃的条目也计入引用，产物就会声称"这条提案针对 query 17"，
  而实际配置里根本没有任何东西碰 query 17。

`assert_contract()` 在合并后再查一次上下界（包括模型够不着的权重字段——它们是从线上 current
继承来的，继承一份非法配置同样会让整轮提案在服务端炸掉），不在这里查就要等
`POST /evaluations/candidate` 返回 400 才发现，那时错误落在 HTTP 层，跟"是哪条提案越界了"
对不上号。

### `loop.py` — 提案闭环与 `TrainBench`

固定阶段：**诊断 → 提案 → 干跑预览 → 自证评测 → 门禁 →（非 dry_run 时）建草稿并提交待审**。

刻意**不是**一个自由的 ReAct 循环：模型能决定"提什么"，不能决定"跳过哪一步"。
循环终止于 `SUBMITTED`——`approve` / `publish` / `rollback` 不在能力范围内。

#### 为什么要"自证"

旧实现的 `run(baseline, candidate_eval)` 要求调用方把两份评测结果喂进来，于是 Agent 并没有
自证能力——它只是在别人给的数字上过一遍门禁。谁给的数字、数字怎么来的、跟提案里那份配置是
不是同一份，全在闭环之外，无法审计。

现在候选那一侧由 Agent 自己跑：提案里的 `StrategyConfig` 对象**直接**送进
`evaluate_candidate`，因此"被门禁判定的配置"和"将要提交审批的配置"在代码上是同一个对象，
不可能错位。

#### `TrainBench`：自证用的评测台

三条方法学选择：

1. **自证只在 train 上做，`holdout` 在本模块里根本没有取数路径。** `select_train_rows()`
   会显式断言选中的 `query_id` 与 `holdout_ids` 无交集；基线一侧还会核对文件里的
   `split == "train"` 与 `query_ids_sha256`。就算有人把 `baseline-v7-holdout.json` 传进来，
   也会当场 `BaselineMismatch` 而不是静默比出一个数字。
2. **基线直接读 `experiments/baseline-v7-train.json`，不重跑。** 省一轮时间是次要的，主要是
   保证"基线"这个参照点在所有提案、所有提案器之间**逐位相同**；每次重跑都引入一份新噪声，
   两个提案器就不再是在同一把尺子上比。读进来后对三件事失败关闭：文件是 train 侧、查询集
   指纹与 manifest 一致、基线权重与线上 current strategy 一致（否则"候选 vs 基线"的差里混进了
   未记录的策略漂移）。
3. **评测子集是 train 置换序的前 N 条**（`DEFAULT_EVAL_SAMPLE = 700`，`0` 表示整个 train）。
   因为 `build_split()` 里 train 已经是"按 `sha256(f'{seed}|{query_id}')` 排序"的置换序，
   "前 N 条"同时具备三条性质：**确定性**（跨机器跨进程同一批 id）、**无偏**（哈希序的前缀，
   与查询本身的任何属性无关）、**嵌套**（N=700 的子集是 N=1400 的前缀，加大 N 不换掉已有样本，
   历史产物仍可对照）。不用 `random.sample(seed=...)` 的理由与 `splits.py` 相同。

#### `diagnose()` 与 `_train_only()`：证据侧的泄漏防线

`zero_result_queries` / `low_quality_queries` 读的是**线上分析表**，那张表按流量记录，
**不认识 train/holdout 的划分**。不滤的后果实测发生过：模型拿到 holdout 查询当证据，写出
逐字钉死该查询的 `rewrite_rule`。本轮评测只在 train 上做，所以门禁判定没被污染；但这类提案
一旦拿去 holdout 验收，就等于"用考题训练"，那个数字再也不能用了。

`_train_only()` 按 `query_id` 与查询文本双路匹配（分析表里的行不一定带 id），
**两者都对不上的行一律丢弃**——宁可少给证据，也不给来历不明的证据。被丢掉的行数记进
`self.diagnosis_dropped`，让这条防线可审计。

#### 两种分析并列，只有一种能决定门禁

符号空间的提案效应是**稀疏**的（一条 `rewrite_rule` 最多影响一条查询），于是"整体均值上什么
都没发生"与"20 条被显著改善、10 条被改坏"这两种完全不同的事实，在全子集配对检验里长得
一模一样。所以 `assess()` 在门禁之后额外算一份**受影响子集**：

- **(a) 整体配对检验** —— 预注册、全评测子集、BH 校正、**门禁的唯一依据**；
- **(b) 受影响子集分析** —— **事后条件化**，只作描述性证据，`used_by_gate` 恒为 `false`，
  并原样带上 `stats.POST_HOC_CAVEAT` 全文。

门禁判定不读 (b) 的任何字段。这不是"暂时没接上"：子集的选中条件恰好是"这条查询动了"，
零差值样本被系统性排除，差值天然远离零——接上就等于挑一个对自己有利的切片。

**失败隔离**：(b) 的计算包在自己的 `try` 里，出错只写进 `affected_subset_error`，不改变 (a)
的结论也不改变提案的 `status`。描述性分析永远不该有能力改变门禁结局。

#### 产物

每次 `run()` 写一份 `experiments/propose-{提案器}-{run_tag}.json`，schema 为
`searchops.propose-run/2`（相对 `/1` 是**纯增量**，`schema_supersedes` 原样写进产物）。

**模块内不取系统时间。** 文件名里塞一个 wall clock 会让"同参数重跑得到同一份产物"失效，
也会让两次运行无法按内容比对。`run_tag` 由调用方传入，不传就按目录里已有的同提案器产物数
递增（`run001`、`run002`…）。产物里唯一的时间量是 `eval_seconds`（`perf_counter()` 的单调差值，
是耗时不是时刻），它不参与命名，也不参与任何选择。

`FIELD_NOTES` 把"这份 p 值不能当预注册结论用"这类限定条件**写进产物本身**——下游读的是 JSON，
限定必须跟着数据走，不能靠读代码的人记得。同理，`_affected_payload()` 里 `post_hoc` /
`used_by_gate` / `caveat` 三个字段是**故意冗余**的：下游可能只读其中任意一个，限定条件必须在
每条读取路径上都撞得到。

### `eval/stats.py` — 配对统计检验

存在理由不是"课上教过"，而是：搜索策略之间的指标差常常落在噪声范围内，只报均值会把随机波动
当成改进。逐查询指标让配对检验成为可能——同一批查询在两个策略下的表现是**配对样本**。
只依赖 numpy，不引入 scipy。

三个基本量：

- `paired_bootstrap()` —— 对配对差值做 bootstrap，重采样的单位是"查询"，保留配对关系；
- `permutation_test()` —— 随机翻转每个查询上差值的符号构造零分布（零假设"策略标签与结果无关"
  下的等价重排），`+1` 平滑避免在有限迭代下报出 `p=0` 这种不可能的精度；
- `cliffs_delta()` —— 非参数效应量，对指标分布形态不敏感。

#### 判定口径：**三个各自具名的判据**

这是整个模块最重要的一节。`p_value` 与 `[ci_low, ci_high]` 出自两套不同的重采样过程，
零分布也不同，在效应贴近阈值时**不必然一致**（实测反例见
`experiments/rerank-holdout-verdict.json` 的 `depth_ladder`）。因此：

| 判据 | 定义 | 用在哪 | 方向 |
|---|---|---|---|
| `PairedResult.significant` | `p < ALPHA`（仅此一条） | 报告口径 | 说得清楚 |
| `promotion_evidence()` | BH 通过 **且** `p<0.05` **且** CI 不跨零（**合取**） | 门禁主指标 | 难以判定为"提升" |
| `harm_evidence()` | `p<0.05` **或** CI 不跨零（**析取**，不做 BH） | 门禁护栏指标 | 容易判定为"劣化" |

- **报告口径只看 p**：因为"显著"在统计学里有约定含义，而摘要里紧挨着标签打印的正是 p 值；
  标签与它相矛盾，读者无法自洽解读。历史上这里写的是"CI 不跨零"，于是边界情形被误标为显著。
- **门禁不复用报告口径**：报告要的是"说得清楚"，门禁要的是"失败关闭"。在 p 与 CI 分歧的
  边界带上没有理由押注其中一个是对的——正确的动作是不晋级、去拿更多数据。代价是功效降低
  （假阴性），但对一个只能提案、必须由人批准的门禁来说，假阴性远比假阳性便宜。
- **护栏方向必须相反**：若护栏也用合取，等于"更难证明被打坏"，反而更容易放行——方向错了。
  护栏不做 BH：BH 压的是假阳性，而在护栏方向上多拦一次正是我们愿意付的代价。

分歧时 `verdict_label` **显式打印"边界"**并说明分歧方向，而不是悄悄按 p 值下结论。

#### 受影响子集分析（模块后半段）

`affected_analysis()` 先机械算出哪些查询的指标真的动了（容差比较，`AFFECTED_TOLERANCE = 1e-9`），
再在那个子集上跑**同一套** `compare()`（同一 bootstrap / 同一置换检验 / 同一随机种子）——
两套结论之间唯一的差别必须是样本集合，不能是统计实现或随机流。

- 容差取 `1e-9` 不是拍脑袋：k=10 下指标的**最小可实现变化**在 `1e-3` 量级，而 JSON 往返与
  双精度求和的噪声在 `1e-15` 量级；`1e-9` 落在两者中间六个数量级的空档里，结论对容差不敏感。
  **绝不用 `==` 比较浮点。**
- `AffectedSubset.post_hoc` **恒为 True 且没有关掉它的入口**——这个字段的用途就是让产物里
  永远带着这条限定，而不是靠写报告的人记得加一句。
- 子集上的 p 值**不做 BH 校正**：BH 控制的是一个预先定好的检验族的 FDR，而这个检验根本不在
  预注册族里；给它套一个 BH 会让它看起来像门禁族的一员。
- `win_loss()` 单独数涨跌——均值会把"20 涨 10 跌"抹成一个小正数，这里不抹。
- `zero_result_flips()` 单独报告、**不**并入子集定义：两侧指标都为 0 时它是唯一能看见
  "结果换了一批"的信号。
- `MetricMissing` —— 逐查询比对时缺指标直接报错，不猜、不跳过。

### `eval/gate.py` — 晋级门禁

`GatePolicy` 默认：主指标 `ndcg10`，护栏 `("recall10", "mrr10")`，zero-result 率上升容忍
**0.0**（零容忍），主指标跌幅超过 `catastrophic_drop = 0.30` 的查询占比上限
`max_catastrophic_ratio = 0.10`（**均值可以掩盖长尾崩塌**，所以要单独查）。

门禁的判定顺序与三条判据前面已述。这里单说 `MIN_AFFECTED`：

```python
MIN_AFFECTED = 5
```

配对置换检验的零分布**只由非零差值决定**：受影响 k 条时只有 `2^k` 种符号组合，因此可达的
最小 p 是 `1/2^k`。k=3 时下限 0.125、k=4 时 0.0625，都大于 α=0.05——也就是说**无论候选策略
多好，检验都不可能判显著**。

这类情形必须与"测过了，没通过"分开报，于是 `GateDecision.verdict` 有三个取值：
`PROMOTE` / `BLOCK` / **`INSUFFICIENT_EVIDENCE`**。两者都返回 BLOCK 的话，门禁就重演了本项目
一路在修的同一个缺陷：把两种成因不同的状态塌缩成同一个信号（`AiRewriteStatus` 的由来也是
这个）。运维看到 BLOCK 会以为提案被否决了，实际是这批证据根本不足以裁决它。

### `eval/splits.py` — train / holdout 划分

从全量 10000 条里抽一个**固定的** train / holdout：train 用来搜参数、可以反复看；
holdout 只在最后验收时看一次。三条设计选择：

**(1) 只保留至少有一条 E/S 标注的查询**（与 `EvaluationService.relevant()` 同口径）。
一条查询若没有任何 E/S，则 `relevantTotal=0 → recall10 恒为 0`，`IDCG=0 → ndcg10 恒为 0`：
无论排序怎么变这两个指标都不动。把它们放进评测集等于往分母里灌常数——既稀释真实效应量，
又在配对差值向量里塞进一堆结构性的 0，把 sd 压小、把置信区间做窄，得到虚假的"稳"。
代价写清楚了：排除之后这个集合不再覆盖"本来就该返回空"的查询，zero-result 类问题必须靠
别的监控看。（实测本仓库的 `queries.jsonl` 全部满足该条件，实际排除 0 条；过滤仍然保留，
因为它是评测集的**语义前提**，不是一次性的数据清洗动作。）

**(2) 划分不用 `random`，用 `query_id` 的 SHA-256 排序。**
`random.Random(seed).shuffle()` 的复现性依赖 CPython 的 RNG 实现细节；而
`sha256(f"{seed}|{query_id}")` 只依赖种子和 id，跨解释器版本、跨机器、跨语言都一样。
附带好处：置换稳定，只增大 `total` 时前面的样本不重排。反过来也成立——改 `total` 或
`train_ratio` 会改变两侧成员，所以这两个值也一并记进 manifest，**不能只记种子**。

**(3) 规模 2000（train 1400 / holdout 600）。** 两端约束都实测过：一端是扫描的墙上时间预算，
另一端是配对检验在 α=0.05 / power=0.80 下的最小可检出差。文件 docstring 里列了各规模下的
具体数值与"如果只用 600/300 会怎样"的对照。

配套设施：

- `Split.manifest()` 写出两侧的 `query_ids_sha256`、成员 id 全表、过滤口径与分配规则说明；
  **`generated_at` 由调用方传入**，模块内部不取系统时间，否则同种子重跑会产出不同文件。
- `load_split()` 走的是**同一条** `build_split` 路径再比对指纹，所以它既是加载器也是复现性
  检查：哈希对不上就说明 `queries.jsonl` 变了或参数被改过，直接报错，绝不静默返回一个
  "看起来像"的集合。
- `build_baselines()` 走 `evaluate_candidate`（DRY_RUN）而不是 `/evaluations/run`，因此可以
  随时重跑而不污染策略历史；跑之前会跟线上 current strategy 对一次权重，不一致就报错。
- CLI 的 `--out` 由调用方给，模块本身不假设写到哪儿——`baselines/` 下的历史基线是**不可再生
  资产**，绝不能被这条命令覆盖。

### `eval/sweep.py` — BM25 字段权重扫描

存在理由：后面要上 AI。如果不先把 BM25 自身的调参空间榨干，AI 带来的任何提升都无法归因——
本该属于"把 `bullet_point` 调高一点"的收益会被算到模型头上。

**参数化成"相对 title 的比值"而不是绝对值。** 查询是 `multi_match` / `best_fields` 且没设
`tie_breaker`，所以 `score(d) = max_f(w_f · bm25_f(d))`；把整个权重向量乘以任意正数只是把
所有文档的分数同乘一个常数，**逐位不改变排序**。这是实测出来的（v7 权重 ×2.5 后指标十位
小数完全一致），不是推出来的。于是 title 钉死在 4.0，只搜另外四个字段的比值 `r_f`——搜索
空间从 5 维降到 4 维，而且**每个点都对应一个不同的排序**，不会把大量互为缩放的等价配置
反复评测一遍还误以为它们"稳定复现"。

`r_f = 0` 是一个**语义不同**的点，不是"很小的权重"：`SearchQueryCompiler` 里有
`.filter(entry -> entry.getValue() > 0)`，权重为 0 的字段会被整个从 `fields` 列表里删掉，
于是"只在该字段命中"的文档不再被召回——0 改变的是**召回集合**，正数只改变**排序**。

四段搜索，每段回答一个问题：A 单轴响应曲线（有没有头部空间、峰在哪）→ B 坐标下降（轴序按
A 段实测敏感度降序）→ C 对数箱内准随机采样（补坐标下降抓不到的轴间交互）→ B2 再收敛 →
D 乘性微调（确认是局部极值而不是网格粗粒度的假象）。全网格不可行，所以分段。

纪律与自检：

- **`sweep` 子命令这条路径根本不构造 holdout 的查询体**，holdout 由 `verify` 子命令处理。
  纪律写进代码结构，而不是写进注释里靠人自觉。
- `_verify_cmd` 走 `evaluate()` 而不是 `_evaluate_one()`，因为前者会把这次评测**写进 JSONL
  日志**——holdout 只允许看一次，那一次必须留下痕迹，否则"看过几次"无从审计。
- 每评完一组就往 JSONL 追加一行并 `flush`：扫描要跑几十分钟，中途挂掉不能把前面的真实测量
  一起丢掉。
- 返回的 `strategy_version` 必须是哨兵 `-1`，否则说明这轮没走候选路径，直接拒绝把它当成
  干跑结果。
- 冠军配置**重跑一次**拿完整逐查询数组，并要求聚合值逐位对上（评测是确定性的）。
- 并发固定 2：`EvaluationService.run` 整个方法是 `@Transactional`，一轮评测占住一条 Hikari
  连接（池 8）约 20 s。再往上就是拿整个环境的可用性换扫描速度。
- 报告里带 **`caveat_winners_curse`**：best 是在 N 组候选里按 train 指标取的 argmax，该数值
  向上有偏，train 上那个 p 值也不可信（检验用的数据正是挑选用的数据）。**唯一有效的证据是
  holdout 上那一次配对检验。**

### `eval/loader.py`

读 platform 产出的评测 JSON（`load_run` / `by_query_id` / `headline`）。
`DEFAULT_LATEST` 指向 `platform/data/processed/evaluation-latest.json`——注意
`make evaluate-ai` 写的是 `evaluation-ai-latest.json`，两者不互相覆盖，所以 `load_latest()`
永远读的是无 AI 那一份。

---

## 测试

```bash
cd agent && .venv/bin/python -m pytest -q      # 26 条
```

| 文件 | 守的性质 |
|---|---|
| `tests/test_governance_boundary.py` | 危险方法不在注册表；暴露的工具都在自动化上限内；三个特权方法的等级没被人偷偷降级；未登记方法按最高危处理；把 `publish` 加进白名单必须炸；写操作有幂等键参数；`evaluate_candidate` 是 DRY_RUN；诊断证据不含 holdout 查询 |
| `tests/test_significance_verdict.py` | 零假设自检（同一份数据比自身必须不显著）；`significant` 的定义就是 `p < ALPHA`；CI 单独报告不参与合成；两个方向的边界数据；晋级判据是合取、护栏判据是析取；门禁在两种边界数据上都 BLOCK；两个过程都干净时仍然放行；数值按位钉死；`INSUFFICIENT_EVIDENCE` 与 `BLOCK` 分离 |

两条值得单独说：

- `test_diagnosis_evidence_never_includes_holdout_queries` 用的是**真实泄漏过的那两条
  `query_id`（75 与 288）** 做回归，不是构造的例子。
- `test_numeric_outputs_are_pinned` 把 Δ / CI / p / 效应量按位钉死。口径标签可以改，
  重采样逻辑的"顺手优化"不许悄悄改数——一改这条就红。

**CI 里不得依赖任何 API key**：`LLMProposer` 的构造在缺 key 时抛异常（这是刻意的），
所以涉及模型的路径必须打桩，不调真实模型。

---

## 产物与实验

| 命令 | 写出 |
|---|---|
| `python -m searchops_agent.eval.splits --out … --baseline` | `split-manifest.json`、`baseline-v7-train.json`、`baseline-v7-holdout.json` |
| `python -m searchops_agent.eval.sweep sweep …` | `sweep-train-log.jsonl`、`sweep-train-report.json`、`best-config.json`、`sweep-best-train.json` |
| `python -m searchops_agent.eval.sweep verify …` | `sweep-holdout-log.jsonl`、`sweep-best-holdout.json` |
| `searchops-agent propose …` | `propose-{proposer}-{run_tag}.json` |

`experiments/` 下其余产物（`*-verdict.json`、`rerank-holdout-*`、`rewrite-holdout-*`、
`control-*`）**不由本包的任何代码路径写出**——包内没有这些文件名的写入点。它们由一次性驱动
脚本产生，内容形状与 `compare` / `gate` 的结论对齐。

`experiments/` 与 `baselines/` 下的文件是实测归档，任何工具都不应覆盖它们。

---

## 已知的边角

- **`SymbolicRuleProposer` 没有接进 CLI。** `cli.py` 的 `--proposer` 只有 `rule` / `llm`，
  `searchops_agent/__init__.py` 也没导出它。它通过 `from searchops_agent.proposers import
  SymbolicRuleProposer` 以编程方式驱动（对照实验产物见
  `experiments/propose-symbolic-ctl20260816-symbolic.json`）。
- **`RuleProposer` 的类 docstring 列了三类失败模式，实现发射两条提案。** 读代码时以代码为准。
- **`select_train_rows()` 的交集断言与 `TrainBench.load()` 的 `split == "train"` 校验目前
  没有专属单测**——它们是代码里的失败关闭，证据侧的那条防线（`_train_only`）才有回归测试。
