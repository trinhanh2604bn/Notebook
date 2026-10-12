"""Check FACEBOOK_ACCESS_TOKEN against the Graph API without printing the token.

Shows the token type, expiry, granted permissions, the Pages it manages and,
optionally, whether it can read a given video's ``source`` (what URL import needs):

    python scripts/check_facebook_token.py
    python scripts/check_facebook_token.py "https://www.facebook.com/reel/1234567890"
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.url_import.urls import parse_video_url, resolve_short_link  # noqa: E402


def _error(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        return f"HTTP {resp.status_code}"
    return f"HTTP {resp.status_code}, code {err.get('code')}: {str(err.get('message', ''))[:200]}"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
    token = settings.facebook_access_token
    if not token:
        print("FACEBOOK_ACCESS_TOKEN chưa được đặt (trong .env hoặc Replit Secrets).")
        return 1
    base = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    headers = {"Authorization": f"Bearer {token}"}
    ok = True
    with httpx.Client(timeout=20, headers=headers, trust_env=False) as c:
        r = c.get(f"{base}/debug_token", params={"input_token": token})
        if r.status_code != 200:
            print(f"Token không hợp lệ hoặc đã hết hạn ({_error(r)}).")
            return 1
        data = r.json().get("data") or {}
        exp = data.get("expires_at") or 0
        print(f"Loại token : {data.get('type')}  (cần PAGE để đọc video của Trang)")
        print(f"Hợp lệ     : {data.get('is_valid')}")
        print(f"Hết hạn    : {datetime.fromtimestamp(exp, timezone.utc):%Y-%m-%d %H:%M UTC}" if exp else
              "Hết hạn    : không hết hạn")
        print(f"Quyền      : {', '.join(data.get('scopes') or []) or '(không có)'}")
        if data.get("profile_id"):
            print(f"Trang (ID) : {data['profile_id']}")

        r = c.get(f"{base}/me/accounts", params={"fields": "id,name"})
        if r.status_code == 200:
            pages = r.json().get("data") or []
            print("Trang quản lý: " + (", ".join(f"{p['name']} ({p['id']})" for p in pages) or "(không có)"))

        if len(sys.argv) > 1:
            parsed = parse_video_url(sys.argv[1])
            if parsed.needs_resolve:
                parsed = resolve_short_link(parsed, c)
            r = c.get(f"{base}/{parsed.video_id}", params={"fields": "title,length,from,source"})
            if r.status_code == 200 and r.json().get("source"):
                owner = (r.json().get("from") or {}).get("name")
                print(f"Video {parsed.video_id}: ĐỌC ĐƯỢC (chủ sở hữu: {owner}) -> URL import sẽ hoạt động.")
            else:
                ok = False
                print(f"Video {parsed.video_id}: KHÔNG đọc được ({_error(r)}).")
                print("  Token chỉ đọc được video của Trang mà token đó thuộc về.")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
