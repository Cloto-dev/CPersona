#!/usr/bin/env bash
# Call one MCP tool on the container published at 127.0.0.1:18402 and print the
# raw response.
#
#   mcp-call.sh <tool-name> <arguments-json>
#
# The transport is stateless Streamable HTTP, so a tools/call needs no prior
# handshake; the Accept header carries both types because the server answers
# with an event stream.
set -euo pipefail

tool="$1"
arguments="$2"

curl -s -X POST http://127.0.0.1:18402/mcp \
  -H "Authorization: Bearer ${CPERSONA_CI_TOKEN:-ci-token}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --max-time 20 \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"${tool}\",\"arguments\":${arguments}}}"
