#!/usr/bin/env bash
#
# WANTED - install Windows onto a Hetzner dedicated server from the Linux rescue system.
#
# Runs QEMU/KVM inside the rescue system with the real NVMe device passed through as the VM's
# only disk, so Windows Setup writes a genuine bootable UEFI installation directly onto the
# hardware. When the VM is shut down and the machine is rebooted out of rescue mode, that
# installation is simply what the machine boots.
#
# This needs no KVM console, no ISO mounting by the provider, and no physical access. If anything
# goes wrong the machine can always be put back into the rescue system from Robot, which makes
# every step here retryable.
#
# Usage:
#   ./install-windows.sh build      build the unattend ISO only
#   ./install-windows.sh run        build (if needed) then start the install VM
#   ./install-windows.sh status     is the VM running? has Windows come up inside it?
#   ./install-windows.sh console    print how to attach a VNC viewer
#   ./install-windows.sh stop       shut the VM down cleanly
#
# Required environment (never hard-coded, never committed):
#   WASTED_WIN_ADMIN_USER, WASTED_WIN_ADMIN_PASSWORD, WASTED_WIN_COMPUTERNAME

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WASTED_WORK:-/root/win}"
TARGET_DISK="${WASTED_TARGET_DISK:-/dev/nvme0n1}"
WIN_ISO="$WORK/win11.iso"
UNATTEND_ISO="$WORK/unattend.iso"
OVMF_CODE="/usr/share/OVMF/OVMF_CODE_4M.fd"
OVMF_VARS_SRC="/usr/share/OVMF/OVMF_VARS_4M.fd"
OVMF_VARS="$WORK/OVMF_VARS.fd"
MONITOR="$WORK/monitor.sock"
PIDFILE="$WORK/qemu.pid"
VNC_DISPLAY=1                 # -> 127.0.0.1:5901
SSH_FWD=2222                  # -> VM port 22
RDP_FWD=33890                 # -> VM port 3389
IMAGE_NAME="Windows 11 Enterprise Evaluation"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

need_env() {
    : "${WASTED_WIN_ADMIN_USER:?set WASTED_WIN_ADMIN_USER}"
    : "${WASTED_WIN_ADMIN_PASSWORD:?set WASTED_WIN_ADMIN_PASSWORD}"
    : "${WASTED_WIN_COMPUTERNAME:?set WASTED_WIN_COMPUTERNAME}"
}

# --------------------------------------------------------------------------- safety
check_disk() {
    [[ -b "$TARGET_DISK" ]] || die "$TARGET_DISK is not a block device"

    # Refuse to wipe a disk that already holds something, unless explicitly forced. The whole
    # point of this script is that it destroys the target disk; that must never be a surprise.
    local parts
    parts="$(lsblk -no NAME "$TARGET_DISK" | tail -n +2 | wc -l)"
    if [[ "$parts" -gt 0 && "${WASTED_FORCE_WIPE:-0}" != "1" ]]; then
        lsblk "$TARGET_DISK"
        die "$TARGET_DISK already has $parts partition(s). Set WASTED_FORCE_WIPE=1 to destroy it."
    fi
    log "target disk $TARGET_DISK is empty - safe to install onto"
}

# --------------------------------------------------------------------------- build
build_unattend() {
    need_env
    [[ -f "$HERE/autounattend.xml.template" ]] || die "missing autounattend.xml.template"

    local stage="$WORK/unattend-stage"
    rm -rf "$stage"
    mkdir -p "$stage/wasted"

    # Render the answer file. Escape XML metacharacters in the password so that a generated
    # password can never corrupt the document.
    local esc_pw
    esc_pw="$(printf '%s' "$WASTED_WIN_ADMIN_PASSWORD" \
        | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&apos;/g")"

    # The renderer below reads these from the environment; they must be exported, not just set.
    export ESC_PW="$esc_pw"
    export IMAGE_NAME
    export WASTED_WIN_ADMIN_USER WASTED_WIN_COMPUTERNAME

    python3 - "$HERE/autounattend.xml.template" "$stage/autounattend.xml" <<PYEOF
import sys, os
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding='utf-8').read()
subs = {
    '@@ADMIN_USER@@'    : os.environ['WASTED_WIN_ADMIN_USER'],
    '@@ADMIN_PASSWORD@@': os.environ['ESC_PW'],
    '@@COMPUTERNAME@@'  : os.environ['WASTED_WIN_COMPUTERNAME'],
    '@@IMAGE_NAME@@'    : os.environ['IMAGE_NAME'],
}
for k, v in subs.items():
    if k not in text:
        sys.exit('placeholder %s missing from template' % k)
    text = text.replace(k, v)
leftover = [l for l in text.splitlines() if '@@' in l]
if leftover:
    sys.exit('unsubstituted placeholders remain: %s' % leftover)
open(dst, 'w', encoding='utf-8').write(text)
import xml.dom.minidom; xml.dom.minidom.parse(dst)
print('rendered %s (well-formed XML, no placeholders left)' % dst)
PYEOF

    cp -r "$HERE/payload/wasted/." "$stage/wasted/"

    # Render the network template. These values are private to the deployment, so they live in
    # the gitignored infra/.env.server rather than in this repository.
    : "${WASTED_SERVER_IPV4:?set WASTED_SERVER_IPV4}"
    : "${WASTED_SERVER_GATEWAY:?set WASTED_SERVER_GATEWAY}"
    : "${WASTED_SERVER_MAC:?set WASTED_SERVER_MAC}"
    local prefixlen="${WASTED_SERVER_CIDR##*/}"
    local mac_dashed
    mac_dashed="$(printf '%s' "$WASTED_SERVER_MAC" | tr 'a-f:' 'A-F-')"
    sed -e "s|@@SERVER_IPV4@@|$WASTED_SERVER_IPV4|g" \
        -e "s|@@SERVER_PREFIXLEN@@|${prefixlen:-26}|g" \
        -e "s|@@SERVER_GATEWAY@@|$WASTED_SERVER_GATEWAY|g" \
        -e "s|@@SERVER_DNS1@@|${WASTED_SERVER_DNS1:-185.12.64.1}|g" \
        -e "s|@@SERVER_DNS2@@|${WASTED_SERVER_DNS2:-185.12.64.2}|g" \
        -e "s|@@SERVER_MAC_DASHED@@|$mac_dashed|g" \
        "$stage/wasted/wasted-network.ps1.template" > "$stage/wasted/wasted-network.ps1"
    rm -f "$stage/wasted/wasted-network.ps1.template"
    if grep -q '@@' "$stage/wasted/wasted-network.ps1"; then
        die "unsubstituted placeholders remain in wasted-network.ps1"
    fi
    # CRLF: these are read by Windows tooling, and a stray LF-only .cmd is a classic silent failure.
    find "$stage" -type f \( -name '*.ps1' -o -name '*.cmd' -o -name '*.xml' \) -exec sed -i 's/$/\r/' {} \;

    rm -f "$UNATTEND_ISO"
    xorriso -as mkisofs -quiet -J -joliet-long -R -V WASTEDUA -o "$UNATTEND_ISO" "$stage"
    log "built $UNATTEND_ISO ($(stat -c %s "$UNATTEND_ISO") bytes)"
    log "contents:"
    xorriso -indev "$UNATTEND_ISO" -find / 2>/dev/null | sed 's/^/    /'
}

# --------------------------------------------------------------------------- run
run_vm() {
    [[ -f "$WIN_ISO" ]] || die "missing $WIN_ISO"
    [[ -f "$UNATTEND_ISO" ]] || build_unattend
    check_disk

    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        die "install VM is already running (pid $(cat "$PIDFILE"))"
    fi

    cp -f "$OVMF_VARS_SRC" "$OVMF_VARS"
    rm -f "$MONITOR"

    log "starting install VM: disk=$TARGET_DISK vnc=127.0.0.1:590$VNC_DISPLAY ssh-fwd=$SSH_FWD"

    setsid qemu-system-x86_64 \
        -name wasted-win-install \
        -enable-kvm -cpu host -smp 10 -m 12G \
        -machine q35 \
        -rtc base=utc \
        -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
        -drive if=pflash,format=raw,file="$OVMF_VARS" \
        -drive file="$TARGET_DISK",format=raw,if=none,id=nvme0,cache=none,aio=threads \
        -device nvme,drive=nvme0,serial=WASTED0 \
        -drive file="$WIN_ISO",media=cdrom,readonly=on \
        -drive file="$UNATTEND_ISO",media=cdrom,readonly=on \
        -nic none \
        -vga std \
        -display none \
        -vnc "127.0.0.1:$VNC_DISPLAY" \
        -monitor "unix:$MONITOR,server,nowait" \
        -boot order=d \
        -pidfile "$PIDFILE" \
        < /dev/null > "$WORK/qemu.log" 2>&1 &
    disown

    sleep 4
    [[ -f "$PIDFILE" ]] || { cat "$WORK/qemu.log"; die "VM failed to start"; }
    log "VM started, pid $(cat "$PIDFILE")"

    # Windows install media shows "Press any key to boot from CD" and falls through to a dead UEFI
    # shell if nothing answers. Nothing can press that key on a headless box, so press it here.
    #
    # This MUST stop the instant Setup's UI appears. Setup's progress screen has a focused Cancel
    # button, so a stray Enter after that point opens an "Are you sure you want to quit?" modal
    # that silently halts the install - which is exactly what happened on the first attempt with a
    # naive fixed count of 20 presses. Key presses are therefore gated on what is actually on the
    # screen, not on elapsed time.
    #
    # Signal: the firmware/boot-prompt screens are near-black; Setup's screen is a full-screen
    # blue. Mean pixel brightness separates them cleanly (~0-20 vs ~90). PNG size does NOT - the
    # TianoCore splash compresses larger than Setup's flat blue.
    log 'answering the boot-from-CD prompt (stops as soon as Setup appears)'
    local brightness
    for _ in $(seq 1 40); do
        printf 'screendump %s/probe.ppm\n' "$WORK" | timeout 3 socat - "UNIX-CONNECT:$MONITOR" >/dev/null 2>&1 || true
        sleep 0.4
        brightness=$(python3 - "$WORK/probe.ppm" <<'PY'
import sys
try:
    d = open(sys.argv[1], 'rb').read()
    # P6 header: magic, width, height, maxval - each possibly separated by whitespace/comments
    parts, i = [], 2
    while len(parts) < 3:
        while i < len(d) and d[i:i+1].isspace(): i += 1
        if d[i:i+1] == b'#':
            while i < len(d) and d[i:i+1] != b'\n': i += 1
            continue
        j = i
        while j < len(d) and not d[j:j+1].isspace(): j += 1
        parts.append(int(d[i:j])); i = j
    i += 1
    px = d[i:]
    # Sample WHOLE pixels. A flat byte stride aliases against the 3-byte RGB layout: any
    # stride divisible by 3 reads one channel forever, so a blue screen reads as its red
    # value (26) instead of its true mean (90). Step in multiples of 3 and average all three.
    stride = max(3, ((len(px) // 3) // 10000) * 3)
    total = count = 0
    for o in range(0, len(px) - 2, stride):
        total += px[o] + px[o + 1] + px[o + 2]
        count += 3
    print(int(total / max(1, count)))
except Exception:
    print(-1)
PY
)
        if [[ "$brightness" =~ ^[0-9]+$ ]] && (( brightness > 40 )); then
            log "Setup UI is up (screen brightness $brightness) - stopping key presses"
            break
        fi
        printf 'sendkey ret\n' | timeout 3 socat - "UNIX-CONNECT:$MONITOR" >/dev/null 2>&1 || true
        sleep 1
    done
    log "install running. Watch with: $0 status"
}

# --------------------------------------------------------------------------- status
status_vm() {
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "VM:        RUNNING (pid $(cat "$PIDFILE"))"
    else
        echo "VM:        not running"
        return 0
    fi

    echo "elapsed:   $(ps -o etime= -p "$(cat "$PIDFILE")" | tr -d ' ')"
    echo -n "screen:    "
    printf 'screendump %s/screen.ppm\n' "$WORK" | timeout 5 socat - "UNIX-CONNECT:$MONITOR" >/dev/null 2>&1 \
        && echo "captured to $WORK/screen.ppm" || echo "screendump failed"
    echo
    echo "The install VM has NO network by design (a half-working NAT is what broke the first"
    echo "attempt). Completion is therefore signalled by the guest powering ITSELF off at the end"
    echo "of wasted-firstboot.ps1 - so 'VM: not running' plus a populated disk means SUCCESS."
}

console_info() {
    cat <<EOF
The install VM's screen is on VNC, bound to localhost on the server only.
From your own machine:

    ssh -i ~/.ssh/wasted_server -L 5901:127.0.0.1:590$VNC_DISPLAY root@<your-server-ip> -N

then point a VNC viewer at  127.0.0.1:5901
(macOS has one built in:  open vnc://127.0.0.1:5901 )
EOF
}

stop_vm() {
    [[ -S "$MONITOR" ]] || die "no monitor socket; VM not running?"
    printf 'system_powerdown\n' | timeout 3 socat - "UNIX-CONNECT:$MONITOR" >/dev/null || true
    log "sent ACPI shutdown; waiting up to 120s"
    for _ in $(seq 1 120); do
        [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null || { log "VM stopped"; return 0; }
        sleep 1
    done
    die "VM did not stop; inspect before forcing"
}

mkdir -p "$WORK"
case "${1:-run}" in
    build)   build_unattend ;;
    run)     run_vm ;;
    status)  status_vm ;;
    console) console_info ;;
    stop)    stop_vm ;;
    *)       die "unknown command: $1" ;;
esac
