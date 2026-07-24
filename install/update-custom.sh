#!/bin/bash

# Custom update wrapper for devendra2206/openalgo (custom-strategies branch).
#
# The official install/update.sh (git pull + dependency sync + DB migrations
# + service/nginx restart) covers everything a stock OpenAlgo install needs.
# This wrapper adds the two things specific to this fork that update.sh
# can't know about:
#   1. Deploying the tracked strategy scripts from strategies/deployed/ into
#      the live strategies/scripts/ directory (that directory has its own
#      nested .gitignore -- *.py is intentionally untracked there upstream,
#      so a plain `git pull` never touches the actual running scripts).
#   2. A reminder to restart each running strategy from the /python UI --
#      not automatable from here, since those endpoints are session-cookie
#      authenticated (browser login), not API-key authenticated.
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
DEPLOYED_SCRIPTS_DIR="$OPENALGO_PATH/strategies/deployed"
LIVE_SCRIPTS_DIR="$OPENALGO_PATH/strategies/scripts"
WEB_USER="www-data"
WEB_GROUP="www-data"

echo -e "${BLUE}=== [1/3] Running official OpenAlgo update.sh ===${NC}"
sudo "$OPENALGO_PATH/install/update.sh"

echo ""
echo -e "${BLUE}=== [2/3] Deploying tracked strategy scripts ===${NC}"
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
echo -e "${BLUE}=== [3/3] Manual step required ===${NC}"
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
