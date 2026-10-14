#!/bin/zsh
# overnight.sh — keep re-running the deletion plan until it is finished.
#
# iCloud throttles bulk IMAP commands and the connection drops now and then, so a
# single run stops after a while ("server refused repeated batches" or "connection
# lost"). This script waits 15 minutes after each stop and re-runs the SAME plan;
# messages already moved are skipped, so re-running is safe. It stops by itself when
# a run ends with "Failed: 0" (everything in the plan is gone) or after MAX_RUNS.
#
# Start it yourself from the project folder (lid open, power connected, Mail closed):
#     caffeinate -i -s zsh overnight.sh
# Stop it at any time:
#     pkill -f overnight.sh; pkill -f "main.py execute"
# Everything is also written to data/overnight_<date>.log

cd "$(dirname "$0")" || exit 1
PLAN=data/plan_20261010_182719.json
CONFIRM=402940            # must equal the plan's total_emails
PAUSE=900                 # seconds to wait after a stopped run (15 min)
MAX_RUNS=30
LOG=data/overnight_$(date +%Y%m%d_%H%M).log
RUN_OUT=$(mktemp -t overnight_run)

for i in $(seq 1 $MAX_RUNS); do
  echo "=== run $i started $(date '+%F %T') ===" | tee -a "$LOG"
  ./venv/bin/python main.py execute "$PLAN" --confirm $CONFIRM 2>&1 | tee "$RUN_OUT" | tee -a "$LOG" | grep -E "Trashed:|Failed:|⚠️|Error|error"
  if grep -Eq "Failed: +0$" "$RUN_OUT"; then
    echo "=== plan finished: nothing left to move $(date '+%F %T') ===" | tee -a "$LOG"
    break
  fi
  echo "=== run $i stopped $(date '+%F %T'); pausing ${PAUSE}s before the next attempt ===" | tee -a "$LOG"
  sleep $PAUSE
done
rm -f "$RUN_OUT"
