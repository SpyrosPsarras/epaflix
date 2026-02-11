#!/bin/bash
# Fix Worker-61 Disk Pressure
# This script provides options to resolve disk pressure on worker-61

set -euo pipefail

echo "=== Worker-61 Disk Pressure Resolution ==="
echo ""

# Check current status
echo "Current disk usage:"
ssh ubuntu@192.168.10.61 "df -h /" | tail -1
echo ""

echo "Largest PVCs on worker-61:"
ssh ubuntu@192.168.10.61 'sudo bash -c "cd /var/lib/rancher/k3s/storage && for d in pvc-*; do echo -n \"\$d: \"; du -sh \"\$d\" 2>/dev/null; done"' | sort -h -k2 | tail -5
echo ""

echo "Choose a solution:"
echo ""
echo "Option 1: Cordon worker-61 and move workloads (RECOMMENDED)"
echo "  - Prevents new pods from scheduling on worker-61"
echo "  - Allows cluster to reschedule pods to other nodes"
echo "  - No data loss if PVCs are on shared storage"
echo ""
echo "Option 2: Expand worker-61 disk from 20GB to 40GB"
echo "  - Requires expanding TrueNAS iSCSI extent"
echo "  - Requires VM disk resize in Proxmox"
echo "  - Requires filesystem expansion"
echo ""
echo "Option 3: Delete old data (postgres/prometheus)"
echo "  - Reduce Prometheus retention (2.6GB -> less)"
echo "  - Clean up old postgres data (7.5GB)"
echo "  - May cause data loss"
echo ""

read -p "Enter option (1/2/3) or 'q' to quit: " OPTION

case $OPTION in
    1)
        echo ""
        echo "=== Option 1: Cordon Node and Move Workloads ==="
        echo "This will:"
        echo "  1. Mark worker-61 as unschedulable"
        echo "  2. Drain pods to other nodes"
        echo "  3. Keep PVC data intact"
        echo ""
        read -p "Proceed? (y/n): " CONFIRM
        if [ "$CONFIRM" = "y" ]; then
            echo "Cordoning worker-61..."
            kubectl cordon k3s-worker-61

            echo "Draining worker-61 (this may take several minutes)..."
            kubectl drain k3s-worker-61 --ignore-daemonsets --delete-emptydir-data

            echo "✓ Worker-61 drained successfully"
            echo ""
            echo "Status:"
            kubectl get nodes
            echo ""
            echo "To uncordon later: kubectl uncordon k3s-worker-61"
        fi
        ;;

    2)
        echo ""
        echo "=== Option 2: Expand Disk ==="
        echo "This requires manual steps in TrueNAS and Proxmox"
        echo ""
        echo "Steps:"
        echo "1. In TrueNAS (192.168.10.200):"
        echo "   - Login with credentials from .github/instructions/secrets.yml"
        echo "   - Go to: Shares > Block (iSCSI) > Extents"
        echo "   - Find: iscsi-worker-61 extent"
        echo "   - Edit: Change size from 20GB to 40GB"
        echo "   - Save"
        echo ""
        echo "2. In Proxmox (192.168.10.10):"
        echo "   ssh root@192.168.10.10 'iscsiadm -m node --rescan'"
        echo "   ssh root@192.168.10.10 'qm resize 1061 scsi0 +20G'"
        echo ""
        echo "3. On Worker-61:"
        echo "   ssh ubuntu@192.168.10.61 'sudo growpart /dev/sda 1'"
        echo "   ssh ubuntu@192.168.10.61 'sudo resize2fs /dev/sda1'"
        echo ""
        echo "4. Verify:"
        echo "   ssh ubuntu@192.168.10.61 'df -h /'"
        ;;

    3)
        echo ""
        echo "=== Option 3: Clean Up Data ==="
        echo "⚠ WARNING: This may cause data loss!"
        echo ""
        echo "Current largest consumers:"
        echo "  - Postgres: 7.5GB"
        echo "  - Prometheus: 2.6GB"
        echo ""
        echo "Actions available:"
        echo "  A. Reduce Prometheus retention (safe - old metrics lost)"
        echo "  B. Vacuum postgres database (safe - reclaims space)"
        echo "  C. Delete unused container images (safe)"
        echo ""
        read -p "Choose action (A/B/C) or 'q' to quit: " ACTION

        case $ACTION in
            A|a)
                echo "Reducing Prometheus retention requires editing values and redeploying"
                echo "See: 2-k3s/10.observability/prometheus-values.yaml"
                echo "Change: retention: 15d -> retention: 7d"
                ;;
            B|b)
                echo "Vacuuming postgres database..."
                echo "Not yet implemented - requires connecting to postgres pod"
                ;;
            C|c)
                echo "Cleaning up container images..."
                ssh ubuntu@192.168.10.61 "sudo k3s crictl rmi --prune" || true
                echo "✓ Completed"
                ssh ubuntu@192.168.10.61 "df -h /"
                ;;
        esac
        ;;

    q|Q)
        echo "Exiting..."
        exit 0
        ;;

    *)
        echo "Invalid option"
        exit 1
        ;;
esac

echo ""
echo "Done!"
