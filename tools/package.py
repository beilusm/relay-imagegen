#!/usr/bin/env python3
"""Build a deterministic skill ZIP from an explicit allowlist of runtime resources."""
import argparse
import hashlib
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'relay-imagegen'


def build(output):
    output = Path(output).expanduser().resolve()
    if SKILL == output or SKILL in output.parents:
        raise ValueError('安装包不能写入 skill 源码目录。')
    files = [SKILL / 'SKILL.md', SKILL / 'agents' / 'openai.yaml']
    for folder, pattern in [('scripts', 'relay_*.py'), ('references', '*.md'), ('assets', '*.html')]:
        files.extend(sorted((SKILL / folder).glob(pattern)))
    if any(p.is_symlink() or not p.is_file() for p in files):
        raise ValueError('运行资源缺失或使用符号链接。')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(files):
                info = zipfile.ZipInfo(str(Path('relay-imagegen') / path.relative_to(SKILL)).replace('\\', '/'), (2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError('ZIP 校验失败。')
            for path in files:
                name = str(Path('relay-imagegen') / path.relative_to(SKILL)).replace('\\', '/')
                if archive.read(name) != path.read_bytes():
                    raise ValueError('ZIP 内容与源文件不一致。')
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(files), hashlib.sha256(output.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'relay-imagegen.zip')
    args = parser.parse_args()
    try:
        count, checksum = build(args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f'{args.output.resolve()}\n{count} files\nSHA256 {checksum}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
