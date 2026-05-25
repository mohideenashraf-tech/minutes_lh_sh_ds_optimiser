# Deploy on Render

1. Connect repo: https://github.com/mohideenashraf-tech/minutes_lh_sh_ds_optimiser
2. **New → Blueprint** — uses `render.yaml`
3. Set `ORS_API_KEY` in Render env (optional)
4. **Starter** plan (~$7/mo) + HTTP timeout **600s** for full optimizer runs

Service URL: `https://spillover-milkrun-optimizer.onrender.com`

Health: `/api/health`
