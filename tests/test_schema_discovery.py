import unittest

from app.risk.schema_discovery import suggest_project_mapping


class SchemaDiscoveryTests(unittest.TestCase):
    def test_suggest_project_mapping_prefers_project_like_table_and_fields(self):
        schema = [
            {"table_name": "app_user", "column_name": "user_name", "column_comment": "用户名称"},
            {"table_name": "project_info", "column_name": "project_no", "column_comment": "项目编号"},
            {"table_name": "project_info", "column_name": "project_name", "column_comment": "项目名称"},
            {"table_name": "project_info", "column_name": "project_status", "column_comment": "项目状态"},
            {"table_name": "project_info", "column_name": "audit_status", "column_comment": "审核状态"},
            {"table_name": "project_info", "column_name": "budget_amount", "column_comment": "项目预算"},
            {"table_name": "project_info", "column_name": "estimated_profit", "column_comment": "预估收益"},
            {"table_name": "contract", "column_name": "project_id", "column_comment": "项目ID"},
        ]

        mapping = suggest_project_mapping(schema)

        self.assertEqual(mapping.primary_table, "project_info")
        self.assertEqual(mapping.fields["project_id"], "project_no")
        self.assertEqual(mapping.fields["name"], "project_name")
        self.assertEqual(mapping.fields["approval_status"], "audit_status")
        self.assertIn("contract", mapping.related_tables["contracts"])


if __name__ == "__main__":
    unittest.main()
