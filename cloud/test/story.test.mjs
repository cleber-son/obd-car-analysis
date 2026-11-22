// Diagnóstico da viagem no painel online: o documento /api/trips/<id>/story é montado no Pi (analytics/tripstory.py) e
// não leva posição nem nome de lugar, então é um documento comum (público); o trajeto continua atrás da senha.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { isLoc, docKey } from "../src/index.js";

test("o diagnóstico da viagem é um documento comum e o trajeto continua protegido", () => {
  assert.equal(isLoc("/api/trips/32/story"), false);
  assert.equal(docKey(new URL("https://x/api/trips/32/story?t=1")), "/api/trips/32/story");
  assert.equal(isLoc("/api/gps/trips/32/track"), true);
});

test("story.js entra no painel e no service worker", () => {
  const S = new URL("../../src/obdpi/web/static/", import.meta.url);
  assert.match(readFileSync(new URL("index.html", S), "utf8"), /<script src="\/static\/story\.js"><\/script>/);
  assert.match(readFileSync(new URL("sw.js", S), "utf8"), /"\/static\/story\.js"/);
  const js = readFileSync(new URL("story.js", S), "utf8");
  assert.doesNotMatch(js, /\(\?<[=!]/);                 // sem lookbehind: iPhone antigo nem carrega o arquivo
  assert.match(js, /\/api\/gps\/trips\/\$\{s\.trip_id\}\/track/);
});
