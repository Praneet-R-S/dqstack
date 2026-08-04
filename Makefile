# DQ-Stack. Plain entry points, per the capstone stack substitution for Dagster.

PY ?= python

.PHONY: all data ingest diagnose features pipeline quick test ablate clean ui help

help:
	@echo "make all       generate data, run all seven stages, export the UI bundle"
	@echo "make pipeline  run the full pipeline and export (assumes data exists)"
	@echo "make quick     fewer origins and epochs, for a fast smoke run"
	@echo "make test      leakage, simulator and stage-contract tests"
	@echo "make ablate    run every single-stage ablation and write the comparison"
	@echo "make data      regenerate the synthetic M5-shaped panel"
	@echo "make ui        print the path to open"
	@echo "make clean     remove generated data and artifacts"

all: data pipeline

data:
	$(PY) -m dqstack.data.generate

ingest:
	$(PY) -m dqstack.ingest

diagnose:
	$(PY) -m dqstack.diagnose

features:
	$(PY) -m dqstack.features

pipeline:
	$(PY) -m dqstack.pipeline --export

quick:
	$(PY) -m dqstack.pipeline --quick --export

test:
	$(PY) -m pytest tests/ -q

ablate:
	$(PY) -m dqstack.ablate

ui:
	@echo "Open ui/index.html in a browser. No server required."

clean:
	rm -rf data/raw/* data/interim/* data/processed/* artifacts/* ui/data/*
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
