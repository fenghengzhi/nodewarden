"""Build and publish a deterministic deployment commit without a worktree.

Only reviewed wrangler.toml/webapp changes are applied. No upstream program,
checkout filter, hook, package manager, or shell command is executed.
"""
import base64
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlparse


REVIEWED_TIP = 'da2bf6f062fe00c66c3ab601815a6ab0b52748a4'
BASELINE = '29c2de852411f3aa66de0c8a2ea16e7f87fa932c'
BOOTSTRAP = 'd928f8945a9be575e7c2f899d827f20961653dc1'
REMOTE = 'https://github.com/fenghengzhi/nodewarden.git'
SHA = re.compile(r'[0-9a-f]{40}')
SEMVER = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


class DeploymentGitError(RuntimeError):
    """A fail-closed Git operation or integrity check."""


class DeploymentGit:
    def __init__(self, repo_dir, remote_url, token='', reviewed_tip=REVIEWED_TIP,
                 baseline=BASELINE, bootstrap=BOOTSTRAP, allow_local_test=False):
        self.repo_dir = Path(repo_dir).resolve()
        self.remote_url = str(remote_url)
        self.token = token
        self.reviewed_tip = self._sha(reviewed_tip)
        self.baseline = self._sha(baseline)
        self.bootstrap = self._sha(bootstrap)
        self.allow_local_test = allow_local_test
        if allow_local_test:
            parsed = urlparse(self.remote_url)
            if not ((not parsed.scheme and Path(self.remote_url).is_absolute())
                    or (parsed.scheme == 'file' and parsed.netloc in ('', 'localhost')
                        and Path(parsed.path).is_absolute())):
                raise DeploymentGitError('Tests must use an absolute local bare repository')
        elif (self.remote_url != REMOTE or self.reviewed_tip != REVIEWED_TIP
              or self.baseline != BASELINE or self.bootstrap != BOOTSTRAP):
            raise DeploymentGitError('Production requires the fixed GitHub remote and reviewed commits')
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        if any(self.repo_dir.iterdir()):
            raise DeploymentGitError('The temporary bare repository directory must be empty')
        self._run(['init', '--bare', '--object-format=sha1', str(self.repo_dir)], use_repo=False)

    @staticmethod
    def _sha(value):
        if not isinstance(value, str) or not SHA.fullmatch(value):
            raise DeploymentGitError('Expected an immutable 40-character commit SHA')
        return value

    @staticmethod
    def _version(value):
        if not isinstance(value, str) or not SEMVER.fullmatch(value):
            raise DeploymentGitError('Expected a stable MAJOR.MINOR.PATCH version')
        return value

    def _environment(self, index=None, extra=None):
        env = os.environ.copy()
        # Do not inherit config injection, tracing, object stores, or worktree state.
        for key in list(env):
            if key.startswith(('GIT_CONFIG_', 'GIT_TRACE', 'GIT_SSH')) or key in {
                    'GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                    'GIT_REPLACE_REF_BASE', 'GIT_CURL_VERBOSE', 'GIT_ASKPASS',
                    'SSH_ASKPASS', 'GIT_NAMESPACE', 'GIT_SHALLOW_FILE',
                    'GIT_TEMPLATE_DIR', 'GIT_EXEC_PATH', 'GIT_CONFIG',
                    'GIT_EXTERNAL_DIFF', 'GIT_DIFF_OPTS', 'GIT_PROXY_COMMAND'}:
                env.pop(key, None)
        env.update({
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1',
            'GIT_ATTR_NOSYSTEM': '1', 'GIT_LITERAL_PATHSPECS': '1',
            'GIT_ALLOW_PROTOCOL': 'file' if self.allow_local_test else 'https',
            'LC_ALL': 'C',
        })
        settings = [
            ('core.hooksPath', os.devnull), ('credential.helper', ''),
            ('credential.interactive', 'false'), ('http.followRedirects', 'false'),
            ('core.autocrlf', 'false'), ('core.safecrlf', 'false'),
            ('commit.gpgSign', 'false'), ('fetch.writeCommitGraph', 'false'),
            ('protocol.allow', 'never'),
            ('protocol.file.allow' if self.allow_local_test else 'protocol.https.allow', 'always'),
        ]
        if self.token:
            basic = base64.b64encode(('x-access-token:' + self.token).encode()).decode()
            settings.append(('http.https://github.com/.extraheader', 'AUTHORIZATION: basic ' + basic))
        env['GIT_CONFIG_COUNT'] = str(len(settings))
        for number, (key, value) in enumerate(settings):
            env['GIT_CONFIG_KEY_' + str(number)] = key
            env['GIT_CONFIG_VALUE_' + str(number)] = value
        if index is not None:
            env['GIT_INDEX_FILE'] = str(index)
        if extra:
            env.update(extra)
        return env

    def _run(self, arguments, *, data=None, index=None, extra_env=None,
             use_repo=True, allowed=(0,)):
        command = ['git']
        if use_repo:
            command.append('--git-dir=' + str(self.repo_dir))
        command.extend(arguments)
        try:
            result = subprocess.run(command, input=data, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False, timeout=180,
                                    env=self._environment(index, extra_env))
        except (OSError, subprocess.TimeoutExpired):
            raise DeploymentGitError('Git operation failed or timed out; no fallback was attempted') from None
        if result.returncode not in allowed:
            # Never expose argv, stderr, environment, or an HTTP authentication header.
            raise DeploymentGitError('git ' + arguments[0] + ' failed (exit '
                                     + str(result.returncode) + '); no fallback was attempted')
        return result

    def fetch_commit(self, commit):
        commit = self._sha(commit)
        present = self._run(['cat-file', '-t', commit], allowed=(0, 1, 128))
        if present.returncode:
            self._run(['fetch', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head',
                       self.remote_url, commit])
            present = self._run(['cat-file', '-t', commit])
        if present.stdout.strip() != b'commit':
            raise DeploymentGitError('Expected a commit object, not a tag or tree')
        return commit

    def parents(self, commit):
        self.fetch_commit(commit)
        fields = self._run(['rev-list', '--parents', '-n', '1', commit]).stdout.decode('ascii').split()
        if not fields or fields[0] != commit or any(not SHA.fullmatch(value) for value in fields):
            raise DeploymentGitError('Invalid commit ancestry')
        return tuple(fields[1:])

    def parent(self, commit):
        parents = self.parents(commit)
        if len(parents) != 1:
            raise DeploymentGitError('Deployment commits must have exactly one parent')
        return parents[0]

    def tree(self, commit):
        self.fetch_commit(commit)
        return self._sha(self._run(['rev-parse', commit + '^{tree}']).stdout.decode('ascii').strip())

    def is_ancestor(self, ancestor, descendant):
        self.fetch_commit(ancestor)
        self.fetch_commit(descendant)
        result = self._run(['merge-base', '--is-ancestor', ancestor, descendant], allowed=(0, 1))
        return result.returncode == 0

    def verify_upstream_config(self, official_sha):
        self.fetch_commit(official_sha)
        self.fetch_commit(self.baseline)
        original = self._run(['cat-file', 'blob', self.baseline + ':wrangler.toml']).stdout
        candidate = self._run(['cat-file', 'blob', official_sha + ':wrangler.toml']).stdout
        if candidate != original:
            raise DeploymentGitError('Official wrangler.toml differs from the reviewed baseline; review is required')
        return True

    def _overlay_tree(self, official_sha, overlay_tip):
        official_sha = self.fetch_commit(official_sha)
        self.fetch_commit(self.baseline)
        self.fetch_commit(overlay_tip)
        self.verify_upstream_config(official_sha)
        if not self.is_ancestor(self.baseline, overlay_tip):
            raise DeploymentGitError('Reviewed overlay does not descend from its fixed baseline')
        patch = self._run([
            'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
            self.baseline, overlay_tip, '--', 'wrangler.toml', 'webapp',
        ]).stdout
        with tempfile.TemporaryDirectory(prefix='deployment-index-', dir=self.repo_dir) as temporary:
            index = Path(temporary) / 'index'
            self._run(['read-tree', official_sha], index=index)
            if patch:
                self._run(['apply', '--cached', '--check', '-'], data=patch, index=index)
                self._run(['apply', '--cached', '-'],
                          data=patch, index=index)
            if self._run(['ls-files', '--unmerged', '-z'], index=index).stdout:
                raise DeploymentGitError('Reviewed overlay has unresolved conflicts')
            tree = self._sha(self._run(['write-tree'], index=index).stdout.decode('ascii').strip())
        changed = self._run([
            'diff-tree', '--no-commit-id', '--no-ext-diff', '--no-textconv', '--name-only',
            '--no-renames', '-r', '-z', official_sha, tree,
        ]).stdout.split(b'\0')
        if any(path and path != b'wrangler.toml' and path != b'webapp'
               and not path.startswith(b'webapp/') for path in changed):
            raise DeploymentGitError('Deployment tree changed a path outside the reviewed overlay')
        return tree

    def build_custom(self, official_sha, version):
        official_sha = self.fetch_commit(official_sha)
        version = self._version(version)
        tree = self._overlay_tree(official_sha, self.reviewed_tip)
        timestamp = self._run(['show', '--no-patch', '--no-notes', '--format=%ct', official_sha]).stdout.decode('ascii').strip()
        if not re.fullmatch(r'[0-9]+', timestamp):
            raise DeploymentGitError('Invalid official commit timestamp')
        identity = {
            'GIT_AUTHOR_NAME': 'NodeWarden Deployment',
            'GIT_AUTHOR_EMAIL': 'nodewarden-deploy@users.noreply.github.com',
            'GIT_COMMITTER_NAME': 'NodeWarden Deployment',
            'GIT_COMMITTER_EMAIL': 'nodewarden-deploy@users.noreply.github.com',
            'GIT_AUTHOR_DATE': '@' + timestamp + ' +0000',
            'GIT_COMMITTER_DATE': '@' + timestamp + ' +0000',
        }
        message = (f'NodeWarden deployment v{version}\n\n'
                   f'Official-Commit: {official_sha}\nOverlay-Commit: {self.reviewed_tip}\n').encode()
        custom = self._sha(self._run(['commit-tree', tree, '-p', official_sha],
                                     data=message, extra_env=identity).stdout.decode('ascii').strip())
        if self.parents(custom) != (official_sha,) or self.tree(custom) != tree:
            raise DeploymentGitError('Constructed deployment commit failed parent/tree verification')
        return custom, tree

    def validate_custom(self, tip, official, version):
        tip = self.fetch_commit(tip)
        official = self.fetch_commit(official)
        self._version(version)
        if tip == self.bootstrap:
            expected_tree = self._overlay_tree(self.baseline, self.bootstrap)
            if official != self.baseline or self.parents(tip) != (self.baseline,) or self.tree(tip) != expected_tree:
                raise DeploymentGitError('Bootstrap commit failed fixed-parent/overlay-tree verification')
        else:
            expected, expected_tree = self.build_custom(official, version)
            if tip != expected or self.parents(tip) != (official,) or self.tree(tip) != expected_tree:
                raise DeploymentGitError('Archived deployment commit does not match its deterministic official release')
        return True

    def _remote_refs(self, refs):
        output = self._run(['ls-remote', '--refs', self.remote_url, *refs]).stdout
        result = {}
        for line in output.decode('ascii').splitlines():
            sha, ref = line.split('\t', 1)
            if ref not in refs or ref in result or not SHA.fullmatch(sha):
                raise DeploymentGitError('Unexpected remote ref advertisement')
            result[ref] = sha
        return result

    def atomic_publish(self, old_sha, new_sha, version, bootstrap=False):
        old_sha = self.fetch_commit(old_sha)
        new_sha = self.fetch_commit(new_sha)
        version = self._version(version)
        official = self.parent(new_sha)
        self.validate_custom(new_sha, official, version)
        if bootstrap and (new_sha != self.bootstrap or old_sha != self.baseline
                          or not self.is_ancestor(old_sha, new_sha)):
            raise DeploymentGitError('Bootstrap must fast-forward the fixed baseline to the fixed bootstrap commit')
        if not bootstrap and new_sha == self.bootstrap:
            raise DeploymentGitError('The fixed bootstrap commit requires the explicit bootstrap path')
        production_ref = 'refs/heads/production'
        tag_ref = 'refs/tags/nodewarden-deploy/v' + version
        refs = self._remote_refs([production_ref, tag_ref])
        if refs.get(production_ref) != old_sha:
            raise DeploymentGitError('Remote production changed; refusing a stale publication')
        if tag_ref in refs and refs[tag_ref] != new_sha:
            raise DeploymentGitError('Deployment archive tag already points elsewhere; refusing publication')
        result = self._run([
            'push', '--atomic', '--force-with-lease=' + production_ref + ':' + old_sha,
            self.remote_url, new_sha + ':' + production_ref, new_sha + ':' + tag_ref,
        ])
        after = self._remote_refs([production_ref, tag_ref])
        if after.get(production_ref) != new_sha or after.get(tag_ref) != new_sha:
            raise DeploymentGitError('Remote refs drifted after publication; no fallback was attempted')
        return result.stdout.decode('utf-8', errors='replace')
