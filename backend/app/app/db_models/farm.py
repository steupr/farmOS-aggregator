from sqlalchemy import Boolean, Column, Integer, String

from app.db.base_class import Base


class Farm(Base):
    id = Column(Integer, primary_key=True, index=True)
    farm_name = Column(String, index=True)
    url = Column(String, index=True)
    username = Column(String, index=True)
    password = Column(String, index=True)
    is_authenticated = Column(Boolean, index=True)