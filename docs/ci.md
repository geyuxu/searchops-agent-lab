# 持续集成

本文说明 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) 跑了什么、为什么这么切分、
以及如何在本机复现完全相同的检查。

部署与运行形态见 [`deployment.md`](deployment.md)，故障排查见 [`troubleshooting.md`](troubleshooting.md)，
目录结构见 [`code-structure.md`](code-structure.md)；`ai_status` / `rerank_status` 的逐状态语义与评测操作
见 [`../platform/docs/runbook.md`](../platform/docs/runbook.md)。本文不重复这些内容。

> **这套 workflow 还没有在 GitHub 上跑过。** 本仓库此前没有任何 CI 配置，本文里所有"某某检查会通过"
> 的说法都只代表**在本机用仓库自带的 venv 跑出来的结果**（核验状态见文末表格），不代表 GitHub Actions
> 的运行结果。第一次 push 后请以 Actions 页面为准。另外，**首次运行预计是红的**，原因见
> [已知问题：ruff 会失败](#已知问题ruff-会失败)。

---

## 两条铁律

整个 workflow 只围绕两条规则设计，其余都是它们的推论。

**一、不依赖任何 secret。** `ci.yml` 里不出现 `secrets.`，没有任何一步读取模型凭据。
需要 key 的东西一律不进 CI（清单见[CI 里没有、也不会有的东西](#ci-里没有也不会有的东西)）。
LangChain 相关测试全部走打桩：`test_providers_langchain.py` 在导入时就把
`langchain*` 整族模块合成进 `sys.modules`，`test_providers_langchain_rerank.py` 则在
provider 自己的命名空间里替换 `ChatOpenAI`，两者都不碰网络，其中前者还会毒化
`socket.connect`，让"绕过 LangChain 自己发 HTTP"的实现当场炸掉而不是偷偷联网。

**二、被跳过的测试等于失败的测试。** 这个仓库真实发生过一次事故：一份契约测试因为
`langchain` 没装进 venv，`requires_provider` 这个 `skipif` 把里面所有用例全跳过了，
测试报告是绿的，而它什么都没有守住。CI 必须让"被跳过"这件事可见且致命，
这是本次最重要的要求，实现见[核心守卫](#核心守卫不许绿在跳过上)。

---

## 触发与作业总览

触发条件：push 到 `master`，以及任何 `pull_request`。同一个 ref 上有新提交时，
`concurrency` 会取消仍在跑的旧运行。`permissions` 收到 `contents: read`。

除非另有说明，下表"本地等价命令"的工作目录都是**仓库根**。

| Job | 跑什么 | 本地等价命令 |
| --- | --- | --- |
| `lint-python` | ruff（两棵 Python 树） | `cd platform/services/ai-adapter && .venv/bin/ruff check app tests`<br>`cd platform/data && ../.venv-data/bin/ruff check scripts tests` |
| `typecheck-ts` | 三个包的 TypeScript strict | `cd platform/packages/api-contracts/typescript && npm ci && npm run typecheck`<br>`cd platform/apps/storefront && npm ci && npm run typecheck`<br>`cd platform/apps/operations-console && npm ci && npm run typecheck` |
| `test-ai-adapter` | pytest + 85% 覆盖率门禁 + 三道 LangChain 守卫 | `cd platform/services/ai-adapter && .venv/bin/pytest` |
| `test-agent` | agent pytest + LLM 版本对齐守卫 | `cd agent && .venv/bin/pytest` |
| `test-data` | 数据流水线 pytest | `cd platform/data && ../.venv-data/bin/pytest -q tests` |
| `test-node` | commerce 领域测试 + 三份契约文档可解析 | `cd platform/services/commerce && npm ci && npm test`<br>`cd platform/packages/api-contracts && python3 -m json.tool openapi/ai-adapter.openapi.json >/dev/null`（另两份同理） |
| `test-java-unit` | 纯 JVM 测试（排除 Testcontainers） | `cd platform/services/search-service && mvn -B test -Dtest='!StrategyWorkflowIntegrationTest' -Dsurefire.failIfNoSpecifiedTests=false` |
| `test-java-integration` | 唯一一个 Testcontainers 测试（需要 Docker） | `cd platform/services/search-service && mvn -B test -Dtest=StrategyWorkflowIntegrationTest -Dsurefire.failIfNoSpecifiedTests=false` |

本机上 Java 需要 JDK 21，`mvn` 默认拿到的常常不是它：

```bash
export JAVA_HOME="$(/usr/libexec/java_home -v 21)"; export PATH="$JAVA_HOME/bin:$PATH"
```

`make test-unit` 与上表不是同一件事——它还会跑 `npm run build`（Next.js 与 medusa），
CI 不跑构建，理由见[CI 里没有、也不会有的东西](#ci-里没有也不会有的东西)。

---

## 为什么这么切分

**第一层 `lint-python` / `typecheck-ts`——最便宜，最先出结果。**
它们不装测试依赖、不起容器，几十秒内给出结论。

**它们刻意不是测试作业的前置门（没有 `needs:`）。**
GitHub Actions 默认并行跑所有无依赖的 job，所以"快的先失败"靠的是它们先跑完，
而不是靠阻塞别人。如果把单元测试挂在 lint 后面，一个全角逗号就能让你看不到任何一条测试结果——
本仓库当下恰好就处在这个状态（见[已知问题](#已知问题ruff-会失败)），
这条设计的收益是立刻可见的：lint 红着，八个作业里其余七个照常给出真实结论。

**第二层是单元测试**，五个作业按语言/组件拆开，为的是失败可归因：
看作业名就知道是 Java、Python 哪一侧还是前端，不用翻日志。它们都不需要 Docker、
不联模型、不读 secret。

**第三层 `test-java-integration` 单独隔离**，因为它是唯一需要 Docker 守护进程的作业：
`StrategyWorkflowIntegrationTest` 会真起一个 `postgres:17.6-alpine` 验证治理工作流。
它挂在 `needs: test-java-unit` 后面——纯 JVM 测试都挂了的话，再拉一次镜像只是重复确认同一件事。
它给了 `timeout-minutes: 25`，明确允许它慢。

`search-service` 的 `pom.xml` 没有配 surefire 的 includes/excludes，所以 `mvn test` 默认会把
Testcontainers 测试一起跑掉。切分靠命令行 `-Dtest` 完成，不改 pom：

- 纯 JVM 作业用 `-Dtest='!StrategyWorkflowIntegrationTest'` 排除它；
- 集成作业用 `-Dtest=StrategyWorkflowIntegrationTest` 只跑它；
- 两边都带 `-Dsurefire.failIfNoSpecifiedTests=false`，这样过滤器在某个模块上没匹配到任何测试时
  不会把构建判失败。

判断"谁需要 Docker"不能靠文件名里的 `IntegrationTest`：`SearchRerankIntegrationTest` 名字里带
Integration 但是纯 JVM 的，而 `AiTestSupport`、`ModelIdentityTest` 等文件的注释里出现
"Testcontainers" 只是在说明"本文件**不**启动 Testcontainers"。真正的判据是
`@Testcontainers` / `@Container` 注解，全仓库只有 `StrategyWorkflowIntegrationTest` 带。

`packages/api-contracts/java` 没有 `src/test`，那一步 `mvn test` 实质是编译检查：
确认共享的 Jackson 3 客户端在 JDK 21 下仍然构建得起来。它很便宜，且能早一步暴露
Jackson 3 / Spring Boot 4.1 那条线上的契约漂移。

---

## 核心守卫：不许"绿在跳过上"

`test-ai-adapter` 里有三道守卫，一道比一道靠后，堵的是同一个洞的三个阶段。

### 守卫一：装上的版本必须与 pin 逐位相同

解析 `requirements.txt` 与 `requirements-dev.txt` 里的每一条 `pkg==version`，
用 `importlib.metadata.version()` 取实际安装版本逐条比对，**未安装**与**版本不符**都判失败。

`pip install` 返回 0 不是证据——解析器回溯或缓存命中都可能留下另一个版本，
而事故当初的形态正是"venv 里就是没有 langchain"。这一步会明确打印
`langchain-core: NOT INSTALLED (pinned 1.5.5)` 这样的行。
`openai` 和 `tiktoken` 一并检查，理由与 `requirements.txt` 里钉死它们的理由相同：
`extra_body` 是否透传到请求体**顶层**是 `openai` 客户端的行为，整条
`enable_thinking=false` 的关思考路径依赖它；`tiktoken` 换版本会换编码表。

### 守卫二：收集数量必须大于 0（预检）

```
pytest --collect-only --no-cov tests/test_providers_langchain.py tests/test_providers_langchain_rerank.py
```

统计输出里 `::` 的行数。pytest 钉死在 9.1.1，它的 `--collect-only` 每行打印一个
`path::name` 节点 id，所以数 `::` 是精确计数，不是去猜某个汇总行的格式。
加 `--no-cov` 是因为 `pyproject.toml` 的 `addopts` 里有 `--cov-fail-under=85`，
而"只收集不执行"的覆盖率必然是 0，会误判。

这一步秒级失败，排在跑全量套件之前。

### 守卫三：一条都不许被跳过（决定性的一道）

守卫二只能证明用例被**收集**了；跳过发生在收集之后，两者只有测试报告能区分。
所以真实运行这一步带 `--junitxml`，跑完再解析 JUnit XML：

- 两个 LangChain 测试模块各自收集数必须 > 0；
- 这两个模块里 skipped 必须为 0；
- **全套件**范围内，任何 skipped 用例必须落在白名单里，否则失败。

白名单当前只有一条：

```
("tests.test_providers_echo", "test_marker_token_is_present_in_the_indexed_corpus")
```

它读 `data/processed/products.jsonl`，那是 `.gitignore` 掉的派生物，由 `make data` 从
1.1GB 的 ESCI parquet 重建，CI 里必然不存在。**注意这是白名单不是过滤器**：
将来任何新增的 skip 都会让这一步红，必须有人显式把它加进白名单——
也就是说"新增一个 skip"从此是一个需要解释的动作，而不是一件没人看见的事。

这一步带 `if: always()`，pytest 失败时它依然运行，这样"挂了几条"和"跳了几条"会一起报出来。

### 怎么在本地验证这道守卫真的有效

守卫本身也可能是坏的。可以这样在**副本**上复现当年那次事故（不要在仓库里改）：

```bash
cp -R platform/services/ai-adapter/{app,tests,pyproject.toml} /tmp/repro/
# 让 provider 无法被发现：改掉模块名里的 "lang"
mv /tmp/repro/app/providers/langchain_rewrite.py /tmp/repro/app/providers/rewrite_disabled.py
mv /tmp/repro/app/providers/langchain_rerank.py  /tmp/repro/app/providers/rerank_disabled.py
cd /tmp/repro && pytest --no-cov --junitxml=/tmp/repro.xml -q tests/test_providers_langchain.py
```

会看到 `test_langchain_provider_module_exists` **失败**（这是测试文件自带的兜底，
它故意不跳过），而同文件其余用例全部 **skipped**——正是当年那个"看起来在守契约、
实际什么都没守"的形态。把 `/tmp/repro.xml` 喂给守卫三的脚本，它必须判失败。

注意副本里 `test_contract_drift.py` / `test_tool_gateway_contract.py` 会因为找不到
`packages/api-contracts/` 而收集报错——它们按相对路径回溯到仓库布局，换目录就失效，
所以上面只跑那一个文件。

### agent 侧的对应守卫

`agent/pyproject.toml` 的 `llm` extra 与 `platform/services/ai-adapter/requirements.txt`
钉的是同一套 LangChain 版本，pyproject 里写明是"逐位对齐"，理由是两侧跑同一家端点、
同一个模型，版本分叉时"到底是谁的问题"就无法归因。注释管不住这件事，`test-agent`
里的一步能：它抽出双方的 `langchain-core` / `langchain-openai` / `openai` / `tiktoken`
四个版本逐条比对，不一致就失败。

`test-agent` **不安装** `llm` extra：`agent/tests` 从不 import LangChain
（`LLMProposer` 是在调用内部惰性 import 的），装上只会多几十 MB 轮子，不会多守住任何东西。

### 顺带打开的两个开关

- `--strict-markers`：marker 打错字会直接报错，而不是变成一个没人注册、也没人执行的标记。
  当前两棵树只用了内置的 `parametrize` 与 `skipif`，所以今天它是免费的，
  等有人发明第一个自定义 marker 那天它才开始起作用。
- `-rs`：在日志里列出所有 skip 的原因，让人不必下载 artifact 就能看见。

JUnit XML 与 surefire 报告都会作为 artifact 上传（`if: always()`），失败时可直接下载。

---

## 依赖安装口径与缓存

三种包管理器都按 lockfile / 精确版本安装，绝不让 CI 自己去解析"当时的最新版"：

| 生态 | 命令 | 依据 |
| --- | --- | --- |
| npm | `npm ci` | `package-lock.json`；与 `npm install` 不同，它拒绝 lockfile 与 package.json 不一致 |
| pip | `pip install -r requirements-dev.txt` | 文件内全部是 `==` 精确 pin；`requirements-dev.txt` 首行 `-r requirements.txt`，一条命令装齐运行期与测试期 |
| pip（agent） | `pip install -e ".[dev]"` | `pyproject.toml` |
| Maven | `mvn -B -ntp test` | `pom.xml` + Spring Boot 4.1.0 的 BOM |

**一个必须说清的局限**：Python 侧只有直接依赖被钉死，传递依赖（如 `starlette`、`anyio`）
没有 hash 锁定，仓库里也没有 `requirements.lock` / `pip-compile` 产物。
所以 CI 额外加了[守卫一](#守卫一装上的版本必须与-pin-逐位相同)去断言那几个
**行为承载方**确实是钉住的版本。真想要完全可复现的安装，得引入 hash-pinned lockfile，
那是另一件事，本次没做。

缓存全部用 setup-* action 自带的机制，缓存键都包含对应 lockfile 的哈希：

- `actions/setup-python` + `cache: pip`，`cache-dependency-path` 显式列出该作业实际安装的
  requirements 文件；
- `actions/setup-node` + `cache: npm`，`cache-dependency-path` 列出该作业用到的
  `package-lock.json`；
- `actions/setup-java` + `cache: maven`，键为 `hashFiles('**/pom.xml')`——本仓库没有独立的
  Maven lockfile，`pom.xml` 就是依赖清单。

用自带机制而不是手写 `actions/cache`，是因为键的构造和 restore-key 回退都已经处理好了，
手写一份只是多一处可能写错的地方。

`lint-python` 的缓存键挂在两份 `requirements-dev.txt` 上，虽然它只装 ruff 一个包——
ruff 的 pin 就写在这两个文件里，所以这正是那个 wheel 该用的键。

ruff 版本不写死在 workflow 里，而是用 `grep -m1 '^ruff=='` 从
`requirements-dev.txt` 里读出来，并断言两棵树读到的是同一行：CI 与 `make test-unit`
用不同版本的 linter，等于两套判据。

---

## 版本选择

| 运行时 | CI 取值 | 依据 |
| --- | --- | --- |
| Java | Temurin **21** | `search-service/pom.xml` 的 `<java.version>21</java.version>`；`api-contracts/java` 的 `maven.compiler.release=21` |
| Python | **3.10** | `services/ai-adapter/.venv` 是 3.10.10；`agent/pyproject.toml` 声明 `requires-python = ">=3.10"`；ruff 配置 `target-version = "py310"` |
| Node | **24** | 三个 `package.json` 都声明 `"engines": {"node": ">=22 <=24"}`，取该区间上限 |

**Node 版本有一处需要注意。** 任务口径是"用与本地一致的大版本"，但本机当前是
**v25.7.0**，落在所有 `package.json` 声明的 `>=22 <=24` 之外。CI 选 24 而不是 25，
理由是以仓库自己声明的 engines 为准（Medusa 对 Node 版本敏感）。
仓库里没有 `.nvmrc`，也没有 `engine-strict=true` 的 `.npmrc`，所以本机跑 25 只是没有报错，
不代表被支持。**这处不一致值得单独定夺**：要么本机降到 24，要么把 engines 放宽到 25
并在 24/25 两个版本上验证——两者都不是 CI 配置能替你决定的事。

---

## CI 里没有、也不会有的东西

以下检查全部**不在** CI 里，每一条都给了本地怎么跑。

### 需要模型凭据的：`make evaluate-ai`、真实 rerank/rewrite 链路

CI 没有 secret，也不打算有。真实模型调用还有第二个理由不适合 CI：它不是确定性的，
把它当门禁会得到一个随机失败的红叉。

离线评测属于**实验产物**而不是回归门禁——它的结论进 `experiments/`，
由人来读（留出集 n=600，重排 NDCG@10 0.4720→0.5926，p=0.0001；改写 0.4745，
p=0.5942，不显著），而不是由 CI 来判定 pass/fail。

本地跑（需要 `platform/.env` 里的 key）：

```bash
cd platform && make evaluate      # BM25 基线 → evaluation-latest.json
cd platform && make evaluate-ai   # 开 AI 改写 → evaluation-ai-latest.json
```

两个目标写两个不同的文件，互不覆盖。

### E2E（Playwright）

`tests/e2e` 的 `baseURL` 指向 `http://localhost:3000`，需要**完整栈在跑**且**数据已灌**。
数据这一步要从上游下载 1.1GB 的 ESCI parquet 再构建派生物，即使加了缓存，
把它放进每次 push 的常规 CI 也是几十分钟起步、且依赖上游可用性——
一条随时可能因为别人的服务器而变红的门禁，不是门禁。

本地跑：

```bash
cd platform && make data        # 一次性：下载并构建 ESCI 子集
cd platform && make demo-up     # 拉起完整栈（开发形态）
cd platform && make seed        # 灌商城数据并重建索引
cd platform && make test-e2e    # Playwright
cd platform && make demo-down   # 收干净
```

如果将来要把 E2E 接进 CI，合理的形态是**独立的定时或手动触发的 workflow**
（`schedule` / `workflow_dispatch`），配合数据集缓存，而不是挂在 push 上。

### `make up` / 完整 compose 冒烟

`make up` 会构建并起全部 8 个服务。它进不了当前的 CI，原因是**它需要 `platform/.env`**
——里面有 `POSTGRES_PASSWORD` / `JWT_SECRET` / `COOKIE_SECRET` /
`SEARCHOPS_APPROVAL_SECRET`，而 `.env` 是 gitignore 的。用 `.env.example` 伪造一份是可行的，
但代价是每次 push 都要构建五个应用镜像（Maven + 两个 Next.js + Medusa），
收益只是"镜像还能 build"。

真要加，建议单独一个 workflow，只做 `docker compose build` 而不 `up`，并且不挂在每次 push 上。

### `npm run build`（Next.js / medusa）

`make test-unit` 里有，CI 里没有。`medusa build` 要数据库连接串；两个 Next.js 应用的
build 比 `tsc --noEmit` 慢一个量级，而**类型错误已经被 `typecheck` 作业抓住了**。
两个应用都设了 `typedRoutes: false`，所以 `tsc --noEmit` 不依赖 `next build` 生成的
`.next/types`，`next-env.d.ts` 也已入库——这正是 typecheck 可以脱离 build 独立跑的前提。

### `make data` / `make seed`

见上，1.1GB 下载 + DuckDB 抽取 + 真实 Postgres 写入。`test-data` 作业只跑
`platform/data/tests` 里的纯逻辑测试。

---

## 在本地复现整条 CI

仓库自带的 venv 已经装好了对应版本，逐条对应上面的作业：

```bash
export JAVA_HOME="$(/usr/libexec/java_home -v 21)"; export PATH="$JAVA_HOME/bin:$PATH"

# lint-python
(cd platform/services/ai-adapter && .venv/bin/ruff check app tests)
(cd platform/data && ../.venv-data/bin/ruff check scripts tests)

# typecheck-ts
(cd platform/packages/api-contracts/typescript && npm ci && npm run typecheck)
(cd platform/apps/storefront && npm ci && npm run typecheck)
(cd platform/apps/operations-console && npm ci && npm run typecheck)

# test-ai-adapter（含覆盖率门禁；守卫三需要 junitxml）
(cd platform/services/ai-adapter && .venv/bin/pytest --strict-markers -rs --junitxml=junit-ai-adapter.xml)

# test-agent / test-data / test-node
(cd agent && .venv/bin/pytest --strict-markers -rs)
(cd platform/data && ../.venv-data/bin/pytest --strict-markers -rs tests)
(cd platform/services/commerce && npm ci && npm test)

# test-java-unit / test-java-integration
(cd platform/services/search-service && mvn -B -ntp test -Dtest='!StrategyWorkflowIntegrationTest' -Dsurefire.failIfNoSpecifiedTests=false)
(cd platform/services/search-service && mvn -B -ntp test -Dtest=StrategyWorkflowIntegrationTest -Dsurefire.failIfNoSpecifiedTests=false)
```

本机已经装过依赖时，Maven 可以加 `-o` 走离线，快很多。
`junit-ai-adapter.xml` 是 CI 产物，跑完记得删掉（它没有被 `.gitignore` 覆盖）。

三道守卫脚本内嵌在 `ci.yml` 的 `run:` 块里（没有单独的脚本文件，避免在仓库里多放一份
只有 CI 用得到的代码）。想在本地跑其中一道，可以直接从 workflow 里抽出来：

```bash
python3 -c "
import yaml
d = yaml.safe_load(open('.github/workflows/ci.yml'))
for s in d['jobs']['test-ai-adapter']['steps']:
    if s.get('name','').startswith('Guard'):
        print(s['run'])
" > /tmp/guards.sh
(cd platform/services/ai-adapter && PATH="$PWD/.venv/bin:$PATH" bash /tmp/guards.sh)
```

---

## 已知问题：ruff 会失败

**`lint-python` 作业目前会红**，本机复现结果是 8 条 `RUF003`，全部集中在
`platform/services/ai-adapter/tests/test_providers_langchain.py` 的 460–466 行：

```
tests/test_providers_langchain.py:460:38: RUF003 Comment contains ambiguous `，` (FULLWIDTH COMMA)...
...
Found 8 errors.
```

这不是 CI 引入的问题，也不是配置写错——`scripts/test-unit.sh` 里那条
`ruff check app tests` 本来就会以同样方式失败。这 8 个全角标点来自最近一次提交
（`890aeef`，正是"修复一份从未真正运行过的契约测试"那次）新增的中文注释块。

**为什么 `platform/data` 不报，尽管它的全角标点更多。**
本机统计：`data/scripts/evaluate.py` 里有 46 个全角逗号/冒号，`test_providers_langchain.py`
里只有 8 个，但前者通过、后者失败。原因不是字符，是**规则集不同**：

- `services/ai-adapter/pyproject.toml` 写了 `[tool.ruff.lint] select = ["E","F","I","B","UP","RUF"]`，
  `RUF` 打开了 RUF001/002/003；
- `platform/data` 往上一直到仓库根都没有任何 ruff 配置文件，于是走 ruff 默认规则集
  （E4/E7/E9/F），`RUF003` 根本不参与判定。

验证方法：`cd platform/data && ../.venv-data/bin/ruff check scripts tests --select RUF`
会报出 58 条错误。也就是说，同一个 pinned ruff 在两棵树上执行的是**两套判据**——
CI 忠实地保留了这个既有事实，没有替仓库统一它。

**两个修法**（都在 CI 管不到的文件里，需要单独决定）：

1. 把 460–466 行的 `，` `：` 换成 ASCII 的 `,` `:`。这与仓库其余部分的既有习惯一致——
   `app/` 下的 Python 源码里全角逗号/冒号的出现次数是 0。
2. 在 `services/ai-adapter/pyproject.toml` 里放宽规则，例如：

   ```toml
   [tool.ruff.lint.per-file-ignores]
   "tests/test_providers_langchain.py" = ["RUF003"]
   ```

改哪个都行，但**别把 `ruff check` 从 CI 里拿掉，也别给它加 `continue-on-error`**：
那会让 CI 与 `make test-unit` 的判据分叉，而"两套判据"正是这一节要说明的问题本身。

---

## 本文数字的来源与核验状态

本节区分"本机实测跑出来的"与"没跑的"，避免把没验证的数字当成事实引用。

**本机实测**（2026-08-18，macOS，仓库自带 venv 与本地 `~/.m2`）：

| 检查 | 结果 |
| --- | --- |
| `ai-adapter` pytest | 207 passed，覆盖率 94.19%（门禁 85%），0 skipped |
| LangChain 契约测试收集数 | 106（`test_providers_langchain.py` 29 + `..._rerank.py` 77） |
| `agent` pytest | 26 passed |
| `data` pytest | 1 passed |
| `commerce` npm test | 3 passed |
| Java 纯 JVM（排除 Testcontainers） | Tests run: 198, Failures: 0, Errors: 0, Skipped: 0 |
| Java Testcontainers（`StrategyWorkflowIntegrationTest`） | Tests run: 1, Failures: 0, Errors: 0, Skipped: 0 |
| `ruff check app tests`（ai-adapter） | **Found 8 errors**（RUF003） |
| `ruff check scripts tests`（data） | All checks passed |
| `api-contracts/typescript` typecheck | 通过 |
| 依赖 pin 守卫 | 12 个精确 pin 全部与已安装版本相符 |
| LangChain 跳过守卫 | 正常路径判过；在人为复现事故的副本上判失败（27 skipped 被抓出） |

198 + 1 = 199，与仓库既有的 Java 测试总数一致，这也反过来确认了
`-Dtest='!StrategyWorkflowIntegrationTest'` 这个排除没有多切或少切。

**未运行**：

- 整个 workflow 在 GitHub Actions 上的实际运行（本仓库此前无 CI，这是第一份配置）；
- `storefront` / `operations-console` 的 `npm ci` + `typecheck`（本机 Node 是 v25.7.0，
  与 CI 选定的 24 不是同一个大版本，在这里跑出来的结论不能代表 CI）；
- `make up`、`make data`、`make seed`、`make test-e2e`、`make evaluate*`（均不在 CI 范围内）。
