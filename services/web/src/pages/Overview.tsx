// 태그 현황: `GET /tags`로 시작하고 WS `tag_update`로 갱신한다(구독 직후 서버가 현재값을 한 번 보낸다).
// 쓰기 가능한 태그(현장 스위치 켜짐·선언됨·역할 충족, `GET /writes/targets`)는 행에 쓰기 버튼을 둔다.
import { useEffect, useState } from "react";

import {
  api,
  failureMessage,
  type Load,
  networkMessage,
  type TagValue,
  type WriteTarget,
  type WriteTargets,
} from "../api/client";
import { useSession } from "../app/session";
import { writableTarget } from "../app/writes";
import { formatTime, formatValue } from "../format";
import { WriteDialog } from "./WriteDialog";

type Tags = ReadonlyMap<number, TagValue>;

function merge(tags: Tags, values: readonly TagValue[]): Tags {
  const next = new Map(tags);
  for (const value of values) next.set(value.tag_id, value);
  return next;
}

function writesSummary(
  targets: WriteTargets,
  rows: readonly TagValue[],
  writable: (tagId: number) => WriteTarget | undefined,
): string {
  if (!targets.enabled) return "현장 쓰기 꺼짐";
  return `쓰기 가능 ${String(rows.filter((tag) => writable(tag.tag_id) !== undefined).length)}개`;
}

export function Overview() {
  const { me, realtime } = useSession();
  const [tags, setTags] = useState<Load<Tags>>({ kind: "loading" });
  const [targets, setTargets] = useState<Load<WriteTargets>>({ kind: "loading" });
  const [writing, setWriting] = useState<WriteTarget | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let unsubscribe: () => void = () => undefined;
    const offUpdate = realtime.on("tag_update", (values) => {
      setTags((prev) => (prev.kind === "ok" ? { kind: "ok", value: merge(prev.value, values) } : prev));
    });
    api
      .GET("/api/v1/tags", { signal: controller.signal })
      .then(({ data, response }) => {
        if (controller.signal.aborted) return; // 정리 뒤에 구독하면 해제되지 않는다
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

  useEffect(() => {
    const controller = new AbortController();
    api
      .GET("/api/v1/writes/targets", { signal: controller.signal })
      .then(({ data, response }) => {
        setTargets(
          data === undefined ? { kind: "error", message: failureMessage(response) } : { kind: "ok", value: data },
        );
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setTargets({ kind: "error", message: networkMessage(err) });
      });
    return () => {
      controller.abort();
    };
  }, []);

  if (tags.kind === "loading") return <p className="page-status">태그를 불러오는 중…</p>;
  if (tags.kind === "error") return <p className="page-status error">{tags.message}</p>;

  const rows = [...tags.value.values()];
  const good = rows.filter((tag) => tag.quality === "GOOD").length;
  // 쓰기 대상을 못 불러오면 버튼을 하나도 보이지 않는다(누를 수 없는 버튼을 보이지 않음).
  const writable = (tagId: number) =>
    targets.kind === "ok" ? writableTarget(targets.value, tagId, me.role) : undefined;
  const anyWritable = rows.some((tag) => writable(tag.tag_id) !== undefined);
  return (
    <section>
      <h1>태그 현황</h1>
      <p className="summary">
        전체 {rows.length}개 · GOOD {good}개 · 그 밖 {rows.length - good}개
        {targets.kind === "ok" ? <> · {writesSummary(targets.value, rows, writable)}</> : null}
      </p>
      {targets.kind === "error" ? (
        <p className="notice" role="note">
          쓰기 대상을 불러오지 못해 쓰기 버튼을 표시하지 않습니다. ({targets.message})
        </p>
      ) : null}
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
              {anyWritable ? <th>쓰기</th> : null}
            </tr>
          </thead>
          <tbody>
            {rows.map((tag) => {
              const target = writable(tag.tag_id);
              return (
                <tr key={tag.tag_id} data-tag-path={tag.path}>
                  <td>{tag.path}</td>
                  <td>{tag.device_id}</td>
                  <td className="num">{formatValue(tag.value)}</td>
                  <td>
                    <span className={`quality q-${tag.quality.toLowerCase()}`}>{tag.quality}</span>
                  </td>
                  <td>{formatTime(tag.ts)}</td>
                  {anyWritable ? (
                    <td>
                      {target === undefined ? null : (
                        <button
                          type="button"
                          aria-label={`${tag.path} 쓰기`}
                          onClick={() => {
                            setWriting(target);
                          }}
                        >
                          쓰기
                        </button>
                      )}
                    </td>
                  ) : null}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {writing === null ? null : (
        <WriteDialog
          key={writing.tag_id}
          target={writing}
          current={tags.value.get(writing.tag_id)}
          onClose={() => {
            setWriting(null);
          }}
        />
      )}
    </section>
  );
}
