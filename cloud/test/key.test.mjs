import { test } from "node:test";
import assert from "node:assert/strict";
import { docKey } from "../src/index.js";

test("normaliza chaves como o Pi", () => {
  const k = (u) => docKey(new URL("https://x" + u));
  assert.equal(k("/api/series?signals=rpm&span=3600000&max_points=600"), "/api/series?signals=rpm&span=3600000");
  assert.equal(k("/api/alerts?days=90&severity=crit"), "/api/alerts?days=90&severity=crit");
  assert.equal(k("/api/advice?rule=z%3Amap%40idle_warm&source="), "/api/advice?rule=z:map@idle_warm");
  assert.equal(k("/api/trips/12"), "/api/trips/12");
  assert.equal(k("/api/fuel?x=1"), "/api/fuel");
  assert.equal(k("/api/report/period?offset=-1&kind=week"), "/api/report/period?kind=week&offset=-1");
});
