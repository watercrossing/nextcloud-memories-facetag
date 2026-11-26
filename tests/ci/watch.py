#!/usr/bin/env python3
"""Watch for new Memories, Recognize and Nextcloud releases, test them, and propose widening.

    watch.py plan                              # what is new and what would be done (reads only)
    watch.py run [--dry-run] [--only KIND,…] [--force] [--fixture FILE]

Run nightly by .github/workflows/release-watch.yml. Three kinds of candidate, each on its own
branch, each compared with what is already open so a rerun with nothing new does nothing:

- repin       a new release within the tested majors (Compatibility::TESTED_MAJORS, info.xml's
              Nextcloud range): branch watch/repin re-pinning tests/ci/versions.json, a pull
              request, and the compatibility matrix dispatched on the branch — that run is the test.
- widen       a new major of Memories or Recognize, or a new released Nextcloud major: branch
              watch/widen with TESTED_MAJORS widened, info.xml's max-version raised, the app's
              patch version bumped, the README's version notes and versions.json updated; the matrix
              is dispatched on it and awaited. Every cell green → a pull request. Anything red → an
              issue (one, updated in place) naming the failed cells and linking the run.
- prerelease  a beta/RC of a Nextcloud major newer than any release: branch watch/nextcloud-next
              (the widened state, plus max-version raised to the prerelease's major) and one cell
              — the prerelease with the newest app releases, force-enabled where they don't yet
              declare support. Allowed to fail: the outcome goes into one issue per major (open
              while failing, closed while passing); it never fails the watcher.

Pull requests, pushes and issues made with a workflow's GITHUB_TOKEN start no workflows, so the
watcher dispatches compat.yml on its branches itself (workflow_dispatch is the exception). The
token needs contents, pull-requests, issues and actions: write, and the repository must allow
GitHub Actions to create pull requests (Settings → Actions → General → Workflow permissions).

--dry-run reads (App Store, Docker image list, prereleases, existing PRs and issues) but changes
nothing. --fixture FILE fakes the release sources (see tests/ci/watch-fixtures/README.md); with it
the branches are watch-sim/NAME/…, titles say "[simulation NAME]" and pull requests are drafts.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import versions as V  # noqa: E402

ROOT = V.ROOT
INFO = ROOT / "appinfo/info.xml"
COMPAT = ROOT / "lib/Service/Compatibility.php"
README = ROOT / "README.md"
PINNED = V.PINNED
APPS = {"memories": "Memories", "recognize": "Recognize"}
KINDS = ("repin", "widen", "prerelease")
WORKFLOW = "compat.yml"
POLL_SECONDS = 60
# Candidates are built on this commit (the checkout by default) and proposed against WATCH_BASE
BASE_REF = os.environ.get("WATCH_BASE_REF", "HEAD")
WAIT_MINUTES = 150


def base_file(path: str) -> str:
    return run(["git", "show", f"{BASE_REF}:{path}"], cwd=ROOT)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def major(v: str) -> int:
    return int(re.match(r"\d+", v).group())


def and_list(items: list) -> str:
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


# --- the files a candidate changes ---------------------------------------------------------------

def render_compat(src: str, tested: dict[str, list[int]]) -> str:
    block = re.search(r"TESTED_MAJORS\s*=\s*\[(.*?)\];", src, re.S)
    body = block.group(1)
    for app, majors in tested.items():
        body = re.sub(rf"('{app}'\s*=>\s*)\[[^\]]*\]", lambda m: m.group(1) + "[" + ", ".join(map(str, majors)) + "]", body)
    return src[:block.start(1)] + body + src[block.end(1):]


def render_info(src: str, max_version: str, bump: bool) -> str:
    src = re.sub(r'(<nextcloud\s+min-version="\d+"\s+max-version=")\d+(")', rf"\g<1>{max_version}\g<2>", src)
    if bump:
        src = re.sub(r"<version>(\d+)\.(\d+)\.(\d+)</version>",
                     lambda m: f"<version>{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}</version>", src)
    return src


def render_readme(src: str, nc: list[str], tested: dict[str, list[int]], max_version: str) -> str:
    values = {"nextcloud": and_list(nc), "nextcloud-max": max_version,
              **{app: and_list(majors) for app, majors in tested.items()}}
    return re.sub(r"<!--tested:([\w-]+)-->.*?<!--/tested-->",
                  lambda m: f"<!--tested:{m.group(1)}-->{values[m.group(1)]}<!--/tested-->", src)


def app_version(info: str) -> str:
    return re.search(r"<version>(.*?)</version>", info).group(1)


# --- planning --------------------------------------------------------------------------------------

@dataclass
class Candidate:
    kind: str
    title: str
    what: str                         # one line: what is new
    nc: list[str]                     # Nextcloud majors pinned
    tested: dict[str, list[int]]      # TESTED_MAJORS
    chosen: dict[str, dict[str, list[str]]]  # app -> nc -> versions (versions.json's content)
    max_version: str
    bump: bool
    dispatch: dict[str, str] = field(default_factory=dict)  # compat.yml inputs (empty: the branch's matrix)
    notes: list[str] = field(default_factory=list)

    def files(self, base: dict[str, str], pin: bool) -> dict[str, str]:
        """{repo path: content}. pin=False leaves versions.json out (it needs every tarball downloaded)."""
        out = {
            "lib/Service/Compatibility.php": render_compat(base["lib/Service/Compatibility.php"], self.tested),
            "appinfo/info.xml": render_info(base["appinfo/info.xml"], self.max_version, self.bump),
            "README.md": render_readme(base["README.md"], self.nc, self.tested, self.max_version),
        }
        if pin:
            pinned = V.compute_pin(self.nc, self.tested)
            assert {a: {n: [r["version"] for r in rs] for n, rs in p.items()} for a, p in pinned["apps"].items()} == self.chosen
            out["tests/ci/versions.json"] = json.dumps(pinned, indent=2) + "\n"
        return {k: v for k, v in out.items() if k not in base or v != base[k]}

    def key(self) -> str:
        """Identifies what is proposed and tested, without downloading anything."""
        spec = {k: getattr(self, k) for k in ("kind", "nc", "tested", "chosen", "max_version", "bump", "dispatch")}
        return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]

    def cells(self) -> list[str]:
        if self.dispatch:
            return [f"NC {self.dispatch['nextcloud']} · Memories {self.dispatch['memories']} · Recognize {self.dispatch['recognize']}"]
        out = []
        for nc in self.nc:
            for m in self.chosen["memories"][nc]:
                for r in self.chosen["recognize"][nc]:
                    out.append(f"NC {nc} · Memories {m} · Recognize {r}")
        return out


def pinned_versions(pinned: dict) -> dict[str, dict[str, list[str]]]:
    return {app: {nc: [r["version"] for r in rels] for nc, rels in per_nc.items()} for app, per_nc in pinned["apps"].items()}


def describe_diff(old: dict, new: dict) -> list[str]:
    lines = []
    for app in new:
        for nc in sorted(set(old.get(app, {})) | set(new[app]), key=int):
            a, b = old.get(app, {}).get(nc, []), new[app].get(nc, [])
            if a != b:
                lines.append(f"Nextcloud {nc}: {APPS[app]} {', '.join(a) or '—'} → {', '.join(b) or '—'}")
    return lines


def plan() -> dict:
    """What is new. Reads the App Store, the Docker image's versions and the prerelease server."""
    base = {p: base_file(p) for p in ("appinfo/info.xml", "lib/Service/Compatibility.php", "README.md")}
    cur_nc = V.nextcloud_majors(base["appinfo/info.xml"])
    tested = V.tested_majors(base["lib/Service/Compatibility.php"])
    pinned = json.loads(base_file("tests/ci/versions.json"))
    have = pinned_versions(pinned)
    released = V.nextcloud_releases()
    newest_released = max(int(m) for m in released)
    out: dict = {"base": base, "current": {"nextcloud": cur_nc, "tested": tested, "pinned": have},
                 "released": released, "notes": [], **{k: None for k in KINDS}}

    # repin: the same majors, newer releases
    want = V.choose(cur_nc, tested)
    if want != have or pinned["nextcloud"] != cur_nc:
        diff = describe_diff(have, want)
        out["repin"] = Candidate(
            kind="repin", title="Re-pin tested releases: " + "; ".join(diff),
            what="new releases within the tested majors: " + "; ".join(diff),
            nc=cur_nc, tested=tested, chosen=want, max_version=cur_nc[-1], bump=False, notes=diff)

    # widen: newer majors of the apps, and newer released Nextcloud majors
    cand_nc = cur_nc + [str(m) for m in range(int(cur_nc[-1]) + 1, newest_released + 1)]
    cand = copy.deepcopy(tested)
    for app in tested:
        for nc in cand_nc:
            _, rels = V.releases(nc, app)
            if rels and (m := major(rels[0]["version"])) > max(cand[app]):
                cand[app] = sorted(set(cand[app]) | {m})
    # a Nextcloud major can only be tested once both apps have a release for it
    usable = list(cur_nc)
    for nc in cand_nc[len(cur_nc):]:
        missing = [APPS[a] for a in cand if not [r for r in V.releases(nc, a)[1] if major(r["version"]) in cand[a]]]
        if missing:
            out["notes"].append(f"Nextcloud {nc} is released, but {' and '.join(missing)} has no release for it yet")
            break
        usable.append(nc)
    # app majors only offered for a Nextcloud major that can't be tested yet stay out
    for app in cand:
        offered = {major(r["version"]) for nc in usable for r in V.releases(nc, app)[1]}
        cand[app] = [m for m in cand[app] if m in tested[app] or m in offered]
    if usable != cur_nc or cand != tested:
        new_bits = [f"Nextcloud {nc}" for nc in usable[len(cur_nc):]]
        new_bits += [f"{APPS[a]} {m}" for a in cand for m in cand[a] if m not in tested[a]]
        chosen = V.choose(usable, cand)
        out["widen"] = Candidate(
            kind="widen", title="Widen tested versions: " + ", ".join(new_bits),
            what="a new major: " + ", ".join(new_bits),
            nc=usable, tested=cand, chosen=chosen, max_version=usable[-1], bump=True,
            notes=describe_diff(have, chosen))

    # prerelease: a beta/RC of an unreleased Nextcloud major, on top of the widened state
    pres = [p for p in V.prereleases() if major(p) > newest_released]
    if pres:
        pre = pres[-1]
        pm = str(major(pre))
        w = out["widen"]
        nc, tst = (w.nc, w.tested) if w else (cur_nc, tested)
        tst = copy.deepcopy(tst)
        picks = {}
        for app in tst:
            # the newest release for the prerelease's major if there is one, else for the newest released major
            rels = V.releases(pm, app)[1] or V.releases(str(newest_released), app)[1]
            picks[app] = rels[0]["version"]
            tst[app] = sorted(set(tst[app]) | {major(picks[app])})
        out["prerelease"] = Candidate(
            kind="prerelease", title=f"Nextcloud {pm} prerelease",
            what=f"Nextcloud {pre}, with Memories {picks['memories']} and Recognize {picks['recognize']}",
            nc=nc, tested=tst, chosen=w.chosen if w else have, max_version=pm, bump=False,
            dispatch={"nextcloud": pre, "memories": picks["memories"], "recognize": picks["recognize"]},
            notes=[f"Memories {picks['memories']}, Recognize {picks['recognize']}: "
                   "force-enabled where they don't declare support for the prerelease yet"])
    return out


# --- GitHub ----------------------------------------------------------------------------------------

def run(cmd: list[str], cwd: Path | None = None, check: bool = True, input: str | None = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: exit {r.returncode}\n{r.stdout}{r.stderr}")
    return r.stdout


class GitHub:
    def __init__(self, repo: str, dry: bool, sim: str | None):
        self.repo, self.dry, self.sim = repo, dry, sim
        self.prefix = f"watch-sim/{sim}/" if sim else "watch/"
        self.label = "release-watch-sim" if sim else "release-watch"
        self.tag = f"release-watch{'[sim=' + sim + ']' if sim else ''}"
        self.actions: list[str] = []

    def gh(self, *args: str, write: bool = False, input: str | None = None) -> str:
        if write and self.dry:
            self.actions.append("gh " + " ".join(a if len(a) < 80 else a[:77] + "…" for a in args))
            log(f"  [dry-run] gh {' '.join(args[:4])} …")
            return ""
        return run(["gh", *args, "--repo", self.repo] if args[0] not in ("api",) else ["gh", *args], input=input)

    def branch(self, kind: str) -> str:
        return self.prefix + {"repin": "repin", "widen": "widen", "prerelease": "nextcloud-next"}[kind]

    def title(self, title: str) -> str:
        return f"[simulation {self.sim}] {title}" if self.sim else title

    def marker(self, kind: str, **kv: str) -> str:
        return f"<!-- {self.tag}:{kind} " + " ".join(f"{k}={v}" for k, v in kv.items()) + " -->"

    def find_marked(self, what: str, kind: str) -> list[dict]:
        """PRs or issues carrying this watcher's marker for KIND, newest first."""
        fields = "number,state,body,url,title" + (",headRefName,mergedAt" if what == "pr" else "")
        items = json.loads(self.gh(what, "list", "--state", "all", "--label", self.label, "--limit", "100",
                                   "--json", fields) or "[]")
        return [i for i in items if f"<!-- {self.tag}:{kind} " in (i.get("body") or "")]

    def ensure_label(self) -> None:
        self.gh("label", "create", self.label, "--force", "--color", "BFD4F2",
                "--description", "Opened by the release watcher (tests/ci/watch.py)", write=True)

    def push(self, kind: str, files: dict[str, str], message: str) -> str:
        """Commit FILES on top of HEAD as the candidate branch and force-push it; returns the commit."""
        branch = self.branch(kind)
        if self.dry:
            names = sorted(files) + ([] if "tests/ci/versions.json" in files else ["tests/ci/versions.json (not resolved in a dry run)"])
            self.actions.append(f"push {branch}: {', '.join(names)}")
            log(f"  [dry-run] push {branch} ({', '.join(names)})")
            return "0" * 40
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "wt"
            run(["git", "worktree", "add", "--detach", str(wt), BASE_REF], cwd=ROOT)
            try:
                for path, content in files.items():
                    (wt / path).write_text(content)
                run(["git", "add", *files], cwd=wt)
                run(["git", "-c", "user.name=github-actions[bot]",
                     "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                     "commit", "-q", "-m", message], cwd=wt)
                sha = run(["git", "rev-parse", "HEAD"], cwd=wt).strip()
                run(["git", "push", "-q", "--force", "origin", f"HEAD:refs/heads/{branch}"], cwd=wt)
            finally:
                run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT, check=False)
        log(f"  pushed {branch} at {sha[:10]}")
        return sha

    def dispatch(self, kind: str, sha: str, inputs: dict[str, str]) -> str | None:
        """Start compat.yml on the candidate branch; returns the run's URL (None in a dry run)."""
        branch = self.branch(kind)
        args = ["workflow", "run", WORKFLOW, "--ref", branch]
        for k, v in inputs.items():
            args += ["-f", f"{k}={v}"]
        started = time.time()
        out = self.gh(*args, write=True)
        if self.dry:
            return None
        if m := re.search(r"https://\S+/actions/runs/\d+", out):
            return m.group()
        for _ in range(30):  # older gh prints no run URL: find the run by its commit
            time.sleep(5)
            runs = json.loads(self.gh("run", "list", "--workflow", WORKFLOW, "--branch", branch, "--event",
                                      "workflow_dispatch", "--limit", "10", "--json", "url,headSha,createdAt"))
            for r in runs:
                if r["headSha"] == sha and time.mktime(time.strptime(r["createdAt"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone >= started - 120:
                    return r["url"]
        raise RuntimeError(f"dispatched {WORKFLOW} on {branch} but cannot find the run")

    def wait(self, urls: list[str]) -> dict[str, dict]:
        """Wait for runs to finish: {url: {"ok": bool, "failed": [job names], "conclusion": …}}."""
        done: dict[str, dict] = {}
        retried: dict[str, list[str]] = {}
        deadline = time.time() + WAIT_MINUTES * 60
        while len(done) < len(urls):
            if time.time() > deadline:
                for u in urls:
                    done.setdefault(u, {"ok": False, "conclusion": "timed out waiting", "failed": []})
                break
            for u in urls:
                if u in done:
                    continue
                rid = u.rsplit("/", 1)[1]
                v = json.loads(self.gh("run", "view", rid, "--json", "status,conclusion,jobs,attempt"))
                if v["status"] == "completed":
                    failed = [j["name"] for j in v["jobs"] if j["conclusion"] not in ("success", "skipped")]
                    log(f"  {u} (attempt {v['attempt']}): {v['conclusion']}" + (f" ({', '.join(failed)})" if failed else ""))
                    # a runner or network hiccup shouldn't decide a major: rerun the failed jobs once
                    if v["conclusion"] == "failure" and v["attempt"] < 2:
                        self.gh("run", "rerun", rid, "--failed")
                        retried[u] = failed
                        continue
                    done[u] = {"ok": v["conclusion"] == "success", "conclusion": v["conclusion"], "failed": failed,
                               "retried": retried.get(u, [])}
            if len(done) < len(urls):
                time.sleep(POLL_SECONDS)
        return done


# --- the watcher ----------------------------------------------------------------------------------

def body(c: Candidate, intro: str, run_url: str | None, gh: GitHub, key: str, base: str, extra: str = "") -> str:
    lines = [intro, ""]
    if run_url:
        lines += [f"Compatibility run: {run_url}", ""]
    if c.kind != "repin":
        lines += [f"- `Compatibility::TESTED_MAJORS`: " + ", ".join(f"{APPS[a]} {and_list(m)}" for a, m in c.tested.items()),
                  f"- `appinfo/info.xml`: Nextcloud up to {c.max_version}" + (" (and the app's patch version bumped)" if c.bump else "")]
    lines += [f"- {n}" for n in c.notes]
    lines += ["", "Cells:"] + [f"- {cell}" for cell in c.cells()]
    if extra:
        lines += ["", extra]
    lines += ["", f"_Opened by the release watcher (`tests/ci/watch.py`, {os.environ.get('WATCH_RUN_URL', 'run by hand')})._",
              gh.marker(c.kind, key=key, base=base)]
    return "\n".join(lines)


def marker_value(text: str, name: str) -> str | None:
    m = re.search(rf"\b{name}=(\S+)", text or "")
    return m.group(1) if m else None


def watch(args) -> int:
    if args.fixture:
        os.environ["FACETAG_WATCH_FIXTURE"] = str(Path(args.fixture).resolve())
    sim = Path(args.fixture).stem if args.fixture else None
    repo = os.environ.get("GITHUB_REPOSITORY") or re.sub(
        r"^.*github\.com[:/](.*?)(\.git)?$", r"\1", run(["git", "remote", "get-url", "origin"], cwd=ROOT).strip())
    gh = GitHub(repo, args.dry_run, sim)
    only = set(args.only.split(",")) if args.only else set(KINDS)
    p = plan()
    base_tree = run(["git", "rev-parse", f"{BASE_REF}^{{tree}}"], cwd=ROOT).strip()[:16]
    for n in p["notes"]:
        log(f"note: {n}")
    summary: list[str] = []
    gh.ensure_label()

    pending: list[tuple[Candidate, str, str]] = []  # (candidate, run url, key)

    # repin: PR at once; the dispatched run is its check
    c = p["repin"] if "repin" in only else None
    prs = gh.find_marked("pr", "repin") if "repin" in only else []
    open_pr = next((x for x in prs if x["state"] == "OPEN"), None)
    if c is None:
        if open_pr and "repin" in only:
            gh.gh("pr", "close", str(open_pr["number"]), "--delete-branch", "--comment",
                  "Nothing to re-pin any more (main has these releases, or newer ones replaced them).", write=True)
            summary.append(f"repin: nothing new; closed stale {open_pr['url']}")
        elif "repin" in only:
            summary.append("repin: nothing new")
    else:
        key = c.key()
        declined = next((x for x in prs if x["state"] == "CLOSED" and marker_value(x["body"], "key") == key), None)
        if not args.force and open_pr and marker_value(open_pr["body"], "key") == key:
            summary.append(f"repin: unchanged, already proposed in {open_pr['url']}")
        elif not args.force and declined:
            summary.append(f"repin: these releases were declined in {declined['url']}; not proposing again")
        else:
            log(f"repin: {c.what}")
            sha = gh.push("repin", c.files(p["base"], pin=not gh.dry), f"Re-pin tested releases\n\n" + "\n".join(c.notes))
            url = gh.dispatch("repin", sha, {})
            text = body(c, f"The release watcher found {c.what}. This re-pins `tests/ci/versions.json`; "
                           "the compatibility run below (dispatched by the watcher, since its pushes start no workflows) is the test.",
                        url, gh, key, base_tree)
            if open_pr:
                gh.gh("pr", "edit", str(open_pr["number"]), "--title", gh.title(c.title), "--body-file", "-", write=True, input=text)
                summary.append(f"repin: updated {open_pr['url']} ({c.what}); run {url}")
            else:
                out = create_pr(gh, "repin", c.title, text)
                summary.append(f"repin: opened {out} ({c.what}); run {url}")

    # widen and prerelease: push, dispatch, wait, then decide
    for kind in ("widen", "prerelease"):
        if kind not in only:
            continue
        c = p[kind]
        if kind == "widen":
            prs = gh.find_marked("pr", kind)
            issues = gh.find_marked("issue", kind)
        else:
            prs = []
            issues = [i for i in gh.find_marked("issue", kind) if c is None or i["title"].endswith(gh.title(c.title))]
        # a prerelease issue whose major is now released has done its job
        for i in gh.find_marked("issue", kind) if kind == "prerelease" else []:
            m = re.search(r"Nextcloud (\d+) prerelease", i["title"])
            if i["state"] == "OPEN" and m and int(m.group(1)) <= max(int(x) for x in p["released"]):
                gh.gh("issue", "close", str(i["number"]), "--comment",
                      f"Nextcloud {m.group(1)} is released; the watcher now tests it as a new major.", write=True)
                summary.append(f"prerelease: closed {i['url']} (released)")
        if c is None:
            summary.append(f"{kind}: nothing new")
            continue
        key = c.key()
        open_pr = next((x for x in prs if x["state"] == "OPEN"), None)
        issue = issues[0] if issues else None
        if not args.force and open_pr and marker_value(open_pr["body"], "key") == key:
            summary.append(f"{kind}: unchanged, passed and proposed in {open_pr['url']}")
            continue
        if not args.force and issue and marker_value(issue["body"], "key") == key and marker_value(issue["body"], "base") == base_tree:
            summary.append(f"{kind}: unchanged since the last run, reported in {issue['url']}")
            continue
        log(f"{kind}: {c.what}")
        msg = (f"{c.title}\n\n{c.what}\n" + "\n".join(c.notes))
        sha = gh.push(kind, c.files(p["base"], pin=not gh.dry and kind == "widen"), msg)
        url = gh.dispatch(kind, sha, c.dispatch)
        if gh.dry:
            summary.append(f"{kind}: would test {c.what} ({len(c.cells())} cells) and then open a PR or update an issue")
            continue
        pending.append((c, url, key))

    results = gh.wait([u for _, u, _ in pending]) if pending else {}
    for c, url, key in pending:
        res = results[url]
        prs = gh.find_marked("pr", c.kind) if c.kind == "widen" else []
        open_pr = next((x for x in prs if x["state"] == "OPEN"), None)
        issues = gh.find_marked("issue", c.kind)
        if c.kind == "prerelease":
            issues = [i for i in issues if i["title"].endswith(gh.title(c.title))]
        issue = issues[0] if issues else None
        verdict = "passed" if res["ok"] else f"failed ({res['conclusion']}: {', '.join(res['failed']) or 'see the run'})"
        if res.get("retried"):
            verdict += f" (after rerunning, once, what failed first: {', '.join(res['retried'])})"
        if c.kind == "widen" and res["ok"]:
            text = body(c, f"The release watcher found {c.what}. Every cell of the compatibility matrix passed on this "
                           "branch, the face-model test included, so this widens the tested versions.", url, gh, key, base_tree,
                        ("Some cells failed first and passed when rerun: " + ", ".join(res["retried"]) + ".\n\n" if res.get("retried") else "")
                        + "After merging, deploy the app (`./deploy`): until then an installation that upgrades to these "
                        "versions switches the app off.")
            if open_pr:
                gh.gh("pr", "edit", str(open_pr["number"]), "--title", gh.title(c.title), "--body-file", "-", write=True, input=text)
                pr_url = open_pr["url"]
            else:
                pr_url = create_pr(gh, "widen", c.title, text)
            if issue and issue["state"] == "OPEN":
                gh.gh("issue", "close", str(issue["number"]), "--comment", f"Passes now: {pr_url} ({url}).", write=True)
            summary.append(f"widen: {c.what} passed ({url}); PR {pr_url}")
            continue
        if c.kind == "widen":
            title = f"Release watch: {c.what} fails the compatibility tests"
            intro = (f"The release watcher found {c.what} and ran the compatibility matrix on branch "
                     f"`{gh.branch('widen')}`; it {verdict}, so the tested versions are not widened.")
        else:
            title = c.title
            intro = (f"The release watcher ran {c.what} (branch `{gh.branch('prerelease')}`, allowed to fail): it {verdict}.")
        text = body(c, intro, url, gh, key, base_tree)
        if issue:
            gh.gh("issue", "edit", str(issue["number"]), "--title", gh.title(title), "--body-file", "-", write=True, input=text)
            gh.gh("issue", "comment", str(issue["number"]), "--body", f"{c.what}: {verdict} — {url}", write=True)
            if res["ok"] and issue["state"] == "OPEN":
                gh.gh("issue", "close", str(issue["number"]), write=True)
            elif not res["ok"] and issue["state"] != "OPEN":
                gh.gh("issue", "reopen", str(issue["number"]), write=True)
            summary.append(f"{c.kind}: {verdict}; updated {issue['url']}")
        else:
            out = gh.gh("issue", "create", "--title", gh.title(title), "--label", gh.label, "--body-file", "-",
                        write=True, input=text).strip()
            if res["ok"] and out:
                gh.gh("issue", "close", out.rsplit("/", 1)[1], write=True)
            summary.append(f"{c.kind}: {verdict}; reported in {out}")

    print("\n".join(summary))
    if gh.dry:
        print("\n".join(["", "would do:"] + [f"  {a}" for a in gh.actions] if gh.actions else ["", "would do: nothing"]))
    if (step_summary := os.environ.get("GITHUB_STEP_SUMMARY")):
        with open(step_summary, "a") as f:
            f.write("### Release watch" + (f" (simulation {sim})" if sim else "") + "\n\n"
                    + "\n".join(f"- {s}" for s in summary + [f"note: {n}" for n in p["notes"]]) + "\n")
    return 0


def create_pr(gh: GitHub, kind: str, title: str, text: str) -> str:
    args = ["pr", "create", "--head", gh.branch(kind), "--base", os.environ.get("WATCH_BASE", "main"),
            "--title", gh.title(title), "--label", gh.label, "--body-file", "-"]
    if gh.sim:
        args.append("--draft")
    try:
        return gh.gh(*args, write=True, input=text).strip()
    except RuntimeError as e:
        if "not permitted to create or approve pull requests" in str(e):
            raise SystemExit(
                f"Branch {gh.branch(kind)} is pushed, but GitHub refused the pull request: allow it under the "
                "repository's Settings → Actions → General → Workflow permissions → \"Allow GitHub Actions to "
                "create and approve pull requests\", then rerun the watcher.") from e
        raise


def show_plan() -> int:
    p = plan()
    cur = p["current"]
    print(f"tested: Nextcloud {', '.join(cur['nextcloud'])}; " +
          "; ".join(f"{APPS[a]} {and_list(m)}" for a, m in cur["tested"].items()))
    print(f"released Nextcloud majors (Docker image): {', '.join(f'{k} ({v})' for k, v in sorted(p['released'].items()))}")
    for n in p["notes"]:
        print(f"note: {n}")
    for kind in KINDS:
        c = p[kind]
        if c is None:
            print(f"{kind}: nothing new")
            continue
        print(f"{kind}: {c.what}  [key {c.key()}]")
        for n in c.notes:
            print(f"    {n}")
        for cell in c.cells():
            print(f"    cell: {cell}")
        for path in sorted(c.files(p["base"], pin=False)):
            print(f"    changes {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--fixture")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--only", help="comma-separated: " + ",".join(KINDS))
    r.add_argument("--force", action="store_true", help="test and propose even if unchanged since the last run")
    r.add_argument("--fixture")
    args = ap.parse_args()
    if args.cmd == "plan":
        if args.fixture:
            os.environ["FACETAG_WATCH_FIXTURE"] = str(Path(args.fixture).resolve())
        return show_plan()
    return watch(args)


if __name__ == "__main__":
    sys.exit(main())
