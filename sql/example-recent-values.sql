-- Latest stored value per tag for a subset of tags.
-- Edit the tag list, then run it from the SQL card on the dashboard.
select name,
       name->ip_input_value as value,
       name->ip_input_time  as ts
from   all_records
where  name in ('REACTOR_TEMP', 'FLOW_101')
