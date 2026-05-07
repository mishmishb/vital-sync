# Contributing to vital-sync

Contributions are welcome. This project values clean, well-tested code over volume.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/vital-sync.git`
3. Install dev dependencies: `pip install -e ".[dev]"`
4. Create a branch: `git checkout -b your-feature`

## Development

- **Python 3.11+** required
- **Zero external runtime dependencies** — stdlib only
- Tests use `pytest`: run with `pytest`
- Linting with `ruff`: `ruff check . && ruff format --check .`
- Line length: 99 characters

## Pull Requests

- Keep PRs focused — one feature or fix per PR
- Include tests for new functionality
- Update the README if adding user-facing features
- Ensure all tests pass before submitting

## Code Style

- Type hints on all public functions
- Docstrings for public modules and functions
- Follow existing patterns — this project uses `from __future__ import annotations` throughout
- UK-English spelling in docs and comments

## Testing

```bash
pytest                          # full suite
pytest -x                       # stop on first failure
pytest --cov=vital_sync         # with coverage
```

Integration tests (real API calls) are skipped by default. To run them:

```bash
OURA_API_KEY=xxx HEVY_API_KEY=xxx VITAL_SYNC_INTEGRATION_TESTS=1 pytest
```

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
