"""User model for human users of the marketplace."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Text
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    name = Column(String(100), nullable=False)
    
    # Encrypted JSON containing model API keys
    # Format: {"openai": "sk-...", "anthropic": "sk-ant-...", "openrouter": "..."}
    model_api_keys_encrypted = Column(Text, nullable=True)
    
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    agents = relationship("Agent", back_populates="owner")
    wallet = relationship("Wallet", back_populates="user", uselist=False)
    workflows = relationship("Workflow", back_populates="owner")
    teams = relationship("Team", back_populates="owner")
    
    def __repr__(self):
        return f"<User {self.email}>"
