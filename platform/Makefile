SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help doctor bootstrap data up down seed test test-unit test-integration test-e2e evaluate logs clean-local

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

seed: ## Idempotently load commerce data and rebuild the search index
	@./data/scripts/seed.sh

test: test-unit test-integration ## Run all non-browser automated tests

test-unit: ## Run Java, Python, TypeScript and data unit tests
	@./scripts/test-unit.sh

test-integration: ## Run service contract and governed policy workflow tests
	@./scripts/test-integration.sh

test-e2e: ## Run storefront journey and operations-console Playwright smoke tests
	@cd tests/e2e && npm ci && npx playwright install chromium && npm test

evaluate: ## Run reproducible ESCI offline evaluation and save real output
	@./data/scripts/evaluate.sh

logs: ## Follow core-service logs
	docker compose logs -f --tail=150 storefront operations-console commerce search-service ai-adapter

clean-local: ## Remove only regenerable project containers, volumes and local runtime data
	@./scripts/clean-local.sh
