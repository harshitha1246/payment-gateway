import uuid
from sqlalchemy import (
    Column,
    String,
    Integer,
    Boolean,
    Text,
    DateTime,
    ForeignKey,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class Merchant(Base):
    __tablename__ = 'merchants'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False, unique=True)
    api_key = Column(String(64), nullable=False, unique=True)
    api_secret = Column(String(64), nullable=False)
    webhook_url = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

class Order(Base):
    __tablename__ = 'orders'
    id = Column(String(64), primary_key=True)
    merchant_id = Column(UUID(as_uuid=True), ForeignKey('merchants.id'), nullable=False)
    amount = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default='INR')
    receipt = Column(String(255), nullable=True)
    notes = Column(JSONB, nullable=True)
    status = Column(String(20), nullable=False, default='created')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

Index('ix_orders_merchant_id', Order.merchant_id)

class Payment(Base):
    __tablename__ = 'payments'
    id = Column(String(64), primary_key=True)
    order_id = Column(String(64), ForeignKey('orders.id'), nullable=False)
    merchant_id = Column(UUID(as_uuid=True), ForeignKey('merchants.id'), nullable=False)
    amount = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default='INR')
    method = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default='processing')
    vpa = Column(String(255), nullable=True)
    card_network = Column(String(20), nullable=True)
    card_last4 = Column(String(4), nullable=True)
    error_code = Column(String(50), nullable=True)
    error_description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

Index('ix_payments_order_id', Payment.order_id)
Index('ix_payments_status', Payment.status)
