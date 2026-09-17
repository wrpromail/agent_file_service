#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# build.sh — 构建 agent-file-service 双镜像（api + worker）
#
# 符合 version-control-standard / dev-security-standard 构建规范：
#   - 自动检测宿主架构，支持 --amd64 / --aarch64
#   - 标准标签: {name}:{arch}-{timestamp}-{git_commit}
#   - --native 极速本地调试
#   - 写入 last_image_tag.txt（可 source 的 KEY=value）
#   - 本地构建额外打 :local，对齐 docker-compose 默认镜像名
#   - 同步 docker-compose.yml 中 API_IMAGE/WORKER_IMAGE 默认 tag
#   - 支持 --export-tar：docker save 到 dist/（api+worker 各一包，架构与 tag 一致）
#   - 构建后 Trivy 扫描 api + worker（HIGH/CRITICAL，--scanners vuln）
#   - 漏洞库持久化缓存（默认 ~/.cache/trivy），禁止 docker 回退每次全量下载 DB
#   - 可用 --skip-scan / TRIVY_SKIP_DB_UPDATE=1（见 dev-security-standard §5.6）
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 镜像仓库前缀名（固定；勿用目录名）
API_NAME="agent-file-service-api"
WORKER_NAME="agent-file-service-worker"

REGISTRY="${REGISTRY:-}"
PUSH=false
NO_CACHE=false
NATIVE=false
SKIP_SCAN=false
EXPORT_TAR=false
ARCH=""
CUSTOM_TAG=""
# api | worker | all
TARGET="all"

usage() {
  cat <<EOF
Usage: ./build.sh [options]

Options:
  --amd64               Build for x86_64 architecture
  --aarch64             Build for ARM64 architecture
  --api                 Only build API image (slim, no LibreOffice)
  --worker              Only build Worker image (LibreOffice + heavy deps)
  --native              极速本地调试模式: 绕过 buildx，直接调用本机 docker daemon
  --export-tar          构建后 docker save 到 dist/（api/worker 各一个 tar，内网 load）
  --dist                同 --export-tar
  --tag <tag>           Override auto-generated tag (shared by api/worker)
  --registry <prefix>   Registry prefix, e.g. registry.example.com/team
  --push                Push image after build (uses buildx --push)
  --no-cache            Disable build cache
  --skip-scan           Skip Trivy image scan (local debug only)
  -h, --help            Show this help

Environment (scan):
  TRIVY_CACHE_DIR       Vulndb cache dir (default: ~/.cache/trivy); reused across builds
  TRIVY_SKIP_DB_UPDATE   Set to 1 to pass --skip-db-update (offline / fast rescan)

Artifacts:
  last_image_tag.txt    KEY=value file (source-able):
                          IMAGE_TAG / API_IMAGE / WORKER_IMAGE
  last_image_tar.txt    KEY=value (with --export-tar): API_TAR / WORKER_TAR
  dist/*.tar            离线镜像包（gitignore）
  Local convenience:    also tags built images as {name}:local
                        so \`docker compose up\` works without env export.

Examples:
  ./build.sh --native
  ./build.sh --amd64 --export-tar    # 内网 x86：两 tar 到 dist/
  ./build.sh --aarch64 --export-tar
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --amd64)      ARCH="amd64"; shift ;;
    --aarch64)    ARCH="aarch64"; shift ;;
    --api)        TARGET="api"; shift ;;
    --worker)     TARGET="worker"; shift ;;
    --native)     NATIVE=true; shift ;;
    --export-tar|--dist) EXPORT_TAR=true; shift ;;
    --tag)        CUSTOM_TAG="$2"; shift 2 ;;
    --registry)   REGISTRY="$2"; shift 2 ;;
    --push)       PUSH=true; shift ;;
    --no-cache)   NO_CACHE=true; shift ;;
    --skip-scan)  SKIP_SCAN=true; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

if [[ "$NATIVE" == "true" ]] && [[ "$PUSH" == "true" ]]; then
  echo "ERROR: --native 模式不支持 --push"
  exit 1
fi

if [[ "$EXPORT_TAR" == "true" ]] && [[ "$PUSH" == "true" ]]; then
  echo "ERROR: --export-tar 需要本机镜像，不能与 --push 同时使用"
  echo "  内网包: ./build.sh --amd64 --export-tar"
  echo "  推仓库: ./build.sh --amd64 --push"
  exit 1
fi

cd "$SCRIPT_DIR"

# ── 自动检测架构 ──
if [[ -z "$ARCH" ]]; then
  HOST_ARCH="$(uname -m)"
  case "$HOST_ARCH" in
    x86_64)        ARCH="amd64" ;;
    aarch64|arm64) ARCH="aarch64" ;;
    *) echo "ERROR: Unsupported host architecture: $HOST_ARCH"; exit 1 ;;
  esac
  echo "ℹ️  Auto-detected architecture: $ARCH"
fi

case "$ARCH" in
  amd64)   PLATFORM="linux/amd64" ;;
  aarch64) PLATFORM="linux/arm64" ;;
  *) echo "ERROR: Invalid architecture: $ARCH"; exit 1 ;;
esac

# ── Docker buildx 检查与环境准备 ──
if [[ "$NATIVE" == "false" ]]; then
  if ! docker buildx version >/dev/null 2>&1; then
    echo "ERROR: docker buildx is required. Install Docker 20.10+ with buildx support."
    exit 1
  fi

  BUILDER_NAME="global-toolkit-builder"
  if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
    docker buildx create --name "$BUILDER_NAME" --use >/dev/null 2>&1
  else
    docker buildx use "$BUILDER_NAME" >/dev/null 2>&1
  fi
fi

# ── 生成标准镜像标签 ──
GIT_COMMIT=$(git rev-parse --short=7 HEAD 2>/dev/null || echo "unknown")
TIMESTAMP=$(date +%Y%m%d%H%M%S)
IMAGE_TAG="${CUSTOM_TAG:-${ARCH}-${TIMESTAMP}-${GIT_COMMIT}}"

qualify() {
  local name="$1"
  local full="${name}:${IMAGE_TAG}"
  [[ -n "$REGISTRY" ]] && full="${REGISTRY}/${full}"
  echo "$full"
}

API_IMAGE="$(qualify "$API_NAME")"
WORKER_IMAGE="$(qualify "$WORKER_NAME")"

# 部分构建时保留上次未重建的槽位，便于 source 后仍能 compose 起全栈
PREV_API_IMAGE=""
PREV_WORKER_IMAGE=""
if [[ -f last_image_tag.txt ]]; then
  # shellcheck disable=SC1091
  # 只读取 IMAGE 行，避免污染当前 IMAGE_TAG
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      API_IMAGE=*)    PREV_API_IMAGE="${line#API_IMAGE=}" ;;
      WORKER_IMAGE=*) PREV_WORKER_IMAGE="${line#WORKER_IMAGE=}" ;;
    esac
  done < last_image_tag.txt
fi

BUILD_START=$(date +%s)
BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "======================================================="
echo "Build: agent-file-service (api + worker)"
echo "  tag: ${IMAGE_TAG}"
echo "  target: ${TARGET}"
echo "======================================================="

build_one() {
  local target="$1"
  local tag="$2"
  local common_args=(
    --file Dockerfile
    --target "$target"
    --tag "$tag"
    --build-arg "BUILD_TIME=${BUILD_TIMESTAMP}"
    --build-arg "COMMIT_SHA=${GIT_COMMIT}"
    --label "build.date=${BUILD_TIMESTAMP}"
    --label "build.platform=${PLATFORM}"
    --label "build.commit=${GIT_COMMIT}"
    --label "build.target=${target}"
  )
  [[ "$NO_CACHE" == "true" ]] && common_args+=(--no-cache)

  if [[ "$NATIVE" == "true" ]]; then
    echo "📦 Native Build [${target}]: ${tag}"
    docker build "${common_args[@]}" .
  else
    local bx=(docker buildx build --platform "$PLATFORM" "${common_args[@]}")
    [[ "$PUSH" == "true" ]] && bx+=(--push) || bx+=(--load)
    echo "📦 Buildx [${target}]: ${tag}"
    "${bx[@]}" .
  fi
}

BUILT_API=false
BUILT_WORKER=false
BUILT_IMAGES=()

if [[ "$TARGET" == "all" || "$TARGET" == "api" ]]; then
  build_one api "$API_IMAGE"
  BUILT_API=true
  BUILT_IMAGES+=("$API_IMAGE")
fi
if [[ "$TARGET" == "all" || "$TARGET" == "worker" ]]; then
  build_one worker "$WORKER_IMAGE"
  BUILT_WORKER=true
  BUILT_IMAGES+=("$WORKER_IMAGE")
fi

BUILD_END=$(date +%s)

# ── 本地便捷别名 :local（与 compose 默认 image 对齐）──
# --push 且未 load 时本地无镜像，跳过
LOCAL_AVAILABLE=false
if [[ "$NATIVE" == "true" ]] || [[ "$PUSH" == "false" ]]; then
  LOCAL_AVAILABLE=true
fi

tag_local() {
  local full_ref="$1"
  local short_name="$2"
  if [[ "$LOCAL_AVAILABLE" != "true" ]]; then
    return 0
  fi
  if docker image inspect "$full_ref" >/dev/null 2>&1; then
    docker tag "$full_ref" "${short_name}:local"
    echo "🏷️  tagged ${short_name}:local ← ${full_ref}"
  else
    echo "⚠️  skip :local for ${short_name}（本地无镜像 ${full_ref}）"
  fi
}

if [[ "$BUILT_API" == "true" ]]; then
  tag_local "$API_IMAGE" "$API_NAME"
fi
if [[ "$BUILT_WORKER" == "true" ]]; then
  tag_local "$WORKER_IMAGE" "$WORKER_NAME"
fi

# ── 校验 api 镜像内开发控制台静态资源（内网离线必须带 web/）──
verify_api_web_assets() {
  local img="$1"
  if [[ -z "$img" ]]; then
    return 0
  fi
  if [[ "$LOCAL_AVAILABLE" != "true" ]]; then
    echo "⚠️  skip web/ verify（本地无镜像，可能是 --push 未 --load）: ${img}"
    return 0
  fi
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "ERROR: 无法校验 web/：本机无镜像 ${img}"
    exit 1
  fi
  echo "🔍 Verify web/ static assets in ${img}"
  if ! docker run --rm --entrypoint sh "$img" -c '
    set -e
    test -f /app/web/dashboard.html
    test -f /app/web/index.html
    test -f /app/web/files.html
    test -f /app/web/file.html
    test -f /app/web/upload.html
    test -f /app/web/session.html
    test -f /app/web/metrics.html
    test -f /app/web/playground.html
    test -f /app/web/auth.html
    test -f /app/web/shared/config.js
    test -f /app/web/shared/styles/tokens.css
    test -f /app/web/shared/styles/app.css
    test -f /app/web/shared/styles/pages.css
    test -f /app/web/shared/js/chrome.js
    test -f /app/web/shared/js/api.js
    test -f /app/web/shared/js/uc.js
    test -f /app/web/shared/js/uc-strip.js
    test -f /app/web/shared/js/uc-gate.js
    test -d /app/web/shared/js/pages
    test -f /app/web/shared/js/pages/dashboard.js
    n=$(ls -1 /app/web/*.html 2>/dev/null | wc -l | tr -d " ")
    test "$n" -ge 8
    echo "web_html_count=$n"
    find /app/web -type f | wc -l | awk "{print \"web_file_count=\" \$1}"
  '; then
    echo "ERROR: api 镜像缺少开发控制台静态文件（/app/web）。内网部署将无法打开 dashboard。"
    exit 1
  fi
  echo "✅ web/ assets present in api image"
}

if [[ "$BUILT_API" == "true" ]]; then
  verify_api_web_assets "$API_IMAGE"
fi

# ── 保存标签（可 source 的 KEY=value；槽位始终写出）──
# 未在本次构建的槽位：尽量保留上次值，避免 partial rebuild 后 compose 缺镜像
SAVED_API_IMAGE=""
SAVED_WORKER_IMAGE=""
if [[ "$BUILT_API" == "true" ]]; then
  SAVED_API_IMAGE="$API_IMAGE"
else
  SAVED_API_IMAGE="$PREV_API_IMAGE"
fi
if [[ "$BUILT_WORKER" == "true" ]]; then
  SAVED_WORKER_IMAGE="$WORKER_IMAGE"
else
  SAVED_WORKER_IMAGE="$PREV_WORKER_IMAGE"
fi

cat > last_image_tag.txt <<EOF
IMAGE_TAG=${IMAGE_TAG}
API_IMAGE=${SAVED_API_IMAGE}
WORKER_IMAGE=${SAVED_WORKER_IMAGE}
EOF

# ── 同步 compose 中 API_IMAGE / WORKER_IMAGE 默认 tag ──
sync_compose_defaults() {
  local api_img="$1"
  local worker_img="$2"
  local compose_file
  for compose_file in docker-compose.yml; do
    [[ -f "$compose_file" ]] || continue
    if ! command -v python3 >/dev/null 2>&1; then
      echo "⚠️  python3 missing; update ${compose_file} image defaults manually"
      continue
    fi
    API_IMG="$api_img" WORKER_IMG="$worker_img" COMPOSE_FILE="$compose_file" python3 - <<'PY'
import os, re
from pathlib import Path
p = Path(os.environ["COMPOSE_FILE"])
text = p.read_text()
api = os.environ.get("API_IMG") or ""
worker = os.environ.get("WORKER_IMG") or ""
n_total = 0
if api:
    text2, n = re.subn(
        r"(?m)^(\s*image:\s*\$\{API_IMAGE:-)[^}]+(\})",
        rf"\g<1>{api}\2",
        text,
        count=1,
    )
    text, n_total = text2, n_total + n
if worker:
    text2, n = re.subn(
        r"(?m)^(\s*image:\s*\$\{WORKER_IMAGE:-)[^}]+(\})",
        rf"\g<1>{worker}\2",
        text,
        count=1,
    )
    text, n_total = text2, n_total + n
if n_total:
    p.write_text(text)
    print(f"📝 {p.name}: API/WORKER defaults updated ({n_total} field(s))")
else:
    print(f"⚠️  {p.name}: no API_IMAGE/WORKER_IMAGE default lines updated")
PY
  done
}

if [[ -n "${SAVED_API_IMAGE}" || -n "${SAVED_WORKER_IMAGE}" ]]; then
  sync_compose_defaults "${SAVED_API_IMAGE}" "${SAVED_WORKER_IMAGE}"
fi

echo
echo "✅ Build completed in $((BUILD_END - BUILD_START))s"
for img in "${BUILT_IMAGES[@]}"; do
  echo "📦 ${img}"
done
echo "📝 Artifact tags saved to: last_image_tag.txt (KEY=value, source-able)"
cat last_image_tag.txt | sed 's/^/   /'

# ── 导出 dist/*.tar（内网离线；api / worker 各一包）──
export_one_tar() {
  local img="$1"
  local out_var_name="$2"
  if [[ -z "$img" ]]; then
    return 0
  fi
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "ERROR: 本机不存在镜像 ${img}，无法 docker save"
    exit 1
  fi
  local safe="${img//\//-}"
  safe="${safe//:/-}"
  local path="${SCRIPT_DIR}/dist/${safe}.tar"
  echo "📦 save ${img} → ${path}"
  docker save -o "$path" "$img"
  echo "${img}" > "${SCRIPT_DIR}/dist/${safe}.image.txt"
  printf -v "$out_var_name" '%s' "$path"
  echo "   size: $(du -h "$path" | awk '{print $1}')"
}

if [[ "$EXPORT_TAR" == "true" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker 不可用，无法 --export-tar"
    exit 1
  fi
  mkdir -p "${SCRIPT_DIR}/dist"
  echo ""
  echo "── Export tar (platform=${PLATFORM}, arch=${ARCH}) ──"
  API_TAR=""
  WORKER_TAR=""
  # 导出本次构建的镜像；若 all 构建则 api+worker 都有
  if [[ "$BUILT_API" == "true" ]]; then
    export_one_tar "$API_IMAGE" API_TAR
  elif [[ -n "$SAVED_API_IMAGE" ]] && docker image inspect "$SAVED_API_IMAGE" >/dev/null 2>&1; then
    export_one_tar "$SAVED_API_IMAGE" API_TAR
  fi
  if [[ "$BUILT_WORKER" == "true" ]]; then
    export_one_tar "$WORKER_IMAGE" WORKER_TAR
  elif [[ -n "$SAVED_WORKER_IMAGE" ]] && docker image inspect "$SAVED_WORKER_IMAGE" >/dev/null 2>&1; then
    export_one_tar "$SAVED_WORKER_IMAGE" WORKER_TAR
  fi
  if [[ -z "$API_TAR" && -z "$WORKER_TAR" ]]; then
    echo "ERROR: --export-tar 未导出任何 tar（本地无可用镜像）"
    exit 1
  fi
  cat > last_image_tar.txt <<EOF
API_TAR=${API_TAR}
WORKER_TAR=${WORKER_TAR}
EOF
  echo "📝 Tar paths saved to: last_image_tar.txt"
  cat last_image_tar.txt | sed 's/^/   /'
  echo "➡️  内网: docker load -i <tar> && set -a && source last_image_tag.txt && set +a"
  echo "➡️  本地: docker compose up -d"
fi

# ── 安全扫描（api + worker 制品；对齐 version-control-standard §8 / dev-security-standard §5.6）──
# 必须：漏洞库持久化缓存，禁止 docker run --rm 不挂卷导致每次 ~100MB 全量下载
if [[ "$SKIP_SCAN" == "true" ]]; then
  echo ""
  echo "── Security scan: SKIPPED (--skip-scan) ──"
else
  echo ""
  echo "── Security scan (trivy) ──"
  TRIVY_CACHE_DIR="${TRIVY_CACHE_DIR:-${HOME}/.cache/trivy}"
  mkdir -p "$TRIVY_CACHE_DIR"
  TRIVY_SCAN_ARGS=(image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed)
  if [[ "${TRIVY_SKIP_DB_UPDATE:-0}" == "1" ]]; then
    TRIVY_SCAN_ARGS+=(--skip-db-update)
    echo "DB update: skipped (TRIVY_SKIP_DB_UPDATE=1), cache=$TRIVY_CACHE_DIR"
  else
    echo "DB cache: $TRIVY_CACHE_DIR (reuses local DB when still valid; both api & worker share one DB)"
  fi

  scan_image() {
    local img="$1"
    if ! docker image inspect "$img" >/dev/null 2>&1; then
      echo "SKIP: ${img}（本地无镜像，可能仅 --push）"
      return 0
    fi
    # 不加 --exit-code 1：扫描结果不阻断构建；工具超时/失败也仅告警，不拖垮 build
    set +e
    if command -v trivy >/dev/null 2>&1; then
      trivy --cache-dir "$TRIVY_CACHE_DIR" --timeout 15m "${TRIVY_SCAN_ARGS[@]}" "$img"
    elif command -v docker >/dev/null 2>&1; then
      docker run --rm \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v "$TRIVY_CACHE_DIR:/root/.cache/trivy" \
        aquasec/trivy:latest \
        --timeout 15m \
        "${TRIVY_SCAN_ARGS[@]}" "$img"
    else
      echo "SKIP: ${img}（不具备扫描条件）"
      set -e
      return 0
    fi
    local rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
      echo "WARN: trivy scan failed for ${img} (exit=${rc}); build continues"
    fi
  }

  # 本次构建的每个目标各扫一遍（api / worker）；共享 TRIVY_CACHE_DIR，第二张镜像不再重下漏洞库
  for img in "${BUILT_IMAGES[@]}"; do
    echo "→ scan ${img}"
    scan_image "$img"
  done
fi

echo ""
echo "======================================================="
echo "  IMAGE_TAG : ${IMAGE_TAG}"
echo "  API       : ${SAVED_API_IMAGE:-"(not set)"}"
echo "  Worker    : ${SAVED_WORKER_IMAGE:-"(not set)"}"
echo "  Saved     : last_image_tag.txt"
if [[ "$EXPORT_TAR" == "true" ]]; then
  echo "  Tars      : last_image_tar.txt → dist/*.tar"
fi
if [[ "$LOCAL_AVAILABLE" == "true" ]]; then
  echo "  Local     : ${API_NAME}:local / ${WORKER_NAME}:local (for built targets)"
fi
echo "======================================================="
echo "Next step:"
echo "  # 本地开发（默认 compose，源码挂载 + reload）："
echo "  docker compose up -d --force-recreate"
echo ""
echo "  # 或用刚构建的版本号镜像（仍走本仓库本地 compose）"
echo "  set -a && source last_image_tag.txt && set +a"
echo "  docker compose up -d --force-recreate"
echo ""
echo "  # 内网离线（需曾使用 --export-tar）："
echo "  docker load -i dist/agent-file-service-api-….tar"
echo "  docker load -i dist/agent-file-service-worker-….tar"
