#!/bin/bash
# One-shot: authorize the operator laptop's SSH public key for the invoking
# user on this droplet (2026-07-06). Exists because the DO recovery console
# mangles long pasted strings — the repo is the file-transfer channel; the
# key below is PUBLIC material and safe to commit.
#
# Usage (on the droplet, as the user that should receive access):
#   cd /opt/packn/hubspot_ticket_automation && git pull && bash scripts/authorize_laptop_key.sh
#
# Idempotent: skips the append if the key is already present.
set -eu

KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII5lrJRtu+w0zvKqEfmMMa/p4W9FKaWgBXtjQNNvBZVV packn-os-laptop'
AUTH="$HOME/.ssh/authorized_keys"

mkdir -p "$HOME/.ssh"
touch "$AUTH"
if grep -qF "packn-os-laptop" "$AUTH"; then
  echo "key already authorized for $(whoami) — nothing to do"
else
  printf '%s\n' "$KEY" >> "$AUTH"
  echo "key appended for $(whoami)"
fi
chmod 700 "$HOME/.ssh"
chmod 600 "$AUTH"
echo "done — try: ssh $(whoami)@$(hostname -I | awk '{print $1}') from the laptop"
