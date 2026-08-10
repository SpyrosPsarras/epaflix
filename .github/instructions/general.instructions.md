---
applyTo: "**"
description: "General Instructions for all setups"
---

# CRITICAL General instructions

CRITICAL: Never save username, hostnames and passwords on any documentation, script, yaml or anywhere other than the credential store.
CRITICAL: The credential store is `.github/instructions/secrets.enc.yaml` — SOPS+age encrypted and **committed**. If we need to reference a credential, reference a key name in that file.

There is no plaintext `secrets.yml` any more. It was the only unencrypted secret file left in the repo path, it drifted between machines (the June 2026 homePC merge), and being git-ignored it could not be reviewed or backed up with the repo. Anything that still says "see secrets.yml" in an older doc means this file.

## Reading a value out of the credential store

`sops -d --extract` returns the **YAML-parsed** value, so quotes and escaping are handled for you. Never decrypt the whole file to get one key, and never echo the value:

```bash
# one key
TOKEN=$(sops -d --extract '["<key_name>"]' .github/instructions/secrets.enc.yaml)

# nested key (e.g. the epaflix_bot block)
TOKEN=$(sops -d --extract '["epaflix_bot"]["proxmox_token"]' .github/instructions/secrets.enc.yaml)
```

Sanity-check with `${#TOKEN}` (a length is safe to print, the value is not).

Two things this fixed for good:

- **The quote trap.** With the old plaintext file, a naive `grep`/`awk -F': '` read handed back the value **with its double quotes attached**, and a quoted secret fails in a way that looks like a wrong secret - an API answers `403`, a login just fails, nothing says "you sent quotes". This cost real time: #293 read the `ak-iac` Authentik token with the quotes on, got `403`, and wrongly concluded the mirror was stale - #545 proved the mirror byte-matches the blueprint and returns `200` once the quotes are stripped. `--extract` cannot reproduce this.
- **The grep-echo leak.** The project rule "never extract a secret with a pattern that can echo the value" (#602 forced a token rotation) is now structurally enforced: there is no plaintext line for a `grep 'key:'` to match and print.

### Key names are readable without decrypting

The store encrypts values only - key **names** stay in cleartext. So `grep -c '^airvpn' .github/instructions/secrets.enc.yaml` answers "does this credential exist" with no key and no decryption, and PR diffs show *which* credential changed without showing the value.

### Requirements

The age private key must be at `~/.config/sops/age/keys.txt` (the default sops
lookup path). On this workstation that is a symlink to the real
`~/.config/sops/age/k3s-cluster.txt`. Without it every read fails with
`Failed to get the data key required to decrypt the SOPS file`. Recovery copies
and the post-reboot unlock are in `sops.instructions.md`.

## Never fetch a whole Secret to check one key

A value-echo leak is not only a `grep` problem. **Fetching an entire multi-key
Secret echoes every value in it**, base64 or not - and base64 is not a control,
#602 already set the bar at "value present in a retained transcript in any
encoding". #712 was caused exactly this way: an `mcp__kubernetes__resources_get`
on `servarr/unpackerr-secret` and `servarr/newtarr-config-seed`, run only to
check whether either already held a `prowlarr_api_key`, printed their full
`data` blocks and forced a 3-key rotation.

Rules:

- **Never** `resources_get` / `kubectl --context epaflix get secret -o yaml|json` a Secret. The
  MCP `resources_get` has no field selection, so there is no safe way to use it
  on a Secret at all.
- Fetch the **single key** you need, straight into a variable or a file:
  ```bash
  # into a variable - never printed
  VAL=$(kubectl --context epaflix -n <ns> get secret <name> -o jsonpath='{.data.<key>}' | base64 -d)
  # or straight to a 0600 file for a tool to read
  kubectl --context epaflix -n <ns> get secret <name> -o jsonpath='{.data.<key>}' | base64 -d > /tmp/k && chmod 600 /tmp/k
  ```
- To answer only "**does this key exist here?**", read key **names** and never
  values: `kubectl --context epaflix -n <ns> get secret <name> -o jsonpath='{.data}' | ...` still
  carries values, so use
  `kubectl --context epaflix -n <ns> get secret <name> --template '{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'`,
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

## Never page a decrypted file by line number (#943)

The two rules above both assume you were **after** a secret. The third shape is
the one that actually bit: you are after something else entirely, and a
credential is collateral. #943 leaked the live `ak-iac` Authentik token by
printing "lines 29-62 and 66-99" of the decrypted IaC blueprint while looking
for two unrelated blueprint entries. Nothing was misconfigured - the blueprint
*must* declare that key - and no rule was broken as written, because no rule
covered "I printed a range of a file that happens to contain a secret".

Any file that needs `sops -d` to read must be assumed to hold a credential
**somewhere you are not looking**.

Rules:

- **Address the entry, not the line range.** Parse the decrypted body and print
  the node you want. For the blueprint that is
  `yaml.safe_load(...)['stringData'][<key>]` then a search for the entry `id`,
  not `sed -n '29,62p'`. Line windows drift with every edit, so today's safe
  range is tomorrow's leak.
- **If you genuinely need raw context, redact on the way out.** Filter known
  secret-bearing keys before anything reaches the transcript:
  ```bash
  sops -d <file> | sed -E 's/^([[:space:]]*(key|token|password|secret|apiKey):).*/\1 <redacted>/'
  ```
  Add the key name to that list the moment a new one appears in the file.
- **Never widen a read "to get my bearings".** Locate the line numbers you need
  with a `grep` for the identifier first, then read only those. In #943 the two
  entries were 4 and 3 lines long; the windows printed were 34 lines each, and
  the whole leak lived in the 30 lines that were not needed.
- Same bar as #602: once a value is in a transcript it is burned, in any
  encoding. Rotate it, do not argue about whether anyone read it.

## An app's own API can hand you a credential you did not ask for (#520)

The three rules above all assume the credential sits somewhere you already know
is secret - a Secret, a decrypted SOPS file, a database column. This shape is
different: the value arrived in the body of an ordinary application read.
Verifying #520 on 2026-08-10 meant checking Seerr's Sonarr routing, so an agent
called `GET /api/v1/settings/sonarr`. That response embeds each server's
`apiKey`. No Secret was fetched, no SOPS file was opened, no rule as written was
broken - and the live `sonarr` API key landed in a retained transcript and had
to be rotated across seven consumers.

An API that returns configuration returns credentials with it. Assume every
`GET .../settings/...` is secret-bearing until you have read its schema.

Rules:

- **Ask for the fields you came for.** Piping the body through `jq` at display
  time is too late only if you print it first, so never print it first: project
  in the same command that fetches.
  ```bash
  curl -s -H "X-Api-Key: $K" .../api/v1/settings/sonarr \
    | jq -c '[.[] | {id, name, hostname, is4k, activeProfileName}]'
  ```
  Never `curl ... | jq '.'` on a settings endpoint, and never park the raw body
  in a file you later page - `artifacts/` is git-ignored, transcripts are not.
- **Redact by key name, not by inspection.** Drop the credential-shaped fields
  before anything is displayed, so an unfamiliar schema cannot surprise you:
  ```bash
  jq 'walk(if type == "object" then with_entries(select(.key | test("apikey|token|password|secret"; "i") | not)) else . end)'
  ```
- **Treat a settings endpoint like a Secret.** The "never fetch a whole Secret"
  rule (#712) exists because a multi-key object echoes every value it holds. A
  settings response is that same object with a friendlier URL.
- Same bar as #602: burned is burned. Rotate, and rotate by the app's own
  runbook - each *arr key has **seven** consumers and five keep their copy
  PVC-only or in the app's own database, so a git-only rotation leaves those
  five authenticating with a dead key
  (`2-k3s/08.servarr/README.md`, "Rotating the radarr / sonarr / sonarr2 API
  keys").

## Select the leaf, never the parent (#932)

Selecting a parent node hands you every leaf under it. You asked for one field,
the serializer gave you the whole object with the credential inside. #932 was a
probe for #296 running `curl -s .../api/system/settings | jq -r '.auth'` against
bazarr, expecting `.auth` to be a string. It is an object, so `jq -r` printed
the block and the live bazarr API key with it. Nothing errored and no rule as
written was broken: #712 covers Kubernetes Secrets, #740 covers a parser that
was meant to hash. Neither covers "I selected one level too high". The same
probe then hit the mirror case - `grep -n '^auth:' -A6 config.yaml` with a `sed`
redacting `apikey` and `password` by name, and the `-A6` window ran straight
past both into an unrelated `cf_clearance` cookie.

Rules:

- **Name the leaf in the selector.** `jq -r '.auth.type'`, never
  `jq -r '.auth'`. If you do not know the shape, print **key names** first
  (`jq -r '.auth | keys[]'`), then select the one you want. A `jq -r` on a
  parent is a full dump of that parent.
- **Never `-o json` / `-o yaml` an object that may embed a credential.** Ask for
  the leaf by path:
  ```bash
  kubectl --context epaflix -n <ns> get <kind> <name> -o jsonpath='{.spec.<leaf>}'
  ```
  The whole-object form is not the cheap option, it is the leak. Same for
  Proxmox, TrueNAS and every app API.
- **An application's own API is a credential source.** A `/settings` or
  `/config` response carries keys inline as ordinary fields - bazarr's
  `GET /api/system/settings` and the `*arr` `GET /api/v3/config/host` both do.
  See the `#520` rule in this file for the settings response that burned a live
  key. Treat every one of them as secret-bearing.
- **If you must move a whole object, `del()` the credential keys before anything
  reaches the transcript.** Strip at the source, not with a redacting `sed`
  downstream:
  ```bash
  curl -s -H "X-Api-Key: $KEY" http://<host>:5055/api/v1/settings/sonarr \
    | jq 'map(del(.apiKey))'
  ```
  A single object is `jq 'del(.apiKey, .password, .token)'`. Add the key the
  moment a new one appears in the response.
- **Anchor the match, never a context window.** No `-A` / `-B` / `-C` on a file
  that holds credentials - that is #943's line-range leak in `grep` clothing.
  `grep -n` the identifier, then read only the lines you named.
- **Prefer proving behaviour to reading state.** #740 makes this point for
  rotations; it holds for probes too. #932's rotation was proved with four HTTP
  codes and never needed a value. Where a value must be handled, #822 and #823
  track detecting it after the fact - detection is not a substitute for not
  printing it. Same bar as #602: once it is in a transcript it is burned, so
  rotate.

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
