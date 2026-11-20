"""Selected-control demo compatibility and preservation of a user's assessment work."""
from copy import deepcopy
from io import BytesIO
import hashlib
import importlib.util
from pathlib import Path
import unittest
from zipfile import ZipFile

from app.reporting import excel

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('iso27001_demo', ROOT / 'scripts' / 'create_iso27001_demo.py')
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class FakeApi:
    def __init__(self, data):
        self.data = data
        self.standards, self.assessments, self.skills = [], [], []
        self.evidence, self.policies = {}, {}
        self.writes = []

    def request(self, path, method='GET', payload=None):
        if method != 'GET':
            self.writes.append((path, method))
        if path == '/standards':
            return deepcopy(self.standards)
        if path == '/skills':
            if method == 'POST':
                self.skills.append({**payload, 'id': 'skill-1'})
                return deepcopy(self.skills[-1])
            return deepcopy(self.skills)
        if path == '/assessments':
            if method == 'POST':
                a = {**payload, 'id': f'a-{len(self.assessments)+1}', 'current_version': 1,
                     'answers': {c['id']: {'text': '', 'status': 'unanswered'} for c in self.data['controls']}}
                self.assessments.append(a)
                return deepcopy(a)
            return deepcopy(self.assessments)
        assessment = next(a for a in self.assessments if path.split('/')[2] == a['id'])
        base = '/assessments/' + assessment['id']
        if path == base:
            return deepcopy(assessment)
        if path.endswith('/answers'):
            assert method == 'PUT'
            assert payload['expected_version'] == assessment['current_version']
            assessment['answers'] = deepcopy(payload['answers'])
            assessment['current_version'] += 1
            return deepcopy(assessment)
        if path.endswith('/evidence'):
            return deepcopy(self.evidence.get(base, []))
        if path.endswith('/policies'):
            if method == 'POST':
                self.policies.setdefault(base, []).append(deepcopy(payload))
                return deepcopy(payload)
            return deepcopy(self.policies.get(base, []))
        if path.endswith('/runs'):
            return {'id': 'run-1'}
        raise AssertionError(f'Unexpected request: {method} {path}')

    def upload(self, path, fields, filename, content, mime):
        if path == '/standards/import':
            self.standards.append({**fields, 'id': 's-1', 'controls': deepcopy(self.data['controls'])})
            return deepcopy(self.standards[-1])
        assert path.endswith('/evidence')
        base = path.removesuffix('/evidence')
        record = {'control_id': fields['control_id'], 'sha256': hashlib.sha256(content).hexdigest()}
        self.evidence.setdefault(base, []).append(record)
        return record


class ISO27001DemoTests(unittest.TestCase):
    def setUp(self):
        self.data = demo.load_dataset()
        self.workbook = (ROOT / self.data['workbook']).read_bytes()

    def test_workbook_import_and_assessment_roundtrip_preserve_supporting_sheets(self):
        controls, mapping = excel.parse_standard(self.workbook, {'sheet': 'Checklist', 'columns': {'question': 'Question'}})
        self.assertEqual([c['id'] for c in controls], [c['id'] for c in self.data['controls']])
        answers = {c['id']: {'status': c['status'], 'text': c['answer']} for c in self.data['controls']}
        manifest = {'assessment_id': 'iso-demo', 'standard_id': 'selected-2022', 'version': 1}
        exported = excel.export(self.workbook, mapping, controls, answers, manifest)
        self.assertEqual(excel.import_answers(exported, mapping, controls, manifest), answers)
        with ZipFile(BytesIO(self.workbook)) as before, ZipFile(BytesIO(exported)) as after:
            for name in ('xl/worksheets/sheet2.xml', 'xl/worksheets/sheet3.xml'):
                self.assertEqual(before.read(name), after.read(name))

    def test_guided_seed_leaves_answers_blank_and_reruns_reuse_records(self):
        api = FakeApi(self.data)
        first = demo.seed(api, self.data, self.workbook)
        second = demo.seed(api, self.data, self.workbook)
        self.assertEqual(first['evidence_added'], 12)
        self.assertEqual(second['evidence_added'], 0)
        self.assertEqual(len(api.assessments), 1)
        self.assertEqual(len(api.skills), 1)
        self.assertEqual(len(api.policies['/assessments/a-1']), 1)
        self.assertTrue(all(a['status'] == 'unanswered' for a in first['assessment']['answers'].values()))
        self.assertFalse(any(p.endswith(('/answers', '/runs')) for p, _ in api.writes))

    def test_worked_example_uses_put_and_preserves_later_user_edits(self):
        api = FakeApi(self.data)
        demo.seed(api, self.data, self.workbook)
        first = demo.seed(api, self.data, self.workbook, answered=True)
        self.assertEqual(first['assessment']['current_version'], 2)
        self.assertIn(('/assessments/a-2/answers', 'PUT'), api.writes)
        api.assessments[1]['answers']['A.8.13']['text'] = 'User recorded a new restoration exercise.'
        api.assessments[1]['current_version'] = 3
        repeated = demo.seed(api, self.data, self.workbook, answered=True, run=True)
        self.assertEqual(repeated['assessment']['answers']['A.8.13']['text'], 'User recorded a new restoration exercise.')
        self.assertEqual(repeated['assessment']['current_version'], 3)
        self.assertEqual(len([p for p, _ in api.writes if p.endswith('/answers')]), 1)
        self.assertEqual(repeated['run']['id'], 'run-1')


if __name__ == '__main__':
    unittest.main()
