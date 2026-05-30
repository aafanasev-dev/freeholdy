#!/bin/bash
# install.sh — populates the build context for the hello-world plugin.
#
# freeholdy invokes this with cwd = the project build context and:
#   PLUGIN_DIR   — this plugin's source directory (copy bundled assets from here)
#   PROJECT_DIR  — the build context the Dockerfile COPYs from (== cwd)
#
# Here we just copy the bundled page in. The same hook could instead fetch from git, e.g.:
#   git clone --depth 1 https://github.com/example/site.git "$PROJECT_DIR/site"
set -e

cp "$PLUGIN_DIR/index.html" "$PROJECT_DIR/index.html"
echo "Copied index.html into the build context"
