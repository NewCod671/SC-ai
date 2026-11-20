"""CPU-only checks for training-data integrity; no model download or GPU calls."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('compliance_tune', HERE/'compliance_tune.py')
tune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tune)


class PrepareChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.rows = tune.read_jsonl(HERE/'review-cases.example.jsonl')
        # Approval here is strictly a test fixture, never a training-data export.
        for row in self.rows:
            row['approved'] = True; row['reviewed_by'] = 'unit-test-only'

    def prepare(self, rows):
        source = self.folder/'cases.jsonl'
        tune.write_jsonl(source, rows)
        args = argparse.Namespace(input=str(source), output=str(self.folder/'out'),
            seed=42, eval_fraction=.2, allow_demo=True)
        with redirect_stdout(io.StringIO()): tune.prepare(args)
        return tune.read_jsonl(self.folder/'out/train.jsonl'), tune.read_jsonl(self.folder/'out/eval.jsonl')

    def test_group_variants_never_cross_the_split(self):
        train, evaluation = self.prepare(self.rows)
        self.assertEqual(len(train)+len(evaluation), len(self.rows))
        self.assertTrue(train and evaluation)
        self.assertFalse({r['group_id'] for r in train} & {r['group_id'] for r in evaluation})
        self.assertEqual({json.loads(r['messages'][2]['content'])[0]['audit']['category'] for r in evaluation},
            {'comply','ofi','nc_minor','nc_major','needs_review'})

    def test_unapproved_and_string_approval_rejected(self):
        for value in (False, 'false', 'true'):
            rows = deepcopy(self.rows); rows[0]['approved'] = value
            with self.assertRaisesRegex(ValueError, 'approved=true'): self.prepare(rows)

    def test_same_input_in_another_group_is_rejected(self):
        row = deepcopy(self.rows[0]); row['case_id'] += '-copy'; row['group_id'] = 'different-company'
        with self.assertRaisesRegex(ValueError, 'Duplicate requirement/answer'): self.prepare(self.rows+[row])

    def test_inconsistent_nc_major_label_is_rejected(self):
        row = deepcopy(next(r for r in self.rows if r['finding']['audit']['category']=='nc_major'))
        row['finding']['status'] = 'met'
        with self.assertRaisesRegex(ValueError, 'NC needs'): tune.check_finding(row['finding'], row['control_id'])

    def test_malformed_audit_is_a_validation_error(self):
        row = deepcopy(self.rows[0]); row['finding']['audit'] = []
        with self.assertRaises(ValueError): tune.check_finding(row['finding'], row['control_id'])


if __name__ == '__main__': unittest.main()
