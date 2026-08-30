# Collaboration rules

Rules for anyone contributing to Airside Machine via GitHub.

---

## 1. Git & branches

### Branch naming (required)

Every feature branch **must** start with your nickname, then a slash, then a short topic:

```
<nickname>/<short-topic>
```

| Good | Bad |
|------|-----|
| `tcun/gate-peak-fix` | `gate-peak-fix` (no nickname) |
| `jd/flight-display` | `tcun_gate_fix` (use `/`, not `_`) |
| `alex.m/popular-destinations` | `main`, `fix-bug` |

**Pattern (enforced on PRs by CI):**  
`^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]+$`

- **Nickname:** your GitHub username or agreed handle (`tcun`, `jd`, …)
- **Topic:** lowercase words with hyphens (`gate-peak-fix`, `ui-phase-6`)
- Pick **one nickname** and use it on every branch so reviews are easy to trace

### Do

- **Branch from latest `main`** for every task
- Create branches with: `git checkout -b <nickname>/<short-topic>`
- **Open a PR into `main`** — do not push directly to `main`
- Keep PRs **small and focused** (one feature or one bug fix)
- **Pull before you branch** when starting new work:
  ```bash
  git checkout main
  git pull origin main
  git checkout -b yourname/my-feature
  ```
- Sync your branch with `main` before merge if `main` moved:
  ```bash
  git fetch origin
  git merge origin/main   # or rebase if the team agrees
  ```

### Do not

- Commit **`db/airline_sim.db`** or `*.db-wal` / `*.db-shm` (player saves — gitignored)
- Commit **`.env`**, secrets, or **`.cursor/`**
- Commit random notes as `.md` outside allowed paths (see `.gitignore`)
- **Force-push to `main`**
- Merge your own PR without review (unless repo owner explicitly allows it)

### Leaving work on a branch

Branches that are **not merged** (e.g. experimental work) can stay on GitHub. That is normal. Only `main` is production; other branches do not affect players until merged.

### Clean local `main` after a mess

If your folder does not match GitHub `main` and you want to **discard local uncommitted changes** on `main`:

```bash
git fetch origin
git checkout main
git reset --hard origin/main
```

**Warning:** `reset --hard` deletes uncommitted work on `main`. Stash first if you might need it: `git stash push -u -m "WIP"`.

---

## 2. Pull requests

- Link the issue or describe the player-visible problem
- List what you ran in **Test plan** (at minimum `pytest`)
- Call out **DB migrations** if you touched `db/schema.sql` or `db/db.py`
- Request review from the repo owner for `engine/`, `db/`, `data/`, `web/`

---

## 3. Before you push (required)

```bash
source .venv/bin/activate
pip install -r requirements.txt   # if needed
pytest
```

CI also runs:

- `flake8` — syntax / undefined names must pass (`E9`, `F63`, `F7`, `F82`)
- `pytest` — full test suite

Optional but recommended:

```bash
python3 -m unittest discover -s tests -v
```

### UI changes

After editing `web/` or `server/`:

```bash
python3 main.py --ui
```

Hard-refresh the browser: `Cmd+Shift+R` (Mac) or `Ctrl+Shift+R` (Windows).

---

### Project layout (where to edit)

| Path | Role |
|------|------|
| `engine/` | Game logic (clock, demand, scheduling, settlement, AI, gates, slots) |
| `db/` | Schema, migrations, seed |
| `data/` | CSV catalogs, `competitors.json`, `financial_constants.csv` |
| `web/` + `server/` | Overlay UI and localhost API |
| `ui/` | Rich CLI panels |
| `tests/` | Regression tests |

See `docs/phase_implementation_detail.md` for what each build phase added.

---

## 4. What not to do

- Commit player saves or “fix” corruption by committing a `.db` file
- Add heavy dependencies to `requirements.txt` without discussion (project stays lean: `rich` + test tools)
- Change boot behavior in `server/game_http.py` without agreement (e.g. spawning flights on every server start caused duplicate segments)
- Skip tests because the change “looks small”
- Open a PR that mixes unrelated features (split into separate PRs)

---

## 5. Commit messages

Use clear, short messages focused on **why**:

```
Fix gate peak double-count when respawning the same tail.

Spawn idempotency: skip insert if (tail, week, route_id, dep) already exists.
```

Avoid: `fix`, `update`, `changes` with no context.

---

## 6. Ownership & questions

- **Repo owner** reviews merges to `main`
- When unsure: open a draft PR early or ask before large schema/API changes
- Known issues and doc/code drift: see `GAME_REVIEW.md` (reference only; do not commit drive-by fixes unless scoped)

---

## One-line summary

> Small branches, PR to `main`, `pytest` green, never commit saves, gates are not slots, minimal diffs.
