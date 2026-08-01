#!/bin/sh
# Build tinyproxy's allowlist from $ALLOW_HOST, then run it in the foreground.
set -e

ALLOW_HOST="${ALLOW_HOST:-ai.hackclub.com}"

# Escape dots; anchor so we allow the host and its subdomains and nothing else.
escaped=$(printf '%s' "$ALLOW_HOST" | sed 's/\./\\./g')
printf '(^|\\.)%s$\n' "$escaped" > /etc/tinyproxy/filter

echo "egress-proxy: allowlisting -> ${ALLOW_HOST} (deny all else)"
exec tinyproxy -d
