# Snackible review tools

- `portal.py` - portal where real reviewers pick a product, tick true keywords and publish to Judge.me (port 5000). Needs ANTHROPIC_API_KEY (optional), JUDGEME_API_TOKEN, SHOP_DOMAIN.
- `synth_app.py` - synthetic review dataset builder for ML experiments only (port 5001). Needs GROQ_API_KEY in `.env`. Has no publishing code.
- `review_audit.py` - scrapes existing reviews and writes an Excel audit.

Setup: `pip install -r requirements.txt`, copy `.env.example` to `.env`, add keys. Never commit `.env`.

## Deploying on Vercel
- `pyproject.toml` sets the Vercel entrypoint (`synth_app:app`). To deploy the review portal instead, change it to `portal:app` (or use a second Vercel project).
- Set env vars in Vercel: GROQ_API_KEY (synth), JUDGEME_API_TOKEN + SHOP_DOMAIN (portal), and APP_PASSWORD (login for both, strongly recommended).
- Vercel's disk is temporary: SQLite lives in /tmp and is wiped between cold starts. Export CSVs often or move to a hosted database.
- `prompt_template.txt` is the editable generation prompt; `style_corpus.json` holds the sample reviews used as style anchors.
