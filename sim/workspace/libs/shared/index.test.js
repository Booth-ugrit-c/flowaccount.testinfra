const oracle = require("../../oracle");
const { summary } = require("./index");

test("summary matches the source literal", () => {
  expect(summary()).toBe(oracle.name("libs/shared"));
});
