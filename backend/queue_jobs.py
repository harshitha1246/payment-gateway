import hashlib
import hmac
import json
import os
import random
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

import requests
from redis import Redis
from rq import Queue, Worker
from rq.registry import FailedJobRegistry, FinishedJobRegistry, StartedJobRegistry
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Merchant, Payment, Refund, WebhookLog

QUEUE_NAME = os.getenv("QUEUE_NAME", "gateway_jobs")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
TEST_PAYMENT_SUCCESS = os.getenv("TEST_PAYMENT_SUCCESS", "true").lower() == "true"
TEST_PROCESSING_DELAY = int(os.getenv("TEST_PROCESSING_DELAY", "1000"))
WEBHOOK_RETRY_INTERVALS_TEST = os.getenv("WEBHOOK_RETRY_INTERVALS_TEST", "false").lower() == "true"

UPI_SUCCESS_RATE = float(os.getenv("UPI_SUCCESS_RATE", "0.90"))
CARD_SUCCESS_RATE = float(os.getenv("CARD_SUCCESS_RATE", "0.95"))
PROCESSING_DELAY_MIN = int(os.getenv("PROCESSING_DELAY_MIN", "5000"))
PROCESSING_DELAY_MAX = int(os.getenv("PROCESSING_DELAY_MAX", "10000"))
REFUND_DELAY_MIN = int(os.getenv("REFUND_DELAY_MIN", "3000"))
REFUND_DELAY_MAX = int(os.getenv("REFUND_DELAY_MAX", "5000"))

PROD_RETRY_SECONDS = [0, 60, 300, 1800, 7200]
TEST_RETRY_SECONDS = [0, 5, 10, 15, 20]


def utc_now() -> datetime:
    return datetime.utcnow()


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + "Z"


def get_redis_conn() -> Redis:
    return Redis.from_url(REDIS_URL)


def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=get_redis_conn(), default_timeout=120)


def generate_webhook_signature(payload_json: str, webhook_secret: str) -> str:
    return hmac.new(webhook_secret.encode("utf-8"), payload_json.encode("utf-8"), hashlib.sha256).hexdigest()


def get_retry_seconds_for_attempt(attempt_number: int) -> int:
    schedule = TEST_RETRY_SECONDS if WEBHOOK_RETRY_INTERVALS_TEST else PROD_RETRY_SECONDS
    if attempt_number < 1:
        return 0
    if attempt_number > len(schedule):
        return schedule[-1]
    return schedule[attempt_number - 1]


def payment_payload_dict(payment: Payment) -> Dict:
    data = {
        "id": payment.id,
        "order_id": payment.order_id,
        "amount": payment.amount,
        "currency": payment.currency,
        "method": payment.method,
        "status": payment.status,
        "created_at": to_iso(payment.created_at),
    }
    if payment.method == "upi":
        data["vpa"] = payment.vpa
    else:
        data["card_network"] = payment.card_network
        data["card_last4"] = payment.card_last4
    return data


def refund_payload_dict(refund: Refund) -> Dict:
    return {
        "id": refund.id,
        "payment_id": refund.payment_id,
        "amount": refund.amount,
        "reason": refund.reason,
        "status": refund.status,
        "created_at": to_iso(refund.created_at),
        "processed_at": to_iso(refund.processed_at),
    }


def build_event_payload(event: str, payment: Optional[Payment] = None, refund: Optional[Refund] = None) -> Dict:
    payload = {"event": event, "timestamp": int(time.time()), "data": {}}
    if payment is not None:
        payload["data"]["payment"] = payment_payload_dict(payment)
    if refund is not None:
        payload["data"]["refund"] = refund_payload_dict(refund)
    return payload


def enqueue_process_payment(payment_id: str):
    get_queue().enqueue("queue_jobs.process_payment_job", payment_id)


def enqueue_process_refund(refund_id: str):
    get_queue().enqueue("queue_jobs.process_refund_job", refund_id)


def enqueue_webhook_event(db: Session, merchant_id, event: str, payload: Dict) -> Optional[str]:
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    if not merchant or not merchant.webhook_url:
        return None

    log = WebhookLog(
        merchant_id=merchant_id,
        event=event,
        payload=payload,
        status="pending",
        attempts=0,
        next_retry_at=utc_now(),
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    get_queue().enqueue("queue_jobs.deliver_webhook_job", str(log.id))
    return str(log.id)


def process_payment_job(payment_id: str):
    db = SessionLocal()
    try:
        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            return

        if TEST_MODE:
            delay_ms = TEST_PROCESSING_DELAY
            success = TEST_PAYMENT_SUCCESS
        else:
            delay_ms = random.randint(PROCESSING_DELAY_MIN, PROCESSING_DELAY_MAX)
            success = random.random() < (UPI_SUCCESS_RATE if payment.method == "upi" else CARD_SUCCESS_RATE)

        time.sleep(delay_ms / 1000.0)

        if success:
            payment.status = "success"
            payment.error_code = None
            payment.error_description = None
            event = "payment.success"
        else:
            payment.status = "failed"
            payment.error_code = "PAYMENT_FAILED"
            payment.error_description = "Simulated payment gateway failure"
            event = "payment.failed"

        db.add(payment)
        db.commit()
        db.refresh(payment)

        payload = build_event_payload(event, payment=payment)
        enqueue_webhook_event(db, payment.merchant_id, event, payload)
    finally:
        db.close()


def deliver_webhook_job(webhook_id: str):
    db = SessionLocal()
    try:
        log = db.query(WebhookLog).filter(WebhookLog.id == webhook_id).first()
        if not log:
            return

        merchant = db.query(Merchant).filter(Merchant.id == log.merchant_id).first()
        if not merchant or not merchant.webhook_url or not merchant.webhook_secret:
            log.status = "failed"
            log.attempts = 5
            log.last_attempt_at = utc_now()
            log.response_body = "Merchant webhook configuration missing"
            log.next_retry_at = None
            db.add(log)
            db.commit()
            return

        payload_json = json.dumps(log.payload, separators=(",", ":"))
        signature = generate_webhook_signature(payload_json, merchant.webhook_secret)

        log.attempts = (log.attempts or 0) + 1
        log.last_attempt_at = utc_now()

        response_code = None
        response_body = None
        ok = False
        try:
            response = requests.post(
                merchant.webhook_url,
                data=payload_json,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Signature": signature,
                },
                timeout=5,
            )
            response_code = response.status_code
            response_body = (response.text or "")[:2000]
            ok = 200 <= response.status_code <= 299
        except Exception as ex:
            response_body = str(ex)[:2000]

        log.response_code = response_code
        log.response_body = response_body

        if ok:
            log.status = "success"
            log.next_retry_at = None
            db.add(log)
            db.commit()
            return

        if log.attempts >= 5:
            log.status = "failed"
            log.next_retry_at = None
            db.add(log)
            db.commit()
            return

        next_attempt = log.attempts + 1
        delay_seconds = get_retry_seconds_for_attempt(next_attempt)
        log.status = "pending"
        log.next_retry_at = utc_now() + timedelta(seconds=delay_seconds)
        db.add(log)
        db.commit()

        get_queue().enqueue_in(timedelta(seconds=delay_seconds), "queue_jobs.deliver_webhook_job", str(log.id))
    finally:
        db.close()


def process_refund_job(refund_id: str):
    db = SessionLocal()
    try:
        refund = db.query(Refund).filter(Refund.id == refund_id).first()
        if not refund:
            return

        payment = db.query(Payment).filter(Payment.id == refund.payment_id).first()
        if not payment or payment.status != "success":
            return

        total_refunded_rows = (
            db.query(Refund.amount)
            .filter(Refund.payment_id == payment.id, Refund.status.in_(["pending", "processed"]))
            .all()
        )
        refunded_amount = sum(row[0] for row in total_refunded_rows)
        if refunded_amount > payment.amount:
            return

        time.sleep(random.randint(REFUND_DELAY_MIN, REFUND_DELAY_MAX) / 1000.0)

        refund.status = "processed"
        refund.processed_at = utc_now()
        db.add(refund)
        db.commit()
        db.refresh(refund)

        payload = build_event_payload("refund.processed", refund=refund)
        enqueue_webhook_event(db, refund.merchant_id, "refund.processed", payload)
    finally:
        db.close()


def get_job_queue_status() -> Dict:
    queue = get_queue()
    started = StartedJobRegistry(queue=queue)
    failed = FailedJobRegistry(queue=queue)
    finished = FinishedJobRegistry(queue=queue)
    workers = Worker.all(connection=get_redis_conn())

    return {
        "pending": queue.count,
        "processing": len(started.get_job_ids()),
        "completed": len(finished.get_job_ids()),
        "failed": len(failed.get_job_ids()),
        "worker_status": "running" if len(workers) > 0 else "stopped",
    }
