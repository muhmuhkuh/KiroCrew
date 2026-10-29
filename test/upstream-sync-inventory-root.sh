#!/usr/bin/env bash
# Synthetic regression test for repository roots ending in newline bytes.
set -euo pipefail

if [[ $# -gt 2 ]]; then
  printf 'Usage: bash test/upstream-sync-inventory-root.sh [bash-3.2] [bash-5.3]\n' >&2
  exit 2
fi
bash32=${1:-/bin/bash}
bash53=${2:-/opt/homebrew/bin/bash}
for interpreter in "$bash32" "$bash53"; do
  if [[ ! -x $interpreter ]]; then
    printf 'Required Bash interpreter is unavailable: %s\n' "$interpreter" >&2
    exit 2
  fi
done

repo_dir=$(cd "$(dirname "$0")/.." && pwd)
helper=$repo_dir/scripts/upstream-sync-inventory.sh
test_dir=$(mktemp -d /tmp/kirocrew-inventory-root.XXXXXX)
mkdir "$test_dir/bin"

cleanup() {
  rm -f "$test_dir/bin/git"
  rmdir "$test_dir/bin" "$test_dir"
}
trap cleanup EXIT

cat > "$test_dir/bin/git" <<'FAKE_GIT'
#!/bin/bash
if [[ ${GIT_OPTIONAL_LOCKS:-} != 0 || ${GIT_NO_LAZY_FETCH:-} != 1 ]]; then
  printf 'fake git: read-only environment missing\n' >&2
  exit 90
fi
if [[ ${1:-} != -c || ${2:-} != core.fsmonitor=false ]]; then
  printf 'fake git: FSMonitor override missing\n' >&2
  exit 91
fi
shift 2

git_dir=
if [[ ${1:-} == -C ]]; then
  git_dir=$2
  shift 2
fi
git_command=${1:-}
if [[ $# -gt 0 ]]; then shift; fi

if [[ $git_command == rev-parse && ${1:-} == --show-toplevel ]]; then
  if [[ ${FAKE_MODE:-} == empty-root ]]; then
    printf '\n'
  else
    printf '%s\n' "$EXPECTED_ROOT"
  fi
  exit 0
fi

if [[ $git_dir == "$EXPECTED_ROOT" ]]; then
  repo_kind=correct
elif [[ $git_dir == "$WRONG_ROOT" ]]; then
  repo_kind=wrong
else
  printf 'fake git: unexpected repository path\n' >&2
  exit 92
fi

case "$git_command" in
  branch)
    if [[ $repo_kind == correct ]]; then
      printf 'correct-root-branch\n'
    else
      printf 'wrong-root-branch\n'
    fi
    ;;
  rev-parse)
    case ${1:-} in
      --short=12)
        if [[ $repo_kind == correct ]]; then
          printf '0123456789ab\n'
        else
          printf 'badbadbadbad\n'
        fi
        ;;
      --abbrev-ref)
        printf 'origin/main\n'
        ;;
      *) exit 93 ;;
    esac
    ;;
  for-each-ref)
    printf 'refs/remotes/origin/main\n'
    ;;
  rev-list)
    printf '0 0\n'
    ;;
  worktree)
    if [[ $repo_kind == correct ]]; then
      worktree_path=$EXPECTED_ROOT
    else
      worktree_path=$WRONG_ROOT
    fi
    printf 'worktree %s\0HEAD %s\0branch refs/heads/synthetic\0\0' "$worktree_path" '0123456789abcdef'
    ;;
  status)
    if [[ $git_dir != "$EXPECTED_ROOT" && $git_dir != "$WRONG_ROOT" ]]; then
      exit 94
    fi
    ;;
  *)
    printf 'fake git: unexpected command\n' >&2
    exit 95
    ;;
esac
FAKE_GIT
chmod +x "$test_dir/bin/git"

expected_root=$'/synthetic/current\n\n'
wrong_root=/synthetic/current
escaped_root=$(printf '%q' "$expected_root")

run_case() {
  local interpreter=$1 mode=$2 expected_status=$3 expected_text=$4
  local output status
  if output=$(PATH="$test_dir/bin:$PATH" FAKE_MODE="$mode" EXPECTED_ROOT="$expected_root" WRONG_ROOT="$wrong_root" "$interpreter" "$helper" 2>&1); then
    status=0
  else
    status=$?
  fi
  if [[ $status -ne $expected_status ]]; then
    printf 'FAIL %s/%s: exit %s, expected %s\n%s\n' "$interpreter" "$mode" "$status" "$expected_status" "$output" >&2
    return 1
  fi
  case "$output" in
    *"$expected_text"*) ;;
    *)
      printf 'FAIL %s/%s: expected output was missing\n%s\n' "$interpreter" "$mode" "$output" >&2
      return 1
      ;;
  esac
  if [[ $mode == normal ]]; then
    case "$output" in
      *"$escaped_root"*correct-root-branch*'Overall: complete'*) ;;
      *)
        printf 'FAIL %s: exact newline root was not used\n%s\n' "$interpreter" "$output" >&2
        return 1
        ;;
    esac
    case "$output" in
      *wrong-root-branch*|*badbadbadbad*)
        printf 'FAIL %s: truncated root selected the wrong repository\n' "$interpreter" >&2
        return 1
        ;;
    esac
  fi
  printf 'PASS %s %s\n' "$interpreter" "$mode"
}

for interpreter in "$bash32" "$bash53"; do
  "$interpreter" -n "$helper"
  run_case "$interpreter" normal 0 'Overall: complete'
  run_case "$interpreter" empty-root 2 'could not read the current repository path'
done
