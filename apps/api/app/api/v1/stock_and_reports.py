"""Stock management, operating reports, recurring commitments and forecasting API.

Implements Phase 09 requirements:
- P09-T01: Stock units/counts, purchase drafts, lead-time/stock-cover views, accepted-return follow-up and WAC calculations.
- P09-T02: Recurring commitments, stock/cash-aware reorder comparison, and operating reports separating cash, profit, inventory and dues.
- P09-T03: Optional history-based forecast adapter with cold-start gating and rolling-origin evaluation.
- P09-T04: Supported selected-file CSV import/export and daily briefing with deduplication and RBAC.
"""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import io
import csv

from fastapi import APIRouter, Depends, HTTPException, Query, status, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.database import get_db
from app.persistence.models import (
    TenantModel, UserModel, PartyModel, ProductItemModel,
    StockMovementModel, DailySnapshotModel, AccountModel,
    PurchaseDraftModel, RecurringCommitmentModel,
    JournalEntryModel, PostingModel,
    now_utc, generate_uuid
)
from app.core.auth import get_current_user
from app.core.permissions import verify_tenant_ownership
from app.adapters.forecast import HistoryForecastAdapter
from app.domain.ledger import StandardAccount
from app.domain.merchant import MerchantService
from app.domain.csv_io import csv_count, csv_paisa, spreadsheet_text

stock_reports_router = APIRouter(tags=["Stock, Reports & Forecasting"])


# ---------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------

class ProductCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    sku: Optional[str] = None
    unit: str = "pcs"
    quantity_on_hand: int = 0
    cost_price_paisa: Optional[int] = None
    selling_price_paisa: int = 0
    reorder_threshold: int = 5
    lead_time_days: int = 3


class PurchaseDraftCreateRequest(BaseModel):
    product_id: str
    quantity: int = Field(..., gt=0)
    unit_cost_paisa: int = Field(..., ge=0)
    supplier_id: Optional[str] = None
    lead_time_days: int = 3
    notes: Optional[str] = None


class ReturnProcessRequest(BaseModel):
    return_type: str = Field(..., pattern="^(customer_return|supplier_return)$")
    product_id: str
    quantity: int = Field(..., gt=0)
    party_id: Optional[str] = None
    refund_amount_paisa: Optional[int] = None
    refund_method: str = "credit_adjustment"  # "credit_adjustment" or "cash"


class RecurringCommitmentCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    category: str = "other"  # rent, utilities, salary, loan, other
    amount_paisa: int = Field(..., gt=0)
    frequency: str = "monthly"  # daily, weekly, monthly
    party_id: Optional[str] = None
    due_day_of_month: Optional[int] = Field(None, ge=1, le=31)


class CSVImportRequest(BaseModel):
    csv_content: str = Field(min_length=1, max_length=1_000_000)


class CSVImportResponse(BaseModel):
    total_processed: int
    created_count: int
    updated_count: int
    errors: List[str]


# ---------------------------------------------------------
# Stock & Purchasing Endpoints (P09-T01)
# ---------------------------------------------------------

@stock_reports_router.get("/api/v1/stock/products")
def list_stock_products(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """List products with stock counts, WAC cost, margin, velocity and stock cover days."""
    products = db.query(ProductItemModel).filter(
        ProductItemModel.tenant_id == current_user.tenant_id
    ).order_by(ProductItemModel.name.asc()).all()

    # Calculate 30-day sales velocity per product
    product_stats = []
    for p in products:
        # Sum sale deductions for this product
        sale_deductions = db.query(func.sum(StockMovementModel.quantity_change)).filter(
            StockMovementModel.tenant_id == current_user.tenant_id,
            StockMovementModel.product_id == p.id,
            StockMovementModel.movement_type == "sale_deduction"
        ).scalar() or 0
        total_units_sold = abs(sale_deductions)
        daily_velocity = round(total_units_sold / 30.0, 2)

        stock_cover_days = round(p.quantity_on_hand / daily_velocity, 1) if daily_velocity > 0 else 999.0

        # Margin calculation: if cost is unknown, suppress unsupported margin claims
        if p.cost_price_paisa is not None:
            margin_paisa = p.selling_price_paisa - p.cost_price_paisa
            margin_percent = round((margin_paisa / p.selling_price_paisa) * 100, 2) if p.selling_price_paisa > 0 else 0.0
        else:
            margin_paisa = None
            margin_percent = None

        # Reorder status
        threshold = p.reorder_threshold if p.reorder_threshold is not None else 5
        lead_time = p.lead_time_days if p.lead_time_days is not None else 3
        if p.quantity_on_hand <= 0:
            reorder_status = "out_of_stock"
        elif p.quantity_on_hand <= threshold or stock_cover_days <= lead_time:
            reorder_status = "reorder_urgent"
        else:
            reorder_status = "normal"

        product_stats.append({
            "id": p.id,
            "name": p.name,
            "sku": p.sku,
            "unit": p.unit,
            "quantity_on_hand": p.quantity_on_hand,
            "cost_price_paisa": p.cost_price_paisa,
            "selling_price_paisa": p.selling_price_paisa,
            "reorder_threshold": threshold,
            "lead_time_days": lead_time,
            "margin_paisa": margin_paisa,
            "margin_percent": margin_percent,
            "daily_sales_velocity": daily_velocity,
            "stock_cover_days": stock_cover_days,
            "reorder_status": reorder_status,
        })

    return {"products": product_stats}


@stock_reports_router.post("/api/v1/stock/products", status_code=status.HTTP_201_CREATED)
def create_product(
    req: ProductCreateRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """Create a new product item with initial stock and WAC cost."""
    product = ProductItemModel(
        tenant_id=current_user.tenant_id,
        name=req.name,
        sku=req.sku,
        unit=req.unit,
        quantity_on_hand=req.quantity_on_hand,
        cost_price_paisa=req.cost_price_paisa,
        selling_price_paisa=req.selling_price_paisa,
        reorder_threshold=req.reorder_threshold,
        lead_time_days=req.lead_time_days,
    )
    db.add(product)
    db.flush()
    if req.quantity_on_hand > 0:
        db.add(
            StockMovementModel(
                tenant_id=current_user.tenant_id,
                product_id=product.id,
                movement_type="opening_stock",
                quantity_change=req.quantity_on_hand,
                reference_id=f"stock-open:{product.id[:8]}",
            )
        )
        if req.cost_price_paisa and req.cost_price_paisa > 0:
            inv_val = req.quantity_on_hand * req.cost_price_paisa
            inv_acc = MerchantService.get_or_create_account(
                db, current_user.tenant_id, StandardAccount.INVENTORY.value, "Inventory", "asset"
            )
            cap_acc = MerchantService.get_or_create_account(
                db, current_user.tenant_id, StandardAccount.OWNER_CAPITAL.value, "Owner capital", "equity"
            )
            entry = JournalEntryModel(
                tenant_id=current_user.tenant_id,
                entry_number="INV-OPEN-" + product.id[:8],
                event_id="stock-product-opening:" + product.id,
                description=f"Opening inventory for {product.name}"[:255],
            )
            db.add(entry)
            db.flush()
            db.add_all([
                PostingModel(journal_entry_id=entry.id, account_id=inv_acc.id, direction="debit", amount_paisa=inv_val),
                PostingModel(journal_entry_id=entry.id, account_id=cap_acc.id, direction="credit", amount_paisa=inv_val),
            ])
            inv_acc.current_balance_paisa += inv_val
            cap_acc.current_balance_paisa += inv_val
    db.commit()
    db.refresh(product)
    return {"status": "created", "product_id": product.id, "name": product.name}


@stock_reports_router.post("/api/v1/stock/purchase-drafts", status_code=status.HTTP_201_CREATED)
def create_purchase_draft(
    req: PurchaseDraftCreateRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """Draft a supplier purchase order with quantities, unit cost, and lead time."""
    product = db.query(ProductItemModel).filter(
        ProductItemModel.id == req.product_id,
        ProductItemModel.tenant_id == current_user.tenant_id
    ).first()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    total_cost_paisa = req.quantity * req.unit_cost_paisa
    draft = PurchaseDraftModel(
        tenant_id=current_user.tenant_id,
        supplier_id=req.supplier_id,
        product_id=req.product_id,
        quantity=req.quantity,
        unit_cost_paisa=req.unit_cost_paisa,
        total_cost_paisa=total_cost_paisa,
        lead_time_days=req.lead_time_days,
        status="draft",
        notes=req.notes,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return {
        "status": "created",
        "draft_id": draft.id,
        "product_name": product.name,
        "quantity": draft.quantity,
        "unit_cost_paisa": draft.unit_cost_paisa,
        "total_cost_paisa": draft.total_cost_paisa,
    }


@stock_reports_router.get("/api/v1/stock/purchase-drafts")
def list_purchase_drafts(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """List purchase drafts for tenant."""
    drafts = db.query(PurchaseDraftModel).filter(
        PurchaseDraftModel.tenant_id == current_user.tenant_id
    ).order_by(PurchaseDraftModel.created_at.desc()).all()

    return {
        "drafts": [
            {
                "id": d.id,
                "product_id": d.product_id,
                "product_name": d.product.name if d.product else "Unknown",
                "supplier_id": d.supplier_id,
                "supplier_name": d.supplier.name if d.supplier else None,
                "quantity": d.quantity,
                "unit_cost_paisa": d.unit_cost_paisa,
                "total_cost_paisa": d.total_cost_paisa,
                "lead_time_days": d.lead_time_days,
                "status": d.status,
                "created_at": d.created_at.isoformat(),
                "received_at": d.received_at.isoformat() if d.received_at else None,
            }
            for d in drafts
        ]
    }


@stock_reports_router.post("/api/v1/stock/purchase-drafts/{draft_id}/receive")
def receive_purchase_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """
    Receive goods from an approved or drafted purchase order.
    Updates stock count, recalculates Weighted Average Cost (WAC per D011),
    creates a stock movement, and updates supplier balance if applicable.
    """
    draft = db.query(PurchaseDraftModel).filter(
        PurchaseDraftModel.id == draft_id,
        PurchaseDraftModel.tenant_id == current_user.tenant_id
    ).first()
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase draft not found.")
    if draft.status == "received":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Purchase draft already received.")

    product = db.query(ProductItemModel).filter(
        ProductItemModel.id == draft.product_id,
        ProductItemModel.tenant_id == current_user.tenant_id
    ).first()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    # Recalculate WAC (D011)
    existing_qty = product.quantity_on_hand
    existing_cost = product.cost_price_paisa or draft.unit_cost_paisa
    received_qty = draft.quantity
    received_unit_cost = draft.unit_cost_paisa

    if existing_qty <= 0:
        new_wac = received_unit_cost
    else:
        new_wac = int(((existing_qty * existing_cost) + (received_qty * received_unit_cost)) / (existing_qty + received_qty))

    old_wac = product.cost_price_paisa
    product.cost_price_paisa = new_wac
    product.quantity_on_hand += received_qty

    # Stock movement
    movement = StockMovementModel(
        tenant_id=current_user.tenant_id,
        product_id=product.id,
        movement_type="purchase_addition",
        quantity_change=received_qty,
        reference_id=draft.id,
    )
    db.add(movement)

    # If supplier exists, add to supplier payable
    supplier = None
    if draft.supplier_id:
        supplier = db.query(PartyModel).filter(
            PartyModel.id == draft.supplier_id,
            PartyModel.tenant_id == current_user.tenant_id
        ).first()
        if supplier:
            supplier.current_due_paisa += draft.total_cost_paisa

    if draft.total_cost_paisa > 0:
        inv_acc = MerchantService.get_or_create_account(
            db, current_user.tenant_id, StandardAccount.INVENTORY.value, "Inventory", "asset"
        )
        credit_code = StandardAccount.PAYABLES.value if supplier else StandardAccount.OWNER_CAPITAL.value
        credit_type = "liability" if supplier else "equity"
        credit_name = "Accounts Payable" if supplier else "Owner capital"
        credit_acc = MerchantService.get_or_create_account(
            db, current_user.tenant_id, credit_code, credit_name, credit_type
        )
        entry = JournalEntryModel(
            tenant_id=current_user.tenant_id,
            entry_number="PUR-" + draft.id[:8].upper(),
            event_id="purchase-receive:" + draft.id,
            description=f"Received purchase draft for {product.name}"[:255],
        )
        db.add(entry)
        db.flush()
        db.add_all([
            PostingModel(journal_entry_id=entry.id, account_id=inv_acc.id, direction="debit", amount_paisa=draft.total_cost_paisa),
            PostingModel(journal_entry_id=entry.id, account_id=credit_acc.id, direction="credit", amount_paisa=draft.total_cost_paisa),
        ])
        inv_acc.current_balance_paisa += draft.total_cost_paisa
        credit_acc.current_balance_paisa += draft.total_cost_paisa

    draft.status = "received"
    draft.received_at = now_utc()
    db.commit()

    return {
        "status": "received",
        "draft_id": draft.id,
        "product_id": product.id,
        "new_quantity_on_hand": product.quantity_on_hand,
        "old_wac_paisa": old_wac,
        "new_wac_paisa": new_wac,
    }


@stock_reports_router.post("/api/v1/stock/returns")
def process_stock_return(
    req: ReturnProcessRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """
    Handle accepted customer or supplier returns.
    Customer return restocks inventory and adjusts customer receivables.
    Supplier return deduces inventory and reduces supplier payables.
    """
    product = db.query(ProductItemModel).filter(
        ProductItemModel.id == req.product_id,
        ProductItemModel.tenant_id == current_user.tenant_id
    ).first()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    if req.return_type == "customer_return":
        # Restock customer item
        product.quantity_on_hand += req.quantity
        movement = StockMovementModel(
            tenant_id=current_user.tenant_id,
            product_id=product.id,
            movement_type="return_restock",
            quantity_change=req.quantity,
            reference_id=f"cust_return_{now_utc().strftime('%Y%m%d%H%M%S')}"
        )
        db.add(movement)

        # Adjust customer due if applicable
        refund_amount = req.refund_amount_paisa if req.refund_amount_paisa is not None else (req.quantity * product.selling_price_paisa)
        if req.party_id:
            party = db.query(PartyModel).filter(
                PartyModel.id == req.party_id,
                PartyModel.tenant_id == current_user.tenant_id
            ).first()
            if party and req.refund_method == "credit_adjustment":
                party.current_due_paisa = max(0, party.current_due_paisa - refund_amount)

        db.commit()
        return {
            "status": "processed",
            "return_type": "customer_return",
            "product_id": product.id,
            "new_quantity_on_hand": product.quantity_on_hand,
            "refund_amount_paisa": refund_amount,
        }

    else:  # supplier_return
        if product.quantity_on_hand < req.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot return {req.quantity} units to supplier; only {product.quantity_on_hand} units in stock."
            )
        product.quantity_on_hand -= req.quantity
        movement = StockMovementModel(
            tenant_id=current_user.tenant_id,
            product_id=product.id,
            movement_type="supplier_return",
            quantity_change=-req.quantity,
            reference_id=f"supp_return_{now_utc().strftime('%Y%m%d%H%M%S')}"
        )
        db.add(movement)

        refund_amount = req.refund_amount_paisa if req.refund_amount_paisa is not None else (req.quantity * (product.cost_price_paisa or 0))
        if req.party_id:
            party = db.query(PartyModel).filter(
                PartyModel.id == req.party_id,
                PartyModel.tenant_id == current_user.tenant_id
            ).first()
            if party:
                party.current_due_paisa = max(0, party.current_due_paisa - refund_amount)

        db.commit()
        return {
            "status": "processed",
            "return_type": "supplier_return",
            "product_id": product.id,
            "new_quantity_on_hand": product.quantity_on_hand,
            "deducted_supplier_payable_paisa": refund_amount,
        }


# ---------------------------------------------------------
# Reorder Recommendations & Commitments (P09-T02)
# ---------------------------------------------------------

@stock_reports_router.get("/api/v1/stock/reorder-recommendations")
def get_reorder_recommendations(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """
    Evaluate stock levels against lead times, calculate estimated replenishment cost,
    and compare against actual spendable cash in liquid accounts.
    """
    products = db.query(ProductItemModel).filter(
        ProductItemModel.tenant_id == current_user.tenant_id
    ).all()

    # Calculate liquid cash from ledger accounts (1001 Cash, 1002 bKash, 1003 Bank)
    liquid_accounts = db.query(AccountModel).filter(
        AccountModel.tenant_id == current_user.tenant_id,
        AccountModel.code.in_(["1001", "1002", "1003"])
    ).all()
    spendable_cash_paisa = sum(acc.current_balance_paisa for acc in liquid_accounts)

    recommendations = []
    total_reorder_cost_paisa = 0

    for p in products:
        threshold = p.reorder_threshold if p.reorder_threshold is not None else 5
        if p.quantity_on_hand <= threshold:
            # Suggested reorder quantity to bring stock up to 2x threshold or at least 10 units
            suggested_qty = max(10, threshold * 2) - max(0, p.quantity_on_hand)
            unit_cost = p.cost_price_paisa or (int(p.selling_price_paisa * 0.7))
            est_cost = suggested_qty * unit_cost
            total_reorder_cost_paisa += est_cost

            recommendations.append({
                "product_id": p.id,
                "name": p.name,
                "sku": p.sku,
                "quantity_on_hand": p.quantity_on_hand,
                "reorder_threshold": threshold,
                "lead_time_days": p.lead_time_days or 3,
                "suggested_order_qty": suggested_qty,
                "estimated_unit_cost_paisa": unit_cost,
                "estimated_total_cost_paisa": est_cost,
                "urgency": "critical" if p.quantity_on_hand <= 0 else "warning"
            })

    cash_sufficient = spendable_cash_paisa >= total_reorder_cost_paisa
    cash_warning = None
    if not cash_sufficient and len(recommendations) > 0:
        shortfall = total_reorder_cost_paisa - spendable_cash_paisa
        cash_warning = (
            f"Available spendable cash (৳{spendable_cash_paisa // 100:,.0f}) is insufficient to fund "
            f"all recommended reorders (৳{total_reorder_cost_paisa // 100:,.0f}). Shortfall: ৳{shortfall // 100:,.0f}."
        )

    return {
        "spendable_cash_paisa": spendable_cash_paisa,
        "total_recommended_reorder_cost_paisa": total_reorder_cost_paisa,
        "cash_sufficient": cash_sufficient,
        "cash_warning": cash_warning,
        "recommendations": recommendations,
    }


@stock_reports_router.get("/api/v1/commitments")
def list_recurring_commitments(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """List all active recurring commitments (rent, utilities, salaries, loans)."""
    commitments = db.query(RecurringCommitmentModel).filter(
        RecurringCommitmentModel.tenant_id == current_user.tenant_id,
        RecurringCommitmentModel.is_active == True
    ).order_by(RecurringCommitmentModel.due_day_of_month.asc().nullslast()).all()

    return {
        "commitments": [
            {
                "id": c.id,
                "title": c.title,
                "category": c.category,
                "amount_paisa": c.amount_paisa,
                "frequency": c.frequency,
                "party_id": c.party_id,
                "party_name": c.party.name if c.party else None,
                "due_day_of_month": c.due_day_of_month,
                "created_at": c.created_at.isoformat(),
            }
            for c in commitments
        ]
    }


@stock_reports_router.post("/api/v1/commitments", status_code=status.HTTP_201_CREATED)
def create_recurring_commitment(
    req: RecurringCommitmentCreateRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """Register a new recurring commitment for expenditure planning."""
    commitment = RecurringCommitmentModel(
        tenant_id=current_user.tenant_id,
        title=req.title,
        category=req.category,
        amount_paisa=req.amount_paisa,
        frequency=req.frequency,
        party_id=req.party_id,
        due_day_of_month=req.due_day_of_month,
        is_active=True,
    )
    db.add(commitment)
    db.commit()
    db.refresh(commitment)
    return {
        "status": "created",
        "commitment_id": commitment.id,
        "title": commitment.title,
        "amount_paisa": commitment.amount_paisa,
    }


@stock_reports_router.delete("/api/v1/commitments/{commitment_id}")
def delete_recurring_commitment(
    commitment_id: str,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """Deactivate a recurring commitment."""
    commitment = db.query(RecurringCommitmentModel).filter(
        RecurringCommitmentModel.id == commitment_id,
        RecurringCommitmentModel.tenant_id == current_user.tenant_id
    ).first()
    if not commitment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Commitment not found.")

    commitment.is_active = False
    db.commit()
    return {"status": "deactivated", "commitment_id": commitment.id}


# ---------------------------------------------------------
# Operating Reports (P09-T02, D011)
# ---------------------------------------------------------

@stock_reports_router.get("/api/v1/reports/operating-summary")
def get_operating_summary(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """
    Generate complete operating report strictly separating:
    1. Cash Statement (Liquid ledger accounts 1001, 1002, 1003)
    2. Accrual Profit & Loss (Sales revenue, COGS via WAC, operating expenses).
       Flags incomplete cost data and suppresses unsupported profit claims per D011.
    3. Inventory Valuation (Known cost valuation, units on hand, uncosted products count).
    4. Dues Statement (Customer receivables vs Supplier payables).
    """
    if current_user.role == "cashier":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cashier role is not permitted to view complete financial profit reports."
        )

    # 1. Cash Statement
    accounts = db.query(AccountModel).filter(
        AccountModel.tenant_id == current_user.tenant_id
    ).all()
    cash_in_hand = 0
    bkash_balance = 0
    bank_balance = 0
    for acc in accounts:
        if acc.code in (StandardAccount.CASH.value, "1001"):
            cash_in_hand = acc.current_balance_paisa
        elif (acc.code.startswith("asset:wallet:") or acc.code == "1002"):
            bkash_balance += acc.current_balance_paisa
        elif (acc.code.startswith("asset:bank:") or acc.code == "1003"):
            bank_balance += acc.current_balance_paisa
    total_liquid_cash = cash_in_hand + bkash_balance + bank_balance

    # 2. Accrual Profit & Loss
    # Revenue: Account 4001 Sales Revenue credit balance
    rev_acc = next((a for a in accounts if a.code in (StandardAccount.SALES_REVENUE.value, "4001")), None)
    total_revenue_paisa = rev_acc.current_balance_paisa if rev_acc else 0

    # Operating Expenses: Account 5001 Expenses debit balance
    exp_acc = next((a for a in accounts if a.code in (StandardAccount.OPERATING_EXPENSE.value, "5001")), None)
    total_expenses_paisa = exp_acc.current_balance_paisa if exp_acc else 0

    # Calculate COGS from stock movements
    # Check if any sold product is missing cost data
    products = db.query(ProductItemModel).filter(
        ProductItemModel.tenant_id == current_user.tenant_id
    ).all()
    product_map = {p.id: p for p in products}

    sale_movements = db.query(StockMovementModel).filter(
        StockMovementModel.tenant_id == current_user.tenant_id,
        StockMovementModel.movement_type == "sale_deduction"
    ).all()

    cogs_acc = next((a for a in accounts if a.code == StandardAccount.COGS.value), None)
    total_cogs_paisa = cogs_acc.current_balance_paisa if (cogs_acc and cogs_acc.current_balance_paisa > 0) else 0
    cost_data_incomplete = False
    for sm in sale_movements:
        p = product_map.get(sm.product_id)
        if p and p.cost_price_paisa is not None:
            if not (cogs_acc and cogs_acc.current_balance_paisa > 0):
                total_cogs_paisa += abs(sm.quantity_change) * p.cost_price_paisa
        else:
            cost_data_incomplete = True

    # Check uncosted active products
    uncosted_count = sum(1 for p in products if p.cost_price_paisa is None)
    if uncosted_count > 0:
        cost_data_incomplete = True

    # Current product cost is not historical sale cost when sales were recorded without item lines.
    if total_revenue_paisa > 0 and not sale_movements:
        cost_data_incomplete = True

    # If cost data is incomplete, suppress unsupported profit claim (D011)
    if cost_data_incomplete:
        unsupported_profit_suppressed = True
        gross_profit_paisa = None
        net_profit_paisa = None
        profit_disclaimer = (
            "Caution: Incomplete purchase cost data detected. Certain products lack recorded "
            "purchase invoices or WAC cost prices. Unsupported profit claims are suppressed per accounting rule D011."
        )
    else:
        unsupported_profit_suppressed = False
        gross_profit_paisa = total_revenue_paisa - total_cogs_paisa
        net_profit_paisa = gross_profit_paisa - total_expenses_paisa
        profit_disclaimer = "Accrual profit fully verified based on complete recorded purchase costs and ledger entries."

    # 3. Inventory Valuation
    total_units_on_hand = sum(p.quantity_on_hand for p in products)
    known_cost_valuation_paisa = sum(
        (p.quantity_on_hand * p.cost_price_paisa)
        for p in products
        if p.cost_price_paisa is not None and p.quantity_on_hand > 0
    )

    # 4. Dues Statement
    parties = db.query(PartyModel).filter(
        PartyModel.tenant_id == current_user.tenant_id
    ).all()
    customer_receivables = sum(p.current_due_paisa for p in parties if p.party_type == "customer")
    supplier_payables = sum(p.current_due_paisa for p in parties if p.party_type == "supplier")

    return {
        "cash_statement": {
            "cash_in_hand_paisa": cash_in_hand,
            "bkash_balance_paisa": bkash_balance,
            "bank_balance_paisa": bank_balance,
            "total_liquid_cash_paisa": total_liquid_cash,
        },
        "accrual_profit_statement": {
            "total_sales_revenue_paisa": total_revenue_paisa,
            "total_cogs_paisa": total_cogs_paisa if not cost_data_incomplete else None,
            "total_operating_expenses_paisa": total_expenses_paisa,
            "gross_profit_paisa": gross_profit_paisa,
            "net_profit_paisa": net_profit_paisa,
            "cost_data_incomplete": cost_data_incomplete,
            "unsupported_profit_suppressed": unsupported_profit_suppressed,
            "disclaimer": profit_disclaimer,
        },
        "inventory_valuation": {
            "total_products_count": len(products),
            "total_units_on_hand": total_units_on_hand,
            "known_cost_valuation_paisa": known_cost_valuation_paisa,
            "uncosted_products_count": uncosted_count,
        },
        "dues_statement": {
            "customer_receivables_paisa": customer_receivables,
            "supplier_payables_paisa": supplier_payables,
            "net_dues_paisa": customer_receivables - supplier_payables,
        }
    }


@stock_reports_router.get("/api/v1/reports/daily-briefing")
def get_daily_briefing(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """Generate executive morning briefing: cash, sales, low stock alerts, and near-term commitments."""
    # Liquid cash
    liquid_accs = db.query(AccountModel).filter(
        AccountModel.tenant_id == current_user.tenant_id,
        AccountModel.code.in_(["1001", "1002", "1003"])
    ).all()
    total_liquid_cash = sum(a.current_balance_paisa for a in liquid_accs)

    # Today's sales snapshot
    today_str = now_utc().strftime("%Y-%m-%d")
    snapshot = db.query(DailySnapshotModel).filter(
        DailySnapshotModel.tenant_id == current_user.tenant_id,
        DailySnapshotModel.snapshot_date == today_str
    ).first()
    today_sales_paisa = snapshot.total_sales_paisa if snapshot else 0

    # Low stock items
    products = db.query(ProductItemModel).filter(
        ProductItemModel.tenant_id == current_user.tenant_id
    ).all()
    urgent_stock_items = [
        {"name": p.name, "sku": p.sku, "qty": p.quantity_on_hand, "threshold": p.reorder_threshold or 5}
        for p in products
        if p.quantity_on_hand <= (p.reorder_threshold or 5)
    ]

    # Commitments
    commitments = db.query(RecurringCommitmentModel).filter(
        RecurringCommitmentModel.tenant_id == current_user.tenant_id,
        RecurringCommitmentModel.is_active == True
    ).all()
    total_monthly_commitments = sum(c.amount_paisa for c in commitments)

    return {
        "date": today_str,
        "total_liquid_cash_paisa": total_liquid_cash,
        "today_sales_paisa": today_sales_paisa,
        "low_stock_count": len(urgent_stock_items),
        "low_stock_items": urgent_stock_items[:5],
        "active_commitments_count": len(commitments),
        "total_monthly_commitments_paisa": total_monthly_commitments,
    }


# ---------------------------------------------------------
# Forecasting Endpoint (P09-T03)
# ---------------------------------------------------------

@stock_reports_router.get("/api/v1/forecast/sales")
def get_sales_forecast(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """
    History-based 7-day sales forecast adapter.
    Evaluated with rolling-origin historical backtests and explicit cold-start gating.
    """
    snapshots = db.query(DailySnapshotModel).filter(
        DailySnapshotModel.tenant_id == current_user.tenant_id
    ).order_by(DailySnapshotModel.snapshot_date.asc()).all()

    history = [(s.snapshot_date, s.total_sales_paisa) for s in snapshots]
    return HistoryForecastAdapter.evaluate_and_forecast(history)


# ---------------------------------------------------------
# CSV Import / Export Endpoints (P09-T04)
# ---------------------------------------------------------

@stock_reports_router.get("/api/v1/reports/export/products.csv")
def export_products_csv(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """Export all product catalog and inventory state to CSV."""
    if current_user.role == "cashier":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cashier cannot export catalog.")

    products = db.query(ProductItemModel).filter(
        ProductItemModel.tenant_id == current_user.tenant_id
    ).order_by(ProductItemModel.name.asc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["sku", "name", "unit", "quantity_on_hand", "cost_price_bdt", "selling_price_bdt", "reorder_threshold", "lead_time_days"])

    for p in products:
        cost_bdt = f"{p.cost_price_paisa / 100:.2f}" if p.cost_price_paisa is not None else ""
        selling_bdt = f"{p.selling_price_paisa / 100:.2f}"
        writer.writerow([
            spreadsheet_text(p.sku),
            spreadsheet_text(p.name),
            spreadsheet_text(p.unit),
            p.quantity_on_hand,
            cost_bdt,
            selling_bdt,
            p.reorder_threshold or 5,
            p.lead_time_days or 3
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=products_{now_utc().strftime('%Y%m%d')}.csv"}
    )


@stock_reports_router.post("/api/v1/reports/import/products.csv")
def import_products_csv(
    req: CSVImportRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """
    Import product catalog from CSV with deduplication by SKU.
    If SKU exists for tenant, updates values; else creates new product.
    """
    if current_user.role == "cashier":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cashier cannot import catalog.")

    reader = csv.DictReader(io.StringIO(req.csv_content.lstrip('\ufeff').strip()))
    if not reader.fieldnames or 'name' not in reader.fieldnames:
        raise HTTPException(422, 'CSV requires a name column.')
    created_count = 0
    updated_count = 0
    errors = []

    prepared = []
    seen_skus = set()
    seen_blank_sku_names = set()
    for row_idx, row in enumerate(reader, start=1):
        if row_idx > 5000:
            raise HTTPException(422, 'Import at most 5000 products per file.')
        if None in row or any(value is None for value in row.values()):
            errors.append(f'Row {row_idx}: column count does not match the header.')
            continue
        name = row.get("name", "").strip()
        if not name:
            errors.append(f"Row {row_idx}: 'name' is required.")
            continue

        sku = row.get("sku", "").strip() or None
        unit = row.get("unit", "pcs").strip() or "pcs"
        try:
            qty = csv_count(row.get("quantity_on_hand", '0'), 'Quantity')
            cost_bdt_str = row.get("cost_price_bdt", "").strip()
            cost_paisa = csv_paisa(cost_bdt_str) if cost_bdt_str else None
            selling_bdt_str = row.get("selling_price_bdt", "0").strip()
            selling_paisa = csv_paisa(selling_bdt_str)
            threshold = csv_count(row.get("reorder_threshold", '5'), 'Reorder threshold')
            lead_time = csv_count(row.get("lead_time_days", '3'), 'Lead time', 3650)
        except ValueError as ex:
            errors.append(f"Row {row_idx}: invalid numeric format ({ex}).")
            continue

        if sku and sku in seen_skus:
            errors.append(f'Row {row_idx}: duplicate SKU in the same import.')
        if sku:
            seen_skus.add(sku)
        else:
            norm_name = name.casefold()
            if norm_name in seen_blank_sku_names:
                errors.append(f'Row {row_idx}: duplicate product name without SKU in the same import.')
            seen_blank_sku_names.add(norm_name)
        prepared.append((name, sku, unit, qty, cost_paisa, selling_paisa, threshold, lead_time))

    if errors:
        raise HTTPException(422, 'No products were changed. ' + '; '.join(errors[:10]))

    batch_ref = f"csv_import_{generate_uuid()[:8]}"
    total_valuation_delta_paisa = 0

    for name, sku, unit, qty, cost_paisa, selling_paisa, threshold, lead_time in prepared:
        # Existing import remains an explicit owner-reviewed absolute stock count.
        existing = None
        if sku:
            existing = db.query(ProductItemModel).filter(
                ProductItemModel.tenant_id == current_user.tenant_id,
                ProductItemModel.sku == sku
            ).with_for_update().first()
        else:
            existing = db.query(ProductItemModel).filter(
                ProductItemModel.tenant_id == current_user.tenant_id,
                ProductItemModel.sku.is_(None),
                ProductItemModel.name == name,
            ).with_for_update().first()

        if existing:
            verify_tenant_ownership(existing.tenant_id, current_user.tenant_id)
            old_qty = existing.quantity_on_hand or 0
            old_cost = existing.cost_price_paisa or 0
            old_val = old_qty * old_cost if existing.cost_price_paisa is not None else 0
            new_val = qty * (cost_paisa or 0) if cost_paisa is not None else 0
            total_valuation_delta_paisa += (new_val - old_val)
            qty_delta = qty - old_qty

            existing.name = name
            existing.unit = unit
            existing.quantity_on_hand = qty
            existing.cost_price_paisa = cost_paisa
            existing.selling_price_paisa = selling_paisa
            existing.reorder_threshold = threshold
            existing.lead_time_days = lead_time
            if qty_delta != 0:
                db.add(
                    StockMovementModel(
                        tenant_id=current_user.tenant_id,
                        product_id=existing.id,
                        movement_type="csv_import_adjustment",
                        quantity_change=qty_delta,
                        reference_id=batch_ref,
                    )
                )
            updated_count += 1
        else:
            new_prod = ProductItemModel(
                tenant_id=current_user.tenant_id,
                name=name,
                sku=sku,
                unit=unit,
                quantity_on_hand=qty,
                cost_price_paisa=cost_paisa,
                selling_price_paisa=selling_paisa,
                reorder_threshold=threshold,
                lead_time_days=lead_time,
            )
            db.add(new_prod)
            db.flush()
            if qty > 0:
                db.add(
                    StockMovementModel(
                        tenant_id=current_user.tenant_id,
                        product_id=new_prod.id,
                        movement_type="csv_import_adjustment",
                        quantity_change=qty,
                        reference_id=batch_ref,
                    )
                )
            if cost_paisa is not None and qty > 0:
                total_valuation_delta_paisa += qty * cost_paisa
            created_count += 1

    if total_valuation_delta_paisa != 0:
        inv_acc = MerchantService.get_or_create_account(
            db, current_user.tenant_id, StandardAccount.INVENTORY.value, "Inventory", "asset"
        )
        cap_acc = MerchantService.get_or_create_account(
            db, current_user.tenant_id, StandardAccount.OWNER_CAPITAL.value, "Owner capital", "equity"
        )
        abs_delta = abs(total_valuation_delta_paisa)
        inv_dir = "debit" if total_valuation_delta_paisa > 0 else "credit"
        cap_dir = "credit" if total_valuation_delta_paisa > 0 else "debit"
        entry = JournalEntryModel(
            tenant_id=current_user.tenant_id,
            entry_number="CSV-" + batch_ref[-8:].upper(),
            event_id=f"csv-import:{batch_ref}",
            description=f"CSV inventory valuation sync ({batch_ref})",
        )
        db.add(entry)
        db.flush()
        db.add_all([
            PostingModel(journal_entry_id=entry.id, account_id=inv_acc.id, direction=inv_dir, amount_paisa=abs_delta),
            PostingModel(journal_entry_id=entry.id, account_id=cap_acc.id, direction=cap_dir, amount_paisa=abs_delta),
        ])
        inv_acc.current_balance_paisa = max(0, inv_acc.current_balance_paisa + total_valuation_delta_paisa)
        cap_acc.current_balance_paisa = cap_acc.current_balance_paisa + total_valuation_delta_paisa

    db.commit()
    return {
        "total_processed": created_count + updated_count,
        "created_count": created_count,
        "updated_count": updated_count,
        "errors": errors,
    }


@stock_reports_router.get("/api/v1/reports/export/dues.csv")
def export_dues_csv(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    """Export customer receivables and supplier payables to CSV."""
    if current_user.role == "cashier":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cashier cannot export financial dues.")

    parties = db.query(PartyModel).filter(
        PartyModel.tenant_id == current_user.tenant_id,
        PartyModel.current_due_paisa > 0
    ).order_by(PartyModel.current_due_paisa.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["party_name", "party_type", "phone", "due_bdt"])

    for p in parties:
        writer.writerow([
            spreadsheet_text(p.name),
            spreadsheet_text(p.party_type),
            spreadsheet_text(p.phone),
            f"{p.current_due_paisa / 100:.2f}"
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=dues_{now_utc().strftime('%Y%m%d')}.csv"}
    )
