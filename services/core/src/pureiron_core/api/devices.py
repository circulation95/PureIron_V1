"""`GET /devices`: 장비 통신 상태(viewer 이상, plan M3b D9). 장비 상태는 태그가 아니다 — 정본은
collector `DeviceHealth` → core `DeviceMonitor`. 변화 스트림은 WS `device_update`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from pureiron_core.acquisition.ports import DeviceStatusSource
from pureiron_core.api.auth import SessionGuard
from pureiron_core.auth.ports import Role
from pureiron_core.realtime.protocol import DeviceOut


class DeviceList(BaseModel):
    devices: list[DeviceOut] = Field(description="현재 plan의 장비 전부(device_id 순)")


def build_devices_router(*, devices: DeviceStatusSource, guard: SessionGuard) -> APIRouter:
    router = APIRouter(prefix="/devices", tags=["devices"])
    viewer = Depends(guard.require_role(Role.VIEWER))

    @router.get(
        "",
        operation_id="list_devices",
        dependencies=[viewer],
        responses={status.HTTP_401_UNAUTHORIZED: {"description": "not authenticated"}},
    )
    async def list_devices() -> DeviceList:
        return DeviceList(devices=[DeviceOut.of(d) for d in devices.devices()])

    return router
