"""core의 유일한 조립 지점(ADR-019). 모든 의존성은 여기서 만들어 생성자로 주입한다.

도메인 모듈·platform·api는 이 모듈을 import하지 않는다(import-linter 계약 (a)).
"""

from __future__ import annotations

from dataclasses import dataclass

from pureiron_core.acquisition.server import AcquisitionServer, AcquisitionService
from pureiron_core.platform.bus import EventBus
from pureiron_core.platform.db import Database
from pureiron_core.platform.migrate import Catalog, SchemaGuard, load_catalog
from pureiron_core.project.stub import PgProjectStateStore
from pureiron_core.settings import Settings, read_secret


@dataclass(slots=True)
class SiteRuntime:
    settings: Settings
    db: Database
    bus: EventBus
    catalog: Catalog
    schema: SchemaGuard
    acquisition: AcquisitionServer
    project: PgProjectStateStore
    updater_token: str | None

    @classmethod
    def build(cls, settings: Settings) -> SiteRuntime:
        db = Database(settings.db_dsn, password=read_secret(settings.db_password_file))
        bus = EventBus()
        catalog = load_catalog(settings.migrations_dir)
        return cls(
            settings=settings,
            db=db,
            bus=bus,
            catalog=catalog,
            schema=SchemaGuard(catalog, db.connection),
            acquisition=AcquisitionServer(
                AcquisitionService(bus), host=settings.grpc_host, port=settings.grpc_port
            ),
            project=PgProjectStateStore(db),
            updater_token=read_secret(settings.updater_token_file),
        )

    async def start(self) -> None:
        await self.db.start()
        await self.acquisition.start()

    async def stop(self) -> None:
        await self.acquisition.stop()
        await self.db.close()
