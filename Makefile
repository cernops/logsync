PYTHON := python
PYLINT := pylint
PYRIGHT := pyright

PYTHONPATH := src
PYLINTHOME := $(CURDIR)/.cache/pylint

.PHONY: help test test-unittest test-pytest test-root test-podman-root test-integration lint typecheck check clean all

help:
	@printf '%s\n' \
		'Available targets:' \
		'  make test-unittest Run the unittest suite' \
		'  make test-pytest   Run the pytest suite' \
		'  make test-root     Run local Linux tests that require root' \
		'  make test-podman-root Run root tests in a rootless Podman user namespace' \
		'  make test-integration Run large-scale tests on configured real filesystems' \
		'  make test          Run both test suites' \
		'  make lint          Run pylint to lint' \
		'  make typecheck     Run pyright to typecheck' \
		'  make check         Run test, lint, and typecheck' \
		'  make all           Alias for make check' \
		'  make clean         Delete Python and tool caches'

all: check

test: test-unittest test-pytest

test-unittest:
	PYTHONHASHSEED=0 PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest -q tests/unittests

test-pytest:
	PYTHONHASHSEED=0 PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest -q tests/pytests

test-root:
	PYTHONHASHSEED=0 PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest -q tests/root

test-podman-root:
	+podman unshare $(MAKE) test-root

test-integration:
	PYTHONHASHSEED=0 PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pytest -q tests/integration

lint:
	PYTHONPATH=$(PYTHONPATH) PYLINTHOME=$(PYLINTHOME) $(PYLINT) src tests

typecheck:
	$(PYRIGHT)
	$(PYRIGHT) --project tests/pyrightconfig.json

check: test lint typecheck

clean:
	find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
	find . -depth -type d -name __pycache__ -exec rmdir {} +
	rm -rf .cache .pytest_cache .mypy_cache .pyright .ruff_cache
