# How to enforce contribution rules on GitHub

Steps for the **repo owner** to set up GitHub so collaborators follow [COLLABORATION_RULES.md](COLLABORATION_RULES.md).

---

## 1. Branch protection on `main`

**Settings → Branches → Add branch protection rule** (branch name: `main`)

Recommended settings:

| Setting | Value |
|---------|--------|
| Require a pull request before merging | ✅ On |
| Required approvals | 1 (owner reviews) |
| Dismiss stale approvals | ✅ Optional |
| Require status checks to pass | ✅ On |
| Status checks required | `build` (tests), **`branch-name`** (naming) — see §2b |
| Require branches to be up to date | ✅ Recommended |
| Do not allow bypassing | ✅ For admins too, if you want strict mode |
| Restrict who can push to matching branches | ✅ Only you (optional) |
| Allow force pushes | ❌ Off |
| Allow deletions | ❌ Off |

Result: nobody merges broken code to `main` without CI + PR.

---

## 2. CI (already in repo)

Workflow: `.github/workflows/python-app.yml`

Runs on every PR and push to `main`:

- `flake8` (syntax / serious errors)
- `pytest`

No extra setup needed if Actions are enabled (**Settings → Actions → Allow**).

### 2b. Branch name check (required for PRs)

Workflow: `.github/workflows/branch-name-check.yml`

Runs on every pull request. The head branch must match:

```
<nickname>/<topic>
```

Examples: `tcun/dev-contribution-rules`, `jd/gate-fix`

**Add to branch protection:** after the workflow has run once on a PR, include check name **`branch-name`** (job name) in **Required status checks** alongside `build`.

**Exempt:** `dependabot/*` only.

**Local git is not blocked** — you can still create a badly named branch locally. The PR will fail CI until you rename:

```bash
git branch -m old-name tcun/new-name
git push origin -u tcun/new-name
git push origin --delete old-name   # if you already pushed the old name
```

#### Optional: block bad names at push time (GitHub Rulesets)

**Settings → Rules → Rulesets → New ruleset → Target: branches**

- **Branch name pattern** (regex):  
  `^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]+$`
- Apply to: **branch creation** (and optionally updates)
- **Bypass:** admins only
- **Exclude** default branch `main` from this ruleset (protect `main` separately)

This stops `git push origin bad-branch-name` before a PR exists. Requires GitHub **Rulesets** (available on public repos and newer plans).

---

## 3. Pull request template (optional auto-fill)

GitHub only auto-loads templates from:

- `.github/pull_request_template.md`, or
- `.github/PULL_REQUEST_TEMPLATE.md`

This repo keeps the canonical template at **`contribution-rules/PR_Template.md`**.

**Option A — duplicate for GitHub UI** (recommended):

Create `.github/pull_request_template.md` with:

```markdown
See [contribution-rules/PR_Template.md](../contribution-rules/PR_Template.md).

<!-- Paste the template sections from that file below, or link and fill in -->
```

**Option B — contributors copy manually** from `contribution-rules/PR_Template.md` each PR.

---

## 4. CODEOWNERS (optional)

Create `.github/CODEOWNERS`:

```
# Default owner for sensitive paths
/engine/     @your-github-username
/db/         @your-github-username
/data/       @your-github-username
/server/     @your-github-username
/web/        @your-github-username
/tests/      @your-github-username
```

Requires **GitHub Teams** or individual usernames. PRs touching those paths auto-request your review.

---

## 5. Labels (optional)

Suggested PR labels:

| Label | Use |
|-------|-----|
| `bug` | Fixes broken behavior |
| `feature` | New player-facing capability |
| `engine` | `engine/` only |
| `ui` | Web or CLI |
| `db-migration` | Schema change — extra scrutiny |
| `do-not-merge` | WIP |

---

## 6. Branch hygiene

| Practice | Why |
|----------|-----|
| Delete branch after merge | GitHub offers “Delete branch” on merged PRs |
| Keep experimental work on named branches | e.g. `tcun/da-ds` — no need to merge until ready |
| Do not require every branch to merge | Unmerged branches on GitHub are normal |

---

## 7. What GitHub cannot enforce locally

These still need discipline or review:

- Minimal diff / no drive-by refactors
- Gates vs slots correctness in copy
- Not committing saves (gitignore helps; review still needed)
- Running manual `--ui` tests for front-end changes
- Branch naming **before** push (unless you enable Rulesets §2b) — CI only catches it at PR time

---

## 8. Quick setup checklist for repo owner

- [ ] Enable GitHub Actions
- [ ] Protect `main` (PR required + CI must pass: **`build`** and **`branch-name`**)
- [ ] (Optional) Ruleset regex for `nickname/topic` on branch creation
- [ ] Add yourself as required reviewer
- [ ] (Optional) Add `.github/CODEOWNERS`
- [ ] (Optional) Add `.github/pull_request_template.md` pointing to `contribution-rules/PR_Template.md`
- [ ] Share `contribution-rules/README.md` with your collaborator

---

## 9. Collaborator onboarding message (copy/paste)

```
Clone the repo, read contribution-rules/README.md, branch from main as
<your-nickname>/<topic> (required — e.g. jd/gate-fix), run pytest before pushing, open PR using
contribution-rules/PR_Template.md. Do not commit db/airline_sim.db.
Ask before large schema or engine changes.
```
