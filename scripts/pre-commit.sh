#!/usr/bin/env bash
# MHDE pre-commit hook.
#
# Goal: run a tight set of checks in <30 seconds wall-clock so the hook
# stays useful instead of getting bypassed.
#
# Three stages, all of them blocking:
#   1. py_compile staged .py files            (~0.1s; catches syntax errors)
#   2. Tiny pytest smoke (fixtures + a handful of fast unit modules)
#                                              (~3-8s; catches obvious bugs)
#   3. Forbidden-pattern lint                 (~0.1s; catches the few rules
#                                              we always enforce — see below)
#
# Bypass with `git commit --no-verify` when you really mean it. Don't make
# bypassing the default.
#
# Install: `make install-hooks`. Uninstall: `rm .git/hooks/pre-commit`.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

PYTHON="$REPO_ROOT/venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "[pre-commit] FAIL: $PYTHON not found. Set up venv first." >&2
    exit 1
fi

START=$(date +%s)

# ── Stage 1: py_compile staged .py files ──────────────────────────────
STAGED_PY=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.py$' || true)

if [ -n "$STAGED_PY" ]; then
    echo "[pre-commit] py_compile staged .py files..."
    if ! echo "$STAGED_PY" | xargs "$PYTHON" -m py_compile; then
        echo "[pre-commit] FAIL: py_compile errored on a staged file." >&2
        exit 1
    fi
fi

# ── Stage 2: pytest smoke ─────────────────────────────────────────────
# A curated fast subset. Adjust here if a specific test starts hogging
# the budget; the hook is allowed to be selective.
SMOKE_TESTS=(
    "tests/test_session2_infra_smoke.py"
    "tests/equity/test_base.py"
    "tests/equity/test_storage.py"
    "tests/equity/test_config_loader.py"
)

# Only include smoke tests that exist (the regression dir starts empty
# and equity tests may be reorganized further in later sessions).
TESTS_TO_RUN=()
for t in "${SMOKE_TESTS[@]}"; do
    if [ -f "$t" ]; then
        TESTS_TO_RUN+=("$t")
    fi
done

if [ ${#TESTS_TO_RUN[@]} -gt 0 ]; then
    echo "[pre-commit] pytest smoke (${#TESTS_TO_RUN[@]} files)..."
    if ! "$PYTHON" -m pytest "${TESTS_TO_RUN[@]}" -q --no-header -x --tb=short; then
        echo "[pre-commit] FAIL: pytest smoke failed." >&2
        exit 1
    fi
fi

# ── Stage 3: forbidden-pattern lint ───────────────────────────────────
# Cheap grep-based rules. Add new ones here when a recurring footgun
# justifies it; keep them O(1) per file so the hook stays fast.
LINT_FAIL=0

if [ -n "$STAGED_PY" ]; then
    # Rule: never commit `print('DEBUG ...` or stray pdb.set_trace.
    if echo "$STAGED_PY" | xargs grep -nE 'pdb\.set_trace\(\)|breakpoint\(\)' 2>/dev/null; then
        echo "[pre-commit] FAIL: pdb.set_trace() / breakpoint() in staged files." >&2
        LINT_FAIL=1
    fi

    # Rule: no `python -c` inside .claude/local_scripts (project policy).
    if echo "$STAGED_PY" | grep -E '^\.claude/local_scripts/' | xargs grep -nE 'python -c|python3 -c' 2>/dev/null; then
        echo "[pre-commit] FAIL: 'python -c' usage detected in .claude/local_scripts." >&2
        LINT_FAIL=1
    fi

    # Rule: no `User=` or `Group=` in user-level systemd unit files
    # (silent-failure footgun documented in INFRASTRUCTURE.md).
    STAGED_USER_UNITS=$(git diff --cached --name-only --diff-filter=ACM | \
        grep -E 'systemd/.*\.service$' | xargs -I {} grep -lE '^(User|Group)=' {} 2>/dev/null || true)
    if [ -n "$STAGED_USER_UNITS" ]; then
        echo "[pre-commit] WARN: systemd unit has User=/Group= line:" >&2
        echo "$STAGED_USER_UNITS" >&2
        echo "  — fine for system-level units (/etc/systemd/system/), forbidden for user-level (~/.config/systemd/user/)." >&2
        echo "  — verify the deploy target before committing." >&2
        # Warn-only because system-level units legitimately set these.
    fi
fi

if [ $LINT_FAIL -ne 0 ]; then
    exit 1
fi

END=$(date +%s)
echo "[pre-commit] OK ($((END - START))s)"
