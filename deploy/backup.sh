#!/usr/bin/env bash
# Backup database harian, simpan 14 hari terakhir.
# Pasang di crontab: 15 3 * * * /path/ke/project/deploy/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
sqlite3 data/agent.db ".backup 'backups/agent-$(date +%F).db'"
find backups -name 'agent-*.db' -mtime +14 -delete
