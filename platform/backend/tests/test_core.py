import unittest
from io import BytesIO
from zipfile import ZipFile
from app import ai
from app.reporting import excel
from .fixtures import workbook, patch_cell

class ExcelTests(unittest.TestCase):
    def setUp(self):
        self.template = workbook()
        self.controls, self.mapping = excel.parse_standard(self.template, {})
        self.manifest = {'assessment_id': 'a1', 'standard_id': 's1', 'version': 1}
        self.answers = {c['id']: {'status': 'yes', 'text': '=HYPERLINK("evil")'} for c in self.controls}

    def exported(self):
        return excel.export(self.template, self.mapping, self.controls, self.answers, self.manifest)

    def test_round_trip_preserves_unknown_parts_and_layout(self):
        data = self.exported()
        self.assertEqual(excel.import_answers(data, self.mapping, self.controls, self.manifest), self.answers)
        with ZipFile(BytesIO(self.template)) as before, ZipFile(BytesIO(data)) as after:
            for name in before.namelist():
                if name not in {'xl/worksheets/sheet1.xml','xl/workbook.xml','xl/_rels/workbook.xml.rels','[Content_Types].xml'}:
                    self.assertEqual(before.read(name), after.read(name))
        wb = excel.read(data)
        self.assertEqual(wb['Checklist'].freeze_panes, 'A2')
        self.assertEqual(str(wb['Checklist'].merged_cells), 'I1:J1')
        self.assertEqual(wb['Reference']['A1'].value, '=SUM(1,2)')
        self.assertEqual(wb[excel.META].sheet_state, 'veryHidden')
        self.assertEqual(wb['Checklist']['D2'].data_type, 's')
        self.assertEqual(wb['Checklist'].row_dimensions[2].height, 25)
        self.assertEqual(wb['Checklist'].column_dimensions['A'].width, 24)

    def test_stale_and_foreign_workbooks_rejected(self):
        for meta in [dict(self.manifest, version=2), dict(self.manifest, assessment_id='other')]:
            with self.assertRaisesRegex(ValueError, 'stale revision'):
                excel.import_answers(self.exported(), self.mapping, self.controls, meta)

    def test_requirement_tampering_rejected(self):
        with self.assertRaisesRegex(ValueError, 'cannot be edited'):
            excel.import_answers(patch_cell(self.exported(),2,2,'Different requirement'), self.mapping, self.controls, self.manifest)

    def test_invalid_status_and_missing_metadata(self):
        with self.assertRaisesRegex(ValueError, 'Invalid answer'):
            excel.import_answers(patch_cell(self.exported(),2,5,'approved'), self.mapping, self.controls, self.manifest)
        with self.assertRaisesRegex(ValueError, 'Missing assessment metadata'):
            excel.import_answers(self.template, self.mapping, self.controls, self.manifest)

    def test_duplicate_ids_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            excel.parse_standard(workbook([('A','first','IT'),('A','second','HR')]), {})

    def test_invalid_archive_rejected(self):
        with self.assertRaisesRegex(ValueError, 'valid .xlsx'):
            excel.parse_standard(b'not excel', {})

    def test_reordered_rows_map_by_control_id(self):
        data = self.exported()
        for row, control in ((2, self.controls[1]), (3, self.controls[0])):
            for col, key in ((1,'id'),(2,'requirement'),(3,'department')):
                data = patch_cell(data,row,col,control[key])
            data = patch_cell(data,row,4,control['id']+' answer')
        answers = excel.import_answers(data,self.mapping,self.controls,self.manifest)
        self.assertEqual(answers['SC-A']['text'],'SC-A answer')
        self.assertEqual(answers['SC-B']['text'],'SC-B answer')

    def test_namespace_declarations_preserved(self):
        with ZipFile(BytesIO(self.template)) as z:
            parts = {n:z.read(n) for n in z.namelist()}
        path = 'xl/worksheets/sheet1.xml'
        parts[path] = parts[path].replace(b'<worksheet ', b'<worksheet xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac" mc:Ignorable="x14ac" ')
        out = BytesIO()
        with ZipFile(out,'w') as z:
            for n,v in parts.items(): z.writestr(n,v)
        data = excel.export(out.getvalue(),self.mapping,self.controls,self.answers,self.manifest)
        with ZipFile(BytesIO(data)) as z:
            self.assertIn(b'xmlns:x14ac=',z.read(path))
            self.assertIn(b'mc:Ignorable="x14ac"',z.read(path))

    def test_formula_output_rejected(self):
        from lxml import etree as ET
        with ZipFile(BytesIO(self.template)) as z:
            parts = {n:z.read(n) for n in z.namelist()}
        root = ET.fromstring(parts['xl/worksheets/sheet1.xml'])
        cell = root.find(f'.//{{{excel.NS}}}c[@r="D2"]')
        cell.clear(); cell.attrib['r'] = 'D2'; ET.SubElement(cell, f'{{{excel.NS}}}f').text = '1+1'
        parts['xl/worksheets/sheet1.xml'] = ET.tostring(root)
        out = BytesIO()
        with ZipFile(out,'w') as z:
            for n,v in parts.items(): z.writestr(n,v)
        with self.assertRaisesRegex(ValueError, 'cannot contain formulas'):
            excel.parse_standard(out.getvalue(), {})

class AITests(unittest.TestCase):
    def setUp(self):
        self.controls, _ = excel.parse_standard(workbook(), {})
        self.provider = ai.OfflineProvider()

    def test_questions_use_context_and_stable_ids(self):
        q = self.provider.questions(self.controls, {'company':'Example Co', 'business_type':'Retail', 'departments':['IT','HR'], 'it_systems':['ERP'], 'data_types':['PII']})
        self.assertEqual(q[0]['control_id'], 'SC-A')
        self.assertTrue(q[0]['text'].endswith('?'))
        self.assertEqual(q[0]['departments'], ['IT'])
        for inventory in ('Consider systems:', 'Reference existing policies:', 'ERP', 'PII'):
            self.assertNotIn(inventory, q[0]['text'])

    def test_mapping_has_reviewable_reasons(self):
        result = self.provider.mapping('Review access permissions', self.controls)
        self.assertEqual(result[0]['control_id'], 'SC-A')
        self.assertIn('access',result[0]['reason'])

    def test_advisory_results_never_claim_compliance(self):
        payload = {'controls': self.controls, 'answers': {'SC-A':{'status':'yes'}, 'SC-B':{'status':'no'}},
                   'evidence':[{'id':'e1','control_id':'SC-A','text':'Access review completed'}]}
        findings = self.provider.assess(payload)
        result = ai.validate_findings(findings,self.controls,payload['evidence'])
        self.assertEqual(result[0]['status'],'needs_review')
        self.assertEqual(result[1]['status'],'gap')
        result[1]['evidence_ids'] = ['e1']
        with self.assertRaisesRegex(ValueError, 'incorrectly linked'):
            ai.validate_findings(result,self.controls,payload['evidence'])

    def test_unknown_control_output_rejected(self):
        result = [{'control_id':'unknown','status':'gap','rationale':'x','recommendation':'x','evidence_ids':[]}]
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            ai.validate_findings(result, self.controls, [])

if __name__ == '__main__':
    unittest.main()
