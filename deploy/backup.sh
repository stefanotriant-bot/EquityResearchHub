#!/bin/bash
# Daily backup of the SQLite database.
# Add to crontab as the erh user:
#   0 3 * * * /home/erh/EquityResearchHub/deploy/backup.sh >> /home/erh/backups/backup.log 2>&1
set -euo pipefail

DB_PATH="/home/erh/EquityResearchHub/erh.db"
BACKUP_DIR="/home/erh/backups"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="$BACKUP_DIR/erh-$TIMESTAMP.db"

mkdir -p "$BACKUP_DIR"

# sqlite3 .backup is safe to run while the app is writing
sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"
gzip "$BACKUP_FILE"

# Keep last 30 days only
find "$BACKUP_DIR" -name "erh-*.db.gz" -mtime +30 -delete

echo "[$(date)] Backed up $BACKUP_FILE.gz"
