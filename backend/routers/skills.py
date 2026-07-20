"""Skill catalog routes, including the user-created skill registry."""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.skill import Skill
from models.user import User
from schemas import SkillResponse, SkillCreate
from services.skill_catalog import get_all_skills, get_skill_by_name
from auth import get_current_active_user, get_current_admin_user

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("", response_model=List[SkillResponse])
async def list_skills(
    tier: str = None,
    mine: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List skills.

    By default returns platform skills (core/connected) plus any the caller
    created. Pass ``mine=true`` to return only the caller's own skills.
    """
    if mine:
        result = await db.execute(
            select(Skill).where(Skill.owner_id == current_user.id)
        )
        return result.scalars().all()

    # Platform skills + the caller's own private skills.
    result = await db.execute(
        select(Skill).where(
            (Skill.visibility == "platform") | (Skill.owner_id == current_user.id)
        )
    )
    skills = result.scalars().all()
    if tier:
        skills = [s for s in skills if s.tier == tier]
    return skills


@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(skill_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Skill).where(Skill.id == skill_id))
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return skill


@router.post("", response_model=SkillResponse)
async def create_skill(
    skill_data: SkillCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Create a new skill.

    Any authenticated user can create a skill (it becomes a private,
    user-owned skill). Admins may also create platform skills by setting
    ``visibility="platform"`` in the body (handled below).
    """
    from re import match as _match

    name = skill_data.name.strip()
    if not _match(r"^[a-z0-9_]{2,50}$", name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Skill name must be 2-50 chars: lowercase letters, digits, underscores.",
        )

    existing = await get_skill_by_name(db, name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Skill with name '{name}' already exists",
        )

    is_admin = getattr(current_user, "is_admin", False)
    visibility = skill_data.visibility if hasattr(skill_data, "visibility") else "private"
    if visibility == "platform" and not is_admin:
        # Non-admins cannot publish platform-wide skills.
        visibility = "private"

    skill = Skill(
        name=name,
        display_name=skill_data.display_name,
        description=skill_data.description,
        tier=skill_data.tier,
        category=skill_data.category,
        required_env_vars=skill_data.required_env_vars,
        definition=skill_data.definition,
        source="user" if not is_admin or visibility == "private" else "core",
        visibility=visibility,
        owner_id=None if visibility == "platform" else current_user.id,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill


@router.put("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: str,
    skill_data: SkillCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update a skill the caller owns (or any, if admin)."""
    result = await db.execute(select(Skill).where(Skill.id == skill_id))
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if skill.owner_id != current_user.id and not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your skill")

    for field in ("display_name", "description", "tier", "category",
                  "required_env_vars", "definition"):
        val = getattr(skill_data, field, None)
        if val is not None:
            setattr(skill, field, val)
    await db.commit()
    await db.refresh(skill)
    return skill


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete a skill the caller owns (or any, if admin)."""
    result = await db.execute(select(Skill).where(Skill.id == skill_id))
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if skill.owner_id != current_user.id and not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your skill")
    await db.delete(skill)
    await db.commit()
