from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import csv,io,urllib.parse
from fastapi import FastAPI,Depends,Form,Request,UploadFile,File,HTTPException
from fastapi.responses import HTMLResponse,RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from openai import OpenAI
from .config import settings
from .db import Base,engine,get_db
from .models import Invoice
Base.metadata.create_all(bind=engine)
app=FastAPI(title=settings.app_name)
templates=Jinja2Templates(directory="app/templates")
def money(v): return f"₹{Decimal(v):,.2f}"
def build_message(i,tone="friendly"):
 d=i.due_date.strftime("%d %b %Y")
 if tone=="firm": return f"Hello {i.customer_name}, this is a payment follow-up for invoice {i.invoice_number} of ₹{i.balance:,.2f}. The due date was {d} and the balance is still outstanding. Please arrange payment and share the confirmation. Thank you."
 if tone=="final": return f"Hello {i.customer_name}, invoice {i.invoice_number} for ₹{i.balance:,.2f} is still outstanding after the due date of {d}. Please clear the balance at the earliest and confirm once paid. If there is any issue with the invoice, please let us know today."
 return f"Hello {i.customer_name}, a quick reminder regarding invoice {i.invoice_number} for ₹{i.balance:,.2f}, due on {d}. Please let us know when we can expect the payment. Thank you."
def ai_message(i,tone):
 if not settings.openai_api_key: return None
 try:
  client=OpenAI(api_key=settings.openai_api_key)
  p=f"Write a concise WhatsApp payment reminder for an Indian B2B customer. Tone: {tone}. Customer: {i.customer_name}. Invoice: {i.invoice_number}. Outstanding: ₹{i.balance:,.2f}. Due: {i.due_date.strftime('%d %b %Y')}. Days overdue: {i.days_overdue}. Do not threaten or make legal claims. 55 words max."
  r=client.chat.completions.create(model=settings.openai_model,messages=[{"role":"user","content":p}],temperature=.4)
  return r.choices[0].message.content.strip()
 except Exception: return None
@app.get("/health")
def health(): return {"status":"ok","app":settings.app_name}
@app.get("/",response_class=HTMLResponse)
def dashboard(request:Request,db:Session=Depends(get_db)):
 inv=list(db.scalars(select(Invoice).order_by(Invoice.due_date.asc())).all())
 out=sum((i.balance for i in inv),Decimal("0")); overdue=sum((i.balance for i in inv if i.days_overdue>0),Decimal("0")); today=sum((i.balance for i in inv if i.balance>0 and i.due_date==date.today()),Decimal("0")); customers=len({i.customer_name for i in inv})
 return templates.TemplateResponse("dashboard.html",{"request":request,"invoices":inv,"outstanding":out,"overdue":overdue,"due_today":today,"customers":customers,"money":money})
@app.post("/demo/seed")
def seed_demo(db: Session = Depends(get_db)):
    today = date.today()
    samples = [
        {"invoice_number":"DEMO-001","customer_name":"Apex Industrial Supplies","phone":"919876543210","email":"accounts@apex.example","amount":"84000","paid_amount":"0","days_due":-3},
        {"invoice_number":"DEMO-002","customer_name":"Northstar Traders","phone":"919812345678","email":"finance@northstar.example","amount":"31500","paid_amount":"5000","days_due":-12},
        {"invoice_number":"DEMO-003","customer_name":"Himalaya Components","phone":"919998877665","email":"accounts@himalaya.example","amount":"125000","paid_amount":"25000","days_due":-45},
        {"invoice_number":"DEMO-004","customer_name":"Greenline Services","phone":"919900112233","email":"billing@greenline.example","amount":"18000","paid_amount":"0","days_due":0},
        {"invoice_number":"DEMO-005","customer_name":"Cedar & Co.","phone":"919811223344","email":"accounts@cedar.example","amount":"46500","paid_amount":"20000","days_due":-6},
        {"invoice_number":"DEMO-006","customer_name":"Summit Retail Network","phone":"919887766554","email":"finance@summit.example","amount":"22000","paid_amount":"0","days_due":2},
    ]
    added = 0
    for row in samples:
        if db.scalar(select(Invoice).where(Invoice.invoice_number == row["invoice_number"])):
            continue
        amount = Decimal(row["amount"])
        paid = Decimal(row["paid_amount"])
        due = today + timedelta(days=row["days_due"])
        issue = due - timedelta(days=30)
        db.add(Invoice(
            invoice_number=row["invoice_number"],
            customer_name=row["customer_name"],
            phone=row["phone"],
            email=row["email"],
            amount=amount,
            paid_amount=paid,
            issue_date=issue,
            due_date=due,
            status="paid" if paid >= amount else "partially_paid" if paid > 0 else "unpaid",
            notes="Demo data — safe to delete before production use.",
        ))
        added += 1
    db.commit()
    return RedirectResponse(f"/?demo_added={added}", status_code=303)

@app.get("/invoices/new",response_class=HTMLResponse)
def new_invoice(request:Request): return templates.TemplateResponse("invoice_form.html",{"request":request,"today":date.today()})
@app.post("/invoices/new")
def create_invoice(invoice_number:str=Form(...),customer_name:str=Form(...),phone:str=Form(""),email:str=Form(""),amount:str=Form(...),paid_amount:str=Form("0"),issue_date:str=Form(...),due_date:str=Form(...),db:Session=Depends(get_db)):
 try: amount_d,paid_d=Decimal(amount),Decimal(paid_amount)
 except InvalidOperation: raise HTTPException(400,"Invalid amount")
 if db.scalar(select(Invoice).where(Invoice.invoice_number==invoice_number.strip())): raise HTTPException(400,"Invoice number already exists")
 i=Invoice(invoice_number=invoice_number.strip(),customer_name=customer_name.strip(),phone=phone.strip() or None,email=email.strip() or None,amount=amount_d,paid_amount=paid_d,issue_date=date.fromisoformat(issue_date),due_date=date.fromisoformat(due_date),status="paid" if paid_d>=amount_d else "partially_paid" if paid_d>0 else "unpaid")
 db.add(i);db.commit();return RedirectResponse("/",303)
@app.post("/invoices/import")
async def import_csv(file:UploadFile=File(...),db:Session=Depends(get_db)):
 if not file.filename.lower().endswith(".csv"): raise HTTPException(400,"Upload a CSV file")
 reader=csv.DictReader(io.StringIO((await file.read()).decode("utf-8-sig"))); req={"invoice_number","customer_name","amount","issue_date","due_date"}
 if not req.issubset(set(reader.fieldnames or [])): raise HTTPException(400,"CSV missing required columns")
 for row in reader:
  if db.scalar(select(Invoice).where(Invoice.invoice_number==row["invoice_number"].strip())): continue
  paid=Decimal((row.get("paid_amount") or "0").strip()); amount=Decimal(row["amount"].strip()); db.add(Invoice(invoice_number=row["invoice_number"].strip(),customer_name=row["customer_name"].strip(),phone=(row.get("phone") or "").strip() or None,email=(row.get("email") or "").strip() or None,amount=amount,paid_amount=paid,issue_date=date.fromisoformat(row["issue_date"].strip()),due_date=date.fromisoformat(row["due_date"].strip()),status="paid" if paid>=amount else "partially_paid" if paid>0 else "unpaid"))
 db.commit();return RedirectResponse("/",303)
@app.get("/invoices/{invoice_id}",response_class=HTMLResponse)
def invoice_detail(invoice_id:int,request:Request,tone:str="friendly",db:Session=Depends(get_db)):
 i=db.get(Invoice,invoice_id)
 if not i: raise HTTPException(404,"Invoice not found")
 msg=ai_message(i,tone) or build_message(i,tone); digits="".join(ch for ch in (i.phone or "") if ch.isdigit()); wa=f"https://wa.me/{digits}?text={urllib.parse.quote(msg)}" if digits else None
 return templates.TemplateResponse("invoice_detail.html",{"request":request,"invoice":i,"message":msg,"tone":tone,"wa_url":wa,"money":money})
@app.post("/invoices/{invoice_id}/payment")
def payment(invoice_id:int,db:Session=Depends(get_db)):
 if not db.get(Invoice,invoice_id): raise HTTPException(404,"Invoice not found")
 return RedirectResponse(f"/invoices/{invoice_id}?payment=demo",303)
@app.post("/invoices/{invoice_id}/mark-paid")
def paid(invoice_id:int,db:Session=Depends(get_db)):
 i=db.get(Invoice,invoice_id)
 if not i: raise HTTPException(404,"Invoice not found")
 i.paid_amount=i.amount;i.status="paid";db.commit();return RedirectResponse(f"/invoices/{invoice_id}",303)
