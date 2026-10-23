import assert from "node:assert/strict";
import { test } from "node:test";

import { parseLinkFragment, takeLinkFromLocation } from "./cloudLink";

const RAW = "pkl_abcdefghijklmnopqrstuvwxyz0123456789_-AB";

test("parseLinkFragment reads #link= and rejects anything else", () => {
  assert.equal(parseLinkFragment(`#link=${RAW}`), RAW);
  assert.equal(parseLinkFragment(`#x=1&link=${RAW}`), RAW);
  assert.equal(parseLinkFragment("#link=pkd_device_token_is_not_a_link"), null);
  assert.equal(parseLinkFragment("#link=pkl_<script>"), null);
  assert.equal(parseLinkFragment(""), null);
});

test("takeLinkFromLocation clears the link from the address bar", () => {
  const calls: string[] = [];
  const link = takeLinkFromLocation(
    { hash: `#link=${RAW}`, pathname: "/", search: "" },
    { replaceState: (_d, _u, url) => calls.push(url ?? "") },
  );
  assert.equal(link, RAW);
  assert.deepEqual(calls, ["/"]);
  assert.equal(takeLinkFromLocation({ hash: "", pathname: "/", search: "" }, undefined), null);
});
