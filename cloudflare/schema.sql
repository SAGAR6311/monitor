create table if not exists issues(
  id integer primary key, project text, fingerprint text unique, title text, culprit text,
  level text, platform text, environment text, count int, first_seen real, last_seen real,
  status text default 'unresolved');
create table if not exists events(id integer primary key, issue_id int, received real, data text);
create index if not exists events_issue on events(issue_id, id);
