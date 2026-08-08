"""
Collect flow metadata from public SFDX repositories, to survey against.

    python harvest.py trailheadapps                 # a whole org
    python harvest.py trailheadapps/coral-cloud     # one repo
    python survey.py --dir corpus

One tree API call per repo rather than a clone: these repos are mostly LWC and
Apex, and the flows are a few dozen files among thousands. Unauthenticated, so
60 API calls an hour and one repo per call - the raw.githubusercontent
downloads do not count against that. Over the limit the API answers 403 and
this says so per repo rather than failing; wait an hour and run it again,
already-downloaded files are skipped.

Set FLOW_CORPUS to write somewhere other than ./corpus.

A corpus is not an org. Sample apps are showcases: heavy on screen components,
light on the record-triggered automation most orgs are made of. Prefer a real
org's numbers when you have them, and read these as a lower bound on variety.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

OUT = os.environ.get("FLOW_CORPUS", "corpus")


def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "flow-survey"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def repos_of(org):
    data = json.loads(get(f"https://api.github.com/orgs/{org}/repos?per_page=100"))
    return [f"{org}/{r['name']}" for r in data if not r["fork"]]


def flows_in(full_name):
    """
    Every .flow-meta.xml path in the default branch, or None if unreachable.

    Tries the two common branch names rather than asking which one it is: the
    API allows 60 calls an hour unauthenticated, and a second call per repo
    halves how many repos fit in that.
    """
    for branch in ("main", "master"):
        try:
            tree = json.loads(get(
                f"https://api.github.com/repos/{full_name}/git/trees/{branch}"
                "?recursive=1"
            ))
            break
        except urllib.error.HTTPError as problem:
            if problem.code != 404:
                return None, str(problem)
    else:
        return None, "no main or master branch"
    paths = [
        node["path"] for node in tree.get("tree", [])
        if node["path"].endswith(".flow-meta.xml")
    ]
    return (branch, paths)


def harvest(full_name):
    result = flows_in(full_name)
    if result[0] is None:
        print(f"  !! {full_name}: {result[1]}")
        return 0
    branch, paths = result
    if not paths:
        return 0
    owner, repo = full_name.split("/")
    # One directory per repo. Two sample apps ship a flow of the same name
    # without it being the same flow, and a flat directory keeps only one of
    # them. The survey qualifies its keys by this directory for that reason.
    #
    # Not a "repo__name" prefix either: a double underscore is how Salesforce
    # marks a managed-package namespace, so the survey read every file as
    # somebody else's metadata and excluded the lot.
    folder = os.path.join(OUT, repo)
    os.makedirs(folder, exist_ok=True)
    saved = 0
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        target = os.path.join(folder, name)
        if os.path.exists(target):
            continue
        url = (f"https://raw.githubusercontent.com/{full_name}/{branch}/"
               + urllib.parse.quote(path))
        try:
            body = get(url)
        except urllib.error.HTTPError as problem:
            print(f"  !! {full_name}/{path}: {problem}")
            continue
        with open(target, "wb") as handle:
            handle.write(body)
        saved += 1
    print(f"  {saved:3}  {full_name}")
    return saved


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    targets = sys.argv[1:]
    expanded = []
    for target in targets:
        if "/" in target:
            expanded.append(target)
        else:
            try:
                expanded.extend(repos_of(target))
            except urllib.error.HTTPError as problem:
                print(f"!! org {target}: {problem}")
    total = sum(harvest(name) for name in expanded)
    on_disk = sum(len(files) for _, _, files in os.walk(OUT))
    print(f"\n{total} new flows from {len(expanded)} repos -> {OUT}")
    print(f"{on_disk} files in the corpus, across "
          f"{len(os.listdir(OUT))} repos")
