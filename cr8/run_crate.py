import argh
import os
import json
import re
import subprocess
import tempfile
import sys
import shutil
import contextlib
import logging
import random
import time
import gzip
import io
import tarfile
import threading
import fnmatch
import socket
import ssl
from pathlib import Path
from functools import partial
from typing import Dict, Any, List
from urllib.request import urlopen

from cr8.misc import parse_version, init_logging


log = logging.getLogger(__name__)

NO_SSL_VERIFY_CTX = ssl._create_unverified_context()
RELEASE_URL = 'https://cdn.crate.io/downloads/releases/crate-{version}.tar.gz'
VERSION_RE = re.compile('^(\d+\.\d+\.\d+)$')
DYNAMIC_VERSION_RE = re.compile('^((\d+|x)\.(\d+|x)\.(\d+|x))$')
FOLDER_VERSION_RE = re.compile('crate-(\d+\.\d+\.\d+)')

DEFAULT_SETTINGS = {
    'cluster.routing.allocation.disk.watermark.low': '1b',
    'cluster.routing.allocation.disk.watermark.high': '1b',
    'discovery.initial_state_timeout': 0,
    'network.host': '127.0.0.1',
    'udc.enabled': False
}


def _format_cmd_option_legacy(k, v):
    return '-Des.{0}={1}'.format(k, v)


def _format_cmd_option(k, v):
    if isinstance(v, bool):
        return '-C{0}={1}'.format(k, str(v).lower())
    return '-C{0}={1}'.format(k, v)


def _extract_version(crate_dir) -> tuple:
    m = FOLDER_VERSION_RE.findall(crate_dir)
    if m:
        return parse_version(m[0])
    return (1, 0, 0)


class OutputMonitor:

    def __init__(self):
        self.consumers = []

    def _consume(self, proc):
        try:
            for line in proc.stdout:
                for consumer in self.consumers:
                    consumer.send(line)
        except:
            if proc.returncode is not None:
                return
            raise

    def start(self, proc):
        out_thread = threading.Thread(target=self._consume, args=(proc,))
        out_thread.daemon = True
        out_thread.start()


class Timeout:
    def __init__(self, timeout, sleep=0.1):
        self.start_time = time.time()
        self.sleep = sleep
        self._first_ok = True
        self.timeout = timeout

        def timeout_expired():
            if self._first_ok:
                self._first_ok = False
                return False
            now = time.time()
            if (now - self.start_time) > self.timeout:
                return True
            if self.sleep:
                time.sleep(self.sleep)

        self._timeout_expired = timeout_expired

    def __call__(self):
        if self._timeout_expired():
            raise TimeoutError()
        return True


def wait_until(predicate, timeout=30):
    """Wait until predicate returns a truthy value or the timeout is reached.

    >>> wait_until(lambda: True, timeout=10)
    """
    not_expired = Timeout(timeout)
    while not_expired():
        r = predicate()
        if r:
            break


def _is_up(host: str, port: int):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ex = s.connect_ex((host, port))
    s.close()
    return ex == 0


def _has_ssl(host: str, port: int):
    s = ssl.wrap_socket(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
    try:
        ex = s.connect_ex((host, port))
        s.close()
        return ex == 0
    except ssl.SSLError:
        return False


def cluster_state_200(url):
    try:
        with urlopen(url, context=NO_SSL_VERIFY_CTX) as r:
            p = json.loads(r.read().decode('utf-8'))
            return int(p['status']) == 200
    except Exception as e:
        log.debug(e)
        return False


def _get_settings(settings=None):
    s = DEFAULT_SETTINGS.copy()
    if settings:
        s.update(settings)
    return s


def _try_print_log(logfile):
    try:
        with open(logfile) as f:
            for line in f:
                log.error(line)
    except:
        pass


class CrateNode(contextlib.ExitStack):
    """Class that allows starting and stopping a Crate process

    This is similar to the ``CrateLayer`` in ``crate.testing.layer``.
    But additionaly it supports setting environment variables and it can infer
    the port to which Crate binds by sniffing Crate's stdout.

    Attributes:
        http_url: The HTTP URL of the Crate process.
            Only available after ``start()`` has been called.
        process: The subprocess. Only available after ``start()`` has been called.
    """

    def __init__(self,
                 crate_dir: str,
                 data_path: str=None,
                 env: Dict[str, Any]=None,
                 settings: Dict[str, Any]=None,
                 keep_data: bool=False) -> None:
        """Create a CrateNode

        Args:
            crate_dir: Path to the extracted Crate tarball
            env: Environment variables with which the Crate process will be
                started.
            settings: Additional Crate settings.
        """
        super().__init__()
        self.crate_dir = crate_dir
        version = _extract_version(crate_dir)
        self.env = env or {}
        self.env.setdefault('JAVA_HOME', os.environ.get('JAVA_HOME', ''))
        self.monitor = OutputMonitor()
        self.process = None  # type: subprocess.Popen
        self.http_url = None  # type: str
        self.http_host = None  # type: str
        start_script = 'crate.bat' if sys.platform == 'win32' else 'crate'

        settings = _get_settings(settings)
        if version < (1, 1, 0):
            settings.setdefault('discovery.zen.ping.multicast.enabled', False)
        self.data_path = settings.get('path.data') or tempfile.mkdtemp()
        self.logs_path = settings.get('path.logs') or os.path.join(crate_dir, 'logs')
        self.cluster_name = settings.get('cluster.name') or 'cr8'
        self.keep_data = keep_data
        settings['path.data'] = self.data_path
        settings['cluster.name'] = self.cluster_name
        if version < (1, 0, 0):
            _format_option = _format_cmd_option_legacy
        else:
            _format_option = _format_cmd_option
        args = [_format_option(k, v) for k, v in settings.items()]
        self.cmd = [
            os.path.join(crate_dir, 'bin', start_script)] + args

    def start(self):
        """Start the process.

        This will block until the Crate cluster is ready to process requests.
        """
        log.info('Starting Crate process')
        self.process = proc = self.enter_context(subprocess.Popen(
            self.cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self.env,
            universal_newlines=True
        ))
        msg = ('Crate launched:\n'
               '    PID: %s\n'
               '    Logs: %s\n'
               '    Data: %s')
        if not self.keep_data:
            msg += ' (removed on stop)\n'

        logfile = os.path.join(self.logs_path, self.cluster_name + '.log')
        log.info(
            msg,
            proc.pid,
            logfile,
            self.data_path
        )
        self.monitor.consumers.append(AddrConsumer(self._set_addr))
        self.monitor.start(proc)

        try:
            line_buf = LineBuffer()
            self.monitor.consumers.append(line_buf)
            wait_until(lambda: self.http_host, timeout=30)
            host, port = self.http_host.split(':')
            port = int(port)
            wait_until(lambda: _is_up(host, port), timeout=30)
            if _has_ssl(host, port):
                self.http_url = self.http_url.replace('http://', 'https://')
            wait_until(lambda: cluster_state_200(self.http_url), timeout=30)
        except TimeoutError as e:
            if not line_buf.lines:
                _try_print_log(logfile)
            else:
                for line in line_buf.lines:
                    log.error(line)
            raise
        else:
            self.monitor.consumers.remove(line_buf)
            line_buf = None
        log.info('Cluster ready to process requests')

    def _set_addr(self, protocol, addr):
        log.info('{0:10}: {1}'.format(protocol.capitalize(), addr))
        if protocol == 'http':
            self.http_host = addr
            self.http_url = 'http://' + addr

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.communicate(timeout=10)
        if not self.keep_data:
            path = self.data_path.split(',')
            for p in path:
                shutil.rmtree(p)

    def __enter__(self):
        return self

    def __exit__(self, *ex):
        self.stop()


class LineBuffer:

    def __init__(self):
        self.lines = []

    def send(self, line):
        self.lines.append(line.strip())


class AddrConsumer:

    ADDRESS_RE = re.compile(
        '.*\[(?P<protocol>http|o.e.h.HttpServer|psql|transport|o.e.t.TransportService) +\] \[.*\] .*'
        'publish_address {'
        '(?:inet\[[\w\d\.-]*/|\[)?'
        '(?:[\w\d\.-]+/)?'
        '(?P<addr>[\d\.:]+)'
        '(?:\])?'
        '}'
    )
    PROTOCOL_MAP = {
        'o.e.h.HttpServer': 'http',
        'o.e.t.TransportService': 'transport'
    }

    def __init__(self, on_addr):
        self.on_addr = on_addr

    @staticmethod
    def _parse(line):
        """ Parse protocol and bound address from log message

        >>> AddrConsumer._parse('NONE')
        (None, None)

        >>> AddrConsumer._parse('[INFO ][psql  ] [8f64DTi] publish_address {127.0.0.1:5432}, bound_addresses {127.0.0.1:5432}')
        ('psql', '127.0.0.1:5432')
        """
        m = AddrConsumer.ADDRESS_RE.match(line)
        if not m:
            return None, None
        protocol = m.group('protocol')
        protocol = AddrConsumer.PROTOCOL_MAP.get(protocol, protocol)
        return protocol, m.group('addr')

    def send(self, line):
        protocol, addr = AddrConsumer._parse(line)
        if protocol:
            self.on_addr(protocol, addr)


def _openuri(uri):
    if os.path.isfile(uri):
        return open(uri, 'rb')
    return io.BytesIO(urlopen(uri).read())


def _download_and_extract(uri, crate_root):
    filename = os.path.basename(uri)
    crate_folder_name = re.sub('\.tar(\.gz)?$', '', filename)
    crate_dir = os.path.join(crate_root, crate_folder_name)
    if os.path.exists(crate_dir):
        log.info('Skipping download, tarball alrady extracted at %s', crate_dir)
        return crate_dir
    log.info('Downloading %s and extracting to %s', uri, crate_root)
    with _openuri(uri) as tmpfile:
        with tarfile.open(fileobj=tmpfile) as t:
            t.extractall(crate_root)
    return crate_dir


def _from_versions_json(key):
    def retrieve():
        with urlopen('https://crate.io/versions.json') as r:
            if r.headers.get('Content-Encoding') == 'gzip':
                with gzip.open(r, 'rt') as r:
                    versions = json.loads(r.read())
            else:
                versions = json.loads(r.read().decode('utf-8'))
        return versions[key]
    return retrieve


NIGHTLY_RE = re.compile('.*>(?P<filename>crate-\d+\.\d+\.\d+-\d{12}-[a-z0-9]{7,}\.tar\.gz)<.*')
RELEASE_RE = re.compile('.*>(?P<filename>crate-(?P<version>\d+\.\d+\.\d+)\.tar\.gz)<.*')


def _find_last_nightly(lines):
    """Return the last nightly release tarball filename.

    >>> lines = [
    ...     '<a href="crate-0.55.0-201606080301-3ceb1ed.tar.gz">crate-0.55.0-201606080301-3ceb1ed.tar.gz</a>           08-Jun-2016 01:01            46298304'
    ...     '<a href="crate-0.55.0-201606090301-b32a36f.tar.gz">crate-0.55.0-201606090301-b32a36f.tar.gz</a>           09-Jun-2016 01:01            46297737'
    ...     '<a href="crate-0.55.0-201606100301-23388dd.tar.gz">crate-0.55.0-201606100301-23388dd.tar.gz</a>           10-Jun-2016 01:01            46300496'
    ... ]
    >>> _find_last_nightly(lines)
    'crate-0.55.0-201606100301-23388dd.tar.gz'
    """
    for line in reversed(lines):
        m = NIGHTLY_RE.match(line)
        if m:
            return m.group('filename')
    raise ValueError("Couldn't find a valid nightly tarball filename in the lines")


def _get_latest_nightly_uri():
    base_uri = 'https://cdn.crate.io/downloads/releases/nightly/'
    with urlopen(base_uri) as r:
        filename = _find_last_nightly([line.decode('utf-8') for line in r])
        return base_uri + filename


def _retrieve_crate_versions():
    base_uri = 'https://cdn.crate.io/downloads/releases/'
    with urlopen(base_uri) as r:
        lines = (line.decode('utf-8') for line in r)
        for line in lines:
            m = RELEASE_RE.match(line)
            if m:
                yield m.group('version')


def _find_matching_version(versions, version_pattern):
    """
    Return the first matching version

    >>> _find_matching_version(['1.1.4', '1.0.12', '1.0.5'], '1.0.x')
    '1.0.12'

    >>> _find_matching_version(['1.1.4', '1.0.6', '1.0.5'], '2.x.x')
    """
    pattern = fnmatch.translate(version_pattern.replace('x', '*'))
    return next((v for v in versions if re.match(pattern, v)), None)


_version_lookups = {
    'latest': _from_versions_json('crate'),
    'latest-stable': _from_versions_json('crate'),
    'latest-testing': _from_versions_json('crate_testing'),
    'latest-nightly': _get_latest_nightly_uri
}


def _lookup_uri(version):
    if version in _version_lookups:
        version = _version_lookups[version]()
    m = VERSION_RE.match(version)
    if m:
        return RELEASE_URL.format(version=m.group(0))
    m = DYNAMIC_VERSION_RE.match(version)
    if m:
        versions = reversed(list(_retrieve_crate_versions()))
        release = _find_matching_version(versions, m.group(0))
        if release:
            return RELEASE_URL.format(version=release)
    return version


def _is_project_repo(src_repo):
    return (os.path.isdir(src_repo) and
            os.path.exists(os.path.join(src_repo, '.git')) and
            os.path.exists(os.path.join(src_repo, 'gradlew')))


def _build_from_src(src_repo):
    run = partial(subprocess.run, cwd=src_repo, check=True)
    run(['git', 'clean', '-xdff'])
    run(['git', 'submodule', 'update', '--init'])
    run(['./gradlew', 'clean', 'distTar'])
    distributions = Path(src_repo) / 'app' / 'build' / 'distributions'
    tarball = next(distributions.glob('crate-*.tar.gz'))
    with tarfile.open(tarball) as t:
        t.extractall(tarball.parent)
    # remove two suffixes ('.tar, '.gz') to get the folder name
    return str(tarball.with_suffix('').with_suffix(''))


def get_crate(version, crate_root=None):
    """Retrieve a Crate tarball, extract it and return the path.

    Args:
        version: The Crate version to get.
            Can be specified in different ways:

            - A concrete version like '0.55.0'
            - A version including a `x` as wildcards. Like: '1.1.x' or '1.x.x'.
              This will use the latest version that matches.
            - An alias: 'latest-stable' or 'latest-testing'
            - A URI pointing to a crate tarball
        crate_root: Where to extract the tarball to.
            If this isn't specified ``$XDG_CACHE_HOME/.cache/cr8/crates``
            will be used.
    """
    if _is_project_repo(version):
        return _build_from_src(version)
    uri = _lookup_uri(version)
    crate_root = crate_root or os.environ.get(
        'XDG_CACHE_HOME', os.path.join(os.path.expanduser('~'), '.cache', 'cr8', 'crates'))
    crate_dir = _download_and_extract(uri, crate_root)
    return crate_dir


def _parse_options(options: List[str]) -> Dict[str, str]:
    """ Parse repeatable CLI options

    >>> opts = _parse_options(['cluster.name=foo', 'CRATE_JAVA_OPTS="-Dxy=foo"'])
    >>> print(json.dumps(opts, sort_keys=True))
    {"CRATE_JAVA_OPTS": "\\"-Dxy=foo\\"", "cluster.name": "foo"}
    """
    return dict(i.split('=', maxsplit=1) for i in options)


def create_node(version, env=None, setting=None, crate_root=None, keep_data=False):
    init_logging(log)
    settings = {
        'cluster.name': 'cr8-crate-run' + str(random.randrange(1e9))
    }
    crate_dir = get_crate(version, crate_root)
    if setting:
        settings.update(_parse_options(setting))
    if env:
        env = _parse_options(env)
    return CrateNode(
        crate_dir=crate_dir, env=env, settings=settings, keep_data=keep_data)


@argh.arg('version', help='Crate version to run')
@argh.arg('-e', '--env', action='append',
          help='Environment variable. Option can be specified multiple times.')
@argh.arg('-s', '--setting', action='append',
          help='Crate setting. Option can be specified multiple times.')
@argh.arg('--keep-data', help='If this is set the data folder will be kept.')
def run_crate(version, env=None, setting=None, crate_root=None, keep_data=False):
    """Launch a crate instance.

    Supported version specifications:
        - Concrete version like "0.55.0" or with wildcard: "1.1.x"
        - An alias (one of [latest-nightly, latest-stable, latest-testing])
        - A URI pointing to a CrateDB tarball (in .tar.gz format)
        - A URI pointing to a checked out CrateDB repo directory

    run-crate supports command chaining. To first launch a CrateDB node and then
    another sub-command use:

        cr8 run-crate <ver> -- timeit -s "select 1" --hosts '{node.http_url}'

    To launch any (blocking) subprocess, prefix the name with '@':

        cr8 run-crate <version> -- @http '{node.http_url}'

    If run-crate is invoked using command chaining it won't block but exit once
    all chained commands finished.
    """
    with create_node(version, env, setting, crate_root, keep_data) as n:
        try:
            n.start()
            n.process.wait()
        except KeyboardInterrupt:
            print('Stopping Crate...')


if __name__ == "__main__":
    argh.dispatch_command(run_crate)
