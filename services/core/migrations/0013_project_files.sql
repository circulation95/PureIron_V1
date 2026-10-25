-- epoch 5(r4): 적용된 고객 프로젝트 정규형 파일 원본(M5-0, plan D5). `pureiron-core project apply --tar`가
-- 한 트랜잭션에서 정의(devices·tags·alarms)·project_state와 함께 전체를 교체한다. DB 정의 행만으로는
-- 파일 배치(알람이 어느 파일인지)·project.yaml 메타를 되살릴 수 없어서 둔다 — `project export`가
-- 이 파일들에 현재 DB 운영 값을 덮어 현재 유효 트리(백업·드래프트)를 만든다.
-- content: project-tools 정규형 바이트(UTF-8 YAML), sha256: content의 hex 다이제스트.
CREATE TABLE project_files (
    path    text  PRIMARY KEY,
    content bytea NOT NULL,
    sha256  text  NOT NULL
);
