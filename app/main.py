from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import csv, io, json, urllib.parse, secrets, hashlib
from fastapi import FastAPI, Depends, Form, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from openai import OpenAI
from .config import settings
from .db import get_db, init_db
from .models import User, Invoice, PaymentLink, ReminderLog, BillingPlan, DeviceToken, Workspace, TeamMember, TeamInvite, AuditLog, RecurringInvoice, Lead
from .security import set_session, clear_session, read_session, new_csrf, encrypt, decrypt, make_api_token, read_api_token, make_public_invoice_token, read_public_invoice_token, make_statement_token, read_statement_token, make_api_token, read_api_token
from .password import hash_password, verify_password
from .integrations import create_payment_link, send_whatsapp_template, verify_webhook, merchant_keys, webhook_secret, platform_request, send_expo_push

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

ROLES={"owner","admin","finance","collector","viewer"}

def workspace_for(user,db):
    if user.workspace_id:
        workspace=db.get(Workspace,user.workspace_id)
        if workspace: return workspace
    workspace=Workspace(name=user.company_name or "My Business")
    db.add(workspace); db.flush()
    user.workspace_id=workspace.id; user.role="owner"
    db.add(TeamMember(workspace_id=workspace.id,user_id=user.id,role="owner",status="active"))
    return workspace

def require_role(request,db,*allowed):
    user=session_user(request,db)
    if not user: raise HTTPException(status_code=303,headers={"Location":"/login"})
    if user.role not in allowed: raise HTTPException(403,"Your role does not have access to this action.")
    return user,workspace_for(user,db)

def api_require_role(request,db,*allowed):
    user=api_user(request,db)
    if user.role not in allowed: raise HTTPException(403,"Your role does not have access to this action.")
    return user,workspace_for(user,db)

def audit(db,user,event,entity_type=None,entity_id=None,metadata=None):
    if user.workspace_id:
        db.add(AuditLog(workspace_id=user.workspace_id,user_id=user.id,event=event,entity_type=entity_type,entity_id=str(entity_id) if entity_id is not None else None,metadata_json=json.dumps(metadata or {},separators=(",",":"))))

def invite_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def month_days(y,m):
    if m==12: return (date(y+1,1,1)-date(y,m,1)).days
    return (date(y,m+1,1)-date(y,m,1)).days

def advance_date(d,cadence):
    if cadence=="weekly": return d+timedelta(days=7)
    if cadence=="quarterly":
        m=d.month+3; y=d.year+(m-1)//12; m=(m-1)%12+1
        return date(y,m,min(d.day,month_days(y,m)))
    if cadence=="yearly":
        y=d.year+1; return date(y,d.month,min(d.day,month_days(y,d.month)))
    m=d.month+1; y=d.year+(m-1)//12; m=(m-1)%12+1
    return date(y,m,min(d.day,month_days(y,m)))

@app.get("/health")
def health(): return {"status":"ok","app":settings.app_name}

@app.get("/welcome",response_class=HTMLResponse)
def welcome(request:Request):
    return templates.TemplateResponse("welcome.html",{"request":request,"plans":PLANS})

def api_user(request:Request, db:Session):
    header=request.headers.get("authorization","")
    if not header.lower().startswith("bearer "): raise HTTPException(401,"Authentication required")
    payload=read_api_token(header[7:].strip())
    if not payload or not payload.get("user_id"): raise HTTPException(401,"Invalid or expired token")
    user=db.get(User,int(payload["user_id"]))
    if not user: raise HTTPException(401,"User not found")
    return user

@app.post("/api/v1/auth/login")
async def api_login(request:Request,db:Session=Depends(get_db)):
    try: data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    email=str(data.get("email","")).strip().lower(); password=str(data.get("password",""))
    if not email or not password: raise HTTPException(400,"Email and password are required")
    user=db.scalar(select(User).where(func.lower(User.email)==email))
    if not user or not verify_password(password,user.password_hash): raise HTTPException(401,"Invalid email or password")
    workspace=workspace_for(user,db); db.commit()
    return {"access_token":make_api_token(user.id),"token_type":"bearer","expires_in":60*60*24*30,"workspace_id":workspace.id,"role":user.role}

@app.post("/api/v1/auth/register")
async def api_register(request:Request,db:Session=Depends(get_db)):
    try: data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    email=str(data.get("email","")).strip().lower(); password=str(data.get("password","")); company_name=str(data.get("company_name","My Business")).strip() or "My Business"
    if not email or not password: raise HTTPException(400,"Email and password are required")
    if len(password)<8: raise HTTPException(400,"Password must be at least 8 characters")
    if db.scalar(select(User).where(func.lower(User.email)==email)): raise HTTPException(409,"Email already registered")
    user=User(email=email,password_hash=hash_password(password),company_name=company_name,role="owner")
    db.add(user); db.flush()
    workspace=Workspace(name=company_name); db.add(workspace); db.flush()
    user.workspace_id=workspace.id; db.add(TeamMember(workspace_id=workspace.id,user_id=user.id,role="owner",status="active"))
    audit(db,user,"workspace.created","workspace",workspace.id,{"company_name":company_name}); db.commit()
    return {"access_token":make_api_token(user.id),"token_type":"bearer","expires_in":60*60*24*30,"workspace_id":workspace.id,"role":user.role}

@app.get("/api/v1/me")
def api_me(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); workspace=workspace_for(user,db); db.commit()
    return {"id":user.id,"email":user.email,"company_name":user.company_name,"workspace_id":workspace.id,"role":user.role,"subscription_status":user.subscription_status,"subscription_plan":user.subscription_plan}

@app.get("/api/v1/dashboard")
def api_dashboard(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); ensure_access(user); workspace=workspace_for(user,db)
    invoices=list(db.scalars(select(Invoice).where(Invoice.workspace_id==workspace.id).order_by(Invoice.due_date.asc())).all())
    outstanding=sum((i.balance for i in invoices),Decimal("0")); overdue=sum((i.balance for i in invoices if i.days_overdue>0),Decimal("0")); due_today=sum((i.balance for i in invoices if i.balance>0 and i.due_date==date.today()),Decimal("0")); paid=sum((i.paid_amount for i in invoices),Decimal("0")); invoiced=sum((i.amount for i in invoices),Decimal("0"))
    rate=(paid/invoiced*Decimal("100")) if invoiced else Decimal("0")
    return {"outstanding":str(outstanding),"overdue":str(overdue),"due_today":str(due_today),"paid":str(paid),"invoice_count":len(invoices),"customer_count":len({i.customer_name for i in invoices}),"collection_rate":str(rate.quantize(Decimal("0.1")))}

@app.get("/api/v1/customers")
def api_customers(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); ensure_access(user); workspace=workspace_for(user,db)
    invoices=list(db.scalars(select(Invoice).where(Invoice.workspace_id==workspace.id)).all()); groups={}
    for i in invoices:
        key=i.customer_name.strip().lower(); g=groups.setdefault(key,{"name":i.customer_name,"phone":i.phone,"email":i.email,"invoice_count":0,"outstanding":Decimal("0"),"overdue":Decimal("0")})
        g["invoice_count"]+=1; g["outstanding"]+=i.balance
        if i.days_overdue:g["overdue"]+=i.balance
    return [{**g,"outstanding":str(g["outstanding"]),"overdue":str(g["overdue"])} for g in sorted(groups.values(),key=lambda x:x["outstanding"],reverse=True)]

@app.get("/api/v1/team")
def api_team(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin")
    members=list(db.scalars(select(TeamMember).where(TeamMember.workspace_id==workspace.id,TeamMember.status=="active").order_by(TeamMember.created_at.asc())).all())
    return [{"id":m.id,"user_id":m.user_id,"email":db.get(User,m.user_id).email,"role":m.role,"status":m.status} for m in members]

@app.post("/api/v1/team/invite")
async def api_team_invite(request:Request,db:Session=Depends(get_db)):
    user,workspace=api_require_role(request,db,"owner","admin")
    try:data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    email=str(data.get("email","")).strip().lower(); role=str(data.get("role","viewer"))
    if not email or role not in {"admin","finance","collector","viewer"}: raise HTTPException(400,"Invalid invite")
    existing=db.scalar(select(User).where(func.lower(User.email)==email))
    if existing and existing.workspace_id==workspace.id: raise HTTPException(409,"User is already in this workspace")
    raw=secrets.token_urlsafe(32); inv=TeamInvite(workspace_id=workspace.id,email=email,role=role,token_digest=invite_digest(raw),status="pending")
    db.add(inv); db.flush(); audit(db,user,"team.invite_created","team_invite",inv.id,{"email":email,"role":role}); db.commit()
    return {"ok":True,"invite_url":str(request.base_url).rstrip("/")+"/invite/"+raw,"role":role}

@app.get("/api/v1/recurring")
def api_recurring(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); ensure_access(user); workspace=workspace_for(user,db)
    items=list(db.scalars(select(RecurringInvoice).where(RecurringInvoice.workspace_id==workspace.id).order_by(RecurringInvoice.active.desc(),RecurringInvoice.next_issue_date.asc())).all())
    return [{"id":r.id,"customer_name":r.customer_name,"phone":r.phone,"email":r.email,"amount":str(r.amount),"cadence":r.cadence,"next_issue_date":r.next_issue_date.isoformat(),"due_days":r.due_days,"active":r.active} for r in items]

@app.post("/api/v1/recurring")
async def api_create_recurring(request:Request,db:Session=Depends(get_db)):
    user,workspace=api_require_role(request,db,"owner","admin","finance")
    try:data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    cadence=str(data.get("cadence","monthly"))
    if cadence not in {"weekly","monthly","quarterly","yearly"}: raise HTTPException(400,"Invalid cadence")
    try: amount=Decimal(str(data["amount"])); next_date=date.fromisoformat(str(data["next_issue_date"])); due_days=int(data.get("due_days",7))
    except (KeyError,InvalidOperation,ValueError): raise HTTPException(400,"Invalid recurring invoice values")
    if amount<=0 or due_days<0: raise HTTPException(400,"Invalid recurring invoice values")
    item=RecurringInvoice(workspace_id=workspace.id,created_by_user_id=user.id,customer_name=str(data.get("customer_name","")).strip(),phone=str(data.get("phone","")).strip() or None,email=str(data.get("email","")).strip() or None,amount=amount,cadence=cadence,next_issue_date=next_date,due_days=due_days,active=True)
    if not item.customer_name: raise HTTPException(400,"Customer name is required")
    db.add(item); db.flush(); audit(db,user,"recurring.created","recurring_invoice",item.id,{"customer_name":item.customer_name,"cadence":cadence}); db.commit()
    return {"id":item.id,"active":item.active,"next_issue_date":item.next_issue_date.isoformat()}

@app.post("/api/v1/recurring/{recurring_id}/toggle")
def api_toggle_recurring(recurring_id:int,request:Request,db:Session=Depends(get_db)):
    user,workspace=api_require_role(request,db,"owner","admin","finance")
    item=db.scalar(select(RecurringInvoice).where(RecurringInvoice.id==recurring_id,RecurringInvoice.workspace_id==workspace.id))
    if not item: raise HTTPException(404,"Recurring invoice not found")
    item.active=not item.active; audit(db,user,"recurring.toggled","recurring_invoice",item.id,{"active":item.active}); db.commit()
    return {"ok":True,"active":item.active}

@app.get("/api/v1/invoices")
def api_invoices(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); ensure_access(user); workspace=workspace_for(user,db)
    invoices=list(db.scalars(select(Invoice).where(Invoice.workspace_id==workspace.id).order_by(Invoice.due_date.asc())).all())
    return [{"id":i.id,"invoice_number":i.invoice_number,"customer_name":i.customer_name,"phone":i.phone,"email":i.email,"amount":str(i.amount),"paid_amount":str(i.paid_amount),"balance":str(i.balance),"issue_date":i.issue_date.isoformat(),"due_date":i.due_date.isoformat(),"status":i.status,"days_overdue":i.days_overdue,"aging_bucket":i.aging_bucket,"gstin":i.gstin,"place_of_supply":i.place_of_supply,"tax_rate":str(i.tax_rate) if i.tax_rate is not None else None,"tax_amount":str(i.tax_amount) if i.tax_amount is not None else None,"tds_amount":str(i.tds_amount) if i.tds_amount is not None else None} for i in invoices]

@app.post("/api/v1/invoices")
async def api_create_invoice(request:Request,db:Session=Depends(get_db)):
    user,workspace=api_require_role(request,db,"owner","admin","finance","collector")
    try: data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    required=["invoice_number","customer_name","amount","issue_date","due_date"]; missing=[k for k in required if not str(data.get(k,"")).strip()]
    if missing: raise HTTPException(400,"Missing fields: "+", ".join(missing))
    try:
        amount=Decimal(str(data["amount"])); paid=Decimal(str(data.get("paid_amount","0"))); issue_date=date.fromisoformat(str(data["issue_date"])); due_date=date.fromisoformat(str(data["due_date"]))
        tax_rate=Decimal(str(data["tax_rate"])) if str(data.get("tax_rate","")).strip() else None; tax_amount=Decimal(str(data["tax_amount"])) if str(data.get("tax_amount","")).strip() else None; tds_amount=Decimal(str(data["tds_amount"])) if str(data.get("tds_amount","")).strip() else None
    except (InvalidOperation,ValueError): raise HTTPException(400,"Invalid invoice values")
    if amount<=0 or paid<0 or paid>amount or any(v is not None and v<0 for v in (tax_rate,tax_amount,tds_amount)): raise HTTPException(400,"Invalid invoice values")
    num=str(data["invoice_number"]).strip()
    if db.scalar(select(Invoice).where(Invoice.invoice_number==num)): raise HTTPException(409,"Invoice number already exists")
    i=Invoice(owner_id=user.id,workspace_id=workspace.id,invoice_number=num,customer_name=str(data["customer_name"]).strip(),phone=str(data.get("phone","")).strip() or None,email=str(data.get("email","")).strip() or None,amount=amount,paid_amount=paid,issue_date=issue_date,due_date=due_date,status="paid" if paid>=amount else "partially_paid" if paid>0 else "unpaid",notes=str(data.get("notes","")).strip() or None,gstin=str(data.get("gstin","")).strip() or None,place_of_supply=str(data.get("place_of_supply","")).strip() or None,tax_rate=tax_rate,tax_amount=tax_amount,tds_amount=tds_amount)
    db.add(i); db.flush(); audit(db,user,"invoice.created","invoice",i.id,{"invoice_number":i.invoice_number}); db.commit()
    return {"id":i.id,"invoice_number":i.invoice_number,"balance":str(i.balance),"status":i.status}

@app.get("/api/v1/invoices/{invoice_id}")
def api_invoice(invoice_id:int,request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); workspace=workspace_for(user,db)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    return {"id":invoice.id,"invoice_number":invoice.invoice_number,"customer_name":invoice.customer_name,"phone":invoice.phone,"email":invoice.email,"amount":str(invoice.amount),"paid_amount":str(invoice.paid_amount),"balance":str(invoice.balance),"issue_date":invoice.issue_date.isoformat(),"due_date":invoice.due_date.isoformat(),"status":invoice.status,"days_overdue":invoice.days_overdue,"aging_bucket":invoice.aging_bucket,"gstin":invoice.gstin,"place_of_supply":invoice.place_of_supply,"tax_rate":str(invoice.tax_rate) if invoice.tax_rate is not None else None,"tax_amount":str(invoice.tax_amount) if invoice.tax_amount is not None else None,"tds_amount":str(invoice.tds_amount) if invoice.tds_amount is not None else None}

@app.post("/api/v1/invoices/{invoice_id}/payment-link")
def api_payment_link(invoice_id:int,request:Request,db:Session=Depends(get_db)):
    user,workspace=api_require_role(request,db,"owner","admin","finance","collector")
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    result=create_payment_link(user,invoice)
    link=PaymentLink(owner_id=user.id,invoice_id=invoice.id,provider_link_id=result["id"],short_url=result["short_url"],amount_paise=int(result["amount"]),status=result.get("status","created"))
    db.add(link); db.flush(); audit(db,user,"payment_link.created","payment_link",link.id,{"invoice_id":invoice.id}); db.commit()
    return {"ok":True,"url":link.short_url,"status":link.status}

@app.post("/api/v1/invoices/{invoice_id}/whatsapp")
def api_whatsapp(invoice_id:int,request:Request,db:Session=Depends(get_db)):
    user,workspace=api_require_role(request,db,"owner","admin","finance","collector")
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    link=db.scalar(select(PaymentLink).where(PaymentLink.invoice_id==invoice.id).order_by(PaymentLink.created_at.desc()))
    message_id=send_whatsapp_template(user,invoice,link.short_url if link else None)
    db.add(ReminderLog(owner_id=user.id,invoice_id=invoice.id,stage="mobile",channel="whatsapp",status="sent",provider_message_id=message_id)); audit(db,user,"whatsapp.reminder_sent","invoice",invoice.id); db.commit()
    return {"ok":True,"provider_message_id":message_id}

@app.post("/api/v1/notifications/register")
async def api_register_notification(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db)
    try: data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    token=str(data.get("token","")).strip(); platform=str(data.get("platform","unknown")).strip().lower()
    if not token or not token.startswith("ExponentPushToken["): raise HTTPException(400,"Invalid Expo push token")
    existing=db.scalar(select(DeviceToken).where(DeviceToken.token==token))
    if existing: existing.owner_id=user.id; existing.platform=platform; existing.active=True
    else: db.add(DeviceToken(owner_id=user.id,token=token,platform=platform,active=True))
    db.commit(); return {"ok":True}

@app.post("/api/v1/notifications/test")
def api_test_notification(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db)
    tokens=list(db.scalars(select(DeviceToken).where(DeviceToken.owner_id==user.id,DeviceToken.active==True)).all())
    result=send_expo_push([t.token for t in tokens],"RecoverFlow is connected","Push notifications are working.",{"screen":"home"})
    return {"ok":True,"sent":len(result)}

@app.get("/pay/{token}",response_class=HTMLResponse)
def public_invoice_pay(request:Request,token:str,db:Session=Depends(get_db)):
    payload=read_public_invoice_token(token)
    if not payload or not payload.get("invoice_id"): raise HTTPException(404,"Payment page not found")
    invoice=db.get(Invoice,int(payload["invoice_id"]))
    if not invoice or invoice.balance<=0: raise HTTPException(404,"Payment page not found")
    link=db.scalar(select(PaymentLink).where(PaymentLink.invoice_id==invoice.id).order_by(PaymentLink.created_at.desc()))
    owner=db.get(User,invoice.owner_id)
    return templates.TemplateResponse("pay.html",{"request":request,"invoice":invoice,"payment_link":link,"money":money,"company_name":owner.company_name if owner else "Business"})

@app.post("/api/v1/invoices/{invoice_id}/mark-paid")
def api_mark_paid(invoice_id:int,request:Request,db:Session=Depends(get_db)):
    user,workspace=api_require_role(request,db,"owner","admin","finance","collector")
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    invoice.paid_amount=invoice.amount; invoice.status="paid"; audit(db,user,"invoice.marked_paid","invoice",invoice.id); db.commit()
    tokens=list(db.scalars(select(DeviceToken).where(DeviceToken.owner_id==user.id,DeviceToken.active==True)).all())
    send_expo_push([t.token for t in tokens],"Payment received","Invoice "+invoice.invoice_number+" is marked paid.",{"screen":"invoices","invoiceId":invoice.id})
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
    user=User(email=email,password_hash=hash_password(password),company_name=company_name,role="owner")
    db.add(user); db.flush()
    workspace=Workspace(name=company_name); db.add(workspace); db.flush()
    user.workspace_id=workspace.id; db.add(TeamMember(workspace_id=workspace.id,user_id=user.id,role="owner",status="active"))
    audit(db,user,"workspace.created","workspace",workspace.id,{"company_name":company_name}); db.commit()
    response=RedirectResponse("/",303); set_session(response,user.id); return response

@app.post("/logout")
def logout():
    response=RedirectResponse("/login",303); clear_session(response); return response

@app.get("/",response_class=HTMLResponse)
def dashboard(request:Request,db:Session=Depends(get_db)):
    user=session_user(request,db)
    if not user:
        return templates.TemplateResponse("welcome.html",{"request":request,"plans":PLANS})
    if subscription_state(user)=="expired":
        return RedirectResponse("/billing?message=Your+14-day+trial+has+ended.+Choose+a+plan+to+continue.",303)
    workspace=workspace_for(user,db)
    invoices=list(db.scalars(select(Invoice).where(Invoice.workspace_id==workspace.id).order_by(Invoice.due_date.asc())).all())
    outstanding=sum((i.balance for i in invoices),Decimal("0"))
    overdue=sum((i.balance for i in invoices if i.days_overdue>0),Decimal("0"))
    due_today=sum((i.balance for i in invoices if i.balance>0 and i.due_date==date.today()),Decimal("0"))
    customers=len({i.customer_name for i in invoices}); invoiced=sum((i.amount for i in invoices),Decimal("0")); paid=sum((i.paid_amount for i in invoices),Decimal("0"))
    collection_rate=(paid/invoiced*Decimal("100")) if invoiced else Decimal("0")
    aging={bucket:sum((i.balance for i in invoices if i.aging_bucket==bucket),Decimal("0")) for bucket in ["Current","1–7 days","8–30 days","30+ days"]}
    return templates.TemplateResponse("dashboard.html",{"request":request,"user":user,"csrf":csrf_for(request),"invoices":invoices,"outstanding":outstanding,"overdue":overdue,"due_today":due_today,"customers":customers,"money":money,"collection_rate":collection_rate,"aging":aging,"paid":paid,"invoiced":invoiced,"demo_added":request.query_params.get("demo_added"),"message":request.query_params.get("message")})

@app.post("/demo/seed")
def seed_demo(request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance"); check_csrf(request,csrf)
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
        db.add(Invoice(owner_id=user.id,workspace_id=workspace.id,invoice_number=num,customer_name=name,phone=phone,email=None,amount=amount_d,paid_amount=paid_d,issue_date=due-timedelta(days=30),due_date=due,status="paid" if paid_d>=amount_d else "partially_paid" if paid_d>0 else "unpaid",notes="Demo data"))
        added+=1
    db.commit()
    return RedirectResponse(f"/?demo_added={added}",303)

@app.get("/invoices/new",response_class=HTMLResponse)
def new_invoice(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector")
    return templates.TemplateResponse("invoice_form.html",{"request":request,"user":user,"csrf":csrf_for(request),"today":date.today()})

@app.post("/invoices/new")
def create_invoice(request:Request,csrf:str=Form(...),invoice_number:str=Form(...),customer_name:str=Form(...),phone:str=Form(""),email:str=Form(""),amount:str=Form(...),paid_amount:str=Form("0"),issue_date:str=Form(...),due_date:str=Form(...),db:Session=Depends(get_db)):
    user=require_user(request,db); check_csrf(request,csrf)
    try: amount_d,paid_d=Decimal(amount),Decimal(paid_amount)
    except InvalidOperation: raise HTTPException(400,"Invalid amount")
    if amount_d<=0 or paid_d<0 or paid_d>amount_d: raise HTTPException(400,"Invalid payment values")
    if db.scalar(select(Invoice).where(Invoice.invoice_number==invoice_number.strip())): raise HTTPException(400,"Invoice number already exists")
    i=Invoice(owner_id=user.id,workspace_id=workspace.id,invoice_number=invoice_number.strip(),customer_name=customer_name.strip(),phone=phone.strip() or None,email=email.strip() or None,amount=amount_d,paid_amount=paid_d,issue_date=date.fromisoformat(issue_date),due_date=date.fromisoformat(due_date),status="paid" if paid_d>=amount_d else "partially_paid" if paid_d>0 else "unpaid")
    db.add(i); audit(db,user,"invoice.created","invoice",i.id,{"invoice_number":i.invoice_number}); db.commit();return RedirectResponse("/",303)

@app.post("/invoices/import")
async def import_csv(request:Request,csrf:str=Form(...),file:UploadFile=File(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector"); check_csrf(request,csrf)
    if not file.filename.lower().endswith(".csv"): raise HTTPException(400,"Upload a CSV file")
    reader=csv.DictReader(io.StringIO((await file.read()).decode("utf-8-sig")))
    req={"invoice_number","customer_name","amount","issue_date","due_date"}
    if not req.issubset(set(reader.fieldnames or [])): raise HTTPException(400,"CSV missing required columns")
    for row in reader:
        num=row["invoice_number"].strip()
        if db.scalar(select(Invoice).where(Invoice.invoice_number==num)): continue
        paid=Decimal((row.get("paid_amount") or "0").strip()); amount=Decimal(row["amount"].strip())
        db.add(Invoice(owner_id=user.id,workspace_id=workspace.id,invoice_number=num,customer_name=row["customer_name"].strip(),phone=(row.get("phone") or "").strip() or None,email=(row.get("email") or "").strip() or None,amount=amount,paid_amount=paid,issue_date=date.fromisoformat(row["issue_date"].strip()),due_date=date.fromisoformat(row["due_date"].strip()),status="paid" if paid>=amount else "partially_paid" if paid>0 else "unpaid"))
    db.commit();return RedirectResponse("/",303)

@app.get("/invoices/{invoice_id}",response_class=HTMLResponse)
def invoice_detail(request:Request,invoice_id:int,tone:str="friendly",db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector","viewer")
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    msg=ai_message(invoice,tone) or build_message(invoice,tone)
    latest=db.scalar(select(PaymentLink).where(PaymentLink.invoice_id==invoice.id).order_by(PaymentLink.created_at.desc()))
    digits="".join(ch for ch in (invoice.phone or "") if ch.isdigit())
    wa=f"https://wa.me/{digits}?text={urllib.parse.quote(msg)}" if digits else None
    pay_url=str(request.base_url).rstrip("/")+"/pay/"+make_public_invoice_token(invoice.id)
    return templates.TemplateResponse("invoice_detail.html",{"request":request,"user":user,"csrf":csrf_for(request),"invoice":invoice,"message":msg,"tone":tone,"wa_url":wa,"money":money,"payment_link":latest,"pay_url":pay_url})

@app.post("/invoices/{invoice_id}/payment")
def payment(invoice_id:int,request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector"); check_csrf(request,csrf)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    result=create_payment_link(user,invoice)
    link=PaymentLink(owner_id=user.id,invoice_id=invoice.id,provider_link_id=result["id"],short_url=result["short_url"],amount_paise=int(result["amount"]),status=result.get("status","created"))
    db.add(link); audit(db,user,"payment_link.created","payment_link",link.id,{"invoice_id":invoice.id}); db.commit()
    return RedirectResponse(f"/invoices/{invoice.id}?message=Payment+link+created",303)

@app.post("/invoices/{invoice_id}/whatsapp")
def whatsapp(invoice_id:int,request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector"); check_csrf(request,csrf)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    link=db.scalar(select(PaymentLink).where(PaymentLink.invoice_id==invoice.id,PaymentLink.owner_id==user.id).order_by(PaymentLink.created_at.desc()))
    message_id=send_whatsapp_template(user,invoice,link.short_url if link else None)
    db.add(ReminderLog(owner_id=user.id,invoice_id=invoice.id,stage="manual",channel="whatsapp",status="sent",provider_message_id=message_id))
    db.commit()
    return RedirectResponse(f"/invoices/{invoice.id}?message=WhatsApp+sent",303)

@app.post("/invoices/{invoice_id}/mark-paid")
def mark_paid(invoice_id:int,request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector"); check_csrf(request,csrf)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    invoice.paid_amount=invoice.amount;invoice.status="paid"; audit(db,user,"invoice.marked_paid","invoice",invoice.id); db.commit();return RedirectResponse(f"/invoices/{invoice.id}?message=Invoice+marked+paid",303)

@app.get("/settings",response_class=HTMLResponse)
def settings_page(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin")
    return templates.TemplateResponse("settings.html",{"request":request,"user":user,"csrf":csrf_for(request),"rzp_configured":bool(decrypt(user.razorpay_key_id_enc) and decrypt(user.razorpay_key_secret_enc)),"wa_configured":bool(decrypt(user.whatsapp_access_token_enc) and user.whatsapp_phone_number_id),"wa_version":settings.whatsapp_graph_version})

@app.post("/settings")
def update_settings(request:Request,csrf:str=Form(...),company_name:str=Form(...),razorpay_key_id:str=Form(""),razorpay_key_secret:str=Form(""),razorpay_webhook_secret:str=Form(""),whatsapp_access_token:str=Form(""),whatsapp_phone_number_id:str=Form(""),whatsapp_template_name:str=Form("invoice_payment_reminder"),whatsapp_template_language:str=Form("en"),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin"); check_csrf(request,csrf)
    user.company_name=company_name.strip() or user.company_name
    if razorpay_key_id.strip(): user.razorpay_key_id_enc=encrypt(razorpay_key_id.strip())
    if razorpay_key_secret.strip(): user.razorpay_key_secret_enc=encrypt(razorpay_key_secret.strip())
    if razorpay_webhook_secret.strip(): user.razorpay_webhook_secret_enc=encrypt(razorpay_webhook_secret.strip())
    if whatsapp_access_token.strip(): user.whatsapp_access_token_enc=encrypt(whatsapp_access_token.strip())
    user.whatsapp_phone_number_id=whatsapp_phone_number_id.strip() or user.whatsapp_phone_number_id
    user.whatsapp_template_name=whatsapp_template_name.strip() or user.whatsapp_template_name
    user.whatsapp_template_language=whatsapp_template_language.strip() or user.whatsapp_template_language
    audit(db,user,"settings.updated","workspace",workspace.id)
    db.commit()
    return RedirectResponse("/settings?message=Settings+saved",303)

@app.get("/billing",response_class=HTMLResponse)
def billing(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin")
    return templates.TemplateResponse("billing.html",{"request":request,"user":user,"csrf":csrf_for(request),"plans":PLANS,"message":request.query_params.get("message",""),"subscription_url":request.query_params.get("subscription_url","")})

@app.post("/billing/subscribe")
def subscribe(request:Request,csrf:str=Form(...),plan_code:str=Form(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin",subscription=False); check_csrf(request,csrf)
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
    user.subscription_id=result.get("id");user.subscription_status=result.get("status","created");user.subscription_plan=plan_code; audit(db,user,"subscription.created","subscription",user.subscription_id,{"plan":plan_code}); db.commit()
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
    if not settings.cron_secret or request.headers.get("x-cron-secret")!=settings.cron_secret: raise HTTPException(401,"Unauthorized")
    sent=0; skipped=0
    owners=list(db.scalars(select(User).where(User.role=="owner")).all())
    for user in owners:
        workspace=workspace_for(user,db)
        token_ok=bool(decrypt(user.whatsapp_access_token_enc) and user.whatsapp_phone_number_id and settings.whatsapp_graph_version)
        if not token_ok: continue
        invoices=list(db.scalars(select(Invoice).where(Invoice.workspace_id==workspace.id,Invoice.paid_amount<Invoice.amount)).all())
        for invoice in invoices:
            d=invoice.days_overdue; stage="1d" if d>=1 and d<7 else "7d" if d>=7 and d<30 else "30d" if d>=30 else ""
            if not stage or not invoice.phone: continue
            if db.scalar(select(ReminderLog).where(ReminderLog.invoice_id==invoice.id,ReminderLog.stage==stage,ReminderLog.channel=="whatsapp")): continue
            try:
                link=db.scalar(select(PaymentLink).where(PaymentLink.invoice_id==invoice.id).order_by(PaymentLink.created_at.desc()))
                if not link and decrypt(user.razorpay_key_id_enc) and decrypt(user.razorpay_key_secret_enc):
                    result=create_payment_link(user,invoice)
                    link=PaymentLink(owner_id=user.id,invoice_id=invoice.id,provider_link_id=result["id"],short_url=result["short_url"],amount_paise=int(result["amount"]),status=result.get("status","created"))
                    db.add(link); db.flush()
                mid=send_whatsapp_template(user,invoice,link.short_url if link else None)
                db.add(ReminderLog(owner_id=user.id,invoice_id=invoice.id,stage=stage,channel="whatsapp",status="sent",provider_message_id=mid)); audit(db,user,"reminder.automated","invoice",invoice.id,{"stage":stage}); db.commit(); sent+=1
            except HTTPException:
                db.rollback(); skipped+=1
    return {"ok":True,"sent":sent,"skipped":skipped}

@app.get("/customers",response_class=HTMLResponse)
def customers_page(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector","viewer")
    invoices=list(db.scalars(select(Invoice).where(Invoice.workspace_id==workspace.id).order_by(Invoice.customer_name.asc(),Invoice.due_date.asc())).all()); groups={}
    for i in invoices:
        key=i.customer_name.strip().lower(); g=groups.setdefault(key,{"name":i.customer_name,"phone":i.phone,"email":i.email,"count":0,"outstanding":Decimal("0"),"overdue":Decimal("0")})
        g["count"]+=1; g["outstanding"]+=i.balance
        if i.days_overdue:g["overdue"]+=i.balance
    rows=[]
    for g in groups.values():
        tok=make_statement_token(workspace.id,g["name"])
        rows.append({**g,"statement_url":str(request.base_url).rstrip("/")+"/statement/"+tok})
    rows.sort(key=lambda x:x["outstanding"],reverse=True)
    return templates.TemplateResponse("customers.html",{"request":request,"user":user,"rows":rows,"money":money})

@app.get("/statement/{token}",response_class=HTMLResponse)
def public_statement(request:Request,token:str,db:Session=Depends(get_db)):
    payload=read_statement_token(token)
    if not payload or not payload.get("workspace_id") or not payload.get("customer_name"): raise HTTPException(404,"Statement not found")
    invoices=list(db.scalars(select(Invoice).where(Invoice.workspace_id==int(payload["workspace_id"]),func.lower(Invoice.customer_name)==payload["customer_name"].lower()).order_by(Invoice.due_date.asc())).all())
    workspace=db.get(Workspace,int(payload["workspace_id"]))
    if not invoices or not workspace: raise HTTPException(404,"Statement not found")
    total=sum((i.balance for i in invoices),Decimal("0")); overdue=sum((i.balance for i in invoices if i.days_overdue>0),Decimal("0"))
    return templates.TemplateResponse("statement.html",{"request":request,"workspace":workspace,"customer_name":invoices[0].customer_name,"invoices":invoices,"total":total,"overdue":overdue,"money":money})

@app.get("/team",response_class=HTMLResponse)
def team_page(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin")
    members=list(db.scalars(select(TeamMember).where(TeamMember.workspace_id==workspace.id,TeamMember.status=="active").order_by(TeamMember.created_at.asc())).all())
    member_rows=[{"membership":m,"user":db.get(User,m.user_id)} for m in members]
    invites=list(db.scalars(select(TeamInvite).where(TeamInvite.workspace_id==workspace.id,TeamInvite.status=="pending").order_by(TeamInvite.created_at.desc())).all())
    return templates.TemplateResponse("team.html",{"request":request,"user":user,"workspace":workspace,"members":member_rows,"invites":invites,"csrf":csrf_for(request),"invite_url":request.query_params.get("invite_url",""),"message":request.query_params.get("message",""),"error":request.query_params.get("error","")})

@app.post("/team/invite")
def team_invite(request:Request,csrf:str=Form(...),email:str=Form(...),role:str=Form("viewer"),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin"); check_csrf(request,csrf)
    email=email.strip().lower()
    if role not in {"admin","finance","collector","viewer"}: raise HTTPException(400,"Invalid team role")
    existing=db.scalar(select(User).where(func.lower(User.email)==email))
    if existing and existing.workspace_id==workspace.id: return RedirectResponse("/team?error=User+is+already+in+this+workspace",303)
    raw=secrets.token_urlsafe(32); invite=TeamInvite(workspace_id=workspace.id,email=email,role=role,token_digest=invite_digest(raw),status="pending")
    db.add(invite); db.flush(); audit(db,user,"team.invite_created","team_invite",invite.id,{"email":email,"role":role}); db.commit()
    url=str(request.base_url).rstrip("/")+"/invite/"+raw
    return RedirectResponse("/team?invite_url="+urllib.parse.quote(url,safe=""),303)

@app.get("/invite/{token}",response_class=HTMLResponse)
def invite_page(request:Request,token:str,db:Session=Depends(get_db)):
    invite=db.scalar(select(TeamInvite).where(TeamInvite.token_digest==invite_digest(token),TeamInvite.status=="pending"))
    if not invite: raise HTTPException(404,"Invite not found or already used")
    return templates.TemplateResponse("invite.html",{"request":request,"invite":invite,"token":token,"error":request.query_params.get("error","")})

@app.post("/invite/{token}")
def invite_accept(request:Request,token:str,password:str=Form(""),db:Session=Depends(get_db)):
    invite=db.scalar(select(TeamInvite).where(TeamInvite.token_digest==invite_digest(token),TeamInvite.status=="pending"))
    if not invite: raise HTTPException(404,"Invite not found or already used")
    workspace=db.get(Workspace,invite.workspace_id)
    if not workspace: raise HTTPException(404,"Workspace not found")
    existing=db.scalar(select(User).where(func.lower(User.email)==invite.email))
    if existing:
        if existing.workspace_id and existing.workspace_id!=workspace.id: raise HTTPException(409,"This email is already attached to another workspace.")
        existing.workspace_id=workspace.id; existing.role=invite.role
        member=db.scalar(select(TeamMember).where(TeamMember.workspace_id==workspace.id,TeamMember.user_id==existing.id))
        if not member: db.add(TeamMember(workspace_id=workspace.id,user_id=existing.id,role=invite.role,status="active"))
        member_user=existing
    else:
        if len(password)<8: return RedirectResponse("/invite/"+token+"?error=Password+must+be+at+least+8+characters",303)
        member_user=User(email=invite.email,password_hash=hash_password(password),company_name=workspace.name,workspace_id=workspace.id,role=invite.role)
        db.add(member_user); db.flush(); db.add(TeamMember(workspace_id=workspace.id,user_id=member_user.id,role=invite.role,status="active"))
    invite.status="accepted"; audit(db,member_user,"team.invite_accepted","team_invite",invite.id,{"role":invite.role}); db.commit()
    response=RedirectResponse("/",303); set_session(response,member_user.id); return response

@app.post("/team/remove/{membership_id}")
def team_remove(membership_id:int,request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin"); check_csrf(request,csrf)
    member=db.scalar(select(TeamMember).where(TeamMember.id==membership_id,TeamMember.workspace_id==workspace.id,TeamMember.status=="active"))
    if not member: raise HTTPException(404,"Member not found")
    if member.user_id==user.id: raise HTTPException(400,"You cannot remove yourself")
    target=db.get(User,member.user_id); member.status="revoked"; target.workspace_id=None; target.role="viewer"
    audit(db,user,"team.member_removed","team_member",member.id,{"user_id":target.id}); db.commit()
    return RedirectResponse("/team?message=Member+removed",303)

@app.get("/audit",response_class=HTMLResponse)
def audit_page(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin")
    logs=list(db.scalars(select(AuditLog).where(AuditLog.workspace_id==workspace.id).order_by(AuditLog.created_at.desc()).limit(150)).all())
    return templates.TemplateResponse("audit.html",{"request":request,"user":user,"logs":logs})

@app.get("/recurring",response_class=HTMLResponse)
def recurring_page(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector","viewer")
    items=list(db.scalars(select(RecurringInvoice).where(RecurringInvoice.workspace_id==workspace.id).order_by(RecurringInvoice.active.desc(),RecurringInvoice.next_issue_date.asc())).all())
    return templates.TemplateResponse("recurring.html",{"request":request,"user":user,"items":items,"csrf":csrf_for(request),"message":request.query_params.get("message","")})

@app.post("/recurring")
def recurring_create(request:Request,csrf:str=Form(...),customer_name:str=Form(...),phone:str=Form(""),email:str=Form(""),amount:str=Form(...),cadence:str=Form("monthly"),next_issue_date:str=Form(...),due_days:int=Form(7),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance"); check_csrf(request,csrf)
    if cadence not in {"weekly","monthly","quarterly","yearly"}: raise HTTPException(400,"Invalid cadence")
    amount_d=Decimal(amount)
    if amount_d<=0 or due_days<0: raise HTTPException(400,"Invalid recurring invoice values")
    item=RecurringInvoice(workspace_id=workspace.id,created_by_user_id=user.id,customer_name=customer_name.strip(),phone=phone.strip() or None,email=email.strip() or None,amount=amount_d,cadence=cadence,next_issue_date=date.fromisoformat(next_issue_date),due_days=due_days,active=True)
    db.add(item); db.flush(); audit(db,user,"recurring.created","recurring_invoice",item.id,{"customer_name":item.customer_name,"cadence":cadence}); db.commit()
    return RedirectResponse("/recurring?message=Recurring+invoice+created",303)

@app.post("/recurring/{recurring_id}/toggle")
def recurring_toggle(recurring_id:int,request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance"); check_csrf(request,csrf)
    item=db.scalar(select(RecurringInvoice).where(RecurringInvoice.id==recurring_id,RecurringInvoice.workspace_id==workspace.id))
    if not item: raise HTTPException(404,"Recurring invoice not found")
    item.active=not item.active; audit(db,user,"recurring.toggled","recurring_invoice",item.id,{"active":item.active}); db.commit()
    return RedirectResponse("/recurring",303)

@app.post("/internal/recurring")
def internal_recurring(request:Request,db:Session=Depends(get_db)):
    if not settings.cron_secret or request.headers.get("x-cron-secret")!=settings.cron_secret: raise HTTPException(401,"Unauthorized")
    generated=0
    items=list(db.scalars(select(RecurringInvoice).where(RecurringInvoice.active==True,RecurringInvoice.next_issue_date<=date.today())).all())
    for item in items:
        owner=db.get(User,item.created_by_user_id); workspace=db.get(Workspace,item.workspace_id)
        if not owner or not workspace: continue
        while item.active and item.next_issue_date<=date.today():
            number="RF-"+str(workspace.id)+"-"+str(item.id)+"-"+item.next_issue_date.strftime("%Y%m%d")
            if not db.scalar(select(Invoice).where(Invoice.invoice_number==number)):
                inv=Invoice(owner_id=owner.id,workspace_id=workspace.id,invoice_number=number,customer_name=item.customer_name,phone=item.phone,email=item.email,amount=item.amount,paid_amount=Decimal("0"),issue_date=item.next_issue_date,due_date=item.next_issue_date+timedelta(days=item.due_days),status="unpaid",notes="Generated from recurring invoice")
                db.add(inv); db.flush(); audit(db,owner,"recurring.invoice_generated","invoice",inv.id,{"recurring_id":item.id,"invoice_number":number}); generated+=1
            item.next_issue_date=advance_date(item.next_issue_date,item.cadence)
        db.commit()
    return {"ok":True,"generated":generated}

@app.get("/export/invoices.csv")
def export_invoices(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector","viewer")
    invoices=list(db.scalars(select(Invoice).where(Invoice.workspace_id==workspace.id).order_by(Invoice.due_date.asc())).all())
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(["invoice_number","customer_name","phone","email","amount","paid_amount","balance","issue_date","due_date","status","gstin","place_of_supply","tax_rate","tax_amount","tds_amount"])
    for i in invoices: writer.writerow([i.invoice_number,i.customer_name,i.phone or "",i.email or "",i.amount,i.paid_amount,i.balance,i.issue_date,i.due_date,i.status,i.gstin or "",i.place_of_supply or "",i.tax_rate or "",i.tax_amount or "",i.tds_amount or ""])
    return PlainTextResponse(output.getvalue(),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=\"recoverflow-invoices.csv\""})
