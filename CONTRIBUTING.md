# Contributing to memtomem-stm

Thank you for your interest in contributing to memtomem-stm!

## Development Setup

```bash
# Clone
git clone https://github.com/memtomem/memtomem-stm.git
cd memtomem-stm

# Install (requires Python 3.12+ and uv)
uv sync

# Run tests
uv run pytest -m "not ollama"          # skip Ollama-dependent tests
uv run pytest                          # full suite (requires running Ollama)

# Lint and format
uv run ruff check src
uv run ruff format src

# Type check
uv run mypy src
```

## Project Structure

- `src/memtomem_stm/` — Core: MCP server, proxy pipeline, compression, surfacing, caching, observability
  - `proxy/` — 4-stage pipeline (CLEAN → COMPRESS → SURFACE → INDEX), privacy scanning
  - `surfacing/` — Memory surfacing engine and relevance gating
  - `observability/` — Langfuse tracing and metrics
  - `cli/` — `mms` / `memtomem-stm-proxy` CLI
  - `utils/` — Circuit breaker and shared helpers
- `tests/` — pytest suite
- `docs/` — Architecture, operations, and integration guides (incl. `custom-integration.md`)

The LTM core lives in a separate repository: [memtomem/memtomem](https://github.com/memtomem/memtomem). Communication between STM and LTM happens entirely through the MCP protocol — there is no Python-level dependency.

## Pull Request Guidelines

1. Create a feature branch from `main`
2. Keep changes focused — one feature or fix per PR
3. Add tests for new functionality
4. Ensure `uv run ruff check src` and `uv run ruff format --check src` pass
5. Ensure `uv run pytest -m "not ollama"` passes
6. `uv run mypy src` is advisory but aim to not introduce new errors
7. Write a clear commit message describing the "why"
8. Sign the CLA on your first pull request (see below)

## Contributor License Agreement (CLA)

Before we can merge your first pull request, you need to sign the
[Contributor License Agreement](CLA.md). The CLA Assistant bot will
automatically comment on your PR with instructions — you sign by replying
with:

> I have read the CLA Document and I hereby sign the CLA

You only need to sign once per GitHub account. Your signature is stored in
`signatures/v1/cla.json` in this repository.

The CLA is adapted from the Apache Software Foundation Individual
Contributor License Agreement with one additional section covering future
licensing rights. This preserves DAPADA Inc.'s ability to adopt different
license terms for the Work in the future (for example, a dual-licensing
arrangement) without needing to re-collect consent from every contributor.
The CLA does not change the current license of the Work, which remains
Apache License 2.0.

For questions about the CLA, contact contact@dapada.co.kr.

## Reporting Issues

Open an issue at https://github.com/memtomem/memtomem-stm/issues with:
- Steps to reproduce
- Expected vs actual behavior
- Environment (OS, Python version, memtomem-stm version, upstream MCP server versions)
- Relevant config (`stm_proxy.json` or `mms status` output, with secrets redacted)
