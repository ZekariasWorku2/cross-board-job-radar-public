"""Cross-board hard rejection of explicitly non-remote jobs."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from crossboard import rules_de, rules_sql
from crossboard.remote import remote_status
from crossboard.sources import enrich_page
from crossboard.engine import scan, pending_for_lane
from tests.test_manual_review import config, job, history, NOW


class WorkplaceHardRejectionTests(unittest.TestCase):
    def test_board_metadata_rejected_in_both_job_lanes(self):
        cases=(
            ("linkedin","On-site"), ("indeed","HYBRID"),
            ("dice","on site"), ("remoteok","in-office"),
            ("himalayas","Hybrid, 3 days in office"),
            ("workingnomads","on-site"),
        )
        for source,value in cases:
            with self.subTest(source=source,value=value):
                de=job(source=source,workplace_type=value)
                sql={**de,"title":"SQL Server Developer",
                     "description":("Develop SQL Server T-SQL stored procedures and SSIS ETL, "
                                    "query tuning and database design. Fully remote in the US.")}
                self.assertEqual(remote_status(de)[0],"reject")
                self.assertEqual(rules_de.validate(de)["validation_class"],"rejected")
                self.assertEqual(rules_sql.validate(sql)["validation_class"],"rejected")

    def test_page_metadata_overrides_board_remote(self):
        for source in ("linkedin","indeed","dice","remoteok","jobicy"):
            for value in ("On-site","Hybrid"):
                with self.subTest(source=source,value=value):
                    listing=job(source=source,workplace_type="remote",
                                page_workplace_type=value)
                    self.assertEqual(remote_status(listing)[0],"reject")

    def test_location_badges_rejected_on_all_sources(self):
        for location in ("On-site - Philadelphia, PA","Hybrid - Altamonte Springs, FL",
                         "On site, Oklahoma City, OK"):
            with self.subTest(location=location):
                self.assertEqual(remote_status(job(location=location))[0],"reject")

    def test_structured_job_page_hybrid_label_is_captured(self):
        html=('<html><body><section class="job-header">'
              '<span class="workplace-type">Hybrid</span>'
              '<h1>Data Engineer II</h1></section></body></html>')
        with patch("crossboard.sources._request",
                   return_value=SimpleNamespace(text=html,url="https://careers.example.com/jobs/123")):
            result=enrich_page(job(source="indeed",apply_url="https://careers.example.com/jobs/123"),config())
        self.assertEqual(result["page_workplace_type"],"hybrid")
        self.assertEqual(remote_status(result)[0],"reject")

    def test_linkedin_topcard_onsite_badge_is_captured(self):
        html=('<html><body><div class="top-card-layout">'
              '<span class="topcard__flavor topcard__flavor--bullet">On-site</span>'
              '</div></body></html>')
        with patch("crossboard.sources._request",
                   return_value=SimpleNamespace(text=html,url="https://www.linkedin.com/jobs/view/123")):
            result=enrich_page(job(source="linkedin",apply_url="https://www.linkedin.com/jobs/view/123"),config())
        self.assertEqual(result["page_workplace_type"],"on-site")
        self.assertEqual(remote_status(result)[0],"reject")

    def test_explicit_onsite_overrides_conflicting_schema_remote(self):
        html=('''<html><head><script type="application/ld+json">'''
              '''{"@context":"https://schema.org","@type":"JobPosting",'''
              '''"title":"Data Engineer II","jobLocationType":"TELECOMMUTE"}'''
              '''</script></head><body><div class="job-header">'''
              '''<span class="workplace-type">On-site</span></div></body></html>''')
        with patch("crossboard.sources._request",
                   return_value=SimpleNamespace(text=html,url="https://careers.example.com/jobs/124")):
            result=enrich_page(job(source="linkedin",apply_url="https://careers.example.com/jobs/124"),config())
        self.assertEqual(result["page_workplace_type"],"on-site")
        self.assertEqual(remote_status(result)[0],"reject")

    def test_hybrid_cloud_is_not_a_workplace_requirement(self):
        listing=job(description="Build hybrid cloud data pipelines using AWS Glue and SQL.",
                    page_workplace_type="")
        self.assertNotEqual(remote_status(listing)[0],"reject")

    @patch("crossboard.engine.enrich_page")
    @patch("crossboard.engine.fetch_source")
    def test_bad_duplicate_member_blocks_all_email_classes(self,fetch,enrich):
        cfg=config()
        cfg["sources"]={"weworkremotely":{"enabled":True},
                        "indeed":{"enabled":True}}
        original=job(source="weworkremotely",source_job_id="a",key="a",
                     url="https://careers.example.com/jobs/456",
                     apply_url="https://careers.example.com/jobs/456")
        bad=job(source="indeed",source_job_id="b",key="b",
                url="https://careers.example.com/jobs/456",
                apply_url="https://careers.example.com/jobs/456",
                workplace_type="on-site")
        fetch.side_effect=lambda source,lane,cfg,now,**kw: [original] if source=="weworkremotely" else [bad]
        enrich.side_effect=lambda row,cfg:{**row,"page_verified_live":True}
        state=history()
        state["sources"]["indeed"]={"initialized_at":"2026-10-20T00:00:00Z"}
        result=scan(cfg,state,NOW)
        self.assertEqual(pending_for_lane(state,"data_engineer"),[])
        self.assertFalse(result["lanes"]["data_engineer"]["manual_review"])
        self.assertFalse(result["lanes"]["data_engineer"]["qualifying"])

    def test_unspecified_workplace_still_manual_review(self):
        listing=job(workplace_type="unknown",location="Oklahoma City, OK",
                    description="Build AWS Glue ETL data pipelines with SQL and Python.")
        status,why=remote_status(listing)
        self.assertEqual(status,"review")
        self.assertIn("not confirmed",why)


if __name__=="__main__":
    unittest.main()
