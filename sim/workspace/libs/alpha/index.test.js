const oracle = require("../../oracle");
const { summary } = require("./index");

test("summary carries every dependency literal from source", () => {
  expect(summary()).toBe(`${oracle.name("libs/alpha")}(${oracle.name("libs/shared")})`);
});
