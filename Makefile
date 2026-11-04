.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv

.PHONY: help setup lint fmt test register run-keymaster run-sentinel up doctor e2e-mock

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

setup: ## Install Python 3.12 deps (incl. dev) into .venv
	$(UV) sync --python 3.12
	@mkdir -p .state .secrets/agents logs && chmod 700 .state .secrets .secrets/agents

lint: ## ruff check, ruff format --check, mypy on leasedesk/
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

fmt: ## Auto-fix lint and format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

test: ## Run the test suite
	$(UV) run pytest

doctor: ## Check tools, env, Switch health and credential file modes
	$(UV) run python scripts/doctor.py

register: ## Register LeaseDesk agents with Switch (Part 2)
	@echo "register: filled in by Part 2 (use scripts/spikes/register.py for the probe)"; exit 1

run-keymaster: ## Run the keymaster agent (Part 2)
	@echo "run-keymaster: filled in by Part 2"; exit 1

run-sentinel: ## Run the sentinel agent (Part 3)
	@echo "run-sentinel: filled in by Part 3"; exit 1

up: ## Start everything for a demo (Part 2)
	@echo "up: filled in by Part 2"; exit 1

e2e-mock: ## End-to-end run against moto (Part 2)
	@echo "e2e-mock: filled in by Part 2"; exit 1
