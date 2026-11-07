.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv

.PHONY: help setup lint fmt test register run-keymaster run-sentinel review up doctor e2e-mock moto check-access template

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

doctor: ## PASS/FAIL: file modes, Switch, agent keys, rooms, roles, package, policy docs, template, AWS profiles
	$(UV) run python scripts/doctor.py

register: ## Register keymaster and sentinel with Switch (idempotent; addressing anyone)
	$(UV) run python -m leasedesk.switch.register keymaster sentinel

moto: ## Run mock AWS (moto server, IAM auth on) for mock mode; keys go to .state/mock-aws.env
	$(UV) run python -m leasedesk.keymaster.mockaws

run-keymaster: ## Run the keymaster agent (mock mode needs `make moto` running)
	@mkdir -p logs
	$(UV) run python -m leasedesk.keymaster

run-sentinel: ## Run the sentinel reviewer agent alone (mock mode makes no AWS call)
	@mkdir -p logs .state && chmod 700 .state
	$(UV) run python -m leasedesk.sentinel

review: ## Offline review of a proposal file: make review P=path/to/proposal.json
	$(UV) run python -m leasedesk.sentinel.cli review $(P)

up: ## Mock demo: moto in the background, keymaster + sentinel supervised (Ctrl-C stops all)
	@mkdir -p logs .state && chmod 700 .state
	@$(UV) run python -m leasedesk.keymaster.mockaws > logs/moto.log 2>&1 & echo $$! > .state/moto.pid; \
	 trap 'kill $$(cat .state/moto.pid) 2>/dev/null' EXIT; \
	 for i in $$(seq 1 100); do [ .state/mock-aws.env -nt .state/moto.pid ] && break; sleep 0.2; done; \
	 [ .state/mock-aws.env -nt .state/moto.pid ] || { echo "mock AWS did not start; see logs/moto.log"; exit 1; }; \
	 echo "mock AWS up (logs/moto.log). Starting keymaster and sentinel..."; \
	 LEASEDESK_AWS_MODE=mock $(UV) run python scripts/demo/supervise.py

template: ## Regenerate switch/leasedesk.template.yaml from switch/briefs/
	$(UV) run python scripts/build_template.py

e2e-mock: ## End-to-end in one process: real keymaster + sentinel on FakeSwitch and moto (< 60 s)
	$(UV) run pytest -m e2e tests/e2e tests/keymaster/test_e2e_moto.py

check-access: ## Demo helper: ALLOWED/DENIED for lease-demo-dev every 10 s
	@scripts/demo/check_access.sh
