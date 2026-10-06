#!/usr/bin/env bash
# OnFailure= handler: tell Telegram a unit failed, with the tail of its log.
# Usage: notify-failure.sh <unit>   (DRY_RUN=1 prints instead of sending)
set -uo pipefail
unit="$1"
# strip markdown hermes would render; Telegram caps messages at 4096 chars
log=$(journalctl --user -u "$unit" -n 15 --no-pager -o cat 2>&1 | tr -d '*_`[]' | tail -c 2500)
msg="💥 $unit failed on $(hostname) at $(date '+%F %T %Z')
journalctl --user -u $unit

$log"
if [[ "${DRY_RUN:-}" == 1 ]]; then printf '%s\n' "$msg"; exit 0; fi
printf '%s' "$msg" | "${HERMES_BIN:-hermes}" send --to "${NOTIFY_TARGET:-telegram}"
