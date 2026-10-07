#!/usr/bin/env bash
set -euo pipefail
pod=t3code-0
idle_needed=${IDLE_SECONDS:-1800}
here=$(dirname "$0")

want=$(kubectl get statefulset t3code -o jsonpath='{.status.updateRevision}')
have=$(kubectl get pod "$pod" -o jsonpath='{.metadata.labels.controller-revision-hash}')
if [[ -z $want || $want == "$have" ]]; then
  echo "idle-rollout: $pod is on $have"
  exit 0
fi

waiting=$(kubectl get pod "$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="t3")].state.waiting.reason}')
if [[ $waiting != CrashLoopBackOff ]]; then
  ready=$(kubectl get pod "$pod" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')
  if [[ $ready != True ]]; then
    echo "idle-rollout: $pod is not ready and not crash-looping; waiting"
    exit 0
  fi
  for container in t3 sshd; do
    idle=$(kubectl exec -i "$pod" -c "$container" -- python3 - <"$here/t3-idle.py")
    if ! [[ $idle =~ ^-?[0-9]+$ ]] || ((idle < idle_needed)); then
      echo "idle-rollout: $have -> $want waits; $container last active ${idle}s ago"
      exit 0
    fi
  done
fi

echo "idle-rollout: rolling $pod $have -> $want (t3 ${waiting:-ready and idle})"
kubectl delete pod "$pod" --wait=false
