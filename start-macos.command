#!/bin/bash
# JazAI for macOS: double-click this file in Finder.
# If macOS blocks it, right-click the file and choose "Open", or run: bash start-macos.command
cd "$(dirname "$0")" || exit 1
bash scripts/start_unix.sh
status=$?
echo
read -r -p "Press Enter to close this window " _
exit $status
