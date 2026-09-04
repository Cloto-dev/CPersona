#!/usr/bin/env bash
# Call one MCP tool on a container published on the loopback interface and print
# the raw response.
#
#   mcp-call.sh <tool-name> <arguments-json>
#
# The port is CPERSONA_CI_PORT, defaulting to 18402. A gate that runs two
# servers at once -- one wired to an embedding service and one deliberately not
# -- needs to address them separately, and hard-coding the port made the second
# one impossible to ask.
#
# The transport is stateless Streamable HTTP, so a tools/call needs no prior
# handshake; the Accept header carries both types because the server answers
# with an event stream.
set -euo pipefail

tool="$1"
arguments="$2"

curl -s -X POST "http://127.0.0.1:${CPERSONA_CI_PORT:-18402}/mcp" \
  -H "Authorization: Bearer ${CPERSONA_CI_TOKEN:-ci-token}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --max-time 20 \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"${tool}\",\"arguments\":${arguments}}}"
