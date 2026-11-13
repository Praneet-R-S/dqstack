#!/bin/sh
# Build the public website as plain static files in site/public/, for a static host such
# as a Render Static Site (deploy/RENDER.md, "The landing page alone"). On Render, set
# Root Directory to `site`, so it finds no requirements.txt and installs nothing:
#
#   Root Directory: site    Build Command: sh tools/build_static.sh    Publish: public
#
# Locally:  APP_URL=https://dqstack.onrender.com sh site/tools/build_static.sh
#
# The application normally serves site/assets at /site-assets/ and itself at /app/. Here
# the assets are copied to public/site-assets/, and links into the app point at APP_URL,
# the address of the running app; without APP_URL they go to the page's last section.
set -e
cd "$(dirname "$0")/.."                 # the site folder

rm -rf public
mkdir -p public
cp ./*.html public/
cp -R assets public/site-assets

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
echo "site/public/: $(find public -type f | wc -l | tr -d ' ') files; app links -> ${APP_URL:-this page's last section}"
