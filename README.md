# Grant Collaboration Network

This Streamlit app reads the existing `Grants-Network.csv` structure without changing it.

## Features

- Uses the same 10 CSV columns.
- Automatically rereads the CSV every 60 seconds while the page is open.
- Filters by program, academic year, and minimum shared-grant count.
- Clicking a partner node shows all associated individual grant records.
- Clicking an edge shows the grants shared by the two partners.
- Correctly parses quoted organization names containing commas.

## Files

```text
app.py
 grant_network.py
Grants-Network.csv
requirements.txt
```

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows
pip install -r requirements.txt
streamlit run app.py
```

## CSV location

By default, the app reads `Grants-Network.csv` from the same folder.

For a remotely maintained CSV, set `CSV_SOURCE` to a direct CSV URL. For example, in Streamlit Community Cloud, add this to the app's Secrets:

```toml
CSV_SOURCE = "https://raw.githubusercontent.com/OWNER/REPOSITORY/BRANCH/Grants-Network.csv"
```

You can also set the environment variables:

```bash
export CSV_SOURCE="https://example.org/Grants-Network.csv"
export REFRESH_SECONDS="60"
streamlit run app.py
```

The URL must return the CSV file itself, not an HTML preview page.

## Important CSV formatting rule

Continue quoting any partner name that contains a comma:

```csv
"Wegmans Food Markets, Inc",Other Partner
```

The app uses Python's CSV parser for the `Community Partners` cell, matching the logic in the original notebook.

## Optional name cleanup

Confirmed alternative spellings can be mapped in `ALIASES` near the top of `grant_network.py`. This changes only the network representation; it does not edit the source CSV.
