#!/usr/bin/env python3
"""Install this repository's skill without copying credentials or generated files."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
from uuid import uuid4

SOURCE = Path(__file__).resolve().parents[1] / 'skills' / 'relay-imagegen'


def install(destination, replace=False):
    destination = Path(destination).expanduser().absolute()
    if destination.resolve() == SOURCE.resolve():
        return destination, None, '已通过目录或链接安装，无需重复复制。'
    if SOURCE in destination.resolve().parents or destination.resolve() in SOURCE.parents:
        raise ValueError('安装目录不能包含源码目录，也不能位于 skill 源码内部。')
    if destination.is_symlink():
        raise ValueError('目标是其他符号链接，请使用实际目录或另选 --destination；不会覆盖链接目标。')
    if destination.exists() and not replace:
        raise ValueError('目标已存在；确认更新时加 --replace，旧版本会自动备份。')
    if destination.exists() and not destination.is_dir():
        raise ValueError('目标不是目录。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / ('.relay-install-' + uuid4().hex)
    backup = None
    try:
        shutil.copytree(SOURCE, temporary, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'test_*.py'))
        if destination.exists():
            # Outside the discovery directory, so Codex will not load the backup as a duplicate skill.
            backups = destination.parent.parent / 'skill-backups'
            backups.mkdir(exist_ok=True)
            backup = backups / (destination.name + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:6])
            destination.rename(backup)
        try:
            temporary.rename(destination)
        except OSError:
            if backup:
                backup.rename(destination)
            raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination, backup, '安装完成。新会话中可使用 $relay-imagegen。'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=Path.home() / '.agents' / 'skills' / 'relay-imagegen', help='完整目标技能目录；默认 ~/.agents/skills/relay-imagegen')
    parser.add_argument('--replace', action='store_true', help='更新已有安装，并将旧版本备份至 skill-backups')
    args = parser.parse_args()
    try:
        path, backup, message = install(args.destination, args.replace)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(message)
    print('技能目录：', path)
    if backup:
        print('旧版备份：', backup)
    print('API 配置保留在原位置，本命令不读取或修改密钥。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
