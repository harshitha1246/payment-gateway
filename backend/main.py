import os
import re
import time
import uuid
import random
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from fastapi.middleware.cors import CORSMiddleware

from database import SessionLocal, engine
from models import Base, Merchant, Order, Payment

# Create tables
Base.metadata.create_all(bind=engine)

# Seed test merchant
TEST_MERCHANT_ID = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
TEST_MERCHANT_EMAIL = os.getenv("TEST_MERCHANT_EMAIL", "test@example.com")
TEST_API_KEY = os.getenv("TEST_API_KEY", "key_test_abc123")
TEST_API_SECRET = os.getenv("TEST_API_SECRET", "secret_test_xyz789")

def seed_test_merchant():
    db = SessionLocal()
    try:
        m = db.query(Merchant).filter(Merchant.email == TEST_MERCHANT_EMAIL).first()
        if m:
            return
        merchant = Merchant(
            id=TEST_MERCHANT_ID,
            name="Test Merchant",
            email=TEST_MERCHANT_EMAIL,
            api_key=TEST_API_KEY,
            api_secret=TEST_API_SECRET,
        )
        db.add(merchant)
        db.commit()
    finally:
        db.close()

seed_test_merchant()

app = FastAPI()

# Allow dashboard and checkout frontends to call the API from the browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Helpers
def iso_now():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def auth_error():
    return JSONResponse(status_code=401, content={"error": {"code": "AUTHENTICATION_ERROR", "description": "Invalid API credentials"}})

def get_merchant_from_headers(db: Session, api_key: Optional[str], api_secret: Optional[str]):
    if not api_key or not api_secret:
        return None
    m = db.query(Merchant).filter(Merchant.api_key == api_key).first()
    if not m or m.api_secret != api_secret:
        return None
    return m

ALNUM = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

def gen_id(prefix: str):
    return prefix + '_' + ''.join(random.choice(ALNUM) for _ in range(16))

# Validation functions
VPA_RE = re.compile(r'^[a-zA-Z0-9._-]+@[a-zA-Z0-9]+$')

def validate_vpa(vpa: str) -> bool:
    return bool(VPA_RE.match(vpa))

def luhn_check(card_number: str) -> bool:
    s = re.sub(r'[\s-]', '', card_number)
    if not s.isdigit() or not (13 <= len(s) <= 19):
        return False
    total = 0
    reverse_digits = s[::-1]
    for i, ch in enumerate(reverse_digits):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0

def detect_card_network(card_number: str) -> str:
    s = re.sub(r'[\s-]', '', card_number)
    if s.startswith('4'):
        return 'visa'
    if any(s.startswith(str(x)) for x in range(51, 56)):
        return 'mastercard'
    if s.startswith('34') or s.startswith('37'):
        return 'amex'
    if s.startswith('60') or s.startswith('65') or any(s.startswith(str(x)) for x in range(81, 90)):
        return 'rupay'
    return 'unknown'

def validate_expiry(month: str, year: str) -> bool:
    try:
        m = int(month)
        if not (1 <= m <= 12):
            return False
        y = int(year)
        if len(year) == 2:
            y += 2000
        now = datetime.utcnow()
        # expire at end of month, so valid if year>now.year or same year and month >= now.month
        if y > now.year:
            return True
        if y == now.year and m >= now.month:
            return True
        return False
    except Exception:
        return False

# Env-driven test mode
TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'
TEST_PAYMENT_SUCCESS = os.getenv('TEST_PAYMENT_SUCCESS', 'true').lower() == 'true'
TEST_PROCESSING_DELAY = int(os.getenv('TEST_PROCESSING_DELAY', '1000'))
UPI_SUCCESS_RATE = float(os.getenv('UPI_SUCCESS_RATE', '0.90'))
CARD_SUCCESS_RATE = float(os.getenv('CARD_SUCCESS_RATE', '0.95'))
PROCESSING_DELAY_MIN = int(os.getenv('PROCESSING_DELAY_MIN', '5000'))
PROCESSING_DELAY_MAX = int(os.getenv('PROCESSING_DELAY_MAX', '10000'))

# Pydantic models
class CreateOrderReq(BaseModel):
    amount: int
    currency: Optional[str] = 'INR'
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

@app.get('/health')
def health():
    db_status = 'disconnected'
    try:
        db = SessionLocal()
        db.execute('SELECT 1')
        db_status = 'connected'
    except Exception:
        db_status = 'disconnected'
    finally:
        try:
            db.close()
        except Exception:
            pass
    return {"status": "healthy", "database": db_status, "timestamp": iso_now()}

@app.post('/api/v1/orders')
def create_order(req: CreateOrderReq, x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()
        if not isinstance(req.amount, int) or req.amount < 100:
            return JSONResponse(status_code=400, content={"error": {"code": "BAD_REQUEST_ERROR", "description": "amount must be at least 100"}})
        # generate unique id
        for _ in range(5):
            oid = gen_id('order')
            exists = db.query(Order).filter(Order.id == oid).first()
            if not exists:
                break
        order = Order(id=oid, merchant_id=merchant.id, amount=req.amount, currency=req.currency or 'INR', receipt=req.receipt, notes=req.notes, status='created')
        db.add(order)
        db.commit()
        db.refresh(order)
        return JSONResponse(status_code=201, content={
            "id": order.id,
            "merchant_id": str(order.merchant_id),
            "amount": order.amount,
            "currency": order.currency,
            "receipt": order.receipt,
            "notes": order.notes or {},
            "status": order.status,
            "created_at": order.created_at.replace(microsecond=0).isoformat() + 'Z'
        })
    finally:
        db.close()

@app.get('/api/v1/orders/{order_id}')
def get_order(order_id: str, x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND_ERROR", "description": "Order not found"}})
        if str(order.merchant_id) != str(merchant.id):
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND_ERROR", "description": "Order not found"}})
        return {
            "id": order.id,
            "merchant_id": str(order.merchant_id),
            "amount": order.amount,
            "currency": order.currency,
            "receipt": order.receipt,
            "notes": order.notes or {},
            "status": order.status,
            "created_at": order.created_at.replace(microsecond=0).isoformat() + 'Z',
            "updated_at": order.updated_at.replace(microsecond=0).isoformat() + 'Z'
        }
    finally:
        db.close()

@app.post('/api/v1/payments')
def create_payment(req: CreatePaymentReq, x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()
        order = db.query(Order).filter(Order.id == req.order_id).first()
        if not order or str(order.merchant_id) != str(merchant.id):
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND_ERROR", "description": "Order not found"}})
        method = req.method.lower()
        if method not in ('upi', 'card'):
            return JSONResponse(status_code=400, content={"error": {"code": "BAD_REQUEST_ERROR", "description": "Unsupported payment method"}})
        # Validate method specific
        vpa = None
        card_network = None
        card_last4 = None
        if method == 'upi':
            if not req.vpa or not validate_vpa(req.vpa):
                return JSONResponse(status_code=400, content={"error": {"code": "INVALID_VPA", "description": "VPA format invalid"}})
            vpa = req.vpa
        else:
            if not req.card:
                return JSONResponse(status_code=400, content={"error": {"code": "INVALID_CARD", "description": "Card data missing"}})
            card = req.card
            if not luhn_check(card.number):
                return JSONResponse(status_code=400, content={"error": {"code": "INVALID_CARD", "description": "Card validation failed"}})
            if not validate_expiry(card.expiry_month, card.expiry_year):
                return JSONResponse(status_code=400, content={"error": {"code": "EXPIRED_CARD", "description": "Card expiry date invalid"}})
            card_network = detect_card_network(card.number)
            card_last4 = re.sub(r'[\s-]', '', card.number)[-4:]
        # create payment with processing status
        for _ in range(5):
            pid = gen_id('pay')
            if not db.query(Payment).filter(Payment.id == pid).first():
                break
        payment = Payment(id=pid, order_id=order.id, merchant_id=merchant.id, amount=order.amount, currency=order.currency, method=method, status='processing', vpa=vpa, card_network=card_network, card_last4=card_last4)
        db.add(payment)
        db.commit()
        db.refresh(payment)
        # process synchronously
        if TEST_MODE:
            delay_ms = TEST_PROCESSING_DELAY
            success = TEST_PAYMENT_SUCCESS
        else:
            delay_ms = random.randint(PROCESSING_DELAY_MIN, PROCESSING_DELAY_MAX)
            success = random.random() < (UPI_SUCCESS_RATE if method == 'upi' else CARD_SUCCESS_RATE)
        time.sleep(delay_ms / 1000.0)
        if success:
            payment.status = 'success'
            payment.error_code = None
            payment.error_description = None
        else:
            payment.status = 'failed'
            payment.error_code = 'PAYMENT_FAILED'
            payment.error_description = 'Simulated payment gateway failure'
        db.add(payment)
        db.commit()
        db.refresh(payment)
        resp = {
            "id": payment.id,
            "order_id": payment.order_id,
            "amount": payment.amount,
            "currency": payment.currency,
            "method": payment.method,
            "status": payment.status,
            "created_at": payment.created_at.replace(microsecond=0).isoformat() + 'Z'
        }
        if method == 'upi':
            resp['vpa'] = payment.vpa
        else:
            resp['card_network'] = payment.card_network or 'unknown'
            resp['card_last4'] = payment.card_last4
        return JSONResponse(status_code=201, content=resp)
    finally:
        db.close()

@app.get('/api/v1/payments/{payment_id}')
def get_payment(payment_id: str, x_api_key: Optional[str] = Header(None), x_api_secret: Optional[str] = Header(None)):
    db = SessionLocal()
    try:
        merchant = get_merchant_from_headers(db, x_api_key, x_api_secret)
        if not merchant:
            return auth_error()
        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND_ERROR", "description": "Payment not found"}})
        if str(payment.merchant_id) != str(merchant.id):
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND_ERROR", "description": "Payment not found"}})
        out = payment_to_dict(payment)
        return out
    finally:
        db.close()

def payment_to_dict(p: Payment):
    d = {
        "id": p.id,
        "order_id": p.order_id,
        "amount": p.amount,
        "currency": p.currency,
        "method": p.method,
        "status": p.status,
        "created_at": p.created_at.replace(microsecond=0).isoformat() + 'Z',
        "updated_at": p.updated_at.replace(microsecond=0).isoformat() + 'Z'
    }
    if p.method == 'upi':
        d['vpa'] = p.vpa
    else:
        d['card_network'] = p.card_network
        d['card_last4'] = p.card_last4
    if p.error_code:
        d['error_code'] = p.error_code
        d['error_description'] = p.error_description
    return d

# Public endpoints for checkout
@app.get('/api/v1/orders/{order_id}/public')
def public_get_order(order_id: str):
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND_ERROR", "description": "Order not found"}})
        return {"id": order.id, "amount": order.amount, "currency": order.currency, "status": order.status}
    finally:
        db.close()

@app.post('/api/v1/payments/public')
def public_create_payment(req: CreatePaymentReq):
    # No auth; validate order exists
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == req.order_id).first()
        if not order:
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND_ERROR", "description": "Order not found"}})
        merchant = db.query(Merchant).filter(Merchant.id == order.merchant_id).first()
        # Reuse create logic but without auth headers
        # Validate and create
        method = req.method.lower()
        if method not in ('upi', 'card'):
            return JSONResponse(status_code=400, content={"error": {"code": "BAD_REQUEST_ERROR", "description": "Unsupported payment method"}})
        vpa = None
        card_network = None
        card_last4 = None
        if method == 'upi':
            if not req.vpa or not validate_vpa(req.vpa):
                return JSONResponse(status_code=400, content={"error": {"code": "INVALID_VPA", "description": "VPA format invalid"}})
            vpa = req.vpa
        else:
            if not req.card:
                return JSONResponse(status_code=400, content={"error": {"code": "INVALID_CARD", "description": "Card data missing"}})
            card = req.card
            if not luhn_check(card.number):
                return JSONResponse(status_code=400, content={"error": {"code": "INVALID_CARD", "description": "Card validation failed"}})
            if not validate_expiry(card.expiry_month, card.expiry_year):
                return JSONResponse(status_code=400, content={"error": {"code": "EXPIRED_CARD", "description": "Card expiry date invalid"}})
            card_network = detect_card_network(card.number)
            card_last4 = re.sub(r'[\s-]', '', card.number)[-4:]
        for _ in range(5):
            pid = gen_id('pay')
            if not db.query(Payment).filter(Payment.id == pid).first():
                break
        payment = Payment(id=pid, order_id=order.id, merchant_id=order.merchant_id, amount=order.amount, currency=order.currency, method=method, status='processing', vpa=vpa, card_network=card_network, card_last4=card_last4)
        db.add(payment)
        db.commit()
        db.refresh(payment)
        if TEST_MODE:
            delay_ms = TEST_PROCESSING_DELAY
            success = TEST_PAYMENT_SUCCESS
        else:
            delay_ms = random.randint(PROCESSING_DELAY_MIN, PROCESSING_DELAY_MAX)
            success = random.random() < (UPI_SUCCESS_RATE if method == 'upi' else CARD_SUCCESS_RATE)
        time.sleep(delay_ms / 1000.0)
        if success:
            payment.status = 'success'
            payment.error_code = None
            payment.error_description = None
        else:
            payment.status = 'failed'
            payment.error_code = 'PAYMENT_FAILED'
            payment.error_description = 'Simulated payment gateway failure'
        db.add(payment)
        db.commit()
        db.refresh(payment)
        resp = {
            "id": payment.id,
            "order_id": payment.order_id,
            "amount": payment.amount,
            "currency": payment.currency,
            "method": payment.method,
            "status": payment.status,
            "created_at": payment.created_at.replace(microsecond=0).isoformat() + 'Z'
        }
        if method == 'upi':
            resp['vpa'] = payment.vpa
        else:
            resp['card_network'] = payment.card_network or 'unknown'
            resp['card_last4'] = payment.card_last4
        return JSONResponse(status_code=201, content=resp)
    finally:
        db.close()

@app.get('/api/v1/test/merchant')
def test_merchant():
    db = SessionLocal()
    try:
        m = db.query(Merchant).filter(Merchant.email == TEST_MERCHANT_EMAIL).first()
        if not m:
            return JSONResponse(status_code=404, content={})
        return {"id": str(m.id), "email": m.email, "api_key": m.api_key, "seeded": True}
    finally:
        db.close()

# Additional helper: list payments by merchant for dashboard
@app.get('/api/v1/payments')
def list_payments(merchant_id: Optional[str] = None):
    db = SessionLocal()
    try:
        q = db.query(Payment)
        if merchant_id:
            q = q.filter(Payment.merchant_id == merchant_id)
        payments = q.order_by(Payment.created_at.desc()).limit(100).all()
        return [payment_to_dict(p) for p in payments]
    finally:
        db.close()
