.PHONY: backend frontend dev replay verify demo test lint fmt

# Start the backend API server (port 8001)
backend:
	uv run uvicorn backend.main:app --host 0.0.0.0 --port 8001 --reload

# Start the frontend static server (port 8080)
frontend:
	cd frontend && uv run python -m http.server 8080

# Start both in parallel (Ctrl-C kills both)
dev:
	@trap 'kill %1 %2 2>/dev/null' INT; \
	uv run uvicorn backend.main:app --host 0.0.0.0 --port 8001 --reload & \
	(cd frontend && uv run python -m http.server 8080) & \
	wait

# Rebuild TLE cache (run before first demo after catalog change)
replay:
	python scripts/replay.py --hours 72

# Verify catalog altitudes against TLE cache
verify:
	python scripts/verify_catalog_altitudes.py

# Validate catalog NORAD IDs and names against Space-Track satcat
verify-ids:
	python scripts/verify_catalog_ids.py

# Run demo sequence. Pass args with: make demo ARGS="--act 3"
demo:
	python scripts/demo.py --act all $(ARGS)

# Pull fresh TLEs from Space-Track into cache
ingest:
	curl -X POST http://localhost:8001/admin/trigger-ingest

# Run Kalman filter processing on cached TLEs (makes objects appear on globe)
process:
	curl -X POST http://localhost:8001/admin/trigger-process

# Reload catalog.json into the running backend without restart
reload-catalog:
	curl -X POST http://localhost:8001/admin/reload-catalog

# Run unit tests (mirrors ci-develop gate)
test:
	uv run pytest -m unit -v --tb=short

# Run all tests including integration (requires running backend)
test-all:
	uv run pytest tests/ -v

# Lint + format check + type check (mirrors ci-main build gate)
build:
	uv run ruff check backend/ tests/
	uv run ruff format --check backend/ tests/
	uv run python -m py_compile backend/main.py backend/processing.py backend/anomaly.py backend/kalman.py backend/propagator.py backend/conjunction.py backend/ingest.py
	uv run mypy backend/ --ignore-missing-imports

# Lint with ruff
lint:
	uv run ruff check backend/ tests/

# Format with ruff (in-place)
fmt:
	uv run ruff format backend/ tests/

# Kill anything holding port 8000
kill-backend:
	-kill -9 $$(lsof -ti :8001)
