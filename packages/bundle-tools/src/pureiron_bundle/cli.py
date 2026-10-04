"""`pureiron-bundle` — 키·release cert·번들 검증 도구.

    pureiron-bundle keys gen --out DIR --name NAME      # NAME.key(0600) + NAME.pub
    pureiron-bundle keys export-pem PUB                 # SubjectPublicKeyInfo PEM(결정적)
    pureiron-bundle keys fingerprint PUB                # sha256(raw 32B) hex
    pureiron-bundle cert issue --root-key K --release-pub P --serial N [--days 180]
                               [--not-before TS] --out DIR
    pureiron-bundle verify PIB --root-pub P --site ID [--updater-version X] [--extract-to DIR]

종료 코드: 0 성공, 1 번들 거부(stderr 첫 줄 = 거부 코드), 2 사용법·입력 파일 오류.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejectedError
from pureiron_bundle.pib import CERT_FILE, CERT_SIG
from pureiron_bundle.trust import (
    DEFAULT_CERT_DAYS,
    format_timestamp,
    issue_cert,
    parse_timestamp,
)
from pureiron_bundle.verify import VerifyContext, verify_bundle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pureiron-bundle")
    sub = parser.add_subparsers(dest="command", required=True)

    key_cmd = sub.add_parser("keys", help="Ed25519 키 생성·변환")
    key_sub = key_cmd.add_subparsers(dest="action", required=True)
    gen = key_sub.add_parser("gen", help="키쌍 생성(raw 32B seed·공개키)")
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--name", required=True)
    for name, text in (("export-pem", "공개키 → PEM"), ("fingerprint", "공개키 지문")):
        cmd = key_sub.add_parser(name, help=text)
        cmd.add_argument("public", type=Path)

    cert_cmd = sub.add_parser("cert", help="release cert")
    cert_sub = cert_cmd.add_subparsers(dest="action", required=True)
    issue = cert_sub.add_parser("issue", help="root key로 release cert 발급")
    issue.add_argument("--root-key", type=Path, required=True)
    issue.add_argument("--release-pub", type=Path, required=True)
    issue.add_argument("--serial", type=int, required=True)
    issue.add_argument("--days", type=int, default=DEFAULT_CERT_DAYS)
    issue.add_argument("--not-before", default=None, help="RFC 3339(기본: 지금)")
    issue.add_argument("--out", type=Path, required=True)

    verify = sub.add_parser("verify", help=".pib 검증(새 설치 기준 상태)")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--root-pub", type=Path, required=True)
    verify.add_argument("--site", required=True)
    verify.add_argument("--updater-version", default=None)
    verify.add_argument("--extract-to", type=Path, default=None)
    return parser


def _utf8_console() -> None:
    """Windows 콘솔(cp949 등)에서도 한국어 메시지 출력으로 죽지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _keys(args: argparse.Namespace) -> int:
    if args.action == "gen":
        out: Path = args.out
        out.mkdir(parents=True, exist_ok=True)
        seed = keys.generate_seed()
        keys.write_private_key(out / f"{args.name}.key", seed)
        public = keys.public_key(seed)
        keys.write_public_key(out / f"{args.name}.pub", public)
        print(keys.fingerprint(public))
        return 0
    public = keys.read_public_key(args.public)
    if args.action == "export-pem":
        sys.stdout.write(keys.export_pem(public))
    else:
        print(keys.fingerprint(public))
    return 0


def _cert(args: argparse.Namespace) -> int:
    not_before = parse_timestamp(args.not_before) if args.not_before else datetime.now(UTC)
    data, sig = issue_cert(
        keys.read_private_key(args.root_key),
        keys.read_public_key(args.release_pub),
        args.serial,
        not_before.replace(microsecond=0),
        args.days,
    )
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / CERT_FILE).write_bytes(data)
    (out / CERT_SIG).write_bytes(sig)
    print(data.decode("utf-8"))
    return 0


def _verify(args: argparse.Namespace) -> int:
    ctx = VerifyContext(
        root_pubkey=keys.read_public_key(args.root_pub),
        site_id=args.site,
        updater_version=args.updater_version,
    )
    try:
        result = verify_bundle(args.bundle, ctx, extract_to=args.extract_to)
    except BundleRejectedError as exc:
        print(f"{exc.code}: {exc.detail}", file=sys.stderr)
        return 1
    manifest = result.manifest
    report = {
        "site_id": manifest.site_id,
        "generation": manifest.generation,
        "created_at": format_timestamp(manifest.created_at),
        "product": manifest.product_release.version,
        "decision": result.generation.value,
        "schema": None
        if result.schema is None
        else {
            "action": result.schema.action.value,
            "current": result.schema.current,
            "target": result.schema.target,
        },
        "cert_serial": result.cert.serial,
        "trust_update_applied": result.trust.trust_update_applied,
        "files": len(result.files),
        "verify_s": round(result.verify_s, 3),
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    args = _parser().parse_args(argv)
    try:
        if args.command == "keys":
            return _keys(args)
        if args.command == "cert":
            return _cert(args)
        return _verify(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
