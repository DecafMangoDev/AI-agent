.PHONY: test lint check

test:
	python3 -m unittest discover -v

lint:
	python3 -m ruff check agent tests

check: lint test
