.PHONY: dev test test-docker down

dev:
	docker compose up --build

down:
	docker compose down -v

test:
	PYTHONPATH=backend python -m pytest backend/tests -q

test-docker:
	docker compose up -d db redis
	docker compose run --rm backend-test
