// fleet 표(읽기 전용). 판정은 fleetView.ts — 여기서는 그리기만 한다.
import { formatTime, LABELS, sourceLabel, timeZoneLabel, type CurrentCell, type SiteView } from "./fleetView";

function Current({ cell }: { cell: CurrentCell }) {
  if (cell.kind === "none") return <span className="summary">{LABELS.noCurrent}</span>;
  const stale = cell.kind === "last-reported";
  return (
    <div className={stale ? "current stale" : "current"}>
      {stale && <span className="last-reported">{LABELS.lastReported}</span>}
      <span>{cell.version}</span>
      <span className="summary">
        {LABELS.reportedAt} {formatTime(cell.reportedAt)} · {sourceLabel(cell.source)}
      </span>
    </div>
  );
}

export function FleetTable({ rows }: { rows: SiteView[] }) {
  if (rows.length === 0) return <p className="page-status">등록된 현장이 없습니다.</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>현장</th>
          <th>연결</th>
          <th>목표(desired)</th>
          <th>현재(current)</th>
          <th>
            {LABELS.lastSeen} <span className="summary">({timeZoneLabel()})</span>
          </th>
          <th>마지막 적용 시도</th>
          <th>디스크</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.site} data-site={row.site} data-connection={row.connection}>
            <td>{row.site}</td>
            <td>
              <span className={`conn conn-${row.connection}`}>{row.connectionLabel}</span>
            </td>
            <td>{row.desired}</td>
            <td data-col="current">
              <Current cell={row.current} />
              {row.latest && <span className="badge-latest">{LABELS.latest}</span>}
            </td>
            <td data-col="last-seen">{row.lastSeen === null ? "-" : formatTime(row.lastSeen)}</td>
            <td>
              {row.attempt === null ? (
                "-"
              ) : (
                <>
                  {row.attempt} <span className="summary">({row.attemptSource})</span>
                </>
              )}
            </td>
            <td>{row.diskState}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
