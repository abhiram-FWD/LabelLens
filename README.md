# LabelLens

LabelLens is a Django application for reviewing product packaging labels. Upload one or more package photos to extract common declarations, check them against configured rules, and download a PDF summary.

## Features

- Account registration, login, and private scan history.
- OCR text extraction, field detection, and barcode / QR decoding.
- Weighted compliance checks with findings and suggested fixes.
- Dashboard charts, filtered history, and downloadable PDF reports.
- Authenticated JSON scan endpoint and database-backed health endpoint.

## Architecture

```text
Browser ── Django pages ──┐
                          ├── Scan pipeline ── PostgreSQL
Client ─── POST /api/scan ┘       │
                                  ├── OpenCV + Tesseract OCR
                                  ├── Field extractor
                                  ├── pyzbar barcode / QR decoder
                                  └── Active DB rules ── Rule results
                                           │
                                           └── ReportLab PDF ── media storage
```

## Local setup

Prerequisites: Python 3.12+, PostgreSQL, and the Tesseract OCR executable. The Docker image installs Tesseract and ZBar for barcode decoding. On Windows, install Tesseract separately and set `TESSERACT_CMD` to its executable path. If `pyzbar` cannot find ZBar, install the platform's ZBar library; barcode decoding will otherwise be skipped while OCR continues.

1. Create and activate a virtual environment.
2. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set the values for your machine. Create the PostgreSQL database named in `DATABASE_URL` first.
4. Apply schema migrations and seed rules:

   ```powershell
   python manage.py migrate
   python manage.py seed_rules
   ```

5. Start the development server:

   ```powershell
   python manage.py runserver
   ```

The app uses PostgreSQL through `DATABASE_URL`; it does not silently fall back to SQLite.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Required PostgreSQL connection URL, such as `postgresql://user:password@localhost:5432/metrology`. |
| `SECRET_KEY` | Django signing key. Use a unique generated secret outside local development. |
| `DEBUG` | `True` for local development; set `False` in production. |
| `ALLOWED_HOSTS` | Comma-separated Django host names. |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated full origins, including `https://` where applicable. |
| `TESSERACT_CMD` | Tesseract executable name or full path; defaults to `tesseract`. |
| `PORT` | Optional web-server port. The Docker command defaults to `8000` when unset. |

When `DEBUG=False`, session and CSRF cookies are secure. `SECURE_PROXY_SSL_HEADER` trusts the standard forwarded-protocol header from the hosting proxy.

## API and health check

`POST /api/scan/` accepts authenticated multipart form data with one or more image files under the `images` key. It returns the scan status and score, extracted text and fields, product / barcode data, and rule results with suggested fixes. Authenticate with the signed-in session (include the CSRF token for browser requests) or a DRF token:

```text
Authorization: Token <token>
```

Create a token for an existing user with:

```powershell
python manage.py drf_create_token <username>
```

`GET /health/` returns `200` when the database is reachable and `503` when it is not. It does not require authentication and is suitable for a platform health probe.

## How rules work

Active `Rule` rows are read from the database for each evaluation. Each rule has a stable unique `code`, display name, legal reference, weight, and optional fix suggestion. The evaluator dispatches each code to a validator in `scanner/services/rules.py`. Validators return `pass`, `fail`, or `missing`; passing weights form the score. Scores of 85 or more are compliant, 50 through 84 are partial, and below 50 are non-compliant. The country-of-origin check is treated as not applicable unless an “Imported by” declaration is detected.

## Adding a rule

1. Add the rule row with a unique `code` and the desired weight and fix suggestion. Use the Django admin or a data migration / seed command so the row exists in each environment.
2. Add a validator function to `scanner/services/rules.py` and add its code-to-function entry in `VALIDATORS`.
3. Add tests covering passing, failing, and missing data.
4. If the new rule should be installed by `seed_rules`, add its definition to `scanner/management/commands/seed_rules.py`.

An active database rule without a registered validator is reported as missing with an explanatory message.

## Testing

Configure `DATABASE_URL` to a test-capable PostgreSQL database, install the requirements, then run:

```powershell
python manage.py test
```

## Deployment: Render Docker web service and managed PostgreSQL

1. Push the repository to a Git provider and create a Render **PostgreSQL** database in the desired region. Use PostgreSQL 16 or later and keep the database and web service in the same region.
2. Create a Render **Web Service** for the repository and select **Docker** as the runtime. Render will build the root `Dockerfile`.
3. Set these web-service environment variables in Render:
   - `DATABASE_URL`: the managed database's internal connection string.
   - `SECRET_KEY`: generate a unique secret in Render.
   - `DEBUG`: `False`.
   - `ALLOWED_HOSTS`: `.onrender.com` (or the exact Render hostname and any custom domain).
   - `CSRF_TRUSTED_ORIGINS`: the full HTTPS Render hostname, for example `https://your-service.onrender.com`.
   - `TESSERACT_CMD`: `tesseract` (already installed in the Docker image).
4. Set the health check path to `/health/`.
5. The container entrypoint runs `migrate`, `seed_rules`, and `collectstatic` before starting Gunicorn. Rule seeding is safe to repeat.
6. Create an administrator from the Render shell with `python manage.py createsuperuser`.

The container's local media directory is ephemeral on typical web-service instances. Use a persistent disk or object storage for uploaded images and generated reports before relying on them across deploys or restarts. Keep a separate backup policy for PostgreSQL.

## Limitations

- OCR and field extraction are best-effort and can misread low-resolution, reflective, curved, or multilingual labels.
- Rule checks are automated screening aids, not a legal opinion. Exceptions and product-specific requirements may need human review.
- Barcode / QR decoding needs the native ZBar library; the app continues without it but cannot attach a decoded barcode to the product.
- Uploaded images and generated PDFs need persistent or object storage in production; the default filesystem storage is local to the running container.
