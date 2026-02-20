import os
import random
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import SessionLocal, engine
from models import Base, IdempotencyKey, Merchant, Order, Payment, Refund, WebhookLog
from queue_jobs import (
    build_event_payload,
    enqueue_process_payment,
    enqueue_process_refund,
    get_job_queue_status,
    get_queue,
)

Base.metadata.create_all(bind=engine)

TEST_MERCHANT_ID = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_MERCHANT_EMAIL = os.getenv("TEST_MERCHANT_EMAIL", "test@example.com")
TEST_API_KEY = os.getenv("TEST_API_KEY", "key_test_abc123")
TEST_API_SECRET = os.getenv("TEST_API_SECRET", "secret_test_xyz789")
TEST_WEBHOOK_SECRET = os.getenv("TEST_WEBHOOK_SECRET", "whsec_test_abc123")

ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
VPA_RE = re.compile(r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9]+$")


class CreateOrderReq(BaseModel):
    amount: int
    currency: Optional[str] = "INR"
    receipt: Optional[str] = None
    notes: Optional[dict] = None


class CreatePaymentCardInfo(BaseModel):
    number: str
    expiry_month: str
    expiry_year: str
    cvv: str
    holder_name: str


class CreatePaymentReq(BaseModel):
    order_id: str
    method: str
    vpa: Optional[str] = None
    card: Optional[CreatePaymentCardInfo] = None


class CapturePaymentReq(BaseModel):
    amount: int


class CreateRefundReq(BaseModel):
    amount: int
    reason: Optional[str] = None


class UpdateWebhookConfigReq(BaseModel):
    webhook_url: Optional[str] = None


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + "Z"


def is_not_expired(dt: datetime) -> bool:
    if dt.tzinfo is not None:
        return dt > datetime.now(dt.tzinfo)
    return dt > datetime.utcnow()


def auth_error():
    return JSONResponse(
        status_code=401,
        content={"error": {"code": "AUTHENTICATION_ERROR", "description": "Invalid API credentials"}},
    )


def bad_request(description: str):
    return JSONResponse(status_code=400, content={"error": {"code": "BAD_REQUEST_ERROR", "description": description}})


def not_found(description: str):
    return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND_ERROR", "description": description}})


def get_merchant_from_headers(db: Session, api_key: Optional[str], api_secret: Optional[str]):
    if not api_key or not api_secret:
        return None
    merchant = db.query(Merchant).filter(Merchant.api_key == api_key).first()
    if not merchant or merchant.api_secret != api_secret:
        return None
    return merchant


def seed_test_merchant():
    db = SessionLocal()
    try:
        m = db.query(Merchant).filter(Merchant.email == TEST_MERCHANT_EMAIL).first()
        if m:
            if not m.webhook_secret:
                m.webhook_secret = TEST_WEBHOOK_SECRET
                db.add(m)
                db.commit()
            return
        merchant = Merchant(
            id=TEST_MERCHANT_ID,
            name="Test Merchant",
            email=TEST_MERCHANT_EMAIL,
            api_key=TEST_API_KEY,
            api_secret=TEST_API_SECRET,
            webhook_secret=TEST_WEBHOOK_SECRET,
        )
        db.add(merchant)
        db.commit()
    finally:
        db.close()


def gen_unique_id(db: Session, model, prefix: str):
    for _ in range(30):
        generated = prefix + "_" + "".join(random.choice(ALNUM) for _ in range(16))
        if not db.query(model).filter(model.id == generated).first():
            return generated
    raise RuntimeError("Could not generate unique ID")


def validate_vpa(vpa: str) -> bool:
    return bool(VPA_RE.match(vpa))


def luhn_check(card_number: str) -> bool:
    digits = re.sub(r"[\s-]", "", card_number)
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    for index, ch in enumerate(digits[::-1]):
        value = int(ch)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def detect_card_network(card_number: str) -> str:
    digits = re.sub(r"[\s-]", "", card_number)
    if digits.startswith("4"):
        return "visa"
    if any(digits.startswith(str(x)) for x in range(51, 56)):
        return "mastercard"
    if digits.startswith("34") or digits.startswith("37"):
        return "amex"
    if digits.startswith("60") or digits.startswith("65") or any(digits.startswith(str(x)) for x in range(81, 90)):
        return "rupay"
    return "unknown"


def validate_expiry(month: str, year: str) -> bool:
    try:
        m = int(month)
        if m < 1 or m > 12:
            return False
        y = int(year)
        if len(year) == 2:
            y += 2000
        now = datetime.utcnow()
        return y > now.year or (y == now.year and m >= now.month)
    except Exception:
        return False


def payment_to_dict(payment: Payment):
    payload = {
        "id": payment.id,
        "order_id": payment.order_id,
        "amount": payment.amount,
        "currency": payment.currency,
        "method": payment.method,
        "status": payment.status,
        "captured": bool(payment.captured),
        "created_at": iso(payment.created_at),
        "updated_at": iso(payment.updated_at),
    }
    if payment.method == "upi":
        payload["vpa"] = payment.vpa
    else:
        payload["card_network"] = payment.card_network
        payload["card_last4"] = payment.card_last4
    if payment.error_code:
        payload["error_code"] = payment.error_code
        payload["error_description"] = payment.error_description
    return payload


def refund_to_dict(refund: Refund):
    return {
        "id": refund.id,
        "payment_id": refund.payment_id,
        "amount": refund.amount,
        "reason": refund.reason,
        "status": refund.status,
        "created_at": iso(refund.created_at),
        "processed_at": iso(refund.processed_at),
    }


def enqueue_webhook(db: Session, merchant_id, event: str, payload: dict):
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    if not merchant or not merchant.webhook_url:
        return
    log = WebhookLog(
        merchant_id=merchant_id,
        event=event,
        payload=payload,
        status="pending",
        attempts=0,
        next_retry_at=datetime.utcnow(),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    get_queue().enqueue("queue_jobs.deliver_webhook_job", str(log.id))


seed_test_merchant()


@app.get("/health")
def health():
    db_status = "disconnected"
    redis_status = "disconnected"

    db = None
    try:
        db = SessionLocal()
        db.execute("SELECT 1")
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    finally:
        if db:
            db.close()

    try:
        get_queue().connection.ping()
        redis_status = "connected"
    except Exception:
        redis_status = "disconnected"

    return {"status": "healthy", "database": db_status, "redis": redis_status, "timestamp": iso(datetime.utcnow())}


@app.post("/api/v1/orders")
def create_order(req: CreateOrderReq, x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()
        if not isinstance(req.amount, int) or req.amount < 100:
            return bad_request("amount must be at least 100")

        order_id = gen_unique_id(db, Order, "order")
        order = Order(
            id=order_id,
            merchant_id=merchant.id,
            amount=req.amount,
            currency=req.currency or "INR",
            receipt=req.receipt,
            notes=req.notes,
            status="created",
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        return JSONResponse(
            status_code=201,
            content={
                "id": order.id,
                "merchant_id": str(order.merchant_id),
                "amount": order.amount,
                "currency": order.currency,
                "receipt": order.receipt,
                "notes": order.notes or {},
                "status": order.status,
                "created_at": iso(order.created_at),
            },
        )
    finally:
        db.close()


@app.get("/api/v1/orders/{order_id}")
def get_order(order_id: str, x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()

        order = db.query(Order).filter(Order.id == order_id).first()
        if not order or str(order.merchant_id) != str(merchant.id):
            return not_found("Order not found")

        return {
            "id": order.id,
            "merchant_id": str(order.merchant_id),
            "amount": order.amount,
            "currency": order.currency,
            "receipt": order.receipt,
            "notes": order.notes or {},
            "status": order.status,
            "created_at": iso(order.created_at),
            "updated_at": iso(order.updated_at),
        }
    finally:
        db.close()


@app.get("/api/v1/orders/{order_id}/public")
def public_get_order(order_id: str):
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            return not_found("Order not found")
        return {"id": order.id, "amount": order.amount, "currency": order.currency, "status": order.status}
    finally:
        db.close()


def create_payment_internal(db: Session, req: CreatePaymentReq, merchant: Merchant):
    order = db.query(Order).filter(Order.id == req.order_id).first()
    if not order or str(order.merchant_id) != str(merchant.id):
        return None, not_found("Order not found")

    method = req.method.lower()
    if method not in ("upi", "card"):
        return None, bad_request("Unsupported payment method")

    vpa = None
    card_network = None
    card_last4 = None
    if method == "upi":
        if not req.vpa or not validate_vpa(req.vpa):
            return None, JSONResponse(status_code=400, content={"error": {"code": "INVALID_VPA", "description": "VPA format invalid"}})
        vpa = req.vpa
    else:
        if not req.card:
            return None, JSONResponse(status_code=400, content={"error": {"code": "INVALID_CARD", "description": "Card data missing"}})
        if not luhn_check(req.card.number):
            return None, JSONResponse(status_code=400, content={"error": {"code": "INVALID_CARD", "description": "Card validation failed"}})
        if not validate_expiry(req.card.expiry_month, req.card.expiry_year):
            return None, JSONResponse(status_code=400, content={"error": {"code": "EXPIRED_CARD", "description": "Card expiry date invalid"}})
        card_network = detect_card_network(req.card.number)
        card_last4 = re.sub(r"[\s-]", "", req.card.number)[-4:]

    payment_id = gen_unique_id(db, Payment, "pay")
    payment = Payment(
        id=payment_id,
        order_id=order.id,
        merchant_id=merchant.id,
        amount=order.amount,
        currency=order.currency,
        method=method,
        status="pending",
        captured=False,
        vpa=vpa,
        card_network=card_network,
        card_last4=card_last4,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    enqueue_process_payment(payment.id)

    payment_created_payload = build_event_payload("payment.created", payment=payment)
    payment_pending_payload = build_event_payload("payment.pending", payment=payment)
    enqueue_webhook(db, payment.merchant_id, "payment.created", payment_created_payload)
    enqueue_webhook(db, payment.merchant_id, "payment.pending", payment_pending_payload)

    return payment, None


@app.post("/api/v1/payments")
def create_payment(
    req: CreatePaymentReq,
    x_api_key: Optional[str] = Header(None),
    x_api_secret: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()

        if idempotency_key:
            idem = (
                db.query(IdempotencyKey)
                .filter(IdempotencyKey.key == idempotency_key, IdempotencyKey.merchant_id == merchant.id)
                .first()
            )
            if idem:
                if is_not_expired(idem.expires_at):
                    return JSONResponse(status_code=201, content=idem.response)
                db.delete(idem)
                db.commit()

        payment, err = create_payment_internal(db, req, merchant)
        if err:
            return err

        response_body = {
            "id": payment.id,
            "order_id": payment.order_id,
            "amount": payment.amount,
            "currency": payment.currency,
            "method": payment.method,
            "status": payment.status,
            "created_at": iso(payment.created_at),
        }
        if payment.method == "upi":
            response_body["vpa"] = payment.vpa
        else:
            response_body["card_network"] = payment.card_network or "unknown"
            response_body["card_last4"] = payment.card_last4

        if idempotency_key:
            record = IdempotencyKey(
                key=idempotency_key,
                merchant_id=merchant.id,
                response=response_body,
                expires_at=datetime.utcnow() + timedelta(hours=24),
            )
            db.add(record)
            db.commit()

        return JSONResponse(status_code=201, content=response_body)
    finally:
        db.close()


@app.post("/api/v1/payments/public")
def public_create_payment(req: CreatePaymentReq):
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == req.order_id).first()
        if not order:
            return not_found("Order not found")
        merchant = db.query(Merchant).filter(Merchant.id == order.merchant_id).first()
        payment, err = create_payment_internal(db, req, merchant)
        if err:
            return err

        response_body = {
            "id": payment.id,
            "order_id": payment.order_id,
            "amount": payment.amount,
            "currency": payment.currency,
            "method": payment.method,
            "status": payment.status,
            "created_at": iso(payment.created_at),
        }
        if payment.method == "upi":
            response_body["vpa"] = payment.vpa
        else:
            response_body["card_network"] = payment.card_network or "unknown"
            response_body["card_last4"] = payment.card_last4

        return JSONResponse(status_code=201, content=response_body)
    finally:
        db.close()


@app.get("/api/v1/payments/{payment_id}")
def get_payment(payment_id: str, x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()

        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment or str(payment.merchant_id) != str(merchant.id):
            return not_found("Payment not found")
        return payment_to_dict(payment)
    finally:
        db.close()


@app.get("/api/v1/payments/public/{payment_id}")
def public_get_payment(payment_id: str):
    db = SessionLocal()
    try:
        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            return not_found("Payment not found")
        return payment_to_dict(payment)
    finally:
        db.close()


@app.post("/api/v1/payments/{payment_id}/capture")
def capture_payment(
    payment_id: str,
    req: CapturePaymentReq,
    x_api_key: Optional[str] = Header(None),
    x_api_secret: Optional[str] = Header(None),
):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()

        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment or str(payment.merchant_id) != str(merchant.id):
            return not_found("Payment not found")

        if payment.status != "success" or req.amount != payment.amount:
            return bad_request("Payment not in capturable state")

        payment.captured = True
        db.add(payment)
        db.commit()
        db.refresh(payment)
        return payment_to_dict(payment)
    finally:
        db.close()


@app.post("/api/v1/payments/{payment_id}/refunds")
def create_refund(
    payment_id: str,
    req: CreateRefundReq,
    x_api_key: Optional[str] = Header(None),
    x_api_secret: Optional[str] = Header(None),
):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()

        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment or str(payment.merchant_id) != str(merchant.id):
            return not_found("Payment not found")
        if payment.status != "success":
            return bad_request("Payment is not refundable")
        if not isinstance(req.amount, int) or req.amount <= 0:
            return bad_request("amount must be a positive integer")

        total_refunded_rows = (
            db.query(Refund.amount)
            .filter(Refund.payment_id == payment.id, Refund.status.in_(["processed", "pending"]))
            .all()
        )
        total_refunded_amount = sum(row[0] for row in total_refunded_rows)
        if req.amount > (payment.amount - total_refunded_amount):
            return bad_request("Refund amount exceeds available amount")

        refund_id = gen_unique_id(db, Refund, "rfnd")
        refund = Refund(
            id=refund_id,
            payment_id=payment.id,
            merchant_id=merchant.id,
            amount=req.amount,
            reason=req.reason,
            status="pending",
        )
        db.add(refund)
        db.commit()
        db.refresh(refund)

        enqueue_process_refund(refund.id)

        payload = build_event_payload("refund.created", refund=refund)
        enqueue_webhook(db, refund.merchant_id, "refund.created", payload)

        return JSONResponse(status_code=201, content=refund_to_dict(refund))
    finally:
        db.close()


@app.get("/api/v1/refunds/{refund_id}")
def get_refund(refund_id: str, x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()

        refund = db.query(Refund).filter(Refund.id == refund_id).first()
        if not refund or str(refund.merchant_id) != str(merchant.id):
            return not_found("Refund not found")
        return refund_to_dict(refund)
    finally:
        db.close()


@app.get("/api/v1/webhooks")
def list_webhook_logs(
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    x_api_key: Optional[str] = Header(None),
    x_api_secret: Optional[str] = Header(None),
):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()

        q = db.query(WebhookLog).filter(WebhookLog.merchant_id == merchant.id)
        total = q.count()
        rows = q.order_by(WebhookLog.created_at.desc()).offset(offset).limit(limit).all()

        return {
            "data": [
                {
                    "id": str(row.id),
                    "event": row.event,
                    "status": row.status,
                    "attempts": row.attempts,
                    "created_at": iso(row.created_at),
                    "last_attempt_at": iso(row.last_attempt_at),
                    "response_code": row.response_code,
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        db.close()


@app.post("/api/v1/webhooks/{webhook_id}/retry")
def retry_webhook(webhook_id: str, x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()

        log = db.query(WebhookLog).filter(WebhookLog.id == webhook_id).first()
        if not log or str(log.merchant_id) != str(merchant.id):
            return not_found("Webhook log not found")

        log.status = "pending"
        log.attempts = 0
        log.next_retry_at = datetime.utcnow()
        db.add(log)
        db.commit()

        get_queue().enqueue("queue_jobs.deliver_webhook_job", str(log.id))
        return {"id": str(log.id), "status": "pending", "message": "Webhook retry scheduled"}
    finally:
        db.close()


@app.get("/api/v1/merchant/webhook")
def get_webhook_config(x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()
        return {"webhook_url": merchant.webhook_url, "webhook_secret": merchant.webhook_secret}
    finally:
        db.close()


@app.put("/api/v1/merchant/webhook")
def update_webhook_config(
    req: UpdateWebhookConfigReq,
    x_api_key: Optional[str] = Header(None),
    x_api_secret: Optional[str] = Header(None),
):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()
        merchant.webhook_url = req.webhook_url
        if not merchant.webhook_secret:
            merchant.webhook_secret = "whsec_" + "".join(random.choice(ALNUM) for _ in range(16))
        db.add(merchant)
        db.commit()
        db.refresh(merchant)
        return {"webhook_url": merchant.webhook_url, "webhook_secret": merchant.webhook_secret}
    finally:
        db.close()


@app.post("/api/v1/merchant/webhook/regenerate-secret")
def regenerate_webhook_secret(x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()
        merchant.webhook_secret = "whsec_" + "".join(random.choice(ALNUM) for _ in range(16))
        db.add(merchant)
        db.commit()
        db.refresh(merchant)
        return {"webhook_secret": merchant.webhook_secret}
    finally:
        db.close()


@app.post("/api/v1/merchant/webhook/test")
def send_test_webhook(x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()

        payload = {"event": "payment.success", "timestamp": int(datetime.utcnow().timestamp()), "data": {"payment": {"id": "pay_test"}}}
        enqueue_webhook(db, merchant.id, "payment.success", payload)
        return {"status": "scheduled"}
    finally:
        db.close()


@app.get("/api/v1/test/jobs/status")
def test_jobs_status():
    try:
        return get_job_queue_status()
    except Exception:
        return {"pending": 0, "processing": 0, "completed": 0, "failed": 0, "worker_status": "stopped"}


@app.get("/api/v1/test/merchant")
def test_merchant():
    db = SessionLocal()
    try:
        merchant = db.query(Merchant).filter(Merchant.email == TEST_MERCHANT_EMAIL).first()
        if not merchant:
            return JSONResponse(status_code=404, content={})
        return {
            "id": str(merchant.id),
            "email": merchant.email,
            "api_key": merchant.api_key,
            "api_secret": merchant.api_secret,
            "webhook_url": merchant.webhook_url,
            "webhook_secret": merchant.webhook_secret,
            "seeded": True,
        }
    finally:
        db.close()


@app.get("/api/v1/payments")
def list_payments(merchant_id: Optional[str] = None):
    db = SessionLocal()
    try:
        q = db.query(Payment)
        if merchant_id:
            q = q.filter(Payment.merchant_id == merchant_id)
        return [payment_to_dict(row) for row in q.order_by(Payment.created_at.desc()).limit(200).all()]
    finally:
        db.close()
