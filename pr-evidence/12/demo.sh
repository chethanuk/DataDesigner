#!/usr/bin/env bash
# Usage: demo.sh <repo-worktree>. Runs the same config.toml scenarios against that checkout.
set -u
REPO=$1
export COLUMNS=110 NO_COLOR=1
cd /home/chethan/.cache/osflow-scratch/pr12-demo
dd() { uv run --no-sync --project "$REPO" data-designer "$@"; }
export DATA_DESIGNER_HOME=$(mktemp -d -p . home.XXXX)
echo "\$ git log -1 --oneline"; git -C "$REPO" log -1 --format='%h %s'
echo "\$ export DATA_DESIGNER_HOME=\$(mktemp -d)"
cat > "$DATA_DESIGNER_HOME/config.toml" <<'EOF'
version = 1

[[model.configs]]
alias = "local-text"
model = "meta/llama-3.1-8b-instruct"
provider = "nvidia"
EOF
echo "\$ cat \$DATA_DESIGNER_HOME/config.toml"; cat "$DATA_DESIGNER_HOME/config.toml"; echo
echo "# 1. Which models does the CLI see?"
echo "\$ data-designer config list | awk '/Model Configurations/,/└/' | grep -oE '^│ [a-z0-9-]+' | cut -c5-"
dd config list | awk '/Model Configurations/,/└/' | grep -oE '^│ [a-z0-9-]+' | cut -c5-
echo
echo "# 2. Typo in config.toml: 'provder' instead of 'provider'"
sed -i 's/^provider = /provder = /' "$DATA_DESIGNER_HOME/config.toml"
echo "\$ data-designer config list; echo exit=\$?   # blank lines dropped, first 12 lines"
dd config list > out.txt 2>&1; code=$?
head -12 out.txt | grep -v '^\s*$'; echo "exit=$code"
rm -rf "$DATA_DESIGNER_HOME" out.txt
