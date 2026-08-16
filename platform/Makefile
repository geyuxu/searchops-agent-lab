SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help doctor bootstrap data up down seed test test-unit test-integration test-e2e evaluate evaluate-ai logs clean-local \
        infra-up infra-down dev-info dev-search dev-ai dev-store dev-console dev-commerce

# 只有这三个是真正的基础设施：有状态、版本敏感、装在本机很麻烦。
# 其余五个服务是应用代码，容器化只会拖慢开发反馈回路（改一行 Java 要重打 Maven 镜像、
# Next.js 没有热重载、调试器挂不上），所以提供 dev-* 目标让它们跑在本机进程里。
INFRA := postgres redis elasticsearch

# 本地开发环境：先取 .env（含密钥），再叠加 dev.env（把 Docker 主机名换成 localhost）。
# set -a 让后续赋值自动导出为环境变量，供子进程读取。
DEV_ENV := set -a; . ./.env; . ./dev.env; set +a;

# 仓库统一用 JDK 21；macOS 系统默认 java 常常是别的版本。
JAVA21 := export JAVA_HOME="$$(/usr/libexec/java_home -v 21)"; export PATH="$$JAVA_HOME/bin:$$PATH";

help:
	@awk 'BEGIN {FS = ":.*##"; print "Commerce SearchOps Lab\n"} /^[a-zA-Z_-]+:.*##/ {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

doctor: ## Check host tools, Docker and local configuration
	@./scripts/doctor.sh

bootstrap: ## Install and lock all dependencies, then prepare local configuration
	@./scripts/bootstrap.sh

data: ## Download and build the configured deterministic ESCI subset
	@./data/scripts/run-data.sh

up: ## Build and start all core services
	docker compose up -d --build
	@./scripts/wait-healthy.sh

down: ## Stop project containers without deleting data
	docker compose down

# ---------------------------------------------------------------------------
# 本地开发路径：基础设施跑容器，应用跑本机进程
#
# 与 make up 并存而非取代它。make up 用于演示与 CI（一条命令拉起整套系统，
# 这是验收标准之一）；dev-* 用于开发迭代，改代码不必重建镜像。
# 典型用法：一个终端 make infra-up，其余每个服务各开一个终端跑对应的 dev-* 目标。
# ---------------------------------------------------------------------------

infra-up: ## Start only stateful infrastructure (postgres, redis, elasticsearch)
	docker compose up -d $(INFRA)
	@echo "等待基础设施就绪..."
	@until [ "$$(docker compose ps --format '{{.Health}}' $(INFRA) | grep -c healthy)" = "$(words $(INFRA))" ]; \
		do sleep 2; done
	@echo "基础设施就绪：postgres:5432 · redis:6379 · elasticsearch:9200"
	@$(MAKE) --no-print-directory dev-info

infra-down: ## Stop infrastructure containers, keep volumes
	docker compose stop $(INFRA)

dev-info: ## Show how to run each application locally
	@echo ""
	@echo "在各自的终端里启动应用（改代码即时生效，无需重建镜像）："
	@echo "  make dev-search     搜索服务   :8080  (改 Java 后需 Ctrl-C 重跑；未装 devtools)"
	@echo "  make dev-ai         AI 适配器  :8000  (uvicorn --reload)"
	@echo "  make dev-commerce   商城服务   :9000"
	@echo "  make dev-store      商城前台   :3000  (Next.js HMR)"
	@echo "  make dev-console    运营后台   :3001  (Next.js HMR)"
	@echo ""
	@echo "连接串由 dev.env 覆盖为 localhost；密钥仍只存在于 .env。"
	@echo "若某个服务你不打算改，让它继续跑在容器里即可：docker compose up -d <service>"
	@echo ""

dev-search: ## Run the search service as a local process
	@$(DEV_ENV) $(JAVA21) cd services/search-service && mvn spring-boot:run

dev-ai: ## Run the AI adapter as a local process with hot reload
	@$(DEV_ENV) cd services/ai-adapter && \
		.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

dev-commerce: ## Run the commerce service as a local process
	@$(DEV_ENV) cd services/commerce && npm run dev

dev-store: ## Run the storefront with hot module reload
	@$(DEV_ENV) cd apps/storefront && npm run dev

dev-console: ## Run the operations console with hot module reload
	@$(DEV_ENV) cd apps/operations-console && npm run dev

seed: ## Idempotently load commerce data and rebuild the search index
	@./data/scripts/seed.sh

test: test-unit test-integration ## Run all non-browser automated tests

test-unit: ## Run Java, Python, TypeScript and data unit tests
	@./scripts/test-unit.sh

test-integration: ## Run service contract and governed policy workflow tests
	@./scripts/test-integration.sh

test-e2e: ## Run storefront journey and operations-console Playwright smoke tests
	@cd tests/e2e && npm ci && npx playwright install chromium && npm test

# 两个独立目标而不是 make evaluate USE_AI=1：本 Makefile 通篇是"一个动作一个目标"，
# 且 help 的 awk 只扫 `目标: ## 说明`，变量开关在 make help 里根本看不见。
# 两个目标写两个文件（evaluation-latest.json / evaluation-ai-latest.json），
# 谁也不会覆盖谁，A/B 两份结果可以直接对比。
evaluate: ## Run reproducible ESCI offline evaluation and save real output
	@./data/scripts/evaluate.sh

evaluate-ai: ## Run the same offline evaluation with AI query rewriting enabled
	@./data/scripts/evaluate.sh --use-ai

logs: ## Follow core-service logs
	docker compose logs -f --tail=150 storefront operations-console commerce search-service ai-adapter

clean-local: ## Remove only regenerable project containers, volumes and local runtime data
	@./scripts/clean-local.sh
