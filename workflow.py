#!/usr/bin/env python3
"""Render a portable workflow into an actual AGENTS entry; stdlib only."""
import argparse
import difflib
import hashlib
import json
import os
import re
import socket
import stat
import sys
import tempfile
from pathlib import Path

BEGIN = '<!-- assistant-workflow:begin -->'
END = '<!-- assistant-workflow:end -->'
SOURCE = Path(__file__).resolve().parent


class WorkflowError(Exception):
    pass


def digest(value):
    data = value.encode('utf-8') if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def read_text(path):
    return path.read_bytes().decode('utf-8')


def read_json(path):
    try:
        value = json.loads(read_text(path))
    except (ValueError, UnicodeError) as exc:
        raise WorkflowError('Invalid local JSON: {}'.format(path)) from exc
    if not isinstance(value, dict):
        raise WorkflowError('Expected a JSON object: {}'.format(path))
    return value


def source_rules(source=SOURCE):
    version = read_text(source / 'VERSION').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise WorkflowError('VERSION must contain major.minor.patch')
    rules = read_text(source / 'rules' / 'COMMON.md').strip()
    if not rules or BEGIN in rules or END in rules:
        raise WorkflowError('Invalid or empty shared rules')
    return version, rules, digest(rules)


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise WorkflowError('Invalid recorded version')
    return tuple(int(part) for part in value.split('.'))


def active_entry(root):
    override = root / 'AGENTS.override.md'
    regular = root / 'AGENTS.md'
    if override.exists() and not read_text(override).strip() and regular.exists() and read_text(regular).strip():
        raise WorkflowError('Empty override would hide existing AGENTS rules; merge explicitly first')
    return override if override.exists() else regular


def managed_span(text):
    starts, ends = text.count(BEGIN), text.count(END)
    if starts == 0 and ends == 0:
        return None
    if starts != 1 or ends != 1:
        raise WorkflowError('Duplicate or incomplete managed markers; merge manually')
    start, end = text.index(BEGIN), text.index(END) + len(END)
    if start >= end - len(END):
        raise WorkflowError('Managed markers are out of order')
    return start, end


def scalar(value, field, identifier=False):
    if not isinstance(value, str) or not value or len(value) > 200:
        raise WorkflowError('Invalid local {}'.format(field))
    if any(ord(c) < 32 for c in value) or BEGIN in value or END in value or '`' in value:
        raise WorkflowError('Unsafe local {}'.format(field))
    if identifier and not re.fullmatch(r'[A-Za-z0-9_.-]+', value):
        raise WorkflowError('Invalid identifier in {}'.format(field))
    return value


def local_config(root, source):
    local_path = root / '.assistant-workflow' / 'local.json'
    cfg = read_json(local_path) if local_path.exists() else {}
    defaults = {
        'schema_version': 1,
        'device_id': re.sub(r'[^A-Za-z0-9_.-]', '-', socket.gethostname()) or 'local-device',
        'execution_model': 'gpt-6.1-sol',
        'execution_effort': 'xhigh',
        'language': '中文',
        'main_thread_id': None,
        'host_id': None,
        'timezone': None,
        'scheduler_owner': False,
        'capabilities': {},
    }
    defaults.update(cfg)
    defaults['workspace_root'] = str(root)
    defaults['source_directory'] = str(source.resolve())
    for key in ('device_id', 'execution_model', 'execution_effort'):
        scalar(defaults[key], key, identifier=True)
    scalar(defaults['language'], 'language')
    for key in ('workspace_root', 'source_directory'):
        scalar(defaults[key], key)
    return defaults


def render_block(root, source, version, rules, rules_hash, cfg):
    replacements = {
        '{{EXECUTION_MODEL}}': cfg['execution_model'],
        '{{EXECUTION_EFFORT}}': cfg['execution_effort'],
        '{{LANGUAGE}}': cfg['language'],
    }
    for token, value in replacements.items():
        rules = rules.replace(token, value)
    if re.search(r'\{\{[A-Z_]+\}\}', rules):
        raise WorkflowError('Unknown rule placeholder')
    binding = (
        '\n\n## 本机配置定位\n\n'
        '- 工作目录以本会话真实 cwd 为准。规则包源目录、设备标识、线程、实际能力和调度归属'
        '由执行对话在本机 `.assistant-workflow/local.json` 中按需读取；不要把这些值复制到本文件。\n'
        '- 共同规则的生成源是本机配置所指向的 rules/COMMON.md；改进时同时维护 VERSION 和 CHANGELOG.md。\n'
        '- `.assistant-workflow/local.md` 是私有补充资料，只在相关任务中由执行者按需读取，'
        '并非自动注入的共同指令；不要将私密内容渲染或粘贴进可提交的 AGENTS。\n'
        '- 安装只证明文件已生成，不能证明权限、模型可用性或自动化已经生效。\n'
    )
    return '{}\n<!-- version: {}; rules-sha256: {} -->\n\n{}{}{}'.format(
        BEGIN, version, rules_hash, rules, binding, END)


def make_plan(workspace, source=SOURCE, max_bytes=32768):
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise WorkflowError('Workspace must be an existing directory')
    source = Path(source).resolve()
    if (root / '.assistant-workflow').is_symlink():
        raise WorkflowError('Refusing a symlinked local state directory')
    version, rules, rules_hash = source_rules(source)
    entry = active_entry(root)
    if entry.is_symlink():
        raise WorkflowError('Refusing to replace a symlinked instruction entry')
    before = read_text(entry) if entry.exists() else ''
    span = managed_span(before)
    state_path = root / '.assistant-workflow' / 'state.json'
    state = read_json(state_path) if state_path.exists() else None
    if state:
        if state.get('entry') != entry.name or state.get('workspace_root') != str(root):
            raise WorkflowError('Instruction entry or workspace changed; inspect before migrating')
        if not span:
            raise WorkflowError('Previously installed block is missing; inspect before reinstalling')
        current_block = before[span[0]:span[1]]
        if digest(current_block) != state.get('block_sha256'):
            base_path = root / '.assistant-workflow' / 'base-block.md'
            base = read_text(base_path) if base_path.exists() else ''
            diff = ''.join(difflib.unified_diff(base.splitlines(True), current_block.splitlines(True),
                                               fromfile='installed-baseline', tofile='current-block'))
            raise WorkflowError('Managed block was edited; preserve and merge these changes:\n' + diff)
        if version_key(version) < version_key(state.get('version')):
            raise WorkflowError('Refusing a shared rule downgrade')
        if version == state.get('version') and rules_hash != state.get('rules_sha256'):
            raise WorkflowError('Shared rules changed without a VERSION increment')
    elif span:
        raise WorkflowError('Managed block exists without state; recover baseline before updating')
    cfg = local_config(root, source)
    block = render_block(root, source, version, rules, rules_hash, cfg)
    if span:
        after = before[:span[0]] + block + before[span[1]:]
    else:
        separator = '' if not before else ('\n' if before.endswith('\n') else '\n\n')
        after = before + separator + block + '\n'
    if len(after.encode('utf-8')) > max_bytes:
        raise WorkflowError('Entry exceeds configured byte budget; inspect instruction chain first')
    return {'root': root, 'entry': entry, 'before': before, 'after': after, 'block': block,
            'version': version, 'rules_sha256': rules_hash, 'config': cfg, 'state': state}


def atomic_write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=str(path.parent), delete=False) as handle:
            name = handle.name
            handle.write(text)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def apply_plan(plan):
    root, entry = plan['root'], plan['entry']
    current = read_text(entry) if entry.exists() else ''
    if current != plan['before']:
        raise WorkflowError('Entry changed since preview; regenerate the plan')
    local_root = root / '.assistant-workflow'
    ignore_path = local_root / '.gitignore'
    ignore_before = read_text(ignore_path) if ignore_path.exists() else ''
    if not ignore_before.endswith('*\n'):
        separator = '' if not ignore_before or ignore_before.endswith('\n') else '\n'
        atomic_write(ignore_path, ignore_before + separator + '# Local machine state: never commit\n*\n')
    if plan['before'] != plan['after'] and entry.exists():
        backup = local_root / 'backups' / '{}.{}.md'.format(entry.name, digest(plan['before'])[:16])
        if not backup.exists():
            atomic_write(backup, plan['before'])
    if plan['before'] != plan['after']:
        mode = stat.S_IMODE(entry.stat().st_mode) if entry.exists() else 0o644
        atomic_write(entry, plan['after'], mode)
    config = json.dumps(plan['config'], ensure_ascii=False, indent=2) + '\n'
    state = {'schema_version': 1, 'workspace_root': str(root), 'entry': entry.name,
             'version': plan['version'], 'rules_sha256': plan['rules_sha256'],
             'block_sha256': digest(plan['block'])}
    atomic_write(local_root / 'local.json', config)
    atomic_write(local_root / 'state.json', json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    atomic_write(local_root / 'base-block.md', plan['block'])
    return {'status': 'installed', 'version': plan['version'], 'entry': str(entry),
            'changed': plan['before'] != plan['after'], 'runtime_loaded': 'not_verified'}


def status(workspace, source=SOURCE):
    root = Path(workspace).expanduser().resolve()
    state_path = root / '.assistant-workflow' / 'state.json'
    if not state_path.exists():
        return {'status': 'not_installed', 'runtime_loaded': 'not_verified'}, 1
    plan = make_plan(root, source)
    same = plan['before'] == plan['after']
    return {'status': 'current' if same else 'update_available',
            'installed_version': plan['state']['version'], 'source_version': plan['version'],
            'entry': str(plan['entry']), 'runtime_loaded': 'not_verified'}, 0 if same else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'status'])
    parser.add_argument('--workspace', required=True, help='existing target work directory')
    parser.add_argument('--apply', action='store_true', help='apply the previewed installation')
    parser.add_argument('--max-bytes', type=int, default=32768,
                        help='entry byte budget; does not verify the total instruction chain')
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            if args.apply:
                raise WorkflowError('--apply is only valid for install')
            value, code = status(args.workspace)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return code
        plan = make_plan(args.workspace, max_bytes=args.max_bytes)
        diff = ''.join(difflib.unified_diff(plan['before'].splitlines(True), plan['after'].splitlines(True),
                                          fromfile=str(plan['entry']), tofile=str(plan['entry'])))
        print(diff if diff else 'No instruction changes.')
        if args.apply:
            print(json.dumps(apply_plan(plan), ensure_ascii=False, indent=2))
        else:
            print('Preview only. No files written. Use --apply under the existing installation authorization.')
        return 0
    except (WorkflowError, OSError, UnicodeError) as exc:
        print('ERROR: {}'.format(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
