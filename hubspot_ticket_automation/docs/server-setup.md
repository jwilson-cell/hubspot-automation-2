# Server setup — DigitalOcean Ubuntu

Runbook for provisioning (or re-provisioning) the Ubuntu droplet that runs Pack'N's ticket automation under cron. The production deployment currently lives at `167.99.229.91`, runs as user `packn`, project rooted at `/opt/packn/hubspot_ticket_automation/`.

This runbook is the **corrected version** — it reflects the specific quirks learned during the initial deployment (2026-04-24). If you re-run it on a fresh droplet, you'll land on a working system without re-hitting the same gotchas.

---

## Phase 1: Create the droplet

In the DigitalOcean web UI:

1. Create → Droplets
2. Image: **Ubuntu 24.04 LTS x64**
3. Size: **Basic / Regular Intel, 2 GB RAM / 1 vCPU / 50 GB SSD** (~$12/mo). A 1 GB droplet runs out of memory during `npm install -g @anthropic-ai/claude-code`.
4. Region: NYC1 or NYC3 (closest to US Eastern ops).
5. Authentication: **SSH Key**. Add your laptop's public key from `C:\Users\sonia\.ssh\id_ed25519.pub` (contents of that file, single line starting with `ssh-ed25519`).
6. Hostname: `packn-automation`.
7. Create.

First SSH as root from your laptop:

```bash
ssh root@<IPV4>
```

---

## Phase 2: Create the packn user + SSH hardening

As root on the droplet:

```bash
adduser --gecos "" --disabled-password packn
usermod -aG sudo packn
mkdir -p /home/packn/.ssh
cp /root/.ssh/authorized_keys /home/packn/.ssh/
chown -R packn:packn /home/packn/.ssh
chmod 700 /home/packn/.ssh
chmod 600 /home/packn/.ssh/authorized_keys
```

**IMPORTANT — packn has no password yet.** `--disabled-password` means SSH key login works but `sudo` will fail. Set one now **while still in the root session**:

```bash
passwd packn
```

Pick any strong password, write it down. Then make sudo passwordless for the automation account (cleaner for cron):

```bash
echo 'packn ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/packn
chmod 0440 /etc/sudoers.d/packn
visudo -c -f /etc/sudoers.d/packn
```

The `visudo -c` check should print `parsed OK`. From here on `sudo` from packn just works — no password prompt.

**From a new terminal on your laptop**, verify packn SSH works:

```bash
ssh packn@<IPV4>
whoami   # → packn
sudo whoami   # → root (no password prompt)
```

Keep the root session open as a safety net until the packn session is confirmed. Then harden SSH + firewall + unattended upgrades in the root session:

```bash
# Disable root SSH and password auth
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# Firewall — SSH only
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

# Unattended security updates
apt-get update
apt-get install -y unattended-upgrades
systemctl enable --now unattended-upgrades
```

From here on, work as packn via `ssh packn@<IPV4>`. If you ever need root console access (forgot sudo password, etc.), use DigitalOcean's **Recovery Console**: your droplet → Settings tab → Recovery console → Launch Console. Reset root password at Settings → Reset root password (emailed to you) if needed.

---

## Phase 3: Install runtime

As packn (with `sudo` for system installs):

```bash
# Node.js 20 LTS (for Claude Code CLI)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
node --version   # expect v20.x.x

# npm cache permissions — can get polluted if earlier installs ran as root
sudo chown -R packn:packn /home/packn/.npm

# Claude Code CLI
sudo npm install -g @anthropic-ai/claude-code
claude --version   # expect 2.x

# Python venv + git + tmux (handy for keeping long commands alive through SSH drops)
sudo apt-get install -y python3-venv python3.12-venv python3-pip git tmux
```

### ~/.bashrc ownership

If the adduser step left `~/.bashrc` root-owned for any reason, fix it before adding env vars:

```bash
sudo chown packn:packn /home/packn/.bashrc /home/packn/.profile /home/packn/.bash_logout
```

---

## Phase 4: Anthropic API key

Generate a key at https://console.anthropic.com → API Keys → Create Key. Name it `packn-server`. Copy the `sk-ant-...` value.

Set it for interactive shells AND cron:

```bash
# Interactive
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.bashrc
source ~/.bashrc

# Cron uses /etc/environment (not ~/.bashrc)
echo 'ANTHROPIC_API_KEY=sk-ant-...' | sudo tee -a /etc/environment

# Verify
echo "$ANTHROPIC_API_KEY" | head -c 12
```

**Never paste the key into a chat transcript.** Rotate immediately if it leaks.

---

## Phase 5: Clone the project + Python venv

```bash
# Directory layout — project lives in a subdirectory because the GitHub repo
# name differs from the project folder name inside it.
cd /opt
sudo mkdir packn
sudo chown packn:packn packn
cd /opt/packn
git clone https://github.com/jwilson-cell/hubspot-automation-2.git .

# This creates /opt/packn/hubspot_ticket_automation/ (subdirectory).
# All further commands operate from THAT path, not /opt/packn.
cd /opt/packn/hubspot_ticket_automation

# Python venv
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r scripts/requirements.txt

# Verify
.venv/bin/python -c "import google.auth, yaml; print('imports ok')"
```

### `py` shell wrapper (do NOT use a symlink)

The SKILL.md files and the cron entries call `py scripts/...`. We need `/usr/local/bin/py` to resolve to the venv python.

**Using a symlink fails silently** — Python's venv detection walks back to the resolved target (system Python) instead of the venv. Use a shell wrapper script that explicitly execs the venv interpreter:

```bash
printf '#!/bin/sh\nexec /opt/packn/hubspot_ticket_automation/.venv/bin/python "$@"\n' | sudo tee /usr/local/bin/py > /dev/null
sudo chmod +x /usr/local/bin/py
hash -r
py --version   # Python 3.12.x
py -c "import sys; print(sys.prefix)"   # /opt/packn/hubspot_ticket_automation/.venv
py -c "import google.auth; print('ok')"
```

All three checks must pass. If `sys.prefix` is anything other than the venv path, `py` is wrong and the scripts won't find the Google libs.

### Clean up Ubuntu's pre-installed `pythonpy`

Ubuntu 24.04 ships a command called `py` (the `pythonpy` package) at `/usr/bin/py`. Our wrapper at `/usr/local/bin/py` takes precedence because `/usr/local/bin` is earlier in PATH, but removing the conflict is cleaner:

```bash
sudo apt-get remove -y pythonpy 2>/dev/null || true
hash -r
```

---

## Phase 6: Copy secrets from the laptop

From a terminal **on your Windows laptop** (not the SSH session), one scp per file, each on a single line:

```bash
scp C:\Users\sonia\claude_code\projects\hubspot_ticket_automation\config\.secrets\hubspot_token.txt       packn@<IPV4>:/opt/packn/hubspot_ticket_automation/config/.secrets/
scp C:\Users\sonia\claude_code\projects\hubspot_ticket_automation\config\.secrets\shipsidekick_token.txt  packn@<IPV4>:/opt/packn/hubspot_ticket_automation/config/.secrets/
scp C:\Users\sonia\claude_code\projects\hubspot_ticket_automation\config\.secrets\token.json              packn@<IPV4>:/opt/packn/hubspot_ticket_automation/config/.secrets/
scp C:\Users\sonia\claude_code\projects\hubspot_ticket_automation\config\.secrets\credentials.json        packn@<IPV4>:/opt/packn/hubspot_ticket_automation/config/.secrets/
scp C:\Users\sonia\claude_code\projects\hubspot_ticket_automation\config\.secrets\sheets_token.json       packn@<IPV4>:/opt/packn/hubspot_ticket_automation/config/.secrets/
scp C:\Users\sonia\claude_code\projects\hubspot_ticket_automation\config\.secrets\sheets_client.json      packn@<IPV4>:/opt/packn/hubspot_ticket_automation/config/.secrets/
```

Back on the server (as packn):

```bash
mkdir -p /opt/packn/hubspot_ticket_automation/config/.secrets
chmod 700 /opt/packn/hubspot_ticket_automation/config/.secrets
chmod 600 /opt/packn/hubspot_ticket_automation/config/.secrets/*
ls -la config/.secrets/   # expect 6 files, -rw-------, owned by packn:packn
```

Verify Gmail OAuth works through the helper:

```bash
cd /opt/packn/hubspot_ticket_automation
PAYLOAD='{"to_emails":["lconner@gopackn.com"],"subject":"smoke","body_plain":"server verify"}'
echo "$PAYLOAD" | py scripts/send_digest_email.py
```

Should print a dry-run preview with `"from": "Pack'N Customer Care <customercare@gopackn.com>"`. Add `--send` for a live test.

---

## Phase 7: HubSpot MCP (for Claude Code CLI)

Claude Code CLI on a standalone server doesn't inherit claude.ai-hosted MCPs. HubSpot publishes a self-hostable `@hubspot/mcp-server` npm package that authenticates with the private-app bearer token. Configure it once:

```bash
HUBSPOT_TOKEN=$(cat /opt/packn/hubspot_ticket_automation/config/.secrets/hubspot_token.txt)
claude mcp add hubspot --env HUBSPOT_ACCESS_TOKEN="$HUBSPOT_TOKEN" -- npx -y @hubspot/mcp-server
claude mcp list
```

**Important**: the env var name is `HUBSPOT_ACCESS_TOKEN`, NOT `PRIVATE_APP_ACCESS_TOKEN`. The latter is from HubSpot's own docs but doesn't match what this package actually reads.

The MCP tool names exposed by `@hubspot/mcp-server` differ from the `mcp__claude_ai_HubSpot__*` names the skills were originally written against. Tools are now `mcp__hubspot__hubspot-search-objects`, `mcp__hubspot__hubspot-create-engagement`, etc. Claude adapts to the mismatch at runtime (and the skills fall back to direct REST calls using the same token if MCP is unavailable), so no skill edits are strictly required — but long-term it's cleaner to update the skill files to match the new tool names.

### Pre-authorize tool usage (critical for headless cron)

Claude Code prompts for permission before calling any tool by default. In a cron context there's no one to answer, so the automation hangs. Two belt-and-suspenders mitigations:

1. **Commit a project allow-list** in `.claude/settings.json`:

   ```json
   {
     "permissions": {
       "allow": ["mcp__hubspot__*", "Bash", "Read", "Write", "Edit", "Glob", "Grep"]
     }
   }
   ```

2. **Pass `--dangerously-skip-permissions`** on every cron invocation (see Phase 8).

Both are already in place in the current deployment.

---

## Phase 8: Cron

Edit packn's crontab:

```bash
crontab -e
```

Paste (replace the `ANTHROPIC_API_KEY` value with the real key, **no leading whitespace on any line**):

```cron
MAILTO=lconner@gopackn.com
PATH=/usr/local/bin:/opt/packn/hubspot_ticket_automation/.venv/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/bin
ANTHROPIC_API_KEY=sk-ant-...

# Ticket processing — every 30 minutes
*/30 * * * *  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-tickets --dangerously-skip-permissions >> outputs/runs/cron-tickets.log 2>&1

# Digest — 8am / 12pm / 3pm ET weekdays (UTC during EDT: 12:00, 16:00, 19:00)
0 12 * * 1-5  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-digest --dangerously-skip-permissions >> outputs/runs/cron-digest.log 2>&1
0 16 * * 1-5  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-digest --dangerously-skip-permissions >> outputs/runs/cron-digest.log 2>&1
0 19 * * 1-5  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-digest --dangerously-skip-permissions >> outputs/runs/cron-digest.log 2>&1
```

Save with `Ctrl+O`, Enter, `Ctrl+X`. Verify:

```bash
crontab -l
```

### DST

These UTC hours are correct for EDT (UTC−4). On Sunday, Nov 1, 2026, EST resumes (UTC−5); shift each digest UTC hour by **+1**:

```cron
0 13 * * 1-5   (was 12)
0 17 * * 1-5   (was 16)
0 20 * * 1-5   (was 19)
```

The `*/30` ticket cron stays as-is (interval-based, DST-agnostic). An email reminder routine is scheduled for Nov 1 at 13:00 UTC (`trig_018y6PNzoMQJo8XYzu7q2Vhz` in the claude.ai /schedule system).

### Logrotate

Prevent `cron-*.log` from growing unbounded:

```bash
sudo tee /etc/logrotate.d/packn > /dev/null <<'EOF'
/opt/packn/hubspot_ticket_automation/outputs/runs/cron-*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
EOF
```

---

## Phase 9: Smoke tests

Manual:

```bash
cd /opt/packn/hubspot_ticket_automation
claude -p /packn-tickets --dangerously-skip-permissions 2>&1 | tee /tmp/smoke-tickets.log
claude -p /packn-digest  --dangerously-skip-permissions 2>&1 | tee /tmp/smoke-digest.log
```

Expected outcomes:

1. `/packn-tickets` → run log at `outputs/runs/<ISO>.md`. Any new/modified tickets get draft notes with `PACKN_METADATA_V1` blocks OR auto-sent replies for form Mispack/Carrier. Sheets sync runs.
2. `/packn-digest` → Gmail send (message id on stdout) to `lconner + chansen`. Each included ticket gets a `[DIGESTED at …]` marker note in HubSpot.

Cron:

```bash
# Wait for the next */30 tick, then:
cat outputs/runs/cron-tickets.log | tail -30
```

Should show a run started, processed 0+ tickets, exited cleanly.

---

## Troubleshooting

- **Claude hangs on first tool call** → `.claude/settings.json` missing/corrupt OR forgot `--dangerously-skip-permissions` in the invocation.
- **"Unknown command: /packn-tickets"** → `.claude/commands/packn-tickets.md` missing in the repo. `git pull` on server, verify with `ls .claude/commands/`.
- **"ModuleNotFoundError: No module named 'google'"** → `py` is resolving to system Python, not the venv. Check `readlink -f /usr/local/bin/py` points at the venv's `bin/python`, AND that `/usr/local/bin/py` is a wrapper script (not a symlink). See Phase 5.
- **SSH drops during long `claude` runs** → use `tmux new -s packn` before invoking, `tmux attach -t packn` to reconnect.
- **Gmail token expires** → the OAuth refresh token is long-lived but can be revoked by Gmail settings. Re-run `py scripts/gmail_auth.py` from a machine with a browser, copy fresh `config/.secrets/token.json` to the server.
- **Cron writes to cron-*.log but the email never arrives** → check `head cron-digest.log` for the final status line. If it says "queue empty", the queue genuinely had no undigested tickets. If it says "send failed", check the traceback. Most likely cause: `crm.objects.companies.read` scope missing on the HubSpot private app.
- **Cron-level mail routing** — MAILTO only works if the server can send mail. If it can't, `ufw allow out 25` or install postfix in "Internet Site" mode and point at Gmail SMTP. Not wired up by default.
