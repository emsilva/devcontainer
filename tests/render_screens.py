"""Draw every round 1 screen for the owner to read line by line (spec, Done means 2).

Run `python3 -m tests.render_screens > RENDER.md`. This script draws nothing itself. `task work` frames come from
`overview.view`, using `tests/test_overview.py`'s fixture. Wizard text comes from `devcontainer_setup.__main__.main`,
run in `tests/test_wizard.py`'s `SetupFromDefaultBranchTests` fixture (a bare repository behind a GitHub URL and a
fake `gh`). The replace questions come from `replacement._Coordinator`, and `Show details` from
`workspace.status_text` on `tests/test_workspace_codespaces.py`'s four states. A script, not a test module: its
name keeps `unittest discover` from collecting it.
"""
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path
import signal
import sys
from unittest.mock import patch

from devcontainer_setup import replacement
from devcontainer_setup.project_sync import overview, workspace
from devcontainer_setup.project_sync.probe import Observation, Remote, Space
from devcontainer_setup.project_sync.standing import Checkout, Launch, Refusal
# Modules only: a TestCase class imported by name would be collected again by any test module importing this one.
from tests import test_overview, test_replacing, test_wizard, test_workspace_codespaces

# One clock for every task work frame: the drawings' day, at the overview fixture's hour (12:00, checked 09:10).
TODAY = datetime(2026, 11, 14, 12, 0)
NOW = TODAY.timestamp()
AT = TODAY.replace(hour=9, minute=10).timestamp()
CS, OLD = test_overview.CS, test_overview.OLD
CHECKING = 'Codespace  checking GitHub…'  # The product's word while GitHub is read, not a cut.
LONG_FOLDER = 'Line one: a folder name too long to fit'
LONG_NAME = 'Line one: a 100-character Codespace name'
LONG_NAMES = (LONG_FOLDER, LONG_NAME)
INSIDE = 'Inside a Codespace'
ONE_ACTION = (INSIDE,)  # The Codespace's own frame offers only Stop (overview.view, in_codespace).
WIZARD_SECONDS = 1200  # Bounds each wizard run: a prompt the fake UI misreads must not loop forever. Sized for a loaded Mac.


def _fixture():
    """`tests/test_overview.py`'s fixture, with its observations moved onto TODAY."""
    case = test_overview.OverviewTests('test_view_is_pure_and_its_output_is_immutable')
    case.setUp()
    shift = AT - case.at

    def moved(observation):
        value = observation.value
        if isinstance(value, tuple) and all(isinstance(space, Space) for space in value):
            value = tuple(space if space.last_used_at is None
                          else replace(space, last_used_at=space.last_used_at + shift) for space in value)
        return replace(observation, value=value, at=None if observation.at is None else observation.at + shift)

    case.remote = Remote(*(moved(getattr(case.remote, field.name)) for field in fields(Remote)))
    case.today, case.now, case.at = TODAY, NOW, AT
    return case


def frame(local, remote):
    return overview.view(local, remote, now=NOW)


def _situation_rows(case):
    local, remote, ok = case.local, case.remote, case.ok
    unpaired = replace(local, pairing=None)
    return [
        ('Setup: not done yet', replace(local, setup='missing'), remote),
        ('Setup: not done yet, nothing paired', replace(local, setup='missing', pairing=None), remote),
        ('Setup: not done yet, with a log', replace(local, setup='missing', logs=True), remote),
        ('Setup: devcontainer.json is not managed', replace(local, setup='unmanaged'), remote),
        ('Setup: devcontainer.json cannot be read', replace(local, setup='unknown'), remote),
        ('Setup: one pull request open', local, replace(remote, setup_prs=ok((7,)))),
        ('Setup: two pull requests open', local, replace(remote, setup_prs=ok((9, 7)))),
        ('Codespace: failed on GitHub', local, case.remote_state('Failed')),
        ('Codespace: setup did not finish validation', replace(local, launch=Launch(CS, False)), remote),
        ('Setup: an unpaired Codespace did not finish validation',
         replace(local, launch=Launch(OLD, False), pairing=None), remote),
        ('Codespace: deleted, confirmed by GitHub', local,
         replace(remote, names=ok(frozenset()), spaces=ok(()))),
        ('Codespace: deleted, found by Recovery', replace(local, deleted_at=AT), case.unavailable()),
        ('Codespace: not listed for the account', local,
         replace(remote, account=ok('mannu_kyndryl'), names=ok(frozenset()), spaces=ok(()))),
        ('Codespace: not listed, account unknown', local,
         replace(remote, account=case.failed(), names=ok(frozenset()), spaces=ok(()))),
        ('Codespace: listed under another repository', local, replace(remote, spaces=ok(()))),
        ('Unpaired: GitHub lists none', unpaired, replace(remote, spaces=ok(()))),
        ('Unpaired: GitHub lists 1', unpaired, remote),
        ('Unpaired: GitHub lists 2', unpaired,
         replace(remote, spaces=ok((Space(CS, 'Available', None), Space(OLD, 'Shutdown', None))))),
        ('Stopped: you stopped it', replace(local, owner_stop_at=AT), case.remote_state('Shutdown')),
        ("Stopped: GitHub's 12-hour limit", replace(local, down_kind='LIFETIME', down_at=AT),
         case.remote_state('Shutdown')),
        ('Stopped: not by you', replace(local, down_kind='OTHER', down_at=AT), case.remote_state('Shutdown')),
        ('Stopped: cause not known', local, case.remote_state('Shutdown')),
        ('Stopped: with a log', replace(local, logs=True), case.remote_state('Shutdown')),
        ('Available', local, remote),
        ('Starting', local, case.remote_state('Starting')),
        ('Stopping', local, case.remote_state('ShuttingDown')),
        ('In another state (Moved)', local, case.remote_state('Moved')),
        ('Cached reading while GitHub does not answer', replace(local, reading=('Available', AT)),
         case.unavailable()),
        ('Checking GitHub (before the answer)', local, None),
        ('GitHub not answering: paired', local,
         replace(case.unavailable(), spaces=case.failed('no answer within 2 s', 'unknown'))),
        ('GitHub not answering: a request failed', local, case.unavailable()),
        ('GitHub not answering: nothing paired', unpaired, case.unavailable()),
        ('Git did not give origin: pairing not read',
         replace(unpaired, origin_unread=True, state=None, repository='', armed=None, keeper='unknown',
                 worker='unknown', problems=('Git remote cannot be read',)),
         replace(remote, names=case.ok(frozenset()), spaces=case.ok(()))),
    ]


def _recovery_rows(case):
    local, remote = case.local, case.remote
    armed = replace(local, armed=True, keeper='running')
    shutdown = case.remote_state('Shutdown')
    later = TODAY.replace(hour=17, minute=45).timestamp()
    tomorrow = datetime(2026, 11, 15, 20, 12).timestamp()
    deleted = replace(remote, names=case.ok(frozenset()), spaces=case.ok(()))
    return [
        ('Recovery 1: arming.json cannot be read',
         replace(local, armed=None, problems=('arming.json cannot be read',)), remote),
        ('Recovery 2: not armed', local, remote),
        ('Recovery 3: not answering', replace(armed, keeper='stale', keeper_heartbeat_age=181), remote),
        ('Recovery 3: not answering, no age', replace(armed, keeper='stale'), remote),
        ('Recovery 4: state unknown', replace(armed, keeper='unknown'), remote),
        ('Recovery 5: stopped, pairing deleted', replace(armed, keeper='stopped'), deleted),
        ('Recovery 6: on hold, Codespace stopped', replace(armed, hold_until=NOW + 3600), shutdown),
        ('Recovery 6: on hold, Codespace available', replace(armed, hold_until=NOW + 3600), remote),
        ('Recovery 7: restarting it now',
         replace(armed, keeper_phase='restarting', down_kind='LIFETIME', down_at=AT), shutdown),
        ('Recovery 8: you stopped it', replace(armed, down_kind='OWNER', down_at=AT), shutdown),
        ('Recovery 9: stopped during your hold',
         replace(armed, hold_at_stop=AT, down_kind='LIFETIME', down_at=AT), shutdown),
        ('Recovery 10: out of restarts',
         replace(armed, restarts_free_at=tomorrow, down_kind='LIFETIME', down_at=AT), shutdown),
        ('Recovery 11: not the 12-hour stop', replace(armed, down_kind='OTHER', down_at=AT), shutdown),
        ('Recovery 12: not restarted yet', replace(armed, down_kind='LIFETIME', down_at=AT), shutdown),
        ('Recovery 13: checking why it stopped', armed, shutdown),
        ('Recovery 14: next 12-hour stop', replace(armed, next_stop_at=later), remote),
        ('Recovery 15: the 12-hour stop was due', replace(armed, next_stop_at=AT), remote),
        ('Recovery 16: next stop not known yet', armed, remote),
        ('Recovery: off until you connect', replace(armed, keeper='stopped'), shutdown),
    ]


def _drawings(case):
    """The six frames drawn in the plan's group 3; 2, 3 and 6 are the busy ones."""
    at = lambda hour, minute: TODAY.replace(hour=hour, minute=minute).timestamp()
    base = replace(case.local, armed=True, keeper='running')
    available = replace(case.remote, spaces=Observation('ok', (Space(CS, 'Available', None),), at(8, 30), None))
    stopped = replace(case.remote, spaces=Observation('ok', (Space(CS, 'Shutdown', None),), at(8, 30), None))
    lifetime = dict(down_kind='LIFETIME', down_at=at(7, 55))
    retired = replace(stopped, spaces=Observation(
        'ok', (Space(CS, 'Shutdown', None), Space(OLD, 'Shutdown', None)), at(8, 30), None))
    return [
        ('Drawing 1: available and armed', replace(base, next_stop_at=at(17, 45), cycle_at=at(8, 29)), available),
        ('Drawing 2 (busy): restarting after the 12-hour stop',
         replace(base, keeper_phase='restarting', cycle_at=at(7, 54), signed_out=('Plaud',),
                 checkout=Checkout(default='main', branch='docs/design-20261023', relation='other-branch'),
                 **lifetime), stopped),
        ('Drawing 3 (busy): a stopped replace, a cleanup and three problems',
         replace(base, keeper='stopped', replacing={'old': CS, 'new': OLD, 'phase': 'ready'}, cleanup=(OLD,),
                 problems=('arming.json cannot be read', 'keeper-runtime.json cannot be read',
                           'signins.json cannot be read'),
                 owner_stop_at=datetime(2026, 11, 13, 7, 55).timestamp()), stopped),
        ('Drawing 4: nothing paired, checkout on another branch',
         replace(case.local, pairing=None, checkout=Checkout(
             default='main', branch='docs/design-ux1-high-5e4c-20261114', relation='other-branch')), available),
        ('Drawing 5: nothing paired, not set up', replace(case.local, pairing=None, setup='missing'), available),
        ('Drawing 6 (busy): out of restarts, sync stopped, a replaced Codespace',
         replace(base, worker='stopped', retired=(OLD,), restarts_free_at=datetime(2026, 11, 15, 20, 12).timestamp(),
                 refusals=tuple(Refusal('codex', f'session-{n:02}', 'refused') for n in range(12)), **lifetime),
         retired),
    ]


def _other_rows(case):
    local = case.local
    checkout = local.checkout
    return [
        ('Sign-ins: two tools', replace(local, signed_out=('Plaud', 'Codex')), case.remote),
        ('Sign-ins: three tools, sync stopped', replace(local, signed_out=('Plaud', 'Codex', 'Claude'),
                                                        worker='stopped'), case.remote),
        ('Sign-ins: two not checked', replace(local, unchecked=('Plaud', 'Codex')), case.remote),
        ('Sign-ins: one signed out, two not checked', replace(local, signed_out=('Plaud',),
                                                              unchecked=('Codex', 'Claude')), case.remote),
        ('History: one conversation refused', replace(local, refusals=(Refusal('codex', 'session-1', 'refused'),)),
         case.remote),
        ('Sign-ins: one signed out, a conversation refused',
         replace(local, signed_out=('Plaud',), refusals=(Refusal('codex', 'session-1', 'refused'),)), case.remote),
        ('History: not synced yet', replace(local, history_pending_since=NOW - 901), case.remote),
        ("Checkout: behind GitHub's main", replace(local, checkout=replace(checkout, relation='behind', behind=2)),
         case.remote),
        ('Checkout: diverged', replace(local, checkout=replace(checkout, relation='diverged')), case.remote),
        ('Checkout: detached HEAD', replace(local, checkout=Checkout(default='main', relation='detached')),
         case.remote),
        ('Generator: upgrade available', local, replace(case.remote, upstream=case.ok('b' * 40))),
        ('Replaced: still on GitHub', replace(local, retired=(OLD,)),
         replace(case.remote, spaces=case.ok((*case.remote.spaces.value, Space(OLD, 'Shutdown', None))))),
    ]


def _long_rows(case):
    local = case.local
    long_name = 'a' * 100
    problem = f'retired/{CS}.json cannot be read'
    branch = 'docs/' + 'b' * 55  # 60 characters
    return [
        (LONG_FOLDER, replace(local, root=Path('/fixture/consult-sm-strata-mainframe-modernisation')), case.remote),
        (LONG_NAME, replace(local, pairing=long_name),
         replace(case.remote, spaces=case.ok((Space(long_name, 'Available', None),)))),
        ('Problem: a long problem', replace(local, problems=(problem,)), case.remote),
        ('Problem: a long problem and two more',
         replace(local, problems=(problem, 'arming.json cannot be read', 'signins.json cannot be read')),
         case.remote),
        ('Checkout: a 60-character branch',
         replace(local, checkout=Checkout(default='main', branch=branch, relation='other-branch')), case.remote),
    ]


def _groups():
    case = _fixture()
    return [
        ('The situation row', _situation_rows(case)),
        ('The Recovery row', _recovery_rows(case)),
        ("The plan's drawings", _drawings(case)),
        ('Other rows', _other_rows(case)),
        ('Long names and texts', _long_rows(case)),
        (INSIDE, [(INSIDE, replace(case.local, in_codespace=True), None)]),
    ]


def situations():
    """Every task work situation drawn in the render file, as (heading, Local, Remote or None)."""
    return [situation for _, group in _groups() for situation in group]


def _measured(lines):
    """Each line behind its width in terminal cells (overview.cells)."""
    return [f'{overview.cells(line):>3}  {line}' if line else '' for line in lines]


def _block(lines):
    return ['```text', *_measured(lines), '```', '']


def _frame_block(name, local, remote):
    shown = frame(local, remote)
    menu = ['Menu: What next?', *('  ' + item for item in (*(action.item for action in shown.actions), 'Exit'))]
    return [f'### {name}', '', *_block([*shown.lines, '', *menu, '', *overview.next_lines(shown)])]


@contextmanager
def _bounded(seconds):
    def expire(signum, stack):
        raise TimeoutError(f'a wizard run took longer than {seconds} s')
    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def _wizard():
    """`SetupFromDefaultBranchTests`' fixture; its fake GitHub creates run 3's 36-character Codespace name."""
    case = test_wizard.SetupFromDefaultBranchTests('test_every_wizard_prompt_is_a_question')
    try:
        with patch.object(test_wizard, 'FAKE_GH', test_wizard.FAKE_GH.replace("'new-space'", repr(CS))):
            case.setUp()
        yield case
    finally:
        case.doCleanups()  # The fixture patches os.environ (HOME, PATH, GIT_SSH_COMMAND): never leak it.


def _run(case, answers):
    """`main()` on a terminal; returns (exit code, prompts as the recording UI saw them, output)."""
    prompts, fixture_ui = [], case.ui

    def ui(given, seen):
        base = fixture_ui(given, seen)

        class Recording(base):
            def note(self, *prompt):
                prompts.append(prompt)
                if len(prompts) > 100:
                    raise RuntimeError('more than 100 prompts: a question is being asked again')

            def multi(self, title, options, selected):
                self.note('multi', title, list(options), list(selected))
                return super().multi(title, options, selected)

            def input(self, title, value=''):
                self.note('input', title, value)
                return super().input(title, value)

            def confirm(self, title, default=True):
                self.note('confirm', title, default)
                if title not in given:  # Enter on the default.
                    return default
                return super().confirm(title, default)

            def choose(self, title, options, selected=None):
                self.note('choose', title, list(options), selected)
                answer = given.get(title)
                if isinstance(answer, list):
                    return answer.pop(0)
                if title not in given:  # Enter on the preselected option.
                    return selected if selected is not None else options[0]
                return super().choose(title, options, selected)
        return Recording

    case.ui = ui
    try:
        with _bounded(WIZARD_SECONDS):
            code = case.run_main(answers)
    finally:
        case.ui = fixture_ui
    output = case.seen['output']
    base = Path(case.tmp.name)
    for form in (str(base.resolve()), str(base)):
        output = output.replace(form, '<fixture>')
    return code, prompts, output


def _prompt_lines(prompts):
    lines = []
    for kind, title, *rest in prompts:
        if kind == 'choose':
            options, selected = rest
            lines += [title, *(('> ' if option == selected else '  ') + option for option in options)]
        elif kind == 'multi':
            options, selected = rest
            lines += [title, *(('[x] ' if option in selected else '[ ] ') + option for option in options)]
        elif kind == 'confirm':
            lines += [title, f"  (yes or no; Enter means {'yes' if rest[0] else 'no'})"]
        else:
            lines += [title, f'  (typed text; Enter keeps {rest[0]!r})']
    return lines


def _wizard_section(name, note, code, prompts, output):
    lines = [f'### {name}', '', note, '', f'Exit code: {code}.', '']
    if prompts:
        lines += ['Prompts, as the recording UI receives them (`>` marks the preselected option):', '',
                  *_block(_prompt_lines(prompts))]
    else:
        lines += ['No prompt was shown.', '']
    lines += ['The screen afterwards: the answer lines and what the wizard printed (stdout and stderr):', '',
              *_block(output.rstrip('\n').splitlines())]
    return lines


def _wizard_sections():
    lines = []
    with _wizard() as case:
        # No dotfiles: an empty repository answer would be asked again forever (group 2's trap).
        answers = dict(case.LAUNCH, **{case.MERGE: True, 'Apply dotfiles inside the container?': False})
        lines += _wizard_section(
            'First setup',
            'A checkout on `main` with no launch history. Answers: the fixture\'s launch run (merge into main, '
            'create a Codespace, 4 cores, 30m), no to dotfiles, yes to the merge, Enter on every other default. '
            "The fixture's home has no Codespace SSH key, which the last two lines report.",
            *_run(case, answers))
        lines += _wizard_section(
            'Rerun with launch history',
            'The same project once the first setup has created and validated its Codespace. The owner answers '
            '`Change answers` at Review, then Enter on every default and `Continue`.',
            *_run(case, {'Review: go ahead with these answers?': ['Change answers', 'Continue']}))
    with _wizard() as case:
        case.checkout_feature()
        lines += _wizard_section(
            'Refused on a branch',
            "A checkout on `feature/local`, whose files GitHub's `main` mostly lacks.", *_run(case, {}))
    with _wizard() as case:
        for number in range(10):
            path = case.root / f'src/module_{number}.py'
            path.parent.mkdir(exist_ok=True)
            path.write_text(f'VALUE = {number}\n')
        case.git('add', 'src')
        case.git('commit', '--quiet', '-m', 'Project work not on GitHub')
        lines += _wizard_section(
            'Refused on main',
            "This Mac's `main` holds the project in ten commits' worth of files that were never pushed; "
            "GitHub's `main` holds only `package.json`.", *_run(case, {}))
    return lines


def _replace_sections():
    coordinator = replacement._Coordinator(
        Path('/fixture/consult-sm-innovate'), Path('/fixture/state'), None, api=None, ui=None, ask=None, env={},
        runner=None, now=None, sleep=None)
    fresh = [
        ('As drawn in the plan', test_replacing.journal(
            old=CS, branch='main', commit='9f2fae2' + '0' * 33,
            resources=dict(machine='standardLinux32gb', cpus=4, idle_timeout_minutes=240,
                           billable_owner='kyndryl-agentic-ai'))),
        ('The widest machine and idle timeout', test_replacing.journal(
            old=CS, branch='main', commit='9f2fae2' + '0' * 33,
            resources=dict(machine='premiumLinux256gb', cpus=32, idle_timeout_minutes=1440,
                           billable_owner='kyndryl-agentic-ai'))),
    ]
    lines = ['### Replace: the question before anything is written', '',
             '`replacement._Coordinator.preview` and its two options; Gum starts on the first.', '']
    for note, value in fresh:
        options = [replacement.NOT_NOW, replacement.REPLACE_IT]
        lines += [f'{note}:', '', *_block([*coordinator.preview(value).splitlines(), *_options(options)])]
    lines += ['### Replace: continue or abandon', '',
              '`replacement._Coordinator.unfinished` for a replace that stopped before the move.', '']
    for note, value in (('A new Codespace was created', test_replacing.journal(old=CS, new=OLD, phase='created')),
                        ('Nothing was created yet', test_replacing.journal(old=CS, new=None, phase='inspected'))):
        title, options = coordinator.unfinished(value)
        lines += [f'{note}:', '', *_block([*title.splitlines(), *_options(options)])]
    return lines


def _options(options):
    return [('> ' if index == 0 else '  ') + option for index, option in enumerate(options)]


def _details_sections():
    case = test_workspace_codespaces.ShowDetailsTests('test_no_status_row_is_wider_than_78_cells')
    lines = []
    try:
        case.setUp()
        for name, value in case.states().items():
            lines += [f'### Show details: {name}', '', *_block(workspace.status_text(value).splitlines())]
    finally:
        case.doCleanups()
    return lines


def render():
    lines = [
        '# Round 1 screens', '',
        'Made by `python3 -m tests.render_screens`. Every line in a block is preceded by its width in terminal '
        'cells (`overview.cells`); `task work` allows 78. Names are run 3\'s: the paired Codespace '
        f'`{CS}` (36 characters), the other `{OLD}` (34), the folder `consult-sm-innovate`. '
        '`task work` frames are drawn at 2026-11-14 12:00 local time, and GitHub was read at 09:10 unless a frame '
        'says otherwise. Wizard paths under the test fixture show as `<fixture>`; commit ids and the times in '
        '`Show details` change from run to run.', '',
        'Each `task work` frame: the lines, the Gum menu (`run` adds `Exit`), then the `Next:` lines printed '
        'without a terminal.',
        '']
    for title, group in _groups():
        lines += [f'## task work: {title}', '']
        for situation in group:
            lines += _frame_block(*situation)
    lines += ['## Setup wizard', '', *_wizard_sections()]
    lines += ['## Starting a replace', '', *_replace_sections()]
    lines += ['## Show details (task work:status)', '', *_details_sections()]
    return '\n'.join(lines).rstrip('\n') + '\n'


if __name__ == '__main__':
    sys.stdout.write(render())
