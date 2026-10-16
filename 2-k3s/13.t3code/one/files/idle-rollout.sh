#!/usr/bin/env bash
set -euo pipefail
statefulset=${STATEFULSET_NAME:-t3code}
pod=$statefulset-0
idle_needed=${IDLE_SECONDS:-1800}
here=$(dirname "$0")

want=$(kubectl get statefulset "$statefulset" -o jsonpath='{.status.updateRevision}')
current=$(kubectl get pod "$pod" --ignore-not-found -o jsonpath='{.metadata.name}{" "}{.metadata.labels.controller-revision-hash}')
read -r name have <<<"$current"
if [[ -z $name ]]; then
  echo "idle-rollout: $pod does not exist; nothing to roll out"
  exit 0
fi
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
  uid=$(kubectl get pod "$pod" -o jsonpath='{.metadata.uid}')
  for container in t3 sshd; do
    idle=$(kubectl exec -i "$pod" -c "$container" -- python3 - <"$here/t3-idle.py") || {
      code=$?
      current=$(kubectl get pod "$pod" --ignore-not-found -o jsonpath='{.metadata.uid}{" "}{.metadata.deletionTimestamp}') || exit "$code"
      read -r current_uid deleting <<<"$current"
      if [[ $current_uid != "$uid" || -n $deleting ]]; then
        echo "idle-rollout: $pod went away during the idle check"
        exit 0
      fi
      exit "$code"
    }
    if ! [[ $idle =~ ^-?[0-9]+$ ]] || ((idle < idle_needed)); then
      echo "idle-rollout: $have -> $want waits; $container last active ${idle}s ago"
      exit 0
    fi
  done
fi

echo "idle-rollout: rolling $pod $have -> $want (t3 ${waiting:-ready and idle})"
kubectl delete pod "$pod" --wait=false
