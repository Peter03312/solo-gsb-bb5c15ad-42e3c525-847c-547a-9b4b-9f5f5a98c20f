#!/usr/bin/env bash
# 放映许可核验服务调用示例。启动服务后执行：bash examples/demo.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 录入两份授权（同影片同场馆，到期时间不同） =="
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d '{
  "film_id": "film-autumn-river", "venue_id": "hall-1",
  "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-10T23:59:59Z",
  "max_sessions": 1, "note": "早到期授权，应被优先消耗"
}' | pretty
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d '{
  "film_id": "film-autumn-river", "venue_id": "hall-1",
  "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-31T23:59:59Z",
  "max_sessions": 2, "note": "兜底授权"
}' | pretty

echo
echo "== 2. 核验场次：命中最早到期的授权 1，剩余额度 0 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d '{
  "film_id": "film-autumn-river", "venue_id": "hall-1",
  "start_time": "2026-10-05T19:30:00Z", "idempotency_key": "screening-0001"
}' | pretty

echo
echo "== 3. 同一幂等键重试：返回原结果（replay=true），不重复扣额度 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d '{
  "film_id": "film-autumn-river", "venue_id": "hall-1",
  "start_time": "2026-10-05T19:30:00Z", "idempotency_key": "screening-0001"
}' | pretty

echo
echo "== 4. 同一幂等键但内容变化：409 冲突 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/verifications" \
  -H 'Content-Type: application/json' -d '{
  "film_id": "film-autumn-river", "venue_id": "hall-1",
  "start_time": "2026-10-06T19:30:00Z", "idempotency_key": "screening-0001"
}'

echo
echo "== 5. 授权 1 额度已尽，新场次落到授权 2 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d '{
  "film_id": "film-autumn-river", "venue_id": "hall-1",
  "start_time": "2026-10-06T19:30:00Z", "idempotency_key": "screening-0002"
}' | pretty

echo
echo "== 6. 时间不在任何授权时段内：明确拒绝 TIME_OUT_OF_WINDOW =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d '{
  "film_id": "film-autumn-river", "venue_id": "hall-1",
  "start_time": "2027-01-05T19:30:00Z", "idempotency_key": "screening-0003"
}' | pretty

echo
echo "== 7. 撤销授权 2：后续核验被拒绝（授权 1 额度已尽、授权 2 已撤销），已核准记录仍可追溯 =="
curl -s -X POST "$BASE/licenses/2/revoke" -H 'Content-Type: application/json' \
  -d '{"reason": "版权方临时撤片"}' | pretty
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d '{
  "film_id": "film-autumn-river", "venue_id": "hall-1",
  "start_time": "2026-10-07T19:30:00Z", "idempotency_key": "screening-0004"
}' | pretty

echo
echo "== 8. 查询核验流水（含撤销前已核准的 screening-0001/0002） =="
curl -s "$BASE/verifications" | pretty

echo
echo "== 9. 查看授权实时额度与撤销状态 =="
curl -s "$BASE/licenses" | pretty
