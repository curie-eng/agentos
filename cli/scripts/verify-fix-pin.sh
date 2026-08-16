#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: verify-fix-pin.sh <change> <selector>" >&2
  exit 2
}

fail() {
  echo "$*" >&2
  exit 1
}

if [[ $# -ne 2 ]]; then
  usage
fi

change=$1
selector=$2
repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || fail "not inside a git repository"

temp_root=$(mktemp -d)
scratch="$temp_root/worktree"
patch_file="$temp_root/change.patch"
names_file="$temp_root/changed-files"
worktree_added=0

cleanup() {
  local status=$?
  local cleanup_status=0
  trap - EXIT

  if (( worktree_added )); then
    git -C "$repo_root" worktree remove --force "$scratch" >/dev/null 2>&1 || cleanup_status=1
  fi
  rm -rf -- "$temp_root" || cleanup_status=1

  if (( cleanup_status != 0 )); then
    echo "failed to clean up the verification worktree" >&2
    exit 1
  fi
  exit "$status"
}
trap cleanup EXIT

changed_files=()
change_kind=pull_request
if [[ ! "$change" =~ ^[0-9]+$ && ! "$change" =~ ^https://github\.com/[^/]+/[^/]+/pull/[0-9]+/?$ ]] && \
  commit=$(git -C "$repo_root" rev-parse --verify "${change}^{commit}" 2>/dev/null); then
  change_kind=commit
fi

if [[ "$change_kind" == commit ]]; then
  parent=$(git -C "$repo_root" rev-parse --verify "${commit}^1" 2>/dev/null) || \
    fail "commit $commit has no first parent"
  git -C "$repo_root" diff --binary "$parent" "$commit" -- >"$patch_file"
  git -C "$repo_root" diff --name-only -z "$parent" "$commit" -- >"$names_file"
  while IFS= read -r -d '' path; do
    changed_files+=("$path")
  done <"$names_file"
else
  gh pr view "$change" >/dev/null || \
    fail "change is neither a commit nor a pull request: $change"
  gh pr diff "$change" >"$patch_file" || \
    fail "could not read pull request patch: $change"
  gh pr diff "$change" --name-only >"$names_file" || \
    fail "could not read pull request files: $change"
  while IFS= read -r path || [[ -n "$path" ]]; do
    [[ -n "$path" ]] && changed_files+=("$path")
  done <"$names_file"
fi

if [[ ${#changed_files[@]} -eq 0 ]]; then
  fail "change contains no files"
fi

is_test_path() {
  case "$1" in
    apps/*/tests/* | packages/*/tests/* | cli/tests/* | charts/curie/ci/*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

test_files=()
product_files=()
for path in "${changed_files[@]}"; do
  if is_test_path "$path"; then
    test_files+=("$path")
  else
    product_files+=("$path")
  fi
done

if [[ ${#test_files[@]} -eq 0 ]]; then
  fail "change contains no test files"
fi
if [[ ${#product_files[@]} -eq 0 ]]; then
  fail "change contains no product files to reverse"
fi

selector_file=
selector_kind=
rust_target=
rust_test=
case "$selector" in
  apps/*/tests/*.py::* | packages/*/tests/*.py::*)
    selector_file=${selector%%::*}
    if [[ "$selector" == "$selector_file" || -z "${selector#*::}" ]]; then
      fail "unsupported selector: $selector"
    fi
    selector_kind=python
    ;;
  cli/tests/*.rs::*)
    selector_file=${selector%%::*}
    rust_test=${selector#*::}
    if [[ -z "$rust_test" || "$rust_test" == *::* ]]; then
      fail "unsupported selector: $selector"
    fi
    rust_target=${selector_file##*/}
    rust_target=${rust_target%.rs}
    selector_kind=rust
    ;;
  charts/curie/ci/*.sh)
    selector_file=$selector
    selector_kind=chart
    ;;
  *)
    fail "unsupported selector: $selector"
    ;;
esac

selector_changed=0
for path in "${test_files[@]}"; do
  if [[ "$path" == "$selector_file" ]]; then
    selector_changed=1
    break
  fi
done
if (( selector_changed == 0 )); then
  fail "selector file was not changed by $change: $selector_file"
fi

head_commit=$(git -C "$repo_root" rev-parse --verify 'HEAD^{commit}') || \
  fail "could not resolve current HEAD"
git -C "$repo_root" worktree add --detach "$scratch" "$head_commit" >/dev/null
worktree_added=1

run_selector() {
  case "$selector_kind" in
    python)
      (cd "$scratch" && uv run --python 3.13 pytest "$selector")
      ;;
    rust)
      (
        cd "$scratch"
        cargo test --manifest-path cli/Cargo.toml --test "$rust_target" "$rust_test"
      )
      ;;
    chart)
      (cd "$scratch" && bash "$selector")
      ;;
  esac
}

if ! run_selector; then
  fail "baseline selector is red: $selector"
fi

include_args=()
for path in "${product_files[@]}"; do
  include_args+=("--include=$path")
done

if ! git -C "$scratch" apply --check --reverse "${include_args[@]}" "$patch_file"; then
  echo "could not reverse the changed product files:" >&2
  printf '  %s\n' "${product_files[@]}" >&2
  exit 1
fi
git -C "$scratch" apply --reverse "${include_args[@]}" "$patch_file"

if run_selector; then
  echo "UNPINNED"
  exit 1
fi

echo "PINNED"
