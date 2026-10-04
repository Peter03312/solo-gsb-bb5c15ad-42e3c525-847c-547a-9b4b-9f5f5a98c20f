"""FastAPI application exposing the screening-license verification API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .schemas import (
    LicenseCreate,
    LicenseOut,
    PoolCreate,
    PoolOut,
    RevokeOut,
    RevokeRequest,
    VerificationOut,
    VerifyRequest,
)
from .service import ConflictError, NotFoundError, Store, UnprocessableError
from .timeutil import now_iso

DEFAULT_DB_PATH = os.environ.get("FESTIVAL_DB_PATH", "./filmfest.db")


def _seed_demo(store: Store) -> None:
    """Insert demo licenses when SEED_DEMO_DATA is enabled and DB is empty."""
    if store.count_licenses() > 0:
        return
    now = datetime.now(timezone.utc)
    store.create_license(
        film_id="film-autumn-river",
        venue_id="hall-1",
        valid_from=now - timedelta(days=1),
        valid_to=now + timedelta(days=7),
        max_sessions=2,
        note="demo: 早到期授权，核验时优先消耗",
    )
    store.create_license(
        film_id="film-autumn-river",
        venue_id="hall-1",
        valid_from=now - timedelta(days=1),
        valid_to=now + timedelta(days=30),
        max_sessions=5,
        note="demo: 兜底授权",
    )


def create_app(db_path: Optional[str] = None) -> FastAPI:
    db_path = db_path or DEFAULT_DB_PATH
    store = Store(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.ensure()  # 启动即完成建表初始化
        if os.environ.get("SEED_DEMO_DATA", "").lower() in {"1", "true", "yes"}:
            _seed_demo(store)
        yield

    app = FastAPI(
        title="Film Festival Screening License Service",
        version="1.0.0",
        description="电影节片源放映许可核验：授权录入、幂等核验、额度控制与撤销追溯",
        lifespan=lifespan,
    )
    app.state.store = store

    @app.exception_handler(ConflictError)
    async def _conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "code": "CONFLICT", "existing": exc.existing},
        )

    @app.exception_handler(NotFoundError)
    async def _not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(
            status_code=404,
            content={"detail": str(exc), "code": "NOT_FOUND"},
        )

    @app.exception_handler(UnprocessableError)
    async def _unprocessable_handler(request: Request, exc: UnprocessableError):
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "code": exc.code},
        )

    # ------------------------------------------------------------------
    # meta
    # ------------------------------------------------------------------
    @app.get("/", tags=["meta"])
    def root():
        return {
            "service": "film-festival-licensing",
            "version": "1.0.0",
            "docs": "/docs",
            "health": "/health",
        }

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok", "time": now_iso()}

    # ------------------------------------------------------------------
    # quota pools (film-level shared cap across venues)
    # ------------------------------------------------------------------
    @app.post("/pools", response_model=PoolOut, status_code=201, tags=["pools"])
    def create_pool(body: PoolCreate):
        """登记影片级共享额度池（影片 + 正整数上限），供多场馆授权绑定。"""
        return store.create_pool(
            film_id=body.film_id,
            max_sessions=body.max_sessions,
            note=body.note,
        )

    @app.get("/pools", response_model=list[PoolOut], tags=["pools"])
    def list_pools(film_id: Optional[str] = Query(default=None)):
        """列出共享额度池及实时已用/剩余次数，可按 film_id 过滤。"""
        return store.list_pools(film_id=film_id)

    @app.get("/pools/{pool_id}", response_model=PoolOut, tags=["pools"])
    def get_pool(pool_id: int):
        return store.get_pool(pool_id)

    # ------------------------------------------------------------------
    # licenses
    # ------------------------------------------------------------------
    @app.post("/licenses", response_model=LicenseOut, status_code=201, tags=["licenses"])
    def create_license(body: LicenseCreate):
        """录入一份放映授权（影片、场馆、有效时段、场次上限）。

        可选 ``pool_id`` 绑定影片级共享额度池：绑定后不可修改；池不存在
        或池的影片与授权影片不一致时返回 422。不传则保持旧语义。
        """
        return store.create_license(
            film_id=body.film_id,
            venue_id=body.venue_id,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            max_sessions=body.max_sessions,
            note=body.note,
            pool_id=body.pool_id,
        )

    @app.get("/licenses", response_model=list[LicenseOut], tags=["licenses"])
    def list_licenses(
        film_id: Optional[str] = Query(default=None),
        venue_id: Optional[str] = Query(default=None),
        pool_id: Optional[int] = Query(default=None),
    ):
        """列出授权及实时剩余额度、撤销状态、所绑池快照。"""
        return store.list_licenses(
            film_id=film_id, venue_id=venue_id, pool_id=pool_id
        )

    @app.get("/licenses/{license_id}", response_model=LicenseOut, tags=["licenses"])
    def get_license(license_id: int):
        return store.get_license(license_id)

    @app.post(
        "/licenses/{license_id}/revoke",
        response_model=RevokeOut,
        tags=["licenses"],
    )
    def revoke_license(license_id: int, body: RevokeRequest):
        """撤销授权：仅阻止后续核验，已核准场次记录保留可追溯。"""
        return store.revoke_license(license_id, body.reason)

    # ------------------------------------------------------------------
    # verifications
    # ------------------------------------------------------------------
    @app.post("/verifications", response_model=VerificationOut, tags=["verifications"])
    def verify_screening(body: VerifyRequest):
        """核验一个待放映场次（幂等）。

        - 多份授权同时有效时只消耗一份额度，优先最早到期且自身与所绑池
          均有余额的授权；核准时授权与其池各消耗一次；
        - 授权自身仍有余额、但均因所绑池耗尽而不可用时拒绝为
          POOL_EXHAUSTED；
        - 同一幂等键重试返回原结果（replay=true），请求内容变化返回 409；
        - 拒绝时 decision=rejected 并给出 reject_reason。
        """
        return store.verify(
            film_id=body.film_id,
            venue_id=body.venue_id,
            start_time=body.start_time,
            idempotency_key=body.idempotency_key,
        )

    @app.get("/verifications", response_model=list[VerificationOut], tags=["verifications"])
    def list_verifications(
        film_id: Optional[str] = Query(default=None),
        venue_id: Optional[str] = Query(default=None),
        decision: Optional[str] = Query(default=None, pattern="^(approved|rejected)$"),
        license_id: Optional[int] = Query(default=None),
        pool_id: Optional[int] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        """核验流水（含已核准场次的可追溯记录）。"""
        return store.list_verifications(
            film_id=film_id,
            venue_id=venue_id,
            decision=decision,
            license_id=license_id,
            pool_id=pool_id,
            limit=limit,
        )

    @app.get(
        "/verifications/{idempotency_key}",
        response_model=VerificationOut,
        tags=["verifications"],
    )
    def get_verification(idempotency_key: str):
        return store.get_verification(idempotency_key)

    return app


app = create_app()
