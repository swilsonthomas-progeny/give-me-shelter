# Give Me Shelter

A public tracker of tiny houses, pallet shelters, emergency housing units and RV safe lot spaces opened and closed in Seattle since January 1, 2026, alongside Mayor Katie Wilson's promises and policy moves.

- `index.html` – the site. It reads `data.json` on load.
- `data.json` – every site, promise, timeline entry, city count and watch-list item, each with sources.
- `feeds.txt` – the news, city and provider RSS feeds the job reads first. Add or remove lines freely.
- `scripts/update.py` – the daily research pass. Reads the feeds, has Claude confirm leads and search for anything missed, validates what comes back, and merges it into `data.json`.
- `.github/workflows/daily-update.yml` – runs the research pass every morning and opens a pull request when something changed.

## How the daily loop works

1. Every morning around 7am Pacific, GitHub runs `scripts/update.py`.
2. It reads every feed in `feeds.txt` and keeps items that mention shelter, tiny houses, safe lots, encampments and similar terms. Then Claude checks those leads and searches for anything the feeds missed.
3. The script drops anything missing a source, a valid date or a valid status, then writes the rest into `data.json`.
4. If anything changed, GitHub opens a pull request titled **Ledger update for review**, with a summary of each change and links to sources.
5. You read it (GitHub's phone app works fine) and tap **Merge**. The site updates within a minute or two. Close the PR instead to reject it.

If you don't merge a pull request the same day, the next run adds to it instead of replacing it, so any edits you made in the pull request are kept. Each day's summary is stacked at the top of the description. If you edit data.json on main while a review is open, the job stops and asks you to merge or close the open pull request first.

## One-time setup (about 20 minutes)

1. **Create a public GitHub repository** (for example `give-me-shelter`) and upload every file in this folder, including the hidden `.github` folder and the empty `.nojekyll` file.
2. **Let the workflow sign in to Claude without a key.** The daily job uses workload identity federation: GitHub vouches for the workflow, and the Claude API hands it a token that lasts a few minutes. No API key is stored anywhere.
   1. In the Claude Console (platform.claude.com), open Settings → Workload identity federation → **Connect workload** and choose **GitHub Actions**. You need the admin or owner role.
   2. Enter your repository as `OWNER/give-me-shelter` and the branch `main`. The wizard creates a service account, an issuer and a rule that only accepts tokens from that repo's `main` branch, with audience `https://api.anthropic.com`.
   3. Copy the four IDs it shows: federation rule (`fdrl_…`), organization, service account (`svac_…`) and workspace.
   4. In the repo: Settings → Secrets and variables → Actions → **Variables** tab → New repository variable, once for each: `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`, `ANTHROPIC_SERVICE_ACCOUNT_ID`, `ANTHROPIC_WORKSPACE_ID`. They're identifiers, not passwords, so variables are fine.
   5. Don't add an `ANTHROPIC_API_KEY` secret. If one exists the script uses it instead of federation.
3. **Allow the workflow to open pull requests.** Settings → Actions → General → Workflow permissions: choose "Read and write permissions" and tick "Allow GitHub Actions to create and approve pull requests".
4. **Turn on GitHub Pages.** Settings → Pages → Source: "Deploy from a branch", branch `main`, folder `/ (root)`.
5. **Connect your domain.** In Settings → Pages → Custom domain, enter it (for example `shelterledger.org`). At your registrar add the DNS records GitHub shows you: four `A` records for the bare domain, or a `CNAME` to `<your-username>.github.io` for a `www` subdomain. Tick "Enforce HTTPS" once it's available.
6. **Run it once by hand.** Actions → Daily ledger update → Run workflow. Check the pull request it opens.
7. **Get notified.** Watch the repo (Watch → Custom → Pull requests) so new updates reach your email or phone.

## Costs

- Hosting on GitHub Pages: free.
- Domain: roughly $10–20 a year.
- Claude API: each run uses up to 10 web searches ($10 per 1,000 searches) plus model tokens. Expect roughly $5–15 a month. Feeds are free. Set a monthly spend limit in the Anthropic Console to cap it.

Optional settings in the workflow's `env` block: `LEDGER_MODEL`, `LEDGER_MAX_SEARCHES`, `LEDGER_SEARCH_TOOL`.

## Editing by hand

Everything lives in `data.json`. Edit it on GitHub directly (pencil icon) to fix a number, add a site, or mark a closure. Rules the site relies on:

- `status`: `announced`, `construction`, `open` or `closed`. An `open` site needs `dates.opened`; a `closed` site needs `dates.closed`.
- `units`: counts under `tiny_house`, `pallet`, `emergency_housing` (whole leased buildings), `rv`.
- `units`: tiny houses and pallet shelters are separate keys (`tiny_house`, `pallet`) but share the microshelter band on the chart.
- `city_counts`: unit totals the city has announced (`kind: count`). They're listed in the record but kept off the chart. City projections go in `promises` as goals.
- `promises`: each goal needs `made` (date it was set), `target` and `by` to appear on the chart. Mark it `missed`, `revised` or `delivered` once its date passes.
- A site moves from "announced" to the running total automatically once its status is `open`.
- `program`: `initiative` (part of the mayor's new shelter initiative), `accelerated` (earlier project the city says it sped up) or `other`.
- Unconfirmed closures go in `watch`, which is shown but never subtracted.

To test the script without spending API credit, save a sample reply and run
`LEDGER_FAKE_RESPONSE=sample.txt python scripts/update.py`.

If a feed URL stops working, the next pull request lists it under "Feeds that didn't load." Fix the address or delete the line in `feeds.txt`.
