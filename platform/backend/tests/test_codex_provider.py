from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from app import codex_provider,assessment_jobs as jobs
from .test_codex_agent_mode import finding


@pytest.fixture(autouse=True)
def private_temporary_files(tmp_path,monkeypatch):
    monkeypatch.setattr(codex_provider.tempfile,'tempdir',str(tmp_path))



def context_receipt(work,tools):
    from hashlib import sha256
    snapshot=json.loads((work/'context.json').read_text(encoding='utf-8'))
    receipt={'sha256':sha256((work/'context.json').read_bytes()).hexdigest(),
             'control_ids':snapshot.get('control_ids',[snapshot.get('control_id')]),'tools':tools}
    (work/'context-access.json').write_text(json.dumps(receipt),encoding='utf-8')


def case():
    return {'control_id':'A.5.18','control':{'id':'A.5.18','requirement':'Review user access'},'answer':{'text':'Reviewed quarterly','status':'unanswered'},'evidence':[{'id':'E1','control_id':'A.5.18','text':'Auditor verified quarterly review entries'}],'context':{'company':'SYNTHETIC-CUSTOMER'},'approved_examples':[],'questions':[],'policies':[]}


@pytest.mark.parametrize('installed,authenticated,compatible,code',[(False,False,False,'codex_not_installed'),(True,False,True,'codex_not_authenticated'),(True,True,False,'codex_cli_unsupported'),(True,True,True,'ready')])
def test_health_uses_only_non_billable_cli_commands_and_never_returns_secrets(monkeypatch,installed,authenticated,compatible,code):
    monkeypatch.setenv('CODEX_AGENT_ENABLED','true')
    monkeypatch.setattr(codex_provider,'cli_path',lambda:'synthetic-codex.exe' if installed else None)
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        if command[-1]=='-':
            assert kwargs['input']=='' and '--output-schema' in command
            assert 'tools.view_image=false' not in command
            return SimpleNamespace(returncode=1,stdout='',stderr='No prompt provided via stdin.')
        if command[-1]=='--version':return SimpleNamespace(returncode=0,stdout='codex-cli 0.163.0')
        if command[-1]=='list':return SimpleNamespace(returncode=0,stdout='\n'.join(name+' stable true' for name in codex_provider.DISABLED_FEATURES+codex_provider.REQUIRED_FEATURES))
        if command[-1]=='status':return SimpleNamespace(returncode=0 if authenticated else 1,stdout='SECRET_AUTH_TEXT')
        return SimpleNamespace(returncode=0,stdout='--output-schema --ephemeral --ignore-user-config --skip-git-repo-check --sandbox --json --strict-config --output-last-message' if compatible else '--help')
    monkeypatch.setattr(codex_provider.subprocess,'run',run)
    result=codex_provider.health()
    assert result['code']==code
    assert 'SECRET' not in json.dumps(result)
    assert all('exec' not in c or c[-1] in {'--help','-'} for c in calls)


def test_cli_contract_is_readonly_scoped_uses_saved_login_and_discards_raw_events(monkeypatch):
    monkeypatch.setattr(codex_provider,'cli_path',lambda:'synthetic-codex.exe')
    monkeypatch.setenv('AI_API_KEY','DO_NOT_INHERIT')
    monkeypatch.setenv('OPENAI_API_KEY','DO_NOT_INHERIT')
    monkeypatch.setenv('CODEX_API_KEY','DO_NOT_INHERIT')
    paths=[]
    class Process:
        returncode=0
        def __init__(self,command,stdin,stdout,**kwargs):
            assert '--ignore-user-config' in command and '--strict-config' in command
            assert command[command.index('--sandbox')+1]=='read-only'
            assert '--ephemeral' in command and '--output-schema' in command and '--json' in command
            assert command[-1]=='-' and 'SYNTHETIC-CUSTOMER' not in ' '.join(command)
            assert all('features.'+feature+'=false' in command for feature in codex_provider.DISABLED_FEATURES)
            assert 'features.code_mode_host=true' in command and 'features.code_mode_host=false' not in command
            assert 'tools.view_image=false' not in command and 'features.view_image=false' in command and 'web_search="disabled"' in command
            assert not any('dangerously' in part for part in command)
            assert not any(k in kwargs['env'] for k in ('AI_API_KEY','OPENAI_API_KEY','CODEX_API_KEY'))
            work=Path(kwargs['cwd']);paths.append(work)
            context=json.loads((work/'context.json').read_text(encoding='utf-8'))
            assert context['control_id']=='A.5.18'
            assert 'Never' in stdin.read().decode()
            context_receipt(work,['get_control','get_evidence','get_approved_examples'])
            (work/'result.json').write_text(json.dumps({'finding':finding(case())}),encoding='utf-8')
            stdout.write((json.dumps({'type':'agent.message','text':'PRIVATE_REASONING'})+'\n'+json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}})).encode());stdout.flush()
        def poll(self):return 0
    monkeypatch.setattr(codex_provider.subprocess,'Popen',Process)
    provider=codex_provider.CodexProvider()
    result=provider.assess_control(case())
    assert result['audit']['category']=='comply'
    assert provider.usage_calls[0]['prompt_tokens']==10
    assert 'PRIVATE_REASONING' not in json.dumps(provider.usage_calls)
    assert all(not path.exists() for path in paths)


def test_cancel_terminates_cli_and_does_not_accept_late_output(monkeypatch):
    monkeypatch.setattr(codex_provider,'cli_path',lambda:'synthetic-codex.exe')
    job=jobs.reserve('synthetic')
    terminated=[]
    class Process:
        returncode=None
        def __init__(self,*args,**kwargs):jobs.cancel(job['id'])
        def poll(self):return None
    monkeypatch.setattr(codex_provider.subprocess,'Popen',Process)
    monkeypatch.setattr(codex_provider,'terminate',lambda p:terminated.append(p))
    with jobs.generation(job['id'],'synthetic'):
        with pytest.raises(jobs.AssessmentCancelled):codex_provider.CodexProvider().assess_control(case())
    assert len(terminated)==1


def test_output_schema_requires_all_fields_and_evidence_cannot_confirm_uncertain_gap():
    schema=codex_provider.output_schema()
    assert set(schema['properties'])=={'finding'} and schema['additionalProperties'] is False
    assert set(schema['properties']['finding']['required'])==set(schema['properties']['finding']['properties'])
    data=case();data['evidence']=[]
    raw=finding(data,'nc_major');raw['audit']['category']='needs_review'
    checked=codex_provider.validate_result({'finding':raw},data)
    assert checked['status']=='needs_review'


def test_credentials_are_minimized_without_changing_control_or_evidence_ids():
    data=case()
    data['answer']['text']='password=hunter2 api_key=sk-abcdefghijklmnop Bearer syntheticlongtoken'
    sent=codex_provider.redact_credentials(data)
    assert 'hunter2' not in sent['answer']['text'] and 'sk-abcdefghijklmnop' not in sent['answer']['text']
    assert 'syntheticlongtoken' not in sent['answer']['text']
    assert sent['control_id']==data['control_id'] and sent['evidence'][0]['id']=='E1'
    assert 'hunter2' in data['answer']['text']


def test_stdio_mcp_server_with_scoped_synthetic_context(tmp_path):
    import subprocess,sys
    folder=tmp_path/'mcp';folder.mkdir()
    path=folder/'context.json'
    path.write_text(json.dumps({'format':'codex-audit-context/v1','control_id':'C1','control':{'requirement':'Synthetic scoped requirement ข้อกำหนด 🔒'},'evidence':[],'approved_examples':[]}),encoding='utf-8')
    messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},{'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_control','arguments':{'control_id':'C1'}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'get_evidence','arguments':{'control_id':'OTHER'}}}]
    process=subprocess.run([sys.executable,str(Path(codex_provider.__file__).with_name('codex_context_mcp.py')),str(path)],input='\n'.join(json.dumps(m) for m in messages)+'\n',text=True,encoding='utf-8',capture_output=True,timeout=10)
    assert process.returncode==0
    rows=[json.loads(line) for line in process.stdout.splitlines()]
    assert [r['id'] for r in rows]==[1,2,3,4]
    assert 'Synthetic scoped requirement ข้อกำหนด 🔒' in rows[2]['result']['content'][0]['text']
    assert rows[3]['result']['isError'] is True


def test_application_database_error_messages_hide_bound_audit_data():
    from app.db import engine as application_engine
    from sqlalchemy import create_engine,text
    from sqlalchemy.exc import DBAPIError
    engine=create_engine('sqlite://',hide_parameters=application_engine.hide_parameters)
    try:
        with engine.connect() as connection:
            with pytest.raises(DBAPIError) as error:connection.execute(text('SELECT :audit_record FROM missing_table'),{'audit_record':'PRIVATE_CUSTOMER_SENTINEL'})
        assert 'PRIVATE_CUSTOMER_SENTINEL' not in str(error.value)
    finally:engine.dispose()


def test_cli_error_diagnostics_are_allowlisted_and_do_not_expose_stderr():
    private='PRIVATE_CLIENT_SENTINEL'
    assert codex_provider.execution_error('Error loading config.toml: unknown configuration field tools.view_image '+private)=='codex_configuration_invalid'
    assert codex_provider.execution_error(private)=='codex_exec_failed'


def test_health_rejects_configuration_error_before_allowing_an_assessment(monkeypatch):
    monkeypatch.setenv('CODEX_AGENT_ENABLED','true')
    monkeypatch.setattr(codex_provider,'cli_path',lambda:'synthetic-codex.exe')
    def run(command,**kwargs):
        if command[-1]=='--version':return SimpleNamespace(returncode=0,stdout='codex-cli 0.163.0')
        if command[-1]=='list':return SimpleNamespace(returncode=0,stdout='\n'.join(name+' stable true' for name in codex_provider.DISABLED_FEATURES+codex_provider.REQUIRED_FEATURES))
        if command[-1]=='status':return SimpleNamespace(returncode=0,stdout='')
        if command[-1]=='-':
            assert kwargs['input']==''
            return SimpleNamespace(returncode=1,stdout='',stderr='Error loading config.toml: unknown configuration field PRIVATE_SENTINEL')
        return SimpleNamespace(returncode=0,stdout='--output-schema --ephemeral --ignore-user-config --skip-git-repo-check --sandbox --json --strict-config --output-last-message')
    monkeypatch.setattr(codex_provider.subprocess,'run',run)
    result=codex_provider.health()
    assert result['code']=='codex_configuration_invalid'
    assert not result['available'] and not result['compatible']
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


def test_batch_cli_uses_one_scoped_mcp_read_and_accounts_usage_once(monkeypatch):
    monkeypatch.setenv('CODEX_MODEL','gpt-6-luna')
    monkeypatch.setattr(codex_provider,'cli_path',lambda:'synthetic-codex.exe')
    monkeypatch.setenv('CODEX_REASONING_EFFORT','low')
    first=case();second=deepcopy(first)
    second['control_id']='A.5.19';second['control']['id']='A.5.19'
    first['questions']=[{'id':'UQ-1','source':'user','control_id':'A.5.18','control_ids':['A.5.18'],
                         'text':'ทีมจัดการบัญชีพนักงานที่ลาออกอย่างไร?'}]
    second['evidence']=[dict(second['evidence'][0],id='E2',control_id='A.5.19')]
    shared={'id':'P1','text':'Shared policy','control_ids':['A.5.18','A.5.19']}
    first['policies']=[shared];second['policies']=[shared]
    calls=[]
    class Process:
        returncode=0
        def __init__(self,command,stdin,stdout,**kwargs):
            calls.append(command)
            assert 'model_reasoning_effort="low"' in command
            assert command[command.index('--model')+1]=='gpt-6-luna'
            assert 'mcp_servers.compliance.enabled_tools=["get_batch_context"]' in command
            work=Path(kwargs['cwd']);snapshot=json.loads((work/'context.json').read_text(encoding='utf-8'))
            assert snapshot['control_ids']==['A.5.18','A.5.19'] and len(snapshot['policies'])==1
            assert snapshot['cases'][0]['questions'][0]['source']=='user'
            assert snapshot['cases'][0]['questions'][0]['text']=='ทีมจัดการบัญชีพนักงานที่ลาออกอย่างไร?'
            assert all('context' not in item and 'policies' not in item for item in snapshot['cases'])
            prompt=stdin.read().decode()
            assert 'get_batch_context tool once' in prompt
            assert 'Read source=user supplemental questions' in prompt
            assert 'question wording itself is never implementation evidence' in prompt
            assert 'features.code_mode_host=true' in command
            context_receipt(work,['get_batch_context'])
            assert prompt.count('name: finding-classifier')==1
            schema=json.loads((work/'schema.json').read_text(encoding='utf-8'))
            assert schema['properties']['findings']['minItems']==2
            (work/'result.json').write_text(json.dumps({'findings':[finding(first),finding(second)]}),encoding='utf-8')
            stdout.write((json.dumps({'type':'turn.completed','usage':{'input_tokens':120,'output_tokens':30}})+'\n').encode());stdout.flush()
        def poll(self):return 0
    monkeypatch.setattr(codex_provider.subprocess,'Popen',Process)
    provider=codex_provider.CodexProvider()
    rows=provider.assess_controls([first,second])
    assert len(rows)==2 and len(calls)==1 and len(provider.usage_calls)==1
    assert provider.usage_calls[0]['control_ids']==['A.5.18','A.5.19']
    assert provider.usage_calls[0]['prompt_tokens']==120


def test_batch_mcp_rejects_extra_ids_and_deduplicates_shared_context():
    from app.codex_context_mcp import dispatch
    first=case();second=deepcopy(first);second['control_id']='OTHER';second['control']['id']='OTHER';second['evidence']=[]
    snapshot=codex_provider.compact_batch([first,second])
    tools=dispatch(snapshot,{'method':'tools/list'})['tools']
    assert [tool['name'] for tool in tools]==['get_batch_context']
    request={'method':'tools/call','params':{'name':'get_batch_context','arguments':{'control_ids':['A.5.18','OTHER']}}}
    assert not dispatch(snapshot,request)['isError']
    for ids in [['A.5.18'],['A.5.18','OTHER','UNAUTHORIZED']]:
        request['params']['arguments']['control_ids']=ids
        response=dispatch(snapshot,request)
        assert response['isError'] and 'Reviewed quarterly' not in json.dumps(response)
    second['context']={'company':'ANOTHER CLIENT'}
    with pytest.raises(codex_provider.CodexError):codex_provider.compact_batch([first,second])


def test_real_stdio_batch_mcp_returns_only_the_authorized_snapshot(tmp_path):
    import subprocess,sys
    data=codex_provider.compact_batch([case()]);path=tmp_path/'context.json'
    path.write_text(json.dumps(data),encoding='utf-8')
    messages=[{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_batch_context','arguments':{'control_ids':['A.5.18']}}},
              {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_control','arguments':{'control_id':'OTHER'}}}]
    process=subprocess.run([sys.executable,str(Path(codex_provider.__file__).with_name('codex_context_mcp.py')),str(path)],input='\n'.join(json.dumps(m) for m in messages)+'\n',text=True,encoding='utf-8',capture_output=True,timeout=10)
    assert process.returncode==0
    rows=[json.loads(line) for line in process.stdout.splitlines()]
    assert not rows[0]['result']['isError'] and rows[1]['result']['isError']
    codex_provider.verify_context_receipt(tmp_path,data,['get_batch_context'])


def test_valid_json_without_mcp_context_read_is_rejected_and_usage_is_retained(monkeypatch):
    monkeypatch.setattr(codex_provider,'cli_path',lambda:'synthetic-codex.exe')
    class Process:
        returncode=0
        def __init__(self,command,stdin,stdout,**kwargs):
            work=Path(kwargs['cwd'])
            # Reproduces the user's failure: syntactically valid output without fetching records.
            output=finding(case());output['rationale']='get_batch_context is unavailable; not assessable'
            (work/'result.json').write_text(json.dumps({'findings':[output]}),encoding='utf-8')
            stdout.write((json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}})+'\n').encode());stdout.flush()
        def poll(self):return 0
    monkeypatch.setattr(codex_provider.subprocess,'Popen',Process)
    provider=codex_provider.CodexProvider()
    with pytest.raises(codex_provider.CodexError) as error:provider.assess_controls([case()])
    assert error.value.code=='codex_context_not_read'
    assert len(provider.usage_calls)==1 and provider.usage_calls[0]['prompt_tokens']==10


def test_receipt_checks_exact_snapshot_ids_and_required_tool_reads(tmp_path):
    snapshot=codex_provider.compact_batch([case()])
    (tmp_path/'context.json').write_text(json.dumps(snapshot),encoding='utf-8')
    context_receipt(tmp_path,['get_batch_context'])
    codex_provider.verify_context_receipt(tmp_path,snapshot,['get_batch_context'])
    for field,value in [('sha256','wrong'),('control_ids',['OTHER']),('tools',[])]:
        context_receipt(tmp_path,['get_batch_context'])
        path=tmp_path/'context-access.json';record=json.loads(path.read_text())
        record[field]=value;path.write_text(json.dumps(record))
        with pytest.raises(codex_provider.CodexError):codex_provider.verify_context_receipt(tmp_path,snapshot,['get_batch_context'])


def test_luna_model_is_explicitly_requested_and_host_enabled(monkeypatch):
    monkeypatch.setenv('CODEX_MODEL','gpt-6-luna')
    monkeypatch.setenv('CODEX_REASONING_EFFORT','low')
    provider=codex_provider.CodexProvider()
    assert provider.name=='codex:gpt-6-luna'
    options=codex_provider.restricted_config()
    assert 'features.code_mode_host=true' in options
    assert 'features.shell_tool=false' in options and 'features.unified_exec=false' in options
    assert 'features.code_mode_host=false' not in options


def test_assessor_home_overrides_only_child_environment_and_never_falls_back(monkeypatch,tmp_path):
    import os
    desktop=tmp_path/'desktop';assessor=tmp_path/'assessor'
    monkeypatch.setenv('CODEX_HOME',str(desktop))
    monkeypatch.setenv('CODEX_AGENT_HOME',str(assessor))
    monkeypatch.setenv('CODEX_ACCESS_TOKEN','DO_NOT_INHERIT')
    env=codex_provider.clean_environment()
    assert Path(env['CODEX_HOME'])==assessor
    assert os.environ['CODEX_HOME']==str(desktop)
    assert 'CODEX_ACCESS_TOKEN' not in env
    assert codex_provider.auth_config()==['-c','cli_auth_credentials_store="file"']
    assert 'cli_auth_credentials_store="file"' in codex_provider.restricted_config()


def test_dedicated_profile_cannot_point_at_the_chat_profile(monkeypatch,tmp_path):
    desktop=tmp_path/'desktop'
    monkeypatch.setenv('CODEX_HOME',str(desktop))
    monkeypatch.setenv('CODEX_AGENT_HOME',str(desktop))
    with pytest.raises(codex_provider.CodexError) as error:codex_provider.clean_environment()
    assert error.value.code=='codex_account_home_not_isolated'
