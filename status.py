import sqlite3

c = sqlite3.connect("data/pipeline.db")


def q(sql, *a):
    return c.execute(sql, a).fetchall()


print("records by type:", dict(q("SELECT record_type, COUNT(*) FROM records GROUP BY 1")))
print("papers with GitHub stars:", q("SELECT COUNT(*) FROM records WHERE record_type='RESEARCH_PAPER' "
      "AND json_extract(data_json,'$.content.github_stars') IS NOT NULL")[0][0])
print("startups aiRelated:", q("SELECT COUNT(*) FROM records WHERE record_type='STARTUP' "
      "AND json_extract(data_json,'$.content.data.aiRelated')=1")[0][0])
print("products by pricingModel:", dict(q("SELECT json_extract(data_json,'$.content.pricingModel'), COUNT(*) "
      "FROM records WHERE record_type='PRODUCT' GROUP BY 1")))
print("products by method:", dict(q("SELECT COALESCE(json_extract(data_json,'$.content.method'),'rules'), COUNT(*) "
      "FROM records WHERE record_type='PRODUCT' GROUP BY 1")))
print("product crawl outcomes:", dict(q("SELECT value, COUNT(*) FROM crawl_state WHERE source='products' "
      "GROUP BY 1 ORDER BY 2 DESC")))
print("news by source / date confidence:", q("SELECT source_name, json_extract(data_json,'$.content.date_confidence'), "
      "COUNT(*) FROM records WHERE record_type='NEWS' GROUP BY 1,2"))
print("jobs by source:", dict(q("SELECT source_name, COUNT(*) FROM records WHERE record_type='JOB' GROUP BY 1")))
print("jobs by role family:", dict(q("SELECT json_extract(data_json,'$.content.role_family'), COUNT(*) "
      "FROM records WHERE record_type='JOB' GROUP BY 1 ORDER BY 2 DESC")))
print("entity mapping by method:", dict(q("SELECT method, COUNT(*) FROM entity_mapping_log "
      "WHERE record_type!='DEMO' GROUP BY 1 ORDER BY 2 DESC")))
print("LLM usage today:", q("SELECT tier, requests, tokens FROM llm_usage WHERE day=date('now')"))