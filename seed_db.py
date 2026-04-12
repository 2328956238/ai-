#!/usr/bin/env python3
"""
随机生成 100 条数据并插入 product_db 数据库
根据实际表结构：id, product_id, title, description, price, category, source_country, status, created_at, updated_at
"""

import random
from datetime import datetime, timedelta
from faker import Faker
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Enum
from sqlalchemy.orm import sessionmaker, declarative_base
import uuid

# ---------- 1. 数据库连接 ----------
DATABASE_URL = "mysql+pymysql://root:root123@localhost:3306/product_db"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ---------- 2. 定义模型（与现有表结构一致）----------
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(String(50), unique=True, nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(String(1000), nullable=True)
    price = Column(Float, nullable=False)
    category = Column(String(50), nullable=True)
    source_country = Column(String(50), nullable=True)
    status = Column(Enum('ACTIVE', 'INACTIVE'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, nullable=True)

# ---------- 3. 生成随机数据 ----------
fake = Faker()

# 可能的类别和产地
CATEGORIES = ["Electronics", "Clothing", "Books", "Home", "Sports", "Toys", "Food", "Beauty"]
COUNTRIES = ["China", "USA", "Germany", "Japan", "Vietnam", "India", "Italy", "France"]
STATUSES = ["ACTIVE", "INACTIVE"]

def random_date(start_year=2023, end_year=2025):
    """生成随机日期"""
    start = datetime(start_year, 1, 1)
    end = datetime(end_year, 12, 31)
    return fake.date_time_between(start_date=start, end_date=end)

def generate_product(index):
    """生成一条随机产品数据"""
    # product_id 使用 uuid 的前8位 + 序号保证唯一性
    unique_id = str(uuid.uuid4())[:8]
    return {
        "product_id": f"PROD-{unique_id}-{index}",
        "title": fake.catch_phrase()[:200],  # 随机短语，限制长度
        "description": fake.text(max_nb_chars=500) if random.choice([True, False]) else None,
        "price": round(random.uniform(5.0, 1999.99), 2),
        "category": random.choice(CATEGORIES),
        "source_country": random.choice(COUNTRIES),
        "status": random.choice(STATUSES),
        "created_at": random_date(),
        "updated_at": random_date() if random.choice([True, False]) else None,
    }

# ---------- 4. 插入100条数据 ----------
def seed_database(num_records=100):
    db = SessionLocal()
    try:
        for i in range(num_records):
            data = generate_product(i+1)
            product = Product(**data)
            db.add(product)
        db.commit()
        print(f"✅ 成功插入 {num_records} 条随机数据到 products 表")
    except Exception as e:
        db.rollback()
        print(f"❌ 插入失败: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    seed_database(100)