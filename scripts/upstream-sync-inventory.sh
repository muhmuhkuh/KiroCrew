#!/usr/bin/env bash
# Read-only snapshot of local sync refs and every linked worktree.
set -euo pipefail

git_readonly() {
  GIT_OPTIONAL_LOCKS=0 GIT_NO_LAZY_FETCH=1 command git -c core.fsmonitor=false "$@"
}

usage() {
  cat <<'HELP'
Usage: scripts/upstream-sync-inventory.sh [--help]

Run from any KiroCrew worktree. Reports its branch/HEAD, locally stored remote
main refs and divergence, then each linked worktree with a compact status count.
Every Git call disables optional locks, lazy fetching and FSMonitor. Status
diagnostics are summarized without printing file paths; incomplete queries are
marked and return exit 1. Counts are porcelain status entries; Git may summarize
an untracked directory as one entry.
HELP
}

error() {
  printf 'upstream-sync-inventory: %s\n' "$1" >&2
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
fi

root_marker=__KIROCREW_ROOT_PATH_END__
if ! repo_root_output=$(git_readonly rev-parse --show-toplevel 2>/dev/null && printf '%s' "$root_marker"); then
  error 'run inside a Git worktree'
  exit 2
fi
case "$repo_root_output" in
  *"$root_marker") ;;
  *)
    error 'could not read the current repository path'
    exit 2
    ;;
esac
repo_root=${repo_root_output%"$root_marker"}
repo_root=${repo_root%$'\n'}
if [[ -z $repo_root ]]; then
  error 'could not read the current repository path'
  exit 2
fi

if ! branch=$(git_readonly -C "$repo_root" branch --show-current 2>/dev/null); then
  error 'could not read the current branch'
  exit 1
fi
if ! head=$(git_readonly -C "$repo_root" rev-parse --short=12 HEAD 2>/dev/null); then
  error 'could not read HEAD'
  exit 1
fi
tracking_ref=$(git_readonly -C "$repo_root" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
if [[ -z $branch ]]; then
  branch=detached
fi
if [[ -z $tracking_ref ]]; then
  tracking_ref=none
fi

printf 'Repository: %q\n' "$repo_root"
printf 'Current:    %s @ %s (upstream: %s)\n' "$branch" "$head" "$tracking_ref"
printf 'Stored remote main refs (no fetch):\n'

if ! remote_refs=$(git_readonly -C "$repo_root" for-each-ref --format='%(refname)' 'refs/remotes/*/main' 2>/dev/null); then
  error 'could not list stored remote refs; diagnostics suppressed'
  exit 1
fi
if [[ -z $remote_refs ]]; then
  printf '  (none found)\n'
else
  while IFS= read -r ref || [[ -n $ref ]]; do
    [[ -n $ref ]] || continue
    if ! commit=$(git_readonly -C "$repo_root" rev-parse --short=12 "$ref" 2>/dev/null); then
      error 'could not resolve a stored remote ref; diagnostics suppressed'
      exit 1
    fi
    if ! counts=$(git_readonly -C "$repo_root" rev-list --left-right --count "HEAD...$ref" 2>/dev/null); then
      error 'could not compare a stored remote ref; diagnostics suppressed'
      exit 1
    fi
    if ! read -r head_only ref_only <<< "$counts"; then
      error 'could not parse a stored remote ref comparison'
      exit 1
    fi
    case "$head_only:$ref_only" in
      *[!0-9:]*|:*|*:)
        error 'received an invalid stored remote ref comparison'
        exit 1
        ;;
    esac
    printf '  %-30s %s (HEAD-only %s, ref-only %s)\n' "${ref#refs/remotes/}" "$commit" "$head_only" "$ref_only"
  done <<< "$remote_refs"
fi

printf 'Linked worktrees (status paths are not printed):\n'
tree_path=
tree_branch=detached
tree_head=
tree_flags=
inventory_incomplete=0
worktree_listing_status=

reset_tree() {
  tree_path=
  tree_branch=detached
  tree_head=
  tree_flags=
}

emit_tree() {
  [[ -n $tree_path ]] || return 0
  local status_result status_rc total staged unstaged untracked diagnostic_count status_counts

  status_rc=0
  if status_result=$(git_readonly -C "$tree_path" status --porcelain=v1 --untracked-files=normal 2>&1 | awk '
    {
      x = substr($0, 1, 1)
      y = substr($0, 2, 1)
      if (substr($0, 3, 1) == " " &&
          x ~ /^[ MADRCU?!T]$/ && y ~ /^[ MADRCU?!T]$/) {
        total++
        if (x == "?" && y == "?") {
          untracked++
        } else {
          if (x != " ") staged++
          if (y != " ") unstaged++
        }
      } else {
        diagnostics++
      }
    }
    END {
      printf "%d %d %d %d %d\n", total, staged, unstaged, untracked, diagnostics
    }
  '); then
    :
  else
    status_rc=$?
  fi
  if [[ -n $status_result ]]; then
    read -r total staged unstaged untracked diagnostic_count <<< "$status_result"
  else
    total=0
    staged=0
    unstaged=0
    untracked=0
    diagnostic_count=0
  fi

  if [[ $status_rc -ne 0 ]]; then
    status_counts='unavailable (Git status failed; diagnostics suppressed)'
    inventory_incomplete=1
  elif [[ $diagnostic_count -gt 0 ]]; then
    status_counts="$total entries ($staged staged, $unstaged unstaged, $untracked untracked; incomplete, Git warnings suppressed)"
    inventory_incomplete=1
  else
    status_counts="$total entries ($staged staged, $unstaged unstaged, $untracked untracked)"
  fi

  printf '  %q | %s @ %s | %s' "$tree_path" "$tree_branch" "${tree_head:0:12}" "$status_counts"
  [[ -n $tree_flags ]] && printf ' | %s' "$tree_flags"
  printf '\n'
}

list_worktrees() {
  local listing_status
  if git_readonly -C "$repo_root" worktree list --porcelain -z 2>/dev/null; then
    listing_status=0
  else
    listing_status=$?
  fi
  printf 'upstream-sync-inventory-exit=%s\0' "$listing_status"
}

while IFS= read -r -d '' field; do
  case "$field" in
    'worktree '*)
      emit_tree
      tree_path=${field#worktree }
      tree_branch=detached
      tree_head=
      tree_flags=
      ;;
    'HEAD '*)
      tree_head=${field#HEAD }
      ;;
    'branch '*)
      tree_branch=${field#branch refs/heads/}
      ;;
    detached)
      tree_branch=detached
      ;;
    locked*)
      tree_flags="${tree_flags:+$tree_flags,}locked"
      ;;
    prunable*)
      tree_flags="${tree_flags:+$tree_flags,}prunable"
      ;;
    '')
      emit_tree
      reset_tree
      ;;
    upstream-sync-inventory-exit=*)
      worktree_listing_status=${field#upstream-sync-inventory-exit=}
      ;;
    *)
      # Ignore attributes added by newer Git versions.
      ;;
  esac
done < <(list_worktrees)
emit_tree

if [[ -z $worktree_listing_status || $worktree_listing_status != 0 ]]; then
  error 'could not list linked worktrees; diagnostics suppressed'
  inventory_incomplete=1
fi

if [[ $inventory_incomplete -ne 0 ]]; then
  printf 'Overall: incomplete (one or more Git queries failed or warned; diagnostics suppressed)\n'
  exit 1
fi
printf 'Overall: complete\n'
