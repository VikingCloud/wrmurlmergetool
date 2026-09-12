# WRM URL Merge Tool

A command line tool that makes the list of active URLs for a single
VikingCloud Web Risk Monitoring (WRM) merchant match a plain text file of
URLs, using the WRM API.

* URLs in the file that the merchant does not already have are **added**.
* Active URLs the merchant has that are not in the file are **disabled**.
* URLs in both places are left alone.

When the tool finishes, the merchant's active URLs equal the contents of the
file.

## Why this exists

The WRM Bulk Import spreadsheet's *advanced merge* mode requires every URL for
a merchant to be placed in a single cell, separated by semicolons. Merchants
with a very large number of URLs (100 or more) can exceed what fits in one
cell, and the import fails. This tool performs the same merge for one merchant
at a time through the API, with no cell size limit.

## Requirements

* Python 3.9 or newer
* A VikingCloud Portal user account that is enabled for API access and has
  permission to manage the merchant's URLs. If you are a WRM customer and need
  API access, contact <WRMSupport@vikingcloud.com>.

## Installation

Clone the repository and create a virtual environment so the tool's
dependencies stay isolated from the rest of your system.

macOS / Linux:

```bash
git clone https://github.com/vikingcloud/wrmurlmergetool.git
cd wrmurlmergetool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
git clone https://github.com/vikingcloud/wrmurlmergetool.git
cd wrmurlmergetool
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Each time you open a new terminal, activate the virtual environment again
(`source .venv/bin/activate` or `.venv\Scripts\Activate.ps1`) before running
the tool.

## Credentials

The tool reads your credentials from environment variables, or from a `.env`
file in the directory you run it from. Environment variables take priority
over the `.env` file.

| Variable       | Required | Description                                                     |
| -------------- | -------- | --------------------------------------------------------------- |
| `WRM_USERNAME` | yes      | VikingCloud Portal user name                                    |
| `WRM_PASSWORD` | yes      | VikingCloud Portal password                                     |
| `WRM_API_URL`  | no       | API base URL. Defaults to `https://api.vikingcloud.com`         |

The easiest setup is to copy the example file and edit it:

```bash
cp .env.example .env
```

```
WRM_USERNAME=apiuser@example.com
WRM_PASSWORD=your-password
```

`.env` is already listed in `.gitignore`. Never commit it, share it, or paste
its contents into a ticket.

Alternatively, export the variables in your shell:

```bash
export WRM_USERNAME='apiuser@example.com'
export WRM_PASSWORD='your-password'
```

### Pointing at a non-production environment

Set `WRM_API_URL` to use a different API host, for example for testing:

```bash
export WRM_API_URL=https://api-test.example.com
```

When a non-default URL is in use the tool prints `Using API URL: ...` at
startup so it is always obvious which environment you are changing.

## Usage

The tool has three commands. Run `python wrm_url_merge.py --help` or
`python wrm_url_merge.py <command> --help` for the full option list.

### 1. Find the merchantId

The WRM API identifies merchants by an internal `merchantId` that is not shown
in the WRM user interface. Use `search` to find it. The search term is matched
(case insensitive) against the merchant name, DBA, MID, id, email and city.

```bash
python wrm_url_merge.py search "Acme"
```

```
merchantId  sponsorId  mid       name              dba          status
----------  ---------  --------  ----------------  -----------  ------
254         2          12345678  Acme Corporation  Acme Stores  Active
261         2          12345699  Acme Outlet LLC   Acme Outlet  Active

2 merchant(s) shown, 1832 scanned.
Use the merchantId column with the sync command.
```

Useful options:

* `--sponsor-id 2` limits the search to merchants under one sponsor. Use the
  `sponsors` command to list the sponsors you can see.
* `--active-only` hides merchants whose status is not Active.
* `--all` lists every merchant instead of filtering.
* `--limit 20` stops after 20 matches.

Because the API does not offer server side text search, `search` pages
through the merchant list and filters locally. On very large sponsors this
can take a little while; `--sponsor-id` makes it faster.

### 2. Prepare the URL file

Create a plain text file with one URL per line:

```
https://www.example.com
https://shop.example.com/
http://legacy.example.com/store
```

* Blank lines and lines beginning with `#` are ignored.
* Semicolon separated values on one line are also accepted, so you can paste
  the contents of a Bulk Import URL cell directly.
* Every URL must start with `http://` or `https://`, have a valid host name,
  and be between 7 and 255 characters (the WRM API's limits).
* If any URL is invalid the tool stops before making changes and lists the
  problems. Fix them, or pass `--skip-invalid` to continue with only the
  valid URLs.
* The file must contain at least one URL. An empty file would disable every
  URL on the merchant, so the tool refuses unless you pass `--allow-empty`.

See `examples/urls.example.txt`.

### 3. Preview with a dry run

```bash
python wrm_url_merge.py sync 254 urls.txt --dry-run
```

The tool reads and validates the file, then fetches the merchant and shows its
details so you can confirm you have the right one:

```
Merchant details
  merchantId : 254
  sponsorId  : 2
  id         : 762
  name       : Acme Corporation
  dba        : Acme Stores
  mid        : 12345678
  location   : Anytown, Texas, US
  status     : Active
  active URLs: 143

Is this the correct merchant? [y/N]: y

Plan
  URLs already present and kept : 140
  URLs to add                   : 5
  URLs to disable               : 3

  Add:
    + https://new-store.example.com
    ...

  Disable:
    - http://old.example.com  (urlId 9912)
    ...

Dry run; no changes were made.
```

### 4. Apply the changes

```bash
python wrm_url_merge.py sync 254 urls.txt
```

You are asked to confirm the merchant, shown the plan, and then asked a second
time to confirm before anything is changed. New URLs are added first, then
URLs missing from the file are disabled. Finally the tool re-reads the
merchant's URLs and verifies that the active list now matches the file.

Options:

| Option             | Description                                                                   |
| ------------------ | ----------------------------------------------------------------------------- |
| `--dry-run`        | Show the plan and stop. No changes are made.                                  |
| `-y`, `--yes`      | Skip both confirmation prompts (for scripted or unattended use).              |
| `--skip-invalid`   | Continue when the file contains invalid URLs, ignoring them.                  |
| `--allow-empty`    | Permit an empty URL file, which disables every active URL on the merchant.    |
| `--boarding-scan`  | Ask WRM to run a boarding scan on each newly added URL.                       |
| `--batch-size N`   | Number of URLs sent per add request (default 25).                             |
| `-v`, `--verbose`  | Print debug output, including every API request and the list of kept URLs.    |

### Exit codes

| Code | Meaning                                                          |
| ---- | ---------------------------------------------------------------- |
| 0    | Success, or dry run completed                                    |
| 1    | An API or authentication error occurred, or verification failed  |
| 2    | Bad input (missing credentials, invalid URL file)                |
| 3    | Aborted by the user at a confirmation prompt                     |

## How URLs are compared

To avoid disabling and re-adding the same site because it was typed slightly
differently, URLs are compared after light normalization:

* scheme and host name are compared case insensitively
* default ports (`:80` for http, `:443` for https) are ignored
* anything after `#` is ignored
* trailing slashes are ignored

So `https://www.Example.com/` in WRM and `https://www.example.com` in your
file are treated as the same URL.

Everything else must match exactly. In particular the **full hostname** is
compared, never just the registered domain: `https://example.com`,
`https://www.example.com` and `https://shop.example.com` are three different
URLs, because they can be three different websites. Likewise `http://` and
`https://` versions of a host are different URLs, and so are different paths
or query strings. This mirrors how WRM itself treats URLs.

When a URL is added, it is sent exactly as written in your file.

Potential URLs suggested by Merchant Discovery are not part of the merchant's
active URL list and are neither added nor disabled by this tool.

## Safety notes

* Nothing is changed until you have confirmed both the merchant and the plan
  (unless you pass `--yes`).
* `--dry-run` always makes zero write calls to the API.
* Disabling a URL in WRM is the same operation performed by the WRM UI and the
  Bulk Import advanced merge; URL history is retained in WRM.
* Keep your `.env` file private. If you believe a password has been exposed,
  change it in the VikingCloud Portal.
* The API is rate limited (1000 requests per hour by default). The tool
  honors `Retry-After` on 429 responses and retries transient server errors.
  A merchant with several hundred URLs uses well under 100 requests.

## API reference

* WRM API: <https://developer.vikingcloud.com/openapi/wrm/>
* Token API: <https://developer.vikingcloud.com/openapi/token/>

Endpoints used: `POST /token`, `GET /wrm/v1/sponsors`, `GET /wrm/v1/merchants`,
`GET /wrm/v1/merchants/{merchantId}`, `GET /wrm/v1/merchants/{merchantId}/urls`,
`POST /wrm/v1/merchants/{merchantId}/urls`,
`DELETE /wrm/v1/merchants/{merchantId}/urls/{urlId}`.

## License

See [LICENSE](LICENSE).
