#!/bin/bash
# install.sh — populates the build context for the about plugin.
#
# freeholdy invokes this with cwd = the project build context and:
#   PLUGIN_DIR   — this plugin's source directory (copy bundled assets from here)
#   PROJECT_DIR  — the build context the Dockerfile COPYs from (== cwd)
#
# The page is a single self-contained React file, so we just copy it in.
set -e

cp "$PLUGIN_DIR/index.html" "$PROJECT_DIR/index.html"
echo "Copied index.html into the build context"
