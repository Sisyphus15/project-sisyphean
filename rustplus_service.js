// rustplus_service.js
// Minimal Rust+ service: connects to your server and exposes an HTTP API for entity control.

const fs = require('fs');
const path = require('path');
const RustPlus = require('@liamcottle/rustplus.js'); // from github:liamcottle/rustplus.js
const express = require('express');

// ---------- LOAD CONFIG ----------

const CONFIG_PATH = path.join(__dirname, 'rust_config.json');

function loadConfig() {
    try {
        const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
        const cfg = JSON.parse(raw);

        if (!cfg.server_ip || !cfg.server_port || !cfg.player_id || !cfg.player_token) {
            throw new Error('Missing one of server_ip/server_port/player_id/player_token in rust_config.json');
        }

        cfg.server_port = Number(cfg.server_port);
        if (Number.isNaN(cfg.server_port)) {
            throw new Error('server_port is not a valid number');
        }

        cfg.entities = cfg.entities || {};
        return cfg;
    } catch (err) {
        console.error('❌ Failed to load rust_config.json:', err.message);
        process.exit(1);
    }
}

let cfg = loadConfig();
console.log("Loaded config file from:", CONFIG_PATH);
console.log("Config contents:", cfg);

// ---------- CREATE RUST+ CLIENT ----------

// useFacepunchProxy = true (we are using Facepunch’s proxy, not direct game port)
const rustplus = new RustPlus(
    cfg.server_ip,
    Number(cfg.server_port),
    cfg.player_id,
    cfg.player_token,
    false // ✅ direct websocket to server, no Facepunch proxy
);

// ---------- EVENT HOOKS ----------

rustplus.on('connecting', () => {
    console.log(`[${new Date().toISOString()}] 🔌 Connecting to Rust+ server ${cfg.server_ip}:${cfg.server_port}...`);
});

rustplus.on('connected', () => {
    console.log(`[${new Date().toISOString()}] ✅ Connected to Rust+!`);
    // NOTE:
    // We are NOT calling getInfo/getTime here because the older
    // rustplus.proto requires 'queuedPlayers', which this server
    // no longer sends, causing a ProtocolError crash.
});

rustplus.on('message', (msg) => {
    // All messages from Rust+ (alerts, etc.) land here
    console.log(`[${new Date().toISOString()}] 📩 Rust+ message received`);
    // If this gets too spammy we can filter later.
});

rustplus.on('disconnected', () => {
    console.log(`[${new Date().toISOString()}] ❌ Disconnected from Rust+`);
});

rustplus.on('error', (err) => {
    console.error(`[${new Date().toISOString()}] 💥 Rust+ error:`, err);
});

// ---------- HTTP API ----------

const app = express();
app.use(express.json());

const HTTP_PORT = 3000;

/**
 * Helper: get entityId from config name.
 */
function getEntityId(name) {
    const entities = cfg.entities || {};
    if (!Object.prototype.hasOwnProperty.call(entities, name)) {
        return { ok: false, error: `Unknown entity name '${name}'. Check rust_config.json -> entities.` };
    }
    const id = entities[name];
    if (!id || id === 0) {
        return { ok: false, error: `Entity '${name}' has id=0. Update rust_config.json with a real entity ID.` };
    }
    return { ok: true, id };
}

/**
 * Health check endpoint
 */
app.get('/health', (req, res) => {
    res.json({
        ok: true,
        connected: rustplus.isConnected && rustplus.isConnected(),
        server_ip: cfg.server_ip,
        server_port: cfg.server_port
    });
});

/**
 * Generic entity control endpoint:
 *  POST /api/entity/:name/on
 *  POST /api/entity/:name/off
 *  GET  /api/entity/:name/status
 */
app.post('/api/entity/:name/:action', (req, res) => {
    const { name, action } = req.params;

    if (!rustplus.isConnected || !rustplus.isConnected()) {
        return res.status(503).json({ ok: false, error: 'Rust+ is not connected to the server.' });
    }

    const result = getEntityId(name);
    if (!result.ok) {
        return res.status(400).json({ ok: false, error: result.error });
    }

    const entityId = result.id;

    if (action === 'on') {
        console.log(`[${new Date().toISOString()}] 🔼 Turning ON ${name} (entityId=${entityId})`);
        rustplus.turnSmartSwitchOn(entityId, (msg) => {
            if (msg?.response?.error) {
                console.error('Rust+ error in turnSmartSwitchOn:', msg.response.error);
                return res.status(500).json({ ok: false, error: msg.response.error.error || 'Rust+ error' });
            }
            return res.json({ ok: true, message: `${name} turned ON`, raw: msg });
        });
    } else if (action === 'off') {
        console.log(`[${new Date().toISOString()}] 🔽 Turning OFF ${name} (entityId=${entityId})`);
        rustplus.turnSmartSwitchOff(entityId, (msg) => {
            if (msg?.response?.error) {
                console.error('Rust+ error in turnSmartSwitchOff:', msg.response.error);
                return res.status(500).json({ ok: false, error: msg.response.error.error || 'Rust+ error' });
            }
            return res.json({ ok: true, message: `${name} turned OFF`, raw: msg });
        });
    } else {
        return res.status(400).json({ ok: false, error: `Unknown action '${action}'. Use 'on' or 'off'.` });
    }
});

// Common Rust resource item IDs (TC upkeep stuff)
const RESOURCE_IDS = {
    wood: 69511070,
    stone: -2099697608,
    metal_fragments: 317398316,
    hqm: -151838493,
};


app.get('/api/entity/:name/status', (req, res) => {
    const { name } = req.params;

    if (!rustplus.isConnected || !rustplus.isConnected()) {
        return res.status(503).json({ ok: false, error: 'Rust+ is not connected to the server.' });
    }

    const result = getEntityId(name);
    if (!result.ok) {
        return res.status(400).json({ ok: false, error: result.error });
    }

    const entityId = result.id;

    console.log(`[${new Date().toISOString()}] 🔎 Getting status for ${name} (entityId=${entityId})`);
    rustplus.getEntityInfo(entityId, (msg) => {
        if (msg?.response?.error) {
            console.error('Rust+ error in getEntityInfo:', msg.response.error);
            return res.status(500).json({ ok: false, error: msg.response.error.error || 'Rust+ error' });
        }

        const info = msg?.response?.entityInfo || msg;
        return res.json({ ok: true, name, entityId, info });
    });
});

/**
 * TC summary endpoint:
 *   GET /api/tc/:name
 * Example:
 *   /api/tc/tc_main
 */
app.get('/api/tc/:name', (req, res) => {
    const { name } = req.params;

    if (!rustplus.isConnected || !rustplus.isConnected()) {
        return res.status(503).json({ ok: false, error: 'Rust+ is not connected to the server.' });
    }

    const result = getEntityId(name);
    if (!result.ok) {
        return res.status(400).json({ ok: false, error: result.error });
    }

    const entityId = result.id;
    console.log(`[${new Date().toISOString()}] 🧾 Getting TC summary for ${name} (entityId=${entityId})`);

    rustplus.getEntityInfo(entityId, (msg) => {
        // Error from Rust+
        if (msg?.response?.error) {
            console.error('Rust+ error in getEntityInfo (TC):', msg.response.error);
            return res.status(500).json({ ok: false, error: msg.response.error.error || 'Rust+ error' });
        }

        const info = msg?.response?.entityInfo || msg;
        const payload = info?.payload || {};

        // Safety guard
        if (!payload.items || !Array.isArray(payload.items)) {
            return res.json({
                ok: true,
                name,
                entityId,
                items: [],
                resources: {
                    wood: 0,
                    stone: 0,
                    metal_fragments: 0,
                    hqm: 0,
                },
                upkeep: {
                    hasProtection: payload.hasProtection ?? null,
                    protectionExpiry: payload.protectionExpiry ?? null,
                    hours_remaining: null,
                },
                raw: info,
            });
        }

        // -------- Aggregate resources --------
        let wood = 0;
        let stone = 0;
        let metal_fragments = 0;
        let hqm = 0;

        const items = payload.items.map((it) => {
            const itemId = it.itemId;
            const quantity = it.quantity ?? 0;

            if (itemId === RESOURCE_IDS.wood) {
                wood += quantity;
            } else if (itemId === RESOURCE_IDS.stone) {
                stone += quantity;
            } else if (itemId === RESOURCE_IDS.metal_fragments) {
                metal_fragments += quantity;
            } else if (itemId === RESOURCE_IDS.hqm) {
                hqm += quantity;
            }

            return {
                slot: it.slot ?? null,
                itemId,
                quantity,
                name: null, // can fill in later if we add an itemId -> name map
            };
        });

        // -------- Upkeep time remaining --------
        const nowSeconds = Math.floor(Date.now() / 1000);
        let hoursRemaining = null;

        if (typeof payload.protectionExpiry === 'number') {
            const diffSeconds = payload.protectionExpiry - nowSeconds;
            hoursRemaining = Math.max(0, diffSeconds / 3600);
        }

        return res.json({
            ok: true,
            name,
            entityId,
            items,
            resources: {
                wood,
                stone,
                metal_fragments,
                hqm,
            },
            upkeep: {
                hasProtection: !!payload.hasProtection,
                protectionExpiry: payload.protectionExpiry ?? null,
                hours_remaining: hoursRemaining !== null
                    ? Number(hoursRemaining.toFixed(2))
                    : null,
            },
            raw: info, // keep raw for debugging / future features
        });
    });
});

// ---------- STARTUP ----------

console.log('🚀 Project Sisyphean Rust+ service starting...');
rustplus.connect();

app.listen(HTTP_PORT, () => {
    console.log(`🌐 HTTP API listening on http://localhost:${HTTP_PORT}`);
});

// Graceful shutdown
function shutdown() {
    console.log('👋 Shutting down Rust+ service...');
    try {
        rustplus.disconnect();
    } catch (e) {
        // ignore
    }
    process.exit(0);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
