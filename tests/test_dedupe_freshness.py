import unittest
from crossboard.dedupe import same_vacancy
from crossboard.freshness import assess

class CrossBoardTests(unittest.TestCase):
    def test_same_job_different_board_same_company_title(self):
        a={"company":"Acme Inc","title":"SQL Database Developer","description":"SQL Server T-SQL SSIS stored procedures", "url":"https://linkedin.com/jobs/view/1"}
        b={"company":"Acme LLC","title":"SQL Database Developer - Remote","description":"SQL Server T-SQL SSIS stored procedures", "url":"https://dice.com/job-detail/2"}
        self.assertTrue(same_vacancy(a,b))
    def test_different_requisitions_remain_distinct(self):
        a={"company":"Acme","title":"Data Engineer","requisition_id":"100","url":"https://x/a"}
        b={"company":"Acme","title":"Data Engineer","requisition_id":"101","url":"https://x/b"}
        self.assertFalse(same_vacancy(a,b))
    def test_repost_older_copy_makes_cluster_stale(self):
        cluster=[
          {"source":"linkedin","source_published_at":"2026-09-20T10:00:00Z","source_time_semantics":"linkedin_posted_label"},
          {"source":"indeed","source_published_at":"2026-09-17T10:00:00Z","source_time_semantics":"page_date_posted"},
        ]
        r=assess(cluster,{},"2026-09-20T12:00:00Z",24,10)
        self.assertEqual(r["freshness_status"],"stale")
        self.assertTrue(r["earliest_published_at"].startswith("2026-09-17"))
    def test_fresh_24_hour_boundary(self):
        cluster=[{"source":"dice","page_verified_live":True,"page_source_url":"https://careers.acme.example/jobs/123","page_published_at":"2026-09-19T12:00:00Z","page_time_semantics":"employer_date_posted"}]
        self.assertEqual(assess(cluster,{},"2026-09-20T12:00:00Z",24,10)["freshness_status"],"fresh")
    def test_unknown_date_not_treated_as_fresh(self):
        self.assertEqual(assess([{"source":"x"}],{},"2026-09-20T12:00:00Z",24,10)["freshness_status"],"unknown")

if __name__=="__main__": unittest.main()
