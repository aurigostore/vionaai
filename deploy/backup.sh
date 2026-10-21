#!/usr/bin/env bash
# Backup database harian. Pasang di crontab: 15 3 * * * /opt/ai-agent/deploy/backup.sh
set -euo pipefail
cd /opt/ai-agent
mkdir -p backups
sqlite3 data/agent.db ".backup 'backups/agent-$(date +%F).db'"
find backups -name 'agent-*.db' -mtime +14 -delete
