.PHONY: setup test run once dry-run health status test-video cleanup web autostart autostart-off autostart-status backup

PY := .venv/bin/python

setup:
	/usr/local/bin/python3.12 -m venv .venv
	CMAKE_ARGS="-DGGML_METAL=on" $(PY) -m pip install -q -r requirements.txt
	[ -f .env ] || cp .env.example .env

test:
	$(PY) -m pytest -q

run:
	$(PY) -m app.cli run

once:
	$(PY) -m app.cli run --once

dry-run:
	$(PY) -m app.cli run --dry-run

health:
	$(PY) -m app.cli health

status:
	$(PY) -m app.cli status

test-video:
	$(PY) -m app.cli make-test-video

cleanup:
	$(PY) -m app.cli cleanup

web:
	$(PY) -m app.cli web

autostart:
	scripts/autostart.sh on

autostart-off:
	scripts/autostart.sh off

autostart-status:
	scripts/autostart.sh status

backup:
	$(PY) -c "from app.config.settings import load_settings; from app.processing.maintenance import backup_database; print(backup_database(load_settings()))"

models:
	$(PY) -m app.cli download-models

share:
	scripts/share.sh

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down
