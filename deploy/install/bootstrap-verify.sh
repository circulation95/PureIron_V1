#!/usr/bin/env bash
# 새 호스트 부트스트랩 검증(plan M2 설치 번들 2단계). openssl만으로 설치 번들을 검증하는 1회용 경로다 —
# 설치가 끝나면 설치된 Updater(bundle-tools)가 같은 번들을 다시 검증한다(5a, P3).
#
# 실행 전에 운영자는 이 파일의 sha256을 runbook(종이) 값과 대조한다:
#   tar -xf pureiron-install-<release>.pib bootstrap/ manifest.json manifest.sig \
#       release-cert.json release-cert.sig keys/
#   sha256sum bootstrap/bootstrap-verify.sh        # runbook 값과 같아야 실행
#   sudo bash bootstrap/bootstrap-verify.sh --root-fpr <runbook root 지문> \
#       --bundle pureiron-install-<release>.pib --dest /root/pureiron-install
#
# 순서: ① keys/root.pub 지문 == --root-fpr, keys/root.pem(SPKI) 안의 raw 32B == root.pub
#       ② root가 release-cert.json에 한 서명(purpose release-cert)
#       ③ cert의 release key가 manifest.json에 한 서명, kind install, created_at ∈ cert 유효기간
#       ④ --dest(비어 있음)에 manifest `files`에 적힌 경로만 풀고 일반 파일인지·sha256 대조.
# 하나라도 틀리면 종료 코드 1로 멈춘다. 현재 디렉터리 = 2단계에서 메타데이터를 푼 곳.
set -euo pipefail

PY="${PUREIRON_PYTHON:-python3}"
SPKI_PREFIX=302a300506032b6570032100
ROOT_FPR=""
BUNDLE=""
DEST=""

die() { printf 'bootstrap-verify: FAIL: %s\n' "$*" >&2; exit 1; }
usage() { die "usage: bootstrap-verify.sh --root-fpr <sha256 hex> --bundle <install.pib> --dest <empty dir>"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --root-fpr) ROOT_FPR="${2:-}"; shift 2 ;;
    --bundle) BUNDLE="${2:-}"; shift 2 ;;
    --dest) DEST="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$ROOT_FPR" ] && [ -n "$BUNDLE" ] && [ -n "$DEST" ] || usage
ROOT_FPR="$(printf '%s' "$ROOT_FPR" | tr 'A-F' 'a-f' | tr -d ' :')"
[[ "$ROOT_FPR" =~ ^[0-9a-f]{64}$ ]] || die "--root-fpr must be 64 hex characters"
[ -f "$BUNDLE" ] || die "bundle $BUNDLE not found"
for tool in openssl sha256sum tar od "$PY"; do
  command -v "$tool" >/dev/null 2>&1 || die "required tool missing: $tool"
done
for f in manifest.json manifest.sig release-cert.json release-cert.sig keys/root.pub keys/root.pem; do
  [ -f "$f" ] && [ ! -L "$f" ] || die "$f missing in $(pwd) (extract the metadata first)"
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ① root 공개키: 지문 + PEM 안의 raw 키
[ "$(wc -c < keys/root.pub | tr -d ' ')" = 32 ] || die "keys/root.pub is not 32 bytes"
fpr="$(sha256sum keys/root.pub | cut -d' ' -f1)"
[ "$fpr" = "$ROOT_FPR" ] || die "root fingerprint $fpr != runbook $ROOT_FPR"
openssl pkey -pubin -in keys/root.pem -outform DER -out "$WORK/root.der" 2>/dev/null \
  || die "keys/root.pem is not a public key"
[ "$(wc -c < "$WORK/root.der" | tr -d ' ')" = 44 ] || die "keys/root.pem is not an Ed25519 key"
[ "$(head -c 12 "$WORK/root.der" | od -An -tx1 | tr -d ' \n')" = "$SPKI_PREFIX" ] \
  || die "keys/root.pem is not an Ed25519 SubjectPublicKeyInfo"
tail -c 32 "$WORK/root.der" > "$WORK/root.raw"
cmp -s "$WORK/root.raw" keys/root.pub || die "keys/root.pem and keys/root.pub differ"
echo "root key     OK  $fpr"

# ② root → release cert
openssl pkeyutl -verify -pubin -inkey keys/root.pem -rawin \
  -in release-cert.json -sigfile release-cert.sig >/dev/null 2>&1 \
  || die "release-cert.sig is not a root signature of release-cert.json"
echo "release cert OK"

# ③ cert → manifest(+ 형식 확인). release 공개키 PEM과 files 목록은 python3(표준 라이브러리)로 만든다.
"$PY" - "$WORK" <<'PYEOF' || die "release-cert.json / manifest.json are malformed"
import base64, json, re, sys
from datetime import datetime
work = sys.argv[1]
cert = json.load(open("release-cert.json", encoding="utf-8"))
if cert.get("format") != 1 or cert.get("purpose") != "release-cert":
    sys.exit("release-cert.json: not a format 1 release-cert")
raw = bytes.fromhex(cert["release_pubkey"])
if len(raw) != 32:
    sys.exit("release_pubkey is not 32 bytes")
der = bytes.fromhex("302a300506032b6570032100") + raw
body = base64.b64encode(der).decode()
with open(f"{work}/release.pem", "w", encoding="ascii") as out:
    out.write("-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----\n")
manifest = json.load(open("manifest.json", encoding="utf-8"))
if manifest.get("format") != 1 or manifest.get("kind") != "install":
    sys.exit("manifest.json: not an install bundle")
stamp = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
if not stamp(cert["not_before"]) <= stamp(manifest["created_at"]) <= stamp(cert["not_after"]):
    sys.exit("manifest created_at is outside the cert validity")
pattern = re.compile(r"(?!(.*/)?\.\.?(/|$))[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")
digest = re.compile(r"sha256:[0-9a-f]{64}")
with open(f"{work}/files.sha256", "w", encoding="ascii", newline="\n") as out:
    for name, value in sorted(manifest["files"].items()):
        if not pattern.fullmatch(name) or not digest.fullmatch(value):
            sys.exit(f"bad files entry {name!r}")
        out.write(f"{value[7:]}  {name}\n")
PYEOF
openssl pkeyutl -verify -pubin -inkey "$WORK/release.pem" -rawin \
  -in manifest.json -sigfile manifest.sig >/dev/null 2>&1 \
  || die "manifest.sig is not a release-key signature of manifest.json"
echo "manifest     OK  $(wc -l < "$WORK/files.sha256" | tr -d ' ') files"

# ④ 서명된 목록의 경로만 풀고 sha256 대조
if [ -e "$DEST" ] && [ -n "$(ls -A "$DEST")" ]; then
  die "--dest $DEST must be empty or absent"
fi
mkdir -p "$DEST"
mapfile -t NAMES < <(cut -d' ' -f3- "$WORK/files.sha256")
tar --force-local -xf "$BUNDLE" -C "$DEST" --no-same-owner --no-same-permissions -- "${NAMES[@]}" \
  || die "cannot extract the signed files from $BUNDLE"
[ -z "$(find "$DEST" -type l -print -quit)" ] || die "bundle contains symbolic links"
for name in "${NAMES[@]}"; do
  [ -f "$DEST/$name" ] || die "$name is not a regular file"
done
(cd "$DEST" && sha256sum --quiet --strict -c "$WORK/files.sha256") || die "payload sha256 mismatch"
cp manifest.json manifest.sig release-cert.json release-cert.sig "$DEST/"
echo "payload      OK  → $DEST"
echo "bootstrap-verify: OK — next: sudo bash $DEST/install/install.sh --bundle $BUNDLE"
