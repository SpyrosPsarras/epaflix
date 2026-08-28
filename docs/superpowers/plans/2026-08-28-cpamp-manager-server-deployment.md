# CPAMP (CPA Manager Plus) full-mode deployment — 2026-08-28

Goal: give CPA's request/token/cost history a persistent home. Branch
`feat/cpamp-manager-server`, fixed point abfce33. This file is the durable record referenced by the
accepted-risk entry in `docs/accepted-risks.md`. It is here rather than in
`.history/` because that directory is git-ignored and opt-in per file, and
`.gitignore` directs write-ups that belong in git to `docs/`.

## Why this exists at all

A taskbar widget (SpyrosPsarras/dms-cliproxy-quota#15) asked for token consumption
per account, ideally per day for ~7 days, served from pi-bridge. Investigation
showed CPA cannot provide it and pi-bridge should not:

  internal/store/postgresstore.go            three tables only: config, auth, cooldown
  sdk/logging/request_logger.go:18           NewFileRequestLogger(enabled, logsDir, configDir)
                                             - files on an emptyDir that dies with the pod
  internal/api/handlers/management/usage.go:36
                                             GET /v0/management/usage-queue -> redisqueue.PopOldest()
                                             DESTRUCTIVE pop, 60s default retention
  sdk/pluginapi/types.go:1186-1188           UsagePlugin{HandleUsage} exists (also in v7.2.93)
  sdk/cliproxy/usage/manager.go:345          dispatch() copies the plugin slice, invokes EVERY plugin
                                             - fan-out, so a plugin consumer is NOT destructive

So: tokens are reachable in-process, but persistence is the hard part, and CPAMP
already implements the whole thing. Widget contract unchanged; pi-bridge untouched.

## Two usage transports, not one

The first version of this document, and of the manifests, said "exactly one
drainer, cluster-wide" and treated the destructive HTTP pop as the mechanism in
use. Round-7 review showed that is the FALLBACK, not the operative path. Both
mechanisms live in the same `redisqueue` package:

| | RESP `SUBSCRIBE` (used here) | `GET /v0/management/usage-queue` (fallback) |
| --- | --- | --- |
| Semantics | fan-out, own cloned channel per subscriber | destructive `PopOldest` |
| Consumers | unlimited, none interfere | at most one useful poller |
| Reached via | RESP on the same 8317 port, routed by `internal/api/protocol_multiplexer.go` to `internal/api/redis_queue_protocol.go:147,229` -> `redisqueue.SubscribeUsage()` | management API, `internal/api/handlers/management/usage.go:36` |
| Retention | live delivery | 60s default, clamped to 3600 |

CPAMP defaults to `USAGE_COLLECTOR_MODE=auto`
(`apps/manager-server/internal/config/config.go:147`), and `auto` tries
`runSubscribe` FIRST, falling back to HTTP when the dial, the AUTH, or the
SUBSCRIBE command itself fails - three branches at :180, :190 and :200 inside
`runSubscribe` (:159), dispatched from
`apps/manager-server/internal/collector/collector.go:139-158`. If HTTP also
fails, `auto` makes a third attempt via `runRESP` (:156) against a real Redis
endpoint, which this deployment does not have, so that stage is inert here. `resp.Dial`
plain-TCP dials the CPA URL host (`apps/manager-server/internal/resp/client.go:25-37`) and `cliproxy` is a ClusterIP TCP
passthrough on 8317, so the subscribe path connects in this cluster.

The operative hazard is therefore the interaction, not the pop: `Enqueue`
publishes to subscribers and returns WITHOUT queueing whenever a subscriber
exists (`internal/redisqueue/queue.go:72-76`). While CPAMP is subscribed the HTTP
queue stays empty, so any other poller reads nothing and looks idle; and if CPAMP
falls back to HTTP while something else is subscribed, CPAMP is the one starved.
Neither case logs an error.

So `replicas: 1` + `Recreate` is justified by the RWO SQLite volume - two
subscribers would each get a full copy, but two writers to one SQLite file
corrupt it - plus upstream's own one-Manager-Server-per-queue guidance, NOT by
queue theft.

pi-bridge uses neither transport (quota, models and /api-call only) - verified by
reading quota.go and upstream.go in abix5/pi-cliproxyapi-bridge.

## Source citations and the CPA version

Every CPA citation in this document and in the manifests was first verified against
v7.2.140, then re-verified against **v7.2.144**, which is what `kustomization.yaml`
pins after the rebase onto a Renovate bump. 18 of 19 cited ranges are byte-identical
between the two tags; the one that moved is `sdk/pluginapi/types.go`, where the
`UsagePlugin` interface shifted from :1163-1164 to :1186-1188, and that citation now
names the v7.2.144 lines. The claims that gate this change are unaffected:
`usage-statistics-enabled: false` is still line 126 of the image's own
`config.example.yaml` and still the default in `internal/config/config_load.go:70`.

## Pre-flight verification (before any manifest was written)

Image seakee/cpa-manager-plus:v1.9.2, resolved 2026-08-28:
  index digest  sha256:c0751252cc3a04eb7da97bc1e2b8b8abd9f120c26c9f9ab96aaf94d5251e64c2
  amd64 child   sha256:6151374a5d065fdabb4f479f05612442cc16ff56099732732ba23970bd78c70b
  `latest` was a DIFFERENT manifest (sha256:dbaad3f9...), so the tag is pinned.
  126 published tags; v1.9.2 newest.

1. Under the pod's posture - `docker run --read-only --user 1000:1000 -v ...:/data`:
   starts clean, writes ONLY /data (data.key 0600, usage.sqlite + -shm + -wal),
   /health -> {"ok":true,"service":"cpa-manager-plus"}. The image declares no USER,
   so runAsUser is what makes it non-root.
2. No writable /tmp needed (re-run without the tmpfs: identical result).
3. `mode":"embedded"` from /usage-service/info is NOT a lesser mode. It is a
   hardcoded constant in the Manager Server's own setup service
   (apps/manager-server/internal/service/setup/service.go:88) naming the panel
   asset as embedded in the binary (apps/manager-server/internal/service/panel/service.go:19
   `Embedded fs.FS`). The lightweight "CPAMP panel" is one HTML file served by CPA
   itself on 8317 via panel-github-repository - no Manager Server, no SQLite, no
   second port. Answering on 18317 at all proves Manager Server is the process.
4. reconcile-config.psql run TWICE against postgres:16 seeded from the v7.2.140
   image's own config.example.yaml (851 lines; usage-statistics-enabled: false at
   line 126, so it takes the regexp_replace branch): idempotent, one occurrence,
   value true. The seed was v7.2.140 because that was the pin at the time; the
   same key is still `false` at line 126 of v7.2.144's config.example.yaml, which
   is what the rebased kustomization pins, so the branch taken does not change. Production invocation
   (`psql --no-psqlrc --set=ON_ERROR_STOP=1 --file=...`) -> exit=0, 5 NOTICEs.
5. NEGATIVE test, same production invocation, value flipped to false -> exit=3 and
   `ERROR: usage-statistics-enabled is not true after reconcile`. The check can fail.
6. Admin key: supplied, never generated. Left unset, upstream prints it at
   apps/manager-server/cmd/cpa-manager-plus/main.go:128
   (`log.Printf("CPA Manager Plus admin key generated: %s", ...)`) and this
   namespace's stdout goes to Loki for 31 days. With CPA_MANAGER_ADMIN_KEY set the
   line becomes the value-free "CPA Manager Plus admin credential initialized".
   Verified end to end with the real key from the sops Secret: no generated-key
   line, and `grep -F` for the literal key finds nothing in the log.
7. GET /usage-service/config answers ANY caller until setup completes - deliberate
   upstream bootstrap behaviour, apps/manager-server/internal/http/controller/managerconfig/handler.go:75-76
   returns true while no management key is stored. It
   discloses an unconfigured config document only. POST /setup DOES require the
   admin key (apps/manager-server/internal/http/controller/setup/handler.go:22), so supplying the key
   also stops another in-cluster workload claiming the instance first.

## Verification traps hit during this work (both self-inflicted)

- `kustomize build` "renders the overlay" proved LESS than claimed: ksops is not
  installed on the workstation, so the generators emit NOTHING and kustomize STILL
  EXITS 0. Zero Secrets in the render, including the two pre-existing cliproxy
  ones. Secret correctness is verified structurally instead (decrypt -> assert
  kind/name/namespace/keys, values never printed) with a negative control proving
  the validator rejects a bad manifest. Same shape as the trap in
  docs/verification-traps.md.
- A retention audit summarised as a basic-grep `"a|b"` pattern, which cannot match.
  Re-run with `grep -rnEi` plus two positive controls (159 hits for a known-present
  term). Result held but had to be narrowed: no PRODUCTION path age-prunes raw
  usage history; the three `delete from usage_events` hits are all in _test.go;
  rollup/derived deletes are rebuild bookkeeping keyed on structure_revision,
  checkpoint and cursor names; api_key_aliases / codex_inspection_leases / settings
  have ordinary lifecycle deletes; and CPAMP DOES have a 24h import-session TTL
  (apps/manager-server/internal/config/config.go:25,
  DefaultUsageImportSessionTTL), which is session
  files, not history.

## Storage: the claim bounds nothing

local-path is a bind mount with NO quota, documented in this repo at
2-k3s/10.observability/prometheus-values.yaml:9-16 - where a 25Gi advisory claim let
Prometheus reach 30G and push k3s-worker-62 to 85%, breaking kubelet image GC (#463).
So the 2Gi request on cpa-manager-plus-data is advisory: the real bound is the node
root disk (48G), and CPAMP has no age-based retention to stay under it.

Observed volume for the arithmetic: the pi-bridge usage document on 2026-08-28
reported 2238 successful claude requests + 904 copilot on a 20-day-old deployment,
i.e. ~160 requests/day. At an order of ~1-2KiB per stored event (raw_json carries
response headers) that is ~0.2-0.3 MiB/day, ~100 MiB/year - years inside 2Gi, and
nowhere near node pressure. That estimate is the basis for accepting the risk;
the measurement that replaces it is in the accepted-risk reopen conditions.

Failure mode if it ever does fill: events are LOST, not deferred. The collector pops
before it writes (apps/manager-server/internal/collector/collector.go:360
`client.Pop` -> :372 `processItems` -> :434 `InsertEvents`), and in the default
`auto` mode CPA's Enqueue hands payloads straight to live subscribers and RETURNS
without queueing (internal/redisqueue/queue.go:72-76), so no copy is left behind to
age out. CPA keeps serving model requests either way.

Copy paths, neither automated here: CPAMP's JSONL export, and its
`manager-data-snapshot` command (registered at apps/manager-server/cmd/cpa-manager-plus/main.go:59),
whose snapshot includes the database, WAL/SHM/journal AND data.key
(apps/manager-server/internal/command/managerdatasnapshot/command.go:21-26, snapshotFiles) - so a snapshot is secret
material and must be treated like one.

## What CPAMP does NOT fix

Copilot quota. `grep -ril copilot` across the whole CPAMP repo returns NOTHING. Its
credential-quota views read CPA the same way pi-bridge does, so they hit the same
`$TOKEN$` resolution failure. The chain, with citations:

  CPA v7.2.144 — all in internal/api/handlers/management/api_tools.go
    :99       APICall handler
    :141-163  substitutes $TOKEN$ in the request headers
    :244-254  resolveTokenForAuth -> tokenValueForAuth (non-antigravity providers)
    :229-242  tokenValueForAuth   -> tokenValueFromMetadata, then Attributes["api_key"]
    :422-464  tokenValueFromMetadata checks, in order: accessToken, access_token,
              token (string or map carrying either), id_token, cookie
    :151-156  empty token -> 400 "auth token not found", before any outbound request

  cliproxyapi-copilot-plugin v0.3.3
    internal/provider/storage.go:16     stores GitHubAccessToken as json "github_access_token"
                                        inside StorageJSON
    internal/provider/storage.go:64-72  publishes metadata {type, github_login} and
                                        attributes {auth_kind: oauth}
None of the five metadata keys CPA checks, and no api_key attribute, is where the
Copilot plugin puts its credential - so substitution yields "" and the call is
rejected before GitHub is contacted. Observed live as `upstream returned status 400`
in the pi-bridge usage document, and in the pod log as
`400 | 0s | POST /v0/management/api-call` - 0s because nothing left the process.

That is a separate change, deliberately sequenced after this one.

## Review

Two-axis review (Standards + Spec), reviewer model different from the
implementer's, re-run against the same fixed point after every fix until both
axes passed.

**No round count or per-round tally is recorded here, deliberately.** An earlier
version of this section carried one, and it went stale three separate times -
once by arithmetic that did not sum, once by omitting three rounds, and once by
omitting the round that had just added the very sentence warning about staleness.
A running tally in a static document is a claim that must be rewritten every time
the process it describes advances, which is a defect generator, not a record.
This document is the record while the branch is uncommitted; once it lands, the
audit trail is the branch history and the PR, where each fix commit should name
the finding it answers.

What is worth recording is the SHAPE of what review caught, because it says where
this kind of change is fragile:

- **Security, once, and it mattered.** The CPAMP admin key was being generated by
  the container and printed to stdout, which this namespace ships to Loki for 31
  days. Now supplied from a sops Secret so the generating branch never runs.
- **A wrong central premise.** The first version documented CPA's destructive
  HTTP usage-queue pop as the transport in use. It is the fallback; CPAMP's
  default `auto` mode subscribes over RESP, which is fan-out. Every rule that
  followed from the wrong premise had to be rewritten.
- **A storage bound that bounded nothing.** `local-path` is a bind mount with no
  quota, so the PVC request is advisory and the real limit is the node root disk,
  which is the #463 shape.
- **Repeatedly: a fact corrected in one file and left stale in another.** Four
  separate occurrences, in the README, this document, `storage.yaml` and
  `kustomization.yaml`. Grepping for a phrase does not find a claim reworded.
- **Repeatedly: my own verifications passing while the fact was wrong.** A render
  that emitted zero Secrets and still exited 0 because ksops was absent; a
  retention audit whose basic-grep alternation could not match; an empty
  `sops --extract` on a multi-document file read as a mismatch; a citation
  checker that confirmed quoted lines while the architecture built from them
  named the wrong code path.

The owner authorised continuing past the four-round budget the `review-gate` skill
sets, to reach a genuine pass rather than a waived one.

This document is the durable record for that work: there was no PR while the gate
was failing, which is why the accepted-risk entry in `docs/accepted-risks.md`
points here rather than at one.
