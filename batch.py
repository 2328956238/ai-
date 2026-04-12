from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import List
import uuid
from ..database import get_db
from .. import models
from ..tasks.batch_processor import process_batch_products
from slowapi import Limiter
from slowapi.util import get_remote_address
import os
from fastapi.responses import FileResponse

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/batch", tags=["batch"])

class BatchProductItem(BaseModel):
    product_id: str
    title: str
    description: str = None
    price: float
    category: str = None
    source_country: str = None
    status: str = "active"

class BatchRequest(BaseModel):
    products: List[BatchProductItem] = Field(..., max_items=1000)

@router.post("/products")
@limiter.limit("10/minute")  # 批量接口限流更严格
def submit_batch(request: Request, batch: BatchRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    提交批量商品导入任务，立即返回 task_id
    """
    if len(batch.products) == 0:
        raise HTTPException(status_code=400, detail="商品列表不能为空")
    if len(batch.products) > 1000:
        raise HTTPException(status_code=400, detail="单次最多1000条商品")

    task_id = str(uuid.uuid4())
    # 创建任务记录
    db_task = models.BatchTask(
        task_id=task_id,
        status="pending",
        total=len(batch.products)
    )
    db.add(db_task)
    db.commit()

    # 将原始数据转为字典列表，传递给后台任务
    products_dict = [item.dict() for item in batch.products]
    background_tasks.add_task(process_batch_products, products_dict, task_id)

    return {"task_id": task_id, "message": "任务已提交，请使用 task_id 查询进度"}

@router.get("/{task_id}/status")
def get_batch_status(task_id: str, db: Session = Depends(get_db)):
    task = db.query(models.BatchTask).filter(models.BatchTask.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id": task.task_id,
        "status": task.status,
        "total": task.total,
        "success": task.success,
        "failed": task.failed,
        "error_file": task.error_file_path,
        "created_at": task.created_at,
        "updated_at": task.updated_at
    }

@router.get("/{task_id}/errors")
def download_error_report(task_id: str, db: Session = Depends(get_db)):
    task = db.query(models.BatchTask).filter(models.BatchTask.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not task.error_file_path or not os.path.exists(task.error_file_path):
        raise HTTPException(status_code=404, detail="错误报告不存在或任务没有错误")
    return FileResponse(task.error_file_path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=f"{task_id}_errors.xlsx")