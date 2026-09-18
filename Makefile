APP_URL ?= http://localhost:5000

.PHONY: help up down logs reset inject faults test

help:
	@echo "up                 bring up the stack"
	@echo "down               tear it down"
	@echo "logs               follow logs"
	@echo "test               run the test suite (everything except llm)"
	@echo "reset              clear injected faults and reseed the target app"
	@echo "inject FAULT=slow  arm a fault on the target app"
	@echo "faults             show armed faults"

test:
	docker compose --profile test run --rm --build tests

up:
	docker compose up --build -d

down:
	docker compose down -v

logs:
	docker compose logs -f

reset:
	curl -s -X POST $(APP_URL)/admin/reset

inject:
	curl -s -X POST $(APP_URL)/admin/inject \
	  -H 'Content-Type: application/json' \
	  -d '{"fault":"$(FAULT)","once":true}'

faults:
	curl -s $(APP_URL)/admin/status
