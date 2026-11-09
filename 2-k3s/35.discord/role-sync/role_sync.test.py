#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from role_sync import NoLinks, active_member_pks, plan

ROLE_IDS = {"Servarr users": "900", "Screener users": "901"}


class PlanTests(unittest.TestCase):
    def test_adds_role_for_linked_group_member(self):
        result = plan({"Servarr users": {"1"}}, {"1": "100"}, {"100": set()}, ROLE_IDS)
        self.assertEqual(result, ([("100", "900")], [], []))

    def test_removes_role_from_non_member(self):
        result = plan({"Servarr users": set()}, {"1": "100"}, {"100": {"900"}}, ROLE_IDS)
        self.assertEqual(result, ([], [("100", "900")], []))

    def test_removes_role_from_unlinked_member(self):
        result = plan({"Servarr users": {"1"}}, {"9": "999"}, {"100": {"900"}}, ROLE_IDS)
        self.assertEqual(result, ([], [("100", "900")], ["100"]))

    def test_ignores_unmapped_roles(self):
        result = plan({"Servarr users": set()}, {"1": "100"}, {"100": {"555"}}, ROLE_IDS)
        self.assertEqual(result, ([], [], []))

    def test_skips_group_member_not_in_guild(self):
        result = plan({"Servarr users": {"1"}}, {"1": "100"}, {}, ROLE_IDS)
        self.assertEqual(result, ([], [], []))

    def test_unlinked_lists_members_without_link(self):
        result = plan({"Servarr users": set()}, {"9": "999"}, {"100": set()}, ROLE_IDS)
        self.assertEqual(result, ([], [], ["100"]))

    def test_refuses_to_run_without_any_links(self):
        with self.assertRaises(NoLinks):
            plan({"Servarr users": {"1"}}, {}, {"100": {"900"}}, ROLE_IDS)

    def test_no_changes_when_in_sync(self):
        result = plan(
            {"Servarr users": {"1"}, "Screener users": {"2"}},
            {"1": "100", "2": "200"},
            {"100": {"900"}, "200": {"901", "555"}},
            ROLE_IDS,
        )
        self.assertEqual(result, ([], [], []))


class GroupTests(unittest.TestCase):
    def test_active_member_pks_skips_deactivated_users(self):
        group = {"users_obj": [{"pk": 1, "is_active": True}, {"pk": 2, "is_active": False}]}
        self.assertEqual(active_member_pks(group), {"1"})


if __name__ == "__main__":
    unittest.main()
