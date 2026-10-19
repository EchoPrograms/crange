"""Content-addressed copies of fixed MHL recipes; no caller-supplied automation."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ROLES = {'mhl_application', 'mhl_database', 'mhl_build', 'mhl_backup'}
SUFFIXES = {'.yml', '.j2', '.py', '.sql', '.csv'}
WINDOWS = {'ansible/windows/project-100-domain.yml', 'ansible/windows/project-100-members.yml'}


class RecipeError(ValueError):
    pass


def allowed(kind, name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name:
        return False
    if kind == 'windows':return name in WINDOWS
    return kind == 'business' and (name == 'ansible/linux/project-100-business.yml' or
        (len(path.parts) >= 5 and path.parts[:2] == ('ansible','roles') and
         path.parts[2] in ROLES and path.suffix in SUFFIXES))


def source_files(kind, root):
    root = Path(root)
    if kind == 'windows':files = [root/name for name in sorted(WINDOWS)]
    elif kind == 'business':
        files = [root/'ansible/linux/project-100-business.yml']
        files += [p for p in (root/'ansible/roles').glob('mhl_*/**/*') if p.is_file() and p.suffix in SUFFIXES]
    else:raise RecipeError('Unknown fixed MHL service recipe.')
    content = {}
    for path in sorted(files):
        name = str(path.relative_to(root))
        if not allowed(kind,name) or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise RecipeError('Recipe contains an unapproved path or symlink.')
        content[name] = path.read_bytes().decode('utf-8')
    return content


def content_digest(files):
    hashes = {name:hashlib.sha256(value.encode()).hexdigest() for name,value in sorted(files.items())}
    return hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()


def digest(kind, root=ROOT):return content_digest(source_files(kind,root))


def archive_path(expected, root):
    if not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected):
        raise RecipeError('Release service recipe fingerprint is invalid.')
    return Path(root)/'releases/project-100/service-recipes'/(expected+'.json')


def load(kind, expected, root=ROOT):
    path = archive_path(expected,root)
    if not path.exists():return None
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_mode & 0o022 or
            info.st_uid not in (0,os.geteuid()) or info.st_size > 2*1024**2):
        raise RecipeError('Archived service recipe has unsafe ownership, permissions or size.')
    record = json.loads(path.read_text())
    if not isinstance(record,dict):
        raise RecipeError('Archived service recipe record must be an object.')
    files = record.get('files')
    if (record.get('schema_version') != 1 or record.get('kind') != kind or
            record.get('source_sha256') != expected or not isinstance(files,dict) or
            not files or len(files)>100 or any(not allowed(kind,k) or not isinstance(v,str)
                or len(v.encode())>512*1024 for k,v in files.items()) or content_digest(files)!=expected):
        raise RecipeError('Archived service recipe content differs from the release fingerprint.')
    return files


def verify(kind, expected, root=ROOT):
    if load(kind,expected,root) is not None:return
    if digest(kind,root)!=expected:
        raise RecipeError('Release service recipe changed and no verified archive exists; configure staging and capture a new release.')


def preserve(kind, expected, root=ROOT):
    if load(kind,expected,root) is not None:return expected
    files=source_files(kind,root)
    if content_digest(files)!=expected:
        raise RecipeError('Current service recipe differs from the release; archive creation was refused.')
    destination=archive_path(expected,root);destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    data=json.dumps({'schema_version':1,'kind':kind,'source_sha256':expected,'files':files},sort_keys=True)
    fd,name=tempfile.mkstemp(prefix='.recipe-',dir=destination.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        try:os.link(name,destination)
        except FileExistsError:load(kind,expected,root)
    finally:os.unlink(name)
    return expected


@contextmanager
def source_directory(kind, expected=None, root=ROOT):
    if expected is None:
        yield Path(root);return
    files=load(kind,expected,root)
    if files is None:
        verify(kind,expected,root);yield Path(root);return
    with tempfile.TemporaryDirectory(prefix='crange-fixed-recipe-') as temporary:
        base=Path(temporary);base.chmod(0o700)
        for name,content in files.items():
            path=base/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            path.write_text(content);path.chmod(0o600)
        yield base
