from sqlalchemy.orm import Session
from . import models, schemas

def get_product(db: Session, product_id: int):
    return db.query(models.Product).filter(models.Product.id == product_id).first()

def get_product_by_ext_id(db: Session, ext_id: str):
    return db.query(models.Product).filter(models.Product.product_id == ext_id).first()

def get_products(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.Product).offset(skip).limit(limit).all()

def create_product(db: Session, product: schemas.ProductCreate):
    db_product = models.Product(
        product_id=product.product_id,
        title=product.title,
        description=product.description,
        price=product.price,
        category=product.category,
        source_country=product.source_country,
        status=product.status
    )
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    return db_product

def update_product(db: Session, db_product: models.Product, product_update: schemas.ProductUpdate):
    for var, value in vars(product_update).items():
        if value is not None:
            setattr(db_product, var, value)
    db.commit()
    db.refresh(db_product)
    return db_product

def delete_product(db: Session, db_product: models.Product):
    db.delete(db_product)
    db.commit()