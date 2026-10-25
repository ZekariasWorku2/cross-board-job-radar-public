import unittest
from crossboard import rules_de, rules_sql


class DataEngineerRulesTests(unittest.TestCase):
    def base(self, **kw):
        job={"title":"Data Engineer II","company":"Example","source":"linkedin","location":"Remote, United States","workplace_type":"remote","description":"Build AWS data pipelines with SQL, ETL, data warehouse, Python, PySpark and dimensional modeling. 4 years experience.","page_verified_live":True}
        job.update(kw); return job

    def test_data_title_whole_words(self):
        self.assertTrue(rules_de.title_matches("Cloud Data Platform Engineer II"))
        self.assertFalse(rules_de.title_matches("ETL Engineer"))

    def test_senior_rejected(self):
        self.assertIn("Excluded title", rules_de.title_rejection("Senior Data Engineer"))

    def test_over_six_years_rejected(self):
        result=rules_de.validate(self.base(description="Data pipelines ETL SQL AWS data warehouse Python. Minimum 7 years experience."),{"max_required_years":6,"strong_score_threshold":78,"min_possible_alert_score":55,"max_travel_percent":20})
        self.assertEqual(result["validation_class"],"rejected")

    def test_clearance_eligibility_allowed(self):
        result=rules_de.validate(self.base(description="Build data pipelines with SQL ETL AWS data warehouse Python. Must be eligible to obtain a Secret clearance. 4 years experience."),{"max_required_years":6,"strong_score_threshold":78,"min_possible_alert_score":55,"max_travel_percent":20})
        self.assertNotEqual(result["validation_reject_reason"],"Requires an active/current security clearance or Public Trust")

    def test_active_clearance_rejected(self):
        result=rules_de.validate(self.base(description="Build ETL pipelines. Active Secret clearance required."),{"max_required_years":6,"strong_score_threshold":78,"min_possible_alert_score":55,"max_travel_percent":20})
        self.assertEqual(result["validation_class"],"rejected")

    def test_hybrid_rejected(self):
        result=rules_de.validate(self.base(description="This position is hybrid 2 days per week in the office."),{})
        self.assertEqual(result["validation_class"],"rejected")

    def test_in_office_overrides_remote_board_metadata(self):
        result=rules_de.validate(self.base(description="This will be an in-office role in our Plymouth, MN location. Build SQL ETL data pipelines."),{})
        self.assertEqual(result["validation_class"],"rejected")
        self.assertIn("hybrid/on-site",result["validation_reject_reason"])

    def test_employer_page_hybrid_overrides_remote_board_badge(self):
        result=rules_de.validate(self.base(
            page_location="New York, NY",
            page_description="This is a hybrid role. Employees work remotely but must report to the office two days per week. Build AWS SQL ETL data pipelines.",
        ),{})
        self.assertEqual(result["validation_class"],"rejected")
        self.assertIn("hybrid/on-site",result["validation_reject_reason"])

    def test_employer_page_without_remote_confirmation_is_manual_review(self):
        result=rules_de.validate(self.base(
            page_location="Dallas, TX",
            page_description="Build AWS SQL ETL data pipelines and data warehouse solutions from our Dallas team.",
        ),{})
        self.assertNotEqual(result["validation_class"],"rejected")
        self.assertTrue(any("not confirmed" in s for s in result["validation_review_reasons"]))

    def test_fully_remote_employer_page_is_allowed(self):
        result=rules_de.validate(self.base(
            page_location="Remote, United States",
            page_description="This is a fully remote position in the United States. Build AWS SQL ETL data pipelines and data warehouse solutions. 4 years experience.",
        ),{})
        self.assertNotEqual(result["validation_class"],"rejected")

    def test_hybrid_cloud_word_does_not_mean_hybrid_work(self):
        result=rules_de.validate(self.base(
            page_location="Remote, United States",
            page_description="This fully remote U.S. role builds hybrid cloud data platforms with AWS, SQL, ETL, Python and data warehousing. 4 years experience.",
        ),{})
        self.assertNotEqual(result["validation_class"],"rejected")

    def test_foreign_remote_job_is_rejected(self):
        result=rules_de.validate(self.base(
            page_location="Remote, Brazil",
            page_description="This is a fully remote position in Brazil building AWS SQL ETL data pipelines.",
        ),{})
        self.assertEqual(result["validation_class"],"rejected")
        self.assertIn("outside the United States",result["validation_reject_reason"])

    def test_state_restriction_without_virginia_is_rejected(self):
        result=rules_de.validate(self.base(
            page_location="Remote, United States",
            page_description="Fully remote in the United States. Candidates must reside in California, Oregon, or Washington. Build AWS SQL ETL data pipelines.",
        ),{})
        self.assertEqual(result["validation_class"],"rejected")
        self.assertIn("Virginia",result["validation_reject_reason"])


class SqlRulesTests(unittest.TestCase):
    def base(self, **kw):
        job={"title":"Senior SQL Server Developer","company":"Example","source":"indeed","location":"Remote, United States","workplace_type":"remote","description":"Develop Microsoft SQL Server and T-SQL stored procedures, views, functions, triggers, SSIS ETL packages, query tuning, indexes, execution plans and data warehouse solutions. 8 years experience.","page_verified_live":True}
        job.update(kw); return job

    def settings(self):
        return {"strong_score_threshold":80,"min_possible_alert_score":55,"verified_sql_years_review_threshold":12,"max_travel_percent":20,"require_live_verification":True}

    def test_hands_on_senior_allowed(self):
        r=rules_sql.validate(self.base(),self.settings()); self.assertNotEqual(r["validation_class"],"rejected"); self.assertTrue(r["validation_alertable"])

    def test_lead_sql_development_allowed(self):
        self.assertIsNone(rules_sql.title_rejection("Lead SQL Server Developer"))

    def test_dba_rejected(self):
        self.assertIsNotNone(rules_sql.title_rejection("SQL Server DBA"))

    def test_part_time_not_rejected(self):
        r=rules_sql.validate(self.base(employment_type="part-time"),self.settings()); self.assertNotEqual(r["validation_class"],"rejected")

    def test_oracle_primary_rejected_for_adjacent_title(self):
        r=rules_sql.validate(self.base(title="Database Developer",description="Remote database developer using Oracle PL/SQL, Informatica, PostgreSQL and Power BI for enterprise systems."),self.settings()); self.assertEqual(r["validation_class"],"rejected")

    def test_ability_to_obtain_allowed(self):
        r=rules_sql.validate(self.base(description=self.base()["description"]+" Must be eligible to obtain Public Trust."),self.settings()); self.assertNotEqual(r["validation_reject_reason"],"Requires a current/active clearance or Public Trust")

    def test_current_public_trust_rejected(self):
        r=rules_sql.validate(self.base(description=self.base()["description"]+" Current Public Trust required."),self.settings()); self.assertEqual(r["validation_class"],"rejected")

    def test_live_verification_required_for_alert(self):
        r=rules_sql.validate(self.base(page_verified_live=None),self.settings()); self.assertFalse(r["validation_alertable"])

    def test_in_office_overrides_remote_board_metadata(self):
        r=rules_sql.validate(self.base(description=self.base()["description"]+" This is an in-office position in Dallas, TX."),self.settings())
        self.assertEqual(r["validation_class"],"rejected")

    def test_employer_page_hybrid_overrides_remote_board_badge(self):
        r=rules_sql.validate(self.base(
            page_location="Charlotte, NC",
            page_description=self.base()["description"]+" This role is hybrid and requires three days per week in the office.",
        ),self.settings())
        self.assertEqual(r["validation_class"],"rejected")

    def test_employer_page_missing_remote_is_manual_review(self):
        r=rules_sql.validate(self.base(
            page_location="Dallas, TX",
            page_description=self.base()["description"]+" Work with the Dallas engineering team.",
        ),self.settings())
        self.assertNotEqual(r["validation_class"],"rejected")
        self.assertTrue(any("not confirmed" in s for s in r["validation_review_reasons"]))

    def test_fully_remote_sql_page_is_alertable(self):
        r=rules_sql.validate(self.base(
            page_location="Remote, United States",
            page_description=self.base()["description"]+" This is a fully remote position in the United States.",
        ),self.settings())
        self.assertNotEqual(r["validation_class"],"rejected")
        self.assertTrue(r["validation_alertable"])


if __name__=="__main__": unittest.main()
