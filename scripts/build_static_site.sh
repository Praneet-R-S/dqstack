#!/bin/sh
# Build the public website (site/) as plain static files in public/, for a static host
# such as a Render Static Site (deploy/RENDER.md, "The landing page alone").
#
#   APP_URL=https://dqstack.onrender.com sh scripts/build_static_site.sh
#
# The server normally serves site/assets at /site-assets/ and the app at /app/. Here the
# assets are copied to public/site-assets/, and links into the app point at APP_URL, the
# address of the running app. Without APP_URL they point at the page's own last section.
set -e
cd "$(dirname "$0")/.."

rm -rf public
mkdir -p public
cp site/*.html public/
cp -R site/assets public/site-assets

APP_URL=$(printf '%s' "${APP_URL:-}" | sed 's:/*$::')
if [ -n "$APP_URL" ]; then
  SIGNIN="$APP_URL/app/#/signin"
  SIGNUP="$APP_URL/app/#/signup"
  PLANNER="$APP_URL/planner/"
else
  SIGNIN="#ready"
  SIGNUP="#ready"
  PLANNER="#ready"
fi

for f in public/*.html; do
  sed -i.bak \
    -e "s|\"/app/#/signin\"|\"$SIGNIN\"|g" \
    -e "s|\"/app/#/signup\"|\"$SIGNUP\"|g" \
    -e "s|\"/planner/\"|\"$PLANNER\"|g" \
    "$f"
  rm -f "$f.bak"
done

if grep -l '"/app/\|"/planner/' public/*.html >/dev/null 2>&1; then
  echo "links into the app were left unchanged:" >&2
  grep -n '"/app/\|"/planner/' public/*.html >&2
  exit 1
fi
echo "public/: $(find public -type f | wc -l | tr -d ' ') files; app links -> ${APP_URL:-this page's last section}"
