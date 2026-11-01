#!/usr/bin/env bash
# CI-only Linux helper. Keep APT authentication and isolate mirror selection.
set -euo pipefail

packages=(ffmpeg)
if [[ "${1:-}" == --pdf ]]; then
  packages+=(poppler-utils)
elif [[ $# -gt 0 ]]; then
  echo 'Usage: install_ci_media_tools.sh [--pdf]' >&2
  exit 2
fi

verify_tools() {
  ffmpeg -version >/dev/null 2>&1 &&
    ffprobe -version >/dev/null 2>&1 &&
    { [[ "${1:-}" != --pdf ]] || { pdftotext -v >/dev/null 2>&1 && pdftoppm -v >/dev/null 2>&1; }; }
}

if verify_tools "${1:-}"; then
  echo 'Required media tools already work.'
  exit 0
fi

codename="${VERSION_CODENAME:-}"
if [[ -z "$codename" ]]; then
  # shellcheck source=/dev/null
  source /etc/os-release
  codename="${VERSION_CODENAME:?Ubuntu codename is required}"
fi
[[ "$codename" =~ ^[a-z]+$ ]] || { echo 'Invalid Ubuntu codename' >&2; exit 2; }
sources=$(mktemp)
trap 'rm -f "$sources"' EXIT
chmod 644 "$sources"
cat > "$sources" <<EOF
deb https://archive.ubuntu.com/ubuntu $codename main restricted universe multiverse
deb https://archive.ubuntu.com/ubuntu $codename-updates main restricted universe multiverse
deb https://security.ubuntu.com/ubuntu $codename-security main restricted universe multiverse
EOF
apt_options=(
  -o "Dir::Etc::sourcelist=$sources"
  -o 'Dir::Etc::sourceparts=-'
  -o 'Acquire::Retries=2'
  -o 'Acquire::http::Timeout=30'
  -o 'Acquire::https::Timeout=30'
  -o 'APT::Update::Error-Mode=any'
)
# Wall-clock bounds also catch a mirror that makes continuous but very slow progress.
timeout 90s sudo env DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" update
timeout 240s sudo env DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" install --no-install-recommends -y "${packages[@]}"
verify_tools "${1:-}"
echo 'Required media tools verified.'
