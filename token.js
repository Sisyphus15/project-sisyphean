const RustPlus = require("rustplus.js");

RustPlus.getLocalInfo().then(info => {
    console.log("===== Rust+ Token Found =====");
    console.log(JSON.stringify(info, null, 2));
}).catch(err => {
    console.error("ERROR:", err);
});
