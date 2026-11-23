import sqlite3

c = sqlite3.connect("data/pipeline.db")
for t, s in c.execute("SELECT json_extract(data_json,'$.content.title'), source_name FROM records "
                      "WHERE record_type='JOB' AND json_extract(data_json,'$.content.role_family')='Other'"):
    print(s, "|", t)