# Open DART corporation fetcher

`opendart_fetcher.py` retrieves, for one 8-digit Open DART corporation code:

- key financial accounts and the complete `fnlttSinglAcntAll` rows;
- employment rows from `empSttus` (staff count, average years of service, total annual compensation and average salary where the filing provides them);
- the latest annual report and latest available Q1 or Q3 report;
- each original filing as the Open DART XML ZIP, plus a PDF printed from the DART viewer.

Open DART's official original-document API returns XML ZIP files rather than PDF files. The script therefore uses a locally installed Edge/Chrome/Chromium executable in headless mode to print the DART viewer page. If no browser is found, it writes a viewer URL text file so the report can still be opened manually.

## Usage

Get an API key from Open DART, then run from this folder:

```powershell
$env:OPENDART_API_KEY = "your-40-character-key"
python .\opendart_fetcher.py --corp-code 00126380 --out-dir .\temp
```

For detailed troubleshooting, add `--debug`. This writes both console output and `temp\opendart_debug.log`; the API key is masked.

```powershell
python .\opendart_fetcher.py --corp-code 00126380 --debug
```

Test the API key and corporation code before fetching reports:

```powershell
python .\opendart_fetcher.py --corp-code 00126380 --test-connection --debug
```

An `ok: true` result confirms the key and corporation code. If it fails, the response includes the Open DART status and message.

To skip slow browser-based PDF generation and fetch only JSON data plus the original filing ZIPs:

```powershell
python .\opendart_fetcher.py --corp-code 00126380 --skip-pdf
```

To export every Open DART corporation code and company name:

```powershell
python .\opendart_fetcher.py --export-corp-list .\temp\corporations.txt
```

The file uses this format:

```text
corp-code | company name
```

## Local HTML dashboard

First create the corporation lookup file, then start the local dashboard:

```powershell
python .\opendart_fetcher.py --export-corp-list .\temp\corporations.txt
$env:OPENDART_API_KEY = "your-40-character-key"
python .\dashboard_server.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Type a company name such as `삼성`, select a suggestion, and click **Search** or press Enter. The dashboard keeps the API key in the Python backend and shows annual and quarterly charts in the browser.

The dashboard asks for a starting point and leaves it blank on a first visit.
After you enter one, it is remembered in the browser for future visits. The
CLI route commands still use `START_ADDRESS` in `naver_route.py` as their
default and can override it with `--start`.

To enable Naver Maps API features, create Naver Maps API credentials and set:

```powershell
$env:NAVER_MAP_CLIENT_ID = "your-naver-client-id"
$env:NAVER_MAP_CLIENT_SECRET = "your-naver-client-secret"
python .\dashboard_server.py
```

The dashboard's public-transit panel uses a browser session for Naver Maps
directions. It does not require geocoding credentials; the CLI's geocoding and
driving-route commands do require the appropriate Naver Maps API credentials.

You can also query a driving route directly. The command prints a compact
summary with distance, duration, toll, taxi fare, and fuel cost:

```powershell
$env:NAVER_MAP_CLIENT_ID = "your-client-id"
$env:NAVER_MAP_CLIENT_SECRET = "your-client-secret"
python .\naver_route.py "서울특별시 중구 세종대로 67"
```

`naver_route.py` reads the Naver credentials from the environment. The driving
route uses Directions 5 and requires that API to be enabled in the Maps
application.

Use `--start` to override the default starting address:

```powershell
python .\naver_route.py "서울특별시 중구 세종대로 67" --start "서울 금천구 가산디지털2로 61"
```

To generate the Naver Maps public-transit app URL used by the dashboard, add
`--public-transit-url`.

The dashboard opens Naver Maps in headless Chromium with a per-process profile.
On Render, the included Docker image installs Node.js, Playwright, and
Playwright's bundled Chromium automatically. A custom browser executable is
optional via `NAVER_EDGE_PATH`; do not use a Windows Edge path on Render.
Naver Maps resolves the start and destination inside its own directions form,
so the dashboard does not make separate Maps Geocoding API requests. It sends
the first public-transit route to the left panel, then takes a screenshot of
that same page for the right panel. Edge and Node.js with
Playwright are required. The included `Dockerfile` and `render.yaml` provide
the Render runtime. Deploy the service with the Docker runtime and set
`OPENDART_API_KEY` in Render's environment settings.

For a local non-Docker run, install Node.js and Playwright, then run
`npm install` and `npx playwright install chromium` before starting the
Python server.

Or pass the key directly:

```powershell
python .\opendart_fetcher.py --corp-code 00126380 --api-key "your-key" --out-dir .\temp
```

The output folder contains `output.json` by default, annual/quarterly PDFs when browser printing succeeds, and the original filing ZIPs. Use `--output-json` to choose a different JSON filename. The script searches the most recent five years and selects the newest matching annual and Q1/Q3 filings. If no annual report is recognized, it still writes a diagnostic `output.json` containing the disclosure candidates.
