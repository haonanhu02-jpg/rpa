"""Ensure declarative deployment never rolls back the OpenAPI ORM schema."""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def block(source, label):
    start = source.index(label) + len(label)
    depth = 1
    for offset in range(start, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if not depth:
                return source[start:offset]
    raise AssertionError("Unclosed HCL block: " + label)


class ExecutionSchemaContractTest(unittest.TestCase):
    def setUp(self):
        self.hcl = (ROOT / "docker/volumes/atlas/schema.hcl").read_text(encoding="utf-8")

    def test_atlas_preserves_every_workflow_and_execution_model_column(self):
        model = ast.parse((ROOT / "backend/openapi-service/app/models/workflow.py").read_text(encoding="utf-8"))
        for node in model.body:
            if not isinstance(node, ast.ClassDef) or node.name not in ("Workflow", "Execution"):
                continue
            table = "openai_workflows" if node.name == "Workflow" else "openai_executions"
            definition = block(self.hcl, f'table "{table}" {{')
            deployed = set(re.findall(r'column "([a-z_]+)" \{', definition))
            expected = {
                assignment.targets[0].id
                for assignment in node.body
                if isinstance(assignment, ast.Assign)
                and isinstance(assignment.value, ast.Call)
                and isinstance(assignment.value.func, ast.Name)
                and assignment.value.func.id == "Column"
            }
            self.assertTrue(expected)
            self.assertFalse(expected - deployed, f"Atlas would remove {table} columns: {expected - deployed}")

    def test_atlas_keeps_json_capacity_and_unicode(self):
        for table, names in (
            ("openai_workflows", ("parameters",)),
            ("openai_executions", ("parameters", "result", "data_contract")),
        ):
            definition = block(self.hcl, f'table "{table}" {{')
            for name in names:
                column = block(definition, f'column "{name}" {{')
                self.assertRegex(column, r"type\s*=\s*mediumtext")
                self.assertRegex(column, r'charset\s*=\s*"utf8mb4"')

    def test_execution_receipts_remain_unique_and_survive_workflow_deletion(self):
        definition = block(self.hcl, 'table "openai_executions" {')
        unique = block(definition, 'index "uq_execution_user_key" {')
        self.assertIn("unique = true", unique)
        self.assertIn("[column.user_id, column.idempotency_key_hash]", unique)
        dispatch = block(definition, 'index "ix_openai_executions_dispatch_state" {')
        self.assertIn("column.dispatch_state", dispatch)
        self.assertNotIn("foreign_key", definition)


if __name__ == "__main__":
    unittest.main()
