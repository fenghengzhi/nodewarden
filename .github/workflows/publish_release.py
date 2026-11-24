import base64
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from deployment_git import DeploymentGit, DeploymentGitError

UPSTREAM = 'shuaiplus/nodewarden'
FORK = 'fenghengzhi/nodewarden'
CONTROL = 'release-control'
BASELINE = '29c2de852411f3aa66de0c8a2ea16e7f87fa932c'
REVIEWED_TIP = 'da2bf6f062fe00c66c3ab601815a6ab0b52748a4'
BOOTSTRAP = 'd928f8945a9be575e7c2f899d827f20961653dc1'
MAX_PR_PAGES = 10
SEMVER = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
SHA = re.compile(r'[0-9a-f]{40}')
HEAD = re.compile(r'release-update/v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))')

class WorkflowError(RuntimeError):
    pass

class ApprovalError(WorkflowError):
    pass

class ApiError(WorkflowError):
    def __init__(self, method, path, status):
        self.status = status
        super().__init__(f'GitHub API {method} {path} returned HTTP {status}')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None

# Requests must remain on their explicitly validated api.github.com URL.
urllib.request.install_opener(urllib.request.build_opener(NoRedirect()))

def api(path, *, method='GET', data=None, authenticated=False, missing_ok=False):
    headers = {
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'nodewarden-approved-release-publisher',
    }
    root = 'repos/' + FORK
    if authenticated:
        if path != root and not path.startswith(root + '/'):
            raise WorkflowError('Authenticated API access outside this fork is forbidden')
        if method != 'GET':
            raise WorkflowError('Publisher REST access is read-only; publishing uses an atomic Git push with an explicit lease')
    elif method != 'GET' or not path.startswith('repos/' + UPSTREAM + '/'):
        raise WorkflowError('Upstream API access must be read-only and use the fixed official repository')
    body = None if data is None else json.dumps(data).encode()
    if body is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(
        'https://api.github.com/' + path, data=body, headers=headers, method=method
    )
    # The same fork-scoped token authenticates upstream GETs to avoid
    # anonymous API limits. It grants no extra upstream write access.
    # Keep it out of redirectable headers as a second isolation layer.
    request.add_unredirected_header('Authorization', 'Bearer ' + os.environ['GH_TOKEN'])
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if missing_ok and status == 404:
            return None
        # Never include request headers or API response bodies in errors.
        raise ApiError(method, path, status) from None

def own(path='', **kwargs):
    return api('repos/' + FORK + ('/' + path if path else ''), authenticated=True, **kwargs)

def public(path, **kwargs):
    return api('repos/' + UPSTREAM + '/' + path, **kwargs)

def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None

def version_tuple(value):
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise WorkflowError('Expected a stable MAJOR.MINOR.PATCH version')
    return tuple(map(int, value.split('.')))

def ref_sha(branch):
    value = own('git/ref/heads/' + branch)['object']
    if value.get('type') != 'commit' or not valid_sha(value.get('sha')):
        raise WorkflowError('Invalid ' + branch + ' branch ref')
    return value['sha']

def is_ancestor(ancestor, descendant):
    if ancestor == descendant:
        return True
    result = own('compare/' + ancestor + '...' + descendant)
    return (result.get('status') in ('ahead', 'identical')
            and result.get('merge_base_commit', {}).get('sha') == ancestor)

def candidate_version(pr):
    head, base = pr.get('head') or {}, pr.get('base') or {}
    user = pr.get('user') or {}
    match = HEAD.fullmatch(head.get('ref', ''))
    if (pr.get('state') != 'closed' or not pr.get('merged_at')
            or user.get('login') != 'github-actions[bot]' or user.get('type') != 'Bot'
            or (head.get('repo') or {}).get('full_name', '').lower() != FORK
            or (base.get('repo') or {}).get('full_name', '').lower() != FORK
            or base.get('ref') != CONTROL or match is None):
        return None
    return match.group(1)

def official_release(version):
    version_tuple(version)
    tag = 'v' + version
    release = public('releases/tags/' + tag, missing_ok=True)
    if (release is None or release.get('tag_name') != tag
            or release.get('draft') is not False or release.get('prerelease') is not False
            or not release.get('published_at')):
        raise ApprovalError(f'{tag} is not a published stable official release')
    target = public('git/ref/tags/' + tag)['object']
    for _ in range(16):
        if not valid_sha(target.get('sha')):
            raise ApprovalError('Invalid official tag object')
        if target.get('type') == 'commit':
            break
        if target.get('type') != 'tag':
            raise ApprovalError('Official tag does not resolve to a commit')
        target = public('git/tags/' + target['sha'])['object']
    else:
        raise ApprovalError('Too many nested annotated tags')
    return target['sha']

def approved_release(number, control_sha):
    pr = own('pulls/' + str(number))
    version = candidate_version(pr)
    if version is None or pr.get('merged') is not True:
        raise ApprovalError(f'PR #{number} is not a merged same-fork release approval from github-actions[bot]')
    merge_sha = pr.get('merge_commit_sha')
    if not valid_sha(merge_sha) or not is_ancestor(merge_sha, control_sha):
        raise ApprovalError(f'PR #{number} is not included in the current release-control history')
    official_sha = official_release(version)
    if pr['head'].get('sha') != official_sha:
        raise ApprovalError(f'PR #{number} head is not the exact official tag commit')
    return {'number': number, 'version': version, 'tag': 'v' + version,
            'sha': official_sha, 'approval_sha': merge_sha}

def choose_approval(control_sha, requested):
    if requested:
        if not re.fullmatch(r'[1-9][0-9]*', requested):
            raise WorkflowError('pr_number must be a positive integer')
        return approved_release(int(requested), control_sha)
    candidates = []
    for page in range(1, MAX_PR_PAGES + 1):
        query = urllib.parse.urlencode({
            'state': 'closed', 'base': CONTROL, 'sort': 'updated',
            'direction': 'desc', 'per_page': 100, 'page': page,
        })
        records = own('pulls?' + query)
        for pr in records:
            version = candidate_version(pr)
            number = pr.get('number')
            if version is not None and isinstance(number, int) and not isinstance(number, bool) and number > 0:
                candidates.append((version_tuple(version), number))
        if len(records) < 100:
            break
    else:
        raise WorkflowError('Too many closed PRs to reconcile completely; provide an explicit pr_number')
    for _, number in sorted(candidates, reverse=True):
        try:
            return approved_release(number, control_sha)
        except ApprovalError as error:
            print('Skipping unverified approval: ' + str(error))
    return None

def production_version(commit):
    record = own('contents/package.json?' + urllib.parse.urlencode({'ref': commit}))
    if record.get('type') != 'file' or record.get('encoding') != 'base64':
        raise WorkflowError('production/package.json must be an ordinary JSON file')
    value = json.loads(base64.b64decode(record['content']))['version']
    version_tuple(value)
    return value

def archive_sha(version):
    version_tuple(version)
    record = own('git/ref/tags/nodewarden-deploy/v' + version, missing_ok=True)
    if record is None:
        return None
    value = record.get('object') or {}
    if value.get('type') != 'commit' or not valid_sha(value.get('sha')):
        raise WorkflowError('Deployment archive must be an immutable lightweight commit tag')
    return value['sha']

def validated_production(git):
    tip = ref_sha('production')
    if tip == BASELINE:
        raise WorkflowError('Run workflow_dispatch with empty pr_number once to initialize the reviewed v1.8.1 deployment')
    git.fetch_commit(tip)
    installed = production_version(tip)
    official = git.parent(tip)
    if tip == BOOTSTRAP:
        if installed != '1.8.1' or official != BASELINE:
            raise WorkflowError('Initial deployment identity does not match the reviewed baseline')
    elif official_release(installed) != official:
        raise WorkflowError('Production sole parent is not the exact published official version')
    git.fetch_commit(BASELINE)
    git.fetch_commit(official)
    if not git.is_ancestor(BASELINE, official):
        raise WorkflowError('Production official parent does not retain the intentional baseline history')
    git.validate_custom(tip, official, installed)
    if archive_sha(installed) != tip:
        raise WorkflowError('Current production is not archived at its immutable version tag; refusing to rewrite it')
    return {'tip': tip, 'official': official, 'version': installed}

def bootstrap(git):
    # This narrow exception is only called by an explicit empty manual dispatch.
    if ref_sha('production') != BASELINE:
        return False
    git.fetch_commit(BASELINE)
    git.fetch_commit(BOOTSTRAP)
    git.validate_custom(BOOTSTRAP, BASELINE, '1.8.1')
    if git.parent(BOOTSTRAP) != BASELINE or not git.is_ancestor(BASELINE, BOOTSTRAP):
        raise WorkflowError('Bootstrap must be the reviewed one-parent fast-forward')
    existing = archive_sha('1.8.1')
    if existing is not None and existing != BOOTSTRAP:
        raise WorkflowError('Existing v1.8.1 archive differs from the reviewed initial deployment')
    git.atomic_publish(BASELINE, BOOTSTRAP, '1.8.1', bootstrap=True)
    if ref_sha('production') != BOOTSTRAP or archive_sha('1.8.1') != BOOTSTRAP:
        raise WorkflowError('Bootstrap push returned but both remote refs could not be verified')
    print('Initialized v1.8.1 at the reviewed deployment commit ' + BOOTSTRAP + '. No new release was published.')
    return True

def publish(approved, git):
    state = validated_production(git)
    if version_tuple(approved['version']) <= version_tuple(state['version']):
        print('No-op: the validated archived production deployment already has this or a newer version.')
        return
    git.fetch_commit(approved['sha'])
    if not git.is_ancestor(state['official'], approved['sha']):
        raise WorkflowError('Approved official release does not descend from the currently deployed official parent')
    if not is_ancestor(approved['approval_sha'], ref_sha(CONTROL)):
        raise ApprovalError('Approval is no longer included in release-control')
    target, tree = git.build_custom(approved['sha'], approved['version'])
    if git.parents(target) != (approved['sha'],) or git.tree(target) != tree:
        raise WorkflowError('Generated deployment commit failed its exact parent/tree assertions')
    existing = archive_sha(approved['version'])
    if existing is not None and existing != target:
        raise WorkflowError('Version archive already points elsewhere; immutable tags are never overwritten')
    # This is the only authorized non-fast-forward branch change. The current
    # tip was fully validated and archived above, and the push atomically creates
    # the new archive with an explicit production old-SHA lease.
    git.atomic_publish(state['tip'], target, approved['version'])
    if ref_sha('production') != target or archive_sha(approved['version']) != target:
        raise WorkflowError('Atomic publish returned but its two remote refs could not be verified')
    print(f'Published {approved["tag"]}: deployment {target}, sole official parent {approved["sha"]}, tree {tree}.')

def main():
    if (os.environ.get('REPOSITORY', '').lower() != FORK
            or os.environ.get('GITHUB_REF') != 'refs/heads/' + CONTROL
            or os.environ.get('GITHUB_EVENT_NAME') not in ('pull_request', 'workflow_dispatch')):
        raise WorkflowError('Run only in the intended fork on release-control')
    metadata = own()
    if (metadata.get('fork') is not True or metadata.get('private') is not False
            or metadata.get('full_name', '').lower() != FORK
            or (metadata.get('source') or {}).get('full_name', '').lower() != UPSTREAM
            or metadata.get('default_branch') != CONTROL):
        raise WorkflowError('Expected the public official fork with release-control as its default branch')
    requested = os.environ.get('PR_NUMBER', '').strip()
    if os.environ['GITHUB_EVENT_NAME'] == 'pull_request' and not requested:
        raise WorkflowError('Pull request event is missing its PR number')
    with tempfile.TemporaryDirectory(prefix='nodewarden-deploy-') as directory:
        git = DeploymentGit(directory, 'https://github.com/' + FORK + '.git',
                            token=os.environ['GH_TOKEN'], reviewed_tip=REVIEWED_TIP,
                            baseline=BASELINE, bootstrap=BOOTSTRAP)
        if os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch' and not requested and bootstrap(git):
            return
        approved = choose_approval(ref_sha(CONTROL), requested)
        if approved is None:
            print('No verified merged release approval; production is unchanged.')
            return
        publish(approved, git)

if __name__ == '__main__':
    try:
        main()
    except (WorkflowError, DeploymentGitError, urllib.error.URLError, TimeoutError) as error:
        if isinstance(error, (WorkflowError, DeploymentGitError)):
            print('::error::' + str(error))
            if isinstance(error, DeploymentGitError):
                print('::error::Git operations may be rejected by lease/tag races, repository rules, or GitHub Workflows write restrictions. No REST force:true or broader-token fallback is attempted.')
        else:
            print('::error::GitHub API connection failed; no credentials or response body are logged.')
        raise SystemExit(1)
