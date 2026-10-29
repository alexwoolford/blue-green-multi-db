.PHONY: up doctor test-unit test-local e2e down

up:
	mkdir -p work
	docker compose up -d neo4j-local
	docker compose build toolbox

doctor: up
	docker compose run --rm toolbox doctor

test-unit:
	cd orchestrator && python3 -m pytest -q

test-local: up
	docker compose run --rm --entrypoint pytest toolbox -m local -q

e2e: up
	docker compose run --rm toolbox e2e --yes

down:
	docker compose run --rm toolbox aura down --yes
	docker compose down
