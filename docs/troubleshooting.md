# 故障排查

这份文档只记录**在这个仓库里真实踩过并修掉的坑**。通用条目（检查端口占用、看看服务起没起）
不在此列——它们谁都会查，而下面这七条的共同特征是：**症状指向的地方不是根因所在的地方**，
不知道就会往错误的方向调一整天。

部署、环境变量与资源要求见 [`deployment.md`](deployment.md)；
`ai_status` / `rerank_status` 的逐状态语义见
[`../platform/docs/runbook.md`](../platform/docs/runbook.md)。

---

## 先分清"降级"和"故障"

搜索请求**永远返回 200**，AI 只是可选增强。所以看 HTTP 状态码判断 AI 有没有跑，
结论一定是错的。唯一可信的信号是响应体里的 `ai_status`（改写）与 `rerank_status`（重排），
两条链路有各自独立的状态机——一次请求完全可能是"改写生效、重排超时"。

下面多条排查的第一步都是：

```bash
curl -s "http://localhost:8080/api/v1/search?q=headphones&size=1&use_ai=true" | python3 -m json.tool
```

看 `ai_status`，不看状态码。

---

## 索引

| # | 症状一句话 | 真正的根因在哪 |
|---|---|---|
| [1](#1-新增可选请求字段后既有调用方突然-400) | 新增一个可选字段后，既有调用方全 400 | Jackson 3 的 primitive 默认值语义翻转 |
| [2](#2-换真实-provider-后整片-timeout或-400-tool_choice) | 换真实模型后整片 TIMEOUT / 400 / 空 content | 厂商默认开着思考模式，关它的参数必须在请求体顶层 |
| [3](#3-端口连得上但没人应答java-侧记成-timeout-而不是连接被拒) | 端口连得上但没人应答 | `uvicorn --reload` 父进程持着 socket，子进程 import 挂了 |
| [4](#4-第二次评测把基线覆盖了) | 跑第二次评测把上一次覆盖了 | 输出路径与落库键都会静默 upsert |
| [5](#5-agent-提案里混进了-holdout-查询) | 提案证据里混进 holdout 查询 | 线上分析表按流量记录，不认识 train/holdout |
| [6](#6-门禁返回-insufficient_evidence被当成提案被否决) | 门禁报 INSUFFICIENT_EVIDENCE 被读成"否决" | 受影响查询太少时检验的 p 下限就大于 α |
| [7](#7-仓库目录移动后venv-里的命令全都用不了) | 仓库改名/搬家后 venv 命令全挂 | console script 的 shebang 是绝对路径 |

---

## 1. 新增可选请求字段后，既有调用方突然 400

### 症状

给 `/api/v1/evaluations/run`（或任何 Java 侧请求体）加了一个新的可选布尔/数值字段之后，
**没有传这个键的既有调用方直接 400**。最典型的是 `data/scripts/evaluate.py`——
它发的 payload 只有 `queries` / `k` / `persist`，于是整条基线复现链路当场断掉。

这个坑最阴的地方在**它出现的时机**：正在运行中的旧容器镜像照常工作，
本地也可能因为测试全都显式传了该字段而通过；只有等到下一次 `make up --build` 重建镜像、
或者哪个老脚本再跑一次，才会炸出来。那时你多半已经不记得改过请求体了。

### 根因

Jackson 3 把 `FAIL_ON_NULL_FOR_PRIMITIVES` 的默认值从 `false` **翻转成了 `true`**，
而 Spring Boot 4.1 的 `use-jackson2-defaults` 默认为 `false`，**不恢复 Jackson 2 语义**。

后果：primitive 字段（`boolean` / `int` / …）一旦在 JSON 里缺省，
不再静默填零值，而是抛 `MismatchedInputException` → HTTP 400。

### 怎么确认

用一个**不带新字段**的最小 payload 直接打：

```bash
curl -i -sS -X POST http://localhost:8080/api/v1/evaluations/run \
  -H 'Content-Type: application/json' \
  -d '{"queries":[{"query_id":1,"query":"headphones","judgments":{}}],"k":10,"persist":false}'
```

`application.yml` 里设了 `server.error.include-message: always`，
所以 400 的响应体会直接带出 `MismatchedInputException` 与出问题的字段名。
也可以在 search-service 日志里搜同名异常。

### 怎么修

**新增可选请求字段一律用包装类型**（`Boolean` / `Integer` / `Long` / `Double`），
并在 record 的紧凑构造器里归一化，而不是依赖任何全局 Jackson 配置：

```java
// platform/services/search-service/src/main/java/lab/searchops/domain/ApiModels.java
@JsonProperty("use_ai") Boolean useAi,
@JsonProperty("use_rerank") Boolean useRerank,
…
public EvaluationRequest {
    useAi = Boolean.TRUE.equals(useAi);
    useRerank = Boolean.TRUE.equals(useRerank);
}
```

包装类型让"缺省"真正等于"没传"，紧凑构造器再把 `null` 归一成 `false`。
这样既保住了默认值语义，又不受全局配置影响。

对象类型字段（如 `strategy_config`）本来就不适用这条陷阱，`null` 就是"没传"，
这时**要保留 null 语义**——"没传候选配置"与"传了一个空配置"必须是两件事。

---

## 2. 换真实 provider 后整片 TIMEOUT（或 400 tool_choice）

### 症状

把 `AI_PROVIDER` 从 `mock` 切到真实 LangChain provider 之后，症状按厂商分三种，
但共同点是 **HTTP 层完全正常**：

- **DashScope（qwen3.x-flash / plus）**：一轮评测几乎全部落到 `ai_status=TIMEOUT`。
  单次调用从约 1s 涨到 8–28s，而读超时是 5000ms。
- **DashScope + 强制 tool_choice**：适配器侧直接 400，报文原文是
  `The tool_choice parameter does not support being set to required or object in thinking mode`。
- **DeepSeek v4**：思考吃掉全部 `max_tokens`，`message.content` 返回空串，
  于是 Java 侧每次判 `INVALID_RESPONSE` 并降级 BM25——看起来"能跑"，其实一条改写都没生效。

### 根因

这几个模型**默认开启思考模式**。关掉它的参数是厂商私有的
（DashScope 用 `enable_thinking`，DeepSeek 用 `reasoning_effort`），OpenAI schema 里没有它的位置，
只能通过 `extra_body` 透传。

关键的一条：**这个参数必须出现在请求体的顶层才生效**。
实测把它再嵌套一层（`{"extra_body": {"enable_thinking": false}}`）时，
服务端**静默忽略**，思考照旧打开——不报错，只是继续慢。
这正是本项目一路在修的那类缺陷：参数放错位置不报错，只是悄悄变慢或变错。

### 怎么确认

不要靠人眼盯延迟。适配器有一条自检：`_warn_if_thinking` 读响应 usage 里的
`output_token_details.reasoning`，只要 > 0 就打一条 WARN。

```bash
# 容器路径
docker compose logs ai-adapter | grep thinking_enabled
# 本机路径：直接看 make dev-ai 的终端
```

看到 `"event": "ai.rewrite.thinking_enabled"` 就是它。日志里同时带了修法提示。

### 怎么修

在 `platform/.env` 里显式声明：

```bash
AI_EXTRA_BODY={"enable_thinking": false}      # DashScope qwen3.x-flash / plus
AI_EXTRA_BODY={"reasoning_effort": "none"}    # DeepSeek v4
AI_EXTRA_BODY={}                              # 显式声明"什么都不透传"
```

两个容易漏的细节：

- **默认档才会自动补。** 只有 `AI_MODEL` 与 `AI_API_BASE_URL` **都**没设置时，
  provider 才自动补上默认档配套的 `{"enable_thinking": false}`。
  一旦你指定了自己的模型或端点，就必须自己声明（不需要就填 `{}`）——
  因为 `enable_thinking` 是 DashScope 私有字段，发给 OpenAI 官方端点会 400。
- **改完必须重启 uvicorn 父进程。** `--reload` 只重载代码、不重载环境变量。
  改了 `.env` 却只保存了一下 Python 文件，新值不会生效。

顺带一提 `AI_STRUCTURED_OUTPUT_METHOD`：默认 `function_calling` 而不是 LangChain 自己的
`json_schema`，因为实测 DeepSeek 的 `deepseek-v4-flash` / `deepseek-v4-pro` 与
DashScope 的 `qwen3-30b-a3b-instruct-2507` 对 `json_schema` 直接 400
（`This response_format type is unavailable now`），而 tool-calling 在实测过的
DashScope 四个模型与 DeepSeek 两个模型上全部可用。

---

## 3. 端口连得上但没人应答，Java 侧记成 TIMEOUT 而不是连接被拒

### 症状

Java 侧**整片** `ai_status=TIMEOUT`。注意是 TIMEOUT，不是 `TRANSPORT_ERROR`——
这个区别会把人带偏：TRANSPORT_ERROR 一眼就知道去看适配器，
TIMEOUT 看起来像"模型太慢"，于是第一反应是去调 `AI_TIMEOUT_MS`。调了也没用，
只会把整片快 TIMEOUT 变成整片慢 TIMEOUT。

直接 curl 适配器的表现是**挂住**，而不是 connection refused：

```bash
curl http://localhost:8000/ai/health     # 一直等，不返回
```

### 根因

`uvicorn --reload` 是父子两个进程：**reloader 父进程先绑定并持有 :8000 的监听 socket**，
真正的应用跑在子进程里。子进程 import 失败时（最常见的是 `load_provider()` 在
`app.main` 的 import 期抛异常，比如 `AI_API_KEY_ENV` 指向的变量没导出），
父进程仍然持着 socket——于是 TCP 握手成功、连接被 accept，但**没有任何人处理请求**。

对调用方来说，连接建立成功，剩下的只能是读超时。所以 Java 记 `TIMEOUT`，
而不是连接被拒。

顺带说明为什么 provider 缺 key 时是"抛异常起不来"而不是"退回 mock"：
静默退化会把"模型根本没跑"伪装成"AI 没效果"，而后者会被写进评测结论。
宁可服务起不来，也不要出一份看起来正常的假指标。这条设计的代价，就是本条症状。

### 怎么确认

**看到整片 TIMEOUT，第一件事是去 `make dev-ai` 的终端找 import traceback。**
不是去调超时，不是去 ping 模型厂商。

辅助确认：

```bash
curl -m 3 -sS http://localhost:8000/ai/health   # 挂到超时 ≠ 连接被拒，两者含义完全不同
lsof -nP -iTCP:8000 -sTCP:LISTEN                # 进程还在，socket 还在
```

`_api_key` 抛出的异常信息会**直接点名缺哪个环境变量**：它会写明
`AI_API_KEY_ENV` 当前指向哪个变量名、该变量未设置或为空，
并提示 `--reload` 不重载环境变量、必须重启父进程。traceback 里读这一行就够了。

### 怎么修

1. 按 traceback 修掉根因（多数情况是导出 `AI_API_KEY_ENV` 指向的那个变量）；
2. Ctrl-C 停掉 `make dev-ai`，**重启父进程**（不是等热重载）；
3. 再 `curl -m 3 http://localhost:8000/ai/health` 确认它这次是真的答话了。

容器路径下不存在这个坑：`ai-adapter` 容器没有 `--reload`，import 失败就是容器起不来，
健康检查直接转红，`make up` 的等待脚本会把它列出来。

---

## 4. 第二次评测把基线覆盖了

### 症状

跑完一轮评测，再跑一轮，发现上一轮的结果没了——正好是 A/B 对照最不能出的事：
BM25 基线和 AI 候选互相踩，两份数据同时作废。

### 根因

两层，都是"静默覆盖"：

- **文件层**：`evaluate.py` 早先把输出路径写死成 `evaluation-latest.json`，跑第二次直接盖掉第一次。
- **数据库层**：`quality_metrics` 的唯一键是 `(query_id, strategy_version)`，服务端用
  `ON CONFLICT DO UPDATE`。同一个 `strategy_version` 下跑一次 AI 评测并落库，
  就会把该版本的逐查询 BM25 指标原地覆盖掉——而运营后台的"低 NDCG 查询"正是从这张表读的。
  文件分开了、库里还混着，一样没法对照。

### 怎么确认

现状是两个 make 目标写两个文件，互不覆盖：

| 命令 | 输出文件 | 默认落库 |
|---|---|---|
| `make evaluate` | `data/processed/evaluation-latest.json` | 是 |
| `make evaluate-ai` | `data/processed/evaluation-ai-latest.json` | **否** |

但**同一个目标跑两次仍然会覆盖同名文件**。分辨手上这份是哪一次，读文件里的
`evaluation_client` 块——它记了 `use_ai` / `ai_provider` / `ai_status` / `query_limit` /
`persist` / `client_timeout_seconds` / `search_url`，再配合顶层的 `generated_at`：

```bash
python3 -c "import json;d=json.load(open('platform/data/processed/evaluation-latest.json'));print(d.get('generated_at'),d.get('evaluation_client'))"
```

### 怎么修

- 要保留某一次结果，**显式给 `--output`**：

  ```bash
  ./data/scripts/evaluate.sh --use-ai --limit 20 --output /tmp/ai-smoke.json
  ./data/scripts/evaluate.sh --limit 5 --no-persist --output /tmp/bm25-smoke.json
  ```

  `evaluate.sh` 会把额外参数原样透传给 `evaluate.py`，argparse 取最后一次出现的值，
  所以后面的 `--limit` 会覆盖 `.env` 里的 `EVALUATION_QUERY_LIMIT`。

- **AI 评测默认 `--no-persist`**，就是为了不污染同版本的逐查询 BM25 行。
  真要落库请显式传 `--persist`，并且清楚你在覆盖什么。

- **`baselines/` 是只读归档，不是工作副本。** 不要拿"反正还有备份"当覆盖工作文件的理由，
  也不要修改或删除该目录下任何文件。

另外，AI 评测在开跑前会先用一条真实检索探一次 AI 链路，
如果 `ai_status` 不是 `APPLIED` / `NO_CHANGE` 就**中止且不写文件**——
一份全程降级的 BM25 结果不该被存成"AI 结果"。但要知道这个探针只反映起跑那一刻的状态：
适配器中途死掉，剩下的查询照样静默降级。

---

## 5. agent 提案里混进了 holdout 查询

### 症状

模型给出的提案里出现了留出集里的查询，甚至写出逐字钉死某条 holdout 查询的 `rewrite_rule`。
本轮评测只在 train 上做，所以门禁判定当时没被污染；
但这类提案一旦拿去 holdout 验收，就等于"用考题训练"——那个数字再也不能用了。
留出集 n=600 只有一次可信的验收机会，这类泄漏是不可逆的。

### 根因

`zero_result_queries` / `low_quality_queries` 这两个工具读的是**线上分析表**。
那张表按流量记录，**不认识 train/holdout 的划分**，也没有理由认识它。
直接拿它的返回当提案证据，就会把 holdout 查询混进来。

### 怎么确认

- 走 `ProposalLoop.diagnose` 取证据的路径已经加了过滤（`agent/searchops_agent/loop.py`）：
  `_train_only` 只保留可确认属于 train 的行，按 `query_id` 与查询文本双路匹配，
  两者都对不上的行一律丢弃（失败关闭：宁可少给证据，也不给来历不明的证据），
  被丢弃的条数计入 `diagnosis_dropped`。跑完看这个计数就知道过滤有没有在工作。
- **绕开 loop 直接调工具就没有这层过滤。** 如果你自己写了脚本调
  `zero_result_queries` / `low_quality_queries`，默认就是不安全的。

### 怎么修

- 证据一律经 `loop.diagnose()` 取；
- 自建脚本必须自己按 `bench.split_train_ids` 与 `bench.train_query_texts` 过滤，
  并且**失败关闭**——归属无法确认的行丢弃，而不是保留；
- 自证评测也只在 train 上做（`select_train_rows` 已断言与 holdout 无交集）。
  两处都堵住，泄漏才真的不可能发生：只堵一处，另一处就是缺口。

---

## 6. 门禁返回 INSUFFICIENT_EVIDENCE，被当成"提案被否决"

### 症状

晋级门禁返回 `INSUFFICIENT_EVIDENCE`，运维/调用方读成"这个提案没通过、可以扔了"。
实际含义完全不同：**这批证据根本不足以裁决它**。

### 根因

配对置换检验的零分布**只由非零差值决定**：受影响查询有 k 条时，只有 2^k 种符号组合，
因此可达的最小 p 是 1/2^k。

| k | p 下限 |
|---|---|
| 3 | 0.125 |
| 4 | 0.0625 |
| 5 | 0.03125 |

k ≤ 4 时下限已经大于 α=0.05——**无论候选策略多好，检验都不可能判显著**。
所以 `MIN_AFFECTED = 5`：低于它就不给结论。

把这种情形也报成 `BLOCK`，门禁就重演了本项目一路在修的同一个缺陷：
把两种成因不同的状态塌缩成同一个信号（`AiRewriteStatus` 的由来正是如此）。
"测过了没通过"和"这批证据无法裁决"必须分开报。

### 怎么确认

看 `GateDecision`：

- `verdict` == `INSUFFICIENT_EVIDENCE`，`undecidable == True`；
- `affected` 是实际受影响的查询数；
- `render()` 的第一行是 `INSUFFICIENT_EVIDENCE — 证据不足以裁决，不得据此下结论`，
  `reasons` 里会写明"仅 N 条查询的 ndcg10 发生变化（阈值 5）；配对置换检验在该规模下的
  p 下限为 …，已大于显著性水平，本次判定不具备分辨力"。

代码在 `agent/searchops_agent/eval/gate.py`（`MIN_AFFECTED` 的注释里写了完整推理）。

### 怎么修

不是修门禁，是修证据：

- **扩大受影响面**——让提案覆盖更多查询，而不是只钉住少数几条；
- **扩大评测集**——受影响查询数是 k，不是评测总量，但更大的评测集通常会带来更大的 k；
- **绝不**为了拿到结论去调低 α、调低 `MIN_AFFECTED` 或直接放行。
  那不是让提案通过了，那是让门禁不再有意义。

---

## 7. 仓库目录移动后，venv 里的命令全都用不了

### 症状

仓库整体改名或搬到别的路径之后，`make dev-ai`、`scripts/test-unit.sh`、
`scripts/test-integration.sh` 报 `bad interpreter: no such file or directory`，
或者更隐蔽：命令能跑，但用的是**另一个** Python 环境，装的包对不上。

### 根因

venv 里的 console script（`uvicorn` / `pytest` / `ruff` / `pip` …）第一行 shebang
写的是**创建时的绝对路径**。venv 本身不可搬迁。

本项目有两个 venv 都会中招：
`platform/.venv-data` 与 `platform/services/ai-adapter/.venv`。
`platform/services/ai-adapter/.venv` 的 console script 就曾经指向迁移前的旧目录（已修）。

### 怎么确认

直接看 shebang 指向的路径是不是当前仓库位置：

```bash
head -1 platform/services/ai-adapter/.venv/bin/uvicorn
head -1 platform/services/ai-adapter/.venv/bin/pytest
head -1 platform/.venv-data/bin/pytest
```

（这三处曾经全部指向迁移前的旧目录，已逐一修正。写下这句话的那一版文档只核对了前两处，第三处 `platform/.venv-data` 当时仍是旧路径——所以本条的正确用法是**自己跑一遍上面三条命令**，而不是相信任何文档声称的"已修复"。venv 是二进制产物、不入版本控制，它在你机器上的状态只有你自己能确认。）

顺带看一眼解释器本身：

```bash
cat platform/services/ai-adapter/.venv/pyvenv.cfg     # home / version
```

### 怎么修

删掉重建，不要手工改 shebang（改了 console script，`pyvenv.cfg` 与
`lib/pythonX.Y/site-packages` 里的路径记录还是旧的）：

```bash
cd platform
rm -rf .venv-data services/ai-adapter/.venv
make bootstrap
```

`bootstrap.sh` 会按 `requirements-dev.txt` 重建这两个 venv，
并顺带重跑 `npm ci` 与 Maven 依赖预热。

---

## 数据边界提醒

排查过程中会看到大量价格、库存、订单与"用户行为"数据。**它们全部是确定性模拟**，
由 `product_id + DATA_SAMPLE_SEED` 的哈希派生。真实的只有商品文本
（标题、品牌、描述、bullet point、颜色）、查询文本与 ESCI 相关性标注，
来自公开的 [amazon-science/esci-data](https://github.com/amazon-science/esci-data)（Apache-2.0）。

**不得把任何模拟字段表述成亚马逊的交易、行为、库存或价格数据。**
排查日志、截图与对外说明同样适用。详见
[`../platform/docs/data-provenance.md`](../platform/docs/data-provenance.md)。
