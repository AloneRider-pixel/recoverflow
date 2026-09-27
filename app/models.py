from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    company_name: Mapped[str] = mapped_column(String(180), default="My Business")
    razorpay_key_id_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    razorpay_key_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    razorpay_webhook_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    whatsapp_access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    whatsapp_phone_number_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    whatsapp_template_name: Mapped[str] = mapped_column(String(120), default="invoice_payment_reminder")
    whatsapp_template_language: Mapped[str] = mapped_column(String(30), default="en")
    subscription_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    subscription_status: Mapped[str] = mapped_column(String(40), default="trial")
    subscription_plan: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

class BillingPlan(Base):
    __tablename__ = "billing_plans"
    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    amount_paise: Mapped[int] = mapped_column(Integer)
    period: Mapped[str] = mapped_column(String(20), default="monthly")
    interval: Mapped[int] = mapped_column(Integer, default=1)
    razorpay_plan_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    invoice_number: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(180), index=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email: Mapped[str | None] = mapped_column(String(180), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14,2))
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(14,2), default=0)
    issue_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(30), default="unpaid", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(30), nullable=True)
    place_of_supply: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(6,2), nullable=True)
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(14,2), nullable=True)
    tds_amount: Mapped[Decimal | None] = mapped_column(Numeric(14,2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    @property
    def balance(self): return max(Decimal("0"), self.amount - self.paid_amount)
    @property
    def days_overdue(self): return max((date.today() - self.due_date).days, 0) if self.balance > 0 else 0
    @property
    def aging_bucket(self):
        d=self.days_overdue
        return "Current" if d<=0 else "1–7 days" if d<=7 else "8–30 days" if d<=30 else "30+ days"

class PaymentLink(Base):
    __tablename__ = "payment_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    provider_link_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    short_url: Mapped[str] = mapped_column(String(500))
    amount_paise: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

class ReminderLog(Base):
    __tablename__ = "reminder_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    stage: Mapped[str] = mapped_column(String(30))
    channel: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    provider_message_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    __table_args__ = (UniqueConstraint("invoice_id","stage","channel",name="uq_reminder_invoice_stage_channel"),)

class DeviceToken(Base):
    __tablename__ = "device_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(20), default="unknown")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
