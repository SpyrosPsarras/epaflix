#!/usr/bin/env bash
set -euo pipefail
inputs=/run/t3-inputs
[[ ${T3_RUNTIME_VARIANT:-} == "${MCP_HUB_INSTANCE:-}" ]]
[[ $MCP_HUB_INSTANCE == homelab || $MCP_HUB_INSTANCE == work ]]
[[ $HOME == /home/t3 && ${T3CODE_HOME:-} == "$HOME/.t3" ]]
export PATH=/tools/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export MCP_HUB_SERVICES_FILE=$inputs/services.json
if [[ $MCP_HUB_INSTANCE == homelab ]]; then
  export KUBECONFIG=$inputs/kubeconfig MCP_HUB_SERVICES_FILE=$HOME/.config/t3-runtime/services.json
fi
python3 /scripts/isolated-config.py prepare "$inputs" "$HOME" "$MCP_HUB_INSTANCE"
read_config() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$inputs/runtime.json" "$1"; }
ANTHROPIC_BASE_URL=$(read_config modelUrl)
MCP_HUB_URL=$(read_config hubUrl)
export ANTHROPIC_BASE_URL MCP_HUB_URL
repo=$(read_config projectUrl)
project=$HOME/projects/$MCP_HUB_INSTANCE
export GIT_TERMINAL_PROMPT=0
git config --global credential.helper ''
git config --global user.name "Spyros Psarras"
git config --global user.email 13405649+SpyrosPsarras@users.noreply.github.com
# T3 reads a project's branch while `git clone` runs, when HEAD is still the init default; it must match GitHub's.
git config --global init.defaultBranch main
if [[ $(read_config gitAuth) == github-token ]]; then
  git config --global credential.helper /scripts/git-credential-github.sh
fi
tea_config=$HOME/.config/tea/config.yml
rm -f "$tea_config"
git config --global --unset-all credential.https://git.epaflix.com.helper || true
if [[ -n ${FORGEJO_TOKEN:-} ]]; then
  [[ $FORGEJO_TOKEN =~ ^[0-9a-f]{40}$ ]]
  mkdir -p "${tea_config%/*}"
  (umask 077; printf 'logins:\n  - name: git.epaflix.com\n    url: https://git.epaflix.com\n    token: %s\n    default: true\n    user: skills-%s-bot\n' \
    "$FORGEJO_TOKEN" "$MCP_HUB_INSTANCE" > "$tea_config")
  git config --global credential.https://git.epaflix.com.helper ''
  git config --global --add credential.https://git.epaflix.com.helper '!tea login helper'
fi
mkdir -p "$HOME/projects"
if [[ ! -d $project/.git ]]; then git clone -q "$repo" "$project"; fi
[[ $(git -C "$project" remote get-url origin) == "$repo" ]]
ai_skills=$HOME/work/ai-skills
# pi-setup.py loads the work skills from this clone; a missing URL or a failed move, clone or pull must not block startup.
if [[ $MCP_HUB_INSTANCE == work ]]; then
  if [[ ! -e $ai_skills/.git ]]; then
    skills_candidates=()
    for git_dir in "$HOME"/*/ai-skills/.git; do
      candidate=${git_dir%/.git}
      if [[ -d $git_dir && ! -L $git_dir && ! -L $candidate && ! -L ${candidate%/ai-skills} ]]; then
        skills_candidates+=("$candidate")
      fi
    done
    if [[ ${#skills_candidates[@]} == 1 ]]; then
      if [[ ! -L $HOME/work ]] && mkdir -p "$HOME/work" 2>/dev/null &&
         mv -T -- "${skills_candidates[0]}" "$ai_skills" 2>/dev/null; then
        echo 'work skills: moved ai-skills to ~/work/ai-skills' >&2
      else
        echo 'work skills: old ai-skills clone was not moved' >&2
      fi
    fi
  fi
  if [[ -e $ai_skills/.git ]]; then
    timeout 120 git -C "$ai_skills" pull -q --ff-only ||
      echo 'work skills: ai-skills update failed; keeping the current clone' >&2
  else
    skills_repo=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("skillsUrl") or "")' "$inputs/runtime.json")
    if [[ -z $skills_repo ]]; then
      echo 'work skills: runtime.json has no skillsUrl; ai-skills clone and update skipped' >&2
    else
      timeout 120 git clone -q "$skills_repo" "$ai_skills" ||
        echo 'work skills: ai-skills clone failed; skills unavailable' >&2
    fi
  fi
fi
tstack=$HOME/tstack
# pi-setup.py loads tstack skills from this clone; a failed clone or pull must not block startup.
if [[ -e $tstack/.git ]]; then
  timeout 120 git -C "$tstack" pull -q --ff-only || echo 'tstack: update failed; keeping the current clone' >&2
else
  timeout 120 git clone -q https://github.com/t3dotgg/tstack.git "$tstack" ||
    echo 'tstack: clone failed; skills unavailable' >&2
fi
SAFETY_ENTRY=$(/usr/bin/env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  /usr/bin/python3 -I -S /scripts/cc-safety-net-install.py | tail -n 1)
python3 /scripts/pi-setup.py write "$HOME" "$SAFETY_ENTRY"
[[ -s $HOME/.pi/agent/pi-cliproxyapi-provider/config.json ]]
python3 /scripts/isolated-config.py apply "$inputs" "$HOME" "$MCP_HUB_INSTANCE"
python3 /scripts/instructions-sync.py once
/scripts/pi.sh list | python3 /scripts/pi-setup.py check-packages "$HOME"
if [[ $MCP_HUB_INSTANCE == homelab ]]; then
  bash /scripts/ssh-config.sh
else
  python3 /scripts/work-ssh.py install
fi
bash /scripts/ssh-agent-ready.sh
ssh_instructions=/scripts/homelab-ssh.md
if [[ $MCP_HUB_INSTANCE == work ]]; then ssh_instructions=/scripts/work-ssh.md; fi
cat "$HOME/.codex/AGENTS.md" "$ssh_instructions" >"$HOME/.codex/AGENTS.override.md"
: >"$T3CODE_HOME/userdata/server-runtime.json"
(
  until curl -fs --max-time 5 -o /dev/null http://127.0.0.1:3773/health; do sleep 1; done
  printf '{"version":1,"pid":%s,"host":"127.0.0.1","port":3773,"origin":"http://127.0.0.1:3773","startedAt":"%s"}\n' \
    "$$" "$(date -u +%FT%T.000Z)" >"$T3CODE_HOME/userdata/server-runtime.json"
) &
rm -f "$T3CODE_HOME"/ssh-launch/*/pid "$T3CODE_HOME"/ssh-launch/*/port "$T3CODE_HOME"/ssh-launch/*/managed
export PATH=$PATH:$HOME/.local/bin
exec t3 start --no-browser --host 127.0.0.1 --port 3773 --base-dir "$T3CODE_HOME" \
  --auto-bootstrap-project-from-cwd "$project"
