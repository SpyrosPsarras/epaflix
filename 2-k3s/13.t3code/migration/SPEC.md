# T3 consolidation spec (2026-09-21)

Request: sunset t3env-0 and t3env-1 and move the LXC T3 Code server into k3s as one pod in its own namespace `t3code`, without losing the conversations of any of the three.

Decisions taken with the user during the run: new threads default to the host provider instances (`opencode`, `codex`, `claudeAgent`); env instances stay enabled only so old threads resume. The cutover runs from the laptop, since it stops the server the implementing agent lived on.

# Acceptance

- One T3 server pod in namespace `t3code`, on k3s.
- All conversations from the LXC host, t3env-0 and t3env-1 present in it, resumable, with attachments and projects.
- Old servers untouched until the user sunsets them; a rollback exists.
- New threads use the host provider instances; old env threads resume against their own provider history.
