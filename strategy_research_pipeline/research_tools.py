"""Compact inspection and command results for a research session."""

import argparse
import json
from itertools import islice
from pathlib import Path
import subprocess


def list_files(path, pattern, offset, limit):
    # Sorting makes successive pages stable without printing the whole listing.
    entries = sorted(path.glob(pattern))
    return dict(directory=str(path), total=len(entries), offset=offset,
                files=[entry.relative_to(path).as_posix()
                       for entry in islice(entries, offset, offset + limit)])


def process_status(pid):
    proc = subprocess.run(['ps', '-p', str(pid), '-o', 'pid=,stat=,etime=,rss=,comm='],
                          capture_output=True, text=True, check=False)
    if proc.returncode not in (0, 1):
        raise ValueError(proc.stderr.strip()[:500])
    summary = proc.stdout.strip().split(maxsplit=4)
    if not summary:
        return dict(pid=pid, running=False)
    _, state, elapsed, rss, name = summary
    return dict(pid=pid, running=not state.startswith('Z'), state=state,
                elapsed=elapsed, rss_kib=int(rss), name=name)


def run_logged(args, path, cwd):
    path = (cwd / path).resolve()
    if not path.is_relative_to(cwd.resolve()):
        raise ValueError('命令日志须保存在当前研究目录')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        try:
            result = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stream,
                                    stderr=subprocess.STDOUT, check=False)
            code = result.returncode
        except OSError as exc:
            stream.write((str(exc) + '\n').encode('utf-8'))
            code = 127
    size = path.stat().st_size
    with path.open('rb') as stream:
        stream.seek(max(0, size - 1600))
        preview = stream.read().decode('utf-8', errors='replace')
    return dict(exit_code=code, log=path.relative_to(cwd.resolve()).as_posix(),
                bytes=size, truncated=size>1600, tail=preview)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    files = actions.add_parser('files')
    files.add_argument('directory', type=Path, nargs='?', default=Path('.'))
    files.add_argument('--glob', default='*')
    files.add_argument('--offset', type=int, default=0)
    files.add_argument('--limit', type=int, choices=range(1, 31), default=15)
    process = actions.add_parser('process')
    process.add_argument('pid', type=int)
    run = actions.add_parser('run')
    run.add_argument('--log', type=Path, required=True)
    run.add_argument('command', nargs=argparse.REMAINDER)
    opts = parser.parse_args()
    code = 0
    try:
        if opts.action == 'files':
            if opts.offset < 0 or not opts.directory.is_dir():
                raise ValueError('目录须存在，起始偏移须为非负数')
            result = list_files(opts.directory, opts.glob, opts.offset, opts.limit)
        elif opts.action == 'process':
            if opts.pid <= 0:
                raise ValueError('PID 须为正整数')
            result = process_status(opts.pid)
        else:
            args = opts.command[1:] if opts.command[:1] == ['--'] else opts.command
            if not args:
                raise ValueError('请在 -- 后提供命令及其参数')
            result = run_logged(args, opts.log, Path.cwd())
            code = result['exit_code']
    except (OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
