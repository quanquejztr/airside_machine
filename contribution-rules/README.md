# Contribution rules

Team workflow for **Airside Machine**. Read this before opening a PR.

| File | Purpose |
|------|---------|
| [COLLABORATION_RULES.md](COLLABORATION_RULES.md) | Git workflow, code rules, testing, game-design gotchas |
| [GITHUB_ENFORCEMENT.md](GITHUB_ENFORCEMENT.md) | How to enforce these rules on GitHub (branch protection, CI, CODEOWNERS) |

**Quick start for contributors**

1. Branch from `main`: `<your-initials>/<short-topic>`
2. Make a small, focused change
3. Run `pytest` (and fix flake8 errors if CI fails)
4. Open a PR to `main` using [PR_Template.md](PR_Template.md)
5. Wait for CI green + review before merge
