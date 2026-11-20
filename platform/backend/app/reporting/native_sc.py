"""Export the original four-sheet SC workbook by patching its OOXML cells.

The normalized standard/IDs and existing assessment revisions remain unchanged.
Only an allowlisted bundled template is used; model output never chooses addresses.
"""
from copy import deepcopy
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import json
import hashlib
import re
from textwrap import wrap
from lxml import etree as ET
from openpyxl.utils import coordinate_to_tuple, get_column_letter, range_boundaries
from . import excel
from ..schemas import Answer

PROFILE = 'sc-original-v1'
TEMPLATE = Path(__file__).resolve().parents[2] / 'templates' / 'sc-audit-original.xlsx'
CNS = 'http://schemas.openxmlformats.org/drawingml/2006/chart'
SHEETS = ('Dashboard', 'Scope', 'Clause 4-10', 'Annex 5-8')
GRADES = ('comply', 'ofi', 'nc_minor', 'nc_major')
MARKS = ('C', 'OFI', 'N', 'M')
RESULT_COLORS = ('D9EAD3','FFF2CC','FCE4D6','F4CCCC')


@lru_cache(maxsize=1)
def source():
    data = TEMPLATE.read_bytes()
    if hashlib.sha256(data).hexdigest() != "90a2a11d02eb992f0d5e169f8a4ca5ee8f1b91cbb177608f8db6e085f1002664":
        raise ValueError("Bundled original SC template integrity check failed")
    wb = excel.read(data)
    controls = {}
    try:
        if tuple(wb.sheetnames) != SHEETS:
            raise ValueError('Unexpected original SC worksheets')
        for sheet, req, evidence, marks, answer, result, owner in [
                ('Clause 4-10', 'U', 'Y', ('Z','AA','AB','AC'), 'AE','AD','AF'),
                ('Annex 5-8', 'Q', 'V', ('W','X','Y','Z'), 'AB','AA','AC')]:
            ws = wb[sheet]
            for row in range(19, ws.max_row+1):
                heading = str(ws[f'{req}{row}'].value or '').strip()
                match = re.match(r'^(\d+\.\d+(?:\.\d+)?)\b', heading) or re.search(r'\((\d+\.\d+(?:\.\d+)?)\s', heading)
                if not match:
                    continue
                child = re.search(r'\((\d+\.\d+(?:\.\d+)?)\s', heading)
                cid = ('A.'+match[1]) if sheet.startswith('Annex') else (child[1] if child else match[1])
                question = str(ws[f'B{row}'].value or '').strip()
                if not question or cid in controls:
                    raise ValueError('Ambiguous original SC control')
                controls[cid] = {'sheet':sheet,'row':row,'heading':heading,
                    'requirement':heading+'\n\n'+question, 'question':'B','req':req,
                    'evidence':evidence,'marks':marks,'answer':answer,'result':result,'owner':owner}
        if len(controls)!=118 or sum(cid.startswith('A.') for cid in controls)!=93:
            raise ValueError('Original SC coverage differs')
    finally:
        wb.close()
    return data, controls


def validate_controls(controls):
    _, refs = source()
    if len(controls)!=len(refs) or {c['id'] for c in controls}!=set(refs):
        raise ValueError('Original SC export requires the exact 118 control IDs')
    if any(c['requirement']!=refs[c['id']]['requirement'] for c in controls):
        raise ValueError('Original SC export requirements differ from the selected standard')
    return refs


class Editor:
    def __init__(self, data):
        with ZipFile(BytesIO(data)) as z:
            self.parts = {n:z.read(n) for n in z.namelist()}
        self.roots = {s:excel.xml(self.parts[excel.sheet_path(self.parts,s)]) for s in SHEETS}
        self.values = {}
        wb = excel.read(data)
        try:
            for ws in wb:
                for row in ws:
                    for cell in row:
                        if cell.value is not None and cell.data_type!='f':
                            self.values[(ws.title,cell.coordinate)] = cell.value
        finally:
            wb.close()

    def put(self,sheet,ref,value,formula=None):
        root = self.roots[sheet]
        row,col = coordinate_to_tuple(ref)
        text = str(value)
        if len(text)>32767:
            text=text[:32690]+'\n[Full content in assessment history.]'
        excel.set_cell(root,row,col,text)
        cell = root.find(f'.//{{{excel.NS}}}c[@r="{ref}"]')
        if isinstance(value,(int,float)) or formula is not None:
            for child in list(cell): cell.remove(child)
            cell.attrib['t']='n' if isinstance(value,(int,float)) else 'str'
            if formula is not None: ET.SubElement(cell,f'{{{excel.NS}}}f').text=formula.lstrip('=')
            ET.SubElement(cell,f'{{{excel.NS}}}v').text=str(value)
        self.values[(sheet,ref)] = value

    def expose_control_rows(self, refs):
        for sheet, root in self.roots.items():
            controls = [ref for ref in refs.values() if ref['sheet'] == sheet]
            if not controls:
                continue
            ns = f'{{{excel.NS}}}'
            cells = {coordinate_to_tuple(f'{ref[key]}{ref["row"]}')
                     for ref in controls for key in ('answer', 'result')}
            merges = root.find(f'{ns}mergeCells')
            if merges is not None:
                for merge in list(merges):
                    left, top, right, bottom = range_boundaries(merge.get('ref'))
                    if any(top <= row <= bottom and left <= col <= right
                           and (row != top or col != left) for row, col in cells):
                        merges.remove(merge)
                if len(merges):
                    merges.set('count', str(len(merges)))
                else:
                    root.remove(merges)
            rows = {int(row.get('r')): row for row in root.iter(f'{ns}row')}
            widths = list(root.iter(f'{ns}col'))
            for ref in controls:
                row = rows[ref['row']]
                height = float(row.get('ht', '15'))
                for key in ('answer', 'result'):
                    address = f'{ref[key]}{ref["row"]}'
                    _, col = coordinate_to_tuple(address)
                    width = next((float(item.get('width', '12')) for item in widths
                                  if int(item.get('min')) <= col <= int(item.get('max'))), 12)
                    text = str(self.values.get((sheet, address), ''))
                    lines = sum(max(1, len(wrap(line, width=max(8, int(width * 0.9)))))
                                for line in text.splitlines())
                    height = max(height, min(409, lines * 15 + 8))
                row.set('ht', str(height))
                row.set('customHeight', '1')

    def result_colors(self,refs,explicit_white=False,static_only=False):
        styles=excel.xml(self.parts['xl/styles.xml'])
        xfs=styles.find(f'{{{excel.NS}}}cellXfs')
        fills=styles.find(f'{{{excel.NS}}}fills')
        grade_fill_ids=[]
        for color in RESULT_COLORS:
            grade_fill_ids.append(len(fills))
            fill=ET.SubElement(fills,f'{{{excel.NS}}}fill')
            pattern=ET.SubElement(fill,f'{{{excel.NS}}}patternFill',patternType='solid')
            ET.SubElement(pattern,f'{{{excel.NS}}}fgColor',rgb='FF'+color)
            ET.SubElement(pattern,f'{{{excel.NS}}}bgColor',indexed='64')
        white_fill_id=0
        if explicit_white:
            white_fill_id=len(fills)
            white_fill=ET.SubElement(fills,f'{{{excel.NS}}}fill')
            white_pattern=ET.SubElement(white_fill,f'{{{excel.NS}}}patternFill',patternType='solid')
            ET.SubElement(white_pattern,f'{{{excel.NS}}}fgColor',rgb='FFFFFFFF')
            ET.SubElement(white_pattern,f'{{{excel.NS}}}bgColor',indexed='64')
        dxfs=styles.find(f'{{{excel.NS}}}dxfs')
        if dxfs is None:
            dxfs=ET.Element(f'{{{excel.NS}}}dxfs',count='0')
            # OOXML ordering: dxfs precedes tableStyles/colors/extLst.
            following=next((node for node in styles if ET.QName(node).localname in {'tableStyles','colors','extLst'}),None)
            if following is None:styles.append(dxfs)
            else:styles.insert(styles.index(following),dxfs)
        dxf_ids=[]
        for color in RESULT_COLORS:
            dxf_ids.append(len(dxfs))
            dxf=ET.SubElement(dxfs,f'{{{excel.NS}}}dxf')
            font=ET.SubElement(dxf,f'{{{excel.NS}}}font')
            ET.SubElement(font,f'{{{excel.NS}}}color',rgb='FF000000')
            fill=ET.SubElement(dxf,f'{{{excel.NS}}}fill')
            pattern=ET.SubElement(fill,f'{{{excel.NS}}}patternFill',patternType='solid')
            ET.SubElement(pattern,f'{{{excel.NS}}}fgColor',rgb='FF'+color)
            ET.SubElement(pattern,f'{{{excel.NS}}}bgColor',indexed='64')
        blank_dxf_id=len(dxfs)
        dxf=ET.SubElement(dxfs,f'{{{excel.NS}}}dxf')
        pattern=ET.SubElement(ET.SubElement(dxf,f'{{{excel.NS}}}fill'),f'{{{excel.NS}}}patternFill',patternType='solid')
        ET.SubElement(pattern,f'{{{excel.NS}}}fgColor',rgb='FFFFFFFF')
        ET.SubElement(pattern,f'{{{excel.NS}}}bgColor',indexed='64')
        neutral={}
        def set_fill(cell,fill_id):
            original=int(cell.get('s','0'))
            if int(xfs[original].get('fillId','0'))==fill_id:return
            key=(original,fill_id)
            if key not in neutral:
                xf=deepcopy(xfs[original]);xf.set('fillId',str(fill_id));xf.set('applyFill','1')
                neutral[key]=len(xfs);xfs.append(xf)
            cell.set('s',str(neutral[key]))
        for sheet in ('Clause 4-10','Annex 5-8'):
            root=self.roots[sheet]
            rows={r['row'] for r in refs.values() if r['sheet']==sheet}
            cols=next(r['marks'] for r in refs.values() if r['sheet']==sheet)
            extended=set(rows)
            # Also clear the old fill in children of merged result cells.
            for merge in root.findall(f'.//{{{excel.NS}}}mergeCell'):
                a,b,c,d=range_boundaries(merge.get('ref'))
                if b in rows and get_column_letter(a) in cols:
                    extended.update(range(b,d+1))
            for row in range(19,(51 if sheet.startswith('Clause') else 115)+1):
                cells=[root.find(f'.//{{{excel.NS}}}c[@r="{col}{row}"]') for col in cols]
                if row in extended:
                    for i,cell in enumerate(cells):
                        # Start neutral; use a grade color only after the actual
                        # visible result has been written into its matching cell.
                        value=self.values.get((sheet,f'{cols[i]}{row}'),'')
                        set_fill(cell,grade_fill_ids[i] if row in rows and value==MARKS[i] else white_fill_id)
                else:
                    # A sample C was accidentally painted in a section heading.
                    # Restore the same section fill used by the adjacent cells.
                    if row==66 and sheet=='Annex 5-8':
                        adjacent=int(xfs[int(cells[1].get('s','0'))].get('fillId','0'))
                        set_fill(cells[0],adjacent)
            if static_only:
                # Codex exports use direct cell styles only. Excel conditional
                # formatting can render as a dark overlay in some workbook/theme
                # combinations, obscuring the visible status markers.
                continue
            priority=max([int(rule.get('priority','0')) for rule in root.findall(f'.//{{{excel.NS}}}cfRule')]+[0])
            for col,mark,dxf_id in zip(cols,MARKS,dxf_ids):
                cf=ET.Element(f'{{{excel.NS}}}conditionalFormatting',sqref=' '.join(f'{col}{row}' for row in sorted(rows)))
                priority+=1
                rule=ET.SubElement(cf,f'{{{excel.NS}}}cfRule',type='cellIs',operator='equal',dxfId=str(dxf_id),priority=str(priority),stopIfTrue='1')
                ET.SubElement(rule,f'{{{excel.NS}}}formula').text='"'+mark+'"'
                # A cleared or changed marker must not retain the original fill
                # when someone edits the downloaded workbook in Excel.
                priority+=1
                blank=ET.SubElement(cf,f'{{{excel.NS}}}cfRule',type='cellIs',operator='notEqual',dxfId=str(blank_dxf_id),priority=str(priority),stopIfTrue='1')
                ET.SubElement(blank,f'{{{excel.NS}}}formula').text='"'+mark+'"'
                # Conditional formatting precedes validation, printing and drawings.
                following=next((node for node in root if ET.QName(node).localname in {
                    'dataValidations','hyperlinks','printOptions','pageMargins','pageSetup','headerFooter',
                    'rowBreaks','colBreaks','customProperties','cellWatches','ignoredErrors','smartTags',
                    'drawing','legacyDrawing','legacyDrawingHF','picture','oleObjects','controls','webPublishItems','tableParts','extLst'}),None)
                if following is None:root.append(cf)
                else:root.insert(root.index(following),cf)
        xfs.set('count',str(len(xfs)));dxfs.set('count',str(len(dxfs)));fills.set('count',str(len(fills)))
        self.parts['xl/styles.xml']=ET.tostring(styles,encoding='utf-8',xml_declaration=True)

    def finish(self,manifest):
        for sheet,root in self.roots.items():
            self.parts[excel.sheet_path(self.parts,sheet)]=ET.tostring(root,encoding='utf-8',xml_declaration=True)
        # Refresh cached chart values as well as cell/formula values. Keep chart
        # definitions, anchors, drawings and images untouched.
        for name in list(self.parts):
            if not re.fullmatch(r'xl/charts/chart\d+\.xml',name): continue
            root=excel.xml(self.parts[name])
            for item in root.findall(f'.//{{{CNS}}}numRef'):
                f=item.find(f'{{{CNS}}}f')
                if f is None or not f.text: continue
                match=re.fullmatch(r"'?([^']+?)'?!\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)",f.text)
                if not match: raise ValueError('Unsupported SC chart reference')
                sheet,a,r1,b,r2=match.groups()
                x1,y1,x2,y2=range_boundaries(f'{a}{r1}:{b}{r2}')
                vals=[self.values.get((sheet,f'{get_column_letter(c)}{r}'),0) for r in range(y1,y2+1) for c in range(x1,x2+1)]
                if any(not isinstance(v,(int,float)) for v in vals): raise ValueError('SC chart data must be numeric')
                cache=item.find(f'{{{CNS}}}numCache')
                if cache is None: cache=ET.SubElement(item,f'{{{CNS}}}numCache')
                for child in list(cache): cache.remove(child)
                ET.SubElement(cache,f'{{{CNS}}}formatCode').text='General'
                ET.SubElement(cache,f'{{{CNS}}}ptCount',val=str(len(vals)))
                for i,v in enumerate(vals):
                    ET.SubElement(ET.SubElement(cache,f'{{{CNS}}}pt',idx=str(i)),f'{{{CNS}}}v').text=str(v)
            for item in root.findall(f'.//{{{CNS}}}strRef'):
                formula=item.find(f'{{{CNS}}}f')
                if formula is None or not formula.text:continue
                match=re.fullmatch(r"'?([^']+?)'?!\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)",formula.text)
                if not match:continue
                sheet,a,r1,b,r2=match.groups()
                x1,y1,x2,y2=range_boundaries(f'{a}{r1}:{b}{r2}')
                vals=[self.values.get((sheet,f'{get_column_letter(c)}{r}'),'') for r in range(y1,y2+1) for c in range(x1,x2+1)]
                cache=item.find(f'{{{CNS}}}strCache')
                if cache is None:cache=ET.SubElement(item,f'{{{CNS}}}strCache')
                for child in list(cache):cache.remove(child)
                ET.SubElement(cache,f'{{{CNS}}}ptCount',val=str(len(vals)))
                for i,v in enumerate(vals):ET.SubElement(ET.SubElement(cache,f'{{{CNS}}}pt',idx=str(i)),f'{{{CNS}}}v').text=str(v)
            self.parts[name]=ET.tostring(root,encoding='utf-8',xml_declaration=True)
        _remove_sample_logo(self.parts)
        _prune_shared_strings(self.parts)
        _metadata(self.parts,manifest)
        out=BytesIO()
        with ZipFile(out,'w',ZIP_DEFLATED) as z:
            for name,data in self.parts.items():z.writestr(name,data)
        return out.getvalue()


def _remove_sample_logo(parts):
    # The only original image is the previous customer's company logo on Scope.
    # Keep the surrounding layout and all chart drawings.
    name='xl/drawings/drawing2.xml'
    root=excel.xml(parts[name])
    for anchor in list(root):
        if any(ET.QName(node).localname=='pic' for node in anchor.iter()):root.remove(anchor)
    parts[name]=ET.tostring(root,encoding='utf-8',xml_declaration=True)
    name='xl/drawings/_rels/drawing2.xml.rels'
    root=excel.xml(parts[name])
    for rel in list(root):
        if rel.get('Type','').endswith('/image'):root.remove(rel)
    parts[name]=ET.tostring(root,encoding='utf-8',xml_declaration=True)
    parts.pop('xl/media/image1.png',None)


def _prune_shared_strings(parts):
    # Remove unused historical sample strings, remapping remaining references.
    path='xl/sharedStrings.xml'
    if path not in parts:return
    root=excel.xml(parts[path]);items=list(root);used=set();sheets={}
    for name in parts:
        if re.fullmatch(r'xl/worksheets/[^/]+\.xml',name):
            sheet=excel.xml(parts[name]);sheets[name]=sheet
            for c in sheet.findall(f'.//{{{excel.NS}}}c[@t="s"]'):
                used.add(int(c.find(f'{{{excel.NS}}}v').text))
    indices={old:new for new,old in enumerate(sorted(used))}
    count=0
    for name,sheet in sheets.items():
        for c in sheet.findall(f'.//{{{excel.NS}}}c[@t="s"]'):
            v=c.find(f'{{{excel.NS}}}v');v.text=str(indices[int(v.text)]);count+=1
        parts[name]=ET.tostring(sheet,encoding='utf-8',xml_declaration=True)
    for child in list(root):root.remove(child)
    for old in sorted(used):root.append(items[old])
    root.set('count',str(count));root.set('uniqueCount',str(len(used)))
    parts[path]=ET.tostring(root,encoding='utf-8',xml_declaration=True)


def _metadata(parts,manifest):
    wb=excel.xml(parts['xl/workbook.xml']);sheets=wb.find(f'{{{excel.NS}}}sheets')
    rels=excel.xml(parts['xl/_rels/workbook.xml.rels']);types=excel.xml(parts['[Content_Types].xml'])
    rid='rSCMeta';path='xl/worksheets/sc_meta.xml'
    if any(s.get('name')==excel.META for s in sheets):raise ValueError('Original SC template already has assessment metadata')
    ET.SubElement(sheets,f'{{{excel.NS}}}sheet',name=excel.META,sheetId=str(max(int(s.get('sheetId')) for s in sheets)+1),state='veryHidden',attrib={f'{{{excel.DOCREL}}}id':rid})
    ET.SubElement(rels,f'{{{excel.REL}}}Relationship',Id=rid,Type=excel.DOCREL+'/worksheet',Target='worksheets/sc_meta.xml')
    ET.SubElement(types,f'{{{excel.CT}}}Override',PartName='/'+path,ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
    meta=ET.Element(f'{{{excel.NS}}}worksheet');ET.SubElement(meta,f'{{{excel.NS}}}sheetData')
    excel.set_cell(meta,1,1,json.dumps(manifest));excel.set_cell(meta,1,2,PROFILE)
    parts[path]=ET.tostring(meta,encoding='utf-8',xml_declaration=True)
    for name,root in [('xl/workbook.xml',wb),('xl/_rels/workbook.xml.rels',rels),('[Content_Types].xml',types)]:
        parts[name]=ET.tostring(root,encoding='utf-8',xml_declaration=True)


def _context(editor,context,manifest):
    company=context.get('company','')
    teams='\n'.join(context.get('departments',[]))
    policies='\n'.join(context.get('existing_policies',[]))
    participants='\n'.join(label+': '+context[key] for label,key in [('Auditor','auditor'),('Observer','observer')] if context.get(key))
    e=editor.put
    e('Dashboard','B1','ISO/IEC 27001:2022 INTERNAL AUDIT DASHBOARD'+(' — '+company if company else ''))
    e('Dashboard','B2','Assessment version: '+str(manifest.get('version','')))
    for cell,value in {'B3':teams,'B4':company,'B5':'','B7':policies,'A8':'Auditor / Observer','B8':participants,'B9':''}.items(): e('Scope',cell,value)
    e('Scope','A1','ISMS Internal Audit Checklist ISO/IEC 27001:2022')
    for sheet,lead,year,dates in [('Clause 4-10','AG6','AQ2',['AO3','AQ3','AS3']),('Annex 5-8','AD6','AN2',['AL3','AN3','AP3'])]:
        e(sheet,'F5',teams);e(sheet,'F6',company);e(sheet,'F7','');e(sheet,'F13',policies)
        e(sheet,lead[:-1]+'5','Auditor / Observer')
        e(sheet,lead,participants);e(sheet,year,'')
        for cell in dates:e(sheet,cell,'')
    e('Annex 5-8','AD6',participants,"'Clause 4-10'!AG6")
    e('Clause 4-10','AE17','Answer / Internal Audit note')
    e('Annex 5-8','AB17','Answer / Gap Remediation')
    for sheet,cols,row in [('Clause 4-10',('Z','AA','AB','AC'),18),('Annex 5-8',('W','X','Y','Z'),18)]:
        for col,label in zip(cols,('C','OFI','N','M')):e(sheet,f'{col}{row}',label)
        for col,label in zip(cols,('C','OFI','N','M')):e(sheet,f'{col}{52 if sheet.startswith("Clause") else 116}',label)
    for sheet in ('Clause 4-10','Annex 5-8'):
        end=51 if sheet.startswith('Clause') else 115
        cols=('Z','AA','AB','AC') if sheet.startswith('Clause') else ('W','X','Y','Z')
        for row in range(19,end+1):
            for col in cols:e(sheet,f'{col}{row}','')
        e(sheet,'A54' if sheet.startswith('Clause') else 'A118','C = Comply; OFI = Opportunity for Improvement; N = NC-Minor; M = NC-Major. Edit answers in the Answer column.')


def export(controls,answers,manifest,results=None,questions=None,stale=False,unreviewed_ids=None,failed_ids=None,department_overrides=None,context=None,failure_warnings=None):
    refs=validate_controls(controls)
    editor=Editor(source()[0]);_context(editor,context or {},manifest)
    e=editor.put
    from ..questionnaire import text_by_control
    qs=text_by_control(questions or [])
    findings={f['control_id']:f for f in results or []};unreviewed=set(unreviewed_ids or []);failed=set(failed_ids or [])
    failures={cid:w['code'] for w in failure_warnings or [] for cid in w.get('control_ids',[])
              if isinstance(w.get('code'),str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',w['code'])}
    grades={};result_cells={sheet:[] for sheet in SHEETS}
    for c in controls:
        cid=c['id'];r=refs[cid];sheet,row=r['sheet'],r['row']
        f=findings.get(cid) if not stale and cid not in unreviewed|failed else None
        grade=f.get('audit',{}).get('category') if f else None
        if f and not grade:grade={'met':'comply','not_applicable':'not_applicable'}.get(f['status'],'needs_review')
        grades[cid]=grade
        if f:
            rationale=str(f.get('rationale') or f.get('audit',{}).get('reason') or '').strip()
            rationale=re.sub(r'^\s*(?:(?:Audit\s+Finding|Finding)\s*:\s*|Status\s*:\s*(?:C|OFI|N|M|Comply|NC-Minor|NC-Major)\s*[:\-–—]?\s*|(?:C|OFI|N|M|Comply|NC-Minor|NC-Major)\s*[:\-–—]\s*)',
                             '',rationale,flags=re.IGNORECASE)
            rationale = re.sub(r'(?im)^[ \t]*document reference[ \t]*:[ \t]*', '', rationale)
            details=[rationale] if rationale else ['Not assessed.']
            if grade == 'needs_review':
                details.insert(0, 'Needs review')
        elif stale:
            details=['Reassessment required.']
        elif not stale and cid in failed:
            code=failures.get(cid)
            reason={'control_coverage_invalid':'response did not match the supplied controls',
                    'inference_timeout':'inference request timed out',
                    'inference_connection_failed':'AI gateway connection failed',
                    'response_schema_invalid':'response did not match the required format',
                    'evidence_reference_invalid':'response referenced an invalid evidence ID'}.get(code,'AI assessment did not complete')
            details=[f'Review incomplete: {reason}. Reassess to retry.']
        else:
            details=['Not assessed.']
        e(sheet,f'{r["answer"]}{row}',answers[cid]['text'])
        e(sheet,f'{r["result"]}{row}','\n'.join(x for x in details if x))
        result_cells[sheet].append(f'{r["result"]}{row}')
        e(sheet,f'{r["owner"]}{row}',(department_overrides or {}).get(cid,c['department']))
        if cid in qs:e(sheet,f'B{row}',qs[cid])
        if grade in GRADES:e(sheet,f'{r["marks"][GRADES.index(grade)]}{row}',MARKS[GRADES.index(grade)])
    editor.expose_control_rows(refs)
    for sheet,root in editor.roots.items():
        excel.white_cell_fills(editor.parts,root,result_cells[sheet],unlock=True)
        excel.unprotect_sheet(root)
    _dashboard(editor,refs,grades,stale)
    editor.result_colors(refs,explicit_white=True,static_only=True)
    return editor.finish(manifest)


def _dashboard(editor,refs,grades,stale):
    e=editor.put
    def counts(ids):return [sum(grades.get(cid)==g for cid in ids) for g in GRADES]
    sections=[(str(i),[cid for cid in refs if not cid.startswith('A.') and cid.split('.')[0]==str(i)]) for i in range(4,11)]
    domains=[(str(i),[cid for cid in refs if cid.startswith(f'A.{i}.')]) for i in range(5,9)]
    for groups,first,sheet,cols,totalrow,totalcol in [(sections,6,'Clause 4-10',('Z','AA','AB','AC'),53,'Y'),(domains,6,'Annex 5-8',('W','X','Y','Z'),117,'V')]:
        allids=[cid for _,ids in groups for cid in ids]
        for i,(_,ids) in enumerate(groups):
            row=first+i; vals=counts(ids)
            isclause=sheet.startswith('Clause')
            e('Dashboard',f'{"C" if isclause else "J"}{row}',len(ids))
            a,b=min(refs[cid]['row'] for cid in ids),max(refs[cid]['row'] for cid in ids)
            for col,mark,v,dest in zip(cols,MARKS,vals,('D','E','F','G') if isclause else ('K','L','M','N')):
                formula=f'COUNTIF(\'{sheet}\'!{col}{a}:{col}{b},"{mark}")'
                e('Dashboard',f'{dest}{row}',v,formula)
            summaryrow=(16+i) if isclause else (26+i)
            for dest,v in zip(('Q','R','S','T','U'),[len(ids),*vals]):e('Dashboard',f'{dest}{summaryrow}',v)
            state=('NC-Major' if vals[3] else 'NC-Minor' if vals[2] else 'ต้องประเมินใหม่' if stale else
                   'ต้องตรวจเพิ่ม' if any(grades.get(cid)=='needs_review' for cid in ids) else
                   'ยังตรวจไม่ครบ' if any(grades.get(cid) is None for cid in ids) else
                   'ไม่เกี่ยวข้อง' if all(grades.get(cid)=='not_applicable' for cid in ids) else 'OFI' if vals[1] else 'Comply')
            e('Dashboard',f'V{summaryrow}',state)
            chartrow=(39+i) if isclause else (40+i)
            for dest,v in zip(('C','D','E','F') if isclause else ('Q','R','S','T'),vals):e('Dashboard',f'{dest}{chartrow}',v)
        vals=counts(allids)
        for col,mark,v in zip(cols,MARKS,vals):
            e(sheet,f'{col}{totalrow}',v,f'COUNTIF({col}20:{col}{totalrow-2},"{mark}")')
        e(sheet,f'{totalcol}{totalrow}',sum(vals),f'SUM({cols[0]}{totalrow}:{cols[-1]}{totalrow})')
        drow=13 if sheet.startswith('Clause') else 10
        tcol='C' if sheet.startswith('Clause') else 'J'
        e('Dashboard',f'{tcol}{drow}',len(allids),f'SUM({tcol}6:{tcol}{drow-1})')
        for col,v in zip(('D','E','F','G') if sheet.startswith('Clause') else ('K','L','M','N'),vals):
            e('Dashboard',f'{col}{drow}',v,f'SUM({col}6:{col}{drow-1})')
        ratecell='D14' if sheet.startswith('Clause') else 'K11'
        num='D13/C13' if sheet.startswith('Clause') else 'K10/J10'
        e('Dashboard',ratecell,vals[0]/len(allids),num)
        for i,v in enumerate(vals,33):e('Dashboard',f'{"C" if sheet.startswith("Clause") else "J"}{i}',v)
    for row,ids in [(6,[c for c in refs if not c.startswith('A.')]),(7,[c for c in refs if c.startswith('A.')]),(8,list(refs))]:
        vals=counts(ids)
        for col,v in zip(('Q','R','S','T','U'),[len(ids),*vals]):
            e('Dashboard',f'{col}{row}',v,f'SUM({col}6:{col}7)' if row==8 else None)
        e('Dashboard',f'V{row}',vals[0]/len(ids),f'R{row}/Q{row}')
    totals=counts(refs);reviewed=sum(v is not None for v in grades.values());na=sum(g=='not_applicable' for g in grades.values())
    for col,text,v in zip(('P','R','T','V'),('Comply','OFI','NC-Minor','NC-Major'),totals):e('Dashboard',f'{col}11',f'{text}: {v}')
    e('Dashboard','P12',f'Assessed: {reviewed}/118 | Unreviewed: {118-reviewed} | Not applicable: {na}')


def import_answers(data,controls,manifest,original_answers,expected_export):
    if original_answers is None or expected_export is None:
        raise ValueError('Original SC import requires the current assessment export for validation')
    refs=validate_controls(controls);wb=excel.read(data);expected=excel.read(expected_export)
    try:
        if json.loads(wb[excel.META]['A1'].value)!=manifest or wb[excel.META]['B1'].value!=PROFILE:
            raise ValueError('Workbook belongs to another assessment or stale revision')
        if wb.sheetnames!=expected.sheetnames:raise ValueError('Original SC worksheets differ')
        for name in ('Clause 4-10','Annex 5-8'):
            if set(map(str,wb[name].merged_cells.ranges))!=set(map(str,expected[name].merged_cells.ranges)):
                raise ValueError('Original SC merged layout cannot be changed')
        answers={}
        for c in controls:
            cid=c['id'];ref=refs[cid];ws=wb[ref['sheet']];es=expected[ref['sheet']];row=ref['row']
            for col in (ref['req'],ref['evidence'],ref['question'],ref['result'],ref['owner'],*ref['marks']):
                a,b=ws[f'{col}{row}'],es[f'{col}{row}']
                if a.data_type=='f' or (a.value or '')!=(b.value or ''):
                    raise ValueError('Requirements, questions, departments and audit results cannot be edited; change only the Answer cells')
            cell=ws[f'{ref["answer"]}{row}']
            if cell.data_type=='f':raise ValueError('Answer cells must contain literal text, not formulas')
            value='' if cell.value is None else str(cell.value)
            answers[cid]=Answer(status=original_answers[cid]['status'],text=value).model_dump()
        return answers
    except (KeyError,TypeError,json.JSONDecodeError) as exc:
        raise ValueError('Missing or invalid original SC assessment metadata') from exc
    finally:
        wb.close();expected.close()
