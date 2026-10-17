// 로그인한 화면의 공통 틀: 세션 확인(없으면 로그인으로), 상단 내비게이션·사용자·로그아웃·미확인 알람 배지,
// 화면들이 나눠 쓰는 WS 연결과 알람 목록.
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { NavLink, Outlet, useNavigate } from "react-router";

import {
  type Alarm,
  api,
  authEvents,
  failureMessage,
  type Load,
  type Me,
  networkMessage,
  notifyUnauthorized,
  ROLE_LABELS,
  UNAUTHORIZED_EVENT,
} from "../api/client";
import { RealtimeClient, wsUrl } from "../ws/client";
import { applyRestAlarm, isUnacked, mergeSnapshot, upsertAlarm } from "./alarms";
import { type Session, SessionContext } from "./session";

/** `/login`과 그 하위. 여기서는 401 리다이렉트도 `?next=` 대상도 만들지 않는다(동시 401의 `?next=` 중첩 방지). */
export function isLoginPath(pathname: string): boolean {
  return pathname === "/login" || pathname.startsWith("/login/");
}

export function loginPath(next: string): string {
  const pathname = next.split(/[?#]/)[0] ?? next;
  return next === "/" || isLoginPath(pathname) ? "/login" : `/login?next=${encodeURIComponent(next)}`;
}

export function Layout() {
  const navigate = useNavigate();
  const [me, setMe] = useState<Load<Me>>({ kind: "loading" });

  useEffect(() => {
    const toLogin = () => {
      if (isLoginPath(window.location.pathname)) return;
      void navigate(loginPath(window.location.pathname + window.location.search), { replace: true });
    };
    authEvents.addEventListener(UNAUTHORIZED_EVENT, toLogin);
    return () => {
      authEvents.removeEventListener(UNAUTHORIZED_EVENT, toLogin);
    };
  }, [navigate]);

  useEffect(() => {
    const controller = new AbortController();
    api
      .GET("/api/v1/auth/me", { signal: controller.signal })
      .then(({ data, response }) => {
        if (data !== undefined) setMe({ kind: "ok", value: data });
        else if (response.status !== 401) setMe({ kind: "error", message: failureMessage(response) });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setMe({ kind: "error", message: networkMessage(err) });
      });
    return () => {
      controller.abort();
    };
  }, []);

  if (me.kind === "loading") return <p className="page-status">로그인 확인 중…</p>;
  if (me.kind === "error") return <p className="page-status error">{me.message}</p>;
  return <SignedIn me={me.value} />;
}

const ALARM_SNAPSHOT_TIMEOUT_MS = 10_000; // 매달린 요청 때문에 이벤트가 버퍼에만 쌓이는 상태를 막는다

function SignedIn({ me }: { me: Me }) {
  const navigate = useNavigate();
  const [realtime] = useState(() => new RealtimeClient(wsUrl(window.location), notifyUnauthorized));
  const connection = useSyncExternalStore(
    useCallback((onChange: () => void) => realtime.on("state", onChange), [realtime]),
    () => realtime.connectionState,
  );
  const [alarms, setAlarms] = useState<Load<Alarm[]>>({ kind: "loading" });

  useEffect(() => {
    realtime.start();
    return () => {
      realtime.stop();
    };
  }, [realtime]);

  useEffect(() => {
    const controller = new AbortController();
    let latest = 0;
    // 목록 요청이 진행 중일 때 도착한 alarm_event는 버리지 않고 모았다가 스냅샷 적용 뒤 도착 순서대로 재생한다.
    // changed_at 비교는 스냅샷과 버퍼를 합칠 때만 쓴다(mergeSnapshot). 그 뒤의 실시간 이벤트는 비교 없이 순서대로 적용한다.
    let inFlight = false;
    let buffered: Alarm[] = [];
    // 재조회가 실패해도 이미 있는 목록(과 미확인 배지)은 지우지 않고 그 위에 버퍼를 재생한다.
    const fail = (message: string) => {
      const replay = buffered;
      inFlight = false;
      buffered = [];
      setAlarms((prev) =>
        prev.kind === "ok" ? { kind: "ok", value: replay.reduce(upsertAlarm, prev.value) } : { kind: "error", message },
      );
    };
    // 처음 한 번 + 재연결(hello)마다: 끊긴 동안 놓친 alarm_event를 목록 전체로 메운다.
    const load = () => {
      const request = ++latest;
      inFlight = true;
      api
        .GET("/api/v1/alarms", {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(ALARM_SNAPSHOT_TIMEOUT_MS)]),
        })
        .then(({ data, response }) => {
          if (request !== latest) return;
          if (data === undefined) {
            fail(failureMessage(response));
            return;
          }
          const replay = buffered;
          inFlight = false;
          buffered = [];
          setAlarms({ kind: "ok", value: mergeSnapshot(data.alarms, replay) });
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted || request !== latest) return;
          fail(networkMessage(err));
        });
    };
    load();
    const offHello = realtime.on("hello", load);
    const offEvent = realtime.on("alarm_event", (alarm) => {
      if (inFlight) {
        buffered.push(alarm);
        return;
      }
      setAlarms((prev) => (prev.kind === "ok" ? { kind: "ok", value: upsertAlarm(prev.value, alarm) } : prev));
    });
    return () => {
      controller.abort();
      offHello();
      offEvent();
    };
  }, [realtime]);

  const applyAlarms = useCallback((changed: readonly Alarm[]) => {
    setAlarms((prev) =>
      prev.kind === "ok" ? { kind: "ok", value: changed.reduce(applyRestAlarm, prev.value) } : prev,
    );
  }, []);

  const session = useMemo<Session>(
    () => ({ me, realtime, connection, alarms, applyAlarms }),
    [me, realtime, connection, alarms, applyAlarms],
  );

  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);

  // 실패하면 세션이 남아 있을 수 있으므로 이동하지 않고 알린다.
  const logout = () => {
    setLogoutError(null);
    setLoggingOut(true);
    api
      .POST("/api/v1/auth/logout")
      .then(({ response }) => {
        if (response.ok) void navigate("/login", { replace: true });
        else setLogoutError(failureMessage(response));
      })
      .catch((err: unknown) => {
        setLogoutError(networkMessage(err));
      })
      .finally(() => {
        setLoggingOut(false);
      });
  };

  const unacked = alarms.kind === "ok" ? alarms.value.filter(isUnacked).length : 0;

  return (
    <SessionContext value={session}>
      <header className="topbar">
        <span className="brand">PureIron</span>
        <nav>
          <NavLink to="/" end>
            현황
          </NavLink>
          <NavLink to="/alarms">
            알람
            {unacked > 0 && (
              <span className="badge" aria-label={`미확인 알람 ${String(unacked)}개`}>
                {unacked}
              </span>
            )}
          </NavLink>
          <NavLink to="/trend">트렌드</NavLink>
          <NavLink to="/devices">장비</NavLink>
          <NavLink to="/audit">감사 로그</NavLink>
        </nav>
        <span className={`conn conn-${connection}`} title="실시간 연결">
          {connection === "open" ? "실시간 연결됨" : connection === "connecting" ? "연결 중…" : "연결 끊김"}
        </span>
        <span className="user">
          {me.display_name ?? me.username} · {ROLE_LABELS[me.role]}
        </span>
        <button type="button" onClick={logout} disabled={loggingOut}>
          로그아웃
        </button>
      </header>
      {logoutError !== null && (
        <p className="error" role="alert">
          로그아웃하지 못했습니다. {logoutError}
        </p>
      )}
      <main>
        <Outlet />
      </main>
    </SessionContext>
  );
}
