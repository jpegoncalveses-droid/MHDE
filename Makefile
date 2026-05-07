# MHDE — developer convenience targets.
#
# Always uses venv/bin/python directly; never activates the venv (per
# CLAUDE.md project policy).
#
# Quick reference:
#   make test              full pytest suite
#   make test-unit         per-engine unit tests only (fast subset)
#   make test-integration  multi-engine + CLI tests
#   make test-regression   bug-regression tests (Session 5 fills these)
#   make coverage          pytest with coverage report (HTML in htmlcov/)
#   make install-hooks     wire scripts/pre-commit.sh into .git/hooks
#   make precommit         run the same checks the pre-commit hook does

PY := venv/bin/python
PYTEST := $(PY) -m pytest

# Network-touching tests we skip by default in unit / hook contexts.
# Override with `make NET_SKIPS= test-unit` to include them.
NET_SKIPS := \
	--ignore=tests/equity/test_alpha_vantage.py \
	--ignore=tests/equity/test_polygon.py \
	--ignore=tests/equity/test_fred.py \
	--ignore=tests/equity/test_finra.py \
	--ignore=tests/equity/test_cftc.py \
	--ignore=tests/equity/test_yahoo_historical.py \
	--ignore=tests/equity/test_ingest_gdelt_real.py \
	--ignore=tests/equity/test_company_ir.py \
	--ignore=tests/equity/test_nasdaq_earnings.py \
	--ignore=tests/equity/test_sec_edgar.py \
	--ignore=tests/equity/test_ingest_sec.py \
	--ignore=tests/equity/test_ingest_gdelt.py \
	--ignore=tests/equity/test_ingest_sector_etfs.py \
	--ignore=tests/equity/test_ingest_stooq.py \
	--ignore=tests/equity/test_stooq_historical.py \
	--ignore=tests/equity/test_earnings_estimates.py \
	--ignore=tests/equity/test_health.py

.PHONY: test test-unit test-integration test-regression coverage \
        install-hooks precommit help

help:
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
		awk -F':.*?##' '{printf "  %-22s %s\n", $$1, $$2}'

test: ## Run the full pytest suite (no network skips, no coverage)
	$(PYTEST) tests/ -q

test-unit: ## Per-engine unit tests, offline only — fast (<30s target)
	$(PYTEST) tests/equity tests/crypto tests/fx tests/dashboard \
		tests/test_session2_infra_smoke.py \
		-q --no-header $(NET_SKIPS)

test-integration: ## Multi-engine + CLI integration tests
	$(PYTEST) tests/integration -q --no-header

test-regression: ## Bug-regression tests (Session 5 will populate these)
	$(PYTEST) tests/regression -q --no-header

coverage: ## Run unit tests with coverage; HTML report at htmlcov/index.html
	$(PYTEST) tests/equity tests/crypto tests/fx tests/dashboard \
		tests/test_session2_infra_smoke.py \
		$(NET_SKIPS) \
		--cov=ml --cov=crypto --cov=fx --cov=pipelines \
		--cov=health --cov=storage --cov=outcomes --cov=missed \
		--cov-report=term --cov-report=html

install-hooks: ## Symlink scripts/pre-commit.sh into .git/hooks/pre-commit
	@if [ -f .git/hooks/pre-commit ] && [ ! -L .git/hooks/pre-commit ]; then \
		echo "WARN: .git/hooks/pre-commit exists and is not a symlink. Backing up to .git/hooks/pre-commit.bak"; \
		mv .git/hooks/pre-commit .git/hooks/pre-commit.bak; \
	fi
	ln -sf ../../scripts/pre-commit.sh .git/hooks/pre-commit
	chmod +x scripts/pre-commit.sh
	@echo "Pre-commit hook installed."

precommit: ## Run the pre-commit checks against staged + working tree
	@bash scripts/pre-commit.sh
