#!/usr/bin/env bash
# JazAI for Linux: run ./start-linux.sh in a terminal (or: bash start-linux.sh).
cd "$(dirname "$0")" || exit 1
exec bash scripts/start_unix.sh
