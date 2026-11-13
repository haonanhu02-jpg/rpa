#!/bin/sh
# Build-container settings only; never writes ~/.m2/settings.xml on the host.
set -eu
export MAVEN_MIRROR_URL="${MAVEN_MIRROR_URL:-https://maven.aliyun.com/repository/public}"
MAVEN_SETTINGS_DIR="${MAVEN_SETTINGS_DIR:-/opt/astron-rpa-maven}"
case "$MAVEN_MIRROR_URL" in
    https://*) ;;
    *) echo 'MAVEN_MIRROR_URL must use HTTPS' >&2; exit 2 ;;
esac
echo 'Maven: using the project mirror settings (dependencies and plugins).'
if mvn -B -s "$MAVEN_SETTINGS_DIR/settings.xml" "$@"; then
    exit 0
fi
echo 'Maven mirror attempt failed; retrying the same command with official Central.' >&2
exec mvn -B -U -s "$MAVEN_SETTINGS_DIR/settings-central.xml" "$@"
