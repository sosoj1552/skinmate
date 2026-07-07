-- 최초 기동 시 1회 실행 (docker-entrypoint-initdb.d).
-- 이후 스키마/그래프 온톨로지는 /db/migrations에서 관리한다 (WBS A1.1).
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;

-- search_path: public 우선(무자격 CREATE TABLE/DML이 public에 생성되도록).
-- ag_catalog는 뒤에 둬 agtype 등은 계속 resolve됨. AGE cypher 실행 세션(choke/003)은
-- 자체적으로 search_path=ag_catalog,... 를 세팅한다.
ALTER DATABASE skinmate SET search_path = "$user", public, ag_catalog;
