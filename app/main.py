from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import csv, io, json, urllib.parse, secrets, hashlib
from fastapi import FastAPI, Depends, Form, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from openai import OpenAI
from .config import settings
from .db import get_db, init_db
from .models import User, Invoice, PaymentLink, ReminderLog, BillingPlan, DeviceToken, Workspace, TeamMember, TeamInvite, AuditLog, RecurringInvoice
from .security import set_session, clear_session, read_session, new_csrf, encrypt, decrypt, make_api_token, read_api_token, make_public_invoice_token, read_public_invoice_token, make_statement_token, read_statement_token, make_api_token, read_api_token
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