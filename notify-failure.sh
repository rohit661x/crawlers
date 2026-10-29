#!/usr/bin/env bash
# OnFailure= handler: tell Telegram a unit failed, with the tail of its log. Goes to the ops
# target (the owner), never the findings group: the log tail can include proxy hosts and paths.
# Usage: notify-failure.sh <unit>   (DRY_RUN=1 prints instead of sending)
set -uo pipefail
unit="$1"
# environment first, then .env (systemd doesn't load it for this unit)
envval() { if [[ -n "${!1:-}" ]]; then printf '%s' "${!1}"; else
           sed -n "s/^$1=//p" "$(dirname "${BASH_SOURCE[0]}")/.env" 2>/dev/null | tail -n1 |
           sed -E "s/^([\"'])(.*)\1$/\2/"; fi; }  # dotenv-style quotes
target=$(envval NOTIFY_OPS_TARGET); target=${target:-$(envval NOTIFY_TARGET)}; target=${target:-telegram}
# strip markdown hermes would render; Telegram caps messages at 4096 chars
log=$(journalctl --user -u "$unit" -n 15 --no-pager -o cat 2>&1 | tr -d '*_`[]' | tail -c 2500)
msg="💥 $unit failed on $(hostname) at $(date '+%F %T %Z')
journalctl --user -u $unit

$log"
if [[ "${DRY_RUN:-}" == 1 ]]; then printf '[-> %s]\n%s\n' "$target" "$msg"; exit 0; fi
printf '%s' "$msg" | "${HERMES_BIN:-hermes}" send --to "$target"
