"""Read-only comparison against the public WorkBuddy main branch."""
import json
import re
import sys
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def compare(head: str, *, open_request=None) -> dict:
    if not re.fullmatch(r'[0-9a-f]{40}', head):
        raise ValueError('Invalid Git commit')
    url = f'https://api.github.com/repos/wuye999/wb/compare/{head}...main?per_page=1'
    request = Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'sellerops'})
    if open_request is None:
        open_request = build_opener(_NoRedirect).open
    with open_request(request, timeout=1) as response:
        limit = 4 * 1024 * 1024
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError('GitHub comparison too large')
        comparison = json.loads(body)
    ahead, behind = comparison['ahead_by'], comparison['behind_by']
    if type(ahead) is not int or type(behind) is not int or min(ahead, behind) < 0:
        raise ValueError('Invalid GitHub comparison counts')
    status = 'diverged' if ahead and behind else 'ahead' if ahead else 'behind' if behind else 'identical'
    if comparison['status'] != status:
        raise ValueError('Inconsistent GitHub comparison')
    return {'status': {'ahead': 'update_available', 'identical': 'up_to_date',
                       'behind': 'local_ahead', 'diverged': 'diverged'}[status], 'ahead_by': ahead}


def main() -> int:
    try:
        print(json.dumps(compare(sys.argv[1])))
    except Exception:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
