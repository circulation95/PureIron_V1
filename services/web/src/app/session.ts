// 로그인한 화면들이 함께 쓰는 세션: 현재 사용자, WS 연결 하나, 알람 목록(미확인 배지와 Alarms 화면).
import { createContext, useContext } from "react";

import type { Alarm, Load, Me } from "../api/client";
import type { ConnectionState, RealtimeClient } from "../ws/client";

export interface Session {
  me: Me;
  realtime: RealtimeClient;
  connection: ConnectionState;
  alarms: Load<Alarm[]>;
  /** REST 응답(ACK 등)으로 받은 전이 뒤 알람을 목록에 반영한다. */
  applyAlarms: (alarms: readonly Alarm[]) => void;
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (session === null) throw new Error("useSession outside of the signed-in layout");
  return session;
}
