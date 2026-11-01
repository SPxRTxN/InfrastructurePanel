#!/usr/bin/env bash
# Копирует homelab-agent на узлы по SSH — обёртка над handoff/homelab-agent/push-agent:
#   sudo bash /opt/homelab-dashboard/tools/push-agent.sh rpi@192.168.1.10 [user@host ...]
#   sudo bash /opt/homelab-dashboard/tools/push-agent.sh --install rpi@192.168.1.10 [user@host ...]
#     --install: после копирования на каждом узле интерактивно (ssh -t) выполняется sudo bash ~/homelab-agent/start-agent
exec bash "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../handoff/homelab-agent/push-agent" "$@"
