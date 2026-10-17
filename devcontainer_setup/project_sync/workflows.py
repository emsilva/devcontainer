"""Project-scoped orchestration for the interactive daily launcher."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import subprocess
import uuid
from . import agents, handoff, ownership, scratch, sessions
from .state import atomic_write, host_id, read_json
from .transport import CodespaceConnection, clean_env, _name


class Workflows:
    def __init__(self, project, state):
        self.project, self.state = project, Path(state)
        self.is_remote = os.environ.get('CODESPACES') == 'true'

    def binding(self):
        try:
            read_json(self.state / scratch.CONFIG)
        except FileNotFoundError:
            return None
        return scratch._config(self.project.root, self.state)

    def preferences(self):
        try:
            value = read_json(self.state / 'work.json')
        except FileNotFoundError:
            return {}
        self._selection(value['agent'], value['session_id'], value['location'])
        return {key:value[key] for key in ('agent', 'session_id', 'location')}

    @staticmethod
    def _selection(agent, session_id, location):
        sessions.session_uuid(session_id)
        if agent not in ('codex', 'claude') or location not in ('mac', 'codespace'):
            raise ValueError('Choose a supported agent and machine')

    def remember(self, agent, session_id, location):
        self._selection(agent, session_id, location)
        atomic_write(self.state / 'work.json', json.dumps(dict(agent=agent,session_id=session_id,location=location)).encode()+b'\n')

    def spaces(self):
        result = subprocess.run(['gh','api','--method','GET','user/codespaces?per_page=100'],capture_output=True,text=True,env=clean_env(),timeout=30)
        if result.returncode:
            raise ValueError('Cannot list Codespaces; check GitHub authentication')
        if len(result.stdout)>2*1024*1024:
            raise ValueError('Codespace inventory exceeds limit')
        rows=json.loads(result.stdout).get('codespaces',[])
        branch=sessions._git(self.project)['branch']
        return [row for row in rows if row.get('repository',{}).get('full_name','').lower() == self.project.identity.removeprefix('github.com/') and row.get('git_status',{}).get('ref') == branch and _name(row['name'])]

    def space(self):
        binding=self.binding()
        if not binding:
            return None
        return next((row for row in self.spaces() if row['name']==binding['codespace']),None)

    def roots(self, name):
        _name(name)
        # Read-only discovery through the owned transport; never starts a space.
        code = '''import pathlib,subprocess,json
roots=[]
for path in list(pathlib.Path('/workspaces').iterdir())[:100]:
 if not path.is_dir():continue
 p=subprocess.run(['git','-C',str(path),'rev-parse','--show-toplevel'],capture_output=True,text=True)
 if p.returncode:continue
 root=p.stdout.strip()
 r=subprocess.run(['git','-C',root,'remote','get-url','origin'],capture_output=True,text=True)
 roots.append([root,r.stdout.strip()])
print(json.dumps(roots))'''
        from .state import _identity
        with CodespaceConnection(name,state=self.state) as connection:
            connection.accept_new=True  # Explicit pairing discovery TOFU boundary.
            raw=connection.python(code)
        if len(raw)>65536: raise ValueError('Checkout inventory exceeds limit')
        result=[]
        for root, remote in json.loads(raw):
            try:
                if _identity(remote)==self.project.identity and root.startswith('/') and '..' not in Path(root).parts and not any(ord(c)<32 for c in root): result.append(root)
            except ValueError: pass
        return sorted(set(result))

    def pair(self,name,remote_root=None):
        _name(name)
        roots=self.roots(name)
        if remote_root is None:
            if len(roots)!=1: raise ValueError('Choose one verified remote checkout')
            remote_root=roots[0]
        if remote_root not in roots: raise ValueError('Remote checkout does not match project identity')
        return scratch.configure(self.project.root,self.state,name,remote_root)

    def start_codespace(self, name=None):
        config=self.binding()
        if name is None and not config: raise ValueError('Choose or pair a Codespace first')
        name = _name(name or config['codespace'])
        result=subprocess.run(['gh','api','--method','POST','user/codespaces/'+name+'/start'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=clean_env(),timeout=120)
        if result.returncode: raise ValueError('Codespace start failed; check GitHub authentication and retry')

    def inventory(self,remote=False):
        if not remote: return handoff.LocalEndpoint(self.project,self.state).call('inventory')
        config=self.binding()
        if not config: raise ValueError('Pair a Codespace first')
        with CodespaceConnection(config['codespace'],state=self.state) as connection:
            return handoff.RemoteEndpoint(connection,config['remote_root'],self.project.identity).call('inventory')

    def ensure_sync(self):
        return scratch.start(self.project.root,self.state)

    def sync_status(self):
        return scratch.inspect_sync(self.project.root,self.state)

    def stop_sync(self):
        return scratch.stop(self.state)

    def sync_ready(self):
        scratch.flush(self.project.root,self.state)

    @staticmethod
    def _check_git(left,right):
        if (not left.get('head') or not left.get('branch') or any(left.get(k)!=right.get(k) for k in ('branch','head')) or left.get('status') != '' or right.get('status') != ''):
            raise ValueError('Git branch, HEAD or dirty state differs. Commit or preserve changes and reconcile both checkouts manually before switching; Git was not changed.')

    def continue_session(self,agent,session_id,destination):
        self._selection(agent,session_id,destination)
        if self.is_remote and destination=='mac': raise ValueError('Use task work on your Mac to return custody')
        local=self.inventory(); local_location='codespace' if self.is_remote else 'mac'
        target=local if destination==local_location else self.inventory(remote=True)
        row=next((r for r in target['sessions'] if r['agent']==agent and r['session_id']==session_id),None)
        owner=row.get('ownership',{}) if row else {}
        if owner.get('status')=='idle' and owner.get('host')==target['host']:
            if destination != local_location:
                self._check_git(local['git'],target['git']); self.sync_ready()
        else:
            if self.is_remote: raise ValueError('Local conversation needs explicit adoption or writer recovery')
            remote=target if destination=='codespace' else self.inventory(remote=True)
            source=local if destination=='codespace' else remote
            candidate=next((r for r in source['sessions'] if r['agent']==agent and r['session_id']==session_id),None)
            status=candidate.get('ownership',{}) if candidate else {}
            if status.get('status') not in ('idle','transferring','transferred'):
                raise ValueError('Conversation has an active or uncertain writer; stop it or explicitly adopt locally after inspection')
            self._check_git(local['git'],remote['git']); self.sync_ready()
            handoff.handoff(self.project,agent,session_id,destination,self.state)
        self.remember(agent,session_id,destination)
        if destination==local_location:
            return agents.launch(self.project,agent,session_id,state=self.state,host=local['host'])
        return self._launch_remote(agent,session_id)

    def _launch_remote(self,agent,session_id):
        config=self.binding()
        # Generated package is self-contained; invoke its CLI in the checkout.
        command='cd '+shlex.quote(config['remote_root'])+' && { test -f .devcontainer/scripts/project-sync.sh || { printf \'Update project devcontainer helpers before continuing; custody is retained.\\n\'; exit 1; }; } && exec bash .devcontainer/scripts/project-sync.sh agent '+shlex.quote(agent)+' --session '+shlex.quote(session_id)
        with CodespaceConnection(config['codespace'],state=self.state) as connection:
            path=self.state/'ssh-interactive-config'; connection.ssh_config(path)
            return subprocess.call(['ssh','-F',str(path),'-tt','project-sync',command],env=clean_env())

    def new_session(self,agent):
        if agent not in ('codex','claude'): raise ValueError('Unsupported agent')
        host=host_id(); new=agent=='claude'
        sid=str(uuid.uuid4()) if new else agents.create_codex_session(self.project,state=self.state,host=host)
        self.remember(agent,sid,'codespace' if self.is_remote else 'mac')
        return agents.launch(self.project,agent,sid,state=self.state,host=host,new=new),sid

    def adopt(self,agent,session_id):
        self._selection(agent,session_id,'mac')
        home=sessions.native_home(agent); host=host_id()
        sessions.export_snapshot(self.project,agent,session_id,home,source_host=host,generation=0,state=self.state,agent_version=handoff._agent_version(agent))
        ownership.adopt_local(self.state,agent,session_id,host,native_paths=agents.native_paths(self.project,agent,session_id,home,state=self.state))
