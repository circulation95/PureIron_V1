"""DB에 저장할 수 없는 문자 검사: jsonb·text는 NUL(JSON `\\u0000`)을 받지 않고, 짝 없는
서로게이트(JSON `\\ud800`)는 UTF-8로 인코딩되지 않는다. 저장 단계에서 500이 되면 그 메시지를 영원히
보낼 수 없으므로(현장 큐 맨 앞이 막힌다) 받기 전에 400으로 거부한다 — 보고·heartbeat·드래프트 공용.
"""

from __future__ import annotations

from pureiron_bundle.sitesign import Json

UNSTORABLE_HINT = "must not contain NUL or unpaired surrogates (\\u0000, \\ud800-\\udfff)"
_NUL = chr(0)


def storable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return _NUL not in text


def unstorable_at(value: Json, where: str) -> str | None:
    """저장할 수 없는 문자가 든 첫 위치(키·값·중첩, `where` 기준 경로). 없으면 None."""
    if isinstance(value, str):
        return None if storable(value) else where
    if isinstance(value, dict):
        for key, item in value.items():
            found = unstorable_at(key, f"{where} key") or unstorable_at(item, f"{where}.{key}")
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = unstorable_at(item, f"{where}[]")
            if found is not None:
                return found
    return None
