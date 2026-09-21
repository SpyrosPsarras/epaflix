#!/usr/bin/env python3
"""Rewrite /home/t3 path references inside a relocated env home.

Only touches things that are pure path plumbing: Git worktree `.git` pointer
files, `.git/worktrees/*/gitdir` backlinks, absolute symlinks, Codex's
`threads.rollout_path`/`cwd`, OpenCode's `session.directory`/`path`,
`project.worktree` and `project_directory.directory`, and the cwd-derived
`.claude/projects/<dir>` names. Rollout JSONL, OpenCode message/part JSON and
every other file stay identical. Run inside the pod, with no provider running
against that home:

    python3 repair_home.py /home/t3env-0 [--dry-run]
"""
import argparse
import os
from pathlib import Path
import sqlite3
import sys

OLD = "/home/t3"


def rewrite_codex_db(home, new_home, dry):
    db = home / ".codex/state_5.sqlite"
    if not db.exists():
        return 0
    c = sqlite3.connect(db)
    like, n = OLD + "/%", 0
    for col in ("rollout_path", "cwd"):
        rows = c.execute(f"SELECT count(*) FROM threads WHERE {col} LIKE ?", (like,)).fetchone()[0]
        if rows:
            print("codex threads." + col, rows)
            n += rows
            if not dry:
                c.execute(f"UPDATE threads SET {col} = ? || substr({col}, ?) WHERE {col} LIKE ?",
                          (new_home, len(OLD) + 1, like))
    if not dry:
        c.commit()
        missing = [p for p, in c.execute("SELECT rollout_path FROM threads") if not os.path.exists(p)]
        if missing:
            sys.exit(f"codex rollout files missing after rewrite: {len(missing)}")
    c.close()
    return n


def rewrite_text(path, new_home, dry):
    data = path.read_bytes()
    fixed = data.replace(OLD.encode() + b"/", new_home.encode() + b"/")
    if fixed != data:
        print("text", path)
        if not dry:
            path.write_bytes(fixed)
        return 1
    return 0


def rewrite_link(path, new_home, dry):
    target = os.readlink(path)
    if target == OLD or target.startswith(OLD + "/"):
        new = new_home + target[len(OLD):]
        print("link", path, "->", new)
        if not dry:
            path.unlink()
            path.symlink_to(new)
        return 1
    return 0


def rewrite_opencode_db(home, new_home, dry):
    """OpenCode resumes a session in `session.directory`; the server reported
    NotFound on the old /home/t3 path. Only path columns change. Message and
    part `data` JSON is conversation content and stays byte-identical."""
    db = home / ".local/share/opencode/opencode.db"
    if not db.exists():
        return 0
    c = sqlite3.connect(db)
    n = 0
    # (project_id, directory) is the primary key. If the provider already ran
    # once against the relocated path, the new row exists; drop the old one.
    dup = c.execute("SELECT count(*) FROM project_directory o WHERE o.directory LIKE ? AND EXISTS "
                    "(SELECT 1 FROM project_directory n WHERE n.project_id=o.project_id AND n.directory = ? || substr(o.directory, ?))",
                    (OLD + "/%", new_home, len(OLD) + 1)).fetchone()[0]
    if dup:
        print("opencode project_directory already relocated (dropping old row)", dup)
        n += dup
        if not dry:
            c.execute("DELETE FROM project_directory WHERE directory LIKE ? AND EXISTS "
                      "(SELECT 1 FROM project_directory n WHERE n.project_id=project_directory.project_id "
                      "AND n.directory = ? || substr(project_directory.directory, ?))",
                      (OLD + "/%", new_home, len(OLD) + 1))
    for table, col, prefix in (("session", "directory", OLD + "/"), ("session", "path", OLD[1:] + "/"),
                               ("project", "worktree", OLD + "/"), ("project_directory", "directory", OLD + "/")):
        new_prefix = new_home + "/" if prefix.startswith("/") else new_home[1:] + "/"
        rows = c.execute(f"SELECT count(*) FROM {table} WHERE {col} LIKE ? OR {col} = ?",
                         (prefix + "%", prefix[:-1])).fetchone()[0]
        if rows:
            print(f"opencode {table}.{col}", rows)
            n += rows
            if not dry:
                c.execute(f"UPDATE {table} SET {col} = ? || substr({col}, ?) WHERE {col} LIKE ? OR {col} = ?",
                          (new_prefix[:-1], len(prefix), prefix + "%", prefix[:-1]))
    if not dry:
        c.commit()
    c.close()
    return n


def rename_claude_projects(home, new_home, dry):
    """Claude stores transcripts under ~/.claude/projects/<cwd with / -> ->.
    T3 resumes with the relocated cwd, so the directory must carry that name."""
    root = home / ".claude/projects"
    if not root.is_dir():
        return 0
    old_key, new_key, n = OLD.replace("/", "-"), new_home.replace("/", "-"), 0
    for d in root.iterdir():
        if d.name == old_key or d.name.startswith(old_key + "-"):
            target = root / (new_key + d.name[len(old_key):])
            print("claude project dir", d.name, "->", target.name)
            if not dry:
                if target.exists():
                    sys.exit(f"claude project dir already exists: {target}")
                d.rename(target)
            n += 1
    return n


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("home", type=Path)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    home = a.home.resolve()
    new_home = str(home)
    if home.name == "t3" or not home.is_dir():
        sys.exit("refusing: target must be a relocated home, not /home/t3")
    n = (rewrite_codex_db(home, new_home, a.dry_run) + rewrite_opencode_db(home, new_home, a.dry_run)
         + rename_claude_projects(home, new_home, a.dry_run))
    for root, dirs, files in os.walk(home):
        rp = Path(root)
        for name in list(dirs) + files:
            fp = rp / name
            if fp.is_symlink():
                n += rewrite_link(fp, new_home, a.dry_run)
                if name in dirs:
                    dirs.remove(name)
                continue
        # worktree checkout: `.git` is a file with `gitdir: <abs path>`
        if ".git" in files and (rp / ".git").is_file():
            n += rewrite_text(rp / ".git", new_home, a.dry_run)
        # main repo: `.git/worktrees/<id>/gitdir` backlink to the checkout
        if rp.parent.name == "worktrees" and rp.parent.parent.name == ".git" and "gitdir" in files:
            n += rewrite_text(rp / "gitdir", new_home, a.dry_run)
        # skip the big trees; nothing path-encoded lives there
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".cache", ".npm", "objects")]
    print("changes", n, "(dry run)" if a.dry_run else "")


if __name__ == "__main__":
    main()
