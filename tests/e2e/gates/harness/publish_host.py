"""publish 호스트 동기화(M6-4a A2, 상위 계획 D11) — 세션마다 새로 띄운 중앙(`central.py up`: 새
CA·새 토큰)과 nightly NEXT·FAULT 이미지를 publish 호스트(WSL, runner `publish-WANG`, 사용자
`pureiron-publish`)가 쓰게 맞추는 WSL 쪽 스크립트. Windows 쪽 호출은
`fleet_session.sync_publish_host`.

**root로 돈다**(`wsl -u root` — shwang의 `sudo -n`은 비밀번호를 요구한다, A0 ⑤). 저장소 사본의 venv
Python으로 직접 부른다(`uv`·바이트코드 쓰기 없이):

    cd ~shwang/m2d-e2e/repo && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \\
        tests/e2e/gates/harness/publish_host.py <명령> …

- `trust --ca C --sha256 FP --check-url U`: 중앙 루트 CA를 **OS 신뢰 저장소**
  (`/usr/local/share/ca-certificates/pureiron-central-root.crt` + `update-ca-certificates`)에
  넣는다. publish(`ControlApi`, `ssl_context` 없음 = http.client 기본)·check-gen은 OS 신뢰 저장소로
  중앙 HTTPS를 검증한다(M4 마감 15분 실측과 같은 경로). 같은 파일 이름을 덮어쓰므로 지난 세션의 CA는
  남지 않는다. 넣기 전에 지문(`central.py status`의 `ca_sha256`)을 대조하고, 넣은 뒤 같은 Python의
  기본 신뢰(`cafile` 없음)로 `healthz` 200을 본다.
- `tokens --secrets DIR`: 중앙 비밀 디렉터리의 `ci_token`·`publish_viewer_token`을
  `/etc/pureiron/publish/{ci_token, viewer_token}`(소유 `pureiron-publish`, 0600)으로. 파일을
  `install -m 0600 -o … /dev/null`로 먼저 만들고 값은 `cat`의 stdin으로만 넘긴다(인자·로그에 남기지
  않는다).
- `images --store S --names NEXT,FAULT-h,FAULT-m`: `<store>/<이름>/*.tar`(nightly 작업 디렉터리 —
  symlink는 따라간다)를 `/srv/pureiron/images/<이름>/`(publish runner의 `PUREIRON_IMAGE_STORE`)에
  hardlink한다(같은 ext4, 안 되면 복사). nightly 이미지 tar는 0600이라 publish 사용자가 읽도록
  0644로 맞춘다(hardlink는 inode를 나눠 쓰므로 nightly 사본도 0644가 된다 — 이미지는 비밀이 아니다).
  그 뒤 nightly 이름(`n<sha7>`·FAULT 파생)만 `bundles.nightly_prune`으로 nightly 작업 디렉터리와
  같은 개수(`bundles.NIGHTLY_KEEP`)만 남긴다 — `r*` release는 건드리지 않는다.
- `chain --repo OWNER/NAME`: 승인 사슬 `last_approved`(`/srv/pureiron/publish/<owner>/<repo>/`).
- `trust-base --repo R --sha S --reason TEXT`: publish 사용자로(`runuser`) runner의 도구 checkout
  venv에서 `publish.py trust-base`(사슬 복구 — 마지막 수단, 사유를 남긴다).

대상 경로는 모두 인자로 바꿀 수 있다 — 단위 시험과 확인 실행은 tmp 경로에(`--no-update`,
`--owner ""`)만 쓴다. 출력은 JSON 한 줄(비밀값 없음), 실패는 exit 1과 stderr 한 줄. Windows 쪽은
스크립트 실패를 `InfraError`로 본다(publish 호스트 준비는 실행 환경이다).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.bundles import NIGHTLY_KEEP, nightly_group, nightly_prune
from harness.release_pair import HEAD_SHA

TRUST_FILE = Path("/usr/local/share/ca-certificates/pureiron-central-root.crt")
PUBLISH_ETC = Path("/etc/pureiron/publish")
PUBLISH_USER = "pureiron-publish"
IMAGE_STORE = Path("/srv/pureiron/images")
CHAIN_ROOT = Path("/srv/pureiron/publish")
TOKEN_FILES = {"ci_token": "ci_token", "publish_viewer_token": "viewer_token"}
"""중앙 비밀 파일 → publish 토큰 파일(publish `--ci-token-file`·`--viewer-token-file` 기본값)."""
PUBLISH_TOOL = PurePosixPath(
    "/home/pureiron-publish/actions-runner-fleet/_work/pureiron-fleet-test/pureiron-fleet-test/"
    "pureiron-v1"
)
"""runner `publish-WANG`이 마지막 publish run에서 받은 도구 checkout(`fleet-publish.yml`의
`path: pureiron-v1`) — `trust-base`가 그 venv로 돈다."""
IMAGE_MODE = 0o644
COMMAND_TIMEOUT_S = 120.0
TRUST_BASE_TIMEOUT_S = 300.0
HEALTH_TIMEOUT_S = 10.0

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class HostError(RuntimeError):
    pass


def _check(done: subprocess.CompletedProcess[str], what: str) -> None:
    if done.returncode != 0:
        raise HostError(f"{what} exited {done.returncode}: {(done.stderr or '').strip()[-400:]}")


def run_cmd(
    cmd: Sequence[str], *, stdin: str | None = None, timeout_s: float = COMMAND_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd), input=stdin, capture_output=True, text=True, timeout=timeout_s, check=False
    )


def ca_fingerprint(pem: str) -> str:
    """`central.fingerprint`와 같은 표기(대문자 hex, `:` 구분)."""
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def healthz(url: str) -> int:
    """OS 신뢰 저장소만으로(`cafile` 없음) HTTPS `healthz` — publish와 같은 검증."""
    context = ssl.create_default_context()
    with urllib.request.urlopen(url, context=context, timeout=HEALTH_TIMEOUT_S) as response:
        return response.status


def trust(
    ca: Path,
    want_sha256: str,
    *,
    target: Path = TRUST_FILE,
    update: bool = True,
    check_url: str | None = None,
    run: Run = run_cmd,
    probe: Callable[[str], int] = healthz,
) -> dict[str, object]:
    """지문 대조 → `install -m 0644` → `update-ca-certificates` → (주면) `check_url` 200."""
    got = ca_fingerprint(ca.read_text(encoding="ascii"))
    if got != want_sha256.upper():
        raise HostError(f"CA fingerprint {got} != expected {want_sha256}")
    _check(run(["install", "-m", "0644", str(ca), str(target)]), "install CA")
    if update:
        _check(run(["update-ca-certificates"]), "update-ca-certificates")
    status = probe(check_url) if check_url else None
    if check_url and status != 200:
        raise HostError(f"{check_url} answered {status} with the OS trust store")
    return {"ca_sha256": got, "trust_file": str(target), "healthz": status}


def tokens(
    secrets: Path,
    *,
    dest: Path = PUBLISH_ETC,
    owner: str | None = PUBLISH_USER,
    run: Run = run_cmd,
) -> dict[str, object]:
    """토큰 파일마다 0600(소유 `owner`) 빈 파일을 만든 뒤 값을 `cat` stdin으로. 값은 끝 공백을 뗀
    원문이다(publish `read_token`도 strip한다)."""
    written: dict[str, str] = {}
    for source, name in TOKEN_FILES.items():
        value = (secrets / source).read_text(encoding="utf-8").strip()
        if not value:
            raise HostError(f"{secrets / source} is empty")
        target = dest / name
        owned = ("-o", owner, "-g", owner) if owner else ()
        _check(run(["install", "-m", "0600", *owned, "/dev/null", str(target)]), f"create {target}")
        done = run(["sh", "-c", 'cat > "$1"', "sh", str(target)], stdin=value)
        del value
        _check(done, f"write {target}")
        written[name] = str(target)
    return {"token_files": written, "owner": owner}


def is_nightly(name: str) -> bool:
    """`n<sha7>` 또는 그 FAULT 파생(`nightly_group`이 `n<sha7>`을 돌려준다)."""
    group = nightly_group(name)
    return len(group) == 8 and group[0] == "n" and all(c in "0123456789abcdef" for c in group[1:])


def link_images(
    store: Path,
    names: Sequence[str],
    dest: Path = IMAGE_STORE,
    *,
    keep: int = NIGHTLY_KEEP,
    link: Callable[[Path, Path], None] = os.link,
    copy: Callable[[Path, Path], object] = shutil.copy2,
) -> dict[str, object]:
    """`names`(첫 이름 = NEXT, 모두 그 묶음)의 이미지 tar를 `dest/<이름>/`에 hardlink(안 되면 복사)
    → 0644 → nightly 이름만 prune(NEXT 묶음 + 최근 `keep - 1`묶음을 남긴다). 이미 같은 파일(같은
    inode)이면 그대로 둔다."""
    if not names or not is_nightly(names[0]):
        raise HostError(f"first name must be a nightly NEXT (n<sha7>): {list(names)}")
    done: dict[str, dict[str, str]] = {}
    for name in names:
        if nightly_group(name) != nightly_group(names[0]):
            raise HostError(f"{name} is not in the {names[0]} group")
        tars = sorted((store / name).glob("*.tar"))
        if not tars:
            raise HostError(f"{store / name}: no image tar")
        target_dir = dest / name
        target_dir.mkdir(parents=True, exist_ok=True)
        target_dir.chmod(0o755)
        how: dict[str, str] = {}
        for tar in tars:
            source, target = tar.resolve(), target_dir / tar.name
            if target.exists() and os.path.samefile(source, target):
                how[tar.name] = "same"
            else:
                part = target.with_name(target.name + ".part")
                part.unlink(missing_ok=True)
                try:
                    link(source, part)
                    how[tar.name] = "hardlink"
                except OSError:
                    copy(source, part)
                    how[tar.name] = "copy"
                part.replace(target)
            target.chmod(IMAGE_MODE)
        done[name] = how
    nightly = {
        d.name: d.stat().st_mtime for d in dest.iterdir() if d.is_dir() and is_nightly(d.name)
    }
    pruned = nightly_prune(nightly, names[0], keep)
    for old in pruned:
        shutil.rmtree(dest / old)
    return {"dest": str(dest), "linked": done, "pruned": pruned}


def chain_file(repo: str, root: Path = CHAIN_ROOT) -> Path:
    owner, name = repo.split("/", 1)
    return root / owner / name / "last_approved"


def chain(repo: str, *, root: Path = CHAIN_ROOT) -> dict[str, object]:
    """`last_approved` sha(파일이 없으면 None)."""
    path = chain_file(repo, root)
    if not path.is_file():
        return {"repo": repo, "last_approved": None, "file": str(path)}
    sha = path.read_text(encoding="ascii").strip()
    if HEAD_SHA.fullmatch(sha) is None:
        raise HostError(f"{path}: not a commit sha")
    return {"repo": repo, "last_approved": sha, "file": str(path)}


def trust_base_command(
    repo: str,
    sha: str,
    reason: str,
    *,
    tool: PurePosixPath = PUBLISH_TOOL,
    user: str = PUBLISH_USER,
) -> list[str]:
    if HEAD_SHA.fullmatch(sha) is None:
        raise HostError(f"--sha must be a 40-hex commit sha, got {sha!r}")
    return [
        *("runuser", "-u", user, "--"),
        str(tool / ".venv" / "bin" / "python"),
        str(tool / "deploy" / "fleet" / "publish.py"),
        *("trust-base", "--fleet-repo", repo, "--sha", sha, "--reason", reason),
    ]


def trust_base(
    repo: str, sha: str, reason: str, *, tool: PurePosixPath = PUBLISH_TOOL, run: Run = run_cmd
) -> dict[str, object]:
    """publish 사용자로 `publish.py trust-base`(App 키·사슬 디렉터리는 publish 호스트 기본값)."""
    done = run(trust_base_command(repo, sha, reason, tool=tool), timeout_s=TRUST_BASE_TIMEOUT_S)
    _check(done, "publish.py trust-base")
    return {"repo": repo, "trusted": sha, "reason": reason, "out": done.stdout.strip()[-400:]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="publish_host")
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("trust")
    t.add_argument("--ca", type=Path, required=True)
    t.add_argument("--sha256", required=True)
    t.add_argument("--target", type=Path, default=TRUST_FILE)
    t.add_argument("--check-url", default=None)
    t.add_argument(
        "--no-update", action="store_true", help="update-ca-certificates를 부르지 않는다"
    )
    k = sub.add_parser("tokens")
    k.add_argument("--secrets", type=Path, required=True)
    k.add_argument("--dest", type=Path, default=PUBLISH_ETC)
    k.add_argument("--owner", default=PUBLISH_USER, help="빈 값이면 소유를 바꾸지 않는다")
    i = sub.add_parser("images")
    i.add_argument("--store", type=Path, required=True)
    i.add_argument("--names", required=True, help="쉼표로 — 첫 이름이 NEXT")
    i.add_argument("--dest", type=Path, default=IMAGE_STORE)
    i.add_argument("--keep", type=int, default=NIGHTLY_KEEP)
    c = sub.add_parser("chain")
    c.add_argument("--repo", required=True)
    c.add_argument("--root", type=Path, default=CHAIN_ROOT)
    b = sub.add_parser("trust-base")
    b.add_argument("--repo", required=True)
    b.add_argument("--sha", required=True)
    b.add_argument("--reason", required=True)
    b.add_argument("--tool", type=PurePosixPath, default=PUBLISH_TOOL)
    args = parser.parse_args(argv)
    try:
        if args.command == "trust":
            result = trust(
                args.ca,
                args.sha256,
                target=args.target,
                update=not args.no_update,
                check_url=args.check_url,
            )
        elif args.command == "tokens":
            result = tokens(args.secrets, dest=args.dest, owner=args.owner or None)
        elif args.command == "images":
            names = [n for n in args.names.split(",") if n]
            result = link_images(args.store, names, args.dest, keep=args.keep)
        elif args.command == "chain":
            result = chain(args.repo, root=args.root)
        else:
            result = trust_base(args.repo, args.sha, args.reason, tool=args.tool)
    except (HostError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"publish_host {args.command}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
