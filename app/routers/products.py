from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from .. import crud, schemas
from ..cache import get_redis, safe_call
from ..database import get_db

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/products", tags=["products"])


def _cache_key(product_id: int) -> str:
    return f"product:{product_id}"


def _get_cached_product(product_id: int):
    """缓存读取；Redis 不可用时返回 None，自动退化为查库。"""
    import json

    raw = safe_call("get", _cache_key(product_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _set_cached_product(product_id: int, product_data: dict, ttl: int = 300):
    import json

    safe_call("setex", _cache_key(product_id), ttl, json.dumps(product_data))


def _delete_cached_product(product_id: int):
    safe_call("delete", _cache_key(product_id))


@router.post("/", response_model=schemas.ProductResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
def create_product(request: Request, product: schemas.ProductCreate, db: Session = Depends(get_db)):
    existing = crud.get_product_by_ext_id(db, product.product_id)
    if existing:
        raise HTTPException(status_code=400, detail="Product ID already exists")
    return crud.create_product(db, product)


@router.get("/{product_id}", response_model=schemas.ProductResponse)
@limiter.limit("30/minute")
def read_product(request: Request, product_id: int, db: Session = Depends(get_db)):
    cached = _get_cached_product(product_id)
    if cached:
        safe_call("incr", "cache_hits")
        return cached

    safe_call("incr", "cache_misses")

    db_product = crud.get_product(db, product_id)
    if db_product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    product_dict = {
        "id": db_product.id,
        "product_id": db_product.product_id,
        "title": db_product.title,
        "description": db_product.description,
        "price": db_product.price,
        "category": db_product.category,
        "source_country": db_product.source_country,
        "status": db_product.status.value,
        "created_at": db_product.created_at.isoformat(),
        "updated_at": db_product.updated_at.isoformat() if db_product.updated_at else None,
    }
    _set_cached_product(product_id, product_dict)
    return product_dict


@router.put("/{product_id}", response_model=schemas.ProductResponse)
@limiter.limit("30/minute")
def update_product(request: Request, product_id: int,
                   product_update: schemas.ProductUpdate, db: Session = Depends(get_db)):
    db_product = crud.get_product(db, product_id)
    if db_product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    updated = crud.update_product(db, db_product, product_update)
    _delete_cached_product(product_id)
    return updated


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
def delete_product(request: Request, product_id: int, db: Session = Depends(get_db)):
    db_product = crud.get_product(db, product_id)
    if db_product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    crud.delete_product(db, db_product)
    _delete_cached_product(product_id)


@router.get("/", response_model=List[schemas.ProductResponse])
@limiter.limit("30/minute")
def list_products(request: Request, skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return crud.get_products(db, skip=skip, limit=limit)
