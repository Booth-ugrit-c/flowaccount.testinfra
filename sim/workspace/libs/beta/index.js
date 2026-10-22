const shared = require("@sim/shared");

exports.name = () => "beta-v1";
exports.summary = () => `${exports.name()}(${shared.summary()})`;
