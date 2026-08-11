-- Tags and their descriptions, newest first.
-- A plain SELECT, so it runs without ASPY21_SQL_ALLOW_WRITES.
select name,
       name->ip_description  as description,
       name->ip_input_value  as current_value
from   all_records
where  name like '%'
