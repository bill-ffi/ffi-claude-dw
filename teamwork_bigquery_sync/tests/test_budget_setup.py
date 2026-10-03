"""Client budgets live on one project per client in the Monthly Close
category, as a recurring dollar target (agreed 2026-10-03). On that day only
16 of the 48 "### Monthly Books (2026)" projects had one, three active
projects carried budgets elsewhere, and 157 of 200 budgets were in hours.
This report keeps that setup visible every run."""

import inspect
import logging

import sync
import views


def project(name, category="Monthly Close", budget_type=None, capacity=None,
            client="Acme", archived=False):
    return {"name": name, "category_name": category, "budget_type": budget_type,
            "budget_capacity": capacity, "client_name": client,
            "archived_at": "2026-10-01" if archived else None}


class TestBudgetSetupReport:
    def test_lists_budget_projects_without_a_dollar_target(self):
        r = sync.budget_setup_report([
            project("ACC Monthly Books (2026)"),
            project("SOC Monthly Books (2026)", budget_type="TIME", client="SOC"),
            project("CBIC Monthly Books (2026)", budget_type="FINANCIAL", capacity=5000, client="CBIC"),
        ])
        assert r["budget_projects_with_target"] == 1
        # An hour budget is not a target either.
        assert r["budget_projects_without_target"] == [
            "ACC Monthly Books (2026)", "SOC Monthly Books (2026)"]

    def test_archived_budget_projects_are_not_setup_work(self):
        r = sync.budget_setup_report([project("TIA Monthly Books (2026)", archived=True)])
        assert r["budget_projects_without_target"] == []

    def test_lists_budgets_outside_the_category(self):
        r = sync.budget_setup_report([
            project("UMS Onboarding", category="Onboarding", budget_type="FINANCIAL", capacity=900),
            project("SOC - Non-Monthly", category="Tax/Compliance", budget_type="TIME"),
            project("No budget", category="Advisory"),
        ])
        assert r["budgets_outside_budget_category"] == [
            "SOC - Non-Monthly (TIME)", "UMS Onboarding (FINANCIAL)"]

    def test_two_budget_projects_for_one_client_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            r = sync.budget_setup_report([
                project("A Monthly Books (2026)", budget_type="FINANCIAL", capacity=1),
                project("A Monthly Close (2026)", budget_type="FINANCIAL", capacity=1),
            ])
        assert r["clients_with_several_budget_projects"] == {
            "Acme": ["A Monthly Books (2026)", "A Monthly Close (2026)"]}
        assert "counts more than once" in caplog.text

    def test_one_per_client_is_quiet(self, caplog):
        with caplog.at_level(logging.WARNING):
            sync.budget_setup_report([
                project("A Monthly Books (2026)", budget_type="FINANCIAL", capacity=1, client="A"),
                project("B Monthly Books (2026)", budget_type="FINANCIAL", capacity=1, client="B"),
            ])
        assert caplog.records == []

    def test_uses_the_same_rule_as_the_view(self):
        source = inspect.getsource(sync.budget_setup_report)
        assert "views.BUDGET_PROJECT_CATEGORY, views.BUDGET_TYPE" in source
        assert '"budget_setup": budget_setup_report(rows),' in inspect.getsource(sync.sync_projects)
        assert views.BUDGET_PROJECT_CATEGORY == "Monthly Close"
