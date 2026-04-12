from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from typing import List
import json
import redis
import os
from dotenv import load_dotenv
from .. import crud, schemas, models
from ..database import get_db
from slowapi import Limiter
from slowapi.util import get_remote_address

load_dotenv()
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/products", tags=["products"])

# 辅助函数：获取缓存 key
def _cache_key(product_id: int) -> str:
    return f"product:{product_id}"

# 辅助函数：从缓存获取商品
def _get_cached_product(product_id: int):
    data = redis_client.get(_cache_key(product_id))
    if data:
        return json.loads(data)
    return None

# 辅助函数：设置缓存
def _set_cached_product(product_id: int, product_data: dict, ttl=300):
    redis_client.setex(_cache_key(product_id), ttl, json.dumps(product_data))

# 辅助函数：删除缓存
def _delete_cached_product(product_id: int):
    redis_client.delete(_cache_key(product_id))

@router.post("/", response_model=schemas.ProductResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
def create_product(request: Request, product: schemas.ProductCreate, db: Session = Depends(get_db)):
    existing = crud.get_product_by_ext_id(db, product.product_id)
    if existing:
        raise HTTPException(status_code=400, detail="Product ID already exists")
    db_product = crud.create_product(db, product)
    # 新创建的商品不主动缓存，等第一次查询时再缓存
    return db_product

@router.get("/{product_id}", response_model=schemas.ProductResponse)
@limiter.limit("30/minute")
def read_product(request: Request, product_id: int, db: Session = Depends(get_db)):
    # 1. 尝试从缓存读取
    cached = _get_cached_product(product_id)
  # 在 read_product 函数中
    if cached:
        redis_client.incr("cache_hits")   # 加这行
        return cached
    else:
        redis_client.incr("cache_misses") # 加这行
    # ... 后面查库逻辑不变
    
    # 2. 缓存未命中，查数据库
    db_product = crud.get_product(db, product_id)
    if db_product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    
    # 3. 将数据库结果存入缓存（转换为 dict）
    product_dict = {
        "id": db_product.id,
        "product_id": db_product.product_id,
        "title": db_product.title,
        "description": db_product.description,
        "price": db_product.price,
        "category": db_product.category,
        "source_country": db_product.source_country,
        "status": db_product.status.value,  # Enum -> str
        "created_at": db_product.created_at.isoformat(),
        "updated_at": db_product.updated_at.isoformat() if db_product.updated_at else None
    }
    _set_cached_product(product_id, product_dict)
    return product_dict

@router.put("/{product_id}", response_model=schemas.ProductResponse)
@limiter.limit("30/minute")
def update_product(request: Request, product_id: int, product_update: schemas.ProductUpdate, db: Session = Depends(get_db)):
    db_product = crud.get_product(db, product_id)
    if db_product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    updated = crud.update_product(db, db_product, product_update)
    # 更新后删除缓存，下次查询重新加载
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
    # 列表接口一般不做缓存，直接查库
    products = crud.get_products(db, skip=skip, limit=limit)
    return products