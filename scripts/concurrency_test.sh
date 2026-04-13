#!/usr/bin/env bash
set -euo pipefail

# Simple concurrency test helper for SQL mode.
#
# Usage:
#   ./scripts/concurrency_test.sh 10 50 "SELECT COUNT(*) AS total_users FROM users"
#
# Args:
#   $1 = concurrency (default 10)
#   $2 = total requests (default 50)
#   $3 = SQL (default COUNT users)

CONCURRENCY="${1:-10}"
TOTAL="${2:-50}"
SQL="${3:-SELECT COUNT(*) AS total_users FROM users}"

seq "${TOTAL}" | xargs -P "${CONCURRENCY}" -I{} ./bin/database-agent agent --mode sql -m "${SQL}" >/dev/null
echo "done: concurrency=${CONCURRENCY} total=${TOTAL}"
