"""Offline regression tests. Never reach live job boards or send email."""
import unittest
from datetime import date
from unittest.mock import patch
import requests
from crossboard.sources import _as_iso, _fresh, enrich_page
from crossboard.remote import remote_status
from crossboard.engine import scan, pending_for_lane
from crossboard.emailer import _plain

NOW="2026-10-25T12:00:00+00:00"

def config():
    return {
        "timezone":"America/New_York",
        "freshness":{"max_age_hours":24,"max_future_skew_minutes":10},
        "audit":{"retention_days":120},
        "search":{"page_verification_workers":1,"request_timeout_seconds":25,
                  "retry_attempts":2,"request_delay_seconds":0},
        "lanes":{
            "data_engineer":{"enabled":True,"max_required_years":6,"min_possible_alert_score":60},
            "sql_developer":{"enabled":False,"min_possible_alert_score":55,"require_live_verification":True}},
        "sources":{"weworkremotely":{"enabled":True}},
    }

def job(**updates):
    j={"key":"case-one","source":"weworkremotely","source_job_id":"case-one",
       "title":"Data Engineer II","company":"Example","location":"Remote",
       "workplace_type":"remote","description":(
           "Build AWS Glue PySpark ETL pipelines using SQL Server, S3, Redshift, "
           "data warehouse dimensional modeling and Apache Airflow orchestration."),
       "source_published_at":"2026-10-25T10:00:00+00:00",
       "url":"https://weworkremotely.com/remote-jobs/example/data-engineer-ii",
       "apply_url":"https://weworkremotely.com/remote-jobs/example/data-engineer-ii"}
    j.update(updates)
    return j

def history():
    return {"version":1,"radar":"cross-board-job-radar",
        "sources":{"weworkremotely":{"initialized_at":"2026-10-20T00:00:00Z"}},
        "jobs":{"unrelated":{"title":"Old","company":"Different"}},
        "sent":{},"outbox":{},"audit":{},"last_run_at":None}

class ManualReviewRegressionTests(unittest.TestCase):
    def test_day_only_is_not_assumed_midnight_utc(self):
        self.assertEqual(_as_iso(date(2026,10,24)),"2026-10-24")
        self.assertTrue(_fresh(date(2026,10,24),NOW,config()))
        self.assertFalse(_fresh(date(2026,10,22),NOW,config()))

    def test_unknown_location_needs_review_but_hybrid_is_rejected(self):
        status,reason=remote_status(job(location="Remote",workplace_type="unknown"))
        self.assertEqual(status,"review")
        self.assertIn("not confirmed",reason)
        rejected,reason=remote_status(job(description="This role is hybrid two days in the office."))
        self.assertEqual(rejected,"reject")

    @patch("crossboard.sources._request")
    def test_himalayas_checks_public_api_not_protected_html(self,mock_request):
        response=unittest.mock.Mock()
        response.json.return_value={"job":{"description":"AWS Glue data engineering role"}}
        mock_request.return_value=response
        listing=job(source="himalayas",
            himalayas_guid="https://himalayas.app/companies/acme/jobs/data-engineer",
            source_job_id="https://himalayas.app/companies/acme/jobs/data-engineer",
            apply_url="https://himalayas.app/companies/acme/jobs/data-engineer")
        result=enrich_page(listing,config())
        self.assertTrue(result["page_verified_live"])
        self.assertNotIn("page_published_at",result)
        self.assertEqual(mock_request.call_args.args[0],
            "https://himalayas.app/jobs/api/companies/acme/jobs/data-engineer")

    @patch("crossboard.sources._request")
    def test_himalayas_410_is_closed_not_manual(self,mock_request):
        resp=requests.Response()
        resp.status_code=410
        mock_request.side_effect=requests.HTTPError("Gone",response=resp)
        result=enrich_page(job(source="himalayas",
            himalayas_guid="https://himalayas.app/companies/acme/jobs/data-engineer"),config())
        self.assertTrue(result.get("page_closed_confirmed"))
        self.assertIs(result.get("page_verified_live"),False)

    @patch("crossboard.sources._request")
    def test_himalayas_403_is_technical_failure_not_closed(self,mock_request):
        resp=requests.Response()
        resp.status_code=403
        mock_request.side_effect=requests.HTTPError("Forbidden",response=resp)
        result=enrich_page(job(source="himalayas",
            himalayas_guid="https://himalayas.app/companies/acme/jobs/data-engineer"),config())
        self.assertIsNone(result["page_verified_live"])
        self.assertIn("403",result["page_error"])
        self.assertFalse(result.get("page_closed_confirmed",False))

    @patch("crossboard.engine.enrich_page")
    @patch("crossboard.engine.fetch_source")
    def test_manual_verification_job_is_queued_for_email(self,fetch,enrich):
        fetch.return_value=[job()]
        enrich.side_effect=lambda j,c:{**j,"page_verified_live":None,"page_error":"HTTP 403"}
        state=history()
        result=scan(config(),state,NOW)
        self.assertEqual(len(result["lanes"]["data_engineer"]["manual_review"]),1)
        pending=pending_for_lane(state,"data_engineer")
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]["job_notification_class"],"needs_manual_verification")
        self.assertTrue(any("Original" in x for x in pending[0]["verification_needed"]))
        self.assertTrue(any("United States" in x for x in pending[0]["verification_needed"]))
        body=_plain("data_engineer",pending,result["health"],"America/New_York")
        self.assertIn("NEEDS MANUAL VERIFICATION",body)
        self.assertIn("Verify manually",body)

    @patch("crossboard.engine.enrich_page")
    @patch("crossboard.engine.fetch_source")
    def test_explicit_hybrid_is_rejected_and_not_emailed(self,fetch,enrich):
        fetch.return_value=[job(description="Hybrid role, two days per week in the office. "+
            job()["description"])]
        enrich.side_effect=lambda j,c:{**j,"page_verified_live":True}
        state=history()
        result=scan(config(),state,NOW)
        self.assertEqual(pending_for_lane(state,"data_engineer"),[])
        self.assertGreater(len(result["lanes"]["data_engineer"]["rejected"]),0)

if __name__=="__main__":
    unittest.main()
