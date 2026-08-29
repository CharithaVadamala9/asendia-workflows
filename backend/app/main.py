"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.engine import registry
from app.polling.poller import start_scheduler

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    registry.load_builtin_modules()
    log.info("registered %s workflow modules", len(registry.all_modules()))

    db = SessionLocal()
    try:
        from app.seed import seed_workflow

        seed_workflow(db)
    finally:
        db.close()

    if settings.jobdiva_mode == "mock":
        log.warning("JobDiva is in MOCK mode — set JOBDIVA_MODE=live in backend/.env")
    if settings.vapi_mode == "mock":
        log.warning("VAPI is in MOCK mode — no real calls will be placed")

    scheduler = start_scheduler()
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


settings = get_settings()
app = FastAPI(
    title="Asendia Workflows",
    description=(
        "Template-driven recruitment workflow engine with JobDiva ATS integration, "
        "resume scoring, and AI voice interviews."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/", tags=["health"])
def root() -> dict:
    return {"service": settings.app_name, "docs": "/docs"}
