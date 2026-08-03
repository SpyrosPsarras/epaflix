---
applyTo: "**"
description: "General Instructions for all setups"
---

# CRITICAL General instructions

CRITICAL: Never save username, hostnames and passwords on any documentation, script, yaml or anywhere other than secrets.yml.
CRITICAL: If we need to reference it, use the `.github/instructions/secrets.yml` file.

## Reading a value out of secrets.yml

Almost every value in `secrets.yml` is **double-quoted** (64 of 73 top-level keys). A naive `grep`/`awk -F': '` read hands you the value **with its quotes still attached**, and a quoted secret fails in a way that looks like a wrong secret: an API answers `403`, a login just fails. Nothing says "you sent quotes".

Strip them at extraction, and never echo the value:
```bash
# sed form - no extra tooling needed (yq is not installed on this workstation)
TOKEN=$(sed -n 's/^<key_name>:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/p' .github/instructions/secrets.yml)

# or python3 + PyYAML, if the value has awkward characters
TOKEN=$(python3 -c 'import sys,yaml;print(yaml.safe_load(open(sys.argv[1]))["<key_name>"])' .github/instructions/secrets.yml)
```
Sanity-check with `${#TOKEN}` (a length is safe to print, the value is not). A 64-char hex secret reading as 66 means you captured the quotes.

This trap cost real time: #293 read the `ak-iac` Authentik token with the quotes on, got `403`, and concluded the mirror was stale. It was not - #545 proved the mirror byte-matches the blueprint and returns `200` once the quotes are stripped.

## Never fetch a whole Secret to check one key

A value-echo leak is not only a `grep` problem. **Fetching an entire multi-key
Secret echoes every value in it**, base64 or not - and base64 is not a control,
#602 already set the bar at "value present in a retained transcript in any
encoding". #712 was caused exactly this way: an `mcp__kubernetes__resources_get`
on `servarr/unpackerr-secret` and `servarr/newtarr-config-seed`, run only to
check whether either already held a `prowlarr_api_key`, printed their full
`data` blocks and forced a 3-key rotation.

Rules:

- **Never** `resources_get` / `kubectl get secret -o yaml|json` a Secret. The
  MCP `resources_get` has no field selection, so there is no safe way to use it
  on a Secret at all.
- Fetch the **single key** you need, straight into a variable or a file:
  ```bash
  # into a variable - never printed
  VAL=$(kubectl -n <ns> get secret <name> -o jsonpath='{.data.<key>}' | base64 -d)
  # or straight to a 0600 file for a tool to read
  kubectl -n <ns> get secret <name> -o jsonpath='{.data.<key>}' | base64 -d > /tmp/k && chmod 600 /tmp/k
  ```
- To answer only "**does this key exist here?**", read key **names** and never
  values: `kubectl -n <ns> get secret <name> -o jsonpath='{.data}' | ...` still
  carries values, so use
  `kubectl -n <ns> get secret <name> --template '{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'`,
  or for a SOPS file `python3 -c` + `yaml.safe_load` printing `.keys()` only.
- Same rule for comparisons: compare **hashes**, not values
  (`sha256sum`, or `hashlib.sha256(v).hexdigest()[:16]`), and print lengths
  rather than contents.

## Hash in the query, never in a parser (#740)

"I intended to hash it" is not a control. #740 leaked all three freshly-rotated
*arr keys **from the parser that was supposed to hash them**: prowlarr's
`"Settings"` column is pretty-printed JSON, `psql -tA` emitted one row across
~20 lines, every line failed `json.loads`, and the `except` branch printed each
raw line - including `"apiKey": "..."`. The hashing code never ran.

Rules:

- **Compute the hash where the value is produced, not downstream.** In SQL:
  `SELECT "Name", md5("Settings"::jsonb->>'apiKey') FROM "Applications";`. In
  k8s: `kubectl ... -o jsonpath='{.data.<key>}' | base64 -d | sha256sum`. If a
  hash reaches your shell instead of a value, no later bug can leak anything.
- **No error path in a secret pipeline may echo its input.** Any `except` /
  `else` / fallback in secret-handling code prints a **fixed string**
  (`"unparseable"`, `"updated: no"`) - never a variable derived from the data,
  and never the raw line. Assume every parser you write will hit its error
  branch on the one row that holds the secret.
- **Prefer behaviour over reading the stored value at all.** A rotation is
  proved by `200` with the new key plus `401` with the old one - or the app's own
  test endpoint (`POST /api/v1/applications/testall` → `isValid: true`). Read a
  stored value only where behaviour cannot be observed (prowlarr masks the field
  as `********`, lingarr stores it encrypted), and there hash it in-query. In
  #740 the behavioural check had already passed; the stored-value read added
  nothing and cost a second rotation.

## Command History Documentation

IMPORTANT: Document all significant commands and their outputs in the `.history/` directory for future LLM reference and troubleshooting.

### History Directory Structure

```
.history/
├── README.md                    # This explains the history logging system
├── YYYY-MM-DD-session-name.log  # Daily session logs
└── commands/                     # Optional: organized by component
    ├── proxmox.log
    ├── k3s.log
    └── truenas.log
```

### What to Document

1. **All infrastructure commands**: Proxmox VM operations, network configurations, storage setups
2. **K3s cluster commands**: Installations, deployments, kubectl operations
3. **Configuration changes**: Any modifications to system or cluster settings
4. **Troubleshooting sessions**: Commands used to diagnose and fix issues
5. **Command outputs**: Full terminal output, especially for error diagnosis

### History Log Format

Use this format for each log entry:

```markdown
## [YYYY-MM-DD HH:MM] - Brief Description

**Context**: What you're trying to accomplish

**Command**:
```bash
command here
```

**Output**:
```
output here
```

**Result**: Success/Failed/Partial - Brief explanation

**Notes**: Any observations, issues, or things to remember
---

### Best Practices

- Create a new log file for each major session or daily work
- Use descriptive session names: `2026-02-14-k3s-initial-setup.log`
- Redact sensitive information (IPs can stay, but tokens/passwords must use placeholders)
- Include context before commands so future readers understand the "why"
- Document failures and errors - they're valuable learning material
- Cross-reference related documentation sections when applicable

### Git Ignore

The `.history/` directory is git-ignored to prevent committing sensitive information. This means:
- You can safely include actual IPs, hostnames, and system details
- Still avoid including passwords or API tokens when possible
- The history is local to your machine and won't be shared via git

### LLM Context

When asking for help or working with LLMs:
- Reference specific history log files for context
- LLMs can read these files to understand what has been done
- Include the log file path in your questions: "Check `.history/2026-02-14-setup.log` for context"

## Triage Scratch Space (`artifacts/`)

`artifacts/` is a scratch working space for per-issue triage notes and before/after
state captures written while investigating a GitHub issue: `triage-issue-<N>.md`,
`issue-<N>-<topic>.{txt,json,md}`, and topic subdirs (e.g. `vpn-picker/`) for
multi-file feature investigations. It is git-ignored (#662) - nothing under it is
committed, not even selectively.

### Why not tracked

- The durable record is the GitHub issue/PR the notes feed, not the scratch file
  (see "Open a GitHub issue for every follow-up" above) - once the issue closes,
  the file has no further job.
- Raw API/baseline dumps captured here can carry exactly what this repo bans from
  committed git: a Sonarr baseline snapshot in this directory already contained a
  full show title and overview text. Ignoring the whole directory avoids having to
  re-audit every new file for secrets or media names before it could be committed.
- Same shape as `.history/` (local-only, safe to include real IPs/hostnames/output),
  but with zero tracked exceptions - if a note earns permanent status, promote its
  content into `docs/` or a GitHub issue/PR comment instead of tracking the raw file.

Delete freely once its issue/PR is closed - nothing here is required for a fresh clone.
