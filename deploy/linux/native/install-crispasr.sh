#!/usr/bin/env bash
# Install the pinned CUDA 12 CrispASR build for a Pascal (GTX 1070) host (ADR-021).
# Idempotent: re-running verifies instead of re-downloading.
#
#   deploy/linux/native/install-crispasr.sh [--prefix /srv/aerochorus] [--build-from-source] [--cpu]
#
# Result: $PREFIX/crispasr/<version>/crispasr (+ INSTALLED.json with hashes, runtime,
# driver, GPU, build flags) and $PREFIX/crispasr/current -> <version>.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../crispasr.lock
. "$HERE/../crispasr.lock"
PREFIX="/srv/aerochorus"
MODE="release"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --build-from-source) MODE="source"; shift ;;
    --cpu) MODE="cpu"; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[crispasr] $*"; }
DEST="$PREFIX/crispasr/$CRISPASR_VERSION"
[[ "$MODE" == "cpu" ]] && DEST="$DEST-cpu"
CUDA12="$PREFIX/cuda12"
mkdir -p "$DEST" "$PREFIX/crispasr/downloads"

# --- 1. never CUDA 13 on Pascal --------------------------------------------------------
[[ "$CRISPASR_ASSET" != *cuda13* ]] || die "crispasr.lock points at a CUDA 13 asset; Pascal needs CUDA 12"
CC=""
if command -v nvidia-smi >/dev/null; then
  CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | xargs)"
  log "GPU compute capability: $CC"
elif [[ "$MODE" != "cpu" ]]; then
  die "nvidia-smi not found: install the NVIDIA driver first (or use --cpu)"
fi

# --- 2. CUDA 12 runtime libraries -------------------------------------------------------
lib_path() {
  local paths=()
  for d in "$CUDA12"/nvidia/*/lib; do [[ -d "$d" ]] && paths+=("$d"); done
  (IFS=:; echo "${paths[*]}")
}
if [[ "$MODE" != "cpu" ]]; then
  if ldconfig -p | grep -q 'libcudart.so.12' && ldconfig -p | grep -q 'libcublas.so.12'; then
    log "system CUDA 12 runtime found"
  elif [[ -n "$(lib_path)" ]] && ls "$CUDA12"/nvidia/cuda_runtime/lib/libcudart.so.12* >/dev/null 2>&1; then
    log "CUDA 12 runtime already in $CUDA12"
  else
    log "installing CUDA 12 runtime wheels into $CUDA12 (no system toolkit needed)"
    if ! command -v uv >/dev/null; then
      curl -LsSf https://astral.sh/uv/install.sh | sh
      export PATH="$HOME/.local/bin:$PATH"
    fi
    # shellcheck disable=SC2086
    uv pip install --quiet --python-platform x86_64-manylinux_2_28 --target "$CUDA12" $CUDA12_WHEELS
  fi
fi
LD_EXTRA="$(lib_path)"

# --- 3. binary ----------------------------------------------------------------------------
BUILD_FLAGS="release asset $CRISPASR_ASSET"
case "$MODE" in
  release|cpu)
    if [[ "$MODE" == "cpu" ]]; then
      ASSET="$CRISPASR_CPU_ASSET"; URL="$CRISPASR_CPU_URL"; SHA="$CRISPASR_CPU_SHA256"
      SIZE_MB="~40"
      BUILD_FLAGS="release asset $ASSET (CPU only)"
    else
      ASSET="$CRISPASR_ASSET"; URL="$CRISPASR_URL"; SHA="$CRISPASR_SHA256"
      SIZE_MB="$(( CRISPASR_SIZE / 1000000 ))"
    fi
    TARBALL="$PREFIX/crispasr/downloads/$ASSET"
    if [[ ! -f "$TARBALL" ]] || ! echo "$SHA  $TARBALL" | sha256sum -c --status; then
      log "downloading $ASSET ($SIZE_MB MB) from GitHub release $CRISPASR_TAG"
      curl -fL --retry 3 -o "$TARBALL.part" "$URL"
      mv "$TARBALL.part" "$TARBALL"
    fi
    echo "$SHA  $TARBALL" | sha256sum -c --status || die "sha256 mismatch for $TARBALL"
    log "sha256 verified: $SHA"
    tar -xzf "$TARBALL" -C "$DEST" --strip-components=0
    BIN="$(find "$DEST" -type f -name crispasr -perm -u+x | head -1)"
    ;;
  source)
    command -v nvcc >/dev/null || die "nvcc not found: install a CUDA 12.x toolkit for the source build"
    NVCC_MAJOR="$(nvcc --version | grep -oE 'release [0-9]+' | grep -oE '[0-9]+')"
    [[ "$NVCC_MAJOR" == "12" ]] || die "nvcc is CUDA $NVCC_MAJOR; Pascal needs a CUDA 12.x toolkit"
    SRC="$PREFIX/crispasr/src-$CRISPASR_VERSION"
    if [[ ! -d "$SRC/.git" ]]; then
      git clone --depth 1 --branch "$CRISPASR_TAG" https://github.com/CrispStrobe/CrispASR "$SRC"
    fi
    [[ "$(git -C "$SRC" rev-parse HEAD)" == "$CRISPASR_COMMIT" ]] || die "source is not the pinned commit"
    # shellcheck disable=SC2086
    cmake -S "$SRC" -B "$SRC/build" $CRISPASR_BUILD_FLAGS
    cmake --build "$SRC/build" -j"$(nproc)"
    BIN="$(find "$SRC/build" -type f -name crispasr -perm -u+x | head -1)"
    cp "$BIN" "$DEST/crispasr"
    find "$SRC/build" -name '*.so*' -exec cp -P {} "$DEST/" \;
    BIN="$DEST/crispasr"
    BUILD_FLAGS="source $CRISPASR_COMMIT: $CRISPASR_BUILD_FLAGS (nvcc $(nvcc --version | grep -oE 'V[0-9.]+'))"
    ;;
esac
[[ -n "${BIN:-}" && -x "$BIN" ]] || die "crispasr binary not found after install"

# --- 4. verify ----------------------------------------------------------------------------
BIN_DIR="$(dirname "$BIN")"
export LD_LIBRARY_PATH="$BIN_DIR${LD_EXTRA:+:$LD_EXTRA}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
VERSION_OUT="$("$BIN" --version 2>&1 || true)"
DIAG_OUT="$("$BIN" --diagnostics 2>&1 || true)"
echo "$VERSION_OUT" | sed 's/^/    /'
if [[ "$MODE" != "cpu" ]]; then
  if echo "$DIAG_OUT" | grep -qiE 'no kernel image|unsupported gpu'; then
    die "this build has no kernels for compute capability $CC; re-run with --build-from-source"
  fi
  echo "$DIAG_OUT" | grep -qi 'cuda' || log "WARNING: diagnostics do not mention CUDA; check $DEST/diagnostics.txt"
fi
echo "$DIAG_OUT" >"$DEST/diagnostics.txt"
BIN_SHA="$(sha256sum "$BIN" | cut -d' ' -f1)"
cat >"$DEST/INSTALLED.json" <<JSON
{
  "crispasr_version": "$CRISPASR_VERSION",
  "tag": "$CRISPASR_TAG",
  "commit": "$CRISPASR_COMMIT",
  "mode": "$MODE",
  "build": "$BUILD_FLAGS",
  "binary": "$BIN",
  "binary_sha256": "$BIN_SHA",
  "ld_library_path": "$LD_LIBRARY_PATH",
  "gpu": "$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)",
  "compute_capability": "$CC",
  "vram_mb": "$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)",
  "driver": "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)",
  "cuda_runtime": "$(ls "$CUDA12"/nvidia/cuda_runtime/lib/libcudart.so.12* 2>/dev/null | head -1 || ldconfig -p | grep -m1 libcudart.so.12)",
  "installed_at": "$(date -Is)"
}
JSON
ln -sfn "$DEST" "$PREFIX/crispasr/current"
cat >"$PREFIX/crispasr/current.env" <<ENV
# Sourced by the AeroChorus services (see aerochorus.env)
CRISPASR_BINARY=$BIN
LD_LIBRARY_PATH=$LD_LIBRARY_PATH
ENV
log "installed $BIN (sha256 $BIN_SHA)"
log "record: $DEST/INSTALLED.json"
