#!/bin/bash
# Keeps the Pi's staging share mounted on the server host for the web server's
# container (piwii). Unraid only: it relies on the Unassigned Devices plugin,
# Unraid's notify and /boot/config. On another Linux host, keep the share
# mounted with systemd automount or autofs instead (see the README).
# Runs every minute on the server host via
# /boot/config/plugins/dynamix/piwii-staging-mount.cron.
#
# Why: piwii used to mount the share as a Docker NFS volume, so a container
# start failed whenever the Pi was unreachable, and nothing retried it.
# Now the server host owns the mount, so the web server always starts.
#
# Two mounts:
#   REMOTE  /mnt/remotes/piwii-staging  the share, mounted by Unassigned
#           Devices, so it shows in the Unraid UI like the NAS. Its entry is
#           in /boot/config/plugins/unassigned.devices/samba_mount.cfg (UD
#           loads that into /tmp/unassigned.devices/config/ at boot and works
#           from the copy; add it through the UD UI, or edit both).
#           UD mounts it once at array start and never retries; this script
#           asks UD to mount it whenever it's missing or stale.
#   MIRROR  /mnt/addons/piwii/staging   a bind mount of REMOTE. piwii gets the
#           parent, /mnt/addons/piwii (rslave), so a new bind made here shows
#           up inside the running container. Binding REMOTE itself would pin
#           whatever was mounted when piwii started (a remount would need a
#           piwii restart), and binding /mnt/remotes would expose the NAS.
#
# Each run:
#   REMOTE not mounted (boot, failed mount)  -> UD mount, then re-bind MIRROR
#   stale file handle                        -> UD unmount + mount, re-bind
#   mounted but the Pi doesn't answer        -> leave it: the hard mount
#                                               resumes when the Pi is back
#   MIRROR missing or not the live share     -> re-bind it
# `piwii-staging-mount.sh --remount` drops everything and mounts fresh.
# While the share is down, MIRROR is an empty folder; piwii checks for the
# marker file .piwii-staging before using it.
# Reports to the Uptime Kuma push monitor KUMA_PUSH_PIWII_STAGING_MOUNT every
# run, and raises an Unraid notification (-> Discord) whenever it remounts.

# Settings come from the environment, else from piwii's server/.env (the same
# file the container uses; see server/.env.example). Only the named key is
# read, never sourced: .env also holds the login password.
ENV_FILE=${PIWII_ENV_FILE:-$(cd "$(dirname "$0")/../.." && pwd)/server/.env}
setting() {   # setting KEY: $KEY from the environment, else from ENV_FILE
    local v=${!1:-}
    [ -n "$v" ] || v=$(sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -1)
    v=${v%\"}; v=${v#\"}; v=${v%\'}; v=${v#\'}
    printf '%s' "$v"
}

UD_DEVICE=$(setting PIWII_UD_DEVICE)   # the Pi's export, e.g. 192.168.1.50:/srv/piwii/staging (any address; this setup's Pi is 192.168.4.61)
REMOTE=${PIWII_REMOTE:-/mnt/remotes/piwii-staging}
MIRROR=${PIWII_MIRROR:-/mnt/addons/piwii/staging}
CONTAINER=${PIWII_CONTAINER:-piwii}
CONTAINER_MIRROR=${PIWII_CONTAINER_MIRROR:-/mnt/piwii/staging}   # MIRROR inside the container
MARKER=.piwii-staging

exec 9>/var/run/piwii-staging-mount.lock
flock -n 9 || exit 0   # a previous run is still waiting on the Pi

# Optional Uptime Kuma reporting: kuma_push comes from this setup's deployment
# repo (/boot/config/scripts/kuma-push.sh). Without it, reporting is skipped.
if [ -r /boot/config/scripts/kuma-push.sh ]; then
    . /boot/config/scripts/kuma-push.sh
else
    kuma_push() { :; }
fi

if [ -z "$UD_DEVICE" ]; then
    kuma_push PIWII_STAGING_MOUNT down "PIWII_UD_DEVICE isn't set (in $ENV_FILE or the environment)"
    exit 1
fi

notify() {
    /usr/local/emhttp/webGui/scripts/notify -e "piwii staging" "$@"
}

# probe DIR: empty output and status 0 when DIR's marker and status/ are
# readable. status/ is a separate tmpfs export on the Pi (crossmnt), recreated
# at each Pi boot, so that's where a stale handle shows up after a Pi reboot.
# The mount is hard, so a hung Pi blocks the call; NFS waits are killable, so
# a KILL after 15 s gets us out (status 137).
probe() {
    timeout -s KILL 15 stat -c %n "$1/$MARKER" "$1/status/." 2>&1 >/dev/null
}

# Remove MIRROR here and in the piwii container. The host unmount alone isn't
# enough: once piwii reads status/, the NFS client automounts it (crossmnt)
# inside the container, and Linux won't propagate an unmount to a mount that
# has a submount. The old mirror would stay underneath the new one in piwii,
# keeping the old NFS connection (and any stale handle) alive.
drop_mirror() {
    local pid
    pid=$(docker inspect -f '{{.State.Pid}}' "$CONTAINER" 2>/dev/null)
    if [ -n "$pid" ] && [ "$pid" != 0 ]; then
        while grep -q " $CONTAINER_MIRROR " "/proc/$pid/mountinfo"; do
            nsenter -t "$pid" -m -p -r -w umount -l "$CONTAINER_MIRROR" || break
        done
    fi
    while mountpoint -q "$MIRROR"; do umount -l "$MIRROR" || break; done
}

bind_mirror() {
    drop_mirror
    mkdir -p "$MIRROR"
    mount --bind "$REMOTE" "$MIRROR"
}

ud_mount() {   # $1: why
    local err
    drop_mirror   # so nothing keeps the old NFS connection alive
    if mountpoint -q "$REMOTE"; then
        /usr/local/sbin/rc.unassigned umount "$UD_DEVICE" >/dev/null 2>&1
        mountpoint -q "$REMOTE" && umount -l "$REMOTE"
    fi
    # UD refuses to mount a server its ping cache doesn't list as online, and
    # only refreshes that cache while its UI page is open: refresh it first.
    /usr/local/emhttp/plugins/unassigned.devices/scripts/get_ud_stats ping >/dev/null 2>&1
    /usr/local/sbin/rc.unassigned mount "$UD_DEVICE" >/dev/null 2>&1
    if mountpoint -q "$REMOTE" && err=$(probe "$REMOTE") && [ -z "$err" ]; then
        bind_mirror
        logger -t piwii-staging "mounted $UD_DEVICE ($1)"
        kuma_push PIWII_STAGING_MOUNT up "mounted ($1)"
        notify -i normal -s "piwii: staging share mounted" -d "$UD_DEVICE on $REMOTE ($1)."
    else
        logger -t piwii-staging "mount of $UD_DEVICE failed ($1): ${err:-not mounted; see the UD log}"
        kuma_push PIWII_STAGING_MOUNT down "can't mount $UD_DEVICE ($1): ${err:-not mounted}"
    fi
}

if [ "${1:-}" = --remount ]; then   # by hand: start over with a fresh mount
    ud_mount "remount requested by hand"
    exit 0
fi

if ! mountpoint -q "$REMOTE"; then
    ud_mount "was not mounted"
    exit 0
fi

err=$(probe "$REMOTE"); rc=$?
if [ "$rc" -eq 137 ]; then
    kuma_push PIWII_STAGING_MOUNT down "mounted, but the Pi isn't answering (waiting for it)"
    exit 0
elif grep -qi "stale file handle" <<<"$err"; then
    logger -t piwii-staging "stale file handle on $REMOTE; remounting"
    ud_mount "stale file handle"
    exit 0
elif [ "$rc" -ne 0 ] || [ -n "$err" ]; then
    kuma_push PIWII_STAGING_MOUNT down "mounted, but $REMOTE/$MARKER: ${err:-not readable}"
    exit 0
fi

# REMOTE is fine; make sure MIRROR shows it.
if ! mountpoint -q "$MIRROR" || [ -n "$(probe "$MIRROR")" ]; then
    bind_mirror
    logger -t piwii-staging "re-bound $MIRROR to $REMOTE"
fi
kuma_push PIWII_STAGING_MOUNT up
