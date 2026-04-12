import uuid
from sqlalchemy.orm import Session
from .. import models, crud, schemas
from ..database import SessionLocal
import openpyxl
from openpyxl import Workbook
import os

def process_batch_products(batch_data: list, task_id: str):
    """
    后台任务：批量处理商品，记录错误到 Excel
    """
    db = SessionLocal()
    try:
        # 更新任务状态为 processing
        task = db.query(models.BatchTask).filter(models.BatchTask.task_id == task_id).first()
        if task:
            task.status = "processing"
            task.total = len(batch_data)
            db.commit()

        success_count = 0
        failed_records = []  # 存放 (行号, 原始数据, 错误原因)

        for idx, item in enumerate(batch_data, start=1):
            try:
                # 校验数据（简易：确保 product_id, title, price 存在）
                if not item.get("product_id") or not item.get("title") or item.get("price") is None:
                    raise ValueError("缺少必填字段: product_id, title, price")

                # 创建 ProductCreate 对象
                product_in = schemas.ProductCreate(
                    product_id=item["product_id"],
                    title=item["title"],
                    description=item.get("description"),
                    price=float(item["price"]),
                    category=item.get("category"),
                    source_country=item.get("source_country"),
                    status=item.get("status", "active")
                )
                # 检查 product_id 是否已存在
                existing = crud.get_product_by_ext_id(db, product_in.product_id)
                if existing:
                    raise ValueError(f"product_id '{product_in.product_id}' 已存在")

                crud.create_product(db, product_in)
                success_count += 1
            except Exception as e:
                failed_records.append({
                    "row": idx,
                    "data": item,
                    "error": str(e)
                })

        # 生成错误报告 Excel
        error_file_path = None
        if failed_records:
            error_file_path = _generate_error_report(task_id, failed_records)

        # 更新任务状态为完成
        if task:
            task.status = "completed"
            task.success = success_count
            task.failed = len(failed_records)
            task.error_file_path = error_file_path
            db.commit()
    except Exception as e:
        # 全局异常
        if task:
            task.status = "failed"
            db.commit()
    finally:
        db.close()

def _generate_error_report(task_id: str, failed_records: list) -> str:
    """
    生成 Excel 错误报告，返回文件路径
    """
    os.makedirs("error_reports", exist_ok=True)
    filename = f"error_reports/{task_id}_errors.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "错误记录"
    ws.append(["行号", "原始数据(JSON)", "错误原因"])
    for rec in failed_records:
        ws.append([rec["row"], str(rec["data"]), rec["error"]])
    wb.save(filename)
    return filename