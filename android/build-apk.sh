#!/usr/bin/env bash
# Build the Android APK. Needs JDK 17 + the Android SDK (ANDROID_HOME) and Gradle (or ./gradlew if you ran `gradle wrapper`).
#   ./build-apk.sh                       -> app/build/outputs/apk/release/app-release.apk (debug-signed unless DS_KEYSTORE_* set)
#   ./build-apk.sh https://files.me.com  -> same, with a default server baked in
set -euo pipefail
cd "$(dirname "$0")"
G=./gradlew; [ -x "$G" ] || G=gradle
$G --no-daemon assembleRelease ${1:+-PdsServer="$1"}
mkdir -p ../dist
cp app/build/outputs/apk/release/app-release.apk "../dist/domshon-$(grep -oP '__version__\s*=\s*"\K[^"]+' ../src/domshon/__init__.py)-android.apk"
echo "APK in dist/"
