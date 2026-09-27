from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Date, DateTime, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

class Invoice(Base):
    __tablename__="invoices"
    id: Mapped[int]=mapped_column(primary_key=True)
    invoice_number: Mapped[str]=mapped_column(String(80),unique=True,index=True)
    customer_name: Mapped[str]=mapped_column(String(180),index=True)
    phone: Mapped[str|None]=mapped_column(String(30),nullable=True)
    email: Mapped[str|None]=mapped_column(String(180),nullable=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(14,2))
    paid_amount: Mapped[Decimal]=mapped_column(Numeric(14,2),default=0)
    issue_date: Mapped[date]=mapped_column(Date)
    due_date: Mapped[date]=mapped_column(Date,index=True)
    status: Mapped[str]=mapped_column(String(30),default="unpaid",index=True)
    notes: Mapped[str|None]=mapped_column(Text,nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
    @property
    def balance(self): return max(Decimal("0"),self.amount-self.paid_amount)
    @property
    def days_overdue(self): return max((date.today()-self.due_date).days,0) if self.balance>0 else 0
    @property
    def aging_bucket(self):
        d=self.days_overdue
        return "Current" if d<=0 else "1–7 days" if d<=7 else "8–30 days" if d<=30 else "30+ days"
