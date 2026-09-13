.PHONY: help setup db-up db-down migrate discover collect health test lint typecheck check backup restore

help:            ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

setup:           ## Create venv and install dev dependencies
	python -m venv .venv && .venv/bin/pip install -e ".[dev]"

db-up:           ## Start local Postgres
	docker compose up -d postgres

db-down:         ## Stop local Postgres
	docker compose down

migrate:         ## Apply migrations
	rateradar migrate

discover:        ## Refresh brand registry (add --apply to write)
	rateradar discover

collect:         ## Run one collection pass
	rateradar collect --trigger manual

health:          ## Health check (see docs/operations.md)
	rateradar health

test:            ## Run tests
	pytest --cov=rateradar --cov-report=term-missing

lint:            ## Lint and format check
	ruff check src tests && ruff format --check src tests

typecheck:       ## Static types
	mypy src

check: lint typecheck test  ## Everything CI runs

backup:          ## Dump the database
	@mkdir -p backups
	pg_dump "$$RATERADAR_DATABASE_URL" | gzip > backups/rateradar-$$(date +%F).sql.gz

restore:         ## Restore: make restore FILE=backups/rateradar-YYYY-MM-DD.sql.gz
	gunzip -c $(FILE) | psql "$$RATERADAR_DATABASE_URL"
