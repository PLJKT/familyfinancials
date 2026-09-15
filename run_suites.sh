#!/bin/bash
# Run every test suite, each against its own fresh database (they all assume a
# clean start: an empty backup log, no leftover rows, no opening balances).
cd /c/Users/HONOR/familyfinancials || exit 1

run_suite() {
  suite="$1"
  rm -f test_import.db
  DATABASE_URL="sqlite:///./test_import.db" python -m uvicorn app.main:app \
      --host 127.0.0.1 --port 8002 --log-level warning &
  pid=$!
  sleep 9
  python "$suite" > "${suite%.py}_out.txt" 2>&1
  rc=$?
  kill $pid 2>/dev/null
  wait $pid 2>/dev/null
  sleep 2
  summary=$(grep -E '=====' "${suite%.py}_out.txt" | tail -1)
  echo "--- $suite : exit=$rc : $summary"
  grep "FAIL" "${suite%.py}_out.txt" || true
}

run_suite tests_statements.py
run_suite tests_admin_features.py
run_suite tests_accounts_model.py
echo "=== all suites finished ==="
