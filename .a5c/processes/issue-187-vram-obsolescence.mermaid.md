# Issue #187 obsolescence — process flow

```mermaid
flowchart TD
    A[Start: issue #187 VRAM/GPU coordination] --> B[verify-obsolescence<br/>agent: audit k3s Odysseus manifests + live state]
    B --> C{Owner decision gate<br/>breakpoint}
    C -->|Request changes| B
    C -->|Approve and close| D[close-issue<br/>agent: comment + close + optional follow-up]
    C -->|Not approved x3| E[End: not closed]
    D --> F[End: #187 closed as obsolete]
```

## Steps
1. **verify-obsolescence** — Confirm against git manifests + live cluster that the k3s Odysseus
   Deployment has no GPU, no `NVIDIA_*` env, and serves LLM/embeddings only via remote Ollama
   (`192.168.10.200:30068`). Conclude the VRAM-contention premise is dead. Draft closing comment;
   propose a follow-up only if a genuine non-duplicate residual concern exists.
2. **Owner decision gate** (breakpoint) — Present recommendation + closing comment; retry/refine on
   rejection (up to 3 attempts).
3. **close-issue** — Post the approved comment, close #187 as "not planned", open + cross-link any
   approved follow-up. No git changes.
