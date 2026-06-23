# Scraper CLIs

A collection of headless CLI tools for data platforms and insights services.
Each CLI is **zero-dependency (Python stdlib only)** and provides programmatic
access to a platform through its reverse-engineered API.

The same CLI runs in two places, unchanged:
- **Locally** — your own shell / Claude, with credentials in env vars.
- **In the Pip agent container** — the agent just runs the CLI; credentials are
  resolved automatically from the portal Worker (see below).

## Structure

```
scraper-clis/
├── common/
│   ├── pip_creds.py     # shared credential resolver (used by every CLI)
│   └── __init__.py
├── numerator/           # Numerator Insights CLI  (reference implementation)
├── ...                  # additional platform CLIs
└── README.md            # this file
```

## CLI registry

Every CLI, its connector id (used in the Worker creds route), and the env var
names it expects. **Add a row here when you add a CLI.**

| CLI | Directory | Connector id | Credential env vars | Status |
|-----|-----------|--------------|---------------------|--------|
| Numerator Insights | `numerator/` | `numerator` | `NMR_USER`, `NMR_PASS` | ✅ working |

## Credential model — how login works everywhere

Credentials are resolved by one shared helper, `common/pip_creds.py`, so the
logic is written and improved **once** for all CLIs. A CLI's `login` command
resolves credentials in this order:

1. **Explicit flags** (`--user` / `--pass`, or the CLI's equivalent) — always win.
2. **Environment variables** (the CLI's documented vars, e.g. `NMR_USER` /
   `NMR_PASS`) — the local path. If set, used directly; no network call.
3. **Portal Worker** — the Pip container path. When the env vars are absent but
   the container has `PIP_WORKER_URL` + `PIP_AGENT_TOKEN`, the helper fetches
   this connector's credentials for the pinned client, just-in-time:

   ```
   GET {PIP_WORKER_URL}/api/agent-tools/creds/{connector}
       Authorization: Bearer {PIP_AGENT_TOKEN}
       CF-Access-Client-Id / CF-Access-Client-Secret   (if those env vars are set)
   -> 200 {"NMR_USER": "...", "NMR_PASS": "..."}
   ```

   The JSON keys are the CLI's own env var names, so the helper stays generic;
   the Worker route is the only place mapping a connector to its secret store.

This means **Pip never has to fetch credentials or set up the environment** — it
just invokes the CLI, and the first command that needs to authenticate resolves
the credentials transparently. No credentials sit in the container at rest, and
only the connector actually in use ever has its secrets fetched.

> The Worker creds endpoint is not implemented yet — this repo defines the
> contract the CLIs rely on. Until it exists, the env-var path (local) is the
> only one that resolves.

## The `login` contract every CLI must implement

Each CLI must expose a `login` command (and authenticate lazily on first use)
that resolves credentials through the shared helper:

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import pip_creds

CONNECTOR = "yourtool"                       # matches the registry + Worker route
CRED_VARS = ["YT_USER", "YT_PASS"]           # this CLI's documented env vars

def _credentials(args):
    if args.user and args.pw:                # explicit flags win
        return args.user, args.pw
    creds = pip_creds.resolve(CONNECTOR, CRED_VARS)   # env, then Worker
    return args.user or creds["YT_USER"], args.pw or creds["YT_PASS"]
```

`pip_creds.resolve(connector, var_names)` returns `{var_name: value}` and raises
`pip_creds.CredentialError` (a `RuntimeError`) with an actionable message if it
can't resolve from either env or the Worker. See `numerator/numerator.py`
(`_credentials`, `cmd_login`, `_client_logged_in`) for the working reference.

## Prescribed CLI format

Every CLI follows the same layout so that `install.sh` (below) can discover and
install **all** of them with zero per-CLI configuration — and so consumers like
the PipInsights portal container never need per-CLI install steps. A CLI is:

- **A directory named after its connector slug**: `<connector>/` (e.g.
  `numerator/`). This one slug is load-bearing and must be identical everywhere:
  the directory name, the installed command name, the connector id in the Worker
  creds route, AND the `connector_type` stored in the PipInsights `connections`
  table. That alignment is what lets a client's active connection be matched to
  its CLI with no extra mapping (it's also how Pip is told which CLIs it can use).
- **An executable entrypoint at `<connector>/<connector>.py`**: stdlib-only
  Python 3 with a `#!/usr/bin/env python3` shebang. This is what gets symlinked
  onto `PATH` as `<connector>`.
- **Self-documenting via `--help`**: top-level `<connector> --help` must print a
  one-line description of the tool and list every subcommand with a blurb, and
  each subcommand must support `-h`. This is how Pip (and any user) learns a CLI
  without anything documenting it externally — so it must be complete and current.
  With argparse you get this for free; keep the `help=`/`description=` text good.
- **`common/` resolved via `realpath`**: the entrypoint must add the repo root to
  `sys.path` using `os.path.realpath(__file__)` (not `abspath`) so the shared
  `common/` import still resolves when the entrypoint is symlinked onto `PATH`.
  See the top of `numerator/numerator.py`.
- **Optional `<connector>/requirements.txt`**: only if it genuinely can't be
  stdlib-only. `install.sh` `pip3 install`s it. Prefer zero dependencies.
- **Implements the `login` contract** above via `common/pip_creds.py`.

`common/` is shared library code, not a CLI — `install.sh` skips it.

## Container install (`install.sh`)

`./install.sh [BIN_DIR]` discovers every directory matching the format above,
installs each one's requirements (if any), and symlinks its entrypoint into
`BIN_DIR` (default `/usr/local/bin`). It is **generic**: adding a new CLI that
follows the format makes it install automatically — no edit to `install.sh` and
no edit to any consumer.

```bash
./install.sh                # installs all CLIs into /usr/local/bin
./install.sh ~/.local/bin   # or a custom bin dir
```

The PipInsights portal container bakes this repo in and runs `install.sh` at
image-build time, so every Pip agent comes with all CLIs preinstalled.

## Adding a CLI

1. Create `<connector>/<connector>.py` per the **Prescribed CLI format** above.
2. Implement the `login` contract using `common/pip_creds.py`.
3. Add a row to the **CLI registry** table (connector id + env var names).
4. Add a per-CLI `README.md` documenting its commands.
5. Ensure `.gitignore` covers any session cache / capture artifacts.

That's all — `install.sh` picks it up automatically, and the only portal-side
change is one row in its `CRED_ENV_MAP` (connector → env var names) plus the
connector's credential onboarding. No portal install changes, ever.

## Security

- Credentials come only from flags, env vars, or the Worker — never stored in
  code or config.
- Session data is cached in the user's home directory only.
- Capture files containing raw network traffic are gitignored.

## License

Internal use only — proprietary tools for PipInsights platform integration.
