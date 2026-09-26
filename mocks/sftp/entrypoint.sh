#!/bin/sh
# Keep the SSH host identity on a volume: generate it once, then reuse it on every rebuild/restart.
# Without this, every `docker compose build` would mint a new host key and break the pinned known_hosts.
set -eu
KEYDIR=/var/lib/sftp-hostkeys
if [ ! -f "$KEYDIR/ssh_host_ed25519_key" ]; then
  ssh-keygen -q -t ed25519 -N "" -C sftp-host -f "$KEYDIR/ssh_host_ed25519_key"
  echo "generated new host key: $(ssh-keygen -lf "$KEYDIR/ssh_host_ed25519_key.pub")"
fi
ln -sf "$KEYDIR/ssh_host_ed25519_key" /etc/ssh/ssh_host_ed25519_key
ln -sf "$KEYDIR/ssh_host_ed25519_key.pub" /etc/ssh/ssh_host_ed25519_key.pub
echo "host key: $(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub)"
exec /usr/sbin/sshd -D -e
