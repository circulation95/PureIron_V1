"""control-api의 유일한 조립 지점(ADR-019 방식). 모든 의존성을 여기서 만들어 생성자로 주입한다.

도메인 모듈·platform·api는 이 모듈을 import하지 않는다(import-linter 계약 (a)).
"""

from __future__ import annotations

from dataclasses import dataclass

from pureiron_control.bundles import BundleStore
from pureiron_control.credentials import PgCredentialStore
from pureiron_control.drafts import DraftStore
from pureiron_control.fleet import PgFleetReader
from pureiron_control.platform.clock import Clock, utc_now
from pureiron_control.reports import PgReportStore
from pureiron_control.settings import Settings, read_secret
from pureiron_control.sites import PgSiteRegistry
from pureiron_core.platform.db import Database
from pureiron_core.platform.migrate import Catalog, SchemaGuard, load_catalog


@dataclass(slots=True)
class ControlRuntime:
    settings: Settings
    db: Database
    catalog: Catalog
    schema: SchemaGuard
    credentials: PgCredentialStore
    sites: PgSiteRegistry
    reports: PgReportStore
    bundles: BundleStore
    drafts: DraftStore
    fleet: PgFleetReader

    @classmethod
    def build(cls, settings: Settings, *, clock: Clock = utc_now) -> ControlRuntime:
        db = Database(settings.db_dsn, password=read_secret(settings.db_password_file))
        catalog = load_catalog(settings.migrations_dir)
        return cls(
            settings=settings,
            db=db,
            catalog=catalog,
            schema=SchemaGuard(catalog, db.connection),
            credentials=PgCredentialStore(db, clock=clock),
            sites=PgSiteRegistry(db, clock=clock),
            reports=PgReportStore(db, clock=clock),
            bundles=BundleStore(
                db, settings.data_dir, clock=clock, max_bytes=settings.max_bundle_bytes
            ),
            drafts=DraftStore(db, settings.data_dir, clock=clock),
            fleet=PgFleetReader(db, clock=clock, poll_s=settings.poll_s),
        )

    async def start(self) -> None:
        await self.db.start()

    async def stop(self) -> None:
        await self.db.close()
