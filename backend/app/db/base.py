"""
Declarative Base for SQLAlchemy models.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative Base for all application ORM models."""
    pass

