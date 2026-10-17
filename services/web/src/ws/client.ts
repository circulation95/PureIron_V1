// 실시간 WS 클라이언트(plan M3b D10). 로그인한 레이아웃이 연결 하나를 만들고 화면들이 나눠 쓴다.
//
// - 인증은 세션 쿠키(같은 origin `/api/v1/ws`). close 4401 = 세션 무효 → 재연결하지 않고 onUnauthorized.
//   4403 = Origin이 Host와 다름(프록시 설정 문제) → 다시 연결해도 같으므로 멈춘다.
// - 그 밖의 끊김은 0.5s부터 두 배씩 최대 10s 간격으로 다시 연결하고, `hello`를 받으면 구독을 다시 보낸다.
// - 서버 `ping`에는 곧바로 `pong`(60s 동안 pong이 없으면 서버가 끊는다).
// - 서버는 20s마다 ping을 보내므로 SILENCE_TIMEOUT_MS(ping 2회 이상) 동안 아무 메시지도 없으면 죽은 연결로 보고
//   소켓을 버린 뒤 같은 backoff 경로로 다시 연결한다.
// - 메시지 타입은 schema.d.ts components(WsServerMessage·WsClientMessage)가 정본이다.
import type { components } from "../api/schema";

type Schemas = components["schemas"];
type ServerMessage = Schemas["WsServerMessage"];
type ClientMessage = Schemas["WsClientMessage"];

export type ConnectionState = "connecting" | "open" | "closed";

interface EventMap {
  hello: Schemas["HelloMessage"];
  tag_update: Schemas["TagValueOut"][];
  alarm_event: Schemas["AlarmEventMessage"];
  device_update: Schemas["DeviceUpdateMessage"];
  state: ConnectionState;
}

type Handlers = { [K in keyof EventMap]: Set<(payload: EventMap[K]) => void> };

const BACKOFF_START_MS = 500;
const BACKOFF_MAX_MS = 10_000;
const SILENCE_TIMEOUT_MS = 50_000;
const CLOSE_NORMAL = 1000;
const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_FORBIDDEN_ORIGIN = 4403;
const SERVER_TYPES: ReadonlySet<string> = new Set(["hello", "tag_update", "alarm_event", "device_update", "ping"]);

function isServerMessage(value: unknown): value is ServerMessage {
  return (
    typeof value === "object" &&
    value !== null &&
    "type" in value &&
    typeof value.type === "string" &&
    SERVER_TYPES.has(value.type)
  );
}

export function wsUrl(location: Location): string {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}/api/v1/ws`;
}

export class RealtimeClient {
  private socket: WebSocket | null = null;
  private retryTimer: number | null = null;
  private silenceTimer: number | null = null;
  private backoffMs = BACKOFF_START_MS;
  private running = false;
  private state: ConnectionState = "closed";
  // tag_id → 구독한 화면 수. 0이 되면 unsubscribe.
  private readonly tagRefs = new Map<number, number>();
  private readonly handlers: Handlers = {
    hello: new Set(),
    tag_update: new Set(),
    alarm_event: new Set(),
    device_update: new Set(),
    state: new Set(),
  };

  constructor(
    private readonly url: string,
    private readonly onUnauthorized: () => void,
  ) {}

  get connectionState(): ConnectionState {
    return this.state;
  }

  /** 연결을 시작한다. stop() 뒤 다시 부를 수 있다(StrictMode의 effect 재실행). */
  start(): void {
    if (this.running) return;
    this.running = true;
    this.backoffMs = BACKOFF_START_MS;
    this.connect();
  }

  stop(): void {
    this.running = false;
    if (this.retryTimer !== null) {
      window.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.clearSilenceTimer();
    const socket = this.socket;
    this.socket = null;
    socket?.close(CLOSE_NORMAL);
    this.setState("closed");
  }

  on<K extends keyof EventMap>(type: K, handler: (payload: EventMap[K]) => void): () => void {
    const set: Set<(payload: EventMap[K]) => void> = this.handlers[type];
    set.add(handler);
    return () => {
      set.delete(handler);
    };
  }

  /** 태그 구독. 돌려준 함수로 해제한다(같은 태그를 여러 화면이 구독해도 마지막 해제 때만 unsubscribe). */
  subscribe(tagIds: readonly number[]): () => void {
    const added = tagIds.filter((id) => {
      const refs = this.tagRefs.get(id) ?? 0;
      this.tagRefs.set(id, refs + 1);
      return refs === 0;
    });
    if (added.length > 0) this.send({ type: "subscribe", tag_ids: added });
    return () => {
      const removed = tagIds.filter((id) => {
        const refs = (this.tagRefs.get(id) ?? 1) - 1;
        if (refs > 0) {
          this.tagRefs.set(id, refs);
          return false;
        }
        this.tagRefs.delete(id);
        return true;
      });
      if (removed.length > 0) this.send({ type: "unsubscribe", tag_ids: removed });
    };
  }

  private connect(): void {
    this.setState("connecting");
    const socket = new WebSocket(this.url);
    this.socket = socket;
    this.armSilenceTimer(socket);
    socket.onmessage = (event: MessageEvent) => {
      this.armSilenceTimer(socket);
      if (typeof event.data === "string") this.receive(event.data);
    };
    socket.onclose = (event: CloseEvent) => {
      if (this.socket !== socket) return; // stop() 또는 새 연결이 이미 대신함
      this.clearSilenceTimer();
      this.socket = null;
      this.setState("closed");
      if (!this.running) return;
      if (event.code === CLOSE_UNAUTHORIZED) {
        this.running = false;
        this.onUnauthorized();
        return;
      }
      if (event.code === CLOSE_FORBIDDEN_ORIGIN) {
        this.running = false;
        return;
      }
      this.scheduleReconnect();
    };
  }

  private armSilenceTimer(socket: WebSocket): void {
    this.clearSilenceTimer();
    this.silenceTimer = window.setTimeout(() => {
      this.silenceTimer = null;
      if (this.socket !== socket) return;
      // 반쯤 끊긴 연결은 close 핸드셰이크가 오래 걸릴 수 있어 핸들러를 떼고 바로 재연결한다.
      this.socket = null;
      socket.onmessage = null;
      socket.onclose = null;
      socket.close();
      this.setState("closed");
      if (this.running) this.scheduleReconnect();
    }, SILENCE_TIMEOUT_MS);
  }

  private clearSilenceTimer(): void {
    if (this.silenceTimer !== null) {
      window.clearTimeout(this.silenceTimer);
      this.silenceTimer = null;
    }
  }

  private scheduleReconnect(): void {
    const delayMs = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, BACKOFF_MAX_MS);
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      if (this.running) this.connect();
    }, delayMs);
  }

  private receive(text: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return; // 형식이 어긋난 프레임은 버린다(서버는 JSON 텍스트 프레임만 보낸다)
    }
    if (!isServerMessage(parsed)) return;
    switch (parsed.type) {
      case "ping":
        this.send({ type: "pong" });
        return;
      case "hello":
        this.backoffMs = BACKOFF_START_MS;
        this.setState("open");
        if (this.tagRefs.size > 0) this.send({ type: "subscribe", tag_ids: [...this.tagRefs.keys()] });
        this.emit("hello", parsed);
        return;
      case "tag_update":
        this.emit("tag_update", parsed.values);
        return;
      case "alarm_event":
        this.emit("alarm_event", parsed);
        return;
      case "device_update":
        this.emit("device_update", parsed);
        return;
    }
  }

  private send(message: ClientMessage): void {
    // 연결 전이면 버린다: hello를 받을 때 현재 구독 전체를 다시 보낸다.
    if (this.socket?.readyState === WebSocket.OPEN && this.state === "open") {
      this.socket.send(JSON.stringify(message));
    }
  }

  private setState(state: ConnectionState): void {
    if (this.state === state) return;
    this.state = state;
    this.emit("state", state);
  }

  private emit<K extends keyof EventMap>(type: K, payload: EventMap[K]): void {
    const set: Set<(payload: EventMap[K]) => void> = this.handlers[type];
    for (const handler of set) handler(payload);
  }
}
