# Payment Gateway (Deliverable 2)

Production-style mock payment gateway with asynchronous processing, webhook retries, embeddable checkout SDK, idempotent payment creation, and async refunds.

## Services

- API: http://localhost:8000
- Dashboard: http://localhost:3000
- Checkout + SDK: http://localhost:3001
- Redis: `redis://localhost:6379`
- Postgres: `postgresql://gateway_user:gateway_pass@localhost:5432/payment_gateway`

## Run

```bash
docker-compose up --build
```

## Test merchant credentials

- API Key: `key_test_abc123`
- API Secret: `secret_test_xyz789`
- Webhook secret: `whsec_test_abc123`

## Core features implemented

- Async payment processing with Redis + RQ worker (`pending -> success/failed`)
- Webhook delivery with HMAC-SHA256 signature and retry backoff (up to 5 attempts)
- Test retry intervals via `WEBHOOK_RETRY_INTERVALS_TEST=true`
- Refund API (full/partial), queued async processing
- Idempotency for `POST /api/v1/payments` (24h key expiry)
- Capture API (`POST /api/v1/payments/{payment_id}/capture`)
- Webhook logs listing and manual retry endpoint
- Queue status test endpoint: `GET /api/v1/test/jobs/status`
- Embeddable SDK at `http://localhost:3001/checkout.js`

## Main endpoints

- `POST /api/v1/orders`
- `POST /api/v1/payments`
- `POST /api/v1/payments/{payment_id}/capture`
- `POST /api/v1/payments/{payment_id}/refunds`
- `GET /api/v1/refunds/{refund_id}`
- `GET /api/v1/webhooks?limit=10&offset=0`
- `POST /api/v1/webhooks/{webhook_id}/retry`
- `GET /api/v1/test/jobs/status`

## Dashboard pages

- `/dashboard/webhooks`
- `/dashboard/docs`

## SDK usage

```html
<script src="http://localhost:3001/checkout.js"></script>
<script>
const checkout = new PaymentGateway({
  key: 'key_test_abc123',
  orderId: 'order_xyz',
  onSuccess: function (response) {
    console.log(response.paymentId);
  }
});
checkout.open();
</script>
```
