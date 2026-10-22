const alpha = require("@sim/alpha");
const beta = require("@sim/beta");

exports.name = () => "gamma-v1";
exports.summary = () => `${exports.name()}(${alpha.summary()},${beta.summary()})`;
