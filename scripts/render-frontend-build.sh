#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# npm ci can skip platform-specific optional deps when the lockfile was
# generated on another OS (see https://github.com/npm/cli/issues/4828).
rm -rf node_modules
npm install
npm run build
