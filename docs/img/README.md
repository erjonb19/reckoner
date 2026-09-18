# Screenshots

Generated, not hand-taken. Regenerate with the app running locally:

```bash
streamlit run streamlit_app.py --server.headless true --server.port 8599 &
python scripts/shoot_streamlit.py --port 8599 --out docs/img
python scripts/make_gif.py
```

Shot against a **local** server rather than the deployed URL. Community Cloud
sleeps an app on idle and wakes it on the next visit, so a run against the
deployed page captures a loading spinner as often as a page — and a screenshot
of a spinner is worse than none, because it looks like the app.

`scripts/shoot_streamlit.py` waits on the page's own content — a known string, a
settled status widget — never on a fixed number of seconds. A sleep long enough
to be reliable here would be too short on a slower machine, and would fail by
capturing a half-drawn page, which is the kind of failure that ships.

The GIF's intermediate frames are not kept: they are inputs to
`scripts/make_gif.py` and regenerating them is one command.
