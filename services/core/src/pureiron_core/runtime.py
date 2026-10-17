"""core의 유일한 조립 지점(ADR-019). 모든 의존성은 여기서 만들어 생성자로 주입한다.

도메인 모듈·platform·api는 이 모듈을 import하지 않는다(import-linter 계약 (a)).
"""

from __future__ import annotations

from dataclasses import dataclass

from pureiron_core.acquisition.devices import DeviceMonitor
from pureiron_core.acquisition.ingest import Ingestor
from pureiron_core.acquisition.server import AcquisitionServer, AcquisitionService, PlanKeeper
from pureiron_core.alarms.journal import PgAlarmStore
from pureiron_core.alarms.service import AlarmService
from pureiron_core.audit.store import PgAuditStore
from pureiron_core.auth.service import AuthService
from pureiron_core.auth.sessions import PgSessionStore
from pureiron_core.auth.users import PgUserStore
from pureiron_core.historian.reader import PgHistoryReader
from pureiron_core.historian.recorder import Recorder
from pureiron_core.historian.service import Historian
from pureiron_core.historian.writer import HistoryWriter, PgRowStore
from pureiron_core.platform.bus import EventBus
from pureiron_core.platform.db import Database
from pureiron_core.platform.migrate import Catalog, SchemaGuard, load_catalog
from pureiron_core.project.definitions import PgDefinitionsSource
from pureiron_core.project.stub import PgProjectStateStore
from pureiron_core.realtime.service import RealtimeService
from pureiron_core.settings import Settings, read_secret
from pureiron_core.tags.store import TagStore


@dataclass(slots=True)
class SiteRuntime:
    settings: Settings
    db: Database
    bus: EventBus
    catalog: Catalog
    schema: SchemaGuard
    tags: TagStore
    historian: Historian
    acquisition: AcquisitionServer
    project: PgProjectStateStore
    updater_token: str | None
    auth: AuthService
    audit: PgAuditStore
    alarms: AlarmService
    devices: DeviceMonitor
    realtime: RealtimeService
    history: PgHistoryReader

    @classmethod
    def build(cls, settings: Settings) -> SiteRuntime:
        db = Database(settings.db_dsn, password=read_secret(settings.db_password_file))
        bus = EventBus()
        catalog = load_catalog(settings.migrations_dir)
        tags = TagStore()
        devices = DeviceMonitor(tags, silence_min_s=settings.device_silence_min_s)
        writer = HistoryWriter(
            PgRowStore(db),
            max_rows=settings.historian_buffer_max_rows,
            flush_s=settings.historian_flush_s,
            flush_max_rows=settings.historian_flush_max_rows,
        )
        recorder = Recorder(writer)
        audit = PgAuditStore(db)
        alarms = AlarmService(bus, tags, PgAlarmStore(db, audit))
        ingestor = Ingestor(tags, devices, recorder, live_max_age_s=settings.ingest_live_max_age_s)
        service = AcquisitionService(
            bus, tags, devices, ingestor, session_silence_s=settings.session_silence_s
        )
        auth = AuthService(
            PgUserStore(db),
            PgSessionStore(db),
            idle_s=settings.auth_session_idle_s,
            absolute_s=settings.auth_session_absolute_s,
        )
        keeper = PlanKeeper(
            PgDefinitionsSource(db),
            service.set_plan,
            refresh_s=settings.plan_refresh_s,
            on_definitions=(recorder.set_definitions, alarms.set_definitions),
        )
        return cls(
            settings=settings,
            db=db,
            bus=bus,
            catalog=catalog,
            schema=SchemaGuard(catalog, db.connection),
            tags=tags,
            historian=Historian(recorder, writer),
            acquisition=AcquisitionServer(
                service, keeper, host=settings.grpc_host, port=settings.grpc_port
            ),
            project=PgProjectStateStore(db),
            updater_token=read_secret(settings.updater_token_file),
            auth=auth,
            audit=audit,
            alarms=alarms,
            devices=devices,
            realtime=RealtimeService(bus, tags, devices, auth, batch_ms=settings.realtime_batch_ms),
            history=PgHistoryReader(db),
        )

    async def start(self) -> None:
        # 알람·realtime은 수집보다 먼저: 저널 복원·구독이 첫 샘플보다 앞서야 한다(plan M3b §2.1).
        await self.db.start()
        self.historian.start()
        await self.alarms.start()
        await self.realtime.start()
        await self.acquisition.start()

    async def stop(self) -> None:
        # 수집을 먼저 멈춰 새 행·이벤트를 막고 → WS 연결을 닫고 → 남은 알람 저널·이력을 쓴 뒤(각각
        # 제한 시간) DB를 닫는다.
        await self.acquisition.stop()
        await self.realtime.stop()
        await self.alarms.stop()
        await self.historian.stop()
        await self.db.close()
