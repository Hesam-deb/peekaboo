#!/bin/sh
# Removes Peekaboo. PAM goes first, so logins never name a module that is gone.
# Pass --purge to also delete enrolled faces, the key and the configuration.
set -eu
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
! command -v pam-auth-update >/dev/null || pam-auth-update --package --remove peekaboo peekaboo-password
if grep -qs pam_peekaboo.so /etc/pam.d/*; then
    echo "pam_peekaboo is still used in /etc/pam.d; remove it there and retry" >&2
    exit 1
fi
systemctl disable --now peekaboo.socket peekaboo.service 2>/dev/null || true
rm -f /usr/lib/*/security/pam_peekaboo.so /usr/lib64/security/pam_peekaboo.so \
      /usr/lib/security/pam_peekaboo.so /usr/share/pam-configs/peekaboo \
      /usr/share/pam-configs/peekaboo-password /usr/lib/systemd/system/peekaboo.socket \
      /usr/lib/systemd/system/peekaboo.service /usr/bin/peekaboo \
      /usr/share/polkit-1/actions/io.github.hesam-deb.peekaboo.policy \
      /usr/share/applications/io.github.hesam_deb.Peekaboo.desktop \
      /usr/share/metainfo/io.github.hesam_deb.Peekaboo.metainfo.xml \
      /usr/share/man/man1/peekaboo.1 /usr/share/bash-completion/completions/peekaboo \
      /usr/share/icons/hicolor/scalable/apps/io.github.hesam_deb.Peekaboo.svg \
      /usr/share/icons/hicolor/symbolic/apps/io.github.hesam_deb.Peekaboo-symbolic.svg
rm -rf /usr/lib/peekaboo /usr/share/peekaboo \
       /usr/share/gnome-shell/extensions/peekaboo@hesam-deb.github.io
systemctl daemon-reload
if [ "${1:-}" = --purge ]; then
    rm -rf /var/lib/peekaboo /etc/peekaboo /run/peekaboo
fi
echo "Peekaboo removed."
