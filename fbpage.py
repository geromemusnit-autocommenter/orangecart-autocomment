"""
fbpage.py — the Facebook Page side of the auto-commenter (Graph API, stdlib only).

Credentials come from the crosspost agent's config (one source of truth):
    ~/AGENT/crosspost/config.json → facebook.page_id / page_access_token / api_version
The Page token needs: pages_show_list, pages_read_engagement, pages_manage_posts,
pages_manage_engagement (to comment as the Page).
"""
import json, os, urllib.error, urllib.parse, urllib.request

CROSSPOST_CONFIG = os.path.expanduser(
    os.environ.get("CROSSPOST_CONFIG", "~/AGENT/crosspost/config.json"))


class FBError(RuntimeError):
    pass


def load_creds():
    if os.environ.get("FB_PAGE_ID") and os.environ.get("FB_PAGE_TOKEN"):     # cloud runner (secrets)
        return {"page_id": os.environ["FB_PAGE_ID"], "page_access_token": os.environ["FB_PAGE_TOKEN"],
                "api_version": os.environ.get("FB_API_VERSION", "v26.0")}
    try:
        fb = json.load(open(CROSSPOST_CONFIG)).get("facebook", {})
    except FileNotFoundError:
        return None
    if not (fb.get("page_id") and fb.get("page_access_token")):
        return None
    return fb


class Page:
    def __init__(self, creds):
        self.id = creds["page_id"]
        self.token = creds["page_access_token"]
        self.graph = f"https://graph.facebook.com/{creds.get('api_version') or 'v26.0'}"

    def _req(self, path, params=None, method="GET"):
        params = dict(params or {}, access_token=self.token)
        url = f"{self.graph}/{path.lstrip('/')}"
        data = None
        if method == "GET":
            url += "?" + urllib.parse.urlencode(params)
        else:
            data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method),
                                        timeout=40) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read())["error"]["message"]
            except Exception:
                msg = str(e)
            raise FBError(f"{path}: {msg}")

    def _list(self, edge, fields, limit=50):
        out, after = [], None
        for _ in range(4):                                   # up to ~200 items
            p = {"fields": fields, "limit": limit}
            if after:
                p["after"] = after
            res = self._req(f"{self.id}/{edge}", p)
            out += res.get("data", [])
            after = (res.get("paging") or {}).get("cursors", {}).get("after")
            if not after or not (res.get("paging") or {}).get("next"):
                break
        return out

    def name(self):
        return self._req(self.id, {"fields": "name,picture{url}"})

    def posts(self):
        """Scheduled + recently published posts/reels, normalised and de-duplicated.
        Each source is tried separately — Meta exposes scheduled Reels differently
        from scheduled photo/link posts, and an edge we lack permission for is skipped."""
        items, errors = {}, []

        def add(raw, kind, status):
            pid = raw["id"]
            prev = items.get(pid)
            item = {
                "id": pid, "kind": kind, "status": status,
                "message": raw.get("message") or raw.get("description") or "",
                "when": raw.get("scheduled_publish_time") or raw.get("created_time") or raw.get("updated_time"),
                "thumb": raw.get("full_picture") or raw.get("picture"),
                "permalink": raw.get("permalink_url"),
            }
            if prev:                                   # same object from two edges: keep first, fill gaps
                for k, v in item.items():
                    prev[k] = prev.get(k) or v
            else:
                items[pid] = item

        video_ids = set()
        sources = [
            ("scheduled_posts", "id,message,scheduled_publish_time,created_time,full_picture,permalink_url,"
                                "attachments{media_type,target{id}}", "post", "scheduled"),
            ("video_reels", "id,description,created_time,updated_time,picture,permalink_url,status", "reel", None),
            ("videos", "id,description,created_time,published,scheduled_publish_time,picture,permalink_url", "video", None),
            ("published_posts", "id,message,created_time,full_picture,permalink_url,attachments{target{id}}", "post", "live"),
        ]
        for edge, fields, kind, status in sources:
            try:
                for raw in self._list(edge, fields):
                    if edge == "scheduled_posts":
                        # a scheduled Reel/video: key it by its VIDEO id — that's the id it has
                        # once live (video_reels), so scheduled → live is the same item
                        vids = [((a.get("target") or {}).get("id")) for a in
                                ((raw.get("attachments") or {}).get("data") or [])
                                if (a.get("media_type") or "").startswith("video")]
                        if vids and vids[0]:
                            raw = dict(raw, id=vids[0], post_id=raw["id"])
                            video_ids.add(vids[0])
                            kind_here = "reel"
                        else:
                            kind_here = kind
                        add(raw, kind_here, "scheduled")
                        continue
                    if edge == "published_posts":
                        # a Reel/video also shows up here as a feed post with a different ID —
                        # keep only the video object so it can never be commented twice
                        targets = {((a.get("target") or {}).get("id"))
                                   for a in ((raw.get("attachments") or {}).get("data") or [])}
                        if targets & video_ids:
                            continue
                    st = status
                    if st is None:                            # reels / videos: infer
                        vs = ((raw.get("status") or {}).get("video_status") or "").lower()
                        sched = raw.get("scheduled_publish_time") or vs == "scheduled"
                        unpub = raw.get("published") is False or vs in ("draft", "processing")
                        st = "scheduled" if sched else ("draft" if unpub else "live")
                        video_ids.add(raw["id"])
                    if st == "draft":
                        continue
                    add(raw, kind, st)
            except FBError as e:
                errors.append(str(e))
        return list(items.values()), errors

    def page_comments(self, object_id):
        """Comments on a post that were made by this Page."""
        res = self._req(f"{object_id}/comments", {"fields": "id,message,from,created_time,permalink_url",
                                                  "filter": "stream", "limit": 100})
        return [c for c in res.get("data", []) if (c.get("from") or {}).get("id") == self.id]

    def comment(self, object_id, message):
        return self._req(f"{object_id}/comments", {"message": message}, method="POST")["id"]
