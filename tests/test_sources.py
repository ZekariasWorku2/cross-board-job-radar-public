import json
import unittest
from pathlib import Path
from unittest.mock import patch

from crossboard import sources


class FakeResponse:
    def __init__(self, *, text="", data=None, url="https://example.test"):
        self.text = text
        self.content = text.encode("utf-8")
        self._data = data
        self.url = url

    def json(self):
        return self._data


class SourceParserTests(unittest.TestCase):
    def config(self):
        return {
            "search": {
                "max_records_per_source_per_lane": 20,
                "max_records_per_query": 10,
                "request_timeout_seconds": 20,
                "request_delay_seconds": 0,
                "retry_attempts": 1,
                "user_agent": "test",
            },
            "freshness": {"discovery_lookback_days": 1, "max_age_hours": 24},
            "lanes": {
                "data_engineer": {"queries": ["Data Engineer"]},
                "sql_developer": {"queries": ["SQL Developer"]},
            },
        }

    def test_wwr_uses_documented_all_jobs_feed(self):
        xml = """<?xml version='1.0'?><rss><channel><item><title>Acme: SQL Developer</title><link>https://weworkremotely.com/remote-jobs/acme-sql</link><guid>wwr-1</guid><pubDate>Wed, 23 Sep 2026 10:00:00 +0000</pubDate><description><![CDATA[Remote SQL Server and T-SQL work]]></description></item></channel></rss>"""
        with patch("crossboard.sources._request", return_value=FakeResponse(text=xml, url="https://weworkremotely.com/remote-jobs.rss")) as req:
            jobs = sources.fetch_wwr("sql_developer", self.config(), "2026-09-23T12:00:00+00:00")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "SQL Developer")
        self.assertEqual(jobs[0]["company"], "Acme")
        self.assertIn("remote-jobs.rss", req.call_args.args[0])

    def test_jobicy_maps_remote_job_and_requests_public_maximum(self):
        payload = {
            "jobs": [{
                "id": 7,
                "url": "https://jobicy.com/jobs/sql",
                "jobTitle": "SQL Developer",
                "companyName": "Acme",
                "jobGeo": "USA",
                "jobType": ["full-time"],
                "jobDescription": "SQL Server T-SQL SSIS ETL",
                "pubDate": "2026-09-23T10:00:00+00:00",
            }]
        }
        with patch("crossboard.sources._request", return_value=FakeResponse(data=payload)) as req:
            jobs = sources.fetch_jobicy("sql_developer", self.config(), "2026-09-23T12:00:00+00:00")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["workplace_type"], "remote")
        self.assertEqual(jobs[0]["source"], "jobicy")
        self.assertIn("count=200", req.call_args.args[0])
        self.assertIn("geo=usa", req.call_args.args[0])

    def test_builtin_rendered_card_fallback(self):
        html = '''<div data-id="job-card"><a href="/job/data-engineer/123"><h2 data-id="job-title">Data Engineer II</h2></a><div data-id="company-title">Acme</div><div data-id="job-location">Remote, USA</div><span>2 hours ago</span></div>'''
        from bs4 import BeautifulSoup
        jobs = sources._builtin_cards(
            BeautifulSoup(html, "html.parser"),
            "builtin",
            "https://builtin.com/jobs/remote",
            "2026-09-23T12:00:00+00:00",
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["company"], "Acme")
        self.assertIsNotNone(jobs[0]["source_published_at"])

    def test_jobopportunities_requires_confirmed_us_remote_and_24h_cutoff(self):
        payload = {"data": [{
            "id": "abc",
            "slug": "data-engineer-abc",
            "title": "Data Engineer II",
            "company": "Acme",
            "country": "US",
            "location": "Remote - United States",
            "remote": "remote",
            "employment_type": "Full-time",
            "posted_at": "2026-09-23T10:00:00Z",
            "apply_url": "https://acme.example/jobs/abc",
            "description": "Remote SQL ETL data pipelines and AWS",
        }]}
        with patch("crossboard.sources._request", return_value=FakeResponse(data=payload)) as req:
            jobs = sources.fetch_jobopportunities("data_engineer", self.config(), "2026-09-23T12:00:00+00:00")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["workplace_type"], "remote")
        request_url=req.call_args.args[0]
        self.assertIn("remote_confirmed=true", request_url)
        self.assertIn("country=US", request_url)
        self.assertIn("posted_after=2026-09-22T12%3A00%3A00%2B00%3A00", request_url)

    def test_ayla_remote_filter_and_mapping(self):
        payload = {"jobs": [{
            "id": "gov-1",
            "title": "SQL Developer",
            "agency": "Example Agency",
            "locationText": "United States",
            "workArrangement": {"remote": "yes"},
            "jobType": "Full-time",
            "postedDate": "2026-09-23T09:00:00Z",
            "sourceUrl": "https://agency.example/jobs/gov-1",
            "skills": ["SQL Server", "T-SQL", "SSIS"],
        }]}
        with patch("crossboard.sources._request", return_value=FakeResponse(data=payload)) as req:
            jobs = sources.fetch_ayla("sql_developer", self.config(), "2026-09-23T12:00:00+00:00")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["workplace_type"], "remote")
        self.assertIn("workArrangement", req.call_args.args[0])

    def _linkedin_cards(self, start, count):
        cards=[]
        for i in range(start, start+count):
            cards.append(f'''<li data-entity-urn="urn:li:jobPosting:{100000+i}"><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/{100000+i}">open</a><h3>Data Engineer II</h3><h4>Company {i}</h4><span class="job-search-card__location">United States</span><time datetime="2026-09-23T11:00:00Z">1 hour ago</time></li>''')
        return "".join(cards)

    def test_linkedin_exhausts_pages_beyond_old_50_limit_and_uses_24h_remote_us(self):
        cfg=self.config()
        cfg["search"]["max_records_per_source_per_lane"]=100
        cfg["search"]["max_records_per_query"]=100
        calls=[]
        def fake_request(url, config, **kwargs):
            calls.append(url)
            parsed=__import__("urllib.parse",fromlist=["urlparse"]).urlparse(url)
            params=__import__("urllib.parse",fromlist=["parse_qs"]).parse_qs(parsed.query)
            start=int(params.get("start",["0"])[0])
            count=25 if start in (0,25) else 5 if start==50 else 0
            return FakeResponse(text=self._linkedin_cards(start,count),url=url)
        with patch("crossboard.sources._request",side_effect=fake_request):
            jobs=sources.fetch_linkedin_guest("data_engineer",cfg,"2026-09-23T12:00:00+00:00")
        self.assertEqual(len(jobs),55)
        self.assertEqual(len(calls),3)
        self.assertTrue(any("start=50" in u for u in calls))
        self.assertTrue(all("f_TPR=r86400" in u for u in calls))
        self.assertTrue(all("f_WT=2" in u for u in calls))
        self.assertTrue(all("location=United+States" in u for u in calls))

    def test_himalayas_pages_until_result_set_is_exhausted(self):
        cfg=self.config(); cfg["search"]["max_records_per_source_per_lane"]=100
        calls=[]
        def fake_request(url, config, **kwargs):
            calls.append(url)
            page=int(__import__("urllib.parse",fromlist=["parse_qs","urlparse"]).parse_qs(__import__("urllib.parse",fromlist=["urlparse"]).urlparse(url).query)["page"][0])
            if page==1:
                rows=[{"guid":"h1","title":"Data Engineer II","companyName":"A","locationRestrictions":["US"],"pubDate":"2026-09-23T11:00:00Z","applicationLink":"https://a.example/h1","description":"Remote US Data Engineer"}]
            elif page==2:
                rows=[{"guid":"h2","title":"Data Engineer II","companyName":"B","locationRestrictions":["US"],"pubDate":"2026-09-23T10:00:00Z","applicationLink":"https://b.example/h2","description":"Remote US Data Engineer"}]
            else:
                rows=[]
            return FakeResponse(data={"jobs":rows},url=url)
        with patch("crossboard.sources._request",side_effect=fake_request):
            jobs=sources.fetch_himalayas("data_engineer",cfg,"2026-09-23T12:00:00+00:00")
        self.assertEqual(len(jobs),2)
        self.assertEqual(len(calls),3)
        self.assertTrue(any("page=2" in u for u in calls))

    def test_ayla_pages_when_first_page_is_full(self):
        cfg=self.config(); cfg["search"]["max_records_per_source_per_lane"]=200
        first=[]
        for i in range(100):
            first.append({"id":f"g{i}","title":"Data Engineer II","agency":"Agency","locationText":"United States","workArrangement":{"remote":"yes"},"postedDate":"2026-09-23T11:00:00Z","sourceUrl":f"https://agency.example/g{i}","skills":["SQL","ETL"]})
        second=[{"id":"g100","title":"Data Engineer II","agency":"Agency","locationText":"United States","workArrangement":{"remote":"yes"},"postedDate":"2026-09-23T10:00:00Z","sourceUrl":"https://agency.example/g100","skills":["SQL","ETL"]}]
        calls=[]
        def fake_request(url, config, **kwargs):
            calls.append(url)
            page=int(__import__("urllib.parse",fromlist=["parse_qs","urlparse"]).parse_qs(__import__("urllib.parse",fromlist=["urlparse"]).urlparse(url).query)["page"][0])
            return FakeResponse(data={"jobs": first if page==0 else second if page==1 else [], "pagination":{"total":101}},url=url)
        with patch("crossboard.sources._request",side_effect=fake_request):
            jobs=sources.fetch_ayla("data_engineer",cfg,"2026-09-23T12:00:00+00:00")
        self.assertEqual(len(jobs),101)
        self.assertEqual(len(calls),2)
        self.assertTrue(any("page=1" in u for u in calls))

    def test_remotelanders_is_removed_from_production_config(self):
        config=json.loads(Path("tests/fixtures/config.json").read_text(encoding="utf-8"))
        self.assertNotIn("remotelanders",config["sources"])


if __name__ == "__main__":
    unittest.main()
