from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import csv, io, json, urllib.parse
from fastapi import FastAPI, Depends, Form, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from openai import OpenAI
from .config import settings
from .db import get_db, init_db
from .models import User, Invoice, PaymentLink, ReminderLog, BillingPlan
from .security import set_session, clear_session, read_session, new_csrf, encrypt, decrypt, make_api_token, read_api_token
from .password import hash_password, verify_password
from .integrations import create_payment_link, send_whatsapp_template, verify_webhook, merchant_keys, webhook_secret, platform_request

# Initialize all application tables after model imports.
init_db()

app=FastAPI(title=settings.app_name)
templates=Jinja2Templates(directory="app/templates")

PLANS={
    "starter":{"name":"Starter","amount":99900,"description":"Core receivables and reminders"},
    "business":{"name":"Business","amount":249900,"description":"Automation for growing teams"},
    "pro":{"name":"Pro","amount":499900,"description":"Advanced collection operations"},
}

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
    except Exception:
        return None

def session_user(request, db):
    data=read_session(request)
    if not data or not data.get("user_id"):
        return None
    return db.get(User, int(data["user_id"]))

def require_user(request, db):
    user=session_user(request,db)
    if not user:
        raise HTTPException(status_code=303,headers={"Location":"/login"})
    return user

def csrf_for(request):
    data=read_session(request)
    return data.get("csrf") if data else ""

def check_csrf(request, token):
    expected=csrf_for(request)
    if not expected or not token or not hmac_compare(expected,token):
        raise HTTPException(403,"Invalid form token.")

def hmac_compare(a,b):
    import hmac
    return hmac.compare_digest(a,b)

@app.get("/health")
def health(): return {"status":"ok","app":settings.app_name}

@app.get("/welcome",response_class=HTMLResponse)
def welcome(request:Request):
    return templates.TemplateResponse("welcome.html",{"request":request,"plans":PLANS})

def api_user(request:Request, db:Session):
    header=request.headers.get("authorization","")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401,"Authentication required")
    payload=read_api_token(header[7:].strip())
    if not payload or not payload.get("user_id"):
        raise HTTPException(401,"Invalid or expired token")
    user=db.get(User,int(payload["user_id"]))
    if not user:
        raise HTTPException(401,"User not found")
    return user

@app.post("/api/v1/auth/login")
async def api_login(request:Request,db:Session=Depends(get_db)):
    try: data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    email=str(data.get("email","")).strip().lower()
    password=str(data.get("password",""))
    if not email or not password: raise HTTPException(400,"Email and password are required")
    user=db.scalar(select(User).where(func.lower(User.email)==email))
    if not user or not verify_password(password,user.password_hash):
        raise HTTPException(401,"Invalid email or password")
    return {"access_token":make_api_token(user.id),"token_type":"bearer","expires_in":60*60*24*30}

@app.post("/api/v1/auth/register")
async def api_register(request:Request,db:Session=Depends(get_db)):
    try: data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    email=str(data.get("email","")).strip().lower()
    password=str(data.get("password",""))
    company_name=str(data.get("company_name","My Business")).strip() or "My Business"
    if not email or not password: raise HTTPException(400,"Email and password are required")
    if len(password)<8: raise HTTPException(400,"Password must be at least 8 characters")
    if db.scalar(select(User).where(func.lower(User.email)==email)): raise HTTPException(409,"Email already registered")
    user=User(email=email,password_hash=hash_password(password),company_name=company_name)
    db.add(user); db.commit()
    return {"access_token":make_api_token(user.id),"token_type":"bearer","expires_in":60*60*24*30}

@app.get("/api/v1/me")
def api_me(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db)
    return {"id":user.id,"email":user.email,"company_name":user.company_name,"subscription_status":user.subscription_status,"subscription_plan":user.subscription_plan}

@app.get("/api/v1/dashboard")
def api_dashboard(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db)
    invoices=list(db.scalars(select(Invoice).where(Invoice.owner_id==user.id).order_by(Invoice.due_date.asc())).all())
    outstanding=sum((i.balance for i in invoices),Decimal("0"))
    overdue=sum((i.balance for i in invoices if i.days_overdue>0),Decimal("0"))
    due_today=sum((i.balance for i in invoices if i.balance>0 and i.due_date==date.today()),Decimal("0"))
    paid=sum((i.paid_amount for i in invoices),Decimal("0"))
    return {"outstanding":str(outstanding),"overdue":str(overdue),"due_today":str(due_today),"paid":str(paid),"invoice_count":len(invoices),"customer_count":len({i.customer_name for i in invoices})}

@app.get("/api/v1/invoices")
def api_invoices(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db)
    invoices=list(db.scalars(select(Invoice).where(Invoice.owner_id==user.id).order_by(Invoice.due_date.asc())).all())
    return [{"id":i.id,"invoice_number":i.invoice_number,"customer_name":i.customer_name,"phone":i.phone,"email":i.email,"amount":str(i.amount),"paid_amount":str(i.paid_amount),"balance":str(i.balance),"issue_date":i.issue_date.isoformat(),"due_date":i.due_date.isoformat(),"status":i.status,"days_overdue":i.days_overdue,"aging_bucket":i.aging_bucket} for i in invoices]

@app.post("/api/v1/invoices")
async def api_create_invoice(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db)
    try: data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    required=["invoice_number","customer_name","amount","issue_date","due_date"]
    missing=[k for k in required if not str(data.get(k,"")).strip()]
    if missing: raise HTTPException(400,"Missing fields: "+", ".join(missing))
    try:
        amount=Decimal(str(data["amount"])); paid=Decimal(str(data.get("paid_amount","0")))
        issue_date=date.fromisoformat(str(data["issue_date"])); due_date=date.fromisoformat(str(data["due_date"]))
    except (InvalidOperation,ValueError): raise HTTPException(400,"Invalid invoice values")
    num=str(data["invoice_number"]).strip()
    if amount<=0 or paid<0 or paid>amount: raise HTTPException(400,"Invalid payment values")
    if db.scalar(select(Invoice).where(Invoice.invoice_number==num)): raise HTTPException(409,"Invoice number already exists")
    i=Invoice(owner_id=user.id,invoice_number=num,customer_name=str(data["customer_name"]).strip(),phone=str(data.get("phone","")).strip() or None,email=str(data.get("email","")).strip() or None,amount=amount,paid_amount=paid,issue_date=issue_date,due_date=due_date,status="paid" if paid>=amount else "partially_paid" if paid>0 else "unpaid",notes=str(data.get("notes","")).strip() or None)
    db.add(i); db.commit()
    return {"id":i.id,"invoice_number":i.invoice_number,"balance":str(i.balance),"status":i.status}

@app.post("/api/v1/invoices/{invoice_id}/mark-paid")
def api_mark_paid(invoice_id:int,request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.owner_id==user.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    invoice.paid_amount=invoice.amount; invoice.status="paid"; db.commit()
    return {"ok":True,"id":invoice.id,"status":invoice.status}

@app.get("/login",response_class=HTMLResponse)
def login_page(request:Request):
    return templates.TemplateResponse("login.html",{"request":request,"error":request.query_params.get("error","")})

@app.post("/login")
def login(request:Request,email:str=Form(...),password:str=Form(...),db:Session=Depends(get_db)):
    user=db.scalar(select(User).where(func.lower(User.email)==email.strip().lower()))
    if not user or not verify_password(password,user.password_hash):
        return RedirectResponse("/login?error=Invalid+email+or+password",303)
    response=RedirectResponse("/",303)
    set_session(response,user.id)
    return response

@app.get("/register",response_class=HTMLResponse)
def register_page(request:Request):
    return templates.TemplateResponse("register.html",{"request":request,"error":request.query_params.get("error","")})

@app.post("/register")
def register(request:Request,email:str=Form(...),password:str=Form(...),company_name:str=Form(...),db:Session=Depends(get_db)):
    email=email.strip().lower(); company_name=company_name.strip() or "My Business"
    if len(password)<8: return RedirectResponse("/register?error=Password+must+be+at+least+8+characters",303)
    if db.scalar(select(User).where(func.lower(User.email)==email)): return RedirectResponse("/register?error=Email+already+registered",303)
    user=User(email=email,password_hash=hash_password(password),company_name=company_name)
    db.add(user); db.commit()
    response=RedirectResponse("/",303); set_session(response,user.id); return response

@app.post("/logout")
def logout():
    response=RedirectResponse("/login",303); clear_session(response); return response

@app.get("/",response_class=HTMLResponse)
def dashboard(request:Request,db:Session=Depends(get_db)):
    user=session_user(request,db)
    if not user:
        return templates.TemplateResponse("welcome.html",{"request":request,"plans":PLANS})
    invoices=list(db.scalars(select(Invoice).where(Invoice.owner_id==user.id).order_by(Invoice.due_date.asc())).all())
    outstanding=sum((i.balance for i in invoices),Decimal("0"))
    overdue=sum((i.balance for i in invoices if i.days_overdue>0),Decimal("0"))
    due_today=sum((i.balance for i in invoices if i.balance>0 and i.due_date==date.today()),Decimal("0"))
    customers=len({i.customer_name for i in invoices})
    return templates.TemplateResponse("dashboard.html",{"request":request,"user":user,"csrf":csrf_for(request),"invoices":invoices,"outstanding":outstanding,"overdue":overdue,"due_today":due_today,"customers":customers,"money":money,"demo_added":request.query_params.get("demo_added"),"message":request.query_params.get("message")})

@app.post("/demo/seed")
def seed_demo(request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user=require_user(request,db); check_csrf(request,csrf)
    today=date.today(); added=0
    samples=[
        ("DEMO-001","Apex Industrial Supplies","919876543210","84000","0",-3),
        ("DEMO-002","Northstar Traders","919812345678","31500","5000",-12),
        ("DEMO-003","Himalaya Components","919998877665","125000","25000",-45),
        ("DEMO-004","Greenline Services","919900112233","18000","0",0),
        ("DEMO-005","Cedar & Co.","919811223344","46500","20000",-6),
        ("DEMO-006","Summit Retail Network","919887766554","22000","0",2),
    ]
    for num,name,phone,amount,paid,days_due in samples:
        if db.scalar(select(Invoice).where(Invoice.invoice_number==num)): continue
        amount_d=Decimal(amount); paid_d=Decimal(paid); due=today+timedelta(days=days_due)
        db.add(Invoice(owner_id=user.id,invoice_number=num,customer_name=name,phone=phone,email=None,amount=amount_d,paid_amount=paid_d,issue_date=due-timedelta(days=30),due_date=due,status="paid" if paid_d>=amount_d else "partially_paid" if paid_d>0 else "unpaid",notes="Demo data"))
        added+=1
    db.commit()
    return RedirectResponse(f"/?demo_added={added}",303)

@app.get("/invoices/new",response_class=HTMLResponse)
def new_invoice(request:Request,db:Session=Depends(get_db)):
    user=require_user(request,db)
    return templates.TemplateResponse("invoice_form.html",{"request":request,"user":user,"csrf":csrf_for(request),"today":date.today()})

@app.post("/invoices/new")
def create_invoice(request:Request,csrf:str=Form(...),invoice_number:str=Form(...),customer_name:str=Form(...),phone:str=Form(""),email:str=Form(""),amount:str=Form(...),paid_amount:str=Form("0"),issue_date:str=Form(...),due_date:str=Form(...),db:Session=Depends(get_db)):
    user=require_user(request,db); check_csrf(request,csrf)
    try: amount_d,paid_d=Decimal(amount),Decimal(paid_amount)
    except InvalidOperation: raise HTTPException(400,"Invalid amount")
    if amount_d<=0 or paid_d<0 or paid_d>amount_d: raise HTTPException(400,"Invalid payment values")
    if db.scalar(select(Invoice).where(Invoice.invoice_number==invoice_number.strip())): raise HTTPException(400,"Invoice number already exists")
    i=Invoice(owner_id=user.id,invoice_number=invoice_number.strip(),customer_name=customer_name.strip(),phone=phone.strip() or None,email=email.strip() or None,amount=amount_d,paid_amount=paid_d,issue_date=date.fromisoformat(issue_date),due_date=date.fromisoformat(due_date),status="paid" if paid_d>=amount_d else "partially_paid" if paid_d>0 else "unpaid")
    db.add(i);db.commit();return RedirectResponse("/",303)

@app.post("/invoices/import")
async def import_csv(request:Request,csrf:str=Form(...),file:UploadFile=File(...),db:Session=Depends(get_db)):
    user=require_user(request,db); check_csrf(request,csrf)
    if not file.filename.lower().endswith(".csv"): raise HTTPException(400,"Upload a CSV file")
    reader=csv.DictReader(io.StringIO((await file.read()).decode("utf-8-sig")))
    req={"invoice_number","customer_name","amount","issue_date","due_date"}
    if not req.issubset(set(reader.fieldnames or [])): raise HTTPException(400,"CSV missing required columns")
    for row in reader:
        num=row["invoice_number"].strip()
        if db.scalar(select(Invoice).where(Invoice.invoice_number==num)): continue
        paid=Decimal((row.get("paid_amount") or "0").strip()); amount=Decimal(row["amount"].strip())
        db.add(Invoice(owner_id=user.id,invoice_number=num,customer_name=row["customer_name"].strip(),phone=(row.get("phone") or "").strip() or None,email=(row.get("email") or "").strip() or None,amount=amount,paid_amount=paid,issue_date=date.fromisoformat(row["issue_date"].strip()),due_date=date.fromisoformat(row["due_date"].strip()),status="paid" if paid>=amount else "partially_paid" if paid>0 else "unpaid"))
    db.commit();return RedirectResponse("/",303)

@app.get("/invoices/{invoice_id}",response_class=HTMLResponse)
def invoice_detail(request:Request,invoice_id:int,tone:str="friendly",db:Session=Depends(get_db)):
    user=require_user(request,db)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.owner_id==user.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    msg=ai_message(invoice,tone) or build_message(invoice,tone)
    latest=db.scalar(select(PaymentLink).where(PaymentLink.invoice_id==invoice.id,PaymentLink.owner_id==user.id).order_by(PaymentLink.created_at.desc()))
    digits="".join(ch for ch in (invoice.phone or "") if ch.isdigit())
    wa=f"https://wa.me/{digits}?text={urllib.parse.quote(msg)}" if digits else None
    return templates.TemplateResponse("invoice_detail.html",{"request":request,"user":user,"csrf":csrf_for(request),"invoice":invoice,"message":msg,"tone":tone,"wa_url":wa,"money":money,"payment_link":latest})

@app.post("/invoices/{invoice_id}/payment")
def payment(invoice_id:int,request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user=require_user(request,db); check_csrf(request,csrf)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.owner_id==user.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    result=create_payment_link(user,invoice)
    link=PaymentLink(owner_id=user.id,invoice_id=invoice.id,provider_link_id=result["id"],short_url=result["short_url"],amount_paise=int(result["amount"]),status=result.get("status","created"))
    db.add(link);db.commit()
    return RedirectResponse(f"/invoices/{invoice.id}?message=Payment+link+created",303)

@app.post("/invoices/{invoice_id}/whatsapp")
def whatsapp(invoice_id:int,request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user=require_user(request,db); check_csrf(request,csrf)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.owner_id==user.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    link=db.scalar(select(PaymentLink).where(PaymentLink.invoice_id==invoice.id,PaymentLink.owner_id==user.id).order_by(PaymentLink.created_at.desc()))
    message_id=send_whatsapp_template(user,invoice,link.short_url if link else None)
    db.add(ReminderLog(owner_id=user.id,invoice_id=invoice.id,stage="manual",channel="whatsapp",status="sent",provider_message_id=message_id))
    db.commit()
    return RedirectResponse(f"/invoices/{invoice.id}?message=WhatsApp+sent",303)

@app.post("/invoices/{invoice_id}/mark-paid")
def mark_paid(invoice_id:int,request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user=require_user(request,db); check_csrf(request,csrf)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.owner_id==user.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    invoice.paid_amount=invoice.amount;invoice.status="paid";db.commit();return RedirectResponse(f"/invoices/{invoice.id}?message=Invoice+marked+paid",303)

@app.get("/settings",response_class=HTMLResponse)
def settings_page(request:Request,db:Session=Depends(get_db)):
    user=require_user(request,db)
    return templates.TemplateResponse("settings.html",{"request":request,"user":user,"csrf":csrf_for(request),"rzp_configured":bool(decrypt(user.razorpay_key_id_enc) and decrypt(user.razorpay_key_secret_enc)),"wa_configured":bool(decrypt(user.whatsapp_access_token_enc) and user.whatsapp_phone_number_id),"wa_version":settings.whatsapp_graph_version})

@app.post("/settings")
def update_settings(request:Request,csrf:str=Form(...),company_name:str=Form(...),razorpay_key_id:str=Form(""),razorpay_key_secret:str=Form(""),razorpay_webhook_secret:str=Form(""),whatsapp_access_token:str=Form(""),whatsapp_phone_number_id:str=Form(""),whatsapp_template_name:str=Form("invoice_payment_reminder"),whatsapp_template_language:str=Form("en"),db:Session=Depends(get_db)):
    user=require_user(request,db); check_csrf(request,csrf)
    user.company_name=company_name.strip() or user.company_name
    if razorpay_key_id.strip(): user.razorpay_key_id_enc=encrypt(razorpay_key_id.strip())
    if razorpay_key_secret.strip(): user.razorpay_key_secret_enc=encrypt(razorpay_key_secret.strip())
    if razorpay_webhook_secret.strip(): user.razorpay_webhook_secret_enc=encrypt(razorpay_webhook_secret.strip())
    if whatsapp_access_token.strip(): user.whatsapp_access_token_enc=encrypt(whatsapp_access_token.strip())
    user.whatsapp_phone_number_id=whatsapp_phone_number_id.strip() or user.whatsapp_phone_number_id
    user.whatsapp_template_name=whatsapp_template_name.strip() or user.whatsapp_template_name
    user.whatsapp_template_language=whatsapp_template_language.strip() or user.whatsapp_template_language
    db.commit()
    return RedirectResponse("/settings?message=Settings+saved",303)

@app.get("/billing",response_class=HTMLResponse)
def billing(request:Request,db:Session=Depends(get_db)):
    user=require_user(request,db)
    return templates.TemplateResponse("billing.html",{"request":request,"user":user,"csrf":csrf_for(request),"plans":PLANS,"message":request.query_params.get("message",""),"subscription_url":request.query_params.get("subscription_url","")})

@app.post("/billing/subscribe")
def subscribe(request:Request,csrf:str=Form(...),plan_code:str=Form(...),db:Session=Depends(get_db)):
    user=require_user(request,db); check_csrf(request,csrf)
    if plan_code not in PLANS: raise HTTPException(400,"Unknown plan")
    if not settings.razorpay_platform_key_id or not settings.razorpay_platform_key_secret:
        return RedirectResponse("/billing?message=Add+RAZORPAY_PLATFORM_KEY_ID+and+RAZORPAY_PLATFORM_KEY_SECRET+in+Render+Environment+first",303)
    meta=PLANS[plan_code]
    plan=db.get(BillingPlan,plan_code)
    if not plan:
        plan=BillingPlan(code=plan_code,name=meta["name"],amount_paise=meta["amount"],period="monthly",interval=1)
        db.add(plan);db.commit()
    if not plan.razorpay_plan_id:
        result=platform_request("POST","/plans",{"period":"monthly","interval":1,"item":{"name":meta["name"],"amount":meta["amount"],"currency":"INR","description":meta["description"]}})
        plan.razorpay_plan_id=result["id"];db.commit()
    result=platform_request("POST","/subscriptions",{"plan_id":plan.razorpay_plan_id,"total_count":120,"quantity":1,"customer_notify":1,"notes":{"recoverflow_user_id":str(user.id),"plan":plan_code}})
    user.subscription_id=result.get("id");user.subscription_status=result.get("status","created");user.subscription_plan=plan_code;db.commit()
    short_url=result.get("short_url")
    if short_url: return RedirectResponse(short_url,303)
    return RedirectResponse("/billing?message=Subscription+created",303)

@app.post("/webhooks/razorpay")
async def razorpay_webhook(request:Request,db:Session=Depends(get_db)):
    raw=await request.body()
    try: payload=json.loads(raw)
    except json.JSONDecodeError: raise HTTPException(400,"Invalid JSON")
    event=payload.get("event","")
    if event.startswith("payment_link."):
        entity=((payload.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
        notes=entity.get("notes") or {}
        user_id=int(notes.get("recoverflow_user_id",0) or 0)
        invoice_id=int(notes.get("recoverflow_invoice_id",0) or 0)
        user=db.get(User,user_id)
        if not user or not verify_webhook(raw,request.headers.get("x-razorpay-signature"),webhook_secret(user)):
            raise HTTPException(401,"Invalid webhook signature")
        invoice=db.get(Invoice,invoice_id)
        if invoice and invoice.owner_id==user.id:
            paid_paise=int(entity.get("amount_paid",0) or 0)
            invoice.paid_amount=Decimal(paid_paise)/Decimal(100)
            invoice.status="paid" if invoice.paid_amount>=invoice.amount else "partially_paid"
            link=db.scalar(select(PaymentLink).where(PaymentLink.provider_link_id==entity.get("id")))
            if link: link.status=entity.get("status","updated")
            db.commit()
        return JSONResponse({"ok":True})
    if event.startswith("subscription."):
        entity=((payload.get("payload") or {}).get("subscription") or {}).get("entity") or {}
        sub_id=entity.get("id")
        user=db.scalar(select(User).where(User.subscription_id==sub_id))
        secret=settings.razorpay_platform_webhook_secret
        if not user or not verify_webhook(raw,request.headers.get("x-razorpay-signature"),secret):
            raise HTTPException(401,"Invalid webhook signature")
        user.subscription_status=entity.get("status",user.subscription_status);db.commit()
        return JSONResponse({"ok":True})
    return JSONResponse({"ok":True,"ignored":True})

@app.post("/internal/reminders")
def internal_reminders(request:Request,db:Session=Depends(get_db)):
    if not settings.cron_secret or request.headers.get("x-cron-secret")!=settings.cron_secret:
        raise HTTPException(401,"Unauthorized")
    sent=0; skipped=0
    users=list(db.scalars(select(User)).all())
    for user in users:
        token_ok=bool(decrypt(user.whatsapp_access_token_enc) and user.whatsapp_phone_number_id and settings.whatsapp_graph_version)
        if not token_ok: continue
        invoices=list(db.scalars(select(Invoice).where(Invoice.owner_id==user.id,Invoice.paid_amount<Invoice.amount)).all())
        for invoice in invoices:
            d=invoice.days_overdue
            stage="1d" if d>=1 and d<7 else "7d" if d>=7 and d<30 else "30d" if d>=30 else ""
            if not stage or not invoice.phone: continue
            exists=db.scalar(select(ReminderLog).where(ReminderLog.invoice_id==invoice.id,ReminderLog.stage==stage,ReminderLog.channel=="whatsapp"))
            if exists: continue
            try:
                link=db.scalar(select(PaymentLink).where(PaymentLink.invoice_id==invoice.id,PaymentLink.owner_id==user.id).order_by(PaymentLink.created_at.desc()))
                if not link and decrypt(user.razorpay_key_id_enc) and decrypt(user.razorpay_key_secret_enc):
                    result=create_payment_link(user,invoice)
                    link=PaymentLink(owner_id=user.id,invoice_id=invoice.id,provider_link_id=result["id"],short_url=result["short_url"],amount_paise=int(result["amount"]),status=result.get("status","created"))
                    db.add(link);db.flush()
                mid=send_whatsapp_template(user,invoice,link.short_url if link else None)
                db.add(ReminderLog(owner_id=user.id,invoice_id=invoice.id,stage=stage,channel="whatsapp",status="sent",provider_message_id=mid));db.commit();sent+=1
            except HTTPException:
                db.rollback(); skipped+=1
    return {"ok":True,"sent":sent,"skipped":skipped}