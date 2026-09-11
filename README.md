# newsdash (GitHub Pages edition)

A live markets and geopolitics headline dashboard that runs entirely on GitHub.
Nothing needs to be installed anywhere; setup happens in the browser.

## How it works

GitHub Pages can only serve static files, and news sites don't allow browsers
to read their RSS feeds directly from another website. So the work is split:

- **`.github/workflows/update-news.yml`** runs on GitHub's servers every
  5 minutes. It executes `fetch_news.py`, which reads `feeds.json`, collects
  headlines, quotes and (optionally) Benzinga, merges them with the previous
  run and publishes a single `news.json` to a branch called `data`. That
  branch is force-pushed, so it always holds exactly one commit.
- **`index.html`** is the page GitHub Pages serves. It checks `news.json`
  every 60 seconds and slots new headlines into the list without reloading.
  If you're scrolled down, it holds them back and shows a "new headlines"
  button instead of shifting what you're reading.

## Setup (browser only)

1. Create a **public** repository on github.com (e.g. `newsdash`). Tick
   "Add a README" so the repository isn't empty.
2. **Add file → Upload files.** Drag in `index.html`, `feeds.json`,
   `fetch_news.py`, `README.md` **and the `.github` folder**. Commit.
   Check that `.github/workflows/update-news.yml` now exists in the repo.
   If the folder didn't come through, use **Add file → Create new file**,
   type `.github/workflows/update-news.yml` as the name (the slashes create
   the folders), paste the file's contents and commit.
3. Optional, Benzinga: **Settings → Secrets and variables → Actions → New
   repository secret**, name `BENZINGA_API_KEY`, value = your key.
4. **Actions** tab → **Update news** → **Run workflow**. Wait for the green
   tick (about a minute). A `data` branch appears.
5. **Settings → Pages →** Source: *Deploy from a branch*, Branch: `main`,
   folder `/ (root)` → Save.
6. After a minute or two the site is live at
   `https://<your-username>.github.io/<repository-name>/`.

## Changing feeds, quotes or categories

Edit `feeds.json` directly on github.com (pencil icon) and commit. The
workflow runs automatically on that commit, and the open page picks up the
change on its next check. Each source needs `id`, `name`, `category` and
`url`; `category` must match an `id` in `categories`. Add
`"enabled": false` to switch a source off without deleting it.

## Things to know

- **Update rhythm.** GitHub's scheduler aims for every 5 minutes but often
  runs 5–15 minutes late, especially at busy times. The status button shows
  how old the data is and turns amber or red when the job is running late.
- **Everything is public.** Pages sites on free accounts come from public
  repositories, and the `data` branch is readable by anyone. The Benzinga key
  itself stays secret, but the headlines it fetches are published in
  `news.json`. Benzinga's API terms may not allow that, so check your plan
  before adding the secret. Summaries are off for Benzinga by default
  (`include_summaries` in `feeds.json`).
- **Blocked feeds.** Some publishers block requests from cloud servers. Open
  the status button to see which sources failed and why; swap in another
  feed URL if one keeps failing.
- **Quotes** come from Yahoo's chart endpoint and are delayed. If a request
  is rate-limited, the last known value is kept and shown dimmed.
- **Paused schedule.** GitHub may switch off scheduled workflows in public
  repositories after long inactivity. If that happens, the Actions tab shows
  a banner with a button to turn it back on.
- **Custom domain.** If you serve the page from your own domain, set
  `dataUrl` at the top of the script in `index.html` to
  `https://raw.githubusercontent.com/<user>/<repo>/data/news.json`.

## Keyboard

`/` focuses search, `Esc` clears it or closes the sources panel.
