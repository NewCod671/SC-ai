from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import main
from app.reporting import excel, native_sc
from app.db import Base
from app.models import Standard, Assessment, Revision

ROOT=Path(__file__).resolve().parents[2]


def edit(data,sheet,ref,value):
    from openpyxl.utils import coordinate_to_tuple
    with ZipFile(BytesIO(data)) as z:parts={n:z.read(n) for n in z.namelist()}
    path=excel.sheet_path(parts,sheet);root=excel.xml(parts[path]);r,c=coordinate_to_tuple(ref)
    excel.set_cell(root,r,c,value);parts[path]=excel.ET.tostring(root)
    out=BytesIO()
    with ZipFile(out,'w',ZIP_DEFLATED) as z:
        for n,v in parts.items():z.writestr(n,v)
    return out.getvalue()


@pytest.fixture(scope='module')
def setup():
    p=ROOT/'outputs/sc-audit-template'
    template=(p/'sc-iso27001-audit-template.xlsx').read_bytes()
    controls,mapping=excel.parse_standard(template,json.loads((p/'mapping.json').read_text()))
    mapping['export_profile']=native_sc.PROFILE
    answers={c['id']:{'status':'unanswered','text':''} for c in controls}
    questions=[{'control_id':'4.1','control_ids':['4.1'],'text':'Who reviews business changes?'}]
    manifest={'assessment_id':'test','standard_id':'s','version':1}
    return template,controls,mapping,answers,questions,manifest


@pytest.fixture(scope='module')
def exported(setup):
    t,c,m,a,q,v=setup
    a=deepcopy(a);a['A.5.2']['text']='=literal answer'
    findings=[]
    for cid,cat in [('4.1','comply'),('A.5.2','ofi'),('7.5.2','nc_minor'),('A.8.34','nc_major')]:
        findings.append({'control_id':cid,'status':'met' if cat in {'comply','ofi'} else 'gap',
            'rationale':'Actual finding','recommendation':'Actual next action','evidence_ids':['e1'],
            'audit':{'category':cat,'reason':'Actual grading reason','requirement_gap':'Actual requirement gap'}})
    result=excel.export(t,m,c,a,v,findings,q,context={'company':'Synthetic Test Co','departments':['IT','HR'],'existing_policies':['Current policy']})
    return result,a,findings


def test_original_tabs_styles_drawings_and_merged_layout_are_preserved(exported):
    result,_,_=exported
    original=native_sc.source()[0];before=excel.read(original);after=excel.read(result)
    assert after.sheetnames==[*native_sc.SHEETS,excel.META]
    assert after[excel.META].sheet_state=='veryHidden'
    for name in native_sc.SHEETS:
        assert set(map(str,before[name].merged_cells.ranges))==set(map(str,after[name].merged_cells.ranges))
        assert [(k,v.width,v.hidden) for k,v in before[name].column_dimensions.items()]==[(k,v.width,v.hidden) for k,v in after[name].column_dimensions.items()]
    assert len(after['Scope']._images)==0
    assert before['Clause 4-10']['AD20'].style_id==after['Clause 4-10']['AD20'].style_id
    assert before['Annex 5-8']['AB21'].style_id==after['Annex 5-8']['AB21'].style_id
    before.close();after.close()
    with ZipFile(BytesIO(original)) as a,ZipFile(BytesIO(result)) as b:
        for name in a.namelist():
            if name in {'xl/drawings/drawing2.xml','xl/drawings/_rels/drawing2.xml.rels','xl/media/image1.png'}:continue
            if name.startswith(('xl/drawings/','xl/media/')) :
                assert a.read(name)==b.read(name),name


def test_current_grade_marks_questions_answers_and_scope_replace_samples(exported):
    data,_,_=exported;wb=excel.read(data)
    assert wb['Clause 4-10']['B20'].value=='Who reviews business changes?'
    assert wb['Clause 4-10']['Z20'].value=='C'
    assert wb['Annex 5-8']['X21'].value=='OFI' and wb['Annex 5-8']['W21'].value in ('',None)
    r=native_sc.source()[1]['7.5.2'];assert wb[r['sheet']][f'AB{r["row"]}'].value=='N'
    assert wb['Annex 5-8']['Z115'].value=='M'
    assert wb['Annex 5-8']['AB21'].value=='=literal answer'
    assert wb['Annex 5-8']['AB21'].data_type!='f'
    assert wb['Annex 5-8']['AA115'].value=='Actual finding'
    assert 'NC-Major' not in wb['Annex 5-8']['AA115'].value
    assert 'Actual next action' not in wb['Annex 5-8']['AA115'].value
    assert wb['Scope']['B4'].value=='Synthetic Test Co'
    assert wb['Scope']['B8'].value in ('',None)
    assert wb['Clause 4-10']['F7'].value in ('',None)
    assert wb['Annex 5-8']['W66'].value in ('',None)  # sample C on a section heading
    for sheet in native_sc.SHEETS:
        texts=' '.join(str(c.value) for row in wb[sheet] for c in row if c.value is not None)
        assert 'Empyrion' not in texts and 'Haziq' not in texts and 'Prame' not in texts
    wb.close()


def test_dashboard_and_chart_caches_use_new_results(exported):
    data,_,_=exported
    from openpyxl import load_workbook
    wb=load_workbook(BytesIO(data),data_only=True)
    assert [wb['Dashboard'][f'{c}8'].value for c in ('Q','R','S','T','U')]==[118,1,1,1,1]
    assert wb['Clause 4-10']['Y53'].value==2
    assert wb['Annex 5-8']['V117'].value==2
    assert '4/118' in wb['Dashboard']['P12'].value
    wb.close()
    with ZipFile(BytesIO(data)) as z:
        root=excel.xml(z.read('xl/charts/chart1.xml'))
        assert root.xpath('//*[local-name()="numCache"]/*[local-name()="pt"]/*[local-name()="v"]/text()')==['1','0','1','0']


@pytest.mark.parametrize('mode',['blank','stale','failed'])
def test_no_old_passes_or_old_findings_for_unreviewed_controls(setup,exported,mode):
    t,c,m,a,q,v=setup;_,_,f=exported
    data=excel.export(t,m,c,a,v,results=f if mode!='blank' else [],stale=mode=='stale',failed_ids=['4.1'] if mode=='failed' else [])
    wb=excel.read(data)
    assert wb['Clause 4-10']['Z20'].value in ('',None)
    expected={'stale':'Reassessment required.','blank':'Not assessed.',
              'failed':'Review incomplete: AI assessment did not complete. Reassess to retry.'}[mode]
    assert wb['Clause 4-10']['AD20'].value==expected
    assert 'Actual finding' not in wb['Clause 4-10']['AD20'].value
    wb.close()


def test_native_roundtrip_edits_only_answers_and_checks_revision(setup,exported):
    _,c,m,_,_,v=setup;data,answers,_=exported
    changed=edit(data,'Clause 4-10','AE20','Updated interview answer')
    incoming=excel.import_answers(changed,m,c,v,answers,expected_export=data)
    assert incoming['4.1']['text']=='Updated interview answer'
    assert incoming['A.5.2']['text']=='=literal answer'
    assert incoming['A.8.34']==answers['A.8.34']
    with pytest.raises(ValueError,match='stale revision'):
        excel.import_answers(data,m,c,{**v,'version':2},answers,expected_export=data)


@pytest.mark.parametrize('cell,value',[('U20','Changed requirement'),('AD20','Status: Comply'),('Z20','M'),('B20','Changed question')])
def test_forged_output_or_requirements_are_rejected(setup,exported,cell,value):
    _,c,m,_,_,v=setup;data,answers,_=exported
    changed=edit(data,'Clause 4-10',cell,value)
    with pytest.raises(ValueError,match='change only the Answer'):
        excel.import_answers(changed,m,c,v,answers,expected_export=data)


def test_source_template_rejects_another_standard(setup):
    _,controls,_,_,_,_=setup
    bad=deepcopy(controls);bad[0]['id']='CUSTOM'
    with pytest.raises(ValueError,match='exact 118'):
        native_sc.validate_controls(bad)
    bad=deepcopy(controls);bad[0]['requirement']='Changed obligation'
    with pytest.raises(ValueError,match='requirements differ'):
        native_sc.validate_controls(bad)


def test_assessment_export_and_standard_preview_use_native_profile(setup):
    t,controls,m,a,q,_=setup
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine) as db:
        s=Standard(name='Native SC',version='1',controls=controls,mapping=m,template=t,changes={})
        db.add(s);db.flush()
        assessment=Assessment(name='Native test',standard_id=s.id,context={'company':'Current Co','departments':['IT']},questions=q,current_version=1)
        db.add(assessment);db.flush();db.add(Revision(assessment_id=assessment.id,version=1,answers=a,source='test'));db.commit()
        _,data=main.assessment_workbook(assessment.id,db)
        assert excel.read(data)['Scope']['B4'].value=='Current Co'
        preview=main.preview_standard(s.id,None,1,db)
        assert preview['sheets']==list(native_sc.SHEETS)
        assert '92%' not in json.dumps(preview,ensure_ascii=False)
    engine.dispose()

def test_native_import_route_saves_a_new_answer_revision(setup):
    from fastapi import UploadFile
    from tempfile import SpooledTemporaryFile
    t,controls,m,answers,questions,_=setup
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine) as db:
        standard=Standard(name='SC route test',version='1',controls=controls,mapping=m,template=t,changes={})
        db.add(standard);db.flush()
        assessment=Assessment(name='Edit SC',standard_id=standard.id,context={'company':'Test Co','departments':['IT']},questions=questions,current_version=1)
        db.add(assessment);db.flush();db.add(Revision(assessment_id=assessment.id,version=1,answers=answers,source='test'));db.commit()
        _,exported=main.assessment_workbook(assessment.id,db)
        changed=edit(exported,'Annex 5-8','AB21','New control-specific answer')
        # In-memory UploadFile.read completes without asynchronous IO or sockets.
        upload=SpooledTemporaryFile(max_size=10*1024*1024)
        upload.write(changed);upload.seek(0)
        task=main.upload_answers(assessment.id,UploadFile(filename='assessment.xlsx',file=upload),db)
        with pytest.raises(StopIteration) as done:task.send(None)
        upload.close()
        result=done.value.value
        assert result['current_version']==2
        assert result['answers']['A.5.2']['text']=='New control-specific answer'
        assert result['answers']['5.2']['text']==''
    engine.dispose()


def test_export_does_not_keep_historical_strings_in_shared_string_table(exported):
    with ZipFile(BytesIO(exported[0])) as z:
        strings=z.read('xl/sharedStrings.xml').decode('utf-8')
        assert 'Empyrion' not in strings and 'Haziq' not in strings and 'Prame' not in strings


def test_enable_original_layout_is_idempotent_and_preserves_standard_controls(setup):
    import importlib.util
    spec=importlib.util.spec_from_file_location('enable_sc_original_export',ROOT/'scripts/enable_sc_original_export.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    t,c,m,_,_,_=setup
    original_mapping={k:v for k,v in m.items() if k!='export_profile'}
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine) as db:
        s=Standard(name='ISO/IEC 27001:2022 (SC Audit Checklist)',version='SC202512-03-V1',controls=c,mapping=original_mapping,template=t,changes={})
        db.add(s);db.commit()
        assert module.enable(db)==(s.id,True)
        db.commit()
        assert module.enable(db)==(s.id,False)
        assert s.controls==c and s.template==t
    engine.dispose()

def test_answer_department_mapping_is_used_by_original_excel_export(setup,monkeypatch):
    from types import SimpleNamespace
    from app import ai
    from app.schemas import ReplaceAnswers
    t,controls,m,answers,questions,_=setup
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    calls=[]
    def map_departments(payload):
        calls.append([c['id'] for c in payload['controls']])
        return [{'control_id':c['id'],'departments':['IT','HR'],'reason':'Both teams collaborate as stated'} for c in payload['controls']]
    monkeypatch.setattr(ai,'provider',lambda:SimpleNamespace(name='http:synthetic',map_departments=map_departments,usage_calls=[]))
    with Session(engine) as db:
        s=Standard(name='SC coverage integration',version='1',controls=controls,mapping=m,template=t,changes={})
        db.add(s);db.flush()
        assessment=Assessment(name='SC coverage',standard_id=s.id,context={'company':'Actual Co','departments':['IT','HR']},questions=questions,current_version=1)
        db.add(assessment);db.flush();db.add(Revision(assessment_id=assessment.id,version=1,answers=answers,source='test'));db.commit()
        changed=deepcopy(answers);changed['A.5.2']['text']='IT and HR collaborate on these responsibilities'
        saved=main.save_answers(assessment.id,ReplaceAnswers(expected_version=1,answers=changed),db)
        assert saved['department_assignments']['A.5.2']['departments']==['IT','HR']
        assert calls==[['A.5.2']]
        _,data=main.assessment_workbook(assessment.id,db)
        wb=excel.read(data)
        assert wb['Annex 5-8']['AC21'].value=='IT / HR'
        assert wb['Annex 5-8']['AB21'].value==changed['A.5.2']['text']
        wb.close()
    engine.dispose()

def test_result_highlight_depends_on_visible_grade_and_blanks_have_no_old_fill(exported):
    data,_,_=exported;wb=excel.read(data)
    for cid,ref in native_sc.source()[1].items():
        ws=wb[ref['sheet']]
        for col in ref['marks']:
            cell=ws[f'{col}{ref["row"]}']
            if cell.value in ('',None):assert cell.fill.patternType is None,(cid,cell.coordinate)
            else:assert cell.fill.patternType=='solid'
    for sheet,cell,mark,color in [('Clause 4-10','Z20','C','FFD9EAD3'),('Annex 5-8','X21','OFI','FFFFF2CC'),('Clause 4-10','AB39','N','FFFCE4D6'),('Annex 5-8','Z115','M','FFF4CCCC')]:
        ws=wb[sheet]
        assert ws[cell].value==mark
        applicable=[rule for area in ws.conditional_formatting if cell in area.sqref for rule in ws.conditional_formatting[area]]
        matching=[r for r in applicable if r.type=='cellIs' and r.operator=='equal' and r.formula==['"'+mark+'"']]
        assert len(matching)==1
        assert matching[0].dxf.fill.fgColor.rgb==color
        assert ws[cell].fill.fgColor.rgb==color
        reset=[r for r in applicable if r.operator=='notEqual' and r.formula==['"'+mark+'"']]
        assert len(reset)==1 and reset[0].dxf.fill.fgColor.rgb=='FFFFFFFF'
        assert not any(r.formula==['""'] for r in applicable)
    wb.close()


def test_result_style_changes_preserve_existing_style_definitions(exported):
    with ZipFile(BytesIO(native_sc.source()[0])) as a,ZipFile(BytesIO(exported[0])) as b:
        original=excel.xml(a.read('xl/styles.xml'));current=excel.xml(b.read('xl/styles.xml'))
        for name in ('fonts','borders','cellStyleXfs','cellStyles'):
            assert excel.ET.tostring(original.find(f'{{{excel.NS}}}{name}'))==excel.ET.tostring(current.find(f'{{{excel.NS}}}{name}'))
        old_fills=original.find(f'{{{excel.NS}}}fills');new_fills=current.find(f'{{{excel.NS}}}fills')
        for i,node in enumerate(old_fills):assert excel.ET.tostring(node)==excel.ET.tostring(new_fills[i])
        old=original.find(f'{{{excel.NS}}}cellXfs');new=current.find(f'{{{excel.NS}}}cellXfs')
        assert len(new)>=len(old)
        for i,node in enumerate(old):assert excel.ET.tostring(node)==excel.ET.tostring(new[i])


@pytest.mark.parametrize('state',['stale','unreviewed','failed'])
def test_invalidated_result_clears_both_mark_and_old_color(setup,exported,state):
    t,c,m,a,q,v=setup;_,_,findings=exported
    kwargs={'stale':True} if state=='stale' else {'unreviewed_ids':['4.1']} if state=='unreviewed' else {'failed_ids':['4.1']}
    data=excel.export(t,m,c,a,v,results=findings,**kwargs)
    wb=excel.read(data)
    for col in ('Z','AA','AB','AC'):
        cell=wb['Clause 4-10'][f'{col}20']
        assert cell.value in ('',None)
        assert cell.fill.patternType is None
    wb.close()

def test_exported_failure_explains_safe_error_code_without_showing_an_old_grade(setup,exported):
    t,c,m,a,q,v=setup;_,_,findings=exported
    data=excel.export(t,m,c,a,v,results=findings,failed_ids=['4.1'],
        failure_warnings=[{'control_ids':['4.1'],'code':'control_coverage_invalid','stage':'validation','step':1}])
    wb=excel.read(data)
    text=wb['Clause 4-10']['AD20'].value
    assert 'Error: control_coverage_invalid' in text
    assert 'รหัสข้อไม่ตรง' in text
    assert 'Actual finding' not in text
    assert wb['Clause 4-10']['Z20'].value in ('',None)
    assert wb['Clause 4-10']['Z20'].fill.patternType is None
    wb.close()
