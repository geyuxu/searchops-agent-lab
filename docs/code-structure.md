# 代码结构

这份文档回答三个问题：**代码放在哪、按什么顺序读、哪些性质不能被改坏**。

它**不**重复以下内容，需要时请直接跳转：

| 想知道 | 去看 |
|---|---|
| 系统上下文、服务边界、端口与健康探针、失败行为 | [`platform/docs/architecture.md`](../platform/docs/architecture.md) |
| 两条运行路径的具体命令、健康检查、故障恢复 | [`platform/docs/runbook.md`](../platform/docs/runbook.md) |
| 接真实模型、超时、`ai_status` 语义、环境变量 | [`platform/docs/ai-handoff.md`](../platform/docs/ai-handoff.md) |
| ESCI 数据来源与"哪些字段是模拟的" | [`platform/docs/data-provenance.md`](../platform/docs/data-provenance.md) |
| 为什么这样选运行时 / 治理模型 / 采样方式 | [`platform/docs/adr/`](../platform/docs/adr/) 三篇 |
| 未来 MCP server 的工具表与安全等级 | [`platform/docs/mcp-server-design.md`](../platform/docs/mcp-server-design.md) |
| `agent/` 每个模块的完整设计说明 | [`agent/README.md`](../agent/README.md) |

---

## 1. 仓库全景

```
searchops-agent-lab/
├── platform/      电商搜索系统本体：Java 搜索服务、Medusa 商城、两个 Next.js 应用、
│                  FastAPI AI 适配器、ESCI 数据流水线、docker-compose 编排、Makefile
├── agent/         Python 包 searchops_agent：工具注册表与安全分级、离线评测统计、
│                  晋级门禁、提案闭环
├── experiments/   留出集清单、基线、扫描日志、重排与改写的实测产物（只读归档）
└── baselines/     归档的参照评测，带 sha256（不可再生资产）
```

两个目录的分工可以用一句话概括：**`platform/` 决定"系统能做什么"，`agent/` 决定
"自动化被允许做什么、以及凭什么判断它做对了"。** 项目最核心的两条主张——结构化权限边界与
统计晋级门禁——都在 `agent/` 里。

`experiments/` 与 `baselines/` 是实测归档，任何代码路径与任何工具都不应覆盖它们。

---

## 2. 读代码的入口顺序

按这个顺序读，每一步都能独立回答一个问题；跳着读会在第 6 步卡住。

| # | 读什么 | 读完能回答 | 量级 |
|---:|---|---|---|
| 0 | `README.md` → `platform/docs/architecture.md` | 这个项目在证明什么？系统由哪些进程组成？ | 10 分钟 |
| 1 | `agent/searchops_agent/safety.py` → `tools.py` → `agent/tests/test_governance_boundary.py` | **"Agent 可提案不可批准"凭什么是代码事实？** | 232 行 |
| 2 | `agent/searchops_agent/client.py` + `models.py` | 这些能力在 HTTP 上长什么样？幂等键从哪来？ | 250 行 |
| 3 | `platform/…/api/ToolGatewayController.java` + `service/StrategyService.java` | 服务端的另一半：谁真正强制 `approval_token`、谁写审计 | 300 行 |
| 4 | `agent/searchops_agent/eval/stats.py` → `eval/gate.py` → `agent/tests/test_significance_verdict.py` | **"显著"是什么意思？门禁凭什么放行？** | 1000 行 |
| 5 | `agent/searchops_agent/eval/splits.py` | 调参用哪批数据、验收用哪批、怎么保证不串 | 417 行 |
| 6 | `agent/searchops_agent/loop.py` | 1–5 怎么串成一次可审计的提案 | 913 行 |
| 7 | `agent/searchops_agent/proposers.py` + `prompts.py` | 候选从哪来？模型输出凭什么可信？ | 1580 行 |
| 8 | `platform/…/service/SearchQueryCompiler.java`、`EvaluationService.java`、`RerankOrdering.java` | 引擎侧机制——**它决定了第 7 步那些守卫的形状** | 500 行 |
| 9 | `agent/searchops_agent/eval/sweep.py` + `experiments/sweep-train-report.json` | 纯调参的天花板在哪、为什么后面要上 AI | 711 行 |
| 10 | `platform/Makefile` + `platform/docs/runbook.md` | 怎么把这套东西跑起来 | — |

三点提醒：

- **第 1 步只要 232 行**（其中实现 125 行、测试 107 行），却是整个项目的立论点。
  如果只有一刻钟，就读这一步。
- **第 7 步放在第 8 步之前是有意的**：先看守卫在防什么，再看引擎为什么让这些守卫成为必要，
  两遍下来 `_Compiler` 的每一条判据都会对上号。若顺序反过来，读 `proposers.py` 时会觉得
  那些"子串包含 / 整串相等"的检查凭空冒出来。
- **跑起来是第 10 步，不是第 0 步。** 上面 1–9 步全部可以离线读；只有第 5、6、9 步的**执行**
  需要搜索服务在跑，而且第 5 步还需要 `platform/data/processed/queries.jsonl`（被
  `.gitignore` 排除，由 `make data` 重建）。

---

## 3. `agent/` 模块地图

> 每个模块的完整设计说明（含实测依据、被否决的方案与代价）见 [`agent/README.md`](../agent/README.md)。
> 这一节给的是模块之间的关系与每个模块存在的**唯一理由**。

```
                 ┌──────────────┐
                 │  proposers   │  候选从哪来（rule / symbolic / llm，同一套守卫）
                 └──────┬───────┘
                        │ Proposal(config, evidence, guard_notes)
                        ▼
   safety ──▶ tools ──▶ loop ──▶ eval/gate ──▶ PROMOTE / BLOCK / INSUFFICIENT_EVIDENCE
     │          │        │           │
     │          │        │           └── eval/stats  （检验与判据）
     │          │        └── eval/splits（train 子集 + 基线 = TrainBench）
     │          └── client（HTTP + 幂等键）
     └── 分级元数据挂在 client 的每个方法上
```

| 模块 | 做什么 | 为什么是这样，而不是别的样子 |
|---|---|---|
| `safety.py` | 五级安全枚举 + `@safety` 装饰器 + `MAX_AUTOMATED` | 分级挂在**函数对象**上才可内省，写在 Markdown 里的分级表没人能断言。`safety_class_of()` 对未登记函数返回**最高危**——失败关闭，新加的方法默认进不了自动化 |
| `tools.py` | 从 `ALLOWED` 白名单 + 等级上限构建注册表 | 白名单的失效方式是"新功能不生效"（会被立刻发现），黑名单的失效方式是"忘了加进去"（不会）。`FORBIDDEN` 与白名单逻辑冗余，保留是为了让越界在测试里**立刻显形**，而不是等运行期 |
| `client.py` | `/api/v1/tools/*` 的方法级封装，写操作自带 `Idempotency-Key` | 幂等键默认由 `request_id` 派生，重试不重复建单。`approve`/`publish`/`rollback` 留在客户端供人类与集成测试走完整生命周期，隔离由 `tools.py` 负责。`evaluate_candidate` 是 `DRY_RUN`——服务端强制不落库，Agent 才敢反复试错 |
| `models.py` | 与 Java `ApiModels` 对齐的 pydantic 模型 | 不做 snake/camel 重命名，多一层映射就多一处能悄悄漂移的地方 |
| `prompts.py` | 提示词 + `NEGATION_CUES` | 词表由同一个 frozenset 渲染进提示词**并**被代码守卫使用——模型看到的禁令与代码执行的检查是同一份列表，不会分叉 |
| `proposers.py` | 三个提案器 + `_Evidence` + `_Compiler` | 三臂的证据窗口、输出形状、守卫、条数上限、闭环**全部相同**，唯一差别是"提案从哪来"——任何别的差别都会让对照失效。模型够不着 `field_weights`：不在 schema 里，不是提示词里请它别碰 |
| `loop.py` | 固定阶段的提案闭环 + `TrainBench` + 产物 | 不是自由 ReAct：模型能决定"提什么"，不能决定"跳过哪一步"。提案里的 `StrategyConfig` **对象**直接送去评测，所以"被判定的配置"与"将提交的配置"不可能错位。模块内不取系统时间，否则"同参数重跑同一份产物"就是空话 |
| `eval/stats.py` | 配对 bootstrap / 置换检验 / Cliff's delta / BH / 受影响子集 | **三个各自具名的判据**：报告口径只看 p；晋级用合取；护栏用析取。同一个"显著"在两个方向上的失败关闭方向相反，共用一个布尔就必然在某一侧放松 |
| `eval/gate.py` | 晋级判定 | 三值结论 `PROMOTE` / `BLOCK` / `INSUFFICIENT_EVIDENCE`。把"无法裁决"塌缩成"已被否决"是本项目一路在修的同一类缺陷 |
| `eval/splits.py` | 固定的 train(1400) / holdout(600) 划分 | 用 `sha256(f"{seed}|{query_id}")` 排序而不是 `random.shuffle`：后者的复现性依赖 CPython RNG 实现细节。`load_split()` 走同一条构造路径再比指纹，所以它同时是加载器和复现性检查 |
| `eval/sweep.py` | BM25 权重扫描 | 只搜"相对 title 的比值"——等比缩放权重向量在 `best_fields` 下是恒等变换（实测），搜绝对值等于把大量等价配置反复评测。`sweep` 子命令**根本不构造 holdout 的查询体** |
| `eval/loader.py` | 读 platform 产出的评测 JSON | `DEFAULT_LATEST` 指向 `evaluation-latest.json`；`make evaluate-ai` 写的是 `evaluation-ai-latest.json`，两者不互相覆盖 |
| `cli.py` | `tools` / `show` / `selfcheck` / `compare` / `gate` / `propose` | `selfcheck` 是整套检验的体温计：同一份数据与自身比较必须全部不显著 |

### `agent/tests/`（26 条）

- `test_governance_boundary.py` —— 治理边界与证据隔离的可执行断言。
- `test_significance_verdict.py` —— 显著性口径、两个方向的边界数据、门禁三值结论、数值钉桩。

---

## 4. `platform/` 导航

> 服务职责、端口、健康探针、失败行为的**权威描述**在
> [`platform/docs/architecture.md`](../platform/docs/architecture.md)。
> 这里只回答"我要找的东西在哪个文件"。

```
platform/
├── services/
│   ├── search-service/     Java 21 · Spring Boot · 系统的中枢
│   ├── ai-adapter/         FastAPI · 可选 AI 边界（改写 / 重排 / 策略建议）
│   └── commerce/           Medusa · 购物车与模拟订单
├── apps/
│   ├── storefront/         Next.js :3000
│   └── operations-console/ Next.js :3001
├── packages/
│   ├── api-contracts/      OpenAPI / JSON Schema / TS / Java 契约与示例
│   └── ui/
├── data/                   ESCI 流水线（下载 → 处理 → 校验 → 播种 → 评测）
├── infra/                  docker / elasticsearch 配置
├── scripts/                doctor · bootstrap · wait-healthy · test-unit · test-integration · clean-local
├── tests/                  e2e（Playwright）· integration_policy.py
├── docker-compose.yml      8 个服务
├── Makefile                up/down · infra-up + dev-* · seed · test-* · evaluate/evaluate-ai
└── dev.env / .env.example  开发路径的连接串覆盖 / 配置模板（.env 含密钥，已 gitignore）
```

### `services/search-service`（Java 21 / Spring Boot，Jackson 3）

| 包 | 关键类 | 职责 |
|---|---|---|
| `api/` | `SearchController` · `StrategyController` · **`ToolGatewayController`** · `EvaluationController` · `OperationsController` · `IndexController` · `ApiExceptionHandler` | HTTP 边界。`ToolGatewayController` 就是 `/api/v1/tools/*`——Agent 唯一的入口 |
| `service/` | `ProductSearchService` · **`SearchQueryCompiler`** · `ElasticsearchGateway` · **`StrategyService`** · `StrategyRepository` · **`EvaluationService`** · `SearchAnalyticsRepository` · `AiAdapterClient` · **`RerankOrdering`** | 检索、策略编译、治理状态机、离线评测、分析表、AI 客户端、重排合并 |
| `domain/` | `ApiModels` · `StrategyConfig` · `AiRewriteStatus` · `AiRerankStatus` | 线格式与两条独立的降级状态机 |
| `config/` | `HttpConfig` · `SearchProperties` · `RerankProperties` · `RequestIdFilter` | RestClient / 超时 / 重排预算 / 请求 ID |

读这四个类就能理解系统的大部分行为：

- **`SearchQueryCompiler`** —— 策略如何变成一个 `multi_match`。`applyRewrite` 是**整串相等**
  替换，`expandSynonyms` 是**子串包含**追加，`fieldWeights` 里 `>0` 的过滤会把权重为 0 的字段
  整个从 `fields` 里删掉。这三条机制直接决定了 `agent/searchops_agent/proposers.py` 里每一条
  守卫的形状，也决定了 `sweep.py` 为什么把 `r_f=0` 当成语义不同的点。
- **`EvaluationService`** —— 指标定义（E=3 / S=2 / C=1，`relevant()` 只认 E/S）与候选评测。
  `CANDIDATE_STRATEGY_VERSION = -1` 是"这不是任何已发布版本的成绩"的诚实标记；候选评测
  **强制不落库**（理由：`quality_metrics` 的唯一键是 `(query_id, strategy_version)`，候选没有
  版本号，允许落库就能用一次参数扫描把整张质量基线表覆盖掉，且不可逆）。
- **`StrategyService`** —— `DRAFT → IN_REVIEW → APPROVED → PUBLISHED` 状态机、HMAC 生成的
  `approval_token`、`(key, operation)` 幂等表、每次转换写审计。**这里是权限边界的最终强制点**：
  即使有人绕过 Agent 侧的注册表直接调 HTTP，`publish` 仍然要过 `verifyToken`。
- **`RerankOrdering`** —— 见下一节的不变式。

测试在 `src/test/java/lab/searchops/`，15 个类，其中 `StrategyWorkflowIntegrationTest`
用 Testcontainers 起 PostgreSQL 跑完整治理生命周期。

### `services/ai-adapter`（FastAPI）

- `app/main.py` —— `/ai/query-rewrite`、`/ai/rerank`、`/ai/strategy-suggest`，外加
  `/ai/health`、`/ready`、`/metrics`。三条路由都带 `response_model_exclude_none=True`，
  使"没有值"在线格式上与 Java 侧 `default-property-inclusion=non_null` 完全一致。
- `app/models.py` —— `StrictModel` 是 `extra="forbid"`；`AiAdapterClient` 里的一组字符长度
  上限就是照着这份契约裁的（任何一条超限都会让整次调用被 422 拒掉，于是"某个商品描述特别长"
  这种数据问题会伪装成 AI 故障）。
- `app/provider.py` + `app/providers/` —— `echo_upper`（确定性 mock，不需要 key）、
  `langchain_rewrite`、`langchain_rerank`。`langchain_rewrite.py` 里的 `_NEGATION_CUES`
  与 `agent/searchops_agent/prompts.py` 的 `NEGATION_CUES` 逐字相同。
- 测试 8 个文件，覆盖率门禁 `--cov-fail-under=85`（配置在 `pyproject.toml` 的 `addopts`）。

### `apps/`

两个 Next.js 应用都是**薄 BFF**：浏览器只跟自己的 `app/api/**/route.ts` 说话，由它转发到
`SEARCH_SERVICE_URL` 或商城，并在上游不可达时返回结构化的 503。

- `storefront/` —— `app/api/search`、`app/api/products/[id]`、`app/api/commerce/[...path]`；
  组件 `Storefront.tsx` / `ProductCard.tsx` / `ProductArt.tsx`；`lib/cart.ts` 存设备本地购物车 ID。
- `operations-console/` —— 单个 `app/api/searchops/[...path]` 通配转发（**会透传
  `Idempotency-Key`**，治理写操作因此能从后台发起）；界面主体是 `components/ControlRoom.tsx`。

### `data/`

`scripts/` 下按流水线顺序：`download.py` → `process.py` → `validate.py` → `seed_commerce.py`
→ `simulate_traffic.py` → `evaluate.py`，外加三个包装 `run-data.sh` / `seed.sh` / `evaluate.sh`。

两件必须知道的事：

- `processed/` 被 `.gitignore` 排除（可由 `make data` 重建）。`agent` 侧
  `eval/splits.py` 的默认查询源与 `eval/loader.py` 的 `DEFAULT_LATEST` 都指向这里。
- `evaluate.py` 默认写 `evaluation-latest.json`，**跑第二次会覆盖前一次**；
  `make evaluate-ai` 写的是 `evaluation-ai-latest.json`，两者不互相覆盖，A/B 可直接对比。

---

## 5. 关键不变式

这四条是"改坏了系统就不再成立"的性质。每条都给出**代码位置**与**守护它的测试**。

### 5.1 重排的排列不变式

> **输出永远是输入候选集的一个排列。** 无论模型返回什么——少返、多返、集外 id、重复 id、
> 空串、大小写被改坏、整个列表是 `null`——文档集合（含重复度）与输入完全相同。

**代码**：`platform/services/search-service/src/main/java/lab/searchops/service/RerankOrdering.java`

合并按**下标**而不是按 id 做：用 id 做集合运算在候选集出现重复 id 时会悄悄丢文档（去重 = 丢
文档），用下标则天然保证结果是输入的排列——每个下标从队列里弹出后不再放回，未被点名的下标
在第二趟按原序补齐。因此末尾的 `ordered.size() != candidates.size()` 检查就足以证明"每个下标
恰好出现一次"，不成立时抛 `IllegalStateException`。

**为什么不可妥协**：重排只对顺序负责，对召回不负任何责任。一旦它能让文档消失，模型的一次
幻觉就变成一次**静默的召回下降**——用户搜不到本来搜得到的商品，评测里表现为 Recall@10 下跌，
而故障形状与"BM25 根本没召回"一模一样，排查会被引向索引和查询编译，没人会怀疑到重排头上。
反过来，只要集合恒等，重排最坏也只是"顺序没变好"。**这条不变量还是重排收益可被单独归因的
前提**：候选集不变 ⇒ 指标差异只可能来自顺序，所以留出集 n=600 上 NDCG@10 从 0.4720 到 0.5926
（p=0.0001）才能被读成"重排的收益"。

**测试**：
- `RerankOrderingTest.java` —— 参数化列举各种恶意与畸形输入，外加一个定种子的属性式测试做
  上万次随机组合遍历；不变量断言本体是"输出是输入的排列"。
- `RerankFallbackTest.java` —— 降级路径：读/连超时、连接被拒、未知主机、5xx、422、
  各类不可用响应体、并发预算耗尽，每一种都退回 BM25 顺序并给出**具体的** `AiRerankStatus`；
  其中 `maliciousRankingsNeverChangeTheDocumentSet` 在整条服务链路上复查文档集合不变，
  `brokenPermutationInvariantIsReportedAsInternalErrorNotInvalidResponse` 明确要求
  "不变量被破坏"报成 `INTERNAL_ERROR`（我们自己的缺陷）而不是 `INVALID_RESPONSE`（怪适配器）。
- `AiRerankStatus` 的 javadoc 把这条不变量写进了枚举文档本身。

### 5.2 Agent 权限边界

> **提案者永远拿不到审批与发布能力。** `approve` / `publish` / `rollback` 不是"被劝阻"，
> 而是**不存在于工具注册表中**；越界在注册表构造期抛异常，不留到运行期。

**代码**（三层，任何一层单独失效都拦得住）：
1. `agent/searchops_agent/safety.py` —— 三个方法分别登记为 `PRIVILEGED_WRITE` /
   `TOKEN_GATED_WRITE`，都高于 `MAX_AUTOMATED = GOVERNED_WRITE`；未登记的函数按最高危处理。
2. `agent/searchops_agent/tools.py` —— `build_registry()` 只遍历 `ALLOWED`，并对
   "同时出现在 `FORBIDDEN` 里"与"等级超过上限"两种情况抛 `GovernanceViolation`。
   `loop.py` 全程只通过 `self.tools[...]` 访问客户端。
3. `platform/…/service/StrategyService.java` —— **最终强制点**。即使绕过 Agent 直接发 HTTP，
   `publish` / `rollback` 仍要过 `verifyToken`（HMAC 生成、SHA-256 存哈希、
   `MessageDigest.isEqual` 定时安全比较），且每次转换都写审计。

**测试**：
- `agent/tests/test_governance_boundary.py`：
  `test_privileged_operations_absent_from_registry`、
  `test_every_exposed_tool_is_within_automation_ceiling`、
  `test_approve_publish_rollback_really_are_privileged`（防止有人把危险方法**降级**来绕过上一条）、
  `test_unregistered_method_defaults_to_most_dangerous`、
  `test_registry_rejects_privileged_method_added_to_allowlist`（把 `publish` 塞进白名单必须炸）、
  `test_writes_require_idempotency_key`、
  `test_candidate_evaluation_is_dry_run`（Agent 的自证能力必须是 DRY_RUN 级——能算，但不写
  任何状态；一旦被误标成写入级，Agent 试错就会污染策略历史与审计流）。
- `platform/…/StrategyWorkflowIntegrationTest.java` —— Testcontainers 起 PostgreSQL，
  走完 create → submit → approve → publish → rollback，并断言幂等重放与五条审计记录。

### 5.3 门禁失败关闭

> **任何一条判据缺数据或落在分歧带上，都不放行。** 而"证据不足以裁决"必须与"测过了没通过"
> 分开报。

**代码**：`agent/searchops_agent/eval/gate.py` + `eval/stats.py`

- `MIN_AFFECTED = 5`：配对置换检验的零分布只由非零差值决定，受影响 k 条时可达最小 p 为
  `1/2^k`（k=4 时 0.0625 > α=0.05）。低于阈值时返回 **`INSUFFICIENT_EVIDENCE`** 而不是
  `BLOCK`——否则就是把"这批证据无法裁决该提案"说成"提案被否决"，重演本项目一路在修的那类
  状态塌缩（`AiRewriteStatus` 的由来同源）。
- `promotion_evidence()` 是**合取**（BH 且 `p<0.05` 且 CI 不跨零）：主指标要难以判定为"提升"。
- `harm_evidence()` 是**析取**且不做 BH：护栏要容易判定为"劣化"。两个方向的失败关闭要求
  相反的严格度，共用一个布尔必然在某一侧放松。
- 另外三道无条件检查：主指标 `delta <= 0` 直接拦；zero-result 率上升超过容忍值（默认 **0**）
  拦；主指标跌幅超过 0.30 的查询占比超过 10% 拦（**均值可以掩盖长尾崩塌**）。
- `stats.MetricMissing` —— 逐查询比对时缺指标直接报错，不猜、不跳过。
- `loop.BaselineMismatch` —— 基线文件不是 train 侧 / 指纹不符 / 权重与线上不一致，一律拒跑，
  "宁可不跑，也不比出一个来历不明的差值"。

**测试**（`agent/tests/test_significance_verdict.py`）：
`test_same_data_compared_with_itself_is_not_significant`（零假设自检，整套检验的体温计）、
`test_promotion_evidence_needs_all_three_criteria`、`test_harm_evidence_is_a_disjunction`、
`test_gate_blocks_on_both_kinds_of_boundary_data`、
`test_gate_blocks_when_a_guard_metric_degrades_on_boundary_evidence`（护栏方向的回归：
旧实现在这一档会放行）、`test_gate_separates_undecidable_from_rejected`、
`test_gate_promotes_when_both_procedures_agree`（修的是边界标签，不是把门禁焊死）、
`test_numeric_outputs_are_pinned`（Δ / CI / p / 效应量按位钉死）。

### 5.4 holdout 不参与提案与选择

> **holdout 只在最后验收时看一次。** 它既不进评测子集，也不进提案证据，也不进任何 argmax。

**代码**（四处，覆盖"选择"与"证据"两条路径）：

| 位置 | 挡住什么 |
|---|---|
| `loop.select_train_rows()` | 评测子集与 `split.holdout_ids` 的交集断言 → `BaselineMismatch` |
| `loop.TrainBench.load()` | 基线文件必须 `split == "train"`，且 `query_ids_sha256` 与 manifest 一致 |
| `loop.diagnose()` / `_train_only()` | 线上分析表**不按 split 过滤**，按 id 与文本双路匹配，两者都对不上的行一律丢弃；丢弃计数记进 `diagnosis_dropped` |
| `sweep._sweep_cmd` / `_verify_cmd` | `sweep` 路径根本不构造 holdout 的查询体；`verify` 是唯一读 holdout 的路径，且走 `evaluate()` 以便**留下 JSONL 痕迹**（"看过几次"必须可审计） |

另有 `splits.load_split()` 在每次加载时重建划分并校验两侧指纹——`queries.jsonl` 变了或参数被
改过就直接报错，绝不静默返回一个"看起来像"的集合。

**为什么**：`sweep` 的报告里写着 `caveat_winners_curse`——best 是在 N 组候选里按 train 指标取的
argmax，该数值向上有偏，train 上那个 p 值也不可信（检验用的数据正是挑选用的数据）。
**唯一有效的证据是 holdout 上那一次配对检验。** 证据侧同理：模型拿到 holdout 查询当证据、
写出逐字钉死该查询的 `rewrite_rule`，这件事实测发生过；本轮评测在 train 上做所以门禁没被污染，
但这类提案一旦拿去 holdout 验收，那个数字就再也不能用了。

**测试**：`agent/tests/test_governance_boundary.py::test_diagnosis_evidence_never_includes_holdout_queries`
——用**真实泄漏过的那两条 `query_id`（75 与 288）** 做回归，并断言归属不明的行同样被丢弃。

诚实说明：`select_train_rows()` 的交集断言与 `TrainBench.load()` 的 `split` 校验目前**没有
专属单测**，它们是代码里的失败关闭；有回归测试守着的是证据侧那条防线。

---

## 6. 跨语言契约的对齐点

改动其中任何一侧时，另一侧必须同步——否则失效方式都是"不报错，只是悄悄变错"。

| 对齐点 | 两侧位置 | 不同步的后果 |
|---|---|---|
| 查询归一化 | `SearchQueryCompiler.applyRewrite`（`String.join(" ", trim().split("\\s+")).toLowerCase()`）↔ `proposers._normalize_query()` | "这条 rewrite 会不会触发"在两侧给出不同答案，提案器放行一堆永不触发的规则 |
| 否定词表 | `ai-adapter/app/providers/langchain_rewrite.py` 的 `_NEGATION_CUES` ↔ `agent/…/prompts.py` 的 `NEGATION_CUES` | 适配器放行的改写被 Agent 拦下（或反过来） |
| `StrategyConfig` 上下界 | Java 侧 `@Size/@Min/@Max` ↔ `proposers._Compiler.assert_contract()` 的一组 `ClassVar` | 越界要等 `POST /evaluations/candidate` 返回 400 才发现，错误落在 HTTP 层，跟"是哪条提案越界了"对不上号 |
| 相关性口径 | `EvaluationService.relevant()`（只认 E/S）↔ `splits.RELEVANT_LABELS` | 划分过滤与指标计算各判各的，评测集里混进指标恒为 0 的查询 |
| 传输字段名 | `lab.searchops.domain.ApiModels` 的 `@JsonProperty` ↔ `agent/…/models.py` | 字段静默丢失（pydantic 默认忽略未知键） |
| 适配器字段长度 | `ai-adapter/app/models.py`（`StrictModel` = `extra="forbid"`）↔ `AiAdapterClient` 的 `MAX_*_CHARS` | 一条超长描述让整次调用被 422 拒掉，数据问题伪装成 AI 故障 |
| 候选评测哨兵 | `EvaluationService.CANDIDATE_STRATEGY_VERSION = -1` ↔ `sweep.Evaluator._evaluate_one` 的哨兵检查 | 一轮"其实评测了已发布策略"的结果被当成候选干跑结果归档 |

**Jackson 3 的一条硬性约定**（影响所有新增的请求字段）：Jackson 3 把
`FAIL_ON_NULL_FOR_PRIMITIVES` 的默认值从 `false` 翻成了 `true`，且 Spring Boot 4.1 不恢复
Jackson 2 语义。因此在 `ApiModels` 里**新增可选请求字段一律用包装类型**（`Boolean` / `Integer`
而不是 `boolean` / `int`）。用 primitive 会让缺省该键的既有调用方直接 400，而且运行中的旧镜像
看不出来、一重建才炸。

---

## 7. 命令与产物的对应关系

| 命令 | 位置 | 写出 |
|---|---|---|
| `make data` | `platform/` | `data/processed/{products,queries}.jsonl` + `manifest.json`（**被 gitignore**） |
| `make evaluate` | `platform/` | `data/processed/evaluation-latest.json`（**重跑会覆盖**） |
| `make evaluate-ai` | `platform/` | `data/processed/evaluation-ai-latest.json`（与上一条互不覆盖） |
| `python -m searchops_agent.eval.splits --out … --baseline` | `agent/` | `experiments/split-manifest.json` · `baseline-v7-train.json` · `baseline-v7-holdout.json` |
| `python -m searchops_agent.eval.sweep sweep …` | `agent/` | `experiments/sweep-train-log.jsonl` · `sweep-train-report.json` · `best-config.json` · `sweep-best-train.json` |
| `python -m searchops_agent.eval.sweep verify …` | `agent/` | `experiments/sweep-holdout-log.jsonl` · `sweep-best-holdout.json` |
| `searchops-agent propose …` | `agent/` | `experiments/propose-{proposer}-{run_tag}.json`（schema `searchops.propose-run/2`） |

`experiments/` 下的 `*-verdict.json`、`rerank-holdout-*`、`rewrite-holdout-*`、`control-*`
**不由本仓库任何包内代码路径写出**（包内没有这些文件名的写入点），由一次性驱动脚本产生。

---

## 8. 一句话总结

`platform/` 是被治理的系统，`agent/` 是治理本身。前者可以照着
[`platform/docs/architecture.md`](../platform/docs/architecture.md) 逐服务读；后者约 4900 行源码，
而第 5 节那四条不变式全部落在它和 `search-service` 的交界处——**读代码时先读断言，再读实现。**
