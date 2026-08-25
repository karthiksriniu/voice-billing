#!/bin/bash
# Builds the sideload APK and puts it at a stable path, overwriting the previous one.
# One file, not one per version — the phone is flashed by hand and nobody wants to pick
# from a directory of near-identical names.
set -euo pipefail
cd "$(dirname "$0")"
export JAVA_HOME="${JAVA_HOME:-$HOME/Library/Java/temurin-17/Contents/Home}"

"$HOME/Library/Gradle/gradle-8.9/bin/gradle" --no-daemon -q :app:assembleRelease

mkdir -p dist
cp app/build/outputs/apk/release/app-arm64-v8a-release.apk dist/synthia.apk

if [ ! -f keystore.properties ]; then
  echo
  echo "WARNING: no keystore.properties — this APK is signed with the DEBUG key."
  echo "Fine for testing. Not fine for a shop: switching to the real key later forces an"
  echo "uninstall/reinstall on every phone, because Android will not accept the upgrade."
  echo "See README.md > Signing."
fi

echo
ls -lh dist/synthia.apk | awk '{print $5, $9}'
