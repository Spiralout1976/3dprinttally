"""Query OSV using only names/versions of installed application dependencies."""
import json
from pathlib import Path
import urllib.request
from importlib.metadata import version
from packaging.requirements import Requirement

requirements=[]
for line in (Path(__file__).resolve().parents[1]/'requirements.txt').read_text().splitlines():
    if not line.strip() or line.startswith('#'):continue
    requirement=Requirement(line)
    if requirement.marker and not requirement.marker.evaluate():continue
    requirements.append({'package':{'name':requirement.name,'ecosystem':'PyPI'},'version':version(requirement.name)})
request=urllib.request.Request('https://api.osv.dev/v1/querybatch',data=json.dumps({'queries':requirements}).encode(),headers={'Content-Type':'application/json'})
result=json.load(urllib.request.urlopen(request,timeout=30))
findings=[]
for item,response in zip(requirements,result['results']):
    if response.get('vulns'):findings.append({**item,'advisories':response['vulns']})
print(json.dumps({'packages_checked':len(requirements),'findings':findings},indent=2))
raise SystemExit(1 if findings else 0)
