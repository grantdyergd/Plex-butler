#!/bin/bash
git remote remove origin 2>/dev/null || true
git remote add origin https://grantdyergd:$GITHUB_PERSONAL_ACCESS_TOKEN@github.com/grantdyergd/Plex-butler.git
git push -u origin main --force
echo "Done!"
