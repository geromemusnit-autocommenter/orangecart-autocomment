#!/usr/bin/env python3
"""
autocomment.py — auto "Order here 👉 <link>" first comment on your Facebook Page posts.

  scan         read scheduled + live posts, match each to a product, verify links
  run          scan, then comment on posts that just went live (auto mode only)
  mode preview|auto
  install      run every 5 min in the background (launchd); `uninstall` to stop
  status       print what's matched / pending / commented
  export       write catalog.json (the product list the cloud runner uses)
  tick         what launchd runs: local mode → `run`; cloud mode → just pull results

Cloud mode (GitHub Actions does the commenting every 5 min, even with this Mac off):
  autocomment_settings.json  mode / auto_since / holds   — written HERE, read by the cloud
  autocomment_state.json     posts / comments / log      — written by the CLOUD, read here
  catalog.json               products + links + captions — written here (export)
Only one side ever writes each file, so they never conflict. With cloud mode on, this Mac
never comments automatically (no double comments); "Comment now" still works.

Safety rules (all enforced in `run`):
  • the comment goes to that post's own Facebook ID — never "the latest post"
  • a post is matched to a product by a Shopee link in its caption, or by its caption text;
    no confident match (or two possible products) → skipped and flagged, never guessed
  • the link must resolve to YOUR affiliate ID and to THAT product's item ID
  • one comment per post (ledger + a check of the Page's own comments on the post)
  • only posts that go live after auto mode was switched on are auto-commented;
    older live posts need a click on "Comment now" in the dashboard
  • preview mode (default) never posts anything
"""
import argparse, datetime as dt, glob, json, os, re, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fbpage

STATE = os.environ.get("AUTOCOMMENT_STATE") or os.path.join(HERE, "autocomment_state.json")
SETTINGS = os.environ.get("AUTOCOMMENT_SETTINGS") or os.path.join(HERE, "autocomment_settings.json")
CATALOG = os.path.join(HERE, "catalog.json")
JOBS = os.path.join(HERE, "jobs")
IN_CLOUD = os.environ.get("AUTOCOMMENT_RUNNER") == "cloud"
HEARTBEAT = 3600          # rewrite an unchanged state file at most hourly (keeps git history quiet)
AFFILIATE_ID = "an_13338420573"
LINK_RE = re.compile(r"https?://s\.shopee\.ph/[A-Za-z0-9]+")
LINK_TTL = 6 * 3600
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.germsmusnt.shopee-autocomment.plist")


def now():
    return int(time.time())


def iso(ts):
    if isinstance(ts, str):
        try:
            return int(dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=dt.timezone.utc).timestamp())
        except ValueError:
            return None
    return ts


# ---------------------------------------------------------------- settings / state
def load_settings():
    if os.path.exists(SETTINGS):
        return json.load(open(SETTINGS))
    old = json.load(open(STATE)) if os.path.exists(STATE) else {}
    return {"mode": old.get("mode", "preview"), "auto_since": old.get("auto_since"),
            "holds": {k: True for k, e in (old.get("posts") or {}).items() if e.get("hold")},
            "cloud": None}


def save_settings(cfg):
    tmp = SETTINGS + ".tmp"
    json.dump(cfg, open(tmp, "w"), indent=2, ensure_ascii=False)
    os.replace(tmp, SETTINGS)


def cloud_on(cfg=None):
    return bool((cfg or load_settings()).get("cloud"))


def load_state():
    st = json.load(open(STATE)) if os.path.exists(STATE) else {"posts": {}, "linkcache": {}, "log": []}
    cfg = load_settings()
    st["mode"], st["auto_since"] = cfg.get("mode", "preview"), cfg.get("auto_since")
    holds = cfg.get("holds") or {}
    for pid, e in st["posts"].items():
        e["hold"] = bool(holds.get(pid))
    return st


def _stable(st):
    d = {k: v for k, v in st.items() if k not in ("last_scan", "mode", "auto_since")}
    d["posts"] = {k: {kk: vv for kk, vv in e.items() if kk != "hold"} for k, e in (st.get("posts") or {}).items()}
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def save_state(st, force=False):
    """Writes posts/comments/log. Settings (mode, auto_since, holds) never go in here.
    Skips the write when nothing but the scan time changed (hourly heartbeat instead)."""
    out = {k: v for k, v in st.items() if k not in ("mode", "auto_since")}
    out["posts"] = {k: {kk: vv for kk, vv in e.items() if kk != "hold"} for k, e in st["posts"].items()}
    if not force and os.path.exists(STATE):
        try:
            old = json.load(open(STATE))
            if _stable(old) == _stable(out) and now() - (old.get("last_scan") or 0) < HEARTBEAT:
                return False
        except ValueError:
            pass
    tmp = STATE + ".tmp"
    json.dump(out, open(tmp, "w"), indent=2, ensure_ascii=False)
    os.replace(tmp, STATE)
    return True


def git(*args, check=False):
    return subprocess.run(["git", "-C", HERE, *args], capture_output=True, text=True, check=check)


def git_pull():
    return git("pull", "--rebase", "--autostash", "-q")


def git_push(files, msg):
    git("add", *files)
    if git("diff", "--cached", "--quiet").returncode == 0:
        return True
    git("commit", "-q", "-m", msg)
    git_pull()
    r = git("push", "-q")
    return r.returncode == 0


def log(st, msg):
    st["log"] = (st.get("log") or [])[-199:] + [{"t": now(), "msg": msg}]


# ---------------------------------------------------------------- products
def catalog():
    """Products from jobs/ (this Mac) or catalog.json (cloud runner has no jobs folder)."""
    if not glob.glob(os.path.join(JOBS, "*", "product.json")):
        return json.load(open(CATALOG)) if os.path.exists(CATALOG) else []
    out = []
    for pj in sorted(glob.glob(os.path.join(JOBS, "*", "product.json"))):
        job = os.path.dirname(pj)
        p = json.load(open(pj))
        if not p.get("affiliate_link"):
            continue
        read = lambda f: open(os.path.join(job, f)).read().strip() if os.path.exists(os.path.join(job, f)) else ""
        out.append({"job": os.path.basename(job), "item_id": str(p.get("item_id")), "name": p.get("name"),
                    "link": p["affiliate_link"], "comment": f"Order here 👉 {p['affiliate_link']}",
                    "caption": read("caption_facebook.txt") or read("caption.txt"),
                    "caption_shopee": read("caption_shopee.txt"),
                    "hashtags": p.get("hashtags") or []})
    return out


def export_catalog(push=None):
    cat = catalog()
    json.dump(cat, open(CATALOG, "w"), indent=2, ensure_ascii=False)
    if push if push is not None else cloud_on():
        git_push([CATALOG], f"catalog: {len(cat)} products")
    return cat


def thumb(job):
    """A frame from the finished edit, for the dashboard (cached as thumb.jpg)."""
    d = os.path.join(JOBS, job)
    out = os.path.join(d, "thumb.jpg")
    if not os.path.exists(out):
        shots = sorted(glob.glob(os.path.join(d, "shots", "*.mp4")))
        if shots:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", shots[0], "-frames:v", "1",
                            "-vf", "scale=360:-2", out])
    return out if os.path.exists(out) else None


# ---------------------------------------------------------------- link check
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def check_link(url, st, expect_item=None):
    c = (st.setdefault("linkcache", {})).get(url)
    if not c or now() - c["checked"] > LINK_TTL:
        loc, err = "", None
        try:
            urllib.request.build_opener(_NoRedirect).open(
                urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20)
            err = "no redirect"
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") or ""
        except Exception as e:
            err = str(e)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
        m = re.search(r"/(\d+)/(\d+)(?:$|\?|/)", urllib.parse.urlparse(loc).path + "/")
        c = {"checked": now(), "affiliate": (q.get("utm_source") or [None])[0],
             "item_id": m and m.group(2), "shop_id": m and m.group(1), "error": err}
        st["linkcache"][url] = c
    return dict(c, affiliate_ok=c["affiliate"] == AFFILIATE_ID,
                item_ok=(expect_item is None or c["item_id"] == str(expect_item)))


# ---------------------------------------------------------------- matching
def norm(s):
    s = re.sub(r"https?://\S+", " ", (s or "").lower())
    return " ".join(re.sub(r"[^a-z0-9ñ#₱ ]+", " ", s).split())


def first_line(s):
    return next((norm(l) for l in (s or "").splitlines() if norm(l)), "")


def match(message, cat, st):
    """→ (product or None, how, detail). Never guesses between two products."""
    for url in LINK_RE.findall(message or ""):
        info = check_link(url, st)
        prod = next((p for p in cat if p["item_id"] == info.get("item_id")), None)
        if prod:
            return prod, "Shopee link in caption", url
        return None, "unknown link", f"{url} → item {info.get('item_id')} (not one of your products)"
    m, f = norm(message), first_line(message)
    if not m:
        return None, "no caption", ""
    toks = set(m.split())
    scored = []
    for p in cat:
        best = 0.0
        for cand in (p["caption"], p["caption_shopee"]):
            if not cand:
                continue
            n, cf = norm(cand), first_line(cand)
            if n == m:
                best = 1.0
            elif f and len(f) > 10 and (f == cf or f in n or cf in m):
                best = max(best, 0.95)
            else:
                ct = set(n.split())
                best = max(best, len(toks & ct) / max(1, len(toks | ct)))
        scored.append((best, p))
    scored.sort(key=lambda x: -x[0])
    top, second = scored[0], (scored[1] if len(scored) > 1 else (0, None))
    if top[0] >= 0.6 and top[0] - second[0] >= 0.15:
        return top[1], "caption", f"{top[0]:.0%} match"
    if top[0] >= 0.6:
        return None, "ambiguous", f"{top[1]['name']} vs {second[1]['name']}"
    return None, "no match", f"best {top[0]:.0%}"


# ---------------------------------------------------------------- scan / run
def scan(st, page=None):
    cat = catalog()
    creds = fbpage.load_creds()
    st["connected"] = bool(creds)
    st["last_scan"] = now()
    st["errors"] = []
    if not creds:
        return st, cat
    page = page or fbpage.Page(creds)
    try:
        info = page.name()
        st["page"] = {"id": page.id, "name": info.get("name"),
                      "picture": ((info.get("picture") or {}).get("data") or {}).get("url")}
    except fbpage.FBError as e:
        st["errors"].append(str(e))
        return st, cat
    posts, errs = page.posts()
    st["errors"] += errs
    seen = set()
    for p in posts:
        seen.add(p["id"])
        e = st["posts"].setdefault(p["id"], {"first_seen": now()})
        went_live = e.get("status") == "scheduled" and p["status"] == "live"
        if went_live:
            e["live_at"] = now()
        e.update({k: p[k] for k in ("kind", "status", "message", "when", "thumb", "permalink")})
        if p["status"] == "live" and not e.get("live_at"):
            e["live_at"] = iso(p["when"]) or now()
        prod, how, detail = match(p["message"], cat, st)
        e["match"] = {"how": how, "detail": detail,
                      "job": prod and prod["job"], "name": prod and prod["name"],
                      "item_id": prod and prod["item_id"], "comment": prod and prod["comment"],
                      "link": prod and prod["link"]}
        if prod:
            e["link_check"] = check_link(prod["link"], st, prod["item_id"])
        if p["status"] == "live" and not e.get("commented"):
            try:   # someone (you) may already have commented a link by hand
                mine = page.page_comments(p["id"])
                hit = next((c for c in mine if "s.shopee.ph" in (c.get("message") or "")), None)
                if hit:
                    e["commented"] = {"id": hit["id"], "message": hit["message"], "by": "manual/earlier",
                                      "at": iso(hit.get("created_time")), "landed": True,
                                      "permalink": hit.get("permalink_url")}
            except fbpage.FBError as ex:
                st["errors"].append(str(ex))
    for pid in [k for k, e in st["posts"].items() if k not in seen and not e.get("commented")]:
        del st["posts"][pid]              # unscheduled / deleted in Meta, or re-keyed
    return st, cat


def kind_of(e):
    """'product' | 'other' (not a product post — leave it alone) | 'attention' (looks like a
    product post but can't be safely commented)."""
    m, lc = e.get("match") or {}, e.get("link_check") or {}
    if m.get("job"):
        return "attention" if not (lc.get("affiliate_ok") and lc.get("item_ok")) else "product"
    return "other" if m.get("how") in ("no match", "no caption") else "attention"


def ready(e):
    m, lc = e.get("match") or {}, e.get("link_check") or {}
    if not m.get("job") and kind_of(e) == "other":
        return False, "not a product post — no comment"
    if not m.get("job"):
        return False, f"not matched ({m.get('how')}: {m.get('detail')})"
    if not lc.get("affiliate_ok"):
        return False, f"link is not your affiliate ID ({lc.get('affiliate')})"
    if not lc.get("item_ok"):
        return False, f"link goes to item {lc.get('item_id')}, expected {m.get('item_id')}"
    if e.get("hold"):
        return False, "on hold"
    return True, "ready"


def comment_on(st, page, pid, manual=False):
    e = st["posts"][pid]
    ok, why = ready(e)
    if not ok:
        return False, why
    if e.get("commented"):
        return False, "already commented"
    if e.get("status") != "live":
        return False, "not live yet"
    if not manual and st.get("mode") != "auto":
        return False, "preview mode"
    if not manual and (e.get("live_at") or 0) < (st.get("auto_since") or 10 ** 12):
        return False, "went live before auto mode — use Comment now"
    if any("s.shopee.ph" in (c.get("message") or "") for c in page.page_comments(pid)):
        return False, "Page already has a link comment"
    cid = page.comment(pid, e["match"]["comment"])
    landed = next((c for c in page.page_comments(pid) if c["id"] == cid), None)
    e["commented"] = {"id": cid, "message": e["match"]["comment"], "by": "manual click" if manual else "auto",
                      "at": now(), "landed": bool(landed), "permalink": landed and landed.get("permalink_url")}
    log(st, f"commented on {pid} ({e['match']['name']}) → {'landed ✓' if landed else 'NOT FOUND after posting'}")
    return True, "commented"


def run(st):
    st, _ = scan(st)
    if not st.get("connected") or st.get("errors") and not st.get("page"):
        return st
    page = fbpage.Page(fbpage.load_creds())
    for pid, e in st["posts"].items():
        if e.get("status") == "live" and not e.get("commented"):
            try:
                done, why = comment_on(st, page, pid)
                e["pending_reason"] = None if done else why
            except fbpage.FBError as ex:
                e["pending_reason"] = f"error: {ex}"
                log(st, f"comment failed on {pid}: {ex}")
    return st


# ---------------------------------------------------------------- launchd
def install():
    py = sys.executable
    plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.germsmusnt.shopee-autocomment</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{os.path.join(HERE, "autocomment.py")}</string><string>tick</string></array>
  <key>WorkingDirectory</key><string>{HERE}</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{os.path.join(HERE, "autocomment.log")}</string>
  <key>StandardErrorPath</key><string>{os.path.join(HERE, "autocomment.log")}</string>
</dict></plist>
'''
    open(PLIST, "w").write(plist)
    subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
    subprocess.run(["launchctl", "load", PLIST], check=True)
    print(f"installed: runs every 5 min → {PLIST}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["scan", "run", "mode", "install", "uninstall", "status", "export", "tick"])
    ap.add_argument("arg", nargs="?")
    a = ap.parse_args()
    cfg = load_settings()
    if a.cmd == "mode":
        if a.arg not in ("preview", "auto"):
            sys.exit("mode preview|auto")
        cfg["mode"] = a.arg
        if a.arg == "auto" and not cfg.get("auto_since"):
            cfg["auto_since"] = now()
        save_settings(cfg)
        if cloud_on(cfg):
            git_push([SETTINGS], f"mode → {a.arg}")
    elif a.cmd == "install":
        return install()
    elif a.cmd == "uninstall":
        subprocess.run(["launchctl", "unload", PLIST]); os.path.exists(PLIST) and os.remove(PLIST)
        return print("uninstalled")
    elif a.cmd == "export":
        return print(f"catalog.json: {len(export_catalog())} products")
    elif a.cmd == "tick" or (a.cmd == "run" and cloud_on(cfg) and not IN_CLOUD):
        if cloud_on(cfg) and not IN_CLOUD:
            git_pull()                    # the cloud comments; this Mac only reads results
        else:
            save_state(run(load_state()))
    elif a.cmd in ("scan", "run"):
        st = load_state()
        st = run(st) if a.cmd == "run" else scan(st)[0]
        save_state(st, force=not IN_CLOUD)
    st = load_state()
    print(f"[{dt.datetime.now():%H:%M}] mode={st['mode']} connected={st.get('connected')} "
          f"posts={len(st['posts'])} errors={len(st.get('errors') or [])}")
    for pid, e in st["posts"].items():
        m = e.get("match") or {}
        print(f"  {e.get('status'):9} {pid:28} {str(m.get('name'))[:34]:34} "
              f"{'✓ commented' if e.get('commented') else ready(e)[1]}")


if __name__ == "__main__":
    main()
