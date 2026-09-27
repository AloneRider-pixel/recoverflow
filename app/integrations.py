import hashlib, hmac, json, secrets
from datetime import datetime
import razorpay
import httpx
from fastapi import HTTPException
from .config import settings
from .security import decrypt

RAZORPAY_BASE = "https://api.razorpay.com/v1"

def merchant_keys(user):
    return decrypt(user.razorpay_key_id_enc), decrypt(user.razorpay_key_secret_enc)

def webhook_secret(user):
    return decrypt(user.razorpay_webhook_secret_enc)

def _razorpay_request(method, path, key_id, key_secret, payload=None):
    if not key_id or not key_secret:
        raise HTTPException(400, "Razorpay is not configured. Open Settings → Integrations.")
    with httpx.Client(timeout=20) as client:
        r = client.request(method, RAZORPAY_BASE + path, auth=(key_id, key_secret), json=payload)
    if r.status_code >= 400:
        try:
            detail = r.json().get("error", {}).get("description", r.text)
        except Exception:
            detail = r.text
        raise HTTPException(502, f"Razorpay error: {detail}")
    return r.json()

def create_payment_link(user, invoice):
    # Prefer workspace credentials; fall back to global Render credentials
    # for single-merchant/test deployments.
    key_id, key_secret = standard_checkout_keys(user)
    if invoice.balance < 1:
        raise HTTPException(400, "Razorpay Payment Links require at least ₹1.")
    payload = {
        "amount": int(invoice.balance * 100),
        "currency": "INR",
        "accept_partial": True,
        "description": f"Invoice {invoice.invoice_number} - {invoice.customer_name}",
        "reference_id": f"RF-{user.id}-{invoice.id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}",
        "customer": {
            "name": invoice.customer_name,
            "email": invoice.email or "",
            "contact": invoice.phone or "",
        },
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {
            "recoverflow_invoice_id": str(invoice.id),
            "recoverflow_user_id": str(user.id),
        },
    }
    return _razorpay_request("POST", "/payment_links", key_id, key_secret, payload)

def verify_webhook(raw_body, signature, secret):
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

def send_whatsapp_template(user, invoice, payment_url=None):
    token = decrypt(user.whatsapp_access_token_enc)
    phone_id = user.whatsapp_phone_number_id or ""
    version = settings.whatsapp_graph_version
    if not token or not phone_id or not version:
        raise HTTPException(400, "WhatsApp Business API is not configured. Open Settings → Integrations.")
    to = "".join(ch for ch in (invoice.phone or "") if ch.isdigit())
    if not to:
        raise HTTPException(400, "Invoice has no customer phone number.")
    params = [
        {"type":"text","text":invoice.invoice_number},
        {"type":"text","text":f"₹{invoice.balance:,.2f}"},
        {"type":"text","text":invoice.due_date.strftime("%d %b %Y")},
    ]
    if payment_url:
        params.append({"type":"text","text":payment_url})
    body = {
        "messaging_product":"whatsapp",
        "to":to,
        "type":"template",
        "template":{
            "name":user.whatsapp_template_name,
            "language":{"code":user.whatsapp_template_language},
            "components":[{"type":"body","parameters":params}],
        },
    }
    url=f"https://graph.facebook.com/{version}/{phone_id}/messages"
    with httpx.Client(timeout=20) as client:
        r=client.post(url, headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}, json=body)
    if r.status_code >= 400:
        try: detail=r.json().get("error",{}).get("message",r.text)
        except Exception: detail=r.text
        raise HTTPException(502, f"WhatsApp API error: {detail}")
    data=r.json()
    messages=data.get("messages") or []
    return messages[0].get("id") if messages else None

def standard_checkout_keys(user=None):
    """
    Prefer workspace-scoped merchant credentials for invoice checkout.
    Fall back to global RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET for single-merchant
    deployments and test environments.
    """
    if user is not None:
        key_id, key_secret = merchant_keys(user)
        if key_id and key_secret:
            return key_id, key_secret
    return settings.razorpay_key_id, settings.razorpay_key_secret


def standard_checkout_client(user=None):
    key_id, key_secret = standard_checkout_keys(user)
    if not key_id or not key_secret:
        raise HTTPException(400, "Razorpay Standard Checkout is not configured.")
    client = razorpay.Client(auth=(key_id, key_secret))
    client.set_app_details({"title": "RecoverFlow", "version": "1.0.0"})
    return client, key_id, key_secret


def create_standard_checkout_order(user, invoice):
    client, _, _ = standard_checkout_client(user)
    amount_paise = int(invoice.balance * 100)
    if amount_paise < 100:
        raise HTTPException(400, "Razorpay Checkout requires an amount of at least ₹1.")
    receipt = f"RF-{invoice.id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    return client.order.create(data={
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "notes": {
            "recoverflow_invoice_id": str(invoice.id),
            "recoverflow_user_id": str(user.id),
        },
    })


def fetch_standard_checkout_order(user, order_id):
    client, _, _ = standard_checkout_client(user)
    return client.order.fetch(order_id)


def fetch_standard_checkout_payment(user, payment_id):
    client, _, _ = standard_checkout_client(user)
    return client.payment.fetch(payment_id)


def platform_keys():
    return settings.razorpay_platform_key_id, settings.razorpay_platform_key_secret

def platform_request(method,path,payload=None):
    key_id,key_secret=platform_keys()
    return _razorpay_request(method,path,key_id,key_secret,payload)

def send_expo_push(tokens, title, body, data=None):
    messages=[]
    for token in tokens:
        if not token or not token.startswith("ExponentPushToken["): continue
        messages.append({"to":token,"title":title,"body":body,"data":data or {}, "sound":"default"})
    if not messages: return []
    with httpx.Client(timeout=15) as client:
        r=client.post("https://exp.host/--/api/v2/push/send",json=messages,headers={"Content-Type":"application/json"})
    if r.status_code >= 400:
        return []
    return (r.json() or {}).get("data") or []
