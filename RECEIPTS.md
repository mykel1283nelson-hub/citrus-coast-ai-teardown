# Sell-side receipts + x402 notes

## Settlement (the fix, 2026-10-16)

Before this change the server called facilitator `/verify` only. A buyer
could pay and verify, but funds never moved on-chain, the seller was never
paid, and no settlement ever triggered Bazaar indexing. Now the server
calls `/settle` after a successful `/verify` and delivers the report only
after a successful settle. The settlement tx hash is returned to the buyer
in the `X-PAYMENT-RESPONSE` header and in `body.settlement`.

## Receipts

- On-chain settlement tx hash = the durable receipt. It is in every
  response header + body, and the buyer/client records it.
- `sell_receipts.jsonl` (next to `server.py`) is a best-effort local log,
  same JSONL shape as the buy-side receipts. It is ephemeral on Render
  free tier — do not treat it as the source of truth.
- Shape: `agent, direction="sell", resource, amount, asset, txid,
  network, payer, pay_to, verdict_class="settled", http_status,
  observed_at`.

## Referral (verified, not implemented as a seller header)

toll402 docs (`https://toll402.dev/llms.txt`, "Referrals"): send
`x-toll402-ref: <recommender 0x wallet>` + `x-toll402-agent: <your
wallet>` on calls **to toll402**; 30% of settled calls are paid on-chain
to the recommender. This is gateway-level on toll402 calls, NOT a
seller-added header on this endpoint. The earlier "10% referral header"
shorthand was wrong (wrong rate, wrong placement). No fake referral
header is implemented here.

## v2 (separate verified step, not in this change)

Live challenge stays x402 v1 until v2 is verified end-to-end. v2 spec
findings (Coinbase x402): facilitator `POST /verify` + `POST /settle`
with body `{x402Version: 2, paymentPayload, paymentRequirements}`;
`PaymentPayload = {x402Version: 2, accepted: PaymentRequirements,
payload: {signature, authorization}}` (EIP-3009 authorization);
headers `X-PAYMENT` / `PAYMENT-SIGNATURE` (request, base64 JSON),
`PAYMENT-RESPONSE` (success, SettleResponse). Do not advertise v2 until
a v2 self-settle is verified against the live endpoint with a v2 client.

## Listings (correct indexes)

- CDP Bazaar / PayAI Bazaar: settlement-triggered discovery — verify via
  each index's `/discovery/resources` filtered by payTo after a settled call.
- x402scan: SIWX registration.
- 402index.io: crawler pickup via `/.well-known/402index-verify.txt`.
- MCP Registry: INAPPLICABLE — this service has no MCP interface.
  Do not submit it there.
