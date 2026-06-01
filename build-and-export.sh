#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

PYTHON=python3
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python
fi

VERSION="$("$PYTHON" -c "import sys; sys.path.insert(0, '.'); import config; print(config.VERSION)" 2>/dev/null || true)"
VERSION="${VERSION:-0.0.0}"

RELEASE_DIR="build/edgeops-${VERSION}"
BUNDLE_TGZ="build/edgeops-v${VERSION}.tgz"
export EDGEOPS_VERSION="${VERSION}"

# 默认按本机 Linux 架构选择 PyArmor 平台；可覆盖：EDGEOPS_BUILD_PLATFORM=linux.x86_64 ./build-and-export.sh
if [ -n "${EDGEOPS_BUILD_PLATFORM:-}" ]; then
  PLATFORM="${EDGEOPS_BUILD_PLATFORM}"
else
  case "$(uname -s)-$(uname -m 2>/dev/null || echo unknown)" in
    Linux-x86_64|Linux-amd64) PLATFORM=linux.x86_64 ;;
    Linux-aarch64|Linux-arm64) PLATFORM=linux.aarch64 ;;
    *) PLATFORM=linux.x86_64 ;;
  esac
fi

echo ""
echo "毛竹 release bundle v${VERSION}"
echo "  edgeops-v${VERSION}.tgz 解压后:"
echo "    edgeops-${VERSION}/"
echo "      docker-compose.yml  run.bat  run.sh  start-compose.*"
echo "      edgeops-v${VERSION}.tar"
echo "      data/data  data/fs  data/logs"
echo ""

echo "[1/2] Build image and assemble release directory..."
"$PYTHON" scripts/build_release.py \
  --platform "${PLATFORM}" \
  --mode pyc \
  --build-image \
  --export-tar \
  --tag "edgeops:v${VERSION}"

echo ""
echo "[2/2] Creating ${BUNDLE_TGZ} ..."
if [ ! -d "${RELEASE_DIR}" ]; then
  echo "Release directory not found: ${RELEASE_DIR}"
  exit 1
fi

rm -f "${BUNDLE_TGZ}"
tar -acf "${BUNDLE_TGZ}" -C build "edgeops-${VERSION}"

echo ""
echo "Done: ${BUNDLE_TGZ}"
echo "Extract, enter edgeops-${VERSION}/, run ./start-compose.sh"
