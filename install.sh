#!/bin/bash
# Public host bootstrap. Prerequisites are installed only with explicit consent.
set -Eeuo pipefail
umask 077
export LC_ALL=C
BOOTSTRAP_VERSION=1.1.1
REPOSITORY=https://github.com/whollycrypto-com/whollycrypto-docker.git
INSTALL_ROOT=/root/whollycrypto-docker
CHECK=false; APPROVE=false; ONLY_DEPS=false; RESUME=false
STEP='Checking host'

say() { printf '%s\n' "$*"; }
fail() { printf '\n[ERROR] %s\n' "$*" >&2; exit 1; }
on_error() {
  local status=$?
  printf '\n[ERROR] %s failed (exit %s). Existing volumes and wallets were not deleted.\n' "$STEP" "$status" >&2
  say 'Read the output above. Fix the cause, then rerun setup or use ./whollycrypto-docker resume if setup choices were saved.' >&2
  exit "$status"
}
confirm() {
  local answer
  if "$APPROVE"; then return; fi
  printf '%s [y/N] ' "$1" >/dev/tty || fail 'Interactive confirmation is required. --yes approves prerequisite installation only.'
  read -r answer </dev/tty || fail 'No confirmation received.'
  [[ "$answer" == y || "$answer" == Y || "$answer" == yes ]] || fail 'Cancelled; no installation was started.'
}
python_command() {
  local candidate
  for candidate in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' 2>/dev/null; then
      printf '%s\n' "$candidate"; return
    fi
  done
  return 1
}
host_check() {
  [[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail 'Requires Linux amd64 / x86-64. ARM, macOS and Windows hosts are not supported.'
  [[ $EUID == 0 ]] || fail 'Run as root. Secrets must remain root-owned.'
  local variable
  for variable in DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES; do
    [[ -z ${!variable:-} ]] || fail "Unset $variable first. Only the local whollycrypto Compose project is supported."
  done
  [[ ! -e /root/whollycrypto/config/whollycrypto.env && ! -e /root/whollycrypto/.env ]] || fail 'A native Wholly Crypto installation exists here. Use a separate VPS for Docker; automatic migration is not supported.'
}
distro_plan() {
  [[ -r ${1:-/etc/os-release} ]] || fail 'Cannot identify Linux. Install prerequisites manually using the official Docker documentation.'
  # shellcheck source=/dev/null
  . "${1:-/etc/os-release}"
  DISTRO=${ID:-}; DISTRO_VERSION=${VERSION_ID:-}; CODENAME=${VERSION_CODENAME:-}
  PACKAGE_KIND=manual
  case "$DISTRO:$DISTRO_VERSION" in
    ubuntu:22.04|ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) PACKAGE_KIND=apt ;;
    fedora:43|fedora:44|fedora:45|rhel:9|rhel:9.*|rhel:10|rhel:10.*) PACKAGE_KIND=dnf ;;
  esac
}
docker_context_check() {
  local endpoint
  endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}') || fail 'Could not inspect the Docker context.'
  [[ "$endpoint" == unix:///var/run/docker.sock || "$endpoint" == unix:///run/docker.sock ]] || fail 'Select the local rootful default Docker context. Remote, Desktop and rootless engines are not supported.'
}
docker_ready_check() {
  docker_context_check
  local identity security compose_version
  identity=$(docker info --format '{{.OSType}}/{{.Architecture}}') || fail 'Docker is not reachable. Start the local Docker service, then retry.'
  [[ "$identity" == linux/x86_64 || "$identity" == linux/amd64 ]] || fail 'Docker must run Linux amd64 containers locally.'
  security=$(docker info --format '{{json .SecurityOptions}}') || fail 'Cannot inspect Docker security settings.'
  [[ "$security" != *rootless* ]] || fail 'Rootless Docker is not supported.'
  compose_version=$(docker compose version --short) || fail 'Docker Compose plugin is missing.'
  [[ "$compose_version" =~ ^v?([2-9]|[1-9][0-9]+)\. ]] || fail 'Docker Compose v2 or newer is required.'
  docker buildx version || fail 'Docker Buildx plugin is missing.'
}
apt_install() {
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=180 -o Acquire::Retries=3 install -y --no-install-recommends "$@"
}
base_packages() {
  local -a packages=(ca-certificates)
  command -v curl >/dev/null 2>&1 || packages+=(curl)
  command -v git >/dev/null 2>&1 || packages+=(git)
  python_command >/dev/null || packages+=(python3)
  case "$PACKAGE_KIND" in
    apt)
      apt-get -o Acquire::Retries=3 update
      apt_install --no-upgrade "${packages[@]}" gnupg ;;
    dnf) dnf -y install "${packages[@]}" gnupg2 ;;
    *) fail "Automatic prerequisite installation is not supported for $DISTRO $DISTRO_VERSION. Install Python 3.9+, Git, curl, Docker Engine, Compose and Buildx manually, then rerun: https://docs.docker.com/engine/install/" ;;
  esac
}
download() {
  curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 120 --retry 3 "$1" -o "$2"
}
check_key() {
  local actual
  actual=$(gpg --batch --show-keys --with-colons "$1" 2>/dev/null | awk -F: '$1 == "fpr" {print $10; exit}')
  [[ "$actual" == "$2" ]] || fail 'Docker repository signing-key fingerprint mismatch. No Docker packages were installed.'
}
install_engine() {
  local scratch conflict destination
  [[ -d /run/systemd/system ]] || fail 'A systemd Linux VPS is required for automatic Docker installation.'
  if [[ "$PACKAGE_KIND" == apt ]]; then
    for conflict in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc docker-ce docker-ce-cli; do
      if dpkg-query -W -f='${Status}' "$conflict" 2>/dev/null | grep -q 'install ok installed'; then
        fail "Existing package $conflict needs manual review. No container runtime was removed or replaced."
      fi
    done
    [[ "$CODENAME" =~ ^[a-z]+$ ]] || fail 'Invalid distribution codename.'
    if { grep -R -l 'download.docker.com' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || true; } | grep -v '/whollycrypto-docker.sources$' >/dev/null; then
      fail 'An existing Docker APT repository was found. Install Docker from it manually, then rerun; it was not overwritten.'
    fi
  elif [[ "$PACKAGE_KIND" == dnf ]]; then
    for conflict in docker docker-client docker-common docker-engine podman-docker containerd runc docker-ce docker-ce-cli; do
      if rpm -q "$conflict" >/dev/null 2>&1; then
        fail "Existing package $conflict needs manual review. No container runtime was removed or replaced."
      fi
    done
    if { grep -R -l 'download.docker.com' /etc/yum.repos.d 2>/dev/null || true; } | grep -v '/whollycrypto-docker.repo$' >/dev/null; then
      fail 'An existing Docker RPM repository was found. Install Docker manually from it, then rerun; it was not overwritten.'
    fi
  else
    fail 'Install Docker manually on this distribution, then rerun setup.'
  fi
  scratch=$(mktemp -d /tmp/whollycrypto-docker-repo.XXXXXXXX)
  download "https://download.docker.com/linux/$DISTRO/gpg" "$scratch/key.asc"
  if [[ "$PACKAGE_KIND" == apt ]]; then
    check_key "$scratch/key.asc" 9DC858229FC7DD38854AE2D88D81803C0EBFCD88
    install -d -m 755 /etc/apt/keyrings
    destination=/etc/apt/keyrings/whollycrypto-docker.asc
    [[ ! -L "$destination" ]] || fail 'Refusing a symbolic-link repository key.'
    [[ ! -e "$destination" ]] || cmp -s "$scratch/key.asc" "$destination" || fail 'An existing repository key differs. Review it manually.'
    install -m 644 "$scratch/key.asc" "$destination"
    printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: amd64\nSigned-By: %s\n' "$DISTRO" "$CODENAME" "$destination" >"$scratch/repository"
    destination=/etc/apt/sources.list.d/whollycrypto-docker.sources
  else
    check_key "$scratch/key.asc" 060A61C51B558A7F742B77AAC52FEB6B621E9F35
    rpm --import "$scratch/key.asc"
    # DNF, not this shell, expands these two repository variables.
    # shellcheck disable=SC2016
    printf '[whollycrypto-docker]\nname=Docker CE Stable\nbaseurl=https://download.docker.com/linux/%s/$releasever/$basearch/stable\nenabled=1\ngpgcheck=1\ngpgkey=https://download.docker.com/linux/%s/gpg\n' "$DISTRO" "$DISTRO" >"$scratch/repository"
    destination=/etc/yum.repos.d/whollycrypto-docker.repo
  fi
  [[ ! -L "$destination" ]] || fail 'Refusing a symbolic-link repository configuration.'
  [[ ! -e "$destination" ]] || cmp -s "$scratch/repository" "$destination" || fail 'An existing repository configuration differs. Review it manually.'
  install -m 644 "$scratch/repository" "$destination"
  if [[ "$PACKAGE_KIND" == apt ]]; then
    apt-get -o Acquire::Retries=3 update
    apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  else
    dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  systemctl enable --now docker
  # Only these known public temporary files are removed, never host/application data.
  rm -f -- "$scratch/key.asc" "$scratch/repository"
  rmdir -- "$scratch"
}
install_plugins() {
  local plan
  local -a missing_plugins=()
  docker compose version >/dev/null 2>&1 || missing_plugins+=(docker-compose-plugin)
  docker buildx version >/dev/null 2>&1 || missing_plugins+=(docker-buildx-plugin)
  ((${#missing_plugins[@]})) || return 0
  if [[ "$PACKAGE_KIND" == apt ]]; then
    plan=$(apt-get --simulate --no-upgrade install "${missing_plugins[@]}") || fail 'Install the missing Compose/Buildx plugins from your existing Docker package provider, then retry.'
    if printf '%s\n' "$plan" | grep -E '^(Inst|Remv) (docker-ce |docker-ce:|docker-ce-cli |docker-ce-cli:|docker.io |containerd|runc|podman)' >/dev/null; then
      fail 'Installing plugins would replace or upgrade an existing runtime. Install compatible plugins manually; no runtime changes were made.'
    fi
    apt_install --no-upgrade "${missing_plugins[@]}"
  elif [[ "$PACKAGE_KIND" == dnf ]]; then
    dnf -y --exclude='docker-ce,docker-ce-cli,containerd.io,runc,podman*' install "${missing_plugins[@]}"
  else
    fail 'Install Compose and Buildx plugins for your existing engine, then retry. The engine was not changed.'
  fi
}
checkout() {
  local script_dir staging
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
  if [[ -f "$script_dir/host/cli.py" && -f "$script_dir/compose.yaml" ]]; then
    INSTALL_ROOT=$script_dir
  elif [[ -e "$INSTALL_ROOT" || -L "$INSTALL_ROOT" ]]; then
    [[ ! -L "$INSTALL_ROOT" && -f "$INSTALL_ROOT/host/cli.py" && -f "$INSTALL_ROOT/compose.yaml" ]] || fail "Existing $INSTALL_ROOT needs review. For an older integration, follow the README upgrade steps. No directory was overwritten."
    [[ $(git -C "$INSTALL_ROOT" remote get-url origin) == "$REPOSITORY" ]] || fail 'The existing checkout has a different origin. It was not modified.'
    [[ $(git -C "$INSTALL_ROOT" describe --tags --exact-match 2>/dev/null) == "v$BOOTSTRAP_VERSION" ]] || fail 'The existing checkout has a different version. Update the integration following its README; no code or data was overwritten.'
    [[ -z $(git -C "$INSTALL_ROOT" status --porcelain --untracked-files=no) ]] || fail 'Tracked Docker files were modified. Review them before setup.'
  else
    staging=$(mktemp -d /root/.whollycrypto-docker-download.XXXXXXXX)
    git clone --depth 1 --branch "v$BOOTSTRAP_VERSION" --single-branch "$REPOSITORY" "$staging/repository"
    [[ ! -e "$INSTALL_ROOT" && ! -L "$INSTALL_ROOT" ]] || fail 'The destination appeared during download. No files were overwritten.'
    mv -T -- "$staging/repository" "$INSTALL_ROOT"
    rmdir -- "$staging"
  fi
  [[ ! -L "$INSTALL_ROOT" && $(stat -c '%u' "$INSTALL_ROOT") == 0 ]] || fail 'Integration directory must be root-owned, not a symlink.'
}
main() {
  local option python existing=false missing=false
  for option in "$@"; do
    case "$option" in
      --check) CHECK=true ;; --yes) APPROVE=true ;;
      --prerequisites-only) ONLY_DEPS=true ;; --resume) RESUME=true ;;
      --help|-h) say 'Usage: bash install.sh [--check | --prerequisites-only] [--yes] [--resume]'; return ;;
      *) fail "Unknown option: $option" ;;
    esac
  done
  trap on_error ERR
  say "Wholly Crypto Docker $BOOTSTRAP_VERSION"
  say '[1/4] Check host and prerequisites'
  host_check
  distro_plan
  say "  Host: $DISTRO $DISTRO_VERSION (Linux amd64)"
  python=$(python_command) || { missing=true; say '  Missing: Python 3.9+'; }
  command -v git >/dev/null 2>&1 || { missing=true; say '  Missing: Git'; }
  command -v curl >/dev/null 2>&1 || { missing=true; say '  Missing: curl'; }
  if command -v docker >/dev/null 2>&1; then
    existing=true
    docker_context_check
    docker compose version >/dev/null 2>&1 || { missing=true; say '  Missing: Docker Compose plugin'; }
    docker buildx version >/dev/null 2>&1 || { missing=true; say '  Missing: Docker Buildx plugin'; }
    say '  Existing Docker detected: its engine, containers and volumes will be preserved.'
  else
    missing=true
    say '  Missing: Docker Engine'
  fi
  if "$CHECK"; then
    "$missing" && fail 'Prerequisites are missing. Run setup to install supported missing prerequisites; no host changes were made.'
    docker_ready_check
    say '[OK] Host prerequisites ready. DNS, HTTPS and application readiness are checked during setup.'
    return
  fi
  command -v flock >/dev/null 2>&1 || fail 'Install util-linux (flock), then rerun.'
  [[ ! -L /run/whollycrypto-docker-bootstrap.lock ]] || fail 'Unsafe bootstrap lock path.'
  exec 9>/run/whollycrypto-docker-bootstrap.lock
  flock -n 9 || fail 'Another Docker setup is running. Wait for it to finish.'
  if "$missing"; then
    say 'Missing tools will be installed from distribution and official Docker repositories.'
    say 'Docker creates firewall rules. Published ports can bypass UFW; also protect ports with your provider firewall.'
    say 'We do not remove runtimes, stop other containers, disable SELinux, change SSH or erase data.'
    confirm 'Install missing prerequisites on this host?'
    STEP='Install host prerequisites'
    say '[2/4] Install missing prerequisites (package-manager output follows)'
    if ! python_command >/dev/null || ! command -v git >/dev/null || ! command -v curl >/dev/null || ! "$existing"; then
      base_packages
    fi
    python=$(python_command) || fail 'Python 3.9+ is still unavailable. Install a supported version, then retry.'
    if "$existing"; then install_plugins; else install_engine; fi
  else
    say '[2/4] Prerequisites already installed; no packages changed'
  fi
  if ! docker info >/dev/null 2>&1; then
    [[ -d /run/systemd/system ]] || fail 'Start the local Docker daemon manually, then retry.'
    confirm 'Docker is stopped. Start docker.service (without restarting other services)?'
    systemctl start docker
  fi
  docker_ready_check
  if "$ONLY_DEPS"; then say '[OK] Host prerequisites ready. No Wholly Crypto containers were created.'; return; fi
  STEP='Get Docker integration'
  say '[3/4] Prepare the versioned Docker integration'
  checkout
  STEP='Configure and start Wholly Crypto'
  say '[4/4] Start guided application setup'
  say 'The image downloads the latest signed stable merchant release, independently of the Docker integration version.'
  if [[ ! -t 0 ]]; then
    exec 0</dev/tty || fail 'Application setup needs a terminal for domains and passwords. SSH interactively and rerun.'
  fi
  if "$RESUME"; then "$python" "$INSTALL_ROOT/host/cli.py" resume; else "$python" "$INSTALL_ROOT/host/cli.py" setup; fi
}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
