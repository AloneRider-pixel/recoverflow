    try:
        amount_d,paid_d=Decimal(amount),Decimal(paid_amount)
        tax_d=Decimal(tax_amount) if tax_amount.strip() else None
        tax_rate_d=Decimal(tax_rate) if tax_rate.strip() else None
        tds_d=Decimal(tds_amount) if tds_amount.strip() else None
    except InvalidOperation:
        raise HTTPException(400,"Invalid amount, tax or TDS values")
    if amount_d<=0 or paid_d<0 or paid_d>amount_d or (tax_rate_d is not None and tax_rate_d<0) or (tax_d is not None and tax_d<0) or (tds_d is not None and tds_d<0):
        raise HTTPException(400,"Invalid invoice values")
    if db.scalar(select(Invoice).where(Invoice.invoice_number==invoice_number.strip())): raise HTTPException(400,"Invoice number already exists")
    i=Invoice(owner_id=user.id,invoice_number=invoice_number.strip(),customer_name=customer_name.strip(),phone=phone.strip() or None,email=email.strip() or None,amount=amount_d,paid_amount=paid_d,issue_date=date.fromisoformat(issue_date),due_date=date.fromisoformat(due_date),status="paid" if paid_d>=amount_d else "partially_paid" if paid_d>0 else "unpaid",gstin=gstin.strip() or None,place_of_supply=place_of_supply.strip() or None,tax_rate=tax_rate_d,tax_amount=tax_d,tds_amount=tds_d)
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
    pay_url=str(request.base_url).rstrip("/")+"/pay/"+make_public_invoice_token(invoice.id)
    return templates.TemplateResponse("invoice_detail.html",{"request":request,"user":user,"csrf":csrf_for(request),"invoice":invoice,"message":msg,"tone":tone,"wa_url":wa,"money":money,"payment_link":latest,"pay_url":pay_url})

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
