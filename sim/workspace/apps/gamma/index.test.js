const oracle = require("../../oracle");
const { summary } = require("./index");

test("summary carries every dependency literal from source", () => {
  const shared = oracle.name("libs/shared");
  const expected = `${oracle.name("apps/gamma")}(${oracle.name("libs/alpha")}(${shared}),${oracle.name("libs/beta")}(${shared}))`;
  expect(summary()).toBe(expected);
});
