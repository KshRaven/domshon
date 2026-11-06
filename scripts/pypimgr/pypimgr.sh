#!/usr/bin/env bash
# ======================================================================
#  pypimgr.sh  -  Domshon Setup / Run / Build / Update Manager (Linux/macOS)
#  Requirements: bash 3.2+ (the macOS default is fine), python 3.10+ (git optional, docker optional)
#  Usage: bash scripts/pypimgr/pypimgr.sh            interactive menu
#         bash scripts/pypimgr/pypimgr.sh <command>  setup | init | serve | gui | build |
#                                                update | docker | service | passwd | pypi
#
#  Everything installs into <project>/.venv - nothing touches system Python.
#  `pypi` builds the sdist/wheel and uploads to TestPyPI or PyPI (twine, token prompt).
# ======================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
VENV_DIR="$PROJECT_DIR/.venv"
VPY="$VENV_DIR/bin/python"

if [[ -t 1 ]]; then
    RED=$'\033[0;31m'  GRN=$'\033[0;32m'  YLW=$'\033[0;33m'
    BLU=$'\033[0;34m'  CYN=$'\033[0;36m'  BOLD=$'\033[1m'
    DIM=$'\033[2m'     RST=$'\033[0m'
else
    RED='' GRN='' YLW='' BLU='' CYN='' BOLD='' DIM='' RST=''
fi
TW=$(tput cols 2>/dev/null || echo 72); (( TW < 64 )) && TW=64; (( TW > 104 )) && TW=104

hr()   { printf "${DIM}"; printf '%*s' "$TW" '' | tr ' ' '-'; printf "${RST}\n"; }
ok()   { printf "  ${GRN}[OK]${RST}  %s\n" "$*"; }
warn() { printf "  ${YLW}[!!]${RST}  %s\n" "$*"; }
err()  { printf "  ${RED}[XX]${RST}  %s\n" "$*"; }
info() { printf "  ${BLU} * ${RST}  %s\n" "$*"; }
banner() { echo; hr; printf "${CYN}${BOLD}  %s${RST}\n" "$1"; hr; }
anykey() { echo; printf "  ${DIM}Press any key to continue...${RST}"; read -r -s -n1; echo; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
confirm() { printf "  ${YLW}?${RST}  %s [y/N] " "${1:-Are you sure?}"; local r; read -r r; [[ "$(lower "$r")" =~ ^(y|yes)$ ]]; }
ask() { printf "  %-34s [${DIM}%s${RST}]: " "$1" "$2"; local v; read -r v; printf -v "$3" '%s' "${v:-$2}"; }

trap 'echo; echo "  Interrupted."; tput cnorm 2>/dev/null; exit 130' INT TERM

find_python() {
    local c
    for c in "${PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3 python; do
        [[ -n "$c" ]] || continue
        command -v "$c" &>/dev/null || continue
        "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null && { echo "$c"; return 0; }
    done
    return 1
}

have_venv() { [[ -x "$VPY" ]]; }
need_venv() { have_venv || { warn "Not set up yet - running setup first."; do_setup || return 1; }; }

# ---- steps -----------------------------------------------------------
do_setup() {
    banner "Setup: fetch dependencies"
    local py; py=$(find_python) || { err "Python 3.10+ not found (set PYTHON=/path/to/python)"; return 1; }
    if ! have_venv; then
        info "Creating virtual environment (.venv) ..."
        "$py" -m venv "$VENV_DIR" || { err "venv creation failed (on Debian/Ubuntu: apt install python3-venv)"; return 1; }
    fi
    "$VPY" -m pip install --quiet --upgrade pip || warn "pip upgrade failed (continuing)"
    local extras="" r
    ask "Install the desktop app too ([gui], PySide6)? y/n" "y" r
    [[ "$(lower "$r")" == y* ]] && extras="gui"
    ask "Install development tools ([dev,build] - only for working on Domshon)? y/n" "n" r
    [[ "$(lower "$r")" == y* ]] && extras="${extras:+$extras,}dev,build"
    info "Installing Domshon${extras:+ [$extras]} (editable) ..."
    "$VPY" -m pip install -e "$PROJECT_DIR${extras:+[$extras]}" || { err "pip install failed"; return 1; }
    ok "Dependencies installed in $VENV_DIR"
}

do_init() {
    banner "Initialize"
    need_venv || return 1
    cd "$PROJECT_DIR" || return 1
    if [[ ! -f .env ]]; then
        cp .env.example .env && ok "Created .env from .env.example"
    else
        info ".env already exists - keeping it"
    fi
    local domain port
    ask "Public domain (blank = none yet)" "" domain
    ask "Server port" "8787" port
    set_env DS_DOMAIN "$domain"; set_env DS_PORT "$port"
    [[ -n "$domain" ]] && set_env DS_TRUST_PROXY "true"
    ok "Settings written to .env (the running server reloads it live, except host/port/proxy)"
    if confirm "Set the admin password now?"; then do_passwd; fi
    "$VPY" -m domshon server show | head -n 20
}

set_env() {   # set_env KEY VALUE  (edit or append in .env)
    local k="$1" v="$2"
    if grep -qE "^${k}=" "$PROJECT_DIR/.env" 2>/dev/null; then
        sed -i.bak -E "s|^${k}=.*|${k}=${v}|" "$PROJECT_DIR/.env" && rm -f "$PROJECT_DIR/.env.bak"
    else
        printf '%s=%s\n' "$k" "$v" >> "$PROJECT_DIR/.env"
    fi
}

do_passwd() { need_venv || return 1; (cd "$PROJECT_DIR" && "$VPY" -m domshon server passwd); }
do_serve()  { banner "Server (Ctrl+C to stop)"; need_venv || return 1; (cd "$PROJECT_DIR" && "$VPY" -m domshon server serve); }
do_gui()    {
    need_venv || return 1
    "$VPY" -c 'import PySide6' 2>/dev/null || { info "Installing desktop support..."; "$VPY" -m pip install --quiet -e "$PROJECT_DIR[gui]" || return 1; }
    "$VPY" -m domshon gui
}

do_build() {
    banner "Build applications"
    need_venv || return 1
    "$VPY" -c 'import PyInstaller, PySide6' 2>/dev/null || {
        info "Installing build dependencies..."
        "$VPY" -m pip install --quiet -e "$PROJECT_DIR[gui,build]" || { err "install failed"; return 1; }
    }
    local srv; ask "Default server address to bake in" "${DS_BUILD_SERVER:-}" srv
    "$VPY" "$PROJECT_DIR/scripts/build.py" ${srv:+--server "$srv"} --package || { err "Build failed"; return 1; }
    ok "Built. See $PROJECT_DIR/dist"
}

do_update() {
    banner "Update"
    cd "$PROJECT_DIR" || return 1
    if [[ -d .git ]] && command -v git &>/dev/null; then
        info "Pulling latest source..."
        git pull --ff-only || { err "git pull failed (local changes?)"; return 1; }
    else
        warn "Not a git checkout - skipping source update."
    fi
    have_venv || { warn "No .venv - run setup."; return 1; }
    "$VPY" -m pip install --quiet --upgrade pip
    "$VPY" -m pip install --upgrade -e "$PROJECT_DIR" || { err "pip install failed"; return 1; }
    ok "Updated."
    if command -v systemctl &>/dev/null && systemctl is-active --quiet domshon-server.service 2>/dev/null; then
        if confirm "Restart the domshon-server service now?"; then
            if [[ -d /opt/domshon ]]; then sudo /opt/domshon/bin/python -m pip install --quiet --upgrade "$PROJECT_DIR"; fi
            sudo systemctl restart domshon-server.service && ok "Service restarted"
        fi
    fi
}

do_docker() {
    banner "Docker (testing / containerised server)"
    command -v docker &>/dev/null || { err "docker not found"; return 1; }
    cd "$PROJECT_DIR" || return 1
    local c="${1:-}"
    if [[ -z "$c" ]]; then
        echo "   1) build + start   2) stop   3) logs   4) start with nginx HTTPS (needs DS_DOMAIN + certificate, see README)   0) back"
        printf "  > "; local k; read -r k
        case "$k" in 1) c=up;; 2) c=down;; 3) c=logs;; 4) c=https;; *) return 0;; esac
    fi
    [[ -f .env ]] || { cp .env.example .env; ok "Created .env from .env.example"; }
    case "$c" in
        up)    docker compose up -d --build ;;
        down)  docker compose down ;;
        logs)  docker compose logs -f --tail=100 ;;
        https) docker compose --profile https up -d --build ;;
        *)     err "unknown docker command '$c' (up|down|logs|https)"; return 1 ;;
    esac
}

do_service() {
    banner "Install server as boot service"
    if [[ "$(uname -s)" == "Darwin" ]]; then
        need_venv || return 1
        info "macOS: installing a launchd LaunchAgent (starts when you log in)"
        "$VPY" -m domshon service install
        return
    fi
    sudo bash "$PROJECT_DIR/scripts/install-service.sh" install
}


do_pypi() {
    banner "PyPI: build / check / upload"
    need_venv || return 1
    cd "$PROJECT_DIR" || return 1
    "$VPY" -c 'import build, twine' 2>/dev/null || {
        info "Installing build + twine into .venv ..."
        "$VPY" -m pip install --quiet build twine || { err "install failed"; return 1; }
    }
    local c="${1:-}"
    if [[ -z "$c" ]]; then
        echo "   1) build + check   2) upload to TestPyPI   3) upload to PyPI   4) clean dist/   0) back"
        printf "  > "; local k; read -r k
        case "$k" in 1) c=build;; 2) c=test;; 3) c=release;; 4) c=clean;; *) return 0;; esac
    fi
    case "$c" in
        clean)   rm -rf dist build ./*.egg-info src/*.egg-info; ok "Cleaned" ; return 0 ;;
        build|test|release)
            rm -rf dist; "$VPY" -m build || { err "build failed"; return 1; }
            "$VPY" -m twine check dist/* || { err "twine check failed"; return 1; }
            ok "Built and checked:"; ls -1 dist
            [[ "$c" == build ]] && return 0
            local repo="testpypi"; [[ "$c" == release ]] && repo="pypi"
            confirm "Upload to $repo now?" || return 0
            info "Username is __token__; the password is your API token (or set TWINE_USERNAME / TWINE_PASSWORD)."
            "$VPY" -m twine upload --repository "$repo" dist/* && ok "Uploaded to $repo" || err "Upload failed"
            ;;
        *) err "unknown pypi command '$c' (build|test|release|clean)"; return 1 ;;
    esac
}

# ---- entry -------------------------------------------------------------
if [[ $# -gt 0 ]]; then
    case "$1" in
        setup) do_setup ;; init) do_init ;; serve) do_serve ;; gui) do_gui ;; build) do_build ;;
        update) do_update ;; docker) do_docker "${2:-}" ;; service) do_service ;; passwd) do_passwd ;; pypi) do_pypi "${2:-}" ;;
        *) echo "Unknown command: $1"; echo "Use: setup init serve gui build update docker service passwd pypi"; exit 2 ;;
    esac
    exit $?
fi

while true; do
    clear 2>/dev/null
    banner "Domshon Manager"
    echo "   1) Setup      - create .venv, fetch dependencies"
    echo "   2) Initialize - create .env, set domain/port/admin password"
    echo "   3) Run server"
    echo "   4) Run desktop app"
    echo "   5) Build applications (dist/)"
    echo "   6) Update     - pull source, refresh dependencies"
    echo "   7) Docker     - build / start / stop / logs"
    echo "   8) Install server as boot service (needs sudo)"
    echo "   9) PyPI       - build, check, upload (TestPyPI / PyPI)"
    echo "   0) Exit"
    echo; printf "  > "; read -r choice
    case "$choice" in
        1) do_setup; anykey ;;  2) do_init; anykey ;;  3) do_serve; anykey ;;
        4) do_gui; anykey ;;    5) do_build; anykey ;; 6) do_update; anykey ;;
        7) do_docker; anykey ;; 8) do_service; anykey ;; 9) do_pypi; anykey ;;
        0|q|Q) exit 0 ;;
    esac
done
