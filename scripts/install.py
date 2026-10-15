#!/usr/bin/env python3
"""Install the local skill without editing host settings or hook trust."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

NAME = 'ai-scold-review'
MARKER = '.ai-scold-review-install.json'
PACKAGE = Path(__file__).resolve().parents[1]


def _paths(root):
    root = Path(root).expanduser().resolve()
    return root, root / 'skills' / NAME, root / NAME


def _managed(target):
    try:
        return json.loads((target / MARKER).read_text()).get('package') == NAME
    except (OSError, ValueError):
        return False


def install_one(platform, root, timezone_name=None, dry_run=False, data_dir=None):
    if platform not in {'codex', 'claude-code'}:
        raise ValueError('unsupported platform')
    root, target, data = _paths(root)
    if target.is_symlink():
        raise ValueError('refusing to replace a symlinked skill directory')
    if target.exists() and not _managed(target):
        raise ValueError('existing skill is not managed by this installer')
    if target.exists():
        existing_host = json.loads((target / 'host.json').read_text())
        data = Path(existing_host['data_dir']).resolve()
        if data_dir is not None and Path(data_dir).expanduser().resolve() != data:
            raise ValueError('existing installation uses a different data directory')
    elif data_dir is not None:
        data = Path(data_dir).expanduser().resolve()
    result = {'platform': platform, 'action': 'update' if target.exists() else 'install',
              'skill_dir': str(target), 'data_dir': str(data), 'source_root': str(root),
              'dry_run': dry_run, 'schedule_enabled': False}
    if dry_run:
        return result
    required = ['SKILL.md', 'scripts/scold.py', 'scripts/sources.py',
                'scripts/context.py', 'scripts/reasons.py',
                'scripts/identity.py', 'scripts/sharing.py']
    if any(not (PACKAGE / item).is_file() for item in required):
        raise ValueError('package is incomplete')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.ai-scold-review-', dir=target.parent))
    backup = None
    try:
        for filename in ['SKILL.md']:
            shutil.copy2(PACKAGE / filename, stage / filename)
        for directory in ['scripts', 'references', 'agents']:
            source = PACKAGE / directory
            if source.exists():
                shutil.copytree(source, stage / directory,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        host = {'platform': platform, 'data_dir': str(data), 'source_root': str(root)}
        (stage / 'host.json').write_text(json.dumps(host, ensure_ascii=False, indent=2) + '\n')
        (stage / MARKER).write_text(json.dumps({'package': NAME, 'version': '0.3.0'}) + '\n')
        command = [sys.executable, str(stage / 'scripts/scold.py'), 'init',
                   '--data-dir', str(data), '--platform', platform,
                   '--source-root', str(root)]
        if timezone_name is not None:
            command.extend(['--timezone', timezone_name])
        proc = subprocess.run(command, capture_output=True, text=True)
        if proc.returncode:
            raise ValueError('ledger initialization failed: ' + proc.stderr.strip()[:500])
        result['initialization'] = json.loads(proc.stdout)
        if target.exists():
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = data / 'install-backups' / stamp
            backup.parent.mkdir(parents=True, exist_ok=True)
            target.rename(backup)
        try:
            stage.rename(target)
        except Exception:
            if backup is not None:
                backup.rename(target)
            raise
        result['backup'] = str(backup) if backup else None
        return result
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def uninstall_one(platform, root, dry_run=False):
    root, target, data = _paths(root)
    if target.is_symlink() or (target.exists() and not _managed(target)):
        raise ValueError('refusing to remove an unmanaged skill directory')
    if target.exists():
        data = Path(json.loads((target / 'host.json').read_text())['data_dir'])
    result = {'platform': platform, 'action': 'uninstall', 'skill_dir': str(target),
              'data_retained': str(data), 'dry_run': dry_run}
    if not dry_run and target.exists():
        shutil.rmtree(target)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'uninstall'])
    parser.add_argument('--platform', choices=['codex', 'claude-code', 'both'], required=True)
    parser.add_argument('--codex-root', default=os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))
    parser.add_argument('--claude-root', default=os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home()/'.claude')))
    parser.add_argument('--timezone', help='first install defaults to Asia/Shanghai; updates preserve the configured timezone')
    parser.add_argument('--data-base', help='optional persistent data base, with one subdirectory per platform')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    platforms = ['codex', 'claude-code'] if args.platform == 'both' else [args.platform]
    results = []
    try:
        for platform in platforms:
            root = args.codex_root if platform == 'codex' else args.claude_root
            if args.action == 'install':
                data_dir = Path(args.data_base) / platform if args.data_base else None
                results.append(install_one(platform, root, args.timezone, args.dry_run, data_dir))
            else:
                results.append(uninstall_one(platform, root, args.dry_run))
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': str(exc), 'completed': results}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({'results': results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
