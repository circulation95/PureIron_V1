// 화면 공통 표시 형식(시각·값). 운영 화면은 현장 시각(브라우저 로캘) 24시간제로 보여준다.

export function formatTime(iso: string | null): string {
  if (iso === null) return "—";
  return new Date(iso).toLocaleString("ko-KR", { hour12: false });
}

export function formatValue(value: number | boolean | null): string {
  if (value === null) return "—";
  if (typeof value === "boolean") return value ? "참" : "거짓";
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}
