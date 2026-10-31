#!/usr/bin/env bash
# Offline smoke test; run inside the OpenCode image, including with --user 12345:0.
set -euo pipefail

test "$(id -u)" != 0
for tool in jq node npm corepack yarn koji gdb git rg rpm rpmspec rpmbuild \
    rpm2cpio cpio tar unzip xz go rustc cargo readelf objdump nm file spectool; do
    command -v "$tool" > /dev/null
done

# Catch the root-owned scratch directory that broke source extraction in triage.
test "$(stat -c %a /tmp/opencode)" = 1777
work=$(mktemp -d /tmp/opencode/triage-smoke.XXXXXX)
trap 'rm -rf "$work"' EXIT
mkdir "$work/source" "$work/extracted"
echo source > "$work/source/probe.txt"
tar -cf "$work/probe.tar" -C "$work/source" probe.txt
tar -xf "$work/probe.tar" -C "$work/extracted"
cmp "$work/source/probe.txt" "$work/extracted/probe.txt"

echo '{"package":"rpm-ostree"}' | jq -e '.package == "rpm-ostree"' > /dev/null
node -e 'if (JSON.parse("{\"ok\":true}").ok !== true) process.exit(1)'
npm --version
corepack --version
koji --help > /dev/null
gdb --version > /dev/null

# Corepack must not silently pin a version by editing the inspected package.
test "$COREPACK_ENABLE_AUTO_PIN" = 0
test "$COREPACK_DEFAULT_TO_LATEST" = 0
test "$COREPACK_ENABLE_DOWNLOAD_PROMPT" = 0
test -r "$NODE_EXTRA_CA_CERTS"
python3 -c 'import koji, rpm, specfile'

echo "OpenCode triage tools and scratch extraction OK (UID $(id -u))"
