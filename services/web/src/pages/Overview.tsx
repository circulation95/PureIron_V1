// 태그 현황: `GET /tags`로 시작하고 WS `tag_update`로 갱신한다(구독 직후 서버가 현재값을 한 번 보낸다).
import { useEffect, useState } from "react";

import { api, failureMessage, type Load, networkMessage, type TagValue } from "../api/client";
import { useSession } from "../app/session";
import { formatTime, formatValue } from "../format";

type Tags = ReadonlyMap<number, TagValue>;

function merge(tags: Tags, values: readonly TagValue[]): Tags {
  const next = new Map(tags);
  for (const value of values) next.set(value.tag_id, value);
  return next;
}

export function Overview() {
  const { realtime } = useSession();
  const [tags, setTags] = useState<Load<Tags>>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    let unsubscribe: () => void = () => undefined;
    const offUpdate = realtime.on("tag_update", (values) => {
      setTags((prev) => (prev.kind === "ok" ? { kind: "ok", value: merge(prev.value, values) } : prev));
    });
    api
      .GET("/api/v1/tags", { signal: controller.signal })
      .then(({ data, response }) => {
        if (data === undefined) {
          setTags({ kind: "error", message: failureMessage(response) });
          return;
        }
        setTags({ kind: "ok", value: merge(new Map(), data.tags) });
        unsubscribe = realtime.subscribe(data.tags.map((tag) => tag.tag_id));
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setTags({ kind: "error", message: networkMessage(err) });
      });
    return () => {
      controller.abort();
      offUpdate();
      unsubscribe();
    };
  }, [realtime]);

  if (tags.kind === "loading") return <p className="page-status">태그를 불러오는 중…</p>;
  if (tags.kind === "error") return <p className="page-status error">{tags.message}</p>;

  const rows = [...tags.value.values()];
  const good = rows.filter((tag) => tag.quality === "GOOD").length;
  return (
    <section>
      <h1>태그 현황</h1>
      <p className="summary">
        전체 {rows.length}개 · GOOD {good}개 · 그 밖 {rows.length - good}개
      </p>
      {rows.length === 0 ? (
        <p>정의된 태그가 없습니다.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>태그</th>
              <th>장비</th>
              <th className="num">값</th>
              <th>품질</th>
              <th>시각</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((tag) => (
              <tr key={tag.tag_id}>
                <td>{tag.path}</td>
                <td>{tag.device_id}</td>
                <td className="num">{formatValue(tag.value)}</td>
                <td>
                  <span className={`quality q-${tag.quality.toLowerCase()}`}>{tag.quality}</span>
                </td>
                <td>{formatTime(tag.ts)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
