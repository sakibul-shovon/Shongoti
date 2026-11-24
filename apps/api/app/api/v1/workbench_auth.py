"""Workbench authentication uses the existing User/Tenant/JWT boundary.
Phone ownership and recovery are explicitly unverified, not fake SMS flows.
"""
import hashlib
import hmac
import os
import re
import secrets
import shutil
import time
from collections import OrderedDict
from threading import BoundedSemaphore, Lock
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.core.auth import create_access_token, get_current_user
from app.core.database import get_db
from app.domain.ledger import StandardAccount
from app.core.config import settings
from app.judge_seed import seed_judge_workflows
from app.persistence.models import UserModel, TenantModel, UserCredentialModel, AccountModel, ProductItemModel, PartyModel, CaseModel, CaseVersionModel, JournalEntryModel, PostingModel, RevokedSessionModel, StockMovementModel

workbench_auth_router = APIRouter(prefix='/api/v1/auth', tags=['Workbench identity'])
MAX_ATTEMPT_BUCKETS = 2000
_attempts: OrderedDict[str, tuple[float, int]] = OrderedDict()
_lock = Lock()
_hash_slots = BoundedSemaphore(4)


def throttle(request, scope_suffix: str = ''):
    origin=request.headers.get('origin')
    if origin and origin not in set(settings.cors_origins)|{str(request.base_url).rstrip('/')}:
        raise HTTPException(403,'Same-origin authentication required.')
    host = request.client.host if request.client else 'local'
    forwarded = request.headers.get('x-forwarded-for', '').split(',')[0].strip()
    if forwarded and host in ('127.0.0.1', '::1', 'localhost'):
        host = forwarded
    key = f"{host}:{scope_suffix}" if scope_suffix else host
    now = time.monotonic()
    with _lock:
        for old in list(_attempts):
            if now - _attempts[old][0] > 60:
                del _attempts[old]
        started, count = _attempts.get(key, (now, 0))
        if now - started > 60:
            started, count = now, 0
        if count >= 20:
            raise HTTPException(429, 'Too many attempts. Retry in one minute.')
        if key not in _attempts and len(_attempts) >= MAX_ATTEMPT_BUCKETS:
            _attempts.popitem(last=False)
        _attempts[key] = (started, count + 1)
        _attempts.move_to_end(key)


def phone_number(value):
    value = str(value).translate(str.maketrans('০১২৩৪৫৬৭৮৯','0123456789')).replace(' ', '').replace('-', '')
    if value.startswith('+88'): value = value[3:]
    if not re.fullmatch(r'01[3-9]\d{8}', value): raise HTTPException(400, 'Enter a valid Bangladesh mobile number.')
    return value


def password_hash(password, salt, p: int = 1):
    if not _hash_slots.acquire(timeout=5.0):
        raise HTTPException(429, 'Authentication service is busy. Retry shortly.')
    try:
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=p, dklen=64).hex()
    finally:
        _hash_slots.release()


def verify_password(password: str, credential: UserCredentialModel | None) -> bool:
    salt = credential.password_salt if credential else '00' * 16
    candidate = password_hash(password, salt, p=1)
    if not credential:
        return False
    if hmac.compare_digest(candidate, credential.password_hash):
        return True
    # Backward compatibility for any credential hashed with legacy p=5
    legacy_candidate = password_hash(password, salt, p=5)
    return hmac.compare_digest(legacy_candidate, credential.password_hash)

class Register(BaseModel):
    name: str = Field(min_length=2,max_length=100)
    shop_name: str = Field(min_length=2,max_length=100)
    phone: str
    password: str = Field(min_length=12,max_length=128)
class PasswordLogin(BaseModel):
    phone: str
    password: str = Field(min_length=1,max_length=128)

def response(user, db, http_response=None):
    credential = db.get(UserCredentialModel, user.id)
    token=create_access_token(user.id,user.tenant_id,user.role,bool(user.tenant.is_demo))
    if http_response is not None: http_response.set_cookie('shongoti_session',token,httponly=True,samesite='strict',secure=settings.ENVIRONMENT=='production',max_age=86400,path='/')
    judge_case=db.query(CaseModel).filter(CaseModel.tenant_id==user.tenant_id,CaseModel.case_type=='reconciliation').order_by(CaseModel.created_at.asc()).first() if user.tenant.is_demo else None
    return {'access_token':token, 'token_type':'bearer', 'user_id':user.id, 'tenant_id':user.tenant_id, 'role':user.role, 'is_demo':bool(user.tenant.is_demo), 'phone_verified':False, 'name':credential.display_name if credential else 'Judge', 'shop_name':user.tenant.name, 'case_id':judge_case.id if judge_case else None,'execution_mode':'persistent_api'}

@workbench_auth_router.post('/register', status_code=201)
def register(payload:Register, request:Request, http_response:Response, db:Session=Depends(get_db)):
    throttle(request, scope_suffix=payload.phone[:20])
    phone=phone_number(payload.phone)
    if len(payload.name.strip())<2 or len(payload.shop_name.strip())<2: raise HTTPException(400,'Enter your name and shop name.')
    if db.query(UserCredentialModel).filter_by(phone=phone).first(): raise HTTPException(409,'Account already exists. Sign in instead.')
    tenant=TenantModel(name=payload.shop_name.strip(),is_demo=False)
    db.add(tenant);db.flush()
    user=UserModel(tenant_id=tenant.id,phone=phone,role='owner',is_active=True)
    db.add(user);db.flush()
    salt=secrets.token_hex(16)
    db.add(UserCredentialModel(user_id=user.id,phone=phone,display_name=payload.name.strip(),password_salt=salt,password_hash=password_hash(payload.password,salt)))
    try: db.commit()
    except IntegrityError:
        db.rollback();raise HTTPException(409,'Account already exists. Sign in instead.')
    return response(user,db,http_response)

@workbench_auth_router.post('/password-login')
def login(payload:PasswordLogin, request:Request, http_response:Response, db:Session=Depends(get_db)):
    throttle(request, scope_suffix=payload.phone[:20])
    try: phone=phone_number(payload.phone)
    except HTTPException: raise HTTPException(401,'Invalid phone or password.')
    credential=db.query(UserCredentialModel).filter_by(phone=phone).first()
    if not verify_password(payload.password, credential): raise HTTPException(401,'Invalid phone or password.')
    user=db.get(UserModel,credential.user_id)
    if not user or not user.is_active: raise HTTPException(401,'Account unavailable.')
    return response(user,db,http_response)

@workbench_auth_router.get('/me')
def me(user:UserModel=Depends(get_current_user),db:Session=Depends(get_db)):
    data=response(user,db);data.pop('access_token');return data


def _prune_excess_demo_tenants(db: Session, max_keep: int) -> None:
    if max_keep <= 0:
        return
    demo_tenants = (
        db.query(TenantModel)
        .filter(TenantModel.is_demo == True, TenantModel.id != "demo-tenant-001")
        .order_by(TenantModel.created_at.desc())
        .all()
    )
    if len(demo_tenants) >= max_keep:
        from app.ingestion.storage import storage
        for stale_tenant in demo_tenants[max_keep - 1:]:
            stale_dir = os.path.join(storage.base_dir, stale_tenant.id)
            db.delete(stale_tenant)
            shutil.rmtree(stale_dir, ignore_errors=True)
        db.flush()


@workbench_auth_router.post('/judge-session', status_code=201)
def judge(request:Request,http_response:Response,db:Session=Depends(get_db)):
    if not settings.ALLOW_JUDGE_SESSION:
        raise HTTPException(403, 'Judge demo workspace is disabled in this environment.')
    throttle(request, scope_suffix='judge')
    previous=request.cookies.get('shongoti_session')
    if previous:
        digest=hashlib.sha256(previous.encode()).hexdigest()
        if not db.get(RevokedSessionModel,digest): db.add(RevokedSessionModel(token_hash=digest))
    _prune_excess_demo_tenants(db, settings.MAX_DEMO_TENANTS)
    tenant=TenantModel(name='মীনা স্টোর · Judge workspace',is_demo=True)
    db.add(tenant);db.flush()
    user=UserModel(tenant_id=tenant.id,phone='judge-'+secrets.token_hex(8),role='owner',is_active=True)
    db.add(user);db.flush()
    seeds=[(StandardAccount.CASH.value,'নগদ','asset',500000,'debit'),(StandardAccount.MOBILE_NAGAD.value,'Nagad','asset',500000,'debit'),(StandardAccount.MOBILE_BKASH.value,'bKash','asset',200000,'debit'),(StandardAccount.RECEIVABLES.value,'Customer dues','asset',1850000,'debit'),(StandardAccount.INVENTORY.value,'মজুদ পণ্য','asset',476000,'debit'),(StandardAccount.PAYABLES.value,'Supplier dues','liability',2500000,'credit'),(StandardAccount.OWNER_CAPITAL.value,'Synthetic opening equity','equity',1026000,'credit')]
    entry=JournalEntryModel(tenant_id=tenant.id,entry_number='JUDGE-OPENING',description='Synthetic balanced opening; not real merchant history',event_id='judge-opening-'+tenant.id)
    db.add(entry);db.flush()
    for code,name,kind,amount,direction in seeds:
        account=AccountModel(tenant_id=tenant.id,code=code,name=name,type=kind,current_balance_paisa=amount)
        db.add(account);db.flush();db.add(PostingModel(journal_entry_id=entry.id,account_id=account.id,direction=direction,amount_paisa=amount))
    seeded_product = ProductItemModel(tenant_id=tenant.id,name='মিনিকেট চাল ২৫ কেজি',sku='RICE-025',unit='bag',quantity_on_hand=4,cost_price_paisa=119000,selling_price_paisa=130000)
    db.add_all([PartyModel(tenant_id=tenant.id,name='রহমান ট্রেডার্স',party_type='supplier',current_due_paisa=2500000),PartyModel(tenant_id=tenant.id,name='করিম',party_type='customer',current_due_paisa=1850000),seeded_product])
    db.flush()
    db.add(StockMovementModel(tenant_id=tenant.id,product_id=seeded_product.id,movement_type='opening_stock',quantity_change=4,reference_id=entry.id))
    case=CaseModel(tenant_id=tenant.id,case_type='reconciliation',title='Synthetic supplier prior-inclusion investigation',status='waiting_user',current_version=1)
    db.add(case);db.flush();db.add(CaseVersionModel(tenant_id=tenant.id,case_id=case.id,version_num=1,state_snapshot_json='{"origin":"seeded_example"}'));db.flush()
    seed_judge_workflows(db,tenant,case)
    db.commit()
    return {**response(user,db,http_response),'case_id':case.id,'mode':'seeded_merchant_account'}

@workbench_auth_router.post('/logout')
def logout(request:Request,http_response:Response,user:UserModel=Depends(get_current_user),db:Session=Depends(get_db)):
    token=request.headers.get('authorization','').removeprefix('Bearer ') or request.cookies.get('shongoti_session')
    digest=hashlib.sha256(token.encode()).hexdigest()
    if not db.get(RevokedSessionModel,digest): db.add(RevokedSessionModel(token_hash=digest));db.commit()
    http_response.delete_cookie('shongoti_session',path='/',httponly=True,samesite='strict')
    return {'ok':True}
