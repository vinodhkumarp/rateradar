.PHONY: help setup db-up db-down migrate discover collect health test lint typecheck check backup restore \
        local-up local-deploy local-invoke local-logs local-down package

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

package:         ## Build the Lambda deployment zip
	./deploy/build_package.sh

local-up:        ## Start Floci + Postgres, deploy the function locally, migrate
	./deploy/local_floci.sh

local-deploy:    ## Rebuild and update the local function only
	./deploy/build_package.sh
	AWS_ENDPOINT_URL=$${AWS_ENDPOINT_URL:-http://localhost:4566} \
	AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=ap-southeast-2 \
	aws lambda update-function-code --function-name rateradar \
	  --zip-file fileb://build/rateradar.zip >/dev/null && echo "local function updated"

local-invoke:    ## Invoke locally: make local-invoke CMD=collect|discover|backup|migrate
	./deploy/local_invoke.sh $(or $(CMD),collect)

local-logs:      ## Follow the emulated function's logs
	AWS_ENDPOINT_URL=$${AWS_ENDPOINT_URL:-http://localhost:4566} \
	AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=ap-southeast-2 \
	aws logs tail /aws/lambda/rateradar --follow

local-down:      ## Stop Floci and Postgres (data in Postgres survives)
	floci stop || true
	docker compose down

backup:          ## Dump the database
	@mkdir -p backups
	pg_dump "$$RATERADAR_DATABASE_URL" | gzip > backups/rateradar-$$(date +%F).sql.gz

restore:         ## Restore: make restore FILE=backups/rateradar-YYYY-MM-DD.sql.gz
	gunzip -c $(FILE) | psql "$$RATERADAR_DATABASE_URL"
