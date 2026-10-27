#!/usr/bin/env python3
"""Build the Guacamole UI and authenticated management bridge extension."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
from urllib.request import urlretrieve
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "guacamole" / "extension"
TARGET = ROOT / "guacamole" / "crange-theme.jar"
DEPS = {
    'guacamole-common-1.6.0.jar': (
        'https://repo.maven.apache.org/maven2/org/apache/guacamole/guacamole-common/1.6.0/guacamole-common-1.6.0.jar',
        '6ecfed5020bb1077370cf50f91ac08cc93bf6c729033ca091d883a348041da75'),
    'guacamole-ext-1.6.0.jar': (
        'https://repo.maven.apache.org/maven2/org/apache/guacamole/guacamole-ext/1.6.0/guacamole-ext-1.6.0.jar',
        '53d6ad145e869e153f396aee4f543f996a9556198b022279106b9004159ba95e'),
    'javax.ws.rs-api-2.1.1.jar': (
        'https://repo.maven.apache.org/maven2/javax/ws/rs/javax.ws.rs-api/2.1.1/javax.ws.rs-api-2.1.1.jar',
        '2c309eb2c9455ffee9da8518c70a3b6d46be2a269b2e2a101c806a537efe79a4'),
    # Compile against Guacamole 1.6.0's own Jackson version. These libraries
    # are provided by the web application and must not be duplicated in our JAR.
    'jackson-core-2.19.0.jar': (
        'https://repo.maven.apache.org/maven2/com/fasterxml/jackson/core/jackson-core/2.19.0/jackson-core-2.19.0.jar',
        'da8e859bac94874528116a25f20c68560e4287acbf27628711b8a4f96b028430'),
    'jackson-databind-2.19.0.jar': (
        'https://repo.maven.apache.org/maven2/com/fasterxml/jackson/core/jackson-databind/2.19.0/jackson-databind-2.19.0.jar',
        'ceda311f476c3b18e1d2b240c94e2dcb9c8d44e70f8afa9facab88bac4ddc03a'),
    'jackson-annotations-2.19.0.jar': (
        'https://repo.maven.apache.org/maven2/com/fasterxml/jackson/core/jackson-annotations/2.19.0/jackson-annotations-2.19.0.jar',
        'ead60e9cac0e42b57092b9e2d087ff43536e9477d4486ba393037a8cc156cda7'),
}


def dependency(name, url, digest):
    directory = Path.home() / '.cache/crange/guac-build'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        temporary = path.with_suffix('.download')
        urlretrieve(url, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            temporary.unlink(missing_ok=True)
            raise ValueError(f'Guacamole build dependency checksum failed: {name}')
        temporary.replace(path)
    return path


classpath = ':'.join(str(dependency(name, *spec)) for name, spec in DEPS.items())
sources = sorted((SOURCE / 'java').rglob('*.java'))
with tempfile.TemporaryDirectory(prefix='crange-guac-build-') as directory:
    classes = Path(directory) / 'classes'
    classes.mkdir()
    if sources:
        subprocess.run(['javac', '--release', '17', '-encoding', 'UTF-8', '-cp', classpath,
                        '-d', str(classes), *(str(path) for path in sources)], check=True)

    with zipfile.ZipFile(TARGET, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT / 'gui' / 'login.css', 'login.css')
        for path in sorted(SOURCE.rglob("*")):
            if path.is_file() and 'java' not in path.relative_to(SOURCE).parts:
                archive.write(path, path.relative_to(SOURCE))
        for path in sorted(classes.rglob('*.class')):
            archive.write(path, path.relative_to(classes))
print(TARGET)
