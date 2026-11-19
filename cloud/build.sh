#!/bin/bash
# Copia o painel do Pi para os arquivos estáticos do Worker (mesmo HTML/JS/CSS).
set -e
cd "$(dirname "$0")"
rm -rf public && mkdir -p public/static
cp ../src/obdpi/web/static/style.css ../src/obdpi/web/static/app.js public/static/
cp ../src/obdpi/web/static/index.html public/index.html
cp ../src/obdpi/web/static/drive.html public/drive.html
echo "public/ pronto"
