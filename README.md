# Snackible review tools

- `portal.py` - portal where real reviewers pick a product, tick true keywords and publish to Judge.me (port 5000). Needs ANTHROPIC_API_KEY (optional), JUDGEME_API_TOKEN, SHOP_DOMAIN.
- `synth_app.py` - synthetic review dataset builder for ML experiments only (port 5001). Needs GROQ_API_KEY in `.env`. Has no publishing code.
- `review_audit.py` - scrapes existing reviews and writes an Excel audit.

Setup: `pip install -r requirements.txt`, copy `.env.example` to `.env`, add keys. Never commit `.env`.
