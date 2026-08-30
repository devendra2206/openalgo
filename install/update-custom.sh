#!/bin/bash

# Custom update wrapper for devendra2206/openalgo (custom-strategies branch).
#
# The official install/update.sh (git pull + dependency sync + DB migrations
# + service/nginx restart) covers everything a stock OpenAlgo install needs.
# This wrapper adds the things specific to this fork that update.sh can't
# know about:
#   1. Rebuilding frontend/dist locally. Stock OpenAlgo doesn't need this
#      (CI force-commits dist to upstream main), but this fork's frontend
#      commits only ever carry frontend/src -- dist is deliberately left out
#      of every push (see docs/CUSTOMIZATIONS.md) -- so the server must build
#      it itself after every pull that touches frontend/src.
#   2. Deploying the tracked strategy scripts from strategies/deployed/ into
#      the live strategies/scripts/ directory (that directory has its own
#      nested .gitignore -- *.py is intentionally untracked there upstream,
#      so a plain `git pull` never touches the actual running scripts).
#   3. A reminder to restart each running strategy from the /python UI --
#      not automatable from here, since those endpoints are session-cookie
#      authenticated (browser login), not API-key authenticated.
#
# Requires Node.js + npm on the server (frontend/package.json engines range)
# purely for this fork's own rebuild step -- a stock install never needs it.
#
# See docs/CUSTOMIZATIONS.md for the full list of what's different from a
# stock install and why.

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

OPENALGO_PATH="/var/python/openalgo"
FRONTEND_DIR="$OPENALGO_PATH/frontend"
DEPLOYED_SCRIPTS_DIR="$OPENALGO_PATH/strategies/deployed"
LIVE_SCRIPTS_DIR="$OPENALGO_PATH/strategies/scripts"
WEB_USER="www-data"
WEB_GROUP="www-data"

echo -e "${BLUE}=== [1/5] Running official OpenAlgo update.sh ===${NC}"
sudo "$OPENALGO_PATH/install/update.sh"

echo ""
echo -e "${BLUE}=== [2/5] Rebuilding frontend ===${NC}"
if ! command -v npm >/dev/null 2>&1; then
    echo -e "${RED}  npm not found -- cannot rebuild frontend/dist.${NC}"
    echo -e "${YELLOW}  Install Node.js (see frontend/package.json 'engines'), then re-run this script.${NC}"
    echo -e "${YELLOW}  The UI will keep serving whatever dist/ currently has on disk until then.${NC}"
else
    (
        cd "$FRONTEND_DIR"
        npm install
        npm run build
    )
    sudo chown -R "$WEB_USER:$WEB_GROUP" "$FRONTEND_DIR/dist"
    echo -e "${GREEN}  frontend/dist rebuilt.${NC}"
fi

echo ""
echo -e "${BLUE}=== [3/5] Deploying tracked strategy scripts ===${NC}"
if [ -d "$DEPLOYED_SCRIPTS_DIR" ]; then
    deployed_count=0
    for f in "$DEPLOYED_SCRIPTS_DIR"/*.py; do
        [ -e "$f" ] || continue
        name=$(basename "$f")
        sudo cp "$f" "$LIVE_SCRIPTS_DIR/$name"
        sudo chown "$WEB_USER:$WEB_GROUP" "$LIVE_SCRIPTS_DIR/$name"
        echo -e "${GREEN}  deployed: $name${NC}"
        deployed_count=$((deployed_count + 1))
    done
    if [ "$deployed_count" -eq 0 ]; then
        echo -e "${YELLOW}  strategies/deployed/ exists but is empty -- nothing to deploy.${NC}"
    fi
else
    echo -e "${YELLOW}  No strategies/deployed/ directory found -- skipping.${NC}"
    echo -e "${YELLOW}  (Expected if this is the first run before that path was pulled.)${NC}"
fi

echo ""
echo -e "${BLUE}=== [4/5] Patching nginx for the strategy_reporting subprocess ===${NC}"
# Idempotent: adds a "location /python { proxy_pass http://127.0.0.1:8766; ... }"
# block (see install/install.sh's own template for the canonical version) so
# the whole /python surface routes to the dedicated strategy_reporting
# subprocess instead of the main app -- see strategy_reporting/server.py's
# module docstring for why. Safe to re-run: skips if the block is already
# present, and rolls back automatically if the patched config fails
# `nginx -t` (never leaves the live site broken).
NGINX_MARKER="location /python {"
NGINX_CONF=$(sudo grep -rl "proxy_pass http://unix:" /etc/nginx/sites-available /etc/nginx/conf.d 2>/dev/null | head -1)

if [ -z "$NGINX_CONF" ]; then
    echo -e "${YELLOW}  Could not auto-detect the live nginx config file (looked in"
    echo "  /etc/nginx/sites-available and /etc/nginx/conf.d for a proxy_pass to"
    echo "  the app's Unix socket). Add this block to your nginx server {} block"
    echo "  yourself, before the main 'location /' block, then 'nginx -t && systemctl"
    echo "  reload nginx' -- see install/install.sh's own /python location block for"
    echo "  the exact, up-to-date version to copy.${NC}"
elif sudo grep -qF "$NGINX_MARKER" "$NGINX_CONF"; then
    echo -e "${GREEN}  Already present in $NGINX_CONF -- nothing to do.${NC}"
else
    BACKUP="${NGINX_CONF}.bak-$(date +%Y%m%d%H%M%S)"
    sudo cp "$NGINX_CONF" "$BACKUP"
    echo "  Backed up $NGINX_CONF -> $BACKUP"

    # Insert right before the main app's own catch-all "location / {
    # proxy_pass http://unix:...}" block -- NOT just the first "location / {"
    # in the file. A plain-HTTP config typically has TWO server{} blocks (a
    # port-80 one that only redirects to HTTPS, and the real port-443 one),
    # and the port-80 block's own "location / { return 301 ...}" comes first
    # textually -- matching on the first occurrence silently patches the
    # unreachable redirect-only server instead of the one actually serving
    # traffic (2026-08-05 incident: every /python/* request kept falling
    # through to the OLD, un-migrated blueprint route with no visible nginx
    # error, since the patched-but-wrong block still passed `nginx -t`).
    sudo python3 - "$NGINX_CONF" << 'PYEOF'
import re, sys

path = sys.argv[1]
with open(path) as f:
    content = f.read()

block = """    location /python {
        proxy_pass http://127.0.0.1:8766;
        proxy_http_version 1.1;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        proxy_buffering off;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
    }

"""

pattern = re.compile(r"( *)location / \{[^}]*\}", re.DOTALL)
found = False


def repl(m):
    global found
    if not found and "proxy_pass http://unix:" in m.group(0):
        found = True
        return block + m.group(0)
    return m.group(0)


new_content = pattern.sub(repl, content)
if not found:
    print("Could not find the unix-socket location / block -- leaving nginx config unchanged.")
    sys.exit(1)

with open(path, "w") as f:
    f.write(new_content)
PYEOF
    PATCH_STATUS=$?

    if [ "$PATCH_STATUS" -ne 0 ]; then
        echo -e "${YELLOW}  Could not locate the app's own location / block -- config left"
        echo "  unchanged. Add the /python block manually -- see install/install.sh's"
        echo -e "  /python location block for the exact version to copy.${NC}"
    elif sudo nginx -t 2>/dev/null; then
        sudo systemctl reload nginx
        echo -e "${GREEN}  Patched $NGINX_CONF and reloaded nginx.${NC}"
    else
        echo -e "${RED}  Patched config failed 'nginx -t' -- restoring backup, nginx untouched.${NC}"
        sudo cp "$BACKUP" "$NGINX_CONF"
        echo -e "${YELLOW}  Add the block manually -- see install/install.sh's /python location block.${NC}"
    fi
fi

echo ""
echo -e "${BLUE}=== [5/5] Manual step required ===${NC}"
echo -e "${YELLOW}"
echo "update.sh restarted the Flask app/service -- it does NOT restart the"
echo "individual strategy subprocesses, which only pick up the deployed"
echo "script changes when explicitly restarted."
echo ""
echo "Go to /python in the browser and, for each running strategy whose"
echo "script changed above: Stop, then Start. Safe to do without disturbing"
echo "open positions (see docs/CUSTOMIZATIONS.md -- resumability guarantees"
echo "for mid-day restarts)."
echo -e "${NC}"

echo -e "${GREEN}Custom update complete.${NC}"
