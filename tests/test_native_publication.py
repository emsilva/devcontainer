"""Picker-ready checkpoint imports never need an interactive/model turn."""
import copy
import json
import os
from pathlib import Path
import uuid
from unittest.mock import patch

from test_native_checkpoints import NativeFixture, lines


class PublicationTests(NativeFixture):
    def publish(self, entry, *, env=None):
        manifest = dict(schema=2, project=dict(project_id=self.project.project_id, identity=self.project.identity), conversations=dict(schema=1, entries=[entry], issues=[]))
        self.store.load = lambda sid: manifest
        return self.api.publish_conversations(self.project, self.store, 'checkpoint', state=self.root / 'publication-state', env=env or self.env)

    def test_claude_picker_ready_standalone_and_semantic_idempotence(self):
        original = self.claude()
        entry = self.capture()['entries'][0]
        result = self.publish(entry)
        self.assertEqual(result['issues'], [])
        child = result['imported'][0]['session_id']
        target = original.with_name(child + '.jsonl')
        rows = [json.loads(line) for line in target.read_bytes().splitlines()]
        self.assertEqual([r['sessionId'] for r in rows], [child, child])
        self.assertEqual([r['message'] for r in rows], [r['message'] for r in map(json.loads, original.read_bytes().splitlines())])
        self.assertTrue(target.stat().st_mode & 0o200)
        entry['title'] = 'Different display title'; entry['updated_at'] += 100
        again = self.publish(entry)
        self.assertEqual(again['imported'], [])
        self.assertEqual(again['unchanged'][0]['session_id'], child)
        captured = self.api.capture_conversations(self.project, self.store, state=self.root / 'publication-state', env=self.env)
        self.assertEqual(captured['issues'], [])
        self.assertEqual([e['session_id'] for e in captured['entries']], [self.sid])

    def test_claude_completed_turn_advance_exports_child_then_imports_once(self):
        original = self.claude()
        entry = self.capture()['entries'][0]
        result = self.publish(entry)
        child = result['imported'][0]['session_id']
        target = original.with_name(child + '.jsonl')
        with target.open('ab') as out:
            out.write(lines([dict(type='user', uuid=str(uuid.uuid4()), sessionId=child, cwd=str(self.project.root), version='2.1.302', message=dict(role='user', content='Next')), dict(type='assistant', uuid=str(uuid.uuid4()), sessionId=child, cwd=str(self.project.root), version='2.1.302', message=dict(role='assistant', content='Next answer', stop_reason='end_turn'))]))
        captured = self.api.capture_conversations(self.project, self.store, state=self.root / 'publication-state', env=self.env)
        advanced = next(e for e in captured['entries'] if e['session_id'] == child)
        self.assertEqual(self.publish(advanced)['issues'], [])
        self.assertEqual(len(self.publish(advanced)['unchanged']), 1)

    def test_claude_environment_and_request_dependent_payloads_preserved(self):
        self.claude(rows=[dict(type='user',uuid=str(uuid.uuid4()),message=dict(role='user',content='Question')),dict(type='attachment',uuid=str(uuid.uuid4()),attachment=dict(type='environment',snapshot=dict(workingDirectory=str(self.project.root),additionalWorkingDirectories=[]))),dict(type='assistant',uuid=str(uuid.uuid4()),message=dict(role='assistant',content='Answer',stop_reason='end_turn'))])
        entry=self.capture()['entries'][0]
        result=self.publish(entry)
        self.assertEqual(result['issues'],[])
        self.api._preflight(self.project,self.store,entry)

    def test_wrong_manifest_identity_and_legacy_manifest_rejected_without_writes(self):
        self.store.load=lambda sid:dict(schema=1,files=[])
        with self.assertRaises(ValueError):self.api.publish_conversations(self.project,self.store,'bad',state=self.root/'state',env=self.env)
        self.store.load=lambda sid:dict(schema=2,project=dict(project_id='wrong',identity=self.project.identity),conversations=dict(entries=[]))
        with self.assertRaises(ValueError):self.api.publish_conversations(self.project,self.store,'bad',state=self.root/'state',env=self.env)

    def test_codex_publish_never_injects_or_resumes_completed_fork(self):
        self.codex();entry=self.capture()['entries'][0]
        env=dict(self.env,CODEX_HOME=str(self.root/'destination'))
        calls=[]
        class Client:
            def __init__(inner,*args,**kwargs):pass
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def call(inner,method,params):
                if method == 'thread/resume': inner.publication_id = params['threadId']
                if method == 'thread/list': return dict(data=[dict(id=inner.publication_id)], nextCursor=None)
                calls.append((method,params))
                return dict(thread=dict(id=params['threadId']))
        with patch.object(self.api.agents,'CodexClient',Client),patch.object(self.api.agents,'_history',return_value=dict(turns=[dict(id=self.turn,status='completed')],items=[])):
            result=self.publish(entry,env=env)
            again=self.publish(dict(entry,title='New title',updated_at=1234),env=env)
        self.assertEqual(result['issues'],[])
        self.assertEqual(len(again['unchanged']),1)
        child=result['imported'][0]['session_id']
        self.assertEqual([call for call in calls if call[0]=='thread/resume'],[('thread/resume',dict(threadId=child,**self.api.agents._runtime(self.project)))])
        self.assertNotEqual(child,self.sid)
        captured=self.api.capture_conversations(self.project,self.store,state=self.root/'publication-state',env=env)
        self.assertEqual(captured['entries'],[])
        self.assertEqual(captured['issues'],[])

    def test_codex_owned_staging_prefix_advances_without_rewriting_prior_bytes(self):
        path=self.codex();leaf=str(uuid.uuid4())
        child=self.codex(leaf,parent=(path,path.stat().st_size,len(path.read_bytes().splitlines())),rows=[])
        entry=next(e for e in self.capture()['entries'] if e['session_id']==leaf)
        env=dict(self.env,CODEX_HOME=str(self.root/'advance-destination'))
        class Client:
            def __init__(inner,*args,**kwargs):pass
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def call(inner,method,params):
                if method == 'thread/resume': inner.publication_id = params['threadId']
                if method == 'thread/list': return dict(data=[dict(id=inner.publication_id)], nextCursor=None)
                return dict(thread=dict(id=params.get('threadId')))
        turns=[self.turn]
        def history(*args):return dict(turns=[dict(id=turn,status='completed') for turn in turns],items=[])
        with patch.object(self.api.agents,'CodexClient',Client),patch.object(self.api.agents,'_history',side_effect=history):
            first=self.publish(entry,env=env)
            self.assertEqual(first['issues'],[])
            source=next(p for p in Path(env['CODEX_HOME']).rglob('*.jsonl') if p.name.endswith(self.sid+'.jsonl'))
            before=source.read_bytes();inode=source.stat().st_ino
            next_turn=str(uuid.uuid4());turns.append(next_turn)
            rows=[dict(type='event_msg',payload=dict(type='task_started',turn_id=next_turn)),dict(type='event_msg',payload=dict(type='task_complete',turn_id=next_turn))]
            with path.open('ab') as out:out.write(lines([dict(row,ordinal=len(before.splitlines())+i) for i,row in enumerate(rows)]))
            self.codex(leaf,parent=(path,path.stat().st_size,len(path.read_bytes().splitlines())),rows=[])
            advanced=next(e for e in self.capture()['entries'] if e['session_id']==leaf)
            second=self.publish(advanced,env=env)
        self.assertEqual(second['issues'],[])
        self.assertEqual(len(second['imported']),1)
        self.assertEqual(source.stat().st_ino,inode)
        self.assertTrue(source.read_bytes().startswith(before))
        self.assertEqual(source.read_bytes(),path.read_bytes())

    def test_claude_supersedes_only_unchanged_owned_predecessors(self):
        source=self.claude();entry=self.capture()['entries'][0]
        first=self.publish(entry);old=source.with_name(first['imported'][0]['session_id']+'.jsonl')
        user,answer=str(uuid.uuid4()),str(uuid.uuid4())
        with source.open('ab') as out:out.write(lines([dict(type='user',uuid=user,sessionId=self.sid,cwd=str(self.project.root),version='2.1.302',message=dict(role='user',content='Next')),dict(type='assistant',uuid=answer,sessionId=self.sid,cwd=str(self.project.root),version='2.1.302',message=dict(role='assistant',content='Answer',stop_reason='end_turn'))]))
        next_entry=next(e for e in self.capture()['entries'] if e['session_id']==self.sid)
        second=self.publish(next_entry)
        self.assertEqual(second['issues'],[])
        self.assertFalse(old.exists())
        self.assertEqual(len(list((self.root/'publication-state').glob('conversation-imports/*/archived.jsonl'))),1)
        self.assertEqual(len(self.publish(entry)['unchanged']),1)
        current=source.with_name(second['imported'][0]['session_id']+'.jsonl')
        with current.open('ab') as out:out.write(lines([dict(type='user',uuid=str(uuid.uuid4()),sessionId=current.stem,cwd=str(self.project.root),version='2.1.302',message=dict(role='user',content='Unsaved incomplete turn'))]))
        with source.open('ab') as out:out.write(lines([dict(type='assistant',uuid=str(uuid.uuid4()),sessionId=self.sid,cwd=str(self.project.root),version='2.1.302',message=dict(role='assistant',content='Another completed turn',stop_reason='end_turn'))]))
        final=next(e for e in self.capture()['entries'] if e['session_id']==self.sid)
        self.assertEqual(self.publish(final)['issues'],[])
        self.assertTrue(current.exists())

    def test_conflicting_codex_ancestor_identity_refuses_before_new_native_writes(self):
        parent=self.codex();child=str(uuid.uuid4())
        self.codex(child,parent=(parent,parent.stat().st_size,len(parent.read_bytes().splitlines())),rows=[])
        entry=next(e for e in self.capture()['entries'] if e['session_id']==child)
        env=dict(self.env,CODEX_HOME=str(self.root/'foreign-destination'))
        target=Path(env['CODEX_HOME'])/'sessions/2026/10/17'/parent.name
        target.parent.mkdir(parents=True);foreign=parent.read_bytes().replace(b'ORCHID',b'VIOLET');target.write_bytes(foreign)
        with patch.object(self.api.agents,'CodexClient',side_effect=AssertionError('foreign identity must not load')):
            result=self.publish(entry,env=env)
        self.assertEqual(result['imported'],[])
        self.assertIn('diverge',result['issues'][0]['reason'])
        self.assertEqual(target.read_bytes(),foreign)
        self.assertEqual(len(list(Path(env['CODEX_HOME']).rglob('*.jsonl'))),1)

    def test_hidden_native_codex_import_is_an_issue_not_picker_ready_success(self):
        self.codex();entry=self.capture()['entries'][0]
        env=dict(self.env,CODEX_HOME=str(self.root/'hidden-destination'))
        class Client:
            def __init__(inner,*args,**kwargs):pass
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def call(inner,method,params):
                if method == 'thread/resume': inner.publication_id = params['threadId']
                if method=='thread/list':return dict(data=[],nextCursor=None)
                return dict(thread=dict(id=params['threadId']))
        with patch.object(self.api.agents,'CodexClient',Client),patch.object(self.api.agents,'_history',return_value=dict(turns=[dict(id=self.turn,status='completed')],items=[])):
            result=self.publish(entry,env=env)
        self.assertEqual(result['imported'],[])
        self.assertIn('picker',result['issues'][0]['reason'])

    def test_both_agents_publish_into_another_checkout_of_same_origin(self):
        import subprocess
        from devcontainer_setup.project_sync.state import discover_project
        from devcontainer_setup.project_sync.sessions import claude_namespace
        codex=self.codex();claude=self.claude();originals={p:p.read_bytes() for p in (codex,claude)}
        entries=self.capture()['entries']
        root=self.root/'other-checkout'
        subprocess.run(['git','init','-q',str(root)],check=True)
        subprocess.run(['git','-C',str(root),'remote','add','origin',self.project.remote],check=True)
        project=discover_project(root)
        env=dict(self.env,CODEX_HOME=str(self.root/'other-codex'),CLAUDE_CONFIG_DIR=str(self.root/'other-claude'))
        self.store.load=lambda _:dict(schema=2,project=dict(project_id=project.project_id,identity=project.identity),conversations=dict(entries=entries,issues=[]))
        class Client:
            def __init__(inner,*args,**kwargs):pass
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def call(inner,method,params):
                if method=='thread/resume':inner.sid=params['threadId'];return dict(thread=dict(id=inner.sid))
                return dict(data=[dict(id=inner.sid)],nextCursor=None)
        with patch.object(self.api.agents,'CodexClient',Client),patch.object(self.api.agents,'_history',return_value=dict(turns=[dict(id=self.turn,status='completed')],items=[])):
            result=self.api.publish_conversations(project,self.store,'checkpoint',state=self.root/'cross-state',env=env)
        self.assertEqual(result['issues'],[])
        self.assertEqual(len(result['imported']),2)
        for imported in result['imported']:
            if imported['agent']=='codex':
                target=next(Path(env['CODEX_HOME']).rglob('*'+imported['session_id']+'.jsonl'))
                rows=[json.loads(row) for row in target.read_bytes().splitlines()]
                self.assertEqual(rows[0]['payload']['cwd'],str(root))
                self.assertEqual(target.read_bytes().splitlines()[1:],originals[codex].splitlines()[1:])
            else:
                target=Path(env['CLAUDE_CONFIG_DIR'])/'projects'/claude_namespace(root)/(imported['session_id']+'.jsonl')
                rows=[json.loads(row) for row in target.read_bytes().splitlines()]
                self.assertTrue(all(row.get('cwd')==str(root) for row in rows))
                self.assertEqual([row['message'] for row in rows],[row['message'] for row in map(json.loads,originals[claude].splitlines())])
        captured=self.api.capture_conversations(project,self.store,state=self.root/'cross-state',env=env)
        self.assertEqual(captured,dict(schema=1,entries=[],issues=[]))
        for path,original in originals.items():self.assertEqual(path.read_bytes(),original)
