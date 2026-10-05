-- epoch 2 (r1): 1시간 CAGG refresh policy. 범위 [now-3일, now-1시간]을 매시간 다시 맞춘다 —
-- 1분 policy(1일)보다 넓게 잡아 압축(2일) 경계 근처에 늦게 들어온 행·수동 적재분도 반영한다.
-- 이 범위보다 오래된 구간은 policy가 채우지 않는다(업그레이드 이전 이력 backfill은 M3a 계약).
SELECT add_continuous_aggregate_policy(
    'tag_history_1h',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);
