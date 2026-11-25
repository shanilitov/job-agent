#!/usr/bin/env bash
# Deploys the job agent to Azure Functions (Flex Consumption).
# Run it in Azure Cloud Shell (Bash), from the repository root:
#
#     git clone https://github.com/shanilitov/job-agent && cd job-agent
#     bash azure_function/deploy.sh
#
# It asks for the Telegram token, chat ID and Healthchecks ping URL with hidden
# input, so they never appear on screen or in the shell history.
# Safe to run again: existing resources are reused and the code is redeployed.
set -euo pipefail

RG="${RG:-job-agent-rg}"
SUFFIX="$(az account show --query id -o tsv | tr -d '-' | cut -c1-8)"
STORAGE="${STORAGE:-jobagent${SUFFIX}}"
APP="${APP:-job-agent-${SUFFIX}}"

step() { printf '\n==> %s\n' "$*"; }

step "Choosing a region that supports Flex Consumption"
REGIONS="$(az functionapp list-flexconsumption-locations --query '[].name' -o tsv)"
LOC=""
for r in israelcentral westeurope northeurope swedencentral; do
  if grep -qx "$r" <<<"$REGIONS"; then LOC="$r"; break; fi
done
LOC="${LOC:-$(head -n1 <<<"$REGIONS")}"
echo "Region: $LOC"

step "Resource group $RG"
az group create -n "$RG" -l "$LOC" -o none

step "Storage account $STORAGE"
if ! az storage account show -g "$RG" -n "$STORAGE" -o none 2>/dev/null; then
  az storage account create -g "$RG" -n "$STORAGE" -l "$LOC" \
    --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false -o none
fi
CONN="$(az storage account show-connection-string -g "$RG" -n "$STORAGE" -o tsv)"

step "Function app $APP (Python 3.12, Flex Consumption, 512 MB)"
if ! az functionapp show -g "$RG" -n "$APP" -o none 2>/dev/null; then
  az functionapp create -g "$RG" -n "$APP" --storage-account "$STORAGE" \
    --flexconsumption-location "$LOC" --runtime python --runtime-version 3.12 \
    --instance-memory 512 -o none
fi

step "Settings (input is hidden)"
read -r -s -p "Telegram bot token: " TG_TOKEN; echo
read -r -p "Telegram chat ID: " TG_CHAT
read -r -s -p "Healthchecks.io ping URL (Enter to skip): " HC_URL; echo
SETTINGS=("TELEGRAM_BOT_TOKEN=$TG_TOKEN" "TELEGRAM_CHAT_ID=$TG_CHAT" "JOB_AGENT_STATE_CONN=$CONN")
[ -n "$HC_URL" ] && SETTINGS+=("HEALTHCHECK_URL=$HC_URL")
az functionapp config appsettings set -g "$RG" -n "$APP" --settings "${SETTINGS[@]}" -o none
unset TG_TOKEN HC_URL

step "Packaging the code"
BUILD="$(mktemp -d)"
cp azure_function/function_app.py azure_function/host.json azure_function/requirements.txt "$BUILD/"
cp job_agent.py "$BUILD/"
[ -f seen.json ] && cp seen.json "$BUILD/"   # starting state for the first Azure run
(cd "$BUILD" && zip -q -r app.zip .)

step "Deploying (remote build, takes a few minutes)"
az functionapp deployment source config-zip -g "$RG" -n "$APP" \
  --src "$BUILD/app.zip" --build-remote true -o none

step "Waiting for the functions to load"
for _ in $(seq 1 30); do
  if az functionapp function show -g "$RG" -n "$APP" --function-name run_now -o none 2>/dev/null; then break; fi
  sleep 10
done
az functionapp function list -g "$RG" -n "$APP" --query '[].name' -o tsv

KEY="$(az functionapp keys list -g "$RG" -n "$APP" --query functionKeys.default -o tsv)"
HOST="$(az functionapp show -g "$RG" -n "$APP" --query defaultHostName -o tsv)"

cat <<EOF

Done.
  Function app: $APP  (resource group $RG, region $LOC)
  Daily run:    16:30 and 17:30 UTC; sends once, at 19:30 Israel time
  Manual run:   curl "https://$HOST/api/run_now?code=<key>"

Running one manual check now. A Telegram message should arrive within a few minutes.
EOF
curl -sS --max-time 900 "https://$HOST/api/run_now?code=$KEY" || echo "(No reply in time. The run may still be going: check Telegram in a few minutes, or the function logs in the portal.)"
