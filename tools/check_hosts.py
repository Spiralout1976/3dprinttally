"""Read-only reachability check for each hostname this app is served under.

Run it from every client network you expect to use, so a proxy or firewall rule
that only works from one subnet is caught before users find it.

    python3 tools/check_hosts.py tally.example.com tally.lan

With no arguments the hostnames are read from the TRUSTED_HOSTS environment
variable. Exit status is 0 only when every name answers 200 or 401.
"""
import json
import os
import sys
import urllib.error
import urllib.request

hosts = [h.strip() for h in (sys.argv[1:] or os.environ.get('TRUSTED_HOSTS', '').split(',')) if h.strip()]
if not hosts:
    sys.exit('Pass one or more hostnames, or set TRUSTED_HOSTS.')

results = []
for host in hosts:
    try:
        request = urllib.request.Request('https://' + host, method='HEAD')
        with urllib.request.urlopen(request, timeout=10) as response:
            results.append({'host': host, 'status': response.status, 'https': True})
    except urllib.error.HTTPError as error:
        results.append({'host': host, 'status': error.code, 'https': True,
                        'authentication_required': error.code == 401})
    except Exception as error:
        results.append({'host': host, 'error': str(error)})

print(json.dumps(results, indent=2))
raise SystemExit(1 if any('error' in r or r.get('status', 0) not in (200, 401) for r in results) else 0)
