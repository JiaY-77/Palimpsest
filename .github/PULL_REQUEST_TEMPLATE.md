## Description

<!-- What does this change do and why? Link to the issue it solves if any. -->

## Type of change

Please mark what applies (delete the rest):

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor
- [ ] Documentation

## Tests

<!-- Run the gates and paste the results. e.g.:

python -m pytest tests/ -q
> 320 passed in 12.34s
ruff check .
mypy
-->

- [ ] `python -m pytest tests/ -q` passes (all green)
- [ ] `ruff check .` and `mypy` pass
- [ ] User-facing docs updated (README / README_EN / CONTRIBUTING / .env.example) and `python scripts/readme_check.py` is green
- [ ] CI is green (lint / typecheck / test on Python 3.10 / 3.11 / 3.12)

## Breaking changes

<!-- Does this change API / CLI / MCP tool signatures, storage format, or behavior incompatibly? -->

- [ ] Yes — migration guide included
- [ ] No

## Related issue

<!-- If this PR closes an issue: -->
Closes #