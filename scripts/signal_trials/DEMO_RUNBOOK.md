# ProofArena REST signal demo runbook

This is an operator checklist, not an authorization. It grants no deployment, signing, or payment authority.
Run it only against an already reviewed deployment, with a named human separately
authorizing the one state publication and exactly one payment. Never paste credentials, account
identifiers, payment headers, or signing material into this file or a terminal transcript.

The public web host is `https://proofarena.xyz`; the public API host is
`https://api.proofarena.xyz`. The frozen commit window is five-minute (`300000` ms) and the outcome
horizon is one-hour (`3600000` ms). Stop on every failed command or mismatch. `REST` identifies how
the signal was fetched; it makes no claim about the WebSocket path.

## 1. Preflight

Boundary: **container operator**. Start read-only at the public boundary and require both liveness
and durable readiness before any mutation:

```sh
curl -fsS https://api.proofarena.xyz/healthz
curl -fsS https://api.proofarena.xyz/readyz
```

The safe pre-open state is specifically HTTP `404` with `{"error":"no_open_trial"}`. HTTP `200`
means a trial is already open and this run must not create another. Every other status/body pair is
a refusal:

<!-- PREOPEN_CHECK_START -->
```sh
PREOPEN_BODY="$(mktemp)"
trap 'rm -f "$PREOPEN_BODY"' EXIT
PREOPEN_STATUS="$(curl -sS -o "$PREOPEN_BODY" -w '%{http_code}' \
  https://api.proofarena.xyz/signal-trials/open-trial)"
if [ "$PREOPEN_STATUS" = "404" ] && \
  jq -e 'type == "object" and keys == ["error"] and .error == "no_open_trial"' \
    "$PREOPEN_BODY" >/dev/null; then
  echo "expected safe pre-open state: 404 no_open_trial"
elif [ "$PREOPEN_STATUS" = "200" ]; then
  echo "STOP: a trial is already open; do not start a new-open sequence" >&2
  exit 1
else
  echo "STOP: unexpected pre-open status/body mismatch (HTTP $PREOPEN_STATUS)" >&2
  exit 1
fi
```
<!-- PREOPEN_CHECK_END -->

Inside the already deployed API container, use credentials already injected by the deployment
environment. Do not export or print them:

```sh
python /app/scripts/signal_trials/run_preflight.py \
  --out /var/lib/veridex/signal-trials/preflight_result.json
jq -e '.probe_status == "completed" and .season_status != "no_season"' \
  /var/lib/veridex/signal-trials/preflight_result.json
jq -e '.chain_index == "196" and (.bar == "1m" or .bar == "1H")' \
  /var/lib/veridex/signal-trials/preflight_result.json
```

If chain `196` is not the reviewed winner, stop: this producer defaults to `chainIndex=196`.

## 2. Seal

Boundary: **container operator**. Seal only the just-reviewed completed preflight artifact:

```sh
python /app/scripts/signal_trials/fetch_and_seal.py \
  --from-preflight /var/lib/veridex/signal-trials/preflight_result.json \
  --out /var/lib/veridex/signal-trials/packs/
```

Record the emitted sealed pack path and content hash. Do not continue if the path is ambiguous, the
hash is absent, or the command reports `no_season`, `REFUSED`, `ABORTED`, or `FAILED`.

## 3. Publish

Boundary: **container operator**. This is the first durable publication gate. A named human must
approve the exact sealed pack hash before replacing `<reviewed-sealed-pack-dir>`:

```sh
python /app/scripts/signal_trials/score_and_publish.py \
  --pack-dir <reviewed-sealed-pack-dir> \
  --data-dir /var/lib/veridex/signal-trials
curl -fsS https://api.proofarena.xyz/readyz
```

No command here deploys code. Stop if the published season does not report the same chain, bar, and
pack hash that were reviewed.

## 4. Dry-run and open

Boundary: **container operator**. Fetching is separate from opening: the producer prints exactly one
raw JSON signal and never opens a trial.

```sh
python /app/scripts/signal_trials/fetch_latest_signal.py \
  > /tmp/proofarena-rest-signal.json
python /app/scripts/signal_trials/open_live_trial.py \
  --source rest \
  --dry-run \
  --signal-file /tmp/proofarena-rest-signal.json \
  --data-dir /var/lib/veridex/signal-trials \
  > /tmp/proofarena-open-dry-run.json
jq -e '.published == false' /tmp/proofarena-open-dry-run.json
```

The dry-run must say `published=false`; it makes no durable write. Compare its evidence hash and
fields with the reviewed raw signal. Only after a named human approves that exact dry-run may the
container operator perform the single open:

```sh
python /app/scripts/signal_trials/open_live_trial.py \
  --source rest \
  --signal-file /tmp/proofarena-rest-signal.json \
  --data-dir /var/lib/veridex/signal-trials
curl -fsS https://api.proofarena.xyz/signal-trials/open-trial
```

Copy only the public `trial_id`. The buyer must act inside the five-minute (`300000` ms) commit
window. If the window is stale, stop and do not pay.

## 5. Explicit single payment

Boundary: **buyer workstation**, never the API container. The buyer reviews the unpaid `402`
challenge and explicitly authorizes exactly one payment from a separately selected wallet account.
Replace placeholders locally; do not paste their values into shared logs:

```sh
BASE_URL=https://api.proofarena.xyz \
TRIAL_ID=<public-trial-id> \
ONCHAINOS_ACCOUNT_ID=<payer-account-id> \
  ./scripts/signal_trials/demo_pay.sh
```

Do not retry after an indeterminate settlement or missing response. A retry could pay twice. Stop
unless the command proves one confirmed transaction and prints one public receipt id.

## 6. Receipt

Boundary: **buyer workstation**. These are public, read-only checks:

```sh
curl -fsS \
  https://api.proofarena.xyz/signal-trials/receipts/<public-receipt-id>/verify
curl -fsS \
  https://api.proofarena.xyz/signal-trials/trials/<public-trial-id>/receipts
```

Save the public receipt id, trial id, evidence hash, and transaction hash. Never save the payment
challenge, payment response header, wallet account identifier, or signing material.

## 7. One-hour settlement

Boundary: **container operator**. Wait until the trial's `t0_ms + 3600000` one-hour horizon has
passed. Read the reviewed bar from the published preflight; do not guess it:

```sh
BAR="$(jq -er '.bar | select(. == "1m" or . == "1H")' \
  /var/lib/veridex/signal-trials/preflight_result.json)"
python /app/scripts/signal_trials/settle_live_trials.py \
  --data-dir /var/lib/veridex/signal-trials \
  --chain-index 196 \
  --bar "$BAR"
```

If candle coverage is incomplete or the command refuses, record nothing manually and wait for a
later reviewed run.

## 8. QA

Boundary: either workstation, read-only. Recheck both service gates, the open-trial surface, the
receipt, and the public web application:

```sh
curl -fsS https://api.proofarena.xyz/healthz
curl -fsS https://api.proofarena.xyz/readyz
QA_OPEN_BODY="$(mktemp)"
trap 'rm -f "$QA_OPEN_BODY"' EXIT
QA_OPEN_STATUS="$(curl -sS -o "$QA_OPEN_BODY" -w '%{http_code}' \
  https://api.proofarena.xyz/signal-trials/open-trial)"
if [ "$QA_OPEN_STATUS" = "200" ]; then
  jq -e '.trial_id | type == "string"' "$QA_OPEN_BODY"
elif [ "$QA_OPEN_STATUS" = "404" ] && \
  jq -e '.error == "no_open_trial"' "$QA_OPEN_BODY" >/dev/null; then
  echo "QA: closed trial is no longer open, as expected"
else
  echo "STOP: unexpected QA open-trial status/body mismatch (HTTP $QA_OPEN_STATUS)" >&2
  exit 1
fi
curl -fsS \
  https://api.proofarena.xyz/signal-trials/receipts/<public-receipt-id>/verify
curl -fsS https://proofarena.xyz/trials
```

QA is evidence collection only. It authorizes no follow-up deploy, second open, second payment,
manual receipt edit, or forced settlement.
