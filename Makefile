.PHONY: backend frontend dev replay verify demo test

# Start the backend API server (port 8000)
backend:
	uvicorn backend.main:app --host 0.0.0.0 --port 8001 --reload

# Start the frontend static server (port 8080)
frontend:
	cd frontend && python -m http.server 8080

# Start both in parallel (Ctrl-C kills both)
dev:
	@trap 'kill %1 %2 2>/dev/null' INT; \
	uvicorn backend.main:app --host 0.0.0.0 --port 8001 --reload & \
	(cd frontend && python -m http.server 8080) & \
	wait

# Rebuild TLE cache (run before first demo after catalog change)
replay:
	python scripts/replay.py --hours 72

# Verify catalog altitudes against TLE cache
verify:
	python scripts/verify_catalog_altitudes.py

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

# Run tests
test:
	pytest tests/ -v

# Kill anything holding port 8000
kill-backend:
	-kill -9 $$(lsof -ti :8001)
