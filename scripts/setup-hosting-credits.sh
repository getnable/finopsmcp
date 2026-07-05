#!/usr/bin/env bash
# One-shot setup for the hosting-credit purchase flow (the human half of 0.8.119).
#
# Prereq (one browser click, no keys typed):   stripe login
# Then run:                                    ./scripts/setup-hosting-credits.sh
#
# What it does, in order:
#   1. Creates the two hosting-credit products + one-time prices in Stripe:
#        Pro      500 credits  -> $200, grants $100 of managed AI (50% margin)
#        Startups 10000 credits -> $4000, grants $2000 of managed AI
#   2. Creates a payment link for each.
#   3. Writes STRIPE_CREDIT_PAYMENT_LINKS / STRIPE_CREDIT_PRICE_IDS to Vercel
#      production env ("id:granted_usd" pairs, the format the webhook reads).
#   4. Redeploys production so the webhook picks the env up.
#
# Safe to re-run: it creates NEW products each time, so run once; if you need to
# redo it, archive the old products in the Stripe dashboard first.
set -euo pipefail

command -v stripe >/dev/null || { echo "stripe CLI not installed"; exit 1; }
command -v vercel >/dev/null || { echo "vercel CLI not installed"; exit 1; }
stripe config --list | grep -q . || true

echo "── creating Pro credits ($200 → \$100 managed AI) ──"
PRO_PRODUCT=$(stripe products create \
  --name "nable hosting credits — Pro (500 credits)" \
  --description "One month of managed-AI allowance for a hosted nable instance. Use-it-or-lose-it within the month." \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
PRO_PRICE=$(stripe prices create --product "$PRO_PRODUCT" --unit-amount 20000 --currency usd \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
PRO_JSON=$(stripe payment_links create -d "line_items[0][price]=$PRO_PRICE" -d "line_items[0][quantity]=1")
PRO_LINK=$(echo "$PRO_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
PRO_URL=$(echo "$PRO_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['url'])")
echo "  $PRO_PRODUCT / $PRO_PRICE / $PRO_LINK"
echo "  buy link: $PRO_URL"

echo "── creating Startups credits ($4000 → \$2000 managed AI) ──"
ST_PRODUCT=$(stripe products create \
  --name "nable hosting credits — Startups (10000 credits)" \
  --description "One month of managed-AI allowance for a hosted nable instance at Startups scale. Use-it-or-lose-it within the month." \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
ST_PRICE=$(stripe prices create --product "$ST_PRODUCT" --unit-amount 400000 --currency usd \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
ST_JSON=$(stripe payment_links create -d "line_items[0][price]=$ST_PRICE" -d "line_items[0][quantity]=1")
ST_LINK=$(echo "$ST_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
ST_URL=$(echo "$ST_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['url'])")
echo "  $ST_PRODUCT / $ST_PRICE / $ST_LINK"
echo "  buy link: $ST_URL"

LINKS="$PRO_LINK:100,$ST_LINK:2000"
PRICES="$PRO_PRICE:100,$ST_PRICE:2000"
echo "── writing Vercel production env ──"
# Remove existing values first so re-adding never fails on duplicates.
vercel env rm STRIPE_CREDIT_PAYMENT_LINKS production --yes 2>/dev/null || true
vercel env rm STRIPE_CREDIT_PRICE_IDS production --yes 2>/dev/null || true
printf %s "$LINKS"  | vercel env add STRIPE_CREDIT_PAYMENT_LINKS production --sensitive
printf %s "$PRICES" | vercel env add STRIPE_CREDIT_PRICE_IDS production --sensitive
echo "  STRIPE_CREDIT_PAYMENT_LINKS = $LINKS"
echo "  STRIPE_CREDIT_PRICE_IDS     = $PRICES"

echo "── redeploying production so the webhook picks up the env ──"
vercel deploy --prod --yes >/dev/null
echo ""
echo "Done. Test it end to end: buy the \$200 link yourself, then check the Stripe"
echo "customer's metadata for hosting_credit_usd_<month>, then refund the payment"
echo "in the dashboard. Apply a grant on a box with:"
echo "  finops credits grant 100 --note <invoice id>"
