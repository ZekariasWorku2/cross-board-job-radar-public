import unittest
from crossboard.policy import excluded_source, azure_primary, sql_server_primary
from crossboard.identity import already_sent, remember_sent, same_identity
from crossboard.freshness import assess
from crossboard.resume_match import compare_resume
from crossboard import rules_de, rules_sql

NOW="2026-10-23T16:00:00+00:00"

def job(title="Data Engineer II",company="Acme",description=None,**extra):
    j={"source":"linkedin","title":title,"company":company,
       "description":description or "Build AWS Glue PySpark ETL data pipelines to S3 Redshift with SQL Server data warehousing, Airflow and Athena.",
       "location":"Remote, United States","workplace_type":"remote",
       "page_verified_live":True}
    j.update(extra)
    return j

class SourcePolicyTests(unittest.TestCase):
    def test_six_sources_blocked(self):
        for source in ("RemoteHunter","TalentHop","Torentify","Wiraa","Netrolynx AI","Ladders"):
            with self.subTest(source=source):
                self.assertTrue(excluded_source(job(company=source)))
    def test_linkedin_allowed(self):
        self.assertFalse(excluded_source(job(company="Actual Employer",source="linkedin")))
    def test_azure_title_rejected(self):
        self.assertTrue(azure_primary(job(title="Cloud Data Engineer (Azure)")))
    def test_optional_azure_allowed(self):
        j=job(title="AWS Data Engineer",description="Build AWS Glue and Redshift data pipelines. Azure is a nice-to-have optional skill.")
        self.assertFalse(azure_primary(j))
    def test_required_secondary_azure_in_aws_role_allowed(self):
        j=job(title="AWS Data Engineer",description="The primary AWS data platform uses Glue and Redshift. Azure Data Factory experience is required to support a secondary legacy integration.")
        self.assertFalse(azure_primary(j))
    def test_azure_first_jd_rejected(self):
        self.assertTrue(azure_primary(job(title="Data Engineer",description="The primary cloud platform is Microsoft Azure. Build pipelines with Azure Data Factory and Azure Synapse.")))
    def test_sql_server_primary(self):
        self.assertTrue(sql_server_primary(job(title="Principal SQL Server Developer")))
        self.assertFalse(sql_server_primary(job(title="Database Developer",description="Primary Oracle PL/SQL database developer")))

class ResumeMatchingTests(unittest.TestCase):
    def test_resume_evidence(self):
        r=compare_resume(job())
        self.assertTrue(r["resume_evidence_available"])
        self.assertGreaterEqual(r["resume_fit_score"],65)
        self.assertTrue(any(x["group"]=="aws_cloud" for x in r["resume_skill_matches"]))
    def test_azure_data_engineer_rejected_independent_of_score(self):
        r=rules_de.validate(job(title="Azure Data Engineer"))
        self.assertEqual(r["validation_class"],"rejected")
    def test_sql_any_seniority(self):
        settings={"require_live_verification":True}
        for title in ("Senior SQL Server Developer","Lead SQL Server Developer",
                      "Principal SQL Server Database Engineer","Staff SQL Server Developer"):
            with self.subTest(title=title):
                r=rules_sql.validate(job(title=title,description="Develop SQL Server stored procedures, T-SQL views, SSIS ETL and tune indexes. Fully remote in the United States."),settings)
                self.assertTrue(r["validation_alertable"],r["validation_reject_reason"])
    def test_reporting_and_etl_sql_server_family(self):
        settings={"require_live_verification":True}
        samples=[
            ("SSRS Developer","Develop SQL Server Reporting Services SSRS reports and T-SQL views. Fully remote in the United States."),
            ("SSAS Developer","Develop SQL Server Analysis Services SSAS data models and stored procedures. Fully remote in the United States."),
            ("ETL Engineer","Build SQL Server SSIS ETL packages and stored procedures. Fully remote in the United States."),
            ("Data Warehouse Engineer","Build SQL Server warehouse fact dimension models using SSIS and T-SQL. Fully remote in the United States."),
        ]
        for title,description in samples:
            with self.subTest(title=title):
                result=rules_sql.validate(job(title=title,description=description),settings)
                self.assertTrue(result["validation_alertable"],result["validation_reject_reason"])
    def test_dba_excluded(self):
        r=rules_sql.validate(job(title="Senior SQL Server DBA"),{"require_live_verification":True})
        self.assertFalse(r["validation_alertable"])

class IdentityTests(unittest.TestCase):
    def test_linkedin_slug_changes_do_not_resend(self):
        state={"sent":{}}
        j=job(title="Data Engineer I",url="https://www.linkedin.com/jobs/view/data-engineer-i-at-acme-4412345678",
              apply_url="https://www.linkedin.com/jobs/view/data-engineer-i-at-acme-4412345678",canonical_id="first")
        remember_sent(state,"data_engineer",j,NOW)
        new=job(title="Data Engineer I",url="https://www.linkedin.com/jobs/view/4412345678?refId=new",
                apply_url="https://www.linkedin.com/jobs/view/4412345678?refId=new",canonical_id="other")
        self.assertTrue(already_sent(state,new,"other"))
    def test_same_requisition_new_url(self):
        self.assertTrue(same_identity(job(requisition_id="A1",url="https://a.example/jobs/old"),
                                      job(requisition_id="A1",url="https://other.example/jobs/new")))
    def test_distinct_requisitions_preserved(self):
        self.assertFalse(same_identity(job(requisition_id="A1",url="https://a.example/jobs/a"),
                                       job(requisition_id="B2",url="https://b.example/jobs/b")))

class OriginalFreshnessTests(unittest.TestCase):
    def test_linkedin_new_label_is_uncertain(self):
        j=job(source_published_at="2026-10-23T14:00:00+00:00",source_time_semantics="linkedin_posted_label",
              page_source_url="https://www.linkedin.com/jobs/view/4412345678")
        r=assess([j],now=NOW)
        self.assertEqual(r["freshness_status"],"unknown")
    def test_employer_original_timestamp_is_fresh(self):
        j=job(page_source_url="https://careers.example.com/jobs/abcd",page_time_semantics="employer_date_posted",
              page_published_at="2026-10-23T14:00:00+00:00")
        r=assess([j],now=NOW)
        self.assertEqual(r["freshness_decision"],"VERIFIED_FRESH")
    def test_repost_from_old_board_appearance_is_stale(self):
        j=job(page_source_url="https://careers.example.com/jobs/abcd",page_time_semantics="employer_date_posted",
              page_published_at="2026-10-23T14:00:00+00:00",
              source_published_at="2026-10-20T14:00:00+00:00")
        r=assess([j],now=NOW)
        self.assertEqual(r["freshness_status"],"stale")
    def test_date_only_overlaps_cutoff_is_uncertain(self):
        j=job(page_source_url="https://careers.example.com/jobs/abcd",page_time_semantics="employer_date_posted",
              page_published_raw="2026-10-22")
        r=assess([j],now=NOW)
        self.assertEqual(r["freshness_decision"],"DATE_BOUNDARY_UNCERTAIN")
    def test_historical_first_seen_proves_old(self):
        j=job(page_source_url="https://careers.example.com/jobs/abcd",page_time_semantics="employer_date_posted",
              page_published_at="2026-10-23T15:00:00+00:00")
        r=assess([j],previous={"first_seen":"2026-10-01T10:00:00+00:00"},now=NOW)
        self.assertEqual(r["freshness_status"],"stale")

if __name__=="__main__": unittest.main()
