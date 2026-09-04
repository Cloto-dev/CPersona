#!/usr/bin/env bash
# Block until a container's HEALTHCHECK reports healthy, or fail saying what it
# reported instead.
#
# Written as a file rather than inline in the workflow because the loop has to
# distinguish "not yet" from "never", and the inline form of that in a `run:`
# block is where `set -e` and an `&&`-list quietly disagree about whether a
# false test ends the step. A container that never becomes healthy must fail the
# job loudly; one that fails by ending the loop early would pass it silently.
set -euo pipefail

name="$1"
deadline=$(( SECONDS + ${2:-90} ))
state=unknown

while [ "$SECONDS" -lt "$deadline" ]; do
  state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name")
  if [ "$state" = "healthy" ]; then
    echo "$name is healthy"
    exit 0
  fi
  if [ "$state" = "none" ]; then
    echo "::error::$name declares no healthcheck — this gate cannot tell running from serving"
    exit 1
  fi
  sleep 2
done

echo "::error::$name never became healthy (last state: $state)"
docker logs "$name" || true
exit 1
