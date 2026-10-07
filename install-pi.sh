#!/usr/bin/env bash
# install-pi.sh: install or update the Pi's side of piwii from this repo's
# system/ folder. Run it on the Pi, as root, from a clone of the repo; to
# update later, git pull and run it again.
#
#   sudo ./install-pi.sh                   install or update, then reload what changed
#   sudo ./install-pi.sh --check           only report what would change (exit 1 if anything would)
#   sudo ./install-pi.sh --restart-gadget  also restart piwii-gadget if it changed
#                                          (that unplugs the game drive from the Wii)
#   ./install-pi.sh --root DIR             install into DIR instead of /, without touching
#                                          packages or services (for trying it out)
#
# What it does with each kind of file:
#   - files the repo owns (kernel modules list, systemd units, the piwii-* scripts)
#     are installed whenever they differ. Each is replaced in one step, so a
#     sync that's running keeps reading its old copy.
#   - per-Pi files (/etc/default/piwii, the udev rule, the NFS export) are
#     installed from their .example only when missing, never overwritten.
#   - the sudoers file is only checked: granting passwordless sudo stays a
#     manual step.
#   - config.txt is never replaced; only the USB gadget line is added if missing.
set -euo pipefail

SRC=$(cd "$(dirname "$0")/system" && pwd)
ROOT=/
CHECK=0
RESTART_GADGET=0

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case $1 in
        --check) CHECK=1 ;;
        --restart-gadget) RESTART_GADGET=1 ;;
        --root) [ $# -ge 2 ] || usage 2; ROOT=$2; shift ;;
        -h|--help) usage ;;
        *) echo "install-pi: unknown option $1" >&2; usage 2 ;;
    esac
    shift
done
LIVE=0; [ "$ROOT" = / ] && LIVE=1
[ "$LIVE" = 0 ] || [ "$(id -u)" -eq 0 ] || { echo "install-pi: run as root (sudo)" >&2; exit 1; }
[ "$LIVE" = 1 ] || ROOT=$(mkdir -p "$ROOT" && cd "$ROOT" && pwd)

# Per-Pi files: installed from <file>.example only when missing.
PER_PI=(/etc/default/piwii /etc/udev/rules.d/99-piwii-drive.rules /etc/exports.d/piwii.exports)
SUDOERS=/etc/sudoers.d/010-admin-nopasswd
BOOT=/boot/firmware/config.txt
GADGET_LINE=dtoverlay=dwc2,dr_mode=peripheral
STAGING=/srv/piwii/staging
STAGING_OWNER=1000   # the Pi's admin user; the status tmpfs uses the same uid
PACKAGES=(nfs-kernel-server python3)
UNITS=(srv-piwii-staging-status.mount piwii-status.service piwii-sync.path piwii-gadget.service nfs-server.service)
SYSCTL=/etc/sysctl.d/piwii.conf

pending=0      # changes --check found
changed=()     # files this run installed or updated
todo=()        # things left for the person running it
reboot=0

PLACEHOLDER='^[^#]*<[A-Za-z0-9_-]+>'   # an unfilled <value> left from a template

t() { echo "${ROOT%/}$1"; }   # a Pi path under the install root
say() { printf '  %-12s %s\n' "$1" "$2"; }
is_root() { [ "$(id -u)" -eq 0 ]; }
is_changed() { local f; for f in "${changed[@]+"${changed[@]}"}"; do [ "$f" = "$1" ] && return 0; done; return 1; }
any_changed() { local f; for f in "${changed[@]+"${changed[@]}"}"; do case $f in $1) return 0 ;; esac; done; return 1; }

# Copy $1 to Pi path $2 with mode $3, replacing any old copy in one step.
put() {
    local dest; dest=$(t "$2")
    mkdir -p "$(dirname "$dest")"
    install -m "$3" "$1" "$dest.piwii-new"
    if is_root; then chown 0:0 "$dest.piwii-new"; fi
    mv -f "$dest.piwii-new" "$dest"
}

managed() {
    local src=$1 path=$2 mode=644 dest
    case $path in /usr/local/sbin/*) mode=755 ;; esac
    dest=$(t "$path")
    if [ -f "$dest" ] && cmp -s "$src" "$dest" && [ "$(stat -c %a "$dest")" = "$mode" ]; then
        say same "$path"; return
    fi
    if [ "$CHECK" = 1 ]; then
        pending=$((pending + 1))
        if [ -f "$dest" ]; then say "would update" "$path"; diff -u "$dest" "$src" | sed 's/^/      /' || true
        else say "would add" "$path"; fi
        return
    fi
    local verb=added; [ -f "$dest" ] && verb=updated
    put "$src" "$path" "$mode"; changed+=("$path"); say "$verb" "$path"
}

per_pi() {
    local path=$1 dest; dest=$(t "$path")
    if [ -e "$dest" ]; then
        if grep -qE "$PLACEHOLDER" "$dest"; then
            [ "$CHECK" = 0 ] || pending=$((pending + 1))
            say "fill in" "$path (still has <placeholders> from its template)"
            todo+=("fill in $path (see the README), then run this again")
        elif [ -f "$SRC$path" ] && ! cmp -s "$SRC$path" "$dest"; then say kept "$path (yours; differs from this repo's copy)"
        else say kept "$path"; fi
        return
    fi
    if [ "$CHECK" = 1 ]; then pending=$((pending + 1)); say "would add" "$path (from its template)"; return; fi
    put "$SRC$path.example" "$path" 644; changed+=("$path")
    say added "$path (from its template: fill it in)"
    todo+=("fill in $path (see the README), then run this again")
}

boot_config() {
    local dest; dest=$(t "$BOOT")
    if [ ! -f "$dest" ]; then
        say missing "$BOOT"; todo+=("add $GADGET_LINE under [all] in your boot config.txt"); return
    fi
    if grep -qE "^[[:space:]]*$GADGET_LINE([[:space:]]|$)" "$dest"; then say same "$BOOT ($GADGET_LINE)"; return; fi
    if [ "$CHECK" = 1 ]; then pending=$((pending + 1)); say "would add" "$GADGET_LINE to $BOOT"; return; fi
    cp -p "$dest" "$dest.piwii-bak-$(date +%Y%m%d-%H%M%S)"
    printf '\n[all]\n# piwii: USB gadget (device) mode on the USB-C port\n%s\n' "$GADGET_LINE" >> "$dest"
    changed+=("$BOOT"); reboot=1; say updated "$BOOT (added $GADGET_LINE; old copy kept as .piwii-bak-*)"
}

staging_marker() {
    local dir file; dir=$(t "$STAGING"); file=$dir/.piwii-staging
    if [ -f "$file" ]; then say same "$STAGING/.piwii-staging"; return; fi
    if [ "$CHECK" = 1 ]; then pending=$((pending + 1)); say "would add" "$STAGING/.piwii-staging"; return; fi
    local own=(); if is_root; then own=(-o "$STAGING_OWNER" -g "$STAGING_OWNER"); fi
    [ -d "$dir" ] || install -d -m 775 "${own[@]+"${own[@]}"}" "$dir"
    install -m 644 "${own[@]+"${own[@]}"}" /dev/null "$file"
    say added "$STAGING/.piwii-staging"
}

echo "piwii: $( [ "$CHECK" = 1 ] && echo checking || echo installing ) $SRC -> $ROOT"

echo "Packages"
missing=()
for p in "${PACKAGES[@]}"; do
    if dpkg -s "$p" >/dev/null 2>&1; then say same "$p"; else missing+=("$p"); fi
done
if [ ${#missing[@]} -gt 0 ]; then
    if [ "$CHECK" = 1 ] || [ "$LIVE" = 0 ]; then
        [ "$CHECK" = 0 ] || pending=$((pending + ${#missing[@]}))
        for p in "${missing[@]}"; do say missing "$p"; done
    else
        apt-get install -y "${missing[@]}"
        for p in "${missing[@]}"; do say added "$p"; done
    fi
fi

echo "Files"
while IFS= read -r src; do
    path=${src#"$SRC"}
    case $path in
        *.example|"$SUDOERS"|"$BOOT") continue ;;
    esac
    skip=0; for p in "${PER_PI[@]}"; do [ "$path" = "$p" ] && skip=1; done
    [ "$skip" = 1 ] || managed "$src" "$path"
done < <(find "$SRC" -type f | sort)
for p in "${PER_PI[@]}"; do per_pi "$p"; done
if [ -e "$(t "$SUDOERS")" ]; then say kept "$SUDOERS"
else say missing "$SUDOERS"; todo+=("add $SUDOERS by hand if setup tools need passwordless sudo (see the README)"); fi
boot_config
staging_marker
any_changed '/etc/modules-load.d/*' && reboot=1

configured() { [ -f "$(t /etc/default/piwii)" ] && ! grep -qE "$PLACEHOLDER" "$(t /etc/default/piwii)"; }

echo "Services"
if [ "$LIVE" = 0 ]; then
    say skipped "services (--root)"
else
    # Kernel settings from $SYSCTL: applied when the live value differs (a
    # new or changed file, or a value changed by hand since boot).
    while IFS='=' read -r key value; do
        key=$(echo "$key" | tr -d '[:space:]'); value=$(echo "$value" | tr -d '[:space:]')
        case $key in ''|'#'*|';'*) continue ;; esac
        live=$(sysctl -n "$key" 2>/dev/null || echo '?')
        if [ "$live" = "$value" ]; then say same "$key = $value"
        elif [ "$CHECK" = 1 ]; then pending=$((pending + 1)); say "would set" "$key = $value (now $live)"
        else sysctl -q -w "$key=$value" >/dev/null; say set "$key = $value (was $live)"; fi
    done < "$SRC$SYSCTL"
    if [ "$CHECK" = 0 ] && any_changed '/etc/systemd/system/*'; then systemctl daemon-reload; say reloaded systemd; fi
    if [ "$CHECK" = 0 ] && is_changed /etc/udev/rules.d/99-piwii-drive.rules; then
        udevadm control --reload && udevadm trigger; say reloaded udev
    fi
    for u in "${UNITS[@]}"; do
        if systemctl is-enabled -q "$u" 2>/dev/null; then say enabled "$u"
        elif [ "$CHECK" = 1 ]; then pending=$((pending + 1)); say "would enable" "$u"
        else systemctl enable -q "$u"; say enabled "$u (now)"; fi
    done
    if [ "$CHECK" = 1 ]; then
        for u in "${UNITS[@]}"; do systemctl is-active -q "$u" || say inactive "$u"; done
    else
        for u in srv-piwii-staging-status.mount nfs-server.service; do
            systemctl is-active -q "$u" || { systemctl start "$u"; say started "$u"; }
        done
        if ! configured; then
            todo+=("start piwii's services once /etc/default/piwii is filled in: run this again, or reboot")
        else
            if any_changed '/etc/systemd/system/piwii-status.service' || is_changed /usr/local/sbin/piwii-status; then
                systemctl restart piwii-status.service; say restarted piwii-status.service
            elif ! systemctl is-active -q piwii-status.service; then
                systemctl start piwii-status.service; say started piwii-status.service
            fi
            if is_changed /etc/systemd/system/piwii-sync.path; then
                systemctl restart piwii-sync.path; say restarted piwii-sync.path
            elif ! systemctl is-active -q piwii-sync.path; then
                systemctl start piwii-sync.path; say started piwii-sync.path
            fi
            if is_changed /etc/systemd/system/piwii-gadget.service || is_changed /usr/local/sbin/piwii-gadget; then
                if [ "$RESTART_GADGET" = 1 ]; then
                    systemctl restart piwii-gadget.service; say restarted "piwii-gadget.service (the Wii saw the drive unplug and replug)"
                else
                    todo+=("piwii-gadget changed: run sudo systemctl restart piwii-gadget when the Wii isn't using the drive (it unplugs the drive), or reboot")
                fi
            elif ! systemctl is-active -q piwii-gadget.service; then
                todo+=("piwii-gadget isn't running: sudo systemctl start piwii-gadget plugs the drive into the Wii (it also starts at boot)")
            fi
            is_changed /usr/local/sbin/piwii-sync && say note "piwii-sync changes apply from the next sync"
        fi
    fi
    if is_changed /etc/exports.d/piwii.exports; then
        todo+=("set the server host's IP in /etc/exports.d/piwii.exports, then sudo exportfs -ra")
    fi
fi

echo
if [ "$CHECK" = 1 ]; then
    if [ "$pending" -eq 0 ]; then echo "piwii: the Pi matches this repo."
    else echo "piwii: $pending change(s) would be made. Run without --check to apply them."; fi
else
    echo "piwii: ${#changed[@]} file(s) installed or updated."
fi
[ "$reboot" = 0 ] || echo "  Reboot to apply the boot config or kernel module changes."
for item in "${todo[@]+"${todo[@]}"}"; do echo "  To do: $item"; done
[ "$CHECK" = 0 ] || [ "$pending" -eq 0 ]
