"""DesiredState·번들 내려받기(현장 token)와 번들 게시(ci token), foundation §6.4.

- `GET /sites/{site}/desired` → `{generation, bundle_url, sha256, size}`, `ETag: "<gen>"`,
  `If-None-Match`가 같으면 304, 게시가 없으면 404 — 현장 Updater `poll.py`가 이 형태를 소비한다.
  `bundle_url`은 같은 출처의 절대 경로다(Updater는 같은 출처에만 token을 보낸다).
- `GET /sites/{site}/bundles/{generation}` → `.pib`(`Range` 이어받기 206).
- `PUT /bundles/{site}/{generation}`(ci) → 게시. gen ≤ 최신 게시 409, manifest가 경로와 다르면 400,
  데이터 디렉터리 쓰기 실패 507(고정 detail — 원인은 로그에만).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from pureiron_control.api.body import binary_body
from pureiron_control.api.guard import Guard, SitePath
from pureiron_control.bundles import (
    BundleFormatError,
    BundleSiteNotFoundError,
    BundleStorageError,
    BundleStore,
    GenerationConflictError,
    PublishedBundle,
)
from pureiron_control.credentials import ApiPrincipal, Role
from pureiron_control.platform.files import UploadTooLargeError

log = logging.getLogger(__name__)

BUNDLE_MEDIA_TYPE = "application/octet-stream"
BUNDLE_STORAGE_UNAVAILABLE = "bundle storage unavailable"
GENERATION_MAX = 2**31 - 1
"""`bundles.generation` 열(integer) 상한 — 넘는 gen은 16 GiB를 다 받기 전에 422."""

Generation = Annotated[int, Path(ge=1, le=GENERATION_MAX, description="번들 generation(PD-15)")]


class DesiredOut(BaseModel):
    generation: int
    bundle_url: str = Field(description="같은 출처의 절대 경로(`GET …/bundles/{generation}`)")
    sha256: str = Field(description="`sha256:<hex>`")
    size: int = Field(description="바이트")


class PublishedOut(BaseModel):
    site_id: str
    generation: int
    sha256: str
    size: int
    product_version: str
    project_version: str
    published_at: datetime


def etag_of(generation: int) -> str:
    return f'"{generation}"'


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """`If-None-Match` 목록(약한 비교, RFC 9110 13.1.2)에 `etag`가 있거나 `*`인지."""
    if not if_none_match:
        return False
    tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
    return "*" in tags or etag in tags


def build_bundles_router(*, store: BundleStore, guard: Guard, api_prefix: str) -> APIRouter:
    router = APIRouter(tags=["bundles"])
    own_site = Depends(guard.own_site())
    ci = Depends(guard.roles(Role.CI))

    @router.get(
        "/sites/{site}/desired",
        operation_id="get_desired",
        response_model=DesiredOut,
        responses={304: {"description": "If-None-Match = 현재 gen"}, 404: {}},
    )
    async def desired(request: Request, site: str = own_site) -> Response:
        bundle = await store.desired(site)
        if bundle is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no bundle published")
        etag = etag_of(bundle.generation)
        if etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        root = request.scope.get("root_path", "")
        body = DesiredOut(
            generation=bundle.generation,
            bundle_url=f"{root}{api_prefix}/sites/{site}/bundles/{bundle.generation}",
            sha256=bundle.sha256,
            size=bundle.size_bytes,
        )
        return JSONResponse(body.model_dump(mode="json"), headers={"ETag": etag})

    @router.get(
        "/sites/{site}/bundles/{generation}",
        operation_id="get_bundle",
        response_class=FileResponse,
        responses={
            200: {"content": {BUNDLE_MEDIA_TYPE: {}}},
            206: {"description": "Range 이어받기", "content": {BUNDLE_MEDIA_TYPE: {}}},
            404: {},
        },
    )
    async def download(generation: Generation, site: str = own_site) -> FileResponse:
        bundle = await store.get(site, generation)
        path = store.path(site, generation)
        if bundle is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no such bundle")
        if not path.is_file():
            log.error("published bundle file is missing: %s", path)
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="bundle file missing")
        return FileResponse(
            path, media_type=BUNDLE_MEDIA_TYPE, filename=f"{site}-g{generation}.pib"
        )

    @router.put(
        "/bundles/{site}/{generation}",
        operation_id="publish_bundle",
        status_code=status.HTTP_201_CREATED,
        openapi_extra=binary_body(BUNDLE_MEDIA_TYPE, "서명된 현장 번들 `.pib`"),
        responses={
            400: {"description": "`.pib`가 아니거나 manifest site·gen이 경로와 다름"},
            404: {"description": "미등록 현장"},
            409: {"description": "gen ≤ 최신 게시"},
            413: {},
            507: {"description": "데이터 디렉터리 쓰기 실패"},
        },
    )
    async def publish(
        site: SitePath,
        generation: Generation,
        request: Request,
        principal: ApiPrincipal = ci,
    ) -> PublishedOut:
        try:
            bundle = await store.publish(
                site, generation, request.stream(), published_by=principal.token_id
            )
        except BundleSiteNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GenerationConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except BundleFormatError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except UploadTooLargeError as exc:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
        except BundleStorageError as exc:
            log.error("bundle publish failed, data directory not writable: %s", exc)
            raise HTTPException(
                status.HTTP_507_INSUFFICIENT_STORAGE, detail=BUNDLE_STORAGE_UNAVAILABLE
            ) from exc
        return _published(bundle)

    return router


def _published(bundle: PublishedBundle) -> PublishedOut:
    return PublishedOut(
        site_id=bundle.site_id,
        generation=bundle.generation,
        sha256=bundle.sha256,
        size=bundle.size_bytes,
        product_version=bundle.product_version,
        project_version=bundle.project_version,
        published_at=bundle.published_at,
    )
