const shared = require("@sim/shared");

exports.name = () => "alpha-v1";
exports.summary = () => `${exports.name()}(${shared.summary()})`;
