APP_URL ?= http://localhost:5000

.PHONY: help up down logs reset inject faults test test-schema boundary observe \
        replay discover escalation-demo catalog agent-demo stability

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

ARTIFACT ?= lookup_member_balance
PARAMS ?= {"member_id":"10001"}
replay:
	docker compose run --rm cua python -m cua.cli replay \
	  --artifact $(ARTIFACT) --params '$(PARAMS)' $(TENANT_ARGS) \
	  $(if $(EVIDENCE),--evidence $(EVIDENCE),) \
	  $(if $(TRACE),--trace,) $(if $(ASSIST),--assist,)

GOAL ?= Look up member 10001 and read their current savings balance.
CAPID ?= lookup_member_balance
discover:
	docker compose run --rm cua python -m cua.cli discover \
	  --goal "$(GOAL)" --id $(CAPID) $(if $(WRITE),--write-artifact,)

# Also `exec` rather than `run --service-ports`: the running service already
# holds host 8080/6080/6081, so a one-off container's console and VNC bridges
# are unreachable - the operator opens localhost:8080 and gets nothing. The
# handoff has to happen on the display the VNC bridges are attached to.
escalation-demo:
	docker compose exec cua \
	  python scripts/escalation_demo.py $(if $(MANUAL),--manual,)

# Runs inside the already-running `cua` service rather than a one-off
# container, for two reasons: `run --service-ports` collides with the port
# the running service has already published, and `agent-demo` execs into
# that same service, so the catalog has to be on *its* localhost.
catalog:
	docker compose exec cua python -m cua.cli catalog

agent-demo:
	docker compose exec -T cua python scripts/agent_calls_capability.py 	  --catalog http://localhost:8081 $(if $(TENANT),--tenant $(TENANT),)

N ?= 5
stability:
	docker compose run --rm cua python -m cua.cli stability 	  --artifact $(ARTIFACT) --params '$(PARAMS)' --n $(N) 	  $(if $(WRITE),--write,) $(if $(SUPERVISED),--supervised,)
