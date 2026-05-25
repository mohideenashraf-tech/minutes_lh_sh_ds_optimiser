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

**Browser refresh does not deploy code.** Only Render finishing a new build does.

| Check | Old build (what you see now) | New build |
|-------|------------------------------|-----------|
| `/api/health` | `"sample": true`, no `build_id` | `"build_id": "google-sheets-raw1-v2"`, `"data_source": "google_sheets"` |
| `/api/ui-version` | 404 | JSON with `has_live_data_section: true` |
| Homepage | Input workbook / CSV upload | **Live data** + Refresh Raw_1 |

If health still shows `"sample": true` after Manual Deploy, the service is linked to the wrong repo/branch or the deploy failed — open **Logs** on Render.

## Blueprint

**New → Blueprint** uses `render.yaml` (`autoDeploy: true`, Python 3.11, health `/api/health`).

Starter plan: set HTTP request timeout to **600s** for long optimizer runs.
