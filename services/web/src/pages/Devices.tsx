// 장비 통신 상태: `GET /devices`로 시작하고 WS `device_update`로 갱신한다. 재연결(hello)마다 목록을 다시 받는다.
import { useEffect, useState } from "react";

import { api, type Device, failureMessage, type Load, networkMessage } from "../api/client";
import { useSession } from "../app/session";
import { formatTime } from "../format";

type Devices = ReadonlyMap<string, Device>;

function formatNumber(value: number | null, digits: number, unit = ""): string {
  return value === null ? "—" : `${value.toFixed(digits)}${unit}`;
}

export function Devices() {
  const { realtime } = useSession();
  const [devices, setDevices] = useState<Load<Devices>>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    let latest = 0;
    const load = () => {
      const request = ++latest;
      api
        .GET("/api/v1/devices", { signal: controller.signal })
        .then(({ data, response }) => {
          if (request !== latest) return;
          setDevices(
            data === undefined
              ? { kind: "error", message: failureMessage(response) }
              : { kind: "ok", value: new Map(data.devices.map((device) => [device.device_id, device])) },
          );
        })
        .catch((err: unknown) => {
          if (!controller.signal.aborted) setDevices({ kind: "error", message: networkMessage(err) });
        });
    };
    load();
    const offHello = realtime.on("hello", load);
    const offUpdate = realtime.on("device_update", (device) => {
      setDevices((prev) =>
        prev.kind === "ok" ? { kind: "ok", value: new Map(prev.value).set(device.device_id, device) } : prev,
      );
    });
    return () => {
      controller.abort();
      offHello();
      offUpdate();
    };
  }, [realtime]);

  if (devices.kind === "loading") return <p className="page-status">장비 상태를 불러오는 중…</p>;
  if (devices.kind === "error") return <p className="page-status error">{devices.message}</p>;

  const rows = [...devices.value.values()].sort((a, b) => a.device_id.localeCompare(b.device_id));
  return (
    <section>
      <h1>장비 상태</h1>
      {rows.length === 0 ? (
        <p>정의된 장비가 없습니다.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>장비</th>
              <th>프로토콜</th>
              <th>상태</th>
              <th className="num">태그</th>
              <th className="num">BAD 태그</th>
              <th className="num">응답 시간</th>
              <th className="num">읽기 성공률</th>
              <th>마지막 성공</th>
              <th>마지막 오류</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((device) => (
              <tr key={device.device_id}>
                <td>{device.device_id}</td>
                <td>{device.protocol}</td>
                <td>
                  <span
                    className={device.state === "CONNECTED" ? "device-state ok" : "device-state bad"}
                    data-state={device.state}
                  >
                    {device.state === "CONNECTED" ? "연결됨" : "끊김"}
                    {device.silent && " (무신호)"}
                  </span>
                </td>
                <td className="num">{device.tag_count}</td>
                <td className="num">{device.bad_quality_tag_count ?? "—"}</td>
                <td className="num">{formatNumber(device.poll_latency_ms, 0, " ms")}</td>
                <td className="num">
                  {formatNumber(device.read_success_rate === null ? null : device.read_success_rate * 100, 1, "%")}
                </td>
                <td>{formatTime(device.last_success_at)}</td>
                <td title={device.last_error ?? undefined}>
                  {device.last_error === null ? "—" : formatTime(device.last_error_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
