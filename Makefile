APP_URL ?= http://localhost:5000

.PHONY: help up down logs reset inject faults test test-schema boundary observe

help:
	@echo "up                 bring up the stack"
	@echo "down               tear it down"
	@echo "logs               follow logs"
	@echo "test               run the test suite (everything except llm)"
	@echo "test-schema        run the driver-free tests in an image with no browser"
	@echo "boundary           assert no driver type escapes src/cua/surface/"
	@echo "observe            print the live observation as a flat table"
	@echo "reset              clear injected faults and reseed the target app"
	@echo "inject FAULT=slow  arm a fault on the target app"
	@echo "faults             show armed faults"

test: boundary
	docker compose run --rm cua pytest -q -m "not llm"

# The schema and the seam must be testable with no browser installed at all.
test-schema:
	docker compose --profile test run --rm --build tests \
	  pytest -q -m "not llm and not integration"

boundary:
	@! grep -r "playwright" src/cua --include="*.py" | grep -v "surface/" \
	  || (echo "driver reference leaked outside surface/" && exit 1)
	@echo "boundary ok: no driver type outside src/cua/surface/"

PATH_ ?= /app
observe:
	docker compose run --rm cua python -m cua.cli observe --path $(PATH_) \
	  --tenant-prefix "$(TENANT)"

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
