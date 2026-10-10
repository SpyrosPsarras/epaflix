import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { resolve } from "node:path";

const require = createRequire("/tools/node_modules/");
const { createJiti } = require("jiti");
const jiti = createJiti(import.meta.url, { tryNative: false });
const { runRpcQuestionnaire } = await jiti.import(resolve(process.argv[2], "rpc-fallback.ts"));
const question = {
  question: "Post the draft?",
  options: [
    { label: "Post it", description: "Publish the draft" },
    { label: "Keep draft", description: "Do not publish" },
  ],
};
const cases = [
  ["a: offered option", (_title, options) => options[0], "option", "Post it"],
  ["b: typed answer", () => "Post it but change the title", "custom", "Post it but change the title"],
  ["c: digit-prefixed answer", () => "2 but change the title", "custom", "2 but change the title"],
];

for (const [name, select, kind, answer] of cases) {
  try {
    const result = await runRpcQuestionnaire({
      select,
      input: async () => { throw new Error("Unexpected input follow-up"); },
    }, { questions: [question] });
    assert.equal(result.cancelled, false);
    assert.equal(result.answers.length, 1);
    assert.equal(result.answers[0].kind, kind);
    assert.equal(result.answers[0].answer, answer);
    console.log(`PASS ${name}`);
  } catch (error) {
    console.error(`FAIL ${name}: ${error.message}`);
    process.exitCode = 1;
  }
}
