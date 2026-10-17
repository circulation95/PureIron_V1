// 트렌드: 태그(최대 8개)·기간 프리셋을 골라 `GET /history?resolution=auto`를 uPlot(canvas)으로 그린다.
// auto는 core가 기간으로 고른다(≤1h 원시 · ≤24h 1분 · 그 이상 1시간 bucket). bucket은 평균값을 잇는다.
import { useEffect, useMemo, useRef, useState } from "react";
import uPlot from "uplot";

import {
  api,
  failureMessage,
  type History,
  type HistorySeries,
  type Load,
  networkMessage,
  type TagValue,
} from "../api/client";

const MAX_TAGS = 8;
const CHART_HEIGHT_PX = 360;
const SERIES_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#4b5563"];

const RANGES = {
  "1h": { label: "1시간", ms: 60 * 60 * 1000 },
  "24h": { label: "24시간", ms: 24 * 60 * 60 * 1000 },
  "7d": { label: "7일", ms: 7 * 24 * 60 * 60 * 1000 },
} as const;
type Range = keyof typeof RANGES;
const RANGE_KEYS: readonly Range[] = ["1h", "24h", "7d"];

const RESOLUTION_LABELS: Record<History["resolution"], string> = { raw: "원시 샘플", "1m": "1분 평균", "1h": "1시간 평균" };

function toNumber(value: number | boolean | null): number | null {
  return typeof value === "boolean" ? Number(value) : value;
}

function toTable(series: HistorySeries): uPlot.AlignedData {
  if (series.raw.length > 0) {
    return [series.raw.map((point) => Date.parse(point.time) / 1000), series.raw.map((point) => toNumber(point.value))];
  }
  return [series.buckets.map((bucket) => Date.parse(bucket.time) / 1000), series.buckets.map((bucket) => bucket.avg)];
}

export function Trend() {
  const [tags, setTags] = useState<Load<TagValue[]>>({ kind: "loading" });
  const [selected, setSelected] = useState<readonly number[]>([]);
  const [range, setRange] = useState<Range>("1h");
  const [refresh, setRefresh] = useState(0);
  const [history, setHistory] = useState<Load<History>>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    api
      .GET("/api/v1/tags", { signal: controller.signal })
      .then(({ data, response }) => {
        if (data === undefined) {
          setTags({ kind: "error", message: failureMessage(response) });
          return;
        }
        setTags({ kind: "ok", value: data.tags });
        const first = data.tags[0];
        if (first !== undefined) setSelected([first.tag_id]);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setTags({ kind: "error", message: networkMessage(err) });
      });
    return () => {
      controller.abort();
    };
  }, []);

  useEffect(() => {
    if (selected.length === 0) return;
    const controller = new AbortController();
    const end = new Date();
    const start = new Date(end.getTime() - RANGES[range].ms);
    api
      .GET("/api/v1/history", {
        params: {
          query: {
            tag_id: [...selected],
            start: start.toISOString(),
            end: end.toISOString(),
            resolution: "auto",
          },
        },
        signal: controller.signal,
      })
      .then(({ data, response }) => {
        setHistory(
          data === undefined
            ? { kind: "error", message: failureMessage(response) }
            : { kind: "ok", value: data },
        );
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setHistory({ kind: "error", message: networkMessage(err) });
      });
    return () => {
      controller.abort();
    };
  }, [selected, range, refresh]);

  const paths = useMemo(
    () => new Map(tags.kind === "ok" ? tags.value.map((tag) => [tag.tag_id, tag.path]) : []),
    [tags],
  );

  const toggle = (tagId: number) => {
    setHistory({ kind: "loading" });
    setSelected((prev) => (prev.includes(tagId) ? prev.filter((id) => id !== tagId) : [...prev, tagId]));
  };

  if (tags.kind === "loading") return <p className="page-status">태그를 불러오는 중…</p>;
  if (tags.kind === "error") return <p className="page-status error">{tags.message}</p>;

  return (
    <section>
      <h1>트렌드</h1>
      <div className="toolbar">
        <div className="segmented" role="group" aria-label="기간">
          {RANGE_KEYS.map((key) => (
            <button
              key={key}
              type="button"
              aria-pressed={range === key}
              onClick={() => {
                setHistory({ kind: "loading" });
                setRange(key);
              }}
            >
              {RANGES[key].label}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => {
            setHistory({ kind: "loading" });
            setRefresh((n) => n + 1);
          }}
        >
          새로고침
        </button>
        <span className="summary">
          {selected.length}/{MAX_TAGS}개 선택
        </span>
      </div>
      <div className="trend">
        <fieldset className="tag-picker">
          <legend>태그</legend>
          {tags.value.map((tag) => (
            <label key={tag.tag_id}>
              <input
                type="checkbox"
                checked={selected.includes(tag.tag_id)}
                disabled={!selected.includes(tag.tag_id) && selected.length >= MAX_TAGS}
                onChange={() => {
                  toggle(tag.tag_id);
                }}
              />
              {tag.path}
            </label>
          ))}
        </fieldset>
        <div className="trend-body">
          {selected.length === 0 ? (
            <p>태그를 선택하세요.</p>
          ) : history.kind === "loading" ? (
            <p className="page-status">이력을 불러오는 중…</p>
          ) : history.kind === "error" ? (
            <p className="page-status error">{history.message}</p>
          ) : (
            <HistoryView history={history.value} paths={paths} />
          )}
        </div>
      </div>
    </section>
  );
}

function HistoryView({ history, paths }: { history: History; paths: ReadonlyMap<number, string> }) {
  const labels = useMemo(
    () => history.series.map((series) => paths.get(series.tag_id) ?? `tag ${String(series.tag_id)}`),
    [history, paths],
  );
  const data = useMemo(() => uPlot.join(history.series.map(toTable)), [history]);
  const truncated = history.series.filter((series) => series.truncated);
  return (
    <>
      <p className="summary">해상도: {RESOLUTION_LABELS[history.resolution]}</p>
      {truncated.length > 0 && (
        <p className="notice" role="note">
          행 상한을 넘어 앞부분만 표시: {truncated.map((series) => paths.get(series.tag_id) ?? `tag ${String(series.tag_id)}`).join(", ")}
        </p>
      )}
      <TrendChart data={data} labels={labels} />
    </>
  );
}

function TrendChart({ data, labels }: { data: uPlot.AlignedData; labels: readonly string[] }) {
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = container.current;
    if (element === null) return;
    const chart = new uPlot(
      {
        width: element.clientWidth,
        height: CHART_HEIGHT_PX,
        scales: { x: { time: true } },
        series: [
          {},
          ...labels.map((label, index) => ({ label, stroke: SERIES_COLORS[index] ?? "#888", width: 1.5 })),
        ],
      },
      data,
      element,
    );
    const observer = new ResizeObserver(() => {
      chart.setSize({ width: element.clientWidth, height: CHART_HEIGHT_PX });
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
      chart.destroy();
    };
  }, [data, labels]);

  return <div ref={container} className="chart" data-testid="trend-chart" />;
}
