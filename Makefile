PYTHON := $(shell if command -v python3.11 >/dev/null 2>&1; then echo python3.11; else echo python3; fi)

.PHONY: test

test:
	$(PYTHON) -m unittest discover -s tests
