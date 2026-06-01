#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
npm run pack
echo ""
echo "Pack done. Tarball is in this folder (edgeops-claw-ops-*.tgz)."
