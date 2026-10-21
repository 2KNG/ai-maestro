#!/usr/bin/env python3
"""`maestro setup`: local orchestration setup with optional remote hub.

Run by install.sh / install.ps1 at the end of installation, by hand, or by an
AI agent on the user's behalf (see <AI_SETUP_ONBOARDING> in SKILL.md/README.md).

  1. Core agent CLIs  [x] Antigravity  [x] OpenAI Codex CLI  [x] Claude Code CLI
  2. Orca install check, CLI shim and background service link
  3. Optional remote hub address -> ~/.config/ai-maestro/remote.json -> `maestro remote up`

Existing ~/.codex/config.toml and ~/.claude/settings.json are snapshotted before
any CLI installation and restored byte-for-byte if an installer touched them.
Nothing is downloaded and executed from the network; CLIs come from npm and
desktop apps from their official release pages.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

import orca_bridge
import remote_config

STEP1_PROMPT = ('오르카(Orca) 및 AI Maestro 구동에 필요한 기본 CLI [Antigravity / Codex / Claude] '
                '(기본 전체 선택)를 먼저 확인하고 설치할까요?')
STEP2_PROMPT = '이어서 오르카(Orca)를 설치하고 AI Maestro와 연동할까요?'
STEP3_PROMPT = ("[선택] 원격 관제 허브 서버 주소(도메인)나 IP를 입력해 주세요 "
                "(Enter를 누르면 로컬 오케스트레이터 모드로 완료; 나중에 연결할 수 있습니다):")

ORCA_RELEASES = 'https://github.com/stablyai/orca/releases/latest'
CLIS = [
    {'key': 'agy', 'name': 'Antigravity', 'commands': ('agy', 'antigravity'), 'npm': None,
     'manual': 'https://antigravity.google/download 에서 Antigravity를 설치하면 `agy` CLI가 함께 설치됩니다.'},
    {'key': 'codex', 'name': 'OpenAI Codex CLI', 'commands': ('codex',), 'npm': '@openai/codex',
     'manual': 'npm install -g @openai/codex'},
    {'key': 'claude', 'name': 'Claude Code CLI', 'commands': ('claude',), 'npm': '@anthropic-ai/claude-code',
     'manual': 'npm install -g @anthropic-ai/claude-code'},
]


def protected_configs():
    codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    claude_home = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    return [codex_home / 'config.toml', claude_home / 'settings.json']


def snapshot_configs():
    snap = {}
    for path in protected_configs():
        try:
            snap[path] = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        except OSError:
            snap[path] = None
    return snap


def restore_configs(snap):
    """Put back any protected config an installer changed or removed. Returns restored paths."""
    restored = []
    for path, original in snap.items():
        if original is None:
            continue
        try:
            current = path.read_bytes() if path.is_file() else None
        except OSError:
            current = None
        if current != original:
            tmp = path.with_name(path.name + '.maestro-restore.tmp')
            tmp.write_bytes(original)
            os.replace(tmp, path)
            restored.append(str(path))
    return restored


def find_cli(cli):
    for name in cli['commands']:
        found = shutil.which(name)
        if found:
            return found
    return None


class Wizard:
    def __init__(self, args, out=None, ask=None):
        self.args = args
        self.out = out or print
        self.interactive = not args.yes and not args.check_only and (ask is not None or sys.stdin.isatty())
        self._ask = ask or input
        self.report = {}

    def ask(self, prompt, default=''):
        if not self.interactive:
            return default
        try:
            answer = self._ask(prompt + ' ').strip()
        except EOFError:
            return default
        return answer or default

    def confirm(self, prompt, default=True):
        if self.args.check_only:
            return False
        answer = self.ask(f"{prompt} [{'Y/n' if default else 'y/N'}]", 'y' if default else 'n')
        return answer.lower() in ('y', 'yes', 'ㅇ', '네', '예', '응')

    # Step 1 ---------------------------------------------------------------
    def step_clis(self):
        self.out('\n[1/3] 기본 에이전트 CLI')
        selected = {c['key'] for c in CLIS}
        if self.args.cli is not None:
            wanted = {x.strip().lower() for x in self.args.cli.split(',') if x.strip()}
            selected = {c['key'] for c in CLIS if c['key'] in wanted or c['name'].lower() in wanted}
        for i, cli in enumerate(CLIS, 1):
            path = find_cli(cli)
            state = f'설치됨 ({path})' if path else '미설치'
            self.out(f"  {i}. [{'x' if cli['key'] in selected else ' '}] {cli['name']:<18} {state}")
        if self.args.skip_cli:
            self.report['clis'] = 'skipped'
            return
        if self.interactive and self.args.cli is None:
            answer = self.ask(f'{STEP1_PROMPT}\n  [Y/n 또는 설치할 번호만 입력, 예: 2,3]', 'y').lower()
            if answer in ('n', 'no', '아니', '아니오'):
                self.report['clis'] = 'skipped'
                return
            if answer not in ('y', 'yes', 'ㅇ', '네', '예', '응'):
                picks = {x.strip() for x in answer.replace(' ', ',').split(',') if x.strip()}
                selected = {c['key'] for i, c in enumerate(CLIS, 1) if str(i) in picks or c['key'] in picks}
        elif not self.args.yes:
            self.out(f'  {STEP1_PROMPT}')
            self.out('  (비대화형 확인 모드: 설치하지 않음. 설치하려면 --yes)')
        results = {}
        snap = snapshot_configs()
        try:
            for cli in CLIS:
                if cli['key'] not in selected:
                    continue
                path = find_cli(cli)
                if path:
                    results[cli['key']] = 'installed'
                    continue
                if self.args.check_only or not (self.args.yes or self.interactive):
                    results[cli['key']] = 'missing'
                    continue
                results[cli['key']] = self.install_cli(cli)
        finally:
            restored = restore_configs(snap)
        for path in restored:
            self.out(f'  ! 설치 중 변경된 기존 설정을 원래대로 복원했습니다: {path}')
        for path in protected_configs():
            if snap.get(path) is not None:
                self.out(f'  ✓ 기존 설정 보존: {path}')
        self.report['clis'] = results

    def install_cli(self, cli):
        if not cli['npm']:
            self.out(f"  → {cli['name']}: {cli['manual']}")
            return 'manual'
        npm = shutil.which('npm')
        if not npm:
            self.out(f"  → {cli['name']}: Node.js(npm)가 필요합니다. 설치 후 `{cli['manual']}`")
            return 'manual'
        if self.args.dry_run:
            self.out(f"  (dry-run) {npm} install -g {cli['npm']}")
            return 'dry-run'
        self.out(f"  설치 중: npm install -g {cli['npm']}")
        try:
            proc = subprocess.run([npm, 'install', '-g', cli['npm']], timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            self.out(f"  ✗ {cli['name']} 설치 실패: {type(exc).__name__}")
            return 'failed'
        ok = proc.returncode == 0 and find_cli(cli)
        self.out(f"  {'✓' if ok else '✗'} {cli['name']} {'설치 완료' if ok else '설치 실패 — ' + cli['manual']}")
        return 'installed' if ok else 'failed'

    # Step 2 ---------------------------------------------------------------
    def step_orca(self):
        self.out('\n[2/3] 오르카(Orca) 설치 · 연동')
        if self.args.skip_orca:
            self.report['orca'] = 'skipped'
            return
        if self.interactive and not self.confirm(STEP2_PROMPT):
            self.report['orca'] = 'skipped'
            return
        if not self.interactive:
            self.out(f'  {STEP2_PROMPT}')
        system = platform.system()
        orca = orca_bridge.discover(system)
        if not orca:
            self.out(f'  Orca가 설치되어 있지 않습니다. {ORCA_RELEASES} 에서 OS에 맞는 설치 파일을 받은 뒤 '
                     '`maestro setup --skip-cli`를 다시 실행하세요.')
            if system == 'Linux':
                self.out('  (Linux: /usr/bin/orca 화면낭독기와 구분하기 위해 `orca-ide` 명령을 사용합니다.)')
            self.report['orca'] = 'missing'
            return
        self.out(f'  ✓ Orca CLI: {orca}')
        self.report['orca'] = {'cli': orca, 'shim': self.ensure_shim(orca, system),
                               'service': self.link_service(system)}
        snap = orca_bridge.snapshot(max_age=0, orca=orca)
        self.out(f'  ✓ {orca_bridge.summary_line(snap)}')
        self.out('    maestro orca-list / orca-send / orca-read / orca-worktrees 로 Orca 터미널을 제어할 수 있습니다.')
        self.report['orca']['runtime'] = 'running' if snap.get('running') else 'stopped'

    def ensure_shim(self, orca, system):
        """Linux only: a user `orca` shim so `orca ...` never reaches the screen reader."""
        if system != 'Linux':
            return 'native'
        current = shutil.which('orca')
        if current and not current.startswith('/usr/'):
            return current
        shim = Path.home() / '.local' / 'bin' / 'orca'
        if shim.exists() or shim.is_symlink():
            return str(shim)
        if self.args.dry_run or self.args.check_only:
            return 'would-create'
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(f'#!/usr/bin/env bash\n# Managed by ai-maestro setup: Orca CLI shim\nexec {shlex.quote(orca)} "$@"\n')
        shim.chmod(0o755)
        self.out(f'  ✓ CLI shim 생성: {shim} → {orca}')
        return str(shim)

    def link_service(self, system):
        if system == 'Linux':
            probe = _run(['systemctl', '--user', 'show', 'orca-remote.service', '--property=LoadState,ActiveState'])
            if probe and 'LoadState=loaded' in probe:
                active = 'ActiveState=active' in probe
                if not (self.args.dry_run or self.args.check_only):
                    remote_config.save({'orca_service': 'orca-remote.service'})
                self.out(f"  ✓ orca-remote.service 연동 ({'실행 중' if active else '중지됨'}) — "
                         'maestro remote가 이 서비스를 재사용합니다')
                return 'orca-remote.service'
            self.out('  · orca-remote.service 없음 → 3단계 `maestro remote up`이 노드별 Orca 서비스를 등록합니다')
            return 'deferred'
        kind = 'LaunchAgent' if system == 'Darwin' else 'Windows 예약 작업(Scheduled Task)'
        self.out(f'  · 백그라운드 실행: 3단계 `maestro remote up`이 Orca {kind}을 등록합니다')
        return 'deferred'

    # Step 3 ---------------------------------------------------------------
    def step_hub(self):
        self.out('\n[3/3] 선택 사항: 원격 관제 허브 연결')
        saved = remote_config.resolve()
        address = self.args.hub
        if not address and self.interactive and not self.args.skip_remote:
            hint = f" (현재: {saved['hub']}, Enter=유지)" if saved.get('hub') else ' (Enter=건너뛰기)'
            address = self.ask(STEP3_PROMPT + hint, '')
            if not address and saved.get('hub') and self.confirm('저장된 허브로 원격 노드를 기동할까요?', False):
                address = saved['hub']
        elif not address and not self.args.skip_remote:
            self.out(f'  {STEP3_PROMPT}')
        if not address or self.args.skip_remote:
            current = saved.get('hub')
            self.out(f'  · 건너뜀{f" (저장된 허브: {current})" if current else ""}. 로컬 오케스트레이터 모드로 진행합니다. 나중에 연결: maestro remote up --hub <서버주소>')
            self.report['hub'] = 'skipped'
            return
        try:
            updates = remote_config.apply_server(address, self.args.ssh_user, saved)
        except ValueError as exc:
            self.out(f'  ✗ 주소 형식 오류: {exc}')
            self.report['hub'] = 'invalid'
            return
        settings = {**saved, **updates}
        if self.args.registry_url:
            updates['registry_url'] = settings['registry_url'] = self.args.registry_url
        if not settings.get('registry_url') and self.interactive:
            domain = self.ask('  IP 주소는 TLS 인증서가 없어 wss 페어링에 HTTPS 도메인이 필요합니다. '
                              '허브 도메인을 입력하세요 (Enter=건너뛰기):', '')
            try:
                origin = remote_config.normalize_server(domain).get('registry_url') if domain else None
            except ValueError:
                origin = None
                self.out('  ✗ 도메인 형식 오류 — 레지스트리 주소 없이 진행합니다')
            if origin:
                updates['registry_url'] = settings['registry_url'] = origin
        identity = self.args.identity_file or settings.get('identity_file')
        if not identity and self.interactive:
            identity = self.ask('  허브 SSH 개인키 경로 (Enter=ssh 기본키/agent 사용):', '')
        if identity:
            updates['identity_file'] = settings['identity_file'] = str(Path(identity).expanduser())
        if self.args.node_id:
            updates['node_id'] = settings['node_id'] = self.args.node_id
        import maestro_remote
        node = settings.get('node_id') or maestro_remote.default_node_id()
        if not (self.args.dry_run or self.args.check_only):
            path = remote_config.save(updates)
            self.out(f'  ✓ 저장: {path}')
        self.out(f"  허브 SSH: {settings['hub']} · 레지스트리: {settings.get('registry_url') or '(미설정)'} · 노드: {node}")
        if settings.get('registry_url'):
            host = settings['registry_url'].split('://', 1)[1]
            self.out(f'  페어링 엔드포인트: wss://{host}/orca/{node}/ · 위젯: https://{host}/orca/{node}-maestro/')
        self.report['hub'] = {'hub': settings['hub'], 'registry_url': settings.get('registry_url'), 'node_id': node}
        if self.args.no_remote_up or self.args.check_only:
            self.out(f'  다음 단계: maestro remote up --hub {address}')
            return
        if self.interactive and not self.confirm('지금 `maestro remote up`으로 터널·페어링을 연결할까요?'):
            self.out(f'  다음 단계: maestro remote up --hub {address}')
            return
        argv = ['up', '--node-id', node, '--hub', settings['hub']]
        for flag, key in (('--registry-url', 'registry_url'), ('--identity-file', 'identity_file')):
            if settings.get(key):
                argv += [flag, settings[key]]
        if self.args.dry_run:
            argv.append('--dry-run')
        self.out(f"  실행: maestro remote {' '.join(argv)}")
        self.report['remote_up'] = maestro_remote.main(argv)

    def run(self):
        self.out('ai-maestro 설정 마법사 (Antigravity · Codex · Claude · 로컬 위젯 · 선택적 Orca/원격 허브)')
        self.step_clis()
        self.step_orca()
        self.step_hub()
        if self.report.get('hub') == 'skipped':
            self.out('\nLocal orchestrator mode ready. Antigravity · Codex · Claude 오케스트레이션과 로컬 위젯(127.0.0.1:18765)을 사용할 수 있습니다.')
            if isinstance(self.report.get('orca'), dict):
                self.out('Orca CLI 연동이 확인되었습니다. 로컬 Orca 서비스는 별도로 시작할 수 있습니다 (127.0.0.1:6768).')
            self.out('원격 허브는 나중에 `maestro remote up --hub <서버주소>`로 연결할 수 있습니다.')
        else:
            self.out('\n완료. 상태 확인: maestro remote status · maestro orca-list · ai-usage')
        return 0 if self.report.get('remote_up', 0) == 0 else 1


def _run(argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def build_parser():
    parser = argparse.ArgumentParser(prog='maestro setup', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--yes', '-y', action='store_true', help='Accept defaults without prompting (installs selected CLIs)')
    parser.add_argument('--check-only', action='store_true', help='Report only; install and save nothing')
    parser.add_argument('--dry-run', action='store_true', help='Show what would change')
    parser.add_argument('--cli', help='Comma list of CLIs to install: agy,codex,claude (default: all)')
    parser.add_argument('--skip-cli', action='store_true')
    parser.add_argument('--skip-orca', action='store_true')
    parser.add_argument('--skip-remote', action='store_true')
    parser.add_argument('--hub', help='Remote hub server: domain, IP or user@host')
    parser.add_argument('--ssh-user', help='SSH user when --hub has none (default ubuntu)')
    parser.add_argument('--registry-url', help='Hub HTTPS origin when --hub is an IP')
    parser.add_argument('--identity-file', help='SSH private key for the hub')
    parser.add_argument('--node-id', help='Name of this machine in the fleet (default: hostname)')
    parser.add_argument('--no-remote-up', action='store_true', help='Save the hub but do not start services')
    return parser


def main(argv=None, ask=None, out=None):
    args = build_parser().parse_args(argv)
    return Wizard(args, out=out, ask=ask).run()


if __name__ == '__main__':
    raise SystemExit(main())
