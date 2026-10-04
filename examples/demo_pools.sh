#!/usr/bin/env bash
# 影片级共享额度池调用示例。启动服务后执行：bash examples/demo_pools.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-shared-demo"

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 登记影片级共享额度池：影片 $FILM 跨场馆总放映 3 场 =="
curl -s -X POST "$BASE/pools" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"max_sessions\": 3,
  \"note\": \"发行方设定的跨场馆总放映次数\"
}" | pretty

echo
echo "== 2. 录入两份场馆授权并绑定池 1（hall-1 / hall-2，各自上限 10） =="
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2026-10-01T00:00:00Z\", \"valid_to\": \"2026-10-31T23:59:59Z\",
  \"max_sessions\": 10, \"pool_id\": 1
}" | pretty
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-2\",
  \"valid_from\": \"2026-10-01T00:00:00Z\", \"valid_to\": \"2026-10-31T23:59:59Z\",
  \"max_sessions\": 10, \"pool_id\": 1
}" | pretty

echo
echo "== 3. 绑定不存在的池：422 POOL_NOT_FOUND =="
curl -s -w 'HTTP %{http_code}\n' -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-3\",
  \"valid_from\": \"2026-10-01T00:00:00Z\", \"valid_to\": \"2026-10-31T23:59:59Z\",
  \"max_sessions\": 10, \"pool_id\": 999
}"

echo
echo "== 4. 绑定其他影片的池：422 POOL_FILM_MISMATCH =="
curl -s -w 'HTTP %{http_code}\n' -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"other-film\", \"venue_id\": \"hall-3\",
  \"valid_from\": \"2026-10-01T00:00:00Z\", \"valid_to\": \"2026-10-31T23:59:59Z\",
  \"max_sessions\": 10, \"pool_id\": 1
}"

echo
echo "== 5. 跨场馆核验 3 场（hall-1/hall-2/hall-1）：各授权与池同时扣减 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-05T19:30:00Z\", \"idempotency_key\": \"pool-demo-1\"
}" | pretty
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-2\",
  \"start_time\": \"2026-10-05T20:00:00Z\", \"idempotency_key\": \"pool-demo-2\"
}" | pretty
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-06T19:30:00Z\", \"idempotency_key\": \"pool-demo-3\"
}" | pretty

echo
echo "== 6. 同一幂等键重试：replay=true，池额度不重复占用 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-05T19:30:00Z\", \"idempotency_key\": \"pool-demo-1\"
}" | pretty

echo
echo "== 7. 授权自身仍有余额，但池已耗尽：POOL_EXHAUSTED =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-2\",
  \"start_time\": \"2026-10-07T19:30:00Z\", \"idempotency_key\": \"pool-demo-4\"
}" | pretty

echo
echo "== 8. 查询池：used=3 / remaining=0 / 绑定授权 2 份 =="
curl -s "$BASE/pools/1" | pretty

echo
echo "== 9. 查询授权：自身余额与池余额分别展示且与池视图一致 =="
curl -s "$BASE/licenses?pool_id=1" | pretty

echo
echo "== 10. 撤销授权不退还池占用：撤销 hall-2 授权后池仍为 used=3 =="
curl -s -X POST "$BASE/licenses/2/revoke" -H 'Content-Type: application/json' \
  -d '{"reason": "场馆临时关闭，已核准占用不退"}' | pretty
curl -s "$BASE/pools/1" | pretty
