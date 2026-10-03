# The Orange Cart PH — auto "Order here" comment

Every ~5 minutes GitHub Actions checks the Facebook Page for Reels that just went live and
posts `Order here 👉 <that product's Shopee link>` as a comment **on that Reel's own ID**.

- `autocomment.py` / `fbpage.py`: the commenter (stdlib Python, Graph API)
- `catalog.json`: products + affiliate links (synced from the Mac)
- `autocomment_settings.json`: Preview/Auto + holds (set from the Mac dashboard)
- `autocomment_state.json`: results, written by the workflow

Facebook access lives only in the repo secrets `FB_PAGE_ID` / `FB_PAGE_TOKEN`.
A post is commented only when it's matched to exactly one product, its link resolves to the
affiliate ID and that product, and the Page hasn't already commented a link on it.
