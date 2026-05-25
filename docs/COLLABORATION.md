# Adding a collaborator

## GitHub (recommended)

1. Open https://github.com/mohideenashraf-tech/minutes_lh_sh_ds_optimiser/settings/access
2. Click **Invite a collaborator**
3. Enter their **GitHub username** or email
4. Choose role:
   - **Write** — push code, no admin settings
   - **Maintain** — manage issues/PRs without full admin
   - **Admin** — full repo settings (use sparingly)

They accept the invite by email or GitHub notifications.

## Clone after invite

```bash
git clone https://github.com/mohideenashraf-tech/minutes_lh_sh_ds_optimiser.git
cd minutes_lh_sh_ds_optimiser
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Branch workflow

```bash
git checkout -b feature/your-change
# edit, commit
git push -u origin feature/your-change
```

Open a Pull Request on GitHub for review before merging to `main`.

## Secrets (do not commit)

- `ORS_API_KEY` — set locally in env or in Render dashboard only
- Never commit `.env` files

## Render access

Collaborators need separate access to the Render workspace, or the repo owner adds them under **Render Dashboard → Team**.
