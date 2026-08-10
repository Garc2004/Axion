# Tareas de desarrollo de axion-wizard.
#
# Todo pasa por `scripts/bootstrap.sh`, que es quien sabe montar el entorno
# (uv si está, venv+pip si no). Así hay una sola definición de "cómo se
# instala esto" y el Makefile no se desincroniza de ella.
#
# En Windows sin make, el equivalente es `.\scripts\bootstrap.ps1` con los
# mismos flags (-Check, -NoRun).

PYTHON := .venv/bin/python
BOOTSTRAP := ./scripts/bootstrap.sh

.DEFAULT_GOAL := help
.PHONY: help setup check test lint typecheck fmt run doctor build clean

help: ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Monta el entorno y las dependencias (idempotente)
	@$(BOOTSTRAP) --no-run

check: ## Monta el entorno y corre lint + tipos + tests
	@$(BOOTSTRAP) --check --no-run

test: setup ## Tests con cobertura
	@$(PYTHON) -m pytest -q --cov --cov-report=term-missing

lint: setup ## Lint (ruff)
	@$(PYTHON) -m ruff check .

typecheck: setup ## Comprobación de tipos (mypy)
	@$(PYTHON) -m mypy src

fmt: setup ## Corrige lo que ruff pueda corregir solo
	@$(PYTHON) -m ruff check --fix .
	@$(PYTHON) -m ruff format .

run: setup ## Arranca el wizard (make run ARGS="doctor")
	@$(PYTHON) -m axion_wizard $(ARGS)

doctor: setup ## Re-valida un stack ya desplegado
	@$(PYTHON) -m axion_wizard doctor

build: setup ## Empaqueta el binario con PyInstaller
	@./build/build.sh

clean: ## Borra entorno, cachés y salidas de build
	@rm -rf .venv build/work dist .pytest_cache .mypy_cache .ruff_cache .coverage
	@find . -type d -name __pycache__ -prune -exec rm -rf {} +
	@echo "Limpio."
