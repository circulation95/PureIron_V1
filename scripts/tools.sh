#!/usr/bin/env bash
# G1 외부 도구(buf, oasdiff)를 버전·sha256 고정으로 .tools/bin 에 설치한다.
# Linux(g1 runner, WSL2 Ubuntu)와 Windows(Git Bash 개발 PC) 모두 같은 버전을 쓴다.
# 버전을 올릴 때는 아래 표의 버전과 sha256을 함께 바꾼다(릴리스의 checksum 파일 값).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/.tools/bin"
mkdir -p "$BIN"

BUF_VERSION=1.73.0
BUF_SHA256_LINUX=8f2986298ad08f0cc1bf999b9797b7c383adf32d7edf0f73d6f1e1a701baeac1
BUF_SHA256_WINDOWS=13542f2892c4f774150ddb525266d6421d457b3e741297056b64427853526e36
OASDIFF_VERSION=1.33.0
OASDIFF_SHA256_LINUX=43a4e328e2d13ba1552d760aa68d2485c75c5621f309f6ff64ae895188345247
OASDIFF_SHA256_WINDOWS=22f98c7247075f8a446e783595802d6d38637de1760df264f3d4b3309f766c5b

case "$(uname -s)" in
  Linux*) OS=linux; EXE="" ;;
  MINGW*|MSYS*|CYGWIN*) OS=windows; EXE=".exe" ;;
  *) echo "tools.sh: 지원하지 않는 OS: $(uname -s)" >&2; exit 1 ;;
esac

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

fetch() {  # url dest expected_sha
  local tmp
  tmp="$(mktemp)"
  curl -fsSL --retry 3 -o "$tmp" "$1"
  local got
  got="$(sha256_of "$tmp")"
  if [ "$got" != "$3" ]; then
    rm -f "$tmp"
    echo "tools.sh: sha256 불일치 $1 (기대 $3, 실제 $got)" >&2
    exit 1
  fi
  mv "$tmp" "$2"
}

stamp_ok() { [ -f "$BIN/.$1.version" ] && [ "$(cat "$BIN/.$1.version")" = "$2" ] && [ -x "$BIN/$1$EXE" ]; }

if ! stamp_ok buf "$BUF_VERSION"; then
  if [ "$OS" = linux ]; then
    fetch "https://github.com/bufbuild/buf/releases/download/v$BUF_VERSION/buf-Linux-x86_64" "$BIN/buf" "$BUF_SHA256_LINUX"
  else
    fetch "https://github.com/bufbuild/buf/releases/download/v$BUF_VERSION/buf-Windows-x86_64.exe" "$BIN/buf.exe" "$BUF_SHA256_WINDOWS"
  fi
  chmod +x "$BIN/buf$EXE"
  echo "$BUF_VERSION" > "$BIN/.buf.version"
fi

if ! stamp_ok oasdiff "$OASDIFF_VERSION"; then
  archive="$(mktemp)"
  if [ "$OS" = linux ]; then
    fetch "https://github.com/oasdiff/oasdiff/releases/download/v$OASDIFF_VERSION/oasdiff_${OASDIFF_VERSION}_linux_amd64.tar.gz" "$archive" "$OASDIFF_SHA256_LINUX"
  else
    fetch "https://github.com/oasdiff/oasdiff/releases/download/v$OASDIFF_VERSION/oasdiff_${OASDIFF_VERSION}_windows_amd64.tar.gz" "$archive" "$OASDIFF_SHA256_WINDOWS"
  fi
  tar -xzf "$archive" -C "$BIN" "oasdiff$EXE"
  rm -f "$archive"
  chmod +x "$BIN/oasdiff$EXE"
  echo "$OASDIFF_VERSION" > "$BIN/.oasdiff.version"
fi

echo "$BIN"
