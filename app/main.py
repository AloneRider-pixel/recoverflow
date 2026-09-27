from datetime import date, timedelta, datetime
from decimal import Decimal, InvalidOperation
import csv, io, json, urllib.parse, secrets, hashlib, hmac
from fastapi import FastAPI, Depends, Form, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from openai import OpenAI
from .config import settings
from .db import get_db, init_db, engine
from .models import User, Invoice, PaymentLink, PaymentTransaction, WebhookEvent, ReminderLog, BillingPlan, DeviceToken, Workspace, TeamMember, TeamInvite, AuditLog, RecurringInvoice, Lead, CollectionEvent
from .security import set_session, clear_session, read_session, new_csrf, encrypt, decrypt, make_api_token, read_api_token, make_public_invoice_token, read_public_invoice_token, make_statement_token, read_statement_token, make_api_token, read_api_token
from .password import hash_password, verify_password
from .integrations import create_payment_link, send_whatsapp_template, verify_webhook, merchant_keys, webhook_secret, platform_request, platform_keys, send_expo_push, standard_checkout_keys, standard_checkout_client, create_standard_checkout_order

# Initialize all application tables after model imports.
init_db()

app=FastAPI(title=settings.app_name)
templates=Jinja2Templates(directory="app/templates")

@app.middleware("http")
async def security_headers(request:Request, call_next):
    response=await call_next(request)
    response.headers.setdefault("X-Content-Type-Options","nosniff")
    response.headers.setdefault("X-Frame-Options","DENY")
    response.headers.setdefault("Referrer-Policy","strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy","camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Strict-Transport-Security","max-age=31536000; includeSubDomains")
    return response

PLANS={
    "starter":{"name":"Starter","amount":99900,"description":"Core receivables and reminders"},
    "business":{"name":"Business","amount":249900,"description":"Automation for growing teams"},
    "pro":{"name":"Pro","amount":499900,"description":"Advanced collection operations"},
}

PLAN_LIMITS={
    "starter":{"invoices":100,"members":1,"recurring":10},
    "business":{"invoices":2000,"members":5,"recurring":100},
    "pro":{"invoices":10000,"members":25,"recurring":1000},
}
TRIAL_DAYS=14

def subscription_state(user):
    if user.subscription_status in {"active","authenticated"}:
        return "active"
    if user.trial_ends_at and datetime.utcnow() < user.trial_ends_at:
        return "trial"
    return "expired"

def trial_days_left(user):
    if not user.trial_ends_at:
        return 0
    return max((user.trial_ends_at.date()-date.today()).days,0)

def access_plan(user):
    if subscription_state(user)=="active" and user.subscription_plan in PLAN_LIMITS:
        return user.subscription_plan
    return "business"

def ensure_access(user):
    if subscription_state(user)=="expired":
        raise HTTPException(402,"Your RecoverFlow trial has ended. Choose a plan in Billing to continue.")

def ensure_limit(user,workspace,kind,db,incoming=1):
    ensure_access(user)
    limit=PLAN_LIMITS[access_plan(user)].get(kind)
    if kind=="invoices":
        used=db.scalar(select(func.count(Invoice.id)).where(Invoice.workspace_id==workspace.id)) or 0
    elif kind=="members":
        used=db.scalar(select(func.count(TeamMember.id)).where(TeamMember.workspace_id==workspace.id,TeamMember.status=="active")) or 0
    elif kind=="recurring":
        used=db.scalar(select(func.count(RecurringInvoice.id)).where(RecurringInvoice.workspace_id==workspace.id)) or 0
    else:
        return
    if limit is not None and used+incoming>limit:
        raise HTTPException(402,f"{PLANS[access_plan(user)]['name']} plan limit reached for {kind}. Upgrade in Billing to continue.")

def commercial_context(user):
    return {"subscription_state":subscription_state(user),"trial_days_left":trial_days_left(user),"subscription_plan":user.subscription_plan,"trial_ends_at":user.trial_ends_at}

def credential_mode(key_id):
    if key_id.startswith("rzp_live_"): return "live"
    if key_id.startswith("rzp_test_"): return "test"
    return "unknown" if key_id else "not_configured"

def payment_security_context(user, request, db):
    invoice_key_id, invoice_secret = standard_checkout_keys(user)
    invoice_webhook_secret = decrypt(user.razorpay_webhook_secret_enc)
    platform_key_id, platform_secret = platform_keys()
    platform_webhook_secret = settings.razorpay_platform_webhook_secret
    table_check = inspect(engine)
    postgres_ok = engine.dialect.name == "postgresql"
    idempotency_ok = table_check.has_table("payment_transactions")
    replay_protection_ok = table_check.has_table("webhook_events")
    forwarded_proto = request.headers.get("x-forwarded-proto","")
    https_ok = forwarded_proto.split(",")[0].strip()=="https" or request.url.scheme=="https" or settings.public_base_url.startswith("https://")
    session_ok = bool(settings.session_secret and settings.session_secret!="dev-only-change-me" and len(settings.session_secret)>=32)
    legal_ok = all([
        settings.legal_entity_name.strip(),
        settings.support_email.strip(),
        settings.business_address.strip(),
        settings.jurisdiction.strip(),
    ])
    invoice_mode = credential_mode(invoice_key_id)
    platform_mode = credential_mode(platform_key_id)
    checks=[
        ("Database", "PostgreSQL is active for production data.", "ready" if postgres_ok else "action"),
        ("HTTPS", "Production traffic is protected by HTTPS.", "ready" if https_ok else "action"),
        ("Session security", "Strong production session secret is configured.", "ready" if session_ok else "action"),
        ("Invoice payments", f"Razorpay merchant credentials: {invoice_mode}.", "ready" if invoice_mode=="live" and bool(invoice_secret) else "action"),
        ("Invoice webhooks", "Merchant webhook secret is configured for payment events.", "ready" if bool(invoice_webhook_secret) else "action"),
        ("Payment idempotency", "Payment transaction records protect against duplicate processing.", "ready" if idempotency_ok else "action"),
        ("Webhook replay protection", "Webhook event IDs are persisted and deduplicated.", "ready" if replay_protection_ok else "action"),
        ("Subscription billing", f"RecoverFlow billing credentials: {platform_mode}.", "ready" if platform_mode=="live" and bool(platform_secret) else "action"),
        ("Subscription webhooks", "Platform webhook secret is configured.", "ready" if bool(platform_webhook_secret) else "action"),
        ("Automation", "Cron secret is configured for internal jobs.", "ready" if settings.cron_secret else "action"),
        ("Legal identity", "Legal entity, support email, address and jurisdiction are configured.", "ready" if legal_ok else "action"),
        ("WhatsApp", "Workspace WhatsApp integration is configured.", "ready" if decrypt(user.whatsapp_access_token_enc) and user.whatsapp_phone_number_id and settings.whatsapp_graph_version else "optional"),
    ]
    production_ready=all(state=="ready" for _,_,state in checks if state!="optional")
    return {
        "invoice_mode":invoice_mode,
        "invoice_configured":bool(invoice_secret and invoice_key_id),
        "invoice_webhook_configured":bool(invoice_webhook_secret),
        "platform_mode":platform_mode,
        "platform_configured":bool(platform_secret and platform_key_id),
        "platform_webhook_configured":bool(platform_webhook_secret),
        "production_ready":production_ready,
        "checks":checks,
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

def collection_queue(invoices, events=None):
    events=events or []
    latest_state={}
    latest_any={}
    for event in events:
        if event.invoice_id not in latest_any:
            latest_any[event.invoice_id]=event
        if event.event_type in {"promise","dispute","payment_claimed"} and event.invoice_id not in latest_state:
            latest_state[event.invoice_id]=event

    open_invoices=[i for i in invoices if i.balance>0]
    max_balance=max((i.balance for i in open_invoices), default=Decimal("1"))
    rows=[]
    today=date.today()

    for invoice in open_invoices:
        state=latest_state.get(invoice.id)
        last_any=latest_any.get(invoice.id)
        promise_broken=bool(
            state and state.event_type=="promise" and state.promised_date and state.promised_date < today
        )
        dispute=bool(state and state.event_type=="dispute")
        payment_claimed=bool(state and state.event_type=="payment_claimed")

        overdue=invoice.days_overdue
        balance_weight=(invoice.balance/max_balance)*Decimal("30")
        score=Decimal("10")+Decimal(str(min(overdue,60)))*Decimal("0.7")+balance_weight
        if promise_broken: score+=Decimal("24")
        elif dispute: score+=Decimal("18")
        elif payment_claimed: score+=Decimal("12")
        score=int(min(score,100))

        if promise_broken:
            priority="Critical"; action="Broken promise — follow up today"
        elif dispute:
            priority="High"; action="Resolve dispute before chasing payment"
        elif payment_claimed:
            priority="High"; action="Verify payment claim"
        elif overdue>=30:
            priority="Critical"; action="Call + payment link"
        elif overdue>=8:
            priority="High"; action="WhatsApp + payment link"
        elif overdue>=1:
            priority="Medium"; action="Send overdue reminder"
        elif invoice.due_date==today:
            priority="Medium"; action="Due today — request confirmation"
        elif (invoice.due_date-today).days<=3:
            priority="Low"; action="Pre-due reminder"
        else:
            priority="Low"; action="Monitor"

        if overdue:
            due_label=f"{overdue}d overdue"
        else:
            delta=(invoice.due_date-today).days
            due_label="Due today" if delta==0 else f"Due in {delta}d"

        rows.append({
            "invoice":invoice,
            "score":score,
            "priority":priority,
            "action":action,
            "due_label":due_label,
            "state_event":state,
            "last_event":last_any,
            "last_contact_at": last_any.created_at if last_any and last_any.event_type=="contact" else None,
            "promise_broken":promise_broken,
        })

    rows.sort(key=lambda r:(-r["score"], -float(r["invoice"].balance), r["invoice"].due_date))
    return rows

def parse_amount_from_text(text_value, default_amount):
    import re
    matches=re.findall(r'(?i)(?:₹\\s*)?([0-9][0-9,]*(?:\\.[0-9]+)?)\\s*(lakh|lac|l|crore|cr|k)?', text_value or "")
    if not matches:
        return Decimal(str(default_amount))
    raw, unit=matches[0]
    try:
        value=Decimal(raw.replace(",",""))
    except InvalidOperation:
        return Decimal(str(default_amount))
    unit=unit.lower()
    if unit in {"lakh","lac","l"}: value*=Decimal("100000")
    elif unit in {"crore","cr"}: value*=Decimal("10000000")
    elif unit=="k": value*=Decimal("1000")
    return value

def parse_date_from_text(text_value):
    import re
    from datetime import datetime as _dt
    today=date.today()
    text_lower=(text_value or "").lower()
    named={
        "today":today,
        "tomorrow":today+timedelta(days=1),
        "day after tomorrow":today+timedelta(days=2),
        "monday":None,"tuesday":None,"wednesday":None,"thursday":None,"friday":None,"saturday":None,"sunday":None,
    }
    for word,target in named.items():
        if word in text_lower and target:
            return target
    for fmt in ("%d/%m/%Y","%d-%m-%Y","%Y-%m-%d","%d %b %Y","%d %B %Y"):
        m=re.search(r'\\b\\d{1,2}[ /-](?:\\d{1,2}|[A-Za-z]{3,9})[ /-]\\d{2,4}\\b', text_value or "")
        if not m: continue
        try: return _dt.strptime(m.group(0),fmt).date()
        except ValueError: pass
    weekday_map={d:i for i,d in enumerate(["monday","tuesday","wednesday","thursday","friday","saturday","sunday"])}
    for word,idx in weekday_map.items():
        if word in text_lower:
            delta=(idx-today.weekday())%7
            if delta==0: delta=7
            return today+timedelta(days=delta)
    return None

def analyze_collection_reply(invoice, reply_text):
    text_value=(reply_text or "").strip()
    if not text_value:
        raise HTTPException(400,"Customer reply is required")

    if settings.openai_api_key:
        try:
            client=OpenAI(api_key=settings.openai_api_key)
            prompt=f"""Classify this Indian B2B customer collection reply. Return JSON only with keys:
event_type: one of contact, promise, dispute, payment_claimed, note
promised_date: YYYY-MM-DD or null
promised_amount: number or null
confidence: number from 0 to 1
reason: max 120 characters

Invoice balance: {invoice.balance}
Today: {date.today().isoformat()}
Customer reply: {text_value[:3000]}

Interpret phrases such as Friday, tomorrow, next week, paid, already transferred, TDS, GST, invoice issue, will pay, payment done. Do not invent a date when ambiguous. Do not infer payment completion without the customer actually claiming payment."""
            result=client.chat.completions.create(
                model=settings.openai_model,
                response_format={"type":"json_object"},
                messages=[{"role":"user","content":prompt}],
                temperature=0.0,
            )
            data=json.loads(result.choices[0].message.content or "{}")
            event_type=str(data.get("event_type","note")).lower().strip()
            if event_type not in {"contact","promise","dispute","payment_claimed","note"}:
                event_type="note"
            promise_date=None
            if data.get("promised_date"):
                try: promise_date=date.fromisoformat(str(data["promised_date"]))
                except ValueError: promise_date=None
            promise_amount=None
            if data.get("promised_amount") is not None:
                try: promise_amount=Decimal(str(data["promised_amount"]))
                except InvalidOperation: promise_amount=None
            confidence=Decimal(str(data.get("confidence",0)))
            confidence=max(Decimal("0"),min(confidence,Decimal("1")))
            if event_type=="promise" and promise_amount is None:
                promise_amount=invoice.balance
            return {
                "event_type":event_type,
                "promised_date":promise_date,
                "promised_amount":promise_amount,
                "confidence":float(confidence),
                "reason":str(data.get("reason","")).strip()[:120] or "Classified from customer reply.",
                "source":"ai",
            }
        except Exception:
            pass

    lower=text_value.lower()
    dispute_terms=("gst","tds","wrong invoice","invoice issue","incorrect","dispute","not received","credit note")
    paid_terms=("paid","payment done","transferred","sent the payment","already paid","payment has been made")
    promise_terms=("will pay","pay on","pay by","paying on","payment on","payment by","friday","tomorrow","next week","next monday")
    if any(term in lower for term in paid_terms):
        event_type="payment_claimed"
    elif any(term in lower for term in dispute_terms):
        event_type="dispute"
    elif any(term in lower for term in promise_terms):
        event_type="promise"
    else:
        event_type="note"
    pdate=parse_date_from_text(text_value) if event_type=="promise" else None
    pamount=parse_amount_from_text(text_value,invoice.balance) if event_type=="promise" else None
    return {
        "event_type":event_type,
        "promised_date":pdate,
        "promised_amount":pamount,
        "confidence":0.62 if event_type!="note" else 0.35,
        "reason":"Rule-based fallback; confirm the extracted outcome before saving.",
        "source":"rules",
    }

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

def require_role(request,db,*allowed,subscription=True):
    user=session_user(request,db)
    if not user: raise HTTPException(status_code=303,headers={"Location":"/login"})
    if user.role not in allowed: raise HTTPException(403,"Your role does not have access to this action.")
    if subscription and subscription_state(user)=="expired":
        raise HTTPException(status_code=303,headers={"Location":"/billing?message=Your+14-day+trial+has+ended.+Choose+a+plan+to+continue."})
    return user,workspace_for(user,db)

def api_require_role(request,db,*allowed,subscription=True):
    user=api_user(request,db)
    if user.role not in allowed: raise HTTPException(403,"Your role does not have access to this action.")
    if subscription: ensure_access(user)
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
    user=User(email=email,password_hash=hash_password(password),company_name=company_name,role="owner",subscription_status="trial",trial_started_at=datetime.utcnow(),trial_ends_at=datetime.utcnow()+timedelta(days=TRIAL_DAYS))
    db.add(user); db.flush()
    workspace=Workspace(name=company_name); db.add(workspace); db.flush()
    user.workspace_id=workspace.id; db.add(TeamMember(workspace_id=workspace.id,user_id=user.id,role="owner",status="active"))
    audit(db,user,"workspace.created","workspace",workspace.id,{"company_name":company_name}); db.commit()
    return {"access_token":make_api_token(user.id),"token_type":"bearer","expires_in":60*60*24*30,"workspace_id":workspace.id,"role":user.role}

@app.get("/api/v1/me")
def api_me(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); workspace=workspace_for(user,db); db.commit()
    return {"id":user.id,"email":user.email,"company_name":user.company_name,"workspace_id":workspace.id,"role":user.role,"subscription_status":user.subscription_status,"subscription_plan":user.subscription_plan,"subscription_state":subscription_state(user),"trial_days_left":trial_days_left(user)}

@app.get("/api/v1/dashboard")
def api_dashboard(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); ensure_access(user); workspace=workspace_for(user,db)
    invoices=list(db.scalars(select(Invoice).where(Invoice.workspace_id==workspace.id).order_by(Invoice.due_date.asc())).all())
    outstanding=sum((i.balance for i in invoices),Decimal("0")); overdue=sum((i.balance for i in invoices if i.days_overdue>0),Decimal("0")); due_today=sum((i.balance for i in invoices if i.balance>0 and i.due_date==date.today()),Decimal("0")); paid=sum((i.paid_amount for i in invoices),Decimal("0")); invoiced=sum((i.amount for i in invoices),Decimal("0"))
    rate=(paid/invoiced*Decimal("100")) if invoiced else Decimal("0")
    return {"outstanding":str(outstanding),"overdue":str(overdue),"due_today":str(due_today),"paid":str(paid),"invoice_count":len(invoices),"customer_count":len({i.customer_name for i in invoices}),"collection_rate":str(rate.quantize(Decimal("0.1")))}


@app.get("/api/v1/collections")
def api_collections(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); ensure_access(user); workspace=workspace_for(user,db)
    invoices=list(db.scalars(select(Invoice).where(Invoice.workspace_id==workspace.id,Invoice.paid_amount<Invoice.amount)).all())
    events=list(db.scalars(select(CollectionEvent).where(CollectionEvent.workspace_id==workspace.id).order_by(CollectionEvent.created_at.desc())).all())
    rows=collection_queue(invoices,events)
    return [{
        "invoice_id":r["invoice"].id,
        "invoice_number":r["invoice"].invoice_number,
        "customer_name":r["invoice"].customer_name,
        "balance":str(r["invoice"].balance),
        "days_overdue":r["invoice"].days_overdue,
        "due_date":r["invoice"].due_date.isoformat(),
        "priority":r["priority"],
        "recovery_score":r["score"],
        "recommended_action":r["action"],
        "last_event":r["last_event"].event_type if r["last_event"] else None,
        "promise_date":r["state_event"].promised_date.isoformat() if r["state_event"] and r["state_event"].promised_date else None,
        "promise_amount":str(r["state_event"].promised_amount) if r["state_event"] and r["state_event"].promised_amount is not None else None,
    } for r in rows[:100]]

@app.get("/api/v1/invoices/{invoice_id}/collection-events")
def api_collection_events(invoice_id:int,request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); ensure_access(user); workspace=workspace_for(user,db)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    events=list(db.scalars(select(CollectionEvent).where(CollectionEvent.invoice_id==invoice.id,CollectionEvent.workspace_id==workspace.id).order_by(CollectionEvent.created_at.desc()).limit(50)).all())
    return [{
        "id":e.id,"event_type":e.event_type,"channel":e.channel,"body":e.body,
        "promised_date":e.promised_date.isoformat() if e.promised_date else None,
        "promised_amount":str(e.promised_amount) if e.promised_amount is not None else None,
        "created_at":e.created_at.isoformat() if e.created_at else None,
    } for e in events]

@app.post("/api/v1/invoices/{invoice_id}/collection-events")
async def api_create_collection_event(invoice_id:int,request:Request,db:Session=Depends(get_db)):
    user,workspace=api_require_role(request,db,"owner","admin","finance","collector")
    try: data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    event_type=str(data.get("event_type","note")).strip().lower()
    if event_type not in {"contact","promise","dispute","payment_claimed","note"}:
        raise HTTPException(400,"Invalid collection event")
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    promised_date=None; promised_amount=None
    if event_type=="promise":
        try:
            promised_date=date.fromisoformat(str(data.get("promised_date","")))
            promised_amount=Decimal(str(data.get("promised_amount") or invoice.balance))
        except (ValueError,InvalidOperation):
            raise HTTPException(400,"Promise date and amount are required")
        if promised_amount<=0: raise HTTPException(400,"Promise amount must be positive")
    event=CollectionEvent(
        workspace_id=workspace.id, invoice_id=invoice.id, user_id=user.id,
        event_type=event_type, channel=str(data.get("channel","manual")).strip()[:30] or "manual",
        body=str(data.get("body","")).strip()[:2000] or None,
        promised_date=promised_date, promised_amount=promised_amount
    )
    db.add(event)
    audit(db,user,f"collection.{event_type}","invoice",invoice.id,{
        "channel":event.channel,
        "promised_date":promised_date.isoformat() if promised_date else None,
        "promised_amount":str(promised_amount) if promised_amount is not None else None,
    })
    db.commit()
    return {"ok":True,"id":event.id}


@app.post("/api/v1/collection-intelligence/analyze")
async def api_analyze_collection_reply(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db); ensure_access(user); workspace=workspace_for(user,db)
    try: data=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    invoice_id=int(data.get("invoice_id",0) or 0)
    reply=str(data.get("reply","")).strip()
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    result=analyze_collection_reply(invoice,reply)
    return {
        "ok":True,
        "event_type":result["event_type"],
        "promised_date":result["promised_date"].isoformat() if result["promised_date"] else None,
        "promised_amount":str(result["promised_amount"]) if result["promised_amount"] is not None else None,
        "confidence":result["confidence"],
        "reason":result["reason"],
        "source":result["source"],
    }

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
    ensure_limit(user,workspace,"members",db)
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
    ensure_limit(user,workspace,"recurring",db)
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
    ensure_limit(user,workspace,"invoices",db)
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
    balance_paise=int(invoice.balance*100)
    existing=db.scalar(
        select(PaymentLink)
        .where(
            PaymentLink.invoice_id==invoice.id,
            PaymentLink.owner_id==user.id,
            PaymentLink.amount_paise==balance_paise,
            PaymentLink.status.notin_(["paid","cancelled","expired"]),
        )
        .order_by(PaymentLink.created_at.desc())
    )
    if existing:
        return {"ok":True,"url":existing.short_url,"status":existing.status,"reused":True}
    result=create_payment_link(user,invoice)
    link=PaymentLink(owner_id=user.id,invoice_id=invoice.id,provider_link_id=result["id"],short_url=result["short_url"],amount_paise=int(result["amount"]),status=result.get("status","created"))
    db.add(link); db.flush(); audit(db,user,"payment_link.created","payment_link",link.id,{"invoice_id":invoice.id}); db.commit()
    return {"ok":True,"url":link.short_url,"status":link.status,"reused":False}

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
    return templates.TemplateResponse(
        "pay.html",
        {
            "request":request,
            "token":token,
            "invoice":invoice,
            "payment_link":link,
            "money":money,
            "company_name":owner.company_name if owner else "Business",
        },
    )


@app.post("/api/create-order")
async def create_standard_checkout_order_api(request:Request,db:Session=Depends(get_db)):
    try:
        data=await request.json()
    except Exception:
        raise HTTPException(400,"Invalid JSON")

    token=str(data.get("token","")).strip()
    payload=read_public_invoice_token(token)
    if not payload or not payload.get("invoice_id"):
        raise HTTPException(404,"Payment page not found")

    invoice=db.scalar(
        select(Invoice)
        .where(Invoice.id==int(payload["invoice_id"]))
        .with_for_update()
    )
    if not invoice or invoice.balance<=0:
        raise HTTPException(404,"Payment page not found")

    owner=db.get(User,invoice.owner_id)
    if not owner:
        raise HTTPException(404,"Payment owner not found")

    amount_paise=int(invoice.balance*100)
    if amount_paise < 100:
        raise HTTPException(400,"Razorpay Checkout requires an amount of at least ₹1.")

    existing=db.scalar(
        select(PaymentTransaction)
        .where(
            PaymentTransaction.invoice_id==invoice.id,
            PaymentTransaction.owner_id==owner.id,
            PaymentTransaction.amount_paise==amount_paise,
            PaymentTransaction.status.in_([ "creating", "created", "verification_pending" ]),
        )
        .order_by(PaymentTransaction.created_at.desc())
    )
    if existing and existing.razorpay_order_id:
        return {
            "order_id":existing.razorpay_order_id,
            "amount":existing.amount_paise,
            "currency":existing.currency,
            "key_id":standard_checkout_keys(owner)[0],
        }
    if existing and existing.status=="creating":
        raise HTTPException(409,"A secure payment order is already being prepared. Please wait a moment and try again.")

    tx=PaymentTransaction(
        workspace_id=owner.workspace_id,
        owner_id=owner.id,
        invoice_id=invoice.id,
        amount_paise=amount_paise,
        currency="INR",
        status="creating",
    )
    db.add(tx)
    db.flush()

    try:
        result=create_standard_checkout_order(owner,invoice)
        order_id=str(result["id"])
        tx.razorpay_order_id=order_id
        tx.status="created"
        db.commit()
    except HTTPException as exc:
        db.rollback()
        raise exc
    except Exception as exc:
        db.rollback()
        message=str(exc)
        if "authentication" in message.lower() or "unauthorized" in message.lower():
            raise HTTPException(401,"Razorpay authentication failed. Check the configured API credentials.")
        raise HTTPException(500,"Unable to create Razorpay payment order.")

    return {
        "order_id":order_id,
        "amount":int(result["amount"]),
        "currency":result["currency"],
        "key_id":standard_checkout_keys(owner)[0],
    }


@app.post("/api/verify-payment")
async def verify_standard_checkout_payment(request:Request,db:Session=Depends(get_db)):
    try:
        data=await request.json()
    except Exception:
        raise HTTPException(400,"Invalid JSON")

    token=str(data.get("token","")).strip()
    order_id=str(data.get("razorpay_order_id","")).strip()
    payment_id=str(data.get("razorpay_payment_id","")).strip()
    signature=str(data.get("razorpay_signature","")).strip()

    if not token or not order_id or not payment_id or not signature:
        raise HTTPException(400,"Missing payment verification fields")

    payload=read_public_invoice_token(token)
    if not payload or not payload.get("invoice_id"):
        raise HTTPException(404,"Payment page not found")

    invoice=db.scalar(select(Invoice).where(Invoice.id==int(payload["invoice_id"])))
    if not invoice:
        raise HTTPException(404,"Invoice not found")

    owner=db.get(User,invoice.owner_id)
    if not owner:
        raise HTTPException(404,"Payment owner not found")

    tx=db.scalar(
        select(PaymentTransaction).where(
            PaymentTransaction.razorpay_order_id==order_id,
            PaymentTransaction.invoice_id==invoice.id,
            PaymentTransaction.owner_id==owner.id,
        )
    )
    if not tx:
        raise HTTPException(400,"Unknown checkout order. Start a new payment from the invoice page.")

    try:
        client,key_id,key_secret=standard_checkout_client(owner)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500,"Unable to initialize Razorpay verification.")

    generated_signature=hmac.new(
        key_secret.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(generated_signature,signature):
        raise HTTPException(400,"Payment signature verification failed")

    if tx.status=="paid" and tx.razorpay_payment_id==payment_id:
        return {
            "success":True,
            "invoice_id":invoice.id,
            "invoice_number":invoice.invoice_number,
            "payment_id":payment_id,
            "order_id":order_id,
            "idempotent":True,
        }

    try:
        order=client.order.fetch(order_id)
        notes=order.get("notes") or {}
        if str(notes.get("recoverflow_invoice_id","")) != str(invoice.id):
            raise HTTPException(400,"Order does not belong to this invoice")
        if int(order.get("amount",0)) != tx.amount_paise or order.get("currency") != tx.currency:
            raise HTTPException(400,"Order amount does not match the recorded transaction")

        payment=client.payment.fetch(payment_id)
        if payment.get("order_id") != order_id:
            raise HTTPException(400,"Payment does not belong to this order")
        if payment.get("status") != "captured":
            raise HTTPException(400,"Payment signature is valid, but the payment is not captured yet.")
        if int(payment.get("amount",0)) != tx.amount_paise:
            raise HTTPException(400,"Captured payment amount does not match the recorded transaction")
    except HTTPException:
        raise
    except Exception as exc:
        message=str(exc)
        if "authentication" in message.lower() or "unauthorized" in message.lower():
            raise HTTPException(401,"Razorpay authentication failed. Check the configured API credentials.")
        raise HTTPException(502,"Unable to confirm Razorpay payment.")

    payment_amount=Decimal(tx.amount_paise)/Decimal(100)
    remaining=invoice.amount-invoice.paid_amount
    if remaining < Decimal("0"):
        remaining=Decimal("0")
    if tx.status!="paid":
        if payment_amount > remaining:
            raise HTTPException(409,"Invoice balance changed. This payment order can no longer be applied safely.")
        invoice.paid_amount += payment_amount
        invoice.status="paid" if invoice.paid_amount>=invoice.amount else "partially_paid"
        tx.razorpay_payment_id=payment_id
        tx.status="paid"
        tx.captured_at=datetime.utcnow()
        audit(db,owner,"invoice.paid_via_checkout","invoice",invoice.id,{"order_id":order_id,"payment_id":payment_id,"amount_paise":tx.amount_paise})
    db.commit()

    return {
        "success":True,
        "invoice_id":invoice.id,
        "invoice_number":invoice.invoice_number,
        "payment_id":payment_id,
        "order_id":order_id,
        "idempotent":False,
    }

@app.post("/api/v1/invoices/{invoice_id}/mark-paid")
def api_mark_paid(invoice_id:int,request:Request,db:Session=Depends(get_db)):
    user,workspace=api_require_role(request,db,"owner","admin","finance","collector")
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    invoice.paid_amount=invoice.amount; invoice.status="paid"; audit(db,user,"invoice.marked_paid","invoice",invoice.id); db.commit()
    tokens=list(db.scalars(select(DeviceToken).where(DeviceToken.owner_id==user.id,DeviceToken.active==True)).all())
    send_expo_push([t.token for t in tokens],"Payment received","Invoice "+invoice.invoice_number+" is marked paid.",{"screen":"invoices","invoiceId":invoice.id})
    return {"ok":True,"id":invoice.id,"status":invoice.status}

@app.get("/onboarding",response_class=HTMLResponse)
def onboarding(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector","viewer",subscription=False)
    return templates.TemplateResponse("onboarding.html",{"request":request,"user":user,"workspace":workspace,"csrf":csrf_for(request),**commercial_context(user)})

@app.get("/privacy",response_class=HTMLResponse)
def privacy_page(request:Request):
    return templates.TemplateResponse("legal.html",{"request":request,"page":"privacy","title":"Privacy Policy · RecoverFlow","legal_entity_name":settings.legal_entity_name,"support_email":settings.support_email,"business_address":settings.business_address,"jurisdiction":settings.jurisdiction})

@app.get("/terms",response_class=HTMLResponse)
def terms_page(request:Request):
    return templates.TemplateResponse("legal.html",{"request":request,"page":"terms","title":"Terms of Service · RecoverFlow","legal_entity_name":settings.legal_entity_name,"support_email":settings.support_email,"business_address":settings.business_address,"jurisdiction":settings.jurisdiction})

@app.get("/refunds",response_class=HTMLResponse)
def refunds_page(request:Request):
    return templates.TemplateResponse("legal.html",{"request":request,"page":"refunds","title":"Refund & Cancellation Policy · RecoverFlow","legal_entity_name":settings.legal_entity_name,"support_email":settings.support_email,"business_address":settings.business_address,"jurisdiction":settings.jurisdiction})

@app.get("/request-demo",response_class=HTMLResponse)
def request_demo_page(request:Request):
    return templates.TemplateResponse("request_demo.html",{"request":request,"error":request.query_params.get("error",""),"sent":request.query_params.get("sent","")})

@app.post("/request-demo")
def request_demo(name:str=Form(...),company_name:str=Form(...),email:str=Form(...),phone:str=Form(""),message:str=Form(""),db:Session=Depends(get_db)):
    name=name.strip(); company_name=company_name.strip(); email=email.strip().lower(); phone=phone.strip() or None; message=message.strip() or None
    if not name or not company_name or not email:
        return RedirectResponse("/request-demo?error=Please+complete+the+required+fields",303)
    db.add(Lead(name=name,company_name=company_name,email=email,phone=phone,message=message,status="new")); db.commit()
    return RedirectResponse("/request-demo?sent=1",303)

@app.get("/leads",response_class=HTMLResponse)
def leads_page(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin",subscription=False)
    leads=list(db.scalars(select(Lead).order_by(Lead.created_at.desc()).limit(200)).all())
    return templates.TemplateResponse("leads.html",{"request":request,"user":user,"leads":leads,**commercial_context(user)})

@app.get("/launch",response_class=HTMLResponse)
def launch_page(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin",subscription=False)
    security=payment_security_context(user,request,db)
    checks=security["checks"]
    leads_count=db.scalar(select(func.count(Lead.id))) or 0
    return templates.TemplateResponse(
        "launch.html",
        {
            "request":request,
            "user":user,
            "workspace":workspace,
            "checks":checks,
            "production_ready":security["production_ready"],
            "payment_security":security,
            "leads_count":leads_count,
            "sales_email":settings.sales_email,
            "public_base_url":settings.public_base_url or str(request.base_url).rstrip("/"),
            **commercial_context(user),
        },
    )

@app.get("/leads/export.csv")
def export_leads(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin",subscription=False)
    leads=list(db.scalars(select(Lead).order_by(Lead.created_at.desc())).all())
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(["name","company_name","email","phone","message","status","created_at"])
    for lead in leads:
        writer.writerow([lead.name,lead.company_name,lead.email,lead.phone or "",lead.message or "",lead.status,lead.created_at])
    return PlainTextResponse(output.getvalue(),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=\"recoverflow-leads.csv\""})

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
    user=User(email=email,password_hash=hash_password(password),company_name=company_name,role="owner",subscription_status="trial",trial_started_at=datetime.utcnow(),trial_ends_at=datetime.utcnow()+timedelta(days=TRIAL_DAYS))
    db.add(user); db.flush()
    workspace=Workspace(name=company_name); db.add(workspace); db.flush()
    user.workspace_id=workspace.id; db.add(TeamMember(workspace_id=workspace.id,user_id=user.id,role="owner",status="active"))
    audit(db,user,"workspace.created","workspace",workspace.id,{"company_name":company_name}); db.commit()
    response=RedirectResponse("/onboarding",303); set_session(response,user.id); return response
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
    return templates.TemplateResponse("dashboard.html",{"request":request,"user":user,"csrf":csrf_for(request),"invoices":invoices,"outstanding":outstanding,"overdue":overdue,"due_today":due_today,"customers":customers,"money":money,"collection_rate":collection_rate,"aging":aging,"paid":paid,"invoiced":invoiced,"demo_added":request.query_params.get("demo_added"),"message":request.query_params.get("message"),**commercial_context(user)})

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
    return templates.TemplateResponse("invoice_form.html",{"request":request,"user":user,"csrf":csrf_for(request),"today":date.today(),**commercial_context(user)})

@app.post("/invoices/new")
def create_invoice(request:Request,csrf:str=Form(...),invoice_number:str=Form(...),customer_name:str=Form(...),phone:str=Form(""),email:str=Form(""),amount:str=Form(...),paid_amount:str=Form("0"),issue_date:str=Form(...),due_date:str=Form(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector"); check_csrf(request,csrf); ensure_limit(user,workspace,"invoices",db)
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
    rows=list(reader)
    ensure_limit(user,workspace,"invoices",db,len(rows))
    for row in rows:
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
    latest=db.scalar(select(PaymentLink).where(PaymentLink.invoice_id==invoice.id,PaymentLink.owner_id==user.id).order_by(PaymentLink.created_at.desc()))
    digits="".join(ch for ch in (invoice.phone or "") if ch.isdigit())
    wa=f"https://wa.me/{digits}?text={urllib.parse.quote(msg)}" if digits else None
    pay_url=str(request.base_url).rstrip("/")+"/pay/"+make_public_invoice_token(invoice.id)
    return templates.TemplateResponse("invoice_detail.html",{"request":request,"user":user,"csrf":csrf_for(request),"invoice":invoice,"message":msg,"tone":tone,"wa_url":wa,"money":money,"payment_link":latest,"pay_url":pay_url,**commercial_context(user)})

@app.post("/invoices/{invoice_id}/payment")
def payment(invoice_id:int,request:Request,csrf:str=Form(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector"); check_csrf(request,csrf)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    balance_paise=int(invoice.balance*100)
    existing=db.scalar(
        select(PaymentLink)
        .where(
            PaymentLink.invoice_id==invoice.id,
            PaymentLink.owner_id==user.id,
            PaymentLink.amount_paise==balance_paise,
            PaymentLink.status.notin_(["paid","cancelled","expired"]),
        )
        .order_by(PaymentLink.created_at.desc())
    )
    if existing:
        return RedirectResponse(f"/invoices/{invoice.id}?message=Payment+link+ready",303)
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
    user,workspace=require_role(request,db,"owner","admin",subscription=False)
    return templates.TemplateResponse("settings.html",{"request":request,"user":user,"csrf":csrf_for(request),"rzp_configured":bool(decrypt(user.razorpay_key_id_enc) and decrypt(user.razorpay_key_secret_enc)),"wa_configured":bool(decrypt(user.whatsapp_access_token_enc) and user.whatsapp_phone_number_id),"wa_version":settings.whatsapp_graph_version,**commercial_context(user)})

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
    user,workspace=require_role(request,db,"owner","admin",subscription=False)
    return templates.TemplateResponse("billing.html",{"request":request,"user":user,"csrf":csrf_for(request),"plans":PLANS,"plan_limits":PLAN_LIMITS,"message":request.query_params.get("message",""),"subscription_url":request.query_params.get("subscription_url",""),"payment_security":payment_security_context(user,request,db),**commercial_context(user)})

@app.post("/billing/subscribe")
def subscribe(request:Request,csrf:str=Form(...),plan_code:str=Form(...),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin",subscription=False); check_csrf(request,csrf)
    if plan_code not in PLANS: raise HTTPException(400,"Unknown plan")
    if not settings.razorpay_platform_key_id or not settings.razorpay_platform_key_secret:
        return RedirectResponse("/billing?message=RecoverFlow+subscription+billing+is+not+configured.+Add+dedicated+Razorpay+billing+credentials+in+Render+Environment.",303)

    # Do not create another provider subscription when one already exists.
    if user.subscription_id and user.subscription_status in {"created","authenticated","active","pending","halted"}:
        try:
            existing_sub=platform_request("GET",f"/subscriptions/{user.subscription_id}")
            existing_status=existing_sub.get("status",user.subscription_status)
            user.subscription_status=existing_status
            db.commit()
            if existing_sub.get("short_url"):
                return RedirectResponse(existing_sub["short_url"],303)
            if existing_status in {"active","authenticated"}:
                return RedirectResponse("/billing?message=Your+subscription+is+already+active.",303)
        except HTTPException:
            if user.subscription_status in {"active","authenticated"}:
                return RedirectResponse("/billing?message=Your+subscription+is+already+active.",303)

    meta=PLANS[plan_code]
    plan=db.get(BillingPlan,plan_code)
    if not plan:
        plan=BillingPlan(code=plan_code,name=meta["name"],amount_paise=meta["amount"],period="monthly",interval=1)
        db.add(plan); db.commit()
    if not plan.razorpay_plan_id:
        result=platform_request("POST","/plans",{"period":"monthly","interval":1,"item":{"name":meta["name"],"amount":meta["amount"],"currency":"INR","description":meta["description"]}})
        plan.razorpay_plan_id=result["id"]; db.commit()

    result=platform_request("POST","/subscriptions",{"plan_id":plan.razorpay_plan_id,"total_count":120,"quantity":1,"customer_notify":1,"notes":{"recoverflow_user_id":str(user.id),"plan":plan_code}})
    user.subscription_id=result.get("id")
    user.subscription_status=result.get("status","created")
    user.subscription_plan=plan_code
    audit(db,user,"subscription.created","subscription",user.subscription_id,{"plan":plan_code})
    db.commit()
    short_url=result.get("short_url")
    if short_url: return RedirectResponse(short_url,303)
    return RedirectResponse("/billing?message=Subscription+created",303)

@app.post("/webhooks/razorpay")
async def razorpay_webhook(request:Request,db:Session=Depends(get_db)):
    raw=await request.body()
    signature=request.headers.get("x-razorpay-signature","")
    event_id=request.headers.get("x-razorpay-event-id","").strip() or hashlib.sha256(raw).hexdigest()

    try:
        payload=json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400,"Invalid JSON")

    event_name=str(payload.get("event","")).strip()
    if not event_name:
        raise HTTPException(400,"Missing webhook event")

    # Resolve the correct merchant secret before accepting the event.
    user=None
    workspace_id=None
    invoice_id=None
    if event_name.startswith("payment_link."):
        entity=((payload.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
        notes=entity.get("notes") or {}
        user_id=int(notes.get("recoverflow_user_id",0) or 0)
        invoice_id=int(notes.get("recoverflow_invoice_id",0) or 0)
        user=db.get(User,user_id)
        secret=webhook_secret(user) if user else ""
    elif event_name.startswith("subscription."):
        entity=((payload.get("payload") or {}).get("subscription") or {}).get("entity") or {}
        user=db.scalar(select(User).where(User.subscription_id==entity.get("id")))
        secret=settings.razorpay_platform_webhook_secret
    elif event_name.startswith("payment.") or event_name=="order.paid":
        payment_entity=((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
        order_entity=((payload.get("payload") or {}).get("order") or {}).get("entity") or {}
        order_id=str(payment_entity.get("order_id") or order_entity.get("id") or "")
        tx=db.scalar(select(PaymentTransaction).where(PaymentTransaction.razorpay_order_id==order_id)) if order_id else None
        user=db.get(User,tx.owner_id) if tx else None
        secret=webhook_secret(user) if user else ""
        invoice_id=tx.invoice_id if tx else None
    else:
        return JSONResponse({"ok":True,"ignored":True})

    if not secret or not verify_webhook(raw,signature,secret):
        raise HTTPException(401,"Invalid webhook signature")

    try:
        with db.begin_nested():
            existing=db.scalar(select(WebhookEvent).where(WebhookEvent.provider_event_id==event_id))
            if existing:
                if existing.status in {"processed","ignored","received"}:
                    return JSONResponse({"ok":True,"duplicate":True})
            else:
                existing=WebhookEvent(
                    provider_event_id=event_id,
                    event=event_name,
                    status="received",
                    workspace_id=user.workspace_id if user else workspace_id,
                    user_id=user.id if user else None,
                    invoice_id=invoice_id,
                )
                db.add(existing)
                db.flush()
    except IntegrityError:
        existing=db.scalar(select(WebhookEvent).where(WebhookEvent.provider_event_id==event_id))
        if existing and existing.status in {"processed","ignored","received"}:
            return JSONResponse({"ok":True,"duplicate":True})
        raise HTTPException(409,"Webhook is already being processed")

    try:
        if event_name.startswith("payment_link."):
            entity=((payload.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
            invoice=db.get(Invoice,invoice_id) if invoice_id else None
            if user and invoice and invoice.owner_id==user.id:
                link=db.scalar(select(PaymentLink).where(
                    PaymentLink.provider_link_id==entity.get("id"),
                    PaymentLink.owner_id==user.id,
                ))
                if link:
                    link.status=entity.get("status",link.status)

                payments=entity.get("payments") or []
                for payment_item in payments:
                    payment_id=str(payment_item.get("payment_id") or "")
                    amount_paise=int(payment_item.get("amount") or 0)
                    payment_status=str(payment_item.get("status") or "")
                    if not payment_id or payment_status!="captured" or amount_paise<=0:
                        continue
                    prior=db.scalar(select(PaymentTransaction).where(PaymentTransaction.razorpay_payment_id==payment_id))
                    if prior:
                        continue
                    remaining=invoice.amount-invoice.paid_amount
                    if remaining<=0 or Decimal(amount_paise)>remaining*Decimal(100):
                        continue
                    tx=PaymentTransaction(
                        workspace_id=invoice.workspace_id,
                        owner_id=user.id,
                        invoice_id=invoice.id,
                        razorpay_payment_id=payment_id,
                        amount_paise=amount_paise,
                        currency=str(entity.get("currency") or "INR"),
                        status="paid",
                        captured_at=datetime.utcnow(),
                    )
                    db.add(tx)
                    invoice.paid_amount += Decimal(amount_paise)/Decimal(100)
                if invoice.paid_amount>=invoice.amount:
                    invoice.paid_amount=invoice.amount
                    invoice.status="paid"
                elif invoice.paid_amount>0:
                    invoice.status="partially_paid"

        elif event_name.startswith("subscription."):
            entity=((payload.get("payload") or {}).get("subscription") or {}).get("entity") or {}
            if user:
                user.subscription_status=entity.get("status",user.subscription_status)

        elif event_name.startswith("payment.") or event_name=="order.paid":
            payment_entity=((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
            order_entity=((payload.get("payload") or {}).get("order") or {}).get("entity") or {}
            order_id=str(payment_entity.get("order_id") or order_entity.get("id") or "")
            tx=db.scalar(select(PaymentTransaction).where(PaymentTransaction.razorpay_order_id==order_id)) if order_id else None
            if tx:
                if event_name=="payment.failed":
                    tx.status="failed"
                    error=payment_entity.get("error_description") or payment_entity.get("error_reason") or "Payment failed"
                    tx.failure_code=payment_entity.get("error_code")
                    tx.failure_reason=str(error)[:500]
                elif event_name=="payment.captured" or event_name=="order.paid":
                    amount_paise=int(payment_entity.get("amount") or order_entity.get("amount_paid") or 0)
                    payment_id=str(payment_entity.get("id") or "")
                    if amount_paise != tx.amount_paise:
                        raise HTTPException(400,"Webhook payment amount does not match the recorded transaction")
                    if payment_id:
                        prior=db.scalar(select(PaymentTransaction).where(
                            PaymentTransaction.razorpay_payment_id==payment_id,
                            PaymentTransaction.id!=tx.id,
                        ))
                        if prior:
                            raise HTTPException(409,"Payment ID is already associated with another transaction")
                    if tx.status!="paid":
                        invoice=db.get(Invoice,tx.invoice_id)
                        remaining=invoice.amount-invoice.paid_amount
                        if remaining<Decimal("0"): remaining=Decimal("0")
                        if Decimal(amount_paise)>remaining*Decimal(100):
                            raise HTTPException(409,"Captured payment would exceed the remaining invoice balance")
                        invoice.paid_amount += Decimal(amount_paise)/Decimal(100)
                        invoice.status="paid" if invoice.paid_amount>=invoice.amount else "partially_paid"
                        tx.razorpay_payment_id=payment_id or tx.razorpay_payment_id
                        tx.status="paid"
                        tx.captured_at=datetime.utcnow()

        existing.status="processed"
        existing.processed_at=datetime.utcnow()
        db.commit()
    except HTTPException as exc:
        db.rollback()
        failed=db.scalar(select(WebhookEvent).where(WebhookEvent.provider_event_id==event_id))
        if failed:
            failed.status="failed"
            failed.error_text=str(exc.detail)[:1000]
            db.commit()
        raise exc
    except Exception as exc:
        db.rollback()
        failed=db.scalar(select(WebhookEvent).where(WebhookEvent.provider_event_id==event_id))
        if failed:
            failed.status="failed"
            failed.error_text=str(exc)[:1000]
            db.commit()
        raise HTTPException(500,"Webhook processing failed")

    return JSONResponse({"ok":True})

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


@app.get("/collections",response_class=HTMLResponse)
def collections_page(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector","viewer")
    invoices=list(db.scalars(
        select(Invoice)
        .where(Invoice.workspace_id==workspace.id,Invoice.paid_amount<Invoice.amount)
    ).all())
    events=list(db.scalars(
        select(CollectionEvent)
        .where(CollectionEvent.workspace_id==workspace.id)
        .order_by(CollectionEvent.created_at.desc())
        .limit(1000)
    ).all())
    rows=collection_queue(invoices,events)
    overdue=sum((i.balance for i in invoices if i.days_overdue>0),Decimal("0"))
    due_7=sum((i.balance for i in invoices if 0<= (i.due_date-date.today()).days <= 7),Decimal("0"))
    promises=sum(1 for r in rows if r["state_event"] and r["state_event"].event_type=="promise" and r["state_event"].promised_date and r["state_event"].promised_date>=date.today())
    broken=sum(1 for r in rows if r["promise_broken"])
    return templates.TemplateResponse("collections.html",{
        "request":request,"user":user,"csrf":csrf_for(request),"rows":rows[:50],
        "outstanding":sum((i.balance for i in invoices),Decimal("0")),
        "overdue":overdue,"due_7":due_7,"promises":promises,"broken":broken,
        "money":money,"message":request.query_params.get("message",""),"error":request.query_params.get("error",""),**commercial_context(user)
    })


@app.post("/collections/{invoice_id}/analyze")
async def analyze_collection_reply_page(
    invoice_id:int,request:Request,csrf:str=Form(...),reply:str=Form(...),
    db:Session=Depends(get_db)
):
    user,workspace=require_role(request,db,"owner","admin","finance","collector"); check_csrf(request,csrf)
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")
    result=analyze_collection_reply(invoice,reply)
    return JSONResponse({
        "ok":True,
        "event_type":result["event_type"],
        "promised_date":result["promised_date"].isoformat() if result["promised_date"] else "",
        "promised_amount":str(result["promised_amount"]) if result["promised_amount"] is not None else "",
        "confidence":result["confidence"],
        "reason":result["reason"],
        "source":result["source"],
    })

@app.post("/collections/{invoice_id}/event")
def create_collection_event(
    invoice_id:int,request:Request,csrf:str=Form(...),event_type:str=Form(...),
    body:str=Form(""),promised_date:str=Form(""),promised_amount:str=Form(""),
    db:Session=Depends(get_db)
):
    user,workspace=require_role(request,db,"owner","admin","finance","collector"); check_csrf(request,csrf)
    if event_type not in {"contact","promise","dispute","payment_claimed","note"}:
        raise HTTPException(400,"Invalid collection event")
    invoice=db.scalar(select(Invoice).where(Invoice.id==invoice_id,Invoice.workspace_id==workspace.id))
    if not invoice: raise HTTPException(404,"Invoice not found")

    promise_date=None; promise_amount=None
    if event_type=="promise":
        try: promise_date=date.fromisoformat(promised_date)
        except ValueError: return RedirectResponse(f"/collections?error=Enter+a+valid+promise+date+for+{invoice.invoice_number}",303)
        try: promise_amount=Decimal(promised_amount or str(invoice.balance))
        except InvalidOperation: return RedirectResponse(f"/collections?error=Enter+a+valid+promise+amount+for+{invoice.invoice_number}",303)
        if promise_amount<=0: raise HTTPException(400,"Promise amount must be positive")
    event=CollectionEvent(
        workspace_id=workspace.id,invoice_id=invoice.id,user_id=user.id,event_type=event_type,
        channel="manual",body=body.strip()[:2000] or None,promised_date=promise_date,promised_amount=promise_amount
    )
    db.add(event)
    audit(db,user,f"collection.{event_type}","invoice",invoice.id,{
        "promised_date":promise_date.isoformat() if promise_date else None,
        "promised_amount":str(promise_amount) if promise_amount is not None else None,
    })
    db.commit()
    return RedirectResponse("/collections?message=Collection+activity+saved",303)

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
    return templates.TemplateResponse("customers.html",{"request":request,"user":user,"rows":rows,"money":money,**commercial_context(user)})

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
    return templates.TemplateResponse("team.html",{"request":request,"user":user,"workspace":workspace,"members":member_rows,"invites":invites,"csrf":csrf_for(request),"invite_url":request.query_params.get("invite_url",""),"message":request.query_params.get("message",""),"error":request.query_params.get("error",""),**commercial_context(user)})

@app.post("/team/invite")
def team_invite(request:Request,csrf:str=Form(...),email:str=Form(...),role:str=Form("viewer"),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin"); check_csrf(request,csrf)
    email=email.strip().lower()
    ensure_limit(user,workspace,"members",db)
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
    return templates.TemplateResponse("audit.html",{"request":request,"user":user,"logs":logs,**commercial_context(user)})

@app.get("/recurring",response_class=HTMLResponse)
def recurring_page(request:Request,db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance","collector","viewer")
    items=list(db.scalars(select(RecurringInvoice).where(RecurringInvoice.workspace_id==workspace.id).order_by(RecurringInvoice.active.desc(),RecurringInvoice.next_issue_date.asc())).all())
    return templates.TemplateResponse("recurring.html",{"request":request,"user":user,"items":items,"csrf":csrf_for(request),"message":request.query_params.get("message",""),**commercial_context(user)})

@app.post("/recurring")
def recurring_create(request:Request,csrf:str=Form(...),customer_name:str=Form(...),phone:str=Form(""),email:str=Form(""),amount:str=Form(...),cadence:str=Form("monthly"),next_issue_date:str=Form(...),due_days:int=Form(7),db:Session=Depends(get_db)):
    user,workspace=require_role(request,db,"owner","admin","finance"); check_csrf(request,csrf)
    if cadence not in {"weekly","monthly","quarterly","yearly"}: raise HTTPException(400,"Invalid cadence")
    ensure_limit(user,workspace,"recurring",db)
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
