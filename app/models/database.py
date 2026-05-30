import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings


class Base(DeclarativeBase):
    pass


def get_database_url() -> str:
    return f"sqlite:///{settings.DATA_DIR}/freeholdy.db"


engine = create_engine(
    get_database_url(),
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    Base.metadata.create_all(bind=engine)
