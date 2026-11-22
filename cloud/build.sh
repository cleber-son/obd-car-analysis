#!/bin/bash
# Copia o painel do Pi para os arquivos estáticos do Worker (mesmo HTML/JS/CSS).
set -e
cd "$(dirname "$0")"
rm -rf public && mkdir -p public/static
S=../src/obdpi/web/static
cp $S/*.css $S/*.js $S/*.png public/static/
cp $S/index.html public/index.html
cp $S/drive.html public/drive.html
cp $S/oficina.html public/oficina.html
cp $S/share.html public/share.html                    # F9: página do link de compartilhamento (/s/<token>)
cp $S/sw.js public/sw.js                              # o service worker precisa ficar na raiz para valer para o site todo
cp $S/manifest.webmanifest public/manifest.webmanifest
echo "public/ pronto"
