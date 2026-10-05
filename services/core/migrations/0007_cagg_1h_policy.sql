-- epoch 2 (r1): 1시간 CAGG refresh policy. 늦게 들어온 replay(collector ring)를 3일까지 반영한다.
SELECT add_continuous_aggregate_policy(
    'tag_history_1h',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);
