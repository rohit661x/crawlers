#!/usr/bin/env bash
# ExecStopPost= helper: report a unit's result to a healthchecks.io-style URL (<url>/fail when
# the unit failed). No-op without a URL, and never fails the unit itself.
# Usage: hc-ping.sh <url>   (systemd sets $SERVICE_RESULT)
url="${1:-}"
[[ -n "$url" ]] || exit 0
[[ "${SERVICE_RESULT:-success}" == success ]] || url="${url%/}/fail"
curl -fsS -m 10 --retry 3 -o /dev/null "$url" || echo "healthcheck ping failed: ${url%%/[0-9a-f]*}/..." >&2
exit 0
