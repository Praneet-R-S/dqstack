#!/bin/sh
# Starts DQ-Stack's SFTP file drop (atmoz/sftp; docker-compose.yml service sftp).
#
# The accounts come from /home/.dqstack/users.conf, which
#   python -m dqstack.app.manage sftp-add-folder ...
# writes into the shared volume, with each account's public key in
# /home/<folder>/.ssh/keys/. atmoz/sftp creates the accounts when the container is
# created, so after adding a folder recreate the service:
#   docker compose --profile sftp up -d --force-recreate sftp
# The host key lives in its own volume so that it survives upgrades.
set -e
conf=/home/.dqstack/users.conf
while ! grep -qv '^#' "$conf" 2>/dev/null; do
  echo "dq-sftp: no folders yet; add one with manage sftp-add-folder (deploy/README.md section 13)" >&2
  sleep 60
done
mkdir -p /etc/sftp /etc/ssh/hostkeys
if [ ! -f /etc/ssh/hostkeys/ssh_host_ed25519_key ]; then
  ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/hostkeys/ssh_host_ed25519_key
fi
chmod 600 /etc/ssh/hostkeys/ssh_host_ed25519_key
ln -sf "$conf" /etc/sftp/users.conf
exec /entrypoint
