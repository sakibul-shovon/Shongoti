"""
Phase 08 API Router: Action Center, Scoped Customer Portal, and SSLCOMMERZ Payments.
"""
from datetime import datetime, timedelta, timezone
import json
import secrets
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.permissions import verify_tenant_ownership
from app.domain.ledger import StandardAccount
from app.domain.merchant import MerchantService
from app.domain.money import Money
from app.persistence.models import (
    AccountModel,
    ActionModel,
    CaseModel,
    CustomerPortalTokenModel,
    JournalEntryModel,
    PartyModel,
    PostingModel,
    ProductItemModel,
    PurchaseDraftModel,
    TenantModel,
    UserModel,
    generate_uuid,
    now_utc,
)

actions_router = APIRouter(prefix="/api/v1", tags=["Actions, Portal & Payments"])


def is_expired(dt: Optional[datetime]) -> bool:
    if not dt:
        return False
    now = datetime.now(timezone.utc)
    target = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return now > target


def validate_action_source(action, db):
    payload = json.loads(action.payload_json) if action.payload_json else {}
    if payload.get("case_id"):
        case = db.query(CaseModel).filter(CaseModel.id == payload["case_id"], CaseModel.tenant_id == action.tenant_id).first()
        if not case or case.current_version != payload.get("case_version"):
            raise HTTPException(status_code=409, detail="The source case has changed. Reassess and prepare a new draft.")


# --- DTOs ---

class ActionCreateDTO(BaseModel):
    action_type: str = Field(..., pattern="^(supplier_payment|customer_reminder|stock_reorder)$")
    recipient: str = Field(..., min_length=1)
    amount_paisa: Optional[int] = Field(None, ge=0)
    idempotency_key: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    expires_in_hours: int = 24


class PortalGenerateDTO(BaseModel):
    party_id: str
    invoice_number: str
    amount_paisa: int = Field(..., gt=0)
    description: Optional[str] = None
    expires_in_days: int = 7


class DisputeDTO(BaseModel):
    dispute_note: str = Field(..., min_length=3)


# --- 1. Action Center Endpoints ---

@actions_router.get("/actions")
def list_actions(
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    actions = (
        db.query(ActionModel)
        .filter(ActionModel.tenant_id == current_user.tenant_id)
        .order_by(ActionModel.created_at.desc())
        .all()
    )
    return [
        {
            "id": a.id,
            "action_type": a.action_type,
            "recipient": a.recipient,
            "amount_paisa": a.amount_paisa,
            "status": a.status,
            "idempotency_key": a.idempotency_key,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            "executed_at": a.executed_at.isoformat() if a.executed_at else None,
            "created_at": a.created_at.isoformat(),
            "payload": json.loads(a.payload_json) if a.payload_json else None,
            "execution_capability": "unavailable",
        }
        for a in actions
    ]


@actions_router.post("/actions")
def create_action_draft(
    req: ActionCreateDTO,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Idempotency check
    if req.idempotency_key:
        existing = (
            db.query(ActionModel)
            .filter(
                ActionModel.tenant_id == current_user.tenant_id,
                ActionModel.idempotency_key == req.idempotency_key,
            )
            .first()
        )
        if existing:
            return {"status": "existing", "action_id": existing.id, "current_status": existing.status}

    expires_at = now_utc() + timedelta(hours=req.expires_in_hours)
    action = ActionModel(
        tenant_id=current_user.tenant_id,
        action_type=req.action_type,
        recipient=req.recipient,
        amount_paisa=req.amount_paisa,
        status="pending_approval" if req.amount_paisa and req.amount_paisa > 0 else "draft",
        idempotency_key=req.idempotency_key or f"ACT-{generate_uuid()[:8].upper()}",
        payload_json=json.dumps(req.payload) if req.payload else None,
        created_by_user_id=current_user.id,
        expires_at=expires_at,
    )
    db.add(action)
    db.commit()
    return {"status": "created", "action_id": action.id, "action_status": action.status}


@actions_router.post("/actions/{action_id}/approve")
def approve_action(
    action_id: str,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    action = (
        db.query(ActionModel)
        .filter(ActionModel.id == action_id, ActionModel.tenant_id == current_user.tenant_id)
        .first()
    )
    if not action:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action not found.")

    if action.status not in ["draft", "pending_approval"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot approve action in '{action.status}' state.")

    # Role enforcement: Cashier cannot approve financial disbursements
    if action.amount_paisa and action.amount_paisa > 0 and current_user.role == "cashier":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cashier is not authorized to approve payments.")

    if is_expired(action.expires_at):
        raise HTTPException(status_code=410, detail="Action has expired.")
    validate_action_source(action, db)

    action.status = "approved"
    action.approved_by_user_id = current_user.id
    db.commit()
    return {"status": "approved", "action_id": action.id}


@actions_router.post("/actions/{action_id}/execute")
def execute_action(
    action_id: str,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    action = (
        db.query(ActionModel)
        .filter(ActionModel.id == action_id, ActionModel.tenant_id == current_user.tenant_id)
        .first()
    )
    if not action:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action not found.")
    verify_tenant_ownership(action.tenant_id, current_user.tenant_id)

    if action.status != "approved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Action must be approved before execution. Current state: {action.status}")

    # Check expiry
    if is_expired(action.expires_at):
        action.status = "failed"
        action.outcome_json = json.dumps({"error": "Action expired before execution"})
        db.commit()
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Action has expired.")

    validate_action_source(action, db)
    payload = json.loads(action.payload_json) if action.payload_json else {}
    if action.action_type == "stock_reorder" and payload.get("product_id") and payload.get("quantity"):
        product = db.query(ProductItemModel).filter_by(id=payload["product_id"], tenant_id=current_user.tenant_id).first()
        if product:
            verify_tenant_ownership(product.tenant_id, current_user.tenant_id)
            qty = int(payload["quantity"])
            unit_cost = int(payload.get("unit_cost_paisa", product.cost_price_paisa or 0))
            draft = PurchaseDraftModel(
                tenant_id=current_user.tenant_id,
                product_id=product.id,
                quantity=qty,
                unit_cost_paisa=unit_cost,
                total_cost_paisa=qty * unit_cost,
                status="draft",
                notes=f"Created from approved action {action.id}",
            )
            db.add(draft)
            db.flush()
            action.status = "completed"
            action.executed_at = now_utc()
            action.outcome_json = json.dumps({"purchase_draft_id": draft.id})
            db.commit()
            return {"status": "completed", "action_id": action.id, "purchase_draft_id": draft.id}
    # No dispatcher or verified external receipt exists for external payment/messaging boundary.
    raise HTTPException(status_code=503, detail="Execution adapter is not connected. Approval is recorded; no payment, message or stock movement was executed.")


@actions_router.post("/actions/{action_id}/cancel")
def cancel_action(
    action_id: str,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    action = (
        db.query(ActionModel)
        .filter(ActionModel.id == action_id, ActionModel.tenant_id == current_user.tenant_id)
        .first()
    )
    if not action:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action not found.")
    verify_tenant_ownership(action.tenant_id, current_user.tenant_id)

    if action.status in ["completed", "cancelled"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot cancel action in '{action.status}' state.")

    action.status = "cancelled"
    db.commit()
    return {"status": "cancelled", "action_id": action.id}


# --- 2. Customer Scoped Portal Endpoints ---

@actions_router.post("/portal/generate-link")
def generate_portal_link(
    req: PortalGenerateDTO,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    party = db.query(PartyModel).filter(PartyModel.id == req.party_id, PartyModel.tenant_id == current_user.tenant_id).first()
    if not party:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer party not found.")

    token = secrets.token_urlsafe(32)
    expires_at = now_utc() + timedelta(days=req.expires_in_days)

    portal_token = CustomerPortalTokenModel(
        token=token,
        tenant_id=current_user.tenant_id,
        party_id=req.party_id,
        invoice_number=req.invoice_number,
        amount_paisa=req.amount_paisa,
        description=req.description,
        expires_at=expires_at,
    )
    db.add(portal_token)
    db.commit()

    return {
        "status": "success",
        "token": token,
        "portal_url": f"/pay/{token}",
        "expires_at": expires_at.isoformat(),
    }


@actions_router.get("/portal/{token}")
def get_portal_view(
    token: str,
    db: Session = Depends(get_db),
):
    """Scoped public invoice view without merchant credentials."""
    pt = db.query(CustomerPortalTokenModel).filter(CustomerPortalTokenModel.token == token).first()
    if not pt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid or unknown invoice link.")

    if pt.is_revoked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This invoice link has been revoked by the merchant.")

    if is_expired(pt.expires_at):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This invoice link has expired.")

    tenant = db.query(TenantModel).filter(TenantModel.id == pt.tenant_id).first()
    party = db.query(PartyModel).filter(PartyModel.id == pt.party_id, PartyModel.tenant_id == pt.tenant_id).first()
    if party:
        verify_tenant_ownership(party.tenant_id, pt.tenant_id)

    return {
        "shop_name": tenant.name if tenant else "দোকান",
        "currency": "BDT",
        "customer_name": party.name if party else "সম্মানিত ক্রেতা",
        "invoice_number": pt.invoice_number,
        "amount_paisa": pt.amount_paisa,
        "description": pt.description,
        "is_paid": pt.is_paid,
        "dispute_note": pt.dispute_note,
        "expires_at": pt.expires_at.isoformat(),
    }


@actions_router.post("/portal/{token}/dispute")
def submit_portal_dispute(
    token: str,
    req: DisputeDTO,
    db: Session = Depends(get_db),
):
    pt = db.query(CustomerPortalTokenModel).filter(CustomerPortalTokenModel.token == token).first()
    if not pt or pt.is_revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid invoice link.")

    if is_expired(pt.expires_at):
        raise HTTPException(status_code=410, detail="This invoice link has expired.")

    pt.dispute_note = req.dispute_note
    db.commit()
    return {"status": "success", "message": "Dispute note submitted to merchant."}


# --- 3. SSLCOMMERZ Connector Endpoints ---

def _sslcommerz_base_url() -> str:
    return "https://sandbox.sslcommerz.com" if settings.SSLCOMMERZ_IS_SANDBOX else "https://securepay.sslcommerz.com"


@actions_router.post("/portal/{token}/pay")
def initiate_customer_payment(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    pt = db.query(CustomerPortalTokenModel).filter(CustomerPortalTokenModel.token == token).first()
    if not pt or pt.is_revoked or pt.is_paid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoice is already paid or invalid.")

    if is_expired(pt.expires_at):
        raise HTTPException(status_code=410, detail="This invoice link has expired.")

    store_id = settings.SSLCOMMERZ_STORE_ID.strip()
    store_passwd = settings.SSLCOMMERZ_STORE_PASSWD.strip()
    if not store_id or not store_passwd:
        raise HTTPException(status_code=503, detail="Online payment initiation is not connected to a verified gateway provider. No payment was initiated.")

    import httpx
    from decimal import Decimal
    party = db.query(PartyModel).filter_by(id=pt.party_id, tenant_id=pt.tenant_id).first()
    if party:
        verify_tenant_ownership(party.tenant_id, pt.tenant_id)
    amount_bdt = f"{(Decimal(pt.amount_paisa) / Decimal(100)):.2f}"
    tran_id = f"SHG-{pt.id[:8].upper()}-{secrets.token_hex(4).upper()}"
    base_cb = str(request.base_url).rstrip("/")
    form_data = {
        "store_id": store_id,
        "store_passwd": store_passwd,
        "total_amount": amount_bdt,
        "currency": "BDT",
        "tran_id": tran_id,
        "success_url": f"{base_cb}/api/v1/payments/sslcommerz/ipn",
        "fail_url": f"{base_cb}/pay/{token}",
        "cancel_url": f"{base_cb}/pay/{token}",
        "ipn_url": f"{base_cb}/api/v1/payments/sslcommerz/ipn",
        "cus_name": party.name if party else "Customer",
        "cus_phone": (party.phone if party and party.phone else "01700000000"),
        "cus_email": "portal@shongoti.local",
        "cus_add1": "Dhaka",
        "cus_city": "Dhaka",
        "cus_country": "Bangladesh",
        "shipping_method": "NO",
        "product_name": f"Invoice {pt.invoice_number}",
        "product_category": "MSME Invoice",
        "product_profile": "general",
        "value_a": token,
        "value_b": pt.tenant_id,
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(f"{_sslcommerz_base_url()}/gwprocess/v4/api.php", data=form_data)
            payload = resp.json() if resp.is_success else {}
    except Exception:
        raise HTTPException(status_code=503, detail="Payment gateway session creation failed.")
    gateway_url = payload.get("GatewayPageURL")
    if payload.get("status") != "SUCCESS" or not gateway_url:
        raise HTTPException(status_code=503, detail="Payment gateway rejected session initiation.")
    return {
        "status": "initiated",
        "tran_id": tran_id,
        "gateway_url": gateway_url,
        "amount_paisa": pt.amount_paisa,
    }


@actions_router.post("/payments/sslcommerz/ipn")
async def sslcommerz_ipn(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Handles SSLCOMMERZ IPN Webhook and Callback.
    Validates amount, currency, merchant store, and duplicate transaction via server-to-server Order Validation API.
    Atomically records pending-settlement ledger posting and reduces customer due.
    """
    store_id = settings.SSLCOMMERZ_STORE_ID.strip()
    store_passwd = settings.SSLCOMMERZ_STORE_PASSWD.strip()
    if not store_id or not store_passwd:
        raise HTTPException(status_code=503, detail="Gateway server-to-server validation is not implemented. Unverified callbacks cannot post money.")

    import httpx
    from decimal import Decimal, InvalidOperation
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
    else:
        form = await request.form()
        data = dict(form)

    token = str(data.get("token") or data.get("value_a") or "").strip()
    tran_id = str(data.get("tran_id") or "").strip()
    val_id = str(data.get("val_id") or "").strip()
    if not token or not tran_id or not val_id:
        raise HTTPException(status_code=400, detail="Missing required gateway validation parameters (token, tran_id, val_id).")

    pt = db.query(CustomerPortalTokenModel).filter(CustomerPortalTokenModel.token == token).with_for_update().first()
    if not pt or pt.is_revoked:
        raise HTTPException(status_code=404, detail="Invalid or revoked portal invoice token.")

    event_id = f"sslcommerz:{tran_id}"
    existing_entry = db.query(JournalEntryModel).filter_by(tenant_id=pt.tenant_id, event_id=event_id).first()
    if existing_entry or pt.is_paid:
        return {"status": "already_processed", "tran_id": tran_id}

    try:
        with httpx.Client(timeout=15.0) as client:
            v_resp = client.get(
                f"{_sslcommerz_base_url()}/validator/api/validationserverAPI.php",
                params={"val_id": val_id, "store_id": store_id, "store_passwd": store_passwd, "v": "1", "format": "json"},
            )
            verified = v_resp.json() if v_resp.is_success else {}
    except Exception:
        raise HTTPException(status_code=503, detail="Gateway validation server unreachable.")

    if verified.get("status") not in ("VALID", "VALIDATED") or verified.get("tran_id") != tran_id:
        raise HTTPException(status_code=400, detail="Gateway transaction validation failed.")
    if str(verified.get("currency_type") or verified.get("currency") or "").upper() != "BDT":
        raise HTTPException(status_code=400, detail="Currency mismatch in gateway validation.")
    if str(verified.get("risk_level") or "0") not in ("0", ""):
        raise HTTPException(status_code=400, detail="High-risk transaction requires manual settlement review.")
    try:
        verified_paisa = int((Decimal(str(verified.get("amount", "0"))) * 100).to_integral_exact())
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Invalid amount returned by gateway validation.")
    if verified_paisa != pt.amount_paisa:
        raise HTTPException(status_code=400, detail="Validated payment amount does not match invoice amount.")

    party = db.query(PartyModel).filter_by(id=pt.party_id, tenant_id=pt.tenant_id).with_for_update().first()
    if not party:
        raise HTTPException(status_code=404, detail="Customer party not found.")
    verify_tenant_ownership(party.tenant_id, pt.tenant_id)

    pending_acc = MerchantService.get_or_create_account(
        db, pt.tenant_id, StandardAccount.PENDING_GATEWAY.value, "Pending Gateway Settlement", "asset"
    )
    rec_acc = MerchantService.get_or_create_account(
        db, pt.tenant_id, StandardAccount.RECEIVABLES.value, "Accounts Receivable", "asset"
    )
    entry = JournalEntryModel(
        tenant_id=pt.tenant_id,
        entry_number="GW-" + tran_id[-10:].upper(),
        event_id=event_id,
        description=f"Verified SSLCOMMERZ payment for invoice {pt.invoice_number}",
    )
    db.add(entry)
    db.flush()
    db.add_all([
        PostingModel(journal_entry_id=entry.id, account_id=pending_acc.id, direction="debit", amount_paisa=verified_paisa),
        PostingModel(journal_entry_id=entry.id, account_id=rec_acc.id, direction="credit", amount_paisa=verified_paisa),
    ])
    pending_acc.current_balance_paisa += verified_paisa
    rec_acc.current_balance_paisa = max(0, rec_acc.current_balance_paisa - verified_paisa)
    party.current_due_paisa = max(0, party.current_due_paisa - verified_paisa)
    pt.is_paid = True
    db.commit()
    return {"status": "settled_to_pending_gateway", "tran_id": tran_id, "entry_id": entry.id}
