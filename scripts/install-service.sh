#!/usr/bin/env bash
# ======================================================================
#  install-service.sh  -  Install the Domshon server as a boot-time
#                         systemd service (Linux).
#  Target: Ubuntu / Debian server (any systemd Linux works).
#  Requirements: bash 4.3+, systemd, sudo/root, python 3.10+
#                (Ubuntu: sudo apt install python3 python3-venv python3-pip)
#  Usage: sudo bash scripts/install-service.sh [install|update|uninstall|status|restart|logs]
#                                              [--nginx DOMAIN [--certbot EMAIL]]
#    update             upgrade the application in /opt/domshon from this folder (or PyPI) and restart
#    --nginx DOMAIN     also install nginx (if missing) and proxy DOMAIN to the server
#    --certbot EMAIL    also get a free HTTPS certificate for DOMAIN (Let's Encrypt)
#  Example: sudo bash scripts/install-service.sh install --nginx files.example.com --certbot me@example.com
#  The same pieces exist as commands:  domshon service ... / domshon nginx ...
#
#  Layout created:
#    /opt/domshon                 virtualenv with the application
#    /etc/domshon/domshon.env  settings file (edit it; the server reloads it live)
#    /var/lib/domshon             config (settings.json, tokens, secret key) + files
#  The service runs as the unprivileged user "domshon".
# ======================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
APP_DIR="${APP_DIR:-/opt/domshon}"
ETC_DIR="/etc/domshon"
DATA_DIR="${DATA_DIR:-/var/lib/domshon}"
SVC_USER="domshon"
UNIT="/etc/systemd/system/domshon-server.service"

if [[ -t 1 ]]; then
    RED=$'\033[0;31m' GRN=$'\033[0;32m' YLW=$'\033[0;33m' BLU=$'\033[0;34m' RST=$'\033[0m'
else
    RED='' GRN='' YLW='' BLU='' RST=''
fi
ok()   { printf "  ${GRN}[OK]${RST}  %s\n" "$*"; }
warn() { printf "  ${YLW}[!!]${RST}  %s\n" "$*"; }
err()  { printf "  ${RED}[XX]${RST}  %s\n" "$*"; }
info() { printf "  ${BLU} * ${RST}  %s\n" "$*"; }

NGINX_DOMAIN=""
CERTBOT_EMAIL=""
ACTION="install"
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            install|update|uninstall|status|restart|logs) ACTION="$1" ;;
            --nginx)   NGINX_DOMAIN="${2:-}"; shift ;;
            --certbot) CERTBOT_EMAIL="${2:-}"; shift ;;
            -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
            *) err "unknown option '$1' (try --help)"; exit 2 ;;
        esac
        shift
    done
}

need_root() { [[ $EUID -eq 0 ]] || { err "Run as root:  sudo bash $0 ${1:-install}"; exit 1; }; }

find_python() {
    local c
    for c in "${PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3 python; do
        [[ -n "$c" ]] || continue
        command -v "$c" &>/dev/null || continue
        "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null && { echo "$c"; return 0; }
    done
    return 1
}

do_install() {
    need_root install
    command -v systemctl &>/dev/null || { err "systemd not found"; exit 1; }
    local py; py=$(find_python) || { err "Python 3.10+ not found (set PYTHON=/path/to/python)"; exit 1; }

    id "$SVC_USER" &>/dev/null || {
        useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SVC_USER" \
            && ok "Created system user '$SVC_USER'" || { err "useradd failed"; exit 1; }
    }

    info "Creating virtualenv in $APP_DIR ..."
    mkdir -p "$APP_DIR" "$ETC_DIR" "$DATA_DIR/config" "$DATA_DIR/files"
    if ! "$py" -m venv "$APP_DIR" 2>/dev/null; then
        if command -v apt-get &>/dev/null; then
            info "venv module missing - installing python3-venv ..."
            apt-get update -qq && apt-get install -y -qq python3-venv python3-pip || { err "apt install failed"; exit 1; }
            "$py" -m venv "$APP_DIR" || { err "venv creation failed"; exit 1; }
        else
            err "venv creation failed"; exit 1
        fi
    fi
    "$APP_DIR/bin/python" -m pip install --quiet --upgrade pip || warn "pip upgrade failed (continuing)"
    if [[ -f "$PROJECT_DIR/pyproject.toml" ]]; then
        info "Installing from $PROJECT_DIR ..."
        "$APP_DIR/bin/python" -m pip install --quiet "$PROJECT_DIR" || { err "install failed"; exit 1; }
    else
        info "Installing domshon from PyPI ..."
        "$APP_DIR/bin/python" -m pip install --quiet domshon || { err "install failed"; exit 1; }
    fi
    ok "Application installed"

    if [[ ! -f "$ETC_DIR/domshon.env" ]]; then
        if [[ -f "$PROJECT_DIR/.env" ]]; then
            cp "$PROJECT_DIR/.env" "$ETC_DIR/domshon.env"
        else   # the same template `domshon service install` uses
            "$APP_DIR/bin/python" -c 'from domshon.deploy import ENV_TEMPLATE; print(ENV_TEMPLATE, end="")' \
                > "$ETC_DIR/domshon.env" || { err "could not write the settings file"; exit 1; }
        fi
        ok "Wrote $ETC_DIR/domshon.env (edit it, changes apply live)"
    else
        info "Keeping existing $ETC_DIR/domshon.env"
    fi
    chown root:"$SVC_USER" "$ETC_DIR/domshon.env"; chmod 640 "$ETC_DIR/domshon.env"
    chown -R "$SVC_USER":"$SVC_USER" "$DATA_DIR"; chmod 700 "$DATA_DIR/config"

    # one source of truth for the unit file: domshon.deploy.render_unit
    APP_PY="$APP_DIR/bin/python" SVC_USER="$SVC_USER" ETC_DIR="$ETC_DIR" DATA_DIR="$DATA_DIR" \
        "$APP_DIR/bin/python" -c '
import os
from pathlib import Path
from domshon.deploy import render_unit
print(render_unit(os.environ["APP_PY"], os.environ["SVC_USER"], Path(os.environ["ETC_DIR"]) / "domshon.env",
                  Path(os.environ["DATA_DIR"])), end="")' > "$UNIT" || { err "could not write $UNIT"; exit 1; }
    systemctl daemon-reload
    systemctl enable --now domshon-server.service || { err "Could not start the service"; exit 1; }
    ok "Service enabled: starts at boot"
    if [[ -n "$NGINX_DOMAIN" ]]; then
        info "Configuring nginx for $NGINX_DOMAIN ..."
        local args=(nginx install --domain "$NGINX_DOMAIN" --env-file "$ETC_DIR/domshon.env" --install-packages)
        [[ -n "$CERTBOT_EMAIL" ]] && args+=(--certbot --email "$CERTBOT_EMAIL")
        "$APP_DIR/bin/python" -m domshon "${args[@]}" || { err "nginx setup failed (service itself is running)"; exit 1; }
        systemctl restart domshon-server.service && ok "Server restarted in proxy mode (127.0.0.1 only)"
    fi
    if [[ -z "$NGINX_DOMAIN" ]]; then
        warn "The server listens on 127.0.0.1 only. Add HTTPS + a public address with:"
        echo "       sudo $APP_DIR/bin/domshon nginx install --domain YOUR.DOMAIN --certbot --email YOU@EXAMPLE.COM"
        echo "       (or set DS_HOST=0.0.0.0 in $ETC_DIR/domshon.env and restart to use port 8787 directly)"
    fi
    echo
    info "First start prints a generated admin password once. Read it with:"
    echo "       sudo journalctl -u domshon-server --no-pager | grep -A4 'first start'"
    info "Or set your own:  sudo -u $SVC_USER DS_CONFIG_DIR=$DATA_DIR/config $APP_DIR/bin/domshon server passwd"
}

do_update() {
    need_root update
    [[ -x "$APP_DIR/bin/python" ]] || { err "$APP_DIR not found - run 'install' first"; exit 1; }
    if [[ -f "$PROJECT_DIR/pyproject.toml" ]]; then
        "$APP_DIR/bin/python" -m pip install --quiet --upgrade "$PROJECT_DIR" || { err "update failed"; exit 1; }
    else
        "$APP_DIR/bin/python" -m pip install --quiet --upgrade domshon || { err "update failed"; exit 1; }
    fi
    systemctl restart domshon-server.service && ok "Updated and restarted"
}

do_uninstall() {
    need_root uninstall
    systemctl disable --now domshon-server.service 2>/dev/null
    rm -f "$UNIT"; systemctl daemon-reload
    ok "Service removed"
    warn "Kept: $APP_DIR, $ETC_DIR, $DATA_DIR (your files). Delete them manually if you really want to."
}

parse_args "$@"
case "$ACTION" in
    install)   do_install ;;
    update)    do_update ;;
    uninstall) do_uninstall ;;
    status)    systemctl status domshon-server.service --no-pager ;;
    restart)   need_root restart; systemctl restart domshon-server.service && ok "Restarted" ;;
    logs)      journalctl -u domshon-server.service -f ;;
esac
