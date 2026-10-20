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

To enable the Naver public-transit route button from `서울 금천구 가산디지털2로 61` to the selected company address, create Naver Maps API credentials and set:

```powershell
$env:NAVER_MAP_CLIENT_ID = "your-naver-client-id"
$env:NAVER_MAP_CLIENT_SECRET = "your-naver-client-secret"
python .\dashboard_server.py
```

The route button opens Naver Maps' public-transport route scheme. Naver's geocoding service requires these credentials, and the Naver Maps app must support the `nmap://` URL scheme on the device. [Naver Maps URL scheme documentation](https://guide.ncloud-docs.com/docs/en/maps-url-scheme)

Naver route functionality is isolated in `naver_route.py`; `dashboard_server.py` imports its geocoding and public-transit route functions.

You can also test the route module directly:

```powershell
$env:NAVER_MAP_CLIENT_ID = "your-client-id"
$env:NAVER_MAP_CLIENT_SECRET = "your-client-secret"
python .\naver_route.py "서울특별시 중구 세종대로 67"
```

`naver_route.py` reads the Naver credentials from the environment and prints the values it is using before making the request. This is useful for troubleshooting, but avoid sharing the console output because it contains the client secret.

Use `--start` to override the default starting address:

```powershell
python .\naver_route.py "서울특별시 중구 세종대로 67" --start "서울 금천구 가산디지털2로 61"
```

Or pass the key directly:

```powershell
python .\opendart_fetcher.py --corp-code 00126380 --api-key "your-key" --out-dir .\temp
```

The output folder contains `output.json` by default, annual/quarterly PDFs when browser printing succeeds, and the original filing ZIPs. Use `--output-json` to choose a different JSON filename. The script searches the most recent five years and selects the newest matching annual and Q1/Q3 filings. If no annual report is recognized, it still writes a diagnostic `output.json` containing the disclosure candidates.
