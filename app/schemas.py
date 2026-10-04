"""Request/response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class PoolCreate(BaseModel):
    film_id: str = Field(min_length=1, max_length=128, examples=["film-autumn-river"])
    max_sessions: int = Field(
        ge=1, le=1_000_000,
        description="影片级共享总放映次数上限（正整数）",
        examples=[10],
    )
    name: Optional[str] = Field(default=None, max_length=200, examples=["跨场馆共享额度池"])
    note: Optional[str] = Field(default=None, max_length=500)


class PoolOut(BaseModel):
    id: int
    film_id: str
    name: Optional[str]
    max_sessions: int
    used_sessions: int = Field(description="已由绑定授权的核准核验占用的次数（撤销不退还）")
    remaining_sessions: int
    note: Optional[str]
    created_at: str


class PoolRef(BaseModel):
    """授权详情中内嵌的池摘要（与 GET /pools/{id} 口径一致）。"""

    id: int
    film_id: str
    name: Optional[str]
    max_sessions: int
    used_sessions: int
    remaining_sessions: int


class LicenseCreate(BaseModel):
    film_id: str = Field(min_length=1, max_length=128, examples=["film-autumn-river"])
    venue_id: str = Field(min_length=1, max_length=128, examples=["hall-1"])
    valid_from: datetime = Field(examples=["2026-10-01T00:00:00Z"])
    valid_to: datetime = Field(examples=["2026-10-31T23:59:59Z"])
    max_sessions: int = Field(ge=1, le=1_000_000, examples=[5])
    pool_id: Optional[int] = Field(
        default=None,
        description="可选：绑定同影片的共享额度池；绑定后不可修改。池不存在或影片不符返回 422",
        examples=[1],
    )
    note: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_window(self) -> "LicenseCreate":
        if self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class RevocationOut(BaseModel):
    reason: str
    revoked_at: str


class LicenseOut(BaseModel):
    id: int
    film_id: str
    venue_id: str
    valid_from: str
    valid_to: str
    max_sessions: int
    used_sessions: int
    remaining_sessions: int
    pool_id: Optional[int] = Field(description="绑定的共享额度池 id；null 表示未绑定（旧语义）")
    pool: Optional[PoolRef] = Field(description="所绑池的实时已用/剩余，未绑定时为 null")
    revoked: bool
    revocation: Optional[RevocationOut]
    note: Optional[str]
    created_at: str


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, examples=["版权方临时撤片"])


class RevokeOut(BaseModel):
    id: int
    license_id: int
    reason: str
    revoked_at: str


class VerifyRequest(BaseModel):
    film_id: str = Field(min_length=1, max_length=128)
    venue_id: str = Field(min_length=1, max_length=128)
    start_time: datetime = Field(
        description="场次开始时间，ISO-8601；不带时区时按 UTC 处理",
        examples=["2026-10-05T19:30:00Z"],
    )
    idempotency_key: str = Field(
        min_length=1, max_length=128,
        description="业务幂等键；重复提交返回原结果，内容变化则 409",
        examples=["order-20261005-0001"],
    )


class VerificationOut(BaseModel):
    id: int
    idempotency_key: str
    film_id: str
    venue_id: str
    start_time: str
    decision: str = Field(description="approved 或 rejected")
    license_id: Optional[int] = Field(description="命中并消耗额度的授权 id")
    pool_id: Optional[int] = Field(description="命中授权绑定的池 id；未绑定为 null")
    reject_reason: Optional[str] = Field(
        description="拒绝原因：NO_LICENSE / TIME_OUT_OF_WINDOW / LICENSE_REVOKED / "
                    "QUOTA_EXHAUSTED / POOL_EXHAUSTED"
    )
    remaining_quota: Optional[int] = Field(description="本次消耗后该授权自身的剩余额度")
    pool_remaining_quota: Optional[int] = Field(
        description="本次消耗后所绑池的剩余额度；未绑定或被拒绝时为 null"
    )
    verified_at: str
    replay: bool = Field(description="true 表示本次为幂等重放，未重新扣减额度")
