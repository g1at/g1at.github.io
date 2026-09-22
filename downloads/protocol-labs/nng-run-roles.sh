#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /path/to/nng_roles" >&2
  exit 2
fi

binary="$1"
tmpdir="$(mktemp -d)"
trap 'rm -rf -- "$tmpdir"' EXIT

"${binary}" req >"${tmpdir}/req.txt" &
req_pid=$!
sleep 0.3
printf '\n' | "${binary}" rep >"${tmpdir}/rep.txt"
wait "${req_pid}"

echo '[REQ]'
cat "${tmpdir}/req.txt"
echo '[REP]'
cat "${tmpdir}/rep.txt"
