# web-scraper

Search one or more search engines, read the top results **and the relevant links inside them**, and save
all the raw text into one Markdown file (plus JSON) that you can hand to an LLM or read to summarise.

Supported engines: `google`, `bing`, `duckduckgo`, `brave`, `yahoo` (or `all`).

## Setup (once)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python -m playwright install chromium
```

Chromium is needed for Google and Brave (they block plain HTTP clients) and as a fallback for
JavaScript-heavy or bot-protected pages.

## Usage

### Web app

```bash
python app.py                 # opens http://127.0.0.1:8000 in your browser
python app.py --port 9000 --no-open
```

Same options as the CLI in a form, a live progress log with a Stop button, and a results viewer:
pages with their full text (filterable), the per-engine search results, pages that couldn't be read,
"Copy all text" / download `.md` / `.json` buttons, and a history of past runs.

### Command line

Interactive (asks for query, engines, how many results, link depth):

```bash
python -m scraper
```

One-liner:

```bash
python -m scraper "how do solid state batteries work" -e google,bing,duckduckgo -n 5
```

| Option | Meaning | Default |
| --- | --- | --- |
| `-e, --engines` | comma-separated engines, or `all` | `google,bing,duckduckgo` |
| `-n, --top` | top results to read **per engine** | `5` |
| `-d, --depth` | levels of links to follow from each result (`0` = results only) | `1` |
| `-l, --links` | max links followed per page (picked by relevance to your query) | `3` |
| `--any-domain` | also follow links to other websites | off (same site only) |
| `--max-pages` | hard cap on total pages fetched | `100` |
| `--max-chars` | truncate each page's text in the Markdown file | no limit |
| `--browser` | render every page in Chromium (slower, best for JS sites) | off |
| `--show-browser` | visible browser window, e.g. to solve a CAPTCHA | off |
| `--no-js-fallback` | don't retry blocked / near-empty pages in the browser | off |
| `--ignore-robots` | skip `robots.txt` checks | off |
| `-o, --out-dir` | output folder | `output` |

Examples:

```bash
# 10 results from every engine, results only (no link following)
python -m scraper "rust vs go for backend" -e all -n 10 -d 0

# Go deeper: 3 results each, follow 5 links per page, 2 levels deep
python -m scraper "EU AI act obligations for startups" -e google,brave -n 3 -d 2 -l 5
```

## Output

Each run creates `output/<timestamp>_<query>/`:

- `raw_data.md` – search results list, then the full text of every page with its URL, which engine/rank
  found it (or which page linked to it), author and publish date when available. Feed this to your summariser.
- `data.json` – the same data in structured form.

Duplicate pages (same final URL or identical text) are fetched once; the same URL found by several
engines is listed once with all its sources.

## Notes

- Search engines rate-limit and sometimes block scrapers. If an engine returns nothing, or results that
  have nothing to do with your query (Bing does this to automated traffic), the tool logs a warning and
  carries on with the other engines. Wait a few minutes or pick different engines.
- Bing usually serves automated clients only its first page (~10 results).
- Link following prefers links whose text or URL contains words from your query, and skips
  navigation, login, legal and social-sharing links.
