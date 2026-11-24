#!/bin/sh
# Installs Peekaboo from this directory, replacing FaceID 0.x if it is there.
# On a fresh install PAM is left alone until you run:
#   sudo pam-auth-update --enable peekaboo peekaboo-password
set -eu
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

if command -v dpkg-query >/dev/null; then
    missing=
    for p in python3-opencv python3-numpy python3-cryptography python3-gi gir1.2-adw-1 gjs \
             libpam0g-dev libglib2.0-bin gcc make; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'ok installed' || missing="$missing $p"
    done
    # shellcheck disable=SC2086
    [ -z "$missing" ] || { echo "installing:$missing"; apt-get install -y $missing; }
fi
for m in yunet sface eye; do
    [ -f models/$m.onnx ] || { echo "run 'make models' first" >&2; exit 1; }
done
make -s all
make -s install
gtk-update-icon-cache -qtf /usr/share/icons/hicolor 2>/dev/null || true
/usr/lib/peekaboo/setup
echo
echo "Peekaboo is installed. Next:"
echo "  peekaboo gui              set up your face with both cameras"
echo "  peekaboo doctor           check the hardware"
echo "  peekaboo animation on     GNOME animation (log out and in once)"
if grep -qs pam_peekaboo.so /etc/pam.d/common-auth; then
    echo "It is switched on for logins."
elif [ -d /usr/share/pam-configs ] && command -v pam-auth-update >/dev/null; then
    echo "Then switch it on for logins (keep a root shell open the first time):"
    echo "  sudo pam-auth-update --enable peekaboo peekaboo-password"
else
    echo "See the README for switching it on in PAM on this distribution."
fi
