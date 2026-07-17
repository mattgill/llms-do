"""FastAPI backend for project management with real-time updates."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Sequence

import jwt
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    declared_attr,
    mapped_column,
    relationship,
    sessionmaker,
)

logger = logging.getLogger("backend_api")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL: str = "postgresql+asyncpg://user:pass@localhost:5432/projects_db"
JWT_SECRET: str = "change-me-in-production"
JWT_ALGORITHM: str = "HS256"
JWT_EXPIRY: int = 3600

# ---------------------------------------------------------------------------
# Engine, Session, Base
# ---------------------------------------------------------------------------

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class Base(DeclarativeBase):
    """Shared declarative base."""

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805
        return cls.__name__.lower() + "s"  # type: ignore[attr-defined]


async def get_db() -> AsyncSession:  # type: ignore[misc]
    """Yield an async database session, closing it on teardown."""
    async with AsyncSessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ProjectStatus(StrEnum):
    active = "active"
    archived = "archived"
    draft = "draft"


class User(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    projects: Mapped[list[Project]] = relationship("Project", back_populates="owner")


class Project(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.draft, nullable=False
    )
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    owner: Mapped[User] = relationship("User", back_populates="projects")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    """Schema for creating a project."""

    name: str = Field(..., min_length=1, max_length=150, examples=["My New Project"])
    description: str | None = Field(None, max_length=2000)
    status: ProjectStatus = ProjectStatus.draft


class ProjectRead(BaseModel):
    """Schema returned when reading a project."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    status: ProjectStatus
    owner_id: int
    created_at: datetime
    updated_at: datetime


class ProjectUpdate(BaseModel):
    """Partial-update schema."""

    name: str | None = Field(None, min_length=1, max_length=150)
    description: str | None = None
    status: ProjectStatus | None = None


class PaginatedResponse(BaseModel):
    """Generic paginated response wrapper."""

    items: list[Any]
    total: int
    page: int
    page_size: int
    pages: int


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def create_access_token(user_id: int) -> str:
    """Create a signed JWT for *user_id* that expires after ``JWT_EXPIRY`` seconds."""
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(seconds=JWT_EXPIRY),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT.  Raises :class:`HTTPException` on failure."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc


# ---------------------------------------------------------------------------
# Authentication middleware / dependency
# ---------------------------------------------------------------------------


async def get_current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Extract and validate the ``Authorization: Bearer <token>`` header.

    Returns the :class:`User` that owns the session.
    """
    auth_header: str | None = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing credentials")
    payload = decode_access_token(auth_header.removeprefix("Bearer ").strip())
    stmt = select(User).where(User.id == int(payload["sub"]))
    result = await db.execute(stmt)
    user: User | None = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


# ---------------------------------------------------------------------------
# Background task example
# ---------------------------------------------------------------------------


async def notify_project_created(project_id: int, project_name: str) -> None:
    """Simulate sending an asynchronous notification after project creation."""
    await asyncio.sleep(0.5)
    logger.info("Project '%s' (id=%d) created – notification dispatched.", project_name, project_id)


# ---------------------------------------------------------------------------
# WebSocket manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Simple in-memory WebSocket connection manager for broadcasting."""

    def __init__(self) -> None:
        self._active: dict[int, list[WebSocket]] = {}

    async def connect(self, project_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self._active.setdefault(project_id, []).append(ws)

    def disconnect(self, project_id: int, ws: WebSocket) -> None:
        if project_id in self._active:
            self._active[project_id] = [c for c in self._active[project_id] if c is not ws]

    async def broadcast(self, project_id: int, message: dict[str, Any]) -> None:
        """Push *message* to every WebSocket subscribed to *project_id*."""
        for ws in self._active.get(project_id, []):
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect(project_id, ws)


manager = ConnectionManager()

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Project Management API",
    version="1.0.0",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def global_exception_handler(_request: Request, exc: Exception) -> Any:
    """Catch-all handler that returns a consistent JSON error body."""
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# Auth endpoint
# ---------------------------------------------------------------------------


@app.post("/auth/login", response_model=TokenResponse)
async def login(
    email: str = Query(..., description="User email"),
    password: str = Query(..., description="User password"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Return a JWT if credentials are valid (stub implementation)."""
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not pwd_context.verify(password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return {"access_token": create_access_token(user.id)}


# ---------------------------------------------------------------------------
# CRUD – Projects
# ---------------------------------------------------------------------------


from fastapi.responses import JSONResponse


@app.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Project:
    """Create a new project and schedule a background notification."""
    project = Project(**payload.model_dump(), owner_id=current_user.id)
    db.add(project)
    await db.commit()
    await db.refresh(project)

    background_tasks.add_task(notify_project_created, project.id, project.name)
    await manager.broadcast(
        project.id,
        {"event": "project_created", "data": jsonable_encoder(ProjectRead.model_validate(project))},
    )
    return project


@app.get("/projects", response_model=PaginatedResponse)
async def list_projects(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    status: ProjectStatus | None = Query(None),
    search: str | None = Query(None, description="Free-text search on name"),
) -> PaginatedResponse:
    """List projects with pagination and optional filtering."""
    stmt = select(Project).where(Project.owner_id == current_user.id)

    if status is not None:
        stmt = stmt.where(Project.status == status)
    if search:
        stmt = stmt.where(Project.name.ilike(f"%{search}%"))

    count_stmt = stmt.order_by(None)  # strip columns for count
    total = (await db.execute(select(count_stmt.subquery().c.id))).scalar() or 0

    rows = (
        await db.execute(stmt.order_by(Project.created_at.desc()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    return PaginatedResponse(
        items=[ProjectRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, (total + page_size - 1) // page_size),
    )


@app.get("/projects/{project_id}", response_model=ProjectRead)
async def read_project(
    project_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Project:
    """Fetch a single project by ID."""
    stmt = select(Project).where(Project.id == project_id, Project.owner_id == current_user.id)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@app.patch("/projects/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: int,
    payload: ProjectUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Project:
    """Partially update a project."""
    stmt = select(Project).where(Project.id == project_id, Project.owner_id == current_user.id)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)

    await db.commit()
    await db.refresh(project)

    await manager.broadcast(
        project.id,
        {"event": "project_updated", "data": jsonable_encoder(ProjectRead.model_validate(project))},
    )
    return project


@app.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Delete a project."""
    stmt = select(Project).where(Project.id == project_id, Project.owner_id == current_user.id)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    await db.delete(project)
    await db.commit()
    await manager.broadcast(project_id, {"event": "project_deleted", "data": {"id": project_id}})


# ---------------------------------------------------------------------------
# WebSocket – real-time updates
# ---------------------------------------------------------------------------


@app.websocket("/ws/projects/{project_id}")
async def websocket_project(websocket: WebSocket, project_id: int) -> None:
    """Subscribe to real-time updates for a specific project."""
    await manager.connect(project_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            await manager.broadcast(project_id, {"event": "client_message", "data": data})
    except WebSocketDisconnect:
        manager.disconnect(project_id, websocket)
    except Exception:
        manager.disconnect(project_id, websocket)
