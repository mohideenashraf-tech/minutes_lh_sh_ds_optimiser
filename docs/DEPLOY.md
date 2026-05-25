# Deploy on Render

Live URL: https://spillover-milkrun-optimizer.onrender.com

## One-time setup (required for Google Sheets + new UI)

1. **Render Dashboard** → service **spillover-milkrun-optimizer** → **Settings**
2. Confirm **Repository** = `mohideenashraf-tech/minutes_lh_sh_ds_optimiser`, branch **`main`**
3. Turn **Auto-Deploy** = **Yes**
4. **Environment** → add:
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — full service account JSON (paste as one line)
   - `ORS_API_KEY` — optional
5. Share both Google Sheets with the service account email (`client_email` in the JSON)

## Deploy latest code now

### Option A — Render Dashboard (fastest)

1. Open https://dashboard.render.com
2. Select **spillover-milkrun-optimizer**
3. **Manual Deploy** → **Deploy latest commit**
4. Wait until status is **Live** (commit `9e89160` or newer)
5. Hard refresh the app (Ctrl+Shift+R)

### Option B — GitHub Actions deploy hook (automated on every `main` push)

1. Render → **Settings** → **Deploy Hook** → copy URL
2. GitHub → repo **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
   - Name: `RENDER_DEPLOY_HOOK_URL`
   - Value: paste deploy hook URL
3. Push to `main` or run workflow **Deploy to Render** manually under **Actions**

## Verify deploy succeeded

| Check | Expected (new build) |
|-------|----------------------|
| https://spillover-milkrun-optimizer.onrender.com/api/ui-version | `has_live_data_section: true` |
| https://spillover-milkrun-optimizer.onrender.com/api/health | `"data_source": "google_sheets"` |
| Homepage | **Live data** section, no CSV upload |

## Blueprint

**New → Blueprint** uses `render.yaml` (`autoDeploy: true`, Python 3.11, health `/api/health`).

Starter plan: set HTTP request timeout to **600s** for long optimizer runs.
