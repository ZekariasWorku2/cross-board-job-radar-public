import unittest
from unittest.mock import patch

from crossboard.engine import _external_job_id, mark_audit_delivery, scan


class DiscoveryAuditTests(unittest.TestCase):
    def test_linkedin_job_id_is_extracted_from_slug_url(self):
        job={
            "source":"linkedin",
            "url":"https://www.linkedin.com/jobs/view/data-engineer-at-example-4471866259?pageNum=0",
        }
        self.assertEqual(_external_job_id(job),"4471866259")

    @patch("crossboard.engine.fetch_source")
    def test_every_raw_discovery_gets_persistent_audit_record(self,mock_fetch):
        mock_fetch.return_value=[{
            "key":"raw-key-1",
            "source":"linkedin",
            "title":"Product Manager",
            "company":"Example Co",
            "location":"United States",
            "url":"https://www.linkedin.com/jobs/view/product-manager-at-example-4471866259?pageNum=0",
            "source_published_at":"2026-09-25T10:00:00+00:00",
        }]
        config={
            "freshness":{"max_age_hours":24,"max_future_skew_minutes":10},
            "audit":{"retention_days":14},
            "search":{"page_verification_workers":1},
            "lanes":{
                "data_engineer":{"enabled":True},
                "sql_developer":{"enabled":False},
            },
            "sources":{"linkedin":{"enabled":True}},
        }
        state={"version":1,"radar":"cross-board-job-radar","sources":{},"jobs":{},"sent":{},"outbox":{},"audit":{},"last_run_at":None}
        result=scan(config,state,"2026-09-25T12:00:00+00:00")
        self.assertEqual(len(result["discovery_audit"]),1)
        audit=result["discovery_audit"][0]
        self.assertEqual(audit["source_job_id"],"4471866259")
        self.assertEqual(audit["final_status"],"rejected_pre_enrichment")
        self.assertIn("linkedin:4471866259",state["audit"])
        self.assertEqual(state["audit"]["linkedin:4471866259"]["last_status"],"rejected_pre_enrichment")

    def test_email_delivery_updates_audit_status(self):
        record={
            "audit_key":"linkedin:4471866259",
            "source":"linkedin",
            "source_job_id":"4471866259",
            "job_key":"k",
            "lane_decisions":[{
                "lane":"data_engineer",
                "canonical_id":"cid1",
                "delivery_status":"queued_for_email",
            }],
            "final_status":"queued_for_email",
            "final_reason":None,
        }
        state={"audit":{"linkedin:4471866259":{"last_status":"queued_for_email","last_reason":None,"last_lane_decisions":[]}}}
        result={"discovery_audit":[record]}
        mark_audit_delivery(state,result,"data_engineer",[{"canonical_id":"cid1"}],"emailed")
        self.assertEqual(record["final_status"],"emailed")
        self.assertEqual(state["audit"]["linkedin:4471866259"]["last_status"],"emailed")


if __name__=="__main__":
    unittest.main()
