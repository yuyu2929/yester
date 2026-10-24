#!/bin/sh
# Yester end-to-end demo — creates a throwaway vault and exercises
# every direction. Safe to run anywhere; uses /tmp.
set -e

DEMO="${1:-/tmp/yester-demo}"
rm -rf "$DEMO"
mkdir -p "$DEMO/notes" "$DEMO/public"
cd "$DEMO"

echo "== init =="
yester init . --name "demo-vault"

echo "== seed =="
cat > notes/hello.md <<'EOF'
# Hello

first version — try editing this file and watch the Time Machine.
EOF
cat > public/index.md <<'EOF'
# Demo site

Published straight from the vault's public/ folder.
EOF
yester sync
yester snapshot "kickoff"

echo "== edit + sync =="
printf '\nsecond version — added after the snapshot.\n' >> notes/hello.md
yester sync
yester log

echo "== search across versions =="
yester search --reindex "version"

echo "== publish (build only, no remote configured) =="
yester publish --no-push

echo "== encryption (mirror build only, no remote configured) =="
yester encrypt-init --passphrase "demo-passphrase"
yester mirror --passphrase "demo-passphrase"

echo "== unlock the mirror elsewhere =="
yester unlock .yester/mirror /tmp/yester-demo-restored --passphrase "demo-passphrase"

echo
echo "done. open the Time Machine with:  cd $DEMO && yester ui"
