# 部署与运维

本文覆盖 `platform/` 下电商搜索系统的两条运行路径、环境变量、资源要求与停机清理。
故障排查另见 [`troubleshooting.md`](troubleshooting.md)；`ai_status` / `rerank_status` 的逐状态语义、
评测的操作细节见 [`../platform/docs/runbook.md`](../platform/docs/runbook.md)，本文不重复。

所有命令的工作目录都是 `platform/`（Makefile 所在目录），除非另有说明。

---

## 数据边界声明

启动之后你在前台和后台看到的一切，来源分两类，任何对外表述都必须保持这个区分：

- **公开真实数据**：商品标题、品牌、描述、bullet point、颜色、商品 ID、搜索查询与查询 ID、
  ESCI 相关性标注（E/S/C/I）、上游 train/test 划分。来自
  [amazon-science/esci-data](https://github.com/amazon-science/esci-data)（Apache-2.0），
  由 `make data` 从官方源下载，本仓库不转发这份数据。
- **确定性模拟数据**：价格、库存、展示类目、热度、占位色相，以及用户、搜索流量、点击、
  购物车与订单。全部由 `product_id + DATA_SAMPLE_SEED` 的稳定哈希派生
  （见 `platform/data/scripts/process.py` 与 `simulate_traffic.py`），同样的输入与种子产出同样的值。

**这些模拟字段不是亚马逊的交易、行为、库存或价格数据，任何文档、演示与截图都不得这样表述。**
商品配图是本地按模拟色相生成的 CSS 组合，不抓取也不外链任何亚马逊图片。
完整来源与校验方式见 [`../platform/docs/data-provenance.md`](../platform/docs/data-provenance.md)。

---

## 两条路径

| | 路径一：容器 | 路径二：本机开发 |
|---|---|---|
| 一键入口 | `make up` / `make down` | `make demo-up` / `make demo-down` |
| 逐服务入口 | `docker compose up -d <service>` | `make infra-up` + 各 `make dev-*` |
| 容器里跑什么 | 全部 8 个服务 | 只有 postgres / redis / elasticsearch |
| 本机进程跑什么 | 无 | 搜索服务、AI 适配器、商城服务、前台、后台（按需，可只起你要改的） |
| 连接串来源 | `platform/.env`（Docker 网络主机名） | `.env` 叠加 `platform/dev.env`（覆盖成 `localhost`） |
| 改一行代码的代价 | 重建镜像 | Java 重启进程；Python/Next.js 热重载 |
| 适合 | 演示、验收、CI、"一条命令拉起整套" | 日常迭代、调试器挂载、反复改前端 |

两条路径共用同一份 `platform/.env`，也就是同一套密钥。`dev.env` 里**不含任何凭据**，
它只把 `DATABASE_URL` / `REDIS_URL` / `ELASTICSEARCH_URL` / `SEARCH_SERVICE_URL` /
`COMMERCE_SERVICE_URL` / `AI_ADAPTER_URL` 六个连接串的主机名换成 `localhost`，因此可以安全提交。

**路径二为什么必须存在。** 五个应用服务容器化之后，改一行 Java 要重打一次 Maven 镜像，
Next.js 拿不到热重载，调试器也挂不上——反馈回路从秒级掉到分钟级。所以有状态、版本敏感、
本机安装麻烦的三个基础设施留在容器里，应用跑本机进程。这不是替代 `make up`：
`make up` 是演示与 CI 的验收入口，两者并存。

---

## 前置条件

```bash
make doctor       # 逐项检查下列工具，缺什么直接列出来
```

`scripts/doctor.sh` 检查：Docker、Docker Compose、Docker daemon（同时打印可用 CPU 与内存）、
Node、npm、Python 3、uv、Maven、JDK 21（macOS 走 `/usr/libexec/java_home -v 21`）、
以及 `platform/.env` 是否存在。它以失败项数量作为退出码。

宿主机 Node 版本与容器内不一致是可以接受的：Node 应用在钉死版本的容器里构建，
`doctor.sh` 末尾也明确写了这一点。但**路径二**下前台/后台/商城跑的是宿主机 Node，
版本差异这时才真正生效。

安装依赖（只需一次，之后依赖变更时重跑）：

```bash
make bootstrap
```

`scripts/bootstrap.sh` 会：`.env` 不存在时从 `.env.example` 复制一份；建 `platform/.venv-data`
与 `platform/services/ai-adapter/.venv` 两个 venv 并按 `requirements-dev.txt` 安装；
对 commerce / storefront / operations-console / tests-e2e 四处执行 `npm ci`；
最后 `mvn dependency:go-offline` 预热 Maven 本地仓库。

> venv 不可搬迁：这两个 venv 里的 `uvicorn` / `pytest` / `ruff` 都是带绝对路径 shebang 的
> console script。仓库目录改名或移动之后必须删掉重跑 `make bootstrap`，
> 详见 [`troubleshooting.md`](troubleshooting.md) 的第 7 条。

---

## 路径一：容器（演示 / CI）

### 命令序列

```bash
cd platform
make doctor        # 1. 检查宿主工具与 Docker
make bootstrap     # 2. 装依赖、生成 .env（首次）
make data          # 3. 下载并确定性采样 ESCI（首次最慢，见"资源要求"）
make up            # 4. 构建并启动全部 8 个服务，内置健康等待
make seed          # 5. 灌 PostgreSQL、重建 ES 索引、打一批模拟流量
make evaluate      # 6. 跑一轮可复现的离线评测
```

`make up` = `docker compose up -d --build` 再接 `scripts/wait-healthy.sh`。
后者最多等 360 秒，逐个服务打印未就绪原因（`服务名:状态`），全部就绪才返回 0；
超时会打印 `docker compose ps` 并以 1 退出。所以 `make up` 返回成功本身就是一次验证。

### 端口表

全部端口都绑在 `127.0.0.1`，不对外暴露。

| 服务 | 端口 | 用途 | 健康检查 |
|---|---|---|---|
| postgres | 5432 | 主库 `searchops` + 独立库 `commerce` | `pg_isready` |
| redis | 6379 | 商城会话/缓存 | `redis-cli ping` |
| elasticsearch | 9200 | BM25 检索 | `_cluster/health?wait_for_status=yellow` |
| ai-adapter | 8000 | FastAPI，改写 / 重排 provider | `GET /ai/health` |
| search-service | 8080 | Java 21 搜索与策略治理 API | `GET /actuator/health/readiness` |
| commerce | 9000 | Medusa 商城服务 | `GET /health` |
| storefront | 3000 | 商城前台（Next.js） | `GET /api/health` |
| operations-console | 3001 | 运营后台（Next.js） | `GET /api/health` |

`commerce` 连的是同一个 postgres 实例上的 **`commerce`** 库，
由 `infra/docker/init-databases.sql`（`CREATE DATABASE commerce;`）在首次初始化卷时创建。
删卷重建才会重新执行这个初始化脚本。

### 怎么确认起来了

```bash
docker compose ps                                            # 期望 8 个容器，状态 healthy
curl -fsS http://localhost:8080/actuator/health/readiness     # 搜索服务
curl -fsS http://localhost:8000/ai/health                     # 适配器（会回报当前 provider）
curl -fsS http://localhost:9000/health                        # 商城
curl -fsS http://localhost:3000/api/health                     # 前台
curl -fsS http://localhost:3001/api/health                     # 后台
curl -fsS http://localhost:9200/_cluster/health                # ES
```

再确认数据真的进去了（`make seed` 之后）：

```bash
curl -s "http://localhost:8080/api/v1/search?q=headphones&size=3" | head -c 400
```

返回里有 `products` 且非空，才说明 ES 索引建好了。只连得上端口不等于有数据——
索引未建时搜索会报 index-not-found，这是 `make seed` 漏跑的典型表现。

日志：`make logs` 只跟随五个应用服务的日志（storefront / operations-console / commerce /
search-service / ai-adapter），不混进基础设施噪声。

### 适合什么场景

一条命令拉起整套系统，是本项目的验收标准之一，也是 CI 里跑 E2E（`tests/e2e` 的 Playwright，
需要完整栈在跑）的唯一可行形态。演示时也用它：状态全在命名卷里，`make down` 不丢数据。

---

## 路径二：本机开发

### 一键启停：`make demo-up` / `make demo-down`

手工做法要开五个终端，而且没人替你确认"到底起没起来"。这两个目标把整套流程收成一条命令：

```bash
cd platform
make bootstrap     # 首次：装依赖并生成 .env
make data          # 首次：下载并采样 ESCI
make demo-up       # 起基础设施 → 等健康 → 按依赖顺序后台拉起五个应用 → 逐个探健康端点 → 打印汇总表
make seed          # 首次或换过数据：灌库并重建 ES 索引
…
make demo-down     # 按 pid 文件优雅停进程 → docker compose stop → 残留检查
```

`demo-up` 的行为要点（`scripts/demo-up.sh`）：

- **环境变量与 `make dev-*` 完全一致**：先 `. .env` 再 `. dev.env`，`set -a` 自动导出。
  两个文件缺任何一个都直接报错退出，不会带着半套配置往下跑。
- **启动顺序与 compose 的 `depends_on` 一致**：ai-adapter → search-service → commerce →
  storefront / operations-console。每个服务有各自的就绪预算：
  ai-adapter 90s、search-service 与 commerce 各 420s、两个 Next.js 各 240s
  （Maven 冷编译和 `medusa develop` 的首次迁移都可能跑到分钟级；Next.js 的
  `/api/health` 是被探测请求本身触发编译的，给太紧会假失败）。
- **就绪判定用带超时的 HTTP 探测，不是端口连通性**。这是刻意的：
  `uvicorn --reload` 的父进程在子进程 import 失败时仍持有监听 socket，
  端口探测会给出假成功（见 [`troubleshooting.md`](troubleshooting.md) 第 3 条）。
- **幂等**，重复执行安全。三种情况分开处理：pid 文件里的进程还活着 → 只重新确认健康；
  端口上已有健康服务但不是它起的（比如 `make up` 的容器，或你手工开的 `make dev-*`）
  → 直接复用并跳过；端口被占但探测不健康 → 打印占用者并明确报冲突，
  **不静默继续，也不去杀别人的进程**。
- 启动失败时打印该服务日志的最后 20 行，并给出与本仓库真实坑对应的排查提示
  （例如 ai-adapter 会直接提醒去找 import traceback），而不是通用套话。

`demo-down` 的行为要点（`scripts/demo-down.sh`）：

- 停止顺序与启动相反（先停依赖方）；
- 主路径按进程组停：`demo-up` 用 `set -m` 让每个服务拿到独立进程组，
  所以能精确停掉 `mvn→java`、`npm→next`、`uvicorn→reload worker` 整棵子树；
- pid 文件失效时的兜底匹配是 **"监听该端口" 且 "cwd 在本项目目录之内"** 双重命中——
  不按命令名匹配（`node` / `java` 满机器都是），因此不会误杀你另一个项目的 Next.js；
  cwd 不在本项目下的占用者只报告、不触碰；
- **只 `docker compose stop`，永远不传 `-v` / `--volumes`**，卷里的 ES 索引、
  策略版本与审计流一条都不动。要清空数据请显式走 `make clean-local`；
- 最后做残留检查：逐个报告 3000/3001/8000/8080/9000 是否释放、
  哪些容器还在跑、以及列出保留下来的数据卷作为"没删数据"的证据。

运行产物：

```
platform/.runtime/pid/<service>.pid
platform/.runtime/logs/<service>.log      # 追加写，每次 demo-up 带时间戳分隔
```

`.runtime/` 已被 gitignore，`make clean-local` 会整个删掉它。

**`demo-up` 只负责把服务拉起来，不灌数据。** 首次或换过数据之后仍需另开终端跑 `make seed`。

单独调某个服务时仍然用 `dev-*`：把该服务从 demo 环境里停掉，再在自己的终端里跑，
这样它的日志和调试器都在你眼前。

### 逐服务启动（手工）

```bash
cd platform
make infra-up      # 只起 postgres / redis / elasticsearch，等到三个都 healthy，然后打印 dev-info
```

随后**每个服务各开一个终端**（`dev-*` 目标都在前台运行）：

```bash
make dev-search    # :8080  Spring Boot，Makefile 自己选 JDK 21
make dev-ai        # :8000  uvicorn --reload
make dev-commerce  # :9000
make dev-store     # :3000  Next.js HMR
make dev-console   # :3001  Next.js HMR
```

只起你要改的那些。不打算改的服务让它继续跑在容器里即可：`docker compose up -d <service>`。
`make dev-info` 随时重印这份清单。

首次或换数据之后仍需灌数据，另开一个终端：

```bash
make seed          # 要求 search-service 已在 :8080 应答，且 postgres 已就绪
```

### 环境变量是怎么叠的

Makefile 里这一行是路径二的全部秘密：

```make
DEV_ENV := set -a; . ./.env; . ./dev.env; set +a;
```

先加载 `.env`（含密钥），再把 `dev.env` 叠在上面（只覆盖六个连接串的主机名），
`set -a` 让赋值自动导出给子进程。因此：**密钥永远只存在于 `.env`，`dev.env` 可提交**。

Java 侧还额外套了一层：

```make
JAVA21 := export JAVA_HOME="$$(/usr/libexec/java_home -v 21)"; export PATH="$$JAVA_HOME/bin:$$PATH";
```

macOS 系统默认 `java` 常常不是 21，这一行保证 `make dev-search` 用的是 JDK 21，
不依赖你的 shell 里 `JAVA_HOME` 指向哪儿。

### 端口表

与路径一完全一致（见上表）。区别只在谁在监听：三个基础设施端口由容器监听，
其余五个由你启动的本机进程监听。**同一个端口不能两边都占**——
如果某个服务的容器还在跑，`make dev-*` 会启动失败或连到错误的进程。
停单个容器：`docker compose stop search-service`，不要停整套。

### 怎么确认起来了

走 `make demo-up` 时，**成功退出本身就是验证**：它对每个服务都探过健康端点才继续，
任何一个没在预算内就绪都会以非零码退出。最后打印的汇总表逐行给出
服务 / 地址 / 状态（`started` / `already-running` / `reused`）/ pid，
以及日志与 pid 文件的位置。`make demo-down` 的残留检查表则是"确实收干净了"的对应证据。

走手工路径时，`make infra-up` 自带等待循环：它一直轮询直到三个容器的 `{{.Health}}`
都是 `healthy`，然后打印 `基础设施就绪：postgres:5432 · redis:6379 · elasticsearch:9200`。
看到这行才算成功。

应用侧只对你**实际启动了的**那些服务做检查，用上表里对应的健康端点。
特别地，AI 适配器要用带超时的方式探：

```bash
curl -m 3 -fsS http://localhost:8000/ai/health
```

不加 `-m` 的话，`uvicorn --reload` 子进程 import 失败时会表现成"连得上但一直没响应"，
你会以为是网络慢。原因见 [`troubleshooting.md`](troubleshooting.md) 第 3 条。

确认改写链路真的通（而不是静默降级成纯 BM25）：

```bash
curl -s "http://localhost:8080/api/v1/search?q=noise%20cancelling%20headphones&size=1&use_ai=true"
```

看响应里的 `ai_status`，不要看 HTTP 状态码——降级时 HTTP 照样 200。
逐状态含义见 runbook。

### 适合什么场景

日常改代码。Python 与 Next.js 有热重载，Java 需要 Ctrl-C 重跑（未装 devtools）。
调试器可以直接挂到本机进程上。**注意 `uvicorn --reload` 只重载代码、不重载环境变量**：
改完 `.env` 或 `dev.env` 必须重启 uvicorn 父进程。

---

## 环境变量

真值只写在 `platform/.env`（已被两级 `.gitignore` 忽略）。仓库里可提交的是
`platform/.env.example`（登记变量名与非密钥默认值）与 `platform/dev.env`（只覆盖主机名）。

> **Compose 的一个陷阱**：`platform/.env` 只被 Docker Compose 用来做 `${VAR}` 插值。
> `ai-adapter` / `search-service` / `commerce` 都没有 `env_file`，
> 任何要进入容器的变量都必须显式写进该服务的 `environment:` 块里，
> 否则它在 `.env` 里存在、在容器里却读不到。加新变量时这一步最容易漏。

### 编排与基础设施

| 变量 | 默认值 | 用途 |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | `commerce-searchops-lab` | Compose 项目名，决定容器与命名卷的前缀；`clean-local.sh` 靠它只删本项目资源 |
| `POSTGRES_USER` | `searchops` | 主库用户名 |
| `POSTGRES_PASSWORD` | 见 `.env.example` | **密钥位**。仅本机演示口令；容器与所有连接串共用它 |
| `POSTGRES_DB` | `searchops` | 主库名。`commerce` 库另由 init SQL 创建 |
| `DATABASE_URL` | `postgresql://…@postgres:5432/searchops` | 数据脚本与本机进程用的连接串；`dev.env` 覆盖为 `localhost` |
| `REDIS_URL` | `redis://redis:6379` | 同上，`dev.env` 覆盖为 `localhost` |
| `ELASTICSEARCH_URL` | `http://elasticsearch:9200` | 同上，`dev.env` 覆盖为 `localhost` |
| `ELASTICSEARCH_INDEX_ALIAS` | `products-read` | 搜索读别名。索引重建走别名切换，改它等于换一整套索引 |
| `LOG_LEVEL` | `INFO` | 同时作用于 search-service（`logging.level.root` 与 `lab.searchops`）和 ai-adapter |

### 服务发现

| 变量 | 默认值 | 用途 |
|---|---|---|
| `SEARCH_SERVICE_URL` | `http://search-service:8080` | 服务端到服务端；`dev.env` 覆盖为 `localhost` |
| `COMMERCE_SERVICE_URL` | `http://commerce:9000` | 同上 |
| `AI_ADAPTER_URL` | `http://ai-adapter:8000` | 搜索服务调适配器的地址；同上 |
| `NEXT_PUBLIC_SEARCH_SERVICE_URL` | `http://localhost:8080` | **浏览器侧**读取，本来就是 localhost，故 `dev.env` 不覆盖 |
| `NEXT_PUBLIC_COMMERCE_SERVICE_URL` | `http://localhost:9000` | 同上 |

`NEXT_PUBLIC_*` 在容器路径下同时作为 Dockerfile 的 build arg 传入——它们被编译进前端产物，
改完必须重建镜像，只重启容器不生效。

### AI 边界（可选增强，永远不是启动依赖）

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AI_ENABLED` | `true` | 全局 kill switch。`false` 时搜索服务完全不联系适配器，响应 `ai_status=DISABLED`、`ai_applied=false`，退回纯 BM25。三处默认值（`.env.example` / `docker-compose.yml` / `application.yml`）已对齐为 `true` |
| `AI_PROVIDER` | `mock` | `mock` 或 `module.path:ClassName`（由 `app/provider.py` 的 `load_provider` 解析）。换真实 provider 只改这一处 |
| `AI_TIMEOUT_MS` | `5000` | 搜索服务等**改写**结果的读超时。真实 LLM 的首字节延迟以秒计，太小会让每次调用都超时降级而 HTTP 仍 200 |
| `AI_CONNECT_TIMEOUT_MS` | `1000` | 建连超时，与读超时分开计，适配器不可达时快速失败 |
| `AI_MODEL` | 空 | 模型标识。留空时 LangChain provider 走默认档并打 `ai.provider.defaults_applied` WARN |
| `AI_API_KEY` | 空 | **密钥位**。`AI_API_KEY_ENV` 未设置时的默认取值来源 |
| `AI_API_BASE_URL` | 空 | 自建网关 / 兼容端点 base URL；留空用默认档端点 |
| `AI_TEMPERATURE` | `0` | 采样温度。评测要可复现就保持 0 |
| `AI_MAX_TOKENS` | `512` | 单次输出上限，防止超长响应吃掉超时预算 |
| `AI_REQUEST_TIMEOUT_MS` | `4000` | provider 自身调模型的超时。**必须小于 `AI_TIMEOUT_MS`**，否则 Java 先超时降级而适配器还在跑，白占一个上游连接 |

### LangChain provider 专用

启用方式：`AI_PROVIDER=app.providers.langchain_rewrite:LangChainRewriteProvider`。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AI_API_KEY_ENV` | 空（等价于 `AI_API_KEY`） | 存的是**变量名**，不是 key。详见下一节 |
| `AI_MAX_RETRIES` | 空 = `0` | provider 内部重试次数。默认 0 是刻意的：单次预算 4000ms 已接近 5000ms 读超时，任何重试都必然让 Java 先降级走人，重试只是白占上游连接并继续计费 |
| `AI_STRUCTURED_OUTPUT_METHOD` | 空 = `function_calling` | `function_calling` / `json_mode` / `json_schema`。不用 LangChain 默认的 `json_schema`，因为兼容端点支持面不齐 |
| `AI_EXTRA_BODY` | 空 | 透传到请求体**顶层**的厂商私有参数（一段 JSON）。关思考模式靠它，见 [`troubleshooting.md`](troubleshooting.md) 第 2 条 |

#### `AI_API_KEY_ENV` 的间接读取设计

这个变量存的是**另一个环境变量的名字**，不是 key 本身。provider 的取值逻辑
（`app/providers/langchain_rewrite.py` 的 `_api_key`）是两步：

1. `key_var = os.getenv("AI_API_KEY_ENV") or "AI_API_KEY"` —— 读出"key 存在哪个变量里"；
2. `api_key = os.getenv(key_var)` —— 再按那个名字去环境里取真值。

于是：

```
AI_API_KEY_ENV=DASHSCOPE_API_KEY     # 配置文件里只出现变量名
DASHSCOPE_API_KEY=…                  # 真值只在你的 shell / .env 里，不出现在任何提交物中
```

这样做的收益有三个，都不是形式主义：

- 各厂商的凭据变量名不同（`DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` / …），
  换厂商时改一行配置即可，不必把凭据复制进第二个变量；
- key 的值只经 `os.environ` 读取一次，**不出现在配置项、日志、异常文本与代码里**——
  异常信息只会说"`AI_API_KEY_ENV` 指向 `X`，但 `X` 未设置"，不会回显任何值；
- 取不到 key 时构造函数**直接抛异常、服务起不来**，而不是静默退回 mock。
  这是刻意为之：静默退化会把"模型根本没跑"伪装成"AI 没效果"，
  再把这份假指标写进评测结论。宁可起不来。

代价必须一起说清：本机 `uvicorn --reload` 下，import 期抛异常会表现成"端口连得上但无人应答"，
Java 侧记成整片 `TIMEOUT`。这条链路见 [`troubleshooting.md`](troubleshooting.md) 第 3 条。

### 数据流水线

| 变量 | 默认值 | 用途 |
|---|---|---|
| `PRODUCT_LIMIT` | `20000` | 采样后的商品条数。必须 ≥ `QUERY_LIMIT`，否则 `process.py` 直接报错 |
| `QUERY_LIMIT` | `10000` | 采样后的查询条数 |
| `EVALUATION_QUERY_LIMIT` | `200` | `make evaluate` / `make evaluate-ai` 送去评测的查询条数 |
| `DATA_SAMPLE_SEED` | `searchops-lab-v1` | 采样与全部模拟字段的种子。**改它等于换一份数据集**，新旧指标不可比 |
| `ESCI_PRODUCTS_SHA256` | 见 `.env.example` | 官方 products parquet 的 SHA-256 钉子；留空则跳过精确校验 |
| `ESCI_EXAMPLES_SHA256` | 见 `.env.example` | 官方 examples parquet 的 SHA-256 钉子 |

`COMMERCE_DATABASE_URL` 只被 `data/scripts/seed_commerce.py` 读取，**未登记在 `.env.example`**，
默认 `postgresql://searchops:…@localhost:5432/commerce`。要连别处才需要显式导出。

### 密钥位一览

下面这些是仓库里唯一的凭据槽，`.env.example` 中只保留可提交的本地演示占位串，
**任何对外环境都必须替换**，且真值只能写进本机 `platform/.env`：

- `POSTGRES_PASSWORD`
- `JWT_SECRET`（Medusa 商城签发 token）
- `COOKIE_SECRET`（Medusa 会话 cookie）
- `SEARCHOPS_APPROVAL_SECRET`（策略发布的审批令牌签名密钥；令牌是版本特定的，且不写日志）
- `AI_API_KEY`，以及 `AI_API_KEY_ENV` 所指向的那个变量（例如 `DASHSCOPE_API_KEY`）

---

## 资源要求

### Docker 内存

`docker-compose.yml` 给每个服务都设了 `mem_limit`，加总就是路径一的内存下界：

| 服务 | `mem_limit` | 备注 |
|---|---|---|
| elasticsearch | 1536m | 其中 JVM 堆 1g（`ES_JAVA_OPTS`） |
| commerce | 1536m | Node 老生代上限 1024m（`NODE_OPTIONS`） |
| search-service | 640m | |
| postgres | 512m | |
| storefront | 384m | |
| operations-console | 384m | |
| redis | 256m | 自身还有 `--maxmemory 192mb` + `allkeys-lru` |
| ai-adapter | 256m | |
| **合计** | **5504m ≈ 5.4 GB** | |

据此：

- **路径一**：Docker Desktop 至少分配 **8 GB**。5.4 GB 是稳态限额之和，
  `--build` 期间 Maven 与 Next.js 构建还会额外吃内存，留不出余量会在构建阶段被 OOM kill。
- **路径二**：容器侧只有三个基础设施，合计 `1536 + 512 + 256 = 2304m ≈ 2.25 GB`，
  分配 **4 GB** 足够。剩下的内存留给宿主机上的 JVM 与两个 Next.js dev server。

CPU 与内存的实际可用量可以直接看：`make doctor` 会打印
`server <version> · <N> CPUs · <bytes> bytes`。

### Elasticsearch 的内存限制改在哪

两处，都在 `platform/docker-compose.yml` 的 `elasticsearch` 服务下，**必须一起改**：

```yaml
environment:
  ES_JAVA_OPTS: -Xms1g -Xmx1g      # ① JVM 堆，Xms 与 Xmx 保持相等
mem_limit: 1536m                    # ② 容器内存上限
```

① 是堆，② 是容器总量。ES 除堆之外还要用 Lucene 的堆外内存与文件缓存，
所以 ② 必须显著大于 ①。想省内存就同比缩小，例如 `-Xms512m -Xmx512m` 配 `mem_limit: 1g`；
反过来只调大堆而不动 `mem_limit`，容器会在 GC 压力上来时被直接 OOM kill——
表现是 ES 容器无预警重启，健康检查转红，而 ES 自己的日志里什么都看不到。

### 默认数据量与如何调小

| 项 | 默认 | 实测占用 |
|---|---|---|
| `data/raw/`（官方 parquet 原件） | products + examples 两个文件 | **1.1 GB**（`du -sh` 实测） |
| `data/processed/`（采样产物） | 20000 商品 / 10000 查询 | **30 MB**（`du -sh` 实测） |
| 评测规模 | `EVALUATION_QUERY_LIMIT=200` | — |

调小的方式是在 `.env` 里改，然后重跑：

```bash
# 例：缩到 1/10
PRODUCT_LIMIT=2000
QUERY_LIMIT=1000
```

```bash
make data && make seed     # 重新采样并重灌，两步都要
```

三件事必须知道：

1. **调小 `PRODUCT_LIMIT` / `QUERY_LIMIT` 不会减少下载量。** `download.py` 永远拉完整的两个
   官方 parquet（products 有 900 MB 的最小体积校验），采样是在下载之后用 DuckDB 做的。
   要省的是磁盘上的 processed 体积、ES 索引体积和 seed 时间，不是首次下载时间。
2. `PRODUCT_LIMIT` 必须 ≥ `QUERY_LIMIT`，否则 `process.py` 直接抛
   `PRODUCT_LIMIT must be at least QUERY_LIMIT to retain one judgment per query`。
   因为采样保证每条选中查询至少保留一条最高可得标注的商品。
3. **改了限额或种子，指标就不再与既有基线可比。** `manifest.json` 里的输出哈希会变，
   `experiments/` 与 `baselines/` 下的结论是在默认参数下产生的。想省资源就明确记下你换了参数，
   不要拿新数字去对旧结论。

`process.py` 内部还给 DuckDB 设了 `memory_limit='2GB'` 与 `threads=4`，
这是宿主机侧的采样开销，与 Docker 分配无关。

---

## 停止、清理与重置

| 命令 | 做什么 | 数据 |
|---|---|---|
| `make down` | `docker compose down` | 命名卷保留，下次 `make up` 数据还在 |
| `make demo-down` | 停五个本机应用进程 + `docker compose stop`，再做残留检查 | 卷保留（脚本从不传 `-v`） |
| `make infra-down` | `docker compose stop` 三个基础设施 | 卷保留 |
| `make clean-local` | 先校验项目根标记文件，再 `down --volumes --remove-orphans`，并清空 `data/raw`、`data/processed`、`.runtime` | **全删**，可用 `make data` 重建 |

`clean-local.sh` 在动手前会检查 `platform/.commerce-searchops-lab-root` 这个标记文件是否存在，
不存在就拒绝执行——防止在错误的目录里误删别的 Compose 项目。它只删本项目拥有的资源。

**手工起的 `dev-*` 前台进程要自己 Ctrl-C**：它们不受 Compose 管理，`make down` 碰不到；
`make demo-down` 也只在"端口 + cwd 双重命中"时才会兜底停掉它们。
走 `make demo-up` 起的进程则由 `make demo-down` 按 pid 文件负责收干净。

完整演示重置：

```bash
make clean-local
make data && make up && make seed && make evaluate
```

PostgreSQL 的项目卷被重建，因此策略版本与审计流也回到初始状态。

---

## 验证与测试

`make test` = `test-unit` + `test-integration`，不含浏览器测试。

| 目标 | 内容 |
|---|---|
| `make test-unit` | `scripts/test-unit.sh`：ai-adapter（先 `ruff check` 再 `pytest`，覆盖率门禁 `--cov-fail-under=85`）→ data（ruff + pytest）→ search-service `mvn test` → commerce `npm test && npm run build` → api-contracts 的 TS typecheck 与 Java 测试 → 三份契约 JSON 的语法校验 → storefront 与 operations-console 的 `typecheck` + `build` |
| `make test-integration` | `scripts/test-integration.sh`：`tests/integration_policy.py` + ai-adapter 的 `test_contract.py` |
| `make test-e2e` | `tests/e2e` 的 Playwright，**需要完整栈在跑**（走路径一最省事） |

`agent/` 的测试独立于 platform，用 `agent/` 下的 pytest 跑。

两个必须知道的点：

- **"绿在跳过上" 是这个仓库真实发生过的事故**：`test_providers_langchain.py` 用
  `requires_provider = pytest.mark.skipif(PROVIDER_CLASS is None, …)` 保护，
  `requirements.txt` 里钉死的 `langchain-core` / `langchain-openai` / `openai` / `tiktoken`
  没装上时，相关测试会被静默跳过而整体仍然显示通过。判断"绿"是否可信，
  要看 pytest 摘要里的 skipped 数量，不是看退出码。
- **测试不得依赖任何 API key**。LangChain 相关测试全部打桩，
  测试里还有一个 `no_network` fixture 会毒化 `socket.connect`——
  绕开 LangChain 自己发 HTTP 会在单测里当场炸掉。这条约束是被强制执行的，不是口头约定。

---

## 延伸阅读

| 文档 | 内容 |
|---|---|
| [`troubleshooting.md`](troubleshooting.md) | 七个真实踩过的坑：症状、根因、确认方法、修法 |
| [`../platform/docs/runbook.md`](../platform/docs/runbook.md) | 日常操作、`ai_status` 逐状态语义、评测与恢复 |
| [`../platform/docs/architecture.md`](../platform/docs/architecture.md) | 服务边界、请求与数据流、失败行为 |
| [`../platform/docs/data-provenance.md`](../platform/docs/data-provenance.md) | 数据来源、模拟边界与可复现性校验 |
| [`../platform/docs/ai-handoff.md`](../platform/docs/ai-handoff.md) | 接入真实模型的交接说明 |
| [`../platform/docs/adr/`](../platform/docs/adr/) | 运行时版本基线、治理与审计、ESCI 采样三份 ADR |
