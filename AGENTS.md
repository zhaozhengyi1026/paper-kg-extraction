# Repository Guidelines

## Project Structure & Module Organization

The repository orchestrates biomedical paper collection and knowledge-graph extraction. `run_pipeline.sh` is the main workflow entry point. Root-level `tools/` contains database bridging, PMCID enrichment, PMC downloading, and evidence grading utilities. `paper-kg-extraction/src/` contains extraction, schema validation, provenance, NLI, and LLM clients; `scripts/` contains executable stages; `schema/` defines graph entities and relations; `tests/` contains regression and smoke tests. Runtime databases and generated papers belong under `med-papers/` and `paper-kg-extraction/out/` and should not be committed.

## Build, Test, and Development Commands

Install dependencies with `pip install -r paper-kg-extraction/requirements.txt`. Run the full test suite from `paper-kg-extraction/` with `python -m pytest -q`. Use `python scripts/02_run_extraction.py --from json --input examples/sample_paper.json --mock` for an API-free smoke run. Validate an output using `python scripts/03_validate.py PATH_TO_JSONL`. Run `git diff --check` before committing.

## Coding Style & Naming Conventions

Use Python 3.10+, four-space indentation, UTF-8, type hints where they clarify interfaces, and `snake_case` for functions and variables. Keep command-line options in kebab-case, such as `--shard-count`. Shell scripts must quote paths and variables. Preserve JSONL append-only semantics and deterministic PMID/source identifiers because resume and cross-paper aggregation depend on them.

## Testing Guidelines

Tests use pytest. Add regression coverage for checkpointing, fallback behavior, database schema handling, sharding, and provenance changes. Name tests `test_<expected_behavior>`. Mock external LLM and NCBI calls; routine tests must not require credentials or network access.

## Commit & Pull Request Guidelines

History uses short imperative subjects with an optional area prefix, for example `pipeline: add full-text batch ingestion commands`. Keep commits focused. Pull requests should describe behavior changes, commands used for verification, migration or restart requirements, and any effect on API cost or persisted outputs.

## Mandatory Work Log

After every repository, configuration, data, documentation, Git, or deployment-related change, append to the workspace's single August log at `D:\Apricot_AI\八月工作记录.md`. Record the date and time, files changed, reason, behavior before and after, validation results, commit hash, push/deployment state, and unfinished work. Do not maintain a second log in this repository. Never rewrite earlier entries to hide corrections; append a correction instead.
