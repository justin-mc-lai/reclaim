#!/usr/bin/env python3
"""reclaim — macOS disk triage and safe cleanup, built for humans and agents.

  reclaim scan                 what is eating the disk
  reclaim plan --json          machine-readable cleanup plan
  reclaim clean --safe --yes   run only regenerable cleanups
  reclaim clean --ids a1,b2 --yes
  reclaim selftest
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
GB = 1024 ** 3
MB = 1024 ** 2

RISK_ORDER = {"safe": 0, "review": 1, "dangerous": 2}

PROTECTED = {
    "/", "/System", "/Library", "/Applications", "/Users", "/private", "/bin", "/usr",
    HOME, os.path.join(HOME, "Library"), os.path.join(HOME, "Documents"),
    os.path.join(HOME, "Desktop"), os.path.join(HOME, "Developer"),
}

VIDEO = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".ts"}
IMAGE = {".dmg", ".iso", ".img", ".qcow2", ".raw"}
ARCHIVE = {".zip", ".7z", ".rar", ".tar", ".gz", ".xz", ".bz2"}
MODEL = {".safetensors", ".gguf", ".ckpt", ".pt", ".onnx", ".bin"}


def run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired):
        return 1, "", ""


def du(path):
    if not os.path.exists(path):
        return 0
    rc, out, _ = run(["du", "-sk", path], timeout=600)
    if rc != 0 or not out.strip():
        return 0
    try:
        return int(out.split()[0]) * 1024
    except ValueError:
        return 0


def item(kind, path, size, risk, why, cmd=None, action=None):
    ident = hashlib.sha1(("%s:%s" % (kind, path)).encode()).hexdigest()[:10]
    return {"id": ident, "kind": kind, "path": path, "bytes": int(size),
            "gb": round(size / GB, 2), "risk": risk, "why": why,
            "cmd": cmd, "action": action or ("rm" if path else "cmd")}


def scan_roots(roots):
    rows = []
    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        rc, out, _ = run(["du", "-d1", "-x", "-k", root], timeout=900)
        for line in out.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                size = int(parts[0]) * 1024
            except ValueError:
                continue
            path = parts[1]
            if os.path.abspath(path) == os.path.abspath(root):
                continue
            rows.append({"path": path, "bytes": size, "gb": round(size / GB, 2)})
    return sorted(rows, key=lambda r: -r["bytes"])


def collect_docker():
    if not shutil.which("docker"):
        return []
    rc, out, _ = run(["docker", "system", "df", "--format", "{{json .}}"], timeout=60)
    if rc != 0:
        return []
    found = {}
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        found[d.get("Type", "")] = d
    items = []
    if "Images" in found:
        items.append(item("docker-images", "", 0, "safe",
                          "未被任何容器引用的镜像，需要时可重新 pull",
                          cmd=["docker", "image", "prune", "-a", "-f"]))
        items[-1]["note"] = "%s reclaimable of %s" % (
            found["Images"].get("Reclaimable", "?"), found["Images"].get("Size", "?"))
    if "Build Cache" in found:
        items.append(item("docker-buildcache", "", 0, "safe",
                          "构建缓存，下次构建变慢，不会丢数据",
                          cmd=["docker", "builder", "prune", "-f"]))
        items[-1]["note"] = found["Build Cache"].get("Reclaimable", "?")
    if "Containers" in found:
        items.append(item("docker-containers", "", 0, "safe",
                          "已停止的容器", cmd=["docker", "container", "prune", "-f"]))
        items[-1]["note"] = found["Containers"].get("Reclaimable", "?")
    return items


def collect_caches(min_size=300 * MB):
    base = os.path.join(HOME, "Library", "Caches")
    items = []
    if not os.path.isdir(base):
        return items
    for name in os.listdir(base):
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        size = du(path)
        if size >= min_size:
            items.append(item("cache", path, size, "safe",
                              "应用缓存，会被自动重建"))
    return items


def collect_tool_cleanups():
    items = []
    checks = [
        ("brew", ["brew", "cleanup", "--prune=all", "-s"], "旧版本 formula 与下载缓存"),
        ("pip", ["pip3", "cache", "purge"], "pip 下载缓存"),
        ("npm", ["npm", "cache", "clean", "--force"], "npm 缓存"),
        ("pnpm", ["pnpm", "store", "prune"], "pnpm 全局 store 里的孤儿包"),
        ("go", ["go", "clean", "-cache"], "Go 构建缓存"),
    ]
    for name, cmd, why in checks:
        if shutil.which(cmd[0]):
            items.append(item("tool-" + name, "", 0, "safe", why, cmd=cmd))
    return items


def collect_simulators():
    items = []
    if shutil.which("xcrun"):
        items.append(item("xcode-simulators", "", 0, "safe",
                          "不可用的模拟器设备", cmd=["xcrun", "simctl", "delete", "unavailable"]))
    dd = os.path.join(HOME, "Library", "Developer", "Xcode", "DerivedData")
    size = du(dd)
    if size:
        items.append(item("xcode-deriveddata", dd, size, "safe",
                          "Xcode 编译产物，重新构建即可"))
    return items


def collect_trash():
    path = os.path.join(HOME, ".Trash")
    size = du(path)
    if size:
        return [item("trash", path, size, "review", "废纸篓内容，删了不可恢复")]
    return []


def collect_big_files(min_size=GB):
    if not shutil.which("mdfind"):
        return []
    rc, out, _ = run(["mdfind", "-onlyin", HOME,
                      "kMDItemFSSize > %d" % min_size], timeout=120)
    items, seen = [], set()
    for path in out.splitlines():
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        ext = os.path.splitext(path)[1].lower()
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if ext in VIDEO:
            why = "视频素材"
        elif ext in IMAGE:
            why = "磁盘镜像"
        elif ext in ARCHIVE:
            why = "压缩包"
        elif ext in MODEL:
            why = "模型权重"
        else:
            why = "大文件"
        items.append(item("big-" + (ext.lstrip(".") or "file"), path, size, "review", why))
    return items


def du_tree(root, depth):
    index = {}
    if not os.path.isdir(root):
        return index
    rc, out, _ = run(["du", "-d%d" % depth, "-x", "-k", root], timeout=3600)
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            index[parts[1]] = int(parts[0]) * 1024
        except ValueError:
            continue
    return index


def collect_dev_builds(root=None, min_size=500 * MB, index=None):
    root = root or os.path.join(HOME, "Developer")
    names = {"node_modules", ".venv", "target", "dist", ".next", "build"}
    if index is None:
        index = du_tree(root, 8)
    items = []
    for path, size in index.items():
        base = os.path.basename(path.rstrip(os.sep))
        if base in names and size >= min_size:
            items.append(item("dev-build", path, size, "review",
                              "可重建的构建产物（%s）" % base))
    return items


def collect_worktrees(root=None):
    root = root or os.path.join(HOME, "Developer")
    items = []
    if not os.path.isdir(root) or not shutil.which("git"):
        return items
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames or ".git" in filenames:
            rc, out, _ = run(["git", "-C", dirpath, "worktree", "list", "--porcelain"], timeout=30)
            if rc == 0:
                for line in out.splitlines():
                    if not line.startswith("worktree "):
                        continue
                    wt = line[len("worktree "):]
                    if os.path.abspath(wt) == os.path.abspath(dirpath):
                        continue
                    dirty = 0
                    rc2, out2, _ = run(["git", "-C", wt, "status", "--porcelain",
                                        "--untracked-files=no"], timeout=120)
                    if rc2 == 0:
                        dirty = len([l for l in out2.splitlines() if l.strip()])
                    size = du(wt)
                    if not size:
                        continue
                    if dirty:
                        items.append(item("git-worktree", wt, size, "dangerous",
                                          "worktree 有 %d 个未提交改动" % dirty))
                    else:
                        items.append(item("git-worktree", wt, size, "review",
                                          "干净 worktree，分支提交仍在主仓库"))
            dirnames[:] = [d for d in dirnames if d != ".git"]
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
    return items


def collect_all(root=None, deep=False):
    items = []
    steps = [collect_docker, collect_tool_cleanups, collect_simulators,
             collect_caches, collect_trash, collect_big_files]
    if deep:
        steps += [lambda: collect_dev_builds(root), lambda: collect_worktrees(root)]
    for step in steps:
        try:
            items += step()
        except Exception as e:
            print("skip %s: %s" % (getattr(step, "__name__", step), e), file=sys.stderr)
    return items


def guard(path):
    real = os.path.realpath(path)
    if real in PROTECTED:
        return False
    if real == "/" or not real.startswith(HOME + os.sep):
        return False
    cwd = os.path.realpath(os.getcwd())
    if cwd == real or cwd.startswith(real + os.sep):
        return False
    return True


def remove_path(path):
    if not guard(path):
        return False, "protected path"
    if not os.path.lexists(path):
        return True, "missing"
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        else:
            shutil.rmtree(path, onerror=lambda f, p, e: None)
        return True, "removed"
    except OSError as e:
        return False, str(e)


def free_bytes():
    st = os.statvfs(HOME)
    return st.f_bavail * st.f_frsize


def cmd_scan(args):
    before = free_bytes()
    roots = args.root or [HOME]
    rows = scan_roots(roots)
    if args.json:
        print(json.dumps({"free_gb": round(before / GB, 1), "rows": rows[:args.limit]},
                         ensure_ascii=False, indent=2))
        return 0
    print("free: %.1f GB" % (before / GB))
    for r in rows[:args.limit]:
        print("%8.1f GB  %s" % (r["gb"], r["path"]))
    return 0


def cmd_plan(args):
    items = collect_all(args.root[0] if args.root else None, deep=args.deep)
    items.sort(key=lambda i: (RISK_ORDER[i["risk"]], -i["bytes"]))
    payload = {"free_gb": round(free_bytes() / GB, 1), "items": items}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("free: %.1f GB\n" % payload["free_gb"])
    for risk in ("safe", "review", "dangerous"):
        group = [i for i in items if i["risk"] == risk]
        if not group:
            continue
        total = sum(i["bytes"] for i in group)
        print("[%s] %.1f GB across %d items" % (risk.upper(), total / GB, len(group)))
        for i in group:
            print("  %-10s %8.2f GB  %s" % (i["id"], i["gb"], i["path"] or " ".join(i["cmd"] or [])))
            print("             %s%s" % (i["why"], (" | " + i["note"]) if i.get("note") else ""))
        print("")
    return 0


def cmd_clean(args):
    items = {i["id"]: i for i in collect_all(args.root[0] if args.root else None, deep=args.deep)}
    chosen = []
    if args.ids:
        for ident in args.ids.split(","):
            ident = ident.strip()
            if ident not in items:
                print("unknown id: %s" % ident, file=sys.stderr)
                return 2
            chosen.append(items[ident])
    elif args.safe:
        chosen = [i for i in items.values() if i["risk"] == "safe"]
    else:
        print("nothing selected: pass --safe or --ids", file=sys.stderr)
        return 2

    risky = [i for i in chosen if i["risk"] == "dangerous"]
    if risky and not args.force:
        print("refusing %d dangerous item(s) without --force" % len(risky), file=sys.stderr)
        return 2

    before = free_bytes()
    results = []
    for i in chosen:
        if args.dry_run:
            results.append({"id": i["id"], "ok": True, "result": "dry-run"})
            continue
        if i.get("cmd"):
            rc, out, err = run(i["cmd"], timeout=1800)
            results.append({"id": i["id"], "ok": rc == 0, "result": (out or err).strip()[:200]})
        else:
            ok, msg = remove_path(i["path"])
            results.append({"id": i["id"], "ok": ok, "result": msg})
    after = free_bytes()
    payload = {"free_before_gb": round(before / GB, 1), "free_after_gb": round(after / GB, 1),
               "reclaimed_gb": round((after - before) / GB, 1), "results": results}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print("%-4s %-10s %s" % ("ok" if r["ok"] else "FAIL", r["id"], r["result"]))
        print("reclaimed: %.1f GB (free now %.1f GB)" % (payload["reclaimed_gb"], after / GB))
    return 0 if all(r["ok"] for r in results) else 1


def selftest():
    tmp = os.path.join(HOME, ".reclaim-selftest")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "cache", "big"))
    os.makedirs(os.path.join(tmp, "repo", "node_modules", "pkg"))
    with open(os.path.join(tmp, "cache", "big", "blob"), "wb") as f:
        f.write(b"0" * (2 * MB))
    with open(os.path.join(tmp, "repo", "node_modules", "pkg", "index.js"), "w") as f:
        f.write("x")

    assert du(os.path.join(tmp, "cache", "big")) >= 2 * MB
    assert guard(os.path.join(tmp, "cache", "big")) is True
    assert guard("/") is False
    assert guard(os.path.join(HOME, "Documents")) is False
    assert guard(HOME) is False

    builds = collect_dev_builds(tmp, min_size=1)
    assert any(i["kind"] == "dev-build" and i["risk"] == "review" for i in builds), builds
    assert all(i["bytes"] > 0 for i in builds)

    ok, _ = remove_path(os.path.join(tmp, "cache", "big"))
    assert ok and not os.path.exists(os.path.join(tmp, "cache", "big"))
    ok, msg = remove_path("/")
    assert not ok and msg == "protected path"

    i = item("x", "/tmp/y", 1073741824, "safe", "why")
    assert len(i["id"]) == 10 and i["gb"] == 1.0

    shutil.rmtree(tmp, ignore_errors=True)
    print("selftest: PASS")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="reclaim", description="macOS disk triage and safe cleanup")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("scan", help="largest directories")
    s.add_argument("--json", action="store_true")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--root", action="append")
    s.set_defaults(func=cmd_scan)

    pl = sub.add_parser("plan", help="cleanup plan with risk labels")
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--root", action="append")
    pl.add_argument("--deep", action="store_true", help="also scan dev builds and git worktrees (slow)")
    pl.set_defaults(func=cmd_plan)

    c = sub.add_parser("clean", help="execute cleanups")
    c.add_argument("--safe", action="store_true", help="only risk=safe items")
    c.add_argument("--ids", help="comma separated item ids")
    c.add_argument("--yes", action="store_true", help="actually run (default is dry-run)")
    c.add_argument("--force", action="store_true", help="allow dangerous items")
    c.add_argument("--json", action="store_true")
    c.add_argument("--root", action="append")
    c.add_argument("--deep", action="store_true")
    c.set_defaults(func=cmd_clean)

    t = sub.add_parser("selftest", help="run built-in checks")
    t.set_defaults(func=lambda a: selftest())

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    if args.cmd == "clean" and not getattr(args, "yes", False):
        args.dry_run = True
        print("dry-run (pass --yes to execute)", file=sys.stderr)
    else:
        args.dry_run = False
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
