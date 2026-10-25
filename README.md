# reclaim

macOS disk triage and safe cleanup — one CLI for humans, one JSON contract for agents.

`reclaim` answers three questions without guessing:

1. **What is eating the disk** — actual bytes, sparse files excluded.
2. **What can be deleted** — every item carries a risk label and a reason.
3. **What is safe to run unattended** — `safe` items are regenerable, everything else needs a human.

## Why another cleaner

Existing tools either show raw sizes with no judgement (`du`, `ncdu`) or hide the
decision behind a GUI. Agents need the decision itself: a machine-readable plan
with risk levels, stable ids, and an execution mode that refuses destructive work
by default.

`reclaim` wraps the tools already on your Mac (`du`, `docker`, `brew`, `xcrun`,
`mdfind`, `git`) instead of re-implementing them.

## Install

```bash
git clone https://github.com/justin-mc-lai/reclaim.git
cd reclaim
pipx install .
# or just: python3 reclaim.py <command>
```

Requires macOS and Python 3.9+. No third-party dependencies.

## Usage

```bash
reclaim scan                       # largest directories under $HOME
reclaim plan                       # cleanup plan with risk labels
reclaim plan --deep                # + dev builds and git worktrees (slow)
reclaim plan --json                # agent-readable
reclaim clean --safe               # dry-run: what would happen
reclaim clean --safe --yes         # execute only risk=safe items
reclaim clean --ids 5f45318746 --yes
reclaim clean --ids <id> --yes --force   # dangerous items need --force
```

## Risk model

| level | meaning | examples |
|---|---|---|
| `safe` | regenerated on demand, no user data | app caches, Xcode DerivedData, dangling Docker images, package-manager caches, unavailable simulators |
| `review` | rebuildable or re-downloadable, but costs time or bandwidth | `node_modules`, `dist`, `target`, big media, disk images, clean git worktrees |
| `dangerous` | may lose uncommitted work | git worktrees with modified files |

`clean` is a dry-run unless `--yes` is passed. `dangerous` items are refused
unless `--force` is also passed.

## Agent contract

`plan --json` returns:

```json
{
  "free_gb": 18.4,
  "items": [
    {
      "id": "5f45318746",
      "kind": "docker-images",
      "path": "",
      "bytes": 0,
      "gb": 0.0,
      "risk": "safe",
      "why": "未被任何容器引用的镜像，需要时可重新 pull",
      "cmd": ["docker", "image", "prune", "-a", "-f"],
      "note": "142.5GB (79%) reclaimable of 179.5GB"
    }
  ]
}
```

Agents should read `risk`, act only on `safe`, and report `reclaim clean --json`
output (`reclaimed_gb` plus per-item results) to the user.

## Safety guarantees

- Deletion is confined to `$HOME`; `/`, `/System`, `/Applications`, `$HOME` and
  the well-known user folders are refused outright.
- The current working directory and its parents are never deleted.
- Symlinks are unlinked, not followed.
- Command-based cleanups call the vendor's own prune (`docker image prune`,
  `brew cleanup`, `simctl delete unavailable`), so the vendor decides what is
  orphaned.
- No network calls, no telemetry, no background daemon.

## Limitations

- `plan --deep` walks `~/Developer` once with `du`, so it is IO-bound on large
  trees.
- Risk labels are heuristics. `review` is a prompt to look, not a verdict.
- Docker sizes come from `docker system df`; the on-disk image file is sparse, so
  `ls` and `du` disagree by design.

## Test

```bash
python3 reclaim.py selftest
```

## License

MIT
