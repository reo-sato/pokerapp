/** testMode の判定 (node:test, `npm test`)。 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { isTestMode } from "./testMode";

test("isTestMode: only when ?test is in the URL", () => {
  assert.equal(isTestMode("?test"), true);
  assert.equal(isTestMode("?test=1"), true);
  assert.equal(isTestMode("?a=1&test"), true);
  assert.equal(isTestMode(""), false);
  assert.equal(isTestMode(undefined), false);
  assert.equal(isTestMode("?testing"), false);
  assert.equal(isTestMode("?contest=1"), false);
});
