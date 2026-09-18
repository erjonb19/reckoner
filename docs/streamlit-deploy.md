# Deploying the page on Streamlit Community Cloud

The app reads `summary/*.csv` from this repository and makes **no network
calls**. There is nothing to authenticate with, so there are no secrets to
configure — which is the point. If a deployment step ever asks you for a
credential, something is wrong with the app, not the step.

## Steps

1. Sign in at <https://share.streamlit.io> with the GitHub account that owns
   this repository. Authorise the Streamlit app for **public repositories**
   only; it does not need write access or private-repo scope.
2. **Create app** → **Deploy a public app from GitHub**.
3. Fill in exactly:
   - **Repository**: `erjonb19/reckoner`
   - **Branch**: `main`
   - **Main file path**: `streamlit_app.py`
   - **App URL**: your choice; `reckoner` if it is free.
4. Leave **Advanced settings** alone. Python version defaults are fine, and the
   **Secrets** box stays empty — see above.
5. **Deploy**. First build installs `requirements.txt` (Streamlit only) and takes
   a couple of minutes.

## Afterwards

- The app redeploys on every push to `main`, so the monthly `summary` workflow
  committing a new snapshot refreshes the page with no action from you.
- The page shows `built_at` and the source vintages above the fold. If those
  dates look old, the dataset is old — the page is not guessing.
- Community Cloud sleeps an app after a period of no traffic and wakes it on the
  next visit, which takes a few seconds. Nothing is lost.

## Running it locally first

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

All of the page's logic is in `pipeline.summary_view`, which imports no UI and is
covered by `tests/pipeline/test_summary_view.py`. That is deliberate: the one
thing that would be embarrassing on a public page is a filter that looks applied
and is not, and that is now a pure function with a test on it.
