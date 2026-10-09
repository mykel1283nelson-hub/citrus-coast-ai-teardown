# Citrus Coast AI — Free AI-Visibility Teardown (Model B)

**Live:** https://citrus-coast-ai-teardown.onrender.com

Is your business invisible to AI? Enter any business URL and get a scored report on how visible it is to AI assistants (ChatGPT, Claude, Gemini) — 11 deterministic checks covering DNS, HTTPS, meta tags, mobile viewport, tap-to-call, contact paths, and more. Free, 3 reports/day, no signup.

This is the lead magnet for [Citrus Coast AI](https://buy.stripe.com/3cI00l6IvdZufAUgU2eUU00) ($499/mo AI-employee service for local businesses). The tool is the magnet; the subscription is the close.

## For AI agents (x402)

`POST /api/teardown` is gated by the [x402 protocol](https://x402.org): no API keys, no accounts. Hit the endpoint, receive a `402 Payment Required`, pay **$0.50 USDC on Base**, and get the teardown report in the same round-trip.

- **Endpoint:** `POST https://citrus-coast-ai-teardown.onrender.com/api/teardown`
- **Body:** `{"url": "https://example-business.com"}`
- **Price:** $0.50 USDC (Base, `eip155:8453`)
- **Discovery:** [`/.well-known/x402`](https://citrus-coast-ai-teardown.onrender.com/.well-known/x402) · [`/skill.md`](https://citrus-coast-ai-teardown.onrender.com/skill.md) · [`/llms.txt`](https://citrus-coast-ai-teardown.onrender.com/llms.txt)

## Why deterministic

The teardown engine runs 11 fixed checks — zero LLM inference per call. Marginal cost per report is $0.00, so the economics are clean at any volume.

## Stack

FastAPI + uvicorn, deployed on Render (free tier). Python 3.12.

## Who runs this

Citrus Coast AI — an AI-employee-for-hire operation. Faceless by design; the work speaks.
