# Numerator headless CLI — reverse-engineering workspace

Goal: a **zero-dependency headless CLI** (`numerator.py`, stdlib only) that can do
anything a human clicking around the Numerator web app can do — browse, build &
run reports, export data — so it can later be ported into the PipInsights portal
as a connector + a Pip agent skill (same path SPINS Satori took).

## Method (same as Satori)

1. **Capture** the live app with a headed browser you drive (`capture.mjs`).
2. **Reverse-engineer** the recorded HAR/JSONL into headless HTTP calls.
3. **Verify** each call live, building up `numerator.py`.
4. **Port** the proven HTTP layer into `pipinsights-portal` (`src/ingest/connectors/`).

## `capture.mjs` — network capture harness (reusable for any portal)

```bash
node capture.mjs https://<numerator-portal-url> --label numerator-login
```

Opens a real Chromium window. Log in and click around. It records, into `captures/`:

| File | What |
|------|------|
| `<label>.har` | Full HAR — every request/response with bodies |
| `<label>.net.jsonl` | One line per XHR/fetch/doc/websocket: request headers+body, response headers+body (text/json, capped at 2 MB) |
| `<label>.state.json` | Cookies + localStorage, for headless session replay |

Stop by closing the window or `Ctrl+C`. Start clean (no `--state`) for the first
run so the full login flow is captured.

⚠️ **Captures contain your password (login POST) and session tokens in cleartext.**
`captures/` is gitignored — keep it local, never commit or sync it. The eventual
CLI reads credentials from env/flags only, never from disk.

## `numerator.py` — the headless CLI

Zero-dependency (stdlib only). Auth = Okta (`/api/v1/authn` → sessionToken → app
OIDC code flow → Django `sessionid`+`csrftoken` cookies). Session cached in
`~/.numerator_session`.

### Setup

```bash
export NMR_USER='you@example.com'
export NMR_PASS='your-password'
./numerator.py login
```

Credentials can also be passed via `--user` and `--pass` flags on any command.
When neither flags nor `NMR_USER`/`NMR_PASS` are set (i.e. inside the Pip agent
container), `login` resolves credentials from the portal Worker via the shared
`common/pip_creds.py` helper — see the credential model in the repo-root README.

### Command Reference

#### Authentication Commands

**`login`** - Sign in to Numerator and cache the session
```bash
./numerator.py login [--user EMAIL] [--pass PASSWORD]
```

**`whoami`** - Show the signed-in user information
```bash
./numerator.py whoami
```

#### Report Catalog Commands

**`docs`** - List all available report types (catalog)
```bash
./numerator.py docs [--json] [--long]
```
- `--json`: Output raw JSON instead of formatted list
- `--long`: Show descriptions for each report type

**`doc`** - Show detailed information about a specific report type
```bash
./numerator.py doc <document_id> [--job-id JOB_ID] [--json]
```
- `document_id`: Report type identifier (e.g., `advanced-shopper-profile`)
- `--job-id`: Reload prompts from a specific job run
- `--json`: Output raw JSON instead of formatted display

#### Search Commands

**`search`** - Look up valid values for report parameters
```bash
./numerator.py search <document_id> <prompt> [--attribute ATTR_ID] [--narrative] [--q QUERY] [--json]
```
- `document_id`: Report type identifier
- `prompt`: Prompt ID (parameter name)
- `--attribute`: Attribute ID for search (defaults to prompt name)
- `--narrative`: Treat document_id as a narrativeId
- `--q`: Search query string (default: empty string for all values)
- `--json`: Output raw JSON instead of formatted list

#### Job Management Commands

**`jobs`** - List saved/recent reports
```bash
./numerator.py jobs [--page PAGE] [--size SIZE] [--json]
```
- `--page`: Page number (default: 1)
- `--size`: Items per page (default: 20)
- `--json`: Output raw JSON instead of formatted list

**`job`** - Show detailed status and items for a specific report run
```bash
./numerator.py job <job_id> [--json]
```
- `job_id`: Report run identifier
- `--json`: Output raw JSON instead of formatted summary

**`job-delete`** - Delete one or more saved reports by ID
```bash
./numerator.py job-delete <job_id> [<job_id> ...]
```
- Refuses to delete jobs that are still pending/queued/running

**`run`** - Run a new report with specified parameters
```bash
./numerator.py run <document_id> --answers <file> [--name NAME] [--wait] [--interval SECONDS]
```
- `document_id`: Report type identifier
- `--answers`: JSON file with parameter selections (required)
- `--name`: Custom name for the report run
- `--wait`: Poll until the report finishes
- `--interval`: Poll interval in seconds when using `--wait` (default: 30)

#### Data Commands

**`data`** - Read report result tables and AI summaries
```bash
./numerator.py data <job_id> [item_id] [--summary] [--json]
```
- `job_id`: Report run identifier
- `item_id`: Specific item ID (optional, defaults to all items)
- `--summary`: Show compact summary instead of full JSON
- `--json`: Output raw JSON instead of formatted summary

**`export`** - Download report results as Excel file
```bash
./numerator.py export <job_id> [-o OUTPUT_FILE]
```
- `job_id`: Report run identifier
- `--out`: Output filename (default: `job_<job_id>.xlsx`)

#### Group Management Commands

**`groups`** - List saved groups by type
```bash
./numerator.py groups <type> [--json]
```
- `type`: Group type - one of `product`, `store`, `people`, `trip`
- `--json`: Output raw JSON instead of formatted list

**Custom Groups (Product/Store)**

**`group-get`** - Show one custom group's definition
```bash
./numerator.py group-get <id> [--json]
```
- `id`: Group identifier
- `--json`: Output raw JSON instead of formatted display

**`group-create`** - Create a new custom product or store group
```bash
./numerator.py group-create <name> --type <type> [--from FROM_ID] [--definition DEF] [--json]
```
- `name`: Group name
- `--type`: Group type - `product` or `store` (default: `product`)
- `--from`: Clone definition from existing group ID
- `--definition`: Definition as inline JSON or @file
- `--json`: Output created group details as JSON

**`group-update`** - Update an existing custom group
```bash
./numerator.py group-update <id> [--from FROM_ID] [--definition DEF] [--name NAME] [--json]
```
- `id`: Group identifier to update
- `--from`: Clone definition from existing group ID
- `--definition`: New definition as inline JSON or @file
- `--name`: New group name
- `--json`: Output updated group details as JSON

**`group-delete`** - Delete one or more custom groups
```bash
./numerator.py group-delete <id> [<id> ...]
```

**People/Trip Groups** (use answers payload, no update endpoint)

**`people-group-create`** - Create a new people group
```bash
./numerator.py people-group-create <name> [--from FROM_ID] [--answers ANSWERS] [--static-group GROUP] [--json]
```
- `name`: Group name
- `--from`: Clone answers from existing group ID
- `--answers`: Answers as inline JSON or @file
- `--static-group`: Static group setting (default: cloned from source or `total_commerce`)
- `--json`: Output created group details as JSON

**`trip-group-create`** - Create a new trip group
```bash
./numerator.py trip-group-create <name> [--from FROM_ID] [--answers ANSWERS] [--static-group GROUP] [--json]
```
- Same options as `people-group-create`

**`people-group-delete`** - Delete one or more people groups
```bash
./numerator.py people-group-delete <id> [<id> ...]
```

**`trip-group-delete`** - Delete one or more trip groups
```bash
./numerator.py trip-group-delete <id> [<id> ...]
```

#### Narrative Commands (Packaged Multi-Report Analyses)

**`narratives`** - List available narrative templates
```bash
./numerator.py narratives [--json] [--long]
```
- `--json`: Output raw JSON instead of formatted list
- `--long`: Show descriptions for each narrative

**`narrative`** - Show a narrative's parameters (prompts)
```bash
./numerator.py narrative <narrative_id> [--flow-id FLOW_ID] [--json]
```
- `narrative_id`: Narrative template identifier
- `--flow-id`: Reload answers from a specific narrative run
- `--json`: Output raw JSON instead of formatted display

**`narrative-run`** - Run a narrative analysis
```bash
./numerator.py narrative-run <narrative_id> --answers <file> [--name NAME] [--wait] [--interval SECONDS]
```
- `narrative_id`: Narrative template identifier
- `--answers`: JSON file with parameter selections (required)
- `--name`: Custom name for the narrative run
- `--wait`: Poll until the flow completes
- `--interval`: Poll interval in seconds when using `--wait` (default: 30)

**`flow`** - Show a narrative run's status and topics
```bash
./numerator.py flow <flow_id> [--json]
```
- `flow_id`: Narrative run identifier (from `narrative-run`)
- `--json`: Output raw JSON instead of formatted summary

**`flow-data`** - Read narrative run topic results and insights
```bash
./numerator.py flow-data <flow_id> [topic_id] [--summary] [--json]
```
- `flow_id`: Narrative run identifier
- `topic_id`: Specific topic ID (optional, defaults to all topics)
- `--summary`: Show compact summary instead of full JSON
- `--json`: Output raw JSON instead of formatted summary

**`flow-delete`** - Delete one or more narrative runs
```bash
./numerator.py flow-delete <flow_id> [<flow_id> ...]
```
- Refuses to delete flows that are still SETUP/RUNNING

#### Utility Commands

**`raw`** - Make arbitrary authenticated GET requests (for power users)
```bash
./numerator.py raw <path> [--limit BYTES]
```
- `path`: API path (with or without leading `/`)
- `--limit`: Response size limit in bytes (default: 4000)

**`seed-from-capture`** - Bootstrap session from browser capture (development)
```bash
./numerator.py seed-from-capture <state_file>
```
- `state_file`: Path to capture state JSON file
- For testing without going through login flow

### Common Workflows

```bash
# Initial setup and exploration
./numerator.py login
./numerator.py docs --long           # what report types exist
./numerator.py jobs                   # your saved / recent reports

# Running a report
./numerator.py doc advanced-shopper-profile      # see what parameters it needs
./numerator.py search advanced-shopper-profile location --q midwest  # find location values
./numerator.py run advanced-shopper-profile --answers answers.json --name "Midwest Analysis" --wait

# Getting results
./numerator.py data 12657345 --summary         # read a report's results
./numerator.py export 12657345 -o results.xlsx  # download as Excel

# Managing groups
./numerator.py groups product                   # list product groups
./numerator.py group-get 141569                # see a group's definition
./numerator.py group-create "My Group" --type product --from 141569   # clone and edit

# Narratives (multi-report analyses)
./numerator.py narratives --long               # see available packaged analyses
./numerator.py narrative brand-performance      # see what parameters it needs
./numerator.py narrative-run brand-performance --answers answers.json --wait
./numerator.py flow-data 789012 --summary      # read the insights
```

### Data quirks (read before consuming results downstream)

- **`_chg` columns mix two conventions** — don't treat them all as "% change":
  - `pct_*_chg` = **absolute percentage-point** change (e.g. `pct_pen_chg = -0.00267`
    is `(0.000752 − 0.000779) × 100`).
  - `spend_*_chg` = **relative fraction** change (e.g. `spend_per_unit_chg = 0.211`
    is `(3.948 − 3.260) / 3.260`).
  Treating `pct_*_chg` as a fraction misrenders it by a factor of 100.
- **`status` means different things at the two levels**: job-level `status` is
  1 = pending (just submitted), 2 = queued, 3 = running, 6 = complete;
  item-level `status` is 1 = pending, 2 = queued, 3 = complete. (`run --wait`
  waits through 1/2/3 and stops at 6.)
- **NO_SAMPLE** — an item can be "complete" yet have no data: it comes back
  tagged `NO_SAMPLE` with a human error in `layout`. `job` and `data --summary`
  now flag this explicitly so it isn't mistaken for a clean run.

### File Formats

**Answers files for `run` and `narrative-run`:**
```json
{
  "promptId": ["option1", "option2"],
  "anotherPrompt": ["singleOption"],
  "date_range": ["2024-01-01", "2024-12-31"]
}
```
Or wrapped format:
```json
{
  "answers": {
    "promptId": ["option1", "option2"],
    "anotherPrompt": ["singleOption"],
    "date_range": ["2024-01-01", "2024-12-31"]
  }
}
```

**Group definitions for `group-create`/`group-update`:**
```json
{
  "rule": "category IN (\"some_category\")",
  "label": "rule description"
}
```

### Watching a capture live

```bash
tail -f captures/<label>.net.jsonl | python3 -c "import sys,json;[print(json.loads(l)['method'],json.loads(l)['status'] if 'status' in json.loads(l) else '',json.loads(l)['url']) for l in sys.stdin]"
```